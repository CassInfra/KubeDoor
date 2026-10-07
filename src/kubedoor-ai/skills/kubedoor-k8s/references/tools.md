# 工具选择与调用参考

主 Agent、子 Agent、HTTP 调用方和外部 MCP 客户端统一使用 `kubedoor_tool(operation, arguments, source="auto")`。本轮系统会注入实际 `tool_catalog()`，外部调用方可通过 `GET /api/ai/tools` 查询；操作名、必填字段和参数类型以当前目录为准，不猜测 URL 或接口 body。

先匹配用户需求，再选择来源：

- `kubedoor`：已有 master、Agent、数据库和监控接口。适合的接口可用时优先使用，**既包括查询，也包括重启、扩缩容、定时和周期执行等业务操作**。
- `direct`：通过已配置 kubeconfig 调用实时 K8S SDK/CLI。
- `agent`：通过在线且支持通用工具的 Agent 执行 K8S API/CLI/Pod 命令，无需上传 kubeconfig。
- `auto`：目录 operation 优先使用 KubeDoor；通用 operation 在有 kubeconfig 时选择 direct，否则选择 agent。该选项不会把通用 `api`/`kubectl` 自动改写为业务 operation，因此仍须先选对 operation。

不存在适合的可用接口、资源类型不适用或需要接口未提供的实时细节时，才用通用工具。失败的写操作不能直接换通道再写一次；先读取实际状态，避免重复任务或重复变更。用户指定目标优先于页面默认值，当前集群不可覆盖。

## Deployment 重启、扩缩容与调度

下列重启和扩缩容操作均只支持 **Deployment**；`namespace`、`deployment` 可由明确的页面选择补全，用户在本轮参数中明确指定时采用用户目标。它们不支持 Pod、StatefulSet 或 DaemonSet。Pod 排障和删除使用相应 Pod 工具；不能把 Pod 名传给 Deployment 重启接口。

| 用户需求 | operation | 主要参数 |
| --- | --- | --- |
| 立即滚动重启 | `restart_deployment` | `namespace`、`deployment` |
| 指定日期时间重启一次 | `schedule_deployment_restart` | `namespace`、`deployment`、`run_at` |
| 每天、每周或按 Cron 周期重启 | `cron_deployment_restart` | `namespace`、`deployment`、`cron` |
| 立即扩缩容 | `scale_deployment` | `namespace`、`deployment`、`replicas` |
| 指定日期时间扩缩容一次 | `schedule_deployment_scale` | `namespace`、`deployment`、`replicas`、`run_at` |
| 按 Cron 周期扩缩容 | `cron_deployment_scale` | `namespace`、`deployment`、`replicas`、`cron` |

`replicas` 为非负整数。`run_at` 是不带时区的 `YYYY-MM-DDTHH:mm`（也可用空格分隔日期和时间），表示北京时间（Asia/Shanghai）；不接受 `Z` 或 `+08:00` 等偏移。`cron` 使用五段表达式，顺序为分、时、日、月、星期，同样按北京时间解释，也支持目录允许的 K8S 标准宏。

定时和周期操作复用页面已有的 `POST /api/cron`，由服务构造与现有 Agent 匹配的请求，再由 Agent 创建任务。不要另行手写 CronJob、ServiceAccount/RBAC 或应用 YAML 来替代这些接口。Agent 创建的 CronJob 固定设置 `spec.timeZone: Asia/Shanghai`，按北京时间执行，与 Web 页面的定时执行一致；用户给出其它时区的时间时，先换算成北京时间再填写。该字段需要 K8S 1.25 及以上，旧版 agent 或更老的集群会忽略它，此时按 CronJob 控制器的时区执行，所以注册后要回读核对。单次任务的年份不由原接口强制执行，网关会拒绝已过去或不是该月日时分下一次到达的年份；沿用月、日、时、分调度，不宣称新增跨年绝对时间保证。

例：“帮我每天凌晨2点重启 demo 的 deploy-demo-order”属于周期重启：

```json
{"operation":"cron_deployment_restart","arguments":{"namespace":"demo","deployment":"deploy-demo-order","cron":"0 2 * * *"},"source":"kubedoor"}
```

这里的 2 点是北京时间。“某日 2 点只重启一次”则使用 `schedule_deployment_restart` 和明确日期的 `run_at`，不要把它转成每日 Cron。

