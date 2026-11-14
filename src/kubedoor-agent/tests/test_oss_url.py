"""对象存储上传地址:master 随 /api/pod/* 请求带来,agent 主进程放进请求头转给 pod-mgr,pod-mgr 优先用它。

- 主进程:从 kubedoor-agent.py 里取出真实的 process_request / handle_http_request 执行(整个文件依赖太多,不直接 import)
- pod-mgr:按路径加载 pod-mgr.py(K8S 相关换成假的),用 FastAPI 的 TestClient 调接口
"""

import ast
import asyncio
import base64
import json
import re
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock
from urllib.parse import urlencode

import aiohttp
import pytest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestServer
from loguru import logger

from conftest import AGENT_ROOT, load_module

SECRET = "oss-p%40ss"
OSS = f"https://uploader:{SECRET}@objects.example.com/kubedoor"
MASKED = "https://uploader:******@objects.example.com/kubedoor"


@pytest.fixture
def oss_module():
    return load_module("agent_test_oss_url", AGENT_ROOT / "func_manager" / "oss_url.py")


@pytest.fixture
def capture_logs():
    """capture_logs() 开始收集日志,返回收集到的行;要在加载 pod-mgr.py 之后调用(它加载时会 logger.remove())"""
    handlers = []

    def start():
        lines = []
        handlers.append(logger.add(lambda message: lines.append(str(message)), level="DEBUG"))
        return lines

    yield start
    for handler_id in handlers:
        try:
            logger.remove(handler_id)
        except ValueError:
            pass


# ---------------------------------------------------------------------- 纯函数


def test_local_request_routes_pod_paths_and_forwards_the_address(oss_module):
    url, headers = oss_module.local_request({"path": "/api/agent/res/content"})
    assert (url, headers) == ("https://127.0.0.1/api/agent/res/content", {})
    # 不是 /api/pod/ 的请求即使带了 oss_url 也不加请求头
    assert oss_module.local_request({"path": "/api/scale", "oss_url": OSS}) == ("https://127.0.0.1/api/scale", {})
    url, headers = oss_module.local_request({"path": "/api/pod/auto_dump", "oss_url": OSS})
    assert url == "http://127.0.0.1:81/api/pod/auto_dump" and headers == {"X-Kubedoor-Oss-Url": OSS}
    # 新 master 没配置时带空串:照样加请求头,pod-mgr 据此报「控制端没有配置」
    assert oss_module.local_request({"path": "/api/pod/auto_dump", "oss_url": ""})[1] == {"X-Kubedoor-Oss-Url": ""}
    assert oss_module.local_request({"path": "/api/pod/auto_dump", "oss_url": None})[1] == {"X-Kubedoor-Oss-Url": ""}
    # 旧 master 不带这个字段:不加请求头,pod-mgr 用环境变量
    assert oss_module.local_request({"path": "/api/pod/auto_dump"}) == ("http://127.0.0.1:81/api/pod/auto_dump", {})


def test_header_value_is_always_visible_ascii(oss_module):
    _, headers = oss_module.local_request({"path": "/api/pod/x", "oss_url": " https://u:p%40w@oss.example.com/上传 目录\r\n "})
    value = headers["X-Kubedoor-Oss-Url"]
    assert value == "https://u:p%40w@oss.example.com/%E4%B8%8A%E4%BC%A0%20%E7%9B%AE%E5%BD%95"
    assert value.isascii() and value.isprintable()


def test_resolve_prefers_the_header_and_falls_back_to_the_environment(oss_module):
    assert oss_module.resolve({"X-Kubedoor-Oss-Url": OSS + "/"}, "https://env.example.com") == (OSS, "")
    assert oss_module.resolve({"X-Kubedoor-Oss-Url": " "}, "https://env.example.com") == (
        "", "控制端没有配置对象存储上传地址，请在系统管理 → 通用配置填写")
    assert oss_module.resolve({}, " https://env.example.com/ ") == ("https://env.example.com", "")
    url, reason = oss_module.resolve({}, None)
    assert url == "" and "OSS_URL" in reason


def test_masked_hides_only_the_password(oss_module):
    assert oss_module.masked(OSS) == MASKED
    assert oss_module.masked("https://objects.example.com/kubedoor") == "https://objects.example.com/kubedoor"
    assert oss_module.masked("https://user@objects.example.com") == "https://user@objects.example.com"
    assert oss_module.masked("http://[::1") == "<对象存储地址>"


