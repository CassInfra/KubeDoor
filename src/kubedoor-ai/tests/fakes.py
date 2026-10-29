import asyncio
import copy
import json
import socket
import uuid
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import kubedoor_tools
import uvicorn
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.messages import AIMessage, AIMessageChunk
from mcp_types import ToolAnnotations
from pydantic import Field
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from kubedoor_ai.domain import redact
from kubedoor_ai.models import KubeDoorChatDeepSeek
from kubedoor_ai.permissions import OP_NAMES


CONTROL_MESSAGES = {
    "controlled": "受花折资源管控：准入控制会把副本数、requests/limits、JVM 参数和滚动策略改写成管控表的值",
    "cluster_off": "集群未开启准入控制，不受管控",
    "namespace_off": "命名空间不在准入控制的管控列表中，不受管控",
    "namespace_ignored": "命名空间带 kubedoor-ignore 标签，准入控制不处理，不受管控",
    "webhook_missing": "master 记录已开启准入控制，但集群里没有准入 webhook，实际不受管控",
    "unregistered": "管控表里没有该服务，集群开启了新服务免确认：直接放行，不受管控",
    "unregistered_blocked": "管控表里没有该服务，且未开启新服务免确认：这个 Deployment 的创建和修改都会被准入控制拒绝",
}


def control_state(state="namespace_off", registered=None, **table):
    """master /api/ai/internal/deployment-control 返回的 data；effective 的取值规则同 master"""
    registered = state not in ("unregistered", "unregistered_blocked") if registered is None else registered
    values = {"pod_count": 2, "pod_count_ai": -1, "pod_count_manual": -1, "request_cpu_m": 100, "request_mem_mb": 256,
              "limit_cpu_m": 1000, "limit_mem_mb": 1024, "max_unavailable": None, "max_surge": None,
              "jvm_xms_bytes": None, "jvm_xmx_bytes": None, "jvm_xss_bytes": None, "jvm_max_metaspace_bytes": None,
              "update": None, **table}
    manual, ai, count = values["pod_count_manual"], values["pod_count_ai"], values["pod_count"]
    effective = {
        "replicas": manual if manual >= 0 else ai if ai >= 0 else count,
        "request_cpu_m": max(values["request_cpu_m"], 10) if values["request_cpu_m"] >= 0 else None,
        "request_mem_mb": max(values["request_mem_mb"], 1) if values["request_mem_mb"] >= 0 else None,
        "limit_cpu_m": values["limit_cpu_m"] if values["limit_cpu_m"] > 0 else None,
        "limit_mem_mb": values["limit_mem_mb"] if values["limit_mem_mb"] > 0 else None,
        **{field: values[field] for field in ("jvm_xms_bytes", "jvm_xmx_bytes", "jvm_xss_bytes", "jvm_max_metaspace_bytes")},
    }
    return {"state": state, "controlled": state == "controlled", "blocked": state == "unregistered_blocked",
            "message": CONTROL_MESSAGES[state], "registered": registered,
            "control": values if registered else None, "effective": effective if registered else None,
            "admission": {"enabled": state not in ("cluster_off",), "webhook_present": True, "namespace_ignored": False}}


def deployment(replicas=2, image="registry.example.com:5000/team/app:v1", resources=None, args=None, name="app"):
    """在线 Deployment 对象里管控相关的部分"""
    container = {"name": name, "image": image, "resources": copy.deepcopy(resources) if resources is not None else
                 {"requests": {"cpu": "100m", "memory": "256Mi"}, "limits": {"cpu": "1", "memory": "1Gi"}}}
    if args is not None:
        container["args"] = list(args)
    return {"metadata": {"name": "app", "namespace": "demo", "resourceVersion": "7"},
            "spec": {"replicas": replicas, "template": {"spec": {"containers": [container, {"name": "sidecar", "image": "proxy:1"}]}}}}


def stub_control(gateway, control=None, live=None):
    """让网关的管控状态查询和在线 Deployment 读取不走网络；默认不受管控、读不到 Deployment"""
    gateway.deployment_control = AsyncMock(return_value=control or control_state())
    gateway.read_deployment = AsyncMock(return_value=live)
    return gateway


