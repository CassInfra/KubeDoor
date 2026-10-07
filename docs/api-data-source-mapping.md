# KubeDoor 前端接口数据来源对照表

> 说明：本表梳理 kubedoor-web 前端调用的所有 kubedoor-master 后端接口，并标注每个接口返回数据的**真实来源**。
> 用于评估"去 Prometheus 实时依赖"改造范围。配套改造方案见 [prom-to-k8s-migration-plan.md](./prom-to-k8s-migration-plan.md)。

## 关键机制：兜底转发路由

master 在 `kubedoor-master.py:1115` 注册了通配路由：

```python
app.router.add_route('*', "/api/{tail:.*}", http_handler)
```

**凡是没有在 `kubedoor-master.py:1053-1111` 显式注册的 `/api/*` 路径，全部落到 `http_handler`（`kubedoor-master.py:295-485`），通过 WebSocket 转发给对应 env 的 agent，由 agent 用 kubernetes-client / metrics-server 查 K8S。** 这是判定数据源的核心规则。

数据源判定：
- **显式注册**的 `prom_*` handler → Prometheus/VM
- **显式注册**的 `/api/db/*`、`/api/istio/*`(除 agent)、`ck_*`、`/api/events/*`、`agent_names` → PostgreSQL
- **未显式注册**（走兜底）或 `/api/agent/*`、`/api/nodes/*`、`/api/pod/*`、`/api/scale` 等 → K8S(经 agent)

---

## 一、数据来源汇总统计

| 数据来源 | 接口数 | 占比 |
|---|---:|---:|
| **Prometheus / VictoriaMetrics** | 5 | 6% |
| **PostgreSQL** | 33 | 37% |
| **K8S（经 agent）** | 45 | 50% |
| **混合（多源）** | 7 | 8% |
| **内存 / 本地文件** | 4 | 4% |
| 合计（去重后独立 master 接口） | **90** | — |

> `/api/pod/modify_pod` 与 `/api/scale` 平时纯 K8S 写操作，仅"固定节点均衡模式"(`add_label=true`)时才附带查 Prometheus，故同时计入 K8S 与混合，去重后合计 90。

---

## 二、Prometheus / VM 来源（5 个）★ 改造重点

| 前端函数 | HTTP 接口 | 调用页面 | master handler (文件:行号) | 数据说明 | 改造归属 |
|---|---|---|---|---|---|
| `getPromEnv` | GET `/api/prom_env` | monit/overview | `prom_env_handler` kubedoor-master.py:533 → `utils.fetch_prom_envs` utils.py:160 | `group by(tag)(kube_node_info)` 查集群列表 | 模块 D |
| `getPromServices` | GET `/api/prom_services` | istio/level1-vs (index/detail) | `prom_services_handler` kubedoor-master.py:521 → `utils.fetch_prom_services` utils.py:142 | `kube_service_info` 查 service 列表 | 模块 D |
| `getPromQueryData` | GET `/api/prom_query` | monit/index | `prom_query_handler` kubedoor-master.py:502 → `prom_real_time_data.get_metrics_data` prom_real_time_data.py:46 | 每 deployment 实时 pod数/CPU/内存/req/limit/镜像（9 条 PromQL） | 模块 C |
| `getNodeResourceRank` | GET `/api/prom_node_rank` | monit/index、monit/pod、monit/scale | `prom_node_rank_handler` kubedoor-master.py:543 → `utils.get_node_res_rank` utils.py:843 + `utils.get_deployment_node` utils.py:783 | 节点 CPU/内存使用率排名 + deployment 节点分布 | 模块 B |
| `getPromOverview` | GET `/api/prom_overview` | monit/overview | `prom_overview_handler` kubedoor-master.py:574 → `prom_overview.get_overview_counts_async` func_manager/prom_overview.py:135 | 集群总览大盘 24 条 PromQL（节点/pod/PVC/水位等） | 模块 A（暂留） |

> 另：`/api/prom_ns`（`prom_ns_handler` kubedoor-master.py:510）后端仍注册但**前端已不调用**（命名空间改走 `/api/agent/namespaces`）。

---

## 三、PostgreSQL 来源（33 个）

### 3.1 `/api/db/*` 参数化接口（func_manager/db_api.py，全部 asyncpg）

