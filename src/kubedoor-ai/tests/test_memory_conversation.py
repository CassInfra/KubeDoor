"""Exercise HTTP, shared memories and actual DeepAgent checkpoints together.

Database access and external model/cluster calls are simulated. Production HTTP
handlers, Service, MemoryLibrary, Store.message and the approval graph run here.
"""
from __future__ import annotations

import asyncio
import copy
import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import Field

from fakes import FakeStore, ScriptedModel
from kubedoor_ai.domain import CredentialCipher, Identity, Provider
from kubedoor_ai.gateway import Gateway, RunContext
from kubedoor_ai.http import create_app
from kubedoor_ai.runtime import Runtime
from kubedoor_ai.service import Service
from kubedoor_ai.storage import Store


PROVIDER = {"base_url": "http://unused-model/v1", "api_key": "test-only-provider-key", "model": "mock-model"}
OLD_CONTENT = "memory-original-evidence: exporter 只有一个副本，重启前先核查就绪状态。"
NEW_CONTENT = "memory-new-evidence: 库内的新版本，不应改写已发送上下文。"


def headers(username="alice", permission="rw", **extra):
    return {"X-Kubedoor-Token": "test-token", "X-User-Name": username,
            "X-User-Permission": permission, **extra}


def projected(sql, row):
    columns = sql.split("SELECT ", 1)[1].split(" FROM ", 1)[0]
    return copy.deepcopy(row if columns == "*" else {column.strip().removeprefix("m."): row.get(column.strip().removeprefix("m.")) for column in columns.split(",")})