执行前确认目标 Deployment 和是否存在同名任务。任务名为 `restart-cron-<deployment>`、`restart-once-<deployment>`，扩缩容对应 `scale-cron-<deployment>`、`scale-once-<deployment>`，位于 `kubedoor` 命名空间。同名任务先读现状，不能默认删除或覆盖。注册成功后优先用 `resource_content`（`namespace="kubedoor"`、`resource_type="cronjob"`、`resource_name` 为实际任务名）回读核验；没有可用的此读取能力时，使用通用 API 只读查询：

```json
{"operation":"api","arguments":{"method":"GET","path":"/apis/batch/v1/namespaces/kubedoor/cronjobs/restart-cron-deploy-demo-order"}}
```

核对 `spec.schedule`、`spec.suspend`、`spec.timeZone`（应为 `Asia/Shanghai`；缺少该字段时提醒用户任务将按 CronJob 控制器时区执行）、目标请求和运行记录。任务注册成功不等于已经重启；到点后仍需通过 Deployment 状态、Pod 或任务记录确认执行结果。无法回读时说明核验缺失，不声称已经完成重启。

## KubeDoor 目录操作

先用目录找到已有能力，参数细节查本轮实际 schema。下表覆盖当前接入的业务工具：

| 需求 | operation 与含义 |
| --- | --- |
| 入库资源与历史 | `resource_inventory`：已采集 CPU/内存 requests、limits 与 JVM；`resource_history`：指定 `date` 的每日高峰快照；`resource_max_day`：采集记录最多的日期；`stored_namespaces`、`stored_deployments`：已入库名称列表，Agent 离线仍可查询 |
| 数据库资源管控修改 | `resource_config_update`：保存 Deployment 的 `limit_mem_mb`、`limit_cpu_m`、`pod_count_manual` 和明确指定的 JVM 字段；`resource_pod_count`：仅保存人工管控副本数。均需批准，不会自动重启或立即扩缩容，具体生效语义见下文 |
| 监控 | `deployment_metrics`：工作负载资源指标；`metrics`：自由 MetricsQL/PromQL；`node_resource_rank`：节点 CPU、内存、Pod 与峰值排名 |
| 集群资源列表 | `namespaces`、`services`、`configmaps`、`ingresses`、`statefulsets`、`daemonsets`、`jvm_configs`：对应页面的资源或 JVM 配置查询 |
| 节点管理 | `nodes`：现有节点查询；`node_list`：详细节点列表（`simple=true` 仅名称）；`cordon_nodes`、`uncordon_nodes`：传 `node_names` 批量禁止或恢复新调度，不会驱逐已有 Pod |
| Pod 列表 | `pods`：指定 Deployment 的 Pod 明细；`pod_list`：集群 Pod、容器状态与资源，支持 `namespaces` 多选和 `node_name`；`statefulset_pods`、`daemonset_pods`：各自工作负载的 Pod 明细 |
| 服务与入口排障 | `service_endpoints`、`service_first_port`：指定 Service 的后端或第一个端口；`ingress_rules`：指定 Ingress 的域名、路径及后端规则 |
| 资源 YAML 编辑 | `resource_content`：按 `namespace`、`resource_type`、`resource_name` 读取接口支持的资源，含 Job/CronJob；`resource_apply`：传 `yaml_content` 和可选 `method=apply/replace/create`；`resource_delete`：删除接口支持的资源。CRD 不由这些接口支持，使用通用 K8S API；Deployment 重启调度仍使用前节专用工具 |
| 其他工作负载变更 | `restart_statefulset`、`scale_statefulset`：立即重启或扩缩容 StatefulSet（参数 `statefulset`，扩缩容另传 `replicas`）；`restart_daemonset`：立即重启 DaemonSet（参数 `daemonset`）。这些工具不提供 Deployment 的定时、周期模式 |
| Deployment 镜像 | `image_tags`：按现有仓库配置查询可用及当前镜像标签；`update_deployment_image`：传 `image_tag`，沿用现有 UPDATE_IMAGE 策略更新 |
| Pod 变更 | `delete_pod`：单个删除；`delete_pods`：批量传 `pods=[{namespace,pod}]`；`isolate_pod`：沿用现有隔离操作，均需批准 |
| 日志与 JVM 诊断 | `logs`、`previous_logs`：当前或上次退出容器日志，参数 `namespace`、`pod`、`lines`（最多 5000），指定容器时用通用 Pod/log API；`jvm_mem`、`jvm_dump`、`jvm_jstack`、`jvm_jfr`：执行现有 JVM 诊断流程，需批准且可能生成或上传文件 |
| 当前与历史事件 | `events`：实时事件，可按 `namespace`；`event_history`：入库历史事件，`start_time`、`end_time` 为包含边界的日期，可按 Kind、名称、原因、级别和消息过滤，无需在线 Agent |
| 历史告警 | `alert_total`：按告警名聚合；`alert_detail`：分页详情；`alert_detail_total`：相同条件的总数。过滤字段及 `startTime` 查实际 schema，不修改告警状态 |
| 负载均衡 | `load_balance_analyze`：读取既有配置并生成待执行计划，会保存计划，需批准；`load_balance_execute`：执行经过审阅的 `migrations`；`load_balance_isolated`：查询隔离旧 Pod；`load_balance_check_pods`：核验新 Pod Ready；`load_balance_cleanup`：确认新 Pod 正常后删除指定旧 Pod；`load_balance_node_cpu`：节点 CPU、内存和 Pod 状态 |
| 华为 CCI | `cci_schedule_profile`：查询 Deployment 的 ScheduleProfile 和 maxNum |

