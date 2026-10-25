import json
from unittest.mock import AsyncMock

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.messages import message_to_dict, messages_from_dict
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import InMemorySaver

from kubedoor_ai.domain import CredentialCipher, Identity, Provider
from kubedoor_ai.gateway import Gateway, RunContext
from kubedoor_ai.models import KubeDoorChatDeepSeek, make_model, resolve_model_limits, without_thinking
from kubedoor_ai.runtime import Runtime, SecretRedactionMiddleware
from fakes import FakeStore


TOOL = {"name": "inspect_cluster", "description": "读取集群信息", "parameters": {"type": "object", "properties": {}}}


@pytest.mark.parametrize("model,expected", [
    ("deepseek-flash", (1_048_576, 393_216)),
    ("deepseek/DeepSeek-V4-Pro", (1_048_576, 393_216)),
    ("gpt-5.5", (1_048_576, 131_072)),
    # 按模型名前缀统一设置（D5）：gpt-4o-mini 这类旧型号要用户在界面上手动改小
    ("gpt-4o-mini", (1_048_576, 131_072)),
    ("qwen3", (1_048_576, 131_072)),
])
def test_context_window_follows_model_name_rules(model, expected):
    configured = make_model(Provider("https://gateway.example/v1", "test-only-key", model))
    assert (configured.profile["max_input_tokens"], configured.profile["max_output_tokens"]) == expected
    assert resolve_model_limits(Provider("https://gateway.example/v1", "", model)) == expected


def test_other_models_use_the_langchain_builtin_profile():
    builtin = ChatOpenAI(model="o3", api_key="test-only-key", base_url="https://gateway.example/v1").profile
    assert isinstance(builtin.get("max_input_tokens"), int)
    configured = make_model(Provider("https://gateway.example/v1", "test-only-key", "o3"))
    assert configured.profile["max_input_tokens"] == builtin["max_input_tokens"]
    assert configured.profile["max_output_tokens"] == builtin["max_output_tokens"]


def test_entered_context_window_wins_and_output_never_exceeds_it():
    entered = Provider("https://api.deepseek.com", "test-only-key", "deepseek-flash", 262_144, 65_536, 0.6)
    assert resolve_model_limits(entered) == (262_144, 65_536)
    assert make_model(entered).profile["max_input_tokens"] == 262_144
    only_input = Provider("https://api.deepseek.com", "test-only-key", "deepseek-flash", 65_536)
    assert resolve_model_limits(only_input) == (65_536, 65_536)


def test_model_profile_is_merged_and_streaming_usage_is_requested():
    builtin = KubeDoorChatDeepSeek(model="deepseek-flash", api_key="test-only-key", base_url="https://api.deepseek.com").profile
    configured = make_model(Provider("https://api.deepseek.com", "test-only-key", "deepseek-flash"))
    assert {k: v for k, v in configured.profile.items() if k not in ("max_input_tokens", "max_output_tokens")} == \
        {k: v for k, v in builtin.items() if k not in ("max_input_tokens", "max_output_tokens")}
    assert len(builtin) > 2
    assert configured.stream_usage is True
    assert "user" not in configured._get_request_payload([HumanMessage(content="检查")])


@pytest.mark.parametrize("model, effort, expected", [
    ("deepseek-flash", None, {}),                                            # 默认:请求体和以前完全一样
    ("deepseek-flash", "off", {"extra_body": {"thinking": {"type": "disabled"}}}),
    ("deepseek-flash", "low", {"reasoning_effort": "low"}),
    ("deepseek-flash", "max", {"reasoning_effort": "max"}),
    ("gpt-5.5", "low", {}),                                                  # 只对 DeepSeek 生效
])
def test_thinking_effort_only_changes_deepseek_requests(model, effort, expected):
    configured = make_model(Provider("https://gateway.example/v1", "test-only-key", model, reasoning_effort=effort))
    payload = configured._get_request_payload([HumanMessage(content="检查")])
    assert {k: payload[k] for k in ("extra_body", "reasoning_effort") if k in payload} == expected
    # 摘要、标题这类辅助调用一律关闭思考，不带强度参数
    helper = without_thinking(configured)._get_request_payload([HumanMessage(content="检查")])
    if isinstance(configured, KubeDoorChatDeepSeek):
        assert helper["extra_body"] == {"thinking": {"type": "disabled"}} and "reasoning_effort" not in helper


