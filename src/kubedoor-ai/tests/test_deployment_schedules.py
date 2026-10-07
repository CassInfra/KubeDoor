"""Exercise the existing KubeDoor scheduling contract without a model or K8S server."""

import copy
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import pytest_asyncio
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import Field

from fakes import FakeStore, ScriptedModel
import kubedoor_ai.gateway as gateway_module
from kubedoor_ai.domain import AIError, BEIJING_TZ, CredentialCipher, Identity, Provider
from kubedoor_ai.gateway import Gateway, PendingAction, RunContext, tool_catalog, validate_deployment_change
from kubedoor_ai.runtime import Runtime, SKILLS_ROOT


SCHEDULE_DATE = (datetime.now(timezone.utc) + timedelta(days=2)).replace(second=0, microsecond=0, tzinfo=None)
SCHEDULE_TIME = [SCHEDULE_DATE.year, SCHEDULE_DATE.month, SCHEDULE_DATE.day, SCHEDULE_DATE.hour, SCHEDULE_DATE.minute]
SCHEDULE_RUN_AT = SCHEDULE_DATE.isoformat(timespec="minutes")
SCHEDULES = (
    ("schedule_deployment_restart", "restart", {"run_at": SCHEDULE_RUN_AT}, SCHEDULE_TIME, ""),
    ("cron_deployment_restart", "restart", {"cron": "0 2 * * *"}, "", "0 2 * * *"),
    ("schedule_deployment_scale", "scale", {"run_at": SCHEDULE_RUN_AT.replace("T", " "), "replicas": 3}, SCHEDULE_TIME, ""),
    ("cron_deployment_scale", "scale", {"cron": "0 2 * * *", "replicas": 3}, "", "0 2 * * *"),
)


def context(permission="rw", **scope):
    selected = {
        "env": "cluster-a",
        "namespace": "demo",
        "deployment": {"namespace": "demo", "name": "deploy-demo-order"},
        "pod": None,
    }
    return RunContext(
        {"id": "run-1", "session_id": "session-1", "scope": {**selected, **scope}, "skill_ids": []},
        Identity("operator", permission),
    )


