import asyncio
import json
import time
from collections import Counter
from datetime import datetime

from aiohttp import web
from kubernetes_asyncio.client import AppsV1Api, CoreV1Api, CustomObjectsApi
from kubernetes_asyncio.client.rest import ApiException
from loguru import logger

import utils
from func_manager import workload_status
from k8s_client_manager import list_all_raw
from res_manager.pod_manager import _list_pod_metrics

METRICS_TTL = 10
METRICS_TIMEOUT = 3
EVENT_LOOKUP_LIMIT = 20
EVENT_LOOKUP_CONCURRENCY = 5


def _isoformat(value):
    """K8S 的 RFC3339 时间 → datetime.isoformat()(如 2026-10-05T12:00:00+00:00,和原来模型对象的输出一致)"""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
    except (TypeError, ValueError):
        return value


def _render_events(events):
    """原始 JSON 的事件列表 → 接口响应(JSON 文本)。纯 CPU 计算,调用方放到线程里执行"""
    event_list = []
    for event in events:
        meta = event.get("metadata") or {}
        involved = event.get("involvedObject") or {}
        source = event.get("source")
        event_list.append(
            {
                "name": meta.get("name"),
                "namespace": meta.get("namespace"),
                "type": event.get("type"),
                "reason": event.get("reason"),
                "message": event.get("message"),
                "involved_object": {
                    "kind": involved.get("kind"),
                    "name": involved.get("name"),
                    "namespace": involved.get("namespace"),
                },
                "count": event.get("count"),
                "first_timestamp": _isoformat(event.get("firstTimestamp")),
                "last_timestamp": _isoformat(event.get("lastTimestamp")),
                "source": {"component": source.get("component"), "host": source.get("host")} if source is not None else None,
            }
        )
    return json.dumps({"events": event_list, "success": True})


