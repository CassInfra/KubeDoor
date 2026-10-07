# K8S微服务镜像更新配置说明

> 适用于 KubeDoor 2.x（`deploy/install.sh` + `deploy/kubedoor.conf` 部署）。安装与账号配置见[部署文档](../deploy/README.md)。

## 功能介绍

- 在 Web 的 Deployment 页面直接更换微服务的**镜像标签**，由目标集群的 kubedoor-agent 修改 Deployment 并触发滚动更新。
- 根据微服务当前的镜像地址，**自动**从镜像仓库获取**镜像标签**（最多 20 个，带时间）。支持**华为云 SWR、阿里云 ACR、自建 Harbor**，获取不到时可以手动输入。
- 可以按**集群 + 时间段 + 账号**给**只读账号**开放更新权限；读写账号不受限制。
- 更新过程中，agent 把开始、进度、Pod 重启 / Pending、成功或超时推送到 IM。

与 1.x 的区别：最终生效的仍是 ConfigMap `kubedoor-config` 中的 `UPDATE_IMAGE`、`REGISTRY_SECRET` 两个键，JSON 格式不变，可以直接沿用；2.x 改为在 `kubedoor.conf` 里填写、由 `install.sh` 生成。2.1 起也可以在内置 AI 助手里查询标签、更新镜像。

## 在哪里操作

| 入口 | 操作 | 说明 |
|---|---|---|
| 「资源管理」→「Deployment」 | 选择「K8S」「命名空间」，在目标行点「操作」→「更新」 | 打开「更新镜像」窗口，见下文 |
| 「Agent管理」 | 「更新」列的「更新」按钮 | 升级该集群的 **kubedoor-agent 自身**：只有一个「镜像标签」输入框，不获取标签列表，固定更新 `kubedoor` 命名空间的 `kubedoor-agent` |
| 顶栏「AI 助手」/ 外部 MCP 客户端 | 让助手查询可用标签，或更新指定 Deployment 的镜像 | 查询标签直接执行；更新镜像只允许读写账号，并且要在 Web 上批准后才执行（AI 助手里点「批准这次操作」或勾选「自动批准执行」，外部 MCP 会返回 Web 审批链接），见 [AI 助手文档](../docs/ai-assistant.md) |

「更新镜像」窗口：

- 「当前镜像：」显示当前镜像的地址（不含标签）、标签和时间（该标签在仓库中的推送 / 更新时间）；只有当前标签出现在获取到的列表里时才显示时间。
- 「更新镜像：」下拉框列出仓库中的标签，每项显示为 `时间：标签`（时间格式 `YY-MM-DD HH:MM`，北京时间）；可以搜索，也可以输入列表里没有的标签（占位提示「请选择或输入镜像标签」）。
- 只填**标签**（如 `v1.2.3`），不要填完整镜像地址，仓库和镜像名保持不变。
- 点「确定」提交。成功时提示 `<命名空间> <Deployment> updated with image <新镜像>`，窗口关闭并刷新列表；没有选择或输入标签时提示「请输入镜像标签」。

在「Agent管理」页升级 agent 后，请把该集群 `kubedoor.conf` 中的 `TAG_AGENT` 改成同一版本，否则以后重跑 `./install.sh agent` 会把 agent 改回原版本。

## 谁受权限限制

Web 账号在 `kubedoor.conf` 的 `WEB_AUTH_USERS` 中配置，列在 `WEB_RW_USERS` 里的是**读写账号**，其余都是**只读账号**。

| 途径 | 读写账号 | 只读账号 |
|---|---|---|
| Deployment 页「更新」 | 始终允许，**不检查** `UPDATE_IMAGE` | 由 master 按 `UPDATE_IMAGE` 检查集群、时间段和账号 |
| 打开「更新镜像」窗口、查看标签 | 允许 | 允许 |
| AI 助手 / 外部 MCP 更新镜像 | 批准后执行，**不检查** `UPDATE_IMAGE` | 不允许（报错「当前用户只有查询权限」） |

