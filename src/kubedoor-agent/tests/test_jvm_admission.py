import base64
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def review(args, operation="UPDATE", kind="Deployment"):
    obj = {
        "metadata": {"namespace": "team", "name": "service"},
        "spec": {"replicas": 2, "template": {"metadata": {"labels": {"app": "service"}}, "spec": {"containers": [
            {"name": "main", "image": "app:new", "args": args},
            {"name": "sidecar", "image": "sidecar:latest", "args": ["java", "-Xmx128m"]},
        ]}}},
    }
    old = copy.deepcopy(obj)
    old["spec"]["template"]["spec"]["containers"][0]["image"] = "app:old"
    if kind == "Scale":
        obj = {"metadata": obj["metadata"], "spec": {"replicas": 3}}
        old = {"metadata": old["metadata"], "spec": {"replicas": 2}}
    return {"request": {"uid": "request-1", "kind": {"kind": kind}, "operation": operation, "object": obj, "oldObject": None if operation == "CREATE" else old}}


async def run_review(admission_module, payload, jvm_config=None, legacy=False):
    futures = {}
    service = admission_module.AdmisService(None, None, None, futures)
    service._get_deployment_affinity_old = AsyncMock(return_value=False)
    # Manual count and limits continue to share the existing admission patch.
    result = [2, -1, 4, 100, 128, 2000, 2048, False]
    if not legacy:
        result.append(jvm_config or {})
    sent = []

    async def send(message):
        sent.append(message)
        futures.pop(message["request_id"]).set_result(result)

    service.set_ws_conn(SimpleNamespace(closed=False, send_json=send))
    response = await service.admis_mutate(SimpleNamespace(json=AsyncMock(return_value=payload)))
    envelope = json.loads(response.body)
    patch = json.loads(base64.b64decode(envelope["response"]["patch"]))
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
async def test_restart_template_annotation_applies_jvm_config(admission_module):
    payload = review(["java", "-Xmx256m", "-jar", "app.jar"])
    new_template = payload["request"]["object"]["spec"]["template"]
    old_template = payload["request"]["oldObject"]["spec"]["template"]
    old_template["spec"]["containers"][0]["image"] = new_template["spec"]["containers"][0]["image"]
    new_template["metadata"]["annotations"] = {"kubectl.kubernetes.io/restartedAt": "2026-10-04T09:00:00"}
    _, patch, _ = await run_review(admission_module, payload, {"jvm_xmx_bytes": 1024 ** 3})
    assert next(entry for entry in patch if entry["path"].endswith("/args"))["value"][1] == "-Xmx1024m"
