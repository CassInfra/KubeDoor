from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from fakes import ScriptedModel
from kubedoor_ai.domain import Identity
from kubedoor_ai.service import Service


class TitleStore:
    def __init__(self, title="新会话"):
        self.title = title
        self.updated = []

    async def session(self, session_id, identity):
        assert session_id == "session-1"
        return {"id": session_id, "title": self.title, "username": identity.username}

    async def one(self, sql, *args):
        assert sql.startswith("UPDATE kubedoor_ai_sessions")
        _, title, username = args
        if self.title != "新会话" or username != "alice":
            return None
        self.title = title
        self.updated.append(title)
        return {"id": "session-1", "title": title, "updated_at": "now"}


@pytest.mark.asyncio
async def test_title_generation_is_tool_free_and_updates_only_default_title():
    model = ScriptedModel(responses=[AIMessage(content="  检查日志。\n")])
    store = TitleStore()
    service = Service.__new__(Service)
    service.store = store
    service.runtime = SimpleNamespace(model_factory=lambda _: model)

    result = await service.generate_session_title(
        Identity("alice", "rw"),
        "session-1",
        "请检查 checkout 服务的异常日志",
        {"base_url": "https://model.example/v1", "api_key": "test", "model": "mock"},
    )

    assert result["generated"] is True
    assert result["title"] == "检查日志"
    assert store.updated == ["检查日志"]
    assert len(model.responses) == 1


@pytest.mark.asyncio
async def test_title_generation_failure_is_best_effort_and_manual_title_is_preserved():
    model = ScriptedModel(responses=[AIMessage(content="新标题")])
    store = TitleStore(title="用户自定义")
    service = Service.__new__(Service)
    service.store = store
    service.runtime = SimpleNamespace(model_factory=lambda _: model)

    result = await service.generate_session_title(
        Identity("alice", "rw"),
        "session-1",
        "第一条消息",
        {"base_url": "https://model.example/v1", "api_key": "test", "model": "mock"},
    )

    assert result == {"id": "session-1", "title": "用户自定义", "generated": False}
    assert store.updated == []
