import asyncio
import itertools
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from kubernetes_asyncio.client.rest import ApiException


class FakeResp:
    def __init__(self, status=200, body=None, lines=(), on_eof=None):
        self.status = status
        self._body = json.dumps(body or {}).encode()
        self._lines = [json.dumps(line).encode() + b"\n" for line in lines]
        self._on_eof = on_eof
        self.content = Mock()
        self.content.readline = AsyncMock(side_effect=self._readline)
        self.released = False

    async def _readline(self):
        if self._lines:
            return self._lines.pop(0)
        if self._on_eof:
            self._on_eof()
        return b""

    async def read(self):
        return self._body

    def release(self):
        self.released = True


def make_event(uid, rv, reason="BackOff", level="Warning"):
    return {
        "metadata": {"uid": uid, "resourceVersion": rv, "namespace": "ns", "name": f"{uid}.17a"},
        "involvedObject": {"kind": "Pod", "namespace": "ns", "name": f"pod-{uid}"},
        "type": level,
        "reason": reason,
        "message": "Back-off restarting failed container",
        "count": 3,
        "firstTimestamp": "2026-10-05T12:00:00Z",
        "lastTimestamp": "2026-10-05T12:05:00Z",
        "source": {"component": "kubelet", "host": "node-1"},
    }


def event_list(items, rv, cont=None):
    meta = {"resourceVersion": rv}
    if cont:
        meta["continue"] = cont
    return FakeResp(body={"items": items, "metadata": meta})


def drain(monitor):
    items = []
    while not monitor.event_queue.empty():
        items.append(monitor.event_queue.get_nowait())
    return items


@pytest.fixture
def monitor_env(event_monitor_module):
    core_v1 = Mock()
    core_v1.list_event_for_all_namespaces = AsyncMock()
    return event_monitor_module.K8sEventMonitor(core_v1), core_v1


@pytest.mark.asyncio
async def test_first_list_forwards_everything_and_relist_only_changes(monitor_env):
    monitor, core_v1 = monitor_env
    core_v1.list_event_for_all_namespaces.side_effect = [
        event_list([make_event("a", "1")], "5", cont="tok"),
        event_list([make_event("b", "2")], "5"),
        event_list([make_event("a", "1"), make_event("b", "7"), make_event("c", "8")], "9"),
        event_list([make_event("c", "8")], "12"),
    ]
    await monitor._relist()
    first = drain(monitor)
    assert [(e["eventUid"], e["eventStatus"], rv) for e, rv in first] == [("a", "ADDED", "1"), ("b", "ADDED", "2")]
    assert first[0][0]["k8s"] == "test-cluster" and first[0][0]["reason"] == "BackOff"
    assert monitor._rv == "5"
    paged = core_v1.list_event_for_all_namespaces.call_args_list[1].kwargs
    assert paged["_continue"] == "tok" and paged["limit"] == 500 and paged["_preload_content"] is False
    monitor._mark_sent(first)

    # 410 后重新 list:a 没变不重发,b 有更新,c 是新的
    await monitor._relist()
    second = drain(monitor)
    assert [(e["eventUid"], e["eventStatus"]) for e, _ in second] == [("b", "MODIFIED"), ("c", "ADDED")]
    assert monitor._rv == "9"
    monitor._mark_sent(second)

    # 已经过期删除的事件不再记
    await monitor._relist()
    assert drain(monitor) == []
    assert monitor._sent == {"c": "8"}


@pytest.mark.asyncio
async def test_events_not_delivered_are_resent_by_the_next_list(monitor_env):
    monitor, core_v1 = monitor_env
    core_v1.list_event_for_all_namespaces.side_effect = [event_list([make_event("a", "1")], "5")] * 2
    await monitor._relist()
    drain(monitor)  # 没发出去(master 断开):不记进 _sent
    await monitor._relist()
    assert [e["eventUid"] for e, _ in drain(monitor)] == ["a"]


@pytest.mark.asyncio
async def test_list_error_status_raises(monitor_env):
    monitor, core_v1 = monitor_env
    resp = FakeResp(status=410, body={"message": "continue too old"})
    core_v1.list_event_for_all_namespaces.side_effect = [resp]
    with pytest.raises(ApiException) as info:
        await monitor._relist()
    assert info.value.status == 410 and resp.released


