# kubedoor-ai：KubeDoor AI / Skills / MCP 服务

`kubedoor-ai` 是实际运行的 Python 服务，同时提供 Web AI 对话、Skills 接口和外部 MCP。
它复用 DeepAgents SDK、LangGraph PostgreSQL checkpoint 和 KubeDoor 现有接口，支持
多轮对话、新建会话、历史恢复、流式输出、工具调用和人工批准。

[`kubedoor-tools`](../kubedoor-tools/README.md) 是安装到 AI 与 Agent 镜像中的共享
Python 执行库，负责 Kubernetes SDK、CLI 和 Pod exec；它不监听端口，也不需要单独部署。
会话、鉴权、集群边界、审批与审计由本服务及 KubeDoor 网关负责。

完整使用说明、架构图和选型依据见 [AI 助手说明](../../docs/ai-assistant.md)。

## 模型与会话

用户在 Web 页面顶部中央的 AI 按钮中配置 `base_url`、API key 和模型名，配置按
登录用户名保存在当前浏览器。模型需要支持 OpenAI-compatible Chat Completions
工具调用；服务显式关闭 Responses API。无鉴权的本地模型允许 API key 留空。

`base_url` 填写供应商的 API 基址，例如 `https://api.example.com/v1`，SDK 会自动
追加 `/chat/completions`。若供应商使用 `/api/openai/v1` 等路径前缀，应完整保留；
仅当其 API 位于域名根路径时才只填写域名。填写完整的
`https://api.example.com/v1/chat/completions` 也会自动识别为对应基址。
模型名必须是供应商 API 请求中的准确 `model` ID，且当前 API key 可调用该模型；
页面展示名或自定义别称不一定可用。测试返回 `OpenAIModelNotFoundError` 时，先核对
API 基址与模型 ID，再确认该账号或 API key 的模型使用权限。

连接测试会检查实际工具调用能力；能返回文字但不能调用工具时，界面会显示失败提示。
测试接口返回 HTTP 422 通常表示本服务封装的模型测试失败，不代表供应商也返回了
422。应结合提示与响应中的 `error.code`、`error.details.upstream_status`、
`upstream_code` 和 `upstream_param` 判断网络、鉴权、地址、模型或协议问题。
早期 URL 参数校验失败返回 HTTP 400。供应商的安全错误提示保留在
`error.details.upstream_message`；它不会包含完整响应 body。

排查时复制诊断 ID（响应中的 `error.details.diagnostic_id`），查看同 ID 的日志：

```bash
kubectl -n kubedoor logs deployment/kubedoor-ai --since=15m
```

命名空间与示例不同时替换 `kubedoor`。查找“模型连接测试失败”记录中的
`diagnostic_id`，日志包含失败阶段、异常类型、上游状态、API 主机与路径及模型 ID，
不记录 API key、URL 查询参数、请求 body 或供应商完整响应。

