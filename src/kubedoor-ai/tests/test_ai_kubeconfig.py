"""AI Kubeconfig：网页安装 Agent 时 master 自动配置（不覆盖管理员手动上传的），以及通用操作 Agent 优先的来源选择"""
import json
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from starlette.testclient import TestClient

from fakes import FakeStore
from kubedoor_ai.domain import CredentialCipher, Identity
from kubedoor_ai.gateway import Gateway, RunContext
from kubedoor_ai.http import create_app
from kubedoor_ai.service import Service

ADMIN = {"X-Kubedoor-Token": "internal", "X-User-Name": "kubedoor", "X-User-Permission": "admin"}
AUTO = {"content": json.dumps({"apiVersion": "v1", "kind": "Config"}), "context": "kubedoor-agent", "origin": "agent-install"}
UPSERT_GUARD = "WHERE EXCLUDED.origin='upload' OR kubedoor_ai_connections.origin='agent-install'"


class ConnectionTable:
    """kubedoor_ai_connections 的替身：按 http.py 那条 upsert 的条件决定替换还是保留"""

    def __init__(self, row=None, lands_before_insert=None):
        self.rows = {"prod": dict(row)} if row else {}
        self.lands_before_insert = lands_before_insert   # 检查之后、写入之前管理员刚好上传了一份

    async def connection(self, env):
        row = self.rows.get(env)
        return dict(row) if row else None

    async def one(self, sql, env, encrypted, context, username, origin):
        assert UPSERT_GUARD in sql
        if self.lands_before_insert:
            self.rows[env] = dict(self.lands_before_insert)
        existing = self.rows.get(env)
        if existing and origin == "agent-install" and existing["origin"] != "agent-install":
            return None
        revision = existing["revision"] + 1 if existing else 1
        self.rows[env] = {"env": env, "context": context, "origin": origin, "revision": revision, "updated_by": username}
        return {"env": env, "context": context, "revision": revision, "test_status": "success", "tested_at": "now", "origin": origin}


def fake_service(table, tested=None):
    return SimpleNamespace(store=table,
                           test_connection=AsyncMock(return_value=(tested or {"success": True}, {"apiVersion": "v1"}, "kubedoor-agent")),
                           gateway=SimpleNamespace(cipher=SimpleNamespace(encrypt=lambda env, config: b"encrypted")))


def put(service, body, headers=ADMIN):
    with TestClient(create_app(service, token="internal")) as client:
        return client.put("/api/ai/connections/prod", headers=headers, json=body)


def test_agent_install_fills_an_empty_slot():
    table = ConnectionTable()
    service = fake_service(table)
    response = put(service, AUTO)
    assert response.status_code == 200, response.text
    assert response.json()["origin"] == "agent-install" and table.rows["prod"]["updated_by"] == "kubedoor"
    service.test_connection.assert_awaited_once()


def test_agent_install_never_replaces_a_manual_upload():
    table = ConnectionTable({"env": "prod", "context": "admin@prod", "origin": "upload", "revision": 3})
    service = fake_service(table)
    response = put(service, AUTO)
    assert response.status_code == 200 and response.json()["skipped"] is True
    # 不测试也不写，管理员上传的那份原样保留
    service.test_connection.assert_not_awaited()
    assert table.rows["prod"] == {"env": "prod", "context": "admin@prod", "origin": "upload", "revision": 3}


def test_agent_install_keeps_an_upload_that_lands_while_it_is_testing():
    upload = {"env": "prod", "context": "admin@prod", "origin": "upload", "revision": 1}
    table = ConnectionTable(lands_before_insert=upload)
    response = put(fake_service(table), AUTO)
    assert response.json()["skipped"] is True and table.rows["prod"] == upload


def test_reinstall_refreshes_the_automatic_one_and_a_manual_upload_takes_over():
    table = ConnectionTable({"env": "prod", "context": "kubedoor-agent", "origin": "agent-install", "revision": 1})
    assert put(fake_service(table), AUTO).json()["revision"] == 2
    manual = put(fake_service(table), {"content": "{}", "context": "admin@prod"})
    assert manual.json()["origin"] == "upload" and table.rows["prod"]["revision"] == 3
    # 不认识的 origin 一律按手动上传处理
    assert put(fake_service(table), {**AUTO, "origin": "whatever"}).json()["origin"] == "upload"
    # 之后自动配置不再覆盖
    assert put(fake_service(table), AUTO).json()["skipped"] is True


