"""istio_manager 的 VirtualService 接口:查看单个 VS 返回完整对象,找不到回 404。"""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from kubernetes_asyncio.client.exceptions import ApiException

from conftest import load_module

istio_manager = load_module(
    "agent_test_istio_manager", Path(__file__).resolve().parents[1] / "res_manager" / "istio_manager.py")


def request(**query):
    return SimpleNamespace(query=query)


def body(resp):
    return json.loads(resp.body)


@pytest.mark.asyncio
async def test_detail_returns_whole_object():
    vs = {"apiVersion": "networking.istio.io/v1beta1", "kind": "VirtualService",
          "metadata": {"name": "a", "namespace": "istio-system", "managedFields": [{"manager": "kubectl"}]},
          "spec": {"hosts": ["a.example.com"]}}
    api = Mock()
    api.get_namespaced_custom_object = AsyncMock(return_value=vs)
    resp = await istio_manager.get_virtualservice_detail(api, request(namespace="istio-system", name="a"))
    assert resp.status == 200 and body(resp) == {"success": True, "data": vs}
    assert api.get_namespaced_custom_object.await_args.kwargs == {
        "group": "networking.istio.io", "version": "v1beta1", "namespace": "istio-system",
        "plural": "virtualservices", "name": "a"}


@pytest.mark.asyncio
async def test_detail_not_found_and_missing_params():
    api = Mock()
    api.get_namespaced_custom_object = AsyncMock(side_effect=ApiException(status=404, reason="Not Found"))
    resp = await istio_manager.get_virtualservice_detail(api, request(namespace="istio-system", name="gone"))
    assert resp.status == 404 and "not found" in body(resp)["error"]
    resp = await istio_manager.get_virtualservice_detail(api, request(name="a"))
    assert resp.status == 400 and api.get_namespaced_custom_object.await_count == 1


@pytest.mark.asyncio
async def test_routes_summary_filters_by_host_namespace_and_name():
    items = [
        {"metadata": {"name": "a", "namespace": "istio-system", "resourceVersion": "1"},
         "spec": {"hosts": ["a.example.com"], "gateways": ["istio-system/internal"],
                  "http": [{"match": [{"uri": {"prefix": "/a/"}}],
                            "route": [{"destination": {"host": "svc-a.wms.svc.cluster.local", "port": {"number": 80}}}],
                            "timeout": "5s"}]}},
        {"metadata": {"name": "wild", "namespace": "istio-system"},
         "spec": {"hosts": ["*.example.com"],
                  "http": [{"route": [{"destination": {"host": "svc-w.wms.svc.cluster.local"}}]}]}},
        {"metadata": {"name": "db", "namespace": "wms"},
         "spec": {"hosts": ["b.example.com"],
                  "tcp": [{"match": [{"port": 5432}], "route": [{"destination": {"host": "db.wms.svc.cluster.local"}}]}]}},
    ]
    api = Mock()
    api.list_cluster_custom_object = AsyncMock(return_value={"items": items})
    resp = await istio_manager.get_virtualservice_routes(api, request(host="a.example.com"))
    assert resp.status == 200
    data = body(resp)
    assert data["success"] and data["total"] == 2
    assert [item["name"] for item in data["data"]] == ["a", "wild"]
    assert data["data"][0]["gateways"] == ["istio-system/internal"]
    assert data["data"][0]["http"][0]["destination"] == [{"host": "svc-a.wms.svc.cluster.local", "port": 80}]
    assert data["data"][0]["http"][0]["timeout"] == "5s"
    assert "spec" not in data["data"][0]

    api.list_namespaced_custom_object = AsyncMock(return_value={"items": [items[2]]})
    resp = await istio_manager.get_virtualservice_routes(api, request(namespace="wms"))
    assert [item["name"] for item in body(resp)["data"]] == ["db"]
    api.list_namespaced_custom_object.assert_awaited_once_with(
        group="networking.istio.io", version="v1beta1", namespace="wms", plural="virtualservices")

    resp = await istio_manager.get_virtualservice_routes(api, request(name="db"))
    assert [item["name"] for item in body(resp)["data"]] == ["db"]
    assert body(resp)["data"][0]["tcp"][0]["destination"] == [{"host": "db.wms.svc.cluster.local"}]

    resp = await istio_manager.get_virtualservice_routes(api, request(host="missing.other.com"))
    assert body(resp)["data"] == [] and body(resp)["total"] == 0
