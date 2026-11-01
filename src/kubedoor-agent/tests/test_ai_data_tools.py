"""AI 只读查询数据库 / Redis / ES 在 agent 侧的二次校验:要有 data.query、只读检查不过的不执行、不记录参数"""

import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from loguru import logger

AGENT_ROOT = Path(__file__).resolve().parents[1]

PASSWORD = "Pa55-w0rd!s3cret"
MYSQL = {"host": "db.app.svc", "user": "app", "password": PASSWORD, "database": "orders",
         "sql": "SELECT id FROM orders ORDER BY id DESC LIMIT 5"}


class Socket:
    closed = False

    def __init__(self):
        self.responses = []

    async def send_str(self, text):
        self.responses.append(json.loads(text)["response"])


class Executor:
    def __init__(self):
        self.calls = []

    async def execute(self, operation, arguments, **kwargs):
        self.calls.append(("execute", operation, arguments, kwargs))
        return {"success": True, "data": {"columns": ["id"], "rows": [[1]]}, "source": operation, "truncated": False}

    async def prepare(self, operation, arguments, **kwargs):
        self.calls.append(("prepare", operation, arguments, kwargs))
        return {"success": True, "arguments": arguments, "preview": {}, "read_only": True, "ops": ["data.query"]}

    async def cancel(self, call_id):
        pass

    async def close(self):
        pass


def packet(operation="mysql", arguments=None, **extra):
    return {"type": "ai_tool", "request_id": "r1", "call_id": "run:q1", "phase": "execute", "operation": operation,
            "arguments": MYSQL if arguments is None else arguments, "username": "alice", "permission": "rw",
            "approved": False, "timeout": 30, "env": "prod", "ops": ["data.query"],
            "allowed_ops": ["pod.exec.view", "data.query"], **extra}


@pytest.fixture
def handler(ai_tools_module, monkeypatch):
    monkeypatch.setattr(ai_tools_module, "KubernetesExecutor", Executor)
    return ai_tools_module.AiToolHandler()


@pytest.fixture
def logs():
    lines = []
    sink = logger.add(lambda message: lines.append(str(message)), level="TRACE")
    yield lines
    logger.remove(sink)


@pytest.mark.asyncio
async def test_data_query_needs_data_query_permission(handler):
    ws = Socket()
    await handler.handle(ws, packet(allowed_ops=["pod.exec.view", "ai.diagnostic"]))
    assert ws.responses[-1] == {"success": False, "error": {
        "code": "FORBIDDEN", "message": "当前账号在集群 prod 没有「AI 查询数据库 / Redis / ES」权限"}}
    assert handler.executor.calls == []


@pytest.mark.asyncio
async def test_data_query_runs_through_executor_without_approval(handler):
    # 要不要批准由网关按「权限与审批」决定(默认要批准、可以改成免批准),agent 只校验权限和只读
    ws = Socket()
    await handler.handle(ws, packet())
    assert ws.responses[-1]["success"] and ws.responses[-1]["data"]["rows"] == [[1]]
    assert handler.executor.calls == [("execute", "mysql", MYSQL, {"call_id": "run:q1", "timeout": 30})]
    assert not handler.completed and not handler.owners and not handler.tasks   # 只读结果不缓存


@pytest.mark.parametrize("operation,arguments", [
    ("redis", {"host": "redis.app.svc", "password": PASSWORD, "command": ["HGETALL", "session:1"]}),
    ("elasticsearch", {"url": "http://es.app.svc:9200", "username": "elastic", "password": PASSWORD,
                       "method": "POST", "path": "/logs-*/_search", "body": {"size": 5}}),
])
@pytest.mark.asyncio
async def test_redis_and_elasticsearch_queries_are_allowed(handler, operation, arguments):
    ws = Socket()
    await handler.handle(ws, packet(operation, arguments))
    assert ws.responses[-1]["success"]
    assert handler.executor.calls[-1][:3] == ("execute", operation, arguments)


