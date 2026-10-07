"""
Deployment / Pod 实时状态推送:浏览器 ⇄ master ⇄ agent

浏览器连 /ws/workload-status?env=<集群>&namespace=<命名空间,可空>,master 给每个连接分配 sub_id,
把订阅(全量、幂等的 workload_sub)发给该 env 的 agent;agent 推回的 workload_snapshot / workload_update
按 sub_id 放进对应浏览器的队列,由每个连接自己的发送任务转出去(原样转发 agent 的 JSON 字符串)。

浏览器 → master:{"type": "watch_pods" | "unwatch_pods", "namespace", "deployment"}(展开 / 收起明细)
master → 浏览器:agent 的快照 / 增量消息,以及
               {"type": "status", "state": "syncing" | "agent_offline" | "unsupported" | "resyncing"}

注意:
- dispatch() 在 agent 的接收循环里调用,绝不能 await 浏览器(浏览器一慢就拖住心跳处理,5 秒被判离线),
  只 put_nowait;队列满说明浏览器跟不上,清空队列并让 agent 重发全量快照(每个订阅 5 秒最多一次);
- agent 重连后把该 env 的所有订阅重发一遍;旧版 agent 不认识这些消息,10 秒没快照就告诉页面"不支持"。
"""

import asyncio
import json
import time
import uuid

from aiohttp import WSMsgType, web
from loguru import logger

QUEUE_SIZE = 256
SEND_TIMEOUT = 10  # 推给浏览器的单条超时,超时就断开这个浏览器
SUB_DEBOUNCE = 0.1  # 展开/收起明细后多久把新订阅发给 agent(合并连续操作)
SNAPSHOT_WAIT = 10  # 订阅后多久没收到快照算 agent 不支持
RESYNC_INTERVAL = 5
MAX_PODS_FOR = 20  # 与 agent 的上限一致
MAX_SUBSCRIBERS_PER_ENV = 50  # 与 agent 的订阅上限一致,超出时拒绝新连接,免得 agent 淘汰旧订阅


class _Subscriber:
    def __init__(self, sub_id, env, namespace, ws):
        self.sub_id = sub_id
        self.env = env
        self.namespace = namespace
        self.ws = ws
        self.pods_for = set()
        self.queue = asyncio.Queue(maxsize=QUEUE_SIZE)
        self.got_snapshot = False
        self.last_resync = 0.0
        self.sender = None
        self.debounce = None
        self.resync_task = None
        self.snapshot_check = None

    def subscribe_message(self, resync=False):
        return {
            "type": "workload_sub",
            "sub_id": self.sub_id,
            "namespace": self.namespace,
            "pods_for": [list(key) for key in sorted(self.pods_for)],
            "resync": resync,
        }

    def cancel_timers(self):
        for task in (self.sender, self.debounce, self.resync_task):
            if task is not None and not task.done():
                task.cancel()
        if self.snapshot_check is not None:
            self.snapshot_check.cancel()