class ConversationStore(FakeStore):
    """Small SQL adapter, keeping production message persistence and ownership."""
    session = Store.session
    run = Store.run
    message = Store.message
    pending = Store.pending

    def __init__(self):
        super().__init__()
        self.sessions, self.runs, self.memories = {}, {}, {}
        self.memory_body_reads = 0
        self.clock = 0

    def stamp(self):
        self.clock += 1
        return datetime(2026, 10, 5, tzinfo=timezone.utc) + timedelta(seconds=self.clock)

    def matching_memories(self, pattern):
        fragment = pattern[1:-1].replace(r"\%", "%").replace(r"\_", "_").replace(r"\\", "\\")
        return sorted((row for row in self.memories.values() if fragment.lower() in row["title"].lower()), key=lambda row: (row["updated_at"], row["id"]), reverse=True)

    async def one(self, sql, *args):
        if "kubedoor_ai_memories" in sql:
            if sql.startswith("INSERT"):
                mid, title, content, username = args
                now = self.stamp()
                row = {"id": mid, "title": title, "content": content, "version": 1,
                       "created_by": username, "updated_by": username, "created_at": now, "updated_at": now}
                self.memories[mid] = row
                return copy.deepcopy(row)
            if sql.startswith("UPDATE"):
                mid, title, content, username, version = args
                row = self.memories.get(mid)
                if not row or row["version"] != version:
                    return None
                row.update(title=title, content=content, updated_by=username, version=version + 1, updated_at=self.stamp())
                return copy.deepcopy(row)
            if sql.startswith("DELETE"):
                mid, version = args
                row = self.memories.get(mid)
                if not row or row["version"] != version:
                    return None
                del self.memories[mid]
                return {"id": mid}
            if "count(*)" in sql:
                return {"total": len(self.matching_memories(args[0]))}
            row = self.memories.get(args[0])
            if "content" in sql:
                self.memory_body_reads += 1
            return projected(sql, row) if row else None
        if sql.startswith("INSERT INTO kubedoor_ai_sessions"):
            sid, username, title, key = args
            now = self.stamp()
            row = {"id": sid, "username": username, "title": title, "idempotency_key": key, "created_at": now, "updated_at": now}
            self.sessions[sid] = row
            return copy.deepcopy(row)
        if "FROM kubedoor_ai_sessions" in sql:
            row = self.sessions.get(args[0])
            return copy.deepcopy(row) if row and row["username"] == args[1] else None
        if sql.startswith("INSERT INTO kubedoor_ai_runs"):
            names = ("id", "session_id", "username", "permission", "scope", "origin", "skill_ids", "idempotency_key")
            row = dict(zip(names, copy.deepcopy(args)))
            row.update(status="running", cancel_requested=False, event_seq=0, created_at=self.stamp())
            self.runs[row["id"]] = row
            return copy.deepcopy(row)
        if "FROM kubedoor_ai_runs" in sql:
            if "session_id=$1" in sql:
                rows = [row for row in self.runs.values() if row["session_id"] == args[0]]
                if "idempotency_key=$2" in sql:
                    rows = [row for row in rows if row["idempotency_key"] == args[1]]
                elif "status IN" in sql:
                    rows = [row for row in rows if row["status"] in ("running", "waiting_approval")]
                row = rows[-1] if rows else None
            else:
                row = self.runs.get(args[0])
                if row and len(args) > 1 and row["username"] != args[1]:
                    row = None
            return copy.deepcopy(row)
        if sql.startswith("INSERT INTO kubedoor_ai_messages"):
            names = sql.split("kubedoor_ai_messages(", 1)[1].split(")", 1)[0].split(",")
            row = dict(zip(names, copy.deepcopy(args)))
            row["created_at"] = self.stamp()
            self.messages.append(row)
            return copy.deepcopy(row)
        if "FROM kubedoor_ai_messages" in sql:
            rows = [row for row in self.messages if row["run_id"] == args[0] and row["role"] == "user"]
            return projected(sql, rows[0]) if rows else None
        return await super().one(sql, *args)

    async def fetch(self, sql, *args):
        if "FROM kubedoor_ai_memories" in sql:
            if "id=ANY" in sql:
                self.memory_body_reads += 1
                rows = [self.memories[mid] for mid in args[0] if mid in self.memories]
            else:
                rows = self.matching_memories(args[0])[args[2]:args[2] + args[1]]
            return [projected(sql, row) for row in rows]
        if "FROM kubedoor_ai_messages" in sql:
            rows = [row for row in self.messages if row["session_id"] == args[0]]
            if "JOIN kubedoor_ai_runs" in sql:
                rows = [row for row in rows if self.runs[row["run_id"]]["status"] == "completed"]
                rows = list(reversed(rows))[:args[1]]
            return copy.deepcopy(rows)
        if "FROM kubedoor_ai_actions" in sql:
            rows = [row for row in self.actions.values() if row["run_id"] == args[0]]
            if "state='pending'" in sql:
                rows = [row for row in rows if row["state"] == "pending"]
            return copy.deepcopy(rows)
        raise AssertionError(f"Unexpected SQL in conversation test: {sql}")

    async def execute(self, sql, *args):
        if sql.startswith("UPDATE kubedoor_ai_sessions"):
            self.sessions[args[0]]["updated_at"] = self.stamp()
            return "UPDATE 1"
        return await super().execute(sql, *args)

    async def status(self, rid, status, error=None):
        self.runs[rid].update(status=status, error=error)
        await super().status(rid, status, error)


class RecordingModel(ScriptedModel):
    histories: list[list] = Field(default_factory=list)
    bindings: int = 0

    def bind_tools(self, tools, **kwargs):
        self.bindings += 1
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.histories.append(copy.deepcopy(messages))
        return super()._generate(messages, stop, run_manager, **kwargs)


