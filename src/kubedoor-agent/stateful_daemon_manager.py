from datetime import datetime, timezone, timedelta
import json
from aiohttp import web
from kubernetes_asyncio import client
from kubernetes_asyncio.client.exceptions import ApiException
from kubernetes_asyncio.client import AppsV1Api
from loguru import logger


async def get_statefulset_list(apps_v1, request):
    """
    GET接口：查询StatefulSet列表
    参数：
    - env: 集群名称
    - namespace: 命名空间（可选，如果不提供则查询所有命名空间）
    - apps_v1: Kubernetes AppsV1Api客户端
    返回：集群名、命名空间、StatefulSet名称、Pod数、CPU/Memory的requests和limits
    """
    try:
        env = request.query.get('env')
        namespace = request.query.get('namespace')

        if not env:
            return web.json_response({"error": "缺少必要参数: env"}, status=400)

        # 查询StatefulSet列表
        if namespace:
            statefulset_list = await apps_v1.list_namespaced_stateful_set(namespace=namespace)
        else:
            statefulset_list = await apps_v1.list_stateful_set_for_all_namespaces()

        result = []
        for sts in statefulset_list.items:
            # 获取Pod数量
            desired_replicas = sts.spec.replicas or 0
            ready_replicas = sts.status.ready_replicas or 0

            # 计算资源请求和限制
            cpu_requests, memory_requests = 0, 0
            cpu_limits, memory_limits = 0, 0

            if sts.spec.template.spec.containers:
                for container in sts.spec.template.spec.containers:
                    # CPU资源
                    if container.resources:
                        if container.resources.requests and 'cpu' in container.resources.requests:
                            cpu_req = container.resources.requests['cpu']
                            if cpu_req.endswith('m'):
                                cpu_requests += int(cpu_req[:-1])
                            else:
                                cpu_requests += int(float(cpu_req) * 1000)

                        if container.resources.limits and 'cpu' in container.resources.limits:
                            cpu_lim = container.resources.limits['cpu']
                            if cpu_lim.endswith('m'):
                                cpu_limits += int(cpu_lim[:-1])
                            else:
                                cpu_limits += int(float(cpu_lim) * 1000)

                        # Memory资源
                        if container.resources.requests and 'memory' in container.resources.requests:
                            mem_req = container.resources.requests['memory']
                            if mem_req.endswith('Mi'):
                                memory_requests += int(mem_req[:-2])
                            elif mem_req.endswith('Gi'):
                                memory_requests += int(float(mem_req[:-2]) * 1024)
                            elif mem_req.endswith('Ki'):
                                memory_requests += int(float(mem_req[:-2]) / 1024)
                            else:
                                memory_requests += int(int(mem_req) / 1024 / 1024)

                        if container.resources.limits and 'memory' in container.resources.limits:
                            mem_lim = container.resources.limits['memory']
                            if mem_lim.endswith('Mi'):
                                memory_limits += int(mem_lim[:-2])
                            elif mem_lim.endswith('Gi'):
                                memory_limits += int(float(mem_lim[:-2]) * 1024)
                            elif mem_lim.endswith('Ki'):
                                memory_limits += int(float(mem_lim[:-2]) / 1024)
                            else:
                                memory_limits += int(int(mem_lim) / 1024 / 1024)

            result.append(
                {
                    "env": env,
                    "namespace": sts.metadata.namespace if hasattr(sts.metadata, 'namespace') else namespace,
                    "name": sts.metadata.name,
                    "desired_pods": desired_replicas,
                    "ready_pods": ready_replicas,
                    "cpu_requests": f"{cpu_requests}m" if cpu_requests > 0 else "0m",
                    "cpu_limits": f"{cpu_limits}m" if cpu_limits > 0 else "0m",
                    "memory_requests": f"{memory_requests}Mi" if memory_requests > 0 else "0Mi",
                    "memory_limits": f"{memory_limits}Mi" if memory_limits > 0 else "0Mi",
                    "creation_timestamp": (
                        sts.metadata.creation_timestamp.isoformat() if sts.metadata.creation_timestamp else None
                    ),
                }
            )

        return web.json_response({"success": True, "data": result, "total": len(result)})

    except ApiException as e:
        logger.error(f"查询StatefulSet列表失败: {e}")
        return web.json_response({"error": f"Kubernetes API错误: {e.reason}"}, status=e.status or 500)
    except Exception as e:
        logger.error(f"查询StatefulSet列表异常: {e}")
        return web.json_response({"error": f"服务器内部错误: {str(e)}"}, status=500)


