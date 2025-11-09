import asyncio
import base64
import json
import sys
from urllib.parse import urlencode
from aiohttp import ClientSession, ClientWebSocketResponse, WSMsgType, web
from kubernetes_asyncio import client, config
from kubernetes_asyncio.client.rest import ApiException
from datetime import datetime, timedelta, timezone
from loguru import logger
import utils
import configmap_manager
import service_manager
import ingress_manager
import pod_manager
import istio_manager
import re
from deployment_monitor import DeploymentMonitor
from k8s_event_monitor import K8sEventMonitor
from event_monitor_config import *
from k8s_node_scheduler import K8sNodeScheduler
from k8s_client_manager import K8sClientManager
from node_manager import get_nodes_list, cordon_nodes, uncordon_nodes
import k8s_resource_handler
import stateful_daemon_manager

# 配置日志
logger.remove()
logger.add(
    sys.stderr,
    format='<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> [<level>{level}</level>] <level>{message}</level>',
    level='INFO',
)

VERSION = utils.get_version()
# 全局变量
ws_conn = None
v1 = None  # AppsV1Api
batch_v1 = None  # BatchV1Api
core_v1 = None  # CoreV1Api
networking_v1 = None  # NetworkingV1Api
admission_api = None  # AdmissionregistrationV1Api
custom_api = None  # CustomObjectsApi（用于访问Metrics API）
deployment_monitor = None  # DeploymentMonitor实例
event_monitor = None  # K8sEventMonitor实例
# 用于存储 WebSocket 请求的 Future
request_futures = {}
# 存储Pod日志流任务
pod_logs_tasks = {}


def init_kubernetes():
    """在程序启动时加载 Kubernetes 配置并初始化客户端"""
    global v1, batch_v1, core_v1, networking_v1, admission_api, custom_api, deployment_monitor, event_monitor
    try:
        config.load_incluster_config()
        v1 = client.AppsV1Api()
        batch_v1 = client.BatchV1Api()
        core_v1 = client.CoreV1Api()
        networking_v1 = client.NetworkingV1Api()
        admission_api = client.AdmissionregistrationV1Api()
        custom_api = client.CustomObjectsApi()
        deployment_monitor = DeploymentMonitor(v1, core_v1)
        event_monitor = K8sEventMonitor(core_v1)
        logger.info("Kubernetes 配置加载成功")
    except Exception as e:
        logger.error(f"加载 Kubernetes 配置失败: {e}")
        raise


async def handle_http_request(
    ws: ClientWebSocketResponse, request_id: str, method: str, query: dict, body: dict, path: str
):
    """异步处理 HTTP 请求并发送响应"""
    try:
        async with ClientSession() as session:
            logger.info(f"转发请求: {method} {path}?{urlencode(query)}【{json.dumps(body)}】")
            if method == "GET":
                async with session.get(path, params=query, ssl=False) as resp:
                    response_data = await resp.json()
            elif method == "POST":
                async with session.post(path, params=query, json=body, ssl=False) as resp:
                    response_data = await resp.json()
            elif method == "DELETE":
                async with session.delete(path, params=query, json=body, ssl=False) as resp:
                    response_data = await resp.json()
            else:
                response_data = {"success": False, "error": f"agent收到master发来的不支持的请求方法: {method}"}
                logger.error(response_data["error"])
    except Exception as e:
        response_data = {"success": False, "error": str(e)}
        logger.error(response_data["error"])

    await ws.send_json({"type": "response", "request_id": request_id, "response": response_data})


async def stream_pod_logs(
    ws: ClientWebSocketResponse, connection_id: str, namespace: str, pod_name: str, container: str = ""
):
    """流式获取Pod日志并发送给master"""
    try:
        logger.info(f"开始获取Pod日志: {namespace}/{pod_name}")

        # 发送连接成功消息
        await ws.send_json({"type": "pod_logs", "connection_id": connection_id, "status": "connected"})

        # 使用kubernetes_asyncio的日志流API
        log_stream = await core_v1.read_namespaced_pod_log(
            name=pod_name,
            namespace=namespace,
            container=container if container else None,
            follow=True,
            tail_lines=100,
            timestamps=False,
            _preload_content=False,
        )

        # 流式读取日志
        buffer = ""
        async for chunk in log_stream.content:
            if chunk:
                try:
                    buffer += chunk.decode('utf-8', errors='ignore')
                    lines = buffer.split('\n')
                    buffer = lines[-1]  # 保留最后一行（可能不完整）

                    for line in lines[:-1]:
                        if line.strip():
                            # 直接发送纯净的日志内容，不包装成JSON
                            await ws.send_str(line)
                except Exception as decode_error:
                    logger.warning(f"解码日志行失败: {decode_error}")
                    continue

    except asyncio.CancelledError:
        logger.info(f"Pod日志流被取消: {connection_id}")
        await ws.send_json({"type": "pod_logs", "connection_id": connection_id, "status": "disconnected"})
    except ApiException as e:
        error_msg = f"Kubernetes API错误: {e.status} - {e.reason}"
        logger.error(f"Pod日志流API异常: {connection_id}, 错误: {error_msg}")
        await ws.send_json({"type": "pod_logs", "connection_id": connection_id, "error": error_msg})
    except Exception as e:
        logger.error(f"Pod日志流异常: {connection_id}, 错误: {e}")
        await ws.send_json({"type": "pod_logs", "connection_id": connection_id, "error": str(e)})
    finally:
        # 清理任务
        if connection_id in pod_logs_tasks:
            del pod_logs_tasks[connection_id]


async def process_request(ws: ClientWebSocketResponse):
    """处理服务端发送的请求"""
    async for msg in ws:
        if msg.type == WSMsgType.TEXT:
            try:
                data = json.loads(msg.data)
            except json.JSONDecodeError:
                logger.error(f"收到无法解析的消息：{msg.data}")
                continue
            if data.get("type") == "admis":
                request_id = data.get("request_id")
                deploy_res = data.get("deploy_res")
                logger.info(f"收到 admis 消息：{request_id} {deploy_res}")
                if request_id in request_futures:
                    request_futures[request_id].set_result(deploy_res)
                    del request_futures[request_id]
            elif data.get("type") == "request":
                request_id = data["request_id"]
                method = data["method"]
                query = data["query"]
                body = data["body"]
                path = (
                    'http://127.0.0.1:81' + data["path"]
                    if data["path"].startswith('/api/pod/')
                    else 'https://127.0.0.1' + data["path"]
                )
                asyncio.create_task(handle_http_request(ws, request_id, method, query, body, path))
            elif data.get("type") == "start_pod_logs":
                # 开始Pod日志流
                connection_id = data.get("connection_id")
                namespace = data.get("namespace")
                pod_name = data.get("pod_name")
                container = data.get("container", "")
                logger.info(f"开始Pod日志流: {connection_id}")
                task = asyncio.create_task(stream_pod_logs(ws, connection_id, namespace, pod_name, container))
                pod_logs_tasks[connection_id] = task
            elif data.get("type") == "stop_pod_logs":
                # 停止Pod日志流
                connection_id = data.get("connection_id")
                logger.info(f"停止Pod日志流: {connection_id}")
                if connection_id in pod_logs_tasks:
                    pod_logs_tasks[connection_id].cancel()
                    del pod_logs_tasks[connection_id]
        elif msg.type == WSMsgType.ERROR:
            logger.error(f"WebSocket 错误：{msg.data}")


async def heartbeat(ws: ClientWebSocketResponse):
    """定期发送心跳"""
    while True:
        try:
            await ws.send_json({"type": "heartbeat"})
            logger.debug("成功发送心跳")
            await asyncio.sleep(HEARTBEAT_INTERVAL)
        except Exception as e:
            logger.error(f"心跳发送失败：{e}")
            break


async def monitor_health_check():
    """定期健康检查，监控事件传输状态"""
    last_check_time = datetime.now()
    while True:
        try:
            await asyncio.sleep(HEALTH_CHECK_INTERVAL)  # 健康检查间隔

            current_time = datetime.now()

            # 检查WebSocket连接健康状态
            if not event_monitor.is_websocket_healthy():
                logger.warning("⚠️ 健康检查: WebSocket连接不健康")
                raise Exception("WebSocket连接不健康")

            # 检查事件监控状态
            if not event_monitor.is_running:
                logger.warning("⚠️ 健康检查: 事件监控未运行")
                raise Exception("事件监控未运行")

            # 检查是否长时间没有事件（可能表示K8s事件流断开）
            if event_monitor.last_event_time:
                time_since_last_event = current_time - event_monitor.last_event_time
                if time_since_last_event.total_seconds() > EVENT_TIMEOUT_THRESHOLD:
                    logger.warning(f"⚠️ 健康检查: 已有 {time_since_last_event.total_seconds():.0f} 秒没有收到K8s事件")

            # 定期输出统计信息
            time_since_last_check = current_time - last_check_time
            if time_since_last_check.total_seconds() > STATS_REPORT_INTERVAL:
                logger.debug(
                    f"📊 事件监控状态: 已处理 {event_monitor.event_count} 个事件, WebSocket健康: {event_monitor.is_websocket_healthy()}"
                )
                last_check_time = current_time

        except Exception as e:
            logger.error(f"健康检查失败：{e}")
            break


