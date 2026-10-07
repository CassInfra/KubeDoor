import asyncio
import json
from unittest.mock import AsyncMock, Mock

import pytest
from kubernetes_asyncio.client.rest import ApiException

from test_workload_status import make_pod


class FakeResp:
    def __init__(self, status=200, body=None, lines=()):
        self.status = status
        self.reason = {200: "OK", 403: "Forbidden", 410: "Gone"}.get(status, "Error")
        self.headers = {"Content-Type": "application/json"}
        self._body = json.dumps(body or {}).encode()
        self._lines = [json.dumps(line).encode() + b"\n" for line in lines] + [b""]
        self.content = Mock()
        self.content.readline = AsyncMock(side_effect=self._lines)
        self.released = False

    async def read(self):
        return self._body

    def release(self):
        self.released = True


def make_deployment(name="web", namespace="ns", replicas=3, ready=3, generation=1):
    return {
        "metadata": {"name": name, "namespace": namespace, "generation": generation, "resourceVersion": "5"},
        "spec": {"replicas": replicas},
        "status": {"replicas": replicas, "readyReplicas": ready, "updatedReplicas": replicas, "availableReplicas": ready, "observedGeneration": generation},
    }


class Recorder:
    def __init__(self):
        self.changes = []
        self.resyncs = 0
        self.synced = []

    def on_change(self, key, pods_changed, removed):
        self.changes.append((key, pods_changed, removed))

    def on_resync(self):
        self.resyncs += 1

    def on_synced(self, synced):
        self.synced.append(synced)


@pytest.fixture
def cache_env(workload_modules):
    core_v1 = Mock()
    apps_v1 = Mock()
    core_v1.list_pod_for_all_namespaces = AsyncMock()
    apps_v1.list_deployment_for_all_namespaces = AsyncMock()
    cache = workload_modules.cache.WorkloadCache(core_v1, apps_v1)
    recorder = Recorder()
    cache.add_listener(recorder)
    return cache, core_v1, apps_v1, recorder


@pytest.mark.asyncio
async def test_list_paginates_and_returns_rv(cache_env):
    cache, core_v1, _, _ = cache_env
    core_v1.list_pod_for_all_namespaces.side_effect = [
        FakeResp(body={"items": [make_pod(name="web-7d9f8c6b5-aaaaa")], "metadata": {"continue": "tok"}}),
        FakeResp(body={"items": [make_pod(name="web-7d9f8c6b5-bbbbb")], "metadata": {"resourceVersion": "100"}}),
    ]
    items, rv = await cache._list("pods")
    assert rv == "100"
    assert sorted(items) == [("ns", "web-7d9f8c6b5-aaaaa"), ("ns", "web-7d9f8c6b5-bbbbb")]
    second_call = core_v1.list_pod_for_all_namespaces.call_args_list[1].kwargs
    assert second_call["_continue"] == "tok" and second_call["limit"] == 500 and second_call["_preload_content"] is False


@pytest.mark.asyncio
async def test_list_non_200_raises(cache_env):
    cache, core_v1, _, _ = cache_env
    resp = FakeResp(status=410, body={"message": "continue too old"})
    core_v1.list_pod_for_all_namespaces.return_value = resp
    with pytest.raises(ApiException) as exc:
        await cache._list("pods")
    assert exc.value.status == 410
    assert resp.released


@pytest.mark.asyncio
async def test_watch_applies_events_and_tracks_rv(cache_env):
    cache, core_v1, _, recorder = cache_env
    cache._rv["pods"] = "100"
    pod = make_pod(name="web-7d9f8c6b5-aaaaa")
    pod["metadata"]["resourceVersion"] = "101"
    deleted = dict(pod, metadata=dict(pod["metadata"], resourceVersion="103"))
    resp = FakeResp(
        lines=[
            {"type": "ADDED", "object": pod},
            {"type": "BOOKMARK", "object": {"metadata": {"resourceVersion": "102"}}},
            {"type": "DELETED", "object": deleted},
        ]
    )
    core_v1.list_pod_for_all_namespaces.return_value = resp
    await cache._watch("pods")

    kwargs = core_v1.list_pod_for_all_namespaces.call_args.kwargs
    assert kwargs["watch"] is True and kwargs["resource_version"] == "100" and kwargs["allow_watch_bookmarks"] is True
    assert cache._rv["pods"] == "103"
    assert cache.pods_of("ns", "web") == []
    assert recorder.changes == [(("ns", "web"), True, False), (("ns", "web"), True, False)]
    assert resp.released