async def get_daemonset_list(apps_v1, request):
    """
    GET接口：查询DaemonSet列表
    参数：
    - env: 集群名称
    - namespace: 命名空间（可选，如果不提供则查询所有命名空间）
    - apps_v1: Kubernetes AppsV1Api客户端
    返回：集群名、命名空间、DaemonSet名称、Pod数、CPU/Memory的requests和limits
    """
    try:
        env = request.query.get('env')
        namespace = request.query.get('namespace')

        if not env:
            return web.json_response({"error": "缺少必要参数: env"}, status=400)

        # 查询DaemonSet列表
        if namespace:
            daemonset_list = await apps_v1.list_namespaced_daemon_set(namespace=namespace)
        else:
            daemonset_list = await apps_v1.list_daemon_set_for_all_namespaces()

        result = []
        for ds in daemonset_list.items:
            # 获取Pod数量
            desired_number_scheduled = ds.status.desired_number_scheduled or 0
            current_number_scheduled = ds.status.current_number_scheduled or 0
            number_ready = ds.status.number_ready or 0

            # 计算资源请求和限制
            cpu_requests, memory_requests = 0, 0
            cpu_limits, memory_limits = 0, 0

            if ds.spec.template.spec.containers:
                for container in ds.spec.template.spec.containers:
                    # CPU资源
                    if container.resources:
                        if container.resources.requests and 'cpu' in container.resources.requests:
                            cpu_req = container.resources.requests['cpu']
                            if cpu_req.endswith('m'):
                                cpu_requests += int(cpu_req[:-1])
                            else:
                                cpu_requests += int(float(cpu_req) * 1000)

                        if container.resources.limits and 'cpu' in container.resources.limits:
                            cpu_lim = container.resources.limits['cpu']
                            if cpu_lim.endswith('m'):
                                cpu_limits += int(cpu_lim[:-1])
                            else:
                                cpu_limits += int(float(cpu_lim) * 1000)

                        # Memory资源
                        if container.resources.requests and 'memory' in container.resources.requests:
                            mem_req = container.resources.requests['memory']
                            if mem_req.endswith('Mi'):
                                memory_requests += int(mem_req[:-2])
                            elif mem_req.endswith('Gi'):
                                memory_requests += int(float(mem_req[:-2]) * 1024)
                            elif mem_req.endswith('Ki'):
                                memory_requests += int(float(mem_req[:-2]) / 1024)
                            else:
                                memory_requests += int(int(mem_req) / 1024 / 1024)

                        if container.resources.limits and 'memory' in container.resources.limits:
                            mem_lim = container.resources.limits['memory']
                            if mem_lim.endswith('Mi'):
                                memory_limits += int(mem_lim[:-2])
                            elif mem_lim.endswith('Gi'):
                                memory_limits += int(float(mem_lim[:-2]) * 1024)
                            elif mem_lim.endswith('Ki'):
                                memory_limits += int(float(mem_lim[:-2]) / 1024)
                            else:
                                memory_limits += int(int(mem_lim) / 1024 / 1024)

            result.append(
                {
                    "env": env,
                    "namespace": ds.metadata.namespace if hasattr(ds.metadata, 'namespace') else namespace,
                    "name": ds.metadata.name,
                    "desired_pods": desired_number_scheduled,
                    "current_pods": current_number_scheduled,
                    "ready_pods": number_ready,
                    "cpu_requests": f"{cpu_requests}m" if cpu_requests > 0 else "0m",
                    "cpu_limits": f"{cpu_limits}m" if cpu_limits > 0 else "0m",
                    "memory_requests": f"{memory_requests}Mi" if memory_requests > 0 else "0Mi",
                    "memory_limits": f"{memory_limits}Mi" if memory_limits > 0 else "0Mi",
                    "creation_timestamp": (
                        ds.metadata.creation_timestamp.isoformat() if ds.metadata.creation_timestamp else None
                    ),
                }
            )

        return web.json_response({"success": True, "data": result, "total": len(result)})

    except ApiException as e:
        logger.error(f"查询DaemonSet列表失败: {e}")
        return web.json_response({"error": f"Kubernetes API错误: {e.reason}"}, status=e.status or 500)
    except Exception as e:
        logger.error(f"查询DaemonSet列表异常: {e}")
        return web.json_response({"error": f"服务器内部错误: {str(e)}"}, status=500)