async def connect_to_server():
    """连接到 WebSocket 服务端，并处理连接断开的情况"""
    uri = f"{utils.KUBEDOOR_MASTER}/ws?env={utils.PROM_K8S_TAG_VALUE}&ver={VERSION}"
    while True:
        try:
            async with ClientSession() as session:
                async with session.ws_connect(uri) as ws:
                    logger.info("成功连接到服务端")
                    global ws_conn
                    ws_conn = ws

                    # 设置事件监听器的WebSocket连接
                    event_monitor.set_websocket_connection(ws)

                    # 并发运行请求处理、心跳发送、事件监听和健康检查，使用return_when=FIRST_EXCEPTION
                    # 这样任何一个任务异常都会导致重新连接，而不是整个程序崩溃
                    try:
                        done, pending = await asyncio.wait(
                            [
                                asyncio.create_task(process_request(ws)),
                                asyncio.create_task(heartbeat(ws)),
                                asyncio.create_task(event_monitor.start_monitoring()),
                                asyncio.create_task(monitor_health_check()),
                            ],
                            return_when=asyncio.FIRST_EXCEPTION,
                        )

                        # 取消所有待处理的任务
                        for task in pending:
                            task.cancel()
                            try:
                                await task
                            except asyncio.CancelledError:
                                pass

                        # 检查已完成的任务是否有异常
                        for task in done:
                            if task.exception():
                                logger.error(f"任务异常: {task.exception()}")
                                raise task.exception()

                    except Exception as task_e:
                        logger.error(f"WebSocket任务异常: {task_e}")
                        # 停止事件监控
                        await event_monitor.stop_monitoring()
                        # 清空WebSocket连接引用
                        event_monitor.set_websocket_connection(None)
                        ws_conn = None
                        raise task_e

        except Exception as e:
            logger.error(f"连接到服务端失败：{e}")
            # 确保清理资源
            if event_monitor:
                await event_monitor.stop_monitoring()
                event_monitor.set_websocket_connection(None)
            ws_conn = None
            logger.info(f"等待 {WEBSOCKET_RECONNECT_DELAY} 秒后重新连接...")
            await asyncio.sleep(WEBSOCKET_RECONNECT_DELAY)


async def delete_cronjob_or_not(cronjob_name, job_type):
    """判断是否是一次性 job，是的话删除"""
    if job_type == "once":
        try:
            await batch_v1.delete_namespaced_cron_job(
                name=cronjob_name, namespace="kubedoor", body=client.V1DeleteOptions()
            )
            logger.info(f"CronJob '{cronjob_name}' deleted successfully.")
        except ApiException as e:
            logger.exception(f"删除 CronJob '{cronjob_name}' 时出错: {e}")
            utils.send_msg(f"Error when deleting CronJob '【{utils.PROM_K8S_TAG_VALUE}】{cronjob_name}'!")


async def health_check(request):
    return web.json_response({"ver": VERSION, "status": "healthy"})


async def update_image(request):
    try:
        data = await request.json()
        new_image_tag = data.get('image_tag')
        deployment_name = data.get('deployment')
        namespace = data.get('namespace')

        deployment = await v1.read_namespaced_deployment(deployment_name, namespace)
        current_image = deployment.spec.template.spec.containers[0].image
        image_name = current_image.split(':')[0]
        new_image = f"{image_name}:{new_image_tag}"
        deployment.spec.template.spec.containers[0].image = new_image

        await v1.patch_namespaced_deployment(name=deployment_name, namespace=namespace, body=deployment)

        # 启动后台监控任务
        if deployment_monitor:
            asyncio.create_task(deployment_monitor.monitor_deployment_update(namespace, deployment_name, new_image))

        return web.json_response(
            {
                "success": True,
                "message": f"{namespace} {deployment_name} updated with image {new_image}",
            }
        )
    except Exception as e:
        return web.json_response({"message": str(e)}, status=500)


async def scale(request):
    """批量扩缩容"""
    request_info = await request.json()
    interval = request.query.get("interval")
    add_label = request.query.get("add_label")
    res_type = request.query.get("type")
    temp = request.query.get("temp")
    isolate = request.query.get("isolate")
    scheduler = request.query.get("scheduler", "false")
    error_list = []

    for index, deployment in enumerate(request_info.get('deployment_list', [])):
        namespace = deployment.get("namespace")
        deployment_name = deployment.get("deployment_name")
        num = deployment.get("num")
        job_name = deployment.get("job_name")
        job_type = deployment.get("job_type")
        logger.info(f"【{namespace}】【{deployment_name}】: {num}")
        nodes = await core_v1.list_node()

        try:
            deployment_obj = await v1.read_namespaced_deployment(deployment_name, namespace)
            if not deployment_obj:
                reason = f"未找到【{namespace}】【{deployment_name}】"
                logger.error(reason)
                error_list.append({'namespace': namespace, 'deployment_name': deployment_name, 'reason': reason})
                continue

            # 判断扩容还是缩容
            current_replicas = deployment_obj.spec.replicas
            logger.info(f"当前副本数: {current_replicas}")

            # 如果是临时扩缩容，添加annotations
            if temp == 'true':
                if deployment_obj.metadata.annotations is None:
                    deployment_obj.metadata.annotations = {}
                current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                deployment_obj.metadata.annotations['scale.temp'] = f"{current_time}@{current_replicas}-->{num}"
                logger.info(f"添加临时扩缩容标记: {current_time}@{current_replicas}-->{num}")
            else:
                # 非临时扩缩容，删除临时标记
                del_scale_temp = 0
                if deployment_obj.metadata.annotations and 'scale.temp' in deployment_obj.metadata.annotations:
                    # 使用patch body方式删除注解，设置为None告诉Kubernetes API删除该key
                    deployment_obj.metadata.annotations['scale.temp'] = None
                    del_scale_temp = 1
                    logger.info("删除临时扩缩容标记")

            if num > current_replicas and add_label == 'true':
                # 副本数不能超过节点总数
                if len(nodes.items) < num:
                    return web.json_response(
                        {"message": f"【{namespace}】【{deployment_name}】副本数不能超过节点总数"},
                        status=500,
                    )
                node_cpu_list = request_info[0].get("node_cpu_list")  # kubedoor-master scale接口追加的变量
                logger.info(f"节点{res_type}情况: {node_cpu_list}")
                logger.info(f"扩缩容策略：根据【节点{res_type}】情况，执行扩容，目标副本数: {num}")
                # 判断已有标签数
                labeled_nodes_count = await get_labeled_nodes_count(namespace, deployment_name, nodes)
                if isolate == 'true':
                    add_isolate = 1
                else:
                    add_isolate = 0
                if labeled_nodes_count < num + add_isolate:
                    nodes_to_label_count = num + add_isolate - labeled_nodes_count
                    # 选择当前 CPU 使用率最低的节点，直到满足扩容后的副本数
                    available_nodes = await select_least_loaded_nodes(
                        namespace, nodes_to_label_count, deployment_name, node_cpu_list
                    )

                    if available_nodes:
                        # 为每个节点添加标签
                        for node in available_nodes:
                            await update_node_with_label(namespace, node, deployment_name)
                    else:
                        reason = "剩余可调度节点不足"
                        logger.error(reason)
                        error_list.append(
                            {
                                'namespace': namespace,
                                'deployment_name': deployment_name,
                                'reason': reason,
                            }
                        )
                        return web.json_response(
                            {"message": f"【{namespace}】【{deployment_name}】剩余可调度节点不足"},
                            status=500,
                        )
                else:
                    logger.info(f"已有{labeled_nodes_count}个节点有标签，无需再打标签")

            elif num < current_replicas and add_label == 'true':
                node_cpu_list = request_info[0].get("node_cpu_list")  # kubedoor-master scale接口追加的变量
                logger.info(f"节点CPU情况: {node_cpu_list}")
                logger.info(f"执行缩容，目标副本数: {num}")
                del_label_count = current_replicas - num
                # 选择当前 CPU 使用率最高的节点，直到满足缩容后的副本数
                available_nodes = await select_del_label_nodes(
                    namespace, del_label_count, deployment_name, node_cpu_list
                )
                for node in available_nodes:
                    await del_node_with_label(namespace, node, deployment_name)
                # 删除available_nodes中的pod
                await delete_pods_in_available_nodes(namespace, deployment_name, available_nodes)
                # 删除pod后等待一段时间，让deployment控制器完成pod重建
                logger.info("等待deployment控制器完成pod重建...")
                await asyncio.sleep(2)

            elif num == current_replicas:
                logger.info(f"副本数没有变化，无需操作")
            else:
                logger.info(f"普通模式扩缩容")

            # 重试机制处理409冲突
            max_retries = 3
            retry_count = 0
            if scheduler == 'true' and add_label == 'true':
                return web.json_response({"message": "add_label和scheduler参数不能同时为True"}, status=400)

            # 如果启用scheduler，解析body获取node_scheduler列表
            if scheduler == 'true' and not job_name:
                try:
                    logger.info(
                        f"开始处理scheduler参数，env={utils.PROM_K8S_TAG_VALUE}, ns={namespace}, deployment={deployment_name}"
                    )

                    node_scheduler_list = request_info.get('node_scheduler', [])
                    logger.info(f"获取到node_scheduler列表: {node_scheduler_list}")

                    # 使用客户端管理器确保客户端正确关闭
                    async with K8sClientManager() as k8s_manager:
                        logger.info(f"成功获取K8s客户端: {type(k8s_manager.core_v1_api)}")

                        # 初始化K8s节点调度器
                        logger.info("正在初始化K8s节点调度器...")
                        k8s_scheduler = K8sNodeScheduler(k8s_manager.core_v1_api)
                        logger.info(f"成功初始化K8s节点调度器: {type(k8s_scheduler)}")

                        # 执行禁止调度操作
                        logger.info(f"开始执行禁止调度操作，排除节点: {node_scheduler_list}")
                        cordon_result = await k8s_scheduler.cordon_nodes_exclude(exclude_nodes=node_scheduler_list)
                        logger.info(f"禁止调度操作完成: {cordon_result}")

                        # 检查 cordon 操作是否有错误
                        if cordon_result.get("error_count", 0) > 0:
                            error_details = []
                            for result in cordon_result.get("results", []):
                                if result.get("status") == "error":
                                    error_details.append(f"节点 {result.get('node_name')}: {result.get('message')}")

                            error_message = f"禁止节点调度操作失败，错误详情: {'; '.join(error_details)}"
                            logger.error(error_message)

                            # 执行恢复操作：取消所有节点的禁止调度状态
                            try:
                                logger.warning("⚠️ cordon操作失败，开始执行uncordon恢复操作以确保节点状态一致性...")
                                uncordon_result = await k8s_scheduler.uncordon_nodes_exclude(
                                    exclude_nodes=node_scheduler_list
                                )
                                logger.info(f"uncordon恢复操作完成: {uncordon_result}")

                                if uncordon_result.get("error_count", 0) > 0:
                                    logger.error(f"⚠️ uncordon恢复操作也出现错误: {uncordon_result}")
                                    error_message += (
                                        f"；恢复操作也失败: {uncordon_result.get('error_count', 0)}个节点恢复失败"
                                    )
                                else:
                                    logger.info("✅ uncordon恢复操作成功，所有节点调度状态已恢复")
                                    error_message += "；已执行恢复操作确保节点状态一致性"

                            except Exception as uncordon_e:
                                logger.error(
                                    f"❌ 执行uncordon恢复操作时发生异常: {type(uncordon_e).__name__}: {str(uncordon_e)}"
                                )
                                error_message += f"；恢复操作异常: {str(uncordon_e)}"

                            return web.json_response({"message": error_message}, status=500)

                except Exception as e:
                    logger.error(f"处理scheduler参数时发生异常: {type(e).__name__}: {str(e)}")
                    import traceback

                    logger.error(f"异常堆栈: {traceback.format_exc()}")

                    # 在异常情况下也执行恢复操作
                    if (
                        'node_scheduler_list' in locals()
                        and node_scheduler_list
                        and 'k8s_scheduler' in locals()
                        and k8s_scheduler
                    ):
                        try:
                            logger.warning(
                                "⚠️ 处理scheduler参数时发生异常，开始执行uncordon恢复操作以确保节点状态一致性..."
                            )
                            uncordon_result = await k8s_scheduler.uncordon_nodes_exclude(
                                exclude_nodes=node_scheduler_list
                            )
                            logger.info(f"异常情况下的uncordon恢复操作完成: {uncordon_result}")

                            if uncordon_result.get("error_count", 0) > 0:
                                logger.error(f"⚠️ 异常情况下的uncordon恢复操作也出现错误: {uncordon_result}")
                            else:
                                logger.info("✅ 异常情况下的uncordon恢复操作成功，所有节点调度状态已恢复")

                        except Exception as uncordon_e:
                            logger.error(
                                f"❌ 异常情况下执行uncordon恢复操作时发生异常: {type(uncordon_e).__name__}: {str(uncordon_e)}"
                            )

                    return web.json_response({"message": f"处理scheduler参数失败: {str(e)}"}, status=500)

            while retry_count < max_retries:
                try:
                    # 重新获取deployment对象，避免resourceVersion冲突
                    if num < current_replicas and add_label == 'true':
                        deployment_obj = await v1.read_namespaced_deployment(deployment_name, namespace)
                    deployment_obj.spec.replicas = num
                    logger.info(
                        f"Deployment【{deployment_name}】副本数更改为 {num}，如已接入准入控制, 实际变更已数据库中数据为准。"
                    )

                    # 如果是临时扩缩容，需要使用完整的patch方法来保存annotations
                    if temp == 'true' or del_scale_temp == 1:
                        await v1.patch_namespaced_deployment(deployment_name, namespace, deployment_obj)
                    else:
                        await v1.patch_namespaced_deployment_scale(deployment_name, namespace, deployment_obj)
                    break  # 成功则跳出重试循环
                except ApiException as patch_e:
                    if patch_e.status == 409 and retry_count < max_retries - 1:
                        retry_count += 1
                        logger.warning(f"遇到409冲突，第{retry_count}次重试...")
                        await asyncio.sleep(1)  # 等待1秒后重试
                    else:
                        raise patch_e  # 非409错误或重试次数用完，抛出异常

            if interval and index != len(request_info) - 1:
                logger.info(f"暂停 {interval}s...")
                await asyncio.sleep(int(interval))

            utils.send_msg(
                f"'【{utils.PROM_K8S_TAG_VALUE}】【{namespace}】【{deployment_name}】' has been scaled! {current_replicas} --> {num}"
            )

            if job_name:
                await delete_cronjob_or_not(job_name, job_type)

            # 如果启用了scheduler，在标签修改完成后执行取消禁止调度操作（延迟10秒）
            if scheduler == 'true' and not job_name:
                try:
                    logger.info(f"标签修改完成，准备执行取消禁止调度操作，排除节点: {node_scheduler_list}")
                    logger.info("正在调用uncordon_nodes_exclude方法...")

                    # 定义错误回调函数
                    def uncordon_error_callback(error_message):
                        logger.error(f"取消禁止调度操作失败通知: {error_message}")
                        utils.send_msg(
                            f"⚠️ 取消禁止调度操作失败: {error_message}\n\n{utils.PROM_K8S_TAG_VALUE}, {namespace}, {deployment_name}"
                        )

                    # 为 uncordon 操作创建新的客户端管理器
                    async with K8sClientManager() as uncordon_k8s_manager:
                        uncordon_scheduler = K8sNodeScheduler(uncordon_k8s_manager.core_v1_api)
                        uncordon_result = await uncordon_scheduler.uncordon_nodes_exclude(
                            exclude_nodes=node_scheduler_list, delay_seconds=10, error_callback=uncordon_error_callback
                        )
                        logger.info(f"取消禁止调度操作已安排: {uncordon_result}")
                except Exception as e:
                    logger.error(f"执行取消禁止调度操作失败: {type(e).__name__}: {str(e)}")
                    import traceback

                    logger.error(f"uncordon异常堆栈: {traceback.format_exc()}")
                    # 不影响主流程，只记录错误

        except ApiException as e:
            logger.exception(f"调用 AppsV1Api 时出错: {e}")
            try:
                reason = json.loads(e.body).get("message", str(e))
            except:
                reason = str(e)
            error_list.append({'namespace': namespace, 'deployment_name': deployment_name, 'reason': reason})

    if error_list:
        return web.json_response({"message": f"以下服务未扩缩容成功{error_list}", "success": False})
    else:
        return web.json_response({"message": "ok", "success": True})


