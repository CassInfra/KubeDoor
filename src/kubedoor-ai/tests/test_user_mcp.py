"""用户在 AI 助手「MCP 管理」里接入的外部 MCP：user_mcp.py、gateway.invoke_mcp 和 runtime 的 mcp_tool"""
import json
import logging
import socket
import ssl
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx2
import pytest
from fastmcp import Client, FastMCP
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from mcp.shared.exceptions import MCPError
from mcp_types import ToolAnnotations
from starlette.testclient import TestClient

import kubedoor_ai.gateway as gateway_module
from fakes import FakeStore, ScriptedModel, remote_mcp
from kubedoor_ai import user_mcp
from kubedoor_ai.domain import AIError, CredentialCipher, Identity, Provider
from kubedoor_ai.gateway import Gateway, RunContext
from kubedoor_ai.http import create_app
from kubedoor_ai.runtime import Runtime, build_turn_context, text_content
from kubedoor_ai.service import Service


CIPHER = CredentialCipher("ab" * 32)
HEADER_SECRET = "header-secret-123456"
HEADERS = {"Authorization": f"Bearer {HEADER_SECRET}"}
URL = "https://mcp.example.com/mcp?key=url-secret-abcdef"
SCOPE = {"env": "cluster-a", "namespace": None, "deployment": None, "pod": None}
PROVIDER = Provider("http://model/v1", "provider-private-key", "test-model")
READER = Identity("operator", "read")   # 集群只读的账号：照样能用、能批准自己接入的 MCP


def demo_server():
    server = FastMCP("demo", instructions="演示用的天气和工单服务")

    @server.tool(description="查询城市天气", annotations=ToolAnnotations(readOnlyHint=True))
    def weather(city: str) -> str:
        # 回显请求头里的密钥：结果进模型之前要去掉
        return f"{city} 晴，调试信息 {HEADER_SECRET}"

    @server.tool(description="创建工单")
    def create_ticket(title: str) -> dict:
        return {"id": 7, "title": title}

    @server.tool(description="总是失败")
    def broken() -> str:
        raise ValueError("工单系统维护中")

    return server


class Connections:
    """client_factory：连到内存里的 MCP 服务，记下每次连接用的地址和请求头"""
    def __init__(self, server=None):
        self.server, self.calls = server or demo_server(), []

    def __call__(self, transport, url, headers, recorder=None):
        self.calls.append({"transport": transport, "url": url, "headers": dict(headers)})
        return Client(self.server)


async def connected(approval="readonly", enabled=True, username="operator"):
    """一个已经保存、拿到工具列表的 MCP（名为 demo）"""
    store, connections = FakeStore(), Connections()
    mcp = user_mcp.McpServers(store, CIPHER, client_factory=connections)
    gateway = Gateway(store, CIPHER, "http://master", "internal", mcp=mcp)
    probe = await mcp.probe("streamable_http", URL, HEADERS)
    assert probe["error"] is None
    server_id = str(uuid.uuid4())
    store.mcp_servers[server_id] = {
        "id": server_id, "username": username, "name": "demo", "transport": "streamable_http",
        "encrypted_config": mcp.encrypt(server_id, URL, HEADERS), "enabled": enabled, "approval": approval, "revision": 1,
        "tools": probe["tools"], "server_info": probe["server_info"], "last_error": None, "created_at": "2026-10-26T00:00:00+00:00"}
    return SimpleNamespace(store=store, mcp=mcp, gateway=gateway, connections=connections, server_id=server_id)


def context(run_id="run-1", identity=READER, **run):
    return RunContext({"id": run_id, "session_id": "session-1", "scope": SCOPE, "skill_ids": [], "origin": "chat", **run},
                      identity, secrets=(PROVIDER.api_key,))


def mcp_call(tool, arguments, call_id="call-1", server="demo"):
    return AIMessage(content="", tool_calls=[{"name": "mcp_tool", "args": {"server": server, "tool": tool, "arguments": arguments},
                                              "id": call_id, "type": "tool_call"}])


