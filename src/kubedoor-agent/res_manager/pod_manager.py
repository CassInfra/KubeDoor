import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List

from aiohttp import web
from kubernetes_asyncio import client
from kubernetes_asyncio.client.exceptions import ApiException
from loguru import logger

from k8s_client_manager import list_all_raw
from utils import parse_cpu, parse_memory

BEIJING_TZ = timezone(timedelta(hours=8))


def _format_time_beijing(value: str | None) -> str:
    """将 K8S 的 RFC3339 时间转换为北京时间（UTC+8）的字符串，格式为 YYYY-MM-DD HH:MM:SS。"""
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return value


async def _list_pod_metrics(namespaces: List[str] | None, custom_api) -> Dict[str, Dict[str, float]]:
    """
    获取命名空间下所有Pod的实时资源使用情况（聚合到Pod级别）。
    支持多个命名空间查询。

    返回字典: { pod_name: {"cpu_m": mCPU, "memory_mb": MB} }
    """
    metrics_map: Dict[str, Dict[str, float]] = {}
    try:
        # 根据命名空间数量决定查询方式
        if namespaces and len(namespaces) == 1:
            # 单个命名空间
            pod_metrics = await custom_api.list_namespaced_custom_object(
                group="metrics.k8s.io", version="v1beta1", namespace=namespaces[0], plural="pods"
            )
            items = pod_metrics.get("items", [])
        elif namespaces and len(namespaces) > 1:
            # 多个命名空间，并行查询
            tasks = [
                custom_api.list_namespaced_custom_object(
                    group="metrics.k8s.io", version="v1beta1", namespace=ns, plural="pods"
                )
                for ns in namespaces
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            items = []
            for r in results:
                if isinstance(r, Exception):
                    continue
                items.extend(r.get("items", []))
        else:
            # 全部命名空间
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
        logger.warning(f"无法获取Pod指标: {e}")

    return metrics_map


def _terminated_info(terminated: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "terminated": {
            "exit_code": terminated.get("exitCode"),
            "reason": terminated.get("reason") or "",
            "started_at": _format_time_beijing(terminated.get("startedAt")),
            "finished_at": _format_time_beijing(terminated.get("finishedAt")),
        }
    }


def _extract_container_statuses(pod: Dict[str, Any]) -> List[Dict[str, Any]]:
    """提取各容器状态信息，包括常规容器和Init容器。pod 是 API 返回的原始 JSON。"""
    status = pod.get("status") or {}
    # 按顺序添加：Init 容器状态 -> 常规容器状态
    all_container_statuses = (status.get("initContainerStatuses") or []) + (status.get("containerStatuses") or [])
    if not all_container_statuses:
        return []

    # 从 spec 中获取 Init 容器的名称，用于区分
    init_container_names = {c.get("name") for c in (pod.get("spec") or {}).get("initContainers") or []}

    statuses = []
    for cs in all_container_statuses:
        state = "Unknown"
        state_info = {}

        cs_state = cs.get("state")
        if cs_state is not None:
            if cs_state.get("running") is not None:
                state = "Running"
                state_info = {"start_time": _format_time_beijing(cs_state["running"].get("startedAt"))}
            elif cs_state.get("waiting") is not None:
                state = "Waiting"
                waiting = cs_state["waiting"]
                state_info = {"reason": waiting.get("reason") or "", "message": waiting.get("message") or ""}
            elif cs_state.get("terminated") is not None:
                state = "Terminated"
                state_info = _terminated_info(cs_state["terminated"])

        restart_count = int(cs.get("restartCount") or 0)
        last_terminated = (cs.get("lastState") or {}).get("terminated")
        last_state_info = _terminated_info(last_terminated) if restart_count > 0 and last_terminated is not None else {}

        statuses.append(
            {
                "is_init": cs.get("name") in init_container_names,
                "name": cs.get("name"),
                "ready": bool(cs.get("ready", False)),
                "state": state,
                "state_info": state_info,
                "restart_count": restart_count,
                "last_state": last_state_info,
            }
        )
    return statuses


def _get_controlled_by(pod: Dict[str, Any]) -> str:
    """获取Pod的上级控制器（Controlled By）。"""
    owners = (pod.get("metadata") or {}).get("ownerReferences") or []
    if owners:
        return owners[0].get("kind") or ""
    return ""


def _render_pod_list(all_pods: List[Dict[str, Any]], metrics_map: Dict[str, Dict[str, float]], node_name: str | None) -> str:
    """原始 JSON 的 Pod 列表 → 接口响应(JSON 文本)。纯 CPU 计算,调用方放到线程里执行"""
    result = []
    for pod in all_pods:
        meta = pod.get("metadata") or {}
        spec = pod.get("spec")
        status = pod.get("status")
        # 如果指定了节点名称，过滤不在该节点上的Pod
        if node_name and (spec is None or spec.get("nodeName") != node_name):
            continue

        name = meta.get("name")
        ns = meta.get("namespace") or ""

        container_statuses = _extract_container_statuses(pod)
        restart_count = sum(cs.get("restart_count", 0) for cs in container_statuses)
        controlled_by = _get_controlled_by(pod)

        # 资源使用
        metrics = metrics_map.get(f"{ns}/{name}", {"cpu_m": 0.0, "memory_mb": 0.0})
        cpu_cores = round(float(metrics.get("cpu_m", 0.0)) / 1000.0, 3)
        memory_mb = int(round(float(metrics.get("memory_mb", 0.0))))

        # 如果 Pod 状态为 Failed，获取 reason 和 message
        phase = status.get("phase") if status is not None else "Unknown"
        status_reason = ""
        status_message = ""
        if phase == "Failed":
            status_reason = "Failed"
            phase = status.get("reason") or "Failed"
            status_message = status.get("message") or ""

        result.append(
            {
                "namespace": ns,
                "name": name,
                "containers": container_statuses,
                "restart_count": restart_count,
                "controlled_by": controlled_by,
                "pod_ip": status.get("podIP") if status is not None else None,
                "creation_timestamp": _format_time_beijing(meta.get("creationTimestamp")),
                "node_name": spec.get("nodeName") if spec is not None else "",
                "status": phase,
                "status_reason": status_reason,
                "status_message": status_message,
                "current_cpu_cores": cpu_cores,
                "current_memory_mb": memory_mb,
            }
        )
    return json.dumps({"success": True, "data": result, "total": len(result)})


async def get_pod_list(core_v1, custom_api, request):
    """
    GET接口：查询指定命名空间的Pod列表
    参数：
    - env: 集群名称（可选）
    - namespaces: 命名空间列表，逗号分隔（可选，支持多选）
    - node_name: 节点名称（可选，用于过滤指定节点上的Pod）
    返回字段：name, namespace, containers(各容器状态), restart_count(总重启次数),
            controlled_by, pod_ip, pod_ips, host_ip, creation_timestamp, node_name, status, current_cpu_cores, current_memory_mb

    直接处理原始 JSON(不反序列化成模型对象),解析和组装响应都在线程里做,几千个 Pod 也不卡事件循环
    """
    try:
        namespaces_str = request.query.get("namespaces", "")
        node_name = request.query.get("node_name")

        # 解析命名空间列表
        namespaces: List[str] | None = None
        if namespaces_str:
            namespaces = [ns.strip() for ns in namespaces_str.split(",") if ns.strip()]

        # 若未传入 custom_api，则在此初始化一个
        if not custom_api:
            custom_api = client.CustomObjectsApi()

        # 按节点过滤交给 apiserver,不把全集群 Pod 拉回来再筛
        field_selector = f"spec.nodeName={node_name}" if node_name else None

        # 根据命名空间数量决定查询方式
        if namespaces and len(namespaces) == 1:
            # 单个命名空间，并行获取Pod和metrics
            all_pods, metrics_map = await asyncio.gather(
                list_all_raw(core_v1.list_namespaced_pod, namespace=namespaces[0], field_selector=field_selector),
                _list_pod_metrics(namespaces, custom_api)
            )
        elif namespaces and len(namespaces) > 1:
            # 多个命名空间，并行查询所有命名空间的Pod和metrics
            pod_tasks = [
                list_all_raw(core_v1.list_namespaced_pod, namespace=ns, field_selector=field_selector) for ns in namespaces
            ]
            results = await asyncio.gather(
                asyncio.gather(*pod_tasks, return_exceptions=True),
                _list_pod_metrics(namespaces, custom_api)
            )
            pod_results, metrics_map = results
            all_pods = []
            for r in pod_results:
                if isinstance(r, Exception):
                    continue
                all_pods.extend(r)
        else:
            # 全部命名空间
            all_pods, metrics_map = await asyncio.gather(
                list_all_raw(core_v1.list_pod_for_all_namespaces, field_selector=field_selector),
                _list_pod_metrics(None, custom_api)
            )

        body = await asyncio.to_thread(_render_pod_list, all_pods, metrics_map, node_name)
        return web.Response(text=body, content_type="application/json")

    except ApiException as e:
        logger.error(f"查询Pod列表失败: {e}")
        return web.json_response({"error": f"Kubernetes API错误: {e.reason}"}, status=e.status or 500)
    except Exception as e:
        logger.error(f"查询Pod列表异常: {e}")
        return web.json_response({"error": f"服务器内部错误: {str(e)}"}, status=500)
