"""Verify session query overlap, authorization ordering and response compatibility."""

import asyncio
import copy
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.testclient import TestClient

from kubedoor_ai.domain import AIError, Identity
from kubedoor_ai.http import create_app
from kubedoor_ai.service import Service
from kubedoor_ai.storage import Store


NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
SESSION = {"id": "session-1", "username": "owner", "title": "会话", "idempotency_key": None, "created_at": NOW, "updated_at": NOW}
MESSAGES = [
    {"id": "message-1", "role": "user", "content": "查询资源", "scope": {"env": "cluster-a"}, "tools": None, "created_at": NOW},
    {"id": "message-2", "role": "assistant", "content": "资源正常", "scope": {"env": "cluster-a"}, "tools": [{"id": "tool-1", "status": "completed"}], "created_at": NOW},
]
ACTIVE = {"id": "run-1", "status": "waiting_approval", "scope": {"env": "cluster-a"}, "origin": "chat", "event_seq": 5}
ACTION = {
    "id": "action-1", "operation": "restart_deployment", "source": "kubedoor",
    "arguments": {"namespace": "demo", "deployment": "exporter"},
    "preview": {"method": "POST", "path": "/api/restart", "credential_fingerprint": "internal-fingerprint"},
    "expires_at": NOW,
}


class QueryPool:
    """Use the real Store's SQL and authorization, with controllable local I/O."""

    def __init__(self, active=ACTIVE, owner="owner"):
        self.active = copy.deepcopy(active)
        self.owner = owner
        self.calls = []
        self.started = {name: asyncio.Event() for name in ("owner", "history", "active", "pending", "create", "patch", "delete")}
        self.gates = {}
        self.failures = {}
        self.cancelled = set()

    async def wait_query(self, name, sql, args):
        self.calls.append((name, sql, args))
        self.started[name].set()
        try:
            if name in self.gates:
                await self.gates[name].wait()
            if name in self.failures:
                raise self.failures[name]
        except asyncio.CancelledError:
            self.cancelled.add(name)
            raise

    async def fetchrow(self, sql, *args):
        if sql.startswith("UPDATE kubedoor_ai_sessions"):
            await self.wait_query("patch", sql, args)
            return {"id": args[0], "title": args[1], "updated_at": NOW}
        if sql.startswith("INSERT INTO kubedoor_ai_sessions"):
            await self.wait_query("create", sql, args)
            return {**SESSION, "id": args[0], "username": args[1], "title": args[2], "idempotency_key": args[3]}
        if "FROM kubedoor_ai_sessions" in sql:
            await self.wait_query("owner", sql, args)
            return copy.deepcopy(SESSION) if args == ("session-1", self.owner) else None
        if "FROM kubedoor_ai_runs" in sql:
            await self.wait_query("active", sql, args)
            return copy.deepcopy(self.active)
        raise AssertionError("Unexpected fetchrow: " + sql)

    async def fetch(self, sql, *args):
        if "FROM kubedoor_ai_messages" in sql:
            await self.wait_query("history", sql, args)
            return copy.deepcopy(MESSAGES)
        if "FROM kubedoor_ai_actions" in sql:
            await self.wait_query("pending", sql, args)
            return [copy.deepcopy(ACTION)]
        raise AssertionError("Unexpected fetch: " + sql)

    async def execute(self, sql, *args):
        if sql.startswith("DELETE FROM kubedoor_ai_sessions"):
            await self.wait_query("delete", sql, args)
            return "DELETE 1"
        raise AssertionError("Unexpected execute: " + sql)


def service_for(pool):
    # No model, Kubernetes executor, master service or real database is needed.
    gateway = type("UnusedGateway", (), {"master": AsyncMock()})()
    return Service(Store(pool), gateway, None)


async def started(pool, name):
    await asyncio.wait_for(pool.started[name].wait(), 1)


