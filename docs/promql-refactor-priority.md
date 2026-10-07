# PromQL 接口改造合理性分析与优先级

> 起因：评审"去 Prometheus 实时依赖"方案时提出两个关键质疑——
> 1. 概览大盘定时刷新，若改 agent 直取会否占用大量业务集群资源？
> 2. 使用率类指标取的是 `irate[3m]` 时间窗平均，比瞬时值更有意义，metrics-server 能否等价替代？
>
> 结论：两个质疑都成立。据此**重新评估**每个 PromQL 接口是否值得改，取代之前偏乐观的初版方案。

---

## 一、两条核心判据

### 判据 A：是否"全集群聚合 + 高频刷新"

- Prometheus 是**独立监控系统**，已从 kube-state-metrics/cadvisor 抓走数据，查询在监控侧消化，**不碰业务集群 apiserver**。
- 若改 agent 直取，等价于让 agent 每次 `list_cluster_custom_object(metrics.k8s.io, pods/nodes)` + `list pod/node`，**直接压业务集群的 kube-apiserver 和 metrics-server**。
- 「全集群聚合 + 高频刷新 + 多用户并发」会把本该监控系统扛的压力转嫁给每个业务集群。**这类必须留 Prometheus。**

### 判据 B：指标语义是否需要"时间窗 / 分位数 / 历史"

CPU 是累积计数器，必须在时间窗上算速率才有"使用率"意义。核对各接口时间窗语义：

| 接口 | 指标 | PromQL 时间窗语义 |
|---|---|---|
| prom_overview | CPU 用量、网络速率 | `irate(...[3m])` 最近 3 分钟速率 |
| prom_overview | 节点 CPU/内存水位 TOP1 | `irate[3m]` / working_set |
| prom_node_rank | 节点 CPU 使用率排名 | `irate(...[3m])` |
| prom_query | deployment CPU avg/max | `rate(...[2m])` 最近 2 分钟速率 |
| 高峰期采集 | P80 分位 | `quantile_over_time(0.80, ...[duration])` |

**metrics-server 能给什么**：它返回的 CPU 也不是纯瞬时点，是 cadvisor 在固定短窗口（约 15~30s）算出的速率。但硬限制：
- ❌ 不能自定义时间窗（拿不到 3m 平均，只能固定短窗，抖动更大）
- ❌ 不能算分位数（P80/P95/P99 完全做不到）
- ❌ 不能回溯历史（只有"现在"一个值）
- ❌ 不能 max_over_time / min_over_time / quantile_over_time

→ 用 metrics-server 替代 `irate[3m]`/`quantile_over_time` = 用更粗糙、抖动更大、无分位、无历史的值，换"实时性 + 不依赖 Prometheus"。**是否划算取决于具体接口的用途。**

---

## 二、逐接口评估结论

| 接口 | 用途 | 触发方式 | 判据A(集群压力) | 判据B(语义) | 结论 | 优先级 |
|---|---|---|---|---|---|---|
| **prom_env** | 环境(集群)列表 | 页面加载/低频 | 无（非用量） | 无（只是列表） | ✅ **改** | **高** |
| **prom_services** | 服务列表 | istio 页/低频 | 无（非用量） | 无（只是列表） | ✅ **改** | **高** |
| **get_deployment_image** | deployment 当前镜像 | 更新镜像前/低频 | 无 | 无（K8S 更准） | ✅ **改** | 中 |
| **prom_query** | 工作负载实时表格 | monit 主页/用户触发 | 单 namespace，可控 | ⚠️ CPU 从 rate[2m] 降为 metrics-server 短窗 | ⚠️ **改**（接受语义变化） | 中 |
| **prom_node_rank** | 节点 CPU/内存排名 | 扩缩容选节点复用 | 单集群节点级，可控 | ❌ 排名需稳定值，metrics-server 抖动会选错节点 | ⛔ **暂不改** | — |
| **prom_overview** | 集群概览大盘 | **默认 30s 自动刷新** | ❌❌ 全集群聚合×高频×多用户，直接压 apiserver | ❌ 24 条含 irate[3m]/分位/topk | ⛔ **不改** | — |
| **高峰期采集** | P80 分位落库 | 离线定时 | 离线，无所谓 | ❌ 分位数只有时序库能算 | ⛔ **不改** | — |

---

## 三、建议改造清单（按优先级，供拍板）

### 🟢 P1：元数据类——零集群压力、无语义损失、收益明确

