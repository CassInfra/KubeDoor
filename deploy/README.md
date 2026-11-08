# KubeDoor 部署

首次安装只需要一个 [install.sh](install.sh)。将它单独复制到能通过 `kubectl` 访问集群的机器，用 `sh` 或 `bash` 执行；数据库、时序库和管理员信息都在网页中填写。

```text
deploy/
├── install.sh                 # 内含镜像版本、引导 YAML 和安装逻辑
├── postgres/                  # 可选：独立 PostgreSQL
└── victoria-metrics/          # 可选：独立单机 VictoriaMetrics
```

## 网页向导安装（推荐）

将 `install.sh` 放在任意目录后执行：

```sh
sh install.sh
# 也支持：bash install.sh
```

执行机器只需要 `sh`（或 `bash`）和已配置的 `kubectl`，不需要 Python、PyYAML、jq 或完整源码。脚本内嵌引导 YAML，通过 `kubectl apply` 创建资源。默认镜像面向 amd64 节点；其他架构需要使用对应架构的镜像。使用包含此安装能力的新版 setup、web、master、AI、alarm 镜像。

命令使用当前 context，在 `kubedoor` 命名空间创建 Web、临时 setup 服务及必要权限。setup 在集群内生成安装密钥，脚本读取并打印密钥和访问地址。打开网页后按步骤操作：

1. **环境检查**：核对目标集群及部署权限。
2. **PostgreSQL**：填写地址、库名和凭据，点“测试连接”检查真实连接、认证及建表权限（库不存在时用该账号自动创建），结果显示在本页，通过后再点“继续”。SSL 默认禁用，需要加密连接时改选 SSL 模式。
3. **时序库**：选择 VictoriaMetrics 单机版或集群版，连接地址只填主机名或 IP（不填端口），自动生成可修改的远程读 / 写地址；“测试连接”检查查询，并查找带集群标签的数据，通过后再点“继续”。
4. **基础设置**：设置 `kubedoor` 管理员密码；KubeDoor 访问地址自动获取（当前打开页面的地址，端口转发打开时用 Web 的 NodePort 地址），有域名或负载均衡时可修改；选择是否安装告警组件（vmalert、Alertmanager、kubedoor-alarm，默认安装）。Grafana 看板固定安装。
5. **自动安装**：分别显示 Web、控制服务、AI、Grafana 和告警组件的实际状态。
6. **安装 Agent（可选）**：控制端全部就绪后，可以直接在本集群安装 Agent（填写集群名称，选择是否安装 vmagent、kube-state-metrics、node-exporter），也可以跳过直接完成。本集群的 Agent 直连控制服务，vmagent 直接写入第 3 步的远程写地址。

全部所选服务就绪、最终连接检查及安装收尾通过后，才会通知成功。数据库与时序库凭据存放在 K8S Secret 中。完成后临时安装服务停用并撤销部署权限；其他集群通过“接入第一个集群”进入 Agent 管理页，上传 kubeconfig 安装 Agent。

**第 6 步用到的临时集群权限**：Agent 需要集群管理员级别的权限，安装方也必须有同等权限。因此引导资源里额外创建专用账号 `kubedoor-agent-installer` 并绑定 `cluster-admin`；安装服务平时不持有它，只有在第 6 步选择安装 Agent 时才签发一小时有效的令牌使用。Agent 装完、选择不装、或 2 小时内没有选择（自动按不装处理），收尾时都会删除这个绑定和账号。

- 创建这个绑定需要集群管理员权限；执行 `install.sh` 的身份没有该权限时只打印警告，控制端照常安装，第 6 步显示“未授予”，可在安装完成后到控制台上传 kubeconfig 接入本集群。
- 用 `--setup-tag` 指定安装服务镜像时，版本不能低于脚本本身：旧版本的收尾任务不会删除这个绑定。
- 全新安装发现同名绑定残留时会停止，确认不再使用后手工删除：`kubectl delete clusterrolebinding kubedoor-agent-installer-<命名空间>`。收尾前若有人手工删掉了 `kubedoor-agent-installer` 账号，绑定也需要这样手工删除。
- 用 `--export` 导出的引导文件同样包含这个绑定，重新 `kubectl apply` 会重新授予该权限，只用于全新安装。

**集群指标标签名必须与现有监控一致。** 全新部署可以保持 `origin_prometheus`；例如指标为 `cluster="prod-k8s"`，向导填写 `cluster`，接入 Agent 时集群标识填写 `prod-k8s`。新装 Agent 自动继承该标签名。远程读 / 写地址按类型自动生成（单机版 8428，集群版读 8481、写 8480）。各集群的 vmagent 直连第 3 步的远程写地址（账号口令放进目标集群的 Secret），这个地址需要能从各业务集群访问。

## 镜像版本与启动参数

镜像仓库、KubeDoor 组件版本和第三方镜像都放在 [install.sh](install.sh) 顶部的镜像配置区，无需额外 `.conf` 文件。安装前可以直接改这里，或者用启动参数覆盖：

```bash
sh install.sh --context production --namespace kubedoor --nodeport 30080
sh install.sh --image-repo registry.example.com/kubedoor --tag 2.6.0 \
  --image-pull-secret registry-pull
sh install.sh --render     # 只预览引导资源，不连接集群、不生成凭据
```