@pytest_asyncio.fixture
async def h():
    store = ConversationStore()
    gateway = Gateway(store, CredentialCipher("ab" * 32), "http://unused-master", "test-token")
    gateway.execute_action = AsyncMock(return_value={"success": True, "data": {"replicas": 2}})
    model = RecordingModel(responses=[AIMessage(content="测试回复，未访问集群。")])
    runtime = Runtime(store, gateway, InMemorySaver(), model_factory=lambda _: model)
    service = Service(store, gateway, runtime)
    service.ensure_env = AsyncMock(return_value={"env": "cluster-a", "online": True, "ai_tools": True})
    service.memory_library.resolve = AsyncMock(wraps=service.memory_library.resolve)
    app = create_app(service, token="test-token")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local-test", headers=headers()) as client:
        yield SimpleNamespace(store=store, gateway=gateway, model=model, runtime=runtime, service=service, client=client)
        pending = list(runtime.tasks.values())
        if pending:
            await asyncio.gather(*pending)
    await gateway.close()


async def create_memory(h, content=OLD_CONTENT, title="Exporter 运维经验"):
    response = await h.client.post("/api/ai/memories", json={"title": title, "content": content})
    assert response.status_code == 200, response.text
    return response.json()


async def create_session(h, username="alice", permission="rw"):
    response = await h.client.post("/api/ai/sessions", json={"title": "私有测试会话"}, headers=headers(username, permission))
    assert response.status_code == 200, response.text
    return response.json()["id"]


async def send(h, sid, message, memory_ids=None, username="alice", permission="rw", key=None):
    data = {"message": message, "scope": {"env": "cluster-a", "namespace": "demo"}, "provider": PROVIDER}
    if memory_ids is not None:
        data["memory_ids"] = memory_ids
    request_headers = headers(username, permission)
    if key:
        request_headers["Idempotency-Key"] = key
    response = await h.client.post(f"/api/ai/sessions/{sid}/runs", json=data, headers=request_headers)
    if response.status_code == 200:
        pending = list(h.runtime.tasks.values())
        if pending:
            await asyncio.gather(*pending)
    return response


def user_messages(messages):
    return [str(message.content) for message in messages if isinstance(message, HumanMessage)]


