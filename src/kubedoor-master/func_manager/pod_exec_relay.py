"""
Pod 终端(web shell):浏览器 ⇄ master ⇄ agent

浏览器连 /ws/pod-exec?env=&namespace=&pod_name=&container=&shell=&cols=&rows=(需要目标集群读写权限,见 auth/policy.py),
master 分配 session_id,让该 env 的 agent 在容器里打开 exec(agent 侧见 kubedoor-agent 的 func_manager/pod_exec.py);
agent 推回的消息按 session_id 放进对应浏览器的队列,由每个连接自己的发送任务转出去。

浏览器 → master:{"type": "stdin", "data", "binary"?} / {"type": "resize", "cols", "rows"} / {"type": "ack", "bytes"}
master → 浏览器:二进制帧是终端输出;文本帧 {"type": "started", "container", "containers"} /
               {"type": "exit", "code", "message"}(shell 结束)/ {"type": "error", "message"}(没连上或被断开)

注意:
- dispatch() 在 agent 的接收循环里调用,只 put_nowait,绝不 await 浏览器;
- 流控是端到端的:浏览器按 xterm 实际写完的字节 ack,agent 未确认字节超过窗口就停止读容器输出,所以队列正常不会积压;
  真积压说明浏览器已经收不动了(卡死、断网),直接断开;
- 不看 clients[env]["online"]:心跳 5 秒判离线会抖动,连接本身还在就能用;
- 旧版 agent 不认识 pod_exec,REPLY_WAIT 秒没有任何回应就提示升级 agent;
- 节点终端临时 pod 的镜像取 master 环境变量 NODE_SHELL_IMAGE(可选),随 start 消息下发给 agent;
  没配就不带,agent 用内置默认镜像,所以改镜像不需要动各集群的 agent。
"""

import asyncio
import base64
import json
import os
import re
import time
import uuid

from aiohttp import WSMsgType, web
from loguru import logger

from auth import audit, client_ip, principal_of

