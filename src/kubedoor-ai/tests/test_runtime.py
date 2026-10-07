import json
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from kubedoor_ai.domain import CredentialCipher, Identity, Provider
from kubedoor_ai.gateway import Gateway, RunContext
from kubedoor_ai.runtime import Runtime, SecretStreamRedactor, SecretRedactionMiddleware, make_model
from fakes import FakeStore, ScriptedModel


def setup(responses):
    store = FakeStore()
    gateway = Gateway(store, CredentialCipher("ab" * 32), "http://master", "internal")
    gateway.execute_action = AsyncMock(return_value={"success": True, "data": {"replicas": 3}})
    saver = InMemorySaver()
    model = ScriptedModel(responses=responses)
    runtime = Runtime(store, gateway, saver, model_factory=lambda _: model)
    run = {"id": "run-1", "session_id": "session-1", "scope": {"env": "cluster-a", "namespace": "default", "deployment": None, "pod": None}, "skill_ids": []}
    ctx = RunContext(run, Identity("operator", "rw"), secrets=("provider-private-key",))
    return runtime, ctx, Provider("http://model/v1", "provider-private-key", "test-model")


@pytest.mark.asyncio
async def test_real_deepagent_checkpoint_interrupt_and_precise_resume():
    call = AIMessage(content="准备修改", tool_calls=[{"name": "kubedoor_tool", "args": {"operation": "scale_deployment", "source": "kubedoor", "arguments": {"namespace": "other", "deployment": "app", "replicas": 3}}, "id": "call_1", "type": "tool_call"}])
    runtime, ctx, provider = setup([call, AIMessage(content="已核验，副本数为 3。")])
    await runtime.run_graph(ctx, provider, "扩容所选集群 other 命名空间的 app", None)
    assert runtime.store.statuses[-1] == "waiting_approval"
    runtime.gateway.execute_action.assert_not_awaited()
    action = next(iter(runtime.store.actions.values()))
    assert action["interrupt_id"]
    await runtime.run_graph(ctx, provider, None, {action["interrupt_id"]: {"action_id": action["id"], "decision": "approve"}})
    runtime.gateway.execute_action.assert_awaited_once()
    assert runtime.store.statuses[-1] == "completed"
    assert runtime.store.messages[-1]["tools"][0]["arguments"]["replicas"] == 3
    await runtime.gateway.close()


@pytest.mark.asyncio
async def test_rejection_never_executes_and_is_visible_to_model():
    call = AIMessage(content="", tool_calls=[{"name": "kubedoor_tool", "args": {"operation": "delete_pod", "source": "kubedoor", "arguments": {"namespace": "default", "pod": "p"}}, "id": "call_1", "type": "tool_call"}])
    runtime, ctx, provider = setup([call, AIMessage(content="操作已拒绝。")])
    await runtime.run_graph(ctx, provider, "删除 Pod", None)
    action = next(iter(runtime.store.actions.values()))
    await runtime.run_graph(ctx, provider, None, {action["interrupt_id"]: {"action_id": action["id"], "decision": "reject"}})
    runtime.gateway.execute_action.assert_not_awaited()
    assert runtime.store.actions[action["id"]]["state"] == "rejected"
    assert runtime.store.statuses[-1] == "completed"
    await runtime.gateway.close()


@pytest.mark.asyncio
async def test_secret_echo_is_removed_before_checkpoint_not_just_in_ui():
    runtime, ctx, provider = setup([AIMessage(content="upstream echo provider-private-key", id="echo-provider-private-key", name="provider-private-key")])
    await runtime.run_graph(ctx, provider, "user echo provider-private-key", None)
    assert runtime.store.statuses[-1] == "completed"
    checkpoint = await runtime.checkpointer.aget_tuple({"configurable": {"thread_id": "session-1"}})
    assert "provider-private-key" not in str(checkpoint)
    checkpoints = [str(item) async for item in runtime.checkpointer.alist({"configurable": {"thread_id": "session-1"}})]
    assert "provider-private-key" not in "\n".join(checkpoints)
    state = await runtime.graph(ctx, provider).aget_state({"configurable": {"thread_id": "session-1"}})
    assert "[redacted]" in str(state.values["messages"])
    assert "provider-private-key" not in str(runtime.store.events)
    assert "provider-private-key" not in str(runtime.store.messages)
    await runtime.gateway.close()


