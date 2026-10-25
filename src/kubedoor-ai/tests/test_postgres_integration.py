"""Opt-in integration against an isolated localhost PostgreSQL database.

Set AI_TEST_PG_DATABASE (a name beginning kubedoor_ai_test), AI_TEST_PG_PORT,
AI_TEST_PG_USER and AI_TEST_PG_PASSWORD. No production services are contacted.
"""

import asyncio
import json
import os
import socket
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import httpx
import pytest
import uvicorn
from fastmcp import Client
from fastmcp.client.transports import SSETransport, StreamableHttpTransport
from langchain_core.messages import AIMessage

from kubedoor_ai.domain import Identity, Provider
from kubedoor_ai.gateway import RunContext
from kubedoor_ai.http import create_app
from kubedoor_ai.service import Service
from fakes import ScriptedModel, control_state


pytestmark = pytest.mark.skipif(not os.getenv("AI_TEST_PG_DATABASE"), reason="isolated PostgreSQL integration is opt-in")
TOKEN = "local-integration-token"
MODEL_KEY = "local-provider-private-key"
PROVIDER = {"base_url": "http://model.invalid/v1", "api_key": MODEL_KEY, "model": "local-test-model"}


class Harness:
    async def start(self):
        self.username = "integration-" + uuid.uuid4().hex
        self.headers = {"X-Kubedoor-Token": TOKEN, "X-User-Name": self.username, "X-User-Permission": "rw"}
        self.upstream = []
        self.service = await Service.open("http://master.invalid", TOKEN, "ab" * 32)
        await self.service.gateway.http.aclose()
        self.service.gateway.http = httpx.AsyncClient(transport=httpx.MockTransport(self.master))
        self.app = create_app(self.service, TOKEN)
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.url = f"http://127.0.0.1:{self.sock.getsockname()[1]}"
        self.server = uvicorn.Server(uvicorn.Config(self.app, access_log=False, log_level="error", lifespan="on"))
        self.server_task = asyncio.create_task(self.server.serve(sockets=[self.sock]))
        for _ in range(200):
            if self.server.started:
                break
            if self.server_task.done():
                await self.server_task
                raise AssertionError("test server failed to start")
            await asyncio.sleep(0.025)
        assert self.server.started
        self.http = httpx.AsyncClient(base_url=self.url, headers=self.headers, timeout=15, trust_env=False)
        return self

    def master(self, request):
        assert request.headers["X-Kubedoor-Token"] == TOKEN
        self.upstream.append((request.method, request.url.path, dict(request.url.params), json.loads(request.content) if request.content else None))
        if request.url.path == "/api/ai/internal/bootstrap":
            return httpx.Response(200, json={"clusters": [{"env": "cluster-a", "online": True}, {"env": "cluster-b", "online": True}]})
        if request.url.path == "/api/db/res/list":
            return httpx.Response(200, json={"meta": [{"name": "deployment"}, {"name": "xmx"}], "data": [["app", "512m"]]})
        if request.url.path == "/api/scale":
            return httpx.Response(200, json={"success": True, "data": {"replicas": 3}})
        if request.url.path == "/api/ai/internal/deployment-control":
            return httpx.Response(200, json={"success": True, "data": control_state()})
        return httpx.Response(200, json={"success": True, "data": []})

    def model(self, *responses):
        scripted = ScriptedModel(responses=list(responses))
        self.service.runtime.model_factory = lambda _: scripted

    async def restart_service(self):
        await self.service.close()
        self.service = await Service.open("http://master.invalid", TOKEN, "ab" * 32)
        await self.service.gateway.http.aclose()
        self.service.gateway.http = httpx.AsyncClient(transport=httpx.MockTransport(self.master))
        self.app.state.service = self.service

    async def session(self):
        response = await self.http.post("/api/ai/sessions", json={"title": "集成测试"})
        assert response.status_code == 200, response.text
        return response.json()["id"]

    async def wait(self, rid, status="completed"):
        for _ in range(400):
            run = await self.service.store.run(rid, Identity(self.username, "rw"))
            if run["status"] == status and rid not in self.service.runtime.tasks:
                return run
            if run["status"] == "failed" and status != "failed":
                raise AssertionError(run["error"])
            await asyncio.sleep(0.025)
        raise AssertionError(f"run did not reach {status}: {run}")

    async def close(self):
        self.server.should_exit = True
        await asyncio.wait_for(self.server_task, 10)
        await self.http.aclose()
        sessions = await self.service.store.fetch("SELECT id FROM kubedoor_ai_sessions WHERE username=$1", self.username)
        for row in sessions:
            await self.service.runtime.checkpointer.adelete_thread(row["id"])
        await self.service.store.execute("DELETE FROM kubedoor_ai_sessions WHERE username=$1", self.username)
        await self.service.store.execute("DELETE FROM kubedoor_ai_connections WHERE updated_by=$1", self.username)
        await self.service.close()


