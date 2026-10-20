"""
Pod 实时日志:master 转来的 start_pod_logs / stop_pod_logs,在 agent 上 follow 容器日志并推回 master

master → agent:{"type": "start_pod_logs", "connection_id", "namespace", "pod_name", "container"}
               {"type": "stop_pod_logs", "connection_id"}
agent → master:{"type": "pod_logs", "connection_id", "status": "connected"}
               {"type": "pod_logs", "connection_id", "pod_name", "container", "log": "行1\\n行2"}
               {"type": "pod_logs", "connection_id", "error": "..."}

- follow 日志是长连接,必须用单独的 CoreV1Api(自己的连接池)。以前和普通调用共用 core_v1 的连接池(上限 100),
  「全部 Pod」日志一次开 60 条,再加上没停掉的,池子一满,之后所有 core_v1 调用(命名空间、Pod 列表……)
  都永远等不到连接,接口全部超时、只能重启 agent;
- 同时打开的日志流有上限(MAX_STREAMS),超出直接回错误;
- 和 master 的连接断开时全部停掉(浏览器那头已经不在了);同一个 connection_id 再次 start 先停旧的,
  否则旧任务从 tasks 里被覆盖掉,再也停不掉;
- 按块读(iter_any),一块里的完整行合成一条消息立刻发出:日志少时不用等凑满 N 行才显示,日志多时消息数也少。
"""

import asyncio
import json

import aiohttp
from kubernetes_asyncio.client.rest import ApiException
from loguru import logger

MAX_STREAMS = 200  # agent 上同时打开的日志流上限(日志流专用连接池也按这个大小建)
TAIL_LINES = 100
MAX_MESSAGE_CHARS = 256 * 1024  # 单条消息最多带的日志字符数,超过就拆成多条
MAX_LINE_BYTES = 1024 * 1024  # 一直没有换行时最多攒这么多字节,到了就当作一行发出去
# 只限制建连(含等连接池);follow 流本身不设总时长和读超时:安静的 Pod 可以很久没有输出
OPEN_TIMEOUT = aiohttp.ClientTimeout(connect=15, sock_connect=10)


def _status_message(body):
    """非 200 时 apiserver 返回的 Status 里取 message"""
    try:
        return json.loads(body).get("message") or ""
    except (ValueError, AttributeError):
        return body[:300].decode("utf-8", "replace")


async def _send_quietly(ws, message):
    try:
        await ws.send_json(message)
    except Exception:
        pass


class PodLogStreamer:
    def __init__(self, core_v1):
        self.core_v1 = core_v1  # 日志流专用的 CoreV1Api(独立连接池)
        self.tasks = {}  # connection_id -> Task
        self._background = set()

    # ------------------------------------------------------------------ master 发来的(接收循环里同步调用)
    def start(self, ws, data):
        connection_id = data.get("connection_id")
        if not isinstance(connection_id, str) or not connection_id:
            return
        self.stop(connection_id)  # 同一个 id 再次 start:先停旧的
        namespace, pod_name, container = data.get("namespace"), data.get("pod_name"), data.get("container") or ""
        if not (isinstance(namespace, str) and namespace and isinstance(pod_name, str) and pod_name
                and isinstance(container, str)):
            self._reply_error(ws, connection_id, "参数错误")
            return
        if len(self.tasks) >= MAX_STREAMS:
            logger.warning(f"Pod日志流已达上限 {MAX_STREAMS} 条,拒绝 {connection_id} {namespace}/{pod_name}")
            self._reply_error(ws, connection_id, f"agent 上同时打开的日志流已达上限({MAX_STREAMS} 条),请关闭不用的日志窗口后重试")
            return
        logger.info(f"开始Pod日志流: {connection_id} {namespace}/{pod_name} 当前 {len(self.tasks) + 1} 条")
        task = asyncio.create_task(self._run(ws, connection_id, namespace, pod_name, container))
        self.tasks[connection_id] = task
        task.add_done_callback(lambda done, cid=connection_id: self._forget(cid, done))

    def stop(self, connection_id):
        task = self.tasks.pop(connection_id, None)
        if task is not None:
            task.cancel()

    def close_all(self):
        """到 master 的连接断了:浏览器那头已经不在,全部停掉"""
        if self.tasks:
            logger.info(f"与 master 的连接已断开,停止全部 {len(self.tasks)} 条Pod日志流")
        for task in self.tasks.values():
            task.cancel()
        self.tasks.clear()

    def _forget(self, connection_id, task):
        if self.tasks.get(connection_id) is task:
            del self.tasks[connection_id]

    def _reply_error(self, ws, connection_id, error):
        task = asyncio.create_task(_send_quietly(ws, {"type": "pod_logs", "connection_id": connection_id, "error": error}))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    # ------------------------------------------------------------------ 日志流
    async def _run(self, ws, connection_id, namespace, pod_name, container):
        resp = None
        try:
            await ws.send_json({"type": "pod_logs", "connection_id": connection_id, "status": "connected"})
            resp = await self.core_v1.read_namespaced_pod_log(
                name=pod_name,
                namespace=namespace,
                container=container or None,
                follow=True,
                tail_lines=TAIL_LINES,
                timestamps=False,
                _preload_content=False,
                _request_timeout=OPEN_TIMEOUT,
            )
            # _preload_content=False 时 kubernetes_asyncio 不会对非 2xx 抛异常,要自己判断
            if resp.status != 200:
                raise ApiException(status=resp.status, reason=_status_message(await resp.read()))
            await self._pump(ws, connection_id, pod_name, container, resp)
        except asyncio.CancelledError:
            logger.info(f"Pod日志流被取消: {connection_id}")
            raise
        except ApiException as e:
            error = f"Kubernetes API错误: {e.status} - {e.reason}"
            logger.error(f"Pod日志流API异常: {connection_id}, 错误: {error}")
            await _send_quietly(ws, {"type": "pod_logs", "connection_id": connection_id, "error": error})
        except Exception as e:
            error = str(e) or type(e).__name__
            logger.error(f"Pod日志流异常: {connection_id}, 错误: {error}")
            await _send_quietly(ws, {"type": "pod_logs", "connection_id": connection_id, "error": error})
        finally:
            if resp is not None:
                resp.close()  # 显式关掉,连接马上还给连接池

    async def _pump(self, ws, connection_id, pod_name, container, resp):
        pending = b""
        async for chunk in resp.content.iter_any():
            lines = (pending + chunk).split(b"\n")
            pending = lines.pop()  # 最后一段还没有换行,留到下一块
            if len(pending) >= MAX_LINE_BYTES:
                lines.append(pending)
                pending = b""
            await self._send_lines(ws, connection_id, pod_name, container, lines)
        if pending:
            await self._send_lines(ws, connection_id, pod_name, container, [pending])

    async def _send_lines(self, ws, connection_id, pod_name, container, raw_lines):
        batch, size = [], 0
        for raw in raw_lines:
            line = raw.decode("utf-8", errors="ignore").strip()
            if not line:
                continue
            batch.append(line)
            size += len(line) + 1
            if size >= MAX_MESSAGE_CHARS:
                await self._send_log(ws, connection_id, pod_name, container, batch)
                batch, size = [], 0
        if batch:
            await self._send_log(ws, connection_id, pod_name, container, batch)

    @staticmethod
    async def _send_log(ws, connection_id, pod_name, container, lines):
        await ws.send_json(
            {"type": "pod_logs", "connection_id": connection_id, "pod_name": pod_name, "container": container,
             "log": "\n".join(lines)}
        )
