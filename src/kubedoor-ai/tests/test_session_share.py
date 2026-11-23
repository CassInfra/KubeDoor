"""会话分享：本人开关（Service.set_share）和分享页（Service.shared_session）。真实 SQL 见 test_postgres_integration"""
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kubedoor_ai.domain import AIError, Identity
from kubedoor_ai.service import Service

CODE = "A" * 21 + "_"


class ShareStore:
    """kubedoor_ai_sessions 里一个属于 alice 的网页会话"""

    def __init__(self, rows=(), active=None):
        self.code, self.rows, self.active = None, [dict(row) for row in rows], active
        self.reads = 0

    async def session(self, sid, identity):
        if identity.username != "alice":
            raise AIError("会话不存在", 404, "not_found")
        return {"id": sid, "title": "排查超时", "share_code": self.code}

    async def one(self, sql, *args):
        if sql.startswith("UPDATE kubedoor_ai_sessions SET share_code=$3"):
            if self.code:
                return None
            self.code = args[2]
            return {"id": args[0], "share_code": self.code, "shared_at": "2026-11-23T10:00:00+08:00"}
        if sql.startswith("SELECT id,share_code,shared_at"):
            return {"id": args[0], "share_code": self.code, "shared_at": "2026-11-23T10:00:00+08:00"} if self.code else None
        if sql.startswith("UPDATE kubedoor_ai_sessions SET share_code=NULL"):
            if not self.code:
                return None
            self.code = None
            return {"id": args[0]}
        if "WHERE share_code=$1" in sql:
            self.reads += 1
            return {"id": "s-1", "title": "排查超时", "shared_at": "t1", "updated_at": "t2"} if args[0] == self.code else None
        if "FROM kubedoor_ai_runs" in sql:
            return self.active
        raise AssertionError(sql)

    async def fetch(self, sql, *args):
        assert "m.replaced_at IS NULL" in sql and args == ("s-1",)
        return [dict(row) for row in self.rows]


def service(store, master=None):
    svc = Service.__new__(Service)
    svc.store = store
    svc.gateway = SimpleNamespace(master=master or AsyncMock(return_value={"success": True}))
    return svc


ALICE = Identity("alice", "rw")


@pytest.mark.asyncio
async def test_owner_toggles_sharing_and_reopening_issues_a_new_code():
    store = ShareStore()
    master = AsyncMock(return_value={"success": True})
    svc = service(store, master)
    opened = await svc.set_share(ALICE, "s-1", True)
    code = opened["share_code"]
    assert opened == {"id": "s-1", "shared": True, "share_code": code, "shared_at": "2026-11-23T10:00:00+08:00"}
    assert len(code) == 22
    # 已经在分享：返回原来的码，不重复记审计
    assert (await svc.set_share(ALICE, "s-1", True))["share_code"] == code
    assert await svc.set_share(ALICE, "s-1", False) == {"id": "s-1", "shared": False, "share_code": None, "shared_at": None}
    # 已经关闭：再关一次不记审计
    await svc.set_share(ALICE, "s-1", False)
    assert (await svc.set_share(ALICE, "s-1", True))["share_code"] != code
    calls = [(call.args[1:3], call.kwargs["body"]) for call in master.await_args_list]
    assert calls == [
        (("POST", "/api/ai/internal/audit"), {"action": "ai.share", "target": "排查超时", "detail": {"session_id": "s-1"}}),
        (("POST", "/api/ai/internal/audit"), {"action": "ai.unshare", "target": "排查超时", "detail": {"session_id": "s-1"}}),
        (("POST", "/api/ai/internal/audit"), {"action": "ai.share", "target": "排查超时", "detail": {"session_id": "s-1"}}),
    ]
    # 审计用的是操作人自己的身份
    assert all(call.args[0] is ALICE for call in master.await_args_list)


@pytest.mark.asyncio
async def test_only_the_owner_can_toggle_and_enabled_must_be_a_boolean():
    svc = service(ShareStore())
    with pytest.raises(AIError) as error:
        await svc.set_share(Identity("bob", "admin"), "s-1", True)
    assert error.value.status == 404
    for value in ("true", 1, None):
        with pytest.raises(AIError) as error:
            await svc.set_share(ALICE, "s-1", value)
        assert error.value.status == 400


