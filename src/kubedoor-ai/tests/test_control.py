"""花折资源管控（准入控制）感知：执行计划、直接修改拦截、执行后回读（control.py + gateway）。"""
import copy
import json

import httpx
import pytest
import yaml

from fakes import FakeStore, control_state, deployment, stub_control
from kubedoor_ai.control import (build_plan, changed_fields, control_drift, control_summary, deployment_edits, main_step,
                                 snapshot, verify)
from kubedoor_ai.domain import AIError, CredentialCipher, Identity
from kubedoor_ai.gateway import CATALOG, Gateway, PendingAction, RunContext

ENV = "cluster-a"
ARGS = {"namespace": "demo", "deployment": "app"}
JAVA = ["java", "-Xms512m", "-Xmx1024m", "-jar", "app.jar"]


def scale_step(**query):
    return main_step("scale_deployment", "POST", "/api/scale", {"env": ENV, **query},
                     {"deployment_list": [{"namespace": "demo", "deployment_name": "app", "num": 5}], "node_scheduler": []})


def plan(operation, args, state="controlled", live=None, step=None, **table):
    return build_plan(operation, {**ARGS, **args}, ENV, control_state(state, **table),
                      snapshot(live) if live else None, step)


# ---- 执行计划 ----

def test_persistent_scale_of_a_controlled_deployment_writes_the_control_table_first():
    result = plan("scale_deployment", {"replicas": 5}, live=deployment(replicas=2), step=scale_step())
    assert [(s["name"], s["path"]) for s in result["steps"]] == [("写入管控副本数", "/api/db/res/pod_count"), ("扩缩容", "/api/scale")]
    assert result["steps"][0]["body"] == {"env": ENV, "namespace": "demo", "deployment_name": "app", "pod_count_manual": 5}
    assert "temp" not in result["steps"][1]["query"] and result["expect"] == {"replicas": 5}
    assert {(c["scope"], c["field"], c["current"], c["target"]) for c in result["changes"]} == {
        ("在线", "replicas", "2", "5"), ("管控表", "pod_count_manual", "-1（按 AI 推荐 / 当日 Pod）", "5")}


def test_temporary_scale_keeps_the_control_table_and_says_when_it_reverts():
    result = plan("scale_deployment", {"replicas": 5, "mode": "temporary"}, live=deployment(replicas=2), step=scale_step(), pod_count_ai=3)
    assert [s["path"] for s in result["steps"]] == ["/api/scale"] and result["steps"][0]["query"]["temp"] == "true"
    assert any("恢复为管控值 3" in effect for effect in result["effects"])


def test_scale_of_an_uncontrolled_deployment_changes_k8s_only():
    result = plan("scale_deployment", {"replicas": 5, "mode": "temporary"}, state="namespace_off", step=scale_step())
    assert [s["path"] for s in result["steps"]] == ["/api/scale"] and "temp" not in result["steps"][0]["query"]
    assert result["changes"][0]["current"] == "读取不到" and result["warnings"]


def test_blocked_and_scheduled_persistent_scale_are_rejected_with_alternatives():
    with pytest.raises(AIError) as blocked:
        plan("scale_deployment", {"replicas": 5}, state="unregistered_blocked", step=scale_step())
    assert blocked.value.code == "control_blocked" and "resource_control_add" in str(blocked.value)
    cron = main_step("cron_deployment_scale", "POST", "/api/cron", {"env": ENV}, {})
    scheduled = plan("cron_deployment_scale", {"replicas": 5, "cron": "0 8 * * *"}, step=cron)
    assert scheduled["steps"][0]["query"]["temp"] == "true" and not scheduled["expect"]
    with pytest.raises(AIError) as persistent:
        plan("cron_deployment_scale", {"replicas": 5, "cron": "0 8 * * *", "mode": "persistent"},
             step=main_step("cron_deployment_scale", "POST", "/api/cron", {"env": ENV}, {}))
    assert persistent.value.code == "control_mode"
    # 不受管控时同样带 temp：到点前如果纳入了管控，也不会被改回
    assert plan("cron_deployment_scale", {"replicas": 5, "cron": "0 8 * * *"}, state="cluster_off",
                step=main_step("cron_deployment_scale", "POST", "/api/cron", {"env": ENV}, {}))["steps"][0]["query"]["temp"] == "true"