@pytest.fixture
def harness(monkeypatch):
    database = os.environ["AI_TEST_PG_DATABASE"]
    assert database.startswith("kubedoor_ai_test"), "use an isolated test database"
    for key, default in (("HOST", "127.0.0.1"), ("PORT", "5432"), ("USER", "postgres"), ("PASSWORD", ""), ("DATABASE", database)):
        monkeypatch.setenv("PG_" + key, os.getenv("AI_TEST_PG_" + key, default))
    assert os.environ["PG_HOST"] in ("127.0.0.1", "localhost", "::1")
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    runner = asyncio.Runner(loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None)
    instance = runner.run(Harness().start())
    try:
        yield instance, runner
    finally:
        runner.run(instance.close())
        runner.close()


def tool_message(operation, arguments):
    return AIMessage(content="准备调用", tool_calls=[{"name": "kubedoor_tool", "args": {"operation": operation, "arguments": arguments, "source": "kubedoor"}, "id": "provider-reuses-id", "type": "tool_call"}])


def test_persisted_multi_turn_sse_cluster_changes_and_secret_redaction(harness):
    h, runner = harness

    async def verify():
        async with httpx.AsyncClient(trust_env=False) as anonymous:
            assert (await anonymous.get(h.url + "/health")).status_code == 200
        assert (await h.http.get("/api/ai/bootstrap", headers={"X-Kubedoor-Token": "wrong"})).status_code == 401
        bootstrap = (await h.http.get("/api/ai/bootstrap")).json()
        assert len(bootstrap["clusters"]) == 2
        sid = await h.session()
        h.model(tool_message("resource_inventory", {"namespace": "other-ns"}), AIMessage(content="完成 " + MODEL_KEY),
                tool_message("resource_inventory", {"namespace": "other-ns"}), AIMessage(content="第二轮完成"))
        run_ids = []
        for env in ("cluster-a", "cluster-b"):
            response = await h.http.post(f"/api/ai/sessions/{sid}/runs", json={"message": "查询 " + MODEL_KEY, "scope": {"env": env, "namespace": "selected-ns"}, "provider": PROVIDER})
            assert response.status_code == 200, response.text
            rid = response.json()["run_id"]
            run_ids.append(rid)
            await h.wait(rid)
        detail = (await h.http.get(f"/api/ai/sessions/{sid}")).json()
        assert len(detail["messages"]) == 4
        assert [m["scope"]["env"] for m in detail["messages"]] == ["cluster-a", "cluster-a", "cluster-b", "cluster-b"]
        assert MODEL_KEY not in json.dumps(detail)
        assert [call[2]["env"] for call in h.upstream if call[1] == "/api/db/res/list"] == ["cluster-a", "cluster-b"]
        checkpoint = await h.service.runtime.checkpointer.aget_tuple({"configurable": {"thread_id": sid}})
        assert MODEL_KEY not in str(checkpoint)
        events = await h.http.get(f"/api/ai/runs/{run_ids[0]}/events?after=1")
        rows = [json.loads(line[6:]) for line in events.text.splitlines() if line.startswith("data: ")]
        assert rows and min(row["seq"] for row in rows) > 1
        assert rows[-1]["type"] == "done"
        assert MODEL_KEY not in events.text
        outsider = {**h.headers, "X-User-Name": "other-user"}
        assert (await h.http.get(f"/api/ai/sessions/{sid}", headers=outsider)).status_code == 404
        assert (await h.http.get(f"/api/ai/runs/{run_ids[0]}/events", headers=outsider)).status_code == 404

    runner.run(verify())


