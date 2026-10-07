from unittest.mock import AsyncMock, Mock

import pytest

from test_workload_cache import make_deployment
from test_workload_status import make_pod


@pytest.fixture
def env(workload_modules):
    core_v1, apps_v1 = Mock(), Mock()
    cache = workload_modules.cache.WorkloadCache(core_v1, apps_v1)
    cache._synced = {"pods": True, "deployments": True}
    streamer = workload_modules.streamer.WorkloadStreamer(cache)
    ws = Mock()
    ws.closed = False
    ws.send_json = AsyncMock()
    streamer.attach(ws)
    return cache, streamer, ws


def sent(ws):
    return [call.args[0] for call in ws.send_json.call_args_list]


def seed(cache, deployments=(("ns", "web"),)):
    for namespace, name in deployments:
        cache._apply("deployments", "ADDED", make_deployment(name=name, namespace=namespace))
        cache._apply("pods", "ADDED", make_pod(name=f"{name}-7d9f8c6b5-aaaaa", namespace=namespace, owner=f"{name}-7d9f8c6b5"))


@pytest.mark.asyncio
async def test_new_subscription_gets_snapshot_with_pods(env):
    cache, streamer, ws = env
    seed(cache)
    streamer.subscribe(ws, {"sub_id": "s1", "namespace": "", "pods_for": [["ns", "web"]]})
    await streamer.flush()
    [msg] = sent(ws)
    assert msg["type"] == "workload_snapshot" and msg["sub_id"] == "s1"
    assert msg["synced"] and msg["first"] and msg["final"]
    assert [r["deployment"] for r in msg["deployments"]] == ["web"]
    [pods] = msg["pods"]
    assert (pods["namespace"], pods["deployment"], pods["truncated"]) == ("ns", "web", False)
    assert [p["name"] for p in pods["items"]] == ["web-7d9f8c6b5-aaaaa"]
    assert "cpu" not in pods["items"][0]  # 推送不带指标,页面保留上次的值


@pytest.mark.asyncio
async def test_update_only_carries_changed_rows(env):
    cache, streamer, ws = env
    seed(cache, [("ns", "web"), ("ns", "api")])
    streamer.subscribe(ws, {"sub_id": "s1", "namespace": "", "pods_for": [["ns", "web"]]})
    await streamer.flush()
    ws.send_json.reset_mock()

    # web 的 pod 被删 -> 状态行变化 + pod 列表推送
    cache._apply("pods", "MODIFIED", make_pod(name="web-7d9f8c6b5-aaaaa", deleting=True))
    await streamer.flush()
    [msg] = sent(ws)
    assert msg["type"] == "workload_update"
    assert [(r["deployment"], r["terminating"], r["state"]) for r in msg["deployments"]] == [("web", 1, "progressing")]
    assert msg["pods"][0]["items"][0]["status"] == "Terminating"

    # api 的 pod 换了 IP:快照不记入"已推过"(快照期间可能有未 flush 的变化),第一次照推一行;
    # 再换一次 IP 时状态行和上次推的一样,也没人关注它的 pod,就什么都不推
    pod = make_pod(name="api-7d9f8c6b5-aaaaa", owner="api-7d9f8c6b5")
    pod["status"]["podIP"] = "10.0.0.9"
    cache._apply("pods", "MODIFIED", pod)
    await streamer.flush()
    ws.send_json.reset_mock()
    pod["status"]["podIP"] = "10.0.0.10"
    cache._apply("pods", "MODIFIED", pod)
    await streamer.flush()
    assert sent(ws) == []


@pytest.mark.asyncio
async def test_namespace_filter_and_removed(env):
    cache, streamer, ws = env
    seed(cache, [("ns", "web"), ("other", "db")])
    streamer.subscribe(ws, {"sub_id": "s1", "namespace": "ns", "pods_for": []})
    await streamer.flush()
    assert [r["deployment"] for r in sent(ws)[0]["deployments"]] == ["web"]
    ws.send_json.reset_mock()

    cache._apply("deployments", "MODIFIED", make_deployment(name="db", namespace="other", ready=1))
    await streamer.flush()
    assert sent(ws) == []

    cache._apply("deployments", "DELETED", make_deployment(name="web", namespace="ns"))
    await streamer.flush()
    [msg] = sent(ws)
    assert msg["removed"] == [["ns", "web"]] and msg["deployments"] == []


@pytest.mark.asyncio
async def test_watch_pods_added_later_sends_pod_list(env):
    cache, streamer, ws = env
    seed(cache)
    streamer.subscribe(ws, {"sub_id": "s1", "namespace": "", "pods_for": []})
    await streamer.flush()
    ws.send_json.reset_mock()
    streamer.subscribe(ws, {"sub_id": "s1", "namespace": "", "pods_for": [["ns", "web"]]})
    await streamer.flush()
    [msg] = sent(ws)
    assert msg["type"] == "workload_update" and msg["pods"][0]["deployment"] == "web"