async def get_pod_metrics(namespace, pod_name, custom_api):
    """获取Pod的资源使用指标"""
    try:
        # 使用metrics API获取Pod指标
        metrics = await custom_api.get_namespaced_custom_object(
            group="metrics.k8s.io", version="v1beta1", namespace=namespace, plural="pods", name=pod_name
        )

        cpu_usage = 0
        memory_usage = 0

        if 'containers' in metrics:
            for container in metrics['containers']:
                if 'usage' in container:
                    usage = container['usage']
                    if 'cpu' in usage:
                        cpu = usage['cpu']
                        if cpu.endswith('n'):
                            cpu_usage += int(cpu[:-1]) // 1000000  # 纳秒转毫秒
                        elif cpu.endswith('u'):
                            cpu_usage += int(cpu[:-1]) // 1000  # 微秒转毫秒
                        elif cpu.endswith('m'):
                            cpu_usage += int(cpu[:-1])  # 毫秒
                        else:
                            cpu_usage += int(cpu) * 1000  # 秒转毫秒

                    if 'memory' in usage:
                        memory = usage['memory']
                        if memory.endswith('Ki'):
                            memory_usage += int(memory[:-2]) // 1024  # KiB转MB
                        elif memory.endswith('Mi'):
                            memory_usage += int(memory[:-2])  # MiB
                        elif memory.endswith('Gi'):
                            memory_usage += int(memory[:-2]) * 1024  # GiB转MB
                        else:
                            memory_usage += int(memory) // 1024 // 1024  # bytes转MB

        return {"cpu": cpu_usage, "memory": memory_usage}
    except Exception as e:
        logger.warning(f"无法获取Pod {pod_name} 的指标: {e}")
        return {"cpu": 0, "memory": 0}


async def get_pod_events(namespace, pod_name, core_v1):
    """获取Pod相关事件"""
    try:
        events = await core_v1.list_namespaced_event(
            namespace=namespace, field_selector=f"involvedObject.name={pod_name}"
        )

        # 获取最新的事件
        if events.items:
            latest_event = max(events.items, key=lambda e: e.metadata.creation_timestamp)
            return latest_event.reason or "", latest_event.message or ""
        return "", ""
    except Exception as e:
        logger.warning(f"无法获取Pod {pod_name} 的事件: {e}")
        return "", ""


