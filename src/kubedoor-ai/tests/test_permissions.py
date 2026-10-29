"""权限与审批：网关按「权限与审批」的操作权限表判定 AI 工具调用（kubedoor_ai/permissions.py）。

权限先于审批：没有权限直接 403、不建动作；有权限时按审批列决定 pending / ready，高危命令固定审批。
网页 AI、企微 AI、外部 MCP 用同一套判定。策略默认是 conftest 里与 master 出厂配置一致的那份。
"""
import json
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
import yaml

from fakes import FakeStore, default_policy, stub_control
from kubedoor_ai import permissions
from kubedoor_ai.catalogue import EXTRA_CATALOG
from kubedoor_ai.domain import AIError, CredentialCipher, Identity
from kubedoor_ai.gateway import CATALOG, Gateway, PendingAction, RunContext
from kubedoor_ai.service import Service

# conftest 的 autouse fixture 会换掉它；先留住向 master 取策略的真实实现
FETCH_POLICY = Gateway.permission_policy

ENV = "cluster-a"
CIPHER = CredentialCipher("ab" * 32)
POD = {"namespace": "demo", "pod": "web-1"}
COREDNS = yaml.safe_dump({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "coredns", "namespace": "kube-system"},
                          "data": {"Corefile": ".:53 {\n    forward . /etc/resolv.conf\n}\n"}})
CONFIGMAP = yaml.safe_dump({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "app-config", "namespace": "demo"},
                            "data": {"mode": "fast"}})
VIRTUAL_SERVICE = yaml.safe_dump({"apiVersion": "networking.istio.io/v1", "kind": "VirtualService",
                                  "metadata": {"name": "web", "namespace": "demo"}, "spec": {"hosts": ["web"]}})


def identity(permission="rw", clusters=None):
    return Identity("operator", permission, MappingProxyType(clusters or {}))


def context(permission="rw", clusters=None, origin="chat"):
    return RunContext({"id": "run-1", "session_id": "session-1", "origin": origin, "skill_ids": [],
                       "scope": {"env": ENV, "namespace": "demo", "deployment": None, "pod": None}},
                      identity(permission, clusters))


class AgentMaster:
    """假 master + agent：记录 agent-execute 请求；准备阶段回预览，执行阶段回成功"""

    def __init__(self):
        self.calls = []

    def handle(self, request):
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.url.path, body))
        if request.url.path == "/api/ai/internal/agent-execute":
            if body["phase"] == "prepare":
                return httpx.Response(200, json={"success": True, "arguments": body["arguments"],
                                                 "preview": {"command": ["kubectl", *body["arguments"].get("argv", [])],
                                                             "diff": "--- live\n+++ proposed"}})
            return httpx.Response(200, json={"success": True, "data": "ok"})
        return httpx.Response(200, json={"success": True, "message": "ok"})

    def executed(self, phase="execute"):
        return [body for path, body in self.calls if path == "/api/ai/internal/agent-execute" and body["phase"] == phase]


def make_gateway():
    master = AgentMaster()
    gateway = Gateway(FakeStore(), CIPHER, "http://master", "internal",
                      http=httpx.AsyncClient(transport=httpx.MockTransport(master.handle)))
    return stub_control(gateway), master


async def outcome(gateway, ctx, operation, arguments, source="agent"):
    """一次调用的结果，便于比较：被拒、等待批准、已执行"""
    call_id = f"call-{len(gateway.store.actions)}-{operation}"
    try:
        await gateway.invoke(ctx, operation, arguments, source, call_id=call_id, mcp=True)
    except PendingAction as pending:
        action = gateway.store.actions[pending.action["action_id"]]
        return ("pending", action["ops"], action["approval_fixed"])
    except AIError as exc:
        return ("denied", exc.code, exc.status)
    action = next(item for item in gateway.store.actions.values() if item["call_id"] == call_id)
    assert action["state"] == "succeeded"
    return ("done", action["ops"], action["read_only"])


# ---- 操作类型 ----

