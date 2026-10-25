"""R-6 / T17：模型服务打断生成时原样重发一次；输出被截断、被过滤时在回答末尾提示。"""
import pytest
from langchain_core.messages import AIMessage

from kubedoor_ai.runtime import FINISH_NOTICES
from test_cache_stability import FIRST_TIME, SECOND_TIME, Harness, answer, dumps


def stopped(content, reason):
    return AIMessage(content=content, additional_kwargs={"reasoning_content": "思考"}, response_metadata={"finish_reason": reason})


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["insufficient_system_resource", "aborted"])
async def test_interrupted_generation_is_resent_once_with_the_same_prefix(monkeypatch, reason):
    harness = Harness([stopped("半截回答", reason), answer("完整回答")], monkeypatch)
    try:
        first, retried = await harness.turn("session-1", "run-1", "查询资源", FIRST_TIME)
        assert dumps(first) == dumps(retried)   # 前缀相同，重发能命中缓存
        assert harness.store.messages[-1]["content"] == "完整回答"
        assert harness.store.usage["run-1"]["model_calls"] == 2   # 两次都计费，都计入用量
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_a_second_interruption_is_kept_and_noted(monkeypatch):
    harness = Harness([stopped("半截一", "insufficient_system_resource"), stopped("半截二", "insufficient_system_resource")], monkeypatch)
    try:
        assert len(await harness.turn("session-1", "run-1", "查询资源", FIRST_TIME)) == 2
        assert harness.store.messages[-1]["content"] == "半截二\n\n" + FINISH_NOTICES["insufficient_system_resource"]
        assert harness.store.statuses[-1] == "completed"
    finally:
        await harness.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["length", "content_filter"])
async def test_truncated_or_filtered_answer_is_noted_without_a_retry(monkeypatch, reason):
    harness = Harness([stopped("被截断的回答", reason)], monkeypatch)
    try:
        assert len(await harness.turn("session-1", "run-1", "查询资源", FIRST_TIME)) == 1
        assert harness.store.messages[-1]["content"] == "被截断的回答\n\n" + FINISH_NOTICES[reason]
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_a_turn_without_text_never_repeats_the_previous_answer(monkeypatch):
    harness = Harness([answer("第一轮的回答"), stopped("", "length")], monkeypatch)
    try:
        await harness.turn("session-1", "run-1", "第一轮", FIRST_TIME)
        await harness.turn("session-1", "run-2", "第二轮", SECOND_TIME)
        assert harness.store.messages[-1]["content"] == "本轮操作已结束。\n\n" + FINISH_NOTICES["length"]
    finally:
        await harness.close()