async def get_statefulset_pods(request, core_v1, custom_api, apps_v1):
    """获取指定命名空间和StatefulSet下的所有Pod信息（包括被隔离的Pod）"""
    namespace = request.query.get("namespace")
    statefulset_name = request.query.get("statefulset")

    try:
        # 获取指定StatefulSet的标签选择器
        statefulset = await apps_v1.read_namespaced_stateful_set(statefulset_name, namespace)
        selector = statefulset.spec.selector.match_labels
        selector_str = ",".join([f"{k}={v}" for k, v in selector.items()])
        # 1. 通过标签选择器查询相关的Pod
        pods_by_label = await core_v1.list_namespaced_pod(namespace=namespace, label_selector=selector_str)
        lenmline = pods_by_label.items[0].metadata.name.count("-")
        # 2. 通过 ownerReferences 查询所有属于该 statefulset 的 pod（即使 label 被修改也能查到）
        all_pods = await core_v1.list_namespaced_pod(namespace=namespace)
        pods_by_match = []
        for pod in all_pods.items:
            owner_refs = pod.metadata.owner_references or []
            # 智能匹配：如果没有ownerReferences，尝试用pod名称前缀、镜像等特征判断
            if (
                not owner_refs
                and pod.metadata.name.startswith(statefulset_name + '-')
                and pod.metadata.name.count("-") == lenmline
            ):
                pods_by_match.append(pod)
                # 镜像匹配（可根据实际需求扩展更复杂的规则）
                # statefulset_images = [c.image for c in statefulset.spec.template.spec.containers]
                # pod_images = [c.image for c in pod.spec.containers]
                # if any(img in statefulset_images for img in pod_images):

        # 合并 pod，去重
        all_related_pods = {
            pod.metadata.name: pod
            for pod in pods_by_label.items
            if pod.metadata.name.startswith(statefulset_name + '-') and pod.metadata.name.count("-") == lenmline
        }
        for pod in pods_by_match:
            all_related_pods[pod.metadata.name] = pod

        # 构建Pod信息列表
        pod_list = []
        for pod in all_related_pods.values():
            # 获取Pod资源使用情况
            metrics = await get_pod_metrics(namespace, pod.metadata.name, custom_api)

            # 处理created_at为北京时间并格式化
            created_at = None
            if pod.metadata.creation_timestamp:
                utc_time = pod.metadata.creation_timestamp.replace(tzinfo=timezone.utc)
                beijing_time = utc_time.astimezone(timezone(timedelta(hours=8)))
                created_at = beijing_time.strftime("%Y-%m-%d %H:%M:%S")

            # 处理CPU和内存为整数
            cpu = round(metrics["cpu"])
            memory = round(metrics["memory"])

            # 获取Pod详细状态原因
            pod_status_reason = ""

            # 对非Running状态的Pod获取更详细的原因
            if pod.status.phase != "Running":
                # 1. 从Pod状态本身获取原因
                if pod.status.conditions:
                    for cond in pod.status.conditions:
                        if cond.type == "PodScheduled" and cond.status != "True":
                            pod_status_reason = cond.message or cond.reason or ""
                            break
                if not pod_status_reason and pod.status.reason:
                    pod_status_reason = pod.status.reason

                # 2. 对于所有非Running的Pod，从容器状态获取原因
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

                # 3. 如果以上方法都没有获取到原因，尝试从最新事件获取
                if not pod_status_reason:
                    event_reason, event_message = await get_pod_events(namespace, pod.metadata.name, core_v1)
                    if event_message:
                        pod_status_reason = f"{event_reason}: {event_message}" if event_reason else event_message

            # 获取Last Status，只在Pod有重启时才获取
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

            # 获取主容器镜像信息
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
                "exception_reason": pod_status_reason,  # 显示所有非Running状态的原因
            }
            pod_list.append(pod_info)

        return web.json_response(
            {
                "success": True,
                "pods": pod_list,
            }
        )
    except ApiException as e:
        error_message = (
            json.loads(e.body).get("message") if hasattr(e, 'body') and e.body else f"获取Pod信息失败: {str(e)}"
        )
        logger.error(error_message)
        return web.json_response({"message": error_message, "success": False}, status=500)
    except Exception as e:
        error_message = f"获取Pod信息时发生未知错误: {str(e)}"
        logger.exception(error_message)
        return web.json_response({"message": error_message, "success": False}, status=500)