def test_every_catalogue_mutation_has_operation_types():
    from kubedoor_tools.policy import OP_KEYS
    assert set(EXTRA_CATALOG) <= set(CATALOG)
    mutations = {name for name, item in CATALOG.items() if not item[3]}
    # 修改一个不漏，查询类工具不在表里
    assert mutations == set(permissions.TOOL_OPS)
    assert set(permissions.OP_NAMES) == set(OP_KEYS)
    for name, ops in permissions.TOOL_OPS.items():
        assert ops and set(ops) <= set(OP_KEYS), name
    for name, item in CATALOG.items():
        if item[3]:
            assert permissions.catalog_ops(name, {"resource_type": "configmap"}) == ([], True), name
    # 读 Secret 内容例外：仍是查询，但要查表
    assert permissions.catalog_ops("resource_content", {"resource_type": "Secret"}) == (["resource.secret"], True)


def test_kubectl_delete_needs_resource_delete_and_pods_need_pod_delete():
    policy = permissions.normalize_policy(default_policy())
    delete = permissions.evaluate(identity(), ENV, "kubectl", {"argv": ["delete", "configmap", "app-config", "-n", "demo"]}, policy)
    assert delete["ops"] == ["ai.k8s_write", "resource.delete"] and not delete["denied"] and delete["approval"]
    pod = permissions.evaluate(identity(), ENV, "kubectl", {"argv": ["delete", "pod", "web-1", "-n", "demo"]}, policy)
    assert "pod.delete" in pod["ops"] and "resource.delete" not in pod["ops"]
    query = permissions.evaluate(identity("read"), ENV, "kubectl", {"argv": ["get", "pods", "-n", "demo"]}, policy)
    assert query == {"ops": [], "read_only": True, "approval": False, "approval_fixed": False, "denied": False, "message": None}
    # 管理员把「删除资源」只给管理员：读写账号的 kubectl delete 被拒，即使有「AI 通用修改」
    policy["ops"]["resource.delete"]["rw"] = False
    denied = permissions.evaluate(identity(), ENV, "kubectl", {"argv": ["delete", "configmap", "app-config", "-n", "demo"]}, policy)
    assert denied["denied"] and "「读写」" in denied["message"] and "「删除资源」" in denied["message"]
    assert not permissions.evaluate(identity("read", {ENV: "admin"}), ENV, "kubectl",
                                     {"argv": ["delete", "configmap", "app-config", "-n", "demo"]}, policy)["denied"]


def test_coredns_and_istio_targets_are_identified():
    policy = permissions.normalize_policy(default_policy())

    def ops(operation, arguments):
        return permissions.evaluate(identity("admin"), ENV, operation, arguments, policy)["ops"]

    assert ops("resource_apply", {"yaml_content": CONFIGMAP}) == ["resource.apply"]
    assert ops("resource_apply", {"yaml_content": COREDNS}) == ["resource.apply", "network.coredns"]
    assert ops("resource_delete", {"namespace": "kube-system", "resource_type": "ConfigMap", "resource_name": "coredns"}) == ["resource.delete", "network.coredns"]
    assert ops("resource_delete", {"namespace": "demo", "resource_type": "configmap", "resource_name": "coredns"}) == ["resource.delete"]
    assert "network.istio" in ops("kubectl", {"argv": ["apply", "-f", "-"], "stdin": VIRTUAL_SERVICE})
    assert "network.istio" in ops("kubectl", {"argv": ["delete", "vs", "web", "-n", "demo"]})
    assert "network.istio" in ops("api", {"method": "PATCH", "path": "/apis/networking.istio.io/v1/namespaces/demo/virtualservices/web", "body": {}})
    assert set(ops("istioctl", {"argv": ["install", "-y"]})) == {"ai.k8s_write", "network.istio"}
    # 判断不了（YAML 解析失败）按涉及处理
    assert {"network.coredns", "network.istio"} <= set(ops("resource_apply", {"yaml_content": "kind: [unclosed"}))


# ---- 网关：权限先于审批 ----

