import asyncio
import base64
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import sys
from types import MappingProxyType

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from loguru import logger

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from auth import middleware as auth_middleware  # noqa: E402
from auth.principal import Principal  # noqa: E402

spec = importlib.util.spec_from_file_location("ai_api", Path(__file__).parents[1] / "func_manager" / "ai_api.py")
ai_api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ai_api)

USERS = {
    "viewer": Principal("viewer", "read", user_id=1, via="internal"),
    "reader": Principal("reader", "read", user_id=2, via="internal"),
    "limited": Principal("limited", "none", user_id=3, via="internal",
                         clusters=MappingProxyType({"prod": "rw", "staging": "read"})),
    "boss": Principal("boss", "cluster_admin", user_id=4, via="internal",
                      clusters=MappingProxyType({"staging": "read"})),
}


def internal_app(monkeypatch, clients, utils):
    """真实的认证中间件 + 假的用户库:按 X-User-Name 返回预置身份。"""
    async def principal_by_name(username, via):
        return USERS.get(username)

    monkeypatch.setattr(auth_middleware.store, "principal_by_name", principal_by_name)
    app = web.Application(middlewares=[auth_middleware.create_middleware()])
    ai_api.register_routes(app, clients, utils)
    return app


def fake_utils(prom_type="", tag_key="origin_prometheus", prom_url="", **attrs):
    """假的 utils。时序库配置经 utils.prom_type() / tag_key() / prom_url() 取(master 里是通用配置的当前值);
    返回对象的 settings 是当前配置,测试里改它就相当于在页面上改了配置。"""
    settings = {"prom_type": prom_type, "tag_key": tag_key, "prom_url": prom_url}
    return SimpleNamespace(prom_type=lambda: settings["prom_type"], tag_key=lambda: settings["tag_key"],
                           prom_url=lambda: settings["prom_url"], settings=settings, **attrs)


def test_metrics_scope_cannot_be_overridden_and_tenant_url_is_not_rewritten():
    suffix, params = ai_api.metrics_parameters({
        "env": "production", "query": 'sum(rate(cpu{origin_prometheus="other"}[5m]))',
        "extra_label": "origin_prometheus=other", "start": 1000, "end": 4600, "step": "1m",
    }, "origin_prometheus")
    assert suffix == "query_range"
    assert params["extra_label"] == "origin_prometheus=production"
    assert params["step"] == 60


@pytest.mark.parametrize("updates", [
    {"step": 0}, {"step": ".1s"}, {"end": 1000}, {"end": 1000 + 31 * 86400},
    {"start": "2026-10-05T10:00:00"},
])
def test_metrics_invalid_or_unbounded_range_is_rejected(updates):
    body = {"env": "prod", "query": "up", "start": 1000, "end": 4600, **updates}
    with pytest.raises(ValueError):
        ai_api.metrics_parameters(body, "origin_prometheus")