@pytest.mark.asyncio
async def test_parallel_interrupt_decisions_restore_only_selected_action():
    calls = [{"name": "kubedoor_tool", "args": {"operation": "delete_pod", "source": "kubedoor", "arguments": {"namespace": "default", "pod": pod}}, "id": f"call_{pod}", "type": "tool_call"} for pod in ("p1", "p2")]
    runtime, ctx, provider = setup([AIMessage(content="", tool_calls=calls), AIMessage(content="两项决定已处理。")])
    await runtime.run_graph(ctx, provider, "删除两个 Pod", None)
    actions = list(runtime.store.actions.values())
    assert len(actions) == 2
    assert len({a["interrupt_id"] for a in actions}) == 2
    first = actions[0]
    await runtime.run_graph(ctx, provider, None, {first["interrupt_id"]: {"action_id": first["id"], "decision": "approve"}})
    assert runtime.store.statuses[-1] == "waiting_approval"
    runtime.gateway.execute_action.assert_awaited_once()
    second = runtime.store.actions[actions[1]["id"]]
    await runtime.run_graph(ctx, provider, None, {second["interrupt_id"]: {"action_id": second["id"], "decision": "reject"}})
    runtime.gateway.execute_action.assert_awaited_once()
    assert runtime.store.statuses[-1] == "completed"
    await runtime.gateway.close()


@pytest.mark.asyncio
async def test_subagent_mutation_uses_same_approval_gateway():
    delegation = AIMessage(content="", tool_calls=[{"name": "task", "args": {"description": "删除目标 Pod", "subagent_type": "general-purpose"}, "id": "delegate", "type": "tool_call"}])
    mutation = AIMessage(content="", tool_calls=[{"name": "kubedoor_tool", "args": {"operation": "delete_pod", "source": "kubedoor", "arguments": {"namespace": "default", "pod": "p"}}, "id": "child-call", "type": "tool_call"}])
    runtime, ctx, provider = setup([delegation, mutation, AIMessage(content="子任务已完成。"), AIMessage(content="完成。")])
    await runtime.run_graph(ctx, provider, "委派删除 Pod", None)
    assert runtime.store.statuses[-1] == "waiting_approval"
    action = next(iter(runtime.store.actions.values()))
    runtime.gateway.execute_action.assert_not_awaited()
    await runtime.run_graph(ctx, provider, None, {action["interrupt_id"]: {"action_id": action["id"], "decision": "reject"}})
    runtime.gateway.execute_action.assert_not_awaited()
    assert runtime.store.statuses[-1] == "completed"
    await runtime.gateway.close()


@pytest.mark.asyncio
async def test_skills_edit_denied_and_state_backend_cannot_run_host_commands():
    from kubedoor_ai.runtime import SKILLS_ROOT
    skill = SKILLS_ROOT / "kubedoor-k8s" / "SKILL.md"
    original = skill.read_text(encoding="utf-8")
    calls = [{"name": "edit_file", "args": {"file_path": "/skills/kubedoor-k8s/SKILL.md", "old_string": "KubeDoor", "new_string": "changed", "replace_all": True}, "id": "edit", "type": "tool_call"}, {"name": "execute", "args": {"command": "echo host-command"}, "id": "shell", "type": "tool_call"}]
    runtime, ctx, provider = setup([AIMessage(content="", tool_calls=calls), AIMessage(content="工具已返回。")])
    await runtime.run_graph(ctx, provider, "检查技能隔离", None)
    assert runtime.store.statuses[-1] == "completed"
    assert skill.read_text(encoding="utf-8") == original
    snapshot = await runtime.graph(ctx, provider).aget_state({"configurable": {"thread_id": "session-1"}})
    content = str(snapshot.values["messages"])
    assert "denied" in content.lower() or "permission" in content.lower()
    assert "SandboxBackendProtocol" in content or "not available" in content or "not support" in content or "not a valid tool" in content
    await runtime.gateway.close()


