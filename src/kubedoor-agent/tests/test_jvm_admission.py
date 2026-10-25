import base64
import copy
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def review(args, operation="UPDATE", kind="Deployment", strategy=None):
    obj = {
        "metadata": {"namespace": "team", "name": "service"},
        "spec": {"replicas": 2, "template": {"metadata": {"labels": {"app": "service"}}, "spec": {"containers": [
            {"name": "main", "image": "app:new", "args": args},
            {"name": "sidecar", "image": "sidecar:latest", "args": ["java", "-Xmx128m"]},
        ]}}},
    }
    if strategy is not None:
        obj["spec"]["strategy"] = copy.deepcopy(strategy)
    old = copy.deepcopy(obj)
    old["spec"]["template"]["spec"]["containers"][0]["image"] = "app:old"
    if kind == "Scale":
        obj = {"metadata": obj["metadata"], "spec": {"replicas": 3}}
        old = {"metadata": old["metadata"], "spec": {"replicas": 2}}
    return {"request": {"uid": "request-1", "kind": {"kind": kind}, "operation": operation, "object": obj, "oldObject": None if operation == "CREATE" else old}}


async def run_review(
    admission_module, payload, jvm_config=None, legacy=False, scheduler=False,
    existing_max_unavailable="10%", master_result=None,
):
    futures = {}
    service = admission_module.AdmisService(None, None, None, futures)
    service._get_deployment_affinity_old = AsyncMock(return_value=False)
    # Manual count and limits continue to share the existing admission patch.
    result = [2, -1, 4, 100, 128, 2000, 2048, scheduler]
    if not legacy:
        result.append(jvm_config or {})
    if master_result is not None:
        result = master_result
    service._get_pod_label_and_max_unavailable = AsyncMock(return_value={
        "app_label_value": "service", "maxUnavailable_value": existing_max_unavailable,
    })
    service._get_deployment_affinity = lambda *args: {}
    sent = []

    async def send(message):
        sent.append(message)
        futures.pop(message["request_id"]).set_result(result)

    service.set_ws_conn(SimpleNamespace(closed=False, send_json=send))
    response = await service.admis_mutate(SimpleNamespace(json=AsyncMock(return_value=payload)))
    envelope = json.loads(response.body)
    raw_patch = envelope["response"].get("patch")
    patch = json.loads(base64.b64decode(raw_patch)) if raw_patch else None
    return envelope, patch, sent


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["CREATE", "UPDATE"])
async def test_admission_applies_jvm_by_name_on_create_and_release(admission_module, operation):
    args = ["java", "-Xms128m", "-Xmx256m", "-Xss512k", "-XX:MaxMetaspaceSize=100m", "-Xmx512m", "-XX:+UseG1GC", "-jar", "app.jar", "-Xmx32m"]
    payload = review(args, operation)
    envelope, patch, sent = await run_review(admission_module, payload, {
        "jvm_xms_bytes": 512 * 1024 ** 2, "jvm_xmx_bytes": 1024 ** 3,
        "jvm_xss_bytes": 512 * 1024, "jvm_max_metaspace_bytes": 200 * 1024 ** 2,
    })
    assert envelope["response"]["allowed"] is True
    assert sent[0]["jvm_config"] is True
    args_patch = [entry for entry in patch if entry["path"].endswith("/args")]
    assert args_patch == [{"op": "replace", "path": "/spec/template/spec/containers/0/args", "value": [
        "java", "-Xms512m", "-Xmx1024m", "-Xss512k", "-XX:MaxMetaspaceSize=200m", "-Xmx1024m", "-XX:+UseG1GC", "-jar", "app.jar", "-Xmx32m",
    ]}]
    assert {"op": "replace", "path": "/spec/replicas", "value": 4} in patch
    resource_patch = next(entry for entry in patch if entry["path"].endswith("/resources"))
    assert resource_patch["value"] == {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"cpu": "2000m", "memory": "2048Mi"}}
    assert not any("containers/1" in entry["path"] for entry in patch)
    assert payload["request"]["object"]["spec"]["template"]["spec"]["containers"][0]["args"] == args


@pytest.mark.asyncio
async def test_old_master_contract_keeps_existing_resource_patch(admission_module):
    _, patch, _ = await run_review(admission_module, review(["java", "-Xmx256m"]), legacy=True)
    assert any(entry["path"].endswith("/resources") for entry in patch)
    assert not any(entry["path"].endswith("/args") for entry in patch)


@pytest.mark.asyncio
async def test_missing_and_unmanaged_options_are_preserved(admission_module):
    _, patch, _ = await run_review(admission_module, review(["java", "-Xms128m", "-Xss512k", "-jar", "app.jar"]), {"jvm_xms_bytes": None, "jvm_xmx_bytes": 1024 ** 3})
    assert not any(entry["path"].endswith("/args") for entry in patch)


