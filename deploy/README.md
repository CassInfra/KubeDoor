# KubeDoor 部署

首次安装和控制端升级只需要一个 [install.sh](install.sh)。将它单独复制到能通过 `kubectl` 访问集群的机器，用 `sh` 或 `bash` 执行；数据库、时序库和管理员信息都在网页中填写。

```text
deploy/
├── install.sh                 # 内含镜像版本、安装 YAML 和控制端升级逻辑
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
4. **基础设置**：设置 `kubedoor` 管理员密码；KubeDoor 访问地址自动获取（当前打开页面的地址，端口转发打开时用 Web 的 NodePort 地址），有域名或负载均衡时可修改；按需填写对象存储上传地址（可选，各集群的 Agent 上传 jstack / jmap 等诊断文件用，可以带“账号:密码@”）；选择是否安装告警组件（vmalert、Alertmanager、kubedoor-alarm，默认安装）。Grafana 看板固定安装。
5. **自动安装**：分别显示 Web、控制服务、AI、Grafana 和告警组件的实际状态。控制服务就绪后，安装服务把时序库类型和地址、集群标签名、访问地址、对象存储地址、网页安装 Agent 用的镜像写进控制台的「系统管理 → 通用配置」，再做健康检查。
6. **安装 Agent（可选）**：控制端全部就绪后，可以直接在本集群安装 Agent（填写集群名称，选择是否安装 vmagent、kube-state-metrics、node-exporter），也可以跳过直接完成。本集群的 Agent 直连控制服务，vmagent 直接写入通用配置里的远程写地址（即第 3 步填写的地址）。

全部所选服务就绪、最终连接检查及安装收尾通过后，才会通知成功。数据库凭据和时序库地址存放在 K8S Secret 中（vmalert、Grafana 使用）；其余配置在数据库里，见下面的[控制端配置](#控制端配置通用配置)。网页安装不再创建 ConfigMap `kubedoor-config` 和事件告警规则 ConfigMap。完成后临时安装服务停用并撤销部署权限；其他集群通过“接入第一个集群”进入 Agent 管理页，上传 kubeconfig 安装 Agent。

**第 6 步用到的临时集群权限**：Agent 需要集群管理员级别的权限，安装方也必须有同等权限。因此引导资源里额外创建专用账号 `kubedoor-agent-installer` 并绑定 `cluster-admin`；安装服务平时不持有它，只有在第 6 步选择安装 Agent 时才签发一小时有效的令牌使用。Agent 装完、选择不装、或 2 小时内没有选择（自动按不装处理），收尾时都会删除这个绑定和账号。

- 创建这个绑定需要集群管理员权限；执行 `install.sh` 的身份没有该权限时只打印警告，控制端照常安装，第 6 步显示“未授予”，可在安装完成后到控制台上传 kubeconfig 接入本集群。
- 用 `--setup-tag` 指定安装服务镜像时，版本不能低于脚本本身：旧版本的收尾任务不会删除这个绑定。
- 全新安装发现同名绑定残留时会停止，确认不再使用后手工删除：`kubectl delete clusterrolebinding kubedoor-agent-installer-<命名空间>`。收尾前若有人手工删掉了 `kubedoor-agent-installer` 账号，绑定也需要这样手工删除。
- 用 `--export` 导出的引导文件同样包含这个绑定，重新 `kubectl apply` 会重新授予该权限，只用于全新安装。

**集群指标标签名必须与现有监控一致。** 全新部署可以保持 `origin_prometheus`；例如指标为 `cluster="prod-k8s"`，向导填写 `cluster`，接入 Agent 时集群标识填写 `prod-k8s`。新装 Agent 自动继承该标签名。远程读 / 写地址按类型自动生成（单机版 8428，集群版读 8481、写 8480）。各集群的 vmagent 直连第 3 步的远程写地址（账号口令放进目标集群的 Secret），这个地址需要能从各业务集群访问。

## 控制端配置（通用配置）

安装完成后，时序库类型和地址、集群标签名、KubeDoor 访问地址、对象存储上传地址、网页安装 Agent 用的镜像仓库和采集组件镜像、节点终端镜像、数据保留天数、登录与安全等，都在控制台「系统管理 → 通用配置」修改，保存后生效，不用重启；K8S 事件告警规则在「系统管理 → 事件告警规则」修改，保存后马上生效。消息通知在「系统管理 → 消息通知」配置。

通用配置只管 KubeDoor 自己（master、alarm、ai 和之后安装的 Agent）。下面这些仍使用安装时写进 Secret 的地址，**改了时序库地址后要手动同步**：

- **vmalert 和 Grafana 数据源**（网页安装）：vmalert 从 Secret `kubedoor-connection` 读 `PROM_URL`、`METRICS_WRITE_URL`（账号密码按 URL 编码写在地址里）；Grafana 从 Secret `kubedoor-grafana-datasources` 读不带账号的地址和分开的账号、密码。

  ```sh
  NS=kubedoor
  kubectl -n $NS patch secret kubedoor-connection --type=merge -p \
    '{"stringData":{"PROM_URL":"http://monit:<URL编码的密码>@<新地址>:8428","METRICS_WRITE_URL":"http://monit:<URL编码的密码>@<新地址>:8428/api/v1/write"}}'
  kubectl -n $NS patch secret kubedoor-grafana-datasources --type=merge -p \
    '{"stringData":{"metrics-url":"http://<新地址>:8428","metrics-user":"monit","metrics-password":"<密码>"}}'
  kubectl -n $NS rollout restart deployment/vmalert deployment/kubedoor-dash
  ```

  地址按实际类型填写（集群版读 `:8481/select/<租户>/prometheus`、写 `:8480/insert/<租户>/prometheus`）。原来没有账号、现在要加账号（或反过来）时，还要把同一个 Secret 里 `datasources.yaml` 的 `basicAuth` 改成 `true`（或 `false`）。旧脚本安装的控制端改 `kubedoor.conf` 的 `PROM_URL` 后重新执行 `./install.sh master` 即可。
- **已安装 Agent 的集群里的 vmagent**：用的是安装时写进该集群的远程写地址，不随通用配置变化。在控制台对该集群重新安装 Agent（使用通用配置里的新地址），或手动修改该集群 Agent 命名空间里的 Secret `kubedoor-metrics-credentials` 和 Deployment `vmagent` 的 `-remoteWrite.url`。

## 镜像版本与启动参数

镜像仓库、KubeDoor 组件版本和第三方镜像都放在 [install.sh](install.sh) 顶部的镜像配置区，无需额外 `.conf` 文件。安装前可以直接改这里，或者用启动参数覆盖：

```bash
sh install.sh --context production --namespace kubedoor --nodeport 30080
sh install.sh --image-repo registry.example.com/kubedoor --tag 2.6.0 \
  --image-pull-secret registry-pull