@pytest.mark.asyncio
async def test_no_permission_is_refused_before_any_action_or_preparation():
    gateway, master = make_gateway()
    gateway.execute_action = AsyncMock()
    try:
        with pytest.raises(AIError) as denied:
            await gateway.invoke(context("read"), "delete_pod", POD, mcp=True)
        assert (denied.value.status, denied.value.code) == (403, "permission_denied")
        assert str(denied.value) == f"当前账号在集群 {ENV} 是「只读」权限，不能执行「删除 Pod」"
        # CoreDNS 只给集群管理员：读写账号连准备都不做
        with pytest.raises(AIError) as coredns:
            await gateway.invoke(context(), "kubectl", {"argv": ["apply", "-f", "-"], "stdin": COREDNS}, "agent", mcp=True)
        assert "「CoreDNS 修改」" in str(coredns.value)
        assert gateway.store.actions == {} and master.calls == []
        gateway.execute_action.assert_not_awaited()
        # 单独授权的集群管理员可以
        with pytest.raises(PendingAction) as pending:
            await gateway.invoke(context("read", {ENV: "admin"}), "kubectl", {"argv": ["apply", "-f", "-"], "stdin": COREDNS}, "agent", mcp=True)
        assert set(pending.value.action["ops"]) == {"ai.k8s_write", "resource.apply", "network.coredns"}
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_admin_column_off_disables_an_operation_for_everyone(permission_policy):
    permission_policy.policy["ops"]["pod.delete"].update(read=False, rw=False, admin=False)
    gateway, _ = make_gateway()
    try:
        with pytest.raises(AIError) as denied:
            await gateway.invoke(context("admin"), "delete_pod", POD, mcp=True)
        assert "已被管理员在「权限与审批」里关闭" in str(denied.value)
        assert gateway.store.actions == {}
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_approval_column_decides_pending_or_direct_execution(permission_policy):
    gateway, _ = make_gateway()
    gateway.execute_action = AsyncMock(return_value={"success": True, "data": "deleted"})
    try:
        assert await outcome(gateway, context(), "delete_pod", POD, source="auto") == ("pending", ["pod.delete"], False)
        gateway.execute_action.assert_not_awaited()
        # 审批列取消：有权限就直接执行，照样记下动作和 ops
        permission_policy.policy["ops"]["pod.delete"]["approval"] = False
        assert await outcome(gateway, context(), "delete_pod", POD, source="auto") == ("done", ["pod.delete"], False)
        gateway.execute_action.assert_awaited_once()
        done = [item for item in gateway.store.actions.values() if item["state"] == "succeeded"]
        assert len(done) == 1 and done[0]["preview"]["path"] == "/api/pod/delete_pod"
        # 只读放开后只读也能执行；审批列照常生效
        permission_policy.policy["ops"]["pod.delete"].update(read=True, approval=True)
        assert (await outcome(gateway, context("read"), "delete_pod", POD, source="auto"))[0] == "pending"
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_dangerous_pod_commands_always_need_approval(permission_policy):
    # 页面上取消高危行的审批也不行
    permission_policy.policy["ops"]["pod.exec.danger"]["approval"] = False
    gateway, master = make_gateway()
    try:
        assert await outcome(gateway, context(), "pod_exec", {**POD, "argv": ["rm", "-rf", "/tmp/cache"]}) == ("pending", ["pod.exec.danger"], True)
        pending = next(iter(gateway.store.actions.values()))
        assert gateway.action_preview(pending, context().run["scope"])["approval_fixed"] is True
        assert await outcome(gateway, context(), "pod_exec", {**POD, "argv": ["sh", "-c", "ps aux | grep java"]}) == ("done", ["pod.exec.view"], True)
        assert await outcome(gateway, context(), "pod_exec", {**POD, "argv": ["touch", "/tmp/x"]}) == ("done", ["pod.exec.other"], False)
        # 查看命令出厂只给读写及以上
        assert await outcome(gateway, context("read"), "pod_exec", {**POD, "argv": ["ls", "/"]}) == ("denied", "permission_denied", 403)
        assert [body["argv"] for body in (call["arguments"] for call in master.executed())] == [["sh", "-c", "ps aux | grep java"], ["touch", "/tmp/x"]]
    finally:
        await gateway.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("permission,argv", [
    ("read", ["ls", "/"]), ("rw", ["cat", "/etc/hosts"]), ("rw", ["touch", "/tmp/x"]), ("rw", ["kill", "1"]), ("admin", ["date"]),
])
async def test_web_and_wecom_get_the_same_decision(permission_policy, permission, argv):
    results = []
    for origin in ("chat", "wecom"):
        gateway, _ = make_gateway()
        try:
            results.append(await outcome(gateway, context(permission, origin=origin), "pod_exec", {**POD, "argv": argv}))
        finally:
            await gateway.close()
    assert results[0] == results[1]
    # 管理员按表放开只读的查看命令后，两边同样放行
    permission_policy.policy["ops"]["pod.exec.view"]["read"] = True
    if permission == "read":
        for origin in ("chat", "wecom"):
            gateway, _ = make_gateway()
            try:
                assert (await outcome(gateway, context("read", origin=origin), "pod_exec", {**POD, "argv": argv}))[0] == "done"
            finally:
                await gateway.close()


