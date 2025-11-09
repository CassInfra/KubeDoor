import asyncio
from datetime import datetime
from typing import List, Dict, Any

from aiohttp import web, ClientSession
from kubernetes_asyncio import client
from kubernetes_asyncio.client.exceptions import ApiException
from loguru import logger

from utils import parse_cpu, parse_memory, parse_storage_to_gib, bytes_to_gib, parse_pods


async def get_node_disk_usage(session: ClientSession, node_name: str) -> dict:
    """Get node disk usage from the /stats/summary endpoint asynchronously."""
    token_path = "/var/run/secrets/kubernetes.io/serviceaccount/token"
    try:
        with open(token_path, "r") as f:
            token = f.read()
    except FileNotFoundError:
        logger.error(f"Service account token not found at {token_path}")
        return {}

    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://kubernetes.default.svc/api/v1/nodes/{node_name}/proxy/stats/summary"

    try:
        async with session.get(url, headers=headers, ssl=False) as response:
            response.raise_for_status()
            data = await response.json()
            return {
                "nodefs_capacity_gib": bytes_to_gib(data.get("node", {}).get("fs", {}).get("capacityBytes", 0)),
                "nodefs_used_gib": bytes_to_gib(data.get("node", {}).get("fs", {}).get("usedBytes", 0)),
                "imagefs_capacity_gib": bytes_to_gib(
                    data.get("node", {}).get("runtime", {}).get("imageFs", {}).get("capacityBytes", 0)
                ),
                "imagefs_used_gib": bytes_to_gib(
                    data.get("node", {}).get("runtime", {}).get("imageFs", {}).get("usedBytes", 0)
                ),
            }
    except Exception as e:
        logger.error(f"Failed to get disk usage for node {node_name}: {e}")
        return {}


async def _get_node_metrics(custom_api, node_name: str) -> Dict[str, Any]:
    """
    获取节点的实时资源使用情况
    """
    try:
        # 使用 CustomObjectsApi 获取节点的资源使用情况
        metrics = await custom_api.get_cluster_custom_object(
            group="metrics.k8s.io", version="v1beta1", plural="nodes", name=node_name
        )

        # 初始化CPU和内存使用
        cpu_usage = 0
        memory_usage = 0

        # 获取使用量
        if metrics and "usage" in metrics:
            cpu = metrics["usage"].get("cpu", "0")
            memory = metrics["usage"].get("memory", "0")

            # 转换CPU值（从字符串如 "100m" 转为毫核）
            cpu_usage = parse_cpu(str(cpu))
            # 转换内存值（从字符串如 "1024Mi" 转为MB，然后转为GiB）
            memory_mb = parse_memory(str(memory))
            memory_usage = memory_mb / 1024  # 转换为GiB

        return {'current_cpu_m': cpu_usage, 'current_memory_gi': memory_usage}
    except Exception as e:
        logger.warning(f"无法获取节点 {node_name} 的实时指标: {e}")
        return {'current_cpu_m': 0, 'current_memory_gi': 0.0}


async def _get_node_pod_count(core_v1, node_name: str) -> int:
    """
    获取节点上当前运行的Pod数量
    """
    try:
        pods = await core_v1.list_pod_for_all_namespaces(
            field_selector=f"spec.nodeName={node_name},status.phase!=Failed,status.phase!=Succeeded"
        )
        return len(pods.items)
    except Exception as e:
        logger.warning(f"无法获取节点 {node_name} 的Pod数量: {e}")
        return 0


