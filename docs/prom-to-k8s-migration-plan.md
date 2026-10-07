# KubeDoor 去 Prometheus 实时依赖改造方案

> 状态：方案待评审（计划阶段，未改任何代码）
> 更新（2026-10）：部署已移除 TimescaleDB，三张大表改为普通表，过期数据由 master 定时清理（`func_manager/data_retention.py`）。下文 2.3 的「现状」已过时；第六节的增强方案如要实施，需先重新引入 TimescaleDB。
> 目标：把"本该实时"的数据从绕行 Prometheus/VictoriaMetrics 改为 **agent 直取 K8S + master 缓存 + web 读 master**；保留 Grafana 看板与历史数据查询继续走 Prometheus；同时挖掘 PG/TimescaleDB 时序能力。

---

## 一、背景与目标

当前 KubeDoor 大量"实时数据"是 master 通过 `PROM_URL` 打 Prometheus/VM 的 `/api/v1/query` 拿到的。问题：

- **不实时**：kube-state-metrics 抓取间隔 + PromQL 计算 + 网络往返，通常滞后 15~60s。
- 讽刺的是 `get_prom_url()`（`utils.py:112`）里 `query_range` 分支已全部注释，实际只用瞬时 `query`，所以"时序库"能力其实没用在实时展示上，反而把实时数据绕慢了。

**目标拆分：**
1. 实时展示类（节点排名、工作负载监控、元数据、镜像）→ 改为 agent 直取 K8S（含 metrics-server），master 缓存，web 读 master。
2. CPU/内存实时使用率等用量指标 → 用 metrics-server 直取（接受无分位数/无历史）。
3. Grafana 看板、历史数据查询 → **保留走 Prometheus 不动**。
4. PG/TimescaleDB → 补压缩策略、建连续聚合加速现有查询、新建实时指标 hypertable。

---

## 二、现状调研

### 2.1 Prometheus / VM 使用点全清单（全部在 kubedoor-master）

> agent / alarm / AI 里的 `PROM_*` 只是环境标签 `PROM_K8S_TAG_KEY/VALUE`，**不发起时序查询**。web 前端不直连 Prometheus，全走 master 的 `/api/prom_*`。

| 模块 | 位置 | 数据 | 调用链 | 实时性 | 处置 |
|---|---|---|---|---|---|
| **A 集群概览大盘** | `func_manager/prom_overview.py:8,135`（24 条 PromQL） | 集群/节点/工作负载/Pod/PVC 数、CPU/内存总量与用量、网络速率、节点水位超阈值计数、各类 TOP1 水位 | web `overview.ts:185 getPromOverview` → `/api/prom_overview`（`kubedoor-master.py:574`） | 高 | 用户未选改造，暂保留 |
| **B 节点资源排名/分布** | `promql.py:135 node_rank_query`、`utils.py:822 _get_node_res_rank_sync`；`promql.py:229 deployment_node`、`utils.py:759`；`utils.py:276 get_node_deployments`（`promql.py:131`） | 节点实时 CPU/内存使用率、pod 数排名；deployment 在各节点分布；节点上 deployment 列表 | web `monit.ts:197 getPromNodeRank` → `/api/prom_node_rank`（`kubedoor-master.py:543`）；`/api/balance_node` | 高 | **改造** |
| **C 实时工作负载监控** | `prom_real_time_data.py:11,46`（9 条 PromQL） | 每 deployment 的 pod 数、CPU 使用(avg/max)、CPU req/limit、内存 wss(avg/max)、内存 req/limit、镜像 | web `monit.ts:63 getPromQueryData` → `/api/prom_query`（`kubedoor-master.py:502`）；AI / MCP `src/kubedoor-ai/kubedoor_ai/gateway.py` 的 `deployment_metrics` 目录工具 | 高 | **改造** |
| **D 元数据下拉** | `utils.py:160 fetch_prom_envs`、`utils.py:142 fetch_prom_services`、`utils.py:125 fetch_prom_namespaces` | 环境(集群)/服务/命名空间列表 | `monit.ts:19 getPromEnv`→`/api/prom_env`；`monit.ts:53 getPromServices`→`/api/prom_services`；`/api/prom_ns` | 中 | **改造**（namespace 已走 agent） |
| **E 镜像查询** | `promql.py:190/208 deployment_image*`、`utils.py:789` | deployment 当前运行镜像 | `image_tags_fetcher.py:19` | 中 | **改造** |
| **F 高峰期采集** | `promql.py:1 query_dict`、`utils.py:176 get_prom_data`/`222 merged_dict` | 高峰期时段 P80 分位 CPU/内存/负载 | `/api/init_peak_data`、`/api/cron_peak_data` → 落库 `k8s_resources` | 低(离线) | **不改**（时序聚合，Prometheus 合理） |

