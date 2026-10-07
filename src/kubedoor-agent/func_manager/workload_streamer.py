"""
Deployment / Pod 实时状态推送

管理 master 下发的订阅,把 WorkloadCache 的变化合并(每 0.4 秒一批)后经现有的 agent↔master
WebSocket 推出去,master 再按 sub_id 转给对应的浏览器。

消息格式:
  master→agent  {"type":"workload_sub","sub_id","namespace","pods_for":[[ns,dep],...],"resync":bool}
                全量、幂等:每次都带完整的期望状态,master 重连后直接重发
                {"type":"workload_unsub","sub_id"}
  agent→master  {"type":"workload_snapshot","sub_id","synced","first","final","deployments":[行],"pods":[...]}
                全量快照,deployment 多时分块发,first/final 标记首尾块
                {"type":"workload_update","sub_id","synced","deployments":[行],"removed":[[ns,dep]],"pods":[...]}
  pods 的每一项:{"namespace","deployment","items":[pod],"truncated":bool},items 是该 deployment 的全部 pod

每次连上 master(attach)都会清空订阅,由 master 重新下发;发送只走当前连接,不会写到旧连接上。
"""

import asyncio

from loguru import logger

from func_manager.workload_status import pod_view

FLUSH_INTERVAL = 0.4
MAX_SUBS = 50
MAX_PODS_FOR = 20
SNAPSHOT_CHUNK = 1000  # 每条快照消息最多带多少个 deployment
MAX_PODS_PER_DEP = 1000  # 每个 deployment 最多推多少个 pod(aiohttp WS 单条消息默认上限 4MiB)


class _Sub:
    __slots__ = ("sub_id", "namespace", "pods_for", "need_full", "pending_pods")

    def __init__(self, sub_id, namespace):
        self.sub_id = sub_id
        self.namespace = namespace
        self.pods_for = set()
        self.need_full = True  # 下次 flush 发全量快照
        self.pending_pods = set()  # 新增关注、需要先发一次 pod 全量的 deployment

    def matches(self, key):
        return not self.namespace or key[0] == self.namespace