@pytest.mark.asyncio
async def test_new_turn_closes_cancelled_checkpoint_without_replaying_old_write():
    call = AIMessage(content="", tool_calls=[{"name": "kubedoor_tool", "args": {"operation": "delete_pod", "source": "kubedoor", "arguments": {"namespace": "default", "pod": "p"}}, "id": "call_1", "type": "tool_call"}])
    runtime, ctx, provider = setup([call, AIMessage(content="改为查询，旧删除没有执行。")])
    await runtime.run_graph(ctx, provider, "删除 Pod", None)
    assert runtime.store.statuses[-1] == "waiting_approval"
    action = next(iter(runtime.store.actions.values()))
    runtime.store.actions[action["id"]].update(state="cancelled", result={"success": False})
    # Simulate the persisted terminal status, then a new run of the same session.
    await runtime.store.status(ctx.run["id"], "cancelled")
    new_ctx = RunContext({**ctx.run, "id": "run-2"}, ctx.identity, secrets=ctx.secrets)
    await runtime.run_graph(new_ctx, provider, "停止删除，改为查询", None)
    assert runtime.store.statuses[-1] == "completed"
    runtime.gateway.execute_action.assert_not_awaited()
    assert len(runtime.store.actions) == 1
    state = await runtime.graph(new_ctx, provider).aget_state({"configurable": {"thread_id": "session-1"}})
    assert "previous_run_interrupted" in str(state.values["messages"])
    await runtime.gateway.close()


def test_provider_stream_redaction_across_chunks_and_interrupt_flush():
    stream = SecretStreamRedactor(("local-provider-private-key",))
    chunks = ["前缀 local-provider-", "private-key 后缀"]
    emitted = [stream.feed(chunk) for chunk in chunks] + [stream.flush()]
    assert "local-provider-private-key" not in "".join(emitted)
    assert "".join(emitted) == "前缀 [redacted] 后缀"
    assert stream.feed("中断 local-provider-") == "中断 "
    assert stream.flush() == "[redacted]"


def test_model_tool_arguments_keep_exact_secret_but_drop_provider_echo():
    arguments = {"operation": "api", "arguments": {"body": {"kind": "Secret", "data": {"payload": "base64-private"}}, "files": {"input.yaml": "exact-private-file"}}, "echo": "provider-private-key"}
    message = AIMessage(content="", tool_calls=[{"name": "kubedoor_tool", "args": arguments, "id": "call", "type": "tool_call"}])
    SecretRedactionMiddleware(("provider-private-key",)).scrub({"messages": [message]})
    assert message.tool_calls[0]["args"]["arguments"]["body"]["data"]["payload"] == "base64-private"
    assert message.tool_calls[0]["args"]["arguments"]["files"]["input.yaml"] == "exact-private-file"
    assert "provider-private-key" not in str(message)
    assert make_model(Provider("http://localhost:11434/v1", "", "local")).openai_api_key.get_secret_value() == "kubedoor-local-no-auth"


@pytest.mark.asyncio
async def test_reused_tool_id_does_not_revive_cancelled_later_turn():
    def call(operation, arguments):
        return AIMessage(content="", tool_calls=[{"name": "kubedoor_tool", "args": {"operation": operation, "source": "kubedoor", "arguments": arguments}, "id": "call_1", "type": "tool_call"}])

    runtime, first, provider = setup([call("resource_inventory", {}), AIMessage(content="第一轮查询完成。"), call("delete_pod", {"namespace": "default", "pod": "p"}), AIMessage(content="第三轮继续查询，已取消的删除未重放。")])
    await runtime.run_graph(first, provider, "查询资源", None)
    assert runtime.store.statuses[-1] == "completed"
    runtime.gateway.execute_action.assert_awaited_once()
    second = RunContext({**first.run, "id": "run-2"}, first.identity, secrets=first.secrets)
    await runtime.run_graph(second, provider, "删除 Pod", None)
    assert runtime.store.statuses[-1] == "waiting_approval"
    mutation = next(a for a in runtime.store.actions.values() if not a["read_only"])
    runtime.store.actions[mutation["id"]].update(state="cancelled", result={"success": False})
    await runtime.store.status(second.run["id"], "cancelled")
    third = RunContext({**first.run, "id": "run-3"}, first.identity, secrets=first.secrets)
    await runtime.run_graph(third, provider, "停止删除，继续查询", None)
    assert runtime.store.statuses[-1] == "completed"
    runtime.gateway.execute_action.assert_awaited_once()
    assert len(runtime.store.actions) == 2
    snapshot = await runtime.graph(third, provider).aget_state({"configurable": {"thread_id": "session-1"}})
    assert "previous_run_interrupted" in str(snapshot.values["messages"])
    await runtime.gateway.close()
