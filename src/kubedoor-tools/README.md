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
| `diagnostic` | `program` 选择 `jq`、`yq`、`curl`、`dig`、`openssl` 或 `rg`；其余参数与命令工具相同 |
| `pod_exec` | 必填 `namespace`、`pod`、`argv`；可选 `container`、`output_limit` |

`files` 是“文件名 → 文本内容”的字典，文件名必须是临时工作目录内的简单名称，不能包含目录路径。`stdin` 为标准输入文本。`execute()` 和 `prepare()` 均支持以秒为单位的 `timeout`，默认 120 秒。

执行结果包含 `success`、`data`、`source`、`observed_at`、`truncated`，命令还可包含 `exit_code`。失败时返回 `error: {code, message}`；SDK 已提供 HTTP 状态时额外保留 `error.status`，供网关区分明确拒绝与结果未知。

API 结果在 64 MiB 内保留完整资源列表，超出后要求通过 Kubernetes 的 `limit` / `continue` 分页。命令输出达到限制时设置 `truncated`；上层 Agent 传输也可施加更小的响应限制。

## 操作准备、完整参数与预览

`prepare()` 返回复制并固定后的完整 `arguments`、脱敏的 `preview`、`read_only`、`reason`，以及可选的 `preconditions` 或 `error`。准备成功只表示可以进入上层审批流程。

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

不支持 dry-run 的操作会明确标记 `dry_run_supported=False`，不能视为已经通过服务端验证。其他命令提供实际命令预览；修改、未知命令和 Pod exec 由网关审批。CLI 前置检查无法提供任意命令或多个资源之间的原子事务。

## 令牌轮换与工具依赖

集群内凭据通过 SDK 的公开访问方法读取，兼容 `BearerToken` 和 `authorization` 两种存储键。API、Pod exec 和每次生成 kubectl/istioctl 临时 kubeconfig 前都会触发 SDK 令牌刷新，支持 ServiceAccount 投射令牌轮换；最新令牌及其带前缀形式同时加入输出脱敏。

镜像需要提供 `kubectl`、`istioctl`、`jq`、mikefarah 版本的 `yq`、`curl`、`dig`、`openssl` 和 `rg`。CLI 安装版本由 [安装脚本](../../deploy/tools/install-ai-tools.sh) 和各镜像构建参数管理。`yq` 必须支持 `--security-disable-file-ops` 和 `--security-disable-env-ops`，不能用不兼容的同名 Python 包替代。

kubectl 使用 [GitHub 社区镜像](https://github.com/ChampiYann/kubectl-binaries/releases) 的官方二进制压缩包，istioctl 和 yq 从各自的 GitHub Releases 下载，都是直连 GitHub；构建机访问 GitHub 不稳定时，可在 `docker build` 时加 `--build-arg HTTPS_PROXY=http://<代理地址>`。默认 kubectl 版本 `1.37.1` 的 amd64/arm64 使用脚本内固定的官方 SHA256 校验，无需访问 `dl.k8s.io`。自定义 `KUBECTL_VERSION` 时必须同时传入对应架构的 `KUBECTL_SHA256` build arg；未提供时安装会失败，不会尝试下载官方校验文件。

诊断执行会固定 jq 的模块搜索目录，禁用 yq 文件及环境操作，并让 curl 禁用默认配置、仅使用 HTTP(S) 协议。涉及文件的参数必须引用提供的工作区文件；jq/yq/rg 的文件选项应分别书写，避免组合短参数产生歧义。

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
