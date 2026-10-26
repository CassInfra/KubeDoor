import asyncio
import base64
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import sys
from types import MappingProxyType

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from auth import middleware as auth_middleware  # noqa: E402
from auth.principal import Principal  # noqa: E402

spec = importlib.util.spec_from_file_location("ai_api", Path(__file__).parents[1] / "func_manager" / "ai_api.py")
ai_api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ai_api)

USERS = {
    "viewer": Principal("viewer", "read", user_id=1, via="internal"),
    "reader": Principal("reader", "read", user_id=2, via="internal"),
    "limited": Principal("limited", "none", user_id=3, via="internal",
                         clusters=MappingProxyType({"prod": "rw", "staging": "read"})),
}


def internal_app(monkeypatch, clients, utils):
    """真实的认证中间件 + 假的用户库:按 X-User-Name 返回预置身份。"""
    async def principal_by_name(username, via):
        return USERS.get(username)

    monkeypatch.setattr(auth_middleware.store, "principal_by_name", principal_by_name)
    app = web.Application(middlewares=[auth_middleware.create_middleware()])
    ai_api.register_routes(app, clients, utils)
    return app


def test_metrics_scope_cannot_be_overridden_and_tenant_url_is_not_rewritten():
    suffix, params = ai_api.metrics_parameters({
        "env": "production", "query": 'sum(rate(cpu{origin_prometheus="other"}[5m]))',
        "extra_label": "origin_prometheus=other", "start": 1000, "end": 4600, "step": "1m",
    }, "origin_prometheus")
    assert suffix == "query_range"
    assert params["extra_label"] == "origin_prometheus=production"
    assert params["step"] == 60


@pytest.mark.parametrize("updates", [
    {"step": 0}, {"step": ".1s"}, {"end": 1000}, {"end": 1000 + 31 * 86400},
    {"start": "2026-10-05T10:00:00"},
])
def test_metrics_invalid_or_unbounded_range_is_rejected(updates):
    body = {"env": "prod", "query": "up", "start": 1000, "end": 4600, **updates}
    with pytest.raises(ValueError):
        ai_api.metrics_parameters(body, "origin_prometheus")


@pytest.mark.asyncio
async def test_internal_routes_require_service_token_and_user_identity(monkeypatch):
    monkeypatch.setenv("AI_INTERNAL_TOKEN", "secret-test-token")
    utils = SimpleNamespace(get_k8s_aliases=AsyncMock(return_value={"offline": ["离线集群"]}),
                            PROM_TYPE="Victoria-Metrics-Cluster", PROM_K8S_TAG_KEY="origin_prometheus")
    app = internal_app(monkeypatch, {"online": {"online": True, "ver": "2"}}, utils)
    async with TestClient(TestServer(app)) as client:
        assert (await client.get("/api/ai/internal/bootstrap")).status == 401
        assert (await client.get("/api/ai/internal/bootstrap", headers={"X-Kubedoor-Token": "wrong"})).status == 401
        # 有 token 没有用户身份(系统调用)不能代表用户使用 AI 桥
        assert (await client.get("/api/ai/internal/bootstrap", headers={"X-Kubedoor-Token": "secret-test-token"})).status == 403
        # 用户不存在或已禁用
        assert (await client.get("/api/ai/internal/bootstrap", headers={
            "X-Kubedoor-Token": "secret-test-token", "X-User-Name": "ghost"})).status == 403
        response = await client.get("/api/ai/internal/bootstrap", headers={
            # X-User-Permission 由 AI 服务转发,master 不采信,只按库里的用户权限计算
            "X-Kubedoor-Token": "secret-test-token", "X-User-Name": "viewer", "X-User-Permission": "rw",
        })
        assert response.status == 200
        body = await response.json()
        assert body["username"] == "viewer" and body["permission"] == "read"
        # 库里还没有的集群(只在 clients 里)别名是空列表
        assert [(c["env"], c["online"], c["permission"], c["aliases"]) for c in body["clusters"]] == [
            ("offline", False, "read", ["离线集群"]), ("online", True, "read", [])]
        identity = await client.get("/api/ai/internal/identity", headers={
            "X-Kubedoor-Token": "secret-test-token", "X-User-Name": "limited", "X-User-Permission": "admin",
        })
        assert identity.status == 200
        identity_body = await identity.json()
        assert identity_body["username"] == "limited"
        assert identity_body["permission"] == "none"
        assert identity_body["clusters"] == {"prod": "rw", "staging": "read"}