关键点：`get_prom_url()`（`utils.py:112-122`）只用瞬时 `query`；`query_range` 分支已注释。

### 2.2 数据来源分层现状

- **Prometheus/VM**：上表 A~F。
- **PostgreSQL（原 ClickHouse+MySQL 迁移）**：管控表 `k8s_res_control`、资源表 `k8s_resources`、告警 `k8s_pod_alert_days`、事件 `k8s_events`、Istio 三表等；REST 走 `func_manager/db_api.py`（`/api/db/*`）与 `func_manager/top_queries.py`（`/api/stats/*`）。
- **K8S（经 agent WS）**：`/api/agent/*`、`/api/nodes/list`、`/api/scale` 等，agent 用 kubernetes-client 直查。

### 2.3 PG / TimescaleDB 现状（`db.sql`）

- 3 张 hypertable：`k8s_resources`（date/1月，含列压缩+365天保留）、`k8s_pod_alert_days`（start_time/7天，365天保留）、`k8s_events`（lasttimestamp/1月，90天保留）。
- **已用**：hypertable 自动分区、retention(TTL)、`k8s_resources` 列压缩。
- **未用（增强空间）**：
  - ❌ 无 Continuous Aggregate / `time_bucket`：TOP10、每日统计、"最近10天负载最高日"都是查询时实时 SQL 聚合（`func_manager/top_queries.py`、`utils.py`）。
  - ❌ `k8s_events`、`k8s_pod_alert_days` 未配压缩。
  - ⚠️ 落库用瞬时 `query` 非 `query_range`，每天高峰期只取 1 个采样点。

---

## 三、关键发现：agent 侧能力已基本就绪（改造成本低）

调研 kubedoor-agent 后确认，改造所需能力**多数已存在**，主要是"重组/聚合"而非"从零写"：

1. **metrics-server 全链路已通**：`custom_api = client.CustomObjectsApi()`（`kubedoor-agent.py:73`，注释即"用于访问Metrics API"）已在 5 个文件复用：
   - `pod_manager.py:33/40/53`（pod 用量）、`node_manager.py:52`（节点用量）、`stateful_daemon_manager.py:217`、`mcp_service.py:294/326`、`load_balancer.py:88/181/1013`。
   - 单位换算工具齐全：`utils.parse_cpu`（→mCPU，处理 n/m）、`parse_memory`（→MB，处理 Ki/Mi/Gi）、`parse_pods`、`parse_storage_to_gib`、`bytes_to_gib`。
2. **节点维度数据完整**：`node_manager.get_nodes_list`（`/api/nodes/list`）已返回 allocatable/capacity 的 cpu/mem/pods、实时 usage、当前 pod 数、磁盘用量；`load_balancer.get_all_nodes_cpu`（`/api/load-balance/nodes-cpu`）返回每节点 cpu%/mem%/pod数。
3. **workload 声明量聚合样板已存在**：`stateful_daemon_manager.get_statefulset_list`（`:42-81`）已逐容器聚合 request/limit + 副本数，可原样移植到 Deployment。
4. **pod 明细已含实时用量**：`pod_manager.get_pod_list`（`/api/agent/pods`）Pod 列表与 metrics 用 `asyncio.gather` 并行，返回 `current_cpu_cores/current_memory_mb`。
5. **WS→HTTP 回环分发成熟**：master `http_handler`（`kubedoor-master.py:295-485`）把请求打包成 WS message 发 agent；agent `process_request`（`kubedoor-agent.py:132`）转成本地 HTTP 回环调用自身 handler，回传 response。**新增只读 GET 接口零协议改动即可被 master 调用。**

**主要缺口（改造需补）：**
1. `pod_manager.get_pod_list` 有实时用量但**无 request/limit、无 image**。
2. **无 Deployment 级聚合列表接口**（STS/DS 有 list，Deployment 无 `get_deployment_list`）——模块 C 需新建。
3. metrics-server 仅瞬时值，无历史/峰值 —— 模块 F 高峰期采集不可用它替代。

### master 缓存范式（可复用）

