"""func_manager/workload_relay.py 的单元测试:浏览器 ⇄ master ⇄ agent 的订阅转发。不需要真实 agent。"""
import asyncio
import json
import pathlib
import sys
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from func_manager import workload_relay as relay_module  # noqa: E402


def agent_ws():
    ws = Mock()
    ws.closed = False
    ws.send_json = AsyncMock()
    return ws


def agent_messages(ws):
    return [call.args[0] for call in ws.send_json.call_args_list]


def make_sub(relay, env="prod", namespace=""):
    sub = relay_module._Subscriber("s1", env, namespace, Mock())
    relay.subs[sub.sub_id] = sub
    return sub


def drain(sub):
    items = []
    while not sub.queue.empty():
        items.append(sub.queue.get_nowait())
    return items


@pytest.mark.asyncio
async def test_dispatch_forwards_raw_message_and_marks_snapshot():
    ws = agent_ws()
    relay = relay_module.WorkloadRelay({"prod": {"ws": ws}})
    sub = make_sub(relay)
    sub.snapshot_check = Mock()
    raw = json.dumps({"type": "workload_snapshot", "sub_id": "s1"})
    relay.dispatch("prod", json.loads(raw), raw)
    assert drain(sub) == [raw]
    assert sub.got_snapshot is True
    sub.snapshot_check.cancel.assert_called_once()


@pytest.mark.asyncio
async def test_dispatch_unknown_sub_unsubscribes_with_rate_limit():
    ws = agent_ws()
    relay = relay_module.WorkloadRelay({"prod": {"ws": ws}})
    data = {"type": "workload_update", "sub_id": "gone"}
    relay.dispatch("prod", data, json.dumps(data))
    relay.dispatch("prod", data, json.dumps(data))
    await asyncio.sleep(0)
    assert agent_messages(ws) == [{"type": "workload_unsub", "sub_id": "gone"}]


@pytest.mark.asyncio
async def test_dispatch_ignores_other_env():
    ws = agent_ws()
    relay = relay_module.WorkloadRelay({"prod": {"ws": ws}, "test": {"ws": agent_ws()}})
    sub = make_sub(relay, env="prod")
    data = {"type": "workload_update", "sub_id": "s1"}
    relay.dispatch("test", data, json.dumps(data))
    assert drain(sub) == []


@pytest.mark.asyncio
async def test_queue_overflow_requests_resync(monkeypatch):
    monkeypatch.setattr(relay_module, "QUEUE_SIZE", 2)
    monkeypatch.setattr(relay_module, "RESYNC_INTERVAL", 0)
    ws = agent_ws()
    relay = relay_module.WorkloadRelay({"prod": {"ws": ws}})
    sub = make_sub(relay)
    sub.pods_for = {("ns", "web")}
    for i in range(3):
        relay._push(sub, f"msg{i}")
    assert drain(sub) == [{"type": "status", "state": "resyncing"}]
    await sub.resync_task
    [msg] = agent_messages(ws)
    assert msg == {"type": "workload_sub", "sub_id": "s1", "namespace": "", "pods_for": [["ns", "web"]], "resync": True}


@pytest.mark.asyncio
async def test_agent_reconnect_resends_subscriptions():
    ws = agent_ws()
    relay = relay_module.WorkloadRelay({"prod": {"ws": ws}})
    sub = make_sub(relay, namespace="ns")
    sub.pods_for = {("ns", "web")}
    sub.got_snapshot = True
    await relay.on_agent_connected("prod")
    assert agent_messages(ws) == [
        {"type": "workload_sub", "sub_id": "s1", "namespace": "ns", "pods_for": [["ns", "web"]], "resync": False}
    ]
    assert drain(sub) == [{"type": "status", "state": "syncing"}]
    assert sub.got_snapshot is False
    sub.cancel_timers()