def test_failed_connection_test_saves_nothing():
    table = ConnectionTable()
    tested = {"success": False, "error": {"code": "UNREACHABLE", "message": "connect timeout"}}
    response = put(fake_service(table, tested), AUTO)
    assert response.status_code == 422 and response.json()["error"]["message"] == "connect timeout"
    assert table.rows == {}


def test_only_administrators_can_save_even_automatically():
    table = ConnectionTable()
    response = put(fake_service(table), AUTO, headers={**ADMIN, "X-User-Permission": "rw"})
    assert response.status_code == 403 and table.rows == {}


# ---- 来源选择：Agent 优先 ----

ONLINE = {"agent": {"available": True, "online": True, "ai_tools": True, "data_tools": True}}
OLD_AGENT = {"agent": {"available": False, "online": True, "ai_tools": False, "data_tools": False}}
OFFLINE = {"agent": {"available": False, "online": False, "ai_tools": True, "data_tools": False}}


class Store(FakeStore):
    def __init__(self, configured):
        super().__init__()
        self.configured = configured

    async def connection(self, env):
        return {"env": env, "context": "kubedoor-agent", "revision": 1, "origin": "agent-install"} if self.configured else None


def context(capabilities):
    run = {"id": "run-1", "session_id": "session-1", "origin": "chat", "skill_ids": [],
           "scope": {"env": "prod", "namespace": "demo", "deployment": None, "pod": None}}
    if capabilities is not None:
        run["capabilities"] = capabilities
    return RunContext(run, Identity("operator", "rw", MappingProxyType({})))


@pytest.mark.asyncio
@pytest.mark.parametrize("capabilities, configured, expected", [
    (ONLINE, True, "agent"),        # 配置了 kubeconfig 也先经在线 Agent（master 复核 + Agent 二次校验）
    (ONLINE, False, "agent"),
    (OLD_AGENT, True, "direct"),    # Agent 版本不支持通用工具：用 kubeconfig 直连
    (OFFLINE, True, "direct"),      # Agent 离线：用 kubeconfig 直连
    (OFFLINE, False, "agent"),      # 都不可用：照旧报 Agent 不可用
    (None, True, "direct"),         # 不知道 Agent 状态：沿用旧规则，有 kubeconfig 就直连
])
async def test_generic_operations_prefer_an_available_agent(capabilities, configured, expected):
    gateway = Gateway(Store(configured), CredentialCipher("ab" * 32), "http://master", "internal-token",
                      http=httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(404))))
    try:
        ctx = context(capabilities)
        for operation in ("api", "kubectl", "istioctl", "diagnostic", "pod_exec"):
            assert await gateway.choose_source(ctx, operation, "auto") == expected, operation
        assert await gateway.k8s_source(ctx) == expected
        # 模型明确指定的来源照旧
        assert await gateway.choose_source(ctx, "kubectl", "direct") == "direct"
        assert await gateway.choose_source(ctx, "kubectl", "agent") == "agent"
    finally:
        await gateway.close()


# ---- 资源下拉菜单：同样 Agent 优先，主通道失败换另一条 ----

class ResourceGateway:
    def __init__(self, failing=()):
        self.failing, self.calls = set(failing), []

    async def execute_action(self, ctx, action):
        self.calls.append(action["source"])
        if action["source"] in self.failing:
            return {"success": False, "error": {"code": "UNAVAILABLE"}}
        return {"success": True, "data": {"items": [{"metadata": {"name": "team"}}], "metadata": {}}}

    async def close_context(self, ctx):
        pass


def resource_service(cluster, configured, failing=()):
    service = Service.__new__(Service)
    service.store = Store(configured)
    service.gateway = ResourceGateway(failing)
    service.ensure_env = AsyncMock(return_value=cluster)
    return service


@pytest.mark.asyncio
@pytest.mark.parametrize("cluster, configured, failing, calls, used", [
    ({"online": True, "ai_tools": True}, True, (), ["agent"], "agent"),
    ({"online": True, "ai_tools": True}, True, ("agent",), ["agent", "direct"], "direct"),
    ({"online": False}, True, (), ["direct"], "direct"),
    ({"online": False}, True, ("direct",), ["direct", "agent"], "agent"),
    ({"online": True, "ai_tools": True}, False, (), ["agent"], "agent"),
])
async def test_resource_menus_follow_the_same_order(cluster, configured, failing, calls, used):
    service = resource_service(cluster, configured, failing)
    result = await service.resources(Identity("operator", "rw", MappingProxyType({})), {"env": "prod", "kind": "namespaces"})
    assert result["items"] == [{"name": "team"}] and result["source"] == used
    assert service.gateway.calls == calls
