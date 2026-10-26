"""Menus fetch selected descendants; model context excludes UI option lists."""

import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import Field

from fakes import FakeStore, ScriptedModel
from kubedoor_ai.domain import CredentialCipher, Identity, Provider
from kubedoor_ai.gateway import Gateway, RunContext, tool_catalog
from kubedoor_ai.http import create_app
from kubedoor_ai.runtime import Runtime, text_content
from kubedoor_ai.service import Service


HEADERS = {"X-Kubedoor-Token": "test-token", "X-User-Name": "operator", "X-User-Permission": "read"}
UI_ONLY_NAMES = ["namespace-option-never-selected", "deployment-option-never-selected", "pod-option-never-selected"]


@pytest_asyncio.fixture
async def menus():
    calls = []
    selector = {"matchLabels": {"app": "exporter"}, "matchExpressions": [{"key": "tier", "operator": "In", "values": ["metrics", "monitoring"]}]}

    def master(request):
        assert request.url.path == "/api/ai/internal/agent-execute"
        payload = json.loads(request.content)
        assert dict(request.url.params) == {"env": "cluster-a"}
        assert payload["phase"] == "execute"
        assert payload["operation"] == "api"
        arguments = payload["arguments"]
        assert arguments["method"] == "GET"
        calls.append(copy.deepcopy(arguments))
        if arguments["path"] == "/apis/apps/v1/namespaces/demo/deployments/exporter":
            data = {"spec": {"selector": selector}}
        else:
            data = {"items": [{"metadata": {"name": "item-b", "namespace": "demo"}}, {"metadata": {"name": "item-a", "namespace": "demo"}}], "metadata": {}}
        return httpx.Response(200, json={"success": True, "data": data})

    store = FakeStore()
    store.connection = AsyncMock(return_value=None)
    http = httpx.AsyncClient(transport=httpx.MockTransport(master))
    gateway = Gateway(store, CredentialCipher("ab" * 32), "http://fixed-master", "test-token", http=http)
    service = Service(store, gateway, None)
    service.ensure_env = AsyncMock(return_value={"env": "cluster-a", "online": True, "ai_tools": True})
    app = create_app(service, token="test-token")
    # Exercise the actual ASGI routes/authentication without starting unrelated
    # MCP task groups across pytest's separate fixture setup/teardown tasks.
    app.state.service = service
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://local-test", headers=HEADERS) as client:
        yield SimpleNamespace(client=client, service=service, store=store, calls=calls, selector=selector)
    await gateway.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("query", [
    {"kind": "namespaces"},
    {"env": "", "kind": "namespaces"},
    {"env": "cluster-a", "kind": "deployments"},
    {"env": "cluster-a", "kind": "deployments", "namespace": ""},
    {"env": "cluster-a", "kind": "pods"},
    {"env": "cluster-a", "kind": "pods", "namespace": "demo"},
    {"env": "cluster-a", "kind": "pods", "deployment": "exporter", "deployment_namespace": "demo"},
    {"env": "cluster-a", "kind": "pods", "namespace": "demo", "deployment": "exporter", "deployment_namespace": "other-ns"},
])
async def test_menu_without_selected_parent_rejects_before_any_cluster_lookup(menus, query):
    response = await menus.client.get("/api/ai/resources", params=query)
    assert response.status_code == 400
    assert menus.calls == []
    menus.service.ensure_env.assert_not_awaited()
    menus.store.connection.assert_not_awaited()


@pytest.mark.asyncio
async def test_namespace_menu_only_reads_namespaces_for_selected_cluster(menus):
    response = await menus.client.get("/api/ai/resources", params={"env": "cluster-a", "kind": "namespaces"})
    assert response.status_code == 200
    assert [request["path"] for request in menus.calls] == ["/api/v1/namespaces"]
    assert response.json()["source"] == "agent"
    assert [item["name"] for item in response.json()["items"]] == ["item-a", "item-b"]