> **✅ 已实现（2026-07-09）**。改动仅在 master，前端零改动（返回结构不变）。
> - `prom_env_handler`（kubedoor-master.py）：改为取内存 `clients` 里在线的 env 列表并排序，不再查 Prometheus；保留 username/permission（顺带修了原异常分支 username 未定义的 bug）。
> - `prom_services_handler`：新增内部辅助函数 `call_agent_api(env, path, query, ...)`（复用 WS send+Event 等待机制），经 agent `/api/agent/services` 取数，拍平为去重排序的 service 名字数组返回。
> - `utils.fetch_prom_envs` / `fetch_prom_services` 变为无调用方的 dead code（保留未删，超本次范围）。

**1. `prom_env`（环境/集群列表）**
- 现状：`utils.fetch_prom_envs`（utils.py:160）查 `group by(tag)(kube_node_info)`。
- 改造：master 侧已有在线 agent 列表 `clients` / `agent_names`（`SELECT env FROM k8s_agent_status`）。**环境列表本质就是已连接的 agent 列表，直接用，甚至不用发请求到 agent。**
- 成本：极低（改 handler 数据源即可）。风险：几乎无。

**2. `prom_services`（服务列表）**
- 现状：`utils.fetch_prom_services`（utils.py:142）查 `kube_service_info`。
- 改造：agent 已有 `/api/agent/services`（`service_manager.get_service_list`）。改 web 调用点或 master handler 转发到它。
- 成本：低。风险：几乎无。

### 🟡 P2：镜像查询——K8S 更准，中等收益

**3. `get_deployment_image`（deployment 当前镜像）**
- 现状：`promql.py:190/208` 查 `kube_pod_container_info`（有抓取延迟）。用于 `image_tags_fetcher` 更新镜像前取当前镜像。
- 改造：agent 查 Deployment/Pod spec 的 `containers[].image` 直取，比 Prometheus 准且实时。
- 成本：中（agent 侧加小接口或复用现有）。风险：低。

### 🟠 P3：工作负载监控——收益大但有语义 trade-off，需你确认能接受

**4. `prom_query`（monit 主页工作负载实时表格）**
- 现状：`prom_real_time_data.py` 9 条 PromQL，CPU 用 `rate[2m]`。
- 改造：agent 新增 Deployment 聚合接口（副本/req/limit/镜像从 spec 直取——准确；CPU/内存用量从 metrics-server）。工作量最大（agent 无现成 Deployment 级 list）。
- **语义变化（务必知悉）**：
  - `avg_cpu_usage`：rate[2m] 平均 → metrics-server 短窗速率（抖动更大）
  - `max_cpu_usage`：现在是"2m 窗口内 max 速率" → metrics-server 只能给"当前各 pod 用量的 max"，**语义不同**
  - req/limit/pod数/镜像：**改后反而更准**（直接读 spec，不受抓取延迟）
- 成本：高。风险：中（数字与旧值对不上，需前端文案说明"实时值"）。

### ⛔ 不建议改（保留 Prometheus）

- **`prom_overview`（概览大盘）**：30s 高频 × 全集群聚合，改了会压垮各业务集群 apiserver。这正是 Prometheus 主场。
- **`prom_node_rank`（节点排名）**：被扩缩容选节点复用，需要 irate[3m] 的稳定负载值；metrics-server 瞬时抖动会导致选错节点。
- **高峰期采集**：P80 分位 + 离线，只有时序库能算。

---

## 四、如果坚持要改 overview/node_rank——替代路径（备选，不推荐现在做）

若未来确实想让概览/排名也摆脱 Prometheus，正确姿势**不是**每次 agent 直取，而是：

- **agent 定时采样 + master 落 PG 时序表**：agent 每 N 秒采一次集群级聚合（node/pod metrics 汇总），推给 master 写入新 hypertable（如 `k8s_cluster_metrics_rt`）。web 读 PG，不直接压集群。
- 这样把"高频查询压力"从"每次打业务集群"变成"agent 固定频率采样一次 + 大家读 PG"，且 PG 能存短期历史、做 `time_bucket` 降采样，逐步逼近 Prometheus 的能力。
- 成本高，属于二期。建议先做 P1~P3，观察效果再定。

---

## 五、与既有方案文档的关系

- 本文是对 [prom-to-k8s-migration-plan.md](./prom-to-k8s-migration-plan.md) 中"改造范围"的**收敛修正**：
  - 初版把模块 B（node_rank）、C（prom_query）都列入改造；现修正为 **node_rank 暂不改**、**prom_query 需确认语义 trade-off**。
  - 明确 **prom_overview 不改**（初版为"暂留"，现给出明确理由）。
- 接口来源全貌见 [api-data-source-mapping.md](./api-data-source-mapping.md)。
