import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from test_workload_cache import FakeResp

T = "2026-10-05T12:00:00Z"


def request(**query):
    return SimpleNamespace(query=query)


def page(items, cont=None):
    meta = {"resourceVersion": "1"}
    if cont:
        meta["continue"] = cont
    return FakeResp(body={"items": items, "metadata": meta})


def raw_pod(name, ns="ns", node="node-1", phase="Running", statuses=(), **status_extra):
    return {
        "metadata": {"name": name, "namespace": ns, "creationTimestamp": "2026-10-01T03:04:05Z",
                     "ownerReferences": [{"kind": "ReplicaSet", "name": "rs", "controller": True}]},
        "spec": {"nodeName": node, "containers": [{"name": "app"}]},
        "status": {"phase": phase, "podIP": "10.0.0.1", "containerStatuses": list(statuses), **status_extra},
    }


def body(resp):
    return json.loads(resp.text)


@pytest.fixture
def apis():
    core_v1 = Mock()
    core_v1.list_pod_for_all_namespaces = AsyncMock(side_effect=lambda **kwargs: page([]))
    core_v1.list_namespaced_pod = AsyncMock(side_effect=lambda **kwargs: page([]))
    custom_api = Mock()
    custom_api.list_cluster_custom_object = AsyncMock(return_value={"items": []})
    custom_api.list_namespaced_custom_object = AsyncMock(return_value={"items": []})
    return core_v1, custom_api


@pytest.mark.asyncio
async def test_node_filter_is_pushed_down_to_apiserver(pod_manager_module, apis):
    core_v1, custom_api = apis
    resp = await pod_manager_module.get_pod_list(core_v1, custom_api, request(node_name="node-1"))
    assert body(resp) == {"success": True, "data": [], "total": 0}
    kwargs = core_v1.list_pod_for_all_namespaces.await_args.kwargs
    assert kwargs["field_selector"] == "spec.nodeName=node-1"
    # 原始 JSON + 分页,不反序列化成模型对象
    assert kwargs["_preload_content"] is False and kwargs["limit"] == 500

    await pod_manager_module.get_pod_list(core_v1, custom_api, request(namespaces="a,b", node_name="node-1"))
    assert [(c.kwargs["namespace"], c.kwargs["field_selector"]) for c in core_v1.list_namespaced_pod.await_args_list] == [
        ("a", "spec.nodeName=node-1"),
        ("b", "spec.nodeName=node-1"),
    ]


@pytest.mark.asyncio
async def test_without_node_filter_no_field_selector_is_sent(pod_manager_module, apis):
    core_v1, custom_api = apis
    await pod_manager_module.get_pod_list(core_v1, custom_api, request(namespaces="a"))
    assert core_v1.list_namespaced_pod.await_args.kwargs["field_selector"] is None


@pytest.mark.asyncio
async def test_rows_are_built_from_raw_json_off_the_event_loop(pod_manager_module, apis, monkeypatch):
    core_v1, custom_api = apis
    running = raw_pod("web-1", statuses=[{"name": "app", "ready": True, "restartCount": 0, "state": {"running": {"startedAt": T}}}])
    crashloop = raw_pod("web-2", statuses=[{
        "name": "app", "ready": False, "restartCount": 7,
        "state": {"waiting": {"reason": "CrashLoopBackOff", "message": "back-off 5m0s"}},
        "lastState": {"terminated": {"exitCode": 137, "reason": "OOMKilled", "startedAt": T, "finishedAt": T}},
    }])
    evicted = raw_pod("web-3", phase="Failed", reason="Evicted", message="low on memory")
    core_v1.list_pod_for_all_namespaces.side_effect = [page([running, crashloop], cont="tok"), page([evicted])]
    custom_api.list_cluster_custom_object.return_value = {
        "items": [{"metadata": {"name": "web-1", "namespace": "ns"}, "containers": [{"usage": {"cpu": "250m", "memory": "512Mi"}}]}]
    }
    render_threads = []
    render = pod_manager_module._render_pod_list

    def recording_render(*args):
        render_threads.append(threading.get_ident())
        return render(*args)

    monkeypatch.setattr(pod_manager_module, "_render_pod_list", recording_render)
    data = body(await pod_manager_module.get_pod_list(core_v1, custom_api, request()))

    assert data["total"] == 3 and render_threads and threading.get_ident() not in render_threads
    assert core_v1.list_pod_for_all_namespaces.await_args_list[1].kwargs["_continue"] == "tok"
    assert data["data"][0] == {
        "namespace": "ns", "name": "web-1",
        "containers": [{"is_init": False, "name": "app", "ready": True, "state": "Running",
                        "state_info": {"start_time": "2026-10-05 20:00:00"}, "restart_count": 0, "last_state": {}}],
        "restart_count": 0, "controlled_by": "ReplicaSet", "pod_ip": "10.0.0.1",
        "creation_timestamp": "2026-10-01 11:04:05", "node_name": "node-1", "status": "Running",
        "status_reason": "", "status_message": "", "current_cpu_cores": 0.25, "current_memory_mb": 512,
    }
    [container] = data["data"][1]["containers"]
    assert (container["state"], container["state_info"]["reason"], data["data"][1]["restart_count"]) == ("Waiting", "CrashLoopBackOff", 7)
    assert container["last_state"]["terminated"]["exit_code"] == 137
    assert data["data"][1]["current_cpu_cores"] == 0.0
    assert {k: data["data"][2][k] for k in ("status", "status_reason", "status_message", "containers")} == {
        "status": "Evicted", "status_reason": "Failed", "status_message": "low on memory", "containers": [],
    }


@pytest.mark.asyncio
async def test_api_error_keeps_status_and_reason(pod_manager_module, apis):
    core_v1, custom_api = apis
    core_v1.list_pod_for_all_namespaces.side_effect = [FakeResp(status=403, body={"message": "forbidden"})]
    resp = await pod_manager_module.get_pod_list(core_v1, custom_api, request())
    assert resp.status == 403
    assert json.loads(resp.text) == {"error": "Kubernetes API错误: Forbidden"}


@pytest.mark.asyncio
async def test_failing_namespace_is_skipped_in_multi_namespace_query(pod_manager_module, apis):
    core_v1, custom_api = apis
    core_v1.list_namespaced_pod.side_effect = [FakeResp(status=403, body={}), page([raw_pod("web-1", ns="b")])]
    data = body(await pod_manager_module.get_pod_list(core_v1, custom_api, request(namespaces="a,b")))
    assert [p["name"] for p in data["data"]] == ["web-1"]