def test_postgres_interrupt_exact_approval_and_mcp_transports(harness):
    h, runner = harness

    async def verify():
        sid = await h.session()
        h.model(tool_message("scale_deployment", {"namespace": "other-ns", "deployment": "app", "replicas": 3}), AIMessage(content="修改完成"))
        started = await h.http.post(f"/api/ai/sessions/{sid}/runs", json={"message": "扩容", "scope": {"env": "cluster-a", "namespace": "selected-ns"}, "provider": PROVIDER})
        assert started.status_code == 200, started.text
        rid = started.json()["run_id"]
        await h.wait(rid, "waiting_approval")
        assert not any(call[1] == "/api/scale" for call in h.upstream)
        pending = (await h.http.get(f"/api/ai/sessions/{sid}")).json()["active_run"]["pending_actions"][0]
        other = {**h.headers, "X-User-Name": "other-user"}
        data = {"action_id": pending["action_id"], "decision": "approve", "provider": PROVIDER}
        assert (await h.http.post(f"/api/ai/runs/{rid}/decisions", json=data, headers=other)).status_code == 404
        approved = await h.http.post(f"/api/ai/runs/{rid}/decisions", json={**data, "arguments": {"replicas": 99}})
        assert approved.status_code == 200, approved.text
        await h.wait(rid)
        writes = [call for call in h.upstream if call[1] == "/api/scale"]
        assert len(writes) == 1 and writes[0][3] == [{"namespace": "other-ns", "deployment_name": "app", "num": 3}]
        assert (await h.http.post(f"/api/ai/runs/{rid}/decisions", json=data)).status_code == 200
        assert len([call for call in h.upstream if call[1] == "/api/scale"]) == 1
        for transport in (StreamableHttpTransport(h.url + "/mcp", headers=h.headers), SSETransport(h.url + "/sse", headers=h.headers)):
            async with Client(transport) as client:
                tools = await client.list_tools()
                assert len(tools) == 17
                result = await client.call_tool("get_k8s_list", {})
                assert not result.is_error
                resources = await client.list_resources()
                assert any(str(r.uri) == "skills://kubedoor-k8s" for r in resources)
        async with Client(StreamableHttpTransport(h.url + "/mcp", headers=h.headers)) as client:
            pending_result = await client.call_tool("scale_deployment", {"namespace": "team", "deployment": "mcp-app", "replicas": 2, "k8s": "cluster-a"})
            value = pending_result.data
            assert value["pending"] and "/#/monit/overview?ai_session=" in value["browserlink"]
            reject = await h.http.post(f"/api/ai/runs/{value['run_id']}/decisions", json={"action_id": value["action_id"], "decision": "reject"})
            assert reject.status_code == 200, reject.text
            assert len([call for call in h.upstream if call[1] == "/api/scale"]) == 1

    runner.run(verify())