async def reboot(request):
    """批量重启微服务"""
    request_info = await request.json()
    interval = request.query.get("interval")
    scheduler = request.query.get("scheduler", "false")
    patch = {
        "spec": {
            "template": {"metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": datetime.now().isoformat()}}}
        }
    }
    error_list = []

    for index, deployment in enumerate(request_info.get('deployment_list', [])):
        namespace = deployment.get("namespace")
        deployment_name = deployment.get("deployment_name")
        job_name = deployment.get("job_name")
        job_type = deployment.get("job_type")
        logger.info(f"【{namespace}】【{deployment_name}】")

        try:
            deployment_obj = await v1.read_namespaced_deployment(deployment_name, namespace)
            if not deployment_obj:
                reason = f"未找到【{namespace}】【{deployment_name}】"
                logger.error(reason)
                error_list.append({'namespace': namespace, 'deployment_name': deployment_name, 'reason': reason})
                continue

            # 如果启用scheduler，解析body获取node_scheduler列表
            if scheduler == 'true' and not job_name:
                try:
                    logger.info(
                        f"开始处理scheduler参数，env={utils.PROM_K8S_TAG_VALUE}, ns={namespace}, deployment={deployment_name}"
                    )

                    node_scheduler_list = request_info.get('node_scheduler', [])
                    logger.info(f"获取到node_scheduler列表: {node_scheduler_list}")

                    # 使用客户端管理器确保客户端正确关闭
                    async with K8sClientManager() as k8s_manager:
                        logger.info(f"成功获取K8s客户端: {type(k8s_manager.core_v1_api)}")

                        # 初始化K8s节点调度器
                        logger.info("正在初始化K8s节点调度器...")
                        k8s_scheduler = K8sNodeScheduler(k8s_manager.core_v1_api)
                        logger.info(f"成功初始化K8s节点调度器: {type(k8s_scheduler)}")

                        # 执行禁止调度操作
                        logger.info(f"开始执行禁止调度操作，排除节点: {node_scheduler_list}")
                        cordon_result = await k8s_scheduler.cordon_nodes_exclude(exclude_nodes=node_scheduler_list)
                        logger.info(f"禁止调度操作完成: {cordon_result}")

                        # 检查 cordon 操作是否有错误
                        if cordon_result.get("error_count", 0) > 0:
                            error_details = []
                            for result in cordon_result.get("results", []):
                                if result.get("status") == "error":
                                    error_details.append(f"节点 {result.get('node_name')}: {result.get('message')}")

                            error_message = f"禁止节点调度操作失败，错误详情: {'; '.join(error_details)}"
                            logger.error(error_message)

                            # 执行恢复操作：取消所有节点的禁止调度状态
                            try:
                                logger.warning("⚠️ cordon操作失败，开始执行uncordon恢复操作以确保节点状态一致性...")
                                uncordon_result = await k8s_scheduler.uncordon_nodes_exclude(
                                    exclude_nodes=node_scheduler_list
                                )
                                logger.info(f"uncordon恢复操作完成: {uncordon_result}")

                                if uncordon_result.get("error_count", 0) > 0:
                                    logger.error(f"⚠️ uncordon恢复操作也出现错误: {uncordon_result}")
                                    error_message += (
                                        f"；恢复操作也失败: {uncordon_result.get('error_count', 0)}个节点恢复失败"
                                    )
                                else:
                                    logger.info("✅ uncordon恢复操作成功，所有节点调度状态已恢复")
                                    error_message += "；已执行恢复操作确保节点状态一致性"

                            except Exception as uncordon_e:
                                logger.error(
                                    f"❌ 执行uncordon恢复操作时发生异常: {type(uncordon_e).__name__}: {str(uncordon_e)}"
                                )
                                error_message += f"；恢复操作异常: {str(uncordon_e)}"

                            return web.json_response({"message": error_message}, status=500)

                except Exception as e:
                    logger.error(f"处理scheduler参数时发生异常: {type(e).__name__}: {str(e)}")
                    import traceback

                    logger.error(f"异常堆栈: {traceback.format_exc()}")

                    # 在异常情况下也执行恢复操作
                    if (
                        'node_scheduler_list' in locals()
                        and node_scheduler_list
                        and 'k8s_scheduler' in locals()
                        and k8s_scheduler
                    ):
                        try:
                            logger.warning(
                                "⚠️ 处理scheduler参数时发生异常，开始执行uncordon恢复操作以确保节点状态一致性..."
                            )
                            uncordon_result = await k8s_scheduler.uncordon_nodes_exclude(
                                exclude_nodes=node_scheduler_list
                            )
                            logger.info(f"异常情况下的uncordon恢复操作完成: {uncordon_result}")

                            if uncordon_result.get("error_count", 0) > 0:
                                logger.error(f"⚠️ 异常情况下的uncordon恢复操作也出现错误: {uncordon_result}")
                            else:
                                logger.info("✅ 异常情况下的uncordon恢复操作成功，所有节点调度状态已恢复")

                        except Exception as uncordon_e:
                            logger.error(
                                f"❌ 异常情况下执行uncordon恢复操作时发生异常: {type(uncordon_e).__name__}: {str(uncordon_e)}"
                            )

                    return web.json_response({"message": f"处理scheduler参数失败: {str(e)}"}, status=500)

            logger.info(f"重启 Deployment【{deployment_name}】，如已接入准入控制, 实际变更已数据库中数据为准。")
            await v1.patch_namespaced_deployment(deployment_name, namespace, patch)

            if interval and index != len(request_info) - 1:
                logger.info(f"暂停 {interval}s...")
                await asyncio.sleep(int(interval))

            utils.send_msg(f"'【{utils.PROM_K8S_TAG_VALUE}】【{namespace}】【{deployment_name}】' has been restarted!")

            if job_name:
                await delete_cronjob_or_not(job_name, job_type)

            # 如果启用了scheduler，在标签修改完成后执行取消禁止调度操作（延迟10秒）
            if scheduler == 'true' and not job_name:
                try:
                    logger.info(f"标签修改完成，准备执行取消禁止调度操作，排除节点: {node_scheduler_list}")
                    logger.info("正在调用uncordon_nodes_exclude方法...")

                    # 定义错误回调函数
                    def uncordon_error_callback(error_message):
                        logger.error(f"取消禁止调度操作失败通知: {error_message}")
                        utils.send_msg(
                            f"⚠️ 取消禁止调度操作失败: {error_message}\n\n{utils.PROM_K8S_TAG_VALUE}, {namespace}, {deployment_name}"
                        )

                    # 为 uncordon 操作创建新的客户端管理器
                    async with K8sClientManager() as uncordon_k8s_manager:
                        uncordon_scheduler = K8sNodeScheduler(uncordon_k8s_manager.core_v1_api)
                        uncordon_result = await uncordon_scheduler.uncordon_nodes_exclude(
                            exclude_nodes=node_scheduler_list, delay_seconds=120, error_callback=uncordon_error_callback
                        )
                        logger.info(f"取消禁止调度操作已安排: {uncordon_result}")
                except Exception as e:
                    logger.error(f"执行取消禁止调度操作失败: {type(e).__name__}: {str(e)}")
                    import traceback

                    logger.error(f"uncordon异常堆栈: {traceback.format_exc()}")
                    # 不影响主流程，只记录错误

        except ApiException as e:
            logger.exception(f"调用 AppsV1Api 时出错: {e}")
            try:
                reason = json.loads(e.body).get("message", str(e))
            except:
                reason = str(e)
            error_list.append({'namespace': namespace, 'deployment_name': deployment_name, 'reason': reason})

    return web.json_response({"message": "ok", "error_list": error_list})