async def get_daemonset_pods(request, core_v1, custom_api, apps_v1):
    """获取指定命名空间和DaemonSet下的所有Pod信息（包括被隔离的Pod）"""
    namespace = request.query.get("namespace")
    daemonset_name = request.query.get("daemonset")

    try:
        # 获取指定DaemonSet的标签选择器
        daemonset = await apps_v1.read_namespaced_daemon_set(daemonset_name, namespace)
        selector = daemonset.spec.selector.match_labels
        selector_str = ",".join([f"{k}={v}" for k, v in selector.items()])
        # 1. 通过标签选择器查询相关的Pod
        pods_by_label = await core_v1.list_namespaced_pod(namespace=namespace, label_selector=selector_str)
        lenmline = pods_by_label.items[0].metadata.name.count("-")
        # 2. 通过 ownerReferences 查询所有属于该 daemonset 的 pod（即使 label 被修改也能查到）
        all_pods = await core_v1.list_namespaced_pod(namespace=namespace)
        pods_by_match = []
        for pod in all_pods.items:
            owner_refs = pod.metadata.owner_references or []
            # 智能匹配：如果没有ownerReferences，尝试用pod名称前缀、镜像等特征判断
            if (
                not owner_refs
                and pod.metadata.name.startswith(daemonset_name + '-')
                and pod.metadata.name.count("-") == lenmline
            ):
                pods_by_match.append(pod)
                # 镜像匹配（可根据实际需求扩展更复杂的规则）
                # daemonset_images = [c.image for c in daemonset.spec.template.spec.containers]
                # pod_images = [c.image for c in pod.spec.containers]
                # if any(img in daemonset_images for img in pod_images):

        # 合并 pod，去重
        all_related_pods = {
            pod.metadata.name: pod
            for pod in pods_by_label.items
            if pod.metadata.name.startswith(daemonset_name + '-') and pod.metadata.name.count("-") == lenmline
        }
        for pod in pods_by_match:
            all_related_pods[pod.metadata.name] = pod
        # 构建Pod信息列表
        pod_list = []
        for pod in all_related_pods.values():
            # 获取Pod资源使用情况
            metrics = await get_pod_metrics(namespace, pod.metadata.name, custom_api)

            # 处理created_at为北京时间并格式化
            created_at = None
            if pod.metadata.creation_timestamp:
                utc_time = pod.metadata.creation_timestamp.replace(tzinfo=timezone.utc)
                beijing_time = utc_time.astimezone(timezone(timedelta(hours=8)))
                created_at = beijing_time.strftime("%Y-%m-%d %H:%M:%S")

            # 处理CPU和内存为整数
            cpu = round(metrics["cpu"])
            memory = round(metrics["memory"])

            # 获取Pod详细状态原因
            pod_status_reason = ""

            # 对非Running状态的Pod获取更详细的原因
            if pod.status.phase != "Running":
                # 1. 从Pod状态本身获取原因
                if pod.status.conditions:
                    for cond in pod.status.conditions:
                        if cond.type == "PodScheduled" and cond.status != "True":
                            pod_status_reason = cond.message or cond.reason or ""
                            break
                if not pod_status_reason and pod.status.reason:
                    pod_status_reason = pod.status.reason

                # 2. 对于所有非Running的Pod，从容器状态获取原因
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

                # 3. 如果以上方法都没有获取到原因，尝试从最新事件获取
                if not pod_status_reason:
                    event_reason, event_message = await get_pod_events(namespace, pod.metadata.name, core_v1)
                    if event_message:
                        pod_status_reason = f"{event_reason}: {event_message}" if event_reason else event_message

            # 获取Last Status，只在Pod有重启时才获取
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

            # 获取主容器镜像信息
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
                "exception_reason": pod_status_reason,  # 显示所有非Running状态的原因
            }
            pod_list.append(pod_info)

        return web.json_response(
            {
                "success": True,
                "pods": pod_list,
            }
        )
    except ApiException as e:
        error_message = (
            json.loads(e.body).get("message") if hasattr(e, 'body') and e.body else f"获取Pod信息失败: {str(e)}"
        )
        logger.error(error_message)
        return web.json_response({"message": error_message, "success": False}, status=500)
    except Exception as e:
        error_message = f"获取Pod信息时发生未知错误: {str(e)}"
        logger.exception(error_message)
        return web.json_response({"message": error_message, "success": False}, status=500)


