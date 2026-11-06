"""kubedoor-master.py 的 agent 接收循环:{"type": "notify"} 消息只交给 notify(截到 20000 字符、按这条连接的集群),
不等待;连接 URL 带 notify=1 时记进 clients。"""
import asyncio
import importlib.util
import pathlib
import sys
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

MASTER_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(MASTER_ROOT))


@pytest.fixture
def master(monkeypatch):
    spec = importlib.util.spec_from_file_location("kdmaster_notify_ws", MASTER_ROOT / "kubedoor-master.py")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except ImportError as exc:  # 本地没装 kubedoor-tools 等依赖时跳过(PYTHONPATH=../kubedoor-tools 可以跑)
        pytest.skip(f"无法加载 kubedoor-master.py: {exc}")
    monkeypatch.setattr(module.utils, "init_agent_status", AsyncMock())
    monkeypatch.setattr(module.namespace_cache, "_namespace_cache", {})
    return module


def make_app(master):
    app = web.Application()
    app.router.add_get("/ws", master.websocket_handler)
    return app


async def until(predicate, timeout=2.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("等待超时")
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_agent_notify_messages_go_to_notify(master, monkeypatch):
    sent = Mock(return_value=True)
    monkeypatch.setattr(master, "notify", sent)
    async with TestClient(TestServer(make_app(master))) as client:
        agent = await client.ws_connect("/ws?env=prod&ver=new&notify=1")
        await until(lambda: master.clients.get("prod", {}).get("notify") is True)
        await agent.send_json({"type": "notify", "content": "【prod】扩缩容成功"})
        await agent.send_json({"type": "notify", "content": "x" * 30000})
        await agent.send_json({"type": "notify", "content": {"not": "text"}})  # 不是文本:忽略,连接不断
        await agent.send_json({"type": "notify"})
        await agent.send_json({"type": "heartbeat"})
        assert await agent.receive_json(timeout=2) == {"type": "heartbeat_ack"}
        assert master.clients["prod"]["online"] is True
        await agent.close()
    assert [c.args for c in sent.call_args_list] == [("【prod】扩缩容成功",), ("x" * 20000,)]
    assert [c.kwargs for c in sent.call_args_list] == [{"env": "prod"}, {"env": "prod"}]


@pytest.mark.asyncio
async def test_notify_flag_follows_each_connection(master):
    async with TestClient(TestServer(make_app(master))) as client:
        agent = await client.ws_connect("/ws?env=prod&ver=new&notify=1")
        await until(lambda: master.clients.get("prod", {}).get("notify") is True)
        await agent.close()
        await until(lambda: master.clients["prod"]["online"] is False)
        agent = await client.ws_connect("/ws?env=prod&ver=old")  # 旧版 agent 不带 notify=1
        await until(lambda: master.clients["prod"]["online"] is True and master.clients["prod"]["ver"] == "old")
        assert master.clients["prod"]["notify"] is False
        await agent.close()


@pytest.mark.asyncio
async def test_notify_does_not_block_agent_loop(master, monkeypatch):
    """notify 是同步的、不等发送结果:IM 很慢时心跳照样马上回"""
    from notify import service

    calls = []

    def slow_post(*args, **kwargs):
        import time

        calls.append(args[0])
        time.sleep(0.5)
        raise RuntimeError("never mind")

    monkeypatch.setattr(service.requests, "post", slow_post)
    monkeypatch.setattr(service, "_snapshot", service.build_snapshot(
        {"default": {"type": "slack", "token": "plain:T0/B0/x", "at": ""}}))
    async with TestClient(TestServer(make_app(master))) as client:
        agent = await client.ws_connect("/ws?env=prod&notify=1")
        loop = asyncio.get_running_loop()
        started = loop.time()
        await agent.send_json({"type": "notify", "content": "慢"})
        await agent.send_json({"type": "heartbeat"})
        assert await agent.receive_json(timeout=2) == {"type": "heartbeat_ack"}
        assert loop.time() - started < 0.4
        await agent.close()
    await until(lambda: len(calls) == 1)
    await until(lambda: service._dispatcher.pending() == 0, timeout=3)  # 等后台发送结束(失败只记日志)
