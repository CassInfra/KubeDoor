import asyncio, utils, json, sys, base64
from functools import partial
from urllib.parse import urlencode
import aiohttp
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
from func_manager.jvm_config import get_jvm_configs
from scaler.balance_node_pod_service import BalanceNodeService
from func_manager.mcp_service import MCPService
from func_manager.workload_cache import WorkloadCache
from func_manager.workload_streamer import WorkloadStreamer
from func_manager.ai_tools import AiToolHandler
from func_manager.ws_lifecycle import run_connection_tasks
from k8s_client_manager import OffloadApiClient
from scaler.scale_service import ScaleService
from scaler.cci_scaler import get_schedule_profile_info
from load_balance import analyze_and_plan, execute_plan, get_isolated_pods, cleanup_pods


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
http_session = None  # 全局 HTTP session，用于转发请求
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
workload_cache = None  # Deployment/Pod 的 list+watch 内存缓存
workload_streamer = None  # 把缓存变化推给 master 的订阅者
ai_tool_handler = None


def init_kubernetes():
    """在程序启动时加载 Kubernetes 配置并初始化客户端"""
    global v1, batch_v1, core_v1, networking_v1, admission_api, custom_api, deployment_monitor, event_monitor, scale_service, admis_service, balance_node_service, mcp_service, update_image_handler, reboot_service, workload_cache, workload_streamer, ai_tool_handler
    try:
        config.load_incluster_config()
        ai_tool_handler = AiToolHandler()
        # 大响应的反序列化放到线程里做,list 全集群对象时不卡事件循环(探活 / 心跳 / watch 都在这个循环上)
        v1 = client.AppsV1Api(OffloadApiClient())
        batch_v1 = client.BatchV1Api(OffloadApiClient())
        core_v1 = client.CoreV1Api(OffloadApiClient())
        networking_v1 = client.NetworkingV1Api(OffloadApiClient())
        admission_api = client.AdmissionregistrationV1Api(OffloadApiClient())
        custom_api = client.CustomObjectsApi(OffloadApiClient())
        deployment_monitor = DeploymentMonitor(v1, core_v1)
        event_monitor = K8sEventMonitor(core_v1)
        scale_service = ScaleService(v1, core_v1, custom_api, delete_cronjob_or_not)
        balance_node_service = BalanceNodeService(core_v1, v1)
        workload_cache = WorkloadCache(core_v1, v1)
        workload_streamer = WorkloadStreamer(workload_cache)
        mcp_service = MCPService(core_v1, custom_api, v1, workload_cache)
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
    """异步处理 HTTP 请求并发送响应，使用全局 session 复用连接"""
    try:
        logger.info(f"转发请求: {method} {path}?{urlencode(query)}【{json.dumps(body)}】")
        if method == "GET":
            async with http_session.get(path, params=query, ssl=False, timeout=aiohttp.ClientTimeout(total=300)) as resp:
                content_type = resp.content_type
                # 处理二进制响应（如 gzip 文件下载）
                if content_type in ['application/gzip', 'application/octet-stream']:
                    binary_data = await resp.read()
                    # 获取 Content-Disposition 头
                    content_disposition = resp.headers.get('Content-Disposition', '')
                    response_data = {
                        "binary": True,
                        "content_type": content_type,
                        "content_disposition": content_disposition,
                        "data": base64.b64encode(binary_data).decode('utf-8')
                    }
                else:
                    response_data = await resp.json()
        elif method == "POST":
            async with http_session.post(path, params=query, json=body, ssl=False) as resp:
                response_data = await resp.json()
                if resp.status >= 400:
                    logger.error(f"请求返回错误状态码 {resp.status}: {method} {path} -> {response_data}")
                    response_data = {"success": False, "status": resp.status, **response_data}
        elif method == "DELETE":
            async with http_session.delete(path, params=query, json=body, ssl=False) as resp:
                response_data = await resp.json()
                if resp.status >= 400:
                    logger.error(f"请求返回错误状态码 {resp.status}: {method} {path} -> {response_data}")
                    response_data = {"success": False, "status": resp.status, **response_data}
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
            elif data.get("type") == "ai_tool":
                asyncio.create_task(ai_tool_handler.handle(ws, data))
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
            elif data.get("type") == "workload_sub":
                # 页面订阅 Deployment/Pod 实时状态(master 每次都发全量期望状态)
                workload_streamer.subscribe(ws, data)
            elif data.get("type") == "workload_unsub":
                workload_streamer.unsubscribe(data.get("sub_id"))
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

            # 检查K8s事件 watch 是否长时间没有活动(集群安静时可以很久没有事件,但 watch 每几分钟会重连一次)
            if event_monitor.last_alive_time:
                idle_seconds = (current_time - event_monitor.last_alive_time).total_seconds()
                if idle_seconds > EVENT_TIMEOUT_THRESHOLD:
                    logger.warning(f"⚠️ 健康检查: K8s事件 watch 已有 {idle_seconds:.0f} 秒没有活动")

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
    uri = f"{utils.KUBEDOOR_MASTER}/ws?env={utils.PROM_K8S_TAG_VALUE}&ver={VERSION}&ai_tools=1"
    while True:
        try:
            async with ClientSession() as session:
                async with session.ws_connect(uri, ssl=False, max_msg_size=16 * 1024 * 1024) as ws:
                    logger.info("成功连接到服务端")
                    global ws_conn
                    ws_conn = ws
                    if admis_service:
                        admis_service.set_ws_conn(ws)

                    # 设置事件监听器的WebSocket连接
                    event_monitor.set_websocket_connection(ws)
                    # 实时状态推送改用新连接,旧连接上的订阅作废(master 会重新下发)
                    workload_streamer.attach(ws)

                    try:
                        await run_connection_tasks(ws, event_monitor, process_request, heartbeat, monitor_health_check)
                    finally:
                        event_monitor.set_websocket_connection(None)
                        workload_streamer.detach()
                        ws_conn = None
                        if admis_service:
                            admis_service.set_ws_conn(None)

        except Exception as e:
            logger.error(f"连接到服务端失败：{e}")
            # 确保清理资源
            if event_monitor:
                await event_monitor.stop_monitoring()
                event_monitor.set_websocket_connection(None)
            if workload_streamer:
                workload_streamer.detach()
            ws_conn = None
            if admis_service:
                admis_service.set_ws_conn(None)
        logger.info(f"等待 {WEBSOCKET_RECONNECT_DELAY} 秒后重新连接...")
        await asyncio.sleep(WEBSOCKET_RECONNECT_DELAY)


