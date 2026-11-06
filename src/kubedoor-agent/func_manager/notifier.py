"""
运维通知:agent 不再直接调 IM 接口,统一经 WebSocket 交给 master,由 master 按集群选择通知群发送

agent → master:{"type": "notify", "content": "<markdown 文本>"}

各模块照旧调 utils.send_msg(content):不阻塞调用方,事件循环里、线程里都可以调用。

- 主进程(kubedoor-agent.py)启动时在主 loop 里调 start():之后 send_msg 把通知放进内存队列,后台任务按顺序
  经当前的 MasterConnection 发送(沿用它的发送超时)。没连上 master 时暂存,最多 MAX_PENDING 条、MAX_AGE 秒,
  超出丢最早的并记 warning,连上后补发。发送失败(连接已断开 / 发送超时)的那条放回队首,重连后再发:
  发送超时时 master 可能其实已经收到,极少数情况下会重复,但不会丢。
  入队后超过 DELAY_MARK_AFTER 秒才发出去的(断线暂存、重连后补发),正文最前面加一行
  「【延迟送达】产生于 2026-11-07 12:00:00」(agent 所在环境的本地时间,取主进程入队的时间)。
  线程里调用时,用 start() 时记下的主 loop 做 call_soon_threadsafe,队列只在主 loop 上读写。
- pod-mgr.py 是 runit 起的另一个进程(FastAPI,端口 81),拿不到 WebSocket。没调过 start() 的进程,send_msg 经
  Unix socket(NOTIFY_SOCKET)交给主进程入队,每行一个 JSON {"content": "..."};后台线程按顺序发送,
  每条等主进程入队、关闭连接后再发下一条,连接、发送、等待都有短超时,失败只记日志。
  不用 127.0.0.1 上的 HTTP 路由:master 的兜底转发能打到 agent 的回环地址,等于让浏览器间接发通知。
  socket 文件权限 0600,只有容器里同一用户的进程能连。
"""

import asyncio
import json
import os
import socket
import threading
import time
from collections import deque, namedtuple
from datetime import datetime

from loguru import logger

NOTIFY_SOCKET = "/tmp/kubedoor-agent-notify.sock"
MAX_PENDING = 200  # 暂存上限(条),超出丢最早的
MAX_AGE = 30 * 60  # 暂存超过这么久(秒)还没发出去就丢掉
DELAY_MARK_AFTER = 60  # 入队后超过这么久(秒)才发出去的,正文最前面标注产生时间
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"  # 标注的产生时间的格式
MAX_CONTENT = 20000  # 单条通知最多带的字符数(master 也只取前 20000 个字符),免得断线暂存时占用太多内存
MAX_LINE = 1024 * 1024  # 主进程这边:Unix socket 上一行(一条通知)的字节上限
CLIENT_IDLE_TIMEOUT = 10  # 主进程这边:客户端连上后这么久(秒)没发完一行就断开
SOCKET_TIMEOUT = 3  # pod-mgr 这边:连接、发送、等主进程入队,每一步的超时(秒)
SOCKET_ATTEMPTS = 3  # pod-mgr 这边:主进程没在监听(正在重启)时最多发几次,每次间隔 SOCKET_RETRY_DELAY 秒
SOCKET_RETRY_DELAY = 1


def _normalize(content):
    """统一成非空字符串并截断;空通知返回 None(不发)"""
    if content is None:
        return None
    if not isinstance(content, str):
        content = str(content)
    if not content.strip():
        return None
    return content[:MAX_CONTENT]


def _preview(content, limit=80):
    """日志里只带通知开头的一段(压成一行)"""
    text = " ".join(content.split())
    return text if len(text) <= limit else f"{text[:limit]}..."


# 队列里的一条:入队时间(monotonic,算暂存了多久,不受改系统时间影响)、产生时间(本地时间,标注用)、正文
_Pending = namedtuple("_Pending", "queued_at produced_at content")