| 前端函数 | HTTP 接口 | 调用页面 | handler (db_api.py) | 数据说明 |
|---|---|---|---|---|
| `getEnv`(alarm) | GET `/api/db/alert/envs` | alarm | db_api.py:245 | 告警表 distinct env |
| `getAlertName`(alarm) | GET `/api/db/alert/names` | alarm/detail | db_api.py:250 | distinct alert_name |
| `getAlarmTotal` | POST `/api/db/alert/total` | alarm | db_api.py:289 | 告警每日统计 |
| `getAlarmDetail` | POST `/api/db/alert/detail` | alarm/detail | db_api.py:316 | 告警明细分页 |
| `getAlarmDetailTotal` | POST `/api/db/alert/detail_total` | alarm/detail | db_api.py:335 | 明细总数 |
| `updateOperate` | POST `/api/db/alert/operate` | alarm/detail | db_api.py:346 | 更新处理状态 |
| `getMaxDay`(resource) | GET `/api/db/res/max_day` | resource | db_api.py:75 | 管控表出现最多日期 |
| `getEnv`(resource) | GET `/api/db/res/envs` | resource、collection | db_api.py:53 | 管控表 distinct env |
| `getNamespace`(resource) | GET `/api/db/res/namespaces` | resource | db_api.py:59 | 管控表 namespace |
| `getDeployment`(resource) | GET `/api/db/res/deployments` | resource | db_api.py:67 | 管控表 deployment |
| `getResourceList` | GET `/api/db/res/list` | resource | db_api.py:85 | 资源管控表列表 |
| `addData` | POST `/api/db/res/add` | resource | db_api.py:117 | 新增管控记录 |
| `editData` | POST `/api/db/res/edit` | resource | db_api.py:132 | 编辑管控记录（含已采集的 JVM 四项，按精确 bytes 保存） |
| `getCollection` | GET `/api/db/res/collection` | collection | db_api.py:167 | 高峰期采集数据查询（k8s_resources） |
| `updatePodCount` | POST `/api/db/res/pod_count` | monit | db_api.py:145 | 更新管控表 pod_count |
| `showAddLabel` | GET `/api/db/agent/show_add_label` | monit、pod、alarm/detail | db_api.py:222 | 是否开启固定节点均衡模式 |
| `updateAdmission` | POST `/api/db/agent/admission` | workbench | db_api.py:182 | 更新准入配置 |
| `updateAgentCollect` | POST `/api/db/agent/collect` | workbench | db_api.py:192 | 更新采集开关/高峰时段 |
| `updateNmsNotConfirm` | POST `/api/db/agent/nms_not_confirm` | workbench | db_api.py:202 | 更新 nms_not_confirm |
| `updateScheduler` | POST `/api/db/agent/scheduler` | workbench | db_api.py:212 | 更新调度器开关 |

### 3.2 Istio 路由管理（istio_route/istio_route.py，psycopg 读写 PG 三表）

| 前端函数 | HTTP 接口 | 调用页面 | handler (istio_route.py) | 数据说明 |
|---|---|---|---|---|
| `getVirtualServices` | GET `/api/istio/vs` | istio/level1-vs | istio_route.py:1053 | VS 列表 |
| `getVirtualServiceDetail` | GET `/api/istio/vs?vs_id` | istio/level1-vs/detail | istio_route.py:1053 | VS 详情+关联集群 |
| `updateVirtualServiceClusters` | POST `/api/istio/vs/k8s` | istio/level1-vs/detail | istio_route.py:1480 | 更新 VS 关联集群 |
| `updateVirtualService` | PUT `/api/istio/vs` | istio/level1-vs/detail | istio_route.py:1147 | 更新 VS |
| `getHttpRoutes` | GET `/api/istio/httproute` | istio/level1-vs/detail | istio_route.py:1219 | HTTP 路由列表 |
| `createHttpRoute` | POST `/api/istio/httproute` | istio/level1-vs/detail | istio_route.py:1268 | 新增路由 |
| `updateHttpRoute` | PUT `/api/istio/httproute` | istio/level1-vs/detail | istio_route.py:1308 | 更新路由 |

### 3.3 其他 PostgreSQL

| 前端函数 | HTTP 接口 | 调用页面 | handler | 数据说明 |
|---|---|---|---|---|
| `getAgentNames` | GET `/api/agent_names` | monit/istio/alarm 等 10+ 页面 | `agent_names` kubedoor-master.py:583 → `utils.ck_get_k8s_names` utils.py:325 | `SELECT env FROM k8s_agent_status` |