@pytest.mark.asyncio
async def test_internal_routes_require_service_token_and_user_identity(monkeypatch):
    monkeypatch.setenv("AI_INTERNAL_TOKEN", "secret-test-token")
    utils = fake_utils(get_k8s_aliases=AsyncMock(return_value={"offline": ["离线集群"]}),
                       prom_type="Victoria-Metrics-Cluster", tag_key="origin_prometheus")
    app = internal_app(monkeypatch, {"online": {"online": True, "ver": "2"}}, utils)
    async with TestClient(TestServer(app)) as client:
        assert (await client.get("/api/ai/internal/bootstrap")).status == 401
        assert (await client.get("/api/ai/internal/bootstrap", headers={"X-Kubedoor-Token": "wrong"})).status == 401
        # 有 token 没有用户身份(系统调用)不能代表用户使用 AI 桥
        assert (await client.get("/api/ai/internal/bootstrap", headers={"X-Kubedoor-Token": "secret-test-token"})).status == 403
        # 用户不存在或已禁用
        assert (await client.get("/api/ai/internal/bootstrap", headers={
            "X-Kubedoor-Token": "secret-test-token", "X-User-Name": "ghost"})).status == 403
        response = await client.get("/api/ai/internal/bootstrap", headers={
            # X-User-Permission 由 AI 服务转发,master 不采信,只按库里的用户权限计算
            "X-Kubedoor-Token": "secret-test-token", "X-User-Name": "viewer", "X-User-Permission": "rw",
        })
        assert response.status == 200
        body = await response.json()
        assert body["username"] == "viewer" and body["permission"] == "read"
        assert (body["prom_type"], body["prom_label_key"]) == ("Victoria-Metrics-Cluster", "origin_prometheus")
        # 库里还没有的集群(只在 clients 里)别名是空列表
        assert [(c["env"], c["online"], c["permission"], c["aliases"]) for c in body["clusters"]] == [
            ("offline", False, "read", ["离线集群"]), ("online", True, "read", [])]
        # 通用配置改了,下一次 bootstrap 马上返回新值
        utils.settings.update(prom_type="Prometheus", tag_key="cluster")
        body = await (await client.get("/api/ai/internal/bootstrap", headers={
            "X-Kubedoor-Token": "secret-test-token", "X-User-Name": "viewer"})).json()
        assert (body["prom_type"], body["prom_label_key"]) == ("Prometheus", "cluster")
        identity = await client.get("/api/ai/internal/identity", headers={
            "X-Kubedoor-Token": "secret-test-token", "X-User-Name": "limited", "X-User-Permission": "admin",
        })
        assert identity.status == 200
        identity_body = await identity.json()
        assert identity_body["username"] == "limited"
        assert identity_body["permission"] == "none"
        assert identity_body["clusters"] == {"prod": "rw", "staging": "read"}


@pytest.mark.asyncio
async def test_bootstrap_hides_clusters_without_permission_and_execute_checks_cluster(monkeypatch):
    monkeypatch.setenv("AI_INTERNAL_TOKEN", "secret-test-token")
    utils = fake_utils(get_k8s_aliases=AsyncMock(return_value={"prod": ["生产"], "staging": [], "secret": ["机密"]}),
                       prom_type="Victoria-Metrics-Cluster", tag_key="origin_prometheus")
    sent = []

    def socket_for(env):
        class Socket:
            async def send_json(self, packet):
                sent.append(packet)
                connection = clients[env]
                connection["response_queue"][packet["request_id"]] = {"success": True}
                connection["response_events"][packet["request_id"]].set()
        return Socket()

    clients = {"staging": {"online": True, "ai_tools": True}, "secret": {"online": True, "ai_tools": True}}
    for env, connection in clients.items():
        connection["ws"] = socket_for(env)
    app = internal_app(monkeypatch, clients, utils)
    headers = {"X-Kubedoor-Token": "secret-test-token", "X-User-Name": "limited"}
    async with TestClient(TestServer(app)) as client:
        body = await (await client.get("/api/ai/internal/bootstrap", headers=headers)).json()
        # 看不到的集群连同别名一起隐藏
        assert [(c["env"], c["permission"], c["aliases"]) for c in body["clusters"]] == [
            ("prod", "rw", ["生产"]), ("staging", "read", [])]
        # 看不到的集群不能执行任何工具
        response = await client.post("/api/ai/internal/agent-execute?env=secret", headers=headers,
                                      json={"operation": "api", "phase": "prepare"})
        assert response.status == 403
        # 只读集群不能批准写操作
        response = await client.post("/api/ai/internal/agent-execute?env=staging", headers=headers,
                                      json={"operation": "api", "phase": "execute", "approved": True})
        assert response.status == 403
        # 只读集群可以执行只读工具,agent 收到的权限是 read
        response = await client.post("/api/ai/internal/agent-execute?env=staging", headers=headers,
                                      json={"operation": "api", "phase": "execute"})
        assert response.status == 200
        assert sent[-1]["permission"] == "read" and sent[-1]["username"] == "limited"


