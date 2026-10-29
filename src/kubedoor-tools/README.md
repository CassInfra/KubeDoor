# kubedoor-tools 共享 Python 工具库

`kubedoor-tools` 是供 KubeDoor AI 网关、MCP 和 Agent 复用的 Python 库。它没有 HTTP 监听端口，无需独立容器部署，而是安装到 `kubedoor-ai` 和 `kubedoor-agent` 镜像中，由上层服务调用。

构建这两个镜像时，各自 Dockerfile 将 `src/kubedoor-tools` 复制到构建环境，再通过 `pip install` 安装为 Python 包。SDK 执行器随镜像运行，`kubectl`、`istioctl` 等 CLI 也安装在对应镜像内。构建上下文必须为仓库根目录：

```bash
docker build -f src/kubedoor-agent/Dockerfile -t kubedoor-agent:local .
docker build -f src/kubedoor-ai/Dockerfile -t kubedoor-ai:local .
```

两个镜像均通过 `PIP_INDEX_URL` build arg 和同名环境变量配置 Python 包源，默认使用清华源 `https://pypi.tuna.tsinghua.edu.cn/simple/`。共享工具库、运行依赖及 PEP 517 隔离构建时安装的 setuptools 等构建依赖都使用该源；`pyproject.toml` 只声明包和依赖，不配置 pip 源。切换源时可在构建命令中追加 `--build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/`。

AI 镜像的 apt 使用阿里云 Debian/debian-security 源，Agent 镜像的 apk 沿用华为云 Alpine 源。运行时 Kubernetes、模型 API 和 VictoriaMetrics 连接地址仍由用户配置，与安装源无关。

## 安装与集群连接

在仓库根目录进行本地开发时，可安装为可编辑包：

```powershell
python -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple/ -e "src/kubedoor-tools[test]"
```

每个 `KubernetesExecutor` 都绑定一个选定集群，持有独立的 Kubernetes SDK 客户端，不修改 SDK 全局配置。传入 kubeconfig 时使用选定的 context；未传入时使用当前 Pod 的集群内 ServiceAccount 凭据。

依赖 `kubernetes>=37`（新版 OpenAPI 生成器：请求由 `ApiClient.param_serialize` + `call_api` 发出，HTTP 错误状态由执行器自行映射），不兼容 36 及以下的旧版 SDK。

`parse_kubeconfig()` 支持 YAML、JSON 和字典。多个 context 且没有默认选择时，返回 `requires_context=True`，必须选定 context 后再创建执行器。界面只应显示 `contexts` 中的名称、命名空间及服务地址；包含凭据的 `config` 留在服务内部。

```python
from kubedoor_tools import KubernetesExecutor, parse_kubeconfig

parsed = parse_kubeconfig(kubeconfig_yaml, context="production")
executor = KubernetesExecutor(parsed["config"], context=parsed["context"])
connection = await executor.test_connection()
pods = await executor.execute("api", {
    "path": "/api/v1/namespaces/team/pods",
    "query": {"limit": 500},
}, call_id="list-pods")
```

## 功能与操作参数

通过 Kubernetes SDK 调用选定服务器的原生 API，支持核心资源、扩展 API 组及 CRD，例如 Istio 的 VirtualService。清单准备使用集群实时 API 发现识别资源类型，无需维护固定资源列表。命令工具使用参数数组执行，不经过本机 Shell。

| 操作类型 | 参数说明 |
| --- | --- |
| `api` | `method` 默认为 GET；`path` 为绝对 Kubernetes API 路径；可选 `query`、`body`、`content_type` |
| `kubectl`、`istioctl` | `argv` 为字符串参数数组；可选 `files`、`stdin`、`output_limit` |
| `diagnostic` | `program` 选择 `jq`、`yq`、`curl`、`dig`、`openssl`、`rg`，网络诊断 `nc`、`nmap`、`tcpdump`、`iperf3`、`traceroute`、`mtr`，进程与系统 `ps`、`top`、`pgrep`、`pmap`、`pwdx`、`lsof`、`free`、`vmstat`、`uptime`、`w`、`slabtop`、`iostat`、`mpstat`、`pidstat`，数据查看 `xxd`、`hexdump`、`base64`；其余参数与命令工具相同 |
| `pod_exec` | 必填 `namespace`、`pod`、`argv`；可选 `container`、`output_limit` |

`files` 是“文件名 → 文本内容”的字典，文件名必须是临时工作目录内的简单名称，不能包含目录路径。`stdin` 为标准输入文本。`execute()` 和 `prepare()` 均支持以秒为单位的 `timeout`，默认 120 秒。

执行结果包含 `success`、`data`、`source`、`observed_at`、`truncated`，命令还可包含 `exit_code`。失败时返回 `error: {code, message}`；SDK 已提供 HTTP 状态时额外保留 `error.status`，供网关区分明确拒绝与结果未知。

API 结果在 64 MiB 内保留完整资源列表，超出后要求通过 Kubernetes 的 `limit` / `continue` 分页。命令输出达到限制时设置 `truncated`；上层 Agent 传输也可施加更小的响应限制。