async def cron(request):
    """创建定时任务，执行扩缩容或重启"""
    request_info = await request.json()
    cron_expr = request_info.get("cron")
    time_expr = request_info.get("time")
    type_expr = request_info.get("type")
    service = request_info.get("service")
    add_label = request.query.get("add_label")

    deployment_name = service[0].get("deployment_name")
    name_pre = f"{type_expr}-{'once' if time_expr else 'cron'}-{deployment_name}"
    job_type = "once" if time_expr else "cron"
    cron_new = f"{time_expr[4]} {time_expr[3]} {time_expr[2]} {time_expr[1]} *" if time_expr else cron_expr
    service[0]["job_name"] = name_pre
    service[0]["job_type"] = job_type

    if add_label:
        url = f"https://kubedoor-agent.kubedoor/api/{type_expr}?add_label={add_label}"
    else:
        url = f"https://kubedoor-agent.kubedoor/api/{type_expr}"

    cronjob = client.V1CronJob(
        metadata=client.V1ObjectMeta(name=name_pre),
        spec=client.V1CronJobSpec(
            schedule=cron_new,
            job_template=client.V1JobTemplateSpec(
                spec=client.V1JobSpec(
                    template=client.V1PodTemplateSpec(
                        metadata=client.V1ObjectMeta(labels={"app": name_pre}),
                        spec=client.V1PodSpec(
                            restart_policy="Never",
                            containers=[
                                client.V1Container(
                                    name=name_pre,
                                    image="registry.cn-shenzhen.aliyuncs.com/starsl/busybox-curl",
                                    command=[
                                        "curl",
                                        "-s",
                                        "-k",
                                        "-X",
                                        "POST",
                                        "-H",
                                        "Content-Type: application/json",
                                        "-d",
                                        f'{json.dumps(service)}',
                                        url,
                                    ],
                                    env=[client.V1EnvVar(name="CRONJOB_TYPE", value=job_type)],
                                )
                            ],
                        ),
                    )
                )
            ),
        ),
    )

    namespace = "kubedoor"
    try:
        await batch_v1.create_namespaced_cron_job(namespace=namespace, body=cronjob)
        content = f"CronJob '{name_pre}' created successfully."
        logger.info(content)
        utils.send_msg(f'【{utils.PROM_K8S_TAG_VALUE}】{content}')
        return web.json_response({"message": "ok"})
    except Exception as e:
        error_message = json.loads(e.body).get("message") if hasattr(e, 'body') and e.body else "执行失败"
        logger.error(error_message)
        return web.json_response({"message": error_message}, status=500)


async def update_namespace_label(ns, action):
    """命名空间修改标签"""
    namespace_name = ns
    label_key = "kubedoor-ignore"
    label_value = 'true' if action == "add" else None
    patch_body = {"metadata": {"labels": {label_key: label_value}}}

    try:
        response = await core_v1.patch_namespace(name=namespace_name, body=patch_body)
        logger.info(
            f"Label '{label_key}: {label_value}' {'added to' if action == 'add' else 'removed from'} namespace '{namespace_name}' successfully."
        )
        logger.info(f"Updated namespace labels: {response.metadata.labels}")
    except ApiException as e:
        logger.error(f"Exception when patching namespace '{namespace_name}': {e}")


async def get_mutating_webhook():
    """获取 MutatingWebhookConfiguration"""
    webhook_name = "kubedoor-admis-configuration"
    try:
        await admission_api.read_mutating_webhook_configuration(name=webhook_name)
        logger.info(f"MutatingWebhookConfiguration '{webhook_name}' exists.")
        return {"is_on": True}
    except ApiException as e:
        if e.status == 404:
            logger.error(f"MutatingWebhookConfiguration '{webhook_name}' does not exist.")
            return {"is_on": False}
        error_message = json.loads(e.body).get("message") if hasattr(e, 'body') and e.body else "执行失败"
        logger.error(f"Exception when reading MutatingWebhookConfiguration: {e}")
        return {"message": error_message}


async def create_mutating_webhook():
    """创建 MutatingWebhookConfiguration"""
    webhook_name = "kubedoor-admis-configuration"
    namespace = "kubedoor"
    webhook_config = client.V1MutatingWebhookConfiguration(
        metadata=client.V1ObjectMeta(name=webhook_name),
        webhooks=[
            client.V1MutatingWebhook(
                name="kubedoor-admis.mutating.webhook",
                client_config=client.AdmissionregistrationV1WebhookClientConfig(
                    service=client.AdmissionregistrationV1ServiceReference(
                        namespace=namespace, name="kubedoor-agent", path="/api/admis", port=443
                    ),
                    ca_bundle=utils.BASE64CA,
                ),
                rules=[
                    client.V1RuleWithOperations(
                        operations=["CREATE", "UPDATE"],
                        api_groups=["apps"],
                        api_versions=["v1"],
                        resources=["deployments", "deployments/scale"],
                        scope="*",
                    )
                ],
                failure_policy="Fail",
                match_policy="Equivalent",
                namespace_selector=client.V1LabelSelector(
                    match_expressions=[
                        client.V1LabelSelectorRequirement(key="kubedoor-ignore", operator="DoesNotExist")
                    ]
                ),
                side_effects="None",
                timeout_seconds=30,
                admission_review_versions=["v1"],
                reinvocation_policy="Never",
            )
        ],
    )

    try:
        response = await admission_api.create_mutating_webhook_configuration(body=webhook_config)
        logger.info(f"MutatingWebhookConfiguration created: {response.metadata.name}")
        await update_namespace_label("kube-system", "add")
        await update_namespace_label("kubedoor", "add")
    except ApiException as e:
        error_message = json.loads(e.body).get("message") if hasattr(e, 'body') and e.body else "执行失败"
        logger.error(f"Exception when creating MutatingWebhookConfiguration: {e}")
        return {"message": error_message}


async def delete_mutating_webhook():
    """删除 MutatingWebhookConfiguration"""
    webhook_name = "kubedoor-admis-configuration"
    try:
        await admission_api.delete_mutating_webhook_configuration(name=webhook_name)
        logger.info(f"MutatingWebhookConfiguration '{webhook_name}' deleted successfully.")
        await update_namespace_label("kube-system", "delete")
        await update_namespace_label("kubedoor", "delete")
    except ApiException as e:
        error_message = json.loads(e.body).get("message") if hasattr(e, 'body') and e.body else "执行失败"
        logger.error(f"Exception when deleting MutatingWebhookConfiguration: {e}")
        return {"message": error_message}