@pytest.mark.parametrize("operation,arguments,reason", [
    ("mysql", {**MYSQL, "sql": "UPDATE orders SET status = 'x'"}, "不能执行 UPDATE"),
    ("mysql", {**MYSQL, "sql": "SELECT 1; DROP TABLE orders"}, "一条语句"),
    ("redis", {"host": "redis.app.svc", "command": ["FLUSHALL"]}, "白名单"),
    ("redis", {"host": "redis.app.svc", "command": ["KEYS", "*"]}, "SCAN"),
    ("elasticsearch", {"url": "http://es:9200", "method": "POST", "path": "/orders/_delete_by_query", "body": {}}, "白名单"),
])
@pytest.mark.asyncio
async def test_write_queries_are_rejected_even_if_approved(handler, operation, arguments, reason):
    ws = Socket()
    await handler.handle(ws, packet(operation, arguments, approved=True))
    error = ws.responses[-1]["error"]
    assert error["code"] == "INVALID_ARGUMENT" and reason in error["message"]
    assert handler.executor.calls == []


@pytest.mark.parametrize("allowed_ops", [None, "data.query"])
@pytest.mark.asyncio
async def test_data_query_requires_new_master(handler, allowed_ops):
    # 旧 master 不带 allowed_ops,不能按 read / rw 的旧逻辑放行
    ws = Socket()
    request = packet(allowed_ops=allowed_ops)
    if allowed_ops is None:
        request.pop("allowed_ops")
    await handler.handle(ws, request)
    assert ws.responses[-1]["error"]["code"] == "FORBIDDEN" and "master" in ws.responses[-1]["error"]["message"]
    assert handler.executor.calls == []


@pytest.mark.asyncio
async def test_data_query_prepare_phase_reaches_executor(handler):
    ws = Socket()
    await handler.handle(ws, packet(phase="prepare"))
    assert ws.responses[-1]["success"] and handler.executor.calls[0][:2] == ("prepare", "mysql")


@pytest.mark.asyncio
async def test_data_query_never_logs_or_returns_arguments(ai_tools_module, monkeypatch, logs):
    class Exploding(Executor):
        async def execute(self, operation, arguments, **kwargs):
            raise RuntimeError(f"driver failure with {arguments['password']}")

    monkeypatch.setattr(ai_tools_module, "KubernetesExecutor", Exploding)
    handler, ws = ai_tools_module.AiToolHandler(), Socket()
    await handler.handle(ws, packet())
    await handler.handle(ws, packet(allowed_ops=[]))
    await handler.handle(ws, packet(arguments={**MYSQL, "sql": "DELETE FROM orders"}))
    assert [response["error"]["code"] for response in ws.responses] == ["TOOL_ERROR", "FORBIDDEN", "INVALID_ARGUMENT"]
    assert PASSWORD not in json.dumps(ws.responses, ensure_ascii=False)
    assert all(PASSWORD not in line for line in logs)


def test_op_labels_cover_the_new_operation_types(ai_tools_module):
    from kubedoor_tools import OP_KEYS

    labels = ai_tools_module.OP_LABELS
    assert labels["config.view"] == "查看运行配置" and labels["data.query"] == "AI 查询数据库 / Redis / ES"
    assert set(labels) <= OP_KEYS


def test_agent_connects_with_data_tools_flag():
    source = (AGENT_ROOT / "kubedoor-agent.py").read_text(encoding="utf-8")
    uri = re.search(r'uri = f"([^"]+/ws\?[^"]+)"', source).group(1)
    query = parse_qs(urlsplit(uri).query)
    assert query["ai_tools"] == ["1"] and query["data_tools"] == ["1"]
    # 收到解析不了的消息时不打印原文(ai_tool 消息的参数里可能有密码)
    assert not re.search(r"logger\.\w+\(f?\"收到无法解析的消息[^\n]*msg\.data\}", source)