class MCPService:
    def __init__(self, core_v1_api: CoreV1Api, custom_api: CustomObjectsApi, apps_v1_api: AppsV1Api, workload_cache=None):
        self.core_v1 = core_v1_api
        self.custom_api = custom_api
        self.apps_v1 = apps_v1_api
        self.workload_cache = workload_cache
        self._metrics_cache = {}  # namespace -> (过期时间, {"ns/name": {"cpu_m", "memory_mb"}})

    async def get_namespace_events(self, request):
        """获取指定命名空间的事件，如果不指定namespace则获取所有命名空间的事件"""
        namespace = request.query.get("namespace")

        try:
            field_selector = None
            if namespace:
                field_selector = f"involvedObject.namespace={namespace}"
                logger.info(f"获取命名空间 {namespace} 的事件")
            else:
                logger.info("获取所有命名空间的事件")

            events = await list_all_raw(
                self.core_v1.list_event_for_all_namespaces, field_selector=field_selector, _request_timeout=30
            )
            body = await asyncio.to_thread(_render_events, events)
            logger.info(f"获取事件成功，共 {len(events)} 条")
            return web.Response(text=body, content_type="application/json")
        except ApiException as exc:
            error_message = f"获取事件失败: {exc}"
            logger.error(error_message)
            return web.json_response({"message": error_message, "success": False}, status=500)
        except Exception as exc:
            error_message = f"获取事件时发生未知错误: {exc}"
            logger.exception(error_message)
            return web.json_response({"message": error_message, "success": False}, status=500)

    async def get_nodes_info(self, request):
        """获取所有K8S节点的详细信息"""
        try:
            logger.info("开始获取K8S节点信息...")

            nodes = await self.core_v1.list_node()
            # 只数每个节点上的 Pod 数,用原始 JSON,不反序列化全集群的 Pod 模型对象
            pods = await list_all_raw(self.core_v1.list_pod_for_all_namespaces)
            pods_per_node = Counter((pod.get("spec") or {}).get("nodeName") for pod in pods)

            node_list = []

            for node in nodes.items:
                node_name = node.metadata.name

                node_ip = ""
                for address in node.status.addresses:
                    if address.type == "InternalIP":
                        node_ip = address.address
                        break

                container_runtime = node.status.node_info.container_runtime_version
                os_image = f"{node.status.node_info.os_image} {node.status.node_info.kernel_version}"
                kubelet_version = node.status.node_info.kubelet_version

                conditions = []
                for condition in node.status.conditions:
                    if condition.status == "True":
                        conditions.append(condition.type)

                allocatable_cpu = 0
                allocatable_memory = 0
                max_pods = 0

                if node.status.allocatable:
                    allocatable_cpu_str = node.status.allocatable.get("cpu", "0")
                    allocatable_cpu = utils.parse_cpu(allocatable_cpu_str)

                    allocatable_memory_str = node.status.allocatable.get("memory", "0")
                    allocatable_memory = utils.parse_memory(allocatable_memory_str)

                    max_pods_str = node.status.allocatable.get("pods", "0")
                    try:
                        max_pods = int(max_pods_str)
                    except (ValueError, AttributeError):
                        max_pods = 0

                current_pods = pods_per_node[node_name]

                metrics = await self._get_node_metrics(node_name)
                current_cpu = metrics["cpu"]
                current_memory = metrics["memory"]

                node_info = {
                    "name": node_name,
                    "ip": node_ip,
                    "os_image": os_image,
                    "container_runtime": container_runtime,
                    "kubelet_version": kubelet_version,
                    "conditions": ", ".join(conditions) if conditions else "",
                    "allocatable_cpu": round(allocatable_cpu),
                    "current_cpu": round(current_cpu),
                    "allocatable_memory": round(allocatable_memory),
                    "current_memory": round(current_memory),
                    "max_pods": max_pods,
                    "current_pods": current_pods,
                }

                node_list.append(node_info)

            logger.info(f"获取节点信息成功，共 {len(node_list)} 个节点")
            return web.json_response({"nodes": node_list, "success": True})

        except ApiException as exc:
            error_message = f"获取节点信息失败: {exc}"
            logger.error(error_message)
            return web.json_response({"message": error_message, "success": False}, status=500)
        except Exception as exc:
            error_message = f"获取节点信息时发生未知错误: {exc}"
            logger.exception(error_message)
            return web.json_response({"message": error_message, "success": False}, status=500)

    async def get_deployment_pods(self, request):
        """获取指定命名空间和Deployment下的所有Pod信息（包括被隔离的Pod）

        缓存已同步时直接读 WorkloadCache(不调 K8S API),否则直查 API 兜底;两条路径都用
        workload_status 压成同样的结构。CPU/内存一次查整个 namespace 的 metrics。
        """
        namespace = request.query.get("namespace")
        deployment_name = request.query.get("deployment")
        if not namespace or not deployment_name:
            return web.json_response({"message": "缺少 namespace 或 deployment 参数", "success": False}, status=400)

        try:
            cache = self.workload_cache
            if cache is not None and cache.synced:
                if cache.get_deployment(namespace, deployment_name) is None:
                    message = f'deployments.apps "{deployment_name}" not found'
                    return web.json_response({"message": message, "success": False}, status=404)
                records = cache.pods_of(namespace, deployment_name)
            else:
                records = await self._deployment_pods_from_api(namespace, deployment_name)

            metrics = await self._namespace_pod_metrics(namespace)
            pod_list = [
                workload_status.pod_view(rec, metrics.get(f"{namespace}/{rec['name']}")) for rec in records
            ]
            await self._fill_event_reasons(namespace, records, pod_list)
            return web.json_response({"success": True, "pods": pod_list})
        except ApiException as exc:
            error_message = (
                json.loads(exc.body).get("message") if hasattr(exc, 'body') and exc.body else f"获取Pod信息失败: {str(exc)}"
            )
            logger.error(error_message)
            return web.json_response({"message": error_message, "success": False}, status=500)
        except Exception as exc:
            error_message = f"获取Pod信息时发生未知错误: {str(exc)}"
            logger.exception(error_message)
            return web.json_response({"message": error_message, "success": False}, status=500)

    async def _deployment_pods_from_api(self, namespace, deployment_name):
        """缓存未同步时的兜底:直查 API,用原始 JSON 走和缓存一样的归属判断"""
        # deployment 不存在时抛 404,沿用原来的错误信息
        await self.apps_v1.read_namespaced_deployment(deployment_name, namespace)
        resp = await self.core_v1.list_namespaced_pod(namespace=namespace, _preload_content=False, _request_timeout=30)
        try:
            body = await resp.read()
            if resp.status != 200:
                raise ApiException(status=resp.status, reason=body[:300].decode("utf-8", "replace"))
        finally:
            resp.release()
        pods = [workload_status.slim_pod(obj) for obj in json.loads(body).get("items") or []]
        return sorted((p for p in pods if p["deployment"] == deployment_name), key=lambda p: p["name"])

    async def _namespace_pod_metrics(self, namespace):
        """一次取整个 namespace 的 pod 指标,缓存 METRICS_TTL 秒(metrics-server 本身 15s 左右才更新一次)"""
        now = time.monotonic()
        cached = self._metrics_cache.get(namespace)
        if cached and cached[0] > now:
            return cached[1]
        try:
            metrics = await asyncio.wait_for(_list_pod_metrics([namespace], self.custom_api), METRICS_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning(f"获取命名空间 {namespace} 的 Pod 指标超时")
            metrics = {}
        if metrics:
            self._metrics_cache[namespace] = (now + METRICS_TTL, metrics)
        return metrics

    async def _fill_event_reasons(self, namespace, records, pod_list):
        """非 Running 又看不出原因的 pod,用最近一条事件补异常原因(并发、限量)"""
        targets = [
            (rec, view)
            for rec, view in zip(records, pod_list)
            if rec["phase"] != "Running" and not rec["exception_reason"]
        ][:EVENT_LOOKUP_LIMIT]
        if not targets:
            return
        semaphore = asyncio.Semaphore(EVENT_LOOKUP_CONCURRENCY)

        async def fill(rec, view):
            async with semaphore:
                try:
                    reason, message = await asyncio.wait_for(self._get_pod_events(namespace, rec["name"]), 5)
                except asyncio.TimeoutError:
                    return
            if message:
                view["exception_reason"] = f"{reason}: {message}" if reason else message

        await asyncio.gather(*(fill(rec, view) for rec, view in targets))

    async def _get_node_metrics(self, node_name):
        """获取节点的资源使用情况"""
        try:
            metrics = await self.custom_api.get_cluster_custom_object(
                group="metrics.k8s.io", version="v1beta1", plural="nodes", name=node_name
            )

            cpu_usage = 0
            memory_usage = 0

            if metrics and "usage" in metrics:
                cpu = metrics["usage"].get("cpu", "0")
                memory = metrics["usage"].get("memory", "0")

                if isinstance(cpu, str):
                    cpu_usage = utils.parse_cpu(cpu)
                else:
                    cpu_usage = utils.parse_cpu(str(cpu))

                if isinstance(memory, str):
                    memory_usage = utils.parse_memory(memory)
                else:
                    memory_usage = utils.parse_memory(str(memory))

            return {
                "cpu": round(cpu_usage, 2),
                "memory": round(memory_usage, 2),
            }
        except Exception as exc:
            logger.error(f"获取节点 {node_name} 资源使用情况失败: {exc}")
            return {"cpu": 0, "memory": 0}

    async def _get_pod_events(self, namespace, pod_name):
        """获取指定Pod的事件信息"""
        try:
            field_selector = f"involvedObject.kind=Pod,involvedObject.name={pod_name}"
            events = await self.core_v1.list_namespaced_event(namespace=namespace, field_selector=field_selector)

            sorted_events = sorted(
                events.items,
                key=lambda event: event.last_timestamp or event.first_timestamp or event.metadata.creation_timestamp,
                reverse=True,
            )

            if sorted_events:
                latest_event = sorted_events[0]
                return latest_event.reason, latest_event.message

            return "", ""
        except Exception as exc:
            logger.error(f"获取Pod {pod_name} 事件失败: {exc}")
            return "", ""