async def admis_switch(request):
    action = request.query.get("action")
    res = await get_mutating_webhook()

    if action == "get":
        return web.json_response(res)
    elif action == "on":
        if res.get("is_on"):
            return web.json_response({"message": "Webhook is already opened!", "success": True})
        create_res = await create_mutating_webhook()
        if create_res:
            return web.json_response(create_res, status=500)
    elif action == "off":
        if not res.get("is_on"):
            return web.json_response({"message": "Webhook is already closed!", "success": True})
        delete_res = await delete_mutating_webhook()
        if delete_res:
            return web.json_response(delete_res, status=500)

    return web.json_response({"message": "执行成功", "success": True})


async def get_namespace_events(request):
    """获取指定命名空间的事件，如果不指定namespace则获取所有命名空间的事件"""
    namespace = request.query.get("namespace")

    try:
        # 构造查询条件
        field_selector = None
        if namespace:
            field_selector = f"involvedObject.namespace={namespace}"
            logger.info(f"获取命名空间 {namespace} 的事件")
        else:
            logger.info("获取所有命名空间的事件")

        # 获取事件
        events = await core_v1.list_event_for_all_namespaces(field_selector=field_selector, _request_timeout=30)

        # 格式化事件数据
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
    except ApiException as e:
        error_message = f"获取事件失败: {e}"
        logger.error(error_message)
        return web.json_response({"message": error_message, "success": False}, status=500)
    except Exception as e:
        error_message = f"获取事件时发生未知错误: {e}"
        logger.exception(error_message)
        return web.json_response({"message": error_message, "success": False}, status=500)


async def get_pod_metrics(namespace, pod_name):
    """获取指定Pod的CPU和内存使用情况"""
    try:
        # 使用Metrics API获取Pod的资源使用情况
        metrics = await custom_api.get_namespaced_custom_object(
            group="metrics.k8s.io",
            version="v1beta1",
            namespace=namespace,
            plural="pods",
            name=pod_name,
        )

        # 初始化返回结构
        cpu_usage = 0
        memory_usage = 0

        # 累计所有容器的资源使用
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
        }  # 单位：mCPU (毫核)  # 单位：MB
    except Exception as e:
        logger.error(f"获取Pod {pod_name} 资源使用情况失败: {e}")
        return {"cpu": 0, "memory": 0}


async def get_pod_events(namespace, pod_name):
    """获取指定Pod的事件信息"""
    try:
        # 使用field_selector过滤特定Pod的事件
        field_selector = f"involvedObject.kind=Pod,involvedObject.name={pod_name}"
        events = await core_v1.list_namespaced_event(namespace=namespace, field_selector=field_selector)

        # 按时间降序排序，获取最近的事件
        sorted_events = sorted(
            events.items,
            key=lambda event: event.last_timestamp or event.first_timestamp or event.metadata.creation_timestamp,
            reverse=True,
        )

        # 返回最新的事件消息
        if sorted_events:
            latest_event = sorted_events[0]
            return latest_event.reason, latest_event.message

        return "", ""
    except Exception as e:
        logger.error(f"获取Pod {pod_name} 事件失败: {e}")
        return "", ""


async def get_deployment_pods(request):
    """获取指定命名空间和Deployment下的所有Pod信息（包括被隔离的Pod）"""
    namespace = request.query.get("namespace")
    deployment_name = request.query.get("deployment")

    try:
        # 获取指定Deployment的标签选择器
        deployment = await v1.read_namespaced_deployment(deployment_name, namespace)
        selector = deployment.spec.selector.match_labels
        selector_str = ",".join([f"{k}={v}" for k, v in selector.items()])
        # 1. 通过标签选择器查询相关的Pod
        pods_by_label = await core_v1.list_namespaced_pod(namespace=namespace, label_selector=selector_str)
        lenmline = pods_by_label.items[0].metadata.name.count("-")
        # 2. 通过 ownerReferences 查询所有属于该 deployment 的 pod（即使 label 被修改也能查到）
        all_pods = await core_v1.list_namespaced_pod(namespace=namespace)
        pods_by_match = []
        for pod in all_pods.items:
            owner_refs = pod.metadata.owner_references or []
            # 智能匹配：如果没有ownerReferences，尝试用pod名称前缀、镜像等特征判断
            if (
                not owner_refs
                and pod.metadata.name.startswith(deployment_name + '-')
                and pod.metadata.name.count("-") == lenmline
            ):
                pods_by_match.append(pod)
                # 镜像匹配（可根据实际需求扩展更复杂的规则）
                # deployment_images = [c.image for c in deployment.spec.template.spec.containers]
                # pod_images = [c.image for c in pod.spec.containers]
                # if any(img in deployment_images for img in pod_images):

        # 合并 pod，去重
        all_related_pods = {
            pod.metadata.name: pod
            for pod in pods_by_label.items
            if pod.metadata.name.startswith(deployment_name + '-') and pod.metadata.name.count("-") == lenmline
        }
        for pod in pods_by_match:
            all_related_pods[pod.metadata.name] = pod

        # 构建Pod信息列表
        pod_list = []
        for pod in all_related_pods.values():
            # 获取Pod资源使用情况
            metrics = await get_pod_metrics(namespace, pod.metadata.name)

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
                    event_reason, event_message = await get_pod_events(namespace, pod.metadata.name)
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


async def get_node_metrics(node_name):
    """获取节点的资源使用情况"""
    try:
        # 使用Metrics API获取节点的资源使用情况
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

            # 转换CPU值（从字符串如 "100m" 转为数值 100）
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
        }  # 单位：mCPU (毫核)和MB
    except Exception as e:
        logger.error(f"获取节点 {node_name} 资源使用情况失败: {e}")
        return {"cpu": 0, "memory": 0}


async def get_nodes_info(request):
    """获取所有K8S节点的详细信息"""
    try:
        logger.info("开始获取K8S节点信息...")

        # 获取所有节点列表
        nodes = await core_v1.list_node()

        # 获取所有Pod列表（用于计算每个节点上的Pod数量）
        pods = await core_v1.list_pod_for_all_namespaces()

        # 初始化返回结果
        node_list = []

        for node in nodes.items:
            node_name = node.metadata.name

            # 获取节点的IP地址
            node_ip = ""
            for address in node.status.addresses:
                if address.type == "InternalIP":
                    node_ip = address.address
                    break

            # 获取节点的系统信息
            container_runtime = node.status.node_info.container_runtime_version
            os_image = f"{node.status.node_info.os_image} {node.status.node_info.kernel_version}"
            kubelet_version = node.status.node_info.kubelet_version

            # 获取节点的状态条件，只保留status为True的
            conditions = []
            for condition in node.status.conditions:
                if condition.status == "True":
                    conditions.append(condition.type)

            # 获取节点的可分配资源
            allocatable_cpu = 0
            allocatable_memory = 0
            max_pods = 0

            if node.status.allocatable:
                # CPU单位通常是核心数，如"4"表示4核心
                allocatable_cpu_str = node.status.allocatable.get("cpu", "0")
                allocatable_cpu = utils.parse_cpu(allocatable_cpu_str)

                # 内存单位通常是字节，需要转换为MB
                allocatable_memory_str = node.status.allocatable.get("memory", "0")
                allocatable_memory = utils.parse_memory(allocatable_memory_str)

                # 最大可运行Pod数
                max_pods_str = node.status.allocatable.get("pods", "0")
                try:
                    max_pods = int(max_pods_str)
                except (ValueError, AttributeError):
                    max_pods = 0

            # 计算当前节点上运行的Pod数量
            current_pods = 0
            for pod in pods.items:
                if pod.spec.node_name == node_name:
                    current_pods += 1

            # 获取节点当前资源使用情况
            metrics = await get_node_metrics(node_name)
            current_cpu = metrics["cpu"]  # mCPU
            current_memory = metrics["memory"]  # MB

            # 构建节点信息
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

    except ApiException as e:
        error_message = f"获取节点信息失败: {e}"
        logger.error(error_message)
        return web.json_response({"message": error_message, "success": False}, status=500)
    except Exception as e:
        error_message = f"获取节点信息时发生未知错误: {e}"
        logger.exception(error_message)
        return web.json_response({"message": error_message, "success": False}, status=500)


async def balance_node(request):
    """节点微调均衡 - 将部署从源节点迁移到目标节点"""
    try:
        data = await request.json()
        env = data.get("env")
        source_node = data.get("source")
        target_node = data.get("target")
        top_deployments = data.get("top_deployments", [])

        if not source_node or not target_node or not top_deployments:
            return web.json_response({"message": "缺少必要参数", "success": False}, status=400)

        logger.info(f"开始节点均衡: 源节点 {source_node} -> 目标节点 {target_node}")
        logger.info(f"待迁移的deployment: {json.dumps(top_deployments)}")

        # 存储操作结果
        results = []

        for deployment_info in top_deployments:
            namespace = deployment_info.get("namespace")
            deployment_name = deployment_info.get("deployment")

            if not namespace or not deployment_name:
                continue

            try:
                # 1. 构造标签键
                label_key = f"{namespace}.{deployment_name}"
                logger.info(f"处理标签: {label_key}={utils.NODE_LABLE_VALUE}")

                # 2. 从源节点删除标签
                await remove_node_label(source_node, label_key)

                # 3. 在目标节点添加标签
                await update_node_with_label(namespace, target_node, deployment_name)

                # 4. 查找并删除源节点上的相关 pod
                deleted_pods = await delete_pods_on_node(namespace, deployment_name, source_node)

                results.append(
                    {
                        "namespace": namespace,
                        "deployment": deployment_name,
                        "status": "success",
                        "deleted_pods": deleted_pods,
                    }
                )

            except Exception as e:
                error_message = str(e)
                logger.error(f"迁移 {namespace}.{deployment_name} 时出错: {error_message}")
                results.append(
                    {
                        "namespace": namespace,
                        "deployment": deployment_name,
                        "status": "failed",
                        "error": error_message,
                    }
                )

        return web.json_response(
            {
                "message": f"节点均衡操作完成: {source_node} -> {target_node}",
                "success": True,
                "results": results,
            }
        )

    except Exception as e:
        logger.exception(f"节点均衡操作失败: {e}")
        return web.json_response({"message": f"操作失败: {str(e)}", "success": False}, status=500)