sh install.sh --render     # 只预览引导资源，不连接集群、不生成凭据
```

`--tag` 覆盖 setup、web、master、agent、AI、alarm 的共同版本；第三方组件仍使用脚本中的版本。拉取 Secret 必须已存在于目标命名空间。版本必须已经构建并发布到对应仓库。

首次启动会将版本配置传给 setup。`sh install.sh web` 也可使用，`web` 参数可以省略。安装完成后，普通重复运行保留原安装；更新控制端请显式使用下面的 `upgrade` 命令。

## 更新已有控制端（包括 2.3.0）

每次升级先取得目标发布版本对应的新版 `deploy/install.sh`，单独复制到能访问控制端集群的机器。脚本内的固定版本就是默认升级目标；当前为 **2.6.0**，不会在运行时查询远程版本或使用浮动 `latest` 镜像。

先查看升级计划、实际使用的 Deployment 补丁，以及旧配置的迁移和清理步骤：

```sh
sh install.sh upgrade --context production --namespace kubedoor --dry-run
```

备份 PostgreSQL 后，执行升级：

```sh
sh install.sh upgrade --context production --namespace kubedoor \
  --backup kubedoor-before-2.6.0.yaml
# 自动化执行时添加 --yes，表示已完成数据库备份并确认升级。
```

`--context` 可以省略，默认使用当前 context；命名空间默认 `kubedoor`。脚本会打印实际位置和组件版本供确认。升级适用于旧脚本和网页向导创建的 Kubernetes 控制端；网页安装必须已经完成收尾。Docker 或自行改名的控制端 Deployment 需要按原部署方式维护。

升级同步更新已有的 `kubedoor-master`、`kubedoor-ai`、`kubedoor-alarm`、`kubedoor-web` 的**镜像和新版 Deployment 部署规范**，通过按名称合并的 strategic merge patch 加入卷、挂载、探针、更新策略等变更。镜像版本号没变时（例如同一版本重新构建了镜像），补丁不会改动 Deployment，脚本会滚动重启该组件，按 `imagePullPolicy: Always` 拉取这个版本最新的镜像。例如从 2.3.0 升级后，AI 会新增：

```yaml
# kubedoor-ai 的 spec.template.spec
volumes:
  - name: code-cache
    emptyDir:
      sizeLimit: 10Gi
