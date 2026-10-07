# KubeDoor 部署

用一个 bash 脚本 + 一份配置文件完成部署,不再需要 Helm。数据库(PostgreSQL)独立用 docker compose 跑在 K8S 外面。

```
deploy/
├── kubedoor.conf.example      ← 配置模板,复制为 kubedoor.conf 后修改(唯一需要你改的文件)
├── install.sh                 ← 安装脚本
├── postgres/                  ← PostgreSQL 独立部署(docker compose)
│   ├── docker-compose.yaml
│   ├── .env.example
│   └── initdb/01-init.sql
├── manifests/                 ← K8S 资源模板,占位符形如 __VAR__
│   ├── master/
│   └── agent/
├── dashboards/                ← Grafana 看板(已全部改为 PostgreSQL / Prometheus 数据源)
└── tools/                     ← 辅助脚本,部署时都不用管
    ├── check_dashboard_sql.py ← 看板 SQL 校验,改看板时才用得到
    └── install-ai-tools.sh    ← 构建 agent / ai 镜像时安装 kubectl、istioctl、yq,由 Dockerfile 自动调用
```

`kubedoor.conf` 里有数据库密码、机器人 token 等,已在 `deploy/.gitignore` 里忽略,不要提交到 git。

---

## 架构

KubeDoor 分控制端(master)和集群端(agent)两部分:

- **控制端**装一套,包含 kubedoor-master、web 控制台、告警服务、kubedoor-ai,以及可选的 Grafana、VictoriaMetrics、vmalert、Alertmanager。
- **集群端**在每个要纳管的 K8S 集群各装一套,包含 kubedoor-agent 和采集组件。agent 主动用 WebSocket 连到 master。
- **PostgreSQL** 是唯一的数据库,存管控配置、高峰期资源数据、K8S 事件、告警记录与告警屏蔽规则、Istio 路由,以及 AI 助手的会话、记忆和加密保存的 kubeconfig。全部是原生表,不依赖任何扩展。早期版本用的 ClickHouse 和 MySQL 已经全部不再需要。

控制端和集群端可以装在同一个 K8S 集群里(那就是单集群场景),也可以分开。

---

## 前置条件

| 项目 | 要求 |
|---|---|
| 执行机器 | 能直接跑 `kubectl` 并连上目标集群,bash 4 以上 |
| K8S | 1.21+(用到了 `batch/v1` 的 CronJob),要有一个可用的 StorageClass(仅当启用内置 VictoriaMetrics)。每日采集和 agent 建的定时/周期任务按北京时间执行,这需要 1.25+(CronJob 的 `timeZone` 字段);更老的集群安装脚本会自动去掉该字段并提示,按 kube-controller-manager 的时区执行 |
| metrics-server | 每个纳管集群都要装:Pod、节点管理、Deployment 明细里的实时 CPU/内存用量都靠它,"节点均衡"离不开它 |
| CPU 架构 | KubeDoor 的镜像和 `kubedoor.conf` 里默认的第三方镜像都只有 amd64,ARM 节点需要自己构建 KubeDoor 镜像,并把第三方镜像换成官方的多架构镜像 |
| 数据库 | 一台装了 docker compose 的机器,K8S 节点能访问到它的 5432 端口;或者直接用现成的 PostgreSQL |

脚本在做任何变更前会先检查 kubectl 能不能连上集群,连不上直接报错退出。

Web 上 Deployment 页的列表来自时序库(靠指标上 `PROM_K8S_TAG_KEY`=`K8S_NAME` 这个标签区分集群),集群还没有监控数据时这一页是空的;Pod、Service 等其它资源页由 agent 直接查 K8S,不受影响。

---

## 安装

### 第一步:起数据库

在一台 K8S 节点能访问到的机器上:

```bash
cd deploy/postgres
cp .env.example .env
vi .env                 # 至少改掉 PG_PASSWORD
docker compose up -d
docker compose logs -f postgres    # 看到 database system is ready 就行
```

默认镜像是 `registry.cn-shenzhen.aliyuncs.com/starsl/postgres:18-alpine`(官方 `postgres:18-alpine` 的国内同步,只有 amd64),海外或 ARM 机器在 `.env` 里设 `PG_IMAGE=postgres:18-alpine` 即可。数据放在名为 `kubedoor-pgdata` 的 docker 卷里。