class NotifyQueue:
    """主进程的通知队列:只在主 loop 上读写,后台任务按顺序经当前的 MasterConnection 发给 master"""

    def __init__(self, max_pending=MAX_PENDING, max_age=MAX_AGE, clock=None, now=None):
        self.max_pending = max_pending
        self.max_age = max_age
        self._clock = clock or time.monotonic
        self._now = now or datetime.now  # agent 所在环境的本地时间
        self._pending = deque()  # _Pending,最早的在左边
        self._conn = None  # 当前到 master 的连接(MasterConnection),没连上时为 None
        self._loop = None
        self._wakeup = None
        self._task = None

    @property
    def pending(self):
        return len(self._pending)

    def start(self):
        """在主 loop 里调用一次:记下主 loop(线程里的调用转到它上面),启动后台发送任务"""
        if self._task is not None:
            return
        self._loop = asyncio.get_running_loop()
        self._wakeup = asyncio.Event()
        self._task = self._loop.create_task(self._run())
        self._task.add_done_callback(self._task_done)
        self._kick()

    async def stop(self):
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def put(self, content):
        """放进队列。任何线程都可以调用,不阻塞:不在主 loop 上时用 call_soon_threadsafe 转过去"""
        content = _normalize(content)
        if content is None:
            return
        loop = self._loop
        if loop is None:  # 还没 start(启动阶段,只有主线程):先放进队列,start 之后发送
            self._append(content)
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._append(content)
            return
        try:
            loop.call_soon_threadsafe(self._append, content)
        except RuntimeError:  # 主 loop 已经关闭:agent 正在退出
            logger.warning(f"agent 正在退出,运维通知未发送: {_preview(content)}")

    def attach(self, conn):
        """连上 master:之后的通知经这条连接发送,暂存的马上补发"""
        self._conn = conn
        if self._pending:
            logger.info(f"已连上 master,补发暂存的 {len(self._pending)} 条运维通知")
        self._kick()

    def detach(self, conn=None):
        """和 master 断开。传了 conn 时只有它还是当前连接才清掉,免得清掉已经换上的新连接"""
        if conn is None or self._conn is conn:
            self._conn = None

    def _kick(self):
        if self._wakeup is not None and self._pending:
            self._wakeup.set()

    def _append(self, content):
        now = self._clock()
        self._drop_expired(now)
        if len(self._pending) >= self.max_pending:
            dropped = self._pending.popleft()
            logger.warning(f"运维通知暂存已满 {self.max_pending} 条(还没连上 master),丢弃最早的一条: "
                           f"{_preview(dropped.content)}")
        self._pending.append(_Pending(now, self._now(), content))
        self._kick()

    def _drop_expired(self, now):
        dropped = 0
        while self._pending and now - self._pending[0].queued_at > self.max_age:
            self._pending.popleft()
            dropped += 1
        if dropped:
            logger.warning(f"{dropped} 条运维通知暂存超过 {self.max_age // 60} 分钟仍没发给 master,已丢弃")

    def _requeue(self, item):
        """发送失败的那条放回队首;已经过期,或者发送期间队列又满了(它就是最早的一条),按规则丢掉"""
        if self._clock() - item.queued_at > self.max_age:
            logger.warning(f"运维通知暂存超过 {self.max_age // 60} 分钟仍没发给 master,已丢弃: {_preview(item.content)}")
        elif len(self._pending) >= self.max_pending:
            logger.warning(f"运维通知暂存已满 {self.max_pending} 条(还没连上 master),丢弃最早的一条: "
                           f"{_preview(item.content)}")
        else:
            self._pending.appendleft(item)

    async def _run(self):
        while True:
            await self._wakeup.wait()
            self._wakeup.clear()
            try:
                await self._flush()
            except Exception as e:  # 不让后台任务因为意外异常退出,否则之后的通知都发不出去
                logger.exception(f"运维通知发送任务异常: {type(e).__name__}: {e}")
                await asyncio.sleep(1)

    async def _flush(self):
        while self._pending:
            conn = self._conn
            if conn is None or conn.closed:
                return  # 等连上 master(attach)再发
            self._drop_expired(self._clock())
            if not self._pending:
                return
            item = self._pending.popleft()
            waited = self._clock() - item.queued_at
            content = item.content
            if waited > DELAY_MARK_AFTER:
                # 断线暂存、重连后补发的可能晚到很久,正文里又没有时间:最前面加一行产生时间。
                # 发送失败放回队首的是原文,下次发送时重新判断,不会标两遍
                content = f"【延迟送达】产生于 {item.produced_at.strftime(TIME_FORMAT)}\n{content}"[:MAX_CONTENT]
            try:
                await conn.send_json({"type": "notify", "content": content})
            except Exception as e:
                # 连接已断开或发送超时(MasterConnection 会判定连接失效,随后断开重连):放回队首,重连后补发
                self._requeue(item)
                logger.warning(f"运维通知发送失败,重连后补发(暂存 {len(self._pending)} 条): {type(e).__name__}: {e}")
                return
            late = f"(入队 {waited:.0f} 秒后才发出)" if waited > DELAY_MARK_AFTER else ""
            logger.info(f"运维通知已发给 master{late}: {_preview(item.content)}")

    @staticmethod
    def _task_done(task):
        if not task.cancelled() and task.exception() is not None:
            exc = task.exception()
            logger.error(f"运维通知发送任务已退出: {type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------- Unix socket:主进程接收


def parse_line(line):
    """Unix socket 上收到的一行 → 通知内容;格式不对返回 None"""
    line = line.strip()
    if not line:
        return None
    try:
        data = json.loads(line)
    except ValueError:  # 含 UnicodeDecodeError
        logger.warning(f"运维通知 socket 收到无法解析的数据(长度 {len(line)}),已忽略")
        return None
    content = data.get("content") if isinstance(data, dict) else None
    if not isinstance(content, str):
        logger.warning("运维通知 socket 收到的数据没有 content 字段,已忽略")
        return None
    return content


async def _serve_client(queue, reader, writer):
    """一个连接可以发多行,每行一条通知;读到对端关闭(EOF)为止,处理完再关闭连接,客户端据此知道已经入队"""
    try:
        while True:
            line = await asyncio.wait_for(reader.readline(), CLIENT_IDLE_TIMEOUT)
            if not line:
                break
            content = parse_line(line)
            if content is not None:
                queue.put(content)
    except asyncio.TimeoutError:  # 要放在 OSError 前面:3.11 起 TimeoutError 是 OSError 的子类
        logger.warning(f"运维通知 socket 的客户端 {CLIENT_IDLE_TIMEOUT} 秒没有发完一行,已断开")
    except ValueError as e:  # 一行超过 MAX_LINE(readline 把 LimitOverrunError 转成了 ValueError)
        logger.warning(f"运维通知 socket 收到超长的数据,已断开: {e}")
    except OSError as e:
        logger.warning(f"运维通知 socket 读取失败: {type(e).__name__}: {e}")
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def start_socket_server(queue, path=NOTIFY_SOCKET):
    """主进程:开 Unix socket,接收 pod-mgr 发来的通知放进 queue

    返回 asyncio.Server;开不起来(平台不支持、目录不可写)只记日志返回 None,不影响 agent 的其它功能。
    上次进程留下的 socket 文件由 asyncio 在 bind 前删掉。
    """
    if not hasattr(socket, "AF_UNIX"):
        logger.warning("当前平台不支持 Unix socket,pod-mgr 的运维通知无法发送")
        return None

    async def handle(reader, writer):
        await _serve_client(queue, reader, writer)

    try:
        server = await asyncio.start_unix_server(handle, path=path, limit=MAX_LINE)
    except OSError as e:
        logger.error(f"运维通知 Unix socket 启动失败({path}): {e},pod-mgr 的运维通知将无法发送")
        return None
    try:
        os.chmod(path, 0o600)  # 只让容器里同一用户的进程连接
    except OSError as e:
        logger.warning(f"设置运维通知 Unix socket 权限失败({path}): {e}")
    logger.info(f"运维通知 Unix socket 已启动: {path}")
    return server


# ---------------------------------------------------------------------- Unix socket:其它进程(pod-mgr)发送


def send_line(content, path=NOTIFY_SOCKET, timeout=SOCKET_TIMEOUT):
    """经 Unix socket 把一条通知交给主进程(同步调用,连接、发送、等待都有超时)

    发完半关闭连接,等主进程读完、放进队列后关闭连接(这边读到 EOF)再返回:每条通知一个连接,主进程那边
    几个连接谁先被读到是不确定的,等上一条入队了再发下一条,顺序才不会乱。
    """
    data = json.dumps({"content": content}, ensure_ascii=False).encode("utf-8") + b"\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(path)
        sock.sendall(data)
        sock.shutdown(socket.SHUT_WR)
        while sock.recv(64):  # 主进程不回数据,处理完就关闭连接
            pass


class SocketSender:
    """其它进程用:后台线程按顺序经 Unix socket 交给主进程,调用方(event loop 或线程)不等待"""

    def __init__(self, path=NOTIFY_SOCKET, timeout=SOCKET_TIMEOUT, attempts=SOCKET_ATTEMPTS,
                 retry_delay=SOCKET_RETRY_DELAY, max_pending=MAX_PENDING, transport=send_line):
        self.path = path
        self.timeout = timeout
        self.attempts = attempts
        self.retry_delay = retry_delay
        self.max_pending = max_pending
        self._transport = transport
        self._pending = deque()
        self._cond = threading.Condition()
        self._thread = None

    def send(self, content):
        content = _normalize(content)
        if content is None:
            return
        with self._cond:
            if len(self._pending) >= self.max_pending:
                dropped = self._pending.popleft()
                logger.warning(f"运维通知积压已满 {self.max_pending} 条(agent 主进程没有响应),丢弃最早的一条: {_preview(dropped)}")
            self._pending.append(content)
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._worker, name="kubedoor-notify", daemon=True)
                self._thread.start()
            self._cond.notify()

    def _worker(self):
        while True:
            with self._cond:
                while not self._pending:
                    self._cond.wait()
                content = self._pending.popleft()
            try:
                self._deliver(content)
            except Exception as e:  # 线程不能因为意外异常退出(下次 send 也会重新拉起,这里只是兜底)
                logger.error(f"运维通知发送线程异常: {type(e).__name__}: {e}")

    def _deliver(self, content):
        error = None
        for attempt in range(1, max(self.attempts, 1) + 1):
            try:
                self._transport(content, self.path, self.timeout)
                return True
            except (FileNotFoundError, ConnectionRefusedError) as e:
                # socket 文件不在 / 没人监听:主进程还没起来或正在重启,稍等再试
                error = e
                if attempt < self.attempts:
                    time.sleep(self.retry_delay)
            except Exception as e:  # 超时、权限不对、平台不支持:不重试(超时时主进程可能其实已经收到)
                error = e
                break
        logger.error(f"运维通知交给 agent 主进程失败,已放弃({self.path}: {type(error).__name__}: {error}): "
                     f"{_preview(content)}")
        return False


