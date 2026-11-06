"""运维通知:agent 不直接调 IM 接口,经 WebSocket 交给 master;pod-mgr 经 Unix socket 交给 agent 主进程

覆盖:发给 master 的消息格式、断线暂存 / 重连补发、超出上限丢最早的、补发时标注产生时间、线程里调用、
Unix socket 收发、utils.send_msg 的走向(主进程入队 / 其它进程走 socket)、连 master 时声明 notify=1。
"""

import asyncio
import json
import os
import re
import socket
import stat
import tempfile
import threading
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from loguru import logger

from conftest import AGENT_ROOT, load_module

needs_unix_socket = pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="需要 Unix socket(Windows 上没有 AF_UNIX)")


@pytest.fixture
def logs():
    lines = []
    sink = logger.add(lambda message: lines.append(str(message).rstrip()), level="INFO")
    yield lines
    logger.remove(sink)


@pytest.fixture
def ws_lifecycle_module():
    return load_module("agent_test_ws_lifecycle_notify", AGENT_ROOT / "func_manager" / "ws_lifecycle.py")


class FakeWs:
    """aiohttp ClientWebSocketResponse 的替身:记下发出的原始文本

    fail_next:之后几次发送抛连接错误(连接正在关闭);block:发送一直卡住(master 不读)
    """

    def __init__(self):
        self.closed = False
        self.sent = []
        self.fail_next = 0
        self.block = False

    async def send_str(self, data):
        if self.block:
            await asyncio.Event().wait()
        if self.fail_next:
            self.fail_next -= 1
            self.closed = True
            raise ConnectionResetError("Cannot write to closing transport")
        self.sent.append(data)

    async def close(self):
        self.closed = True

    @property
    def contents(self):
        return [json.loads(data)["content"] for data in self.sent]


class FakeWriter:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


class Clock:
    """假时钟:monotonic(算暂存了多久)和本地时间(标注产生时间)一起往前拨"""

    def __init__(self):
        self.now = 1000.0
        self.local = datetime(2026, 11, 7, 12, 0, 0)

    def __call__(self):
        return self.now

    def wall(self):
        return self.local

    def advance(self, seconds):
        self.now += seconds
        self.local += timedelta(seconds=seconds)


def connection(ws_lifecycle_module, raw, send_timeout=1):
    """真实的 MasterConnection(发送带超时),包一个假的 WebSocket"""
    return ws_lifecycle_module.MasterConnection(raw, send_timeout=send_timeout, ack_timeout=30)


