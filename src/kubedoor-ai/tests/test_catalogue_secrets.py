"""The catalogue resource API keeps exact writes while exporting safe evidence."""

import json
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
import yaml

from fakes import FakeStore
from kubedoor_ai.domain import CredentialCipher, Identity
from kubedoor_ai.gateway import Gateway, PendingAction, RunContext
from kubedoor_ai.storage import Store


PRIVATE_DATA = "c3ludGhldGljLXRlc3QtZGF0YQ=="
PRIVATE_TEXT = "synthetic-secret-for-catalogue-tests"
SECRET = {
    "apiVersion": "v1",
    "kind": "Secret",
    "metadata": {"namespace": "demo", "name": "diagnostic-secret"},
    "type": "Opaque",
    "data": {"payload": PRIVATE_DATA},
    "stringData": {"payload": PRIVATE_TEXT},
}
SECRET_YAML = yaml.safe_dump(SECRET, sort_keys=False)
CONFIGMAP = {
    "apiVersion": "v1",
    "kind": "ConfigMap",
    "metadata": {"namespace": "demo", "name": "diagnostic-config"},
    "data": {"public_setting": "public configuration line"},
}
MULTI_DOCUMENT = yaml.safe_dump_all([CONFIGMAP, SECRET], sort_keys=False)


def context(permission="rw"):
    return RunContext(
        {"id": "run-1", "session_id": "session-1", "scope": {"env": "cluster-a", "namespace": "demo", "deployment": None, "pod": None}},
        Identity("operator", permission),
    )


@pytest_asyncio.fixture
async def resource_gateway():
    calls = []
    response = {"success": True}

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json=response)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    gateway = Gateway(FakeStore(), CredentialCipher("ab" * 32), "http://fixed-master", "test-service-token", http=client)
    yield gateway, calls, response
    await gateway.close()


def assert_safe(value):
    exported = str(value)
    assert PRIVATE_DATA not in exported
    assert PRIVATE_TEXT not in exported
    assert "diagnostic-secret" in exported
    assert "[redacted]" in exported.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("manifest", [SECRET_YAML, json.dumps(SECRET), MULTI_DOCUMENT], ids=["yaml", "json", "multi-document"])
async def test_resource_apply_approves_safely_but_sends_exact_original_manifest(resource_gateway, manifest):
    gateway, calls, response = resource_gateway
    ctx = context()
    arguments = {"method": "apply", "yaml_content": manifest}
    with pytest.raises(PendingAction) as pending:
        await gateway.invoke(ctx, "resource_apply", arguments, call_id="secret-apply", mcp=True)
    action = gateway.store.actions[pending.value.action["action_id"]]
    assert calls == []
    assert action["arguments"]["yaml_content"] == manifest
    assert action["state"] == "pending"
    assert_safe(pending.value.action)
    assert_safe(action["preview"])

    # Exercise the real persistent history and pending-approval exporters too.
    storage = Store(None)
    storage.one = AsyncMock(return_value={"id": "message"})
    storage.execute = AsyncMock()
    storage.fetch = AsyncMock(return_value=[action])
    assert_safe(await storage.pending(ctx.run["id"]))
    await storage.message(ctx.run, "assistant", "申请修改 Secret。", [action])
    assert_safe(storage.one.await_args.args[7])

    arguments["yaml_content"] = yaml.safe_dump(CONFIGMAP)
    response["results"] = [{**SECRET, "status": "success"}]
    result = await gateway.dispatch(ctx, action)
    assert result["success"] is True
    assert len(calls) == 1
    assert calls[0].method == "POST"
    assert calls[0].url.path == "/api/agent/res/ops"
    assert dict(calls[0].url.params) == {"env": "cluster-a", "method": "apply"}
    assert json.loads(calls[0].content) == {"yaml_content": manifest}
    assert gateway.store.actions[action["id"]]["arguments"]["yaml_content"] == manifest
    assert_safe(result)
    assert_safe(gateway.store.events)
    if manifest == MULTI_DOCUMENT:
        assert "public configuration line" in str(pending.value.action)


@pytest.mark.asyncio
@pytest.mark.parametrize("manifest", [SECRET_YAML, json.dumps(SECRET), MULTI_DOCUMENT], ids=["yaml", "json", "multi-document"])
async def test_resource_content_hides_secret_manifest_before_model_history_and_events(resource_gateway, manifest):
    gateway, calls, response = resource_gateway
    response["data"] = manifest
    ctx = context("read")
    result = await gateway.invoke(ctx, "resource_content", {"resource_type": "secret", "resource_name": "diagnostic-secret"}, call_id="secret-read")
    assert result["success"] is True
    assert result["source"] == "kubedoor"
    assert len(calls) == 1
    assert calls[0].method == "GET"
    assert calls[0].url.path == "/api/agent/res/content"
    assert dict(calls[0].url.params) == {"env": "cluster-a", "namespace": "demo", "resource_type": "secret", "resource_name": "diagnostic-secret"}
    assert all(action["read_only"] and action["state"] == "succeeded" for action in gateway.store.actions.values())
    assert_safe(result)
    assert_safe(gateway.store.events)
    documents = list(yaml.safe_load_all(result["data"]))
    assert any(doc["kind"] == "Secret" and doc["metadata"]["name"] == "diagnostic-secret" for doc in documents)
    if manifest == MULTI_DOCUMENT:
        assert documents[0]["data"]["public_setting"] == "public configuration line"