def test_restart_and_image_update_preview_the_values_admission_will_restore():
    live = deployment(replicas=5, args=JAVA)
    restart = plan("restart_deployment", {}, live=live, step=main_step("restart_deployment", "POST", "/api/restart", {"env": ENV}, {}),
                   jvm_xmx_bytes=2048 * 1024 ** 2, jvm_xss_bytes=1024 ** 2)
    changed = {c["field"]: (c["current"], c["target"]) for c in restart["changes"]}
    assert changed["replicas"] == ("5", "2") and changed["jvm_xmx_bytes"] == ("1024m", "2048m")
    # -Xss 不在 args 里：webhook 改不了，不能当成会变的项，也不作为回读期望
    assert "jvm_xss_bytes" not in changed and "jvm_xss_bytes" not in restart["expect"]
    image = plan("update_deployment_image", {"image_tag": "v2"}, live=live,
                 step=main_step("update_deployment_image", "POST", "/api/update-image", {"env": ENV}, {}))
    assert image["expect"]["image"] == "registry.example.com:5000/team/app:v2" and image["expect"]["replicas"] == 2


def test_controlled_resources_change_limits_through_the_table_and_restart():
    live = deployment()
    with pytest.raises(AIError) as requests:
        plan("update_deployment_resources", {"request_cpu_m": 200}, live=live)
    assert requests.value.code == "control_requests"
    result = plan("update_deployment_resources", {"limit_mem_mb": 2048}, live=live, pod_count_manual=4)
    assert [s["path"] for s in result["steps"]] == ["/api/db/res/edit", "/api/restart"]
    assert result["steps"][0]["body"] == {"env": ENV, "namespace": "demo", "deployment": "app",
                                          "limit_cpu_m": 1000, "limit_mem_mb": 2048, "pod_count_manual": 4}
    assert result["expect"]["limit_mem_mb"] == 2048 and result["expect"]["replicas"] == 4
    saved_only = plan("update_deployment_resources", {"limit_mem_mb": 2048, "apply_now": False}, live=live)
    assert [s["path"] for s in saved_only["steps"]] == ["/api/db/res/edit"] and not saved_only["expect"]


def test_uncontrolled_resources_patch_the_deployment_directly():
    live = deployment()
    result = plan("update_deployment_resources", {"limit_cpu_m": 2000, "request_mem_mb": 512}, state="cluster_off", live=live)
    step = result["steps"][0]
    assert step["kind"] == "k8s" and step["arguments"]["path"] == "/apis/apps/v1/namespaces/demo/deployments/app"
    assert step["arguments"]["body"] == [{"op": "add", "path": "/spec/template/spec/containers/0/resources", "value": {
        "requests": {"cpu": "100m", "memory": "512Mi"}, "limits": {"cpu": "2000m", "memory": "1Gi"}}}]
    assert result["expect"] == {"limit_cpu_m": 2000, "request_mem_mb": 512}
    for args, code in (({"limit_cpu_m": 2000, "apply_now": False}, "control_apply_now"), ({"request_mem_mb": 4096}, "invalid_resources")):
        with pytest.raises(AIError) as error:
            plan("update_deployment_resources", args, state="cluster_off", live=live)
        assert error.value.code == code
    with pytest.raises(AIError) as unreadable:
        plan("update_deployment_resources", {"limit_cpu_m": 2000}, state="cluster_off")
    assert unreadable.value.code == "deployment_unreadable"


