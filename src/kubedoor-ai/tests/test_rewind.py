"""重新生成 / 编辑后重发：回退检查点里的最后一轮、替换前的检查、会话搜索片段。"""
import pytest
from langchain_core.messages import HumanMessage

from kubedoor_ai.domain import AIError, Identity
from kubedoor_ai.gateway import RunContext
from kubedoor_ai.service import Service, search_snippet
from test_cache_stability import FIRST_TIME, SCOPE, SECOND_TIME, THIRD_TIME, Harness, answer, dumps, inventory


async def checkpoint_messages(harness, session):
    ctx = RunContext({"id": "probe", "session_id": session, "scope": SCOPE, "skill_ids": []}, Identity("operator", "rw"))
    state = await harness.runtime.graph(ctx, harness.provider).aget_state({"configurable": {"thread_id": session}})
    return state.values.get("messages", [])


@pytest.mark.asyncio
async def test_regenerate_drops_the_last_turn_and_keeps_the_cached_prefix(monkeypatch):
    harness = Harness([answer("第一轮回答"), inventory("call-2"), answer("第二轮旧回答"), answer("第二轮新回答")], monkeypatch)
    try:
        await harness.turn("s", "run-1", "第一轮问题", FIRST_TIME)
        old = await harness.turn("s", "run-2", "第二轮问题", SECOND_TIME)
        new = await harness.turn("s", "run-3", "第二轮问题（改过）", THIRD_TIME, rewind="run-2")
        # 新一轮第一个请求 = 被替换那一轮的第一个请求去掉最后的用户消息，再接新的用户消息：前缀逐字节相同，命中缓存
        assert dumps(new[0]["messages"][:-1]) == dumps(old[0]["messages"][:-1])
        assert new[0]["messages"][-1]["content"].endswith("第二轮问题（改过）")
        assert "第二轮旧回答" not in dumps(new[0]) and "call-2" not in dumps(new[0])
        humans = [m for m in await checkpoint_messages(harness, "s") if isinstance(m, HumanMessage)]
        assert [m.id for m in humans] == ["user-run-1", "user-run-3"]
        assert harness.store.messages[-1]["content"] == "第二轮新回答"
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_rewind_of_a_turn_that_never_reached_the_checkpoint_changes_nothing(monkeypatch):
    harness = Harness([answer("第一轮回答"), answer("第二轮回答")], monkeypatch)
    try:
        first = await harness.turn("s", "run-1", "第一轮问题", FIRST_TIME)
        second = await harness.turn("s", "run-2", "第二轮问题", SECOND_TIME, rewind="run-failed-before-start")
        assert dumps(second[0]["messages"][:len(first[0]["messages"])]) == dumps(first[0]["messages"])
        assert len([m for m in await checkpoint_messages(harness, "s") if isinstance(m, HumanMessage)]) == 2
    finally:
        await harness.close()


class TurnStore:
    def __init__(self, active=None, last=None, writes=None):
        self.active, self.last, self.writes = active, last, writes
        self.queries = []

    async def session(self, sid, identity):
        return {"id": sid}

    async def one(self, sql, *args):
        self.queries.append(sql)
        if "status IN ('running','waiting_approval')" in sql:
            return self.active
        if "FROM kubedoor_ai_messages m JOIN" in sql:
            return self.last
        if "FROM kubedoor_ai_actions" in sql:
            return self.writes
        raise AssertionError(sql)


LAST = {"run_id": "run-9", "usage": {"model_calls": 2}, "rewindable": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("store, code", [
    (TurnStore(active={"id": "run-10"}, last=LAST), "run_active"),
    (TurnStore(), "nothing_to_replace"),
    (TurnStore(last={**LAST, "rewindable": False}), "turn_not_rewindable"),
    (TurnStore(last=LAST, writes={"id": "action-1"}), "turn_has_writes"),
    (TurnStore(last={**LAST, "usage": {"summary_calls": 1}}), "turn_compacted"),
])
async def test_only_a_safe_last_turn_can_be_replaced(store, code):
    with pytest.raises(AIError) as denied:
        await Service(store, None, None).replaceable_turn(Identity("operator", "rw"), "session-1")
    assert denied.value.code == code and denied.value.status == 409


@pytest.mark.asyncio
async def test_replaceable_turn_returns_the_run_to_rewind():
    store = TurnStore(last=LAST)
    assert await Service(store, None, None).replaceable_turn(Identity("operator", "rw"), "session-1") == "run-9"
    writes = next(sql for sql in store.queries if "kubedoor_ai_actions" in sql)
    # 被拒绝和已知失败的修改没有改动集群；部分完成和结果未知的要挡住
    assert "state NOT IN ('rejected','failed','pending')" in writes and "'partial_failure','outcome_unknown'" in writes


def test_search_snippet_centres_the_keyword():
    content = "前面的内容" * 20 + "\norder-api 一直 OOMKilled\n" + "后面的内容" * 20
    snippet = search_snippet(content, "oomkilled")
    assert snippet.startswith("…") and snippet.endswith("…") and "order-api 一直 OOMKilled" in snippet
    assert "\n" not in snippet and len(snippet) < 120
    assert search_snippet("短内容", "不存在") == "短内容" and search_snippet(None, "x") == ""