@pytest.mark.parametrize("base_url,model,deepseek", [
    ("https://api.deepseek.com", "deepseek-flash", True),
    ("https://api.deepseek.com/v1", "deepseek-v4-pro", True),
    ("https://api.deepseek.com", "vendor-model", True),
    ("https://gateway.example/v1", "deepseek-flash", True),
    ("https://gateway.example/v1", "deepseek/deepseek-r1", True),
    ("https://gateway.example/v1", "deepseek-ai/DeepSeek-V3.1", True),
    ("http://local.example/v1", "local-model", False),
])
def test_model_factory_selects_adapter_without_changing_user_endpoint(base_url, model, deepseek):
    configured = make_model(Provider(base_url, "test-only-key", model))
    assert isinstance(configured, KubeDoorChatDeepSeek) is deepseek
    if not deepseek:
        assert isinstance(configured, ChatOpenAI)
    assert str(configured.root_async_client.base_url).rstrip("/") == base_url
    assert configured.model_name == model
    assert configured.use_responses_api is False


def sse_response(model, deltas, finish="stop", usage=None):
    chunks = [
        {"id": "chat-test", "object": "chat.completion.chunk", "model": model,
         "created": 1, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
        for delta in deltas
    ]
    chunks.append({"id": "chat-test", "object": "chat.completion.chunk", "model": model,
                   "created": 1, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})
    if usage:
        # DeepSeek sends the whole request's usage in the last chunk before [DONE].
        chunks.append({"id": "chat-test", "object": "chat.completion.chunk", "model": model,
                       "created": 1, "choices": [], "usage": usage})
    content = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=content)


@pytest.mark.asyncio
async def test_deepseek_cache_hits_in_the_stream_reach_run_usage():
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        return sse_response("deepseek-flash", [{"role": "assistant", "reasoning_content": "核对"}, {"content": "完成"}],
                            usage={"prompt_tokens": 1000, "completion_tokens": 50, "total_tokens": 1050,
                                   "prompt_cache_hit_tokens": 900, "prompt_cache_miss_tokens": 100,
                                   "prompt_tokens_details": {"cached_tokens": 900},
                                   "completion_tokens_details": {"reasoning_tokens": 30}})

    store = FakeStore()
    gateway = Gateway(store, CredentialCipher("ab" * 32), "http://master", "internal")
    provider = Provider("https://mock.example/v1", "test-only-key", "deepseek-flash")
    run = RunContext({"id": "run-1", "session_id": "session-1", "scope": {"env": "cluster-a", "namespace": None, "deployment": None, "pod": None}, "skill_ids": []}, Identity("operator", "rw"), secrets=(provider.api_key,))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        model = KubeDoorChatDeepSeek(model=provider.model, base_url=provider.base_url, api_key=provider.api_key,
                                    http_async_client=client, streaming=True, stream_usage=True, max_retries=0)
        await Runtime(store, gateway, InMemorySaver(), model_factory=lambda _: model).run_graph(run, provider, "检查", None)
    assert store.statuses[-1] == "completed"
    assert requests[0]["stream_options"] == {"include_usage": True} and "user" not in requests[0]
    assert store.usage["run-1"] == {"model_calls": 1, "input_tokens": 1000, "output_tokens": 50, "cache_read_tokens": 900,
                                    "reasoning_tokens": 30, "last_input_tokens": 1000, "max_input_tokens": 1_048_576,
                                    "model": "deepseek-flash"}
    await gateway.close()


