import asyncio
import json
import sys
import time
import base64
import re
import aiohttp
from datetime import datetime, timedelta
from aiohttp import web, WSMsgType
from loguru import logger
import utils, prom_real_time_data
from multidict import MultiDict
from istio_route import istio_route
from func_manager import namespace_cache
from func_manager import prom_overview
from func_manager import ck_top_queries
import image_tags_fetcher
from k8s_event import process_k8s_event_async, init_clickhouse_tables
from k8s_event.event_query_api import query_k8s_events_handler, get_k8s_events_menu_options
from promql import deployment_node

logger.remove()


# 自定义格式化函数，将WARNING显示为WARN
def custom_formatter(record):
    level_name = record["level"].name
    if level_name == "WARNING":
        level_name = "WARN"

    # 替换原始的level为自定义的level_name
    custom_record = record.copy()
    custom_record["level"] = type('Level', (), {'name': level_name, 'no': record["level"].no})()

    return (
        '<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> [<level>' + level_name + '</level>] <level>{message}</level>\n{exception}'
    )


logger.add(
    sys.stderr,
    format=custom_formatter,
    level='INFO',
    colorize=True,  # 启用颜色输出
)


def ensure_clickhouse_schema_initialized():
    """在程序启动时初始化ClickHouse表结构"""
    try:
        init_clickhouse_tables()
        logger.info("ClickHouse表结构初始化成功")
    except Exception as exc:
        logger.error(f"ClickHouse表结构初始化失败: {exc}")
        sys.exit(1)


ensure_clickhouse_schema_initialized()


async def get_authorization_header(username, password):
    credentials = f'{username}:{password}'
    encoded_credentials = base64.b64encode(credentials.encode('utf-8')).decode('utf-8')
    return f'Basic {encoded_credentials}'


