"""
Pod / 节点终端(web shell):master 转来的会话,在 agent 上用 K8S exec(WebSocket,v4.channel.k8s.io)打开带 tty 的 shell

master → agent:{"type": "pod_exec", "action": "start" | "stdin" | "resize" | "ack" | "stop", "session_id", ...}
                start 带 "kind": "pod"(默认)或 "node"
agent → master:{"type": "pod_exec", "event": "accepted" | "started" | "output" | "exit", "session_id", ...}

- 输出按原始字节 base64 后放进 JSON(终端输出可能截在半个 UTF-8 字符上,也可能是二进制);
- 端到端流控:浏览器在 xterm 真正写完后按字节 ack,已发出未确认的字节超过 WINDOW 就暂停读容器输出,
  TCP 背压一路传回容器里的进程。否则 `cat 大文件` / `yes` 会塞满 agent 与 master 之间这条共用连接
  (心跳、K8S 事件、接口回包都走它);
- handle() 在 agent 的接收循环里同步调用,不 await:stdin / resize 进会话队列,由会话自己的任务写给容器;
- 自动识别 shell:sh -c 里依次找 bash、ash,找到就 exec 替换进程。不用 `bash || ash || sh`:
  bash 以非 0 退出时会接着再起一个 shell,要 exit 好几次;容器里没有 sh 时再直接试 bash;
- 节点终端(kind=node)同 Lens:在 kubedoor 命名空间按 nodeName 建一个 privileged + hostPID 的临时 pod,
  用 nsenter 进宿主机 1 号进程的命名空间拿到宿主机 shell;会话结束删除该 pod,agent 重连时清理残留。
"""

import asyncio
import base64
import json
import os
import uuid

from aiohttp import WSMsgType, WSServerHandshakeError
from kubernetes_asyncio import client
from kubernetes_asyncio.client.rest import ApiException
from kubernetes_asyncio.stream import WsApiClient
from loguru import logger

STDIN_CHANNEL = 0
STDOUT_CHANNEL = 1
STDERR_CHANNEL = 2
ERROR_CHANNEL = 3
RESIZE_CHANNEL = 4

WINDOW = 256 * 1024  # 已发出、浏览器还没确认的输出字节上限(小一点 Ctrl+C 后残留的输出少)
MAX_FRAME = 32 * 1024  # 单条 output 消息最多带的原始字节
INBOX_SIZE = 1024  # 待写给容器的 stdin / resize 帧
MAX_SESSIONS = 50  # 与 master 每个集群的上限一致
CONNECT_TIMEOUT = 15
MAX_COLS, MAX_ROWS = 1000, 500
SHELLS = ("bash", "ash", "sh")  # 自动识别时的优先顺序
SHELL_CHOICES = ("auto",) + SHELLS
TERM = "xterm-256color"
DEFAULT_CONTAINER_ANNOTATION = "kubectl.kubernetes.io/default-container"
SIDECAR_HINTS = ("istio-proxy", "linkerd-proxy", "envoy", "sidecar")
_SHELL_MISSING_HINTS = ("executable file not found", "no such file or directory")
NO_SHELL_MESSAGE = "找不到可用的 shell(试过 {}),可能是 distroless 之类的精简镜像,或节点镜像缺少 nsenter"

# 节点终端:临时 pod 的命名空间、容器名、镜像、存活时长(存活到期后容器自己退出,作为 agent 异常未清理时的兜底)
NODE_SHELL_NAMESPACE = "kubedoor"
NODE_SHELL_CONTAINER = "shell"
NODE_SHELL_LABEL = "kubedoor-node-shell"
NODE_SHELL_IMAGE = os.environ.get("NODE_SHELL_IMAGE") or "registry.cn-shenzhen.aliyuncs.com/starsl/alpine:3"
NODE_SHELL_TTL = int(os.environ.get("NODE_SHELL_TTL") or 86400)
NODE_SHELL_READY_TIMEOUT = 120  # 等临时 pod 就绪(含首次拉镜像)的上限秒数
# 进入宿主机 1 号进程的 mount/uts/ipc/net 命名空间;pid 不进,保留容器自身便于 exec 退出时清理
NSENTER_PREFIX = ["nsenter", "-t", "1", "-m", "-u", "-i", "-n", "--"]
_IMAGE_PULL_REASONS = ("ErrImagePull", "ImagePullBackOff", "InvalidImageName", "ErrImageNeverPull")