def test_headers_are_validated_and_blank_values_keep_the_saved_ones():
    saved = {"Authorization": "Bearer old", "X-Team": "ops"}
    # 名称不区分大小写地沿用旧值；没列出的旧请求头就是删掉了
    assert user_mcp.parse_headers([{"name": "authorization", "value": ""}, {"name": "X-New", "value": " v "}], saved) == \
        {"authorization": "Bearer old", "X-New": "v"}
    cases = (([{"name": "Host", "value": "x"}], "MCP 客户端自己设置"),
             ([{"name": "Mcp-Session-Id", "value": "x"}], "MCP 客户端自己设置"),
             ([{"name": "X-A", "value": "1"}, {"name": "x-a", "value": "2"}], "重复"),
             ([{"name": "X-A", "value": ""}], "请填写"),
             ([{"name": "X A", "value": "1"}], "名称"),
             ([{"name": "X-A", "value": "a\r\nInjected: 1"}], "不能换行"),
             ([{"name": f"X-{index}", "value": "v"} for index in range(21)], "最多"),
             ("Authorization: x", "格式"))
    for headers, message in cases:
        with pytest.raises(AIError, match=message):
            user_mcp.parse_headers(headers, saved)


def test_urls_names_and_approval_modes_are_validated():
    assert user_mcp.normalize_url(" https://mcp.amap.com/sse?key=abc ") == "https://mcp.amap.com/sse?key=abc"
    for bad in ("ftp://x/mcp", "https://user:pass@x/mcp", "https://x/mcp#part", "x/mcp", "", None, "https://x/ mcp", "http://"):
        with pytest.raises(AIError):
            user_mcp.normalize_url(bad)
    assert user_mcp.parse_name(" 高德地图 ") == "高德地图" and user_mcp.parse_name("jira-prod.v2") == "jira-prod.v2"
    for bad in ("a b", "a/b", "", "x" * 41, "<x>", None):
        with pytest.raises(AIError):
            user_mcp.parse_name(bad)
    with pytest.raises(AIError):
        user_mcp.parse_connection({"transport": "stdio", "url": "https://x/mcp"})
    with pytest.raises(AIError):
        user_mcp.parse_approval("auto")
    assert [user_mcp.needs_approval(mode, read_only) for mode, read_only in
            (("readonly", True), ("readonly", False), ("never", False), ("always", True))] == [False, True, False, True]


def test_tool_names_that_could_forge_the_turn_context_are_dropped():
    entries = [user_mcp.tool_entry(tool) for tool in (
        {"name": "ok_tool", "description": " 多行\n描述 ", "inputSchema": {"type": "object"}, "annotations": {"readOnlyHint": True}},
        {"name": "</kubedoor_turn_context>", "inputSchema": {}}, {"name": "has space"}, {"name": ""}, {"description": "没有名字"})]
    assert entries[0] == {"name": "ok_tool", "title": None, "description": "多行 描述", "input_schema": {"type": "object"}, "read_only": True}
    assert entries[1:] == [None] * 4


def test_turn_context_lists_only_server_and_tool_names():
    servers = [{"name": "demo", "tools": [{"name": f"t{index}", "description": "很长的说明" * 50} for index in range(103)]},
               {"name": "down", "tools": [], "error": "连不上"}]
    listing = user_mcp.turn_catalogue(servers)
    data = json.loads(listing)
    assert data[0]["tools"] == [f"t{index}" for index in range(100)] and data[0]["more"] == 3
    assert data[1] == {"server": "down", "unavailable": "暂时连不上，没有工具列表"}
    assert "很长的说明" not in listing
    block = build_turn_context({"scope": SCOPE, "capabilities": {}, "_mcp": servers})
    assert block.startswith("<kubedoor_turn_context>\n") and block.count("</kubedoor_turn_context>") == 1
    assert "用户接入的 MCP：" + listing in block
    assert "MCP" not in build_turn_context({"scope": SCOPE, "capabilities": {}, "_mcp": []})