async def forward_request(request):
    try:
        data = await request.text()

        permission = request.headers.get('X-User-Permission', '')
        if permission == 'read' and not data.strip().lower().startswith('select'):
            return web.json_response({"error": "权限不足，只能执行SELECT查询"}, status=403)
        if not data.strip().lower().startswith(('select', 'alter', 'insert')):
            return web.json_response({"error": "不支持的SQL操作"}, status=403)

        # 替换数据库名称
        data = data.replace('__KUBEDOORDB__', utils.CK_DATABASE)
        logger.info(f'📐{data}')

        if data.strip().lower().startswith(('alter')):
            table_name_match = re.search(rf'{re.escape(utils.CK_DATABASE)}\.(\w+)', data)
            table_name = table_name_match.group(1) if table_name_match else None
            utils.ck_alter(data)
            utils.ck_optimize(table_name)
            logger.info("📐SQL: 数据更新完成")
            return web.json_response({"success": True, "msg": "SQL: 数据更新完成"})
        else:
            TARGET_URL = f'http://{utils.CK_HOST}:{utils.CK_HTTP_PORT}/?add_http_cors_header=1&default_format=JSONCompact'
            headers = {
                'Authorization': await get_authorization_header(utils.CK_USER, utils.CK_PASSWORD),
                'Cache-Control': 'no-cache',
                'Pragma': 'no-cache',
                'Content-Type': 'text/plain',
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(TARGET_URL, data=data, headers=headers) as response:
                    if response.content_type == 'application/json':
                        response_data = await response.json()
                    else:
                        text = await response.text()
                        response_data = {"msg": text}
                    logger.info("📐SQL: 数据查询完成")
                    return web.json_response({"success": True, **response_data})
    except Exception as e:
        logger.error(f"Error in forward_request: {e}")
        return web.json_response({"error": str(e)}, status=500)


clients = {}
# 存储Pod日志WebSocket连接
pod_logs_connections = {}


async def websocket_handler(request):
    env = request.query.get("env")
    ver = request.query.get("ver", "unknown")
    if not env:
        return web.json_response({"error": "缺少 env 参数"}, status=400)
    if env in clients and clients[env]["online"]:
        return web.json_response({"error": "目标客户端已在线"}, status=409)

    ws = web.WebSocketResponse()
    await ws.prepare(request)

    logger.info(f"客户端连接成功，env={env} ver={ver}")
    if env not in clients:
        # 如果是新客户端，初始化状态
        clients[env] = {"ws": ws, "ver": ver, "last_heartbeat": time.time(), "online": True}
        utils.ck_init_agent_status(env)
    else:
        # 如果是重连客户端，更新 WebSocket 和状态
        clients[env]["ws"] = ws
        clients[env]["ver"] = ver
        clients[env]["last_heartbeat"] = time.time()
        clients[env]["online"] = True

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                # 首先尝试解析为JSON
                try:
                    data = json.loads(msg.data)
                    # 处理JSON格式的消息
                    if data.get("type") == "heartbeat":
                        # 更新心跳时间
                        clients[env]["last_heartbeat"] = time.time()
                        clients[env]["online"] = True
                        # logger.info(f"[心跳]客户端 env={env} ver={ver}")
                    elif data.get("type") == "admis":
                        request_id = data["request_id"]
                        namespace = data["namespace"]
                        deployment = data["deployment"]
                        logger.info(f"==========客户端 env={env} {request_id} {namespace} {deployment}")
                        deploy_res = utils.get_deploy_admis(env, namespace, deployment)
                        await ws.send_json({"type": "admis", "request_id": request_id, "deploy_res": deploy_res})

                    elif data.get("type") == "response":
                        # 收到客户端的响应，存储到客户端的响应队列中
                        request_id = data["request_id"]
                        response = data["response"]
                        if "response_queue" in clients[env]:
                            clients[env]["response_queue"][request_id] = response
                        logger.info(f"[响应]客户端 env={env}: request_id={request_id}：{response}")

                    elif data.get("type") == "pod_logs":
                        # 处理来自agent的Pod日志数据，转发给前端
                        connection_id = data.get("connection_id")
                        if connection_id in pod_logs_connections:
                            frontend_ws = pod_logs_connections[connection_id]["ws"]
                            try:
                                await frontend_ws.send_json(data)
                            except Exception as e:
                                logger.error(f"转发日志到前端失败: {e}")
                                # 清理断开的连接
                                if connection_id in pod_logs_connections:
                                    del pod_logs_connections[connection_id]
                    elif data.get("type") == "k8s_event":
                        # 处理来自agent的K8S事件消息
                        logger.debug(f"💯[K8S事件]客户端 env={env}: {data}")

                        # 异步存储K8S事件到ClickHouse，避免阻塞WebSocket消息循环
                        try:
                            success = await process_k8s_event_async(data)
                            if success:
                                logger.debug(f"K8S事件已成功存储到ClickHouse: {data.get('data', {}).get('eventUid')}")
                            else:
                                logger.warning(f"K8S事件存储失败: {data.get('data', {}).get('eventUid')}")
                        except Exception as e:
                            logger.error(f"处理K8S事件时发生错误: {e}")
                    else:
                        logger.info(f"收到客户端消息：{msg.data}")

                except json.JSONDecodeError:
                    # 如果不是JSON格式，可能是纯文本日志消息
                    # 需要根据当前活跃的日志连接来转发消息
                    log_message = msg.data.strip()
                    if log_message:
                        # 转发给所有活跃的前端日志连接
                        for connection_id, connection_info in list(pod_logs_connections.items()):
                            if connection_info["env"] == env:
                                try:
                                    await connection_info["ws"].send_str(log_message)
                                except Exception as e:
                                    logger.error(f"转发纯文本日志到前端失败: {e}")
                                    # 清理断开的连接
                                    if connection_id in pod_logs_connections:
                                        del pod_logs_connections[connection_id]

            elif msg.type == WSMsgType.ERROR:
                logger.error(f"客户端连接出错，env={env}")
    except Exception as e:
        logger.error(f"客户端连接异常断开，env={env}，错误：{e}")
    finally:
        # 标记客户端为离线
        if env in clients:
            clients[env]["online"] = False
            logger.info(f"客户端连接关闭，标记为离线，env={env}")

    return ws


async def pod_logs_websocket_handler(request):
    """处理前端Pod日志WebSocket连接"""
    env = request.query.get("env")
    namespace = request.query.get("namespace")
    pod_name = request.query.get("pod_name")
    container = request.query.get("container", "")

    if not all([env, namespace, pod_name]):
        return web.json_response({"error": "缺少必要参数"}, status=400)

    if env not in clients or not clients[env]["online"]:
        return web.json_response({"error": "目标环境不在线"}, status=404)

    ws = web.WebSocketResponse()
    await ws.prepare(request)

    # 生成唯一连接ID
    connection_id = f"{env}_{namespace}_{pod_name}_{int(time.time())}"

    # 存储前端连接
    pod_logs_connections[connection_id] = {
        "ws": ws,
        "env": env,
        "namespace": namespace,
        "pod_name": pod_name,
        "container": container,
    }

    logger.info(f"Pod日志连接建立: {connection_id}")

    try:
        # 向agent发送开始日志流请求
        agent_ws = clients[env]["ws"]
        start_message = {
            "type": "start_pod_logs",
            "connection_id": connection_id,
            "namespace": namespace,
            "pod_name": pod_name,
            "container": container,
        }
        await agent_ws.send_json(start_message)

        # 处理前端消息
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    if data.get("type") == "stop_logs":
                        # 通知agent停止日志流
                        stop_message = {"type": "stop_pod_logs", "connection_id": connection_id}
                        await agent_ws.send_json(stop_message)
                        break
                except json.JSONDecodeError:
                    logger.error(f"收到无法解析的前端消息：{msg.data}")
            elif msg.type == WSMsgType.ERROR:
                logger.error(f"前端日志连接出错: {connection_id}")
                break
    except Exception as e:
        logger.error(f"Pod日志连接异常: {connection_id}, 错误: {e}")
    finally:
        # 清理连接
        if connection_id in pod_logs_connections:
            del pod_logs_connections[connection_id]

        # 通知agent停止日志流
        try:
            if env in clients and clients[env]["online"]:
                stop_message = {"type": "stop_pod_logs", "connection_id": connection_id}
                await clients[env]["ws"].send_json(stop_message)
        except Exception as e:
            logger.error(f"通知agent停止日志流失败: {e}")

        logger.info(f"Pod日志连接关闭: {connection_id}")

    return ws


async def http_handler(request):
    path = request.path
    method = request.method
    query_params = dict(request.query)
    env = query_params.get("env", None)
    try:
        body = await request.json()
    except:
        body = False
    if not env:
        return web.json_response({"error": "缺少 K8S 集群名称参数"}, status=400)

    if env not in clients or not clients[env]["online"]:
        return web.json_response({"error": "目标客户端不在线"}, status=404)

    logger.info(path)
    if path == "/api/agent/istio/vs/apply":
        body = await istio_route.generate_json_handler(request)
    elif path == "/api/update-image":
        username = request.headers.get('X-User-Name', '').lower()
        permission = request.headers.get('X-User-Permission', '')
        logger.info(f"🚧username={username}, permission={permission}: {body}")
        # 如果权限是rw，则跳过所有权限检查
        if permission == "rw":
            pass  # 直接跳过所有权限检查，继续执行后续逻辑
        else:
            # UPDATE_IMAGE权限检查
            if not hasattr(utils, 'UPDATE_IMAGE') or not utils.UPDATE_IMAGE:
                return web.json_response({"error": "拒绝操作：没有UPDATE_IMAGE权限配置"}, status=403)
            try:
                # 解析UPDATE_IMAGE JSON字符串
                update_image_config = json.loads(utils.UPDATE_IMAGE)
            except json.JSONDecodeError:
                return web.json_response({"error": "拒绝操作：UPDATE_IMAGE配置格式错误"}, status=403)

            # 获取环境相关配置
            if env not in update_image_config:
                if "default" not in update_image_config:
                    return web.json_response({"error": "拒绝操作：找不到default配置"}, status=403)
                upimage_dict = update_image_config["default"]
            else:
                upimage_dict = update_image_config[env]

            # 检查isOperationAllowed
            if "isOperationAllowed" not in upimage_dict:
                return web.json_response({"error": "拒绝操作：找不到isOperationAllowed配置"}, status=403)

            if not upimage_dict["isOperationAllowed"]:
                return web.json_response({"error": f"拒绝操作：当前{env}环境禁止操作"}, status=403)

            # 检查allowedOperationPeriod时间段
            if "allowedOperationPeriod" not in upimage_dict:
                return web.json_response({"error": "拒绝操作：找不到allowedOperationPeriod配置"}, status=403)

            allowed_period = upimage_dict["allowedOperationPeriod"]
            try:
                start_time_str, end_time_str = allowed_period.split('-')
                start_hour, start_minute = map(int, start_time_str.split(':'))
                end_hour, end_minute = map(int, end_time_str.split(':'))

                current_time = datetime.now()
                current_hour = current_time.hour
                current_minute = current_time.minute
                current_total_minutes = current_hour * 60 + current_minute

                start_total_minutes = start_hour * 60 + start_minute
                end_total_minutes = end_hour * 60 + end_minute

                # 处理跨天的情况（如19:00-08:00）
                if start_total_minutes > end_total_minutes:
                    # 跨天情况：当前时间应该在start_time之后或end_time之前（开始时间可以等于，结束时间不能等于）
                    if not (current_total_minutes >= start_total_minutes or current_total_minutes < end_total_minutes):
                        return web.json_response(
                            {"error": f"拒绝操作：当前{env}环境只允许在{allowed_period}时段操作"}, status=403
                        )
                else:
                    # 同一天情况：当前时间应该在start_time和end_time之间（开始时间可以等于，结束时间不能等于）
                    if not (start_total_minutes <= current_total_minutes < end_total_minutes):
                        return web.json_response(
                            {"error": f"拒绝操作：当前{env}环境只允许在{allowed_period}时段操作"}, status=403
                        )
            except (ValueError, IndexError):
                return web.json_response({"error": "拒绝操作：allowedOperationPeriod格式错误"}, status=403)

            # 检查用户权限
            if "user" not in upimage_dict:
                return web.json_response({"error": "拒绝操作：找不到user配置"}, status=403)

            user_list = upimage_dict["user"]
            if username not in user_list:
                return web.json_response({"error": f"拒绝操作：当前用户{username}禁止操作"}, status=403)

    elif path == "/api/agent/namespaces" and query_params.get("flush") != 'true':
        cached_namespaces = namespace_cache.get_namespaces_from_cache(env)
        if cached_namespaces is not None:
            return web.json_response({"success": True, "data": cached_namespaces})
    # 扩缩容接口要查询节点cpu使用率并传给agent
    elif path in ["/api/scale", "/api/pod/modify_pod"] and query_params.get("add_label") == 'true':
        res_type = query_params.get("type", "cpu")
        node_cpu_list = await utils.get_node_res_rank(query_params.get("env"), res_type)
        if path == "/api/scale":
            body[0]['node_cpu_list'] = node_cpu_list
        elif path == "/api/pod/modify_pod":
            body = node_cpu_list

    # 固定节点均衡模式，增加节点微调能力
    elif path == "/api/balance_node":
        source = body.get('source')
        target = body.get('target')
        num = body.get('num')
        type = body.get('type')
        logger.info(body)

        # 查询源节点所有deployment列表
        source_deployment_list = utils.get_node_deployments(source, env)
        target_deployment_list = utils.get_node_deployments(target, env)
        deployment_list = []
        for i in source_deployment_list:
            flag = True
            for j in target_deployment_list:
                if i.get('namespace') == j.get('namespace') and i.get('created_by_name') == j.get('created_by_name'):
                    flag = False
                    break
            if flag:
                deployment_list.append(i)
        logger.info(f'deployment_list去重前：{source_deployment_list}')
        logger.info(f'deployment_list去重后：{deployment_list}')
        top_deployments = utils.get_deployment_from_control_data(deployment_list, num, type, env)
        body['top_deployments'] = top_deployments

    # 向目标客户端发送消息
    request_id = str(time.time())  # 使用时间戳作为唯一请求 ID
    message = {
        "type": "request",
        "request_id": request_id,
        "method": method,
        "path": path,
        "query": query_params,
        "body": body,
    }
    await clients[env]["ws"].send_json(message)  # 使用 send_json 发送 JSON 数据
    logger.info(f"[请求]客户端 env={env}: {message}")

    # 等待客户端响应
    if "response_queue" not in clients[env]:
        clients[env]["response_queue"] = {}

    try:
        for _ in range(120 * 10):  # 等待 120 秒，检查响应队列
            if request_id in clients[env]["response_queue"]:
                response = clients[env]["response_queue"].pop(request_id)

                # 特殊处理：如果是 /api/agent/istio/vs 接口，需要对响应进行额外处理
                if path == "/api/agent/istio/vs":
                    # 在这里添加你的额外处理逻辑
                    vs_list = response.get('data', [])
                    processed_response = await istio_route.sync_vs_from_k8s(env, vs_list)
                    return web.json_response(processed_response)
                elif path == '/api/agent/namespaces':
                    namespace_cache.update_namespace_cache(env, response.get('data', []))
                return web.json_response({"success": True, **response})
            await asyncio.sleep(0.1)
    except Exception as e:
        logger.error(f"等待客户端响应时发生错误，env={env}, 错误：{e}")

    return web.json_response({"error": "客户端未响应"}, status=504)


async def status_handler(request):
    agent_info = utils.ck_agent_info()
    agents_status = {
        env: {
            "online": data["online"],
            "last_heartbeat": datetime.fromtimestamp(data["last_heartbeat"]).strftime("%Y-%m-%d %H:%M:%S"),
            "ver": data["ver"],
        }
        for env, data in clients.items()
    }
    agents = utils.merge_dicts(agents_status, agent_info)
    return web.json_response({'success': True, 'data': agents})


async def prom_query_handler(request):
    env_value = request.query.get('env')
    namespace_value = request.query.get('ns')
    metrics_data = prom_real_time_data.get_metrics_data(env_value, namespace_value)
    final_data = prom_real_time_data.process_metrics_data(metrics_data)
    return web.json_response({'success': True, 'data': final_data})


async def prom_ns_handler(request):
    env_value = request.query.get('env')
    if not env_value:
        return web.json_response({'message': 'env query parameter is required'}, status=400)
    try:
        namespaces = utils.fetch_prom_namespaces(env_value)
        return web.json_response({'success': True, 'data': namespaces})
    except Exception as e:
        return web.json_response({'message': str(e)}, status=500)


async def prom_services_handler(request):
    env_value = request.query.get('env')
    namespace = request.query.get('namespace')
    if not env_value or not namespace:
        return web.json_response({'message': 'env and namespace query parameters are required'}, status=400)
    try:
        services = utils.fetch_prom_services(env_value, namespace)
        return web.json_response({'success': True, 'data': services})
    except Exception as e:
        return web.json_response({'message': str(e)}, status=500)


async def prom_env_handler(request):
    try:
        username = request.headers.get('X-User-Name', '')
        permission = request.headers.get('X-User-Permission', '')
        envs = utils.fetch_prom_envs()
        return web.json_response({'success': True, 'data': envs, 'username': username, 'permission': permission})
    except Exception as e:
        return web.json_response({'message': str(e), 'username': username, 'permission': permission}, status=500)


async def prom_node_rank_handler(request):
    env_value = request.query.get('env')
    res_type = request.query.get('type', 'cpu')
    namespace = request.query.get('namespace')
    deployment = request.query.get('deployment')

    if not env_value:
        return web.json_response({'message': 'env query parameter is required'}, status=400)

    try:

        deployment_node_dict = await utils.get_deployment_node(deployment_node, env_value, namespace, deployment)
        node_rank_data = await utils.get_node_res_rank(env_value, res_type)

        # 为node_rank_data的每个元素添加cpod_num字段
        for node_data in node_rank_data:
            node_name = node_data.get("name")
            # 从deployment_node_dict中获取对应节点的cpod_num值，如果不存在则设为0
            cpod_num_str = deployment_node_dict.get(node_name, "0")
            # 将字符串转换为数字
            try:
                cpod_num = int(cpod_num_str)
            except (ValueError, TypeError):
                cpod_num = 0
            node_data["cpod_num"] = cpod_num

        return web.json_response({'success': True, 'data': node_rank_data})
    except Exception as e:
        return web.json_response({'message': str(e)}, status=500)


async def prom_overview_handler(request):
    try:
        env = request.query.get('env')
        data = await prom_overview.get_overview_counts_async(env)
        return web.json_response({'success': True, 'data': data})
    except Exception as e:
        return web.json_response({'message': str(e)}, status=500)


async def agent_names(request):
    try:
        k8s_names = utils.ck_get_k8s_names()
        return web.json_response({'success': True, 'data': k8s_names})
    except Exception as e:
        return web.json_response({'message': str(e)}, status=500)


async def heartbeat_check():
    """定期检查客户端的心跳状态"""
    while True:
        for env, data in clients.items():
            if data["online"] and time.time() - data["last_heartbeat"] > 5:
                # 标记超时客户端为离线
                data["online"] = False
                logger.warning(f"客户端 env={env} 超时，标记为离线")
        await asyncio.sleep(3)


async def cron_peak_data(request):
    param_combinations = utils.ck_agent_collect_info()

    # 使用 streaming response 给客户端逐个返回响应
    async def stream_responses():
        for env, peak_hours in param_combinations:
            query_params = MultiDict([("env", env), ("peak_hours", peak_hours)])
            fake_request = request.clone()
            fake_request._rel_url = fake_request._rel_url.update_query(query_params)
            response = await init_peak_data(fake_request)
            # 解析 JSON 响应并确保中文字符不被转义
            response_json = json.loads(response.body.decode('utf-8'))
            json_str = json.dumps(response_json, ensure_ascii=False)
            # 将 JSON 字符串转换为字节对象再返回
            yield (json_str + '\n').encode('utf-8')

    # 返回流式响应
    return web.Response(
        content_type='application/json',  # 设置正确的 content-type
        charset='utf-8',  # 单独设置字符集
        body=stream_responses(),  # 使用 stream_responses 来逐个返回数据
    )


async def init_peak_data(request):
    """初始化/更新原始资源表k8s_resources，初始化/更新资源管控表k8s_res_control"""
    try:
        env_key = utils.PROM_K8S_TAG_KEY
        env_value = request.query.get("env")
        days = int(request.query.get("days", 2))  # 不传则采集昨天+今天
        peak_hours = request.query.get("peak_hours", "10:00:00-11:30:00")
        logger.info(f"🐛开始获取{env_value}，{days}天，每日【{peak_hours}】高峰期数据")
        duration_str, start_time_part, end_time_part = utils.calculate_peak_duration_and_end_time(peak_hours)

        for i in range(0, days):
            # 计算结束时间字符串
            current_date = datetime.now().date()
            start_time_full = datetime.combine(current_date, start_time_part) - timedelta(days=i)
            end_time_full = datetime.combine(current_date, end_time_part) - timedelta(days=i)
            if datetime.now() < end_time_full:
                logger.info(f"今天的高峰期还未结束，跳过{current_date}的数据采集")
                continue
            utils.check_and_delete_day_data(end_time_full, env_value)
            logger.info(f"🚀获取{end_time_full}的数据======")
            k8s_metrics_list = utils.merged_dict(env_key, env_value, duration_str, end_time_full)
            utils.metrics_to_ck(k8s_metrics_list)
        logger.info(f"🚀{env_value}: 高峰期数据采集流程结束,开始取最近10天cpu使用最高的一天pod数据, 写入管控表")

        # 采集完成后，取最近10天cpu数据最高的一天pod，数据写入管控表
        resources = utils.get_list_from_resources(env_value)
        if utils.is_init_or_update(env_value):
            # 初始化
            logger.info(f"🌊{env_value}: 初始化管控表======")
            flag = utils.init_control_data(resources)
            logger.info(f"✨{env_value}: 更新完成")
        else:
            # 更新
            logger.info(f"🌊{env_value}: 更新管控表======")
            flag = utils.update_control_data(resources)
            logger.info(f"✨{env_value}: 更新完成")

        if not flag:
            return web.json_response(
                {"message": f"{env_value}: 写入管控表执行失败，详情见kubedoor-master日志"},
                status=500,
            )
        return web.json_response({"success": True, "message": f"{env_value}: 执行完成"})
    except Exception as e:
        logger.error(f"Error in table: {e}")
        return web.json_response({"message": str(e)}, status=500)


async def start_background_tasks(app):
    """启动后台任务"""
    app["heartbeat_task"] = asyncio.create_task(heartbeat_check())


async def cleanup_background_tasks(app):
    """清理后台任务"""
    app["heartbeat_task"].cancel()
    await app["heartbeat_task"]


app = web.Application()
app.router.add_get("/ws", websocket_handler)
app.router.add_get("/ws/pod-logs", pod_logs_websocket_handler)
app.router.add_post('/api/sql', forward_request)
app.router.add_get("/api/prom_ns", prom_ns_handler)
app.router.add_get("/api/prom_env", prom_env_handler)
app.router.add_get("/api/prom_services", prom_services_handler)
app.router.add_get("/api/prom_query", prom_query_handler)
app.router.add_get("/api/prom_node_rank", prom_node_rank_handler)
app.router.add_get("/api/prom_overview", prom_overview_handler)
app.router.add_get("/api/ck_top10_events", ck_top_queries.top10_events_handler)
app.router.add_get("/api/ck_top10_pod_alerts", ck_top_queries.top10_pod_alerts_handler)
app.router.add_get("/api/ck_day10_alert_daily", ck_top_queries.alert_daily_stats_handler)
app.router.add_post("/api/image/tags", image_tags_fetcher.get_image_tags_handler)  # 8

# 查询K8S事件相关接口
app.router.add_post("/api/events/query", query_k8s_events_handler)  # 查询K8S事件
app.router.add_get("/api/events/menu", get_k8s_events_menu_options)  # 获取K8S事件查询菜单选项

# ==========需要rw权限==========
app.router.add_get("/api/agent_status", status_handler)  # 获取agent状态
app.router.add_get("/api/agent_names", agent_names)  # istio管理获取K8S列表
app.router.add_get("/api/init_peak_data", init_peak_data)
app.router.add_get("/api/cron_peak_data", cron_peak_data)


# ==================== Istio Route 路由注册 ====================
# VS级别接口 query: vs_id, k8s_cluster, namespace
app.router.add_get("/api/istio/vs", istio_route.get_vs_list_handler)  # 1
app.router.add_post("/api/istio/vs", istio_route.create_vs_handler)  # 3
app.router.add_put("/api/istio/vs", istio_route.update_vs_handler)  # 5
app.router.add_delete("/api/istio/vs", istio_route.delete_vs_handler)

# HTTP路由级别接口 query: route_id, vs_id
app.router.add_get("/api/istio/httproute", istio_route.get_routes_handler)  # 2
app.router.add_post("/api/istio/httproute", istio_route.create_route_handler)  # 4
app.router.add_put("/api/istio/httproute", istio_route.update_route_handler)  # 6
app.router.add_delete("/api/istio/httproute", istio_route.delete_route_handler)

# 路由管理辅助接口
app.router.add_post("/api/istio/httproute/reorder", istio_route.reorder_routes_handler)
app.router.add_get("/api/istio/health", istio_route.health_check_handler)

# K8S集群关联管理接口
app.router.add_post("/api/istio/vs/k8s", istio_route.update_k8s_vs_handler)  # 7


# ==================== 其它接口转发到各个agent ====================
app.router.add_route('*', "/api/{tail:.*}", http_handler)

# 在应用启动和关闭时管理后台任务
app.on_startup.append(start_background_tasks)
app.on_cleanup.append(cleanup_background_tasks)

if __name__ == '__main__':
    logger.info("🌻kubedoor-master is starting on port 80🚀...")
    web.run_app(app, host='0.0.0.0', port=80)