@pytest.mark.asyncio
async def test_snapshot_is_chunked(env, workload_modules, monkeypatch):
    cache, streamer, ws = env
    monkeypatch.setattr(workload_modules.streamer, "SNAPSHOT_CHUNK", 2)
    seed(cache, [("ns", f"svc{i}") for i in range(5)])
    streamer.subscribe(ws, {"sub_id": "s1", "namespace": "", "pods_for": [["ns", "svc0"]]})
    await streamer.flush()
    msgs = sent(ws)
    assert [len(m["deployments"]) for m in msgs] == [2, 2, 1]
    assert [(m["first"], m["final"]) for m in msgs] == [(True, False), (False, False), (False, True)]
    assert [len(m["pods"]) for m in msgs] == [0, 0, 1]


@pytest.mark.asyncio
async def test_unsynced_snapshot_then_full_after_sync(env):
    cache, streamer, ws = env
    seed(cache)
    cache._synced["pods"] = False
    streamer.subscribe(ws, {"sub_id": "s1", "namespace": "", "pods_for": []})
    await streamer.flush()
    [msg] = sent(ws)
    assert msg["synced"] is False and msg["deployments"] == []

    ws.send_json.reset_mock()
    cache._set_synced("pods", True)
    await streamer.flush()
    [msg] = sent(ws)
    assert msg["type"] == "workload_snapshot" and msg["synced"] and len(msg["deployments"]) == 1


@pytest.mark.asyncio
async def test_resync_after_relist_sends_full_snapshot(env):
    cache, streamer, ws = env
    seed(cache)
    streamer.subscribe(ws, {"sub_id": "s1", "namespace": "", "pods_for": []})
    await streamer.flush()
    ws.send_json.reset_mock()
    cache._replace("deployments", dict(cache._deps))
    await streamer.flush()
    assert sent(ws)[0]["type"] == "workload_snapshot"


@pytest.mark.asyncio
async def test_change_without_subscribers_is_not_lost(env):
    """没人订阅期间的变化不能让之后的增量判断出错(上次推过的值要作废)"""
    cache, streamer, ws = env
    seed(cache)
    streamer.subscribe(ws, {"sub_id": "s1", "namespace": "", "pods_for": []})
    await streamer.flush()
    streamer.unsubscribe("s1")
    cache._apply("deployments", "MODIFIED", make_deployment(ready=1))  # 3 -> 1,没人订阅
    streamer.subscribe(ws, {"sub_id": "s2", "namespace": "", "pods_for": []})
    await streamer.flush()  # s2 的快照里 ready=1
    ws.send_json.reset_mock()
    cache._apply("deployments", "MODIFIED", make_deployment(ready=3))  # 改回 3,必须推给 s2
    await streamer.flush()
    [msg] = sent(ws)
    assert msg["deployments"][0]["ready"] == 3


@pytest.mark.asyncio
async def test_attach_resets_and_stale_ws_ignored(env):
    cache, streamer, ws = env
    seed(cache)
    streamer.subscribe(ws, {"sub_id": "s1", "namespace": "", "pods_for": []})
    new_ws = Mock(closed=False, send_json=AsyncMock())
    streamer.attach(new_ws)
    streamer.subscribe(ws, {"sub_id": "old", "namespace": "", "pods_for": []})  # 旧连接上的消息
    await streamer.flush()
    assert sent(new_ws) == [] and sent(ws) == []
    streamer.subscribe(new_ws, {"sub_id": "s2", "namespace": "", "pods_for": []})
    await streamer.flush()
    assert [m["sub_id"] for m in sent(new_ws)] == ["s2"]


@pytest.mark.asyncio
async def test_limits(env, workload_modules, monkeypatch):
    cache, streamer, ws = env
    monkeypatch.setattr(workload_modules.streamer, "MAX_SUBS", 2)
    monkeypatch.setattr(workload_modules.streamer, "MAX_PODS_FOR", 2)
    for sub_id in ("a", "b", "c"):
        streamer.subscribe(ws, {"sub_id": sub_id, "namespace": "", "pods_for": [["ns", f"d{i}"] for i in range(5)]})
    assert list(streamer._subs) == ["b", "c"]
    assert len(streamer._subs["c"].pods_for) == 2
    streamer.subscribe(ws, {"sub_id": "bad", "namespace": "", "pods_for": [["ns"], [1, 2], "x"]})
    assert streamer._subs["bad"].pods_for == set()


@pytest.mark.asyncio
async def test_closed_ws_skips_sending(env):
    cache, streamer, ws = env
    seed(cache)
    streamer.subscribe(ws, {"sub_id": "s1", "namespace": "", "pods_for": []})
    ws.closed = True
    await streamer.flush()
    assert sent(ws) == []
