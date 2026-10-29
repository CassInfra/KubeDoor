# KubeDoor 账号与权限(auth)

master 统一负责认证、授权与审计,nginx 不再做认证。本目录是实现,部署和使用说明见
[deploy/README.md](../../../deploy/README.md#账号与权限)。前端的登录页在 `src/kubedoor-web/src/views/login/`,
用户与权限、权限与审批、LDAP 设置、操作审计页面在 `src/kubedoor-web/src/views/system/`。

## 权限模型

| 全局角色 `role` | 没有单独授权时的集群权限 | 说明 |
|---|---|---|
| `admin` 系统管理员 | 管理员 | 所有集群都是管理员,不能再设集群授权;另外能做系统管理:用户与权限、LDAP、企微 AI 机器人、Agent 管理、权限与审批(`/api/admin/*` 等) |
| `cluster_admin` 集群管理员 | 管理员 | 可以按集群单独授权降低;不能做系统管理 |
| `rw` 读写 | 读写 | |
| `read` 只读(默认) | 只读 | |
| `none` 无 | 无 | 只能访问单独授权的集群 |
| `agent` Agent 账号 | 无 | 只能注册 agent(`/ws`),不能登录 Web,不能调用其它接口 |

集群授权 `auth_user_clusters(user_id, env, permission)`,`permission` 取 `admin` / `rw` / `read` / `none`,**优先于全局角色**。
某个集群的有效权限:系统管理员一律 `admin`;有授权用授权;否则按全局角色兜底(`Principal.permission`)。
有效权限是 `read` 及以上就能查看该集群;重启、删 Pod、改 CoreDNS 这类操作再查下一节的操作权限表(`Principal.can(op, env)`)。
与集群无关的资源只看全局角色:没有限定集群的屏蔽规则、节点均衡全局配置按全局角色对应的那一列查操作权限表(`can_global`);
节点均衡的配置、计划等查看要 `global_read`(admin/cluster_admin/rw/read);AI 共享记忆的写入要全局 admin/cluster_admin/rw(AI 服务判断)。

内置账号(`seed.py`,master 每次启动检查,已存在不覆盖):

- `kubedoor`:内置超级管理员(`builtin and role == 'admin'`)。只能自己在「修改口令」里改密,
  `/api/admin/*` 对它的任何操作都返回 403;出厂口令登录后强制改密(`must_change_password`)。
- `Up4biLko1dNh`:内置 Agent 账号,出厂口令与旧版 nginx 预置账号相同,已部署的跨集群 agent 升级后不用改。
  不能删除;系统管理员可以重置口令。

## 权限与审批(`permissions.py`)

Web「系统管理 → 权限与审批」(`/system/permissions`)配置每种集群权限能做哪些操作、AI 做这些操作前要不要提问人确认。
只有系统管理员能看和改,配置和具体集群无关。

**操作权限表**:行是操作类型(`OPERATIONS`,25 行),列是集群有效权限 只读 / 读写 / 管理员,外加「审批」列。

- 判定:`principal.can(op, env)` = 这一行在该集群有效权限那一列是否勾选。没有权限直接 403 `operation_forbidden`
  (「当前账号在集群 X 是「只读」权限,不能执行「扩缩容」」),AI 也不会建审批;看不到的集群是 `cluster_forbidden`。
- 查询不在表里,有效权限 `read` 及以上都能做;例外是「查看 Secret 内容」。系统管理不在表里,只看是不是系统管理员。
- 「管理员」列也可以取消:取消后包括系统管理员在内谁都不能做(相当于全站关闭)。勾选保持 只读 ⊆ 读写 ⊆ 管理员
  (页面联动,保存时校验)。
- 审批列只对 AI 生效(网页 AI、企微 AI、外部 MCP 规则一样):有权限时,涉及的操作里任意一个勾了审批就要提问人确认,
  否则直接执行。高危命令固定审批;审批列为「—」的行不是 AI 操作(或者是查看),不审批。
- 默认值 = 改造前的行为:每一行都勾读写和管理员、不勾只读;只有「CoreDNS 修改」「Istio 修改」只勾管理员。

| 分组 | 操作类型 | 适用 | 默认审批 |
|---|---|---|---|
| 终端与命令 | `pod.shell` 网页 Pod 终端、`node.shell` 网页节点终端 | 网页 | — |
| | `pod.exec.view` / `pod.exec.other` AI 在 Pod 内执行查看命令 / 其他命令 | AI | 不审批 |
| | `pod.exec.danger` AI 在 Pod 内执行高危命令 | AI | 固定审批 |
| 工作负载 | `workload.restart` 重启、`workload.scale` 扩缩容(含写入管控副本数)、`workload.schedule` 定时 / 周期重启和扩缩容、`workload.image` 更新镜像 | 网页+AI | 审批 |
| Pod | `pod.delete` 删除 Pod、`pod.isolate` 隔离 Pod、`pod.jvm` JVM 诊断 | 网页+AI | 审批 |
| 资源 | `resource.apply` 新建 / 编辑资源 YAML、`resource.delete` 删除资源 | 网页+AI | 审批 |
| | `resource.secret` 查看 Secret 内容 | 网页+AI | — |
| | `ai.k8s_write` AI 通用修改(kubectl / istioctl / K8S API,页面标红) | AI | 审批 |
| | `ai.diagnostic` AI 诊断工具里会发请求或写文件的(curl、openssl、nc、nmap、tcpdump、iperf3、`yq -i`) | AI | 不审批 |
| 节点 | `node.cordon` 禁止 / 允许调度、`balance.run` 节点均衡 | 网页+AI | 审批 |
| 资源管控 | `control.edit` 资源管控(管控服务、requests/limits、JVM,采集高峰期数据) | 网页+AI | 审批 |
| 网络 | `network.coredns` CoreDNS 修改、`network.istio` Istio 修改 | 网页+AI | 审批 |
| | `network.tcp` 端口测试 | 网页 | — |
| 告警 | `alert.operate` 告警处理状态、`alert.silence` 告警屏蔽 | 网页 | — |

**目标识别**:不管从哪个入口(网页资源 YAML 编辑 / 删除,AI 的 `resource_apply`、`resource_delete`、kubectl、K8S API),
改 kube-system 的 ConfigMap `coredns` 还要「CoreDNS 修改」,改 Istio 资源(networking / security / telemetry /
extensions.istio.io 组,以及 Istio 的 kind 和简称)还要「Istio 修改」;判断不了的(YAML 解析失败等)按两个都涉及。
master 在 `identify_targets_master` 里自己实现一份(master 镜像没装 kubedoor-tools),AI 和 agent 用 kubedoor-tools 的 `identify_targets`。
AI 通用工具(kubectl / istioctl / K8S API / Pod 内命令 / 诊断)怎么归到操作类型见 [kubedoor-tools](../../kubedoor-tools/README.md#操作分类)。

**命令清单**:查看命令、高危命令两个清单,只用于给 AI 在 Pod 内执行的命令(`pod_exec`、`kubectl exec`)分类;
网页 Pod 终端是交互式 shell,不受清单影响,只能按「网页 Pod 终端」整体放开或关闭。
默认清单在 `default_commands.json`,和 `kubedoor-tools/kubedoor_tools/default_commands.json` 必须一致(两边都有测试比较)。
保存时校验:每项只填命令名(不带路径),最长 64 个字符,不能有引号和 shell 元字符;查看命令只能是单个命令名;
高危命令可以是「命令」「前缀*」(如 `mkfs*`)或「命令 参数」(如 `find -delete`);两个清单不能有同一个命令(「命令 参数」不算);
每个清单最多 300 项。查看命令的内置参数检查(`BUILTIN_CHECKS`,只是给页面的说明,检查在 kubedoor-tools 里做)不满足时
这条命令只是不算查看命令,继续按高危 / 其他命令判断,不是禁止执行。

**存储与读取**:

- `auth_settings` 的 `permission_policy`:`{"ops": {key: {"read", "rw", "admin", "approval"}}, "commands": {"view": [...], "danger": [...]}}`,
  另记 `updated_by`、`updated_at`。读取时和代码里的默认值合并:新加的操作类型用默认值,已删除的 key 忽略,固定的审批值以代码为准;
  库里被直接改成不单调时按少给的方向收紧。
- master 进程内缓存,保存后立即更新,另有 10 秒 TTL 兜底。鉴权中间件每个请求先 `await permissions.load_policy()`(没过期不查库),
  之后 `principal.can` 和 handler 里的判断都读这份缓存。读库失败时有旧缓存就沿用,从没读到过就返回 503 `policy_unavailable`。
- 接口:`GET /api/admin/permission-policy` 返回行定义、当前值、默认值(「恢复默认」用)和内置参数检查说明;
  `PUT /api/admin/permission-policy` 保存,不合法返回 400,保存记审计 `permission.policy`(detail 是改动差异)。
- 前端:`/api/auth/me` 带 `operations: {操作: [允许的集群权限级别]}`,前端 `can(op, env)` 用它和 `cluster_permissions`
  置灰按钮;只给权限,不含审批和命令清单。
- AI 服务:从 `GET /api/ai/internal/permission-policy`(`{version, ops, commands}`)获取,缓存 5 秒,取不到时修改类一律拒绝、
  查询不受影响。AI 经 agent 执行时,master 的 `agent-execute` 按 AI 判定的 `ops` 用库里的权限复核,再给 agent 下发
  `allowed_ops`、`ops`、`commands`、`env`(agent 用清单重新给 Pod 内命令分类);旧 agent 只认 `permission`(read / rw),
  声明的操作都允许时给 `rw`。

## 认证

`middleware.py` 按以下顺序解析身份(`Principal`):

1. `X-Kubedoor-Token`(= `AI_INTERNAL_TOKEN`):集群内部服务。带 `X-User-Name` 时代表该用户
   (AI 服务),**权限按库里的用户重新计算**,不采信 `X-User-Permission`;不带用户名就是系统身份(CronJob)。
   token 不对直接 401。nginx 会清掉外部请求带的这个头。这类调用不受「先改初始口令」限制:
   网页 AI 助手和 MCP 的入口已经在 `/api/auth/verify` 拦过,企微机器人用不到账号的口令;停用的账号照样拒绝。
2. 会话 Cookie `kubedoor_session`:浏览器登录后的 HttpOnly + SameSite=Strict Cookie。库里只存 token 的
   SHA-256;不勾「记住我」12 小时、空闲 4 小时过期,勾选 7 天(`AUTH_*`)。改密、禁用、重置口令、
   删除账号会删掉对应会话。
3. `Authorization: Basic`:只接受非浏览器客户端(没有 `Sec-Fetch-*` 头):agent、MCP 客户端、脚本。
   浏览器缓存的旧 basic auth 凭据一律忽略,避免被跨站请求利用。校验结果缓存 5 分钟,口令一改缓存失效。

口令校验(`authn.py`):本地账号优先(scrypt + 由 `AI_ENCRYPTION_KEY` 派生的 pepper,`passwords.py`;
参数变了或老哈希没加 pepper,登录成功时自动重新哈希;数据库被读走也无法离线破解);
本地没有该用户名再走 LDAP(`ldap_auth.py`,服务账号搜索 → 用户 DN 绑定,或用户 DN 模板直接绑定;
拒绝空口令、过滤器转义、结果必须唯一),首次登录按 LDAP 设置自动开通。用户不存在时也做一次同等开销的哈希,
防止按响应时间枚举用户名。同一账号 + 来源 IP 连续失败 5 次锁 15 分钟,同一账号累计 20 次也锁(`ratelimit.py`)。

浏览器的跨站请求(`Sec-Fetch-Site` 为 `cross-site` / `same-site`)一律 403,防 CSRF 和跨站 WebSocket 劫持;
加上 SameSite=Strict Cookie,不需要额外的 CSRF token。

## 授权

`policy.py` 的 `RULES` 按顺序匹配 `(方法, 路径)`,第一条命中生效:

| kind | 含义 |
|---|---|
| `public` | 不需要登录(登录、退出、`/api/auth/verify`) |
| `session` | 任何已登录的人,包括必须先改密的会话(`/api/auth/me`、`/api/auth/password`) |
| `login` | 任何已登录的人,handler 自己按可见集群过滤数据 |
| `global_read` / `global_write` / `admin` | 全局角色(只读及以上 / 读写及以上 / 系统管理员) |
| `env_read` | 目标集群(query/body 里的 env、k8s 等)可读,集群参数必填 |
| `env_read_opt` | 带了集群参数就校验,没带由 handler 按 `env_condition` 过滤 |
| `op` | 操作权限表里的一个操作(`rule.op`),每个目标集群都要允许,集群参数必填 |
| `op_any` | 至少一个集群允许 `rule.op`(粗检,handler 再按具体集群判断;Istio 的修改用) |
| `env_write` | 目标集群读写及以上(没登记的 `/api/*` 兜底) |
| `custom` | 自定义校验:改镜像(有「更新镜像」直接放行,没有的只读用户走 `UPDATE_IMAGE` 白名单)、Secret 内容(「查看 Secret 内容」)、资源 YAML 编辑 / 删除(再加目标识别)、管控表新增 / 编辑(本集群和同步的集群都要「资源管控」)、节点均衡全局配置(按全局角色那一列)、屏蔽规则(按 matcher 里的 env,没限定集群的按全局角色那一列)、告警处理(按告警所属集群) |
| `agent` / `internal` | agent 注册通道 / AI 服务内部桥 |

**没登记的 `/api/*` 按「目标集群读写及以上」处理(默认拒绝)**。新增接口时在 `RULES` 里登记;
`tests/test_auth_policy.py::test_every_master_route_has_explicit_rule` 会检查 master 自己注册的路由都有显式规则。
修改类接口登记成 `op`,归到操作权限表的一个操作类型(实在归不进去再新增操作类型:master `OPERATIONS`、kubedoor-tools 的 `OP_KEYS`、
AI 的 `permissions.OP_NAMES`、agent 的 `OP_LABELS` 一起加,前三处有测试比对)。转发给 agent 的接口里,纯查询的 GET 在 `_AGENT_READ_PATHS`
里登记为只读;修改类规则不限方法,因为有些 GET 接口其实在做写操作(`/api/pod/delete_pod`、`/api/pod/auto_dump` 这类会 exec 的、
`/api/admis_switch`),不能按方法判断。

Istio 的修改在这里只粗检(`op_any`),handler 再按组(组内每个集群)或目标集群细检,见 [istio_route/README.md](../istio_route/README.md#接口)。

handler 里取身份:`from auth import principal_of, env_condition`;操作权限用 `principal.can(op, env)` / `can_global(op)`
(读的是 `permissions` 的缓存,鉴权中间件已经刷新过)。按集群过滤列表数据:

```python
params = []
condition = env_condition(principal_of(request), 'env', params)  # 全部可见时返回 None
```

## 给 nginx 用的 `/api/auth/verify`

AI 助手、MCP、Grafana、自定义告警不经过 master,nginx 用 `auth_request` 子请求问 master:

| `for=` | 认证方式 | 要求 | 返回头 |
|---|---|---|---|
| `session` | 会话 Cookie | Web 用户,不需要改密 | `X-Kubedoor-User` / `X-Kubedoor-Role` / `X-Kubedoor-Clusters`(JSON) |
| `mcp` | Cookie 或 Basic | 同上 | 同上 |
| `grafana` | 会话 Cookie | 同上,且能看到全部集群(Grafana 做不到按集群隔离) | 另有 `X-Kubedoor-Grafana-Role`(系统管理员 Admin,其他 Viewer) |
| `alert` | Basic | Agent 账号或全局角色读写及以上 | 同上 |

nginx 把这些值写进 `X-User-Name` / `X-User-Permission` / `X-User-Clusters`(AI 服务)或
`X-WEBAUTH-USER` / `X-WEBAUTH-ROLE`(Grafana auth proxy),客户端自己带的同名头会被覆盖。
AI 服务据此算出各集群的有效权限,再按「权限与审批」判断能否执行、要不要批准;它回调 master 时 master 再按库里的权限复核一次。

## 数据表(`db.sql`)

`auth_users`、`auth_user_clusters`、`auth_sessions`、`auth_settings`(LDAP 配置,绑定口令用
`AI_ENCRYPTION_KEY` 做 AES-GCM 加密;权限与审批 `permission_policy`)、`audit_logs`(登录、账号与权限变更、所有写操作和越权被拒,
`RETENTION_AUDIT_DAYS` 天后清理)。旧库升级时 `db.sql` 用 DO 块给 `ck_auth_users_role` 加上 `cluster_admin`、
给 `ck_auth_user_clusters_perm` 加上 `admin`(按约束定义判断,改过的不再改)。

## 运维

- 忘记口令:`kubectl -n kubedoor exec -it deploy/kubedoor-master -- python3 -m auth.reset_password <用户名>`
- agent 认证失败会在 master 日志里打 `[鉴权] 拒绝 agent 连接`(同一来源每分钟一条)。
- 升级过渡期可以设 `AGENT_AUTH_REQUIRED=false` 临时允许不带口令的 agent 连接。