@pytest.mark.asyncio
@pytest.mark.parametrize("args", [["sh", "-c", "java -Xmx256m"], ["/usr/bin/java", "-Xmx256m"], None])
async def test_admission_rechecks_exact_java_gate(admission_module, args):
    _, patch, _ = await run_review(admission_module, review(args), {"jvm_xmx_bytes": 1024 ** 3})
    assert not any(entry["path"].endswith("/args") for entry in patch)


@pytest.mark.asyncio
async def test_scale_with_new_contract_still_only_patches_replicas(admission_module):
    _, patch, _ = await run_review(admission_module, review([], kind="Scale"), {"jvm_xmx_bytes": 1024 ** 3})
    assert patch == [{"op": "replace", "path": "/spec/replicas", "value": 4}]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["Deployment", "Scale"])
async def test_dry_run_is_rewritten_the_same_but_sends_no_message(admission_module, kind):
    # AI 审批前的 server dry-run 也会经过 webhook:改写结果照常,IM 不能提前通知一个可能被拒绝的修改
    payload = review(["java", "-Xmx256m", "-jar", "app.jar"], kind=kind)
    _, patch, _ = await run_review(admission_module, payload, {"jvm_xmx_bytes": 1024 ** 3})
    assert admission_module.utils.send_msg.called
    admission_module.utils.send_msg.reset_mock()
    dry = copy.deepcopy(payload)
    dry["request"]["dryRun"] = True
    _, dry_patch, _ = await run_review(admission_module, dry, {"jvm_xmx_bytes": 1024 ** 3})
    assert dry_patch == patch
    assert not admission_module.utils.send_msg.called


@pytest.mark.asyncio
async def test_restart_template_annotation_applies_jvm_config(admission_module):
    payload = review(["java", "-Xmx256m", "-jar", "app.jar"])
    new_template = payload["request"]["object"]["spec"]["template"]
    old_template = payload["request"]["oldObject"]["spec"]["template"]
    old_template["spec"]["containers"][0]["image"] = new_template["spec"]["containers"][0]["image"]
    new_template["metadata"]["annotations"] = {"kubectl.kubernetes.io/restartedAt": "2026-10-04T09:00:00"}
    _, patch, _ = await run_review(admission_module, payload, {"jvm_xmx_bytes": 1024 ** 3})
    assert next(entry for entry in patch if entry["path"].endswith("/args"))["value"][1] == "-Xmx1024m"


@pytest.mark.asyncio
@pytest.mark.parametrize("numeric_value", [3, "3", "003"])
async def test_rollout_values_keep_integer_and_percent_types(admission_module, numeric_value):
    payload = review(
        ["java", "-Xmx256m"],
        operation="CREATE",
        strategy={"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": 1, "maxSurge": 2}},
    )
    _, patch, _ = await run_review(admission_module, payload, {
        "max_unavailable": numeric_value, "max_surge": "35%",
    })
    rollout_patches = [entry for entry in patch if "/spec/strategy/" in entry["path"]]
    assert rollout_patches == [
        {"op": "add", "path": "/spec/strategy/rollingUpdate/maxUnavailable", "value": 3},
        {"op": "add", "path": "/spec/strategy/rollingUpdate/maxSurge", "value": "35%"},
    ]


@pytest.mark.asyncio
async def test_missing_rollout_values_default(admission_module):
    payload = review(["java", "-Xmx256m"], operation="CREATE", strategy={"type": "RollingUpdate"})
    _, patch, _ = await run_review(admission_module, payload)
    assert {"op": "add", "path": "/spec/strategy/rollingUpdate", "value": {
        "maxUnavailable": "30%", "maxSurge": 0,
    }} in patch


@pytest.mark.asyncio
async def test_absent_strategy_is_created_with_rolling_update_defaults(admission_module):
    payload = review(["java", "-Xmx256m"], operation="CREATE")
    _, patch, _ = await run_review(admission_module, payload)
    assert {"op": "add", "path": "/spec/strategy", "value": {
        "type": "RollingUpdate", "rollingUpdate": {
            "maxUnavailable": "30%", "maxSurge": 0,
        },
    }} in patch