class ExecError(Exception):
    """可以直接展示给用户的错误"""


def shell_script(shells):
    """sh -c 的脚本:设置 TERM,exec 第一个存在的 shell"""
    steps = [f"export TERM={TERM}"]
    steps += [f"command -v {shell} >/dev/null 2>&1 && exec {shell}" for shell in shells[:-1]]
    steps.append(f"exec {shells[-1]}")
    return "; ".join(steps)


def shell_commands(shell):
    """依次尝试的 exec 命令。sh 走 PATH 查找(distroless debug 镜像的 sh 在 /busybox 下)"""
    shells = SHELLS if shell == "auto" else (shell,)
    commands = [["sh", "-c", shell_script(shells)]]
    if shells[0] != "sh":
        commands.append([shells[0]])  # 容器里没有 sh(极少见)时直接执行
    return commands


def node_shell_commands(shell):
    """节点终端:先 nsenter 进宿主机命名空间,再在宿主机里找 shell"""
    return [NSENTER_PREFIX + command for command in shell_commands(shell)]


def shell_missing(message):
    lowered = (message or "").lower()
    return any(hint in lowered for hint in _SHELL_MISSING_HINTS)


def pick_container(pod, requested=""):
    """选要进入的容器,返回 (容器名, 业务容器列表);Pod 或容器没在运行时抛 ExecError

    指定了容器就用指定的(也允许正在运行的 init 容器);没指定时同 kubectl 先看 default-container 注解,
    再跳过常见 sidecar 取第一个。
    """
    status = pod.status
    phase = status.phase if status else None
    if phase in ("Succeeded", "Failed"):
        raise ExecError(f"Pod 已经结束({phase}),无法进入终端")
    names = [c.name for c in pod.spec.containers or []]
    if requested:
        if requested not in names and requested not in [c.name for c in pod.spec.init_containers or []]:
            raise ExecError(f"容器 {requested} 不存在")
        name = requested
    else:
        name = (pod.metadata.annotations or {}).get(DEFAULT_CONTAINER_ANNOTATION)
        if name not in names:
            name = next((n for n in names if not any(hint in n for hint in SIDECAR_HINTS)), names[0] if names else None)
        if not name:
            raise ExecError("Pod 里没有容器")
    statuses = list(status.container_statuses or []) + list(status.init_container_statuses or []) if status else []
    state = next((s.state for s in statuses if s.name == name), None)
    if state is None or state.running is None:
        reason = ""
        if state is not None and state.waiting is not None:
            reason = state.waiting.reason or ""
        elif state is not None and state.terminated is not None:
            reason = state.terminated.reason or "Terminated"
        raise ExecError(f"容器 {name} 没有在运行" + (f"({reason})" if reason else ""))
    return name, names


def parse_exit_status(raw):
    """error 通道里的 metav1.Status → (退出码, 说明);没收到状态(连接被断开)返回 (None, "")"""
    if not raw:
        return None, ""
    try:
        status = json.loads(raw)
    except ValueError:
        return None, raw.decode("utf-8", "replace")
    if status.get("status") == "Success":
        return 0, ""
    for cause in (status.get("details") or {}).get("causes") or []:
        if cause.get("reason") == "ExitCode":
            try:
                return int(cause.get("message")), ""
            except (TypeError, ValueError):
                break
    return None, status.get("message") or "命令执行失败"


def resize_frame(cols, rows):
    return bytes([RESIZE_CHANNEL]) + json.dumps({"Width": cols, "Height": rows}).encode()


