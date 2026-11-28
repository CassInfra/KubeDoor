"""公网分享：脱敏快照、Cloudflare 客户端和 Service 的发布 / 取消 / 配置"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from starlette.testclient import TestClient

from kubedoor_ai.domain import AIError, CredentialCipher, Identity
from kubedoor_ai.http import create_app
from kubedoor_ai.public_share import (
    CONFIG_AAD, CloudflareShare, PublicShareConfig, build_public_snapshot,
    public_share_url, redact_public_text, render_public_share,
)
from kubedoor_ai.service import Service

CODE = "A" * 21 + "_"
ALICE = Identity("alice", "rw")
ADMIN = Identity("admin", "admin")
KEY = "11" * 32
CONFIG = PublicShareConfig(account_id="a" * 32, api_token="cf-token", worker_name="kubedoor-ai-share",
                           kv_namespace_id="b" * 32, public_base_url="https://share.example.com")


def test_config_validation_and_public_view():
    raw = {"account_id": "A" * 32, "api_token": "cf-token", "worker_name": "kubedoor-ai-share",
           "kv_namespace_id": "B" * 32, "public_base_url": "https://share.example.com/"}
    config = PublicShareConfig.parse(raw)
    assert config.account_id == "a" * 32 and config.kv_namespace_id == "b" * 32
    assert config.public_base_url == "https://share.example.com"
    assert config.public() == {"account_id": "a" * 32, "worker_name": "kubedoor-ai-share",
                               "kv_namespace_id": "b" * 32, "public_base_url": "https://share.example.com",
                               "api_token_set": True}
    # API Token 留空 / ****** 沿用当前值
    assert PublicShareConfig.parse({**raw, "api_token": ""}, config).api_token == "cf-token"
    assert PublicShareConfig.parse({**raw, "api_token": "******"}, config).api_token == "cf-token"
    for broken in (
        {**raw, "account_id": "not-hex"},
        {**raw, "api_token": ""},
        {**raw, "worker_name": "Bad Name"},
        {**raw, "kv_namespace_id": "xyz"},
        {**raw, "public_base_url": "ftp://share.example.com"},
        {**raw, "public_base_url": "https://user:pass@share.example.com"},
        {**raw, "public_base_url": ""},
    ):
        with pytest.raises(AIError):
            PublicShareConfig.parse(broken)
    assert public_share_url("https://share.example.com/", "abc") == "https://share.example.com/s/abc"


def test_public_text_redaction():
    text = ("密码：abc123 token=xyz789 Bearer abc.def.ghi\n"
            "IP 10.1.2.3，邮箱 ops@example.com，手机 13800138000，身份证 110101199001011234\n"
            "地址 https://user:pass@example.com/path 和 https://ops@example.com/x，"
            "key sk-abcdefghijklmnopqrstuvwx")
    value = redact_public_text(text)
    for secret in ("abc123", "xyz789", "abc.def.ghi", "10.1.2.3", "ops@example.com",
                   "13800138000", "110101199001011234", "user:pass", "ops@",
                   "sk-abcdefghijklmnopqrstuvwx"):
        assert secret not in value
    assert "[REDACTED]" in value and "[IP已脱敏]" in value and "[手机号已脱敏]" in value


def test_snapshot_keeps_only_tool_names_and_hides_mcp_content():
    rows = [
        {"id": "m1", "role": "user", "content": "看一下 10.0.0.1", "tools": None, "created_at": "2026-11-28T10:00:00+08:00", "origin": "chat"},
        {"id": "m2", "role": "assistant", "content": "正常", "created_at": "2026-11-28T10:01:00+08:00", "origin": "chat",
         "tools": [{"name": "logs", "state": "completed", "arguments": {"q": "secret-arg"}, "result": "secret-result"}]},
        {"id": "m3", "role": "assistant", "content": '{"data": "mcp-secret"}', "created_at": "2026-11-28T10:02:00+08:00",
         "origin": "mcp", "tools": [{"name": "mcp_tool", "status": "success", "result": "mcp-result"}]},
    ]
    snapshot = build_public_snapshot({"title": "排查 <prod>"}, rows, "running", "2026-11-28T02:00:00+00:00")
    assert snapshot["title"] == "排查 <prod>" and snapshot["active_run"] == "running"
    assert snapshot["message_count"] == 3
    assert snapshot["messages"][0]["content"] == "看一下 [IP已脱敏]"
    assert snapshot["messages"][1]["tools"] == [{"name": "logs", "status": "completed"}]
    assert snapshot["messages"][2]["content"] == "" and snapshot["messages"][2]["mcp"] is True
    page = render_public_share(snapshot)
    assert page.startswith("<!doctype html>")
    assert "公网分享不支持查看工具调用明细" in page
    assert "logs" in page and "mcp_tool" in page
    assert "secret-arg" not in page and "secret-result" not in page and "mcp-secret" not in page


def test_render_escapes_markdown_and_does_not_load_images():
    page = render_public_share(build_public_snapshot(
        {"title": "<b>标题</b>"},
        [{"role": "assistant", "content": "**hi** <script>alert(1)</script> ![x](https://evil.example/pixel)",
          "created_at": "2026-11-28T10:00:00+08:00"}],
        None, "2026-11-28T02:00:00+00:00"))
    assert "<strong>hi</strong>" in page
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page
    assert "evil.example" not in page and "[图片：x]" in page


def test_snapshot_page_is_self_contained_and_has_security_meta():
    page = render_public_share(build_public_snapshot(
        {"title": "测试"}, [{"role": "user", "content": "你好", "created_at": "2026-11-28T10:00:00+08:00"}],
        None, "2026-11-28T02:00:00+00:00"))
    assert 'name="robots" content="noindex, nofollow"' in page
    assert 'name="referrer" content="no-referrer"' in page
    assert "<details class=\"tool\">" not in page
    assert "你好" in page


def mock_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_cloudflare_verify_checks_token_workers_and_namespace():
    calls = []

    def handler(request):
        path = request.url.path.removeprefix("/client/v4")
        calls.append((request.method, path))
        assert request.headers["Authorization"] == "Bearer cf-token"
        if path == "/user/tokens/verify":
            return httpx.Response(200, json={"success": True, "result": {"status": "active"}})
        if path == f"/accounts/{CONFIG.account_id}/workers/scripts":
            return httpx.Response(200, json={"success": True, "result": []})
        if path == f"/accounts/{CONFIG.account_id}/storage/kv/namespaces/{CONFIG.kv_namespace_id}":
            return httpx.Response(200, json={"success": True, "result": {"id": CONFIG.kv_namespace_id}})
        return httpx.Response(404, json={"success": False, "errors": [{"code": 7003, "message": "not found"}]})

    share = CloudflareShare(CONFIG, client=mock_client(handler))
    result = await share.verify()
    assert result["success"] is True and result["worker_name"] == "kubedoor-ai-share"
    assert calls == [("GET", "/user/tokens/verify"),
                     ("GET", f"/accounts/{CONFIG.account_id}/workers/scripts"),
                     ("GET", f"/accounts/{CONFIG.account_id}/storage/kv/namespaces/{CONFIG.kv_namespace_id}")]


@pytest.mark.asyncio
async def test_cloudflare_namespace_reuse_and_create():
    listed = [{"id": "c" * 32, "title": "kubedoor-ai-share-kv"}]

    def handler(request):
        path = request.url.path.removeprefix("/client/v4")
        if request.method == "GET" and path == f"/accounts/{CONFIG.account_id}/storage/kv/namespaces":
            return httpx.Response(200, json={"success": True, "result": listed})
        if request.method == "POST":
            return httpx.Response(200, json={"success": True, "result": {"id": "d" * 32}})
        return httpx.Response(500, json={"success": False, "errors": [{"code": 1, "message": "boom"}]})

    config = PublicShareConfig(**{**CONFIG.__dict__, "kv_namespace_id": ""})
    share = CloudflareShare(config, client=mock_client(handler))
    assert await share.ensure_namespace() == "c" * 32
    listed.clear()
    assert await share.ensure_namespace() == "d" * 32


@pytest.mark.asyncio
async def test_cloudflare_deploy_puts_worker_with_kv_binding():
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["path"] = request.url.path.removeprefix("/client/v4")
        seen["body"] = request.content
        return httpx.Response(200, json={"success": True, "result": {"id": "script-1"}})

    share = CloudflareShare(CONFIG, client=mock_client(handler))
    await share.deploy()
    assert seen["method"] == "PUT"
    assert seen["path"] == f"/accounts/{CONFIG.account_id}/workers/scripts/kubedoor-ai-share"
    assert b'name="metadata"' in seen["body"]
    assert b'"kv_namespace"' in seen["body"] and b"AI_SHARES" in seen["body"]
    assert b'name="worker.js"' in seen["body"] and b"export default" in seen["body"]


@pytest.mark.asyncio
async def test_cloudflare_put_delete_and_error_mapping():
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path.removeprefix("/client/v4")))
        if request.method == "PUT":
            assert b"<!doctype html>" in request.content
            return httpx.Response(200, json={"success": True, "result": {}})
        if request.method == "DELETE":
            return httpx.Response(404, json={"success": False, "errors": [{"code": 7003, "message": "not found"}]})
        return httpx.Response(401, json={"success": False, "errors": [{"code": 10000, "message": "Authentication error"}]})

    share = CloudflareShare(CONFIG, client=mock_client(handler))
    await share.put_value(CODE, "<!doctype html><html></html>")
    await share.delete_value(CODE)   # 404 当作已经删除
    assert calls[0] == ("PUT", f"/accounts/{CONFIG.account_id}/storage/kv/namespaces/{CONFIG.kv_namespace_id}/values/{CODE}")
    assert calls[1] == ("DELETE", f"/accounts/{CONFIG.account_id}/storage/kv/namespaces/{CONFIG.kv_namespace_id}/values/{CODE}")
    with pytest.raises(AIError) as error:
        await share._request("GET", "/user/tokens/verify", error_status=422)
    assert error.value.status == 422 and "Authentication error" in str(error.value)


@pytest.mark.asyncio
async def test_cloudflare_check_public_url_reports_binding_problem():
    def handler(request):
        assert request.url.path == "/health"
        return httpx.Response(404, text="not found")

    share = CloudflareShare(CONFIG, client=mock_client(handler))
    message = await share.check_public_url()
    assert message and "HTTP 404" in message

    ok = CloudflareShare(CONFIG, client=mock_client(lambda request: httpx.Response(200, text="ok")))
    assert await ok.check_public_url() is None


class FakeStore:
    def __init__(self, rows=None, config=None, active=None):
        self.session_row = {"id": "s-1", "title": "排查超时", "public_share_code": None,
                            "public_shared_at": None, "public_share_url": None}
        self.rows = rows or []
        self.config = config
        self.active = active
        self.updates = []

    async def session(self, sid, identity):
        if identity.username != "alice":
            raise AIError("会话不存在", 404, "not_found")
        return dict(self.session_row)

    async def fetch(self, sql, *args):
        assert "m.replaced_at IS NULL" in sql
        return [dict(row) for row in self.rows]

    async def one(self, sql, *args):
        if "FROM kubedoor_ai_runs" in sql:
            return self.active
        if "kubedoor_ai_public_share_config" in sql:
            if not self.config:
                return None
            return {"encrypted_config": self.config, "updated_by": "admin", "updated_at": "2026-11-28T10:00:00+08:00"}
        if sql.startswith("UPDATE kubedoor_ai_sessions SET public_share_code=$2"):
            self.session_row.update(public_share_code=args[1], public_shared_at="2026-11-28T10:00:00+08:00", public_share_url=args[2])
            self.updates.append(("publish", args))
            return {"id": args[0], "public_shared_at": "2026-11-28T10:00:00+08:00"}
        if sql.startswith("UPDATE kubedoor_ai_sessions SET public_share_code=NULL"):
            self.session_row.update(public_share_code=None, public_shared_at=None, public_share_url=None)
            self.updates.append(("unpublish", args))
            return {"id": args[0]}
        raise AssertionError(sql)

    async def execute(self, sql, *args):
        if "INSERT INTO kubedoor_ai_public_share_config" in sql:
            self.config = args[0]
        self.updates.append(("execute", sql, args))


class FakePublisher:
    def __init__(self):
        self.puts, self.deletes, self.ensures = [], [], []
        self.deploys = self.verifies = self.closed = 0
        self.delete_error = None
        self.ensure_result = "e" * 32

    async def ensure_namespace(self):
        self.ensures.append(True)
        return self.ensure_result

    async def deploy(self):
        self.deploys += 1

    async def verify(self):
        self.verifies += 1
        return {"success": True, "account_id": CONFIG.account_id, "worker_name": CONFIG.worker_name,
                "kv_namespace_id": CONFIG.kv_namespace_id}

    async def check_public_url(self):
        return None

    async def put_value(self, code, page):
        self.puts.append((code, page))

    async def delete_value(self, code):
        if self.delete_error:
            raise self.delete_error
        self.deletes.append(code)

    async def close(self):
        self.closed += 1


def make_service(store, publisher):
    svc = Service.__new__(Service)
    svc.store = store
    svc.gateway = SimpleNamespace(cipher=CredentialCipher(KEY), master=AsyncMock(return_value={"success": True}))
    svc._share_publisher = lambda config: publisher
    return svc


def encrypted_config(config=CONFIG, cipher=None):
    cipher = cipher or CredentialCipher(KEY)
    return cipher.encrypt(CONFIG_AAD, config.encrypted())


@pytest.mark.asyncio
async def test_service_publishes_snapshot_and_returns_public_url():
    store = FakeStore(rows=[{"role": "assistant", "content": "正常", "tools": [{"name": "logs", "state": "completed", "result": "secret"}],
                             "created_at": "2026-11-28T10:00:00+08:00", "origin": "chat"}],
                      config=encrypted_config())
    publisher = FakePublisher()
    svc = make_service(store, publisher)
    result = await svc.publish_public_share(ALICE, "s-1")
    assert result["public_shared"] is True and len(result["public_share_code"]) == 22
    assert result["public_url"] == f"https://share.example.com/s/{result['public_share_code']}"
    assert result["message_count"] == 1
    assert publisher.puts[0][0] == result["public_share_code"] and publisher.puts[0][1].startswith("<!doctype html>")
    assert "secret" not in publisher.puts[0][1]
    assert store.session_row["public_share_code"] == result["public_share_code"]
    assert store.session_row["public_share_url"] == result["public_url"]
    audit = svc.gateway.master.await_args_list[-1]
    assert audit.args[1:3] == ("POST", "/api/ai/internal/audit")
    assert audit.kwargs["body"]["action"] == "ai.public_share.publish"
    assert publisher.closed == 1


@pytest.mark.asyncio
async def test_service_republish_deletes_previous_code():
    old = "b" * 21 + "_"
    store = FakeStore(config=encrypted_config())
    store.session_row["public_share_code"] = old
    publisher = FakePublisher()
    result = await make_service(store, publisher).publish_public_share(ALICE, "s-1")
    assert result["public_share_code"] != old
    assert publisher.deletes == [old]


@pytest.mark.asyncio
async def test_service_publish_requires_config_and_owner():
    with pytest.raises(AIError) as error:
        await make_service(FakeStore(), FakePublisher()).publish_public_share(ALICE, "s-1")
    assert (error.value.status, error.value.code) == (409, "public_share_unconfigured")
    store = FakeStore(config=encrypted_config())
    with pytest.raises(AIError) as error:
        await make_service(store, FakePublisher()).publish_public_share(Identity("bob", "admin"), "s-1")
    assert error.value.status == 404


@pytest.mark.asyncio
async def test_service_unpublish_deletes_snapshot_and_clears_state():
    store = FakeStore(config=encrypted_config())
    store.session_row["public_share_code"] = CODE
    publisher = FakePublisher()
    result = await make_service(store, publisher).unpublish_public_share(ALICE, "s-1")
    assert result == {"id": "s-1", "public_shared": False, "public_share_code": None,
                      "public_shared_at": None, "public_url": None}
    assert publisher.deletes == [CODE]
    assert store.session_row["public_share_code"] is None


@pytest.mark.asyncio
async def test_service_unpublish_keeps_state_when_cloudflare_delete_fails():
    store = FakeStore(config=encrypted_config())
    store.session_row["public_share_code"] = CODE
    publisher = FakePublisher()
    publisher.delete_error = AIError("Cloudflare：boom", 502, "cloudflare_error")
    with pytest.raises(AIError) as error:
        await make_service(store, publisher).unpublish_public_share(ALICE, "s-1")
    assert error.value.code == "public_share_unpublish_failed"
    assert store.session_row["public_share_code"] == CODE


@pytest.mark.asyncio
async def test_service_unpublish_without_config_still_clears_local_state():
    store = FakeStore()
    store.session_row["public_share_code"] = CODE
    publisher = FakePublisher()
    result = await make_service(store, publisher).unpublish_public_share(ALICE, "s-1")
    assert result["public_shared"] is False and publisher.deletes == []
    assert store.session_row["public_share_code"] is None


@pytest.mark.asyncio
async def test_service_discard_blocks_session_delete_when_cleanup_fails():
    store = FakeStore(config=encrypted_config())
    store.session_row["public_share_code"] = CODE
    publisher = FakePublisher()
    publisher.delete_error = AIError("Cloudflare：boom", 502, "cloudflare_error")
    with pytest.raises(AIError) as error:
        await make_service(store, publisher).discard_public_share(ALICE, "s-1")
    assert error.value.code == "public_share_cleanup_failed"
    assert store.session_row["public_share_code"] == CODE


@pytest.mark.asyncio
async def test_service_save_config_creates_namespace_deploys_and_encrypts_token():
    store = FakeStore()
    publisher = FakePublisher()
    svc = make_service(store, publisher)
    result = await svc.save_public_share_config(ADMIN, {
        "account_id": "a" * 32, "api_token": "new-token", "worker_name": "kubedoor-ai-share",
        "kv_namespace_id": "", "public_base_url": "https://share.example.com",
    })
    assert result["configured"] is True and result["kv_namespace_id"] == "e" * 32
    assert result["api_token_set"] is True and "api_token" not in result
    assert publisher.ensures == [True] and publisher.deploys == 1 and publisher.closed == 1
    stored = svc.gateway.cipher.decrypt(CONFIG_AAD, bytes(store.config))
    assert stored["api_token"] == "new-token" and stored["kv_namespace_id"] == "e" * 32
    assert svc.gateway.master.await_args_list[-1].kwargs["body"]["action"] == "ai.public_share_config.update"


@pytest.mark.asyncio
async def test_service_save_config_requires_admin_and_test_uses_verify():
    publisher = FakePublisher()
    svc = make_service(FakeStore(config=encrypted_config()), publisher)
    with pytest.raises(AIError) as error:
        await svc.save_public_share_config(ALICE, {"account_id": "a" * 32, "api_token": "x", "worker_name": "w",
                                                   "kv_namespace_id": "", "public_base_url": "https://x.example"})
    assert error.value.status == 403
    result = await svc.test_public_share_config(ADMIN, {
        "account_id": "a" * 32, "worker_name": "kubedoor-ai-share",
        "kv_namespace_id": "b" * 32, "public_base_url": "https://share.example.com",
    })
    assert result["success"] is True and publisher.verifies == 1 and publisher.closed == 1


def test_public_share_http_routes_and_admin_guard():
    service = SimpleNamespace(
        publish_public_share=AsyncMock(return_value={"id": "s-1", "public_shared": True}),
        unpublish_public_share=AsyncMock(return_value={"id": "s-1", "public_shared": False}),
        public_share_config=AsyncMock(return_value={"configured": False}),
        save_public_share_config=AsyncMock(return_value={"configured": True}),
        test_public_share_config=AsyncMock(return_value={"success": True}),
    )
    headers = {"X-Kubedoor-Token": "test-token", "X-User-Name": "alice", "X-User-Permission": "rw"}
    admin = {**headers, "X-User-Permission": "admin"}
    with TestClient(create_app(service, token="test-token")) as client:
        assert client.post("/api/ai/sessions/s-1/public-share", headers=headers).status_code == 200
        service.publish_public_share.assert_awaited_once()
        assert client.delete("/api/ai/sessions/s-1/public-share", headers=headers).status_code == 200
        service.unpublish_public_share.assert_awaited_once()
        assert client.get("/api/ai/public-share-config", headers=headers).status_code == 403
        assert client.get("/api/ai/public-share-config", headers=admin).status_code == 200
        assert client.put("/api/ai/public-share-config", headers=admin, json={"account_id": "a" * 32}).status_code == 200
        service.save_public_share_config.assert_awaited_once()
        assert client.post("/api/ai/public-share-config/test", headers=admin, json={}).status_code == 200
        service.test_public_share_config.assert_awaited_once()