- 只读账号的其它变更操作（扩缩容、重启、编辑、删除等）都会被 Web 的 nginx 直接拒绝（403），**更新镜像是唯一交给 master 按配置判断的变更操作**。
- 授权粒度是**集群**：被授权的只读账号可以更新该集群任意命名空间、任意 Deployment 的镜像。
- 安装默认的两个账号（管理员 `kubedoor` 和预留给跨集群 agent 的账号）都是读写账号。要使用这里的权限控制，先在 `WEB_AUTH_USERS` 中添加只读账号（不要写进 `WEB_RW_USERS`）。账号或 `WEB_RW_USERS` 改动后，重跑 `./install.sh master` 并执行 `kubectl -n kubedoor rollout restart deploy/kubedoor-web`（账号文件和 nginx 配置以 subPath 方式挂载，不会自动更新）。

## 配置在哪里改

| 用途 | `kubedoor.conf`（第六节） | 生效位置：ConfigMap `kubedoor-config` 的键 | 未设置时 |
|---|---|---|---|
| 只读账号的更新权限 | `UPDATE_IMAGE_JSON` | `UPDATE_IMAGE` | `{}`，只读账号一律被拒绝（「拒绝操作：找不到default配置」） |
| 镜像仓库凭据 | `REGISTRY_SECRET_JSON` | `REGISTRY_SECRET` | `{}`，不获取标签，只能手动输入 |

- `kubedoor.conf` 是 shell 语法，JSON 用单引号包起来，所以 JSON 中不能出现单引号 `'`。`kubedoor.conf` 自带的内容只是示例，请按实际情况修改。
- `install.sh` 不检查 JSON 格式，写错要到使用时才报错（提示见后文）。
- 这两项只有 kubedoor-master 使用，通过环境变量注入，ConfigMap 改了之后**必须重启 master 才生效**；重跑 `install.sh` 只会更新 ConfigMap，不会重启 master。

安装后修改：

```bash
# 方式一（推荐）：改 kubedoor.conf，重跑安装脚本，再重启 master
cd deploy
vi kubedoor.conf        # 修改 UPDATE_IMAGE_JSON / REGISTRY_SECRET_JSON
./install.sh master
kubectl -n kubedoor rollout restart deploy/kubedoor-master

# 方式二（快速修改）：直接改 ConfigMap，再重启 master
kubectl -n kubedoor edit configmap kubedoor-config
kubectl -n kubedoor rollout restart deploy/kubedoor-master
```

- 方式二的改动会在下次执行 `./install.sh master` 时被 `kubedoor.conf` 的内容覆盖，记得同步回 `kubedoor.conf`。
- ConfigMap 中这两个键是多行文本（YAML `|` 块），编辑时保持缩进：

  ```yaml
  data:
    UPDATE_IMAGE: |
      {
        "default": {"isOperationAllowed": false}
      }
  ```

- 检查 ConfigMap 中的 JSON 是否合法（两行都输出 `OK` 即可）：

  ```bash
  kubectl -n kubedoor get configmap kubedoor-config -o jsonpath='{.data.UPDATE_IMAGE}' | python3 -m json.tool >/dev/null && echo OK
  kubectl -n kubedoor get configmap kubedoor-config -o jsonpath='{.data.REGISTRY_SECRET}' | python3 -m json.tool >/dev/null && echo OK
  ```

## 权限配置（UPDATE_IMAGE）

只对**只读账号**生效。示例（`kubedoor.conf` 写法）：

```bash
UPDATE_IMAGE_JSON='{
  "default": {
    "isOperationAllowed": false
  },
  "prod-a": {
    "isOperationAllowed": true,
    "allowedOperationPeriod": "20:00-08:00",
    "user": ["zhangsan", "lisi"]
  },
  "test-k8s": {
    "isOperationAllowed": true,
    "allowedOperationPeriod": "00:00-24:00",
    "user": ["zhangsan", "lisi", "wangwu"]
  }
}'
```

| 键 / 字段 | 说明 |
|---|---|
| 集群名（如 `prod-a`） | 必须与该集群 agent 的 `K8S_NAME` **完全一致**（区分大小写），即页面「K8S」下拉框中显示的名字 |
| `default` | 兜底段：集群没有自己的段时使用。集群有自己的段时**只看自己的段**，不会再参考 `default` |
| `isOperationAllowed` | 必填，JSON 布尔值 `true` / `false`，不要加引号（写成 `"false"` 会被当作允许）。为 `false` 时禁止只读账号更新，其余字段可省略 |
| `allowedOperationPeriod` | `isOperationAllowed` 为 `true` 时必填，允许操作的时间段，规则见下 |
| `user` | `isOperationAllowed` 为 `true` 时必填，允许操作的只读账号**数组**。账号名一律写**小写**（master 会先把登录名转成小写再比较）；不支持通配 |