@pytest.mark.asyncio
async def test_attachment_is_added_once_and_checkpoint_carries_history_to_later_turns(h):
    memory = await create_memory(h)
    sid = await create_session(h)
    first = await send(h, sid, "请根据记忆查询 exporter", [memory["id"]])
    assert first.status_code == 200, first.text
    saved = first.json()["message"]
    assert saved["content"] == "请根据记忆查询 exporter"
    assert saved["memories"] == [{"id": memory["id"], "title": memory["title"], "content": OLD_CONTENT, "version": 1}]
    assert h.store.runs[first.json()["run_id"]]["status"] == "completed", h.store.events
    assert "_memories" not in h.store.runs[first.json()["run_id"]]
    assert OLD_CONTENT in user_messages(h.model.histories[0])[0]
    assert all(OLD_CONTENT not in str(message.content) for message in h.model.histories[0] if isinstance(message, SystemMessage))

    updated = await h.client.patch(f"/api/ai/memories/{memory['id']}", json={"title": memory["title"], "content": NEW_CONTENT, "version": 1})
    assert updated.status_code == 200
    reads_before = h.store.memory_body_reads
    second = await send(h, sid, "继续查询，不再次引入记忆")
    assert second.status_code == 200
    assert h.store.memory_body_reads == reads_before
    humans = user_messages(h.model.histories[-1])
    assert len(humans) == 2
    assert OLD_CONTENT in humans[0] and NEW_CONTENT not in "\n".join(humans)
    assert humans[-1] == "继续查询，不再次引入记忆"
    assert sum(OLD_CONTENT in content for content in humans) == 1

    detail = await h.client.get(f"/api/ai/sessions/{sid}")
    assert detail.status_code == 200
    users = [row for row in detail.json()["messages"] if row["role"] == "user"]
    assert users[0]["memories"] == saved["memories"]
    assert users[1]["memories"] == []
    state = await h.runtime.graph(RunContext(h.store.runs[second.json()["run_id"]], Identity("alice", "rw")), Provider.parse(PROVIDER)).aget_state({"configurable": {"thread_id": sid}})
    assert user_messages(state.values["messages"]) == humans

    other_sid = await create_session(h)
    third = await send(h, other_sid, "新会话没有选择记忆")
    assert third.status_code == 200
    assert user_messages(h.model.histories[-1]) == ["新会话没有选择记忆"]
    assert h.store.memory_body_reads == reads_before


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["edit", "delete"])
async def test_approval_resume_uses_original_checkpoint_when_shared_memory_changes(h, change):
    memory = await create_memory(h)
    sid = await create_session(h)
    h.model.responses = [AIMessage(content="准备扩容", tool_calls=[{"name": "kubedoor_tool", "args": {"operation": "scale_deployment", "source": "kubedoor", "arguments": {"namespace": "demo", "deployment": "deploy-demo-order", "replicas": 2}}, "id": "memory-approval-call", "type": "tool_call"}]), AIMessage(content="已完成测试操作。")]
    accepted = await send(h, sid, "结合经验扩容 exporter", [memory["id"]], key="memory-approval-request")
    assert accepted.status_code == 200, accepted.text
    rid = accepted.json()["run_id"]
    assert h.store.runs[rid]["status"] == "waiting_approval", h.store.events
    h.gateway.execute_action.assert_not_awaited()
    action = next(iter(h.store.actions.values()))
    if change == "edit":
        changed = await h.client.patch(f"/api/ai/memories/{memory['id']}", json={"title": memory["title"], "content": NEW_CONTENT, "version": 1})
    else:
        changed = await h.client.delete(f"/api/ai/memories/{memory['id']}?version=1")
    assert changed.status_code == 200, changed.text

    resolve_calls = h.service.memory_library.resolve.await_count
    reads_before = h.store.memory_body_reads
    retry = await send(h, sid, "结合经验扩容 exporter", [memory["id"]], key="memory-approval-request")
    assert retry.status_code == 200, retry.text
    assert retry.json()["run_id"] == rid
    assert retry.json()["message"]["memories"] == accepted.json()["message"]["memories"]
    assert h.service.memory_library.resolve.await_count == resolve_calls

    resumed = await h.client.post(f"/api/ai/runs/{rid}/decisions", json={"action_id": action["id"], "decision": "approve", "provider": PROVIDER})
    assert resumed.status_code == 200, resumed.text
    await asyncio.gather(*list(h.runtime.tasks.values()))
    assert h.store.runs[rid]["status"] == "completed", h.store.events
    h.gateway.execute_action.assert_awaited_once()
    assert h.service.memory_library.resolve.await_count == resolve_calls
    assert h.store.memory_body_reads == reads_before
    assert OLD_CONTENT in "\n".join(user_messages(h.model.histories[-1]))
    assert NEW_CONTENT not in "\n".join(user_messages(h.model.histories[-1]))
    assert len(user_messages(h.model.histories[-1])) == 1