async def _get_node_details(node, core_v1, custom_api, session, env):
    """Helper function to process a single node's details concurrently."""
    # 基本信息
    node_info = {
        "env": env,
        "name": node.metadata.name,
        "os_image": node.status.node_info.os_image if node.status.node_info else "Unknown",
        "kernel_version": node.status.node_info.kernel_version if node.status.node_info else "Unknown",
        "container_runtime": (node.status.node_info.container_runtime_version if node.status.node_info else "Unknown"),
        "kubelet_version": node.status.node_info.kubelet_version if node.status.node_info else "Unknown",
    }

    # 节点状态条件 - 只保留 status 为 True 的条件
    conditions = []
    if node.status.conditions:
        for condition in node.status.conditions:
            if condition.status == "True":
                conditions.append(condition.type)
    node_info["conditions"] = conditions

    # 可分配资源
    allocatable = node.status.allocatable or {}
    node_info["allocatable_cpu_m"] = parse_cpu(allocatable.get('cpu', '0'))
    memory_mb = parse_memory(allocatable.get('memory', '0'))
    node_info["allocatable_memory_gi"] = memory_mb / 1024  # 转换为GiB
    ephemeral_storage = allocatable.get('ephemeral-storage', '0')
    node_info["allocatable_ephemeral_storage_gi"] = parse_storage_to_gib(ephemeral_storage)
    node_info["allocatable_pods"] = parse_pods(allocatable.get('pods', '0'))

    # 容量
    capacity = node.status.capacity or {}
    node_info["capacity_cpu_m"] = parse_cpu(capacity.get('cpu', '0'))
    memory_mb = parse_memory(capacity.get('memory', '0'))
    node_info["capacity_memory_gi"] = memory_mb / 1024  # 转换为GiB
    ephemeral_storage = capacity.get('ephemeral-storage', '0')
    node_info["capacity_ephemeral_storage_gi"] = parse_storage_to_gib(ephemeral_storage)
    node_info["capacity_pods"] = parse_pods(capacity.get('pods', '0'))

    # Concurrently fetch disk usage, metrics, and pod count
    tasks = [
        get_node_disk_usage(session, node.metadata.name),
        (
            _get_node_metrics(custom_api, node.metadata.name)
            if custom_api
            else asyncio.sleep(0, result={'current_cpu_m': 0, 'current_memory_gi': 0.0})
        ),
        _get_node_pod_count(core_v1, node.metadata.name),
    ]
    disk_usage, metrics, pod_count = await asyncio.gather(*tasks)

    node_info.update(disk_usage)
    node_info.update(metrics)
    node_info["current_pods"] = pod_count

    # 调度状态
    node_info["schedulable"] = not (node.spec.unschedulable if node.spec.unschedulable else False)

    # 创建时间
    node_info["creation_timestamp"] = (
        node.metadata.creation_timestamp.isoformat() if node.metadata.creation_timestamp else None
    )

    return node_info


async def get_nodes_list(core_v1, custom_api, request):
    """
    GET接口：获取所有节点的详细信息
    参数：
    - env: 集群名称
    - core_v1: Kubernetes CoreV1Api客户端
    - custom_api: Kubernetes CustomObjectsApi客户端
    返回：节点详细信息列表
    """
    try:
        env = request.query.get('env')

        if not env:
            return web.json_response({"success": False, "data": {"error": "缺少必要参数: env"}}, status=400)

        nodes = await core_v1.list_node()

        async with ClientSession() as session:
            tasks = [_get_node_details(node, core_v1, custom_api, session, env) for node in nodes.items]
            result = await asyncio.gather(*tasks)

        return web.json_response({"success": True, "data": {"nodes": result, "total": len(result)}})

    except ApiException as e:
        logger.error(f"获取节点信息失败: {e}")
        return web.json_response(
            {"success": False, "data": {"error": f"Kubernetes API错误: {e.reason}"}}, status=e.status
        )
    except Exception as e:
        logger.error(f"获取节点信息异常: {e}")
        return web.json_response({"success": False, "data": {"error": f"服务器内部错误: {str(e)}"}}, status=500)