@pytest.mark.asyncio
async def test_ownership_finishes_before_history_and_active_queries_can_overlap():
    pool = QueryPool()
    for name in ("owner", "history", "active"):
        pool.gates[name] = asyncio.Event()
    service = service_for(pool)
    task = asyncio.create_task(service.session_detail(Identity("owner", "read"), "session-1"))
    try:
        await started(pool, "owner")
        assert [call[0] for call in pool.calls] == ["owner"]
        assert not pool.started["history"].is_set()
        assert not pool.started["active"].is_set()
        pool.gates["owner"].set()
        await started(pool, "history")
        await started(pool, "active")
        assert not task.done()
        assert not pool.started["pending"].is_set()
        # Approval lookup proceeds even while the entire history is still loading.
        pool.gates["active"].set()
        await started(pool, "pending")
        assert not pool.gates["history"].is_set()
        assert not task.done()
        pool.gates["history"].set()
        result = await task
        assert [message["id"] for message in result["messages"]] == ["message-1", "message-2"]
        assert result["active_run"]["pending_actions"][0]["action_id"] == "action-1"
        service.gateway.master.assert_not_awaited()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("username,sid", [("other-user", "session-1"), ("owner", "missing-session")])
async def test_missing_or_foreign_session_does_not_read_history_runs_or_approvals(username, sid):
    pool = QueryPool()
    with pytest.raises(AIError) as denied:
        await service_for(pool).session_detail(Identity(username, "rw"), sid)
    assert denied.value.status == 404
    assert denied.value.code == "not_found"
    assert [call[0] for call in pool.calls] == ["owner"]
    assert pool.calls[0][2] == (sid, username)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["running", "waiting_approval"])
async def test_history_and_recovered_pending_approval_keep_existing_response_fields(status):
    active = {**ACTIVE, "status": status}
    pool = QueryPool(active=active)
    result = await service_for(pool).session_detail(Identity("owner", "read"), "session-1")
    assert set(result) == set(SESSION) | {"messages", "active_run"}
    assert result["id"] == SESSION["id"]
    assert result["username"] == "owner"
    assert result["created_at"] == NOW.isoformat()
    assert result["messages"] == [{**message, "created_at": NOW.isoformat()} for message in MESSAGES]
    assert result["active_run"] == {
        **active,
        "pending_actions": [{
            "action_id": "action-1", "name": "restart_deployment", "tool": "restart_deployment", "source": "kubedoor",
            "arguments": ACTION["arguments"], "preview": {"method": "POST", "path": "/api/restart"},
            "diff": None, "command": None, "expires_at": NOW.isoformat(),
        }],
    }
    assert "internal-fingerprint" not in str(result)
    assert set(call[0] for call in pool.calls) == {"owner", "history", "active", "pending"}
    assert next(call[2] for call in pool.calls if call[0] == "pending") == ("run-1",)
    assert "waiting_approval" in next(call[1] for call in pool.calls if call[0] == "active")


@pytest.mark.asyncio
async def test_session_without_active_run_does_not_fetch_pending_actions():
    pool = QueryPool(active=None)
    result = await service_for(pool).session_detail(Identity("owner", "rw"), "session-1")
    assert result["active_run"] is None
    assert len(result["messages"]) == 2
    assert set(call[0] for call in pool.calls) == {"owner", "history", "active"}


@pytest.mark.asyncio
@pytest.mark.parametrize("failed,sibling", [("history", "active"), ("active", "history"), ("pending", "history")])
async def test_failed_query_cancels_sibling_and_preserves_original_error(failed, sibling):
    pool = QueryPool()
    error = AIError("模拟数据库查询错误", 503, "local_query_failed")
    pool.failures[failed] = error
    pool.gates[failed] = asyncio.Event()
    pool.gates[sibling] = asyncio.Event()
    task = asyncio.create_task(service_for(pool).session_detail(Identity("owner", "read"), "session-1"))
    try:
        await started(pool, failed)
        await started(pool, sibling)
        pool.gates[failed].set()
        with pytest.raises(AIError) as raised:
            await task
        assert raised.value is error
        assert sibling in pool.cancelled
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_session_request_leaves_no_history_or_active_query_running():
    pool = QueryPool()
    pool.gates = {"history": asyncio.Event(), "active": asyncio.Event()}
    task = asyncio.create_task(service_for(pool).session_detail(Identity("owner", "read"), "session-1"))
    await started(pool, "history")
    await started(pool, "active")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert pool.cancelled == {"history", "active"}
    assert not pool.started["pending"].is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("title,expected", [("  新会话  ", "新会话"), ("", "新会话"), ("字" * 250, "字" * 200)])
