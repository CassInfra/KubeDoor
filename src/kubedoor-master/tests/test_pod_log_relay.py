"""func_manager/pod_log_relay.py 的单元测试:浏览器 ⇄ master ⇄ agent 的 Pod 日志转发。不需要真实 agent。"""
import asyncio
import json
import pathlib
import sys
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from func_manager import pod_log_relay as relay_module  # noqa: E402

URL = "/ws/pod-logs?env=prod&namespace=ns&pod_name=web-1"


def agent_ws():
    ws = Mock()
    ws.closed = False
    ws.send_json = AsyncMock()
    return ws


def agent_messages(ws):
    return [call.args[0] for call in ws.send_json.call_args_list]


def make_conn(relay, env="prod", connection_id="c1"):
    conn = relay_module._LogConnection(connection_id, env, Mock())
    relay.conns[connection_id] = conn
    return conn


def drain(conn):
    items = []
    while not conn.queue.empty():
        items.append(conn.queue.get_nowait())
    return items


def log_raw(connection_id="c1", text="line"):
    return json.dumps({"type": "pod_logs", "connection_id": connection_id, "pod_name": "web-1", "log": text})


async def until(predicate, timeout=2.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("等待超时")
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_dispatch_queues_raw_message_without_awaiting():
    relay = relay_module.PodLogRelay({"prod": {"ws": agent_ws(), "online": True}})
    conn = make_conn(relay)
    raw = log_raw()
    relay.dispatch("prod", json.loads(raw), raw)
    relay.dispatch("test", json.loads(raw), raw)  # 别的集群发来的同名 id 不转
    assert drain(conn) == [raw]


@pytest.mark.asyncio
async def test_unknown_connection_stops_agent_stream_with_rate_limit():
    ws = agent_ws()
    relay = relay_module.PodLogRelay({"prod": {"ws": ws, "online": False}})  # 不看 online,连接还在就发
    for _ in range(3):
        relay.dispatch("prod", json.loads(log_raw("gone")), log_raw("gone"))
    relay.dispatch("prod", {"type": "pod_logs", "connection_id": "ended", "error": "x"}, "")  # 已经结束的不用 stop
    relay.dispatch("prod", {"type": "pod_logs", "connection_id": "ended", "status": "disconnected"}, "")
    await asyncio.sleep(0)
    assert agent_messages(ws) == [{"type": "stop_pod_logs", "connection_id": "gone"}]


@pytest.mark.asyncio
async def test_queue_overflow_disconnects_browser(monkeypatch):
    monkeypatch.setattr(relay_module, "QUEUE_SIZE", 3)
    relay = relay_module.PodLogRelay({})
    conn = make_conn(relay)
    for _ in range(5):
        relay._push(conn, log_raw())
    items = drain(conn)
    assert json.loads(items[0]) == {"type": "pod_logs", "connection_id": "c1", "error": "浏览器接收日志过慢,连接已断开"}
    assert items[1] is relay_module._CLOSE and len(items) == 2
    relay._push(conn, log_raw())  # 断开前的消息不再入队
    assert drain(conn) == []


@pytest.mark.asyncio
async def test_agent_disconnect_only_for_current_connection():
    old_ws, new_ws = agent_ws(), agent_ws()
    relay = relay_module.PodLogRelay({"prod": {"ws": new_ws}})
    conn = make_conn(relay)
    other = make_conn(relay, env="test", connection_id="c2")
    relay.on_agent_disconnected("prod", old_ws)  # 旧连接迟到的收尾
    assert drain(conn) == []
    relay.on_agent_disconnected("prod", new_ws)
    error, close = drain(conn)
    assert json.loads(error)["error"] == "与 agent 的连接已断开,日志流已停止" and close is relay_module._CLOSE
    assert conn.ended and drain(other) == []


@pytest.mark.asyncio
async def test_dispatch_text_broadcasts_legacy_plain_logs():
    relay = relay_module.PodLogRelay({})
    a, b = make_conn(relay), make_conn(relay, connection_id="c2")
    other = make_conn(relay, env="test", connection_id="c3")
    relay.dispatch_text("prod", "plain log")
    assert drain(a) == drain(b) == ["plain log"] and drain(other) == []


@pytest.mark.asyncio
async def test_browser_websocket_end_to_end():
    ws = agent_ws()
    relay = relay_module.PodLogRelay({"prod": {"ws": ws, "online": True}})
    app = web.Application()
    relay.register_routes(app)
    async with TestClient(TestServer(app)) as client:
        browser = await client.ws_connect(URL + "&container=app")
        await until(lambda: agent_messages(ws))
        [start] = agent_messages(ws)
        connection_id = start.pop("connection_id")
        assert len(connection_id) == 32  # uuid,不再用秒级时间戳(同一秒打开两次会撞车)
        assert start == {"type": "start_pod_logs", "namespace": "ns", "pod_name": "web-1", "container": "app"}

        raw = log_raw(connection_id, "hello")
        relay.dispatch("prod", json.loads(raw), raw)
        assert await browser.receive_str(timeout=2) == raw  # 原样转发

        await browser.close()
        await until(lambda: relay.conns == {})
        assert agent_messages(ws)[-1] == {"type": "stop_pod_logs", "connection_id": connection_id}


@pytest.mark.asyncio
async def test_browser_close_stops_agent_even_when_marked_offline():
    ws = agent_ws()
    clients = {"prod": {"ws": ws, "online": True}}
    relay = relay_module.PodLogRelay(clients)
    app = web.Application()
    relay.register_routes(app)
    async with TestClient(TestServer(app)) as client:
        browser = await client.ws_connect(URL)
        await until(lambda: agent_messages(ws))
        connection_id = agent_messages(ws)[0]["connection_id"]
        clients["prod"]["online"] = False  # 心跳判离线的那一刻浏览器关闭:以前这里不发 stop,日志流就泄漏了
        await browser.close()
        await until(lambda: relay.conns == {})
        assert agent_messages(ws)[-1] == {"type": "stop_pod_logs", "connection_id": connection_id}


@pytest.mark.asyncio
async def test_agent_disconnect_closes_browser_without_stop():
    ws = agent_ws()
    clients = {"prod": {"ws": ws, "online": True}}
    relay = relay_module.PodLogRelay(clients)
    app = web.Application()
    relay.register_routes(app)
    async with TestClient(TestServer(app)) as client:
        browser = await client.ws_connect(URL)
        await until(lambda: agent_messages(ws))
        relay.on_agent_disconnected("prod", ws)
        assert json.loads(await browser.receive_str(timeout=2))["error"] == "与 agent 的连接已断开,日志流已停止"
        assert (await browser.receive(timeout=2)).type in (WSMsgType.CLOSE, WSMsgType.CLOSED)
        await until(lambda: relay.conns == {})
        assert len(agent_messages(ws)) == 1  # agent 那边已经全停了,不用再发 stop


@pytest.mark.asyncio
async def test_browser_websocket_rejections(monkeypatch):
    ws = agent_ws()
    relay = relay_module.PodLogRelay({"prod": {"ws": ws, "online": True}, "test": {"ws": ws, "online": False}})
    app = web.Application()
    relay.register_routes(app)
    async with TestClient(TestServer(app)) as client:
        assert (await client.get("/ws/pod-logs?env=prod&namespace=ns")).status == 400
        assert (await client.get("/ws/pod-logs?env=test&namespace=ns&pod_name=web-1")).status == 404
        assert (await client.get("/ws/pod-logs?env=none&namespace=ns&pod_name=web-1")).status == 404

        monkeypatch.setattr(relay_module, "MAX_CONNECTIONS_PER_ENV", 1)
        first = await client.ws_connect(URL)
        await until(lambda: relay.conns)
        assert (await client.get(URL)).status == 503
        await first.close()
        await until(lambda: relay.conns == {})

        ws.send_json.side_effect = ConnectionResetError("gone")
        failed = await client.ws_connect(URL)
        assert json.loads(await failed.receive_str(timeout=2))["error"] == "发送到集群 prod 的 agent 失败"