- `namespace_cache.py`：TTL 内存缓存（3600s），`get_namespaces_from_cache` / `update_namespace_cache`；`http_handler` 里"命中直返 / 响应回来时更新"（`kubedoor-master.py:387,469`）。
- `admis` 范式（`utils.py:29-42`）：`dict + threading.Lock + TTL`（`ADMIS_CACHE_TTL` 默认 60s）+ **写表时主动失效** `invalidate_admis_cache()`。适合有写操作需即时生效的缓存。

---

## 四、用户决策（已确认）

| 决策项 | 选择 |
|---|---|
| 改造为 agent 直取+缓存的模块 | **B 节点排名/分布、C 实时工作负载监控、D+E 元数据/镜像** |
| CPU/内存实时使用率 | **用 metrics-server 直取**（接受无分位数/无历史） |
| 保留 Prometheus | **Grafana 看板（monitk8s/monitnode/statistics 三个 iframe 页 `/grafana/d/...`）+ 历史数据查询** |
| PG/TimescaleDB 增强 | **补压缩策略 + 连续聚合加速现有查询 + 新建实时指标 hypertable** |
| 模块 A 概览大盘 | 未选改造，暂保留走 Prometheus |
| 模块 F 高峰期采集 | 不改（离线时序聚合） |

---

## 五、改造方案

### 5.1 模块 D+E（元数据/镜像）—— 最简单，先做

| 现状（Prometheus） | 改造为（agent 直取 K8S） |
|---|---|
| `fetch_prom_envs`（`kube_node_info`） | 环境=集群列表本质是"已连接的 agent 列表"，master 侧已有 `clients` 在线表 / `agent_names`；可直接用在线 agent 列表，不必查 K8S |
| `fetch_prom_services`（`kube_service_info`） | agent 已有 `/api/agent/services`（`service_manager.get_service_list`），改 web 调它 |
| `fetch_prom_namespaces`（`kube_namespace_created`） | 已走 `/api/agent/namespaces` + `namespace_cache`；把残留的 `/api/prom_ns` 调用点切过去 |
| `deployment_image*`（`kube_pod_container_info`） | agent 查 Deployment/Pod spec 的 `containers[].image` 直取，更准 |

落地：优先改 web 调用点指向已有 agent 接口；`/api/prom_env`、`/api/prom_services`、`/api/prom_ns` 逐步废弃或内部改为转发 agent。

### 5.2 模块 B（节点资源排名/分布）

- **节点 CPU/内存使用率排名**：复用 agent `/api/load-balance/nodes-cpu`（`get_all_nodes_cpu` 已返回每节点 cpu%/mem%/pod数）或 `/api/nodes/list`。master 新增/改造 `/api/prom_node_rank` 改为转发 agent + 排序，结果按 TTL 缓存。
- **deployment 节点分布**：agent 侧按 `pod.spec.node_name` 对某 deployment 的 pod 计数（新增小接口或复用 pod_manager 带 `node_name`/owner 过滤）。
- master 侧仍可叠加 PG 管控表数据（`get_deployment_from_control_data` 走 `k8s_res_control`），即"K8S 实时 + PG 管控"混合。

### 5.3 模块 C（实时工作负载监控）—— 工作量最大

需要 agent 新增 **Deployment 级聚合接口** `GET /api/agent/deployments`：
- 用 `apps_v1.list_deployment_for_all_namespaces` / `list_namespaced_deployment` 取副本数、`spec.template.containers[].resources` 聚合 request/limit、image；
- 用 metrics-server（`list_*_custom_object` pods）+ owner 归并到 deployment，得实时 CPU/内存用量；
- 参照 `stateful_daemon_manager.get_statefulset_list`（`:42-81`）的聚合写法。
- 返回字段对齐现有 `prom_real_time_data.process_metrics_data` 的列，减少前端改动。

master 侧 `/api/prom_query` 改为转发该 agent 接口 + TTL 缓存；web `getPromQueryData` 保持签名不变，仅数据源切换。

### 5.4 metrics-server 用量指标说明

- 用量=瞬时值，语义与 Prometheus `irate/quantile_over_time` 不同（无分位数、无历史窗口）。前端展示需相应调整文案（"实时"而非"P80/P95"）。
- metrics-server 未装的集群要降级（现有代码已 `try/except` 返回 0，保留该容错）。

### 5.5 缓存层设计

- 新建通用 TTL 缓存模块（或扩展 `namespace_cache` 为通用 `resource_cache`），key=`{env}:{resource}:{参数}`。
- 读路径：命中未过期直返；否则转发 agent、写缓存、返回。
- 写路径（scale/restart/更新镜像等）后**主动失效**对应 env 的缓存（参考 `invalidate_admis_cache`）。
- TTL 建议：节点/工作负载用量 15~30s（实时性优先），元数据 300s，镜像 60s；均设环境变量可调。

