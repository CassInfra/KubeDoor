import asyncio, utils, json, sys
from functools import partial
from urllib.parse import urlencode
from aiohttp import ClientSession, ClientWebSocketResponse, WSMsgType, web
from kubernetes_asyncio import client, config
from kubernetes_asyncio.client.rest import ApiException
from datetime import datetime
from loguru import logger
from res_manager import configmap_manager
from res_manager import service_manager
from res_manager import ingress_manager
from res_manager import pod_manager
from res_manager import istio_manager
from res_manager import stateful_daemon_manager
from res_manager.node_manager import get_nodes_list, cordon_nodes, uncordon_nodes

from func_manager import k8s_resource_handler
from func_manager.upimage_monitor import DeploymentMonitor, update_image
from func_manager.restart_service import RebootService
from func_manager.k8s_event_monitor import K8sEventMonitor
from func_manager.event_monitor_config import *
from func_manager.admis_service import AdmisService
from scaler.balance_node_pod_service import BalanceNodeService
from func_manager.mcp_service import MCPService
from scaler.scale_service import ScaleService


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
admis_service = None
scale_service = None
balance_node_service = None
mcp_service = None
update_image_handler = None
reboot_service = None


def init_kubernetes():
    """在程序启动时加载 Kubernetes 配置并初始化客户端"""
    global v1, batch_v1, core_v1, networking_v1, admission_api, custom_api, deployment_monitor, event_monitor, scale_service, admis_service, balance_node_service, mcp_service, update_image_handler, reboot_service
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
        scale_service = ScaleService(v1, core_v1, custom_api, delete_cronjob_or_not)
        balance_node_service = BalanceNodeService(core_v1, v1)
        mcp_service = MCPService(core_v1, custom_api, v1)
        reboot_service = RebootService(v1, delete_cronjob_or_not)
        update_image_handler = partial(update_image, apps_v1=v1, deployment_monitor=deployment_monitor)
        admis_service = AdmisService(v1, core_v1, admission_api, request_futures)
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
                async with session.ws_connect(uri, ssl=False) as ws:
                    logger.info("成功连接到服务端")
                    global ws_conn
                    ws_conn = ws
                    if admis_service:
                        admis_service.set_ws_conn(ws)

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
                        if admis_service:
                            admis_service.set_ws_conn(None)
                        raise task_e

        except Exception as e:
            logger.error(f"连接到服务端失败：{e}")
            # 确保清理资源
            if event_monitor:
                await event_monitor.stop_monitoring()
                event_monitor.set_websocket_connection(None)
            ws_conn = None
            if admis_service:
                admis_service.set_ws_conn(None)
            logger.info(f"等待 {WEBSOCKET_RECONNECT_DELAY} 秒后重新连接...")
            await asyncio.sleep(WEBSOCKET_RECONNECT_DELAY)


async def health_check(request):
    return web.json_response({"ver": VERSION, "status": "healthy"})


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


async def scale(request):
    if scale_service is None:
        logger.error("ScaleService 尚未初始化")
        return web.json_response({"message": "Scale service 未初始化", "success": False}, status=500)
    return await scale_service.handle_scale(request)


async def cron(request):
    """创建定时任务，执行扩缩容或重启"""
    request_info = await request.json()
    cron_expr = request_info.get("cron")
    time_expr = request_info.get("time")
    type_expr = request_info.get("type")
    service = request_info.get("service")
    add_label = request.query.get("add_label")
    scheduler = request.query.get("scheduler")
    deployment_name = service['deployment_list'][0].get("deployment_name")
    name_pre = f"{type_expr}-{'once' if time_expr else 'cron'}-{deployment_name}"
    job_type = "once" if time_expr else "cron"
    cron_new = f"{time_expr[4]} {time_expr[3]} {time_expr[2]} {time_expr[1]} *" if time_expr else cron_expr
    service['deployment_list'][0]["job_name"] = name_pre
    service['deployment_list'][0]["job_type"] = job_type

    if add_label:
        url = f"https://kubedoor-agent.kubedoor/api/{type_expr}?add_label={add_label}"
    else:
        url = f"https://kubedoor-agent.kubedoor/api/{type_expr}"

    if scheduler:
        url = f"{url}?scheduler={scheduler}"
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


async def setup_routes(app):
    app.router.add_get('/api/health', health_check)
    app.router.add_post('/api/scale', scale)
    app.router.add_post('/api/cron', cron)
    if update_image_handler is None:
        raise RuntimeError("UpdateImage handler 未初始化")
    app.router.add_post('/api/update-image', update_image_handler)
    if reboot_service is None:
        raise RuntimeError("RebootService 未初始化")
    app.router.add_post('/api/restart', reboot_service.reboot)
    if admis_service is None:
        raise RuntimeError("AdmisService 未初始化")
    app.router.add_get('/api/admis_switch', admis_service.admis_switch)
    app.router.add_post('/api/admis', admis_service.admis_mutate)

    if mcp_service is None:
        raise RuntimeError("MCPService 未初始化")
    app.router.add_get('/api/get_dpm_pods', mcp_service.get_deployment_pods)
    app.router.add_get('/api/events', mcp_service.get_namespace_events)
    app.router.add_get('/api/nodes', mcp_service.get_nodes_info)
    if balance_node_service is None:
        raise RuntimeError("BalanceNodeService 未初始化")
    app.router.add_post('/api/balance_node', balance_node_service.balance_node)  # 未使用的接口
    # node管理接口
    app.router.add_get('/api/nodes/list', lambda request: get_nodes_list(core_v1, custom_api, request))
    app.router.add_post('/api/nodes/cordon', lambda request: cordon_nodes(core_v1, request))
    app.router.add_post('/api/nodes/uncordon', lambda request: uncordon_nodes(core_v1, request))
    # ConfigMap管理接口
    app.router.add_get('/api/agent/configmaps', lambda request: configmap_manager.get_configmap_list(core_v1, request))
    app.router.add_get('/api/agent/namespaces', lambda request: configmap_manager.get_namespace_list(core_v1, request))
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
    ssl_context.load_cert_chain('/app/serving-certs/tls.crt', '/app/serving-certs/tls.key')

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