def test_encrypted_kubeconfig_upload_test_resources_and_failed_replace(harness):
    h, runner = harness
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, value):
            data = json.dumps(value).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = urlsplit(self.path).path
            calls.append(("GET", path))
            if path == "/version":
                self.reply({"gitVersion": "v1.34.0"})
            elif path == "/api/v1/namespaces":
                self.reply({"items": [{"metadata": {"name": "team"}}]})
            else:
                self.reply({"items": [], "groups": [], "versions": ["v1"]})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(("POST", self.path))
            if body.get("kind") == "SelfSubjectReview":
                self.reply({"status": {"userInfo": {"username": "test-operator"}}})
            else:
                self.reply({"status": {"allowed": True}})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = {"apiVersion": "v1", "kind": "Config", "current-context": "test", "contexts": [{"name": "test", "context": {"cluster": "test", "user": "test"}}],
              "clusters": [{"name": "test", "cluster": {"server": f"http://127.0.0.1:{server.server_port}"}}],
              "users": [{"name": "test", "user": {"token": "isolated-kube-token"}}]}

    async def verify():
        candidate = {"content": json.dumps(config), "context": "test"}
        reader = {**h.headers, "X-User-Permission": "read"}
        admin = {**h.headers, "X-User-Permission": "admin"}
        assert (await h.http.post("/api/ai/connections/parse", json=candidate, headers=reader)).status_code == 403
        # kubeconfig 直连属于集群接入配置,读写用户也不能改,只有管理员可以
        assert (await h.http.post("/api/ai/connections/parse", json=candidate)).status_code == 403
        parsed = await h.http.post("/api/ai/connections/parse", json=candidate, headers=admin)
        assert parsed.status_code == 200 and parsed.json()["current_context"] == "test"
        tested = await h.http.post("/api/ai/connections/cluster-a/test", json=candidate, headers=admin)
        assert tested.status_code == 200, tested.text
        assert tested.json()["data"]["identity_status"] == "verified"
        saved = await h.http.put("/api/ai/connections/cluster-a", json=candidate, headers=admin)
        assert saved.status_code == 200, saved.text
        row = await h.service.store.connection("cluster-a")
        assert b"isolated-kube-token" not in row["encrypted_config"]
        encrypted = h.service.gateway.cipher.decrypt("cluster-a", bytes(row["encrypted_config"]))
        assert encrypted["users"][0]["user"]["token"] == "isolated-kube-token"
        listing = await h.http.get("/api/ai/connections")
        assert "isolated-kube-token" not in listing.text and "encrypted_config" not in listing.text
        failed = await h.http.put("/api/ai/connections/cluster-a", json={"content": "invalid", "context": "test"}, headers=admin)
        assert failed.status_code == 400
        assert (await h.service.store.connection("cluster-a"))["revision"] == row["revision"]
        context = RunContext({"scope": {"env": "cluster-a"}}, Identity(h.username, "rw"))
        try:
            direct = await h.service.gateway.execute_action(context, {"source": "direct", "operation": "api", "arguments": {"method": "GET", "path": "/api/v1/namespaces", "query": {"limit": "500"}}, "call_id": "selector-check", "read_only": True})
            assert direct["success"] and isinstance(direct["data"], dict), direct
        finally:
            await h.service.gateway.close_context(context)
        namespaces = await h.http.get("/api/ai/resources", params={"env": "cluster-a", "kind": "namespaces"})
        assert namespaces.status_code == 200, namespaces.text
        assert namespaces.json()["items"] == [{"name": "team"}]
        assert namespaces.json()["source"] == "direct"
        assert (await h.http.delete("/api/ai/connections/cluster-a", headers=reader)).status_code == 403
        assert (await h.http.delete("/api/ai/connections/cluster-a", headers=admin)).status_code == 200
        assert await h.service.store.connection("cluster-a") is None

    try:
        runner.run(verify())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_pending_approval_survives_restart_and_cancelled_tools_do_not_revive(harness):
    h, runner = harness

    async def start_write(sid):
        h.model(tool_message("scale_deployment", {"namespace": "team", "deployment": "app", "replicas": 3}), AIMessage(content="写入完成"))
        response = await h.http.post(f"/api/ai/sessions/{sid}/runs", json={"message": "扩容", "scope": {"env": "cluster-a"}, "provider": PROVIDER})
        assert response.status_code == 200, response.text
        rid = response.json()["run_id"]
        await h.wait(rid, "waiting_approval")
        return rid

    async def verify():
        sid = await h.session()
        rid = await start_write(sid)
        action = (await h.service.store.pending(rid))[0]
        await h.restart_service()
        h.model(AIMessage(content="重启后恢复完成"))
        detail = (await h.http.get(f"/api/ai/sessions/{sid}")).json()
        assert detail["active_run"]["status"] == "waiting_approval"
        assert detail["active_run"]["pending_actions"][0]["action_id"] == action["action_id"]
        approved = await h.http.post(f"/api/ai/runs/{rid}/decisions", json={"action_id": action["action_id"], "decision": "approve", "provider": PROVIDER})
        assert approved.status_code == 200, approved.text
        await h.wait(rid)
        assert len([call for call in h.upstream if call[1] == "/api/scale"]) == 1
        cancelled_rid = await start_write(sid)
        cancelled = await h.http.post(f"/api/ai/runs/{cancelled_rid}/cancel")
        assert cancelled.status_code == 200, cancelled.text
        await h.wait(cancelled_rid, "cancelled")
        h.model(tool_message("resource_inventory", {}), AIMessage(content="新一轮只查询"))
        new_round = await h.http.post(f"/api/ai/sessions/{sid}/runs", json={"message": "改为查询另一个集群", "scope": {"env": "cluster-b"}, "provider": PROVIDER})
        assert new_round.status_code == 200, new_round.text
        await h.wait(new_round.json()["run_id"])
        assert len([call for call in h.upstream if call[1] == "/api/scale"]) == 1
        assert [call for call in h.upstream if call[1] == "/api/db/res/list"][-1][2]["env"] == "cluster-b"
        actions = await h.service.store.fetch("SELECT operation FROM kubedoor_ai_actions WHERE run_id=$1::uuid", new_round.json()["run_id"])
        assert [row["operation"] for row in actions] == ["resource_inventory"]

    runner.run(verify())