async def remove_node_label(node_name, label_key):
    """从节点删除指定标签"""
    patch_body = {"metadata": {"labels": {label_key: None}}}  # 设置标签值为 None 表示删除标签
    try:
        await core_v1.patch_node(name=node_name, body=patch_body)
        logger.info(f"从节点 {node_name} 删除标签 {label_key} 成功")
    except ApiException as e:
        logger.error(f"从节点 {node_name} 删除标签 {label_key} 时出错: {e}")
        raise Exception(f"删除标签失败: {str(e)}")


async def delete_pods_on_node(namespace, deployment_name, node_name):
    """删除指定节点上指定deployment的pod"""
    try:
        # 获取该namespace下的所有pod
        pods = await core_v1.list_namespaced_pod(namespace=namespace)

        # 构建正则表达式模式，匹配deployment_name-[a-z0-9]+-[a-z0-9]+
        pattern = re.compile(f"^{re.escape(deployment_name)}-[a-z0-9]+-[a-z0-9]+$")

        deleted_pods = []
        for pod in pods.items:
            # 检查pod是否属于目标deployment（使用正则匹配）并且在指定节点上
            if pattern.match(pod.metadata.name) and pod.spec.node_name == node_name:
                logger.info(f"删除pod: {pod.metadata.name}")
                await core_v1.delete_namespaced_pod(name=pod.metadata.name, namespace=namespace)
                deleted_pods.append(pod.metadata.name)

        logger.info(f"在节点 {node_name} 上删除了 {len(deleted_pods)} 个 {deployment_name} 的pod")
        return deleted_pods
    except ApiException as e:
        logger.error(f"删除pod时出错: {e}")
        raise Exception(f"删除pod失败: {str(e)}")


async def delete_pods_in_available_nodes(namespace, deployment_name, available_nodes):
    """根据namespace和deployment_name精确找到deployment的所有pod，删除在available_nodes中的pod"""
    try:
        # 1. 获取Deployment的标签选择器
        deployment = await v1.read_namespaced_deployment(deployment_name, namespace)
        selector = deployment.spec.selector.match_labels
        label_selector = ",".join([f"{k}={v}" for k, v in selector.items()])

        deleted_count = 0
        target_delete_count = len(available_nodes)
        deleted_pods = []

        # 2. 遍历每个available_node，查找并删除该节点上的pod
        for node_name in available_nodes:
            if deleted_count >= target_delete_count:
                break

            # 使用字段选择器过滤节点 + 标签选择器过滤Pod
            field_selector = f"spec.nodeName={node_name}"
            pods = await core_v1.list_namespaced_pod(
                namespace=namespace, label_selector=label_selector, field_selector=field_selector
            )

            # 3. 删除找到的pod（每个节点只删除一个pod）
            for pod in pods.items:
                if deleted_count >= target_delete_count:
                    break

                logger.info(f"删除pod: {pod.metadata.name} (节点: {pod.spec.node_name})")
                await core_v1.delete_namespaced_pod(name=pod.metadata.name, namespace=namespace)
                deleted_pods.append(pod.metadata.name)
                deleted_count += 1
                break  # 每个节点只删除一个pod

        logger.info(f"删除了 {deleted_count} 个 {deployment_name} 的pod，目标删除数量: {target_delete_count}")
        return deleted_pods

    except ApiException as e:
        logger.error(f"删除pod时出错: {e}")
        raise Exception(f"删除pod失败: {str(e)}")
    except Exception as e:
        logger.error(f"删除pod时发生未知错误: {e}")
        raise Exception(f"删除pod失败: {str(e)}")


def admis_pass(uid):
    return {
        "apiVersion": "admission.k8s.io/v1",
        "kind": "AdmissionReview",
        "response": {"uid": uid, "allowed": True},
    }


def admis_fail(uid, code, message):
    return {
        "apiVersion": "admission.k8s.io/v1",
        "kind": "AdmissionReview",
        "response": {"uid": uid, "allowed": False, "status": {"code": code, "message": message}},
    }


def scale_only(uid, replicas):
    patch_replicas = {"op": "replace", "path": "/spec/replicas", "value": replicas}
    code = base64.b64encode(json.dumps([patch_replicas]).encode()).decode()
    return {
        "apiVersion": "admission.k8s.io/v1",
        "kind": "AdmissionReview",
        "response": {"uid": uid, "allowed": True, "patchType": "JSONPatch", "patch": code},
    }


def get_deployment_affinity(namespace, deployment_name, pod_label):
    affinity = {
        "nodeAffinity": {
            "requiredDuringSchedulingIgnoredDuringExecution": {
                "nodeSelectorTerms": [
                    {
                        "matchExpressions": [
                            {
                                "key": f"{namespace}.{deployment_name}",
                                "operator": "In",
                                "values": [f"{utils.NODE_LABLE_VALUE}"],
                            }
                        ]
                    }
                ]
            }
        },
        "podAntiAffinity": {
            "requiredDuringSchedulingIgnoredDuringExecution": [
                {
                    "labelSelector": {"matchExpressions": [{"key": "app", "operator": "In", "values": [pod_label]}]},
                    "topologyKey": "kubernetes.io/hostname",
                }
            ]
        },
    }
    return affinity


async def get_pod_label_and_maxUnavailable(namespace, deployment_name):
    """从deployment查pod标签和重启策略的maxUnavailable"""
    try:
        # 获取 Deployment
        deployment = await v1.read_namespaced_deployment(deployment_name, namespace)
        # 获取 app 标签的值
        app_label_value = deployment.spec.template.metadata.labels.get('app')
        # 获取 重启策略的maxUnavailable
        maxUnavailable_value = deployment.spec.strategy.rolling_update.max_unavailable
        return {"app_label_value": app_label_value, "maxUnavailable_value": maxUnavailable_value}
    except ApiException as e:
        logger.error(f"Kubernetes API 错误: {e}")
    except Exception as e:
        logger.error(f"发生未知错误: {e}")
    return None


async def get_deployment_affinity_old(namespace, deployment_name):
    """检查deployment是否包含kubedoor-scheduler标签匹配
    返回: has_kubedoor_scheduler
    """
    try:
        # 获取 Deployment
        deployment = await v1.read_namespaced_deployment(deployment_name, namespace)
        # 获取 affinity配置
        affinity = deployment.spec.template.spec.affinity

        has_kubedoor_scheduler = False

        if affinity and affinity.node_affinity:
            # 检查是否包含kubedoor-scheduler
            node_affinity = affinity.node_affinity
            if node_affinity.required_during_scheduling_ignored_during_execution:
                node_selector_terms = (
                    node_affinity.required_during_scheduling_ignored_during_execution.node_selector_terms
                )
                for term in node_selector_terms:
                    for expression in term.match_expressions:
                        if 'kubedoor-scheduler' in expression.values:
                            has_kubedoor_scheduler = True
                            break
                    if has_kubedoor_scheduler:
                        break

        return has_kubedoor_scheduler
    except Exception as e:
        logger.error(f"检查deployment affinity配置失败: {e}")
        return False


def process_max_unavailable(max_unavailable):
    if isinstance(max_unavailable, int) or isinstance(max_unavailable, float):
        return max_unavailable
    # 如果是字符串，并且包含 '%'，表示是百分比
    if '%' in max_unavailable:
        # 去掉百分号并将百分比转为小数
        return float(max_unavailable.strip('%')) / 100
    elif '.' in max_unavailable:
        # 小数
        return float(max_unavailable)
    # 如果是整数，直接返回原值
    else:
        return int(max_unavailable)


async def update_all(
    replicas,
    namespace,
    deployment_name,
    request_cpu_m,
    request_mem_mb,
    limit_cpu_m,
    limit_mem_mb,
    resources_dict,
    uid,
    scheduler,
):
    change_list = []
    scheduler = bool(scheduler)
    if scheduler == True:
        # 如果开了强制调度开关，则要设置affinity
        info_dict = await get_pod_label_and_maxUnavailable(namespace, deployment_name)
        if not info_dict:
            logger.error(f"未查到【{namespace}】【{deployment_name}】pod标签或重启策略的maxUnavailable")
            return web.json_response({"error": f"未查到【{namespace}】【{deployment_name}】pod标签"}, status=500)
        pod_label = info_dict.get("app_label_value")
        value = get_deployment_affinity(namespace, deployment_name, pod_label)
        logger.info("配置affinity（选择节点和反亲和性）")
        affinity = {"op": "replace", "path": "/spec/template/spec/affinity", "value": value}
        logger.info(value)
        change_list.append(affinity)
        maxUnavailable_value = info_dict.get("maxUnavailable_value")
        maxUnavailable_value_new = process_max_unavailable(maxUnavailable_value)
        if int(replicas) * maxUnavailable_value_new < 1:
            logger.info(f"maxUnavailable_value原值为{maxUnavailable_value}，改为1")
            maxUnavailable_value = 1
        restart_strategy = {
            "op": "replace",
            "path": "/spec/strategy/rollingUpdate/maxUnavailable",
            "value": maxUnavailable_value,
        }
        change_list.append(restart_strategy)
    else:
        # 如果deployment配置过affinity选择节点，则删除nodeAffinity部分
        has_kubedoor_scheduler = await get_deployment_affinity_old(namespace, deployment_name)
        if has_kubedoor_scheduler:
            # 直接删除nodeAffinity字段，保留podAffinity和podAntiAffinity
            remove_node_affinity = {"op": "remove", "path": "/spec/template/spec/affinity/nodeAffinity"}
            change_list.append(remove_node_affinity)
            logger.info(
                f"检查到【{namespace}】【{deployment_name}】已配置节点选择，并且调度开关已关闭，删除nodeAffinity字段"
            )
    # 按照数据库修改所有参数
    patch_replicas = {"op": "replace", "path": "/spec/replicas", "value": replicas}
    change_list.append(patch_replicas)
    # request_cpu_m, request_mem_mb, limit_cpu_m, limit_mem_mb，有则改，无则不动
    logger.info(f"改前：{resources_dict}")
    if request_cpu_m > 0:
        resources_dict["requests"]["cpu"] = f"{request_cpu_m}m"
    else:
        utils.send_msg(f"admis:【{utils.PROM_K8S_TAG_VALUE}】【{namespace}】【{deployment_name}】未配置 request_cpu_m")
    if request_mem_mb > 0:
        resources_dict["requests"]["memory"] = f"{request_mem_mb}Mi"
    else:
        utils.send_msg(f"admis:【{utils.PROM_K8S_TAG_VALUE}】【{namespace}】【{deployment_name}】未配置 request_mem_mb")
    if limit_cpu_m > 0:
        resources_dict["limits"]["cpu"] = f"{limit_cpu_m}m"
    else:
        utils.send_msg(f"admis:【{utils.PROM_K8S_TAG_VALUE}】【{namespace}】【{deployment_name}】未配置 limit_cpu_m")
    if limit_mem_mb > 0:
        resources_dict["limits"]["memory"] = f"{limit_mem_mb}Mi"
    else:
        utils.send_msg(f"admis:【{utils.PROM_K8S_TAG_VALUE}】【{namespace}】【{deployment_name}】未配置 limit_mem_mb")
    logger.info(f"改后：{resources_dict}")
    resources = {
        "op": "add",
        "path": "/spec/template/spec/containers/0/resources",
        "value": resources_dict,
    }
    change_list.append(resources)
    code = base64.b64encode(json.dumps(change_list).encode()).decode()
    return {
        "apiVersion": "admission.k8s.io/v1",
        "kind": "AdmissionReview",
        "response": {"uid": uid, "allowed": True, "patchType": "JSONPatch", "patch": code},
    }


