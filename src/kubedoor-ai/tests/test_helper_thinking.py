"""FR-12：不带工具的辅助调用（自动摘要、会话标题、记忆总结）在 DeepSeek 上关闭思考；对话本身和测试连接保持思考。"""
import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

import kubedoor_ai.service as service_module
from fakes import RecordingDeepSeek
from kubedoor_ai.domain import Identity, Provider
from kubedoor_ai.memories import MemoryLibrary
from kubedoor_ai.models import make_model, without_thinking
from kubedoor_ai.service import Service
from test_cache_stability import FIRST_TIME, SECOND_TIME, Harness, answer, inventory
from test_memories import PROVIDER as MEMORY_PROVIDER, WRITER, MemoryStore, completed_message
from test_session_title import TitleStore
from test_summarization import SMALL, SUMMARY


DISABLED = {"thinking": {"type": "disabled"}}
DEEPSEEK = {"base_url": "https://api.deepseek.com", "api_key": "test-only-key", "model": "deepseek-flash"}


def recorder(*script):
    return RecordingDeepSeek(model="deepseek-flash", api_key="test-only-key", base_url="https://api.deepseek.com",
                             streaming=True, max_retries=0, script=list(script))


@pytest.mark.asyncio
async def test_summary_request_disables_thinking_but_the_conversation_keeps_it(monkeypatch):
    harness = Harness([answer("答" * 4000), SUMMARY, inventory("call-2"), answer("第二轮完成")], monkeypatch, SMALL)
    try:
        first = await harness.turn("session-1", "run-1", "问" * 4000, FIRST_TIME)
        summary_request, *conversation = await harness.turn("session-1", "run-2", "第二轮：" + "查" * 4000, SECOND_TIME)
        assert "tools" not in summary_request and summary_request["extra_body"] == DISABLED
        assert len(conversation) == 2 and all("extra_body" not in payload for payload in first + conversation)
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_session_title_disables_thinking():
    model = recorder(AIMessage(content="检查日志"))
    service = Service.__new__(Service)
    service.store = TitleStore()
    service.runtime = SimpleNamespace(model_factory=lambda _: model)
    result = await service.generate_session_title(Identity("alice", "rw"), "session-1", "请检查 checkout 服务的异常日志", DEEPSEEK)
    assert result["generated"] is True and result["title"] == "检查日志"
    assert model.payloads[0]["extra_body"] == DISABLED and "tools" not in model.payloads[0]


@pytest.mark.asyncio
async def test_memory_summary_disables_thinking():
    store = MemoryStore()
    store.messages = [completed_message()]
    model = recorder(AIMessage(content=json.dumps({"title": "排障经验", "content": "先查询，再修改。"}, ensure_ascii=False)))
    draft = await MemoryLibrary(store, lambda _: model).summarize(WRITER, "own-session", MEMORY_PROVIDER)
    assert draft == {"title": "排障经验", "content": "先查询，再修改。"}
    assert model.payloads[0]["extra_body"] == DISABLED and "tools" not in model.payloads[0]


@pytest.mark.asyncio
async def test_connection_test_keeps_thinking(monkeypatch):
    model = recorder(AIMessage(content="", tool_calls=[{"name": "kubedoor_connection_probe", "args": {}, "id": "probe", "type": "tool_call"}]))
    monkeypatch.setattr(service_module, "make_model", lambda provider: model)
    result = await Service.__new__(Service).test_provider(DEEPSEEK)
    assert result["tool_calling"] is True
    assert "extra_body" not in model.payloads[0] and model.payloads[0]["tools"]


def test_only_deepseek_models_get_a_thinking_free_copy():
    other = make_model(Provider("https://gateway.example/v1", "test-only-key", "gpt-5.5"))
    assert without_thinking(other) is other
    deepseek = make_model(Provider(**DEEPSEEK))
    copy = without_thinking(deepseek)
    assert copy is not deepseek and copy.extra_body == DISABLED and deepseek.extra_body is None
    # 共用客户端和窗口 profile，摘要阈值仍按同一个窗口计算
    assert copy.profile == deepseek.profile and copy.root_async_client is deepseek.root_async_client