@pytest.mark.asyncio
async def test_watch_error_event_raises_410(cache_env):
    cache, core_v1, _, _ = cache_env
    cache._rv["pods"] = "1"
    core_v1.list_pod_for_all_namespaces.return_value = FakeResp(
        lines=[{"type": "ERROR", "object": {"code": 410, "reason": "Expired", "message": "too old"}}]
    )
    with pytest.raises(ApiException) as exc:
        await cache._watch("pods")
    assert exc.value.status == 410


@pytest.mark.asyncio
async def test_watch_skips_lines_that_are_not_events(cache_env):
    cache, core_v1, _, recorder = cache_env
    cache._rv["pods"] = "1"
    core_v1.list_pod_for_all_namespaces.return_value = FakeResp(lines=[{"kind": "PodList", "items": []}])
    await cache._watch("pods")
    assert cache._pods == {} and recorder.changes == []


@pytest.mark.asyncio
async def test_run_relists_after_410(cache_env, monkeypatch):
    cache, _, _, recorder = cache_env
    monkeypatch.setattr(cache, "_list", AsyncMock(return_value=({}, "7")))
    monkeypatch.setattr(cache, "_watch", AsyncMock(side_effect=[ApiException(status=410), asyncio.CancelledError()]))
    with pytest.raises(asyncio.CancelledError):
        await cache._run("pods")
    assert cache._list.await_count == 2
    assert recorder.resyncs == 2


@pytest.mark.asyncio
async def test_run_marks_unsynced_on_error_and_resumes_from_rv(cache_env, monkeypatch, workload_modules):
    cache, _, _, recorder = cache_env
    cache._synced["deployments"] = True
    monkeypatch.setattr(workload_modules.cache.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(cache, "_list", AsyncMock(return_value=({}, "7")))
    monkeypatch.setattr(cache, "_watch", AsyncMock(side_effect=[ConnectionError("reset"), asyncio.CancelledError()]))
    with pytest.raises(asyncio.CancelledError):
        await cache._run("pods")
    assert cache._list.await_count == 1  # 不是 410 就从原来的 rv 续 watch,不重新 list
    assert recorder.synced == [True, False]


def test_index_follows_isolation_and_deletion(cache_env):
    cache, _, _, recorder = cache_env
    pod = make_pod(name="web-7d9f8c6b5-aaaaa")
    cache._apply("pods", "ADDED", pod)
    assert [p["name"] for p in cache.pods_of("ns", "web")] == ["web-7d9f8c6b5-aaaaa"]

    # 隔离:app 标签改成 -ALERT,ReplicaSet 释放掉 ownerReferences,仍归属同一个 deployment
    isolated = make_pod(name="web-7d9f8c6b5-aaaaa", owner=None, labels={"app": "web-ALERT"})
    cache._apply("pods", "MODIFIED", isolated)
    [rec] = cache.pods_of("ns", "web")
    assert rec["isolated"] is True and rec["app_label"] == "web-ALERT"

    # 内容没变的 MODIFIED 不通知
    count = len(recorder.changes)
    cache._apply("pods", "MODIFIED", isolated)
    assert len(recorder.changes) == count

    cache._apply("pods", "DELETED", isolated)
    assert cache.pods_of("ns", "web") == []
    assert ("ns", "web") not in cache._by_dep


def test_deployment_events_and_status_row(cache_env):
    cache, _, _, recorder = cache_env
    cache._apply("deployments", "ADDED", make_deployment(ready=2))
    cache._apply("pods", "ADDED", make_pod(name="web-7d9f8c6b5-aaaaa"))
    row = cache.status_row("ns", "web")
    assert (row["desired"], row["ready"], row["pods"]) == (3, 2, 1)
    assert cache.deployment_keys("ns") == [("ns", "web")]
    assert cache.deployment_keys("other") == []

    cache._apply("deployments", "DELETED", make_deployment())
    assert cache.status_row("ns", "web") is None
    assert recorder.changes[-1] == (("ns", "web"), False, True)


def test_replace_rebuilds_index_and_notifies(cache_env, workload_modules):
    cache, _, _, recorder = cache_env
    rec = workload_modules.status.slim_pod(make_pod(name="web-7d9f8c6b5-aaaaa"))
    cache._replace("pods", {("ns", rec["name"]): rec})
    assert cache.pods_of("ns", "web") == [rec]
    assert recorder.resyncs == 1


def test_synced_requires_both_kinds(cache_env):
    cache, _, _, recorder = cache_env
    cache._set_synced("pods", True)
    assert cache.synced is False and recorder.synced == []
    cache._set_synced("deployments", True)
    assert cache.synced is True and recorder.synced == [True]