`resource_inventory` 中 CPU 单位为 milliCPU、内存为 MB；`jvm_xms_bytes`、`jvm_xmx_bytes`、`jvm_xss_bytes`、`jvm_max_metaspace_bytes` 为字节，`update` 说明采集时间。历史样本不是实时状态。`metrics` 传 `query`，`start/end` 同时提供为区间查询，另可传 `step`；服务端强制当前集群标签过滤。

`resource_config_update` 复用 `/api/db/res/edit`，只保存数据库管控配置，**下次发布或滚动重启时应用，不自动重启，也不立即修改在线资源**。如果只修改 JVM，先用 `resource_inventory` 取得目标当前的 `limit_mem_mb`、`limit_cpu_m`、`pod_count_manual`，原样传入这三个必填字段；不得用默认值或 0 替代未取得的当前值。仅传用户要修改的 JVM 字段：省略表示保留，显式 `null` 表示清除，数字以字节计。保存后回读配置，并说明尚待后续发布或重启生效，不能报告在线 JVM 已改变。

`resource_pod_count` 复用 `/api/db/res/pod_count`，仅更新数据库里的 `pod_count_manual`，不会立即改变 Deployment 副本数。用户要立即扩缩容时使用 `scale_deployment`；是否保存人工管控值依用户意图决定，不能把一次临时扩缩容默认变为长期管控配置。

资源修改、工作负载变更、节点调度和负载均衡写操作均通过网关批准。不要把 `load_balance_analyze` 当成完全无写入的查询，它会改变 master 的待执行计划。迁移清理参数从实际隔离查询结果取得，先检查替代 Pod Ready；不修改全局负载均衡配置或共享 Istio 模板。

## 通用操作（direct / agent）

```json
{"operation":"api","arguments":{"method":"GET","path":"/apis/apps/v1/namespaces/default/deployments","query":{"limit":1000}}}
{"operation":"kubectl","arguments":{"argv":["explain","pods.spec.containers.resources"]}}
{"operation":"istioctl","arguments":{"argv":["proxy-status"]}}
{"operation":"pod_exec","arguments":{"namespace":"default","pod":"example","container":"app","argv":["cat","/etc/resolv.conf"]}}
{"operation":"diagnostic","arguments":{"program":"dig","argv":["kubernetes.default.svc.cluster.local"]}}
```

API 支持已发现的资源组、资源类型与 CRD，不确定时先做 discovery。CLI 参数使用数组，不传 shell 字符串；文件通过 `files={basename:text}`、标准输入通过 `stdin` 提供。只读查询自动执行；修改、未知 CLI 和 Pod exec 通过网关冻结参数并等待批准，页面开启自动批准时仍沿用同一网关审批流程。结果包含 success/data/error、source、observed_at、truncated，命令结果还可能包含 exit_code。
