import asyncio
import copy
from unittest.mock import AsyncMock

import pytest

from kubedoor_ai.domain import AIError, CredentialCipher, Identity
from kubedoor_ai.gateway import Gateway, PendingAction, RunContext
from fakes import FakeStore
from kubedoor_ai.storage import Store


def context(permission="rw"):
    return RunContext({"id": "run-1", "session_id": "session-1", "scope": {"env": "cluster-a", "namespace": "default", "deployment": None, "pod": None}}, Identity("user", permission))


@pytest.fixture
def gateway():
    store = FakeStore()
    gateway = Gateway(store, CredentialCipher("ab" * 32), "http://fixed-master", "service-secret")
    gateway.execute_action = AsyncMock(return_value={"success": True, "data": {"replicas": 3}})
    return gateway


@pytest.mark.asyncio
async def test_mcp_write_freezes_arguments_and_requires_approval(gateway):
    args = {"namespace": "other", "deployment": "app", "replicas": 3}
    with pytest.raises(PendingAction) as pending:
        await gateway.invoke(context(), "scale_deployment", args, call_id="call-1", mcp=True)
    args["replicas"] = 99
    action = gateway.store.actions[pending.value.action["action_id"]]
    assert action["arguments"]["replicas"] == 3
    gateway.execute_action.assert_not_awaited()
    await gateway.dispatch(context(), action)
    await gateway.dispatch(context(), action)
    gateway.execute_action.assert_awaited_once()
    await gateway.close()


@pytest.mark.asyncio
async def test_read_only_user_cannot_prepare_mutation(gateway):
    with pytest.raises(AIError) as denied:
        await gateway.invoke(context("read"), "delete_pod", {"namespace": "default", "pod": "p"}, mcp=True)
    assert denied.value.status == 403
    assert gateway.store.actions == {}
    gateway.execute_action.assert_not_awaited()
    await gateway.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("upstream_code,status", [("TIMEOUT", None), ("EXECUTION_ERROR", None), ("COMMAND_FAILED", None), ("OUTPUT_LIMIT", None), ("TOOL_ERROR", None), ("API_ERROR", 0), ("API_ERROR", 500), ("API_ERROR", None)])
async def test_unknown_write_outcome_is_not_replayed_or_switched(gateway, upstream_code, status):
    error = {"code": upstream_code, "message": "uncertain"}
    if status is not None:
        error["status"] = status
    gateway.execute_action.return_value = {"success": False, "error": error}
    with pytest.raises(PendingAction) as pending:
        await gateway.invoke(context(), "delete_pod", {"namespace": "default", "pod": "p"}, call_id="uncertain", mcp=True)
    action = gateway.store.actions[pending.value.action["action_id"]]
    result = await gateway.dispatch(context(), action)
    assert result["error"]["code"] == "outcome_unknown"
    assert result["error"]["upstream_code"] == upstream_code
    assert gateway.store.actions[action["id"]]["state"] == "unknown"
    with pytest.raises(AIError) as blocked:
        await gateway.invoke(context(), "delete_pod", {"namespace": "default", "pod": "p"}, call_id="new-id", mcp=True)
    assert blocked.value.code == "outcome_unknown"
    gateway.execute_action.assert_awaited_once()
    await gateway.close()


@pytest.mark.asyncio
async def test_api_validation_rejection_is_known_and_allows_corrected_proposal(gateway):
    gateway.execute_action.return_value = {"success": False, "error": {"code": "API_ERROR", "status": 422, "message": "invalid resource"}}
    with pytest.raises(PendingAction) as first:
        await gateway.invoke(context(), "delete_pod", {"namespace": "default", "pod": "p"}, call_id="invalid", mcp=True)
    action = gateway.store.actions[first.value.action["action_id"]]
    result = await gateway.dispatch(context(), action)
    assert result["error"]["code"] == "API_ERROR"
    assert gateway.store.actions[action["id"]]["state"] == "failed"
    with pytest.raises(PendingAction):
        await gateway.invoke(context(), "scale_deployment", {"namespace": "default", "deployment": "app", "replicas": 3}, call_id="corrected", mcp=True)
    gateway.execute_action.assert_awaited_once()
    await gateway.close()