@pytest.mark.asyncio
async def test_deployment_control_checks_cluster_and_names_and_verifies_through_agent(monkeypatch):
    monkeypatch.setenv("AI_INTERNAL_TOKEN", "secret-test-token")
    rows = {"k8s_agent_status": {"admission": True, "admission_namespace": '["team"]', "nms_not_confirm": False, "scheduler": False},
            "k8s_res_control": None}

    async def pg_fetchrow(sql, *args):
        return next(row for table, row in rows.items() if table in sql)

    sent = []

    class Socket:
        async def send_json(self, packet):
            sent.append(packet)
            found = packet["arguments"]["path"].endswith("kubedoor-admis-configuration")
            clients["staging"]["response_queue"][packet["request_id"]] = {"success": True, "data": {"metadata": {}}} if found else \
                {"success": True, "data": {"metadata": {"labels": {}}}}
            clients["staging"]["response_events"][packet["request_id"]].set()

    clients = {"staging": {"online": True, "ai_tools": True, "ws": Socket()}}
    utils = fake_utils(get_k8s_names=AsyncMock(return_value=["staging"]), pg_fetchrow=pg_fetchrow,
                       prom_type="", tag_key="k8s")
    app = internal_app(monkeypatch, clients, utils)
    headers = {"X-Kubedoor-Token": "secret-test-token", "X-User-Name": "limited"}
    async with TestClient(TestServer(app)) as client:
        assert (await client.get("/api/ai/internal/deployment-control?env=secret&namespace=team&deployment=api", headers=headers)).status == 403
        assert (await client.get("/api/ai/internal/deployment-control?env=staging&namespace=../x&deployment=api", headers=headers)).status == 400
        body = await (await client.get("/api/ai/internal/deployment-control?env=staging&namespace=team&deployment=api", headers=headers)).json()
        # 只读集群也能查;核实走 agent 的只读 api GET
        assert body["data"]["state"] == "unregistered_blocked" and body["data"]["blocked"] is True
        assert body["data"]["admission"]["webhook_present"] is True and body["data"]["admission"]["namespace_ignored"] is False
        assert {packet["permission"] for packet in sent} == {"read"}


@pytest.mark.asyncio
async def test_rpc_registers_before_fast_reply_and_never_replays_timeout():
    connection = {"online": True}
    packets = []

    class ImmediateSocket:
        async def send_json(self, packet):
            packets.append(packet)
            key = packet["request_id"]
            assert key in connection["response_events"]
            assert key in connection["ai_request_ids"]
            connection["response_queue"][key] = {"success": True, "data": "ok"}
            connection["response_events"][key].set()

    connection["ws"] = ImmediateSocket()
    assert (await ai_api.agent_rpc({"prod": connection}, "prod", {"phase": "execute"}))["success"]
    assert not connection["response_events"] and not connection["ai_request_ids"]

    class SilentSocket:
        async def send_json(self, packet):
            packets.append(packet)

    connection["ws"] = SilentSocket()
    before = len(packets)
    with pytest.raises(web.HTTPGatewayTimeout):
        await ai_api.agent_rpc({"prod": connection}, "prod", {"phase": "execute"}, timeout=.01)
    assert len(packets) == before + 1
    assert not connection["response_events"] and not connection["response_queue"]


@pytest.mark.asyncio
async def test_cancelled_rpc_cleans_pending_response_state():
    connection = {"online": True, "ws": SimpleNamespace(send_json=AsyncMock())}
    task = asyncio.create_task(ai_api.agent_rpc({"prod": connection}, "prod", {}))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not connection["response_events"] and not connection["ai_request_ids"]


@pytest.mark.asyncio
async def test_rpc_timeout_includes_blocked_websocket_send():
    connection = {"online": True}

    class BlockedSocket:
        async def send_json(self, packet):
            await asyncio.Event().wait()

    connection["ws"] = BlockedSocket()
    with pytest.raises(web.HTTPGatewayTimeout):
        await ai_api.agent_rpc({"prod": connection}, "prod", {}, timeout=.01)
    assert not connection["response_events"] and not connection["ai_request_ids"]