containers:
  - name: kubedoor-ai
    volumeMounts:
      - name: code-cache
        mountPath: /data/code-cache
```

`emptyDir` 是 Pod 临时缓存，重建 Pod 后清空。master 和 AI 使用新版模板中的 `Recreate` 策略，先停止旧 Pod；升级期间会有服务短暂中断及 Agent 重连，请安排维护窗口。

升级保留现有副本数、资源 requests/limits、环境变量和 `envFrom`、配置摘要、调度配置、拉取 Secret、自定义卷和 sidecar。数据库连接、账号密码、加密密钥、Service/NodePort 均沿用现有资源，不重新运行初始化。未安装的 AI / alarm 会跳过；master 和 web 必须存在。Grafana、监控/告警第三方服务及业务集群 Agent 不在此命令的升级范围内，Agent 可在控制台按集群单独升级。

2.3.0 的 Nginx 配置会补充 Agent 安装接口专用代理和安全日志，避免新接口收到的 kubeconfig 被旧通用 API 日志记录；其他 Nginx 配置保留。无法识别的自定义 Nginx 配置会在修改前停止，需先人工核对。

### 旧配置迁入数据库（升级到 2.6.0）

2.6.0 起，master、alarm、ai 的配置存在数据库，在控制台「系统管理 → 通用配置」修改；K8S 事件告警规则在「系统管理 → 事件告警规则」修改（见[控制端配置](#控制端配置通用配置)）。`upgrade` 会自动迁移旧配置，按下面的顺序进行：

1. 备份时额外保存 ConfigMap `kubedoor-config`、Secret `kubedoor-connection`（网页安装才有）和事件告警规则 ConfigMap `kubedoor-master-file-cfg`（存在才保存）。
2. 升级 master 并等待就绪。新模板不再挂载规则 ConfigMap，但补丁按名称合并，旧 Deployment 上的规则挂载这时还在，新 master 启动时会读到旧规则文件。
3. 在 master 容器里执行迁移命令，执行 `upgrade` 的身份需要 `pods/exec` 权限：

   ```sh
   kubectl -n kubedoor exec deploy/kubedoor-master -c kubedoor-master -- python3 -m general_settings migrate
   ```

   它把 `kubedoor-config` 里的旧配置导入通用配置，把旧的 `MSG_TYPE` / `MSG_TOKEN` / `DEFAULT_AT` 导入为消息通知的全局配置，把 `UPDATE_IMAGE` / `REGISTRY_SECRET` 导入镜像管理，把规则文件导入事件告警规则。只导入数据库里还没有的项（控制台改过的以控制台为准），可以重复执行。输出最后一行是只有配置项名称和状态的汇总，不含配置值。
4. 照常升级 AI、alarm、Web。
5. 全部组件就绪且第 3 步成功（退出码 0）后清理：去掉 master 的规则卷和挂载并等待就绪，删除 ConfigMap `kubedoor-master-file-cfg`，`kubedoor-config` 删除已迁移的配置项、只保留数据库连接 `PG_*`（ConfigMap 本身保留，工作负载还从它取数据库连接）。Secret `kubedoor-connection` 不动（vmalert、Grafana 还在用）。删除补丁和 ConfigMap 补丁都先由 API Server 试运行，试运行不通过时跳过清理并提示。

因此升级期间 **master 会重启两次**（换镜像一次、去掉规则挂载一次），Agent 各重连一次。`--dry-run` 会打印迁移命令和清理用的补丁。

迁移没有成功时，升级照常完成，旧配置保留不动，也不清理：

- **退出码 3**：有旧配置格式不对、导入不了，脚本列出是哪几项（例如“通用配置 prom_url”“消息通知”“事件告警规则”）。到控制台对应页面填好后再执行一次 `upgrade`（填好的项下次算已迁移），迁移成功后自动清理。
- **其他失败**（没有 `pods/exec` 权限、master 连不上数据库、镜像不是 2.6.0 等）：处理后再执行一次 `upgrade`，也可以手动执行上面的命令看输出。

不清理时旧值留在 ConfigMap 里也没有副作用：配置以数据库为准，在控制台清空的项不会被旧值导回来。中途失败重跑是安全的：补丁可以重复打，迁移只导入还没有的项，清理时键或资源已经不存在也不报错。

默认保留各组件当前的镜像仓库。使用镜像同步仓库或自定义构建 tag 时，可以显式指定：

```sh
sh install.sh upgrade --image-repo registry.example.com/kubedoor --tag 2.6.0 \
  --backup kubedoor-before-upgrade.yaml
