from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from kubedoor_ai.domain import AIError, Identity
from kubedoor_ai.http import create_app
from kubedoor_ai.wecom import WeComIntegration, _limit_stream


class Store:
    def __init__(self, approval=None, mapping=None, unbound=None, unbound_env=None):
        self.seen = set()
        self.approval = approval
        self.mapping = mapping
        self.unbound = unbound
        self.unbound_env = unbound_env
        self.events = []
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
            return {"unbound_username": self.unbound, "unbound_default_env": self.unbound_env}
        if "INSERT INTO kubedoor_ai_sessions" in sql:
            return {"id": "session-1"}
        if "FROM kubedoor_ai_runs" in sql:
            return {"env": "prod"}
        return None

    async def execute(self, sql, *args):
        self.executes.append((sql, args))

    async def fetch(self, sql, *args):
        return self.events if "FROM kubedoor_ai_events" in sql else []


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


CLUSTERS = [{"env": name, "permission": "read", "online": name != "tx-flink-prod"}
            for name in ("b2c-prod-kunlun", "cassmall-beta-kunlun", "cassmall-kunlun", "tx-flink-prod")]


def unbound_chat(current_env=None, default_env=None):
    """未绑定成员按默认权限账号 guest 聊天;_run_message 换成桩,只看这条消息落到哪个集群"""
    store = Store(mapping={"wecom_userid": "wx-bob", "username": None, "current_env": current_env},
                  unbound="guest", unbound_env=default_env)
    master = AsyncMock(return_value={"username": "guest", "permission": "read", "clusters": {}})
    integration, service = make_integration(store=store, master=master)
    service.bootstrap.return_value = {"clusters": CLUSTERS}
    integration._run_message = AsyncMock()
    return integration, store, connect(integration)


def saved_envs(store):
    return [args[1] for sql, args in store.executes if "SET current_env=" in sql]


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
    assert "当前集群：未选择" in last_reply(client) and "1. test · 只读\n" in last_reply(client)


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
        await integration.save_unbound(Identity("admin", "admin"), {"username": "boss"})
    master.return_value = {"username": "nobody", "permission": "none", "clusters": {}}
    with pytest.raises(AIError, match="没有任何 K8S"):
        await integration.save_unbound(Identity("admin", "admin"), {"username": "nobody"})
    assert service.store.executes == []


@pytest.mark.asyncio
async def test_unknown_account_error_explains_master_403():
    master = AsyncMock(side_effect=AIError("KubeDoor 接口返回 HTTP 403", 403, "upstream_error"))
    integration, service = make_integration(master=master)
    with pytest.raises(AIError, match="ghost 不存在、已停用或不是 Web 账号"):
        await integration.save_unbound(Identity("admin", "admin"), {"username": "ghost"})
    assert service.store.executes == []
    master.side_effect = AIError("KubeDoor 接口暂不可用", 502, "upstream_error")
    with pytest.raises(AIError, match="暂不可用"):
        await integration.save_unbound(Identity("admin", "admin"), {"username": "ghost"})


@pytest.mark.asyncio
async def test_default_permission_and_cluster_can_be_saved_and_cleared():
    master = AsyncMock(return_value={"username": "guest", "permission": "read", "clusters": {}})
    integration, service = make_integration(master=master)
    service.bootstrap.return_value = {"clusters": [{"env": "test", "permission": "read"}]}
    admin = Identity("admin", "admin")
    saved = await integration.save_unbound(admin, {"username": " guest ", "default_env": "test"})
    assert saved == {"unbound_username": "guest", "unbound_default_env": "test"}
    sql, args = service.store.executes[-1]
    assert "unbound_username=EXCLUDED.unbound_username" in sql and "unbound_default_env=EXCLUDED" in sql
    assert "encrypted_config=EXCLUDED" not in sql
    assert args[1:] == ("guest", "test", "admin")
    # 默认集群选「无」
    assert (await integration.save_unbound(admin, {"username": "guest", "default_env": None}))["unbound_default_env"] is None
    with pytest.raises(AIError, match="默认集群不存在"):
        await integration.save_unbound(admin, {"username": "guest", "default_env": "hidden"})
    # 关掉默认权限时默认集群一起清掉
    master.reset_mock()
    cleared = await integration.save_unbound(admin, {"username": None, "default_env": "test"})
    assert cleared == {"unbound_username": None, "unbound_default_env": None}
    assert service.store.executes[-1][1][1:3] == (None, None)
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


