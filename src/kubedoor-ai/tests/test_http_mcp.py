import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import openai
import pytest
from langchain_core.messages import AIMessage
from starlette.testclient import TestClient

from kubedoor_ai.domain import AIError
from kubedoor_ai.http import create_app
from kubedoor_ai.service import Service
import kubedoor_ai.service as service_module


HEADERS = {"X-Kubedoor-Token": "internal", "X-User-Name": "reader", "X-User-Permission": "read", "Accept": "application/json, text/event-stream"}


def decode(response):
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        return json.loads(next(line[6:] for line in response.text.splitlines() if line.startswith("data: ")))
    return response.json()


def test_http_identity_and_mcp_lifespan_preserve_all_legacy_tools():
    fake = SimpleNamespace(bootstrap=AsyncMock(return_value={"clusters": [{"env": "test", "online": True}]}))
    app = create_app(fake, token="internal")
    with TestClient(app) as client:
        assert client.get("/health").json() == {"ok": True}
        assert client.get("/api/ai/skills").status_code == 401
        assert client.get("/api/ai/skills", headers=HEADERS).status_code == 200
        assert client.post("/api/ai/connections/parse", headers=HEADERS, json={"content": "x"}).status_code == 403
        init = client.post("/mcp", headers=HEADERS, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}})
        assert init.status_code == 200
        headers = {**HEADERS, "Mcp-Session-Id": init.headers["mcp-session-id"]}
        listing = decode(client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}))
        tools = {t["name"]: t for t in listing["result"]["tools"]}
        legacy = {"get_pod_jvm_dump", "get_pod_jvm_mem", "get_pod_jvm_jstack", "get_pod_jvm_jfr", "delete_pod", "modify_pod", "restart_deployment", "scale_deployment", "update_deployment", "get_namespaces_list", "get_deployments_info", "get_k8s_list", "get_k8s_nodes", "get_k8s_events", "get_pods", "get_pods_logs"}
        assert legacy.issubset(tools)
        assert tools["scale_deployment"]["inputSchema"]["required"] == ["namespace", "deployment", "replicas", "k8s"]
        result = decode(client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "get_k8s_list", "arguments": {}}}))
        assert result["result"]["structuredContent"]["data"]["test"]["online"] is True
        assert fake.bootstrap.await_args.args[0].username == "reader"
        assert {"/sse", "/messages", "/mcp"}.issubset({r.path for r in app.routes})


PROVIDER = {"base_url": "https://llm.example.com/custom/v1/chat/completions", "api_key": "private-provider-key", "model": "deepseek-flash"}


def probe_model(monkeypatch, result=None, error=None):
    model = SimpleNamespace(ainvoke=AsyncMock(side_effect=error, return_value=result))
    model.bind_tools = Mock(return_value=model)
    factory = Mock(return_value=model)
    monkeypatch.setattr(service_module, "make_model", factory)
    return model, factory


def status_error(status, fields):
    request = httpx.Request("POST", "https://supplier.example/v1/chat/completions?private_query=never-log", headers={"Authorization": "Bearer private-provider-key"})
    response = httpx.Response(status, request=request, json={"error": fields})
    return openai.APIStatusError("exception includes private-provider-key and raw-request-body", response=response, body={"error": fields, "debug": "raw-request-body"})


@pytest.mark.asyncio
@pytest.mark.parametrize("status,fields,code", [
    (404, {"message": "route or model missing", "code": "not_found"}, "provider_not_found"),
    (401, {"message": "invalid authentication"}, "provider_auth_failed"),
    (403, {"message": "model access forbidden"}, "provider_auth_failed"),
    (400, {"message": "requested model does not exist", "param": "model"}, "provider_request_rejected"),
    (400, {"message": "tools are not supported", "param": "tools"}, "provider_tool_calling_unsupported"),
    (429, {"message": "quota exceeded"}, "provider_rate_limited"),
    (503, {"message": "upstream unavailable"}, "provider_unavailable"),
])
async def test_provider_upstream_status_diagnosis_does_not_guess_model_or_tool_failure(monkeypatch, status, fields, code):
    model, _ = probe_model(monkeypatch, error=status_error(status, fields))
    with pytest.raises(AIError) as failed:
        await Service(None, None, None).test_provider(PROVIDER)
    assert failed.value.code == code
    assert failed.value.status == 422
    assert failed.value.details["upstream_status"] == status
    assert failed.value.details["diagnostic_id"]
    if status == 404:
        assert "模型名" in str(failed.value) and "API 地址" in str(failed.value)
    model.ainvoke.assert_awaited_once()
    assert model.bind_tools.call_args.kwargs == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("error,code", [
    (openai.APITimeoutError(request=httpx.Request("POST", "https://supplier.example/v1/chat/completions")), "provider_timeout"),
    (openai.APIConnectionError(request=httpx.Request("POST", "https://supplier.example/v1/chat/completions")), "provider_connection_failed"),
])
async def test_provider_network_diagnosis(monkeypatch, error, code):
    probe_model(monkeypatch, error=error)
    with pytest.raises(AIError) as failed:
        await Service(None, None, None).test_provider(PROVIDER)
    assert failed.value.code == code
    assert "upstream_status" not in failed.value.details