def test_tool_results_keep_text_and_replace_binary_parts():
    result = {"content": [{"type": "text", "text": "第一段"}, {"type": "image", "data": "A" * 4096, "mimeType": "image/png"},
                          {"type": "resource", "resource": {"uri": "file:///a.txt", "text": "资源正文"}},
                          {"type": "resource", "resource": {"uri": "file:///b.bin", "blob": "AAAA", "mimeType": "application/zip"}},
                          {"type": "resource_link", "name": "日志", "uri": "https://x/log"}]}
    text, truncated, is_error = user_mcp.call_content(result)
    assert "第一段" in text and "[图片 image/png，约 4 KB，没有提供给模型]" in text and "资源正文" in text
    assert "[二进制资源 file:///b.bin application/zip，没有提供给模型]" in text and "[资源链接 日志 https://x/log]" in text
    assert "AAAA" not in text and not truncated and not is_error
    assert user_mcp.call_content({"content": [], "structuredContent": {"a": 1}}) == ('{"a": 1}', False, False)
    long_text, truncated, _ = user_mcp.call_content({"content": [{"type": "text", "text": "x" * (user_mcp.RESULT_LIMIT + 5)}]})
    assert truncated and len(long_text) == user_mcp.RESULT_LIMIT
    assert user_mcp.call_content({"content": [{"type": "text", "text": "失败了"}], "isError": True})[2] is True


def test_failures_are_explained_without_url_queries_or_header_values():
    secrets = user_mcp.secret_values({"url": URL, "headers": HEADERS})
    assert {f"Bearer {HEADER_SECRET}", HEADER_SECRET, "url-secret-abcdef"} <= set(secrets)
    # 普通的请求头和参数不当密钥，免得结果里的普通词被打码；像随机串的长值照样替换
    plain = user_mcp.secret_values({"url": "https://x/mcp?city=beijing&lang=zh", "headers": {"X-Env": "production",
                                                                                               "X-Trace": "a1b2c3d4e5f6a7b8c9"}})
    assert plain == ("a1b2c3d4e5f6a7b8c9",)
    request = httpx2.Request("POST", URL)
    cases = [
        (httpx2.HTTPStatusError("401", request=request, response=httpx2.Response(401, request=request)), None, "http", "HTTP 401"),
        # Streamable HTTP 把 4xx 都报成笼统的错误，状态码来自响应记录
        (MCPError(-32603, "Server returned an error response"), 404, "http", "可以换成 SSE 试试"),
        (ExceptionGroup("tg", [httpx2.ConnectError("x", request=request)]), None, "connect", "无法连接"),
        (RuntimeError("Client failed to connect"), None, "other", "RuntimeError"),
        (httpx2.ConnectError("dns", request=request), None, "connect", "无法连接"),
        (TimeoutError(), None, "timeout", "超时"),
        (MCPError(-32001, "Request timed out"), None, "timeout", "超时"),
        (ssl.SSLCertVerificationError("bad cert"), None, "tls", "证书"),
        (MCPError(-32603, "boom"), None, "other", "boom"),
    ]
    dns = httpx2.ConnectError("getaddrinfo failed", request=request)
    dns.__cause__ = socket.gaierror(11001, "getaddrinfo failed")
    cases.append((dns, None, "dns", "域名"))
    for exc, status, kind, fragment in cases:
        got_kind, message = user_mcp.classify_failure(exc, "streamable_http", status, secrets)
        assert got_kind == kind and fragment in message, (exc, message)
    kind, message = user_mcp.classify_failure(
        MCPError(-32602, f"Unknown tool, see https://x/doc?key=url-secret-abcdef token {HEADER_SECRET}"), "sse", None, secrets)
    assert kind == "rejected" and "https://x/doc" in message
    assert "url-secret" not in message and HEADER_SECRET not in message


@pytest.mark.asyncio
async def test_probe_lists_tools_and_server_information():
    connections = Connections()
    mcp = user_mcp.McpServers(FakeStore(), CIPHER, client_factory=connections)
    probe = await mcp.probe("sse", URL, {"X-Token": "abc"})
    assert probe["error"] is None
    tools = {tool["name"]: tool for tool in probe["tools"]}
    assert list(tools) == ["weather", "create_ticket", "broken"]
    assert tools["weather"]["read_only"] is True and tools["create_ticket"]["read_only"] is False
    assert tools["weather"]["input_schema"]["required"] == ["city"]
    assert probe["server_info"]["name"] == "demo" and probe["server_info"]["instructions"] == "演示用的天气和工单服务"
    assert connections.calls == [{"transport": "sse", "url": URL, "headers": {"X-Token": "abc"}}]