@pytest.mark.asyncio
async def test_deployment_menu_only_reads_deployments_in_selected_namespace(menus):
    response = await menus.client.get("/api/ai/resources", params={"env": "cluster-a", "kind": "deployments", "namespace": "demo"})
    assert response.status_code == 200
    assert [request["path"] for request in menus.calls] == ["/apis/apps/v1/namespaces/demo/deployments"]
    assert menus.calls[0]["query"] == {"limit": "100"}


@pytest.mark.asyncio
async def test_pod_menu_reads_selected_deployment_then_only_its_pod_selector(menus):
    response = await menus.client.get("/api/ai/resources", params={"env": "cluster-a", "kind": "pods", "namespace": "demo", "deployment": "exporter", "deployment_namespace": "demo"})
    assert response.status_code == 200
    assert [request["path"] for request in menus.calls] == ["/apis/apps/v1/namespaces/demo/deployments/exporter", "/api/v1/namespaces/demo/pods"]
    assert menus.calls[1]["query"] == {"limit": "100", "labelSelector": "app=exporter,tier in (metrics,monitoring)"}


@pytest.mark.asyncio
async def test_invalid_empty_deployment_selector_never_fetches_all_namespace_pods(menus):
    menus.selector.clear()
    response = await menus.client.get("/api/ai/resources", params={"env": "cluster-a", "kind": "pods", "namespace": "demo", "deployment": "exporter"})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "resource_response_invalid"
    assert [request["path"] for request in menus.calls] == ["/apis/apps/v1/namespaces/demo/deployments/exporter"]


@pytest.mark.asyncio
async def test_unauthenticated_menu_request_does_not_query_selected_cluster(menus):
    response = await menus.client.get("/api/ai/resources", params={"env": "cluster-a", "kind": "namespaces"}, headers={"X-Kubedoor-Token": "wrong-token"})
    assert response.status_code == 401
    assert menus.calls == []
    menus.service.ensure_env.assert_not_awaited()


class RecordingModel(ScriptedModel):
    histories: list[list] = Field(default_factory=list)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.histories.append(copy.deepcopy(messages))
        return super()._generate(messages, stop, run_manager, **kwargs)


def prompts(messages):
    """系统提示和当轮用户消息开头的本轮上下文块"""
    system = "\n".join(text_content(message) for message in messages if isinstance(message, SystemMessage))
    user = [text_content(message) for message in messages if isinstance(message, HumanMessage)][-1]
    assert user.startswith("<kubedoor_turn_context>\n")
    return system, user.split("</kubedoor_turn_context>", 1)[0]


