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


def make_model(provider: Provider):
    # Apply DeepSeek's message protocol to official endpoints and compatible
    # gateways exposing DeepSeek model IDs. Other models keep their adapter.
    model_id = provider.model.lower().rsplit("/", 1)[-1]
    deepseek = urlsplit(provider.base_url).hostname == "api.deepseek.com" or model_id.startswith("deepseek-")
    adapter = KubeDoorChatDeepSeek if deepseek else ChatOpenAI
    return adapter(model=provider.model, api_key=provider.api_key or "kubedoor-local-no-auth", base_url=provider.base_url,
                   use_responses_api=False, streaming=True, timeout=90, max_retries=0)