class WorkloadStreamer:
    def __init__(self, cache):
        self.cache = cache
        cache.add_listener(self)
        self._ws = None
        self._subs = {}  # sub_id -> _Sub,按创建顺序(满了淘汰最旧的)
        self._dirty_deps = set()
        self._dirty_pods = set()
        self._removed = set()
        self._last_rows = {}  # 上次推出去的状态行,没变的行不重复推
        self._wake = asyncio.Event()
        self._task = None

    # ------------------------------------------------------------------ 连接
    def attach(self, ws):
        """连上 master 时调用:旧连接上的订阅全部作废,等 master 重新下发"""
        self._ws = ws
        self._reset()

    def detach(self):
        self._ws = None
        self._reset()

    def _reset(self):
        self._subs.clear()
        self._dirty_deps.clear()
        self._dirty_pods.clear()
        self._removed.clear()
        self._last_rows.clear()

    # ------------------------------------------------------------------ 订阅
    def subscribe(self, ws, msg):
        if ws is not self._ws:
            return
        sub_id = msg.get("sub_id")
        if not isinstance(sub_id, str) or not sub_id:
            return
        namespace = msg.get("namespace") or ""
        if not isinstance(namespace, str):
            return
        pods_for = set()
        for item in msg.get("pods_for") or []:
            if isinstance(item, (list, tuple)) and len(item) == 2 and all(isinstance(v, str) and v for v in item):
                pods_for.add((item[0], item[1]))
            if len(pods_for) >= MAX_PODS_FOR:
                break

        sub = self._subs.get(sub_id)
        if sub is None:
            if len(self._subs) >= MAX_SUBS:
                oldest = next(iter(self._subs))
                logger.warning(f"[workload-stream] 订阅数超过 {MAX_SUBS},丢弃最旧的 {oldest}")
                del self._subs[oldest]
            sub = _Sub(sub_id, namespace)
            sub.pending_pods = set(pods_for)
            self._subs[sub_id] = sub
        elif namespace != sub.namespace or msg.get("resync"):
            sub.namespace = namespace
            sub.need_full = True
            sub.pending_pods = set(pods_for)
        else:
            sub.pending_pods |= pods_for - sub.pods_for
        sub.pods_for = pods_for
        self._wake.set()

    def unsubscribe(self, sub_id):
        self._subs.pop(sub_id, None)

    # ------------------------------------------------------------------ cache 回调(同步、只做标记)
    def on_change(self, key, pods_changed, removed):
        if not self._subs:
            # 没人订阅时不攒变化;上次推过的值已经不可信,之后的订阅都从全量快照开始
            self._last_rows.pop(key, None)
            return
        self._dirty_deps.add(key)
        if pods_changed:
            self._dirty_pods.add(key)
        if removed:
            self._removed.add(key)
        self._wake.set()

    def on_resync(self):
        # 重新 list 过,中间丢了哪些变化不知道:所有订阅重发全量
        self._last_rows.clear()
        self._mark_all_full()

    def on_synced(self, synced):
        # 不同步期间的变化没有推,_last_rows 不再可信
        self._last_rows.clear()
        self._mark_all_full()

    def _mark_all_full(self):
        for sub in self._subs.values():
            sub.need_full = True
            sub.pending_pods = set(sub.pods_for)
        if self._subs:
            self._wake.set()

    # ------------------------------------------------------------------ 推送
    def start(self):
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="workload-streamer")

    async def stop(self):
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _loop(self):
        while True:
            await self._wake.wait()
            await asyncio.sleep(FLUSH_INTERVAL)
            self._wake.clear()
            try:
                await self.flush()
            except Exception:
                logger.exception("[workload-stream] 推送失败")

    async def flush(self):
        ws = self._ws
        dirty_deps, self._dirty_deps = self._dirty_deps, set()
        dirty_pods, self._dirty_pods = self._dirty_pods, set()
        removed, self._removed = self._removed, set()
        if not self._subs:
            self._last_rows.clear()
            return
        if ws is None or ws.closed:
            # 和 master 断开了,重连时 attach() 会清空订阅,重新从全量快照开始
            return
        synced = self.cache.synced

        # 本批变化的状态行,每行只算一次;和上次推过的一样就不推
        changed_rows = {}
        if synced:
            for key in dirty_deps - removed:
                row = self.cache.status_row(*key)
                if row is not None and row != self._last_rows.get(key):
                    changed_rows[key] = row
            for key in removed:
                self._last_rows.pop(key, None)
            self._last_rows.update(changed_rows)

        for sub in list(self._subs.values()):
            if ws is not self._ws:
                return
            if sub.need_full:
                sub.need_full = False
                sub.pending_pods.clear()
                if not await self._send_snapshot(ws, sub, synced):
                    return
                continue
            if not synced:
                continue
            pod_keys = (dirty_pods & sub.pods_for) | sub.pending_pods
            sub.pending_pods = set()
            payload = {
                "type": "workload_update",
                "sub_id": sub.sub_id,
                "synced": True,
                "deployments": [row for key, row in changed_rows.items() if sub.matches(key)],
                "removed": [list(key) for key in removed if sub.matches(key)],
                "pods": [self._pods_payload(key) for key in sorted(pod_keys)],
            }
            if payload["deployments"] or payload["removed"] or payload["pods"]:
                if not await self._send(ws, payload):
                    return

    async def _send_snapshot(self, ws, sub, synced):
        if not synced:
            # 缓存还没同步好(agent 刚启动),先告诉页面在同步,好了以后 on_synced 会再发全量
            return await self._send(
                ws,
                {"type": "workload_snapshot", "sub_id": sub.sub_id, "synced": False, "first": True, "final": True, "deployments": [], "pods": []},
            )
        keys = self.cache.deployment_keys(sub.namespace)
        rows = [row for row in (self.cache.status_row(*key) for key in keys) if row is not None]
        chunks = [rows[i : i + SNAPSHOT_CHUNK] for i in range(0, len(rows), SNAPSHOT_CHUNK)] or [[]]
        for i, chunk in enumerate(chunks):
            final = i == len(chunks) - 1
            payload = {
                "type": "workload_snapshot",
                "sub_id": sub.sub_id,
                "synced": True,
                "first": i == 0,
                "final": final,
                "deployments": chunk,
                "pods": [self._pods_payload(key) for key in sorted(sub.pods_for)] if final else [],
            }
            if not await self._send(ws, payload):
                return False
        return True

    def _pods_payload(self, key):
        pods = self.cache.pods_of(*key)
        return {
            "namespace": key[0],
            "deployment": key[1],
            "items": [pod_view(p, with_metrics=False) for p in pods[:MAX_PODS_PER_DEP]],
            "truncated": len(pods) > MAX_PODS_PER_DEP,
        }

    async def _send(self, ws, payload):
        if ws.closed:
            return False
        try:
            await ws.send_json(payload)
            return True
        except Exception as exc:
            logger.debug(f"[workload-stream] 发送失败: {exc}")
            return False
