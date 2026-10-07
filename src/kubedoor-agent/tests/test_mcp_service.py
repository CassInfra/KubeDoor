import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from test_workload_cache import FakeResp, make_deployment
from test_workload_status import make_pod


def request(**query):
    return SimpleNamespace(query=query)


def body(resp):
    return json.loads(resp.body)


@pytest.fixture
def service(mcp_module, workload_modules):
    core_v1, apps_v1, custom_api = Mock(), Mock(), Mock()
    cache = workload_modules.cache.WorkloadCache(core_v1, apps_v1)
    cache._synced = {"pods": True, "deployments": True}
    svc = mcp_module.MCPService(core_v1, custom_api, apps_v1, cache)
    return svc, cache, mcp_module


@pytest.mark.asyncio
async def test_missing_params(service):
    svc, _, _ = service
    resp = await svc.get_deployment_pods(request(namespace="ns"))
    assert resp.status == 400


@pytest.mark.asyncio
async def test_unknown_deployment(service):
    svc, _, _ = service
    resp = await svc.get_deployment_pods(request(namespace="ns", deployment="web"))
    assert resp.status == 404
    assert body(resp) == {"message": 'deployments.apps "web" not found', "success": False}


@pytest.mark.asyncio
async def test_scaled_to_zero_returns_empty_list(service):
    svc, cache, _ = service
    cache._apply("deployments", "ADDED", make_deployment(replicas=0, ready=0))
    resp = await svc.get_deployment_pods(request(namespace="ns", deployment="web"))
    assert body(resp) == {"success": True, "pods": []}


@pytest.mark.asyncio
async def test_reads_cache_with_isolated_pod_and_metrics(service):
    svc, cache, mcp_module = service
    mcp_module._list_pod_metrics = AsyncMock(return_value={"ns/web-7d9f8c6b5-aaaaa": {"cpu_m": 250, "memory_mb": 300}})
    cache._apply("deployments", "ADDED", make_deployment())
    cache._apply("pods", "ADDED", make_pod(name="web-7d9f8c6b5-aaaaa"))
    cache._apply("pods", "ADDED", make_pod(name="web-7d9f8c6b5-bbbbb", owner=None, labels={"app": "web-ALERT"}))
    cache._apply("pods", "ADDED", make_pod(name="web-api-5c4d3b2a1-ccccc", owner="web-api-5c4d3b2a1", pod_hash="5c4d3b2a1"))

    pods = body(await svc.get_deployment_pods(request(namespace="ns", deployment="web")))["pods"]
    assert [(p["name"], p["isolated"], p["cpu"], p["memory"]) for p in pods] == [
        ("web-7d9f8c6b5-aaaaa", False, "250m", "300MB"),
        ("web-7d9f8c6b5-bbbbb", True, "-", "-"),
    ]
    # 旧接口的字段都还在
    assert {"name", "status", "ready", "pod_ip", "cpu", "memory", "created_at", "app_label", "image", "node_name",
            "restart_count", "restart_reason", "exception_reason", "containers"} <= set(pods[0])
    # 指标按 namespace 缓存,第二次不再查 metrics-server
    await svc.get_deployment_pods(request(namespace="ns", deployment="web"))
    assert mcp_module._list_pod_metrics.await_count == 1


@pytest.mark.asyncio
async def test_event_reason_filled_for_unexplained_pending_pod(service):
    svc, cache, _ = service
    cache._apply("deployments", "ADDED", make_deployment())
    cache._apply("pods", "ADDED", make_pod(phase="Pending", statuses=[], conditions=[]))
    svc._get_pod_events = AsyncMock(return_value=("FailedMount", "volume not found"))
    [pod] = body(await svc.get_deployment_pods(request(namespace="ns", deployment="web")))["pods"]
    assert pod["exception_reason"] == "FailedMount: volume not found"


