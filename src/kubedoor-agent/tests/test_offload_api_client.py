"""OffloadApiClient 对接真实的 kubernetes_asyncio API 类,用本地 aiohttp 假 apiserver 验证行为和原版一致"""

import json
import threading
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiohttp import web
from kubernetes_asyncio import client
from kubernetes_asyncio.client.rest import ApiException


def make_pod(i):
    return {
        "metadata": {"name": f"pod-{i}", "namespace": "ns", "labels": {"app": "web", "pad": "x" * 200}},
        "spec": {"nodeName": "node-1", "containers": [{"name": "app", "image": "registry.example/app:1"}]},
        "status": {"phase": "Running", "startTime": "2026-10-05T12:00:00Z"},
    }


@pytest_asyncio.fixture
async def apiserver():
    async def list_pods(request):
        items = [make_pod(i) for i in range(300)]
        meta = {"resourceVersion": "100"}
        limit = int(request.query.get("limit", 0))
        if limit:
            start = int(request.query.get("continue") or 0)
            items, rest = items[start:start + limit], items[start + limit:]
            if rest:
                meta["continue"] = str(start + limit)
        return web.json_response({"kind": "PodList", "metadata": meta, "items": items})

    async def denied(request):
        body = {"kind": "Status", "status": "Failure", "message": "pods is forbidden", "reason": "Forbidden", "code": 403}
        return web.json_response(body, status=403)

    async def read_pod(request):
        name = request.match_info["name"]
        if name == "missing":
            body = {"kind": "Status", "status": "Failure", "message": 'pods "missing" not found', "reason": "NotFound", "code": 404}
            return web.json_response(body, status=404)
        return web.json_response(make_pod(0) | {"metadata": {"name": name, "namespace": "ns"}})

    async def pod_metrics(request):
        return web.json_response({"items": [{"metadata": {"name": "pod-0", "namespace": "ns"}, "containers": []}]})

    app = web.Application()
    app.router.add_get("/api/v1/pods", list_pods)
    app.router.add_get("/api/v1/namespaces/ns/pods/{name}", read_pod)
    app.router.add_get("/api/v1/namespaces/denied/pods", denied)
    app.router.add_get("/apis/metrics.k8s.io/v1beta1/pods", pod_metrics)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    await runner.cleanup()


@pytest_asyncio.fixture
async def api_client(apiserver, client_manager_module):
    class RecordingClient(client_manager_module.OffloadApiClient):
        """记录每次反序列化跑在哪个线程"""

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.threads = []

        def deserialize(self, response, response_type):
            self.threads.append(threading.get_ident())
            return super().deserialize(response, response_type)

    configuration = client.Configuration()
    configuration.host = apiserver
    api = RecordingClient(configuration)
    yield api
    await api.close()


@pytest.mark.asyncio
async def test_large_list_is_deserialized_off_the_event_loop(api_client, client_manager_module):
    pods = await client.CoreV1Api(api_client).list_pod_for_all_namespaces()
    assert isinstance(pods, client.V1PodList)
    assert [p.metadata.name for p in pods.items] == [f"pod-{i}" for i in range(300)]
    assert pods.items[0].status.start_time.year == 2026
    assert len(json.dumps([make_pod(i) for i in range(300)])) >= client_manager_module.OFFLOAD_MIN_BYTES
    assert api_client.threads and threading.get_ident() not in api_client.threads


@pytest.mark.asyncio
async def test_small_response_is_deserialized_inline(api_client):
    pod = await client.CoreV1Api(api_client).read_namespaced_pod("web-1", "ns")
    assert isinstance(pod, client.V1Pod) and pod.metadata.name == "web-1"
    assert api_client.threads == [threading.get_ident()]


@pytest.mark.asyncio
async def test_error_status_raises_api_exception_with_text_body(api_client):
    with pytest.raises(ApiException) as info:
        await client.CoreV1Api(api_client).read_namespaced_pod("missing", "ns")
    assert info.value.status == 404 and info.value.reason == "Not Found"
    # 调用方普遍用 json.loads(e.body) 取 message,body 必须和原版一样是 str
    assert json.loads(info.value.body)["message"] == 'pods "missing" not found'


@pytest.mark.asyncio
async def test_with_http_info_returns_status_and_headers(api_client):
    pods, status, headers = await client.CoreV1Api(api_client).list_pod_for_all_namespaces_with_http_info()
    assert len(pods.items) == 300 and status == 200
    assert headers["Content-Type"].startswith("application/json")


@pytest.mark.asyncio
async def test_preload_content_false_is_passed_through(api_client):
    resp = await client.CoreV1Api(api_client).list_pod_for_all_namespaces(_preload_content=False)
    try:
        assert resp.status == 200
        assert json.loads(await resp.read())["metadata"]["resourceVersion"] == "100"
    finally:
        resp.release()
    assert api_client.threads == []


@pytest.mark.asyncio
async def test_custom_objects_still_return_plain_dicts(api_client):
    metrics = await client.CustomObjectsApi(api_client).list_cluster_custom_object("metrics.k8s.io", "v1beta1", "pods")
    assert metrics["items"][0]["metadata"]["name"] == "pod-0"


@pytest.mark.asyncio
async def test_list_all_raw_follows_pages_and_parses_off_the_event_loop(api_client, client_manager_module, monkeypatch):
    parse_threads = []

    def recording_loads(data):
        parse_threads.append(threading.get_ident())
        return json.loads(data)

    monkeypatch.setattr(client_manager_module, "json", SimpleNamespace(loads=recording_loads))
    monkeypatch.setattr(client_manager_module, "LIST_PAGE_SIZE", 120)
    items = await client_manager_module.list_all_raw(client.CoreV1Api(api_client).list_pod_for_all_namespaces)
    assert [p["metadata"]["name"] for p in items] == [f"pod-{i}" for i in range(300)]
    assert len(parse_threads) == 3 and threading.get_ident() not in parse_threads
    # 原始 JSON,没有走模型反序列化
    assert api_client.threads == []


@pytest.mark.asyncio
async def test_list_all_raw_raises_like_generated_methods(api_client, client_manager_module):
    with pytest.raises(ApiException) as info:
        await client_manager_module.list_all_raw(client.CoreV1Api(api_client).list_namespaced_pod, namespace="denied")
    assert info.value.status == 403 and info.value.reason == "Forbidden"
    assert json.loads(info.value.body)["message"] == "pods is forbidden"