@pytest.mark.asyncio
async def test_external_mcp_follows_the_same_table(permission_policy):
    """外部 MCP（/mcp 的 mcp_call）不再特殊处理：没权限拒绝，不用批准的直接执行，要批准的返回待批准"""
    gateway, _ = make_gateway()
    gateway.execute_action = AsyncMock(return_value={"success": True})
    try:
        with pytest.raises(AIError) as denied:
            await gateway.invoke(context("read", origin="mcp"), "delete_pod", POD, mcp=True)
        assert denied.value.code == "permission_denied"
        with pytest.raises(PendingAction):
            await gateway.invoke(context(origin="mcp"), "delete_pod", POD, call_id="needs-approval", mcp=True)
        permission_policy.policy["ops"]["pod.delete"]["approval"] = False
        result = await gateway.invoke(context(origin="mcp"), "delete_pod", POD, call_id="direct", mcp=True)
        assert result["success"] is True
        gateway.execute_action.assert_awaited_once()
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_secret_content_is_checked_in_the_table_but_never_approved(permission_policy):
    gateway, _ = make_gateway()
    gateway.execute_action = AsyncMock(return_value={"success": True, "data": "kind: Secret"})
    secret = {"namespace": "demo", "resource_type": "secret", "resource_name": "db"}
    try:
        assert await outcome(gateway, context("read"), "resource_content", secret, source="auto") == ("denied", "permission_denied", 403)
        # 「—」行：配置里的审批值不生效
        permission_policy.policy["ops"]["resource.secret"]["approval"] = True
        assert await outcome(gateway, context(), "resource_content", secret, source="auto") == ("done", ["resource.secret"], True)
        # 其他资源的内容是普通查询，不查表
        calls = permission_policy.calls
        assert (await outcome(gateway, context("read"), "resource_content", {**secret, "resource_type": "configmap"}, source="auto"))[0] == "done"
        assert permission_policy.calls == calls
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_unavailable_policy_refuses_mutations_but_not_queries(permission_policy):
    permission_policy.policy = None
    gateway, master = make_gateway()
    try:
        denied = await outcome(gateway, context(), "kubectl", {"argv": ["rollout", "restart", "deployment/web", "-n", "demo"]})
        assert denied == ("denied", "permission_denied", 403)
        # Pod 内命令要查表（查看命令也是），取不到策略同样不执行
        assert (await outcome(gateway, context(), "pod_exec", {**POD, "argv": ["ls"]}))[0] == "denied"
        assert await outcome(gateway, context(), "kubectl", {"argv": ["get", "pods", "-n", "demo"]}) == ("done", [], True)
        assert [body["arguments"]["argv"] for body in master.executed()] == [["get", "pods", "-n", "demo"]]
        with pytest.raises(AIError, match="暂时无法从 KubeDoor 获取"):
            await gateway.invoke(context(), "delete_pod", POD, mcp=True)
    finally:
        await gateway.close()


# ---- 下发给 master / agent 的 approved 和 ops ----