@pytest.mark.asyncio
async def test_bootstrap_hides_clusters_without_permission_and_execute_checks_cluster(monkeypatch):
    monkeypatch.setenv("AI_INTERNAL_TOKEN", "secret-test-token")
    utils = SimpleNamespace(get_k8s_aliases=AsyncMock(return_value={"prod": ["生产"], "staging": [], "secret": ["机密"]}),
                            PROM_TYPE="Victoria-Metrics-Cluster", PROM_K8S_TAG_KEY="origin_prometheus")
    sent = []

    def socket_for(env):
        class Socket:
            async def send_json(self, packet):
                sent.append(packet)
                connection = clients[env]
                connection["response_queue"][packet["request_id"]] = {"success": True}
                connection["response_events"][packet["request_id"]].set()
        return Socket()

    clients = {"staging": {"online": True, "ai_tools": True}, "secret": {"online": True, "ai_tools": True}}
    for env, connection in clients.items():
        connection["ws"] = socket_for(env)
    app = internal_app(monkeypatch, clients, utils)
    headers = {"X-Kubedoor-Token": "secret-test-token", "X-User-Name": "limited"}
    async with TestClient(TestServer(app)) as client:
        body = await (await client.get("/api/ai/internal/bootstrap", headers=headers)).json()
        # 看不到的集群连同别名一起隐藏
        assert [(c["env"], c["permission"], c["aliases"]) for c in body["clusters"]] == [
            ("prod", "rw", ["生产"]), ("staging", "read", [])]
        # 看不到的集群不能执行任何工具
        response = await client.post("/api/ai/internal/agent-execute?env=secret", headers=headers,
                                      json={"operation": "api", "phase": "prepare"})
        assert response.status == 403
        # 只读集群不能批准写操作
        response = await client.post("/api/ai/internal/agent-execute?env=staging", headers=headers,
                                      json={"operation": "api", "phase": "execute", "approved": True})
        assert response.status == 403
        # 只读集群可以执行只读工具,agent 收到的权限是 read
        response = await client.post("/api/ai/internal/agent-execute?env=staging", headers=headers,
                                      json={"operation": "api", "phase": "execute"})
        assert response.status == 200
        assert sent[-1]["permission"] == "read" and sent[-1]["username"] == "limited"


@pytest.mark.asyncio
async def test_deployment_control_checks_cluster_and_names_and_verifies_through_agent(monkeypatch):
    monkeypatch.setenv("AI_INTERNAL_TOKEN", "secret-test-token")
    rows = {"k8s_agent_status": {"admission": True, "admission_namespace": '["team"]', "nms_not_confirm": False, "scheduler": False},
            "k8s_res_control": None}

    async def pg_fetchrow(sql, *args):
        return next(row for table, row in rows.items() if table in sql)

    sent = []

    class Socket:
        async def send_json(self, packet):
            sent.append(packet)
            found = packet["arguments"]["path"].endswith("kubedoor-admis-configuration")
            clients["staging"]["response_queue"][packet["request_id"]] = {"success": True, "data": {"metadata": {}}} if found else \
                {"success": True, "data": {"metadata": {"labels": {}}}}
            clients["staging"]["response_events"][packet["request_id"]].set()

    clients = {"staging": {"online": True, "ai_tools": True, "ws": Socket()}}
    utils = SimpleNamespace(get_k8s_names=AsyncMock(return_value=["staging"]), pg_fetchrow=pg_fetchrow,
                            PROM_TYPE="", PROM_K8S_TAG_KEY="k8s")
    app = internal_app(monkeypatch, clients, utils)
    headers = {"X-Kubedoor-Token": "secret-test-token", "X-User-Name": "limited"}
    async with TestClient(TestServer(app)) as client:
        assert (await client.get("/api/ai/internal/deployment-control?env=secret&namespace=team&deployment=api", headers=headers)).status == 403
        assert (await client.get("/api/ai/internal/deployment-control?env=staging&namespace=../x&deployment=api", headers=headers)).status == 400
        body = await (await client.get("/api/ai/internal/deployment-control?env=staging&namespace=team&deployment=api", headers=headers)).json()
        # 只读集群也能查;核实走 agent 的只读 api GET
        assert body["data"]["state"] == "unregistered_blocked" and body["data"]["blocked"] is True
        assert body["data"]["admission"]["webhook_present"] is True and body["data"]["admission"]["namespace_ignored"] is False
        assert {packet["permission"] for packet in sent} == {"read"}


