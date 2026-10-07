import asyncio
import base64
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


spec = importlib.util.spec_from_file_location("ai_api", Path(__file__).parents[1] / "func_manager" / "ai_api.py")
ai_api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ai_api)


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
    utils = SimpleNamespace(get_k8s_names=AsyncMock(return_value=["offline"]), PROM_TYPE="Victoria-Metrics-Cluster", PROM_K8S_TAG_KEY="origin_prometheus")
    app = web.Application()
    ai_api.register_routes(app, {"online": {"online": True, "ver": "2"}}, utils)
    async with TestClient(TestServer(app)) as client:
        assert (await client.get("/api/ai/internal/bootstrap")).status == 401
        assert (await client.get("/api/ai/internal/bootstrap", headers={"X-Kubedoor-Token": "secret-test-token"})).status == 403
        response = await client.get("/api/ai/internal/bootstrap", headers={
            "X-Kubedoor-Token": "secret-test-token", "X-User-Name": "viewer", "X-User-Permission": "read",
        })
        assert response.status == 200
        body = await response.json()
        assert body["username"] == "viewer"
        assert [(c["env"], c["online"]) for c in body["clusters"]] == [("offline", False), ("online", True)]


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
        app = web.Application()
        ai_api.register_routes(app, {}, utils)
        async with TestClient(TestServer(app)) as client:
            response = await client.post("/api/ai/internal/metrics", json={"env": "prod", "query": "sum(up)", "extra_label": "k8s=other"}, headers={
                "X-Kubedoor-Token": "test-token", "X-User-Name": "reader", "X-User-Permission": "read"})
            assert response.status == 200, await response.text()
    path, params, authorization = received[0]
    assert path == "/select/7/prometheus/api/v1/query"
    assert params == {"query": "sum(up)", "extra_label": "k8s=prod"}
    assert base64.b64decode(authorization.split()[1]).decode() == "metrics-user:password@with:encoding"