### 3.4 统计与事件类接口（PostgreSQL）

> 由 `func_manager/top_queries.py` 与 `k8s_event/pg_event_client.py` 提供，底层走 asyncpg。

| 前端函数 | HTTP 接口 | 调用页面 | handler (文件:行号) | 数据说明 |
|---|---|---|---|---|
| `getTopPodAlerts` | GET `/api/stats/top10_pod_alerts` | monit/overview | top_queries.py | Pod 告警 TOP10（k8s_pod_alert_days） |
| `getTopEvents` | GET `/api/stats/top10_events` | monit/overview | top_queries.py | 异常事件 TOP10（k8s_events） |
| `getAlertDaily` | GET `/api/stats/alert_daily` | monit/overview | top_queries.py | 近 10 天告警统计 |
| `getEventsMenu` | GET `/api/events/menu` | alarm/events | k8s_event/event_query_api.py:29 | 事件查询菜单选项 |
| `queryEvents` | POST `/api/events/query` | alarm/events | k8s_event/event_query_api.py:116 | K8S 事件查询 |

---

## 四、K8S 来源（经 agent 转发，45 个）

> 除 `/api/load-balance/*` 部分有专用 handler（内部仍转发 agent）外，其余均走兜底路由 `kubedoor-master.py:1115` → `http_handler`。含 metrics-server 用量数据的接口已标注 ⚡。

### 4.1 命名空间 / Pod 查询与运维

| 前端函数 | HTTP 接口 | 调用页面 | 数据说明 |
|---|---|---|---|
| `getPromNamespace` ⚠️ | GET `/api/agent/namespaces` | monit 全系 + istio/workbench | 名字带 Prom 实查 K8S；master 有 namespace_cache 缓存(kubedoor-master.py:387/470) |
| `getPodData` | GET `/api/get_dpm_pods` | monit/index | deployment 下 pod 列表 ⚡ |
| `getPodList` | GET `/api/agent/pods` | monit/pod | Pod 列表（含实时 cpu/mem）⚡ |
| `getCciScheduleProfile` | GET `/api/cci/schedule-profile` | monit/scale | CCI ScheduleProfile CRD |
| `getPodPreviousLogs` | GET `/api/pod/get_previous_logs` | PodLogViewer、daemon、stateful | Pod 重启前日志 |
| `downloadPodLogs` | GET `/api/pod/download_logs` | PodLogViewer | 下载完整日志(gzip 二进制) |
| `createPodLogStreamUrl` | **WS** `/ws/pod-logs` | PodLogViewer、daemon、stateful | 流式日志（`pod_logs_websocket_handler` kubedoor-master.py:218） |
| `modifyPod` ◑ | POST `/api/pod/modify_pod` | monit、pod、daemon、stateful、alarm/detail | Pod 隔离（见混合） |
| `deletePod` | GET `/api/pod/delete_pod` | monit、pod、daemon、stateful、alarm/detail | 删除 Pod |
| `deletePodsBatch` | DELETE `/api/pod/delete_pods` | monit/pod | 批量删 Pod |
| `autoDump` | GET `/api/pod/auto_dump` | monit、pod、alarm/detail | Pod 内存 dump |
| `autoJstack` | GET `/api/pod/auto_jstack` | monit、pod、alarm/detail | jstack |
| `autoJfr` | GET `/api/pod/auto_jfr` | monit、pod、alarm/detail | JFR |
| `autoJvmMem` | GET `/api/pod/auto_jvm_mem` | monit、pod、alarm/detail | JVM 内存信息 |

### 4.2 Service / ConfigMap / Ingress / 资源 YAML

| 前端函数 | HTTP 接口 | 调用页面 | 数据说明 |
|---|---|---|---|
| `getServiceList` | GET `/api/agent/services` | monit/service | Service 列表 |
| `getServiceEndpoints` | GET `/api/agent/service/endpoints` | monit/service | Service Endpoints |
| `getServiceFirstPort` | GET `/api/agent/service/first-port` | istio/level1-vs | Service 首端口 |
| `getConfigMapList` | GET `/api/agent/configmaps` | monit/configmap | ConfigMap 列表 |
| `getIngressList` | GET `/api/agent/ingresses` | monit/ingress | Ingress 列表 |
| `getIngressRules` | GET `/api/agent/ingress/rules` | monit/ingress | Ingress 规则 |
| `getServiceContent` | GET `/api/agent/res/content` | monit 多页 | 资源 YAML 内容 |
| `updateServiceContent` | POST `/api/agent/res/ops` | monit 多页 | apply/replace/create YAML |
| `deleteK8sResource` | DELETE `/api/agent/res/delete` | monit 多页 | 删除任意 K8S 资源 |