def turn_line(block, label):
    return block.split("\n" + label, 1)[1].split("\n", 1)[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("selected", [
    {"env": "cluster-a"},
    {"env": "cluster-a", "namespace": "demo"},
    {"env": "cluster-a", "namespace": "demo", "deployment": {"namespace": "demo", "name": "exporter"}},
    {"env": "cluster-a", "namespace": "demo", "deployment": {"namespace": "demo", "name": "exporter"}, "pod": {"namespace": "demo", "name": "exporter-pod"}},
])
async def test_actual_model_receives_only_selected_scope_without_menu_option_lists(selected):
    store = FakeStore()
    gateway = Gateway(store, CredentialCipher("ab" * 32), "http://unused-master", "test-token")
    model = RecordingModel(responses=[AIMessage(content="已读取所选范围。")])
    runtime = Runtime(store, gateway, InMemorySaver(), model_factory=lambda _: model)
    scope = {"env": "cluster-a", "namespace": None, "deployment": None, "pod": None, **selected,
             "namespace_options": UI_ONLY_NAMES, "deployment_options": UI_ONLY_NAMES, "pod_options": UI_ONLY_NAMES}
    original = copy.deepcopy(scope)
    ctx = RunContext({"id": "run-1", "session_id": "session-1", "scope": scope, "skill_ids": []}, Identity("operator", "read"))
    try:
        await runtime.run_graph(ctx, Provider("http://unused-model/v1", "", "mock-model"), "查询当前集群", None)
        assert store.statuses[-1] == "completed"
        system, block = prompts(model.histories[0])
        assert json.loads(turn_line(block, "本轮范围：")) == selected
        assert ctx.run["scope"] == original
        for name in UI_ONLY_NAMES:
            assert name not in system and name not in block
        assert "北京时间：" in block and "北京时间：" not in system
        assert "定时与周期任务的 run_at、Cron 均按北京时间" in block
        for entry in tool_catalog():
            assert entry["name"] in system
    finally:
        await gateway.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("origin, hinted", [("wecom", True), ("chat", False)])
async def test_wecom_runs_tell_the_model_how_to_switch_clusters(origin, hinted):
    store = FakeStore()
    gateway = Gateway(store, CredentialCipher("ab" * 32), "http://unused-master", "test-token")
    model = RecordingModel(responses=[AIMessage(content="当前只能查看 cluster-a。")])
    runtime = Runtime(store, gateway, InMemorySaver(), model_factory=lambda _: model)
    scope = {"env": "cluster-a", "namespace": None, "deployment": None, "pod": None}
    ctx = RunContext({"id": "run-1", "session_id": "session-1", "origin": origin, "scope": scope, "skill_ids": []},
                     Identity("operator", "read"))
    try:
        await runtime.run_graph(ctx, Provider("http://unused-model/v1", "", "mock-model"), "查一下别的集群", None)
        system, block = prompts(model.histories[0])
        # 渠道提示在本轮上下文块里，系统提示对网页和企微完全相同
        assert ("切换集群" in turn_line(block, "对话渠道：")) is hinted
        assert "切换集群" not in system
        assert json.loads(turn_line(block, "本轮范围：")) == {"env": "cluster-a"}
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_wecom_switch_finishes_the_turn_normally_and_the_rerun_sees_the_real_result():
    store = FakeStore()
    gateway = Gateway(store, CredentialCipher("ab" * 32), "http://unused-master", "test-token")
    switch = AIMessage(content="", tool_calls=[{"name": "kubedoor_tool", "id": "switch-1", "type": "tool_call",
                                                "args": {"operation": "switch_cluster", "arguments": {"env": "cluster-b"}, "source": "auto"}}])
    model = RecordingModel(responses=[switch, AIMessage(content="正在切换到 cluster-b 查询。"), AIMessage(content="cluster-b 的版本是 v1.30。")])
    runtime = Runtime(store, gateway, InMemorySaver(), model_factory=lambda _: model)
    provider = Provider("http://unused-model/v1", "", "mock-model")
    clusters = [{"env": "cluster-a", "permission": "read", "aliases": []},
                {"env": "cluster-b", "permission": "read", "aliases": ["beta 昆仑"]}]
    scope = {"env": "cluster-a", "namespace": None, "deployment": None, "pod": None}
    run = {"id": "run-1", "session_id": "session-1", "origin": "wecom", "skill_ids": [], "_clusters": clusters, "scope": scope}
    try:
        await runtime.run_graph(RunContext(run, Identity("operator", "read")), provider, "cassmall beta 昆仑集群的版本", None)
        system, block = prompts(model.histories[0])
        # 可访问的集群和别名在本轮上下文块里，系统提示不变
        assert json.loads(turn_line(block, "可切换的集群：")) == [{"env": "cluster-a"}, {"env": "cluster-b", "aliases": ["beta 昆仑"]}]
        assert "可切换的集群" not in system and "switch_cluster" in turn_line(block, "对话渠道：")
        # 切换只记下目标，不访问集群；模型看到结果后一句话收尾，本轮正常结束，done 带上目标
        assert len(model.histories) == 2 and store.actions == {}
        assert store.statuses[-1] == "completed" and store.messages[-1]["content"] == "正在切换到 cluster-b 查询。"
        assert store.events[-1]["type"] == "done" and store.events[-1]["data"]["switch_env"] == "cluster-b"

        # 企微在 cluster-b 上重新提问：检查点里是真实的切换结果（没有被当成中断的工具调用补一条），不再提供切换
        rerun = {**run, "id": "run-2", "_switched": True, "scope": {**scope, "env": "cluster-b"}}
        await runtime.run_graph(RunContext(rerun, Identity("operator", "read")), provider,
                                "已切换到集群 cluster-b，请在这个集群上回答我上一条消息里的问题。", None)
        history = model.histories[2]
        results = [text_content(message) for message in history if isinstance(message, ToolMessage)]
        assert len(results) == 1 and "已请求切换到集群 cluster-b" in results[0]
        assert not any(line.startswith("可切换的集群：") for line in prompts(history)[1].split("\n"))
        assert store.events[-1]["data"].get("switch_env") is None
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_after_a_switch_request_the_turn_cannot_touch_the_old_cluster():
    from kubedoor_ai.domain import AIError
    from kubedoor_ai.gateway import request_cluster_switch
    store = FakeStore()
    gateway = Gateway(store, CredentialCipher("ab" * 32), "http://unused-master", "test-token")
    run = {"id": "run-1", "origin": "wecom", "_clusters": [{"env": "cluster-a"}, {"env": "cluster-b"}],
           "scope": {"env": "cluster-a", "namespace": None, "deployment": None, "pod": None}}
    ctx = RunContext(run, Identity("operator", "read"))
    try:
        request_cluster_switch(ctx, {"env": "cluster-b"})
        with pytest.raises(AIError) as error:
            await gateway.invoke(ctx, "pod_list", {})
        assert error.value.code == "switch_pending" and store.actions == {}
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_new_run_projects_ui_option_lists_out_of_stored_scope():
    async def insert(sql, *args):
        assert sql.startswith("INSERT INTO kubedoor_ai_runs")
        return {"id": args[0], "session_id": args[1], "scope": copy.deepcopy(args[4]), "status": "running"}

    store = SimpleNamespace(session=AsyncMock(return_value={"id": "session-1"}), one=AsyncMock(side_effect=insert))
    service = Service(store, None, None)
    service.ensure_env = AsyncMock(return_value={"env": "cluster-a", "online": True, "ai_tools": True})
    run, created = await service.new_run(Identity("operator", "read"), "session-1", {"env": "cluster-a", "namespace_options": UI_ONLY_NAMES})
    assert created is True
    assert run["scope"] == {"env": "cluster-a", "namespace": None, "deployment": None, "pod": None}
    assert "namespace-option-never-selected" not in str(run)


@pytest.mark.asyncio
async def test_unselected_scope_still_allows_cluster_wide_pod_list_tool():
    store, requests = FakeStore(), []

    def master(request):
        requests.append(request)
        assert request.url.path == "/api/agent/pods"
        assert dict(request.url.params) == {"env": "cluster-a"}
        return httpx.Response(200, json={"success": True, "data": []})

    http = httpx.AsyncClient(transport=httpx.MockTransport(master))
    gateway = Gateway(store, CredentialCipher("ab" * 32), "http://fixed-master", "test-token", http=http)
    call = AIMessage(content="", tool_calls=[{"name": "kubedoor_tool", "args": {"operation": "pod_list", "arguments": {}, "source": "auto"}, "id": "all-pods", "type": "tool_call"}])
    model = RecordingModel(responses=[call, AIMessage(content="当前集群没有 Pod。")])
    runtime = Runtime(store, gateway, InMemorySaver(), model_factory=lambda _: model)
    ctx = RunContext({"id": "run-1", "session_id": "session-1", "scope": {"env": "cluster-a", "namespace": None, "deployment": None, "pod": None}, "skill_ids": []}, Identity("operator", "read"))
    try:
        await runtime.run_graph(ctx, Provider("http://unused-model/v1", "", "mock-model"), "查询所选集群全部 Pod", None)
        assert store.statuses[-1] == "completed"
        assert len(requests) == 1
        assert list(store.actions.values())[0]["read_only"] is True
        assert ctx.run["scope"]["namespace"] is None
    finally:
        await gateway.close()