@pytest.mark.asyncio
async def test_shared_metadata_pagination_detail_and_http_write_version_checks(h):
    first = await create_memory(h, title="经验一")
    second = await create_memory(h, content="另一个共享正文", title="经验二")
    reader = headers("bob", "read")
    listing = await h.client.get("/api/ai/memories?page=1&page_size=1", headers=reader)
    assert listing.status_code == 200
    page = listing.json()
    assert page["total"] == 2 and page["page_size"] == 1 and len(page["memories"]) == 1
    assert "content" not in page["memories"][0]
    assert page["memories"][0]["created_by"] == "alice"
    page_two = await h.client.get("/api/ai/memories?page=2&page_size=1", headers=reader)
    assert page_two.json()["memories"][0]["id"] != page["memories"][0]["id"]
    detail = await h.client.get(f"/api/ai/memories/{first['id']}", headers=reader)
    assert detail.status_code == 200 and detail.json()["content"] == OLD_CONTENT
    assert (await h.client.get("/api/ai/memories", headers={"X-Kubedoor-Token": "invalid-token"})).status_code == 401
    denied = [
        await h.client.post("/api/ai/memories", headers=reader, json={"title": "不允许", "content": "x"}),
        await h.client.patch(f"/api/ai/memories/{first['id']}", headers=reader, json={"title": "不允许", "content": "x", "version": 1}),
        await h.client.delete(f"/api/ai/memories/{first['id']}?version=1", headers=reader),
    ]
    assert [response.status_code for response in denied] == [403, 403, 403]
    updated = await h.client.patch(f"/api/ai/memories/{first['id']}", headers=headers("carol"), json={"title": "其他用户可编辑", "content": NEW_CONTENT, "version": 1})
    assert updated.status_code == 200 and updated.json()["version"] == 2
    assert updated.json()["updated_by"] == "carol"
    stale_update = await h.client.patch(f"/api/ai/memories/{first['id']}", json={"title": "过期更新", "content": "must-not-save", "version": 1})
    stale_delete = await h.client.delete(f"/api/ai/memories/{first['id']}?version=1")
    assert stale_update.status_code == stale_delete.status_code == 409
    assert h.store.memories[first["id"]]["content"] == NEW_CONTENT
    assert (await h.client.delete(f"/api/ai/memories/{second['id']}?version=1")).status_code == 200


@pytest.mark.asyncio
async def test_unknown_selected_memory_prevents_run_and_reader_can_reference_shared_memory(h):
    sid = await create_session(h, "bob", "read")
    before = len(h.store.runs), len(h.store.messages), h.model.index
    failed = await send(h, sid, "引用不存在的记忆", [str(uuid.uuid4())], "bob", "read")
    assert failed.status_code == 404 and failed.json()["error"]["code"] == "memory_not_found"
    assert (len(h.store.runs), len(h.store.messages), h.model.index) == before
    memory = await create_memory(h)
    accepted = await send(h, sid, "读账号引用共享记忆", [memory["id"]], "bob", "read")
    assert accepted.status_code == 200
    assert accepted.json()["message"]["memories"][0]["content"] == OLD_CONTENT
    assert OLD_CONTENT in user_messages(h.model.histories[-1])[0]
    h.gateway.execute_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_summary_keeps_session_private_and_only_creates_tool_free_editable_draft(h):
    sid = await create_session(h)
    completed = await send(h, sid, "总结前的私有问题与排查结论")
    assert completed.status_code == 200
    private = await h.client.get(f"/api/ai/sessions/{sid}", headers=headers("bob"))
    assert private.status_code == 404
    model_calls = h.model.index
    denied = await h.client.post(f"/api/ai/sessions/{sid}/memory-summary", headers=headers("bob"), json={"provider": PROVIDER})
    assert denied.status_code == 404
    assert h.model.index == model_calls

    h.model.responses = [AIMessage(content=json.dumps({"title": "可编辑草稿", "content": "确认后再保存的运维经验"}, ensure_ascii=False))]
    h.model.index = 0
    bindings = h.model.bindings
    summary = await h.client.post(f"/api/ai/sessions/{sid}/memory-summary", headers=headers("alice", "read"), json={"provider": PROVIDER})
    assert summary.status_code == 200, summary.text
    assert summary.json() == {"title": "可编辑草稿", "content": "确认后再保存的运维经验"}
    assert h.model.bindings == bindings
    h.gateway.execute_action.assert_not_awaited()
    assert h.store.memories == {}
    assert len(h.model.histories[-1]) == 2
    assert "总结前的私有问题" in str(h.model.histories[-1][1].content)
    denied_save = await h.client.post("/api/ai/memories", headers=headers("alice", "read"), json=summary.json())
    assert denied_save.status_code == 403 and h.store.memories == {}
    saved = await h.client.post("/api/ai/memories", json={"title": "已编辑的标题", "content": summary.json()["content"] + "，经过手动编辑"})
    assert saved.status_code == 200
    assert len(h.store.memories) == 1 and saved.json()["title"] == "已编辑的标题"