@pytest.mark.asyncio
async def test_streamed_thinking_survives_tool_reply_checkpoint_and_next_user_turn():
    requests = []

    def handle(request):
        assert request.url.path == "/v1/chat/completions"
        payload = json.loads(request.content)
        assert "tool_choice" not in payload
        assert "response_format" not in payload
        assert payload["stream"] is True
        assert not payload["tools"][0]["function"].get("strict")
        requests.append(payload)
        if len(requests) == 1:
            return sse_response("deepseek-flash", [
                {"role": "assistant", "reasoning_content": "先读取"},
                {"reasoning_content": "集群状态"},
                {"tool_calls": [{"index": 0, "id": "call-test", "type": "function",
                                 "function": {"name": "inspect_cluster", "arguments": "{}"}}]},
            ], finish="tool_calls")
        return sse_response("deepseek-flash", [{"role": "assistant", "reasoning_content": "已核对证据"}, {"content": "集群正常"}])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        model = KubeDoorChatDeepSeek(model="deepseek-flash", base_url="https://mock.example/v1",
                                    api_key="test-only-key", http_async_client=client,
                                    streaming=True, max_retries=0).bind_tools([TOOL])
        user = HumanMessage(content="检查集群")
        first = await model.ainvoke([user], config={"callbacks": []})
        assert first.additional_kwargs["reasoning_content"] == "先读取集群状态"
        assert first.tool_calls[0]["name"] == "inspect_cluster"
        # LangGraph checkpoints serialize these same message fields.
        restored = messages_from_dict([message_to_dict(first)])[0]
        tool_reply = ToolMessage(content="{}", tool_call_id="call-test")
        second = await model.ainvoke([user, restored, tool_reply], config={"callbacks": []})
        assert requests[1]["messages"][1]["reasoning_content"] == "先读取集群状态"
        assert requests[1]["messages"][1]["content"] == ""
        assert requests[1]["messages"][2]["tool_call_id"] == "call-test"
        assert second.additional_kwargs["reasoning_content"] == "已核对证据"
        await model.ainvoke([user, restored, tool_reply, second, HumanMessage(content="继续检查")], config={"callbacks": []})
        assert requests[2]["messages"][1]["reasoning_content"] == "先读取集群状态"
        # Include reasoning even for previous assistant answers without tool calls.
        assert requests[2]["messages"][3]["reasoning_content"] == "已核对证据"


@pytest.mark.asyncio
async def test_nonstreaming_reasoning_is_kept_and_secret_echo_is_removed_before_storage():
    def handle(request):
        assert json.loads(request.content)["stream"] is False
        return httpx.Response(200, json={
            "id": "chat-test", "object": "chat.completion", "model": "deepseek-flash", "created": 1,
            "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": "查询完成", "reasoning_content": "供应商回显 test-only-key"}}],
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        model = KubeDoorChatDeepSeek(model="deepseek-flash", base_url="https://mock.example",
                                    api_key="test-only-key", http_async_client=client,
                                    streaming=False, max_retries=0)
        response = await model.ainvoke([HumanMessage(content="检查")], config={"callbacks": []})
        assert response.additional_kwargs["reasoning_content"] == "供应商回显 test-only-key"
        SecretRedactionMiddleware(("test-only-key",)).scrub({"messages": [response]})
        restored = messages_from_dict([message_to_dict(response)])[0]
        payload = model._get_request_payload([restored])
        assert payload["messages"][0]["reasoning_content"] == "供应商回显 [redacted]"
        assert "test-only-key" not in json.dumps(message_to_dict(response))


def test_synthetic_and_other_provider_history_get_explicit_empty_reasoning():
    model = make_model(Provider("https://api.deepseek.com", "test-only-key", "deepseek-flash"))
    payload = model._get_request_payload([AIMessage(content="历史回答"), AIMessage(content="", tool_calls=[
        {"name": "inspect_cluster", "args": {}, "id": "call-old", "type": "tool_call"}
    ])])
    assert all(message["reasoning_content"] == "" for message in payload["messages"])
    assert payload["messages"][1]["content"] == ""