---

## 六、PG / TimescaleDB 增强方案

### 6.1 补压缩策略（低风险，先做）
给 `k8s_events`、`k8s_pod_alert_days` 增加 `ALTER TABLE ... SET(timescaledb.compress, ...)` + `add_compression_policy`，segmentby 选高基数低区分列（如 `k8s,namespace` / `env,namespace`），orderby 时间倒序。

### 6.2 连续聚合加速现有查询
把以下实时 SQL 聚合改为 Continuous Aggregate（`time_bucket` + 物化 + 刷新策略）：
- `func_manager/top_queries.py` 的 TOP10 事件 / TOP10 pod 告警 / 每日告警统计；
- `utils.py:551 get_list_from_resources` 的"最近 N 天负载最高日"。
好处：查询从全表实时聚合变增量物化读取，提速数量级，数据源不变。

### 6.3 新建实时指标 hypertable（配合模块 C，可选/后续）
- agent 定时（如 15~60s）采样 metrics-server + K8S，master 写入新 hypertable（如 `k8s_pod_metrics_rt`）。
- 好处：PG 自持时序，可做降采样与短期历史，逐步减少对 Prometheus 的实时依赖。
- 成本：需设计采样频率、表结构、保留期、写入压力评估 —— 建议放在 B/C 改造稳定后再做。

---

## 七、保留不动（明确边界）

- Grafana iframe 三页：`views/monitk8s/index.vue`（`/grafana/d/KubeDoor-K8S`）、`views/monitnode/index.vue`（`/grafana/d/KubeDoor-Node`）、`views/statistics/index.vue`（`/grafana/d/KubeDoor`）。
- 模块 A 概览大盘（`prom_overview.py`）暂留。
- 模块 F 高峰期采集（`init_peak_data` → `k8s_resources`）。
- 因此 `PROM_URL` 依赖**不可完全移除**。

---

## 八、风险与注意事项

1. **语义差异**：metrics-server 瞬时值 vs Prometheus 分位数/速率。前端文案与告警阈值需复核。
2. **大集群性能**：全命名空间 `list_pod` + metrics 在超大集群较重；靠 master 缓存 + agent 内并行（`asyncio.gather`）缓解；必要时按 namespace 分页。
3. **metrics-server 依赖**：未装则用量为 0，需保留降级与 UI 提示。
4. **缓存一致性**：写操作后主动失效，避免 UI 改配置后仍读旧缓存。
5. **接口兼容**：尽量保持 web api 函数签名与返回结构不变，数据源在 master/agent 内部切换，减少前端改动面。
6. **命名遗留**：`ck_*` 接口/文件名是历史遗留，数据源已是 PG，不要被名字误导。

---

## 九、实施步骤（建议分阶段）

**阶段 0**：补 PG 压缩策略（6.1）——独立、低风险，可先落地。
**阶段 1**：模块 D+E——多数改 web 调用点指向已有 agent 接口。
**阶段 2**：建 master 通用 TTL 缓存层（5.5）。
**阶段 3**：模块 B——节点排名/分布转发 agent + 缓存。
**阶段 4**：模块 C——agent 新增 `/api/agent/deployments` 聚合接口，master `/api/prom_query` 切源 + 缓存。
**阶段 5**：连续聚合加速（6.2）。
**阶段 6（可选）**：实时指标 hypertable（6.3）。

每阶段完成后跑 `python -m py_compile` / `pnpm typecheck` / `pnpm lint` 校验，并按 CLAUDE.md 用 `type(scope): summary` 提交。

---

## 附：相关文件索引

- Prometheus 调用：`src/kubedoor-master/promql.py`、`prom_real_time_data.py`、`func_manager/prom_overview.py`、`utils.py`（`get_prom_url/get_prom_data/fetch_prom_*/get_node_res_rank/get_deployment_node`）
- agent 可复用样板：`res_manager/pod_manager.py`、`node_manager.py`、`stateful_daemon_manager.py`、`load_balance/load_balancer.py`、`func_manager/mcp_service.py`
- 缓存范式：`func_manager/namespace_cache.py`、`utils.py:29-42`
- WS 转发：master `kubedoor-master.py:295-485`、agent `kubedoor-agent.py:88-158`
- PG/时序：`db.sql`、`db.py`、`func_manager/db_api.py`、`func_manager/top_queries.py`