@pytest.mark.asyncio
async def test_watch_forwards_events_and_follows_bookmarks(monitor_env):
    monitor, core_v1 = monitor_env
    monitor._rv = "5"
    resp = FakeResp(lines=[
        {"type": "ADDED", "object": make_event("a", "6")},
        {"type": "MODIFIED", "object": make_event("a", "7")},
        {"type": "DELETED", "object": make_event("a", "8")},
        {"type": "BOOKMARK", "object": {"metadata": {"resourceVersion": "20"}}},
    ])
    core_v1.list_event_for_all_namespaces.side_effect = [resp]
    await monitor._watch()
    assert [(e["eventStatus"], rv) for e, rv in drain(monitor)] == [("ADDED", "6"), ("MODIFIED", "7"), ("DELETED", "8")]
    assert monitor._rv == "20" and resp.released
    kwargs = core_v1.list_event_for_all_namespaces.call_args.kwargs
    assert kwargs["watch"] is True and kwargs["resource_version"] == "5" and kwargs["allow_watch_bookmarks"] is True
    assert kwargs["_preload_content"] is False
    assert 300 <= kwargs["timeout_seconds"] <= 360 and kwargs["_request_timeout"] == (10, kwargs["timeout_seconds"] + 30)


@pytest.mark.asyncio
async def test_watch_error_event_raises_api_exception(monitor_env):
    monitor, core_v1 = monitor_env
    monitor._rv = "5"
    expired = {"kind": "Status", "code": 410, "reason": "Expired", "message": "The resourceVersion for the provided watch is too old."}
    resp = FakeResp(lines=[{"type": "ERROR", "object": expired}])
    core_v1.list_event_for_all_namespaces.side_effect = [resp]
    with pytest.raises(ApiException) as info:
        await monitor._watch()
    assert info.value.status == 410 and resp.released


@pytest.mark.asyncio
async def test_monitor_resumes_watch_and_relists_after_410(monitor_env, event_monitor_module, monkeypatch):
    monitor, core_v1 = monitor_env
    # 每次 watch 都算跑了足够久,不触发"连上即断开"的退避
    monkeypatch.setattr(event_monitor_module, "time", SimpleNamespace(monotonic=itertools.count(step=10).__next__))
    expired = {"kind": "Status", "code": 410, "reason": "Expired", "message": "too old"}

    def stop():
        monitor.is_running = False

    core_v1.list_event_for_all_namespaces.side_effect = [
        event_list([make_event("a", "1")], "10"),
        FakeResp(lines=[{"type": "MODIFIED", "object": make_event("a", "11")}]),  # 服务端到点断开
        FakeResp(lines=[{"type": "ERROR", "object": expired}]),  # 续 watch 时 410
        event_list([make_event("a", "11"), make_event("b", "12")], "12"),
        FakeResp(on_eof=stop),
    ]
    monitor.is_running = True
    await asyncio.wait_for(monitor.monitor_events(), 5)

    calls = core_v1.list_event_for_all_namespaces.call_args_list
    assert [c.kwargs.get("watch", False) for c in calls] == [False, True, True, False, True]
    assert [c.kwargs["resource_version"] for c in calls if c.kwargs.get("watch")] == ["10", "11", "12"]
    assert not monitor.is_running


@pytest.mark.asyncio
async def test_batch_sender_records_only_delivered_events(monitor_env):
    monitor, _ = monitor_env
    sent = []
    ws = SimpleNamespace(closed=False, send_json=AsyncMock(side_effect=lambda message: sent.append(message)))
    monitor.set_websocket_connection(ws)
    monitor.is_running = True
    sender = asyncio.create_task(monitor._batch_send_events())
    try:
        await monitor._forward("ADDED", make_event("a", "1"))
        await monitor._forward("DELETED", make_event("b", "2"))
        monitor._sent["b"] = "1"
        while not sent:
            await asyncio.sleep(0.01)
        assert [e["eventUid"] for e in sent[0]["data"]] == ["a", "b"] and sent[0]["type"] == "k8s_event_batch"
        assert monitor._sent == {"a": "1"} and monitor.event_count == 2

        # master 断开时发不出去,不能记成已发送
        monitor.set_websocket_connection(None)
        await monitor._forward("ADDED", make_event("c", "3"))
        while not monitor.event_queue.empty():
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        assert "c" not in monitor._sent and len(sent) == 1
    finally:
        sender.cancel()
        await asyncio.gather(sender, return_exceptions=True)