@pytest.mark.asyncio
async def test_probe_failure_is_reported_and_logged_without_secrets(caplog):
    class Denied:
        def __init__(self, recorder):
            self.recorder = recorder

        async def __aenter__(self):
            await self.recorder.response(SimpleNamespace(status_code=401))
            raise MCPError(-32603, f"Server returned an error response for {URL}")

    caplog.set_level(logging.WARNING, logger="kubedoor_ai.user_mcp")
    mcp = user_mcp.McpServers(FakeStore(), CIPHER, client_factory=lambda transport, url, headers, recorder=None: Denied(recorder))
    probe = await mcp.probe("streamable_http", URL, HEADERS)
    assert probe["tools"] == [] and "HTTP 401" in probe["error"]
    assert "https://mcp.example.com/mcp" in caplog.text
    assert "url-secret" not in caplog.text and HEADER_SECRET not in caplog.text


@pytest.mark.asyncio
async def test_readonly_tool_runs_without_approval_for_a_read_only_account_and_results_are_redacted():
    env = await connected()
    ctx = context()
    result = await env.gateway.invoke_mcp(ctx, "demo", "weather", {"city": "北京"}, call_id="run-1:c1")
    assert result["success"] is True and result["source"] == "mcp" and result["server"] == "demo"
    assert "北京 晴" in result["content"] and HEADER_SECRET not in json.dumps(result, ensure_ascii=False)
    action = next(iter(env.store.actions.values()))
    assert (action["operation"], action["source"], action["read_only"], action["state"]) == ("demo/weather", "mcp", True, "succeeded")
    assert action["preview"]["credential_fingerprint"] == f"mcp:{env.server_id}:1"
    # 调用用的是保存的地址和请求头
    assert env.connections.calls[-1] == {"transport": "streamable_http", "url": URL, "headers": HEADERS}
    assert [event["type"] for event in env.store.events] == ["tool_start", "tool_result"]
    assert env.store.events[0]["data"]["source"] == "mcp" and env.store.events[0]["data"]["name"] == "demo/weather"
    # 同一个调用再来一次（审批恢复时会这样）不会重复执行
    calls = len(env.connections.calls)
    assert (await env.gateway.invoke_mcp(ctx, "demo", "weather", {"city": "北京"}, call_id="run-1:c1"))["success"] is True
    assert len(env.connections.calls) == calls
    await env.gateway.close()


@pytest.mark.asyncio
async def test_describe_returns_the_schema_without_connecting_or_recording_an_action():
    env = await connected()
    ctx = context()
    detail = await env.gateway.invoke_mcp(ctx, "demo", "create_ticket", describe=True)
    assert detail["input_schema"]["required"] == ["title"] and detail["read_only"] is False and detail["needs_approval"] is True
    listing = await env.gateway.invoke_mcp(ctx, "demo", describe=True)
    assert [tool["name"] for tool in listing["tools"]] == ["weather", "create_ticket", "broken"]
    assert listing["instructions"] == "演示用的天气和工单服务"
    assert env.store.actions == {} and len(env.connections.calls) == 1   # 只有 connected() 里拿工具列表的那一次
    for server, tool, code in (("nope", "weather", "mcp_not_found"), ("demo", "nope", "mcp_tool_not_found"), ("demo", "", "invalid_request")):
        with pytest.raises(AIError) as failed:
            await env.gateway.invoke_mcp(ctx, server, tool, {})
        assert failed.value.code == code
    with pytest.raises(AIError, match="对象"):
        await env.gateway.invoke_mcp(ctx, "demo", "weather", ["北京"])
    # 停用的 MCP 不在本轮目录里
    env.store.mcp_servers[env.server_id]["enabled"] = False
    with pytest.raises(AIError) as failed:
        await env.gateway.invoke_mcp(context("run-2"), "demo", "weather", {"city": "北京"})
    assert failed.value.code == "mcp_not_found" and "本轮没有可用的 MCP" in str(failed.value)
    await env.gateway.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("approval,tool,pauses", [("readonly", "weather", False), ("readonly", "create_ticket", True),
                                                   ("never", "create_ticket", False), ("always", "weather", True)])