@pytest.mark.asyncio
async def test_metrics_bridge_preserves_tenant_path_decodes_auth_and_injects_cluster(monkeypatch):
    monkeypatch.setenv("AI_INTERNAL_TOKEN", "test-token")
    received = []

    async def metrics(request):
        received.append((request.path, dict(request.query), request.headers["Authorization"]))
        return web.json_response({"status": "success", "data": {"result": []}})

    upstream = web.Application()
    upstream.router.add_get("/select/7/prometheus/api/v1/query", metrics)
    async with TestServer(upstream) as server:
        address = str(server.make_url("/select/7/prometheus")).split("://", 1)[1]
        utils = fake_utils(get_k8s_names=AsyncMock(return_value=["prod"]), prom_type="Victoria-Metrics-Cluster",
                           tag_key="k8s", prom_url=f"http://metrics-user:password%40with%3Aencoding@{address}")
        app = internal_app(monkeypatch, {}, utils)
        async with TestClient(TestServer(app)) as client:
            headers = {"X-Kubedoor-Token": "test-token", "X-User-Name": "reader", "X-User-Permission": "read"}
            response = await client.post("/api/ai/internal/metrics", json={"env": "prod", "query": "sum(up)", "extra_label": "k8s=other"}, headers=headers)
            assert response.status == 200, await response.text()
            # 时序库配置每次请求取:改成 Prometheus 后马上不支持自由查询,清空查询地址后提示未配置
            utils.settings.update(prom_type="Prometheus")
            assert (await client.post("/api/ai/internal/metrics", json={"env": "prod", "query": "up"}, headers=headers)).status == 501
            utils.settings.update(prom_type="Victoria-Metrics-Single", prom_url="")
            assert (await client.post("/api/ai/internal/metrics", json={"env": "prod", "query": "up"}, headers=headers)).status == 503
    assert len(received) == 1
    path, params, authorization = received[0]
    assert path == "/select/7/prometheus/api/v1/query"
    assert params == {"query": "sum(up)", "extra_label": "k8s=prod"}
    assert base64.b64decode(authorization.split()[1]).decode() == "metrics-user:password@with:encoding"


def agent_clients(sent, *envs):
    """假 agent:记下 master 发来的包,立即回成功。"""
    clients = {}

    def socket_for(env):
        class Socket:
            async def send_json(self, packet):
                sent.append(packet)
                connection = clients[env]
                connection["response_queue"][packet["request_id"]] = {"success": True}
                connection["response_events"][packet["request_id"]].set()
        return Socket()

    for env in envs:
        clients[env] = {"online": True, "ai_tools": True, "ws": socket_for(env)}
    return clients