@pytest.mark.asyncio
async def test_parallel_prepare_freezes_the_actual_connection_snapshot(monkeypatch):
    import kubedoor_tools
    store = FakeStore()
    cipher = CredentialCipher("ab" * 32)
    row = {"env": "cluster-a", "context": "chosen", "revision": 1, "encrypted_config": cipher.encrypt("cluster-a", {"server": "old"})}
    store.connection = AsyncMock(side_effect=lambda env: copy.deepcopy(row))
    prepared_started, finish_prepare = asyncio.Event(), asyncio.Event()
    instances = []

    class Executor:
        def __init__(self, configuration, selected):
            self.server = configuration["server"]
            self.close = AsyncMock()
            self.execute = AsyncMock()
            instances.append(self)

        async def prepare(self, operation, arguments):
            prepared_started.set()
            await finish_prepare.wait()
            return {"success": True, "arguments": copy.deepcopy(arguments), "preview": {"server": self.server}, "read_only": False}

    monkeypatch.setattr(kubedoor_tools, "KubernetesExecutor", Executor)
    gateway = Gateway(store, cipher, "http://master", "internal")
    ctx = context()
    task = asyncio.create_task(gateway.invoke(ctx, "api", {"method": "POST", "path": "/api/v1/namespaces", "body": {"metadata": {"name": "x"}}}, "direct", call_id="binding-race", mcp=True))
    await asyncio.wait_for(prepared_started.wait(), 2)
    row.update(revision=2, encrypted_config=cipher.encrypt("cluster-a", {"server": "new"}))
    replacement = await gateway.executor_binding(ctx, "direct")
    assert replacement.revision == 2 and replacement.executor.server == "new"
    instances[0].close.assert_not_awaited()
    finish_prepare.set()
    with pytest.raises(AIError) as rejected:
        await task
    assert rejected.value.code == "connection_changed"
    assert store.actions == {}
    for executor in instances:
        executor.execute.assert_not_awaited()
    await gateway.close_context(ctx)
    for executor in instances:
        executor.close.assert_awaited_once()
    await gateway.close()


@pytest.mark.asyncio
async def test_parallel_executor_creation_has_one_binding(monkeypatch):
    import kubedoor_tools
    cipher = CredentialCipher("ab" * 32)
    gateway = Gateway(FakeStore(), cipher, "http://master", "internal")
    row = {"env": "cluster-a", "context": "chosen", "revision": 1, "encrypted_config": cipher.encrypt("cluster-a", {"server": "one"})}

    async def connection(env):
        await asyncio.sleep(0)
        return copy.deepcopy(row)

    gateway.store.connection = connection
    instances = []

    class Executor:
        def __init__(self, configuration, selected):
            self.close = AsyncMock()
            instances.append(self)

    monkeypatch.setattr(kubedoor_tools, "KubernetesExecutor", Executor)
    ctx = context()
    first, second = await asyncio.gather(gateway.executor_binding(ctx, "direct"), gateway.executor_binding(ctx, "direct"))
    assert first is second
    assert len(instances) == 1
    row["revision"] = 2
    with pytest.raises(AIError) as rejected:
        await gateway.executor_binding(ctx, "direct", first.fingerprint, first.revision)
    assert rejected.value.code == "connection_changed"
    await gateway.close_context(ctx)
    await gateway.close()


@pytest.mark.asyncio
async def test_secret_action_executes_exact_body_but_exports_safe_preview_and_history(gateway):
    raw = {"method": "POST", "path": "/api/v1/namespaces/default/secrets", "body": {"kind": "Secret", "metadata": {"name": "app"}, "data": {"payload": "base64-private-value"}, "stringData": {"payload": "plaintext-private-value"}}}
    gateway.prepare = AsyncMock(return_value={"success": True, "arguments": copy.deepcopy(raw), "preview": {"after": copy.deepcopy(raw["body"])}})
    ctx = context()
    with pytest.raises(PendingAction) as pending:
        await gateway.invoke(ctx, "api", raw, "agent", call_id="secret-write", mcp=True)
    action = gateway.store.actions[pending.value.action["action_id"]]
    assert action["arguments"] == raw
    assert "base64-private-value" not in str(pending.value.action)
    assert "plaintext-private-value" not in str(pending.value.action)
    await gateway.dispatch(ctx, action)
    assert gateway.execute_action.await_args.args[1]["arguments"] == raw
    assert "base64-private-value" not in str(gateway.store.events)
    assert "plaintext-private-value" not in str(gateway.store.events)

    history = Store(None)
    history.one, history.execute = AsyncMock(return_value={"id": "message"}), AsyncMock()
    history.fetch = AsyncMock(return_value=[action])
    await history.message(ctx.run, "assistant", "完成", [action])
    exported = history.one.await_args.args[7]
    assert "base64-private-value" not in str(exported)
    assert "plaintext-private-value" not in str(exported)
    assert "base64-private-value" not in str(await history.pending(ctx.run["id"]))
    assert action["arguments"] == raw
    await gateway.close()


@pytest.mark.asyncio
async def test_cancel_is_forwarded_to_actual_agent_call(gateway):
    ctx = context()
    ctx.active_calls["call"] = {"source": "agent"}
    gateway.master = AsyncMock(return_value={"success": True})
    await gateway.cancel(ctx)
    assert ctx.cancelled.is_set()
    assert gateway.master.call_args.args[2] == "/api/ai/internal/agent-cancel"
    assert gateway.master.call_args.args[4] == {"call_id": "call"}
    with pytest.raises(asyncio.CancelledError):
        await gateway.check_cancel(ctx)
    await gateway.close()