时间段规则：

- 格式 `HH:MM-HH:MM`（24 小时制），每段只能写一个时间段。精确到分钟，**包含开始、不包含结束**：`09:00-18:00` 即 09:00～17:59。
- 开始晚于结束表示**跨零点**：`20:00-08:00` 即每天 20:00 到次日 07:59。
- 全天允许写 `00:00-24:00`；开始与结束相同（如 `08:00-08:00`）表示任何时间都不允许。
- 按**北京时间**判断（master 容器设置了 `TZ=Asia/Shanghai`），与浏览器所在时区无关。

上面示例的效果（读写账号在任何集群、任何时间都允许）：

| 只读账号 | 集群 | 北京时间 | 结果 |
|---|---|---|---|
| zhangsan | prod-a | 21:30 | 允许 |
| zhangsan | prod-a | 10:00 | 拒绝：`拒绝操作：当前prod-a环境只允许在20:00-08:00时段操作` |
| wangwu | prod-a | 21:30 | 拒绝：`拒绝操作：当前用户wangwu禁止操作` |
| wangwu | test-k8s | 任意 | 允许 |
| zhangsan | prod-b（没有单独配置，使用 `default`） | 任意 | 拒绝：`拒绝操作：当前prod-b环境禁止操作` |

master 按以下顺序检查，不满足时返回 403，页面以红色消息显示提示原文：

| 顺序 | 检查项 | 不满足时的提示 |
|---|---|---|
| 1 | `UPDATE_IMAGE` 不为空 | `拒绝操作：没有UPDATE_IMAGE权限配置` |
| 2 | 是合法 JSON | `拒绝操作：UPDATE_IMAGE配置格式错误` |
| 3 | 有该集群的段，或有 `default` 段 | `拒绝操作：找不到default配置` |
| 4 | 有 `isOperationAllowed` | `拒绝操作：找不到isOperationAllowed配置` |
| 5 | `isOperationAllowed` 为真 | `拒绝操作：当前<集群名>环境禁止操作` |
| 6 | 有 `allowedOperationPeriod` | `拒绝操作：找不到allowedOperationPeriod配置` |
| 7 | 时间段格式正确 | `拒绝操作：allowedOperationPeriod格式错误` |
| 8 | 当前时间在时间段内 | `拒绝操作：当前<集群名>环境只允许在<时间段>时段操作` |
| 9 | 有 `user` | `拒绝操作：找不到user配置` |
| 10 | 登录名（转小写后）在 `user` 中 | `拒绝操作：当前用户<登录名>禁止操作` |

> `kubedoor.conf` 自带示例中的 `"user": ["kubedoor"]` 没有实际作用：`kubedoor` 是默认的读写账号，本来就不受限制。请换成实际的只读账号。

## 镜像仓库配置（REGISTRY_SECRET）

配置镜像仓库凭据是为了**自动获取镜像标签**；不配置也能更新，只是需要手动输入标签。

结构为 `{"<仓库域名>": {"<集群名>" 或 "default": {"ak": "…", "sk": "…"}}}`，示例（`kubedoor.conf` 写法）：

```bash
REGISTRY_SECRET_JSON='{
  "swr.cn-south-1.myhuaweicloud.com": {
    "default": {"ak": "<华为云AK>", "sk": "<华为云SK>"}
  },
  "registry.cn-shenzhen.aliyuncs.com": {
    "default": {"ak": "<AccessKey ID>", "sk": "<AccessKey Secret>"},
    "prod-b": {"ak": "<prod-b专用AccessKey ID>", "sk": "<prod-b专用AccessKey Secret>"}
  },
  "harbor.example.com": {
    "default": {"ak": "<Harbor用户名>", "sk": "<Harbor密码>"}
  }
}'
```

打开「更新镜像」窗口时，master 按以下步骤获取标签：