# ---------------------------------------------------------------------- 主进程转发


def agent_functions(oss_module, http_session, spawned):
    """从 kubedoor-agent.py 里取出 process_request、handle_http_request 和它们用到的几个模块级定义,在假的全局变量里执行"""
    source = (AGENT_ROOT / "kubedoor-agent.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    wanted = {"_JSON_CONTENT_TYPE", "_BINARY_CONTENT_TYPES", "_message_size", "handle_http_request", "process_request"}
    nodes = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in wanted:
            nodes.append(node)
        elif isinstance(node, ast.Assign) and any(getattr(t, "id", None) in wanted for t in node.targets):
            nodes.append(node)
    assert len(nodes) == len(wanted), "kubedoor-agent.py 的结构变了,请同步这个测试"
    namespace = {
        "asyncio": asyncio, "base64": base64, "json": json, "re": re, "aiohttp": aiohttp, "urlencode": urlencode,
        "logger": logger, "WSMsgType": WSMsgType, "MasterConnection": object, "WS_MAX_MSG_SIZE": 16 * 1024 * 1024,
        "oss_url": oss_module, "http_session": http_session,
        "_spawn": lambda coroutine: spawned.append(asyncio.ensure_future(coroutine)),
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "kubedoor-agent.py", "exec"), namespace)
    return namespace


class MasterSocket:
    """假的到 master 的连接:按顺序吐出消息,记下回包"""

    def __init__(self, *messages):
        self.messages = [SimpleNamespace(type=WSMsgType.TEXT, data=json.dumps(m)) for m in messages]
        self.sent = []

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for message in self.messages:
            yield message

    async def send_str(self, text):
        self.sent.append(json.loads(text))


async def forward(monkeypatch, oss_module, capture_logs, *messages):
    """跑一遍 process_request:master 发来 messages,本机 pod-mgr / HTTPS 服务换成测试服务器,返回 (收到的请求, 回包, 日志)"""
    received = []

    async def handler(request):
        received.append({"path": request.path, "query": dict(request.query), "headers": dict(request.headers)})
        return web.json_response({"success": True, "message": "ok"})

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    async with TestServer(app) as server:
        base = str(server.make_url("")).rstrip("/")
        monkeypatch.setattr(oss_module, "POD_MGR", base)
        monkeypatch.setattr(oss_module, "AGENT", base)
        lines = capture_logs()
        spawned = []
        async with aiohttp.ClientSession() as session:
            functions = agent_functions(oss_module, session, spawned)
            socket = MasterSocket(*messages)
            await functions["process_request"](socket)
            await asyncio.gather(*spawned)
    return received, socket.sent, lines


def request_message(request_id, path, **extra):
    return {"type": "request", "request_id": request_id, "method": "GET", "path": path,
            "query": {"env": "prod", "ns": "default", "pod_name": "app-1"}, "body": {}, **extra}


@pytest.mark.asyncio
async def test_agent_forwards_the_address_to_pod_mgr_as_a_header_without_logging_it(monkeypatch, oss_module, capture_logs):
    received, sent, lines = await forward(
        monkeypatch, oss_module, capture_logs,
        request_message("r1", "/api/pod/auto_dump", oss_url=OSS),
        request_message("r2", "/api/pod/auto_jstack", oss_url=""),
        request_message("r3", "/api/pod/auto_jfr"),
        request_message("r4", "/api/agent/res/content", oss_url=OSS),
    )
    by_path = {item["path"]: item["headers"] for item in received}
    assert by_path["/api/pod/auto_dump"]["X-Kubedoor-Oss-Url"] == OSS
    assert by_path["/api/pod/auto_jstack"]["X-Kubedoor-Oss-Url"] == ""
    assert "X-Kubedoor-Oss-Url" not in by_path["/api/pod/auto_jfr"]  # 旧 master 不带
    assert "X-Kubedoor-Oss-Url" not in by_path["/api/agent/res/content"]
    assert sorted(item["request_id"] for item in sent) == ["r1", "r2", "r3", "r4"]
    assert all(item["response"] == {"success": True, "message": "ok"} for item in sent)
    text = "\n".join(lines)
    assert "转发请求: GET" in text and SECRET not in text and "objects.example.com" not in text


# ---------------------------------------------------------------------- pod-mgr


class FakeK8s:
    async def __aenter__(self):
        return SimpleNamespace(core_v1_api=object(), core_v1=object())

    async def __aexit__(self, *args):
        return False


