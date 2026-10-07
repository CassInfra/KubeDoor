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

from kubedoor_ai.domain import Identity
from kubedoor_ai.gateway import RunContext
from kubedoor_ai.http import create_app
from kubedoor_ai.service import Service
from fakes import ScriptedModel


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
            assert value["pending"] and "/#/workbench/index?ai_session=" in value["browserlink"]
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
        assert (await h.http.post("/api/ai/connections/parse", json=candidate, headers=reader)).status_code == 403
        parsed = await h.http.post("/api/ai/connections/parse", json=candidate)
        assert parsed.status_code == 200 and parsed.json()["current_context"] == "test"
        tested = await h.http.post("/api/ai/connections/cluster-a/test", json=candidate)
        assert tested.status_code == 200, tested.text
        assert tested.json()["data"]["identity_status"] == "verified"
        saved = await h.http.put("/api/ai/connections/cluster-a", json=candidate)
        assert saved.status_code == 200, saved.text
        row = await h.service.store.connection("cluster-a")
        assert b"isolated-kube-token" not in row["encrypted_config"]
        encrypted = h.service.gateway.cipher.decrypt("cluster-a", bytes(row["encrypted_config"]))
        assert encrypted["users"][0]["user"]["token"] == "isolated-kube-token"
        listing = await h.http.get("/api/ai/connections")
        assert "isolated-kube-token" not in listing.text and "encrypted_config" not in listing.text
        failed = await h.http.put("/api/ai/connections/cluster-a", json={"content": "invalid", "context": "test"})
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
        assert (await h.http.delete("/api/ai/connections/cluster-a")).status_code == 200
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