@pytest.mark.asyncio
async def test_agent_disconnect_only_for_current_connection():
    old_ws, new_ws = agent_ws(), agent_ws()
    clients = {"prod": {"ws": new_ws}}
    relay = relay_module.WorkloadRelay(clients)
    sub = make_sub(relay)
    relay.on_agent_disconnected("prod", old_ws)  # 旧连接迟到的收尾
    assert drain(sub) == []
    relay.on_agent_disconnected("prod", new_ws)
    assert drain(sub) == [{"type": "status", "state": "agent_offline"}]


@pytest.mark.asyncio
async def test_snapshot_timeout_reports_unsupported(monkeypatch):
    monkeypatch.setattr(relay_module, "SNAPSHOT_WAIT", 0.01)
    ws = agent_ws()
    relay = relay_module.WorkloadRelay({"prod": {"ws": ws}})
    sub = make_sub(relay)
    await relay._subscribe_agent(sub)
    await asyncio.sleep(0.05)
    assert drain(sub) == [{"type": "status", "state": "unsupported"}]


@pytest.mark.asyncio
async def test_browser_messages_validated_and_limited(monkeypatch):
    monkeypatch.setattr(relay_module, "MAX_PODS_FOR", 2)
    relay = relay_module.WorkloadRelay({})
    sub = make_sub(relay)
    for text in ["not json", "[]", json.dumps({"type": "watch_pods", "namespace": "ns"}), json.dumps({"type": "other"})]:
        relay._handle_browser_message(sub, text)
    assert sub.pods_for == set()
    for dep in ("a", "b", "c"):
        relay._handle_browser_message(sub, json.dumps({"type": "watch_pods", "namespace": "ns", "deployment": dep}))
    assert sub.pods_for == {("ns", "a"), ("ns", "b")}
    relay._handle_browser_message(sub, json.dumps({"type": "unwatch_pods", "namespace": "ns", "deployment": "a"}))
    assert sub.pods_for == {("ns", "b")}
    sub.cancel_timers()


@pytest.mark.asyncio
async def test_browser_websocket_end_to_end(monkeypatch):
    monkeypatch.setattr(relay_module, "SUB_DEBOUNCE", 0)
    ws = agent_ws()
    clients = {"prod": {"ws": ws}}
    relay = relay_module.WorkloadRelay(clients)
    app = web.Application()
    relay.register_routes(app)
    async with TestClient(TestServer(app)) as client:
        browser = await client.ws_connect("/ws/workload-status?env=prod&namespace=ns")
        assert await browser.receive_json(timeout=2) == {"type": "status", "state": "syncing"}
        [sub_msg] = agent_messages(ws)
        assert sub_msg["type"] == "workload_sub" and sub_msg["namespace"] == "ns" and sub_msg["pods_for"] == []

        # agent 推快照 -> 原样转给浏览器
        snapshot = {"type": "workload_snapshot", "sub_id": sub_msg["sub_id"], "synced": True, "deployments": []}
        relay.dispatch("prod", snapshot, json.dumps(snapshot))
        assert await browser.receive_json(timeout=2) == snapshot

        # 展开明细 -> 带上 pods_for 重新订阅
        await browser.send_json({"type": "watch_pods", "namespace": "ns", "deployment": "web"})
        for _ in range(50):
            if len(ws.send_json.call_args_list) >= 2:
                break
            await asyncio.sleep(0.01)
        assert agent_messages(ws)[1]["pods_for"] == [["ns", "web"]]

        await browser.close()
        for _ in range(50):
            if relay.subs == {}:
                break
            await asyncio.sleep(0.01)
        assert relay.subs == {}
        assert agent_messages(ws)[-1] == {"type": "workload_unsub", "sub_id": sub_msg["sub_id"]}


@pytest.mark.asyncio
async def test_browser_websocket_agent_offline_and_missing_env():
    relay = relay_module.WorkloadRelay({})
    app = web.Application()
    relay.register_routes(app)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/ws/workload-status")
        assert resp.status == 400
        browser = await client.ws_connect("/ws/workload-status?env=prod")
        assert await browser.receive_json(timeout=2) == {"type": "status", "state": "agent_offline"}
        await browser.close()
