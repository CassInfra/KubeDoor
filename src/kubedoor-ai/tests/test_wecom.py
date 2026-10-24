from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from kubedoor_ai.domain import AIError, Identity
from kubedoor_ai.http import create_app
from kubedoor_ai.wecom import WeComIntegration, _limit_stream


class Store:
    def __init__(self, approval=None, mapping=None, unbound=None):
        self.seen = set()
        self.approval = approval
        self.mapping = mapping
        self.unbound = unbound
        self.executes = []

    async def one(self, sql, *args):
        if "INSERT INTO kubedoor_ai_wecom_inbox" in sql:
            msgid = args[0]
            if msgid in self.seen:
                return None
            self.seen.add(msgid)
            return {"msgid": msgid}
        if "FROM kubedoor_ai_wecom_approvals" in sql:
            return self.approval if self.approval and args[0] == self.approval["task_id"] else None
        if "FROM kubedoor_ai_wecom_users" in sql:
            return self.mapping
        if "FROM kubedoor_ai_wecom_config" in sql:
            return {"unbound_username": self.unbound}
        if "INSERT INTO kubedoor_ai_sessions" in sql:
            return {"id": "session-1"}
        if "FROM kubedoor_ai_runs" in sql:
            return {"env": "prod"}
        return None

    async def execute(self, sql, *args):
        self.executes.append((sql, args))


def make_integration(store=None, master=None):
    gateway = SimpleNamespace(master=master or AsyncMock(),
                              cipher=SimpleNamespace(encrypt=Mock(return_value=b"ciphertext")))
    service = SimpleNamespace(store=store or Store(), gateway=gateway,
                              bootstrap=AsyncMock(return_value={"clusters": []}),
                              decision=AsyncMock(return_value={"status": "running"}))
    return WeComIntegration(service), service


def connect(integration):
    integration.client = SimpleNamespace(
        is_connected=True,
        reply_stream=AsyncMock(),
        update_template_card=AsyncMock(),
    )
    return integration.client


def text_frame(userid, content, msgid="message-1"):
    return {"body": {"msgid": msgid, "from": {"userid": userid}, "chattype": "single",
                     "text": {"content": content}}}


def last_reply(client):
    return client.reply_stream.await_args.args[2]


@pytest.mark.asyncio
async def test_identity_uses_live_master_roles_and_cluster_grants():
    master = AsyncMock(return_value={
        "username": "alice", "permission": "none", "clusters": {"prod": "read"}
    })
    integration, _ = make_integration(master=master)
    identity = await integration._identity("alice")
    assert identity.permission == "none"
    assert identity.can_read("prod")
    assert not identity.can_read("other")
    master.assert_awaited_once()


@pytest.mark.asyncio
async def test_message_id_deduplication_is_stable():
    integration, _ = make_integration()
    body = {"msgid": "message-1"}
    assert await integration._claim_message(body, "wecom-user", "text")
    assert not await integration._claim_message(body, "wecom-user", "text")


@pytest.mark.asyncio
async def test_scoped_user_can_be_mapped_but_default_cluster_must_be_visible():
    master = AsyncMock(return_value={
        "username": "alice", "permission": "none", "clusters": {"prod": "rw"}
    })
    integration, service = make_integration(master=master)
    service.bootstrap.return_value = {"clusters": [{"env": "prod", "permission": "rw"}]}
    assert await integration.save_user(Identity("admin", "admin"), {
        "wecom_userid": "wx-alice", "username": "alice", "default_env": "prod"
    }) == {"success": True}
    with pytest.raises(AIError, match="默认集群"):
        await integration.save_user(Identity("admin", "admin"), {
            "wecom_userid": "wx-alice", "username": "alice", "default_env": "hidden"
        })