@pytest.fixture
def pod_mgr(monkeypatch, oss_module):
    """按路径加载 pod-mgr.py:utils、K8S 客户端换成假的,对象存储的逻辑用真的 func_manager/oss_url.py"""
    fake_utils = ModuleType("utils")
    fake_utils.OSS_URL = None
    fake_utils.send_msg = Mock()
    monkeypatch.setitem(sys.modules, "utils", fake_utils)
    package = ModuleType("func_manager")
    package.__path__ = [str(AGENT_ROOT / "func_manager")]
    package.oss_url = oss_module
    monkeypatch.setitem(sys.modules, "func_manager", package)
    monkeypatch.setitem(sys.modules, "func_manager.oss_url", oss_module)
    scheduler = ModuleType("func_manager.k8s_node_scheduler")
    scheduler.K8sNodeScheduler = object
    monkeypatch.setitem(sys.modules, "func_manager.k8s_node_scheduler", scheduler)
    manager = ModuleType("k8s_client_manager")
    manager.K8sClientManager = FakeK8s
    manager.load_incluster_config = lambda: None
    monkeypatch.setitem(sys.modules, "k8s_client_manager", manager)
    module = load_module("agent_test_pod_mgr", AGENT_ROOT / "pod-mgr.py")

    commands = []

    async def execute_command(command, v1, pod_name, ns, container=None, shown=None):
        commands.append(command)
        if "jstack -l" in command:
            return True, '"main" #1 prio=5'
        return True, ""

    async def get_pod_info(ns, pod_name, v1, kind, tail):
        return f"{kind}-{pod_name}.{tail}", None

    async def container_name(v1, ns, pod_name):
        return "app"

    monkeypatch.setattr(module, "execute_command", execute_command)
    monkeypatch.setattr(module, "get_pod_info", get_pod_info)
    monkeypatch.setattr(module, "get_pod_container_name", container_name)
    module.commands = commands
    module.fake_utils = fake_utils
    return module


def call(pod_mgr, path, headers=None):
    from fastapi.testclient import TestClient

    with TestClient(pod_mgr.app) as client:
        response = client.get(path, params={"env": "prod", "ns": "default", "pod_name": "app-1"}, headers=headers or {})
    return response.status_code, response.json()


def uploads(pod_mgr):
    return [command for command in pod_mgr.commands if command.startswith("curl -s -T")]


def test_pod_mgr_prefers_the_address_from_the_header(pod_mgr):
    pod_mgr.fake_utils.OSS_URL = "https://env.example.com/old"
    status, body = call(pod_mgr, "/api/pod/auto_dump", {"X-Kubedoor-Oss-Url": OSS})
    assert status == 200 and body["success"] is True
    assert body["link"] == f"{OSS}/prod/dump/dump-app-1.hprof"
    assert uploads(pod_mgr) == [f"curl -s -T /dump-app-1.hprof {OSS}/prod/dump/dump-app-1.hprof"]
    assert "env.example.com" not in json.dumps(pod_mgr.commands)


def test_pod_mgr_falls_back_to_the_environment_without_the_header(pod_mgr):
    pod_mgr.fake_utils.OSS_URL = "https://env.example.com/old/"
    status, body = call(pod_mgr, "/api/pod/auto_jstack")
    assert status == 200 and body["success"] is True and "jstack文件上传成功" in body["message"]
    assert uploads(pod_mgr) == ["curl -s -T /jstack-app-1.jstack https://env.example.com/old/prod/jstack/jstack-app-1.jstack"]
    pod_mgr.fake_utils.OSS_URL = None
    status, body = call(pod_mgr, "/api/pod/auto_dump")
    assert status == 400 and body["success"] is False and "OSS_URL" in body["message"]


@pytest.mark.parametrize("path", ["/api/pod/auto_dump", "/api/pod/auto_jfr"])
def test_pod_mgr_refuses_dump_and_jfr_when_the_master_has_no_address(pod_mgr, path):
    pod_mgr.fake_utils.OSS_URL = "https://env.example.com/old"  # 新 master 明确说没配置时不再用环境变量
    status, body = call(pod_mgr, path, {"X-Kubedoor-Oss-Url": ""})
    assert status == 400
    assert body == {"message": "控制端没有配置对象存储上传地址，请在系统管理 → 通用配置填写", "success": False}
    assert pod_mgr.commands == []  # 没有 dump、没开飞行记录


