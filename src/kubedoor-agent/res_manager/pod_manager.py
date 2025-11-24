from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List

from aiohttp import web
from kubernetes_asyncio import client
from kubernetes_asyncio.client.exceptions import ApiException
from loguru import logger

from utils import parse_cpu, parse_memory


def _format_time_beijing(dt: datetime | None) -> str:
    """将datetime对象转换为北京时间（UTC+8）的字符串，格式为 YYYY-MM-DD HH:MM:SS。"""
    if not dt:
        return ""
    beijing_tz = timezone(timedelta(hours=8))
    return dt.astimezone(beijing_tz).strftime("%Y-%m-%d %H:%M:%S")


async def _list_pod_metrics(namespace: str | None, custom_api) -> Dict[str, Dict[str, float]]:
    """
    获取命名空间下所有Pod的实时资源使用情况（聚合到Pod级别）。

    返回字典: { pod_name: {"cpu_m": mCPU, "memory_mb": MB} }
    """
    metrics_map: Dict[str, Dict[str, float]] = {}
    try:
        # 一次性获取Pod的指标以减少API调用次数
        if namespace:
            pod_metrics = await custom_api.list_namespaced_custom_object(
                group="metrics.k8s.io", version="v1beta1", namespace=namespace, plural="pods"
            )
        else:
            pod_metrics = await custom_api.list_cluster_custom_object(
                group="metrics.k8s.io", version="v1beta1", plural="pods"
            )
        items = pod_metrics.get("items", [])
        for item in items:
            meta = item.get("metadata", {})
            name = meta.get("name", "")
            ns = meta.get("namespace", "")
            total_mcpu = 0.0
            total_mem_mb = 0.0

            for c in item.get("containers", []):
                usage = c.get("usage", {})
                total_mcpu += parse_cpu(str(usage.get("cpu", "0")))
                total_mem_mb += parse_memory(str(usage.get("memory", "0")))
            key = f"{ns}/{name}" if ns else name
            metrics_map[key] = {"cpu_m": total_mcpu, "memory_mb": total_mem_mb}
    except Exception as e:
        # metrics-server可能未安装或无权限，记录警告但不影响主流程
        logger.warning(f"无法获取命名空间 {namespace} 的Pod指标: {e}")

    return metrics_map


def _extract_container_statuses(pod) -> List[Dict[str, Any]]:
    """提取各容器状态信息，包括常规容器和Init容器。"""
    statuses = []

    all_container_statuses = []
    if pod.status:
        # 按顺序添加：Init 容器状态 -> 常规容器状态
        if pod.status.init_container_statuses:
            all_container_statuses.extend(pod.status.init_container_statuses)
        if pod.status.container_statuses:
            all_container_statuses.extend(pod.status.container_statuses)

    if not all_container_statuses:
        return []

    # 从 spec 中获取 Init 容器的名称，用于区分
    init_container_names = (
        {c.name for c in pod.spec.init_containers} if pod.spec and pod.spec.init_containers else set()
    )

    for cs in all_container_statuses:
        state = "Unknown"
        state_info = {}

        if cs.state:
            if cs.state.running:
                state = "Running"
                state_info = {"start_time": _format_time_beijing(cs.state.running.started_at)}
            elif cs.state.waiting:
                state = "Waiting"
                state_info = {"reason": cs.state.waiting.reason or "", "message": cs.state.waiting.message or ""}
            elif cs.state.terminated:
                state = "Terminated"
                terminated = cs.state.terminated
                state_info = {
                    "terminated": {
                        "exit_code": terminated.exit_code,
                        "reason": terminated.reason or "",
                        "started_at": _format_time_beijing(terminated.started_at),
                        "finished_at": _format_time_beijing(terminated.finished_at),
                    }
                }

        last_state_info = {}
        if cs.restart_count > 0 and cs.last_state and cs.last_state.terminated:
            terminated = cs.last_state.terminated
            last_state_info = {
                "terminated": {
                    "exit_code": terminated.exit_code,
                    "reason": terminated.reason or "",
                    "started_at": _format_time_beijing(terminated.started_at),
                    "finished_at": _format_time_beijing(terminated.finished_at),
                }
            }

        statuses.append(
            {
                "is_init": cs.name in init_container_names,
                "name": cs.name,
                "ready": bool(getattr(cs, "ready", False)),
                "state": state,
                "state_info": state_info,
                "restart_count": int(getattr(cs, "restart_count", 0)),
                "last_state": last_state_info,
            }
        )
    return statuses