async def wait_until(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "等待超时"
        await asyncio.sleep(0.01)


def wait_until_sync(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "等待超时"
        time.sleep(0.01)


def short_socket_path():
    """AF_UNIX 的路径最长约 108 字节,pytest 的 tmp_path 可能超长"""
    return os.path.join(tempfile.mkdtemp(prefix="kd-notify-"), "n.sock")


# ---------------------------------------------------------------------- 主进程:队列 → master


@pytest.mark.asyncio
async def test_notifications_are_sent_in_order_as_notify_messages(notifier_module, ws_lifecycle_module):
    queue = notifier_module.NotifyQueue()
    queue.start()
    try:
        raw = FakeWs()
        queue.attach(connection(ws_lifecycle_module, raw))
        queue.put("【prod】【default】【web】开始更新镜像【web:v2】")
        queue.put("# 【<font color=\"#5bcc85\">prod</font>】default\n## web-1\njstack 成功\n")
        await wait_until(lambda: len(raw.sent) == 2)
        assert [json.loads(data) for data in raw.sent] == [
            {"type": "notify", "content": "【prod】【default】【web】开始更新镜像【web:v2】"},
            {"type": "notify", "content": "# 【<font color=\"#5bcc85\">prod</font>】default\n## web-1\njstack 成功\n"},
        ]
        assert queue.pending == 0
    finally:
        await queue.stop()


@pytest.mark.asyncio
async def test_buffered_while_disconnected_and_resent_after_reconnect(notifier_module, ws_lifecycle_module, logs):
    queue = notifier_module.NotifyQueue()
    queue.start()
    try:
        for i in range(3):
            queue.put(f"m{i}")
        await asyncio.sleep(0.05)
        assert queue.pending == 3  # 还没连上 master:先暂存

        first = FakeWs()
        first_conn = connection(ws_lifecycle_module, first)
        queue.attach(first_conn)
        await wait_until(lambda: len(first.sent) == 3)
        assert first.contents == ["m0", "m1", "m2"]
        assert any("补发暂存的 3 条运维通知" in line for line in logs)

        queue.detach(first_conn)  # 断线
        queue.put("m3")
        await asyncio.sleep(0.05)
        assert queue.pending == 1 and len(first.sent) == 3

        second = FakeWs()
        second_conn = connection(ws_lifecycle_module, second)
        queue.attach(second_conn)
        queue.detach(first_conn)  # 旧连接的清理晚到:不能把新连接清掉
        await wait_until(lambda: second.sent)
        queue.put("m4")
        await wait_until(lambda: len(second.sent) == 2)
        assert second.contents == ["m3", "m4"] and queue.pending == 0
    finally:
        await queue.stop()


@pytest.mark.asyncio
async def test_failed_send_is_put_back_first_and_resent_after_reconnect(notifier_module, ws_lifecycle_module, logs):
    queue = notifier_module.NotifyQueue()
    queue.start()
    try:
        closing = FakeWs()
        closing.fail_next = 1  # 发第一条时连接已经在关闭
        conn = connection(ws_lifecycle_module, closing)
        queue.attach(conn)
        queue.put("m0")
        queue.put("m1")
        await wait_until(lambda: any("重连后补发" in line for line in logs))
        assert closing.sent == [] and queue.pending == 2
        queue.put("m2")  # 连接已关闭、还没 detach:不再往上面发
        await asyncio.sleep(0.05)
        assert closing.sent == [] and queue.pending == 3

        queue.detach(conn)
        fresh = FakeWs()
        queue.attach(connection(ws_lifecycle_module, fresh))
        await wait_until(lambda: len(fresh.sent) == 3)
        assert fresh.contents == ["m0", "m1", "m2"]  # 发送失败的那条放回队首,顺序不变
    finally:
        await queue.stop()


@pytest.mark.asyncio
async def test_send_timeout_breaks_connection_but_keeps_the_notification(notifier_module, ws_lifecycle_module):
    queue = notifier_module.NotifyQueue()
    queue.start()
    try:
        stuck = FakeWs()
        stuck.block = True  # master 不读了:发送卡在 drain 上
        conn = connection(ws_lifecycle_module, stuck, send_timeout=0.05)
        queue.attach(conn)
        queue.put("m0")
        await wait_until(lambda: conn.closed and queue.pending == 1)  # 沿用 MasterConnection 的发送超时,判定连接失效
        assert conn.broken

        fresh = FakeWs()
        queue.attach(connection(ws_lifecycle_module, fresh))
        await wait_until(lambda: fresh.sent)
        assert fresh.contents == ["m0"]
    finally:
        await queue.stop()


@pytest.mark.asyncio
async def test_buffer_keeps_at_most_200_and_drops_the_oldest(notifier_module, ws_lifecycle_module, logs):
    assert notifier_module.MAX_PENDING == 200
    queue = notifier_module.NotifyQueue()
    queue.start()
    try:
        for i in range(203):
            queue.put(f"m{i}")
        assert queue.pending == 200
        dropped = [line for line in logs if "丢弃最早的一条" in line]
        assert [line.rsplit(": ", 1)[-1] for line in dropped] == ["m0", "m1", "m2"]
        assert all("WARNING" in line for line in dropped)

        raw = FakeWs()
        queue.attach(connection(ws_lifecycle_module, raw))
        await wait_until(lambda: len(raw.sent) == 200)
        assert raw.contents == [f"m{i}" for i in range(3, 203)]
    finally:
        await queue.stop()


@pytest.mark.asyncio
async def test_buffered_longer_than_30_minutes_are_dropped(notifier_module, ws_lifecycle_module, logs):
    assert notifier_module.MAX_AGE == 30 * 60
    clock = Clock()
    queue = notifier_module.NotifyQueue(clock=clock, now=clock.wall)
    queue.start()
    try:
        queue.put("old")
        clock.advance(29 * 60 + 30)
        queue.put("recent")
        clock.advance(31)  # old 已暂存 30 分钟零 1 秒,recent 才 31 秒(不到 60 秒,不标注)
        raw = FakeWs()
        queue.attach(connection(ws_lifecycle_module, raw))
        await wait_until(lambda: raw.sent)
        await asyncio.sleep(0.05)
        assert raw.contents == ["recent"]
        assert any("超过 30 分钟" in line and "WARNING" in line for line in logs)
    finally:
        await queue.stop()


@pytest.mark.asyncio
async def test_sent_more_than_60_seconds_after_queued_is_marked_with_its_time(notifier_module, ws_lifecycle_module, logs):
    assert notifier_module.DELAY_MARK_AFTER == 60
    clock = Clock()
    queue = notifier_module.NotifyQueue(clock=clock, now=clock.wall)
    queue.start()
    try:
        queue.put("'【prod】【default】【web】' has been scaled! 1 --> 3")  # 12:00:00 产生,还没连上 master
        clock.advance(30)
        queue.put("12:00:30 产生的")
        clock.advance(31)  # 重连时第一条已等 61 秒,第二条 31 秒
        raw = FakeWs()
        queue.attach(connection(ws_lifecycle_module, raw))
        await wait_until(lambda: len(raw.sent) == 2)
        assert [json.loads(data) for data in raw.sent] == [
            {"type": "notify",
             "content": "【延迟送达】产生于 2026-11-07 12:00:00\n'【prod】【default】【web】' has been scaled! 1 --> 3"},
            {"type": "notify", "content": "12:00:30 产生的"},
        ]
        assert any("入队 61 秒后才发出" in line for line in logs)
    finally:
        await queue.stop()


@pytest.mark.asyncio
async def test_sent_within_60_seconds_is_not_marked(notifier_module, ws_lifecycle_module):
    clock = Clock()
    queue = notifier_module.NotifyQueue(clock=clock, now=clock.wall)
    queue.start()
    try:
        queue.put("刚好等了 60 秒")
        clock.advance(60)  # 「超过 60 秒」才标注
        raw = FakeWs()
        queue.attach(connection(ws_lifecycle_module, raw))
        await wait_until(lambda: raw.sent)
        queue.put("连着 master 时马上发")
        await wait_until(lambda: len(raw.sent) == 2)
        assert raw.contents == ["刚好等了 60 秒", "连着 master 时马上发"]
    finally:
        await queue.stop()


@pytest.mark.asyncio
async def test_resent_after_failure_is_marked_once_with_the_original_time(notifier_module, ws_lifecycle_module):
    clock = Clock()
    queue = notifier_module.NotifyQueue(clock=clock, now=clock.wall)
    queue.start()
    try:
        queue.put("扩容完成")  # 12:00:00 产生
        clock.advance(61)
        closing = FakeWs()
        closing.fail_next = 1  # 补发时连接正在关闭
        conn = connection(ws_lifecycle_module, closing)
        queue.attach(conn)
        await wait_until(lambda: closing.closed and queue.pending == 1)
        clock.advance(10 * 60)
        queue.detach(conn)
        fresh = FakeWs()
        queue.attach(connection(ws_lifecycle_module, fresh))
        await wait_until(lambda: fresh.sent)
        assert fresh.contents == ["【延迟送达】产生于 2026-11-07 12:00:00\n扩容完成"]  # 只标一次,还是原来的产生时间
    finally:
        await queue.stop()


@pytest.mark.asyncio
async def test_pod_mgr_notification_is_dated_when_main_process_queued_it(notifier_module, ws_lifecycle_module):
    clock = Clock()
    queue = notifier_module.NotifyQueue(clock=clock, now=clock.wall)
    queue.start()
    try:
        clock.advance(5 * 60)  # 12:05:00 主进程从 Unix socket 收到 pod-mgr 的通知,还没连上 master
        reader = asyncio.StreamReader()
        reader.feed_data(json.dumps({"content": "jstack 成功"}, ensure_ascii=False).encode() + b"\n")
        reader.feed_eof()
        await asyncio.wait_for(notifier_module._serve_client(queue, reader, FakeWriter()), 2)
        clock.advance(90)
        raw = FakeWs()
        queue.attach(connection(ws_lifecycle_module, raw))
        await wait_until(lambda: raw.sent)
        assert raw.contents == ["【延迟送达】产生于 2026-11-07 12:05:00\njstack 成功"]
    finally:
        await queue.stop()


@pytest.mark.asyncio
async def test_marked_time_is_the_agent_local_time_by_default(notifier_module, ws_lifecycle_module, monkeypatch):
    class LocalDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz is None  # 本地时间,不换算时区
            return cls(2026, 11, 7, 8, 30, 15)

    monkeypatch.setattr(notifier_module, "datetime", LocalDatetime)
    clock = Clock()
    queue = notifier_module.NotifyQueue(clock=clock)  # 不传 now:用 datetime.now()
    queue.start()
    try:
        queue.put("重启完成")
        clock.advance(120)
        raw = FakeWs()
        queue.attach(connection(ws_lifecycle_module, raw))
        await wait_until(lambda: raw.sent)
        assert raw.contents == ["【延迟送达】产生于 2026-11-07 08:30:15\n重启完成"]
    finally:
        await queue.stop()


@pytest.mark.asyncio
async def test_marked_notification_still_fits_the_length_limit(notifier_module, ws_lifecycle_module):
    clock = Clock()
    queue = notifier_module.NotifyQueue(clock=clock, now=clock.wall)
    queue.start()
    try:
        queue.put("x" * notifier_module.MAX_CONTENT)
        clock.advance(61)
        raw = FakeWs()
        queue.attach(connection(ws_lifecycle_module, raw))
        await wait_until(lambda: raw.sent)
        content = raw.contents[0]
        assert content.startswith("【延迟送达】产生于 2026-11-07 12:00:00\nxxx")
        assert len(content) == notifier_module.MAX_CONTENT
    finally:
        await queue.stop()


@pytest.mark.asyncio
async def test_put_from_other_threads_goes_through_the_main_loop(notifier_module, ws_lifecycle_module, monkeypatch):
    queue = notifier_module.NotifyQueue()
    queue.start()
    loop_thread = threading.get_ident()
    appended_on = []
    original_append = queue._append

    def recording_append(content):
        appended_on.append(threading.get_ident())
        original_append(content)

    monkeypatch.setattr(queue, "_append", recording_append)
    try:
        raw = FakeWs()
        queue.attach(connection(ws_lifecycle_module, raw))
        await asyncio.to_thread(queue.put, "线程池里的通知")
        worker = threading.Thread(target=queue.put, args=("普通线程里的通知",))
        worker.start()
        await asyncio.to_thread(worker.join)
        await wait_until(lambda: len(raw.sent) == 2)
        assert raw.contents == ["线程池里的通知", "普通线程里的通知"]
        assert appended_on == [loop_thread, loop_thread]  # 用 call_soon_threadsafe 转到主 loop,队列只在主 loop 上改
    finally:
        await queue.stop()


@pytest.mark.asyncio
async def test_empty_notifications_are_skipped_and_long_ones_truncated(notifier_module, ws_lifecycle_module):
    queue = notifier_module.NotifyQueue()
    queue.start()
    try:
        raw = FakeWs()
        queue.attach(connection(ws_lifecycle_module, raw))
        for content in (None, "", "  \n "):
            queue.put(content)
        queue.put(12345)
        queue.put("x" * (notifier_module.MAX_CONTENT + 100))
        await wait_until(lambda: len(raw.sent) == 2)
        assert raw.contents == ["12345", "x" * notifier_module.MAX_CONTENT]
    finally:
        await queue.stop()


def test_put_after_main_loop_closed_only_logs(notifier_module, logs):
    queue = notifier_module.NotifyQueue()
    loop = asyncio.new_event_loop()
    loop.run_until_complete(asyncio.sleep(0))
    queue._loop = loop  # 相当于 start() 过、主 loop 已经结束(agent 正在退出)
    loop.close()
    queue.put("退出时的通知")
    assert queue.pending == 0 and any("正在退出" in line for line in logs)


# ---------------------------------------------------------------------- 对外接口:utils.send_msg 的走向


@pytest.mark.asyncio
async def test_send_msg_uses_the_queue_after_start_and_unix_socket_before(notifier_module, ws_lifecycle_module, monkeypatch):
    socket_sends = []
    monkeypatch.setattr(notifier_module, "_sender", SimpleNamespace(send=socket_sends.append))
    notifier_module.send_msg("pod-mgr 的通知")  # 没调过 start() 的进程(pod-mgr):经 Unix socket 交给主进程
    assert socket_sends == ["pod-mgr 的通知"]

    path = short_socket_path()
    queue = await notifier_module.start(path=path)
    try:
        assert await notifier_module.start(path=path) is queue  # 重复调用不会再建一份
        raw = FakeWs()
        conn = connection(ws_lifecycle_module, raw)
        notifier_module.attach(conn)
        notifier_module.send_msg("主进程的通知")
        await asyncio.to_thread(notifier_module.send_msg, "主进程线程里的通知")
        await wait_until(lambda: len(raw.sent) == 2)
        assert raw.contents == ["主进程的通知", "主进程线程里的通知"]
        assert socket_sends == ["pod-mgr 的通知"]  # start 之后不再走 socket

        notifier_module.detach(conn)
        notifier_module.send_msg("断线时的通知")
        await asyncio.sleep(0.05)
        assert queue.pending == 1 and len(raw.sent) == 2
    finally:
        await notifier_module.stop()


def test_socket_sender_is_created_once_for_other_processes(notifier_module):
    first = notifier_module._socket_sender()
    assert notifier_module._socket_sender() is first
    assert first.path == "/tmp/kubedoor-agent-notify.sock" and first.timeout == notifier_module.SOCKET_TIMEOUT


def test_utils_send_msg_hands_off_to_notifier_without_im_code(utils_module, notifier_module, monkeypatch):
    for name in ("MSG_TOKEN", "MSG_TYPE", "wecom", "dingding", "feishu", "slack", "_send_msg_impl", "_send_msg_sync"):
        assert not hasattr(utils_module, name), name
    assert utils_module.notifier is notifier_module
    socket_sends = []
    monkeypatch.setattr(notifier_module, "_sender", SimpleNamespace(send=socket_sends.append))
    utils_module.send_msg("【prod】扩容完成")
    assert socket_sends == ["【prod】扩容完成"]


def test_agent_declares_notify_and_wires_the_notifier():
    source = (AGENT_ROOT / "kubedoor-agent.py").read_text(encoding="utf-8")
    uri = re.search(r'uri = f"([^"]+/ws\?[^"]+)"', source).group(1)
    assert parse_qs(urlsplit(uri).query)["notify"] == ["1"]
    for call in ("await notifier.start()", "notifier.attach(ws)", "notifier.detach(ws)", "await notifier.stop()"):
        assert call in source, call
    # pod-mgr 不能 start():它没有到 master 的连接,通知要经 Unix socket 交给主进程
    assert "notifier.start" not in (AGENT_ROOT / "pod-mgr.py").read_text(encoding="utf-8")


def test_agent_no_longer_reads_im_settings():
    for path in AGENT_ROOT.rglob("*.py"):
        if {"tests", "__pycache__"} & set(path.relative_to(AGENT_ROOT).parts):
            continue
        source = path.read_text(encoding="utf-8")
        assert not re.search(r"MSG_TOKEN|MSG_TYPE|msgToken|qyapi\.weixin|oapi\.dingtalk|open\.feishu|hooks\.slack", source), path


# ---------------------------------------------------------------------- Unix socket:主进程接收


def test_parse_line(notifier_module, logs):
    parse = notifier_module.parse_line
    assert parse(json.dumps({"content": "扩容完成\n第二行"}, ensure_ascii=False).encode() + b"\n") == "扩容完成\n第二行"
    assert parse(b"\n") is None and parse(b"") is None  # 空行直接跳过
    for bad in (b"not json\n", b'{"text": "x"}\n', b'["content"]\n', b'{"content": 1}\n', b'{"content": "\xc3\x28"}\n'):
        assert parse(bad) is None, bad
    assert len([line for line in logs if "已忽略" in line]) == 5


@pytest.mark.asyncio
async def test_socket_client_lines_become_one_notification_each(notifier_module):
    queue = notifier_module.NotifyQueue()  # 没 start:put 直接放进队列
    reader = asyncio.StreamReader(limit=notifier_module.MAX_LINE)
    reader.feed_data(json.dumps({"content": "第一条\n两行"}, ensure_ascii=False).encode() + b"\n")
    reader.feed_data(b"garbage\n\n")
    reader.feed_data(json.dumps({"content": "第二条"}).encode())  # 最后一行没有换行也收
    reader.feed_eof()
    writer = FakeWriter()
    await asyncio.wait_for(notifier_module._serve_client(queue, reader, writer), 2)
    assert [item.content for item in queue._pending] == ["第一条\n两行", "第二条"]
    assert writer.closed


@pytest.mark.asyncio
async def test_socket_client_sending_an_overlong_line_is_disconnected(notifier_module, logs):
    queue = notifier_module.NotifyQueue()
    reader = asyncio.StreamReader(limit=64)
    reader.feed_data(b'{"content": "' + b"x" * 200 + b'"}\n')
    reader.feed_eof()
    writer = FakeWriter()
    await asyncio.wait_for(notifier_module._serve_client(queue, reader, writer), 2)
    assert queue.pending == 0 and writer.closed
    assert any("超长" in line for line in logs)


@pytest.mark.asyncio
async def test_idle_socket_client_is_disconnected(notifier_module, monkeypatch, logs):
    monkeypatch.setattr(notifier_module, "CLIENT_IDLE_TIMEOUT", 0.05)
    reader = asyncio.StreamReader()
    reader.feed_data(b'{"content": "half')  # 一直不发完这一行
    writer = FakeWriter()
    await asyncio.wait_for(notifier_module._serve_client(notifier_module.NotifyQueue(), reader, writer), 2)
    assert writer.closed and any("没有发完一行" in line for line in logs)


@pytest.mark.asyncio
async def test_socket_server_is_skipped_without_unix_socket_support(notifier_module, monkeypatch, logs):
    monkeypatch.setattr(notifier_module, "socket", SimpleNamespace())  # 没有 AF_UNIX 的平台
    assert await notifier_module.start_socket_server(notifier_module.NotifyQueue(), short_socket_path()) is None
    assert any("不支持 Unix socket" in line for line in logs)


@needs_unix_socket
@pytest.mark.asyncio
async def test_unix_socket_round_trip_from_pod_mgr_to_master(notifier_module, ws_lifecycle_module):
    path = short_socket_path()
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)  # 上次进程留下的 socket 文件
    stale.bind(path)
    stale.close()
    queue = notifier_module.NotifyQueue()
    queue.start()
    raw = FakeWs()
    queue.attach(connection(ws_lifecycle_module, raw))
    server = await notifier_module.start_socket_server(queue, path)
    assert server is not None
    try:
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        markdown = "# 【<font color=\"#5bcc85\">prod</font>】default\n## web-1\njstack 成功\n"
        # pod-mgr 的同步发送放在线程里跑,不卡测试的 loop
        await asyncio.to_thread(notifier_module.send_line, markdown, path, 2)
        sender = notifier_module.SocketSender(path=path)
        sender.send("第二条")
        await asyncio.to_thread(sender.send, "线程里的第三条")
        await wait_until(lambda: len(raw.sent) == 3)
        assert [json.loads(data) for data in raw.sent] == [
            {"type": "notify", "content": markdown},
            {"type": "notify", "content": "第二条"},
            {"type": "notify", "content": "线程里的第三条"},
        ]
    finally:
        server.close()
        await server.wait_closed()
        await queue.stop()


@needs_unix_socket
@pytest.mark.asyncio
async def test_send_line_returns_after_main_process_queued_it_so_order_is_kept(notifier_module):
    path = short_socket_path()
    queue = notifier_module.NotifyQueue()  # 不 start:收到就直接放进队列,方便看入队的时机
    server = await notifier_module.start_socket_server(queue, path)
    try:
        await asyncio.to_thread(notifier_module.send_line, "第一条", path, 2)
        assert queue.pending == 1  # send_line 返回时主进程已经入队
        # 每条一个连接:要等上一条入队了才发下一条,否则主进程那边谁先被读到不确定
        sender = notifier_module.SocketSender(path=path)
        for i in range(30):
            sender.send(f"m{i}")
        await wait_until(lambda: queue.pending == 31)
        assert [item.content for item in queue._pending] == ["第一条", *(f"m{i}" for i in range(30))]
    finally:
        server.close()
        await server.wait_closed()


@needs_unix_socket
@pytest.mark.asyncio
async def test_socket_server_failure_only_logs(notifier_module, logs):
    path = os.path.join(tempfile.mkdtemp(prefix="kd-notify-"), "missing", "n.sock")
    assert await notifier_module.start_socket_server(notifier_module.NotifyQueue(), path) is None
    assert any("启动失败" in line for line in logs)


@needs_unix_socket
def test_send_line_fails_fast_when_main_process_is_not_listening(notifier_module):
    with pytest.raises(FileNotFoundError):
        notifier_module.send_line("x", short_socket_path(), 1)


# ---------------------------------------------------------------------- Unix socket:pod-mgr 发送


def test_socket_sender_sends_in_order_on_a_background_thread(notifier_module):
    release, delivered, threads = threading.Event(), [], []

    def transport(content, path, timeout):
        release.wait(2)
        threads.append(threading.get_ident())
        delivered.append((content, path, timeout))

    sender = notifier_module.SocketSender(path="/tmp/test.sock", transport=transport)
    started = time.monotonic()
    for i in range(5):
        sender.send(f"m{i}")
    sender.send("  ")  # 空通知不发
    assert time.monotonic() - started < 0.5  # 调用方不等发送:主进程卡住时也不会卡住 pod-mgr 的 event loop
    release.set()
    wait_until_sync(lambda: len(delivered) == 5)
    assert [content for content, _, _ in delivered] == [f"m{i}" for i in range(5)]
    assert {(path, timeout) for _, path, timeout in delivered} == {("/tmp/test.sock", notifier_module.SOCKET_TIMEOUT)}
    assert len(set(threads)) == 1 and threading.get_ident() not in threads


def test_socket_sender_retries_while_main_process_restarts(notifier_module, logs):
    calls = []

    def transport(content, path, timeout):
        calls.append(content)
        if len(calls) < notifier_module.SOCKET_ATTEMPTS:
            raise ConnectionRefusedError(111, "Connection refused")  # 主进程正在重启,还没监听

    sender = notifier_module.SocketSender(transport=transport, retry_delay=0)
    sender.send("重启中发的")
    wait_until_sync(lambda: len(calls) == notifier_module.SOCKET_ATTEMPTS)
    time.sleep(0.05)
    assert not any("已放弃" in line for line in logs)  # 最后一次发出去了


@pytest.mark.parametrize("error, attempts", [
    (FileNotFoundError(2, "No such file or directory"), 3),  # 主进程一直没起来:重试后放弃
    (TimeoutError("timed out"), 1),  # 主进程卡住:不重试
    (PermissionError(13, "Permission denied"), 1),
])
def test_socket_sender_logs_and_drops_when_delivery_fails(notifier_module, logs, error, attempts):
    calls = []

    def transport(content, path, timeout):
        calls.append(content)
        raise error

    sender = notifier_module.SocketSender(transport=transport, retry_delay=0)
    sender.send("发不出去的通知")
    wait_until_sync(lambda: any("已放弃" in line for line in logs))
    assert calls == ["发不出去的通知"] * attempts
    failed = [line for line in logs if "已放弃" in line]
    assert "ERROR" in failed[0] and type(error).__name__ in failed[0] and "发不出去的通知" in failed[0]


def test_socket_sender_backlog_drops_the_oldest(notifier_module, logs):
    entered, release, delivered = threading.Event(), threading.Event(), []

    def transport(content, path, timeout):
        entered.set()
        release.wait(2)
        delivered.append(content)

    sender = notifier_module.SocketSender(transport=transport, max_pending=2)
    sender.send("m0")
    assert entered.wait(2)  # m0 正在发(主进程没响应)
    for i in range(1, 5):
        sender.send(f"m{i}")  # 积压 m1..m4,上限 2 条:丢掉 m1、m2
    release.set()
    wait_until_sync(lambda: len(delivered) == 3)
    assert delivered == ["m0", "m3", "m4"]
    assert [line.rsplit(": ", 1)[-1] for line in logs if "丢弃最早的一条" in line] == ["m1", "m2"]