### 4.3 StatefulSet / DaemonSet

| 前端函数 | HTTP 接口 | 调用页面 | 数据说明 |
|---|---|---|---|
| `getStatefulSetList` | GET `/api/agent/statefulsets` | monit/stateful | STS 列表（含 req/limit 聚合） |
| `getStatefulSetPods` | GET `/api/agent/statefulset/pods` | monit/stateful | STS 下 Pod ⚡ |
| `restartStatefulSet` | POST `/api/agent/statefulset/restart` | monit/stateful | 重启 STS |
| `scaleStatefulSet` | POST `/api/agent/statefulset/scale` | monit/stateful | STS 扩缩容 |
| `getDaemonSetList` | GET `/api/agent/daemonsets` | monit/daemon | DaemonSet 列表 |
| `getDaemonSetPods` | GET `/api/agent/daemonset/pods` | monit/daemon | DS 下 Pod ⚡ |
| `restartDaemonSet` | POST `/api/agent/daemonset/restart` | monit/daemon | 重启 DS |

### 4.4 节点管理

| 前端函数 | HTTP 接口 | 调用页面 | 数据说明 |
|---|---|---|---|
| `getNodesList` | GET `/api/nodes/list` | monit/nodemanager、monit/pod | 节点列表（allocatable/capacity/实时用量/pod数）⚡ |
| `cordonNodes` | POST `/api/nodes/cordon` | monit/nodemanager | 禁止调度 |
| `uncordonNodes` | POST `/api/nodes/uncordon` | monit/nodemanager | 恢复调度 |

### 4.5 扩缩容 / 重启 / 镜像 / 准入

| 前端函数 | HTTP 接口 | 调用页面 | 数据说明 |
|---|---|---|---|
| `execCapacity` ◑ | POST `/api/scale` | monit、resource | 扩缩容（见混合） |
| `execTimeCron` | POST `/api/cron` | monit、resource | 定时扩缩容 |
| `rebootResource` | POST `/api/restart` | monit、resource | 重启 deployment |
| `updateImage` | POST `/api/update-image` | workbench、monit | 更新镜像（master 内做权限校验 kubedoor-master.py:313-385 后转发） |
| `admisSwitch` | GET `/api/admis_switch` | workbench | 准入 webhook 开关 |

### 4.6 负载均衡（load-balance，agent 侧执行）

| 前端函数 | HTTP 接口 | 调用页面 | 数据说明 |
|---|---|---|---|
| `getNamespaces`(lb) | GET `/api/load-balance/namespaces` | monit/load-balance | 命名空间列表（未显式注册，走兜底） |
| `analyzeLoadBalance` | POST `/api/load-balance/analyze` | monit/load-balance | 负载分析（agent 算迁移计划）⚡ |
| `executeLoadBalance` | POST `/api/load-balance/execute` | monit/load-balance | 执行迁移 |
| `getIsolatedPods` | GET `/api/load-balance/isolated` | monit/load-balance | 隔离 Pod 列表 |
| `cleanupIsolatedPods` | POST `/api/load-balance/cleanup` | monit/load-balance | 清理隔离 Pod |
| `getNodesCpu` | GET `/api/load-balance/nodes-cpu` | monit/load-balance | 节点 CPU 使用率 ⚡ |
| `checkPodsStatus` | POST `/api/load-balance/check-pods` | monit/load-balance | 检查 Pod Ready |

---

## 五、混合来源（多源，7 个）