**不用手动建表** —— kubedoor-master 启动时会自动执行 [db.sql](../src/kubedoor-master/db.sql) 把表建好,kubedoor-ai 启动时也会自动建好自己的会话、记忆等表。

**也可以不用 compose**,直接用现成的 PostgreSQL(包括云上托管版)。不需要装任何扩展,也不需要超级用户,建一个库、用库的属主账号连就行。建表脚本用到了 PG 11 起才支持的语法,建议用 14 及以上版本。

### 第二步:装控制端

```bash
cd deploy
cp kubedoor.conf.example kubedoor.conf   # 第一次安装时从模板复制一份
vi kubedoor.conf        # 改 PG_HOST / PG_PASSWORD / MSG_TOKEN 等
./install.sh master
```

`PG_HOST` 要填 **K8S 里的 Pod 能访问到的地址**,通常是数据库宿主机的内网 IP。填 `127.0.0.1` 脚本会直接拒绝,因为那是 Pod 自己的回环地址。

装完会打印 Web 控制台地址和默认账号(`kubedoor` / `kubedoor`),上线前记得改掉,见[安全建议](#安全建议)。

### 第三步:装集群端

到每个业务集群上(或者就在同一个集群):

```bash
vi kubedoor.conf        # 改 K8S_NAME 和 MASTER_WS(换一台机器执行时,先 cp kubedoor.conf.example kubedoor.conf)
./install.sh agent
```

- `K8S_NAME` 是这个集群的唯一标识,会作为指标标签出现在 Web 上,每个集群必须不同。
- `MASTER_WS` 同集群填 `ws://kubedoor-master.kubedoor`;跨集群填 `ws://<账号>:<口令>@<控制端节点IP>:<Web的NodePort>`,账号必须是 `WEB_RW_USERS` 里的读写账号。出厂预置了一个给 agent 用的读写账号 `Up4biLko1dNh`,装控制端时脚本会按它的出厂口令把这一行打印出来;改过口令(见[安全建议](#安全建议))就换成新的。
- 跨集群时还要把 `REMOTE_WRITE_URL` 改成控制端 VictoriaMetrics 的外部地址(装控制端时脚本会把这行打印出来,直接抄)。

几十秒后在 Web 控制台的"Agent管理"里就能看到这个集群上线了。

---

## 其它命令

```bash
./install.sh status                      # 看部署状态
./install.sh render master -o /tmp/out   # 只渲染 YAML 不部署,用来 review
./install.sh uninstall master            # 卸载控制端
./install.sh uninstall agent             # 卸载集群端

./install.sh master -c /path/other.conf  # 用别的配置文件
./install.sh master -y                   # 跳过确认,适合脚本里调用
```

`render` 特别适合在改了 `kubedoor.conf` 之后先看看生成的 YAML 对不对,再真正部署。

卸载前先看[安全建议](#安全建议):要先在 Web 上关掉"准入控制",而且 `uninstall` 会连同 `kubedoor` 命名空间一起删除。

---

## 常见场景

### 已经有 Prometheus / VictoriaMetrics

控制端:
```bash
ENABLE_VICTORIA_METRICS="false"
PROM_URL="http://user:pass@<host>:8428"        # 你的时序库读地址
PROM_TYPE="Prometheus"                          # VictoriaMetrics 填 Victoria-Metrics-Single 或 Victoria-Metrics-Cluster
ENABLE_VMALERT="false"                          # 如果告警规则也用你自己的
```

集群端:
```bash
ENABLE_VMAGENT="false"              # 你的 Prometheus 已经在采集了
ENABLE_KUBE_STATE_METRICS="false"   # 前提是已经装过 kube-state-metrics
ENABLE_NODE_EXPORTER="false"
```

这种情况下,`PROM_URL` 这一个地址要能查到所有集群的指标,而且每条序列都要带 `<PROM_K8S_TAG_KEY>="<K8S_NAME>"` 标签(key 各集群相同,value 是该集群的 `K8S_NAME`)—— KubeDoor 全靠这个标签区分集群。各集群的 Prometheus 远程写到同一个时序库时,可以用 `external_labels` 打这个标签;但 `external_labels` 只在远程写、联邦和发告警时附加,`PROM_URL` 直接指向某个集群自己的 Prometheus 时查不到它,要用 relabel 把标签打到序列上。更多细节见 [FAQ](../help/FAQ.md)。

### AI 助手与外部 MCP

`kubedoor-ai` 是 AI 助手和外部 MCP 共用的后端服务,属于**必装组件,不能关闭**。Deployment、Service 和镜像都使用这个名称,部署模板是 `manifests/master/30-ai.yaml`,镜像版本在 `TAG_AI` 中配置。

```bash
TAG_AI="2.1.0"
ENABLE_MCP="true"      # 外部 MCP 协议接口,默认开启
```

`ENABLE_MCP` 只控制 Web 是否对外开放 `/mcp`、`/sse`、`/messages` 代理;设为 `false` 时网页里的 AI 助手不受影响。

外部 MCP 客户端连 `http://<节点IP>:<Web的NodePort>/mcp`(Streamable HTTP,`/sse` 仍兼容),用 Web 的登录账号做 basic auth,详见 [AI 助手说明](../docs/ai-assistant.md)。

`src/kubedoor-tools` 是共享 Python 库,不是独立服务。构建 `kubedoor-agent` 和 `kubedoor-ai` 镜像时会把它安装进镜像,并通过 `tools/install-ai-tools.sh` 安装 kubectl、istioctl 和 yq;jq、curl、dig、openssl、rg 等诊断工具由镜像的系统包管理器安装。

请将 `kubedoor.conf.ai-secrets` 文件、`kubedoor-ai-security` Secret 与 PostgreSQL 数据一起备份,升级时保留这些密钥与数据。更换加密密钥会使已有 kubeconfig 无法解密。

AI 服务保持单副本,使用 `Recreate` 更新策略,升级时先退出旧 Pod 再启动新 Pod;更新期间 AI/MCP 会短暂不可用。

### 不想要 Grafana / 外部 MCP

```bash
ENABLE_GRAFANA="false"
ENABLE_MCP="false"
```

脚本会自动把 nginx 里对应的反向代理段删掉,不会留下指向空服务的配置导致 nginx 起不来。关掉 Grafana 后,Web 上三个"…看板📊"菜单仍然显示,打开是 404。之前已经装过的组件不会因为开关改成 `false` 而被删除,要手动删(如 `kubectl -n kubedoor delete deploy/kubedoor-dash svc/kubedoor-dash`)。AI 助手是必装组件,没有开关。

### 离线 / 内网环境

把 `kubedoor.conf` 里 `IMAGE_REPO` 和一组 `IMAGE_*` 改成内网仓库地址即可。数据库镜像在 `postgres/.env` 的 `PG_IMAGE` 里改。

例外:agent 建的定时/周期任务用的镜像 `registry.cn-shenzhen.aliyuncs.com/starsl/busybox-curl` 写死在代码里,不受 `IMAGE_BUSYBOX_CURL` 控制,要让各集群节点能拉到这个镜像(比如在容器运行时里把这个仓库地址映射到内网仓库)。

### 调整登录账号

`WEB_AUTH_USERS` 是 htpasswd 格式,一行一个账号,到 <https://tool.lu/htpasswd/> 生成(加密方式选 **Crypt**)。Crypt 只认密码的前 8 位,密码更长时用 `openssl passwd -apr1 '<密码>'` 生成 apr1 格式,写成 `用户名:<输出>`;apr1 的结果里有 `$`,而 `kubedoor.conf` 是按 shell 脚本加载的,这时 `WEB_AUTH_USERS` 的值要改用单引号包起来(或把每个 `$` 写成 `\$`)。
`WEB_RW_USERS` 里列出的账号是读写权限,其余账号只能读,所有变更类接口会被 nginx 挡掉返回 403。例外是改镜像:这个接口对只读账号也放行,由 master 按 `UPDATE_IMAGE_JSON` 里的集群、时段和账号白名单决定是否允许,见[镜像更新配置说明](../help/K8S微服务镜像更新配置说明.md)。

---

## 改配置

装完以后想改参数,不用重跑安装脚本:

```bash
# 改环境变量类配置(数据库、通知、时序库地址...)
kubectl -n kubedoor edit configmap kubedoor-config
kubectl -n kubedoor rollout restart deploy/kubedoor-master deploy/kubedoor-alarm deploy/kubedoor-ai

# 改 K8S 事件告警规则(规则只在 master 启动时加载,改完要重启;镜像里不自带规则文件,必须靠这个 ConfigMap 挂载)
kubectl -n kubedoor edit configmap kubedoor-master-file-cfg
kubectl -n kubedoor rollout restart deploy/kubedoor-master

# 改 vmalert 告警阈值(vmalert 不会自动重载规则,改完要重启)
kubectl -n kubedoor edit configmap vmalert-config
kubectl -n kubedoor rollout restart deploy/vmalert
```

数据库里三张大表的过期数据由 master 每天清理一次,保留天数在 `kubedoor-config` 里调:

| 配置项 | 表 | 默认 |
|---|---|---|
| `RETENTION_EVENTS_DAYS` | K8S 事件 `k8s_events` | 90 天 |
| `RETENTION_ALERTS_DAYS` | 告警记录 `k8s_pod_alert_days` | 365 天 |
| `RETENTION_RESOURCES_DAYS` | 高峰期资源快照 `k8s_resources` | 365 天 |

设为 `0` 表示不清理。

当然,改 `kubedoor.conf` 后重新跑 `./install.sh master` 也可以,是幂等的。但重跑会用模板覆盖上面手工 `kubectl edit` 过的 ConfigMap,想长期保留的改动要同时写进 `kubedoor.conf` 或 `deploy/manifests/` 下对应的模板;而且只改配置、镜像 tag 没变时 Pod 不会自动重启,仍要按上面的命令重启对应组件。

---

## 安全建议

- **上线前改掉默认账号口令。** `WEB_AUTH_USERS` 出厂带两个读写账号:Web 登录用的 `kubedoor`(口令也是 `kubedoor`)和跨集群 agent 用的 `Up4biLko1dNh`,口令都是公开的默认值。按[调整登录账号](#调整登录账号)重新生成这两行(或换成自己的账号并同步改 `WEB_RW_USERS`),重跑 `./install.sh master` 后执行 `kubectl -n kubedoor rollout restart deploy/kubedoor-web`(账号文件是 subPath 挂载,不重启不生效)。跨集群的 agent 要同步改 `MASTER_WS` 里的口令,重跑 `./install.sh agent` 后执行 `kubectl -n kubedoor rollout restart deploy/kubedoor-agent`。
- **生产环境在 Web 前面加 HTTPS。** Web(包括 AI 助手、外部 MCP、Grafana)默认是 HTTP + basic auth,账号口令明文传输。脚本不带 Ingress,请自己用 Ingress 或负载均衡终结 TLS;`/ws` 开头的几个路径是 WebSocket,AI 助手和 MCP 是长连接,代理要支持 Upgrade 并放宽读超时。跨集群 agent 的 `MASTER_WS` 同样带着读写账号,加了 HTTPS 后改用 `wss://`。
- **准入控制是 fail-closed 的。** 某个集群在 Web 上开了"准入控制"后,只要 agent、master 或数据库有一个不可用(包括升级重启期间),这个集群里没打 `kubedoor-ignore` 标签的命名空间(默认只有 `kube-system`、`kubedoor` 打了),Deployment 的创建、更新、扩缩容都会被拒绝。紧急情况下先删掉 webhook 恢复发布,事后再到 Web 上把"准入控制"关掉,让状态同步:
  ```bash
  kubectl delete mutatingwebhookconfiguration kubedoor-admis-configuration
  ```
- **卸载前先在 Web 上关闭各集群的"准入控制"。** `./install.sh uninstall agent` 不会删除上面这个 webhook,残留下来会继续拒绝该集群的 Deployment 变更。另外 `uninstall` 会连同 `kubedoor` 命名空间一起删除:单集群部署时卸载任一端都会把另一端一起删掉,内置 VictoriaMetrics 的 PVC 也会被删(PostgreSQL 在 K8S 外面,不受影响);卸载控制端前先备份 Secret `kubedoor-ai-security` 和 `kubedoor.conf.ai-secrets`。

---

## 排查

**agent 连不上 master**

```bash
kubectl -n kubedoor logs -l app=kubedoor-agent --tail=50
```
重点看 `MASTER_WS` 地址通不通。跨集群时要走 Web 的 NodePort 并带上 basic auth 账号密码,而且必须是读写账号,漏了账号或用了只读账号都会被 nginx 拦在门外。

**master 起不来**

```bash
kubectl -n kubedoor logs -l app=kubedoor-master --tail=50
```
最常见的是连不上数据库。master 启动时会建连接池并建表,连不上会直接退出。确认 `PG_HOST` 是 Pod 能访问的地址,以及数据库确实在监听 `0.0.0.0`。

**web 一直 CrashLoopBackOff**

```bash
kubectl -n kubedoor logs -l app=kubedoor-web --tail=30
```
nginx 启动时要解析上游 service 的域名。如果是刚部署的瞬间 service 还没建好,重启一两次会自己恢复;如果持续失败,看看是不是关了某个组件但 nginx 配置里还留着对它的引用。

**看板没数据**

- KubeDoor-Dash 查的是 PostgreSQL。高峰期数据由每天北京时间 01:00 的 `kubedoor-collect` CronJob 采集,新装的当天没有数据是正常的。想立刻采一次:
  ```bash
  kubectl -n kubedoor create job --from=cronjob/kubedoor-collect collect-now
  ```
  前提是在 Web 的"Agent管理"里给这个集群开了"自动采集"并设置了高峰时段。
- KubeDoor-K8S 和 KubeDoor-Node 查的是 Prometheus,没数据就去查 vmagent 有没有正常远程写。

---

## 从 2.0.x 升级到 2.1

2.1 把外部 MCP 并进了必装的 `kubedoor-ai`(它同时提供网页里的 AI 助手),升级时注意:

- `kubedoor.conf` 里的 `TAG_MCP` 不再使用,要新增 `TAG_AI`(不写会回退到 `latest`)。`ENABLE_MCP` 在 2.0.x 里决定是否部署独立的 `kubedoor-mcp`,现在只控制 Web 是否对外开放 `/mcp`、`/sse`、`/messages`,默认 `true`。
- 改好 `TAG_*` 后重跑 `./install.sh master`,再到各集群重跑 `./install.sh agent`。脚本第一次装 2.1 时会生成 AI 密钥(`kubedoor.conf.ai-secrets` 和 Secret `kubedoor-ai-security`),记得和 PostgreSQL 数据一起备份。
- 脚本只做 `kubectl apply`,不会删除 2.0.x 留下的 `kubedoor-mcp`,装完手动删掉:
  ```bash
  kubectl -n kubedoor delete deploy/kubedoor-mcp svc/kubedoor-mcp
  ```
- 外部 MCP 客户端的地址改为 `http://<节点IP>:<Web的NodePort>/mcp`(Streamable HTTP),原来的 `/sse` 仍然兼容。需要批准的操作会返回待批准动作和 Web 链接,在 Web 上批准后才执行。

---

## 和旧版 Helm 安装的区别

如果你见过老的 `helm-kubedoor`,这些地方变了:

| | 旧版 Helm | 现在 |
|---|---|---|
| 数据库 | ClickHouse(装在 K8S 里)+ MySQL | PostgreSQL 18,独立 docker compose,也可用现成的 PG |
| 注入给 master 的变量 | `CK_*` / `DB_*` | `PG_*` |
| Grafana 数据源 | ClickHouse 插件 + Prometheus | PostgreSQL(内置)+ Prometheus,不再需要装插件 |
| 部署方式 | `helm install` | `./install.sh master` |
| MCP 暴露 | NodePort 直连,无认证 | ClusterIP,统一走 Web 的 basic auth |

旧版 Helm chart 注入的还是 `CK_*` 变量,而现在的代码只读 `PG_*`,**用旧 chart 装出来的 master 连不上数据库**,不要混用。

因为数据库换了,旧数据不能直接沿用,这套部署是按全新安装设计的。