def test_provider_http_error_exposes_safe_supplier_hint_and_matching_log_id(monkeypatch, caplog):
    message = "Unsupported parameter model; echoed private-provider-key; URL https://supplier.example/v1/chat/completions?private_query=never-log"
    probe_model(monkeypatch, error=status_error(400, {"message": message, "param": "model", "code": "invalid_request"}))
    with TestClient(create_app(Service(None, None, None), token="internal")) as client:
        response = client.post("/api/ai/provider/test", headers=HEADERS, json={"provider": PROVIDER})
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "provider_request_rejected"
    assert "Unsupported parameter model" in error["message"]
    assert error["details"]["upstream_param"] == "model"
    assert error["details"]["diagnostic_id"] in caplog.text
    assert error["details"]["diagnostic_id"] in error["message"]
    assert '"upstream_status": 400' in caplog.text
    for output in (response.text, caplog.text):
        assert "private-provider-key" not in output
        assert "private_query" not in output
        assert "never-log" not in output
        assert "raw-request-body" not in output
    assert "Unsupported parameter model" not in caplog.text


@pytest.mark.asyncio
async def test_provider_probe_total_deadline_stops_a_stalled_response(monkeypatch):
    model, _ = probe_model(monkeypatch)
    stopped = asyncio.Event()

    async def stalled(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    model.ainvoke.side_effect = stalled
    real_timeout, limits = asyncio.timeout, []

    def accelerated_deadline(seconds):
        limits.append(seconds)
        return real_timeout(0.01)

    monkeypatch.setattr(service_module.asyncio, "timeout", accelerated_deadline)
    with pytest.raises(AIError) as failed:
        await Service(None, None, None).test_provider(PROVIDER)
    assert failed.value.code == "provider_timeout"
    assert failed.value.details["diagnostic_id"] in str(failed.value)
    assert limits == [50]
    assert stopped.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("calls,expected", [
    ([], False),
    ([{"name": "another_tool", "args": {}, "id": "other", "type": "tool_call"}], False),
    ([{"name": "kubedoor_connection_probe", "args": {}, "id": "probe", "type": "tool_call"}], True),
])
async def test_provider_probe_uses_runtime_default_and_checks_actual_named_call(monkeypatch, calls, expected):
    model, factory = probe_model(monkeypatch, result=AIMessage(content="", tool_calls=calls))
    result = await Service(None, None, None).test_provider(PROVIDER)
    assert result["success"] is True
    assert result["tool_calling"] is expected
    assert result["base_url"] == "https://llm.example.com/custom/v1"
    assert factory.call_args.args[0].base_url == result["base_url"]
    assert model.bind_tools.call_args.kwargs == {}
    model.ainvoke.assert_awaited_once()
    if not expected:
        assert result["warning"]


@pytest.mark.asyncio
async def test_actual_sdk_probe_sends_one_correct_endpoint_without_forced_tool_choice(monkeypatch):
    from langchain_openai import ChatOpenAI
    requests = []

    def upstream(request):
        requests.append(request)
        body = json.loads(request.content)
        assert request.url.path == "/custom/v1/chat/completions"
        assert "tool_choice" not in body
        assert body["model"] == "deepseek-flash"
        chunk = {"id": "probe", "object": "chat.completion.chunk", "created": 0, "model": "deepseek-flash", "choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [{"index": 0, "id": "probe", "type": "function", "function": {"name": "kubedoor_connection_probe", "arguments": "{}"}}]}, "finish_reason": "tool_calls"}]}
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as http:
        def factory(provider):
            return ChatOpenAI(model=provider.model, api_key=provider.api_key, base_url=provider.base_url, use_responses_api=False, streaming=True, http_async_client=http, max_retries=0)

        monkeypatch.setattr(service_module, "make_model", factory)
        result = await Service(None, None, None).test_provider(PROVIDER)
    assert result["tool_calling"] is True
    assert len(requests) == 1
