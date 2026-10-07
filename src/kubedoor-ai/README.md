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
DeepSeek 当前默认启用思考模式，无需额外配置思考开关。服务使用
`langchain-deepseek==1.1.1`，保留普通及流式响应中的 `reasoning_content`，并补齐
后续工具调用和多轮请求中的回传，避免思考模式的上下文丢失。工具选择采用模型默认
行为，不强制 `tool_choice=required` 或指定工具。当前沿用 Chat Completions，
暂不接入 Responses API；前端仅支持文本输入。各项官方文档核对见
[DeepSeek 配置与兼容性](../../docs/ai-assistant.md#deepseek-配置与兼容性)。

模型 API key 仅在本次运行内存中使用，不作为配置写入数据库、checkpoint、日志或
外部 trace。输入、模型输出和跨流式片段中回显的本次 key 会在持久化前过滤。
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

## 全局记忆库

所有登录用户可查看、引用全局记忆，读写用户可手动新建、编辑、删除；会话历史
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

`resource_config_update` 保存 Deployment 的数据库资源/JVM 管控配置，下次发布或
滚动重启时应用，不自动重启。仅改 JVM 时先查询并保留必填的 `limit_mem_mb`、
`limit_cpu_m`、`pod_count_manual` 当前值；JVM 字段省略保留、显式 `null` 清除。
`resource_pod_count` 仅保存数据库人工管控副本数，立即扩缩容仍使用
`scale_deployment`。数据库保存成功与在线资源生效须分别确认。

旧 Agent 仍可提供原有目录接口；通用工具要求 Agent 在线且声明 `ai_tools` 能力，
未具备该能力时会提示升级。已登记的离线集群仍可查询 PostgreSQL 历史资源、JVM
数据与监控指标，实时操作取决于 Agent 或直连连接是否可用。

## 人工批准与 kubeconfig

只读查询自动执行；修改、未知 CLI 与 Pod exec 先冻结目标、参数、预检结果和差异，
展示给用户批准后执行。批准与执行使用同一动作 ID 和持久化派发记录，不会在写失败后
自动换来源重试。停止或取消无法撤销 Kubernetes 已接受的修改，应查询实际状态确认结果。

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

所有浏览器、Skills、工具与 MCP 入口由可信 nginx 注入 `X-Kubedoor-Token`、
`X-User-Name` 和 `X-User-Permission`（`read`/`rw`）。代理不得记录包含模型 key
或 kubeconfig 的请求 body。缺少有效加密密钥、内部令牌或数据库连接时启动失败；
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