def test_jvm_changes_only_flags_present_in_java_args():
    with pytest.raises(AIError) as missing:
        plan("update_deployment_jvm", {"jvm_xss_bytes": 1024 ** 2}, live=deployment(args=JAVA))
    assert missing.value.code == "jvm_flag_missing"
    controlled = plan("update_deployment_jvm", {"jvm_xmx_bytes": 2 * 1024 ** 3}, live=deployment(args=JAVA))
    assert controlled["steps"][0]["body"]["jvm_xmx_bytes"] == 2 * 1024 ** 3 and controlled["steps"][1]["path"] == "/api/restart"
    assert controlled["expect"]["jvm_xmx_bytes"] == 2 * 1024 ** 3
    assert plan("update_deployment_jvm", {"jvm_xmx_bytes": 2 * 1024 ** 3})["warnings"]   # 读不到 Deployment 时提醒
    direct = plan("update_deployment_jvm", {"jvm_xmx_bytes": 2 * 1024 ** 3}, state="namespace_off", live=deployment(args=JAVA))
    assert direct["steps"][0]["arguments"]["body"] == [{"op": "replace", "path": "/spec/template/spec/containers/0/args",
                                                        "value": ["java", "-Xms512m", "-Xmx2048m", "-jar", "app.jar"]}]
    with pytest.raises(AIError):
        plan("update_deployment_jvm", {"jvm_xms_bytes": 4 * 1024 ** 3}, state="namespace_off", live=deployment(args=JAVA))


def test_table_operations_explain_whether_the_saved_values_take_effect():
    with pytest.raises(AIError) as unregistered:
        plan("resource_config_update", {"limit_mem_mb": 1, "limit_cpu_m": 1, "pod_count_manual": 1}, state="unregistered")
    assert unregistered.value.code == "control_unregistered"
    uncontrolled = plan("resource_pod_count", {"pod_count_manual": 3}, state="namespace_off")
    assert uncontrolled["warnings"] and not uncontrolled["effects"]
    assert plan("resource_pod_count", {"pod_count_manual": 3})["effects"]
    with pytest.raises(AIError) as registered:
        plan("resource_control_add", {"pod_count_manual": 2})
    assert registered.value.code == "control_registered"
    assert plan("resource_control_add", {"pod_count_manual": 2}, state="unregistered_blocked")["effects"]
    assert plan("resource_control_add", {"pod_count_manual": 2}, state="cluster_off", registered=False)["warnings"]


# ---- 回读核验与漂移 ----

def test_verification_names_values_rewritten_by_admission():
    result = plan("scale_deployment", {"replicas": 5}, live=deployment(), step=scale_step())
    mismatch = verify(result, deployment(replicas=2))
    assert mismatch["matched"] is False and "被准入控制改写成了管控值" in mismatch["message"]
    assert verify(result, deployment(replicas=5))["matched"] is True
    assert verify(result, None)["checked"] is False
    assert verify({"expect": {}}, deployment()) is None


def test_drift_between_approval_and_execution_is_detected():
    planned = control_summary(control_state("controlled"))
    assert control_drift(planned, control_state("controlled")) is None
    assert "管控状态已变化" in control_drift(planned, control_state("namespace_off"))
    assert "管控表" in control_drift(planned, control_state("controlled", limit_mem_mb=4096))


# ---- 通用执行器直接修改的识别 ----

def kubectl(*argv, files=None, stdin=None):
    return deployment_edits("agent", "kubectl", {"argv": list(argv), "files": files or {}, "stdin": stdin})


def fields_of(edits, live=None):
    return [changed_fields(edit, live if live is not None else deployment(args=JAVA)) for edit in edits]