@pytest.mark.asyncio
async def test_actual_deepagent_restores_thinking_from_checkpoint_for_a_new_run():
    requests = []

    def handle(request):
        payload = json.loads(request.content)
        assert "tool_choice" not in payload
        requests.append(payload)
        if len(requests) == 1:
            arguments = json.dumps({"operation": "resource_inventory", "source": "kubedoor", "arguments": {}})
            return sse_response("deepseek-flash", [
                {"role": "assistant", "reasoning_content": "读取资源 test-only-key"},
                {"tool_calls": [{"index": 0, "id": "call-live", "type": "function",
                                 "function": {"name": "kubedoor_tool", "arguments": arguments}}]},
            ], finish="tool_calls")
        return sse_response("deepseek-flash", [
            {"role": "assistant", "reasoning_content": "核对监控"}, {"content": "资源查询完成"}
        ])

    store = FakeStore()
    gateway = Gateway(store, CredentialCipher("ab" * 32), "http://master", "internal")
    gateway.execute_action = AsyncMock(return_value={"success": True, "data": {"replicas": 3}})
    provider = Provider("https://mock.example/v1", "test-only-key", "deepseek-flash")
    first = RunContext({"id": "run-1", "session_id": "session-1", "scope": {"env": "cluster-a", "namespace": "default", "deployment": None, "pod": None}, "skill_ids": []}, Identity("operator", "rw"), secrets=(provider.api_key,))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        model = KubeDoorChatDeepSeek(model=provider.model, base_url=provider.base_url, api_key=provider.api_key,
                                    http_async_client=client, streaming=True, max_retries=0)
        runtime = Runtime(store, gateway, InMemorySaver(), model_factory=lambda _: model)
        await runtime.run_graph(first, provider, "读取资源", None)
        assert store.statuses[-1] == "completed"
        gateway.execute_action.assert_awaited_once()
        assert len(requests) == 2
        historical = [m for m in requests[1]["messages"] if m["role"] == "assistant"]
        assert historical[0]["reasoning_content"] == "读取资源 [redacted]"
        # Rebuild the graph for a separate run, as on a real multi-turn request.
        second = RunContext({**first.run, "id": "run-2"}, first.identity, secrets=first.secrets)
        await runtime.run_graph(second, provider, "继续核对", None)
        assert store.statuses[-1] == "completed"
        assert len(requests) == 3
        historical = [m for m in requests[2]["messages"] if m["role"] == "assistant"]
        assert [m["reasoning_content"] for m in historical] == ["读取资源 [redacted]", "核对监控"]
        snapshot = await runtime.graph(second, provider).aget_state({"configurable": {"thread_id": "session-1"}})
        assert "test-only-key" not in str(snapshot.values)
    await gateway.close()


@pytest.mark.asyncio
async def test_streamed_insufficient_resource_finish_is_resent_once():
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return sse_response("deepseek-flash", [{"role": "assistant", "content": "生成到一半"}], finish="insufficient_system_resource")
        return sse_response("deepseek-flash", [{"role": "assistant", "content": "完整回答"}])

    store = FakeStore()
    gateway = Gateway(store, CredentialCipher("ab" * 32), "http://master", "internal")
    provider = Provider("https://mock.example/v1", "test-only-key", "deepseek-flash")
    run = RunContext({"id": "run-1", "session_id": "session-1", "scope": {"env": "cluster-a", "namespace": None, "deployment": None, "pod": None}, "skill_ids": []}, Identity("operator", "rw"), secrets=(provider.api_key,))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        model = KubeDoorChatDeepSeek(model=provider.model, base_url=provider.base_url, api_key=provider.api_key,
                                    http_async_client=client, streaming=True, max_retries=0)
        await Runtime(store, gateway, InMemorySaver(), model_factory=lambda _: model).run_graph(run, provider, "检查", None)
    assert len(requests) == 2 and requests[0]["messages"] == requests[1]["messages"]
    assert store.messages[-1]["content"] == "完整回答"
    await gateway.close()