1. **查当前镜像**：从时序库查该 Deployment 运行中 Pod 的镜像（kube-state-metrics 的 `kube_pod_container_info` 指标，按 `<PROM_K8S_TAG_KEY>="<集群名>"` 区分集群，与 Deployment 页列表的数据来源相同），不是读 K8S API。
2. **找仓库**：取镜像地址第一个 `/` 之前的部分（含端口）作为仓库域名，与 `REGISTRY_SECRET` 的键**精确匹配**。
3. **找凭据**：在该域名下先找与集群名同名的段，没有再用 `default`；`ak`、`sk` 缺一不可。
4. **取标签**：按镜像地址判断仓库类型并调用对应接口，最多返回 20 个：

| 镜像地址包含 | 按此仓库处理 | `ak` / `sk` 填写 | 取哪些标签、如何排序 |
|---|---|---|---|
| `myhuaweicloud.com` | 华为云 SWR | AK / SK | 华为云 SDK，按更新时间倒序取最新 20 个 |
| `aliyuncs.com` | 阿里云 ACR | AccessKey ID / AccessKey Secret | 阿里云 SDK（ACR 个人版 `GetRepoTags` 接口），取第一页 20 个，按接口返回顺序显示 |
| 其它 | Harbor（2.0 及以上） | 用户名 / 密码 | `https://<域名>/api/v2.0/` 接口，取第一页 20 个制品（artifact），每个只取第一个标签，按推送时间倒序显示 |

按上面示例配置的匹配结果：

| 集群 | 当前镜像 | 使用的凭据 |
|---|---|---|
| prod-a | `registry.cn-shenzhen.aliyuncs.com/demo/deploy-demo-api:v1.0.0` | 阿里云 `default` |
| prod-b | `registry.cn-shenzhen.aliyuncs.com/demo/deploy-demo-api:v1.0.0` | 阿里云 `prod-b` |
| prod-a | `harbor.example.com/demo/deploy-demo-web:v2.3.1` | Harbor `default` |
| prod-a | `docker.io/library/nginx:1.27` | 没有配置该域名，不获取标签，需手动输入 |

说明：

- 华为云、阿里云的区域取自域名第二段，域名需保持 `swr.<区域>.myhuaweicloud.com`、`registry.<区域>.aliyuncs.com` 的格式。镜像使用 VPC 域名（如 `registry-vpc.cn-shenzhen.aliyuncs.com`）时，键也要写这个域名。
- master 需要能访问云厂商的镜像仓库接口或 Harbor 地址。
- **获取失败不会报错**：域名或凭据没配置、凭据错误、网络不通、仓库不支持时，下拉框只是为空，手动输入标签即可。域名或凭据没配置时，master 日志中会有 `未找到域名 '<域名>' 的认证配置`、`未找到K8S集群 '<集群名>' 或 'default' 的认证配置` 等错误。
- 查不到当前镜像时提示 `400: 未找到deployment <命名空间>/<Deployment> 的镜像信息`（例如副本数为 0、刚创建还没被采集、该集群的 kube-state-metrics 指标没有进入时序库），仍可手动输入标签更新。
- Pod 中有多个容器（如注入了 sidecar）或正在滚动更新时会查到多个镜像，「当前镜像：」和标签列表可能为空或不准确（也可能直接出现上一条的 400 提示），以手动输入为准。
- `REGISTRY_SECRET` 不是合法 JSON 时提示 `400: REGISTRY_SECRET环境变量格式错误: …`。
- 凭据以明文保存在 ConfigMap `kubedoor-config` 中，建议使用只有镜像读取权限的子账号或专用账号。

## 更新过程与 IM 通知

点「确定」后：

1. master 对只读账号做上述权限检查，然后把请求转给目标集群的 kubedoor-agent（该集群 agent 必须在线，可在「Agent管理」页查看）。
2. agent 读取 Deployment，取**第一个容器**的镜像，保留第一个 `:` 之前的部分并拼上新标签，写回 Deployment，触发滚动更新。
3. agent 在后台监控最长 **20 分钟**，通过**该集群 agent 的** IM 配置推送通知（ConfigMap `kubedoor-agent-config` 的 `MSG_TYPE` / `MSG_TOKEN`，即安装该集群 agent 时 `kubedoor.conf` 中的同名配置）：