def test_api_edits_are_recognized_by_subresource_and_patch_content():
    path = "/apis/apps/v1/namespaces/demo/deployments/app"
    assert fields_of(deployment_edits("direct", "api", {"method": "PATCH", "path": path + "/scale", "body": {"spec": {"replicas": 4}}})) == [{"replicas"}]
    strategic = {"spec": {"template": {"spec": {"containers": [{"name": "app", "resources": {"limits": {"memory": "2Gi"}}}]}}}}
    assert fields_of(deployment_edits("direct", "api", {"method": "PATCH", "path": path, "body": strategic})) == [{"resources"}]
    sidecar = {"spec": {"template": {"spec": {"containers": [{"name": "sidecar", "resources": {"limits": {"memory": "2Gi"}}}]}}}}
    assert fields_of(deployment_edits("direct", "api", {"method": "PATCH", "path": path, "body": sidecar})) == [set()]
    json_patch = [{"op": "replace", "path": "/spec/replicas", "value": 9}]
    assert fields_of(deployment_edits("direct", "api", {"method": "PATCH", "path": path, "body": json_patch,
                                                        "content_type": "application/json-patch+json"})) == [{"replicas"}]
    env_only = [{"op": "add", "path": "/spec/template/spec/containers/0/env", "value": [{"name": "A", "value": "1"}]}]
    assert fields_of(deployment_edits("direct", "api", {"method": "PATCH", "path": path, "body": env_only,
                                                        "content_type": "application/json-patch+json"})) == [set()]
    # replace 时缺省的副本数回到 1：在线是 2，就是改了副本数
    assert "replicas" in fields_of(deployment_edits("direct", "api", {"method": "PUT", "path": path, "body": {"spec": {"template": {}}}}))[0]
    assert deployment_edits("direct", "api", {"method": "POST", "path": "/apis/apps/v1/namespaces/demo/deployments", "body": {}}) == []


def test_kubectl_edits_cover_scale_set_patch_and_apply():
    assert fields_of(kubectl("scale", "deployment/app", "--replicas=4", "-n", "demo")) == [{"replicas"}]
    assert kubectl("-n", "demo", "scale", "deploy", "app", "--replicas", "4")[0]["namespace"] == "demo"
    bulk = kubectl("scale", "deployment", "-l", "app=x", "--replicas=0", "-n", "demo")
    assert bulk[0]["name"] is None and bulk[0]["fields"] == {"replicas"}
    assert fields_of(kubectl("set", "resources", "deployment", "app", "-c", "sidecar", "--limits=cpu=1")) == [set()]
    assert fields_of(kubectl("set", "resources", "deployment", "app", "--limits=cpu=1")) == [{"resources"}]
    patch = json.dumps({"spec": {"template": {"spec": {"containers": [{"name": "app", "args": ["java", "-Xms512m", "-Xmx2g", "-jar", "app.jar"]}]}}}})
    assert fields_of(kubectl("patch", "deployment", "app", "-n", "demo", "-p", patch)) == [{"jvm"}]
    manifest = copy.deepcopy(deployment(args=JAVA))
    manifest.update(apiVersion="apps/v1", kind="Deployment")
    manifest["spec"]["replicas"] = 6
    applied = kubectl("apply", "-f", "app.yaml", files={"app.yaml": yaml.safe_dump(manifest)})
    assert fields_of(applied) == [{"replicas"}]
    assert kubectl("set", "image", "deployment/app", "app=app:v2") == [] and kubectl("rollout", "restart", "deployment/app") == []
    assert deployment_edits("kubedoor", "resource_apply", {"yaml_content": yaml.safe_dump(manifest), "method": "create"}) == []
    assert fields_of(deployment_edits("kubedoor", "resource_apply", {"yaml_content": yaml.safe_dump(manifest)})) == [{"replicas"}]


# ---- 网关：拦截、准备 → 执行 → 回读 ----

def context(permission="rw"):
    return RunContext({"id": "run-1", "session_id": "session-1", "skill_ids": [],
                       "scope": {"env": ENV, "namespace": "demo", "deployment": None, "pod": None}}, Identity("operator", permission))