# ---------------------------------------------------------------------- 对外接口

_queue = None  # 主进程:start() 之后的通知队列
_server = None  # 主进程:接收 pod-mgr 通知的 Unix socket
_sender = None  # 其它进程:第一次发送时创建
_sender_lock = threading.Lock()


def send_msg(content):
    """发运维通知(utils.send_msg 调这里):主进程 start() 之后入队经 master 发送,其它进程经 Unix socket 交给主进程"""
    queue = _queue
    if queue is not None:
        queue.put(content)
    else:
        _socket_sender().send(content)


def _socket_sender():
    global _sender
    with _sender_lock:
        if _sender is None:
            _sender = SocketSender()
        return _sender


async def start(path=NOTIFY_SOCKET):
    """主进程启动时在主 loop 里调用一次:之后 send_msg 改为入队,并开 Unix socket 接收 pod-mgr 的通知"""
    global _queue, _server
    if _queue is None:
        queue = NotifyQueue()
        queue.start()
        _queue = queue
    if _server is None:
        _server = await start_socket_server(_queue, path)
    return _queue


def attach(conn):
    """连上 master 后调用(conn 是 MasterConnection)"""
    if _queue is not None:
        _queue.attach(conn)


def detach(conn=None):
    """和 master 断开后调用"""
    if _queue is not None:
        _queue.detach(conn)


async def stop():
    """进程退出前调用:关掉 Unix socket、停掉发送任务(没发出去的通知随进程丢弃)"""
    global _server
    server, _server = _server, None
    if server is not None:
        server.close()
        try:
            await asyncio.wait_for(server.wait_closed(), 2)
        except Exception:
            pass
    if _queue is not None:
        await _queue.stop()