@pytest_asyncio.fixture
async def master_gateway():
    calls = []
    response = {"message": "ok"}

    def handle(request):
        calls.append(request)
        return httpx.Response(response.get("_http_status", 200), json={key: value for key, value in response.items() if key != "_http_status"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    gateway = Gateway(FakeStore(), CredentialCipher("ab" * 32), "http://fixed-master", "test-service-token", http=client)
    yield gateway, calls, response
    await gateway.close()


async def propose(gateway, ctx, operation, arguments, call_id="schedule-test"):
    with pytest.raises(PendingAction) as pending:
        await gateway.invoke(ctx, operation, arguments, call_id=call_id, mcp=True)
    action = gateway.store.actions[pending.value.action["action_id"]]
    assert action["state"] == "pending"
    assert action["source"] == "kubedoor"
    assert action["read_only"] is False
    return action, pending.value.action


def decoded_body(request):
    return json.loads(request.content)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,kind,arguments,time,cron", SCHEDULES)
async def test_schedules_use_existing_master_interface_after_approval(master_gateway, operation, kind, arguments, time, cron):
    gateway, calls, _ = master_gateway
    ctx = context()
    action, approval = await propose(gateway, ctx, operation, arguments)
    assert calls == [], "Preparing an approval must not submit a CronJob."

    deployment = {"namespace": "demo", "deployment_name": "deploy-demo-order"}
    if kind == "scale":
        deployment["num"] = 3
    body = {"type": kind, "service": {"deployment_list": [deployment], "node_scheduler": []}, "time": time, "cron": cron}
    assert approval["preview"]["method"] == "POST"
    assert approval["preview"]["path"] == "/api/cron"
    assert approval["preview"]["query"] == {"env": "cluster-a"}
    assert approval["preview"]["body"] == body

    result = await gateway.dispatch(ctx, action)
    assert result["success"] is True
    assert result["source"] == "kubedoor"
    assert result["schedule"]["time_zone"] == "Asia/Shanghai"
    assert len(calls) == 1
    request = calls[0]
    assert request.method == "POST"
    assert request.url.host == "fixed-master"
    assert request.url.path == "/api/cron"
    assert dict(request.url.params) == {"env": "cluster-a"}
    assert request.headers["X-Kubedoor-Token"] == "test-service-token"
    assert request.headers["X-User-Name"] == "operator"
    assert request.headers["X-User-Permission"] == "rw"
    assert decoded_body(request) == body
    await gateway.dispatch(ctx, action)
    assert len(calls) == 1, "Resuming twice must not create a duplicate schedule."


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,arguments,num", [("restart_deployment", {}, None), ("scale_deployment", {"replicas": 0}, 0)])
async def test_immediate_operations_use_current_agent_object_contract(master_gateway, operation, arguments, num):
    gateway, calls, _ = master_gateway
    ctx = context()
    action, _ = await propose(gateway, ctx, operation, arguments)
    assert calls == []
    result = await gateway.dispatch(ctx, action)
    assert result["success"] is True
    deployment = {"namespace": "demo", "deployment_name": "deploy-demo-order"}
    if num is not None:
        deployment["num"] = num
    assert decoded_body(calls[0]) == {"deployment_list": [deployment], "node_scheduler": []}
    assert calls[0].url.path == ("/api/restart" if num is None else "/api/scale")


@pytest.mark.asyncio
async def test_explicit_target_wins_over_selector_and_frozen_arguments_do_not_drift(master_gateway):
    gateway, calls, _ = master_gateway
    ctx = context()
    arguments = {"namespace": "other-ns", "deployment": "other-app", "cron": "0 2 * * *", "replicas": 4}
    action, approval = await propose(gateway, ctx, "cron_deployment_scale", arguments)
    expected = copy.deepcopy(approval["preview"]["body"])
    arguments.update(namespace="changed", deployment="changed", cron="* * * * *", replicas=99)
    approval["preview"]["body"]["service"]["deployment_list"][0]["num"] = 100
    assert calls == []
    result = await gateway.dispatch(ctx, action)
    assert result["success"] is True
    assert decoded_body(calls[0]) == expected
    assert expected["service"]["deployment_list"] == [{"namespace": "other-ns", "deployment_name": "other-app", "num": 4}]
    assert expected["cron"] == "0 2 * * *"


@pytest.mark.asyncio
async def test_deployment_selector_provides_namespace_when_namespace_selector_is_all(master_gateway):
    gateway, calls, _ = master_gateway
    ctx = context(namespace=None)
    action, _ = await propose(gateway, ctx, "schedule_deployment_restart", {"run_at": SCHEDULE_RUN_AT + ":00"})
    await gateway.dispatch(ctx, action)
    assert decoded_body(calls[0])["time"] == SCHEDULE_TIME
    assert decoded_body(calls[0])["service"]["deployment_list"] == [{"namespace": "demo", "deployment_name": "deploy-demo-order"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,kind,arguments,time,cron", SCHEDULES)
async def test_read_only_identity_cannot_prepare_a_schedule(master_gateway, operation, kind, arguments, time, cron):
    gateway, calls, _ = master_gateway
    with pytest.raises(AIError) as denied:
        await gateway.invoke(context("read"), operation, arguments, mcp=True)
    assert denied.value.status == 403
    assert gateway.store.actions == {}
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", [
    {},
    {"run_at": "not-a-date"},
    {"run_at": "2099-02-30T02:00"},
    {"run_at": "2099-10-06T25:00"},
    {"run_at": "2099-10-06T02:00:01"},
    {"run_at": "2099-10-06T02:00+08:00"},
    {"run_at": "2099-10-06T02:00Z"},
    {"run_at": "2099-10-06T02:00"},
    {"run_at": "2000-10-06T02:00"},
])
async def test_invalid_or_unsupported_time_is_rejected_before_approval(master_gateway, arguments):
    gateway, calls, _ = master_gateway
    with pytest.raises(AIError):
        await gateway.invoke(context(), "schedule_deployment_restart", arguments, mcp=True)
    assert gateway.store.actions == {}
    assert calls == []


# Agent 创建的 CronJob 固定 spec.timeZone=Asia/Shanghai，run_at 按北京时间校验。
FIXED_BEIJING_NOW = datetime(2026, 10, 7, 10, 0, tzinfo=BEIJING_TZ)


@pytest.mark.parametrize("run_at,accepted", [
    ("2026-10-07T10:30", True),   # 北京时间今天稍后
    ("2026-10-07T09:30", False),  # 北京时间已过去；按 UTC（02:00）解释仍在未来，不能放行
    ("2027-10-07T09:30", True),   # 该月日时分今年已过，下一次在明年
    ("2027-10-07T10:30", False),  # 今年还会到达，不能写成明年
    ("2028-01-01T00:00", False),  # 下一次 1 月 1 日在 2027 年，更远的年份拒绝
    ("2028-02-29T02:00", True),   # 下一个 2 月 29 日在 2028 年
])
def test_run_at_is_validated_in_beijing_time(monkeypatch, run_at, accepted):
    monkeypatch.setattr(gateway_module, "beijing_now", lambda: FIXED_BEIJING_NOW)
    arguments = {"namespace": "demo", "deployment": "deploy-demo-order", "run_at": run_at}
    if accepted:
        validate_deployment_change("schedule_deployment_restart", arguments)
    else:
        with pytest.raises(AIError):
            validate_deployment_change("schedule_deployment_restart", arguments)


@pytest.mark.asyncio
@pytest.mark.parametrize("cron", ["", "0 2 * *", "0 0 2 * * *", "@reboot", "not a cron expression"])
async def test_invalid_cron_is_rejected_before_approval(master_gateway, cron):
    gateway, calls, _ = master_gateway
    with pytest.raises(AIError):
        await gateway.invoke(context(), "cron_deployment_restart", {"cron": cron}, mcp=True)
    assert gateway.store.actions == {}
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("cron", ["@daily", "@hourly", "*/15 2-6 * * 1-5"])
async def test_standard_cron_syntax_is_forwarded_without_reinterpretation(master_gateway, cron):
    gateway, calls, _ = master_gateway
    ctx = context()
    action, _ = await propose(gateway, ctx, "cron_deployment_restart", {"cron": cron})
    await gateway.dispatch(ctx, action)
    assert decoded_body(calls[0])["cron"] == cron
    assert decoded_body(calls[0])["time"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,arguments", [
    ("scale_deployment", {"replicas": True}),
    ("scale_deployment", {"replicas": -1}),
    ("schedule_deployment_scale", {"run_at": SCHEDULE_RUN_AT, "replicas": True}),
    ("schedule_deployment_scale", {"run_at": SCHEDULE_RUN_AT, "replicas": -1}),
    ("cron_deployment_scale", {"cron": "0 2 * * *", "replicas": True}),
    ("cron_deployment_scale", {"cron": "0 2 * * *", "replicas": -1}),
])
async def test_invalid_replica_counts_are_not_approved(master_gateway, operation, arguments):
    gateway, calls, _ = master_gateway
    with pytest.raises(AIError):
        await gateway.invoke(context(), operation, arguments, mcp=True)
    assert gateway.store.actions == {}
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [{"resource_type": "pod"}, {"resource_type": "statefulset"}, {"pod": "selected-pod"}])
async def test_deployment_schedules_do_not_accept_another_resource_kind(master_gateway, extra):
    gateway, calls, _ = master_gateway
    with pytest.raises(AIError):
        await gateway.invoke(context(), "cron_deployment_restart", {"cron": "0 2 * * *", **extra}, mcp=True)
    assert gateway.store.actions == {}
    assert calls == []


