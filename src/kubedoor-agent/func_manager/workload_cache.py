"""
Deployment / Pod 的 list+watch 内存缓存(informer 模式)

- 启动后分页 list 全集群对象,再从 list 返回的 resourceVersion 开始 watch,增量维护;
- 410 Gone(resourceVersion 过期)时重新 list,其它异常退避后从最后的 resourceVersion 续上;
- 内存里只存 workload_status 压出来的精简 dict,并按 (namespace, deployment) 建 pod 索引;
- synced 为 False(刚启动 / watch 断开)时数据可能不全,调用方应回退到直接查 K8S API。

不用 kubernetes_asyncio.watch.Watch:它对每个事件还要 dumps/loads 一次,410 和 bookmark 的处理
也随版本变化(requirements 没锁版本),这里直接逐行读 watch 响应自己解析。
agent 的心跳和其它请求都跑在同一个事件循环上(master 5 秒收不到心跳就判离线),
所以 list 分页、处理事件时定期 sleep(0) 让出事件循环。
"""

import asyncio
import json
import random

from kubernetes_asyncio.client.rest import ApiException
from loguru import logger

from func_manager.workload_status import slim_deployment, slim_pod, status_row

PAGE_SIZE = 500
WATCH_TIMEOUT = 120  # 服务端 watch 超时(秒),到点正常断开后续 watch
YIELD_EVERY = 100  # 每处理多少个事件让出一次事件循环
MAX_BACKOFF = 30