@pytest.mark.asyncio
async def test_cluster_menu_numbers_clusters_and_explains_switching():
    integration, store, client = unbound_chat(default_env="cassmall-kunlun")
    await integration._handle_text(text_frame("wx-bob", "集群"))
    menu = last_reply(client)
    assert "当前集群：cassmall-kunlun" in menu
    assert "1. b2c-prod-kunlun · 只读\n" in menu and "3. cassmall-kunlun · 只读（当前）" in menu
    assert "4. tx-flink-prod · 只读 · agent 离线" in menu
    assert "切换集群 序号或名称" in menu
    integration._run_message.assert_not_awaited()
    assert saved_envs(store) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("command, expected", [
    ("切换集群 3", "cassmall-kunlun"),
    ("切换集群cassmall-kunlun", "cassmall-kunlun"),
    ("切换到：CASSMALL-KUNLUN", "cassmall-kunlun"),
    ("/切换集群　flink", "tx-flink-prod"),
])
async def test_switch_cluster_by_index_name_or_unique_part(command, expected):
    integration, store, client = unbound_chat()
    await integration._handle_text(text_frame("wx-bob", command))
    assert saved_envs(store) == [expected]
    assert f"已切换到集群：{expected}" in last_reply(client)
    integration._run_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_switch_cluster_explains_ambiguous_and_unknown_names():
    integration, store, client = unbound_chat()
    await integration._handle_text(text_frame("wx-bob", "切换集群 kunlun", "message-1"))
    reply = last_reply(client)
    assert "匹配到多个集群：b2c-prod-kunlun、cassmall-beta-kunlun、cassmall-kunlun" in reply
    assert "可访问的集群" in reply
    await integration._handle_text(text_frame("wx-bob", "切换集群 9", "message-2"))
    assert "没有找到集群「9」" in last_reply(client)
    assert saved_envs(store) == []
    integration._run_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_switch_wording_that_names_no_cluster_is_an_ordinary_question():
    integration, store, _ = unbound_chat(current_env="cassmall-kunlun")
    await integration._handle_text(text_frame("wx-bob", "切换镜像到v2"))
    args = integration._run_message.await_args.args
    assert args[4] == "cassmall-kunlun" and args[5] == "切换镜像到v2"
    assert saved_envs(store) == []


@pytest.mark.asyncio
async def test_question_naming_exactly_one_cluster_switches_to_it():
    integration, store, _ = unbound_chat()
    await integration._handle_text(text_frame("wx-bob", "cassmall-kunlun集群的 order 服务为什么重启"))
    call = integration._run_message.await_args
    assert call.args[4] == "cassmall-kunlun"
    assert call.kwargs["status"].startswith("已切换到集群 cassmall-kunlun")
    assert saved_envs(store) == ["cassmall-kunlun"]


@pytest.mark.asyncio
async def test_question_naming_several_clusters_stays_on_current_cluster():
    integration, store, _ = unbound_chat(current_env="tx-flink-prod")
    await integration._handle_text(text_frame("wx-bob", "对比 cassmall-kunlun 和 cassmall-beta-kunlun 的副本数"))
    call = integration._run_message.await_args
    assert call.args[4] == "tx-flink-prod" and call.kwargs["status"] is None
    assert saved_envs(store) == []


@pytest.mark.asyncio
async def test_default_cluster_is_used_until_member_switches():
    integration, store, _ = unbound_chat(default_env="cassmall-beta-kunlun")
    await integration._handle_text(text_frame("wx-bob", "order 服务有几个 Pod"))
    assert integration._run_message.await_args.args[4] == "cassmall-beta-kunlun"
    # 默认集群不写进成员的当前集群,管理员以后改默认集群对没切换过的成员立即生效
    assert saved_envs(store) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mapping, unbound_env", [
    ({"wecom_userid": "wx-bob", "username": None}, None),  # 未绑定,默认集群选了「无」
    ({"wecom_userid": "wx-bob", "username": None, "current_env": "gone"}, "gone-too"),  # 集群已无权访问
    ({"wecom_userid": "wx-bob", "username": "alice", "default_env": None}, "cassmall-kunlun"),  # 绑定成员默认集群「无」
])
async def test_without_any_cluster_the_member_must_pick_one_first(mapping, unbound_env):
    store = Store(mapping=mapping, unbound="guest", unbound_env=unbound_env)
    username = mapping["username"] or "guest"
    master = AsyncMock(return_value={"username": username, "permission": "read", "clusters": {}})
    integration, service = make_integration(store=store, master=master)
    service.bootstrap.return_value = {"clusters": CLUSTERS}
    integration._run_message = AsyncMock()
    client = connect(integration)
    await integration._handle_text(text_frame("wx-bob", "order 服务有几个 Pod"))
    reply = last_reply(client)
    assert reply.startswith("还没有选择集群") and "当前集群：未选择" in reply and "1. b2c-prod-kunlun" in reply
    integration._run_message.assert_not_awaited()
    assert saved_envs(store) == []


@pytest.mark.asyncio
async def test_final_answer_names_its_cluster():
    store = Store()
    store.events = [{"seq": 1, "type": "message", "data": {"content": "共有 3 个 Pod 在运行。"}},
                    {"seq": 2, "type": "done", "data": {"status": "completed"}}]
    integration, _ = make_integration(store=store)
    client = connect(integration)
    await integration._watch_run({"body": {}}, "wx-bob", Identity("guest", "read"), "run-1", 0, env="cassmall-kunlun")
    first, final = client.reply_stream.await_args_list[0].args, client.reply_stream.await_args.args
    assert first[2] == "正在分析请求..." and first[3] is False
    assert final[2].startswith("共有 3 个 Pod 在运行。") and "集群：cassmall-kunlun" in final[2]
    assert final[3] is True


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