## 操作准备、完整参数与预览

`prepare()` 返回复制并固定后的完整 `arguments`、脱敏的 `preview`、分类结果（`read_only`、`reason`、`ops`、`category`、`dangerous`，见下文“操作分类”），以及可选的 `preconditions` 或 `error`；可选参数 `commands` 是 master 下发的查看 / 高危命令清单。准备成功只表示可以进入上层的权限检查和审批流程。

```python
prepared = await executor.prepare("api", {
    "method": "PATCH",
    "path": "/apis/apps/v1/namespaces/team/deployments/web",
    "body": {"spec": {"replicas": 3}},
})
# 网关内部保存 prepared["arguments"]，界面只展示 prepared["preview"]。
# 网关检查权限、集群绑定、有效期并完成用户审批后，才执行以下调用。
# result = await executor.execute("api", prepared["arguments"], call_id="scale-web")
```

批准后必须原样执行保存的完整参数，不能让模型重新生成或修改参数。API 写操作在支持时使用服务端 `dryRun=All`，并固定资源版本及 UID；执行前再次检查前置条件。`kubectl apply -f` 支持提供的 YAML/JSON、多文档和 Kubernetes `List` 清单，并实际调用服务端 dry-run。

不支持 dry-run 的操作会明确标记 `dry_run_supported=False`，不能视为已经通过服务端验证。其他命令提供实际命令预览；要不要审批由网关按“权限与审批”配置决定（高危命令固定审批）。CLI 前置检查无法提供任意命令或多个资源之间的原子事务。

## 操作分类

`classify_operation(operation, arguments, commands=None)` 是网关、agent 二次校验和 `prepare()` 共用的保守分类：

- `ops`：涉及“权限与审批”表的哪些操作类型（全部类型见 `OP_KEYS`）。kubectl / istioctl / API 修改都带 `ai.k8s_write`，再按动作加：delete → `pod.delete`（Pod）或 `resource.delete`，scale → `workload.scale`，rollout restart → `workload.restart`，set image → `workload.image`，cordon / uncordon → `node.cordon`，drain → `node.cordon` + `pod.delete`，apply / create / patch / label 等 → `resource.apply`；API 按方法和路径对应（DELETE、`/scale`、驱逐、其余写入），istioctl 的修改加 `network.istio`；`kubectl attach`、API 的 exec / attach 子资源和节点 proxy 判断不了执行什么，加 `pod.exec.danger`（`category` 为 danger）。`kubectl exec` 按下面的 Pod 内命令分类，`kubectl cp` 是 `pod.exec.other`，这两个不带 `ai.k8s_write`。`diagnostic` 里会发请求或写文件的（`curl`、`openssl`、`nc`、`nmap`、`tcpdump`、`iperf3`，以及 `yq -i`）是 `ai.diagnostic`，其余只读。修改 kube-system 的 ConfigMap `coredns` 或 Istio 资源时追加 `network.coredns` / `network.istio`（`identify_targets`，能判断出不涉及的不加，YAML 解析失败、kustomize、拿不到清单等判断不了的两个都加）。只读查询的 `ops` 为空。
- Pod 内命令（`pod_exec`、`kubectl exec` 的 `--` 之后，没有 `--` 分不清命令的按高危）由 `classify_pod_command` 分为查看 / 其他 / 高危，对应 `pod.exec.view|other|danger`，只有查看命令算 `read_only`。查看命令是严格白名单：每个命令都在查看清单里，`sh -c` 里只能用 `|`、`&&`、`||`、`;` 组合，不能有变量、命令替换、子 shell、后台和 heredoc，输出重定向只能到 `/dev/null` 或 fd 复制（`2>&1`），参数不能指向 secrets 目录，curl、wget、nc、find、sort、date、ip、mount、env、hostname 等还有内置参数检查（不满足时只是不算查看，继续判断）。高危命令宽匹配：按 shell 规则递归拆词，任一词命中高危清单、命令位置是变量或通配符、引号没闭合拆不开，都算高危。清单由 master 下发，没下发时用包内的 `default_commands.json`（这个文件缺失或损坏时没有查看命令、所有命令按高危）。

## 令牌轮换与工具依赖

集群内凭据通过 SDK 的公开访问方法读取，兼容 `BearerToken` 和 `authorization` 两种存储键。API、Pod exec 和每次生成 kubectl/istioctl 临时 kubeconfig 前都会触发 SDK 令牌刷新，支持 ServiceAccount 投射令牌轮换；最新令牌及其带前缀形式同时加入输出脱敏。

镜像需要提供 `kubectl`、`istioctl`、`jq`、mikefarah 版本的 `yq`、`curl`、`dig`、`openssl` 和 `rg`，以及上表列出的其余诊断程序（agent 镜像装 Alpine 包、kubedoor-ai 镜像装 Debian 包，见两个 Dockerfile；没装的程序返回 `PROGRAM_UNAVAILABLE`）。CLI 安装版本由 [安装脚本](../../deploy/tools/install-ai-tools.sh) 和各镜像构建参数管理。`yq` 必须支持 `--security-disable-file-ops` 和 `--security-disable-env-ops`，不能用不兼容的同名 Python 包替代。