@pytest.mark.asyncio
async def test_pod_selector_is_not_silently_reinterpreted_as_deployment(master_gateway):
    gateway, calls, _ = master_gateway
    ctx = context(deployment=None, pod={"namespace": "demo", "name": "exporter-pod"})
    with pytest.raises(AIError):
        await gateway.invoke(ctx, "cron_deployment_restart", {"cron": "0 2 * * *"}, mcp=True)
    assert gateway.store.actions == {}
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("cluster_field", ["env", "k8s"])
async def test_schedule_cannot_override_selected_cluster(master_gateway, cluster_field):
    gateway, calls, _ = master_gateway
    with pytest.raises(AIError):
        await gateway.invoke(context(), "cron_deployment_restart", {"cron": "0 2 * * *", cluster_field: "cluster-b"}, mcp=True)
    assert gateway.store.actions == {}
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,arguments", [("restart_deployment", {}), ("scale_deployment", {"replicas": 3})])
async def test_upstream_error_list_is_not_reported_as_success(master_gateway, operation, arguments):
    gateway, calls, response = master_gateway
    errors = [{"namespace": "demo", "deployment_name": "deploy-demo-order", "reason": "not found"}]
    response.update(message="ok", error_list=errors)
    ctx = context()
    action, _ = await propose(gateway, ctx, operation, arguments)
    result = await gateway.dispatch(ctx, action)
    assert len(calls) == 1
    assert result["success"] is False
    assert result["error"]["code"] == "partial_failure"
    assert result["error_list"] == errors
    assert gateway.store.actions[action["id"]]["state"] == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 500])