@pytest.mark.asyncio
async def test_execute_checks_declared_operations(monkeypatch, set_policy):
    monkeypatch.setenv("AI_INTERNAL_TOKEN", "secret-test-token")
    utils = fake_utils(get_k8s_aliases=AsyncMock(return_value={}), prom_type="", tag_key="origin_prometheus")
    sent = []
    app = internal_app(monkeypatch, agent_clients(sent, "prod", "staging"), utils)
    headers = {"X-Kubedoor-Token": "secret-test-token", "X-User-Name": "limited"}

    async def execute(env, status, **body):
        response = await client.post(f"/api/ai/internal/agent-execute?env={env}", headers=headers,
                                     json={"phase": "execute", **body})
        assert response.status == status, await response.text()
        return response

    async with TestClient(TestServer(app)) as client:
        # 只读集群:声明的操作没有权限,批准执行时 403,不发给 agent
        denied = await execute("staging", 403, operation="pod_exec", approved=True, ops=["pod.exec.view"])
        assert "「只读」权限" in await denied.text() and "AI 在 Pod 内执行查看命令" in await denied.text()
        assert sent == []
        # 读写集群:下发 ops、该集群允许的全部操作、命令清单,旧 agent 用的 permission 是 rw
        await execute("prod", 200, operation="kubectl", approved=True, ops=["ai.k8s_write", "resource.delete"])
        packet = sent[-1]
        assert packet["ops"] == ["ai.k8s_write", "resource.delete"] and packet["permission"] == "rw"
        assert "pod.delete" in packet["allowed_ops"] and "network.coredns" not in packet["allowed_ops"]
        assert "find -delete" in packet["commands"]["danger"] and packet["approved"] is True and packet["env"] == "prod"
        # CoreDNS 默认只有管理员;批准执行必须声明操作;ops 格式不对 400
        await execute("prod", 403, operation="kubectl", approved=True, ops=["ai.k8s_write", "network.coredns"])
        await execute("prod", 403, operation="kubectl", approved=True, ops=[])
        await execute("prod", 400, operation="kubectl", approved=True, ops="resource.delete")
        assert len(sent) == 1
        # prepare 阶段(没批准)不检查,agent 自己按 allowed_ops 判断
        await execute("staging", 200, operation="pod_exec", phase="prepare", ops=["pod.exec.other"])
        assert sent[-1]["permission"] == "read" and sent[-1]["allowed_ops"] == []
        # 勾上只读列后,只读集群也能让 AI 执行查看命令
        set_policy({"pod.exec.view": {"read": True}})
        await execute("staging", 200, operation="pod_exec", approved=True, ops=["pod.exec.view"])
        assert sent[-1]["permission"] == "rw" and sent[-1]["allowed_ops"] == ["pod.exec.view"]
        # 升级前建的动作(没有 ops):按集群读写;master 判断不了 Pod 内命令,只读用户不能执行
        await execute("staging", 403, operation="pod_exec")
        await execute("prod", 200, operation="api", approved=True)
        assert sent[-1]["permission"] == "rw" and "allowed_ops" not in sent[-1] and "ops" not in sent[-1]

        response = await client.get("/api/ai/internal/permission-policy", headers=headers)
        policy = await response.json()
        assert response.status == 200 and policy["version"] == 0
        assert policy["ops"]["pod.exec.view"] == {"read": True, "rw": True, "admin": True, "approval": False}
        assert "rm" in policy["commands"]["danger"] and "ls" in policy["commands"]["view"]
        # 没有用户身份的内部调用不行
        assert (await client.get("/api/ai/internal/permission-policy",
                                 headers={"X-Kubedoor-Token": "secret-test-token"})).status == 403


@pytest.mark.asyncio
async def test_cluster_admin_identity_and_bootstrap(monkeypatch):
    monkeypatch.setenv("AI_INTERNAL_TOKEN", "secret-test-token")
    utils = fake_utils(get_k8s_aliases=AsyncMock(return_value={"prod": [], "staging": []}),
                       prom_type="", tag_key="origin_prometheus")
    app = internal_app(monkeypatch, {}, utils)
    headers = {"X-Kubedoor-Token": "secret-test-token", "X-User-Name": "boss"}
    async with TestClient(TestServer(app)) as client:
        identity = await (await client.get("/api/ai/internal/identity", headers=headers)).json()
        # 集群管理员:没单独授权的集群按「管理员」,clusters 只放单独授权的
        assert identity == {"username": "boss", "permission": "cluster_admin", "clusters": {"staging": "read"},
                            "default_permission": "admin"}
        body = await (await client.get("/api/ai/internal/bootstrap", headers=headers)).json()
        assert [(c["env"], c["permission"]) for c in body["clusters"]] == [("prod", "admin"), ("staging", "read")]