@pytest.mark.asyncio
async def test_direct_edits_of_controlled_deployments_are_rejected_before_approval():
    gateway = stub_control(Gateway(FakeStore(), CredentialCipher("ab" * 32), "http://master", "token"),
                           control_state("controlled"), deployment(args=JAVA))
    try:
        with pytest.raises(AIError) as denied:
            await gateway.invoke(context(), "kubectl", {"argv": ["scale", "deployment/app", "--replicas=9", "-n", "demo"]}, source="agent", mcp=True)
        assert denied.value.code == "control_direct_edit" and "scale_deployment" in str(denied.value)
        assert gateway.store.actions == {}
        # 不改副本数 / 资源 / JVM 的修改照常进入审批
        gateway.prepare = lambda *args, **kwargs: _prepared(args[3])
        with pytest.raises(PendingAction):
            await gateway.invoke(context(), "kubectl", {"argv": ["set", "image", "deployment/app", "app=app:v2", "-n", "demo"]}, source="agent", mcp=True)
        # 按标签批量扩缩：命名空间在准入控制管辖内就拒绝
        gateway.deployment_control.return_value = control_state("unregistered")
        with pytest.raises(AIError):
            await gateway.invoke(context(), "kubectl", {"argv": ["scale", "deployment", "--all", "--replicas=0", "-n", "demo"]}, source="agent", mcp=True)
        gateway.deployment_control.return_value = control_state("namespace_off")
        with pytest.raises(PendingAction):
            await gateway.invoke(context(), "kubectl", {"argv": ["scale", "deployment/app", "--replicas=9", "-n", "demo"]}, source="agent", call_id="ok", mcp=True)
    finally:
        await gateway.close()


async def _prepared(arguments):
    return {"success": True, "arguments": arguments, "preview": {"command": arguments.get("argv")}, "read_only": False}


class FakeMaster:
    """master + agent：按准入控制规则模拟副本数被改写，记录业务请求"""

    def __init__(self, state="controlled", **table):
        self.control = control_state(state, **table)
        self.live = deployment(replicas=2, args=JAVA)
        self.calls = []

    def handle(self, request):
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        if path == "/api/ai/internal/deployment-control":
            return httpx.Response(200, json={"success": True, "data": self.control})
        if path == "/api/ai/internal/agent-execute":
            arguments = body["arguments"]
            if arguments["method"] == "GET":
                return httpx.Response(200, json={"success": True, "data": self.live})
            if body["phase"] == "prepare":
                return httpx.Response(200, json={"success": True, "arguments": {**arguments, "_preconditions": [{"path": arguments["path"], "resourceVersion": "7"}]},
                                                 "preview": {"diff": "--- current\n+++ proposed"}})
            assert body["approved"] is True
            self.calls.append(("k8s", arguments["body"]))
            patch = arguments["body"][0]
            # 计划里的直接修改只有两种：整体替换第一个容器的 resources 或 args
            self.live["spec"]["template"]["spec"]["containers"][0][patch["path"].rsplit("/", 1)[-1]] = patch["value"]
            return httpx.Response(200, json={"success": True, "data": self.live})
        self.calls.append((path, body))
        if path == "/api/db/res/pod_count":
            self.control = control_state("controlled", pod_count_manual=body["pod_count_manual"])
        if path == "/api/scale":
            replicas = body["deployment_list"][0]["num"]
            # 受管控且不是临时扩缩：准入控制改写成管控值
            if self.control["controlled"] and request.url.params.get("temp") != "true":
                replicas = self.control["effective"]["replicas"]
            self.live["spec"]["replicas"] = replicas
        return httpx.Response(200, json={"success": True, "message": "ok"})