async def restart_statefulset(request, apps_v1):
    """重启StatefulSet"""
    try:
        data = await request.json()
        namespace = data.get('namespace')
        statefulset_name = data.get('statefulset')

        if not namespace or not statefulset_name:
            return web.json_response({"error": "缺少必要参数: namespace 和 statefulset"}, status=400)

        # 获取当前的StatefulSet
        statefulset = await apps_v1.read_namespaced_stateful_set(statefulset_name, namespace)
        if not statefulset:
            return web.json_response({"error": f"未找到StatefulSet {namespace}/{statefulset_name}"}, status=404)

        # 创建重启补丁
        patch = {
            "spec": {
                "template": {
                    "metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": datetime.now().isoformat()}}
                }
            }
        }

        # 应用补丁重启StatefulSet
        await apps_v1.patch_namespaced_stateful_set(statefulset_name, namespace, patch)

        logger.info(f"重启StatefulSet【{namespace}/{statefulset_name}】")
        return web.json_response(
            {"success": True, "message": f"StatefulSet {namespace}/{statefulset_name} restarted successfully"}
        )

    except ApiException as e:
        error_message = f"重启StatefulSet失败: {e}"
        logger.error(error_message)
        return web.json_response({"message": error_message, "success": False}, status=e.status or 500)
    except Exception as e:
        error_message = f"重启StatefulSet时发生未知错误: {str(e)}"
        logger.exception(error_message)
        return web.json_response({"message": error_message, "success": False}, status=500)


async def scale_statefulset(request, apps_v1):
    """扩缩容StatefulSet"""
    try:
        data = await request.json()
        namespace = data.get('namespace')
        statefulset_name = data.get('statefulset')
        replicas = data.get('replicas')

        if not namespace or not statefulset_name or replicas is None:
            return web.json_response({"error": "缺少必要参数: namespace, statefulset 和 replicas"}, status=400)

        if not isinstance(replicas, int) or replicas < 0:
            return web.json_response({"error": "replicas必须是非负整数"}, status=400)

        # 获取当前的StatefulSet
        statefulset = await apps_v1.read_namespaced_stateful_set(statefulset_name, namespace)
        if not statefulset:
            return web.json_response({"error": f"未找到StatefulSet {namespace}/{statefulset_name}"}, status=404)

        # 更新副本数
        statefulset.spec.replicas = replicas

        # 应用更新
        await apps_v1.patch_namespaced_stateful_set_scale(statefulset_name, namespace, statefulset)

        logger.info(f"StatefulSet【{namespace}/{statefulset_name}】副本数更改为 {replicas}")
        return web.json_response(
            {"success": True, "message": f"StatefulSet {namespace}/{statefulset_name} scaled to {replicas} replicas"}
        )

    except ApiException as e:
        error_message = f"扩缩容StatefulSet失败: {e}"
        logger.error(error_message)
        return web.json_response({"message": error_message, "success": False}, status=e.status or 500)
    except Exception as e:
        error_message = f"扩缩容StatefulSet时发生未知错误: {str(e)}"
        logger.exception(error_message)
        return web.json_response({"message": error_message, "success": False}, status=500)


async def restart_daemonset(request, apps_v1):
    """重启DaemonSet"""
    try:
        data = await request.json()
        namespace = data.get('namespace')
        daemonset_name = data.get('daemonset')

        if not namespace or not daemonset_name:
            return web.json_response({"error": "缺少必要参数: namespace 和 daemonset"}, status=400)

        # 获取当前的DaemonSet
        daemonset = await apps_v1.read_namespaced_daemon_set(daemonset_name, namespace)
        if not daemonset:
            return web.json_response({"error": f"未找到DaemonSet {namespace}/{daemonset_name}"}, status=404)

        # 创建重启补丁
        patch = {
            "spec": {
                "template": {
                    "metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": datetime.now().isoformat()}}
                }
            }
        }

        # 应用补丁重启DaemonSet
        await apps_v1.patch_namespaced_daemon_set(daemonset_name, namespace, patch)

        logger.info(f"重启DaemonSet【{namespace}/{daemonset_name}】")
        return web.json_response(
            {"success": True, "message": f"DaemonSet {namespace}/{daemonset_name} restarted successfully"}
        )

    except ApiException as e:
        error_message = f"重启DaemonSet失败: {e}"
        logger.error(error_message)
        return web.json_response({"message": error_message, "success": False}, status=e.status or 500)
    except Exception as e:
        error_message = f"重启DaemonSet时发生未知错误: {str(e)}"
        logger.exception(error_message)
        return web.json_response({"message": error_message, "success": False}, status=500)