class WorkloadRelay:
    def __init__(self, clients):
        self.clients = clients  # kubedoor-master 的 agent 注册表:env -> {"ws", "online", ...}
        self.subs = {}  # sub_id -> _Subscriber
        self._unknown_unsub = {}  # sub_id -> 上次回 unsub 的时间,限频

    # ------------------------------------------------------------------ agent 侧
    def _agent_ws(self, env):
        # 不看 online 标记:心跳 5 秒判离线会抖动,连接本身还在就能发
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
            logger.warning(f"[workload-relay] 发送订阅到 agent 失败 env={env}: {exc}")
            return False

    async def _subscribe_agent(self, sub, resync=False):
        if sub.sub_id not in self.subs:
            return
        if await self._send_agent(sub.env, sub.subscribe_message(resync)) and not sub.got_snapshot:
            if sub.snapshot_check is not None:
                sub.snapshot_check.cancel()
            sub.snapshot_check = asyncio.get_running_loop().call_later(SNAPSHOT_WAIT, self._snapshot_timeout, sub.sub_id)

    def _snapshot_timeout(self, sub_id):
        sub = self.subs.get(sub_id)
        if sub is not None and not sub.got_snapshot:
            self._push(sub, {"type": "status", "state": "unsupported"})

    async def on_agent_connected(self, env):
        """agent (重新)连上:把这个 env 的订阅全部重发"""
        for sub in [s for s in self.subs.values() if s.env == env]:
            sub.got_snapshot = False
            self._push(sub, {"type": "status", "state": "syncing"})
            await self._subscribe_agent(sub)

    def on_agent_disconnected(self, env, ws):
        client = self.clients.get(env)
        if client is not None and client.get("ws") is not ws:
            return  # 已经换成新连接了
        for sub in [s for s in self.subs.values() if s.env == env]:
            sub.got_snapshot = False
            if sub.snapshot_check is not None:
                sub.snapshot_check.cancel()
            self._push(sub, {"type": "status", "state": "agent_offline"})

    def dispatch(self, env, data, raw):
        """agent 推来的 workload_snapshot / workload_update,在 agent 接收循环里同步调用"""
        sub_id = data.get("sub_id")
        sub = self.subs.get(sub_id)
        if sub is None or sub.env != env:
            self._unsub_unknown(env, sub_id)
            return
        if data.get("type") == "workload_snapshot" and not sub.got_snapshot:
            sub.got_snapshot = True
            if sub.snapshot_check is not None:
                sub.snapshot_check.cancel()
        self._push(sub, raw)

    def _unsub_unknown(self, env, sub_id):
        # 浏览器已经断开,让 agent 别再推这个订阅(限频,免得每条消息都回一次)
        if not isinstance(sub_id, str):
            return
        now = time.monotonic()
        if now - self._unknown_unsub.get(sub_id, 0) < RESYNC_INTERVAL:
            return
        if len(self._unknown_unsub) > 1000:
            self._unknown_unsub.clear()
        self._unknown_unsub[sub_id] = now
        asyncio.create_task(self._send_agent(env, {"type": "workload_unsub", "sub_id": sub_id}))

    # ------------------------------------------------------------------ 浏览器侧
    def _push(self, sub, item):
        try:
            sub.queue.put_nowait(item)
            return
        except asyncio.QueueFull:
            pass
        # 浏览器跟不上:丢掉积压的消息,让 agent 重发全量快照
        while not sub.queue.empty():
            sub.queue.get_nowait()
        sub.queue.put_nowait({"type": "status", "state": "resyncing"})
        if sub.resync_task is None or sub.resync_task.done():
            delay = max(0.0, RESYNC_INTERVAL - (time.monotonic() - sub.last_resync))
            sub.resync_task = asyncio.create_task(self._resync_later(sub, delay))

    async def _resync_later(self, sub, delay):
        await asyncio.sleep(delay)
        sub.last_resync = time.monotonic()
        await self._subscribe_agent(sub, resync=True)

    async def _sender(self, sub):
        try:
            while True:
                item = await sub.queue.get()
                if isinstance(item, str):
                    await asyncio.wait_for(sub.ws.send_str(item), SEND_TIMEOUT)
                else:
                    await asyncio.wait_for(sub.ws.send_json(item), SEND_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.info(f"[workload-relay] 推送到浏览器失败,断开 sub={sub.sub_id}: {exc}")
            await sub.ws.close()

    def _handle_browser_message(self, sub, text):
        try:
            msg = json.loads(text)
        except ValueError:
            return
        if not isinstance(msg, dict) or msg.get("type") not in ("watch_pods", "unwatch_pods"):
            return
        namespace, deployment = msg.get("namespace"), msg.get("deployment")
        if not (isinstance(namespace, str) and namespace and isinstance(deployment, str) and deployment):
            return
        key = (namespace, deployment)
        if msg["type"] == "watch_pods":
            if key in sub.pods_for or len(sub.pods_for) >= MAX_PODS_FOR:
                return
            sub.pods_for.add(key)
        else:
            if key not in sub.pods_for:
                return
            sub.pods_for.discard(key)
        if sub.debounce is not None and not sub.debounce.done():
            sub.debounce.cancel()
        sub.debounce = asyncio.create_task(self._debounced_subscribe(sub))

    async def _debounced_subscribe(self, sub):
        await asyncio.sleep(SUB_DEBOUNCE)
        await self._subscribe_agent(sub)

    async def handle_browser_ws(self, request):
        """GET /ws/workload-status?env=&namespace="""
        env = request.query.get("env")
        if not env:
            return web.json_response({"error": "缺少 env 参数"}, status=400)
        namespace = request.query.get("namespace", "")
        if sum(1 for s in self.subs.values() if s.env == env) >= MAX_SUBSCRIBERS_PER_ENV:
            return web.json_response({"error": "该集群的实时连接数已达上限"}, status=503)

        # heartbeat:定期 ping,及时清掉休眠/断网的浏览器,也让 nginx 不会因空闲断开
        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=64 * 1024)
        await ws.prepare(request)
        sub = _Subscriber(uuid.uuid4().hex, env, namespace, ws)
        self.subs[sub.sub_id] = sub
        sub.sender = asyncio.create_task(self._sender(sub))
        try:
            if self._agent_ws(env) is not None:
                self._push(sub, {"type": "status", "state": "syncing"})
                await self._subscribe_agent(sub)
            else:
                self._push(sub, {"type": "status", "state": "agent_offline"})
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    self._handle_browser_message(sub, msg.data)
        finally:
            self.subs.pop(sub.sub_id, None)
            sub.cancel_timers()
            await self._send_agent(env, {"type": "workload_unsub", "sub_id": sub.sub_id})
        return ws

    def register_routes(self, app):
        app.router.add_get("/ws/workload-status", self.handle_browser_ws)
