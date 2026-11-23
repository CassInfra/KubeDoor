"""网页 AI 助手默认用企微专用模型：Service.provider / shared_model，WeComIntegration 提供配置"""
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from fakes import ScriptedModel
from kubedoor_ai.domain import AIError, Identity, Provider, redact
from kubedoor_ai.service import Service
from kubedoor_ai.wecom import WeComIntegration

SHARED = {"base_url": "https://shared.example/v1/chat/completions", "api_key": "shared-model-key", "model": "deepseek-chat",
          "max_input_tokens": 131072, "max_output_tokens": None, "compression_ratio": None, "reasoning_effort": "high"}
CUSTOM = {"base_url": "https://custom.example/v1", "api_key": "custom-key", "model": "custom-model"}
UNAVAILABLE = {"available": False, "base_url": "", "model": "", "max_input_tokens": None, "max_output_tokens": None,
               "compression_ratio": None, "reasoning_effort": None, "api_key_set": False}


def service(settings=None):
    svc = Service.__new__(Service)
    svc.shared_settings = lambda: None if settings is None else dict(settings)
    return svc


def test_wecom_source_uses_the_shared_settings_and_ignores_fields_sent_by_the_browser():
    provider = service(SHARED).provider({"source": "wecom", "base_url": "https://evil.example/v1", "api_key": "x", "model": "m"})
    assert provider == Provider("https://shared.example/v1", "shared-model-key", "deepseek-chat", 131072, None, None, "high")


def test_custom_settings_are_parsed_as_before():
    assert service(SHARED).provider(CUSTOM) == Provider.parse(CUSTOM)
    # 没有企微集成（测试里直接 __new__ 的 Service、create_app 传入的 service）也照常解析自定义模型
    assert Service.__new__(Service).provider(CUSTOM) == Provider.parse(CUSTOM)
    with pytest.raises(AIError) as error:
        service(SHARED).provider({"base_url": "", "model": ""})
    assert error.value.code == "provider_address_invalid"


@pytest.mark.parametrize("svc", [
    Service.__new__(Service),                            # 没有企微集成
    service(None),                                       # 从没保存过企微配置
    service({}),                                         # 只保存过未绑定成员设置，模型为空
    service({**SHARED, "model": ""}),
    service({**SHARED, "base_url": "ftp://shared.example"}),
], ids=["no-integration", "never-saved", "empty", "no-model", "bad-url"])
def test_missing_or_incomplete_shared_model_is_a_409(svc):
    with pytest.raises(AIError) as error:
        svc.provider({"source": "wecom"})
    assert (error.value.status, error.value.code) == (409, "shared_model_unavailable")
    assert "自定义模型" in str(error.value)
    assert svc.shared_model()["available"] is False


def test_shared_model_info_for_the_browser_never_contains_the_key():
    info = service(SHARED).shared_model()
    assert info == {"available": True, "base_url": SHARED["base_url"], "model": "deepseek-chat", "max_input_tokens": 131072,
                    "max_output_tokens": None, "compression_ratio": None, "reasoning_effort": "high", "api_key_set": True}
    assert "shared-model-key" not in str(info)
    # 接口出口的 redact 不能改掉这些字段（has_api_key 这种名字会被打码成 [REDACTED]，所以叫 api_key_set）
    assert redact(info) == info
    assert service().shared_model() == UNAVAILABLE
    # 本地模型可以不填 Key
    local = service({**SHARED, "api_key": ""}).shared_model()
    assert (local["available"], local["api_key_set"]) == (True, False)


def test_memory_summary_resolves_the_model_through_the_service():
    svc = Service(SimpleNamespace(), SimpleNamespace(), SimpleNamespace(model_factory=None))
    svc.shared_settings = lambda: dict(SHARED)
    assert svc.memory_library.provider_parser({"source": "wecom"}).api_key == "shared-model-key"
    assert svc.memory_library.provider_parser(CUSTOM).api_key == "custom-key"


@pytest.mark.asyncio
async def test_session_title_can_use_the_wecom_model():
    class Store:
        async def session(self, session_id, identity):
            return {"id": session_id, "title": "新会话"}

        async def one(self, sql, *args):
            return {"id": args[0], "title": args[1], "updated_at": "now"}

    used = []

    def factory(provider):
        used.append(provider)
        return ScriptedModel(responses=[AIMessage(content="检查日志")])

    svc = service(SHARED)
    svc.store = Store()
    svc.runtime = SimpleNamespace(model_factory=factory)
    result = await svc.generate_session_title(Identity("alice", "rw"), "s-1", "看看 checkout 的日志", {"source": "wecom"})
    assert result["generated"] is True
    assert [(p.model, p.api_key) for p in used] == [("deepseek-chat", "shared-model-key")]


@pytest.mark.asyncio
async def test_provider_test_with_the_wecom_model_reports_unavailable_as_409():
    with pytest.raises(AIError) as error:
        await service().test_provider({"source": "wecom"})
    assert (error.value.status, error.value.code) == (409, "shared_model_unavailable")


def test_wecom_integration_offers_its_model_even_when_the_bot_is_disabled():
    svc = Service.__new__(Service)
    integration = WeComIntegration(svc)
    # 还没加载配置
    assert svc.shared_model()["available"] is False
    integration.config = {**SHARED, "bot_id": "bot", "bot_secret": "bot-secret", "enabled": False}
    assert svc.provider({"source": "wecom"}).api_key == "shared-model-key"
    # 只给模型那几项，不带机器人的 Secret
    assert set(svc.shared_settings()) == set(SHARED)
    integration.config = None
    with pytest.raises(AIError):
        svc.provider({"source": "wecom"})