@pytest.mark.asyncio
async def test_card_approval_cannot_be_clicked_by_another_wecom_member():
    store = Store(approval={"task_id": "task-1", "wecom_userid": "owner", "username": "alice"})
    integration, service = make_integration(store=store)
    client = connect(integration)
    integration._spawn = Mock()
    await integration._handle_card_event({
        "body": {
            "msgid": "event-1",
            "from": {"userid": "attacker"},
            "event": {"task_id": "task-1", "event_key": "approve"},
        }
    })
    service.decision.assert_not_awaited()
    client.reply_stream.assert_awaited_once()


@pytest.mark.asyncio
async def test_unbound_member_without_default_permission_must_be_bound():
    store = Store(mapping={"wecom_userid": "wx-bob", "username": None})
    integration, service = make_integration(store=store)
    client = connect(integration)
    await integration._handle_text(text_frame("wx-bob", "当前集群"))
    assert "尚未绑定" in last_reply(client)
    integration.service.gateway.master.assert_not_awaited()
    service.bootstrap.assert_not_awaited()


@pytest.mark.asyncio
async def test_unbound_member_uses_live_permissions_of_default_account():
    master = AsyncMock(return_value={"username": "guest", "permission": "read", "clusters": {"prod": "none"}})
    store = Store(mapping={"wecom_userid": "wx-bob", "username": None}, unbound="guest")
    integration, service = make_integration(store=store, master=master)
    service.bootstrap.return_value = {"clusters": [{"env": "test", "permission": "read"}]}
    client = connect(integration)
    await integration._handle_text(text_frame("wx-bob", "当前集群"))
    assert master.await_args.args[0].username == "guest"
    identity = service.bootstrap.await_args.args[0]
    assert identity.username == "guest" and identity.can_read("test") and not identity.can_read("prod")
    assert "可访问：test" in last_reply(client)


@pytest.mark.asyncio
async def test_bound_member_keeps_own_account_when_default_is_set():
    master = AsyncMock(return_value={"username": "alice", "permission": "rw", "clusters": {}})
    store = Store(mapping={"wecom_userid": "wx-alice", "username": "alice"}, unbound="guest")
    integration, service = make_integration(store=store, master=master)
    service.bootstrap.return_value = {"clusters": [{"env": "prod", "permission": "rw"}]}
    connect(integration)
    await integration._handle_text(text_frame("wx-alice", "当前集群"))
    assert master.await_args.args[0].username == "alice"


@pytest.mark.asyncio
async def test_default_account_promoted_to_admin_is_refused():
    master = AsyncMock(return_value={"username": "guest", "permission": "admin", "clusters": {}})
    store = Store(mapping={"wecom_userid": "wx-bob", "username": None}, unbound="guest")
    integration, service = make_integration(store=store, master=master)
    client = connect(integration)
    await integration._handle_text(text_frame("wx-bob", "当前集群"))
    assert "默认权限当前不可用" in last_reply(client)
    service.bootstrap.assert_not_awaited()


@pytest.mark.asyncio
async def test_default_permission_rejects_admin_and_accounts_without_clusters():
    master = AsyncMock(return_value={"username": "boss", "permission": "admin", "clusters": {}})
    integration, service = make_integration(master=master)
    with pytest.raises(AIError, match="管理员"):
        await integration.save_unbound_username(Identity("admin", "admin"), {"username": "boss"})
    master.return_value = {"username": "nobody", "permission": "none", "clusters": {}}
    with pytest.raises(AIError, match="没有任何 K8S"):
        await integration.save_unbound_username(Identity("admin", "admin"), {"username": "nobody"})
    assert service.store.executes == []


@pytest.mark.asyncio
async def test_default_permission_can_be_saved_and_cleared():
    master = AsyncMock(return_value={"username": "guest", "permission": "read", "clusters": {}})
    integration, service = make_integration(master=master)
    service.bootstrap.return_value = {"clusters": [{"env": "test", "permission": "read"}]}
    admin = Identity("admin", "admin")
    assert await integration.save_unbound_username(admin, {"username": " guest "}) == {"unbound_username": "guest"}
    sql, args = service.store.executes[-1]
    assert "unbound_username=EXCLUDED.unbound_username" in sql
    assert "encrypted_config=EXCLUDED" not in sql
    assert args[1:] == ("guest", "admin")
    master.reset_mock()
    assert await integration.save_unbound_username(admin, {"username": None}) == {"unbound_username": None}
    assert service.store.executes[-1][1][1] is None
    master.assert_not_awaited()


