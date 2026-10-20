"""func_manager/pod_log_stream.py:Pod 实时日志流的生命周期、连接池 / 上限、按块转发"""

import asyncio
import json
from unittest.mock import AsyncMock, Mock

import pytest

START = {"connection_id": "c1", "namespace": "ns", "pod_name": "web-1", "container": ""}


class FakeContent:
    def __init__(self, chunks, block):
        self.chunks = list(chunks)
        self.block = block

    def iter_any(self):
        return self._gen()

    async def _gen(self):
        for chunk in self.chunks:
            yield chunk
            await asyncio.sleep(0)
        if self.block:  # follow:发完已有日志后一直挂着等新日志
            await asyncio.Event().wait()


class FakeResponse:
    def __init__(self, chunks=(), status=200, body=b"", block=False):
        self.status = status
        self.body = body
        self.content = FakeContent(chunks, block)
        self.closed = False

    async def read(self):
        return self.body

    def close(self):
        self.closed = True


class FakeLogApi:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    async def read_namespaced_pod_log(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


def fake_ws():
    ws = Mock()
    ws.closed = False
    ws.send_json = AsyncMock()
    return ws


def sent(ws):
    return [call.args[0] for call in ws.send_json.call_args_list]


def log_msg(text, connection_id="c1"):
    return {"type": "pod_logs", "connection_id": connection_id, "pod_name": "web-1", "container": "", "log": text}


async def until(predicate, timeout=2.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("等待超时")
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_streams_complete_lines_per_chunk_and_flushes_tail(pod_log_stream_module):
    module = pod_log_stream_module
    resp = FakeResponse([b"a\nb", b"c\n\n  d  \n", b"tail"])
    api = FakeLogApi(resp)
    streamer = module.PodLogStreamer(api)
    ws = fake_ws()
    streamer.start(ws, START)
    await until(lambda: not streamer.tasks)
    # 每块里的完整行立即发出(不用凑满 N 行),跨块的半行接上,最后没有换行的一段在结束时发出
    assert sent(ws) == [
        {"type": "pod_logs", "connection_id": "c1", "status": "connected"},
        log_msg("a"),
        log_msg("bc\nd"),
        log_msg("tail"),
    ]
    assert resp.closed
    [call] = api.calls
    assert call["follow"] is True and call["_preload_content"] is False and call["container"] is None
    assert call["tail_lines"] == module.TAIL_LINES
    assert call["_request_timeout"] is module.OPEN_TIMEOUT  # 只限制建连,不限制 follow 时长


@pytest.mark.asyncio
async def test_error_status_reports_apiserver_message(pod_log_stream_module):
    body = json.dumps({"kind": "Status", "message": 'container "x" is not valid for pod "web-1"', "code": 400})
    resp = FakeResponse(status=400, body=body.encode())
    streamer = pod_log_stream_module.PodLogStreamer(FakeLogApi(resp))
    ws = fake_ws()
    streamer.start(ws, START)
    await until(lambda: not streamer.tasks)
    assert sent(ws)[-1] == {"type": "pod_logs", "connection_id": "c1",
                            "error": 'Kubernetes API错误: 400 - container "x" is not valid for pod "web-1"'}
    assert resp.closed


@pytest.mark.asyncio
async def test_stream_limit_rejects_new_streams(pod_log_stream_module, monkeypatch):
    monkeypatch.setattr(pod_log_stream_module, "MAX_STREAMS", 1)
    first = FakeResponse(block=True)
    streamer = pod_log_stream_module.PodLogStreamer(FakeLogApi(first))
    ws = fake_ws()
    streamer.start(ws, START)
    streamer.start(ws, dict(START, connection_id="c2"))
    await until(lambda: any("error" in m for m in sent(ws)))
    assert {"type": "pod_logs", "connection_id": "c2",
            "error": "agent 上同时打开的日志流已达上限(1 条),请关闭不用的日志窗口后重试"} in sent(ws)
    assert list(streamer.tasks) == ["c1"]
    streamer.close_all()
    await until(lambda: first.closed)


@pytest.mark.asyncio
async def test_same_connection_id_replaces_old_stream(pod_log_stream_module):
    old, new = FakeResponse(block=True), FakeResponse(block=True)
    streamer = pod_log_stream_module.PodLogStreamer(FakeLogApi(old, new))
    ws = fake_ws()
    streamer.start(ws, START)
    await until(lambda: len(sent(ws)) == 1)
    old_task = streamer.tasks["c1"]
    streamer.start(ws, START)  # 同一个 id 再次 start:旧的必须停掉,否则再也停不掉
    await until(lambda: old_task.done() and old.closed)
    assert old_task.cancelled()
    assert streamer.tasks["c1"] is not old_task
    streamer.stop("c1")
    await until(lambda: new.closed and not streamer.tasks)


@pytest.mark.asyncio
async def test_stop_and_close_all_cancel_streams_and_release_connections(pod_log_stream_module):
    first, second = FakeResponse(block=True), FakeResponse(block=True)
    streamer = pod_log_stream_module.PodLogStreamer(FakeLogApi(first, second))
    ws = fake_ws()
    streamer.start(ws, START)
    streamer.start(ws, dict(START, connection_id="c2"))
    await until(lambda: len(sent(ws)) == 2)
    streamer.stop("c1")
    streamer.stop("unknown")
    await until(lambda: first.closed)
    assert list(streamer.tasks) == ["c2"]
    streamer.close_all()  # 和 master 断线时调用
    await until(lambda: second.closed)
    assert streamer.tasks == {}


@pytest.mark.asyncio
async def test_send_failure_ends_stream(pod_log_stream_module):
    resp = FakeResponse([b"a\n", b"b\n"], block=True)
    streamer = pod_log_stream_module.PodLogStreamer(FakeLogApi(resp))
    ws = fake_ws()
    calls = []

    async def send_json(message):
        calls.append(message)
        if len(calls) > 1:
            raise ConnectionResetError("与 master 的连接已断开")

    ws.send_json = send_json
    streamer.start(ws, START)
    await until(lambda: not streamer.tasks)  # 发不出去就结束,不会一直占着连接
    assert resp.closed


@pytest.mark.asyncio
async def test_large_batches_split_and_unterminated_line_capped(pod_log_stream_module, monkeypatch):
    module = pod_log_stream_module
    monkeypatch.setattr(module, "MAX_MESSAGE_CHARS", 10)
    monkeypatch.setattr(module, "MAX_LINE_BYTES", 8)
    resp = FakeResponse([b"12345\n67890\nabcde\n", b"x" * 20])
    streamer = module.PodLogStreamer(FakeLogApi(resp))
    ws = fake_ws()
    streamer.start(ws, START)
    await until(lambda: not streamer.tasks)
    assert sent(ws)[1:] == [log_msg("12345\n67890"), log_msg("abcde"), log_msg("x" * 20)]


@pytest.mark.asyncio
async def test_invalid_start_is_rejected(pod_log_stream_module):
    streamer = pod_log_stream_module.PodLogStreamer(FakeLogApi())
    ws = fake_ws()
    streamer.start(ws, {"connection_id": "c1", "namespace": "ns"})
    streamer.start(ws, {"namespace": "ns", "pod_name": "web-1"})  # 没有 connection_id 直接忽略
    await until(lambda: sent(ws))
    assert sent(ws) == [{"type": "pod_logs", "connection_id": "c1", "error": "参数错误"}]
    assert streamer.tasks == {}
