"""
Pod 实时日志:浏览器 ⇄ master ⇄ agent

浏览器连 /ws/pod-logs?env=&namespace=&pod_name=&container=,master 分配 connection_id(uuid),
让该 env 的 agent follow 容器日志(agent 侧见 kubedoor-agent 的 func_manager/pod_log_stream.py);
agent 推回的 pod_logs 消息按 connection_id 放进对应浏览器的队列,由每个连接自己的发送任务原样转出去。

master → agent:{"type": "start_pod_logs", "connection_id", "namespace", "pod_name", "container"}
               {"type": "stop_pod_logs", "connection_id"}
agent → master → 浏览器:{"type": "pod_logs", "connection_id", "status" | "log" | "error", ...}

注意:
- dispatch() 在 agent 的接收循环里调用,只 put_nowait,绝不 await 浏览器。以前这里直接 await 浏览器发送,
  浏览器一卡(网络差、笔记本合盖、页面卡顿),整条 agent 连接的心跳、回包、K8S 事件就全部停住;
- 队列满说明浏览器收不动了:断开这个浏览器(关闭时会让 agent 停掉日志流);
- 浏览器断开时一定通知 agent 停止,不看 online 标记(心跳判离线可能有延迟,连接本身还在就能发);
  收到不认识的 connection_id 的日志(浏览器早断了、stop 丢了)也回一次 stop(限频),agent 侧不会留下没人收的日志流。
  以前 stop 只在 online 时才发,connection_id 用秒级时间戳还会撞车,agent 上的日志流越积越多,最终占满它的 K8S 连接池;
- agent 断开时 agent 那边会停掉全部日志流,这里告诉浏览器并断开(单 Pod 模式的页面会自动重连)。
"""

import asyncio
import json
import time
import uuid

from aiohttp import WSMsgType, web
from loguru import logger

QUEUE_SIZE = 1024
SEND_TIMEOUT = 10  # 推给浏览器的单条超时,超时就断开这个浏览器
UNKNOWN_STOP_INTERVAL = 5
MAX_CONNECTIONS_PER_ENV = 200  # 与 agent 的日志流上限一致
_CLOSE = object()  # 发送任务看到它就断开浏览器


def _error(connection_id, message):
    return json.dumps({"type": "pod_logs", "connection_id": connection_id, "error": message})


class _LogConnection:
    def __init__(self, connection_id, env, ws):
        self.connection_id = connection_id
        self.env = env
        self.ws = ws
        self.queue = asyncio.Queue(maxsize=QUEUE_SIZE)
        self.closing = False  # 最后一条已经入队,之后的消息丢弃
        self.ended = False  # agent 那边已经没有这个日志流了(agent 断开 / 没发出去),关闭时不用再发 stop
        self.sender = None