@pytest.mark.asyncio
async def test_audit_failure_does_not_block_sharing():
    svc = service(ShareStore(), AsyncMock(side_effect=AIError("KubeDoor 接口暂不可用", 502, "upstream_error")))
    assert (await svc.set_share(ALICE, "s-1", True))["shared"] is True
    assert (await svc.set_share(ALICE, "s-1", False))["shared"] is False


ROWS = [
    {"id": "m1", "role": "user", "content": "检查集群 A", "scope": {"env": "a"}, "tools": None, "memories": [], "created_at": "1", "origin": "chat"},
    {"id": "m2", "role": "assistant", "content": "A 正常", "scope": {"env": "a"}, "tools": [{"name": "logs", "result": "a-log"}],
     "memories": [], "created_at": "2", "origin": "chat"},
    {"id": "m3", "role": "user", "content": "检查集群 B", "scope": {"env": "b"}, "tools": None, "memories": [], "created_at": "3", "origin": "chat"},
    {"id": "m4", "role": "assistant", "content": "B 有 2 个 Pod 重启", "scope": {"env": "b"}, "tools": [{"name": "logs", "result": "b-log"}],
     "memories": [], "created_at": "4", "origin": "chat"},
    # MCP 调用建的会话：正文本身就是工具参数和结果
    {"id": "m5", "role": "user", "content": '{"operation": "logs"}', "scope": {"env": "b"}, "tools": None, "memories": [], "created_at": "5", "origin": "mcp"},
    {"id": "m6", "role": "assistant", "content": '{"data": "b-secret-log"}', "scope": {"env": "b"}, "tools": [{"name": "logs"}],
     "memories": [], "created_at": "6", "origin": "mcp"},
    # 没有集群的消息不按全局角色放行工具结果
    {"id": "m7", "role": "assistant", "content": "无集群", "scope": None, "tools": [{"name": "x"}], "memories": [], "created_at": "7", "origin": None},
]


@pytest.mark.asyncio
async def test_shared_view_is_anonymous_and_hides_tools_of_clusters_the_viewer_cannot_read():
    store = ShareStore(ROWS, active={"status": "waiting_approval"})
    store.code = CODE
    viewer = Identity("carol", "none", MappingProxyType({"a": "read"}))
    view = await service(store).shared_session(viewer, CODE)
    assert set(view) == {"title", "shared_at", "updated_at", "active_run", "messages"}
    assert view["active_run"] == {"status": "waiting_approval"}
    by_id = {message["id"]: message for message in view["messages"]}
    assert all("origin" not in message for message in view["messages"])
    assert by_id["m2"]["tools"] == [{"name": "logs", "result": "a-log"}] and "tools_hidden" not in by_id["m2"]
    assert (by_id["m4"]["content"], by_id["m4"]["tools"], by_id["m4"]["tools_hidden"]) == ("B 有 2 个 Pod 重启", [], True)
    assert (by_id["m5"]["content"], by_id["m5"]["tools_hidden"]) == ("", True)
    assert (by_id["m6"]["content"], by_id["m6"]["tools"], by_id["m6"]["tools_hidden"]) == ("", [], True)
    assert (by_id["m7"]["content"], by_id["m7"]["tools"], by_id["m7"]["tools_hidden"]) == ("无集群", [], True)
    # 用户消息本来没有工具，也不标 tools_hidden
    assert "tools_hidden" not in by_id["m1"] and "tools_hidden" not in by_id["m3"]
    assert "b-secret-log" not in str(view) and "b-log" not in str(view)


@pytest.mark.asyncio
async def test_viewer_with_cluster_permission_sees_everything():
    store = ShareStore(ROWS)
    store.code = CODE
    for viewer in (Identity("admin", "admin"), Identity("dave", "read")):
        view = await service(store).shared_session(viewer, CODE)
        by_id = {message["id"]: message for message in view["messages"]}
        assert by_id["m6"]["content"] == '{"data": "b-secret-log"}' and by_id["m4"]["tools"]
        assert view["active_run"] is None
        # 没有集群的消息谁都看不到工具结果
        assert by_id["m7"]["tools"] == []


@pytest.mark.asyncio
async def test_closed_or_malformed_codes_are_not_found():
    store = ShareStore(ROWS)
    store.code = CODE
    svc = service(store)
    for code in ("B" * 22, CODE[:-1], CODE + "A", "../" + CODE[3:], None):
        with pytest.raises(AIError) as error:
            await svc.shared_session(Identity("carol", "read"), code)
        assert (error.value.status, error.value.code) == (404, "share_not_found")
    # 格式不对的码不查库
    assert store.reads == 1
