import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


spec = importlib.util.spec_from_file_location("ws_lifecycle", Path(__file__).parents[1] / "func_manager/ws_lifecycle.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Monitor:
    def __init__(self):
        self.running = False

    async def start_monitoring(self):
        self.running = True

    async def stop_monitoring(self):
        self.running = False


@pytest.mark.asyncio
async def test_monitor_launcher_return_does_not_disconnect_but_closed_ws_cleans_tasks():
    monitor, closed = Monitor(), asyncio.Event()
    finished = []

    async def process(_):
        await closed.wait()

    async def heartbeat(_):
        try:
            await asyncio.Event().wait()
        finally:
            finished.append("heartbeat")

    async def health():
        try:
            await asyncio.Event().wait()
        finally:
            finished.append("health")

    task = asyncio.create_task(module.run_connection_tasks(None, monitor, process, heartbeat, health))
    await asyncio.sleep(0)
    assert monitor.running and not task.done()
    closed.set()
    await asyncio.wait_for(task, 1)
    assert set(finished) == {"heartbeat", "health"}
    assert not monitor.running


@pytest.mark.asyncio
async def test_cancelling_connection_supervisor_stops_monitor_and_children():
    monitor = Monitor()

    async def waiting(*args):
        await asyncio.Event().wait()

    task = asyncio.create_task(module.run_connection_tasks(None, monitor, waiting, waiting, waiting))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not monitor.running


class FakeWs:
    """aiohttp ClientWebSocketResponse 的替身:block 时发送卡住(模拟 master 不读),hang_close 时关闭握手卡住"""

    def __init__(self, messages=()):
        self.closed = False
        self.sent = []
        self.block = False
        self.hang_close = False
        self.messages = list(messages)

    async def send_str(self, data):
        if self.block:
            await asyncio.Event().wait()
        self.sent.append(data)

    async def close(self):
        if self.hang_close:
            await asyncio.Event().wait()
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.messages:
            raise StopAsyncIteration
        return self.messages.pop(0)


@pytest.mark.asyncio
async def test_send_timeout_marks_connection_broken():
    raw = FakeWs()
    conn = module.MasterConnection(raw, send_timeout=0.05, ack_timeout=30)
    await conn.send_json({"type": "heartbeat"})
    assert json.loads(raw.sent[0]) == {"type": "heartbeat"}
    assert not conn.closed
    conn.check_alive()

    raw.block = True  # master 不读了:发送卡在 drain 上
    with pytest.raises(ConnectionResetError):
        await conn.send_json({"type": "heartbeat"})
    assert conn.closed and conn.broken
    raw.block = False
    with pytest.raises(ConnectionResetError):  # 判定失效后立即失败,不会再卡住其它发送方
        await conn.send_str("x")
    assert len(raw.sent) == 1
    with pytest.raises(ConnectionResetError):  # 心跳任务据此断开重连
        conn.check_alive()


@pytest.mark.asyncio
async def test_ack_timeout_applies_only_after_master_acked(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    conn = module.MasterConnection(FakeWs(), send_timeout=1, ack_timeout=30)
    now[0] += 3600
    conn.check_alive()  # 旧版 master 从不回 ack:不启用这项判断
    conn.on_ack()
    now[0] += 29
    conn.check_alive()
    now[0] += 2  # 回过 ack 之后 30 秒以上没再回:master 没在处理这条连接
    with pytest.raises(ConnectionResetError):
        conn.check_alive()
    assert conn.closed


@pytest.mark.asyncio
async def test_close_is_bounded_and_iteration_is_delegated():
    raw = FakeWs(messages=["m1", "m2"])
    conn = module.MasterConnection(raw, send_timeout=1, ack_timeout=30)
    assert [msg async for msg in conn] == ["m1", "m2"]
    raw.hang_close = True  # 对端不读,关闭握手发不出去
    await asyncio.wait_for(conn.close(0.05), 1)