async def test_approval_mode_decides_which_calls_pause(monkeypatch, approval, tool, pauses):
    env = await connected(approval=approval)
    interrupts = []

    def approve(value):
        interrupts.append(value)
        return {"action_id": value["action_id"], "decision": "approve"}

    monkeypatch.setattr(gateway_module, "interrupt", approve)
    result = await env.gateway.invoke_mcp(context(), "demo", tool, {"city": "上海"} if tool == "weather" else {"title": "t"})
    assert result["success"] is True and bool(interrupts) is pauses
    if pauses:
        assert interrupts[0]["source"] == "mcp" and interrupts[0]["name"] == f"demo/{tool}"
        assert "credential_fingerprint" not in json.dumps(interrupts[0])
    await env.gateway.close()


@pytest.mark.asyncio
async def test_tool_errors_are_known_failures_and_dropped_writes_are_unknown():
    env = await connected(approval="never")
    ctx = context()
    broken = await env.gateway.invoke_mcp(ctx, "demo", "broken", {}, call_id="run-1:c1")
    assert broken["error"]["code"] == "mcp_tool_error" and "工单系统维护中" in broken["error"]["message"]

    class Dropping:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def call_tool_mcp(self, *args, **kwargs):
            raise httpx2.ReadError("connection reset")

    class Refused:
        async def __aenter__(self):
            raise httpx2.ConnectError("All connection attempts failed")

    env.mcp.client_factory = lambda *args, **kwargs: Dropping()
    dropped = await env.gateway.invoke_mcp(ctx, "demo", "create_ticket", {"title": "a"}, call_id="run-1:c2")
    # 调用发出去以后连接断了：写操作可能已经生效，结果未知，不能当失败重试
    assert dropped["error"]["code"] == "outcome_unknown"
    env.mcp.client_factory = lambda *args, **kwargs: Refused()
    refused = await env.gateway.invoke_mcp(ctx, "demo", "create_ticket", {"title": "b"}, call_id="run-1:c3")
    # 还没连上，调用没发出：已知失败
    assert refused["error"]["code"] == "mcp_unavailable" and "无法连接" in refused["error"]["message"]
    states = {action["call_id"]: action["state"] for action in env.store.actions.values()}
    assert states == {"run-1:c1": "failed", "run-1:c2": "unknown", "run-1:c3": "failed"}
    # MCP 的结果未知和集群无关，不拦集群上的修改
    assert await env.store.one("SELECT id FROM kubedoor_ai_actions WHERE run_id=$1::uuid AND state='unknown' AND read_only=false AND source<>'mcp' LIMIT 1", "run-1") is None
    await env.gateway.close()


@pytest.mark.asyncio
async def test_write_tool_waits_for_approval_and_resumes_without_the_turn_catalogue():
    env = await connected()
    model = ScriptedModel(responses=[mcp_call("create_ticket", {"title": "磁盘告警"}), AIMessage(content="工单已创建")])
    runtime = Runtime(env.store, env.gateway, InMemorySaver(), model_factory=lambda _: model)
    ctx = context(_mcp=await env.mcp.catalogue(READER))
    await runtime.run_graph(ctx, PROVIDER, "帮我建一个工单", None)
    assert env.store.statuses[-1] == "waiting_approval"
    action = next(iter(env.store.actions.values()))
    assert action["state"] == "pending" and action["operation"] == "demo/create_ticket" and action["interrupt_id"]
    approval = next(event for event in env.store.events if event["type"] == "approval")["data"]
    assert approval["source"] == "mcp" and "credential_fingerprint" not in json.dumps(approval)
    calls = len(env.connections.calls)
    # 审批恢复：新的上下文，run 里没有提问时加载的目录
    resumed = context()
    await runtime.run_graph(resumed, PROVIDER, None, {action["interrupt_id"]: {"action_id": action["id"], "decision": "approve"}})
    assert env.store.statuses[-1] == "completed"
    done = env.store.actions[action["id"]]
    assert done["state"] == "succeeded" and json.loads(done["result"]["content"]) == {"id": 7, "title": "磁盘告警"}
    assert len(env.connections.calls) == calls + 1
    assert env.store.messages[-1]["content"] == "工单已创建"
    state = await runtime.graph(resumed, PROVIDER).aget_state({"configurable": {"thread_id": "session-1"}})
    user = next(message for message in state.values["messages"] if isinstance(message, HumanMessage))
    block = text_content(user).split("</kubedoor_turn_context>")[0]
    assert '用户接入的 MCP：[{"server":"demo","tools":["weather","create_ticket","broken"]}]' in block
    await env.gateway.close()