async def run_planned(fake, operation, arguments, change_before_execute=None):
    gateway = Gateway(FakeStore(), CredentialCipher("ab" * 32), "http://master", "token",
                      http=httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)))
    try:
        with pytest.raises(PendingAction) as pending:
            await gateway.invoke(context(), operation, {**ARGS, **arguments}, call_id=f"{operation}-call", mcp=True)
        assert fake.calls == [], "准备阶段不能提交修改"
        action = gateway.store.actions[pending.value.action["action_id"]]
        if change_before_execute:
            change_before_execute()
        return pending.value.action, await gateway.dispatch(context(), action)
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_persistent_scale_end_to_end_writes_table_then_scales_and_verifies():
    fake = FakeMaster()
    approval, result = await run_planned(fake, "scale_deployment", {"replicas": 5})
    assert [step["name"] for step in approval["preview"]["plan"]["steps"]] == ["写入管控副本数", "扩缩容"]
    assert [path for path, _ in fake.calls] == ["/api/db/res/pod_count", "/api/scale"]
    assert result["success"] is True and result["verification"]["matched"] is True
    assert fake.live["spec"]["replicas"] == 5


def test_old_style_scale_would_be_reverted_and_verification_reports_it():
    """没有写管控表的扩缩容被准入控制改回：回读要明确告诉模型，而不是报成功了事"""
    fake = FakeMaster()
    plan_result = build_plan("scale_deployment", {**ARGS, "replicas": 5}, ENV, fake.control, snapshot(fake.live), scale_step())
    fake.live["spec"]["replicas"] = fake.control["effective"]["replicas"]
    assert verify(plan_result, fake.live)["mismatches"][0]["reason"] == "被准入控制改写成了管控值"


@pytest.mark.asyncio
async def test_temporary_scale_end_to_end_sends_temp_and_keeps_the_table():
    fake = FakeMaster()
    _, result = await run_planned(fake, "scale_deployment", {"replicas": 5, "mode": "temporary"})
    assert [path for path, _ in fake.calls] == ["/api/scale"] and result["verification"]["matched"] is True


@pytest.mark.asyncio
async def test_control_change_during_approval_stops_before_any_write():
    fake = FakeMaster()
    _, result = await run_planned(fake, "scale_deployment", {"replicas": 5},
                                  change_before_execute=lambda: setattr(fake, "control", control_state("namespace_off")))
    assert result["success"] is False and result["error"]["code"] == "control_changed" and fake.calls == []


@pytest.mark.asyncio
async def test_uncontrolled_resources_are_dry_run_then_patched_with_approval():
    fake = FakeMaster("namespace_off")
    approval, result = await run_planned(fake, "update_deployment_resources", {"limit_mem_mb": 2048})
    step = approval["preview"]["plan"]["steps"][0]
    assert step["kind"] == "k8s" and step["source"] == "agent" and step["diff"].startswith("---")
    assert step["arguments"]["_preconditions"][0]["resourceVersion"] == "7"
    assert fake.calls[0][0] == "k8s" and result["success"] is True
    assert result["verification"]["matched"] is True


@pytest.mark.asyncio
async def test_planned_operations_need_an_existing_deployment_and_a_plan():
    fake = FakeMaster()
    gateway = Gateway(FakeStore(), CredentialCipher("ab" * 32), "http://master", "token",
                      http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"success": False, "error": {"code": "NOT_FOUND", "status": 404}})
                                                                           if r.url.path == "/api/ai/internal/agent-execute" else fake.handle(r))))
    try:
        with pytest.raises(AIError) as missing:
            await gateway.invoke(context(), "scale_deployment", {**ARGS, "replicas": 5}, mcp=True)
        assert missing.value.code == "deployment_not_found"
        with pytest.raises(AIError) as no_plan:
            await gateway.execute_action(context(), {"source": "kubedoor", "operation": "update_deployment_jvm", "arguments": ARGS, "preview": {}})
        assert no_plan.value.code == "plan_missing" and CATALOG["update_deployment_jvm"][1] == ""
    finally:
        await gateway.close()