class WorkloadCache:
    def __init__(self, core_v1, apps_v1):
        self._list_fns = {
            "pods": core_v1.list_pod_for_all_namespaces,
            "deployments": apps_v1.list_deployment_for_all_namespaces,
        }
        self._pods = {}  # (ns, name) -> 精简 pod
        self._by_dep = {}  # (ns, deployment) -> {pod name}
        self._deps = {}  # (ns, name) -> 精简 deployment
        self._synced = {kind: False for kind in self._list_fns}
        self._rv = {kind: None for kind in self._list_fns}
        self._listeners = []
        self._tasks = []

    # ------------------------------------------------------------------ 对外
    @property
    def synced(self):
        return all(self._synced.values())

    def add_listener(self, listener):
        """listener 需要实现 on_change(key, pods_changed, removed) / on_resync() / on_synced(synced),都是同步调用"""
        self._listeners.append(listener)

    def get_deployment(self, namespace, name):
        return self._deps.get((namespace, name))

    def pods_of(self, namespace, deployment):
        """归属这个 deployment 的精简 pod(含被隔离的),按名字排序"""
        names = self._by_dep.get((namespace, deployment)) or ()
        return [self._pods[(namespace, name)] for name in sorted(names) if (namespace, name) in self._pods]

    def deployment_keys(self, namespace=""):
        """namespace 为空时返回全部 deployment"""
        return sorted(key for key in self._deps if not namespace or key[0] == namespace)

    def status_row(self, namespace, name):
        dep = self._deps.get((namespace, name))
        if dep is None:
            return None
        return status_row(dep, self.pods_of(namespace, name))

    def start(self):
        if not self._tasks:
            self._tasks = [asyncio.create_task(self._run(kind), name=f"workload-cache-{kind}") for kind in self._list_fns]
            logger.info("[workload-cache] 开始 list+watch Deployment / Pod")

    async def stop(self):
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    # ------------------------------------------------------------------ list / watch 主循环
    async def _run(self, kind):
        backoff = 1
        while True:
            try:
                if self._rv[kind] is None:
                    items, rv = await self._list(kind)
                    self._replace(kind, items)
                    self._rv[kind] = rv
                    self._set_synced(kind, True)
                    logger.info(f"[workload-cache] {kind} 同步完成,共 {len(items)} 个")
                await self._watch(kind)
                backoff = 1
            except asyncio.CancelledError:
                raise
            except ApiException as exc:
                if exc.status == 410:
                    logger.info(f"[workload-cache] {kind} resourceVersion 已过期,重新 list")
                    self._rv[kind] = None
                    continue
                logger.warning(f"[workload-cache] {kind} list/watch 失败({exc.status} {exc.reason}),{backoff}s 后重试")
                self._set_synced(kind, False)
                await asyncio.sleep(backoff + random.random())
                backoff = min(backoff * 2, MAX_BACKOFF)
            except Exception as exc:
                logger.warning(f"[workload-cache] {kind} list/watch 异常({type(exc).__name__}: {exc}),{backoff}s 后重试")
                self._set_synced(kind, False)
                await asyncio.sleep(backoff + random.random())
                backoff = min(backoff * 2, MAX_BACKOFF)

    async def _list(self, kind):
        """分页 list,返回 ({key: 精简对象}, resourceVersion)"""
        slim = slim_pod if kind == "pods" else slim_deployment
        items = {}
        cont = None
        while True:
            kwargs = {"limit": PAGE_SIZE, "_preload_content": False, "_request_timeout": 60}
            if cont:
                kwargs["_continue"] = cont
            body = await self._request(self._list_fns[kind], kwargs)
            data = json.loads(body)
            for obj in data.get("items") or []:
                rec = slim(obj)
                items[(rec["namespace"], rec["name"])] = rec
            await asyncio.sleep(0)
            meta = data.get("metadata") or {}
            cont = meta.get("continue")
            if not cont:
                return items, meta.get("resourceVersion")

    @staticmethod
    async def _request(list_fn, kwargs):
        # _preload_content=False 时 kubernetes_asyncio 不会对非 2xx 抛异常,要自己判断
        resp = await list_fn(**kwargs)
        try:
            body = await resp.read()
            if resp.status != 200:
                raise ApiException(status=resp.status, reason=body[:300].decode("utf-8", "replace"))
            return body
        finally:
            resp.release()

    async def _watch(self, kind):
        """从 self._rv[kind] 开始 watch,服务端到点正常断开时返回(外层循环会接着 watch)"""
        timeout = WATCH_TIMEOUT + random.randint(0, 30)
        resp = await self._list_fns[kind](
            watch=True,
            resource_version=self._rv[kind],
            allow_watch_bookmarks=True,
            timeout_seconds=timeout,
            _preload_content=False,
            # sock_read 比服务端超时多 30 秒:连接静默断掉时也能发现
            _request_timeout=(10, timeout + 30),
        )
        try:
            if resp.status != 200:
                body = await resp.read()
                raise ApiException(status=resp.status, reason=body[:300].decode("utf-8", "replace"))
            self._set_synced(kind, True)
            handled = 0
            while True:
                line = await resp.content.readline()
                if not line:
                    return
                event = json.loads(line)
                etype = event.get("type")
                obj = event.get("object") or {}
                if etype == "ERROR":
                    raise ApiException(status=obj.get("code"), reason=f"{obj.get('reason')}: {obj.get('message')}")
                if etype in ("ADDED", "MODIFIED", "DELETED"):
                    self._apply(kind, etype, obj)
                elif etype != "BOOKMARK":
                    # 不认识的行(不是 watch 事件)直接跳过,别把它当成对象存进缓存
                    logger.debug(f"[workload-cache] {kind} 忽略未知 watch 事件: {line[:200]!r}")
                    continue
                rv = (obj.get("metadata") or {}).get("resourceVersion")
                if rv:
                    self._rv[kind] = rv
                handled += 1
                if handled % YIELD_EVERY == 0:
                    await asyncio.sleep(0)
        finally:
            resp.release()

    # ------------------------------------------------------------------ 存储
    def _replace(self, kind, items):
        if kind == "pods":
            self._pods = items
            self._by_dep = {}
            for rec in items.values():
                self._index(rec)
        else:
            self._deps = items
        for listener in self._listeners:
            listener.on_resync()

    def _set_synced(self, kind, value):
        before = self.synced
        self._synced[kind] = value
        if self.synced != before:
            for listener in self._listeners:
                listener.on_synced(self.synced)

    def _index(self, rec):
        if rec["deployment"]:
            self._by_dep.setdefault((rec["namespace"], rec["deployment"]), set()).add(rec["name"])

    def _unindex(self, rec):
        if rec["deployment"]:
            key = (rec["namespace"], rec["deployment"])
            names = self._by_dep.get(key)
            if names is not None:
                names.discard(rec["name"])
                if not names:
                    del self._by_dep[key]

    def _notify(self, key, pods_changed=False, removed=False):
        for listener in self._listeners:
            listener.on_change(key, pods_changed, removed)

    def _apply(self, kind, etype, obj):
        if kind == "pods":
            self._apply_pod(etype, obj)
        else:
            self._apply_deployment(etype, obj)

    def _apply_pod(self, etype, obj):
        rec = slim_pod(obj)
        key = (rec["namespace"], rec["name"])
        old = self._pods.get(key)
        if etype == "DELETED":
            if old is not None:
                del self._pods[key]
                self._unindex(old)
                if old["deployment"]:
                    self._notify((old["namespace"], old["deployment"]), pods_changed=True)
            return
        if old == rec:
            # 只是 resourceVersion / annotations 之类页面不关心的字段变了
            return
        self._pods[key] = rec
        if old is not None and old["deployment"] != rec["deployment"]:
            self._unindex(old)
            if old["deployment"]:
                self._notify((old["namespace"], old["deployment"]), pods_changed=True)
        self._index(rec)
        if rec["deployment"]:
            self._notify((rec["namespace"], rec["deployment"]), pods_changed=True)

    def _apply_deployment(self, etype, obj):
        rec = slim_deployment(obj)
        key = (rec["namespace"], rec["name"])
        if etype == "DELETED":
            if self._deps.pop(key, None) is not None:
                self._notify(key, removed=True)
            return
        if self._deps.get(key) == rec:
            return
        self._deps[key] = rec
        self._notify(key)