@pytest.mark.asyncio
async def test_rpc_registers_before_fast_reply_and_never_replays_timeout():
    connection = {"online": True}
    packets = []

    class ImmediateSocket:
        async def send_json(self, packet):
            packets.append(packet)
            key = packet["request_id"]
            assert key in connection["response_events"]
            assert key in connection["ai_request_ids"]
            connection["response_queue"][key] = {"success": True, "data": "ok"}
            connection["response_events"][key].set()

    connection["ws"] = ImmediateSocket()
    assert (await ai_api.agent_rpc({"prod": connection}, "prod", {"phase": "execute"}))["success"]
    assert not connection["response_events"] and not connection["ai_request_ids"]

    class SilentSocket:
        async def send_json(self, packet):
            packets.append(packet)

    connection["ws"] = SilentSocket()
    before = len(packets)
    with pytest.raises(web.HTTPGatewayTimeout):
        await ai_api.agent_rpc({"prod": connection}, "prod", {"phase": "execute"}, timeout=.01)
    assert len(packets) == before + 1
    assert not connection["response_events"] and not connection["response_queue"]


@pytest.mark.asyncio
async def test_cancelled_rpc_cleans_pending_response_state():
    connection = {"online": True, "ws": SimpleNamespace(send_json=AsyncMock())}
    task = asyncio.create_task(ai_api.agent_rpc({"prod": connection}, "prod", {}))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not connection["response_events"] and not connection["ai_request_ids"]


@pytest.mark.asyncio
async def test_rpc_timeout_includes_blocked_websocket_send():
    connection = {"online": True}

    class BlockedSocket:
        async def send_json(self, packet):
            await asyncio.Event().wait()

    connection["ws"] = BlockedSocket()
    with pytest.raises(web.HTTPGatewayTimeout):
        await ai_api.agent_rpc({"prod": connection}, "prod", {}, timeout=.01)
    assert not connection["response_events"] and not connection["ai_request_ids"]


@pytest.mark.asyncio
async def test_metrics_bridge_preserves_tenant_path_decodes_auth_and_injects_cluster(monkeypatch):
    monkeypatch.setenv("AI_INTERNAL_TOKEN", "test-token")
    received = []

    async def metrics(request):
        received.append((request.path, dict(request.query), request.headers["Authorization"]))
        return web.json_response({"status": "success", "data": {"result": []}})

    upstream = web.Application()
    upstream.router.add_get("/select/7/prometheus/api/v1/query", metrics)
    async with TestServer(upstream) as server:
        address = str(server.make_url("/select/7/prometheus")).split("://", 1)[1]
        utils = SimpleNamespace(get_k8s_names=AsyncMock(return_value=["prod"]), PROM_TYPE="Victoria-Metrics-Cluster",
                                PROM_K8S_TAG_KEY="k8s", PROM_URL=f"http://metrics-user:password%40with%3Aencoding@{address}")
        app = internal_app(monkeypatch, {}, utils)
        async with TestClient(TestServer(app)) as client:
            response = await client.post("/api/ai/internal/metrics", json={"env": "prod", "query": "sum(up)", "extra_label": "k8s=other"}, headers={
                "X-Kubedoor-Token": "test-token", "X-User-Name": "reader", "X-User-Permission": "read"})
            assert response.status == 200, await response.text()
    path, params, authorization = received[0]
    assert path == "/select/7/prometheus/api/v1/query"
    assert params == {"query": "sum(up)", "extra_label": "k8s=prod"}
    assert base64.b64decode(authorization.split()[1]).decode() == "metrics-user:password@with:encoding"
