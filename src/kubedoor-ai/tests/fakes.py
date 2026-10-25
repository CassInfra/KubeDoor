import copy
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.messages import AIMessage, AIMessageChunk
from pydantic import Field
from kubedoor_ai.domain import redact
from kubedoor_ai.models import KubeDoorChatDeepSeek


class FakeStore:
    def __init__(self):
        self.actions, self.events, self.messages = {}, [], []
        self.statuses = []
        self.usage = {}
        self.cancelled = False

    async def connection(self, env):
        return None

    async def one(self, sql, *args):
        if "cancel_requested" in sql:
            return {"cancel_requested": self.cancelled}
        if "state='unknown'" in sql:
            return next((a for a in self.actions.values() if a["state"] == "unknown" and not a["read_only"]), None)
        if sql.startswith("INSERT INTO kubedoor_ai_actions"):
            action = dict(zip(("id", "run_id", "call_id", "operation", "source", "arguments", "preview", "read_only", "connection_revision", "state"), args))
            action.update(expires_at="2099-01-01T00:00:00+00:00", interrupt_id=None, result=None)
            self.actions[action["id"]] = copy.deepcopy(action)
            return copy.deepcopy(action)
        if "WHERE call_id=$1" in sql:
            return copy.deepcopy(next((a for a in self.actions.values() if a["call_id"] == args[0]), None))
        action = self.actions.get(args[0]) if args else None
        if "SET state='dispatching'" in sql:
            if action and action["state"] in ("ready", "pending", "approved"):
                action["state"] = "dispatching"
                return {"id": action["id"]}
            return None
        if "expires_at>now()" in sql:
            return {"id": args[0]} if action else None
        if action:
            return copy.deepcopy(action)
        return None

    async def execute(self, sql, *args):
        if "SET interrupt_id=$2" in sql:
            self.actions[args[0]]["interrupt_id"] = args[1]
        elif "SET state=$2,result=$3" in sql:
            self.actions[args[0]].update(state=args[1], result=copy.deepcopy(args[2]))
        elif "SET state='rejected'" in sql:
            self.actions[args[0]].update(state="rejected", result=args[1])
        elif "cancel_requested=true" in sql:
            self.cancelled = True
        elif "SET usage=$2" in sql:
            self.usage[args[0]] = copy.deepcopy(args[1])

    async def event(self, rid, kind, data):
        self.events.append({"seq": len(self.events) + 1, "type": kind, "data": redact(copy.deepcopy(data))})

    async def status(self, rid, status, error=None):
        self.statuses.append(status)
        await self.event(rid, "status", {"status": status})

    async def message(self, run, role, content, tools=None):
        result = {"id": str(uuid.uuid4()), "role": role, "content": redact(content), "scope": run["scope"], "tools": redact(copy.deepcopy(tools))}
        self.messages.append(result)
        return result

    async def fetch(self, sql, *args):
        return copy.deepcopy(list(self.actions.values()))


class ScriptedModel(BaseChatModel):
    responses: list[AIMessage]
    index: int = 0

    @property
    def _llm_type(self):
        return "local-test-model"

    def bind_tools(self, tools, **kwargs):
        return self

    def get_num_tokens_from_messages(self, messages, **kwargs):
        return len(messages) * 100

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        response = self.responses[min(self.index, len(self.responses) - 1)].model_copy(deep=True)
        self.index += 1
        return ChatResult(generations=[ChatGeneration(message=response)])


class RecordingDeepSeek(KubeDoorChatDeepSeek):
    """Record each request as DeepSeek receives it, then return the next scripted reply.

    Payloads are the serialized Chat Completions bodies (system, tools,
    messages with reasoning_content), not LangChain message objects, so prefix
    comparisons match what the provider cache sees. model_copy() variants share
    the script and the payload list. A scripted exception is raised instead of
    replying, e.g. a provider context-length rejection.
    """
    script: list[Any] = Field(default_factory=list)   # AIMessage or an Exception to raise
    payloads: list[dict] = Field(default_factory=list)

    def _reply(self, messages, stop, kwargs):
        self.payloads.append(copy.deepcopy(self._get_request_payload(messages, stop=stop, **kwargs)))
        assert self.script, "预设回答已用完"
        reply = self.script.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply.model_copy(deep=True)

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=self._reply(messages, stop, kwargs))])

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        message = self._reply(messages, stop, kwargs)
        yield ChatGenerationChunk(message=AIMessageChunk(
            content=message.content, additional_kwargs=message.additional_kwargs,
            response_metadata={"model_provider": "deepseek", "finish_reason": "tool_calls" if message.tool_calls else "stop",
                               **message.response_metadata},
            usage_metadata=message.usage_metadata,
            tool_call_chunks=[{"name": call["name"], "args": json.dumps(call["args"], ensure_ascii=False), "id": call["id"],
                               "index": index, "type": "tool_call_chunk"} for index, call in enumerate(message.tool_calls)],
        ))