def test_crash_recovery_keeps_dispatched_writes_unknown(harness):
    h, runner = harness

    async def verify():
        identity = Identity(h.username, "rw")
        session = await h.service.new_session(identity, "模拟未确认结果的写请求")
        run, _ = await h.service.new_run(identity, session["id"], {"env": "cluster-a"}, origin="mcp")
        action_id = str(uuid.uuid4())
        await h.service.store.execute("INSERT INTO kubedoor_ai_actions(id,run_id,call_id,operation,source,arguments,preview,read_only,state) VALUES($1::uuid,$2::uuid,$3,'scale_deployment','kubedoor',$4,$5,false,'dispatching')", action_id, run["id"], run["id"] + ":dispatched", {"namespace": "team", "deployment": "app", "replicas": 3}, {})
        await h.restart_service()
        recovered = await h.service.store.run(run["id"], identity)
        assert recovered["status"] == "failed"
        action = await h.service.store.one("SELECT state,result FROM kubedoor_ai_actions WHERE id=$1::uuid", action_id)
        assert action["state"] == "unknown"
        assert action["result"]["error"]["code"] == "outcome_unknown"
        response = await h.http.post(f"/api/ai/runs/{run['id']}/decisions", json={"action_id": action_id, "decision": "approve"})
        assert response.status_code == 200 and response.json()["action_status"] == "unknown"
        assert not any(call[1] == "/api/scale" for call in h.upstream)
        replay = await h.http.get(f"/api/ai/runs/{run['id']}/events")
        assert '"runtime_restarted"' in replay.text and '"done"' in replay.text

    runner.run(verify())


