"""kubedoor-master.py 里 agent 的 /ws 接入:心跳 ack / 离线判定、断线时等待中的请求立即失败、命名空间缓存"""
import asyncio
import importlib.util
import pathlib
import sys
import time
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

MASTER_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(MASTER_ROOT))


@pytest.fixture
def master(monkeypatch):
    spec = importlib.util.spec_from_file_location("kdmaster_agent_ws", MASTER_ROOT / "kubedoor-master.py")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except ImportError as exc:  # 本地没装云镜像 SDK 时跳过
        pytest.skip(f"无法加载 kubedoor-master.py: {exc}")
    monkeypatch.setattr(module.utils, "init_agent_status", AsyncMock())
    monkeypatch.setattr(module.namespace_cache, "_namespace_cache", {})
    return module


def make_app(master):
    app = web.Application()
    app.router.add_get("/ws", master.websocket_handler)
    app.router.add_route("*", "/api/{tail:.*}", master.http_handler)
    return app


async def until(predicate, timeout=2.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("等待超时")
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_heartbeat_is_acked_and_restores_online(master):
    async with TestClient(TestServer(make_app(master))) as client:
        agent = await client.ws_connect("/ws?env=prod&ver=test")
        await agent.send_json({"type": "heartbeat"})
        assert await agent.receive_json(timeout=2) == {"type": "heartbeat_ack"}

        master.clients["prod"]["online"] = False  # heartbeat_check 判了离线
        await agent.send_json({"type": "heartbeat"})
        await agent.receive_json(timeout=2)
        assert master.clients["prod"]["online"] is True

        master.clients["prod"]["ws"] = object()  # 已经被新连接替换:旧连接的心跳不能把 env 标成在线
        master.clients["prod"]["online"] = False
        await agent.send_json({"type": "heartbeat"})
        await agent.receive_json(timeout=2)
        assert master.clients["prod"]["online"] is False
        await agent.close()


@pytest.mark.asyncio
async def test_heartbeat_check_leaves_slack_over_heartbeat_interval(master):
    now = time.time()
    master.clients["prod"] = {"online": True, "last_heartbeat": now - 10}
    master.clients["test"] = {"online": True, "last_heartbeat": now - master.AGENT_HEARTBEAT_TIMEOUT - 1}
    task = asyncio.create_task(master.heartbeat_check())
    await asyncio.sleep(0.05)
    task.cancel()
    assert master.clients["prod"]["online"] is True  # 两次心跳没收到不算离线(以前 5 秒就判)
    assert master.clients["test"]["online"] is False


@pytest.mark.asyncio
async def test_disconnect_fails_pending_requests_immediately(master):
    async with TestClient(TestServer(make_app(master))) as client:
        agent = await client.ws_connect("/ws?env=prod")
        await until(lambda: "prod" in master.clients)
        events = master.clients["prod"].setdefault("response_events", {})
        normal, ai = asyncio.Event(), asyncio.Event()
        events["r1"], events["r2"] = normal, ai
        master.clients["prod"]["ai_request_ids"] = {"r2"}
        await agent.close()
        await asyncio.wait_for(asyncio.gather(normal.wait(), ai.wait()), 2)
        queue = master.clients["prod"]["response_queue"]
        assert queue["r1"] == {"success": False, "error": "与 agent 的连接已断开,请求未完成"}
        assert queue["r2"]["success"] is False and queue["r2"]["error"]["code"] == "AGENT_DISCONNECTED"
        assert master.clients["prod"]["online"] is False


@pytest.mark.asyncio
async def test_http_request_fails_fast_when_agent_disconnects(master):
    async with TestClient(TestServer(make_app(master))) as client:
        agent = await client.ws_connect("/ws?env=prod")
        request = asyncio.create_task(client.get("/api/agent/services?env=prod"))
        message = await agent.receive_json(timeout=2)
        assert message["type"] == "request" and message["path"] == "/api/agent/services"
        await agent.close()  # agent 收到请求后断线
        resp = await asyncio.wait_for(request, 3)  # 以前要等满 300 秒
        body = await resp.json()
        assert body["success"] is False and body["error"] == "与 agent 的连接已断开,请求未完成"


@pytest.mark.asyncio
async def test_namespace_errors_are_not_cached(master):
    async with TestClient(TestServer(make_app(master))) as client:
        agent = await client.ws_connect("/ws?env=prod")

        async def ask(response):
            request = asyncio.create_task(client.get("/api/agent/namespaces?env=prod"))
            message = await agent.receive_json(timeout=2)
            await agent.send_json({"type": "response", "request_id": message["request_id"], "response": response})
            return await (await asyncio.wait_for(request, 3)).json()

        # agent 返回失败:不能把错误信息当成命名空间列表缓存一小时
        failed = await ask({"success": False, "data": "Kubernetes API错误: Unauthorized"})
        assert failed["success"] is False
        assert master.namespace_cache.get_namespaces_from_cache("prod") is None

        ok = await ask({"success": True, "data": ["default", "kube-system"]})
        assert ok == {"success": True, "data": ["default", "kube-system"]}
        assert master.namespace_cache.get_namespaces_from_cache("prod") == ["default", "kube-system"]
        await agent.close()


@pytest.mark.asyncio
async def test_pod_logs_are_relayed_without_blocking_agent_loop(master, monkeypatch):
    dispatched = []
    monkeypatch.setattr(master.pod_log_relay, "dispatch", lambda env, data, raw: dispatched.append((env, data, raw)))
    async with TestClient(TestServer(make_app(master))) as client:
        agent = await client.ws_connect("/ws?env=prod")
        await agent.send_json({"type": "pod_logs", "connection_id": "c1", "log": "x"})
        await agent.send_json({"type": "heartbeat"})
        assert await agent.receive_json(timeout=2) == {"type": "heartbeat_ack"}
        assert dispatched and dispatched[0][:2] == ("prod", {"type": "pod_logs", "connection_id": "c1", "log": "x"})
        await agent.close()