@pytest.mark.asyncio
async def test_agent_execute_carries_approved_and_ops():
    gateway, master = make_gateway()
    try:
        # 查看命令：不准备，执行时 approved=true 并带 ops
        await outcome(gateway, context(), "pod_exec", {**POD, "argv": ["ls", "/"]})
        view = master.executed()[-1]
        assert (view["approved"], view["ops"]) == (True, ["pod.exec.view"])
        # 纯查询：approved=false，不带 ops
        await outcome(gateway, context(), "kubectl", {"argv": ["get", "pods", "-n", "demo"]})
        query = master.executed()[-1]
        assert query["approved"] is False and "ops" not in query
        # 修改：准备和执行都带 ops，批准后 approved=true
        argv = {"argv": ["apply", "-f", "-"], "stdin": CONFIGMAP}
        with pytest.raises(PendingAction) as pending:
            await gateway.invoke(context(), "kubectl", argv, "agent", call_id="apply", mcp=True)
        assert master.executed("prepare")[-1]["ops"] == ["ai.k8s_write", "resource.apply"]
        action = gateway.store.actions[pending.value.action["action_id"]]
        await gateway.dispatch(context(), action)
        applied = master.executed()[-1]
        assert (applied["approved"], applied["ops"]) == (True, ["ai.k8s_write", "resource.apply"])
        # 升级前建的动作没有 ops：不带这个键，master 按原来的读写权限处理
        legacy = {**action, "id": "legacy", "call_id": "legacy", "ops": None, "state": "pending"}
        gateway.store.actions["legacy"] = dict(legacy)
        await gateway.dispatch(context(), legacy)
        old = master.executed()[-1]
        assert old["approved"] is True and "ops" not in old
        with pytest.raises(AIError):   # 旧动作仍要读写权限
            await gateway.recheck(identity("read"), ENV, legacy)
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_planned_direct_deployment_edits_carry_the_action_ops():
    """不受管控时网关自己 PATCH Deployment：dry-run 和执行都带所属动作的 ops"""
    from fakes import control_state, deployment
    calls = []

    def handle(request):
        body = json.loads(request.content) if request.content else None
        if request.url.path == "/api/ai/internal/deployment-control":
            return httpx.Response(200, json={"success": True, "data": control_state("namespace_off")})
        assert request.url.path == "/api/ai/internal/agent-execute"
        calls.append(body)
        if body["arguments"]["method"] == "GET":
            return httpx.Response(200, json={"success": True, "data": deployment()})
        if body["phase"] == "prepare":
            return httpx.Response(200, json={"success": True, "arguments": body["arguments"], "preview": {"diff": "--- a\n+++ b"}})
        return httpx.Response(200, json={"success": True, "data": deployment(resources={"limits": {"memory": "2Gi"}})})

    gateway = Gateway(FakeStore(), CIPHER, "http://master", "internal", http=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    try:
        with pytest.raises(PendingAction) as pending:
            await gateway.invoke(context(), "update_deployment_resources", {"namespace": "demo", "deployment": "app", "limit_mem_mb": 2048}, mcp=True)
        await gateway.dispatch(context(), gateway.store.actions[pending.value.action["action_id"]])
        writes = [body for body in calls if body["arguments"]["method"] != "GET"]
        assert [(body["phase"], body["approved"], body["ops"]) for body in writes] == [("prepare", False, ["control.edit"]), ("execute", True, ["control.edit"])]
        assert all("ops" not in body for body in calls if body["arguments"]["method"] == "GET")
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_tool_start_shows_what_a_mutation_changes():
    gateway, _ = make_gateway()
    try:
        # 不需要批准的修改没有审批卡片：开始事件里带上命令和差异
        await outcome(gateway, context(), "pod_exec", {**POD, "argv": ["touch", "/tmp/x"]})
        await outcome(gateway, context(), "kubectl", {"argv": ["get", "pods", "-n", "demo"]})
        starts = [event["data"] for event in gateway.store.events if event["type"] == "tool_start"]
        assert starts[0]["command"] == ["kubectl", "touch", "/tmp/x"] and starts[0]["diff"].startswith("---")
        assert starts[0]["preview"]["command"] == starts[0]["command"]
        assert "preview" not in starts[1] and "command" not in starts[1]
        # 结果事件不重复带预览
        results = [event["data"] for event in gateway.store.events if event["type"] == "tool_result"]
        assert all("preview" not in item for item in results)
    finally:
        await gateway.close()


# ---- 批准和执行前复核 ----

@pytest.mark.asyncio
async def test_dispatch_rechecks_permission_changed_during_approval(permission_policy):
    gateway, _ = make_gateway()
    gateway.execute_action = AsyncMock(return_value={"success": True})
    try:
        with pytest.raises(PendingAction) as pending:
            await gateway.invoke(context(), "delete_pod", POD, call_id="later", mcp=True)
        action = gateway.store.actions[pending.value.action["action_id"]]
        permission_policy.policy["ops"]["pod.delete"]["rw"] = False
        result = await gateway.dispatch(context(), action)
        assert result["error"]["code"] == "permission_denied"
        assert gateway.store.actions[action["id"]]["state"] == "failed"
        gateway.execute_action.assert_not_awaited()
        # 被降权（集群单独授权改成只读）同样拦下
        permission_policy.policy["ops"]["pod.delete"]["rw"] = True
        with pytest.raises(PendingAction) as again:
            await gateway.invoke(context(), "delete_pod", POD, call_id="again", mcp=True)
        demoted = await gateway.dispatch(context("rw", {ENV: "read"}), gateway.store.actions[again.value.action["action_id"]])
        assert "「只读」" in demoted["error"]["message"]
        gateway.execute_action.assert_not_awaited()
    finally:
        await gateway.close()


class DecisionStore(FakeStore):
    def __init__(self, run):
        super().__init__()
        self.run_row = run

    async def run(self, rid, identity):
        if rid != self.run_row["id"] or identity.username != self.run_row["username"]:
            raise AIError("任务不存在", 404, "not_found")
        return dict(self.run_row)


@pytest.mark.asyncio
async def test_decision_rechecks_ops_with_the_current_identity(permission_policy):
    store = DecisionStore({"id": "run-1", "session_id": "session-1", "username": "operator", "status": "waiting_approval",
                           "cancel_requested": False, "origin": "wecom", "scope": {"env": ENV, "namespace": "demo"}})
    store.actions["a-1"] = {"id": "a-1", "run_id": "run-1", "call_id": "run-1:c", "operation": "delete_pod", "source": "kubedoor",
                            "arguments": POD, "preview": {}, "read_only": False, "connection_revision": None, "state": "pending",
                            "interrupt_id": "interrupt-1", "result": None, "expires_at": "2099-01-01T00:00:00+00:00",
                            "ops": ["pod.delete"], "approval_fixed": False}
    gateway = Gateway(store, CIPHER, "http://master", "internal")
    runtime = SimpleNamespace(tasks={}, contexts={}, start=Mock())
    service = Service(store, gateway, runtime)
    service.ensure_env = AsyncMock(return_value={"env": ENV, "online": True, "ai_tools": True})
    body = {"action_id": "a-1", "decision": "approve", "provider": {"base_url": "http://model/v1", "api_key": "k", "model": "m"}}
    try:
        # 审批期间被降成只读：不能批准，但可以拒绝
        with pytest.raises(AIError) as denied:
            await service.decision(identity("read"), "run-1", body)
        assert denied.value.status == 403 and "「删除 Pod」" in str(denied.value)
        runtime.start.assert_not_called()
        assert (await service.decision(identity("read"), "run-1", {**body, "decision": "reject"}))["status"] == "running"
        assert runtime.start.call_args.kwargs["resume"] == {"interrupt-1": {"decision": "reject", "action_id": "a-1"}}
        # 权限表改了同样拦下；恢复后能批准
        runtime.start.reset_mock()
        permission_policy.policy["ops"]["pod.delete"]["rw"] = False
        with pytest.raises(AIError):
            await service.decision(identity(), "run-1", body)
        permission_policy.policy["ops"]["pod.delete"]["rw"] = True
        assert (await service.decision(identity(), "run-1", body))["status"] == "running"
        assert runtime.start.call_args.kwargs["resume"] == {"interrupt-1": {"decision": "approve", "action_id": "a-1"}}
    finally:
        await gateway.close()


# ---- 策略获取 ----

def test_policy_normalization_forces_fixed_approvals_and_rejects_bad_shapes():
    raw = default_policy()
    raw["version"] = 1730000000000
    raw["ops"]["pod.exec.danger"]["approval"] = False
    raw["ops"]["resource.secret"]["approval"] = True
    policy = permissions.normalize_policy(raw)
    assert policy["version"] == "1730000000000"
    assert policy["ops"]["pod.exec.danger"]["approval"] is True and policy["ops"]["resource.secret"]["approval"] is False
    assert policy["commands"]["danger"] == raw["commands"]["danger"]
    for bad in (None, [], {"ops": {}}, {"ops": [], "commands": {"view": [], "danger": []}},
                {"ops": {"pod.delete": "rw"}, "commands": {"view": [], "danger": []}},
                {"ops": {}, "commands": {"view": "ls", "danger": []}}):
        assert permissions.normalize_policy(bad) is None


@pytest.mark.asyncio
async def test_policy_cache_reuses_for_five_seconds_and_never_caches_failures():
    now = [100.0]
    cache = permissions.PolicyCache(clock=lambda: now[0])
    fetch = AsyncMock(return_value=default_policy())
    assert (await cache.get(fetch))["ops"]["pod.delete"]["rw"] is True
    now[0] += 4.9
    await cache.get(fetch)
    assert fetch.await_count == 1
    now[0] += 0.2
    fetch.side_effect = RuntimeError("master down")
    assert await cache.get(fetch) is None
    fetch.side_effect, fetch.return_value = None, {"unexpected": True}
    assert await cache.get(fetch) is None
    fetch.return_value = default_policy()
    assert await cache.get(fetch) is not None and fetch.await_count == 4


@pytest.mark.asyncio
async def test_gateway_fetches_the_policy_from_master_with_the_internal_token():
    seen = []

    def handle(request):
        seen.append(request)
        if len(seen) > 1:
            return httpx.Response(500, json={"error": "down"})
        return httpx.Response(200, json=default_policy())

    gateway = Gateway(FakeStore(), CIPHER, "http://master", "internal-token", http=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    try:
        policy = await FETCH_POLICY(gateway, identity())
        assert policy["ops"]["network.istio"] == {"read": False, "rw": False, "admin": True, "approval": True}
        assert seen[0].url.path == "/api/ai/internal/permission-policy"
        assert seen[0].headers["X-Kubedoor-Token"] == "internal-token" and seen[0].headers["X-User-Name"] == "operator"
        assert await FETCH_POLICY(gateway, identity()) == policy and len(seen) == 1   # 5 秒内复用
        gateway.policy_cache.invalidate()
        assert await FETCH_POLICY(gateway, identity()) is None   # master 出错：按取不到处理
    finally:
        await gateway.close()


# ---- 角色 ----

@pytest.mark.asyncio
async def test_bootstrap_reports_admin_cluster_permission():
    store = FakeStore()
    store.summaries = AsyncMock(return_value=[])
    gateway = Gateway(store, CIPHER, "http://master", "internal")
    gateway.master = AsyncMock(return_value={"clusters": [{"env": "prod", "online": True}, {"env": "test", "online": True}, {"env": "dev", "online": True}]})
    service = Service(store, gateway, None)
    try:
        lead = Identity("lead", "cluster_admin", MappingProxyType({"test": "read", "dev": "none"}))
        data = await service.bootstrap(lead)
        assert (data["permission"], data["is_admin"]) == ("cluster_admin", False)
        assert [(row["env"], row["permission"]) for row in data["clusters"]] == [("prod", "admin"), ("test", "read")]
        root = await service.bootstrap(Identity("root", "admin"))
        assert {row["permission"] for row in root["clusters"]} == {"admin"} and root["is_admin"] is True
    finally:
        await gateway.close()
