"""func_manager/pod_exec_relay.py 的单元测试:浏览器 ⇄ master ⇄ agent 的终端转发。不需要真实 agent。"""
import asyncio
import base64
import json
import pathlib
import sys
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from func_manager import pod_exec_relay as relay_module  # noqa: E402

URL = "/ws/pod-exec?env=prod&namespace=ns&pod_name=web-1"


@pytest.fixture(autouse=True)
def no_audit(monkeypatch):
    record = Mock()
    monkeypatch.setattr(relay_module.audit, "record_bg", record)
    return record


def agent_ws():
    ws = Mock()
    ws.closed = False
    ws.send_json = AsyncMock()
    return ws


def agent_messages(ws):
    return [call.args[0] for call in ws.send_json.call_args_list]


def make_session(relay, env="prod", session_id="s1", username="alice"):
    session = relay_module._Session(session_id, env, Mock(), username)
    relay.sessions[session_id] = session
    return session


def drain(session):
    items = []
    while not session.queue.empty():
        items.append(session.queue.get_nowait())
    return items


def agent_event(event, session_id="s1", **fields):
    return {"type": "pod_exec", "event": event, "session_id": session_id, **fields}


async def until(predicate, timeout=2.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("等待超时")
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_dispatch_queues_output_and_events():
    relay = relay_module.PodExecRelay({"prod": {"ws": agent_ws()}})
    session = make_session(relay)
    session.reply_check = Mock()
    relay.dispatch("prod", agent_event("accepted"))
    relay.dispatch("prod", agent_event("started", container="app", containers=["app", "istio-proxy"]))
    relay.dispatch("prod", agent_event("output", data="aGk="))
    relay.dispatch("prod", agent_event("exit", code=130, message=""))
    relay.dispatch("prod", agent_event("output", data="aGk="))  # 结束之后的丢弃
    assert drain(session) == [
        {"type": "started", "container": "app", "containers": ["app", "istio-proxy"]},
        "aGk=",
        {"type": "exit", "code": 130, "message": ""},
    ]
    assert session.replied and session.ended and session.closing
    session.reply_check.cancel.assert_called_once()


@pytest.mark.asyncio
async def test_dispatch_unknown_session_stops_agent_with_rate_limit():
    ws = agent_ws()
    relay = relay_module.PodExecRelay({"prod": {"ws": ws}})
    relay.dispatch("prod", agent_event("output", "gone", data="aGk="))
    relay.dispatch("prod", agent_event("output", "gone", data="aGk="))
    relay.dispatch("prod", agent_event("exit", "ended"))  # 已经退出的不用再 stop
    await asyncio.sleep(0)
    assert agent_messages(ws) == [{"type": "pod_exec", "action": "stop", "session_id": "gone"}]


@pytest.mark.asyncio
async def test_dispatch_ignores_other_env():
    relay = relay_module.PodExecRelay({"prod": {"ws": agent_ws()}, "test": {"ws": agent_ws()}})
    session = make_session(relay, env="prod")
    relay.dispatch("test", agent_event("output", data="aGk="))
    assert drain(session) == []


@pytest.mark.asyncio
async def test_queue_overflow_disconnects_browser(monkeypatch):
    monkeypatch.setattr(relay_module, "QUEUE_SIZE", 2)
    relay = relay_module.PodExecRelay({})
    session = make_session(relay)
    for _ in range(3):
        relay._push(session, "aGk=")
    relay._push(session, {"type": "exit", "code": 0, "message": ""})
    assert drain(session) == [{"type": "error", "message": "浏览器接收过慢,终端已断开"}]


@pytest.mark.asyncio
async def test_agent_disconnect_only_for_current_connection():
    old_ws, new_ws = agent_ws(), agent_ws()
    relay = relay_module.PodExecRelay({"prod": {"ws": new_ws}})
    session = make_session(relay)
    other = make_session(relay, env="test", session_id="s2")
    relay.on_agent_disconnected("prod", old_ws)  # 旧连接迟到的收尾
    assert drain(session) == []
    relay.on_agent_disconnected("prod", new_ws)
    assert drain(session) == [{"type": "error", "message": "与 agent 的连接已断开,终端已关闭"}]
    assert session.ended and drain(other) == []


@pytest.mark.asyncio
async def test_reply_timeout_reports_old_agent():
    relay = relay_module.PodExecRelay({})
    session = make_session(relay)
    relay._reply_timeout("s1")
    [error] = drain(session)
    assert error["type"] == "error" and "升级 kubedoor-agent" in error["message"]
    session.replied = True
    relay._reply_timeout("s1")
    assert drain(session) == []


@pytest.mark.asyncio
async def test_browser_messages_validated_before_forwarding():
    ws = agent_ws()
    relay = relay_module.PodExecRelay({"prod": {"ws": ws}})
    session = make_session(relay)
    for text in ["not json", "[]", json.dumps({"type": "stdin", "data": ""}), json.dumps({"type": "other"}),
                 json.dumps({"type": "resize", "cols": 0, "rows": 10}), json.dumps({"type": "resize", "cols": "80"}),
                 json.dumps({"type": "ack", "bytes": -1}), json.dumps({"type": "ack", "bytes": True})]:
        await relay._handle_browser_message(session, text)
    assert agent_messages(ws) == []
    await relay._handle_browser_message(session, json.dumps({"type": "stdin", "data": "ls\r"}))
    await relay._handle_browser_message(session, json.dumps({"type": "stdin", "data": "\x1b[M", "binary": True}))
    await relay._handle_browser_message(session, json.dumps({"type": "resize", "cols": 120, "rows": 40}))
    await relay._handle_browser_message(session, json.dumps({"type": "ack", "bytes": 32768}))
    base = {"type": "pod_exec", "session_id": "s1"}
    assert agent_messages(ws) == [
        dict(base, action="stdin", data="ls\r"),
        dict(base, action="stdin", data="\x1b[M", binary=True),
        dict(base, action="resize", cols=120, rows=40),
        dict(base, action="ack", bytes=32768),
    ]


@pytest.mark.asyncio
async def test_browser_websocket_end_to_end(no_audit):
    ws = agent_ws()
    relay = relay_module.PodExecRelay({"prod": {"ws": ws}})
    app = web.Application()
    relay.register_routes(app)
    async with TestClient(TestServer(app)) as client:
        browser = await client.ws_connect(URL + "&container=app&shell=bash&cols=100&rows=30")
        await until(lambda: agent_messages(ws))
        [start] = agent_messages(ws)
        session_id = start.pop("session_id")
        assert start == {"type": "pod_exec", "action": "start", "kind": "pod", "namespace": "ns", "pod_name": "web-1",
                         "container": "app", "shell": "bash", "cols": 100, "rows": 30}
        assert no_audit.call_args.args == ("", "pod.exec")
        assert no_audit.call_args.kwargs["target"] == "ns/web-1/app"

        relay.dispatch("prod", agent_event("accepted", session_id))
        relay.dispatch("prod", agent_event("started", session_id, container="app", containers=["app"]))
        assert await browser.receive_json(timeout=2) == {"type": "started", "container": "app", "containers": ["app"]}
        raw = "你".encode() + b"\x1b[0m"
        relay.dispatch("prod", agent_event("output", session_id, data=base64.b64encode(raw).decode()))
        msg = await browser.receive(timeout=2)
        assert msg.type == WSMsgType.BINARY and msg.data == raw  # 终端输出是二进制帧

        await browser.send_json({"type": "stdin", "data": "exit\r"})
        await until(lambda: len(agent_messages(ws)) == 2)
        assert agent_messages(ws)[1] == {"type": "pod_exec", "action": "stdin", "session_id": session_id,
                                         "data": "exit\r"}

        relay.dispatch("prod", agent_event("exit", session_id, code=0, message=""))
        assert await browser.receive_json(timeout=2) == {"type": "exit", "code": 0, "message": ""}
        assert (await browser.receive(timeout=2)).type in (WSMsgType.CLOSE, WSMsgType.CLOSED)
        await until(lambda: relay.sessions == {})
        assert len(agent_messages(ws)) == 2  # shell 已经退出,不用再发 stop


@pytest.mark.asyncio
async def test_browser_close_stops_agent_session():
    ws = agent_ws()
    relay = relay_module.PodExecRelay({"prod": {"ws": ws}})
    app = web.Application()
    relay.register_routes(app)
    async with TestClient(TestServer(app)) as client:
        browser = await client.ws_connect(URL)
        await until(lambda: agent_messages(ws))
        session_id = agent_messages(ws)[0]["session_id"]
        assert agent_messages(ws)[0]["shell"] == "auto" and agent_messages(ws)[0]["cols"] == 80
        await browser.close()
        await until(lambda: relay.sessions == {})
        assert agent_messages(ws)[-1] == {"type": "pod_exec", "action": "stop", "session_id": session_id}


@pytest.mark.asyncio
async def test_browser_websocket_rejections(monkeypatch):
    ws = agent_ws()
    relay = relay_module.PodExecRelay({"prod": {"ws": ws}})
    app = web.Application()
    relay.register_routes(app)
    async with TestClient(TestServer(app)) as client:
        for bad in ("/ws/pod-exec?env=prod&namespace=ns", URL + "&shell=zsh", URL + "&container=Bad_Name",
                    "/ws/pod-exec?env=prod&namespace=ns&pod_name=../x"):
            assert (await client.get(bad)).status == 400

        offline = await client.ws_connect("/ws/pod-exec?env=test&namespace=ns&pod_name=web-1")
        assert await offline.receive_json(timeout=2) == {"type": "error", "message": "集群 test 的 agent 不在线"}

        monkeypatch.setattr(relay_module, "MAX_SESSIONS_PER_USER", 1)
        first = await client.ws_connect(URL)
        await until(lambda: relay.sessions)
        second = await client.ws_connect(URL)
        assert await second.receive_json(timeout=2) == {"type": "error", "message": "每个账号最多同时打开 1 个终端"}
        await first.close()
        await until(lambda: relay.sessions == {})

        ws.send_json.side_effect = ConnectionResetError("gone")
        failed = await client.ws_connect(URL)
        assert await failed.receive_json(timeout=2) == {"type": "error", "message": "发送到集群 prod 的 agent 失败"}


@pytest.mark.asyncio
async def test_node_exec_start_payload_and_audit(no_audit):
    ws = agent_ws()
    relay = relay_module.PodExecRelay({"prod": {"ws": ws}})
    app = web.Application()
    relay.register_routes(app)
    async with TestClient(TestServer(app)) as client:
        browser = await client.ws_connect("/ws/node-exec?env=prod&node=10.0.0.1&shell=bash&cols=90&rows=24")
        await until(lambda: agent_messages(ws))
        start = agent_messages(ws)[0]
        session_id = start.pop("session_id")
        assert start == {"type": "pod_exec", "action": "start", "kind": "node", "node_name": "10.0.0.1",
                         "shell": "bash", "cols": 90, "rows": 24}
        assert no_audit.call_args.args == ("", "node.exec")
        assert no_audit.call_args.kwargs["target"] == "node/10.0.0.1"

        # 输出 / 退出按同一套 session 机制转发
        relay.dispatch("prod", agent_event("started", session_id, container="shell", containers=["shell"]))
        assert await browser.receive_json(timeout=2) == {"type": "started", "container": "shell",
                                                         "containers": ["shell"]}
        await browser.close()
        await until(lambda: relay.sessions == {})
        assert agent_messages(ws)[-1] == {"type": "pod_exec", "action": "stop", "session_id": session_id}


@pytest.mark.asyncio
async def test_node_exec_rejects_bad_node():
    relay = relay_module.PodExecRelay({"prod": {"ws": agent_ws()}})
    app = web.Application()
    relay.register_routes(app)
    async with TestClient(TestServer(app)) as client:
        for bad in ("/ws/node-exec?env=prod", "/ws/node-exec?env=prod&node=Bad_Node",
                    "/ws/node-exec?env=prod&node=10.0.0.1&shell=zsh"):
            assert (await client.get(bad)).status == 400
