# KubeDoor 账号与权限(auth)

master 统一负责认证、授权与审计,nginx 不再做认证。本目录是实现,部署和使用说明见
[deploy/README.md](../../../deploy/README.md#账号与权限)。前端的登录页在 `src/kubedoor-web/src/views/login/`,
用户与权限、LDAP 设置、操作审计页面在 `src/kubedoor-web/src/views/system/`。

## 权限模型

| 全局角色 `role` | 没有单独授权时的集群权限 | 说明 |
|---|---|---|
| `admin` 管理员 | 读写 | 另外可以管理用户、LDAP、Agent(`/api/admin/*`、Agent 管理);不能再设集群授权 |
| `rw` 读写 | 读写 | |
| `read` 只读(默认) | 只读 | |
| `none` 无 | 无 | 只能访问单独授权的集群 |
| `agent` Agent 账号 | 无 | 只能注册 agent(`/ws`),不能登录 Web,不能调用其它接口 |

集群授权 `auth_user_clusters(user_id, env, permission)`,`permission` 取 `rw` / `read` / `none`,**优先于全局角色**。
某个集群的有效权限:管理员一律 `rw`;有授权用授权;否则按全局角色兜底(`Principal.permission`)。
与集群无关的资源(没有限定集群的屏蔽规则、节点均衡全局配置、AI 共享记忆)只看全局角色:
读要 `global_read`(admin/rw/read),写要 `global_write`(admin/rw)。

内置账号(`seed.py`,master 每次启动检查,已存在不覆盖):

- `kubedoor`:内置超级管理员(`builtin and role == 'admin'`)。只能自己在「修改口令」里改密,
  `/api/admin/*` 对它的任何操作都返回 403;出厂口令登录后强制改密(`must_change_password`)。
- `Up4biLko1dNh`:内置 Agent 账号,出厂口令与旧版 nginx 预置账号相同,已部署的跨集群 agent 升级后不用改。
  不能删除;管理员可以重置口令。

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
| `global_read` / `global_write` / `admin` | 全局角色 |
| `env_read` / `env_write` | 目标集群(query/body 里的 env、k8s 等)可读 / 可写,集群参数必填 |
| `env_read_opt` | 带了集群参数就校验,没带由 handler 按 `env_condition` 过滤 |
| `custom` | 自定义校验:改镜像(只读用户走 `UPDATE_IMAGE` 白名单)、Secret 内容(要读写)、管控表同步、屏蔽规则(按 matcher 里的 env)、Istio(按 VS 关联的集群)、告警处理(按告警所属集群) |
| `agent` / `internal` | agent 注册通道 / AI 服务内部桥 |

**没登记的 `/api/*` 按「目标集群读写」处理(默认拒绝)**。新增接口时在 `RULES` 里登记;
`tests/test_auth_policy.py::test_every_master_route_has_explicit_rule` 会检查 master 自己注册的路由都有显式规则。
转发给 agent 的接口里,纯查询的 GET 在 `_AGENT_READ_PATHS` 里登记为只读;注意有些 GET 接口其实在做写操作
(`/api/pod/delete_pod`、`/api/pod/auto_dump` 这类会 exec 的、`/api/admis_switch`),不能按方法判断。

handler 里取身份:`from auth import principal_of, env_condition`。按集群过滤列表数据:

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
| `grafana` | 会话 Cookie | 同上,且能看到全部集群(Grafana 做不到按集群隔离) | 另有 `X-Kubedoor-Grafana-Role`(管理员 Admin,其他 Viewer) |
| `alert` | Basic | Agent 账号或全局读写 | 同上 |

nginx 把这些值写进 `X-User-Name` / `X-User-Permission` / `X-User-Clusters`(AI 服务)或
`X-WEBAUTH-USER` / `X-WEBAUTH-ROLE`(Grafana auth proxy),客户端自己带的同名头会被覆盖。
AI 服务据此判断各集群能否执行、批准修改;它回调 master 时 master 再按库里的权限复核一次。

## 数据表(`db.sql`)

`auth_users`、`auth_user_clusters`、`auth_sessions`、`auth_settings`(LDAP 配置,绑定口令用
`AI_ENCRYPTION_KEY` 做 AES-GCM 加密)、`audit_logs`(登录、账号与权限变更、所有写操作和越权被拒,
`RETENTION_AUDIT_DAYS` 天后清理)。

## 运维

- 忘记口令:`kubectl -n kubedoor exec -it deploy/kubedoor-master -- python3 -m auth.reset_password <用户名>`
- agent 认证失败会在 master 日志里打 `[鉴权] 拒绝 agent 连接`(同一来源每分钟一条)。
- 升级过渡期可以设 `AGENT_AUTH_REQUIRED=false` 临时允许不带口令的 agent 连接。