async def health_check(request):
    return web.json_response({"ver": VERSION, "status": "healthy"})


async def get_cci_schedule_profile(custom_api, apps_v1_api, request):
    """获取ScheduleProfile信息"""
    namespace = request.query.get("namespace")
    deployment_name = request.query.get("deployment")
    if not namespace or not deployment_name:
        return web.json_response({"error": "缺少namespace或deployment参数"}, status=400)
    try:
        result = await get_schedule_profile_info(custom_api, apps_v1_api, namespace, deployment_name)
        return web.json_response(result)
    except Exception as e:
        logger.error(f"获取ScheduleProfile失败: {e}")
        return web.json_response({"error": str(e)}, status=500)


async def stream_pod_logs(
    ws: ClientWebSocketResponse, connection_id: str, namespace: str, pod_name: str, container: str = ""
):
    """流式获取Pod日志并发送给master，使用 bytearray 和批量发送优化性能"""
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

        # 使用 bytearray 提高性能
        buffer = bytearray()
        batch_lines = []
        batch_size = 10  # 批量发送行数

        async for chunk in log_stream.content:
            if not chunk:
                continue
            buffer.extend(chunk)

            # 查找换行符并处理完整行
            while b'\n' in buffer:
                line_end = buffer.find(b'\n')
                line_bytes = buffer[:line_end]
                buffer = buffer[line_end + 1:]

                try:
                    line = line_bytes.decode('utf-8', errors='ignore').strip()
                    if line:
                        batch_lines.append(line)
                        # 批量发送
                        if len(batch_lines) >= batch_size:
                            await ws.send_json(
                                {
                                    "type": "pod_logs",
                                    "connection_id": connection_id,
                                    "pod_name": pod_name,
                                    "container": container,
                                    "log": '\n'.join(batch_lines),
                                }
                            )
                            batch_lines.clear()
                except Exception as decode_error:
                    logger.warning(f"解码日志行失败: {decode_error}")

        # 发送剩余日志
        if batch_lines:
            await ws.send_json(
                {
                    "type": "pod_logs",
                    "connection_id": connection_id,
                    "pod_name": pod_name,
                    "container": container,
                    "log": '\n'.join(batch_lines),
                }
            )

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


# ==================== 负载均衡接口 ====================