async def admis_mutate(request):
    request_info = await request.json()
    object = request_info['request']['object']
    old_object = request_info['request']['oldObject']
    kind = request_info['request']['kind']['kind']
    operation = request_info['request']['operation']
    uid = request_info['request']['uid']
    namespace = object['metadata']['namespace']
    deployment_name = object['metadata']['name']
    logger.info(f"admis:【{utils.PROM_K8S_TAG_VALUE}】【{namespace}】【{deployment_name}】收到请求{object}")

    # 检查临时扩缩容标记
    annotations = object.get('metadata', {}).get('annotations', {})
    scale_temp = annotations.get('scale.temp')
    if scale_temp:
        try:
            # 提取@前的时间部分
            time_part = scale_temp.split('@')[0]
            # 解析时间，注意格式是 "%Y-%m-%d %H:%M:%S"
            temp_time = datetime.strptime(time_part, "%Y-%m-%d %H:%M:%S")
            current_time = datetime.now()
            time_diff = current_time - temp_time

            # 如果在5分钟内，并且满足特定条件，直接放行
            if time_diff <= timedelta(minutes=5):
                if (kind == 'Scale' and operation == 'UPDATE') or (
                    kind == 'Deployment'
                    and operation == 'UPDATE'
                    and object['spec']['template'] == old_object['spec']['template']
                    and old_object['spec']['replicas'] != object['spec']['replicas']
                ):
                    logger.info(
                        f"admis:【{utils.PROM_K8S_TAG_VALUE}】【{namespace}】【{deployment_name}】临时扩缩容在5分钟内，直接放行: {scale_temp}"
                    )
                    return web.json_response(admis_pass(uid))
        except Exception as e:
            logger.warning(f"解析scale.temp时间失败: {e}")

    if ws_conn is None or ws_conn.closed:
        utils.send_msg(
            f"admis:【{utils.PROM_K8S_TAG_VALUE}】【{namespace}】【{deployment_name}】连接 kubedoor-master 失败"
        )
        return web.json_response(admis_fail(uid, 503, "连接 kubedoor-master 失败"))

    response_future = asyncio.get_event_loop().create_future()
    request_futures[uid] = response_future
    await ws_conn.send_json({"type": "admis", "request_id": uid, "namespace": namespace, "deployment": deployment_name})
    try:
        result = await asyncio.wait_for(response_future, timeout=30)
        logger.info(f"response_future 收到 admis 响应：{uid} {result}")
    except asyncio.TimeoutError:
        del request_futures[uid]
        utils.send_msg(
            f"admis:【{utils.PROM_K8S_TAG_VALUE}】【{namespace}】【{deployment_name}】连接 kubedoor-master 响应超时"
        )
        return web.json_response(admis_fail(uid, 504, "等待 kubedoor-master 响应超时"))

    if len(result) == 2:
        utils.send_msg(f"admis:【{utils.PROM_K8S_TAG_VALUE}】【{namespace}】【{deployment_name}】{result[1]}")
        if result[0] == 200:
            return web.json_response(admis_pass(uid))
        return web.json_response(admis_fail(uid, result[0], result[1]))

    (
        pod_count,
        pod_count_ai,
        pod_count_manual,
        request_cpu_m,
        request_mem_mb,
        limit_cpu_m,
        limit_mem_mb,
        scheduler,
    ) = result
    # 副本数取值优先级：专家建议→ai建议→原本的副本数
    replicas = pod_count_manual if pod_count_manual >= 0 else (pod_count_ai if pod_count_ai >= 0 else pod_count)
    # 如果数据库中request_cpu_m为0，设置为10；如果request_mem_mb为0，设置为1
    request_cpu_m = 10 if 0 <= request_cpu_m < 10 else request_cpu_m
    request_mem_mb = 1 if request_mem_mb == 0 else request_mem_mb
    deploy_baseinfo = f"副本数:{replicas}, 请求CPU:{request_cpu_m}m, 请求内存:{request_mem_mb}MB, 限制CPU:{limit_cpu_m}m, 限制内存:{limit_mem_mb}MB"
    logger.info(deploy_baseinfo)

    try:
        if kind == 'Scale' and operation == 'UPDATE':
            # 把spec.replicas修改成数据库的pod数一致
            admis_msg = f"admis:【{utils.PROM_K8S_TAG_VALUE}】【{namespace}】【{deployment_name}】收到scale请求，仅修改replicas为: {replicas}"
            logger.info(admis_msg)
            utils.send_msg(admis_msg)
            return web.json_response(scale_only(uid, replicas))
        elif kind == 'Deployment' and operation == 'CREATE':
            # 按照数据库修改所有参数
            admis_msg = f"admis:【{utils.PROM_K8S_TAG_VALUE}】【{namespace}】【{deployment_name}】收到 create 请求，修改所有参数"
            logger.info(admis_msg)
            utils.send_msg(f'{admis_msg}\n{deploy_baseinfo}\n固定节点均衡: {scheduler}')
            resources_dict = object['spec']['template']['spec']['containers'][0].get('resources', {}) or {}
            # 确保 requests 和 limits 字段都存在
            resources_dict.setdefault('requests', {})
            resources_dict.setdefault('limits', {})
            return web.json_response(
                await update_all(
                    replicas,
                    namespace,
                    deployment_name,
                    request_cpu_m,
                    request_mem_mb,
                    limit_cpu_m,
                    limit_mem_mb,
                    resources_dict,
                    uid,
                    scheduler,
                )
            )
        elif kind == 'Deployment' and operation == 'UPDATE':
            template = object['spec']['template']
            old_template = old_object['spec']['template']
            if object == old_object:
                logger.info(
                    f"admis:【{utils.PROM_K8S_TAG_VALUE}】【{namespace}】【{deployment_name}】object 和 oldObject 相等，跳过"
                )
                return web.json_response(admis_pass(uid))
            elif template != old_template:
                # spec.template 变了,触发重启逻辑,按照数据库修改所有参数
                admis_msg = f"admis:【{utils.PROM_K8S_TAG_VALUE}】【{namespace}】【{deployment_name}】收到 update 请求，修改所有参数"
                logger.info(admis_msg)
                utils.send_msg(f'{admis_msg}\n{deploy_baseinfo}\n固定节点均衡: {scheduler}')
                resources_dict = object['spec']['template']['spec']['containers'][0].get('resources', {}) or {}
                # 确保 requests 和 limits 字段都存在
                resources_dict.setdefault('requests', {})
                resources_dict.setdefault('limits', {})
                return web.json_response(
                    await update_all(
                        replicas,
                        namespace,
                        deployment_name,
                        request_cpu_m,
                        request_mem_mb,
                        limit_cpu_m,
                        limit_mem_mb,
                        resources_dict,
                        uid,
                        scheduler,
                    )
                )
            elif template == old_template and replicas != object['spec']['replicas']:
                # spec.template 没变,spec.replicas 变了,只把修改spec.replicas和数据库的pod数一致
                admis_msg = f"admis:【{utils.PROM_K8S_TAG_VALUE}】【{namespace}】【{deployment_name}】收到 update 请求，仅修改replicas为: {replicas}"
                logger.info(admis_msg)
                utils.send_msg(admis_msg)
                return web.json_response(scale_only(uid, replicas))
            elif template == old_template and replicas == object['spec']['replicas']:
                # spec.template 没变,spec.replicas 没变,什么也不做
                logger.info(
                    f"admis:【{utils.PROM_K8S_TAG_VALUE}】【{namespace}】【{deployment_name}】template 和 replicas 没变，不处理"
                )
                return web.json_response(admis_pass(uid))
        else:
            admis_msg = f"admis:【{utils.PROM_K8S_TAG_VALUE}】【{namespace}】【{deployment_name}】不符合预设判断条件: {kind} {operation}，直接放行"
            logger.error(admis_msg)
            utils.send_msg(admis_msg)
            return web.json_response(admis_pass(uid))
    except Exception as e:
        logger.exception(f"【{namespace}】【{deployment_name}】Webhook 处理错误：{e}")
        utils.send_msg(f"admis:【{utils.PROM_K8S_TAG_VALUE}】【{namespace}】【{deployment_name}】处理错误：{e}")
        return web.json_response({"error": str(e)}, status=500)


