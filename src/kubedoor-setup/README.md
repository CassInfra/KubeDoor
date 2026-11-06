# KubeDoor 首次安装服务

此服务随独立脚本 `sh install.sh` 创建，独立于应用数据库运行。只在初次部署期间用于验证外部 PostgreSQL/时序库、渲染并部署控制端、跟踪每项服务状态。安装密钥由 setup 在集群内生成，用户使用启动命令读取并输出的密钥进入向导。执行安装脚本的机器不需要 Python；Python 运行环境包含在 setup 镜像中。全部服务及最终检查完成后，收尾 Job 停用安装服务并撤销临时权限。

从仓库根目录构建镜像（发布流程使用 `build.sh setup <版本>`）：

```bash
docker build -f src/kubedoor-setup/Dockerfile -t kubedoor-setup:dev .
```

镜像只打包发行默认值、master 资源模板、看板和必要共享模块；不包含真实 `kubedoor.conf` 或本地密钥。渲染器 `kubedoor_setup.render.render_master` 先解析 YAML 再替换标量，不执行用户提交的 shell 内容。渲染本身不连接 Kubernetes。

安装部署权限保存在 `kubedoor-setup` 的命名空间 Role 中，普通 Web 不持有该服务账号。`kubedoor-setup-finalizer` 的权限仅限本安装器部署、状态 ConfigMap、临时凭据和安装器 RoleBinding。安装状态位于 `kubedoor-setup-state`，敏感草稿位于 Secret；数据库中断不会触发新的初始化。

第 6 步“在本集群安装 Agent”需要集群管理员级别权限，单独放在专用账号 `kubedoor-agent-installer`（引导时绑定 `cluster-admin`，绑定名 `kubedoor-agent-installer-<命名空间>`）上：setup 平时不持有它，只在用户选择安装时用 TokenRequest 签发一小时令牌，拼成 kubeconfig 交给 master 的 Agent 安装流水线（`/api/admin/agent-installations`），令牌不写入状态和日志；探测可用性时用 10 分钟令牌做 SelfSubjectAccessReview。收尾 Job 先用该账号自己的令牌删除这个 ClusterRoleBinding，再删除账号，最后才删除 setup 的 RoleBinding。相关逻辑在 `kubedoor_setup/agent.py`（安装流程）、`api.py`（`/setup-api/agent/install`、`/setup-api/agent/skip` 和看门狗）、`finalize.py`（撤销）。

内部资源位于本目录的 `resources/`（运行默认值、master 模板、看板）；发行镜像版本来自 `deploy/install.sh` 顶部的镜像配置区。脚本本身内嵌引导 YAML，兼容 POSIX `sh` 和 `bash`，可以脱离仓库单独运行。渲染器只读取脚本的镜像配置块，不执行 shell。部署相关回归测试位于 `tests/deployment/`。旧配置式脚本保留在 `tools/legacy-install/`，仍需要完整仓库及原来的 Python 依赖。