async def handle_load_balance_analyze(request):
    """🔄 分析负载并生成迁移计划"""
    try:
        body = await request.json()
        config = body.get("config", {})
        result = await analyze_and_plan(core_v1, v1, custom_api, config)
        return web.json_response({"success": True, "data": result})
    except Exception as e:
        logger.error(f"❌ 负载均衡分析失败: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def handle_load_balance_execute(request):
    """🚀 执行迁移计划"""
    try:
        body = await request.json()
        migrations = body.get("migrations", [])
        exclude_nodes = body.get("exclude_nodes", [])
        if not migrations:
            return web.json_response({"success": False, "error": "迁移列表为空"}, status=400)
        result = await execute_plan(core_v1, v1, custom_api, migrations, exclude_nodes)
        return web.json_response({"success": True, "data": result})
    except Exception as e:
        logger.error(f"❌ 负载均衡执行失败: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def handle_load_balance_isolated(request):
    """🔍 获取隔离Pod列表"""
    try:
        config = {
            "exclude_namespaces": request.query.get("exclude_namespaces", "kube-system,kube-public,istio-system").split(",")
        }
        result = await get_isolated_pods(core_v1, v1, custom_api, config)
        return web.json_response({"success": True, "data": result})
    except Exception as e:
        logger.error(f"❌ 获取隔离Pod列表失败: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def handle_load_balance_cleanup(request):
    """🧹 清理隔离Pod"""
    try:
        body = await request.json()
        pods = body.get("pods", [])
        if not pods:
            return web.json_response({"success": False, "error": "Pod列表为空"}, status=400)
        result = await cleanup_pods(core_v1, v1, custom_api, pods)
        return web.json_response({"success": True, "data": result})
    except Exception as e:
        logger.error(f"❌ 清理隔离Pod失败: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def handle_load_balance_nodes_cpu(request):
    """🖥️ 获取节点CPU使用率"""
    try:
        from load_balance.load_balancer import LoadBalancer
        lb = LoadBalancer(core_v1, v1, custom_api)
        nodes_cpu = await lb.get_all_nodes_cpu()
        return web.json_response({"success": True, "data": nodes_cpu})
    except Exception as e:
        logger.error(f"❌ 获取节点CPU失败: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def handle_load_balance_namespaces(request):
    """📋 获取所有namespace列表"""
    try:
        from load_balance.load_balancer import LoadBalancer
        lb = LoadBalancer(core_v1, v1, custom_api)
        namespaces = await lb.get_all_namespaces()
        return web.json_response({"success": True, "data": namespaces})
    except Exception as e:
        logger.error(f"❌ 获取namespace列表失败: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def handle_load_balance_check_pods(request):
    """🔍 检查Pod Ready状态"""
    try:
        data = await request.json()
        pods = data.get("pods", [])
        from load_balance.load_balancer import check_pods_status
        result = await check_pods_status(core_v1, pods)
        return web.json_response({"success": True, "data": result})
    except Exception as e:
        logger.error(f"❌ 检查Pod状态失败: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


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
            time_zone="Asia/Shanghai",
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
    # CCI扩容接口
    app.router.add_get('/api/cci/schedule-profile', lambda request: get_cci_schedule_profile(custom_api, v1, request))
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
    app.router.add_get('/api/agent/jvm/configs', lambda request: get_jvm_configs(v1, request))
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

    # 负载均衡接口
    app.router.add_post('/api/load-balance/analyze', handle_load_balance_analyze)
    app.router.add_post('/api/load-balance/execute', handle_load_balance_execute)
    app.router.add_get('/api/load-balance/isolated', handle_load_balance_isolated)
    app.router.add_post('/api/load-balance/cleanup', handle_load_balance_cleanup)
    app.router.add_get('/api/load-balance/nodes-cpu', handle_load_balance_nodes_cpu)
    app.router.add_get('/api/load-balance/namespaces', handle_load_balance_namespaces)
    app.router.add_post('/api/load-balance/check-pods', handle_load_balance_check_pods)


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


async def cleanup():
    """清理资源"""
    global http_session
    if ai_tool_handler:
        await ai_tool_handler.close()
    # 停止 Deployment/Pod 缓存和实时推送
    if workload_streamer:
        await workload_streamer.stop()
    if workload_cache:
        await workload_cache.stop()
    # 关闭 HTTP session
    if http_session:
        await http_session.close()
        logger.info("HTTP session 已关闭")
    # 关闭 K8S 客户端
    for api_client in [v1, batch_v1, core_v1, networking_v1, admission_api, custom_api]:
        if api_client and hasattr(api_client, 'api_client'):
            try:
                await api_client.api_client.close()
            except Exception as e:
                logger.warning(f"关闭 K8S 客户端失败: {e}")
    logger.info("K8S 客户端已关闭")


async def main():
    """主函数"""
    global http_session
    init_kubernetes()  # 初始化 Kubernetes 配置
    # Deployment/Pod 缓存跟进程走,只启动一次,不随 master 连接重建
    workload_cache.start()
    workload_streamer.start()
    # 初始化全局 HTTP session
    timeout = aiohttp.ClientTimeout(total=120)
    http_session = ClientSession(timeout=timeout)
    logger.info("HTTP session 已初始化")
    try:
        await asyncio.gather(connect_to_server(), start_https_server())
    finally:
        await cleanup()


if __name__ == "__main__":
    asyncio.run(main())
