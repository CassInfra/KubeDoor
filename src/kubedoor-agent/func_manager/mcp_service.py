import json
from datetime import timedelta, timezone

from aiohttp import web
from kubernetes_asyncio.client import AppsV1Api, CoreV1Api, CustomObjectsApi
from kubernetes_asyncio.client.rest import ApiException
from loguru import logger

import utils


class MCPService:
    def __init__(self, core_v1_api: CoreV1Api, custom_api: CustomObjectsApi, apps_v1_api: AppsV1Api):
        self.core_v1 = core_v1_api
        self.custom_api = custom_api
        self.apps_v1 = apps_v1_api

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

            events = await self.core_v1.list_event_for_all_namespaces(field_selector=field_selector, _request_timeout=30)

            event_list = []
            for event in events.items:
                event_list.append(
                    {
                        "name": event.metadata.name,
                        "namespace": event.metadata.namespace,
                        "type": event.type,
                        "reason": event.reason,
                        "message": event.message,
                        "involved_object": {
                            "kind": event.involved_object.kind,
                            "name": event.involved_object.name,
                            "namespace": event.involved_object.namespace,
                        },
                        "count": event.count,
                        "first_timestamp": (event.first_timestamp.isoformat() if event.first_timestamp else None),
                        "last_timestamp": (event.last_timestamp.isoformat() if event.last_timestamp else None),
                        "source": (
                            {"component": event.source.component, "host": event.source.host} if event.source else None
                        ),
                    }
                )

            logger.info(f"获取事件成功，共 {len(event_list)} 条")
            return web.json_response({"events": event_list, "success": True})
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
            pods = await self.core_v1.list_pod_for_all_namespaces()

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

                current_pods = 0
                for pod in pods.items:
                    if pod.spec.node_name == node_name:
                        current_pods += 1

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
        """获取指定命名空间和Deployment下的所有Pod信息（包括被隔离的Pod）"""
        namespace = request.query.get("namespace")
        deployment_name = request.query.get("deployment")

        try:
            deployment = await self.apps_v1.read_namespaced_deployment(deployment_name, namespace)
            selector = deployment.spec.selector.match_labels
            selector_str = ",".join([f"{k}={v}" for k, v in selector.items()])

            pods_by_label = await self.core_v1.list_namespaced_pod(namespace=namespace, label_selector=selector_str)
            lenmline = pods_by_label.items[0].metadata.name.count("-")

            all_pods = await self.core_v1.list_namespaced_pod(namespace=namespace)
            pods_by_match = []
            for pod in all_pods.items:
                owner_refs = pod.metadata.owner_references or []
                if (
                    not owner_refs
                    and pod.metadata.name.startswith(deployment_name + '-')
                    and pod.metadata.name.count("-") == lenmline
                ):
                    pods_by_match.append(pod)

            all_related_pods = {
                pod.metadata.name: pod
                for pod in pods_by_label.items
                if pod.metadata.name.startswith(deployment_name + '-') and pod.metadata.name.count("-") == lenmline
            }
            for pod in pods_by_match:
                all_related_pods[pod.metadata.name] = pod

            pod_list = []
            for pod in all_related_pods.values():
                metrics = await self._get_pod_metrics(namespace, pod.metadata.name)

                created_at = None
                if pod.metadata.creation_timestamp:
                    utc_time = pod.metadata.creation_timestamp.replace(tzinfo=timezone.utc)
                    beijing_time = utc_time.astimezone(timezone(timedelta(hours=8)))
                    created_at = beijing_time.strftime("%Y-%m-%d %H:%M:%S")

                cpu = round(metrics["cpu"])
                memory = round(metrics["memory"])

                pod_status_reason = ""

                if pod.status.phase != "Running":
                    if pod.status.conditions:
                        for cond in pod.status.conditions:
                            if cond.type == "PodScheduled" and cond.status != "True":
                                pod_status_reason = cond.message or cond.reason or ""
                                break
                    if not pod_status_reason and pod.status.reason:
                        pod_status_reason = pod.status.reason

                    if not pod_status_reason and pod.status.container_statuses:
                        for cs in pod.status.container_statuses:
                            if cs.state and (cs.state.waiting or cs.state.terminated):
                                if cs.state.waiting:
                                    container_reason = cs.state.waiting.reason or ""
                                    container_message = cs.state.waiting.message or ""
                                    pod_status_reason = (
                                        f"{container_reason}: {container_message}"
                                        if container_message
                                        else container_reason
                                    )
                                elif cs.state.terminated:
                                    container_reason = cs.state.terminated.reason or ""
                                    container_message = cs.state.terminated.message or ""
                                    exit_code = cs.state.terminated.exit_code
                                    pod_status_reason = (
                                        f"{container_reason} (exit: {exit_code}): {container_message}"
                                        if container_message
                                        else f"{container_reason} (exit: {exit_code})"
                                    )
                                break

                    if not pod_status_reason:
                        event_reason, event_message = await self._get_pod_events(namespace, pod.metadata.name)
                        if event_message:
                            pod_status_reason = f"{event_reason}: {event_message}" if event_reason else event_message

                last_status = ""
                restart_count = (
                    sum(container_status.restart_count for container_status in pod.status.container_statuses)
                    if pod.status.container_statuses
                    else 0
                )
                if restart_count > 0 and pod.status.container_statuses:
                    for cs in pod.status.container_statuses:
                        if cs.last_state and (cs.last_state.terminated or cs.last_state.waiting):
                            if cs.last_state.terminated:
                                last_status = f"Terminated: {cs.last_state.terminated.reason or ''} ({cs.last_state.terminated.exit_code})"
                            elif cs.last_state.waiting:
                                last_status = f"Waiting: {cs.last_state.waiting.reason or ''}"
                            break

                main_container_image = ""
                if pod.spec.containers and len(pod.spec.containers) > 0:
                    main_container_image = pod.spec.containers[0].image

                pod_info = {
                    "name": pod.metadata.name,
                    "status": pod.status.phase,
                    "ready": (
                        all(container_status.ready for container_status in pod.status.container_statuses)
                        if pod.status.container_statuses
                        else False
                    ),
                    "pod_ip": pod.status.pod_ip,
                    "cpu": f"{cpu}m",
                    "memory": f"{memory}MB",
                    "created_at": created_at,
                    "app_label": pod.metadata.labels.get("app", "无"),
                    "image": main_container_image,
                    "node_name": pod.spec.node_name,
                    "restart_count": restart_count,
                    "restart_reason": last_status,
                    "exception_reason": pod_status_reason,
                }
                pod_list.append(pod_info)

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

    async def _get_pod_metrics(self, namespace, pod_name):
        """获取指定Pod的CPU和内存使用情况"""
        try:
            metrics = await self.custom_api.get_namespaced_custom_object(
                group="metrics.k8s.io",
                version="v1beta1",
                namespace=namespace,
                plural="pods",
                name=pod_name,
            )

            cpu_usage = 0
            memory_usage = 0

            for container in metrics.get("containers", []):
                cpu = container.get("usage", {}).get("cpu", "0")
                memory = container.get("usage", {}).get("memory", "0")

                cpu = utils.parse_cpu(cpu)
                memory = utils.parse_memory(memory)

                cpu_usage += cpu
                memory_usage += memory

            return {
                "cpu": round(cpu_usage, 2),
                "memory": round(memory_usage, 2),
            }
        except Exception as exc:
            logger.error(f"获取Pod {pod_name} 资源使用情况失败: {exc}")
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