使用 DeepSeek 官方 API 时，可填写 Base URL `https://api.deepseek.com`、模型
`deepseek-flash` 和官方 API key。官方 API 兼容 `/v1` 基址；若使用第三方网关，
地址、模型 ID 和 key 均按网关文档或模型列表填写，不能假定与官方完全相同。
DeepSeek 默认启用思考模式（强度 high）。模型设置里的「思考强度」可改为 low、max 或关闭：关闭时
请求带 `thinking: {"type": "disabled"}`，其他强度传 `reasoning_effort`，保持「默认」时请求里不带
这两个参数；其他模型忽略这项设置。服务使用
`langchain-deepseek==1.1.1`，保留普通及流式响应中的 `reasoning_content`，并补齐
后续工具调用和多轮请求中的回传，避免思考模式的上下文丢失。工具选择采用模型默认
行为，不强制 `tool_choice=required` 或指定工具。当前沿用 Chat Completions，
暂不接入 Responses API；前端仅支持文本输入。各项官方文档核对见
[DeepSeek 配置与兼容性](../../docs/ai-assistant.md#deepseek-配置与兼容性)。

模型 API key 仅在本次运行内存中使用，不作为配置写入数据库、checkpoint、日志或
外部 trace。用户输入、模型输出、工具结果和跨流式片段中回显的本次 key 会在持久化前过滤。
会话历史、运行状态、事件、审批动作与 checkpoint 存入 PostgreSQL，服务重启后仍
可恢复待批准操作。已经派发且没有获得结果的写操作标记为“结果未知”，不会自动重试。

每轮只选择一个 K8S 集群；命名空间、Deployment、Pod 可选具体资源或“全部”。
集群是该轮操作的硬边界，其余选择是上下文与默认参数，模型可查询同集群的关联资源。
后续轮次可以切换集群，历史保留每轮的集群标记；活动运行或待批准动作需先停止或拒绝。

资源选择按集群 → 命名空间 → Deployment → Pod 逐级按需加载；具体父级未选或为
“全部”时不预取下级全集群列表。切换上级清空下级并取消旧请求，各级独立加载。
模型上下文只包含实际已选资源，不包含菜单候选列表或未选字段；“全部”的查询
语义及 AI 自主调用工具查询同集群资源的能力保持不变。

新建会话先打开本地未发送草稿，首次发送时持久化，刷新页面不保留未发送草稿。
已访问会话可先显示页面内的历史缓存，再同步最新运行和审批状态；同步期间可继续
输入或切换，核验完成后才开放发送和审批。切换会话会取消旧读取，刷新或切换登录
账号会清除缓存。服务端核对会话归属后并行读取历史与活动运行状态。
桌面对话窗口加宽、加高并适配可用屏幕，手机保持全屏。

会话列表上方可以搜索：按标题和消息内容匹配本人的网页会话（`GET /api/ai/sessions?q=`，最多 50 个），
结果显示最近一条命中消息的片段。

最后一轮可以「重新生成」（同样的问题、集群范围和引用的记忆再问一次）或「编辑」后重发
（`POST /api/ai/sessions/{id}/runs` 带 `replace_last: true`）。只能替换最后一轮，而且这一轮不能已经
执行过修改操作（AI 会忘掉这次修改、可能重复执行），也不能触发过上下文压缩；升级前的旧轮次不支持。
替换时从那一轮的用户消息（检查点里固定 id `user-<run id>`）起用 `RemoveMessage` 删掉，历史回到上一轮
结束时，前缀和原来逐字节相同，照样命中缓存；被替换的消息在库里标记 `replaced_at`，保留用于追溯、不再显示。

告警详情、K8S 事件、Deployment 和 Pod 的操作菜单里有「问 AI」：打开 AI 助手、新开一个会话、预选集群和
资源（只知道 Pod、不知道所属 Deployment 时选到命名空间），把告警或事件原文整理成问题放进输入框，
由用户确认后再发送。

### 上下文窗口、自动压缩与用量

模型设置（网页的「模型设置」和企微专用模型）有三项上下文配置：上下文窗口、最大输出（界面上按
K token 填写，1K = 1024）和压缩阈值（%）。按模型名自动预填：去掉网关路径前缀、不区分大小写后，
以 `deepseek` 开头为 1024K / 384K，以 `gpt` 开头为 1024K / 128K，其他模型按 1024K / 128K
预填并提示按实际情况填写（`gpt-4o-mini` 等旧型号实际只有 128K，要手动改小）。旧配置没有这些
字段时由服务端推断（同样的名称规则，其他模型优先用 LangChain 内置的模型 profile）。最大输出只用于
展示和校验，请求里不设置 `max_tokens`，沿用厂商默认。

请求输入达到「上下文窗口 × 压缩阈值」（默认 50%，可设 20%–80%）时，DeepAgents 用运维专用提示
摘要较早的消息，原文只保留最近约 10%；摘要分节保留用户目标与进展、资源准确名称、关键证据、
变更记录（已执行 / 已批准 / 被拒绝 / 结果不确定）、未完成事项和用户约束。被替换的原文写入
`/conversation_history/` 下的文件，模型需要时可以读回。模型返回「maximum context length」等超长
报错时会先摘要再重试一次。模型是 DeepSeek 时，自动摘要、会话标题和记忆总结这三类不带工具的
请求关闭思考（`thinking: disabled`），对话本身和「测试连接」按设置的思考强度。

所有模型请求都开启流式用量（`stream_usage`）。每轮结束（完成、失败、停止，以及进入等待审批）时把
用量写入 `kubedoor_ai_runs.usage`，并附在 `done` 事件里：`model`、`model_calls`、`input_tokens`、
`output_tokens`、`cache_read_tokens`（DeepSeek 的 `prompt_cache_hit_tokens`）、
`reasoning_tokens`、`last_input_tokens`（主 Agent 最近一次请求的输入，即当前上下文大小）和
`max_input_tokens`；这一轮触发过自动摘要时还有 `summary_calls`、`summary_input_tokens`、
`summary_output_tokens`（摘要请求不经过 agent 中间件，由回调单独计数，不计入前面几项）。
审批恢复后在原有用量上累加；子 Agent 的调用计入总量。

网页对话框头部显示「上下文 N%」（`last_input_tokens ÷ max_input_tokens`，达到 25% 变成警告色），
悬停显示 token 数和本轮的缓存命中率；打开历史会话时用 `GET /api/ai/sessions/{id}` 返回的
`usage`（最近一轮）。「测试连接」同时检查响应里有没有 token 用量：少数网关不支持
`stream_options`，没有用量时压缩只能按估算触发、用量统计会缺失，测试结果会给出警告。
Grafana 的「KubeDoor-AI助手用量与缓存命中看板」（`deploy/dashboards/KubeDoor-AI.json`，PostgreSQL
数据源）按渠道统计轮数、输入、缓存命中率、上下文占用分布、压缩次数和估算费用，用来观察缓存效果、
判断压缩阈值是否合适。

模型返回的 `finish_reason` 是 `insufficient_system_resource`（DeepSeek 资源不足）或 `aborted` 时，
原样重发一次（前缀不变，能命中缓存）；重发后仍被打断，或者是 `length`（输出达到上限）、
`content_filter`，在回答末尾追加一句说明。

## 全局记忆库

所有登录用户可查看、引用全局记忆，全局读写用户（或管理员）可手动新建、编辑、删除；会话历史
仍按用户隔离。列表分页返回标题等信息，正文按需读取，编辑、删除按版本检查，
避免覆盖其他用户的更新。

所有用户可为本人会话通过独立、无运维工具的模型请求生成可编辑标题与正文草稿；
摘要只读取已完成轮次，需先结束活动或待批准任务，长输入会注明截断范围。保存后才
进入全局库。发送时选择的记忆只为当前轮附加，引用的标题、正文、版本快照进入
用户消息、会话历史与 checkpoint；提交成功清除选择，失败保留。后续轮次沿用
已有对话历史，不重新读取或追加记忆，新会话需重新选择。库中编辑、删除不会
改写原历史或待批准任务的上下文。

每轮最多引用 10 条，正文合计最多 32000 字符；单条标题最多 200 字符、正文最多
20000 字符。HTTP 提交本轮引用使用 `memory_ids`，返回的用户消息 `memories`
包含实际引用快照，原 `content` 保留用户问题正文。

## 工具来源与能力

模型为每次工具调用选择 `source`，同一轮对话可以使用多个来源：

| 来源 | 用途 |
| --- | --- |
| `kubedoor` | 优先复用已有 master、Agent 和数据库目录接口，包括资源、日志、事件、指标，以及立即、定时、周期重启和扩缩容等现有运维操作 |
| `direct` | 通过服务端保存的 kubeconfig，在 AI 服务内执行 Kubernetes SDK、CLI 或 Pod exec |
| `agent` | 通过在线 Agent 的集群内身份执行同一共享工具库，无需上传 kubeconfig |
| `auto` | 已有目录 operation 走 `kubedoor`；通用 operation 按连接能力选择，仍需先选对操作名 |

每轮动态注入实际工具目录，`GET /api/ai/tools` 返回同一目录。已有接口符合需求且
可用时，查询和修改都优先使用相应目录操作；没有适合的可用接口再选择通用 K8S
工具，不通过自写 YAML 或 CLI 重做已支持的业务功能。

Deployment 重启和扩缩容均支持三种执行方式：

| 执行方式 | 重启 operation | 扩缩容 operation |
| --- | --- | --- |
| 立即 | `restart_deployment` | `scale_deployment` |
| 定时一次 | `schedule_deployment_restart` | `schedule_deployment_scale` |
| 周期 | `cron_deployment_restart` | `cron_deployment_scale` |

上述接口**只支持 Deployment**，不适用于 Pod、StatefulSet 或 DaemonSet。
参数包含 `namespace`、`deployment`；用户明确目标优先于页面选择，选择可作为默认值。
扩缩容另传非负整数 `replicas`。定时传不带时区的 `run_at`（`YYYY-MM-DDTHH:mm`
或空格分隔，北京时间），周期传五段 `cron`（北京时间）或支持的 K8S 标准宏。
例如每天凌晨 2 点重启 `demo/deploy-demo-order` 应使用
`cron_deployment_restart` + `cron="0 2 * * *"`，复用页面的 `/api/cron`，
由现有 Agent 创建任务；定时一次使用 `schedule_deployment_restart`。

任务注册后优先通过目录内的 `resource_content` 回读 `kubedoor` 命名空间的 CronJob，
核对计划、目标与状态；同名任务先读取，不能默认覆盖。Agent 创建的 CronJob 固定设置
`spec.timeZone: Asia/Shanghai`，按北京时间执行（需 K8S 1.25 及以上，旧版 agent
或更老的集群会忽略该字段，按控制器时区执行）。`run_at` 不接受偏移；单次任务沿用
现有月日时分和执行后删除逻辑，原接口不强制年份，网关只接受该月日时分（北京时间）
下一次到达的年份，未新增跨年绝对时间保证。创建任务成功不等于 Deployment 已执行重启。

目录也接入节点调度、批量 Pod 操作、Service/Ingress 详情、资源 YAML 读写、
StatefulSet/DaemonSet 立即重启、镜像标签、CCI、负载均衡、历史事件与告警等
已有页面接口。工具分类见 [Skill 工具参考](skills/kubedoor-k8s/references/tools.md)，
完整参数以 `/api/ai/tools` 返回为准；接口不支持的类型（如 CRD）使用通用 K8S 工具。

通用能力包括 Kubernetes API 和 CRD、`kubectl`、`istioctl`、Pod exec，以及
`jq`、`yq`、`curl`、`dig`、`openssl`、`rg` 辅助命令。CLI 使用参数数组执行，
不提供后端通用 shell。实际集群的 Istio CRD 与路由可通过通用工具查询或修改；
本版本没有 Istio 共享数据库模板的专用流程。

`resource_inventory` 复用 `/api/db/res/list`，包含 CPU/内存 requests、limits 与
Xms、Xmx、Xss、MaxMetaspace 等已入库 JVM 参数；它表示采集或管控数据，不能当作
实时进程值，`null` 表示未采集。VictoriaMetrics 自由 MetricsQL 查询通过 master
使用现有 `PROM_URL`，保留 tenant 路径并强制限定当前 K8S 标签。普通 Prometheus
继续使用已有指标接口。工具结果标注来源、观测时间、数据类型和截断情况。

### 花折资源管控（准入控制）

集群开启准入控制、命名空间在管控列表里、服务已登记到管控表时，Deployment「受管控」：准入 webhook
会在创建、发布、重启、扩缩容时，把副本数、第一个容器的 requests/limits、JVM 参数（-Xms/-Xmx/-Xss/
-XX:MaxMetaspaceSize）改写成管控表的值。受管控时直接改 K8S 不生效（K8S 照样返回成功），不受管控时
改管控表也不生效。所以这三类修改由网关按管控状态选择执行方式（`kubedoor_ai/control.py`）：

| 操作 | 受管控 | 不受管控 |
| --- | --- | --- |
| `scale_deployment`（`mode` 默认 `persistent`） | 永久：先写管控副本数再扩缩；`temporary`：带 `temp` 临时扩缩，下次发布或重启恢复管控值 | 直接改副本数 |
| `schedule/cron_deployment_scale` | 到点只能临时扩缩（CronJob 写不了管控表），不能传 `mode=persistent` | 同左，一律带 `temp` |
| `update_deployment_resources` | 只能改 limits（requests 由每日高峰期采集决定）：写管控表，`apply_now`（默认 true）再滚动重启 | 直接 patch 第一个容器的 resources |
| `update_deployment_jvm` | 写管控表，`apply_now` 再滚动重启 | 直接替换第一个容器 java args 里已有的选项 |
| `update_deployment_image`、`restart_deployment` | 照常执行，卡片列出准入控制会刷回管控值的副本数、资源、JVM | 照常执行 |
| `resource_config_update`、`resource_pod_count` | 只写管控表，下次发布或重启生效 | 允许，卡片提示保存后不会生效 |
| `resource_control_add` | 登记新服务（已登记的拒绝） | 同左，提示开启准入后才应用 |

管控状态来自 master 的 `/api/ai/internal/deployment-control`（判断规则同准入热路径，agent 在线时再实时
核实 webhook 是否存在、命名空间是否带 `kubedoor-ignore` 标签），目录里对应只读操作 `deployment_control`。
准备阶段把管控状态、在线值和执行步骤冻结进审批预览（不受管控时直接修改的步骤先做 server dry-run，带
resourceVersion 前置条件和 diff）；执行前再查一次管控状态和管控表，审批期间被改过就不执行。执行后回读
Deployment，结果里的 `verification` 写明是否和期望一致，不一致时注明「被准入控制改写成了管控值」。

受管控的 Deployment，用 `kubectl`、`api`、`resource_apply` 直接改副本数、requests/limits 或 JVM 参数会在
审批前被拒绝（结果必然被改回），并提示改用上表的操作；按标签或 `--all` 批量修改时，只要命名空间在准入
控制管辖内就拒绝。改镜像、环境变量等其他字段不受影响。管控表里没有该服务、又没开新服务免确认时，
这个 Deployment 的创建和修改都会被准入拒绝，先用 `resource_control_add` 登记。

`resource_config_update` 仅改 JVM 时先查询并保留必填的 `limit_mem_mb`、`limit_cpu_m`、`pod_count_manual`
当前值；JVM 字段省略保留、显式 `null` 清除。`update_deployment_image` 只换第一个容器镜像的 tag（仓库
地址、端口保留），改 sidecar 镜像或换仓库用通用工具。

旧 Agent 仍可提供原有目录接口；通用工具要求 Agent 在线且声明 `ai_tools` 能力，
未具备该能力时会提示升级。已登记的离线集群仍可查询 PostgreSQL 历史资源、JVM
数据与监控指标，实时操作取决于 Agent 或直连连接是否可用。

## 人工批准与 kubeconfig

只读查询自动执行；修改、未知 CLI 与 Pod exec 先冻结目标、参数、预检结果和差异，
展示给用户批准后执行。批准与执行使用同一动作 ID 和持久化派发记录，不会在写失败后
自动换来源重试。停止或取消无法撤销 Kubernetes 已接受的修改，应查询实际状态确认结果。
副本数、资源、JVM 这类按管控状态生成计划的操作，审批卡片显示管控状态、执行步骤、「当前 → 目标」
变化表、影响和注意事项，执行后显示回读核验结果。企业微信的审批卡片只放得下一行摘要，完整说明
（管控状态、步骤、变化、影响、命令或差异）写在卡片正上方的回复里。

在 Web 工作台的 Kubeconfig 列上传自包含 YAML/JSON，选择 context、测试连接后保存。
支持多 context、静态 token 和内嵌证书；仅读写用户可上传、测试、替换或删除。
kubeconfig 使用 AES-256-GCM 加密后存入 PostgreSQL，原始配置不提供下载，
测试或替换失败会保留旧配置。加密密钥独立保存，必须与数据库一起规划恢复。

共享执行器拒绝外部认证命令、主机文件引用、CLI 集群或凭据覆盖等绕过集群边界的输入。
预检冻结连接 revision 和 fingerprint，批准与执行时再次核对；删除或替换连接会使
原批准失效。K8S Secret 和 CLI files/stdin 在批准卡、事件与会话历史中隐藏，
内部动作保留实际参数以执行已经批准的操作。

## HTTP、Skills 与外部 MCP

浏览器及共享工具接口统一位于 `/api/ai`：

| 接口 | 说明 |
| --- | --- |
| `GET /api/ai/bootstrap` | 当前用户、集群连接能力与 Skills |
| `/api/ai/sessions`、`/api/ai/sessions/{id}/runs` | 会话管理与创建运行 |
| `GET/POST /api/ai/memories`、`GET/PATCH/DELETE /api/ai/memories/{id}` | 全局记忆分页查询、按需读取正文及版本检查后的管理 |
| `POST /api/ai/sessions/{id}/memory-summary` | 生成本人会话的可编辑记忆草稿，保存前不写入全局库 |
| `GET /api/ai/runs/{id}/events` | 标准 SSE，支持 `after`、`Last-Event-ID` 持久化重放 |
| `/api/ai/runs/{id}/decisions`、`/api/ai/runs/{id}/cancel` | 批准、拒绝与停止运行 |
| `GET /api/ai/tools`、`POST /api/ai/tools/execute` | 查询目录与调用统一工具网关 |
| `GET /api/ai/skills`、`GET /api/ai/skills/{id}` | 获取仓库内 Skills |
| `/api/ai/connections`、`/api/ai/provider/test` | 连接管理与模型连通性测试 |
| `GET /health` | 无需身份的健康检查 |

DeepAgents 加载仓库内 `skills/kubedoor-k8s`，Skills 文件只读；会话和运行创建支持
`Idempotency-Key`，流重连或刷新不会自动重复提交工具。

外部 MCP 使用 `/mcp`（Streamable HTTP），并保留 `/sse`、`/messages` 兼容入口。
原有 **16 个工具的名称和参数保留**，另提供统一 `kubedoor_tool` 和
`skills://kubedoor-k8s` 资源。外部调用与 Web 共用权限、审批和审计；需要批准的调用
返回 `pending` 及浏览器会话链接，在批准前不会执行。

## 配置、启动与部署

| 环境变量 | 说明 |
| --- | --- |
| `AI_INTERNAL_TOKEN` | nginx、AI 服务与 master 之间的可信内部令牌 |
| `AI_ENCRYPTION_KEY` | 64 位十六进制 AES-256-GCM 密钥；遗失后无法解密已有 kubeconfig |
| `MASTER_URL` | 固定 master 服务地址，默认 `http://kubedoor-master` |
| `PG_HOST`、`PG_PORT`、`PG_USER`、`PG_PASSWORD`、`PG_DATABASE` | 历史、动作、连接与 checkpoint 使用的 PostgreSQL |
| `PORT` | 服务端口，默认 `8000` |
| `RETENTION_AI_CHECKPOINT_DAYS` | 企微旧对话的检查点保留天数，默认 `7`，`0` 表示不清理（见下文企业微信一节） |

### 企业微信智能机器人

管理员可在「系统管理 → 企微 AI 机器人」配置企业微信 API 模式的 Bot ID、Secret，以及仅供该机器人使用的模型 Base URL、API Key 和模型名。凭证使用 `AI_ENCRYPTION_KEY` 对应的 AES-256-GCM 密钥加密写入 AI 数据库，不会通过管理接口回显。启用后 AI 服务通过 Python SDK 主动建立企业微信 WebSocket 长连接；无需公网回调地址，运行环境需允许出站访问企业微信长连接服务。

首版只处理机器人单聊文本。每条消息仍由 master 按花折账号实时解析用户状态与集群授权，绑定不会授予权限。首次遇到的成员会出现在「成员与默认集群」列表。企微回调里只有 UserID，没有姓名；机器人不是企业超级管理员创建时，UserID 还是加密的（形如 `woTxSZCQAA...`），所以：

- **自助绑定**：花折用户在 Web 右上角用户菜单「绑定企业微信」生成 6 位绑定码（10 分钟有效，每个账号同时只有一个，用过即删），再在企微里给机器人发「绑定 123456」，这个成员就绑定到他自己的花折账号；弹窗轮询到新绑定会提示成功，也能解除自己账号绑定的成员。接口 `GET /api/ai/wecom/binding`、`POST /api/ai/wecom/binding/code`、`DELETE /api/ai/wecom/binding/{userid}`，任何登录用户都能用，但只能管理自己账号绑定的成员。同一成员 10 分钟内输错 5 次绑定码就暂停 10 分钟；所有成员加起来 10 分钟内输错 30 次时，自助绑定整体暂停 10 分钟并记日志（多个企微账号一起猜码也猜不完）。换绑到别的账号时，原来的默认集群和当前集群清空，被拦下的提问作废。
- **辨认成员**：成员可以发「我是 张三」（「我是」后面要有空格或冒号，名字 1~16 个字、不含空白和标点，免得把「我是不是该扩容」当成名字），列表里显示「张三（自称）」；管理员也可以给成员写备注（`PUT /api/ai/wecom/users/{userid}/remark`）。自称和备注只用来辨认，不作为身份依据。
- **管理员绑定**：管理员也可以在同一页面把成员绑定到已启用的花折人类账号，并指定默认集群；列表显示绑定人（「自助绑定」或「admin 绑定」）。
- **未绑定成员默认权限**：未绑定的成员默认不能使用机器人；管理员可在「未绑定成员默认权限」里选一个花折账号（不能是管理员账号，接口 `PUT /api/ai/wecom/unbound`），未绑定的成员就按这个账号的权限（含按集群的单独授权）使用机器人，建议为此单独建一个账号；同时可以给未绑定成员指定默认集群。已绑定的成员仍按各自绑定的账号，解除绑定后回到默认权限；默认权限账号被停用、删除或改成管理员时，未绑定的成员不能继续使用。
- **清理成员**：列表里的「清理」（`POST /api/ai/wecom/users/{userid}/purge`）把成员当作新成员：在成员锁里删掉成员记录（绑定、默认集群、当前集群、对话指针、被拦下的提问）和他的待审批，停止他企微对话里进行中的任务，给他所有企微对话打上 `checkpoint_purged_at` 并删除检查点（旧欢迎卡片也回不去），会话和消息记录保留用于审计。成员下次发消息时重新出现在列表里。欢迎卡片由企业微信在成员当天第一次进入单聊时触发，清理不会让它当天再出现。

集群跟着对话走：

- **默认集群**：每个新对话（发「新对话」、空闲超时、欢迎卡片「开始新对话」）从默认集群开始。绑定成员看绑定时选的，未绑定成员看默认权限里的；默认集群不写进成员的当前集群，管理员改了立即生效。「继续上次」切回的对话恢复成它最后一次提问用的集群。
- **默认集群为「无」**：每个新对话都要先选集群。提问先存下（`pending_text`，10 分钟内有效，开新对话时作废），回复一张选集群卡片（下拉框最多 10 个集群，默认选中上一个对话用的集群），选好后接着处理刚才的问题，结果主动推送；也可以发「切换集群 名称」，切换后同样接着处理。
- **查看和切换**：发送「集群」（或「当前集群」「选择集群」「帮助」）回复带序号的可访问集群（带别名）和一张选集群卡片；「切换集群 X」的 X 可以是序号、集群名、别名，或能唯一确定的一部分，比较时忽略大小写、空格和 `-_.`。「切换到 beta 集群看一下 nginx 日志」这种去掉集群名后还有别的内容的，当成普通提问（同样切过去再回答）。切换集群、在卡片里选集群、发「集群」时，当前对话如果已经空闲超时，先换成新对话，所以刚选的集群就是下一条提问用的集群，不会又被新对话换回默认集群。
- **提问里说到别的集群**：集群名或别名都算，各段之间的空格和 `-_.` 可有可无、不区分大小写（「cassmall beta kunlun」「CASSMALL_KUNLUN」）；一处写法被另一个更长的写法盖住时只算长的；只认出一个集群时直接切过去。本地认不出的交给 AI：企微提问的本轮上下文块里列出成员可访问的集群和别名（「可切换的集群」），AI 判断用户问的是另一个集群时调用 `kubedoor_tool` 的 `switch_cluster`（只在企微提问里可用，网关只校验并记下目标，不访问任何集群），之后本轮不能再调用工具，模型用一句话说明正在切换，本轮正常结束（检查点完整、只追加）；切换目标在任务改成 completed 之前记进 `kubedoor_ai_runs.switch_env`，企微在那个集群上用新的一轮重新处理这个问题（新的一轮不能再切换）。每一轮仍只在一个集群上执行，写操作照常审批。
- **集群别名**在 Web 的 Agent 管理页编辑（master `POST /api/db/agent/aliases`，仅管理员；按「去掉空白和 `-_.`、转小写」查重，不能和其它集群的名称或别名重复），经 `/api/ai/internal/bootstrap` 的 `aliases` 字段带给 AI 服务。中文别名没有词边界，在提问里原样出现就算提到了这个集群，不要用「测试」「生产」这类日常用词。
- 每条回答末尾注明所用集群。

K8S 写操作仍通过 AI 审批；企微审批卡片仅绑定原始成员、花折账号、运行和冻结的 action，其他成员或过期卡片不能批准。

企微的模板卡片事件内容在 `event.template_card_event` 里（`event_key`、`task_id`，下拉框选的值在 `selected_items`）。卡片事件只能用来更新被点的那张卡片（`aibot_respond_update_msg`，5 秒内），不能回复消息：按钮处理完就把卡片换成结果（按钮交互卡片，只留一个显示状态的按钮；`text_notice` 卡片必须带跳转链接，不能用）；审批批准或拒绝后的执行结果、选好集群后接着处理的回答用主动推送（`aibot_send_msg`，markdown，执行中再遇到审批也推送审批卡片）。更新卡片失败时日志里记下企微回执的 errcode。点审批卡片后整个处理限 4 秒：恢复任务前还要问 master 集群状态，master 慢时批准照常生效，卡片先改成「已收到，正在处理」，处理完推送结果；「开始新对话」要停的任务放到后台停。

企微会话单独持久化，不会显示在网页 AI 会话列表中；网页 AI 的模型配置保持原样。Bot Secret 和模型 API Key 留空保存表示保持原密钥不变；修改 Base URL、模型或 Bot ID 后保存会重建长连接。首次接入建议先测试模型工具调用，保存启用配置后确认连接状态为「已连接」，再绑定测试账号和只读集群做单聊验收。

企微只有一个聊天窗口，会话边界由服务端管理：

- **空闲自动开新对话**：距当前对话最后一条消息超过「对话空闲超时」（企微配置页，默认 120 分钟，0 表示关闭），新消息进入新对话，回复开头提示「距上次对话已超过 2 小时，已开始新对话（发送「继续上次」可回到之前的对话）」。新对话不带上一个对话的摘要，从默认集群开始（和上一个对话用的集群不同时再提示「新对话使用默认集群 X」，没有默认集群时先发选集群卡片）。当前对话有运行中的任务或未过期的待审批操作时不切换。
- **「新对话」**（也认「新会话」「开始新对话」「清空上下文」「重新开始」「/new」「/reset」）：停止当前对话里未完成的任务，开始新对话；没有默认集群时附一张选集群卡片。
- **「继续上次」**（也认「继续上次对话」「回到上次」「/resume」）：和上一个对话互换，集群恢复成那个对话最后一次提问用的集群，并刷新它的最后活跃时间，紧接着的消息不会又被空闲规则切走；上一个对话必须属于同一个花折账号。
- **过期审批不再卡住对话**：收到新消息时，当前对话的任务如果只剩已过期的待审批操作，先自动停止它并提示；审批还没过期时回复「该会话已有任务运行或等待审批」，并提示可以发送「新对话」放弃它。
- **欢迎卡片**：成员当天首次进入单聊时回一张卡片（默认集群、上次对话的时间和轮数），带集群下拉框（新对话用的集群）和「开始新对话」「继续上次对话」按钮。下拉框要问 master，3 秒内拿不到就不放，保证 5 秒内回复。「开始新对话」用下拉框里选的集群（选的就是默认集群时不写进当前集群，管理员改默认集群照样生效），「继续上次对话」回到卡片上显示的那个对话。
- **上下文占用提示**：上一轮最后一次请求的输入达到模型窗口的 25% 时，回答末尾的集群标注改为「—— 集群：X · 上下文 31%（发送「新对话」可重新开始）」。

当前对话记在 `kubedoor_ai_wecom_users.session_id`，上一个对话记在 `previous_session_id`，当前对话的集群记在 `current_env`；旧对话保留不删，审计不受影响。同一成员的并发消息在 PostgreSQL advisory lock 内解析当前对话，只会建一个新对话。「继续上次」和卡片上的按钮只用本地记着的花折账号名，不调 master，保证卡片 5 秒内更新；卡片里选的集群在下一次提问时校验，没有权限会再发一张选集群卡片。「新对话」没有默认集群时要问 master 取集群列表来发卡片。切换本身不授予任何权限，下一次提问照常完整校验。

企微里再也不会继续的旧对话（不是任何成员的当前对话或上一个对话，也没有运行中或待审批的任务），最后活跃超过 `RETENTION_AI_CHECKPOINT_DAYS` 天（`kubedoor-config`，默认 7，0 = 不清理）后删除它的 LangGraph 检查点，会话和消息记录保留用于审计。AI 服务启动 5 分钟后第一次清理，之后每 6 小时一轮、每批 200 个；每个对话在它成员的锁里再确认一次再删，并记下 `kubedoor_ai_sessions.checkpoint_purged_at`，之后旧欢迎卡片的「继续上次对话」会被拒绝。网页 AI 的会话由用户自己管理，不自动清理。

企微专用模型同样有上下文窗口、最大输出、压缩阈值和思考强度四项设置（见上文），配置页另有「对话空闲超时」。

所有浏览器、Skills、工具与 MCP 入口由可信 nginx 注入 `X-Kubedoor-Token`、
`X-User-Name`、`X-User-Permission`（全局角色 `admin`/`rw`/`read`/`none`）和
`X-User-Clusters`（单独授权的集群 JSON，如 `{"prod":"rw","test":"none"}`）。这些值来自
nginx 对 master `/api/auth/verify` 的 `auth_request` 子请求，客户端自带的同名头会被覆盖。
某个集群的有效权限：管理员读写；有单独授权按授权；否则按全局角色。写操作与批准按会话所选集群校验，
共享记忆按全局角色校验，kubeconfig 管理仅限管理员。调用 master 时 master 会按用户名重新计算权限，
不采信 `X-User-Permission`。代理不得记录包含模型 key 或 kubeconfig 的请求 body。缺少有效加密密钥、内部令牌或数据库连接时启动失败；
启动会幂等创建 `kubedoor_ai_*` 表并初始化 PostgreSQL checkpointer。

本服务是必装组件，安装控制端时总会部署；`deploy/kubedoor.conf` 的 `ENABLE_MCP`
（默认 `true`）只控制外部 MCP 暴露。安装脚本生成并保留
内部令牌与加密密钥，存放于配置文件旁的 `*.ai-secrets`（0600）及
`kubedoor-ai-security` Secret，升级时必须保留且不要提交到 Git。
Deployment、Service 和镜像组件均命名为 `kubedoor-ai`，部署清单位于
`deploy/manifests/master/30-ai.yaml`，镜像版本由 `TAG_AI` 配置。
首次接入需使用匹配的 master、web、AI 版本；通用 Agent 工具还需集群 Agent 支持。
已启用 AI 的环境，本次全局记忆功能只需更新 `kubedoor-ai` 与 `kubedoor-web`，
沿用已有 master、Agent 接口。启动时自动创建记忆表并补充消息引用字段，复用现有 PostgreSQL。
AI 服务保持单副本，使用 `Recreate` 更新策略；更新期间 AI/MCP 会短暂不可用。

AI 与 Agent 镜像的 Dockerfile 都将 `src/kubedoor-tools` 复制到构建环境，
通过 `pip install` 安装共享执行库，并安装 CLI；没有独立的 tools 服务或镜像。
两个镜像均通过 `PIP_INDEX_URL` build arg 和同名环境变量配置 Python 包源，默认使用
清华源 `https://pypi.tuna.tsinghua.edu.cn/simple/`，覆盖共享工具库、其他依赖和
PEP 517 隔离构建时安装的 setuptools 等构建依赖。AI 镜像的 apt 使用阿里云
Debian/debian-security 源，Agent 镜像的 apk 沿用华为云 Alpine 源。
构建必须使用**仓库根目录作为 context**：

```bash
docker build -f src/kubedoor-ai/Dockerfile -t kubedoor-ai:local .
docker build -f src/kubedoor-agent/Dockerfile -t kubedoor-agent:local .
```

如需切换 Python 包源，可在构建命令中追加
`--build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/`。
运行时的模型 API、Kubernetes 和 VictoriaMetrics 地址仍使用用户配置，与安装源无关。

CLI 版本可通过 `KUBECTL_VERSION`、`ISTIO_VERSION`、`YQ_VERSION` build args
调整，以匹配集群版本。kubectl 从 [GitHub 社区镜像](https://github.com/ChampiYann/kubectl-binaries/releases)
下载，istioctl 和 yq 从各自的 GitHub Releases 下载，都是直连 GitHub；构建机访问
GitHub 不稳定时，可在 `docker build` 时加 `--build-arg HTTPS_PROXY=http://<代理地址>`。
默认 kubectl 版本 `1.37.1` 的 amd64/arm64 二进制均使用脚本内固定的官方 SHA256 校验，
构建时无需访问 `dl.k8s.io`。
修改 kubectl 版本时必须通过 `KUBECTL_SHA256` 传入对应 Linux 架构的官方校验值；
未提供时安装会失败，不会尝试下载官方校验文件。

本地从仓库根目录安装依赖，配置上表环境变量后启动：

服务运行环境为 Linux，建议通过 Docker 容器联调（Windows 主机可使用 Docker
或 WSL）。下面的直接启动方式需要自行安装上述 CLI 工具。

```bash
python -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple/ ./src/kubedoor-tools -r src/kubedoor-ai/requirements.txt
python src/kubedoor-ai/kubedoor-ai.py
```

## 开发约定：前缀缓存与检查点

DeepSeek 等模型的上下文缓存要求请求从第一个 token 起完整匹配；命中价格只有未命中的几十分之一，
长对话的成本和首 token 延迟都取决于它。改动运行时（系统提示、中间件、消息构造）时必须保持以下
不变量（设计见 [design.md 3.1 节](../../docs/ai-agent-framework-study/conversation-context/design.md#31-缓存不变量)）：

| 编号 | 不变量 |
| --- | --- |
| I1 | 系统消息（包括 DeepAgents 追加的部分）只取决于代码版本和模型的 harness profile，不随时间、用户、会话、渠道、集群、来源能力、权限、Skills 选择而变化 |
| I2 | 工具定义（名称、描述、参数 schema、顺序）只取决于代码版本 |
| I3 | 历史消息一旦进入检查点，之后每次请求的序列化结果逐字节相同；例外只有摘要事件（一次性替换）和异常修复（给悬空的工具调用补结果） |
| I4 | 会变的信息只能放进当轮新增用户消息开头的 `<kubedoor_turn_context>` 块（`runtime.build_turn_context`）；子 Agent 放在它系统提示的末尾，审批恢复时沿用本轮开始时的块 |
| I5 | 同一会话中，请求的 messages 只追加、不修改 |
| I6 | 中间件对请求的改写必须是确定性的，不得读取时间、随机数或外部状态来修改提示或历史 |

- 不启用 DeepAgents 的旧参数截断（`truncate_args_settings=None`）和工具结果清理（`ContextEditingMiddleware`）；不给 DeepSeek 传 `user_id`（会按用户隔离缓存）。
- 中间件的 `before_model` / `after_model` 只返回内容实际改动的消息（按消息 ID 替换），没有改动时返回 `None`。messages 用 DeltaChannel 增量存储，返回整个消息列表会让检查点写入随轮数二次增长。
- 不要每次调用模型都扫描整个历史：消息在产生时处理一次，之后不再改动。key 脱敏就是这样做的：用户消息构造时脱敏，模型输出在 `wrap_model_call`、工具结果在 `wrap_tool_call` 里脱敏（工具结果可能是 `ToolMessage`，也可能装在 `Command` 的状态更新里）。全量扫描的耗时随对话增长，2 MB 历史时每次约 0.2 秒，而且阻塞 event loop。
- 摘要、会话标题、记忆总结这类不带工具的辅助请求用 `models.without_thinking` 关闭思考；摘要请求不经过 agent 中间件，它的用量靠回调（`SummaryUsageCallback`）记录。
- 声明式子 Agent 不继承主 Agent 的中间件覆盖，新增的中间件要同时加进子 Agent 的 `middleware`。
- 每轮用户消息在检查点里的 id 固定为 `user-<run id>`，重新生成 / 编辑后重发靠它找到要删掉的那一轮，不要改这个格式；只有 `kubedoor_ai_runs.rewindable=true` 的 run 才有。
- 守护测试：`tests/test_cache_stability.py`（按 DeepSeek 实际请求格式录制 payload，检查 system、tools、历史在轮次、会话、渠道、审批恢复之间逐字节稳定）、`tests/test_summarization.py`（摘要只让前缀变化一次）、`tests/test_runtime.py` 的检查点写入用例。这几组测试失败时先修代码，不要改断言。

## 测试

从仓库根目录安装测试依赖并运行：

```bash
python -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple/ pytest pytest-asyncio aiohttp
python -m pytest -q src/kubedoor-tools/tests
python -m pytest -q src/kubedoor-ai/tests
```

单测使用本地脚本模型、内存 checkpoint 和模拟执行器，不访问真实集群或收费模型。
PostgreSQL 集成测试默认跳过，需显式指定隔离测试库：

```bash
AI_TEST_PG_DATABASE=kubedoor_ai_test \
AI_TEST_PG_HOST=127.0.0.1 AI_TEST_PG_PORT=5432 \
AI_TEST_PG_USER=postgres AI_TEST_PG_PASSWORD=test \
python -m pytest -q src/kubedoor-ai/tests/test_postgres_integration.py
```

集成测试只接受 localhost 与 `kubedoor_ai_test` 前缀数据库，使用模拟模型、master
和本地 Kubernetes HTTP 服务验证会话持久化、SSE 恢复、精确批准、取消/重启不重放、
加密 kubeconfig 和 MCP HTTP/SSE。Docker 构建及真实模型效果需在部署环境验证。