kubectl 使用 [GitHub 社区镜像](https://github.com/ChampiYann/kubectl-binaries/releases) 的官方二进制压缩包，istioctl 和 yq 从各自的 GitHub Releases 下载，都是直连 GitHub；构建机访问 GitHub 不稳定时，可在 `docker build` 时加 `--build-arg HTTPS_PROXY=http://<代理地址>`。默认 kubectl 版本 `1.37.1` 的 amd64/arm64 使用脚本内固定的官方 SHA256 校验，无需访问 `dl.k8s.io`。自定义 `KUBECTL_VERSION` 时必须同时传入对应架构的 `KUBECTL_SHA256` build arg；未提供时安装会失败，不会尝试下载官方校验文件。

诊断执行会固定 jq 的模块搜索目录，禁用 yq 文件及环境操作，并让 curl 禁用默认配置、仅使用 HTTP(S) 协议。涉及文件的参数必须引用提供的工作区文件；jq/yq/rg 的文件选项应分别书写，避免组合短参数产生歧义。

其余诊断程序按程序开放参数白名单（`executor._DIAGNOSTIC_OPTIONS`），按真实工具的 getopt 规则解析，白名单外的选项、缩写的长选项和以 `-` 开头的操作数一律拒绝：`nc` 只能 `-z` 探测端口且不读 stdin；`nmap` 只做 TCP connect 扫描（自动加 `--unprivileged`），拒绝 NSE 脚本、输出与目标文件和版本探测；`tcpdump` 必须用 `-c` 限定包数，不能读写 pcap 或用 `-z` 执行命令；`iperf3` 只做 60 秒内的客户端测试；`traceroute`、`mtr` 不能做 TCP/UDP 端口探测。`top`、`mtr`、`slabtop` 自动改成非交互模式，`vmstat`、`iostat` 等给了间隔就必须给次数。`ps` 的短选项和取值里不能出现字母 e（BSD 风格的 e 会打印进程环境变量，突破下面“诊断子进程不继承上层服务凭据”的边界），选全部进程用 `-A`，带 e 的取值用 `--format=` 这类写法。`strace`、`watch`、`time`、`stdbuf`、`awk`、`sed`、`tar`/`zip`、`pkill`、`htop`、`file`、`crictl` 不开放，原因见代码注释。

进程类工具（`ps`、`top`、`lsof` 等）只看得到执行端（agent 或 kubedoor-ai）自己 Pod 里的进程，两者都没有 hostPID，排查业务进程用 Pod exec；`vmstat`、`iostat`、`free` 等读的是所在节点的统计。`tcpdump`、`mtr`、`traceroute -I` 需要 CAP_NET_RAW：agent 以 root 运行，但 CRI-O 或受限的 Pod 安全策略会去掉 NET_RAW，需要给 agent 容器加 `securityContext.capabilities.add: [NET_RAW]`；kubedoor-ai 以非 root 运行，不能抓包。权限不足导致失败时，结果的错误信息会带上这些提示。

## 安全边界与取消限制

执行器校验集群及认证覆盖参数、外部文件、远程清单、交互会话、凭据插件和外部命令钩子。诊断子进程仅继承必要环境变量，不继承上层服务的模型密钥或数据库凭据。本库按参数数组执行程序，不能作为通用操作系统沙箱。

上层网关负责身份认证、集群与资源范围策略、持久审批、审计记录及调用次数限制。完整准备参数可能含有业务敏感信息，应保存在网关内部；向模型和界面返回脱敏预览。输出脱敏覆盖常见凭据字段、Secret/SecretList 数据和已知 kubeconfig 凭据值，但无法推断自定义输出或任意 Pod 命令中的所有未知密钥，应避免主动请求这些内容。

`cancel(call_id)` 会关闭活动响应或 Pod 流，并终止本机进程组。SDK 网络请求通过关闭响应和 HTTP 超时协作停止。取消不能撤销 Kubernetes 已经接收的写操作；超时、断线及部分执行失败后，应查询实际资源状态，不能盲目重放。

不再使用执行器时调用 `await executor.close()`，释放客户端、临时凭据及活动任务。

## 连接测试与验证

`test_connection()` 查询服务版本、实际身份和 API 发现，并分别检查命名空间、Pod、日志读取、Deployment 修改及 Pod exec 权限。匿名身份会被拒绝；旧集群不支持身份检查时明确返回 `identity_unverified`，并尝试验证受保护资源的访问。拥有部分权限的集群仍可用于相应操作。

测试覆盖真实 Kubernetes SDK 对本地 HTTP API 的调用、动态 CRD 发现、服务端 dry-run、前置条件、令牌轮换、取消与文件边界。在仓库根目录执行：

```powershell
python -m pytest -q src/kubedoor-tools/tests
```