async def cordon_nodes(core_v1, request):
    """
    POST接口：批量禁止节点调度
    参数：
    - env: 集群名称（query参数）
    - node_names: 节点名称列表（请求体）
    - core_v1: Kubernetes CoreV1Api客户端
    """
    try:
        import json

        env = request.query.get('env')

        # 从请求体中获取node_names
        try:
            body = await request.json()
            node_names = body.get('node_names', [])
        except Exception as e:
            return web.json_response({"success": False, "data": {"error": f"请求体解析失败: {str(e)}"}}, status=400)

        if not env:
            return web.json_response({"success": False, "data": {"error": "缺少必要参数: env"}}, status=400)

        if not node_names or not isinstance(node_names, list):
            return web.json_response(
                {"success": False, "data": {"error": "node_names 必须是非空的节点名称列表"}}, status=400
            )

        success_nodes = []
        failed_nodes = []

        for node_name in node_names:
            try:
                # 获取节点
                node = await core_v1.read_node(name=node_name)

                # 设置不可调度
                if not node.spec:
                    node.spec = client.V1NodeSpec()
                node.spec.unschedulable = True

                # 更新节点
                await core_v1.patch_node(name=node_name, body=node)
                success_nodes.append(node_name)
                logger.info(f"节点 '{node_name}' 已设置为不可调度")

            except ApiException as e:
                error_msg = f"节点 '{node_name}' 设置失败: {e.reason}"
                failed_nodes.append({"node": node_name, "error": error_msg})
                logger.error(error_msg)
            except Exception as e:
                error_msg = f"节点 '{node_name}' 设置异常: {str(e)}"
                failed_nodes.append({"node": node_name, "error": error_msg})
                logger.error(error_msg)

        result = {
            "success": len(failed_nodes) == 0,
            "data": {
                "message": f"批量禁止调度完成，成功: {len(success_nodes)}，失败: {len(failed_nodes)}",
                "env": env,
                "success_nodes": success_nodes,
                "failed_nodes": failed_nodes,
                "timestamp": datetime.now().isoformat(),
            },
        }

        return web.json_response(result)

    except Exception as e:
        logger.error(f"批量禁止节点调度异常: {e}")
        return web.json_response({"success": False, "data": {"error": f"服务器内部错误: {str(e)}"}}, status=500)


async def uncordon_nodes(core_v1, request):
    """
    POST接口：批量解除节点调度禁止
    参数：
    - env: 集群名称（query参数）
    - node_names: 节点名称列表（请求体）
    - core_v1: Kubernetes CoreV1Api客户端
    """
    try:
        import json

        env = request.query.get('env')

        # 从请求体中获取node_names
        try:
            body = await request.json()
            node_names = body.get('node_names', [])
        except Exception as e:
            return web.json_response({"success": False, "data": {"error": f"请求体解析失败: {str(e)}"}}, status=400)

        if not env:
            return web.json_response({"success": False, "data": {"error": "缺少必要参数: env"}}, status=400)

        if not node_names or not isinstance(node_names, list):
            return web.json_response(
                {"success": False, "data": {"error": "node_names 必须是非空的节点名称列表"}}, status=400
            )

        success_nodes = []
        failed_nodes = []

        for node_name in node_names:
            try:
                # 获取节点
                node = await core_v1.read_node(name=node_name)

                # 设置可调度
                if not node.spec:
                    node.spec = client.V1NodeSpec()
                node.spec.unschedulable = False

                # 更新节点
                await core_v1.patch_node(name=node_name, body=node)
                success_nodes.append(node_name)
                logger.info(f"节点 '{node_name}' 已设置为可调度")

            except ApiException as e:
                error_msg = f"节点 '{node_name}' 设置失败: {e.reason}"
                failed_nodes.append({"node": node_name, "error": error_msg})
                logger.error(error_msg)
            except Exception as e:
                error_msg = f"节点 '{node_name}' 设置异常: {str(e)}"
                failed_nodes.append({"node": node_name, "error": error_msg})
                logger.error(error_msg)

        result = {
            "success": len(failed_nodes) == 0,
            "data": {
                "message": f"批量解除调度禁止完成，成功: {len(success_nodes)}，失败: {len(failed_nodes)}",
                "env": env,
                "success_nodes": success_nodes,
                "failed_nodes": failed_nodes,
                "timestamp": datetime.now().isoformat(),
            },
        }

        return web.json_response(result)

    except Exception as e:
        logger.error(f"批量解除节点调度禁止异常: {e}")
        return web.json_response({"success": False, "data": {"error": f"服务器内部错误: {str(e)}"}}, status=500)