async def get_labeled_nodes_count(namespace, deployment_name, nodes):
    """获取已有指定标签的节点数"""
    labeled_nodes_count = 0
    label_key = f"{namespace}.{deployment_name}"
    for node in nodes.items:
        labels = node.metadata.labels
        if labels and labels.get(label_key) == utils.NODE_LABLE_VALUE:
            labeled_nodes_count += 1
    return labeled_nodes_count


async def delete_label(namespace, deployment_name, nodes):
    """如果节点上没有这个服务的pod，则删掉标签"""
    label_key = f"{namespace}.{deployment_name}"
    for node in nodes.items:
        node_name = node.metadata.name
        labels = node.metadata.labels
        flag = False
        if labels and labels.get(label_key) == utils.NODE_LABLE_VALUE:
            try:
                # 获取节点上的所有 Pod
                all_pods = await core_v1.list_pod_for_all_namespaces(field_selector=f"spec.nodeName={node_name}")
                for pod in all_pods.items:
                    pod_name = pod.metadata.name
                    # 判断 Pod 是否属于指定的服务 (此逻辑可根据具体服务标签调整)
                    if pod_name and pod_name.startswith(f"{deployment_name}-"):
                        flag = True
                        break
            except ApiException as e:
                logger.error(f"检查节点 {node_name} 的服务 Pod 时出现问题: {e}")
            if not flag:
                patch_body = {"metadata": {"labels": {label_key: None}}}  # 设置标签值为 None 表示删除标签
                try:
                    await core_v1.patch_node(name=node_name, body=patch_body)
                    logger.info(f"节点 {node_name}上未部署服务{deployment_name}，已删除标签 {label_key}")
                except ApiException as e:
                    logger.error(f"删除节点 {node_name} 上标签 {label_key} 时出错: {e}")


async def select_least_loaded_nodes(namespace, nodes_to_label_count, deployment_name, node_cpu_list):
    """选择 CPU 使用率最低的节点并返回"""
    nodes = await core_v1.list_node()
    node_filter_list = []
    sorted_nodes = []

    for node in nodes.items:
        is_scheduled = True
        # taints = node.spec.taints if node.spec.taints else []
        # for taint in taints:
        #     # 如果节点上存在 NoSchedule 或 PreferNoSchedule 的 taint，该节点不可调度
        #     if taint.effect in ['NoSchedule', 'PreferNoSchedule']:
        #         is_scheduled = False
        #         break
        # 过滤掉已加过该服务标签的节点
        labels = node.metadata.labels
        label_key = f"{namespace}.{deployment_name}"
        if labels and labels.get(label_key) == utils.NODE_LABLE_VALUE:
            continue
        if is_scheduled:
            node_filter_list.append(node.metadata.name)
    logger.info(f"【扩容(低-->高)】node_cpu_list: {node_cpu_list}")
    for i in node_cpu_list:
        if i.get('name') in node_filter_list:
            sorted_nodes.append(i.get('name'))

    # 返回 CPU 使用率最低的节点
    if len(sorted_nodes) >= nodes_to_label_count:
        return sorted_nodes[:nodes_to_label_count]
    else:
        return None


async def select_del_label_nodes(namespace, del_label_count, deployment_name, node_cpu_list):
    """选择 CPU 使用率最高的节点并返回"""
    nodes = await core_v1.list_node()
    node_filter_list = []
    sorted_nodes = []

    for node in nodes.items:
        labels = node.metadata.labels
        label_key = f"{namespace}.{deployment_name}"
        if labels and labels.get(label_key) == utils.NODE_LABLE_VALUE:
            node_filter_list.append(node.metadata.name)

    # 按照percent值倒序排列，选择CPU使用率最高的节点
    sorted_cpu_list = sorted(node_cpu_list, key=lambda x: x.get('percent', 0), reverse=True)
    logger.info(f"【缩容(高-->低)】node_cpu_list: {sorted_cpu_list}")
    for i in sorted_cpu_list:
        if i.get('name') in node_filter_list:
            sorted_nodes.append(i.get('name'))

    # 返回 CPU 使用率最高的节点
    return sorted_nodes[:del_label_count]


async def del_node_with_label(namespace, node_name, deployment_name):
    """为节点删除标签"""
    label_key = f"{namespace}.{deployment_name}"  # 使用 命名空间.部署名称 作为标签键
    patch_body = {"metadata": {"labels": {label_key: None}}}
    try:
        await core_v1.patch_node(name=node_name, body=patch_body)
        logger.info(f"节点 {node_name} 上已删除标签 {label_key}")
    except ApiException as e:
        logger.error(f"在节点 {node_name} 上更新标签时出错: {e}")


async def update_node_with_label(namespace, node_name, deployment_name):
    """为节点添加标签"""
    label_key = f"{namespace}.{deployment_name}"  # 使用 命名空间.部署名称 作为标签键
    patch_body = {"metadata": {"labels": {label_key: utils.NODE_LABLE_VALUE}}}
    try:
        await core_v1.patch_node(name=node_name, body=patch_body)
        logger.info(f"节点 {node_name} 上已添加标签 {label_key}={utils.NODE_LABLE_VALUE}")
    except ApiException as e:
        logger.error(f"在节点 {node_name} 上更新标签时出错: {e}")


async def setup_routes(app):
    app.router.add_get('/api/health', health_check)
    app.router.add_post('/api/update-image', update_image)
    app.router.add_post('/api/scale', scale)
    app.router.add_post('/api/restart', reboot)
    app.router.add_post('/api/cron', cron)
    app.router.add_get('/api/admis_switch', admis_switch)
    app.router.add_post('/api/admis', admis_mutate)
    app.router.add_get('/api/events', get_namespace_events)
    app.router.add_get('/api/get_dpm_pods', get_deployment_pods)
    app.router.add_get('/api/nodes', get_nodes_info)
    app.router.add_post('/api/balance_node', balance_node)
    # node管理接口
    app.router.add_get('/api/nodes/list', lambda request: get_nodes_list(core_v1, custom_api, request))
    app.router.add_post('/api/nodes/cordon', lambda request: cordon_nodes(core_v1, request))
    app.router.add_post('/api/nodes/uncordon', lambda request: uncordon_nodes(core_v1, request))
    # ConfigMap管理接口
    app.router.add_get('/api/agent/configmaps', lambda request: configmap_manager.get_configmap_list(core_v1, request))
    # Service管理接口
    app.router.add_get('/api/agent/services', lambda request: service_manager.get_service_list(core_v1, request))
    app.router.add_get(
        '/api/agent/service/endpoints', lambda request: service_manager.get_service_endpoints(core_v1, request)
    )
    app.router.add_get(
        '/api/agent/service/first-port', lambda request: service_manager.get_service_first_port(core_v1, request)
    )
    # Ingress管理接口
    app.router.add_get('/api/agent/ingresses', lambda request: ingress_manager.get_ingress_list(networking_v1, request))
    app.router.add_get(
        '/api/agent/ingress/rules',
        lambda request: ingress_manager.get_ingress_rules(custom_api, request),
    )
    # Pod管理接口
    app.router.add_get('/api/agent/pods', lambda request: pod_manager.get_pod_list(core_v1, custom_api, request))
    # VirtualService管理接口
    app.router.add_get('/api/agent/istio/vs', lambda request: istio_manager.get_virtualservice(custom_api, request))
    app.router.add_post(
        '/api/agent/istio/vs/apply', lambda request: istio_manager.apply_virtualservice(custom_api, request)
    )
    # app.router.add_delete('/api/agent/istio/vs/delete', lambda request: istio_manager.delete_virtualservice(custom_api, request))

    # K8S资源管理接口
    app.router.add_post('/api/agent/res/ops', k8s_resource_handler.handle_k8s_operation)
    app.router.add_get('/api/agent/res/content', k8s_resource_handler.handle_get_resource_content)
    app.router.add_delete('/api/agent/res/delete', k8s_resource_handler.handle_delete_resource)

    # StatefulSet管理接口
    app.router.add_get(
        '/api/agent/statefulsets', lambda request: stateful_daemon_manager.get_statefulset_list(v1, request)
    )
    app.router.add_get(
        '/api/agent/statefulset/pods',
        lambda request: stateful_daemon_manager.get_statefulset_pods(request, core_v1, custom_api, v1),
    )
    app.router.add_post(
        '/api/agent/statefulset/restart', lambda request: stateful_daemon_manager.restart_statefulset(request, v1)
    )
    app.router.add_post(
        '/api/agent/statefulset/scale', lambda request: stateful_daemon_manager.scale_statefulset(request, v1)
    )

    # DaemonSet管理接口
    app.router.add_get('/api/agent/daemonsets', lambda request: stateful_daemon_manager.get_daemonset_list(v1, request))
    app.router.add_get(
        '/api/agent/daemonset/pods',
        lambda request: stateful_daemon_manager.get_daemonset_pods(request, core_v1, custom_api, v1),
    )
    app.router.add_post(
        '/api/agent/daemonset/restart', lambda request: stateful_daemon_manager.restart_daemonset(request, v1)
    )


async def start_https_server():
    """启动 HTTPS 服务器"""
    app = web.Application()
    await setup_routes(app)
    import ssl

    ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ssl_context.load_cert_chain('/serving-certs/tls.crt', '/serving-certs/tls.key')

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 443, ssl_context=ssl_context)
    await site.start()
    logger.info("HTTPS 服务器已启动，监听端口 443")
    while True:
        await asyncio.sleep(3600)


async def main():
    """主函数"""
    init_kubernetes()  # 初始化 Kubernetes 配置
    await asyncio.gather(connect_to_server(), start_https_server())


if __name__ == "__main__":
    asyncio.run(main())