| 时机 | 消息内容（均以 `【集群】【命名空间】【Deployment】` 开头） |
|---|---|
| 开始 | `开始更新镜像【<新镜像>】` |
| 每 10 秒 | `更新状态: 总共N个Pod, 已更新x个, 就绪y个, 待更新z个`，有不可用 Pod 时追加 `, 不可用k个` |
| Pod 重启次数增加 | `Pod【<Pod名>】发生重启，重启次数: n (+d)` |
| Pod Pending 超过 2 分钟 | `Pod【<Pod名>】Pending超过2分钟！原因: …`，仍未恢复则每 2 分钟提醒一次 |
| 完成 | `镜像更新成功！所有N个Pod已成功更新到新镜像` |
| 20 分钟仍未完成 | `镜像更新超时（20分钟），停止监控` |

- 判定成功的条件：所有 Pod 已更新且就绪、没有不可用的 Pod，并且**每个 Pod 的所有容器**都已是新镜像。
- 超时只是停止监控，**不会回滚**；同一个 Deployment 再次更新时，上一次的监控会被取消。
- 通知发到哪个群由集群决定（各集群 agent 可以用不同的 `MSG_TOKEN` 安装），消息中不含操作人。需要追溯时查看 master 日志，每次更新请求都会记录登录名和权限（关键字 `username=`）。
- 该集群开启了「准入控制」且目标命名空间在「管控命名空间」中时，镜像更新同样要经过准入控制：副本数、资源等会按管控表改写，未登记的服务可能被拒绝，见 [K8S资源管控功能说明](K8S资源管控功能说明.md)。
- 在「Agent管理」页升级 agent 时，监控任务运行在即将被替换的旧 agent 中，进度通知可能中途停止，升级结果以该页的「版本」列为准。

## 注意事项

- 只能更换**标签**，不能更换仓库或镜像名；只修改 Pod 的**第一个容器**，其它容器（如 sidecar）的镜像不变。
- Pod 中有多个容器且镜像不同（例如注入了 Istio sidecar）时，滚动更新完成后也收不到「镜像更新成功」，会每 10 秒推送一条进度，直到 20 分钟超时。
- 仓库地址**带端口**（如 `harbor.example.com:8443/demo/app:1.0`）或用 **digest**（`@sha256:…`）引用的镜像，更新后会变成错误的镜像地址，**不要**对这类 Deployment 使用本功能。
- Harbor 固定用 HTTPS 访问并校验证书：只开 HTTP 或使用自签名证书的 Harbor 取不到标签（仍可手动输入）。
- Docker Hub、GHCR 等其它仓库会被当作 Harbor 处理，取不到标签，只能手动输入。
- 以下情况未经验证，可能取不到标签：阿里云 ACR 企业版实例（`*.cr.aliyuncs.com` 域名）、Harbor 多级仓库路径（如 `project/team/app`）。

## 常见问题

| 现象 | 原因与处理 |
|---|---|
| 「更新镜像：」下拉框为空 | `REGISTRY_SECRET` 中没有该仓库域名或集群的凭据、凭据错误、网络不通或仓库不支持；按上文检查，或直接手动输入标签 |
| 提示 `400: 未找到deployment … 的镜像信息` | 时序库中没有该 Deployment 运行中 Pod 的数据，或 Pod 有多个容器 / 正在滚动更新（见上文）；手动输入标签即可更新 |
| 只读账号提示「拒绝操作：…」 | 对照上文的检查顺序表修改 `UPDATE_IMAGE` 并重启 master，或改用读写账号 |
| 改了 `UPDATE_IMAGE` / `REGISTRY_SECRET` 不生效 | 没有重启 master：`kubectl -n kubedoor rollout restart deploy/kubedoor-master` |
| 被拒绝或报错后「确定」按钮一直转圈 | 点「取消」关闭窗口，刷新页面后再操作 |
| 一直收到「更新状态」，收不到「镜像更新成功」 | Pod 有多个容器（见注意事项），或有 Pod 一直未就绪；最多持续 20 分钟 |
| 收不到 IM 通知 | 检查该集群 ConfigMap `kubedoor-agent-config` 的 `MSG_TYPE` / `MSG_TOKEN`，修改后在该集群执行 `kubectl -n kubedoor rollout restart deploy/kubedoor-agent` |
