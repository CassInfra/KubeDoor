"""func_manager/pod_exec.py 的单元测试:用假的 K8S exec WebSocket 代替容器,不需要真实集群。"""

import asyncio
import base64
import contextlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import WSMsgType

NOT_FOUND = {
    "status": "Failure",
    "reason": "InternalError",
    "message": 'OCI runtime exec failed: exec failed: unable to start container process: '
               'exec: "sh": executable file not found in $PATH: unknown',
}


class FakeK8sWs:
    """K8S exec WebSocket 的替身:按帧推送容器输出,记录写进容器的帧"""

    def __init__(self):
        self.frames = asyncio.Queue()
        self.sent = []
        self.closed = False
        self.consumed = 0

    def push(self, channel, payload=b""):
        self.frames.put_nowait(SimpleNamespace(type=WSMsgType.BINARY, data=bytes([channel]) + payload))

    def finish(self, status=None):
        if status is not None:
            self.push(3, json.dumps(status).encode())
        self.frames.put_nowait(None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        msg = await self.frames.get()
        if msg is None:
            raise StopAsyncIteration
        self.consumed += 1
        return msg

    async def send_bytes(self, data):
        self.sent.append(data)

    async def close(self):
        self.closed = True


def make_pod(containers=("app",), running=None, annotations=None, phase="Running", init=()):
    running = set(containers) | set(init) if running is None else set(running)

    def status(name):
        up = name in running
        state = SimpleNamespace(running=SimpleNamespace() if up else None,
                                waiting=None if up else SimpleNamespace(reason="CrashLoopBackOff"), terminated=None)
        return SimpleNamespace(name=name, state=state)

    return SimpleNamespace(
        metadata=SimpleNamespace(annotations=annotations),
        spec=SimpleNamespace(containers=[SimpleNamespace(name=n) for n in containers],
                             init_containers=[SimpleNamespace(name=n) for n in init]),
        status=SimpleNamespace(phase=phase, container_statuses=[status(n) for n in containers],
                               init_container_statuses=[status(n) for n in init]),
    )


def make_manager(module, pod, sockets):
    core_v1 = Mock()
    core_v1.read_namespaced_pod = AsyncMock(return_value=pod)
    manager = module.PodExecManager(core_v1)
    manager.api_client = contextlib.nullcontext
    opened = []

    async def open_exec(api_client, namespace, pod_name, container, command):
        opened.append((namespace, pod_name, container, command))
        return sockets.pop(0)

    manager.open_exec = open_exec
    return manager, opened


def master_ws():
    ws = Mock()
    ws.send_json = AsyncMock()
    return ws


def events(ws):
    return [call.args[0] for call in ws.send_json.call_args_list]


def output_of(ws):
    return b"".join(base64.b64decode(e["data"]) for e in events(ws) if e["event"] == "output")


def start(session_id="s1", **fields):
    return {"type": "pod_exec", "action": "start", "session_id": session_id, "namespace": "ns",
            "pod_name": "web-1", "container": "", "shell": "auto", "cols": 120, "rows": 40, **fields}


def action(name, session_id="s1", **fields):
    return {"type": "pod_exec", "action": name, "session_id": session_id, **fields}


async def until(predicate, timeout=1.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("等待超时")
        await asyncio.sleep(0.001)


def exited(ws):
    return lambda: bool(events(ws)) and events(ws)[-1]["event"] == "exit"


def test_auto_shell_execs_first_available_shell(pod_exec_module):
    commands = pod_exec_module.shell_commands("auto")
    assert commands[1] == ["bash"]  # 没有 sh 时直接试 bash
    assert commands[0][:2] == ["sh", "-c"]
    script = commands[0][2]
    assert script == ("export TERM=xterm-256color; command -v bash >/dev/null 2>&1 && exec bash; "
                      "command -v ash >/dev/null 2>&1 && exec ash; exec sh")


def test_explicit_shell_commands(pod_exec_module):
    assert pod_exec_module.shell_commands("sh") == [["sh", "-c", "export TERM=xterm-256color; exec sh"]]
    assert pod_exec_module.shell_commands("ash") == [["sh", "-c", "export TERM=xterm-256color; exec ash"], ["ash"]]


def test_pick_container_prefers_annotation_then_skips_sidecars(pod_exec_module):
    pick = pod_exec_module.pick_container
    pod = make_pod(("istio-proxy", "app", "worker"))
    assert pick(pod) == ("app", ["istio-proxy", "app", "worker"])
    pod.metadata.annotations = {"kubectl.kubernetes.io/default-container": "worker"}
    assert pick(pod)[0] == "worker"
    assert pick(pod, "istio-proxy")[0] == "istio-proxy"
    assert pick(make_pod(("istio-proxy",)))[0] == "istio-proxy"  # 只有 sidecar 就进 sidecar


def test_pick_container_rejects_unusable_targets(pod_exec_module):
    pick, error = pod_exec_module.pick_container, pod_exec_module.ExecError
    with pytest.raises(error, match="容器 nope 不存在"):
        pick(make_pod(), "nope")
    with pytest.raises(error, match=r"容器 app 没有在运行\(CrashLoopBackOff\)"):
        pick(make_pod(running=()))
    with pytest.raises(error, match="Pod 已经结束"):
        pick(make_pod(phase="Succeeded"))
    # 正在运行的 init 容器可以进
    assert pick(make_pod(init=("setup",)), "setup")[0] == "setup"


def test_parse_exit_status(pod_exec_module):
    parse = pod_exec_module.parse_exit_status
    assert parse(None) == (None, "")
    assert parse(b'{"metadata":{},"status":"Success"}') == (0, "")
    non_zero = {"status": "Failure", "reason": "NonZeroExitCode", "message": "command terminated with non-zero exit code",
                "details": {"causes": [{"reason": "ExitCode", "message": "130"}]}}
    assert parse(json.dumps(non_zero).encode()) == (130, "")
    assert parse(json.dumps(NOT_FOUND).encode()) == (None, NOT_FOUND["message"])
    assert pod_exec_module.shell_missing(NOT_FOUND["message"])


@pytest.mark.asyncio
async def test_session_relays_output_then_exit(pod_exec_module):
    m = pod_exec_module
    k8s = FakeK8sWs()
    manager, opened = make_manager(m, make_pod(), [k8s])
    master = master_ws()
    manager.handle(master, start())
    await until(lambda: k8s.sent)
    assert k8s.sent[0] == m.resize_frame(120, 40)  # 先发终端尺寸
    text = "你好".encode()
    k8s.push(m.STDOUT_CHANNEL, text[:4])  # 截在 UTF-8 字符中间也原样转发
    k8s.push(m.STDOUT_CHANNEL, text[4:])
    k8s.finish({"status": "Success"})
    await until(exited(master))

    got = events(master)
    assert [e["event"] for e in got[:2]] == ["accepted", "started"]
    assert got[1] == {"type": "pod_exec", "event": "started", "session_id": "s1", "container": "app",
                      "containers": ["app"]}
    assert output_of(master).decode() == "你好"
    assert got[-1] == {"type": "pod_exec", "event": "exit", "session_id": "s1", "code": 0, "message": ""}
    assert opened == [("ns", "web-1", "app", m.shell_commands("auto")[0])]
    assert k8s.closed and "s1" not in manager.sessions


@pytest.mark.asyncio
async def test_input_before_connect_is_written_after_initial_size(pod_exec_module):
    m = pod_exec_module
    k8s = FakeK8sWs()
    manager, _ = make_manager(m, make_pod(), [k8s])
    master = master_ws()
    manager.handle(master, start())
    # 连上之前就到的输入和尺寸:尺寸以最新的为准,输入排在尺寸后面
    manager.handle(master, action("resize", cols=100, rows=30))
    manager.handle(master, action("stdin", data="ls\r"))
    manager.handle(master, action("stdin", data="\x1b[M\xff", binary=True))
    await until(lambda: len(k8s.sent) == 3)
    manager.handle(master, action("resize", cols=90, rows=20))
    manager.handle(master, action("resize", cols=0, rows=20))  # 非法尺寸忽略
    await until(lambda: len(k8s.sent) == 4)
    assert k8s.sent == [m.resize_frame(100, 30), b"\x00ls\r", b"\x00\x1b[M\xff", m.resize_frame(90, 20)]
    k8s.finish()
    await until(exited(master))
    assert events(master)[-1]["code"] is None  # 没收到状态(连接被断开)


@pytest.mark.asyncio
async def test_output_pauses_until_browser_acks(pod_exec_module, monkeypatch):
    m = pod_exec_module
    monkeypatch.setattr(m, "WINDOW", 10)
    k8s = FakeK8sWs()
    manager, _ = make_manager(m, make_pod(), [k8s])
    master = master_ws()
    manager.handle(master, start())
    for chunk in (b"12345", b"67890", b"abcde"):
        k8s.push(m.STDOUT_CHANNEL, chunk)
    await until(lambda: len(output_of(master)) == 10)
    await asyncio.sleep(0.02)
    assert k8s.consumed == 2  # 未确认 10 字节,到窗口了,第三帧不读
    manager.handle(master, action("ack", bytes=5))
    await until(lambda: output_of(master) == b"1234567890abcde")
    k8s.finish({"status": "Success"})
    await asyncio.sleep(0.02)
    assert not exited(master)()  # 又到窗口了
    manager.handle(master, action("ack", bytes=10))
    await until(exited(master))


@pytest.mark.asyncio
async def test_falls_back_to_bash_when_sh_missing(pod_exec_module):
    m = pod_exec_module
    first, second = FakeK8sWs(), FakeK8sWs()
    first.finish(NOT_FOUND)
    second.finish({"status": "Success"})
    manager, opened = make_manager(m, make_pod(), [first, second])
    master = master_ws()
    manager.handle(master, start())
    await until(exited(master))
    assert [o[3] for o in opened] == [m.shell_commands("auto")[0], ["bash"]]
    assert [e["event"] for e in events(master)] == ["accepted", "started", "exit"]
    assert events(master)[-1]["code"] == 0


@pytest.mark.asyncio
async def test_reports_when_container_has_no_shell(pod_exec_module):
    m = pod_exec_module
    first, second = FakeK8sWs(), FakeK8sWs()
    first.finish(NOT_FOUND)
    second.finish(dict(NOT_FOUND, message='exec: "bash": executable file not found in $PATH'))
    manager, _ = make_manager(m, make_pod(), [first, second])
    master = master_ws()
    manager.handle(master, start())
    await until(exited(master))
    assert events(master)[-1]["message"] == m.NO_SHELL_MESSAGE.format("、".join(m.SHELLS))


@pytest.mark.asyncio
async def test_precheck_error_is_reported_without_exec(pod_exec_module):
    m = pod_exec_module
    manager, opened = make_manager(m, make_pod(running=()), [])
    master = master_ws()
    manager.handle(master, start())
    await until(exited(master))
    assert events(master)[-1]["message"] == "容器 app 没有在运行(CrashLoopBackOff)"
    assert opened == []


@pytest.mark.asyncio
async def test_stop_and_disconnect_close_sessions_quietly(pod_exec_module):
    m = pod_exec_module
    first, second = FakeK8sWs(), FakeK8sWs()
    manager, _ = make_manager(m, make_pod(), [first, second])
    master = master_ws()
    manager.handle(master, start("s1"))
    manager.handle(master, start("s2"))
    await until(lambda: first.sent and second.sent)
    manager.handle(master, action("stop", "s1"))
    await until(lambda: "s1" not in manager.sessions)
    manager.close_all()
    await until(lambda: not manager.sessions)
    assert first.closed and second.closed
    assert "exit" not in [e["event"] for e in events(master)]  # 浏览器已经不在了,不用回


@pytest.mark.asyncio
async def test_start_rejects_bad_requests(pod_exec_module, monkeypatch):
    m = pod_exec_module
    manager, opened = make_manager(m, make_pod(), [])
    master = master_ws()
    manager.handle(master, start("a", shell="zsh"))
    manager.handle(master, start("b", pod_name=""))
    monkeypatch.setattr(m, "MAX_SESSIONS", 0)
    manager.handle(master, start("c"))
    await until(lambda: len(events(master)) == 3)
    assert {e["session_id"]: e["message"] for e in events(master)} == {
        "a": "不支持的 shell: zsh", "b": "参数错误", "c": "agent 上同时打开的终端已达上限(0 个)"}
    assert opened == [] and manager.sessions == {}


# ---------------------------------------------------------------- 节点终端(kind=node)
def make_node_manager(m, sockets, pod=None):
    core_v1 = Mock()
    core_v1.create_namespaced_pod = AsyncMock()
    core_v1.delete_namespaced_pod = AsyncMock()
    core_v1.read_namespaced_pod = AsyncMock(return_value=pod if pod is not None else make_pod(("shell",)))
    manager = m.PodExecManager(core_v1)
    manager.api_client = contextlib.nullcontext
    opened = []

    async def open_exec(api_client, namespace, pod_name, container, command):
        opened.append((namespace, pod_name, container, command))
        return sockets.pop(0)

    manager.open_exec = open_exec
    return manager, core_v1, opened


def node_start(session_id="n1", **fields):
    return {"type": "pod_exec", "action": "start", "kind": "node", "session_id": session_id,
            "node_name": "10.0.0.1", "shell": "auto", "cols": 100, "rows": 30, **fields}


def waiting_pod(reason):
    state = SimpleNamespace(running=None, waiting=SimpleNamespace(reason=reason), terminated=None)
    return SimpleNamespace(status=SimpleNamespace(phase="Pending",
                           container_statuses=[SimpleNamespace(name="shell", state=state)]))


def test_node_shell_commands_prefix_nsenter(pod_exec_module):
    m = pod_exec_module
    cmds = m.node_shell_commands("auto")
    n = len(m.NSENTER_PREFIX)
    assert cmds[0][:n] == m.NSENTER_PREFIX
    assert cmds[0][n:] == m.shell_commands("auto")[0]
    assert cmds[-1] == m.NSENTER_PREFIX + ["bash"]


@pytest.mark.asyncio
async def test_node_shell_creates_pod_execs_nsenter_and_deletes(pod_exec_module):
    m = pod_exec_module
    k8s = FakeK8sWs()
    manager, core_v1, opened = make_node_manager(m, [k8s])
    master = master_ws()
    manager.handle(master, node_start())
    await until(lambda: opened)

    # 建了一个钉在目标节点、privileged + hostPID 的临时 pod
    core_v1.create_namespaced_pod.assert_awaited_once()
    ns_arg, manifest = core_v1.create_namespaced_pod.await_args.args[:2]
    assert ns_arg == m.NODE_SHELL_NAMESPACE
    assert manifest.spec.node_name == "10.0.0.1"
    assert manifest.spec.host_pid and manifest.spec.host_network
    assert manifest.spec.containers[0].security_context.privileged is True
    assert manifest.spec.containers[0].command == ["sleep", str(m.NODE_SHELL_TTL)]
    assert manifest.spec.containers[0].image == m.DEFAULT_NODE_SHELL_IMAGE  # master 没配镜像,用 agent 内置默认
    assert manifest.spec.tolerations[0].operator == "Exists"

    # exec 命令带 nsenter 前缀,进入临时 pod 的 shell 容器
    namespace, pod_name, container, command = opened[0]
    assert namespace == m.NODE_SHELL_NAMESPACE and container == m.NODE_SHELL_CONTAINER
    assert pod_name.startswith(m.NODE_SHELL_LABEL)
    assert command == m.NSENTER_PREFIX + m.shell_commands("auto")[0]

    started = next(e for e in events(master) if e["event"] == "started")
    assert started["container"] == "shell" and started["containers"] == ["shell"]
    assert any(e["event"] == "output" for e in events(master))  # 创建中的进度提示

    k8s.finish({"status": "Success"})
    await until(exited(master))
    await until(lambda: core_v1.delete_namespaced_pod.await_count == 1)  # 退出后删掉临时 pod
    assert core_v1.delete_namespaced_pod.await_args.args[:2] == (pod_name, m.NODE_SHELL_NAMESPACE)
    assert manager.sessions == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("image,expected", [
    ("registry.local/ops/alpine:3", "registry.local/ops/alpine:3"),  # master 配置的镜像随 start 消息下发
    ("  registry.local/ops/alpine:3 ", "registry.local/ops/alpine:3"),
    ("", None), ("   ", None), (123, None), (None, None), ("x" * 513, None),  # 不合法的一律回落到内置默认
])
async def test_node_shell_image_from_master(pod_exec_module, image, expected):
    m = pod_exec_module
    k8s = FakeK8sWs()
    manager, core_v1, opened = make_node_manager(m, [k8s])
    master = master_ws()
    manager.handle(master, node_start(image=image))
    await until(lambda: opened)
    manifest = core_v1.create_namespaced_pod.await_args.args[1]
    assert manifest.spec.containers[0].image == (expected or m.DEFAULT_NODE_SHELL_IMAGE)
    k8s.finish({"status": "Success"})
    await until(exited(master))


@pytest.mark.asyncio
async def test_node_shell_image_pull_failure_reports_and_cleans_up(pod_exec_module):
    m = pod_exec_module
    manager, core_v1, opened = make_node_manager(m, [], pod=waiting_pod("ImagePullBackOff"))
    master = master_ws()
    manager.handle(master, node_start(image="registry.local/ops/alpine:3"))
    await until(exited(master))
    message = events(master)[-1]["message"]
    assert "ImagePullBackOff" in message
    assert "registry.local/ops/alpine:3" in message and "NODE_SHELL_IMAGE" in message  # 提示拉的哪个镜像、去哪改
    assert opened == []  # 没就绪就不会去 exec
    await until(lambda: core_v1.delete_namespaced_pod.await_count == 1)  # 建了就要删


@pytest.mark.asyncio
async def test_node_shell_stop_deletes_pod(pod_exec_module):
    m = pod_exec_module
    k8s = FakeK8sWs()
    manager, core_v1, opened = make_node_manager(m, [k8s])
    master = master_ws()
    manager.handle(master, node_start())
    await until(lambda: opened)
    manager.handle(master, action("stop", "n1"))
    await until(lambda: "n1" not in manager.sessions)
    await until(lambda: core_v1.delete_namespaced_pod.await_count == 1)


@pytest.mark.asyncio
async def test_node_start_requires_node_name(pod_exec_module):
    m = pod_exec_module
    manager, core_v1, opened = make_node_manager(m, [])
    master = master_ws()
    manager.handle(master, node_start(node_name=""))
    await until(exited(master))
    assert events(master)[-1]["message"] == "参数错误"
    core_v1.create_namespaced_pod.assert_not_awaited()


@pytest.mark.asyncio
async def test_cleanup_orphans_deletes_labeled_pods(pod_exec_module):
    m = pod_exec_module
    core_v1 = Mock()
    items = [SimpleNamespace(metadata=SimpleNamespace(name=f"{m.NODE_SHELL_LABEL}-{s}")) for s in ("aaa", "bbb")]
    core_v1.list_namespaced_pod = AsyncMock(return_value=SimpleNamespace(items=items))
    core_v1.delete_namespaced_pod = AsyncMock()
    manager = m.PodExecManager(core_v1)
    await manager.cleanup_orphans()
    assert core_v1.list_namespaced_pod.await_args.kwargs["label_selector"] == f"app={m.NODE_SHELL_LABEL}"
    assert core_v1.delete_namespaced_pod.await_count == 2