def test_pod_mgr_still_runs_jstack_without_an_address_but_does_not_upload(pod_mgr):
    status, body = call(pod_mgr, "/api/pod/auto_jstack", {"X-Kubedoor-Oss-Url": ""})
    assert status == 200 and body["success"] is True
    assert body["message"].startswith('"main" #1 prio=5')
    assert "文件没有上传：控制端没有配置对象存储上传地址，请在系统管理 → 通用配置填写" in body["message"]
    assert uploads(pod_mgr) == [] and not any("tee" in command for command in pod_mgr.commands)


def test_pod_mgr_jfr_uploads_to_the_header_address_later(pod_mgr, monkeypatch):
    tasks = []
    monkeypatch.setattr(pod_mgr, "jfr_upload", lambda *args: tasks.append(args))
    status, body = call(pod_mgr, "/api/pod/auto_jfr", {"X-Kubedoor-Oss-Url": OSS})
    assert status == 200 and body["link"] == f"{OSS}/prod/jfr/jfr-app-1.jfr"
    [args] = tasks
    assert args[-1] == OSS


class ExecSocket:
    """kubernetes exec 的 WebSocket:按顺序吐出 (通道, 内容)"""

    def __init__(self, chunks):
        self.chunks = chunks

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for channel, text in self.chunks:
            yield SimpleNamespace(type=WSMsgType.BINARY, data=bytes([channel]) + text.encode())


def fake_exec(monkeypatch, pod_mgr, behaviour):
    """换掉 pod-mgr 里的 WsApiClient / CoreV1Api:behaviour(command) 返回 exec 的输出或抛异常"""
    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class CoreV1:
        def __init__(self, api_client=None):
            pass

        async def connect_get_namespaced_pod_exec(self, name, namespace, command, **kwargs):
            return ExecSocket(behaviour(command[-1]))

    monkeypatch.setattr(pod_mgr, "WsApiClient", Client)
    monkeypatch.setattr(pod_mgr, "client", SimpleNamespace(CoreV1Api=CoreV1))


@pytest.mark.asyncio
async def test_upload_command_logs_and_errors_never_show_the_password(pod_mgr, monkeypatch, capture_logs):
    real = load_module("agent_test_pod_mgr_real", AGENT_ROOT / "pod-mgr.py")
    lines = capture_logs()
    seen = []

    def failing_curl(command):
        seen.append(command)
        return [(1, "7\n")]  # curl 失败,退出码 7

    fake_exec(monkeypatch, real, failing_curl)
    url = f"{OSS}/prod/dump/dump-app-1.hprof"
    ok, message = await real.upload_file(object(), "default", "app-1", "app", "dump-app-1.hprof", url)
    assert ok is False and MASKED in message and SECRET not in message
    assert seen == [f"curl -s -T /dump-app-1.hprof {url}; echo $?"]  # 真正执行的命令用完整地址

    def broken(command):
        raise RuntimeError(f"handshake failed: wss://k8s/exec?command={command}")

    fake_exec(monkeypatch, real, broken)
    ok, message = await real.upload_file(object(), "default", "app-1", "app", "dump-app-1.hprof", url)
    assert ok is False and message == f"命令 curl -s -T /dump-app-1.hprof {MASKED}/prod/dump/dump-app-1.hprof 执行失败（RuntimeError）"
    text = "\n".join(lines)
    assert MASKED in text and SECRET not in text


@pytest.mark.asyncio
async def test_upload_address_is_one_shell_argument_even_with_shell_characters(pod_mgr, monkeypatch, capture_logs):
    """通用配置只拦了空白、反斜杠、控制字符、? 和 #:地址里的引号、;、$、& 都不能被 shell 解释"""
    import shlex

    real = load_module("agent_test_pod_mgr_quote", AGENT_ROOT / "pod-mgr.py")
    lines = capture_logs()
    seen = []

    def ok_curl(command):
        seen.append(command)
        return [(1, "0\n")]

    fake_exec(monkeypatch, real, ok_curl)
    base = "https://u:p'w$(id)@oss.example.com/a;b&c`d`"
    url = f"{base}/prod/jstack/jstack-app-1.jstack"
    ok, _ = await real.upload_file(object(), "default", "app-1", "app", "jstack-app-1.jstack", url)
    assert ok is True
    [executed] = seen
    command = executed.removesuffix("; echo $?")
    assert shlex.split(command) == ["curl", "-s", "-T", "/jstack-app-1.jstack", url]
    text = "\n".join(lines)
    assert "p'w$(id)" not in text and "u:******@oss.example.com/a;b&c`d`" in text