QUEUE_SIZE = 4096
SEND_TIMEOUT = 10  # 推给浏览器的单条超时,超时就断开这个浏览器
REPLY_WAIT = 10  # 发出 start 后多久没有任何回应算 agent 不支持
UNKNOWN_STOP_INTERVAL = 5
MAX_SESSIONS_PER_ENV = 50  # 与 agent 的上限一致
MAX_SESSIONS_PER_USER = 10
MAX_COLS, MAX_ROWS = 1000, 500
SHELLS = ("auto", "bash", "ash", "sh")
_NAME = re.compile(r"[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?")  # namespace / pod 名(DNS-1123)
_CONTAINER = re.compile(r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?")


def _query_size(value, default, upper):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if 0 < number <= upper else default


class _Session:
    def __init__(self, session_id, env, ws, username):
        self.session_id = session_id
        self.env = env
        self.ws = ws
        self.username = username
        self.queue = asyncio.Queue(maxsize=QUEUE_SIZE)
        self.replied = False
        self.ended = False  # agent 那边已经没有这个会话了(shell 退出 / agent 断开),关闭时不用再发 stop
        self.closing = False  # 最后一条(exit / error)已经入队,之后的消息丢弃
        self.sender = None
        self.reply_check = None

    def cancel_timers(self):
        if self.sender is not None and not self.sender.done():
            self.sender.cancel()
        if self.reply_check is not None:
            self.reply_check.cancel()


class PodExecRelay:
    def __init__(self, clients):
        self.clients = clients  # kubedoor-master 的 agent 注册表:env -> {"ws", "online", ...}
        self.sessions = {}  # session_id -> _Session
        self._unknown_stop = {}  # session_id -> 上次回 stop 的时间,限频

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
            logger.warning(f"[pod-exec] 发送到 agent 失败 env={env}: {exc}")
            return False

    def dispatch(self, env, data):
        """agent 推来的 pod_exec 消息,在 agent 接收循环里同步调用"""
        session_id = data.get("session_id")
        session = self.sessions.get(session_id)
        if session is None or session.env != env:
            if data.get("event") != "exit":
                self._stop_unknown(env, session_id)
            return
        if not session.replied:
            session.replied = True
            if session.reply_check is not None:
                session.reply_check.cancel()
        event = data.get("event")
        if event == "output":
            payload = data.get("data")
            if isinstance(payload, str) and payload:
                self._push(session, payload)
        elif event == "started":
            containers = data.get("containers")
            self._push(session, {"type": "started", "container": str(data.get("container") or ""),
                                 "containers": containers if isinstance(containers, list) else []})
        elif event == "exit":
            session.ended = True
            code = data.get("code")
            self._push(session, {"type": "exit", "code": code if type(code) is int else None,
                                 "message": str(data.get("message") or "")})

    def _stop_unknown(self, env, session_id):
        # 浏览器已经断开:让 agent 关掉这个会话(限频,免得每条输出都回一次)
        if not isinstance(session_id, str):
            return
        now = time.monotonic()
        if now - self._unknown_stop.get(session_id, 0) < UNKNOWN_STOP_INTERVAL:
            return
        if len(self._unknown_stop) > 1000:
            self._unknown_stop.clear()
        self._unknown_stop[session_id] = now
        asyncio.create_task(self._send_agent(env, {"type": "pod_exec", "action": "stop", "session_id": session_id}))

    def on_agent_disconnected(self, env, ws):
        client = self.clients.get(env)
        if client is not None and client.get("ws") is not ws:
            return  # 已经换成新连接了
        for session in [s for s in self.sessions.values() if s.env == env]:
            session.ended = True  # agent 断线时会关掉它那边的全部会话
            self._push(session, {"type": "error", "message": "与 agent 的连接已断开,终端已关闭"})

    # ------------------------------------------------------------------ 浏览器侧
    def _push(self, session, item):
        if session.closing:
            return
        if isinstance(item, dict) and item["type"] in ("exit", "error"):
            session.closing = True
        try:
            session.queue.put_nowait(item)
        except asyncio.QueueFull:
            logger.warning(f"[pod-exec] 浏览器接收过慢,断开 session={session.session_id}")
            while not session.queue.empty():
                session.queue.get_nowait()
            session.closing = True
            session.queue.put_nowait({"type": "error", "message": "浏览器接收过慢,终端已断开"})

    async def _sender(self, session):
        try:
            while True:
                item = await session.queue.get()
                if isinstance(item, str):
                    await asyncio.wait_for(session.ws.send_bytes(base64.b64decode(item)), SEND_TIMEOUT)
                    continue
                await asyncio.wait_for(session.ws.send_json(item), SEND_TIMEOUT)
                if item["type"] in ("exit", "error"):
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.info(f"[pod-exec] 推送到浏览器失败,断开 session={session.session_id}: {exc}")
        await session.ws.close()

    def _reply_timeout(self, session_id):
        session = self.sessions.get(session_id)
        if session is not None and not session.replied:
            self._push(session, {"type": "error",
                                 "message": "agent 没有响应终端请求,可能版本过旧不支持 Shell,请升级 kubedoor-agent"})

    async def _handle_browser_message(self, session, text):
        try:
            msg = json.loads(text)
        except ValueError:
            return
        if not isinstance(msg, dict) or session.closing:
            return
        kind = msg.get("type")
        forward = {"type": "pod_exec", "session_id": session.session_id}
        if kind == "stdin":
            data = msg.get("data")
            if not isinstance(data, str) or not data:
                return
            forward.update(action="stdin", data=data)
            if msg.get("binary") is True:
                forward["binary"] = True
        elif kind == "resize":
            cols, rows = msg.get("cols"), msg.get("rows")
            if not (type(cols) is int and 0 < cols <= MAX_COLS and type(rows) is int and 0 < rows <= MAX_ROWS):
                return
            forward.update(action="resize", cols=cols, rows=rows)
        elif kind == "ack":
            nbytes = msg.get("bytes")
            if type(nbytes) is not int or nbytes <= 0:
                return
            forward.update(action="ack", bytes=nbytes)
        else:
            return
        await self._send_agent(session.env, forward)

    async def _serve(self, request, env, start_payload, target, audit_action, audit_detail):
        """浏览器连进来后的公共流程:限额检查 → 建会话 → 下发 start → 转发浏览器消息 → 收尾"""
        username = principal_of(request).username
        # heartbeat:定期 ping,及时清掉休眠/断网的浏览器,也让 nginx 不会因空闲断开
        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=64 * 1024)
        await ws.prepare(request)
        # 握手之后再报错:握手失败时浏览器拿不到原因
        error = None
        if self._agent_ws(env) is None:
            error = f"集群 {env} 的 agent 不在线"
        elif sum(1 for s in self.sessions.values() if s.env == env) >= MAX_SESSIONS_PER_ENV:
            error = f"集群 {env} 同时打开的终端已达上限({MAX_SESSIONS_PER_ENV} 个)"
        elif sum(1 for s in self.sessions.values() if s.username == username) >= MAX_SESSIONS_PER_USER:
            error = f"每个账号最多同时打开 {MAX_SESSIONS_PER_USER} 个终端"
        if error:
            await ws.send_json({"type": "error", "message": error})
            await ws.close()
            return ws

        session = _Session(uuid.uuid4().hex, env, ws, username)
        self.sessions[session.session_id] = session
        session.sender = asyncio.create_task(self._sender(session))
        start = {**start_payload, "session_id": session.session_id}
        logger.info(f"[pod-exec] {username} 打开终端 env={env} {target} session={session.session_id}")
        audit.record_bg(username, audit_action, env=env, target=target, ip=client_ip(request),
                        detail={"session_id": session.session_id, **audit_detail})
        opened_at = time.monotonic()
        try:
            if await self._send_agent(env, start):
                session.reply_check = asyncio.get_running_loop().call_later(
                    REPLY_WAIT, self._reply_timeout, session.session_id)
            else:
                session.ended = True
                self._push(session, {"type": "error", "message": f"发送到集群 {env} 的 agent 失败"})
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    await self._handle_browser_message(session, msg.data)
        finally:
            self.sessions.pop(session.session_id, None)
            session.cancel_timers()
            if not session.ended:
                await self._send_agent(env, {"type": "pod_exec", "action": "stop", "session_id": session.session_id})
            logger.info(f"[pod-exec] 终端关闭 session={session.session_id},持续 {time.monotonic() - opened_at:.0f} 秒")
        return ws

    async def handle_browser_ws(self, request):
        """GET /ws/pod-exec?env=&namespace=&pod_name=&container=&shell=&cols=&rows="""
        query = request.query
        env, namespace, pod_name = query.get("env", ""), query.get("namespace", ""), query.get("pod_name", "")
        container, shell = query.get("container", ""), query.get("shell", "auto")
        if (not env or not _NAME.fullmatch(namespace) or not _NAME.fullmatch(pod_name)
                or (container and not _CONTAINER.fullmatch(container)) or shell not in SHELLS):
            return web.json_response({"error": "参数错误"}, status=400)
        start = {"type": "pod_exec", "action": "start", "kind": "pod", "namespace": namespace, "pod_name": pod_name,
                 "container": container, "shell": shell,
                 "cols": _query_size(query.get("cols"), 80, MAX_COLS),
                 "rows": _query_size(query.get("rows"), 24, MAX_ROWS)}
        target = f"{namespace}/{pod_name}" + (f"/{container}" if container else "")
        return await self._serve(request, env, start, target, "pod.exec", {"shell": shell})

    async def handle_node_ws(self, request):
        """GET /ws/node-exec?env=&node=&shell=&cols=&rows=(节点终端:agent 建临时 pod 后 nsenter 进宿主机)"""
        query = request.query
        env, node, shell = query.get("env", ""), query.get("node", ""), query.get("shell", "auto")
        if not env or not _NAME.fullmatch(node) or shell not in SHELLS:
            return web.json_response({"error": "参数错误"}, status=400)
        start = {"type": "pod_exec", "action": "start", "kind": "node", "node_name": node, "shell": shell,
                 "cols": _query_size(query.get("cols"), 80, MAX_COLS),
                 "rows": _query_size(query.get("rows"), 24, MAX_ROWS)}
        # 临时 pod 的镜像由 master 配置(kubedoor-config 的 NODE_SHELL_IMAGE),每次开终端随消息下发;
        # 没配就不带,agent 用内置默认,这样升级 agent 只需要换镜像,不用改各集群 agent 的配置
        image = os.environ.get("NODE_SHELL_IMAGE", "").strip()
        if image:
            start["image"] = image
        return await self._serve(request, env, start, f"node/{node}", "node.exec", {"shell": shell})

    def register_routes(self, app):
        app.router.add_get("/ws/pod-exec", self.handle_browser_ws)
        app.router.add_get("/ws/node-exec", self.handle_node_ws)