def node_shell_pod_manifest(node_name, pod_name):
    """Lens 风格的节点终端临时 pod:privileged + hostPID,维持存活等待 exec 进来 nsenter"""
    return client.V1Pod(
        metadata=client.V1ObjectMeta(name=pod_name, labels={"app": NODE_SHELL_LABEL}),
        spec=client.V1PodSpec(
            node_name=node_name,  # 直接写 nodeName 绕过调度器,已 cordon(不可调度)的节点也能跑
            host_pid=True,
            host_network=True,
            host_ipc=True,
            restart_policy="Never",
            termination_grace_period_seconds=0,
            tolerations=[client.V1Toleration(operator="Exists")],  # 容忍所有污点,保证落到目标节点
            containers=[
                client.V1Container(
                    name=NODE_SHELL_CONTAINER,
                    image=NODE_SHELL_IMAGE,
                    image_pull_policy="IfNotPresent",
                    # 容器自身只需维持存活;真正进宿主机由 exec 时的 nsenter 完成
                    command=["sleep", str(NODE_SHELL_TTL)],
                    security_context=client.V1SecurityContext(privileged=True),
                )
            ],
        ),
    )


def _size(value, default, upper):
    return value if type(value) is int and 0 < value <= upper else default


async def _send_quietly(ws, message):
    try:
        await ws.send_json(message)
    except Exception:
        pass


def _api_error(exc):
    detail = exc.reason
    try:
        detail = json.loads(exc.body).get("message") or detail
    except Exception:
        pass
    return f"K8S API 错误 {exc.status}: {detail}"


class _PodTarget:
    """进入一个已存在的 Pod 容器"""

    kind = "pod"

    def __init__(self, namespace, pod_name, container, shell):
        self.namespace = namespace
        self.pod_name = pod_name
        self.container = container
        self.shell = shell

    async def prepare(self, session):
        pod = await session.manager.core_v1.read_namespaced_pod(self.pod_name, self.namespace, _request_timeout=10)
        self.container, containers = pick_container(pod, self.container)
        return containers

    def commands(self):
        return shell_commands(self.shell)

    def cleanup(self, manager):
        pass

    def label(self):
        return f"{self.namespace}/{self.pod_name}"


class _NodeTarget:
    """进入一个节点:临时建 privileged pod,用 nsenter 进宿主机命名空间;会话结束删 pod"""

    kind = "node"

    def __init__(self, node, shell):
        self.node = node
        self.shell = shell
        self.namespace = NODE_SHELL_NAMESPACE
        self.pod_name = f"{NODE_SHELL_LABEL}-{uuid.uuid4().hex[:12]}"
        self.container = NODE_SHELL_CONTAINER
        self._created = False

    async def prepare(self, session):
        await session.notice(f"正在节点 {self.node} 上创建终端容器(首次拉取镜像可能较慢)...")
        await session.manager.create_node_shell_pod(self.node, self.pod_name)
        self._created = True
        await session.manager.wait_pod_running(self.namespace, self.pod_name, self.container)
        return [self.container]

    def commands(self):
        return node_shell_commands(self.shell)

    def cleanup(self, manager):
        # 可能在会话被取消(stop / 断线)时调用:交给 manager 独立删除,不随会话任务一起被取消
        if self._created:
            manager.schedule_delete_node_pod(self.namespace, self.pod_name)

    def label(self):
        return f"node/{self.node}"