@pytest.mark.asyncio
async def test_zero_is_a_control_value_and_partial_values_default_independently(admission_module):
    payload = review(
        ["java", "-Xmx256m"], operation="CREATE",
        strategy={"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": 2}},
    )
    _, patch, _ = await run_review(admission_module, payload, {"max_unavailable": 0})
    assert {"op": "add", "path": "/spec/strategy/rollingUpdate/maxUnavailable", "value": 0} in patch
    assert {"op": "add", "path": "/spec/strategy/rollingUpdate/maxSurge", "value": 0} in patch


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [True, -1, 1.5, "1.5", "101%", str(1 << 31)])
@pytest.mark.parametrize(
    ("field", "path", "fallback"),
    [("max_unavailable", "maxUnavailable", "30%"), ("max_surge", "maxSurge", 0)],
)
async def test_invalid_rollout_control_defaults(admission_module, invalid, field, path, fallback):
    payload = review(
        ["java", "-Xmx256m"], operation="CREATE",
        strategy={"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": 2, "maxSurge": 2}},
    )
    _, patch, _ = await run_review(admission_module, payload, {field: invalid})
    assert {"op": "add", "path": f"/spec/strategy/rollingUpdate/{path}", "value": fallback} in patch


@pytest.mark.asyncio
async def test_scheduler_keeps_legacy_max_unavailable_and_ignores_new_controls(admission_module):
    payload = review(
        ["java", "-Xmx256m"], operation="CREATE",
        strategy={"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": 1, "maxSurge": 1}},
    )
    _, patch, _ = await run_review(
        admission_module, payload, {"max_unavailable": 7, "max_surge": "40%"}, scheduler=True,
    )
    assert {"op": "replace", "path": "/spec/strategy/rollingUpdate/maxUnavailable", "value": 1} in patch
    assert not any("maxSurge" in entry["path"] for entry in patch)


@pytest.mark.asyncio
async def test_scheduler_keeps_existing_max_unavailable_when_it_is_large_enough(admission_module):
    payload = review(
        ["java", "-Xmx256m"], operation="CREATE",
        strategy={"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": 1, "maxSurge": 1}},
    )
    _, patch, _ = await run_review(admission_module, payload, scheduler=True, existing_max_unavailable="35%")
    assert {"op": "replace", "path": "/spec/strategy/rollingUpdate/maxUnavailable", "value": "35%"} in patch
    assert not any("maxSurge" in entry["path"] for entry in patch)


@pytest.mark.asyncio
async def test_scheduler_strategy_only_update_keeps_legacy_pass_behavior(admission_module):
    payload = review(
        ["java", "-Xmx256m"],
        strategy={"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": 1, "maxSurge": 1}},
    )
    obj = payload["request"]["object"]
    old = payload["request"]["oldObject"]
    old["spec"]["template"] = copy.deepcopy(obj["spec"]["template"])
    old["spec"]["strategy"]["rollingUpdate"]["maxSurge"] = 2
    futures = {}
    service = admission_module.AdmisService(None, None, None, futures)
    service._get_deployment_affinity_old = AsyncMock(return_value=False)
    result = [2, -1, 4, 100, 128, 2000, 2048, True, {"max_unavailable": 3, "max_surge": "40%"}]

    async def send(message):
        futures.pop(message["request_id"]).set_result(result)

    service.set_ws_conn(SimpleNamespace(closed=False, send_json=send))
    response = await service.admis_mutate(SimpleNamespace(json=AsyncMock(return_value=payload)))
    envelope = json.loads(response.body)
    assert envelope["response"]["allowed"] is True
    assert "patch" not in envelope["response"]


@pytest.mark.asyncio
async def test_recreate_strategy_does_not_receive_rolling_update_fields(admission_module):
    payload = review(["java", "-Xmx256m"], operation="CREATE", strategy={"type": "Recreate"})
    _, patch, _ = await run_review(admission_module, payload, {
        "max_unavailable": 2, "max_surge": "35%",
    })
    assert not any("/spec/strategy" in entry["path"] for entry in patch)


@pytest.mark.asyncio
async def test_strategy_only_update_still_applies_resources_and_jvm_args(admission_module):
    payload = review(
        ["java", "-Xmx256m"],
        strategy={"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": 1, "maxSurge": 1}},
    )
    obj = payload["request"]["object"]
    old = payload["request"]["oldObject"]
    old["spec"]["template"] = copy.deepcopy(obj["spec"]["template"])
    old["spec"]["strategy"]["rollingUpdate"]["maxSurge"] = 2
    _, patch, _ = await run_review(admission_module, payload, {
        "jvm_xmx_bytes": 1024 ** 3, "max_unavailable": 4, "max_surge": "35%",
    })
    assert any(entry["path"].endswith("/resources") for entry in patch)
    assert next(entry for entry in patch if entry["path"].endswith("/args"))["value"][1] == "-Xmx1024m"
    assert {"op": "add", "path": "/spec/strategy/rollingUpdate/maxSurge", "value": "35%"} in patch