class PodLogRelay:
    def __init__(self, clients):
        self.clients = clients  # kubedoor-master 的 agent 注册表:env -> {"ws", "online", ...}
        self.conns = {}  # connection_id -> _LogConnection
        self._unknown_stop = {}  # connection_id -> 上次回 stop 的时间,限频

    # ------------------------------------------------------------------ agent 侧
    def _agent_ws(self, env):
        client = self.clients.get(env)
        ws = client.get("ws") if client else None
        return ws if ws is not None and not ws.closed else None

    async def _send_agent(self, env, message):
        ws = self._agent_ws(env)
        if ws is None:
            return False
        try:
            await ws.send_json(message)
            return True
        except Exception as exc:
            logger.warning(f"[pod-logs] 发送到 agent 失败 env={env}: {exc}")
            return False

    def dispatch(self, env, data, raw):
        """agent 推来的 pod_logs 消息,在 agent 接收循环里同步调用"""
        connection_id = data.get("connection_id")
        conn = self.conns.get(connection_id)
        if conn is None or conn.env != env:
            # 浏览器已经不在了;agent 那边已经结束的(error / disconnected)不用再 stop
            if "error" not in data and data.get("status") != "disconnected":
                self._stop_unknown(env, connection_id)
            return
        self._push(conn, raw)

    def dispatch_text(self, env, text):
        """旧版 agent 的纯文本日志(没有 connection_id):转给该 env 的全部日志连接"""
        for conn in [c for c in self.conns.values() if c.env == env]:
            self._push(conn, text)

    def _stop_unknown(self, env, connection_id):
        if not isinstance(connection_id, str):
            return
        now = time.monotonic()
        if now - self._unknown_stop.get(connection_id, 0) < UNKNOWN_STOP_INTERVAL:
            return
        if len(self._unknown_stop) > 1000:
            self._unknown_stop.clear()
        self._unknown_stop[connection_id] = now
        logger.info(f"[pod-logs] 收到已关闭连接的日志,通知 agent 停止 env={env} connection={connection_id}")
        asyncio.create_task(self._send_agent(env, {"type": "stop_pod_logs", "connection_id": connection_id}))

    def on_agent_disconnected(self, env, ws):
        client = self.clients.get(env)
        if client is not None and client.get("ws") is not ws:
            return  # 已经换成新连接了
        for conn in [c for c in self.conns.values() if c.env == env]:
            conn.ended = True  # agent 断线时会停掉它那边的全部日志流
            self._finish(conn, _error(conn.connection_id, "与 agent 的连接已断开,日志流已停止"))

    # ------------------------------------------------------------------ 浏览器侧
    def _push(self, conn, text):
        if conn.closing:
            return
        try:
            conn.queue.put_nowait(text)
        except asyncio.QueueFull:
            logger.warning(f"[pod-logs] 浏览器接收过慢,断开 connection={conn.connection_id}")
            self._finish(conn, _error(conn.connection_id, "浏览器接收日志过慢,连接已断开"))

    def _finish(self, conn, text):
        """最后一条消息:发完就断开浏览器"""
        if conn.closing:
            return
        conn.closing = True
        if conn.queue.maxsize - conn.queue.qsize() < 2:
            while not conn.queue.empty():
                conn.queue.get_nowait()
        conn.queue.put_nowait(text)
        conn.queue.put_nowait(_CLOSE)

    async def _sender(self, conn):
        try:
            while True:
                item = await conn.queue.get()
                if item is _CLOSE:
                    break
                await asyncio.wait_for(conn.ws.send_str(item), SEND_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.info(f"[pod-logs] 推送到浏览器失败,断开 connection={conn.connection_id}: {exc}")
        await conn.ws.close()

    async def handle_browser_ws(self, request):
        """GET /ws/pod-logs?env=&namespace=&pod_name=&container="""
        env = request.query.get("env")
        namespace = request.query.get("namespace")
        pod_name = request.query.get("pod_name")
        container = request.query.get("container", "")
        if not all([env, namespace, pod_name]):
            return web.json_response({"error": "缺少必要参数"}, status=400)
        client = self.clients.get(env)
        if not client or not client.get("online") or self._agent_ws(env) is None:
            return web.json_response({"error": "目标环境不在线"}, status=404)
        if sum(1 for c in self.conns.values() if c.env == env) >= MAX_CONNECTIONS_PER_ENV:
            return web.json_response({"error": f"该集群打开的日志连接已达上限({MAX_CONNECTIONS_PER_ENV} 个)"}, status=503)

        # heartbeat:定期 ping,及时清掉休眠/断网的浏览器(否则不会关闭,也就不会通知 agent 停止日志流)
        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=64 * 1024)
        await ws.prepare(request)
        conn = _LogConnection(uuid.uuid4().hex, env, ws)
        self.conns[conn.connection_id] = conn
        conn.sender = asyncio.create_task(self._sender(conn))
        logger.info(f"Pod日志连接建立: {conn.connection_id} env={env} {namespace}/{pod_name}")
        try:
            start = {"type": "start_pod_logs", "connection_id": conn.connection_id,
                     "namespace": namespace, "pod_name": pod_name, "container": container}
            if not await self._send_agent(env, start):
                conn.ended = True
                self._finish(conn, _error(conn.connection_id, f"发送到集群 {env} 的 agent 失败"))
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        if json.loads(msg.data).get("type") == "stop_logs":
                            break
                    except (ValueError, AttributeError):
                        logger.error(f"收到无法解析的前端消息：{msg.data}")
                elif msg.type == WSMsgType.ERROR:
                    logger.error(f"前端日志连接出错: {conn.connection_id}")
                    break
        finally:
            self.conns.pop(conn.connection_id, None)
            if conn.sender is not None and not conn.sender.done():
                conn.sender.cancel()
            if not conn.ended:
                await self._send_agent(env, {"type": "stop_pod_logs", "connection_id": conn.connection_id})
            logger.info(f"Pod日志连接关闭: {conn.connection_id}")
        return ws

    def register_routes(self, app):
        app.router.add_get("/ws/pod-logs", self.handle_browser_ws)