def _get_controlled_by(pod) -> str:
    """获取Pod的上级控制器（Controlled By）。"""
    owners = (pod.metadata.owner_references or []) if pod and pod.metadata else []
    if owners:
        owner = owners[0]
        return getattr(owner, "kind", "") or ""
    return ""


async def get_pod_list(core_v1, custom_api, request):
    """
    GET接口：查询指定命名空间的Pod列表
    参数：
    - env: 集群名称（可选）
    - namespace: 命名空间（必填）
    返回字段：name, namespace, containers(各容器状态), restart_count(总重启次数),
            controlled_by, pod_ip, pod_ips, host_ip, creation_timestamp, node_name, status, current_cpu_cores, current_memory_mb
    """
    try:
        namespace = request.query.get("namespace")

        # 获取Pod列表；若namespace为空，则查询所有命名空间
        if namespace:
            pods = await core_v1.list_namespaced_pod(namespace=namespace)
        else:
            pods = await core_v1.list_pod_for_all_namespaces()

        # 获取资源使用情况（可能为空，如果metrics-server不可用）
        # 若未传入 custom_api，则在此初始化一个（以便直接调用该函数）
        if not custom_api:
            custom_api = client.CustomObjectsApi()
        metrics_map = await _list_pod_metrics(namespace, custom_api)

        result = []
        for pod in pods.items:
            name = pod.metadata.name
            ns = pod.metadata.namespace or (namespace or "")

            container_statuses = _extract_container_statuses(pod)
            restart_count = sum(cs.get("restart_count", 0) for cs in container_statuses)
            controlled_by = _get_controlled_by(pod)

            # 资源使用
            metrics = metrics_map.get(f"{ns}/{name}", {"cpu_m": 0.0, "memory_mb": 0.0})
            cpu_cores = round(float(metrics.get("cpu_m", 0.0)) / 1000.0, 3)
            memory_mb = int(round(float(metrics.get("memory_mb", 0.0))))

            # 如果 Pod 状态为 Failed，获取 reason 和 message
            status_reason = ""
            status_message = ""
            if pod.status and pod.status.phase == "Failed":
                status_reason = "Failed"
                pod.status.phase = getattr(pod.status, "reason", "Failed") or "Failed"
                status_message = getattr(pod.status, "message", "") or ""

            result.append(
                {
                    "namespace": ns,
                    "name": name,
                    "containers": container_statuses,
                    "restart_count": restart_count,
                    "controlled_by": controlled_by,
                    "pod_ip": (pod.status.pod_ip if pod.status else None),
                    "creation_timestamp": _format_time_beijing(pod.metadata.creation_timestamp),
                    "node_name": pod.spec.node_name if pod.spec else "",
                    "status": pod.status.phase if pod.status else "Unknown",
                    "status_reason": status_reason,
                    "status_message": status_message,
                    "current_cpu_cores": cpu_cores,
                    "current_memory_mb": memory_mb,
                }
            )

        return web.json_response({"success": True, "data": result, "total": len(result)})

    except ApiException as e:
        logger.error(f"查询Pod列表失败: {e}")
        return web.json_response({"error": f"Kubernetes API错误: {e.reason}"}, status=e.status or 500)
    except Exception as e:
        logger.error(f"查询Pod列表异常: {e}")
        return web.json_response({"error": f"服务器内部错误: {str(e)}"}, status=500)
