"""会话内压缩（design.md 第 5 节）：按窗口比例触发、原文保留约 10%、超长报错先摘要再重试，主 Agent 和子 Agent 都替换。"""
import deepagents.graph
import deepagents.middleware.subagents
import pytest
from deepagents.middleware.summarization import SummarizationMiddleware
from langchain_core.messages import AIMessage

from kubedoor_ai.domain import Identity, Provider
from kubedoor_ai.gateway import RunContext
from kubedoor_ai.runtime import OPS_SUMMARY_PROMPT
from test_cache_stability import FIRST_TIME, SECOND_TIME, SCOPE, Harness, answer, assert_appends, dumps, inventory, text


# 固定前缀（system + tools）约 8.3K 近似 token；20K 窗口的 50% 阈值是 10K
SMALL = Provider("https://api.deepseek.com", "test-only-key", "deepseek-flash", max_input_tokens=20_000, compression_ratio=0.5)
SUMMARY = AIMessage(content="## 用户的目标和当前进展\n排查 order 服务（已摘要）",
                    usage_metadata={"input_tokens": 1500, "output_tokens": 120, "total_tokens": 1620})


class ContextLengthExceeded(Exception):
    """Shape of an OpenAI-compatible 400 rejection for an oversized prompt."""
    status_code = 400


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["chat", "wecom"])
async def test_summarizes_at_the_window_fraction_and_keeps_the_new_prefix_stable(monkeypatch, origin):
    harness = Harness([answer("答" * 4000), SUMMARY, inventory("call-2"), answer("第二轮完成")], monkeypatch, SMALL)
    try:
        first = await harness.turn("session-1", "run-1", "问" * 4000, FIRST_TIME, origin=origin)
        second = await harness.turn("session-1", "run-2", "第二轮：" + "查" * 4000, SECOND_TIME, origin=origin)
        assert harness.store.statuses.count("completed") == 2
        summary_request, after, follow = second
        # 摘要请求不带工具，用运维摘要提示，只包含被替换掉的旧消息
        assert "tools" not in summary_request
        prompt = text(summary_request["messages"][-1]["content"])
        assert "## 变更记录" in prompt and "问" * 100 in prompt and "第二轮：" not in prompt
        # 摘要后：system 和 tools 不变，旧原文换成摘要消息，只保留最近约 10% 的原文
        assert dumps(after["messages"][0]) == dumps(first[0]["messages"][0]) and dumps(after["tools"]) == dumps(first[0]["tools"])
        assert [m["role"] for m in after["messages"]] == ["system", "user", "user"]
        assert "已摘要" in after["messages"][1]["content"] and "问" * 100 not in dumps(after["messages"])
        assert after["messages"][2]["content"].endswith("查" * 4000)
        # 摘要只让前缀变化一次，之后的请求只追加
        assert_appends(after, follow)
        # 摘要请求不经过 agent 中间件，用 callback 计入这一轮的用量（看板上的压缩次数）
        usage = harness.store.usage["run-2"]
        assert (usage["summary_calls"], usage["summary_input_tokens"], usage["summary_output_tokens"]) == (1, 1500, 120)
        assert usage["model_calls"] == 2 and "summary_calls" not in harness.store.usage["run-1"]
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_context_length_rejection_is_summarized_and_retried_once(monkeypatch):
    provider = Provider("https://api.deepseek.com", "test-only-key", "deepseek-flash", max_input_tokens=20_000, compression_ratio=0.8)
    rejection = ContextLengthExceeded("Error code: 400 - This model's maximum context length is 20000 tokens")
    harness = Harness([answer("答" * 4000), rejection, SUMMARY, answer("重试成功")], monkeypatch, provider)
    try:
        await harness.turn("session-1", "run-1", "问" * 4000, FIRST_TIME)
        rejected, summary_request, retried = await harness.turn("session-1", "run-2", "继续", SECOND_TIME)
        assert harness.store.statuses[-1] == "completed"
        assert "问" * 100 in dumps(rejected["messages"]) and "tools" not in summary_request
        assert "问" * 100 not in dumps(retried["messages"]) and "已摘要" in dumps(retried["messages"])
        assert harness.store.messages[-1]["content"] == "重试成功"
    finally:
        await harness.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("ratio, trigger", [(None, 0.5), (0.6, 0.6)])
async def test_main_and_child_stacks_use_the_ops_summarization_settings(monkeypatch, ratio, trigger):
    stacks = {}
    for module in (deepagents.graph, deepagents.middleware.subagents):
        real = module.create_agent

        def spy(model, *args, _real=real, **kwargs):
            stacks[kwargs.get("name")] = kwargs["middleware"]
            return _real(model, *args, **kwargs)

        monkeypatch.setattr(module, "create_agent", spy)
    provider = Provider("https://api.deepseek.com", "test-only-key", "deepseek-flash", 262_144, compression_ratio=ratio)
    harness = Harness([], monkeypatch, provider)
    try:
        harness.runtime.graph(RunContext({"id": "run-1", "session_id": "s", "scope": SCOPE, "skill_ids": []}, Identity("u", "rw")), provider)
        assert set(stacks) == {None, "general-purpose"}   # 主 Agent 和子 Agent
        found = []
        for stack in stacks.values():
            summaries = [m for m in stack if m.name == "SummarizationMiddleware"]
            assert len(summaries) == 1 and isinstance(summaries[0], SummarizationMiddleware)
            middleware = summaries[0]
            found.append(middleware)
            assert middleware._lc_helper.trigger == ("fraction", trigger)
            assert middleware._lc_helper.keep == ("fraction", 0.1)
            assert middleware._truncate_args_trigger is None
            assert middleware._lc_helper.trim_tokens_to_summarize is None
            assert middleware._lc_helper.summary_prompt == OPS_SUMMARY_PROMPT
            assert middleware.model.profile["max_input_tokens"] == 262_144
            assert [m.name for m in stack].index("SecretRedactionMiddleware") > [m.name for m in stack].index("SummarizationMiddleware")
        assert found[0] is not found[1]
        assert "{messages}" in OPS_SUMMARY_PROMPT and OPS_SUMMARY_PROMPT.format(messages="<m/>").endswith("<m/>\n</messages>")
    finally:
        await harness.close()