async def test_message_only_http_error_keeps_failure_status_in_master_response(master_gateway, status):
    gateway, calls, response = master_gateway
    response.update(_http_status=status, message="upstream rejected request")
    result = await gateway.master(context().identity, "POST", "/api/cron", {"env": "cluster-a"}, {}, allow_failure=True)
    assert len(calls) == 1
    assert result["success"] is False
    assert result["error"]["code"] == "API_ERROR"
    assert result["error"]["status"] == status
    assert "upstream rejected request" in result["error"]["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status,expected_state,expected_code", [(400, "failed", "API_ERROR"), (500, "unknown", "outcome_unknown")])
async def test_message_only_http_write_error_is_not_success_or_replayed(master_gateway, status, expected_state, expected_code):
    gateway, calls, response = master_gateway
    response.update(_http_status=status, message="upstream request failed")
    ctx = context()
    arguments = {"cron": "0 2 * * *"}
    action, _ = await propose(gateway, ctx, "cron_deployment_restart", arguments)
    result = await gateway.dispatch(ctx, action)
    assert result["success"] is False
    assert result["error"]["code"] == expected_code
    assert gateway.store.actions[action["id"]]["state"] == expected_state
    original_error = result["error"].get("upstream_error", result["error"])
    assert original_error["status"] == status
    await gateway.dispatch(ctx, action)
    assert len(calls) == 1
    if expected_state == "unknown":
        with pytest.raises(AIError) as denied:
            await gateway.invoke(ctx, "cron_deployment_restart", arguments, call_id="different-tool-call", mcp=True)
        assert denied.value.code == "outcome_unknown"
        assert len(calls) == 1


class RecordingModel(ScriptedModel):
    histories: list[list] = Field(default_factory=list)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.histories.append(copy.deepcopy(messages))
        return super()._generate(messages, stop, run_manager, **kwargs)


@pytest.mark.asyncio
async def test_real_graph_loads_current_catalog_and_skill_before_scheduled_operation(master_gateway):
    gateway, calls, _ = master_gateway
    read_skill = AIMessage(content="", tool_calls=[{"name": "read_file", "args": {"file_path": "/skills/kubedoor-k8s/SKILL.md"}, "id": "read-skill", "type": "tool_call"}])
    scheduled = AIMessage(content="", tool_calls=[{"name": "kubedoor_tool", "args": {"operation": "cron_deployment_restart", "source": "auto", "arguments": {"cron": "0 2 * * *"}}, "id": "schedule", "type": "tool_call"}])
    model = RecordingModel(responses=[read_skill, scheduled, AIMessage(content="周期重启任务已经创建。")])
    runtime = Runtime(gateway.store, gateway, InMemorySaver(), model_factory=lambda _: model)
    ctx = context()
    ctx.run["skill_ids"] = ["kubedoor-k8s"]
    provider = Provider("http://unused-model/v1", "unused-private-key", "mock-only")

    await runtime.run_graph(ctx, provider, "帮我每天凌晨2点重启 demo 的 deploy-demo-order", None)
    assert runtime.store.statuses[-1] == "waiting_approval"
    assert calls == []
    assert len(runtime.store.actions) == 1
    action = next(iter(runtime.store.actions.values()))
    assert action["operation"] == "cron_deployment_restart"
    assert action["source"] == "kubedoor"

    system = "\n".join(str(message.content) for message in model.histories[0] if isinstance(message, SystemMessage))
    names = {entry["name"] for entry in tool_catalog()}
    assert {case[0] for case in SCHEDULES} <= names
    for name in names:
        assert name in system, f"The current tool catalog entry {name} must reach the model."
    skill_result = next(message.content for message in model.histories[1] if isinstance(message, ToolMessage) and message.tool_call_id == "read-skill")
    skill_lines = (SKILLS_ROOT / "kubedoor-k8s" / "SKILL.md").read_text(encoding="utf-8").splitlines()
    assert "kubedoor_tool" in str(skill_result)
    for line in skill_lines:
        if "cron_deployment_restart" in line:
            assert line in str(skill_result)

    await runtime.run_graph(ctx, provider, None, {action["interrupt_id"]: {"action_id": action["id"], "decision": "approve"}})
    assert runtime.store.statuses[-1] == "completed"
    assert len(calls) == 1
    assert calls[0].url.path == "/api/cron"
    assert decoded_body(calls[0])["service"]["deployment_list"] == [{"namespace": "demo", "deployment_name": "deploy-demo-order"}]
    assert decoded_body(calls[0])["cron"] == "0 2 * * *"