@pytest.mark.asyncio
async def test_rejected_and_reconfigured_calls_never_execute():
    env = await connected()
    model = ScriptedModel(responses=[mcp_call("create_ticket", {"title": "a"}), AIMessage(content="已取消"),
                                     mcp_call("create_ticket", {"title": "b"}, "call-2"), AIMessage(content="配置变了，没有执行")])
    runtime = Runtime(env.store, env.gateway, InMemorySaver(), model_factory=lambda _: model)
    calls = len(env.connections.calls)

    async def pending_turn(run_id):
        await runtime.run_graph(context(run_id, _mcp=await env.mcp.catalogue(READER)), PROVIDER, "建工单", None)
        return next(action for action in env.store.actions.values() if action["run_id"] == run_id)

    first = await pending_turn("run-1")
    # 审批期间停用了这个 MCP：拒绝照常记下（恢复时不依赖本轮目录）
    env.store.mcp_servers[env.server_id]["enabled"] = False
    await runtime.run_graph(context("run-1"), PROVIDER, None, {first["interrupt_id"]: {"action_id": first["id"], "decision": "reject"}})
    assert env.store.actions[first["id"]]["state"] == "rejected"
    env.store.mcp_servers[env.server_id]["enabled"] = True
    second = await pending_turn("run-2")
    # 审批期间改了连接配置（版本号变了）：批准也不执行
    env.store.mcp_servers[env.server_id]["revision"] = 2
    await runtime.run_graph(context("run-2"), PROVIDER, None, {second["interrupt_id"]: {"action_id": second["id"], "decision": "approve"}})
    assert env.store.actions[second["id"]]["state"] == "failed"
    assert env.store.actions[second["id"]]["result"]["error"]["code"] == "mcp_changed"
    assert len(env.connections.calls) == calls
    await env.gateway.close()


class DecisionStore(FakeStore):
    def __init__(self, run):
        super().__init__()
        self.run_row = run

    async def run(self, rid, identity):
        if rid != self.run_row["id"] or identity.username != self.run_row["username"]:
            raise AIError("任务不存在", 404, "not_found")
        return dict(self.run_row)


@pytest.mark.asyncio
async def test_mcp_decisions_need_only_the_session_owner_and_an_unchanged_config():
    env = await connected()
    store = DecisionStore({"id": "run-1", "session_id": "session-1", "username": "operator", "scope": SCOPE,
                           "status": "waiting_approval", "cancel_requested": False, "origin": "chat"})
    store.mcp_servers = env.store.mcp_servers
    gateway = Gateway(store, CIPHER, "http://master", "internal", mcp=user_mcp.McpServers(store, CIPHER, client_factory=env.connections))
    runtime = SimpleNamespace(tasks={}, contexts={}, start=Mock())
    service = Service(store, gateway, runtime)
    service.ensure_env = AsyncMock(return_value={"env": "cluster-a", "online": True, "ai_tools": True})
    common = {"run_id": "run-1", "arguments": {}, "read_only": False, "state": "pending", "result": None,
              "connection_revision": None, "expires_at": "2099-01-01T00:00:00+00:00"}
    store.actions["a-mcp"] = {**common, "id": "a-mcp", "call_id": "run-1:m", "operation": "demo/create_ticket", "source": "mcp",
                              "interrupt_id": "interrupt-mcp", "preview": {"server_id": env.server_id, "tool": "create_ticket",
                                                                           "credential_fingerprint": f"mcp:{env.server_id}:1"}}
    store.actions["a-k8s"] = {**common, "id": "a-k8s", "call_id": "run-1:k", "operation": "delete_pod", "source": "kubedoor",
                              "interrupt_id": "interrupt-k8s", "preview": {}}
    body = {"action_id": "a-mcp", "decision": "approve", "provider": {"base_url": "http://model/v1", "api_key": "k", "model": "m"}}
    assert (await service.decision(READER, "run-1", body))["status"] == "running"
    assert runtime.start.call_args.kwargs["resume"] == {"interrupt-mcp": {"decision": "approve", "action_id": "a-mcp"}}
    with pytest.raises(AIError) as denied:
        await service.decision(READER, "run-1", {**body, "action_id": "a-k8s"})
    assert denied.value.status == 403
    with pytest.raises(AIError) as other_user:
        await service.decision(Identity("someone-else", "rw"), "run-1", body)
    assert other_user.value.status == 404
    # 审批期间改了连接配置：不能批准，但可以拒绝
    store.mcp_servers[env.server_id]["revision"] = 2
    runtime.start.reset_mock()
    with pytest.raises(AIError) as changed:
        await service.decision(READER, "run-1", body)
    assert changed.value.code == "mcp_changed"
    runtime.start.assert_not_called()
    assert (await service.decision(READER, "run-1", {**body, "decision": "reject"}))["status"] == "running"
    assert runtime.start.call_args.kwargs["resume"] == {"interrupt-mcp": {"decision": "reject", "action_id": "a-mcp"}}
    await gateway.close()
    await env.gateway.close()


