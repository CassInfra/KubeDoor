from __future__ import annotations

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers

from .domain import authenticate, normalize_scope


def create_mcp(service, token):
    server = FastMCP("KubeDoor-AI", mask_error_details=True)

    def identity():
        return authenticate(get_http_headers(), token)

    async def invoke(operation, k8s, namespace=None, deployment=None, pod=None, **arguments):
        scope = normalize_scope({"env": k8s, "namespace": namespace,
                                 "deployment": {"namespace": namespace, "name": deployment} if deployment else None,
                                 "pod": {"namespace": namespace, "name": pod} if pod else None})
        args = {**arguments}
        if namespace is not None:
            args["namespace"] = namespace
        if deployment is not None:
            args["deployment"] = deployment
        if pod is not None:
            args["pod"] = pod
        return await service().mcp_call(identity(), operation, args, scope)

    @server.tool(description="获取在线/离线集群和直接连接配置状态")
    async def get_k8s_list() -> dict:
        data = await service().bootstrap(identity())
        return {"success": True, "data": {r["env"]: r for r in data["clusters"]}}

    @server.tool(description="查询指定集群的命名空间列表")
    async def get_namespaces_list(k8s: str) -> dict:
        return await invoke("namespaces", k8s)

    @server.tool(description="查询 Deployment 资源指标，标明指标来源与观察时间")
    async def get_deployments_info(namespace: str, k8s: str) -> dict:
        return await invoke("deployment_metrics", k8s, namespace)

    @server.tool(description="查询集群节点和资源明细")
    async def get_k8s_nodes(k8s: str) -> dict:
        return await invoke("nodes", k8s)

    @server.tool(description="查询 Kubernetes 事件，namespace 为空代表全部")
    async def get_k8s_events(k8s: str, namespace: str = "") -> dict:
        return await invoke("events", k8s, namespace or None)

    @server.tool(description="查询 Deployment 的 Pod 列表和明细")
    async def get_pods(namespace: str, deployment: str, k8s: str) -> dict:
        return await invoke("pods", k8s, namespace, deployment)

    @server.tool(description="获取 Pod 最新日志，默认 100 行")
    async def get_pods_logs(namespace: str, pod: str, k8s: str, lines: int = 100) -> dict:
        return await invoke("logs", k8s, namespace, pod=pod, lines=lines)

    @server.tool(description="重启 Deployment；准备参数后返回浏览器批准链接")
    async def restart_deployment(namespace: str, deployment: str, k8s: str) -> dict:
        return await invoke("restart_deployment", k8s, namespace, deployment)

    @server.tool(description="扩缩容 Deployment；需要浏览器批准")
    async def scale_deployment(namespace: str, deployment: str, replicas: int, k8s: str) -> dict:
        return await invoke("scale_deployment", k8s, namespace, deployment, replicas=replicas)

    @server.tool(description="更新 Deployment 镜像标签，沿用 KubeDoor 权限策略，需要批准")
    async def update_deployment(namespace: str, deployment: str, image_tag: str, k8s: str) -> dict:
        return await invoke("update_deployment_image", k8s, namespace, deployment, image_tag=image_tag)

    @server.tool(description="删除 Pod；需要浏览器批准")
    async def delete_pod(namespace: str, pod: str, k8s: str) -> dict:
        return await invoke("delete_pod", k8s, namespace, pod=pod)

    @server.tool(description="隔离 Pod；需要浏览器批准")
    async def modify_pod(namespace: str, pod: str, k8s: str) -> dict:
        return await invoke("isolate_pod", k8s, namespace, pod=pod)

    @server.tool(description="进入 Pod 查询 JVM 内存，需要批准 exec")
    async def get_pod_jvm_mem(namespace: str, pod: str, k8s: str) -> dict:
        return await invoke("jvm_mem", k8s, namespace, pod=pod)

    @server.tool(description="生成 JVM heap dump，需要批准")
    async def get_pod_jvm_dump(namespace: str, pod: str, k8s: str) -> dict:
        return await invoke("jvm_dump", k8s, namespace, pod=pod)

    @server.tool(description="生成 JVM jstack，需要批准")
    async def get_pod_jvm_jstack(namespace: str, pod: str, k8s: str) -> dict:
        return await invoke("jvm_jstack", k8s, namespace, pod=pod)

    @server.tool(description="生成 JVM 飞行记录，需要批准")
    async def get_pod_jvm_jfr(namespace: str, pod: str, k8s: str) -> dict:
        return await invoke("jvm_jfr", k8s, namespace, pod=pod)

    @server.tool(description="统一工具网关，按调用选择 kubedoor/direct/agent；所有修改冻结参数后批准")
    async def kubedoor_tool(operation: str, arguments: dict, k8s: str, source: str = "auto", namespace: str | None = None) -> dict:
        return await service().mcp_call(identity(), operation, arguments, normalize_scope({"env": k8s, "namespace": namespace}), source)

    @server.resource("skills://kubedoor-k8s", description="KubeDoor Kubernetes 运维 Skill")
    async def kubedoor_skill() -> str:
        from .runtime import SKILLS_ROOT
        identity()
        return (SKILLS_ROOT / "kubedoor-k8s" / "SKILL.md").read_text(encoding="utf-8")

    return server