def test_wecom_conversation_boundaries_use_one_locked_transaction(harness):
    """企微会话边界的 SQL 在真实 PostgreSQL 上执行：advisory lock、空闲切换、过期审批、继续上次。"""
    h, runner = harness

    async def verify():
        from kubedoor_ai.wecom import WeComIntegration
        store, wecom = h.service.store, WeComIntegration(h.service)
        userid, identity = "wx-" + uuid.uuid4().hex, Identity(h.username, "rw")
        await store.execute("INSERT INTO kubedoor_ai_wecom_users(wecom_userid) VALUES($1)", userid)
        try:
            first, notices, active = await wecom._current_session(userid, identity)
            assert (notices, active) == ([], None)
            row = await store.one("SELECT channel,channel_key FROM kubedoor_ai_sessions WHERE id=$1::uuid", first)
            assert row == {"channel": "wecom", "channel_key": f"{userid}:{h.username}:{first}"}
            assert (await wecom._current_session(userid, identity))[0] == first
            await store.execute("INSERT INTO kubedoor_ai_messages(id,session_id,role,content) VALUES($1::uuid,$2::uuid,'user','查询 Pod')",
                                str(uuid.uuid4()), first)

            # 空闲 3 小时后三条消息同时到达：只建一个新对话，只有一条带提示
            await store.execute("UPDATE kubedoor_ai_sessions SET updated_at=now()-interval '3 hours' WHERE id=$1::uuid", first)
            results = await asyncio.gather(*(wecom._current_session(userid, identity) for _ in range(3)))
            second = results[0][0]
            assert second != first and {r[0] for r in results} == {second}
            assert sum(bool(r[1]) for r in results) == 1
            member = await store.one("SELECT session_id,previous_session_id FROM kubedoor_ai_wecom_users WHERE wecom_userid=$1", userid)
            assert member == {"session_id": second, "previous_session_id": first}

            # 继续上次：切回并刷新最后活跃时间，紧接着的消息不会被空闲切走
            assert (await wecom._resume_previous_session(userid, identity)).startswith("已回到上一个对话（最后活跃：")
            assert await wecom._current_session(userid, identity) == (first, [], None)

            # 只剩过期审批的任务先被停止
            run_id = str(uuid.uuid4())
            await store.execute("INSERT INTO kubedoor_ai_runs(id,session_id,username,permission,status,scope,origin) "
                                "VALUES($1::uuid,$2::uuid,$3,'rw','waiting_approval',$4,'wecom')", run_id, first, h.username, {"env": "cluster-a"})
            await store.execute("INSERT INTO kubedoor_ai_actions(id,run_id,call_id,operation,source,arguments,preview,read_only,state,expires_at) "
                                "VALUES($1::uuid,$2::uuid,$3,'delete_pod','kubedoor','{}','{}',false,'pending',now()-interval '1 minute')",
                                str(uuid.uuid4()), run_id, run_id + ":expired")
            session_id, notices, active = await wecom._current_session(userid, identity)
            assert (session_id, notices, active) == (first, ["上一个待审批的操作已过期，已停止该任务。"], None)
            assert (await store.one("SELECT status FROM kubedoor_ai_runs WHERE id=$1::uuid", run_id))["status"] == "cancelled"

            # 新对话：当前对话记为上一个
            assert await wecom._start_new_session(userid, identity) == "已开始新对话。"
            member = await store.one("SELECT session_id,previous_session_id FROM kubedoor_ai_wecom_users WHERE wecom_userid=$1", userid)
            assert member["previous_session_id"] == first and member["session_id"] not in (first, second)
        finally:
            await store.execute("DELETE FROM kubedoor_ai_wecom_users WHERE wecom_userid=$1", userid)

    runner.run(verify())


def test_unreachable_old_wecom_conversation_loses_its_checkpoint_but_keeps_messages(harness):
    h, runner = harness

    async def verify():
        from kubedoor_ai.wecom import WeComIntegration
        store, wecom, checkpointer = h.service.store, WeComIntegration(h.service), h.service.runtime.checkpointer
        userid, identity = "wx-" + uuid.uuid4().hex, Identity(h.username, "rw")
        await store.execute("INSERT INTO kubedoor_ai_wecom_users(wecom_userid) VALUES($1)", userid)
        try:
            old, _, _ = await wecom._current_session(userid, identity)
            h.model(AIMessage(content="查过了，一切正常"))
            run, _ = await h.service.new_run(identity, old, {"env": "cluster-a"}, origin="wecom", channel="wecom")
            await store.message(run, "user", "查一下")
            await h.service.runtime.run_graph(RunContext(run, identity), Provider.parse(PROVIDER), "查一下", None)
            assert await checkpointer.aget_tuple({"configurable": {"thread_id": old}}) is not None
            # 两次「新对话」（中间那个对话有消息）之后，old 既不是当前对话也不是上一个对话
            await wecom._start_new_session(userid, identity)
            middle = (await store.one("SELECT session_id FROM kubedoor_ai_wecom_users WHERE wecom_userid=$1", userid))["session_id"]
            await store.execute("INSERT INTO kubedoor_ai_messages(id,session_id,role,content) VALUES($1::uuid,$2::uuid,'user','再问一句')",
                                str(uuid.uuid4()), middle)
            await wecom._start_new_session(userid, identity)
            await store.execute("UPDATE kubedoor_ai_sessions SET updated_at=now()-interval '8 days' WHERE id=ANY($1::uuid[])", [old, middle])
            assert await wecom.purge_checkpoints(7) >= 1
            assert await checkpointer.aget_tuple({"configurable": {"thread_id": old}}) is None
            row = await store.one("SELECT checkpoint_purged_at,(SELECT count(*) FROM kubedoor_ai_messages m WHERE m.session_id=s.id) AS messages "
                                  "FROM kubedoor_ai_sessions s WHERE id=$1::uuid", old)
            assert row["checkpoint_purged_at"] and row["messages"] == 2   # 消息记录保留用于审计
            # 上一个对话（middle）还能「继续上次」，不清理；旧卡片切不回已清理的 old
            assert (await store.one("SELECT checkpoint_purged_at FROM kubedoor_ai_sessions WHERE id=$1::uuid", middle))["checkpoint_purged_at"] is None
            assert await wecom._continue_session(userid, identity, old) == "该对话已不可用，直接发送问题即可开始新对话。"
        finally:
            await store.execute("DELETE FROM kubedoor_ai_wecom_users WHERE wecom_userid=$1", userid)

    runner.run(verify())