class _Session:
    def __init__(self, manager, ws, session_id, target, cols, rows):
        self.manager = manager
        self.ws = ws  # 到 master 的连接
        self.id = session_id
        self.target = target
        self.cols = cols
        self.rows = rows
        self.inbox = asyncio.Queue(maxsize=INBOX_SIZE)
        self.out = bytearray()
        self.out_ready = asyncio.Event()
        self.window = asyncio.Event()
        self.window.set()
        self.sent = 0
        self.acked = 0
        self.k8s_ws = None
        self.started = False
        self.reading_done = False
        self.dropped = False
        self.task = None

    # ------------------------------------------------------------------ master 发来的
    def feed(self, frame):
        try:
            self.inbox.put_nowait(frame)
        except asyncio.QueueFull:
            # 容器一直不读输入(比如往不读 stdin 的进程里粘贴大段文本),多出来的丢掉
            if not self.dropped:
                self.dropped = True
                logger.warning(f"[pod-exec] 输入积压,丢弃多余输入 session={self.id}")

    def resize(self, cols, rows):
        self.cols, self.rows = cols, rows
        if self.k8s_ws is not None:  # 还没连上时,连上后按最新尺寸发
            self.feed(resize_frame(cols, rows))

    def ack(self, nbytes):
        self.acked = min(self.sent, self.acked + nbytes)
        if self.sent - self.acked + len(self.out) < WINDOW:
            self.window.set()

    def stop(self):
        if self.task is not None and not self.task.done():
            self.task.cancel()

    # ------------------------------------------------------------------ 会话
    async def _emit(self, event, **fields):
        await self.ws.send_json({"type": "pod_exec", "event": event, "session_id": self.id, **fields})

    async def notice(self, text):
        """往终端里写一行提示(灰色),连上容器前的进度信息走这里"""
        data = base64.b64encode(f"\x1b[90m{text}\x1b[0m\r\n".encode()).decode("ascii")
        await self._emit("output", data=data)

    async def run(self):
        code, message = None, ""
        try:
            await self._emit("accepted")
            containers = await self.target.prepare(self)
            commands = self.target.commands()
            async with self.manager.api_client() as api_client:
                for index, command in enumerate(commands):
                    code, message = await self._exec(api_client, command, containers)
                    if not shell_missing(message) or index == len(commands) - 1:
                        break
                    logger.info(f"[{self.target.kind}-exec] {command[0]} 不存在,换下一个 session={self.id}")
            if shell_missing(message):
                tried = SHELLS if self.target.shell == "auto" else (self.target.shell,)
                message = NO_SHELL_MESSAGE.format("、".join(tried))
        except ExecError as exc:
            message = str(exc)
        except ApiException as exc:
            message = _api_error(exc)
        except WSServerHandshakeError as exc:
            message = f"连接容器失败: HTTP {exc.status} {exc.message}"
        except TimeoutError:
            message = "连接超时(K8S API 没有响应)"
        except Exception as exc:
            logger.exception(f"[{self.target.kind}-exec] 会话异常 session={self.id}: {exc}")
            message = f"终端异常: {exc}"
        finally:
            self.manager.sessions.pop(self.id, None)
            self.target.cleanup(self.manager)
        # 被 stop / 断线取消的会话走不到这里,浏览器已经不在了
        logger.info(f"[{self.target.kind}-exec] 终端结束 {self.target.label()} session={self.id} code={code} {message}")
        try:
            await self._emit("exit", code=code, message=message)
        except Exception:
            pass

    async def _exec(self, api_client, command, containers):
        target = self.target
        ws = await self.manager.open_exec(api_client, target.namespace, target.pod_name, target.container, command)
        self.reading_done = False
        writer = sender = None
        try:
            if not self.started:
                self.started = True
                logger.info(f"[{target.kind}-exec] 打开终端 {target.label()}/{target.container} session={self.id}")
                await self._emit("started", container=target.container, containers=containers)
            # 先设 k8s_ws:发初始尺寸期间到达的 resize 进队列,排在初始尺寸后面
            self.k8s_ws = ws
            await ws.send_bytes(resize_frame(self.cols, self.rows))
            writer = asyncio.create_task(self._write_input(ws))
            sender = asyncio.create_task(self._send_output())
            status = await self._read_output(ws)
            self.reading_done = True
            self.out_ready.set()
            await sender  # 剩下的输出发完再报退出
            return parse_exit_status(status)
        finally:
            self.k8s_ws = None
            for task in (writer, sender):
                if task is not None and not task.done():
                    task.cancel()
            await ws.close()

    async def _read_output(self, ws):
        status = None
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                data = msg.data
            elif msg.type == WSMsgType.TEXT:
                data = msg.data.encode()
            else:
                break
            if len(data) < 2:
                continue
            channel, payload = data[0], data[1:]
            if channel in (STDOUT_CHANNEL, STDERR_CHANNEL):
                self.out += payload
                self.out_ready.set()
                # 未确认 + 待发超过窗口:停止读,等浏览器 ack(发送不改变这个和,只有 ack 会让它变小)
                if self.sent - self.acked + len(self.out) >= WINDOW:
                    self.window.clear()
                    await self.window.wait()
            elif channel == ERROR_CHANNEL:
                status = payload
        return status

    async def _send_output(self):
        while True:
            await self.out_ready.wait()
            self.out_ready.clear()
            while self.out:
                chunk = bytes(self.out[:MAX_FRAME])
                del self.out[:MAX_FRAME]
                self.sent += len(chunk)
                await self._emit("output", data=base64.b64encode(chunk).decode("ascii"))
            if self.reading_done:
                return

    async def _write_input(self, ws):
        try:
            while True:
                frame = await self.inbox.get()
                await ws.send_bytes(frame)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 连接断了,读那边会跟着结束
            logger.debug(f"[pod-exec] 写入容器失败 session={self.id}: {exc}")


