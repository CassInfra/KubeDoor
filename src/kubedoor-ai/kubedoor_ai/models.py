"""Provider adapters for the stateless Chat Completions protocol."""
from __future__ import annotations

from urllib.parse import urlsplit

from langchain_core.messages import AIMessage
from langchain_deepseek import ChatDeepSeek
from langchain_openai import ChatOpenAI

from .domain import Provider


class KubeDoorChatDeepSeek(ChatDeepSeek):
    """Keep DeepSeek reasoning across tools, checkpoints and subsequent turns.

    langchain-deepseek 1.1.1 reads reasoning_content from both normal and SSE
    responses, but the inherited OpenAI message serializer drops it on input.
    DeepSeek thinking mode requires it for assistant history in tool requests.
    """

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        messages = self._convert_input(input_).to_messages()
        for message, serialized in zip(messages, payload["messages"], strict=True):
            if isinstance(message, AIMessage):
                reasoning = message.additional_kwargs.get("reasoning_content")
                # Synthetic agent messages or history from another provider have
                # no reasoning. Preserve an explicit empty value for those.
                serialized["reasoning_content"] = reasoning if isinstance(reasoning, str) else ""
                serialized["content"] = serialized.get("content") or ""
        return payload


def resolve_model_limits(provider: Provider, profile=None) -> tuple[int, int]:
    """Return (max input, max output) tokens; values entered by the user win.

    deepseek* and gpt* model IDs (gateway path prefixes ignored) get the
    agreed defaults. Other models use the LangChain built-in profile passed in,
    falling back to 1024K / 128K.
    """
    model_id = provider.model.lower().rsplit("/", 1)[-1]
    if model_id.startswith("deepseek"):
        inferred = (1_048_576, 393_216)
    elif model_id.startswith("gpt"):
        inferred = (1_048_576, 131_072)
    else:
        profile = profile if isinstance(profile, dict) else {}
        inferred = tuple(value if type(value) is int and value > 0 else default for value, default in (
            (profile.get("max_input_tokens"), 1_048_576), (profile.get("max_output_tokens"), 131_072)))
    max_input = provider.max_input_tokens or inferred[0]
    return max_input, min(provider.max_output_tokens or inferred[1], max_input)


def apply_model_limits(model, provider: Provider):
    """Merge the context window into the model profile without dropping its other capabilities."""
    current = model.profile if isinstance(model.profile, dict) else {}
    max_input, max_output = resolve_model_limits(provider, current)
    model.profile = {**current, "max_input_tokens": max_input, "max_output_tokens": max_output}
    return model


def make_model(provider: Provider):
    # Apply DeepSeek's message protocol to official endpoints and compatible
    # gateways exposing DeepSeek model IDs. Other models keep their adapter.
    model_id = provider.model.lower().rsplit("/", 1)[-1]
    deepseek = urlsplit(provider.base_url).hostname == "api.deepseek.com" or model_id.startswith("deepseek-")
    adapter = KubeDoorChatDeepSeek if deepseek else ChatOpenAI
    # DeepSeek thinking effort; unset keeps the provider default (high) and an
    # unchanged request body.
    options = {}
    if adapter is KubeDoorChatDeepSeek and provider.reasoning_effort == "off":
        options["extra_body"] = {"thinking": {"type": "disabled"}}
    elif adapter is KubeDoorChatDeepSeek and provider.reasoning_effort:
        options["reasoning_effort"] = provider.reasoning_effort
    # stream_usage: compatible providers return usage (and DeepSeek cache hits)
    # only when asked; summarization also triggers on the reported size.
    # user_id is never sent: DeepSeek isolates its prefix cache per user_id.
    model = adapter(model=provider.model, api_key=provider.api_key or "kubedoor-local-no-auth", base_url=provider.base_url,
                    use_responses_api=False, streaming=True, stream_usage=True, timeout=90, max_retries=0, **options)
    return apply_model_limits(model, provider)


def without_thinking(model):
    """A DeepSeek copy for tool-free helper calls: summaries, titles, memory drafts.

    Without tools DeepSeek never sends their reasoning back as context, so
    thinking only adds reasoning tokens and latency. The copy shares the client
    and the context window profile. Other models are returned unchanged.
    """
    if not isinstance(model, KubeDoorChatDeepSeek):
        return model
    return model.model_copy(update={"extra_body": {**(model.extra_body or {}), "thinking": {"type": "disabled"}},
                                    "reasoning_effort": None})