@pytest.mark.asyncio
async def test_unbound_member_can_approve_with_default_account_until_it_changes():
    approval = {"task_id": "task-1", "wecom_userid": "wx-bob", "username": "guest", "run_id": "run-1",
                "action_id": "action-1", "resume_after_seq": 3}
    master = AsyncMock(return_value={"username": "guest", "permission": "rw", "clusters": {}})
    store = Store(approval=approval, mapping={"wecom_userid": "wx-bob", "username": None}, unbound="other")
    integration, service = make_integration(store=store, master=master)
    integration.config = {"base_url": "https://llm.example/v1", "api_key": "key", "model": "model-1"}
    client = connect(integration)
    integration._spawn = Mock()
    integration._watch_run = Mock()
    event = {"body": {"msgid": "event-1", "from": {"userid": "wx-bob"},
                      "event": {"task_id": "task-1", "event_key": "approve"}}}
    await integration._handle_card_event(event)
    service.decision.assert_not_awaited()
    assert "账号绑定已变更" in last_reply(client)

    store.unbound = "guest"
    await integration._handle_card_event({**event, "body": {**event["body"], "msgid": "event-2"}})
    service.decision.assert_awaited_once()
    assert service.decision.await_args.args[0].username == "guest"


@pytest.mark.asyncio
async def test_new_session_never_binds_member_to_default_account():
    store = Store()
    integration, _ = make_integration(store=store)
    session_id = await integration._get_session("wx-bob", Identity("guest", "read"),
                                                {"wecom_userid": "wx-bob", "username": None})
    assert session_id == "session-1"
    sql, args = store.executes[-1]
    assert "SET session_id=" in sql and "username" not in sql
    assert args == ("wx-bob", "session-1")


def test_stream_content_limit_preserves_utf8_boundaries():
    result = _limit_stream("运维" * 12000)
    assert result.encode("utf-8").decode("utf-8") == result
    assert "内容过长" in result
    assert len(result.encode("utf-8")) <= 18000


@pytest.mark.asyncio
async def test_admin_config_never_returns_stored_bot_secret_or_model_key():
    class Cipher:
        def decrypt(self, *_):
            return {"bot_id": "bot-1", "bot_secret": "bot-secret", "base_url": "https://llm.example/v1",
                    "api_key": "model-secret", "model": "model-1"}

    store = SimpleNamespace(one=AsyncMock(return_value={
        "enabled": True, "encrypted_config": b"ciphertext", "updated_by": "admin", "updated_at": "now"
    }))
    service = SimpleNamespace(store=store, gateway=SimpleNamespace(cipher=Cipher()))
    integration = WeComIntegration(service)
    result = await integration.admin_config()
    assert result["bot_id"] == "bot-1"
    assert result["has_bot_secret"] and result["has_api_key"]
    assert "bot_secret" not in result and "api_key" not in result
    assert "bot-secret" not in repr(result) and "model-secret" not in repr(result)


def test_non_admin_cannot_read_or_change_wecom_settings():
    app = create_app(SimpleNamespace(), token="test-token")
    headers = {"X-Kubedoor-Token": "test-token", "X-User-Name": "reader", "X-User-Permission": "read"}
    from starlette.testclient import TestClient

    with TestClient(app) as client:
        assert client.get("/api/ai/wecom", headers=headers).status_code == 403
        assert client.put("/api/ai/wecom", headers=headers, json={"enabled": True}).status_code == 403
        assert client.put("/api/ai/wecom/unbound", headers=headers, json={"username": "reader"}).status_code == 403