# master 出厂配置里审批列默认勾选的操作（高危命令固定审批）；「—」行和其余行默认不审批
APPROVAL_OPS = frozenset({
    "pod.exec.danger", "workload.restart", "workload.scale", "workload.schedule", "workload.image",
    "pod.delete", "pod.isolate", "pod.jvm", "resource.apply", "resource.delete", "ai.k8s_write",
    "node.cordon", "balance.run", "control.edit", "network.coredns", "network.istio",
})
ADMIN_ONLY_OPS = frozenset({"network.coredns", "network.istio"})


def default_policy():
    """和 master 出厂配置一致的「权限与审批」策略（/api/ai/internal/permission-policy 的返回）：
    只读都不允许，读写、管理员都允许（CoreDNS、Istio 只有管理员）"""
    with open(Path(kubedoor_tools.__file__).with_name("default_commands.json"), encoding="utf-8") as f:
        commands = json.load(f)
    ops = {key: {"read": False, "rw": key not in ADMIN_ONLY_OPS, "admin": True, "approval": key in APPROVAL_OPS}
           for key in OP_NAMES}
    return {"version": 1, "ops": ops, "commands": {"view": commands["view"], "danger": commands["danger"]}}


class FakeStore:
    def __init__(self):
        self.actions, self.events, self.messages = {}, [], []
        self.statuses = []
        self.usage = {}
        self.cancelled = False
        self.mcp_servers = {}   # 用户接入的外部 MCP：id → kubedoor_ai_mcp_servers 的一行

    async def connection(self, env):
        return None

    async def one(self, sql, *args):
        if "FROM kubedoor_ai_mcp_servers" in sql:
            row = self.mcp_servers.get(args[0])
            return copy.deepcopy(row) if row and row["username"] == args[1] else None
        if "cancel_requested" in sql:
            return {"cancel_requested": self.cancelled}
        if "state='unknown'" in sql:
            return next((a for a in self.actions.values() if a["state"] == "unknown" and not a["read_only"]
                         and ("source<>'mcp'" not in sql or a["source"] != "mcp")), None)
        if sql.startswith("INSERT INTO kubedoor_ai_actions"):
            # 按 SQL 里的列名取参数：集群工具的动作带 ops / approval_fixed，用户接入的 MCP 不带
            columns = sql[sql.index("(") + 1:sql.index(")")].split(",")
            action = {"ops": None, "approval_fixed": False, **dict(zip(columns, args))}
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
        if "FROM kubedoor_ai_mcp_servers" in sql:
            # 本轮目录（AND enabled）按创建顺序；缓存都当作还没过期
            rows = [row for row in self.mcp_servers.values() if row["username"] == args[0] and (row["enabled"] or "AND enabled" not in sql)]
            return copy.deepcopy([{**row, "stale": False} for row in sorted(rows, key=lambda row: row["created_at"])])
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


class TokenGuard:
    """只放行带 X-Token: good-token 的请求，其余回 401（模拟要鉴权的 MCP 服务）"""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and dict(scope["headers"]).get(b"x-token") != b"good-token":
            await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
            return
        await self.app(scope, receive, send)


@asynccontextmanager
async def remote_mcp():
    """本机端口上的真实 MCP 服务（/mcp 是 Streamable HTTP，/sse 是 SSE），用户接入的外部 MCP 的测试对端"""
    server = FastMCP("remote")

    @server.tool(description="返回收到的令牌", annotations=ToolAnnotations(readOnlyHint=True))
    def whoami() -> dict:
        return {"token": get_http_headers(include_all=True).get("x-token")}

    @server.tool(description="创建工单")
    def create_ticket(title: str) -> dict:
        return {"id": 7, "title": title}

    streamable, legacy = server.http_app(path="/mcp", transport="http"), server.http_app(path="/sse", transport="sse")

    @asynccontextmanager
    async def lifespan(_):
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(streamable.router.lifespan_context(streamable))
            await stack.enter_async_context(legacy.router.lifespan_context(legacy))
            yield

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    web = uvicorn.Server(uvicorn.Config(TokenGuard(Starlette(routes=[*streamable.routes, *legacy.routes], lifespan=lifespan)),
                                        log_level="error", access_log=False, lifespan="on"))
    task = asyncio.create_task(web.serve(sockets=[sock]))
    try:
        for _ in range(200):
            if web.started:
                break
            await asyncio.sleep(0.025)
        assert web.started
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    finally:
        web.should_exit = True
        await asyncio.wait_for(task, 10)