@pytest.mark.asyncio
async def test_falls_back_to_api_when_cache_not_synced(service):
    svc, cache, _ = service
    cache._synced["pods"] = False
    svc.apps_v1.read_namespaced_deployment = AsyncMock()
    svc.core_v1.list_namespaced_pod = AsyncMock(
        return_value=FakeResp(
            body={
                "items": [
                    make_pod(name="web-7d9f8c6b5-aaaaa"),
                    make_pod(name="web-7d9f8c6b5-bbbbb", owner=None),
                    make_pod(name="other-1a2b3c4d5-ccccc", owner="other-1a2b3c4d5", pod_hash="1a2b3c4d5"),
                ]
            }
        )
    )
    pods = body(await svc.get_deployment_pods(request(namespace="ns", deployment="web")))["pods"]
    assert [p["name"] for p in pods] == ["web-7d9f8c6b5-aaaaa", "web-7d9f8c6b5-bbbbb"]
    svc.apps_v1.read_namespaced_deployment.assert_awaited_once_with("web", "ns")


def raw_event(name, namespace="ns", source=True):
    event = {
        "metadata": {"name": name, "namespace": namespace},
        "involvedObject": {"kind": "Pod", "name": "web-1", "namespace": namespace},
        "type": "Warning", "reason": "BackOff", "message": "Back-off restarting", "count": 3,
        "firstTimestamp": "2026-10-05T12:00:00Z", "lastTimestamp": None,
    }
    if source:
        event["source"] = {"component": "kubelet", "host": "node-1"}
    return event


@pytest.mark.asyncio
async def test_events_are_rendered_from_raw_json(service):
    svc, _, _ = service
    svc.core_v1.list_event_for_all_namespaces = AsyncMock(
        side_effect=[FakeResp(body={"items": [raw_event("a.1"), raw_event("b.2", source=False)], "metadata": {}})]
    )
    resp = await svc.get_namespace_events(request(namespace="ns"))
    data = json.loads(resp.text)
    assert data["success"] and len(data["events"]) == 2
    assert data["events"][0] == {
        "name": "a.1", "namespace": "ns", "type": "Warning", "reason": "BackOff", "message": "Back-off restarting",
        "involved_object": {"kind": "Pod", "name": "web-1", "namespace": "ns"}, "count": 3,
        "first_timestamp": "2026-10-05T12:00:00+00:00", "last_timestamp": None,
        "source": {"component": "kubelet", "host": "node-1"},
    }
    assert data["events"][1]["source"] is None
    kwargs = svc.core_v1.list_event_for_all_namespaces.await_args.kwargs
    assert kwargs["field_selector"] == "involvedObject.namespace=ns" and kwargs["_preload_content"] is False


@pytest.mark.asyncio
async def test_nodes_info_counts_pods_from_raw_json(service):
    svc, _, _ = service

    def node(name):
        return SimpleNamespace(
            metadata=SimpleNamespace(name=name),
            status=SimpleNamespace(
                addresses=[SimpleNamespace(type="InternalIP", address="10.0.0.1")],
                node_info=SimpleNamespace(container_runtime_version="containerd://2.0", os_image="Ubuntu",
                                          kernel_version="6.8", kubelet_version="v1.34.0"),
                conditions=[SimpleNamespace(type="Ready", status="True")],
                allocatable={"cpu": "4", "memory": "8Gi", "pods": "110"},
            ),
        )

    svc.core_v1.list_node = AsyncMock(return_value=SimpleNamespace(items=[node("node-1"), node("node-2")]))
    pods = [{"spec": {"nodeName": "node-1"}}, {"spec": {"nodeName": "node-1"}}, {"spec": {"nodeName": "node-2"}}, {"spec": {}}]
    svc.core_v1.list_pod_for_all_namespaces = AsyncMock(side_effect=[FakeResp(body={"items": pods, "metadata": {}})])
    svc.custom_api.get_cluster_custom_object = AsyncMock(return_value={"usage": {"cpu": "1", "memory": "1Gi"}})
    data = body(await svc.get_nodes_info(request()))
    assert [(n["name"], n["current_pods"], n["max_pods"]) for n in data["nodes"]] == [("node-1", 2, 110), ("node-2", 1, 110)]
    assert svc.core_v1.list_pod_for_all_namespaces.await_args.kwargs["_preload_content"] is False