# 便捷函数，用于直接调用而不通过HTTP接口
async def get_all_nodes_info(core_v1, custom_api=None, env: str = "default") -> List[Dict[str, Any]]:
    """
    直接获取所有节点信息的便捷函数
    """
    try:
        nodes = await core_v1.list_node()
        result = []

        for node in nodes.items:
            node_info = {
                "env": env,
                "name": node.metadata.name,
                "os_image": node.status.node_info.os_image if node.status.node_info else "Unknown",
                "kernel_version": node.status.node_info.kernel_version if node.status.node_info else "Unknown",
                "container_runtime": (
                    node.status.node_info.container_runtime_version if node.status.node_info else "Unknown"
                ),
                "kubelet_version": node.status.node_info.kubelet_version if node.status.node_info else "Unknown",
            }

            # 节点状态条件 - 只保留 status 为 True 的条件
            conditions = []
            if node.status.conditions:
                for condition in node.status.conditions:
                    # 只保留状态为 True 的条件
                    if condition.status == "True":
                        conditions.append(condition.type)
            node_info["conditions"] = conditions

            # 可分配资源
            allocatable = node.status.allocatable or {}
            node_info["allocatable_cpu_m"] = parse_cpu(allocatable.get('cpu', '0'))
            memory_mb = parse_memory(allocatable.get('memory', '0'))
            node_info["allocatable_memory_gi"] = memory_mb / 1024  # 转换为GiB
            node_info["allocatable_pods"] = int(allocatable.get('pods', '0'))

            # 获取实时资源使用情况
            if custom_api:
                metrics = await _get_node_metrics(custom_api, node.metadata.name)
                node_info.update(metrics)
            else:
                node_info["current_cpu_m"] = 0
                node_info["current_memory_gi"] = 0.0

            # 获取当前Pod数量
            node_info["current_pods"] = await _get_node_pod_count(core_v1, node.metadata.name)

            # 调度状态
            node_info["schedulable"] = not (node.spec.unschedulable if node.spec.unschedulable else False)

            # 创建时间
            node_info["creation_timestamp"] = (
                node.metadata.creation_timestamp.isoformat() if node.metadata.creation_timestamp else None
            )

            result.append(node_info)

        return result

    except Exception as e:
        logger.error(f"获取节点信息失败: {e}")
        raise


async def batch_cordon_nodes(core_v1, node_names: List[str]) -> Dict[str, Any]:
    """
    批量禁止节点调度的便捷函数
    """
    success_nodes = []
    failed_nodes = []

    for node_name in node_names:
        try:
            node = await core_v1.read_node(name=node_name)

            if not node.spec:
                node.spec = client.V1NodeSpec()
            node.spec.unschedulable = True

            await core_v1.patch_node(name=node_name, body=node)
            success_nodes.append(node_name)
            logger.info(f"节点 '{node_name}' 已设置为不可调度")

        except Exception as e:
            error_msg = f"节点 '{node_name}' 设置失败: {str(e)}"
            failed_nodes.append({"node": node_name, "error": error_msg})
            logger.error(error_msg)

    return {
        "success": len(failed_nodes) == 0,
        "success_nodes": success_nodes,
        "failed_nodes": failed_nodes,
        "timestamp": datetime.now().isoformat(),
    }


async def batch_uncordon_nodes(core_v1, node_names: List[str]) -> Dict[str, Any]:
    """
    批量解除节点调度禁止的便捷函数
    """
    success_nodes = []
    failed_nodes = []

    for node_name in node_names:
        try:
            node = await core_v1.read_node(name=node_name)

            if not node.spec:
                node.spec = client.V1NodeSpec()
            node.spec.unschedulable = False

            await core_v1.patch_node(name=node_name, body=node)
            success_nodes.append(node_name)
            logger.info(f"节点 '{node_name}' 已设置为可调度")

        except Exception as e:
            error_msg = f"节点 '{node_name}' 设置失败: {str(e)}"
            failed_nodes.append({"node": node_name, "error": error_msg})
            logger.error(error_msg)

    return {
        "success": len(failed_nodes) == 0,
        "success_nodes": success_nodes,
        "failed_nodes": failed_nodes,
        "timestamp": datetime.now().isoformat(),
    }