`--tag` 覆盖 setup、web、master、agent、AI、alarm 的共同版本；第三方组件仍使用脚本中的版本。拉取 Secret 必须已存在于目标命名空间。版本必须已经构建并发布到对应仓库。

首次启动会将版本配置传给 setup。`sh install.sh web` 也可使用，`web` 参数可以省略。**安装完成后修改脚本里的版本不会自动升级集群；当前向导用于首次安装，尚不提供网页升级。**

## 导出 YAML 后安装

也可以先导出一个完整的引导文件，再交给目标集群执行 `kubectl apply`。导出不连接集群，不需要本机安装 kubectl、Python 或 jq；支持 `sh`、`bash`，使用系统随机设备生成本次安装标识，文件中不包含安装密钥或数据库密码。

```sh
sh install.sh --export kubedoor-install.yaml
kubectl apply -f kubedoor-install.yaml
```

命名空间、镜像版本和端口选项同样适用于导出，例如：

```sh
sh install.sh --namespace kubedoor --tag 2.6.0 --nodeport 30080 \
  --export kubedoor-install.yaml
```

文件已存在时拒绝覆盖。使用 `--export -` 可将纯 YAML 输出到标准输出。`--render` 仍只用于预览，请用 `--export` 生成安装文件。每套全新实例应分别导出，避免复用安装标识；目标集群的 context 由执行 `kubectl apply` 时选择。

应用后等待两个服务就绪，再读取安装密钥及访问端口：

```sh
kubectl -n kubedoor rollout status deployment/kubedoor-setup --timeout=240s
kubectl -n kubedoor rollout status deployment/kubedoor-web --timeout=240s
kubectl -n kubedoor get secret kubedoor-setup-auth \
  -o 'go-template={{index .data "SETUP_KEY" | base64decode}}{{"\n"}}'
kubectl -n kubedoor get service kubedoor-web -o 'jsonpath={.spec.ports[0].nodePort}{"\n"}'
```

打开 `http://<K8S 节点地址>:<上一步的 NodePort>`，输入安装密钥进入向导。指定其他命名空间时，请替换上述命令中的 `kubedoor`。

手边没有 kubectl 时，把导出的文件导入 K8S 管理平台或云厂商控制台的「YAML 创建」部署，在平台里查看 Secret `kubedoor-setup-auth` 的 `SETUP_KEY`（安装密钥）和 Service `kubedoor-web` 的 NodePort；导出时加 `--nodeport` 固定端口会更方便。

导出文件用于首次安装。完成安装后不要用它升级或恢复：再次应用会把 Web 配置改回引导配置。已有安装的进度查看及恢复继续使用 `sh install.sh`，并指定原来的 namespace 和 context。

## 准备外部数据库

已有 PostgreSQL、VictoriaMetrics 时直接使用，无需再部署（网页安装只支持 VictoriaMetrics 单机版或集群版）。连接地址必须能从 K8S Pod 访问。

没有 PostgreSQL 时，从仓库根目录执行：

```bash
cd deploy/postgres
cp .env.example .env
vi .env                     # 至少修改 PG_PASSWORD
docker compose up -d
```

不需要手动建表；控制服务和 AI 启动时会自动创建各自表结构。使用已有 PostgreSQL 时，准备一个专用账号即可，不要求超级用户：账号有建库权限（`CREATEDB`）时，安装向导检测时自动创建数据库；没有时由数据库管理员先执行 `CREATE DATABASE kubedoor OWNER kubedoor;`（库名、账号按实际填写）。

没有时序库时，从仓库根目录执行：

```bash
cd deploy/victoria-metrics
cp .env.example .env
vi .env                     # 至少修改 VM_PASSWORD
docker compose up -d
```

单机 VM 默认镜像为 `registry.cn-shenzhen.aliyuncs.com/starsl/victoria-metrics:stable`。两个独立服务各自通过 `.env` 管理镜像、端口和凭据，数据卷独立于 KubeDoor。详见 [VictoriaMetrics 部署与迁移说明](victoria-metrics/README.md)。

## 失败恢复

安装过程中刷新或关闭网页不会取消任务。失败时查看对应服务和检查项的原因，修复后在页面重试；不会把已有 KubeDoor 数据库当作空库重新初始化。

如果停留在收尾阶段，修复镜像拉取或调度问题后，重新执行原来的 `sh install.sh`，保留原 context 和 namespace。命令会保留正在运行的收尾 Job，只重建本次安装中失败或缺失的收尾 Job。每次 Job 最长运行 10 分钟；恢复不重置数据库、不重新生成安装密钥。已完成的安装不会重新开放初始化。

## 旧部署与内部资源

旧配置式安装、升级和卸载入口保存在 [tools/legacy-install](../tools/legacy-install/README.md)，供已有脚本部署继续维护；该兼容工具仍需要完整仓库、Bash、Python 和 PyYAML。请沿用原配置，并显式传入 `-c`；不要用全新网页安装覆盖旧部署。

部署模板、看板及运行默认值已移至 `src/kubedoor-setup/resources/`，Agent 模板位于 `src/kubedoor-master/agent_install/manifests/`，随相应镜像发布。测试和构建工具也归入源码目录，无需用户配置。

旧配置和相邻密钥文件可备份到忽略目录 `.local/kubedoor/legacy-deploy/`，该目录不会提交到仓库，网页入口也不会读取其中内容。旧部署维护时仍需显式指定原配置，并妥善备份密钥。
