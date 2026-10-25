"""用量与缓存观测（design.md 第 4 节）：每轮累计模型用量，写入 runs.usage 并附在 done 事件里。"""
import pytest

from test_cache_stability import FIRST_TIME, SECOND_TIME, Harness, answer, delegate, delete_pod, inventory


def billed(message, input_tokens, output_tokens, cache_read, reasoning):
    message.usage_metadata = {"input_tokens": input_tokens, "output_tokens": output_tokens,
                              "total_tokens": input_tokens + output_tokens,
                              "input_token_details": {"cache_read": cache_read},
                              "output_token_details": {"reasoning": reasoning}}
    return message


def done(store):
    return [event["data"] for event in store.events if event["type"] == "done"][-1]


@pytest.mark.asyncio
async def test_run_usage_adds_every_model_call_including_the_child_agent(monkeypatch):
    harness = Harness([
        billed(inventory("call-1"), 1000, 50, 0, 20),
        billed(delegate(), 1200, 40, 1000, 10),
        billed(answer("子任务完成"), 500, 30, 0, 5),        # 子 Agent：只计入总量
        billed(answer("全部完成"), 1400, 60, 1200, 15),
    ], monkeypatch)
    try:
        await harness.turn("session-1", "run-1", "检查资源", FIRST_TIME)
        expected = {"model_calls": 4, "input_tokens": 4100, "output_tokens": 180, "cache_read_tokens": 2200,
                    "reasoning_tokens": 50, "last_input_tokens": 1400, "max_input_tokens": 1_048_576, "model": "deepseek-flash"}
        assert harness.store.usage["run-1"] == expected
        assert done(harness.store) == {"status": "completed", "usage": expected}
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_usage_is_saved_at_approval_and_resumed_runs_keep_adding(monkeypatch):
    harness = Harness([billed(delete_pod("call-1"), 1000, 50, 0, 10), billed(answer("已删除"), 1100, 40, 1000, 5)], monkeypatch)
    try:
        await harness.turn("session-1", "run-1", "删除 Pod p", FIRST_TIME)
        assert harness.store.statuses[-1] == "waiting_approval"
        paused = harness.store.usage["run-1"]
        assert (paused["model_calls"], paused["input_tokens"], paused["last_input_tokens"]) == (1, 1000, 1000)
        # 审批恢复时 runs 行里带着暂停前的用量，恢复后的调用在此基础上累加
        await harness.turn("session-1", "run-1", None, SECOND_TIME, resume=harness.pending_interrupt(), usage=paused)
        assert harness.store.usage["run-1"] == {"model_calls": 2, "input_tokens": 2100, "output_tokens": 90,
                                                "cache_read_tokens": 1000, "reasoning_tokens": 15,
                                                "last_input_tokens": 1100, "max_input_tokens": 1_048_576,
                                                "model": "deepseek-flash"}
        assert done(harness.store)["usage"] == harness.store.usage["run-1"]
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_calls_without_reported_usage_still_count_and_failures_report_usage(monkeypatch):
    harness = Harness([answer("没有用量的回答")], monkeypatch)
    try:
        await harness.turn("session-1", "run-1", "你好", FIRST_TIME)
        assert harness.store.usage["run-1"]["model_calls"] == 1 and harness.store.usage["run-1"]["input_tokens"] == 0
        # 预设回答用完时模型调用失败：失败的 run 同样写入用量并附在 done 里
        await harness.turn("session-1", "run-2", "再问一次", SECOND_TIME)
        assert harness.store.statuses[-1] == "failed"
        assert done(harness.store) == {"status": "failed", "usage": harness.store.usage["run-2"]}
        assert harness.store.usage["run-2"]["model_calls"] == 0
    finally:
        await harness.close()