```

`--tag` 只覆盖镜像版本，Deployment 模板始终来自所执行的脚本；请使用同一发布版本的脚本和镜像，不用于降级。`--web-tag` 可以覆盖 Web 镜像；首装参数 `--setup-tag`、`--nodeport`、`--image-pull-secret`、`--render`、`--export` 不适用于升级。

实际修改前，脚本先让 Kubernetes API Server 校验全部 Deployment 补丁，再保存所有目标 Deployment、Nginx ConfigMap 和上面列出的旧配置。默认备份为当前目录的 `kubedoor-upgrade-backup.yaml`，已有文件绝不覆盖，可用 `--backup` 换一个文件名。资源备份包含数据库连接等 Secret 和原有内联配置，应妥善保管；它**不包含数据库备份**。

每个组件更新后均等待 Deployment 就绪。失败时脚本返回非零状态并停止后续组件；已完成的组件不会自动回退。检查镜像、Pod 事件和日志，修复后使用新的 `--backup` 文件重新执行。相同版本重试也会核对 Deployment 并重新载入 Web 配置。恢复旧版前需同时核对资源备份和数据库迁移兼容性，不能仅凭旧镜像保证回退。

开发者修改正式 Deployment 模板后，运行 `python tools/build-upgrade-patches.py` 同步单文件脚本中的升级模板；`python tools/build-upgrade-patches.py --check` 和部署测试会检查是否遗漏同步。生成工具需要 Python/PyYAML，生产升级仍只需要 sh 和 kubectl。

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

旧配置式安装、全量配置重部署和卸载入口保存在 [tools/legacy-install](../tools/legacy-install/README.md)，供已有脚本部署继续维护；该兼容工具仍需要完整仓库、Bash、Python 和 PyYAML。需要全量重部署配置时请沿用原配置，并显式传入 `-c`；已有 Kubernetes 控制端也可使用上面的单文件 `upgrade`。不要用全新网页安装覆盖旧部署。

部署模板、看板及运行默认值已移至 `src/kubedoor-setup/resources/`，Agent 模板位于 `src/kubedoor-master/agent_install/manifests/`，随相应镜像发布。测试和构建工具也归入源码目录，无需用户配置。

旧配置和相邻密钥文件可备份到忽略目录 `.local/kubedoor/legacy-deploy/`，该目录不会提交到仓库，网页入口也不会读取其中内容。旧部署维护时仍需显式指定原配置，并妥善备份密钥。