| 前端函数 | HTTP 接口 | 数据来源组合 | master handler | 说明 |
|---|---|---|---|---|
| `getAgentStatus` | GET `/api/agent_status` | **内存 + PostgreSQL** | `status_handler` kubedoor-master.py:488 | 在线状态取内存 `clients`，配置取 PG(`utils.ck_agent_info`)后 merge |
| `initPeakData` | GET `/api/init_peak_data` | **Prometheus + K8S + PostgreSQL** | `init_peak_data` kubedoor-master.py | 从 Prometheus 采高峰期指标 → 写 PG；随后经 agent 批量读取当前 JVM args → 补空管控表四项 |
| `getImageTags` | POST `/api/image/tags` | **外部镜像仓库 + Prometheus** | `image_tags_fetcher` image_tags_fetcher.py:411 | 先 Prometheus 取当前镜像(`utils.get_deployment_image`)，再查 Harbor/ACR/SWR 列 tag |
| `deployVirtualService` | POST `/api/agent/istio/vs/apply` | **PostgreSQL + K8S** | http_handler 分支 kubedoor-master.py:311 | 读 PG 生成 YAML → 转发 agent 下发集群 |
| `collectVirtualServiceRoutes` | GET `/api/agent/istio/vs` | **K8S + PostgreSQL** | http_handler + kubedoor-master.py:465 | agent 采集集群 VS → `sync_vs_from_k8s` 写 PG |
| `modifyPod` ◑ | POST `/api/pod/modify_pod` | **K8S + Prometheus** | http_handler kubedoor-master.py:392 | `add_label=true` 时先查 Prometheus 节点 CPU 排名 |
| `execCapacity` ◑ | POST `/api/scale` | **K8S + Prometheus** | http_handler kubedoor-master.py:392 | `add_label=true` 时注入 `node_cpu_list` 到 body |

---

## 六、内存 / 本地文件来源（load-balance 配置，4 个）

| 前端函数 | HTTP 接口 | master handler | 说明 |
|---|---|---|---|
| `getLoadBalanceConfig` | GET `/api/load-balance/config` | kubedoor-master.py:693 → load_balance/service.py:78 | 配置存内存，持久化本地 JSON |
| `updateLoadBalanceConfig` | PUT `/api/load-balance/config` | kubedoor-master.py:699 | 写内存 + 本地文件 |
| `getLoadBalanceStatus` | GET `/api/load-balance/status` | kubedoor-master.py:711 | 运行态 idle/analyzing/executing |
| `getLoadBalanceLogs` | GET `/api/load-balance/logs` | kubedoor-master.py:724 | 内存最近 100 条日志 |

---

## 七、无后端 / 前端 mock / 死代码（不计入统计）

| 前端函数 | HTTP 接口 | 说明 |
|---|---|---|
| `getK8sOverviewSnapshot`/`Trends`/`TopResources`/`EventTimeline` (overview.ts) | 无 | 纯前端 mock 假数据，未见调用 |
| `getLogin` / `refreshTokenApi` / `getAsyncRoutes` | `/login`、`/refresh-token`、`/get-async-routes` | vite-plugin-fake-server mock，非 master 接口 |
| `getLoadBalancePlan` | GET `/api/load-balance/plan` | 导出未调用 |
| `initByDays` | POST `/api/table` | 死代码 |
| `whSwitch` | GET `/api/webhook_switch` | 死代码 |
| `/api/prom_ns` | GET | 后端注册但前端已弃用 |
| `/api/sql` | POST | 已废弃，返回 410 |

---

## 八、结论：Prometheus 依赖收敛点

前端真正依赖 Prometheus 的接口只有 **5 个纯 Prometheus + 3 个混合中含 Prometheus** = 8 个触点：

**纯 Prometheus（5）**：`prom_env`、`prom_services`、`prom_query`、`prom_node_rank`、`prom_overview`
**混合含 Prometheus（3）**：`init_peak_data`（离线采集，保留）、`image/tags`（取当前镜像那步可改 K8S）、`modify_pod`/`scale`（`add_label` 时的节点排名，可复用改造后的 node_rank）

改造优先级见 [prom-to-k8s-migration-plan.md](./prom-to-k8s-migration-plan.md)：
- 模块 D（`prom_env`/`prom_services`）→ 改 agent 直取，最简单
- 模块 C（`prom_query`）→ agent 新增 Deployment 聚合接口，工作量最大
- 模块 B（`prom_node_rank`）→ 复用 agent `nodes-cpu`/`nodes/list`
- 模块 A（`prom_overview`）→ 暂留
- 模块 F（`init_peak_data`）→ 不改（离线时序聚合）
- Grafana 三页 iframe（monitk8s/monitnode/statistics）→ 保留