def test_mcp_server_routes_act_for_the_logged_in_user():
    mcp = SimpleNamespace(**{name: AsyncMock(return_value={"ok": name}) for name in ("list", "create", "test", "update", "patch", "delete", "refresh")})
    app = create_app(SimpleNamespace(gateway=SimpleNamespace(mcp=mcp)), token="internal")
    headers = {"X-Kubedoor-Token": "internal", "X-User-Name": "reader", "X-User-Permission": "read"}
    server_id = str(uuid.uuid4())
    with TestClient(app) as client:
        assert client.get("/api/ai/mcp-servers", headers=headers).json() == {"ok": "list"}
        assert client.post("/api/ai/mcp-servers", headers=headers, json={"name": "a"}).json() == {"ok": "create"}
        assert client.post("/api/ai/mcp-servers/test", headers=headers, json={"url": "x"}).json() == {"ok": "test"}
        assert client.put(f"/api/ai/mcp-servers/{server_id}", headers=headers, json={"name": "b"}).json() == {"ok": "update"}
        assert client.patch(f"/api/ai/mcp-servers/{server_id}", headers=headers, json={"enabled": False}).json() == {"ok": "patch"}
        assert client.delete(f"/api/ai/mcp-servers/{server_id}", headers=headers).json() == {"ok": "delete"}
        assert client.post(f"/api/ai/mcp-servers/{server_id}/refresh", headers=headers).json() == {"ok": "refresh"}
        assert client.get("/api/ai/mcp-servers").status_code == 401
        assert client.post("/api/ai/mcp-servers", headers=headers, content="[]").status_code == 400
    assert mcp.list.await_args.args[0].username == "reader"
    assert mcp.create.await_args.args[1] == {"name": "a"}
    assert mcp.update.await_args.args[1:] == (server_id, {"name": "b"})
    assert mcp.patch.await_args.args[1:] == (server_id, {"enabled": False})


@pytest.mark.asyncio
async def test_real_sse_and_streamable_http_send_the_saved_headers_and_explain_rejections(monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "*")   # 不走开发机的系统代理
    async with remote_mcp() as base:
        store = FakeStore()
        mcp = user_mcp.McpServers(store, CIPHER)
        for transport, path in (("streamable_http", "/mcp"), ("sse", "/sse")):
            probe = await mcp.probe(transport, base + path, {"X-Token": "good-token"})
            assert probe["error"] is None and [tool["name"] for tool in probe["tools"]] == ["whoami", "create_ticket"], probe
            denied = await mcp.probe(transport, base + path + "?key=url-secret-abcdef", {"X-Token": "wrong-token"})
            assert "HTTP 401" in denied["error"] and "url-secret" not in denied["error"], denied
        wrong_kind = await mcp.probe("sse", base + "/mcp", {"X-Token": "good-token"})
        assert "连接方式" in wrong_kind["error"], wrong_kind
        # 对话里的调用带上保存的请求头；回显的请求头值在结果里被去掉
        server_id = str(uuid.uuid4())
        store.mcp_servers[server_id] = {"id": server_id, "username": "operator", "name": "remote", "transport": "sse",
                                        "encrypted_config": mcp.encrypt(server_id, base + "/sse", {"X-Token": "good-token"}),
                                        "enabled": True, "approval": "readonly", "revision": 1}
        result = await mcp.execute(READER, {"preview": {"server_id": server_id, "tool": "whoami",
                                                        "credential_fingerprint": f"mcp:{server_id}:1"}, "arguments": {}})
        assert result["success"] is True and json.loads(result["content"]) == {"token": "[redacted]"}, result
