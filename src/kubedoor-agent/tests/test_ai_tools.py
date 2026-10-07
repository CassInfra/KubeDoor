import asyncio
import importlib.util
import json
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location("agent_ai_tools", Path(__file__).parents[1] / "func_manager" / "ai_tools.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Socket:
    closed = False

    def __init__(self):
        self.responses = []
        self.packets = []

    async def send_str(self, text):
        value = json.loads(text)
        self.packets.append(value)
        self.responses.append(value["response"])


class Executor:
    def __init__(self):
        self.calls = []

    async def execute(self, operation, arguments, **kwargs):
        self.calls.append((operation, arguments))
        return {"success": True, "data": "executed"}

    async def prepare(self, operation, arguments, **kwargs):
        return {"success": True, "arguments": arguments, "preview": {}}

    async def cancel(self, call_id):
        pass

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_agent_checks_permission_and_approval_before_calling_executor(monkeypatch):
    monkeypatch.setattr(module, "KubernetesExecutor", Executor)
    handler, ws = module.AiToolHandler(), Socket()
    packet = {"request_id": "request", "call_id": "call", "operation": "api",
              "arguments": {"method": "DELETE", "path": "/api/v1/namespaces/default/pods/a"},
              "username": "alice", "permission": "read", "approved": True}
    await handler.handle(ws, packet)
    await handler.handle(ws, {**packet, "permission": "rw", "approved": False})
    assert all(not r["success"] for r in ws.responses)
    assert handler.executor.calls == []
    await handler.handle(ws, {**packet, "permission": "rw", "approved": True})
    assert len(handler.executor.calls) == 1


@pytest.mark.asyncio
async def test_agent_deduplicates_a_dispatched_operation_and_separates_owners(monkeypatch):
    monkeypatch.setattr(module, "KubernetesExecutor", Executor)
    handler, ws = module.AiToolHandler(), Socket()
    packet = {"request_id": "r1", "call_id": "call", "operation": "pod_exec",
              "arguments": {"namespace": "app", "pod": "a", "argv": ["date"]},
              "username": "alice", "permission": "rw", "approved": True}
    await handler.handle(ws, packet)
    await handler.handle(ws, {**packet, "request_id": "r2"})
    assert len(handler.executor.calls) == 1
    assert ws.responses[0] == ws.responses[1]
    await handler.handle(ws, {**packet, "username": "bob"})
    assert not ws.responses[-1]["success"]
    assert len(handler.executor.calls) == 1


@pytest.mark.asyncio
async def test_agent_allows_readers_to_query_cluster_without_approval(monkeypatch):
    monkeypatch.setattr(module, "KubernetesExecutor", Executor)
    handler, ws = module.AiToolHandler(), Socket()
    await handler.handle(ws, {"request_id": "r", "call_id": "read", "operation": "api",
                             "arguments": {"path": "/apis/apps/v1/deployments"},
                             "username": "reader", "permission": "read"})
    assert ws.responses[-1]["success"]
    assert len(handler.executor.calls) == 1
    assert ws.packets[-1]["ai"] is True


@pytest.mark.asyncio
async def test_prepare_does_not_keep_unbounded_owner_records(monkeypatch):
    monkeypatch.setattr(module, "KubernetesExecutor", Executor)
    handler, ws = module.AiToolHandler(), Socket()
    await handler.handle(ws, {"request_id": "r", "call_id": "prepare", "phase": "prepare",
                             "operation": "pod_exec", "arguments": {"namespace": "app", "pod": "a", "argv": ["date"]},
                             "username": "alice", "permission": "rw"})
    assert ws.responses[-1]["success"]
    assert not handler.owners and not handler.tasks


@pytest.mark.asyncio
async def test_cancelled_execution_is_not_replayed(monkeypatch):
    class WaitingExecutor(Executor):
        async def execute(self, operation, arguments, **kwargs):
            self.calls.append((operation, arguments))
            await asyncio.Event().wait()

    monkeypatch.setattr(module, "KubernetesExecutor", WaitingExecutor)
    handler, ws = module.AiToolHandler(), Socket()
    packet = {"request_id": "r", "call_id": "cancelled-call", "operation": "pod_exec",
              "arguments": {"namespace": "app", "pod": "a", "argv": ["date"]},
              "username": "alice", "permission": "rw", "approved": True}
    running = asyncio.create_task(handler.handle(ws, packet))
    while not handler.executor.calls:
        await asyncio.sleep(0)
    await handler.handle(ws, {"request_id": "cancel", "call_id": packet["call_id"], "phase": "cancel",
                             "username": "alice", "permission": "rw"})
    await running
    await handler.handle(ws, packet)
    assert len(handler.executor.calls) == 1
    assert ws.responses[-1]["error"]["code"] == "CANCELLED"


@pytest.mark.asyncio
async def test_execute_never_mistakes_an_inflight_prepare_for_execution(monkeypatch):
    started, finish = asyncio.Event(), asyncio.Event()
    observed = []

    class PreparingExecutor(Executor):
        async def prepare(self, operation, arguments, **kwargs):
            observed.append(kwargs["timeout"])
            started.set()
            await finish.wait()
            return await super().prepare(operation, arguments)

    monkeypatch.setattr(module, "KubernetesExecutor", PreparingExecutor)
    handler, ws = module.AiToolHandler(), Socket()
    packet = {"request_id": "r", "call_id": "same-call", "operation": "pod_exec",
              "arguments": {"namespace": "app", "pod": "a", "argv": ["date"]},
              "username": "alice", "permission": "rw", "approved": True, "timeout": 7}
    preparing = asyncio.create_task(handler.handle(ws, {**packet, "phase": "prepare"}))
    await started.wait()
    await handler.handle(ws, packet)
    assert ws.responses[-1]["error"]["code"] == "BUSY"
    assert not handler.executor.calls
    finish.set()
    await preparing
    await handler.handle(ws, packet)
    assert len(handler.executor.calls) == 1 and observed == [7]


@pytest.mark.asyncio
async def test_large_write_results_keep_success_and_bounded_dedup_cache(monkeypatch):
    class LargeExecutor(Executor):
        async def execute(self, operation, arguments, **kwargs):
            self.calls.append((operation, arguments))
            return {"success": True, "data": "x" * (9 * 1024 * 1024)}

    monkeypatch.setattr(module, "KubernetesExecutor", LargeExecutor)
    handler, ws = module.AiToolHandler(), Socket()
    packet = {"request_id": "r", "call_id": "large-write", "operation": "pod_exec",
              "arguments": {"namespace": "app", "pod": "a", "argv": ["date"]},
              "username": "alice", "permission": "rw", "approved": True}
    await handler.handle(ws, packet)
    assert ws.responses[-1]["success"] and ws.responses[-1]["truncated"]
    assert handler.completed_bytes < 1024
    await handler.handle(ws, packet)
    assert ws.responses[-1]["success"] and len(handler.executor.calls) == 1