@pytest.mark.asyncio
async def test_data_queries_need_new_agent_and_data_query_permission(monkeypatch, set_policy):
    """MySQL / Redis / ES 只读查询:新版 agent(data_tools)才能执行;master 固定按「AI 查询数据库 / Redis / ES」复核;
    参数里服务端填入的明文密码只转给 agent,不进日志和审计"""
    monkeypatch.setenv("AI_INTERNAL_TOKEN", "secret-test-token")
    utils = fake_utils(get_k8s_aliases=AsyncMock(return_value={}), prom_type="", tag_key="origin_prometheus")
    sent, audits, logs = [], [], []
    clients = agent_clients(sent, "prod", "staging")
    clients["prod"]["data_tools"] = True  # staging 是只认通用工具的旧 agent
    monkeypatch.setattr(ai_api.audit, "record_bg",
                        lambda username, action, **kwargs: audits.append({"username": username, "action": action, **kwargs}))
    handler_id = logger.add(lambda message: logs.append(str(message)), level="DEBUG")
    app = internal_app(monkeypatch, clients, utils)
    headers = {"X-Kubedoor-Token": "secret-test-token", "X-User-Name": "limited"}  # prod 读写,staging 只读
    password = "S3cret-PW-9527"
    arguments = {"host": "db.example.com", "user": "app", "password": password, "sql": "SELECT 1"}

    async def execute(env, status, **body):
        response = await client.post(f"/api/ai/internal/agent-execute?env={env}", headers=headers,
                                     json={"phase": "execute", "arguments": arguments, "call_id": "call-1", **body})
        assert response.status == status, await response.text()
        return response

    try:
        async with TestClient(TestServer(app)) as client:
            body = await (await client.get("/api/ai/internal/bootstrap", headers=headers)).json()
            assert [(c["env"], c["ai_tools"], c["data_tools"]) for c in body["clusters"]] == [
                ("prod", True, True), ("staging", True, False)]
            # 读写集群 + 新 agent:原样转给 agent,带上 ops 和该集群允许的操作
            await execute("prod", 200, operation="mysql", approved=True, ops=["data.query"])
            packet = sent[-1]
            assert packet["operation"] == "mysql" and packet["arguments"] == arguments
            assert packet["ops"] == ["data.query"] and "data.query" in packet["allowed_ops"]
            # 旧 agent:409 JSON(kubedoor-ai 原样告诉模型),不发给 agent
            unsupported = await execute("staging", 409, operation="redis", approved=True, ops=["data.query"])
            assert await unsupported.json() == {
                "success": False, "code": "agent_unsupported",
                "message": "此 Agent 版本不支持数据库 / Redis / ES 查询，请升级 Agent",
                "error": "此 Agent 版本不支持数据库 / Redis / ES 查询，请升级 Agent"}
            clients["staging"]["data_tools"] = True
            # 只读集群默认不能查
            denied = await execute("staging", 403, operation="elasticsearch", approved=True, ops=["data.query"])
            assert "AI 查询数据库 / Redis / ES" in await denied.text()
            # 不管 AI 声明了什么(或者没声明),都按 data.query 复核
            set_policy({"data.query": {"rw": False}})
            await execute("prod", 403, operation="mysql", approved=True, ops=["pod.exec.view"])
            await execute("prod", 403, operation="mysql", approved=True)
            set_policy({})
            await execute("prod", 200, operation="redis", phase="prepare")
            assert sent[-1]["ops"] == ["data.query"] and sent[-1]["phase"] == "prepare"
            assert len(sent) == 2
            # 未知工具、timeout 不是数字:400
            await execute("prod", 400, operation="mongodb", approved=True, ops=["data.query"])
            await execute("prod", 400, operation="mysql", approved=True, ops=["data.query"], timeout="soon")
            # agent 不在线时照旧 503(由 AI 改用 KubeDoor 自己发起)
            clients["prod"]["online"] = False
            await execute("prod", 503, operation="mysql", approved=True, ops=["data.query"])
    finally:
        logger.remove(handler_id)
    # 审计只记 call_id 和 ops;日志里不能有参数里的密码
    assert [(item["action"], item["target"], item["detail"]) for item in audits] == [
        ("ai.agent_execute", "mysql", {"call_id": "call-1", "ops": ["data.query"]})]
    assert password not in str(audits) and not any(password in line for line in logs)