@pytest.mark.asyncio
async def test_temporary_scale_shortcut_does_not_apply_rollout_config(admission_module):
    from datetime import datetime

    payload = review(
        ["java", "-Xmx256m"],
        strategy={"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": 1, "maxSurge": 1}},
    )
    obj = payload["request"]["object"]
    old = payload["request"]["oldObject"]
    old["spec"] = copy.deepcopy(obj["spec"])
    old["spec"]["replicas"] = 2
    obj["spec"]["replicas"] = 3
    obj["metadata"]["annotations"] = {
        "scale.temp": f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}@test",
    }
    service = admission_module.AdmisService(None, None, None, {})
    response = await service.admis_mutate(SimpleNamespace(json=AsyncMock(return_value=payload)))
    envelope = json.loads(response.body)
    assert envelope["response"]["allowed"] is True
    assert "patch" not in envelope["response"]


def replicas_only(payload, replicas=5):
    """模板和滚动策略都不变,只改副本数(kubectl patch / edit / apply 直接改 spec.replicas)"""
    obj, old = payload["request"]["object"], payload["request"]["oldObject"]
    old["spec"] = copy.deepcopy(obj["spec"])
    obj["spec"]["replicas"] = replicas
    return obj, old


def scale_temp(minutes_ago):
    """花折临时扩缩容写的标记:时间@原副本数-->目标副本数"""
    return f"{(datetime.now() - timedelta(minutes=minutes_ago)).strftime('%Y-%m-%d %H:%M:%S')}@2-->5"


@pytest.mark.asyncio
async def test_replicas_only_update_is_rewritten_to_the_control_value(admission_module):
    # 以前落到「不符合预设判断条件」直接放行:受管控的服务直接 patch 副本数就绕开了管控,要到下次发布、重启才改回
    payload = review(["java", "-Xmx256m"])
    replicas_only(payload)
    envelope, patch, _ = await run_review(admission_module, payload)
    assert envelope["response"]["allowed"] is True
    assert patch == [{"op": "replace", "path": "/spec/replicas", "value": 4}]
    assert "只改副本数" in admission_module.utils.send_msg.call_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["kept_fresh", "kept_stale", "written_stale"])
async def test_scale_temp_must_be_freshly_written_by_this_request(admission_module, case):
    # 对象上原有的标记(哪怕还在 5 分钟内)不能顺带放行别人直接改的副本数;超过 5 分钟的新标记也不行
    fresh, stale = scale_temp(0), scale_temp(6)
    old_marker, new_marker = {
        "kept_fresh": (fresh, fresh), "kept_stale": (stale, stale), "written_stale": (None, stale),
    }[case]
    payload = review(["java", "-Xmx256m"])
    obj, old = replicas_only(payload)
    obj["metadata"]["annotations"] = {"scale.temp": new_marker}
    if old_marker:
        old["metadata"]["annotations"] = {"scale.temp": old_marker}
    _, patch, _ = await run_review(admission_module, payload)
    assert patch == [{"op": "replace", "path": "/spec/replicas", "value": 4}]


@pytest.mark.asyncio
async def test_temporary_scale_replacing_an_old_marker_still_passes(admission_module):
    payload = review(["java", "-Xmx256m"])
    obj, old = replicas_only(payload)
    old["metadata"]["annotations"] = {"scale.temp": scale_temp(30)}
    obj["metadata"]["annotations"] = {"scale.temp": scale_temp(0)}
    envelope, patch, sent = await run_review(admission_module, payload)
    assert envelope["response"]["allowed"] is True and patch is None and sent == []


@pytest.mark.asyncio
async def test_replicas_change_outside_control_and_unchanged_spec_pass_untouched(admission_module):
    outside = review(["java", "-Xmx256m"])
    replicas_only(outside)
    envelope, patch, _ = await run_review(admission_module, outside, master_result=[200, "非管控命名空间，直接放行"])
    assert envelope["response"]["allowed"] is True and patch is None
    # 只改标签、注解(Deployment 控制器每次发布也会改 revision 注解):副本数没变就不动
    labels_only = review(["java", "-Xmx256m"])
    obj, _ = replicas_only(labels_only, replicas=2)
    obj["metadata"]["labels"] = {"owner": "team"}
    envelope, patch, _ = await run_review(admission_module, labels_only)
    assert envelope["response"]["allowed"] is True and patch is None


@pytest.mark.asyncio
async def test_scheduler_mode_replicas_change_still_gets_the_control_value(admission_module):
    # 开了固定节点均衡时滚动策略的变化照旧放行,副本数仍改成管控值
    payload = review(
        ["java", "-Xmx256m"],
        strategy={"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": 1, "maxSurge": 1}},
    )
    _, old = replicas_only(payload)
    old["spec"]["strategy"]["rollingUpdate"]["maxSurge"] = 2
    _, patch, _ = await run_review(admission_module, payload, scheduler=True)
    assert patch == [{"op": "replace", "path": "/spec/replicas", "value": 4}]