class PodExecManager:
    def __init__(self, core_v1):
        self.core_v1 = core_v1
        self.sessions = {}  # session_id -> _Session
        self._tasks = set()

    def api_client(self):
        return WsApiClient()

    async def open_exec(self, api_client, namespace, pod, container, command):
        """建立 K8S exec WebSocket,返回 aiohttp 的 ClientWebSocketResponse(单测替换这个方法)"""
        async with asyncio.timeout(CONNECT_TIMEOUT):
            ws_ctx = await client.CoreV1Api(api_client=api_client).connect_get_namespaced_pod_exec(
                pod, namespace, command=command, container=container,
                stdin=True, stdout=True, stderr=True, tty=True, _preload_content=False,
            )
            return await ws_ctx

    # ------------------------------------------------------------------ 节点终端临时 pod
    async def create_node_shell_pod(self, node_name, pod_name):
        await self.core_v1.create_namespaced_pod(
            NODE_SHELL_NAMESPACE, node_shell_pod_manifest(node_name, pod_name), _request_timeout=15
        )

    async def wait_pod_running(self, namespace, pod_name, container):
        """等临时 pod 的容器进入 Running;拉镜像失败 / pod 失败时抛 ExecError"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + NODE_SHELL_READY_TIMEOUT
        while True:
            pod = await self.core_v1.read_namespaced_pod(pod_name, namespace, _request_timeout=10)
            phase = pod.status.phase if pod.status else None
            state = next((s.state for s in (pod.status.container_statuses or []) if s.name == container), None)
            if state is not None and state.running is not None:
                return
            if phase in ("Failed", "Succeeded"):
                raise ExecError(f"节点终端容器启动失败({phase})")
            if state is not None and state.waiting is not None and state.waiting.reason in _IMAGE_PULL_REASONS:
                raise ExecError(f"拉取节点终端镜像失败({state.waiting.reason}),可把 NODE_SHELL_IMAGE 换成内网镜像")
            if loop.time() > deadline:
                raise ExecError("创建节点终端容器超时(镜像可能还在拉取,请稍后重试)")
            await asyncio.sleep(0.5)

    async def delete_node_shell_pod(self, namespace, pod_name):
        try:
            await self.core_v1.delete_namespaced_pod(
                pod_name, namespace, grace_period_seconds=0, propagation_policy="Background", _request_timeout=15
            )
            logger.info(f"[node-exec] 已删除节点终端 pod {namespace}/{pod_name}")
        except ApiException as exc:
            if exc.status != 404:
                logger.warning(f"[node-exec] 删除节点终端 pod 失败 {namespace}/{pod_name}: {exc.reason}")
        except Exception as exc:
            logger.warning(f"[node-exec] 删除节点终端 pod 异常 {namespace}/{pod_name}: {exc}")

    def schedule_delete_node_pod(self, namespace, pod_name):
        self._spawn(self.delete_node_shell_pod(namespace, pod_name))

    async def cleanup_orphans(self):
        """agent 启动 / 重连时清理上次残留的节点终端 pod(本集群只有这一个 agent,它们都是孤儿)"""
        try:
            pods = await self.core_v1.list_namespaced_pod(
                NODE_SHELL_NAMESPACE, label_selector=f"app={NODE_SHELL_LABEL}", _request_timeout=15
            )
        except Exception as exc:
            logger.warning(f"[node-exec] 清理残留节点终端 pod 失败: {exc}")
            return
        names = [pod.metadata.name for pod in pods.items]
        for name in names:
            await self.delete_node_shell_pod(NODE_SHELL_NAMESPACE, name)
        if names:
            logger.info(f"[node-exec] 清理了 {len(names)} 个残留的节点终端 pod")

    def _spawn(self, coro):
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def handle(self, ws, data):
        """master 发来的 pod_exec 消息,在接收循环里同步调用,不能 await"""
        session_id = data.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            return
        action = data.get("action")
        if action == "start":
            self._start(ws, session_id, data)
            return
        session = self.sessions.get(session_id)
        if session is None:
            return
        if action == "stdin":
            text = data.get("data")
            if isinstance(text, str) and text:
                # onBinary 的数据每个字符就是一个字节
                encoding = "latin-1" if data.get("binary") is True else "utf-8"
                session.feed(bytes([STDIN_CHANNEL]) + text.encode(encoding, "replace"))
        elif action == "resize":
            cols, rows = _size(data.get("cols"), 0, MAX_COLS), _size(data.get("rows"), 0, MAX_ROWS)
            if cols and rows:
                session.resize(cols, rows)
        elif action == "ack":
            nbytes = data.get("bytes")
            if type(nbytes) is int and nbytes > 0:
                session.ack(nbytes)
        elif action == "stop":
            session.stop()

    def _make_target(self, data):
        """按 start 消息构造 target;参数不合法返回 (None, 错误信息)"""
        kind = data.get("kind") or "pod"
        shell = data.get("shell") or "auto"
        if shell not in SHELL_CHOICES:
            return None, f"不支持的 shell: {shell}"
        if kind == "node":
            node = data.get("node_name")
            if not (isinstance(node, str) and node):
                return None, "参数错误"
            return _NodeTarget(node, shell), None
        if kind == "pod":
            namespace, pod, container = data.get("namespace"), data.get("pod_name"), data.get("container") or ""
            if not (isinstance(namespace, str) and namespace and isinstance(pod, str) and pod
                    and isinstance(container, str)):
                return None, "参数错误"
            return _PodTarget(namespace, pod, container, shell), None
        return None, "参数错误"

    def _start(self, ws, session_id, data):
        if session_id in self.sessions:
            return
        if len(self.sessions) >= MAX_SESSIONS:
            target, error = None, f"agent 上同时打开的终端已达上限({MAX_SESSIONS} 个)"
        else:
            target, error = self._make_target(data)
        if error:
            message = {"type": "pod_exec", "event": "exit", "session_id": session_id, "code": None, "message": error}
            self._spawn(_send_quietly(ws, message))
            return
        session = _Session(self, ws, session_id, target,
                           _size(data.get("cols"), 80, MAX_COLS), _size(data.get("rows"), 24, MAX_ROWS))
        self.sessions[session_id] = session
        session.task = self._spawn(session.run())

    def close_all(self):
        """到 master 的连接断了:输出没人收,全部关掉(会话结束时自己从 sessions 里移除并删节点 pod)"""
        for session in list(self.sessions.values()):
            session.stop()