def test_regenerate_and_edit_replace_only_a_safe_last_turn_and_search_finds_sessions(harness):
    h, runner = harness

    async def verify():
        sid = await h.session()
        h.model(AIMessage(content="第一轮回答"), AIMessage(content="第二轮旧回答"), AIMessage(content="第二轮新回答"))
        for text in ("order-api 为什么重启", "继续看看内存"):
            response = await h.http.post(f"/api/ai/sessions/{sid}/runs", json={"message": text, "scope": {"env": "cluster-a"}, "provider": PROVIDER})
            await h.wait(response.json()["run_id"])
        edit = {"message": "继续看看内存（换个问法）", "scope": {"env": "cluster-a"}, "provider": PROVIDER, "replace_last": True}
        response = await h.http.post(f"/api/ai/sessions/{sid}/runs", json=edit, headers={"Idempotency-Key": "edit-1"})
        assert response.status_code == 200, response.text
        replacement = response.json()["run_id"]
        await h.wait(replacement)
        # 同一个幂等键重试返回同一个 run，不会被自己刚建的 run 挡住
        again = await h.http.post(f"/api/ai/sessions/{sid}/runs", json=edit, headers={"Idempotency-Key": "edit-1"})
        assert again.status_code == 200 and again.json()["run_id"] == replacement
        detail = (await h.http.get(f"/api/ai/sessions/{sid}")).json()
        assert [m["content"] for m in detail["messages"]] == ["order-api 为什么重启", "第一轮回答", "继续看看内存（换个问法）", "第二轮新回答"]
        replaced = await h.service.store.fetch("SELECT content FROM kubedoor_ai_messages WHERE session_id=$1::uuid AND replaced_at IS NOT NULL ORDER BY created_at", sid)
        assert [row["content"] for row in replaced] == ["继续看看内存", "第二轮旧回答"]   # 保留用于追溯
        # 检查点里也只剩第一轮和替换后的第二轮
        probe = RunContext({"id": "probe", "session_id": sid, "scope": {"env": "cluster-a", "namespace": None, "deployment": None, "pod": None}, "skill_ids": []}, Identity(h.username, "rw"))
        state = await h.service.runtime.graph(probe, Provider.parse(PROVIDER)).aget_state({"configurable": {"thread_id": sid}})
        contents = " ".join(str(message.content) for message in state.values["messages"])
        assert "第二轮新回答" in contents and "第二轮旧回答" not in contents and contents.count("继续看看内存") == 1
        # 搜索标题和消息内容，不分大小写；% 按字面匹配；被替换的旧消息搜不到
        found = (await h.http.get("/api/ai/sessions", params={"q": "ORDER-API"})).json()["sessions"]
        assert [row["id"] for row in found] == [sid] and "order-api 为什么重启" in found[0]["snippet"]
        assert (await h.http.get("/api/ai/sessions", params={"q": "100%"})).json()["sessions"] == []
        assert (await h.http.get("/api/ai/sessions", params={"q": "第二轮旧回答"})).json()["sessions"] == []
        # 最后一轮执行过修改就不能再替换（结果未知的也不行）
        await h.service.store.execute(
            "INSERT INTO kubedoor_ai_actions(id,run_id,call_id,operation,source,arguments,preview,read_only,state,result) "
            "VALUES($1::uuid,$2::uuid,$3,'scale_deployment','kubedoor','{}','{}',false,'failed',$4)",
            str(uuid.uuid4()), replacement, "call-" + uuid.uuid4().hex, {"success": False, "error": {"code": "partial_failure"}})
        denied = await h.http.post(f"/api/ai/sessions/{sid}/runs", json=edit)
        assert denied.status_code == 409 and denied.json()["error"]["code"] == "turn_has_writes"

    runner.run(verify())