async def test_new_session_keeps_single_database_round_trip_and_idempotency_contract(title, expected):
    pool = QueryPool()
    service = service_for(pool)
    result = await service.new_session(Identity("owner", "read"), title, key="browser-request-1")
    assert len(pool.calls) == 1
    name, sql, args = pool.calls[0]
    assert name == "create"
    assert args[1:] == ("owner", expected, "browser-request-1")
    assert "ON CONFLICT(username,idempotency_key)" in sql
    assert "RETURNING" in sql
    assert result["id"] == args[0]
    assert result["title"] == expected
    assert result["created_at"] == NOW.isoformat()
    assert result["idempotency_key"] == "browser-request-1"
    service.gateway.master.assert_not_awaited()


HTTP_HEADERS = {"X-Kubedoor-Token": "test-token", "X-User-Name": "owner", "X-User-Permission": "rw"}


def http_service(pool):
    service = service_for(pool)
    service.runtime = SimpleNamespace(cancel=AsyncMock(), checkpointer=SimpleNamespace(adelete_thread=AsyncMock()))
    return service


def test_real_http_session_get_checks_owner_once_and_restores_pending_actions():
    pool = QueryPool()
    service = http_service(pool)
    with TestClient(create_app(service, token="test-token")) as client:
        response = client.get("/api/ai/sessions/session-1", headers=HTTP_HEADERS)
    assert response.status_code == 200
    assert response.json()["active_run"]["pending_actions"][0]["action_id"] == "action-1"
    assert [call[0] for call in pool.calls].count("owner") == 1
    assert set(call[0] for call in pool.calls) == {"owner", "history", "active", "pending"}


@pytest.mark.parametrize("headers,expected", [({}, 401), ({**HTTP_HEADERS, "X-User-Name": "other-user"}, 404)])
def test_real_http_unauthorized_session_get_never_reads_history(headers, expected):
    pool = QueryPool()
    with TestClient(create_app(http_service(pool), token="test-token")) as client:
        response = client.get("/api/ai/sessions/session-1", headers=headers)
    assert response.status_code == expected
    assert all(call[0] == "owner" for call in pool.calls)
    assert len(pool.calls) == (0 if expected == 401 else 1)


@pytest.mark.parametrize("method", ["PATCH", "DELETE"])
def test_real_http_patch_and_delete_reject_foreign_owner_before_accessing_data(method):
    pool = QueryPool(active=None)
    service = http_service(pool)
    with TestClient(create_app(service, token="test-token")) as client:
        response = client.request(method, "/api/ai/sessions/session-1", headers={**HTTP_HEADERS, "X-User-Name": "other-user"}, json={"title": "修改标题"})
    assert response.status_code == 404
    assert [call[0] for call in pool.calls] == ["owner"]
    service.runtime.cancel.assert_not_awaited()
    service.runtime.checkpointer.adelete_thread.assert_not_awaited()


def test_real_http_owner_patch_keeps_title_update_and_single_ownership_check():
    pool = QueryPool(active=None)
    with TestClient(create_app(http_service(pool), token="test-token")) as client:
        response = client.patch("/api/ai/sessions/session-1", headers=HTTP_HEADERS, json={"title": " 修改标题 "})
    assert response.status_code == 200
    assert response.json() == {"id": "session-1", "title": "修改标题", "updated_at": NOW.isoformat()}
    assert [call[0] for call in pool.calls] == ["owner", "patch"]


def test_real_http_owner_delete_keeps_cleanup_and_single_ownership_check():
    pool = QueryPool(active=None)
    service = http_service(pool)
    with TestClient(create_app(service, token="test-token")) as client:
        response = client.delete("/api/ai/sessions/session-1", headers=HTTP_HEADERS)
    assert response.status_code == 200
    assert response.json() == {"success": True}
    assert [call[0] for call in pool.calls].count("owner") == 1
    assert set(call[0] for call in pool.calls) == {"owner", "history", "active", "delete"}
    assert next(call[2] for call in pool.calls if call[0] == "delete") == ("session-1",)
    service.runtime.checkpointer.adelete_thread.assert_awaited_once_with("session-1")
