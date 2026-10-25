import asyncio
import json
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from kubedoor_ai.domain import BEIJING_TZ, AIError, Identity
from kubedoor_ai.http import create_app
from kubedoor_ai.wecom import WeComIntegration, _approval_summary, _approval_text, _limit_stream, _retention_days


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


class SessionStore(Store):
    """会话边界用到的表放在内存里;transaction() 用每个成员一把锁模拟 pg_advisory_xact_lock"""
    def __init__(self, members=None, idle_minutes=120, **kwargs):
        super().__init__(**kwargs)
        self.members = members or {}
        self.sessions, self.runs = {}, {}
        self.idle_minutes = idle_minutes
        self.locks = defaultdict(asyncio.Lock)
        self.member_updates = []
        self.cancelled_while_locked = []
        self.now = datetime.now(timezone.utc)

    @asynccontextmanager
    async def transaction(self):
        held = []
        try:
            yield SessionTx(self, held)
        finally:
            for lock in held:
                lock.release()

    async def one(self, sql, *args):
        if "FROM kubedoor_ai_sessions s" in sql:   # 欢迎卡片不进事务,直接读
            return await SessionTx(self, []).one(sql, *args)
        return await super().one(sql, *args)

    async def message(self, run, role, content, tools=None):
        return {"role": role, "content": content}

    def cancel(self, run):
        # 记下停止任务时是否还持有成员锁:运行中的任务要在锁外停止
        self.cancelled_while_locked.append(any(lock.locked() for lock in self.locks.values()))
        self.runs[run["id"]]["status"] = "cancelled"


class SessionTx:
    def __init__(self, store, held):
        self.store, self.held = store, held

    async def execute(self, sql, *args):
        await asyncio.sleep(0)   # 让并发的消息真正交错执行
        store = self.store
        if "pg_advisory_xact_lock" in sql:
            lock = store.locks[args[0]]
            await lock.acquire()
            self.held.append(lock)
        elif sql.startswith("INSERT INTO kubedoor_ai_sessions"):
            store.sessions[args[0]] = {"username": args[1], "channel_key": args[2], "updated_at": store.now, "rounds": 0}
        elif "UPDATE kubedoor_ai_wecom_users SET session_id" in sql:
            store.member_updates.append((sql, args))
            store.members.setdefault(args[0], {}).update(session_id=args[1], previous_session_id=args[2])
        elif "UPDATE kubedoor_ai_sessions SET updated_at=now()" in sql:
            store.sessions[args[0]]["updated_at"] = store.now
        else:
            raise AssertionError(sql)

    async def one(self, sql, *args):
        await asyncio.sleep(0)
        store = self.store
        if "FROM kubedoor_ai_wecom_users" in sql:
            return dict(store.members[args[0]]) if args[0] in store.members else None
        if "FROM kubedoor_ai_wecom_config" in sql:
            return {"session_idle_minutes": store.idle_minutes}
        if "FROM kubedoor_ai_sessions s" in sql:
            session_id, username, prefix, idle = args
            row = store.sessions.get(session_id)
            if not row or row.get("purged") or row["username"] != username or not row["channel_key"].startswith(prefix):
                return None
            return {"id": session_id, "updated_at": row["updated_at"].isoformat(), "rounds": row["rounds"],
                    "idle": store.now - row["updated_at"] > timedelta(minutes=idle)}
        if "FROM kubedoor_ai_runs r" in sql:
            active = [run for run in store.runs.values() if run["session_id"] == args[0] and run["status"] in ("running", "waiting_approval")]
            return {"id": active[-1]["id"], "status": active[-1]["status"],
                    "approvable": any(at > store.now for at in active[-1].get("expires", []))} if active else None
        raise AssertionError(sql)


IDENTITY = Identity("guest", "read")
PROVIDER_CONFIG = {"base_url": "https://llm.example/v1", "api_key": "key", "model": "model-1"}


def own(store, session_id, *, hours_ago=0, rounds=0, username="guest"):
    store.sessions[session_id] = {"username": username, "channel_key": f"wx-bob:{username}",
                                  "updated_at": store.now - timedelta(hours=hours_ago), "rounds": rounds}


def member_chat(members, **kwargs):
    """未绑定成员 wx-bob 按默认权限账号 guest 聊天,会话表在内存里"""
    store = SessionStore(members=members, mapping={"wecom_userid": "wx-bob", "username": None, "current_env": "prod"},
                         unbound="guest", **kwargs)
    master = AsyncMock(return_value={"username": "guest", "permission": "read", "clusters": {}})
    integration, service = make_integration(store=store, master=master)
    service.bootstrap.return_value = {"clusters": [{"env": "prod", "permission": "read"}]}
    integration.config = PROVIDER_CONFIG
    return integration, service, store, connect(integration)


def make_integration(store=None, master=None):
    store = store or Store()
    gateway = SimpleNamespace(master=master or AsyncMock(),
                              cipher=SimpleNamespace(encrypt=Mock(return_value=b"ciphertext")))
    runtime = SimpleNamespace(start=Mock(),
                              cancel=AsyncMock(side_effect=lambda run, identity: getattr(store, "cancel", lambda _: None)(run)))
    service = SimpleNamespace(store=store, gateway=gateway, runtime=runtime, new_run=AsyncMock(),
                              bootstrap=AsyncMock(return_value={"clusters": []}),
                              decision=AsyncMock(return_value={"status": "running"}))
    return WeComIntegration(service), service


def connect(integration):
    integration.client = SimpleNamespace(
        is_connected=True,
        reply_stream=AsyncMock(),
        reply_welcome=AsyncMock(),
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
    store = SessionStore(members={"wx-bob": {}})
    integration, _ = make_integration(store=store)
    session_id, notices, active = await integration._current_session("wx-bob", Identity("guest", "read"))
    assert (notices, active) == ([], None)
    assert store.sessions[session_id]["channel_key"] == f"wx-bob:guest:{session_id}"
    sql, args = store.member_updates[-1]
    assert "SET session_id=" in sql and "username" not in sql
    assert args == ("wx-bob", session_id, None)


@pytest.mark.asyncio
async def test_cluster_menu_numbers_clusters_and_explains_switching():
    integration, store, client = unbound_chat(default_env="cassmall-kunlun")
    await integration._handle_text(text_frame("wx-bob", "集群"))
    menu = last_reply(client)
    assert "当前集群：cassmall-kunlun" in menu
    assert "1. b2c-prod-kunlun · 只读\n" in menu and "3. cassmall-kunlun · 只读（当前）" in menu
    assert "4. tx-flink-prod · 只读 · agent 离线" in menu
    assert "切换集群 序号或名称" in menu
    assert menu.endswith("发送「新对话」重新开始，「继续上次」回到上一个对话。")
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


@pytest.mark.asyncio
@pytest.mark.parametrize("last_input, ending", [
    (262_000, "—— 集群：prod（发送「集群」查看或切换）"),           # 25% 以下不提示
    (262_144, "—— 集群：prod · 上下文 25%（发送「新对话」可重新开始）"),
    (330_000, "—— 集群：prod · 上下文 31%（发送「新对话」可重新开始）"),
])
async def test_large_context_share_suggests_a_new_conversation(last_input, ending):
    store = Store()
    usage = {"model_calls": 3, "last_input_tokens": last_input, "max_input_tokens": 1_048_576}
    store.events = [{"seq": 1, "type": "message", "data": {"content": "已完成。"}},
                    {"seq": 2, "type": "done", "data": {"status": "completed", "usage": usage}}]
    integration, _ = make_integration(store=store)
    client = connect(integration)
    await integration._watch_run({"body": {}}, "wx-bob", IDENTITY, "run-1", 0, env="prod")
    assert last_reply(client) == "已完成。\n\n" + ending


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


class ConfigStore:
    """kubedoor_ai_wecom_config 单行:加密配置 + 空闲超时列"""
    def __init__(self):
        self.row = None

    async def one(self, sql, *args):
        if sql.startswith("INSERT INTO kubedoor_ai_wecom_config"):
            enabled, encrypted, username, idle, default = args
            previous = self.row["session_idle_minutes"] if self.row else default
            self.row = {"enabled": enabled, "encrypted_config": encrypted, "updated_by": username, "updated_at": "now",
                        "session_idle_minutes": previous if idle is None else idle}
            return self.row
        assert "FROM kubedoor_ai_wecom_config" in sql
        return self.row


@pytest.mark.asyncio
async def test_wecom_model_window_and_idle_timeout_are_saved_and_returned():
    cipher = SimpleNamespace(encrypt=lambda env, value: json.dumps(value).encode(), decrypt=lambda env, data: json.loads(data))
    integration = WeComIntegration(SimpleNamespace(store=ConfigStore(), gateway=SimpleNamespace(cipher=cipher)))
    admin = Identity("admin", "admin")
    assert (await integration.admin_config())["session_idle_minutes"] == 120
    base = {"enabled": False, "bot_id": "bot-1", "base_url": "https://api.deepseek.com", "model": "deepseek-flash"}
    saved = await integration.save_config(admin, {**base, "max_input_tokens": 262_144, "max_output_tokens": 65_536,
                                                  "compression_ratio": 0.6, "session_idle_minutes": 90,
                                                  "reasoning_effort": "low"})
    window = ("max_input_tokens", "max_output_tokens", "compression_ratio", "session_idle_minutes", "reasoning_effort")
    assert [saved[key] for key in window] == [262_144, 65_536, 0.6, 90, "low"]
    # 不传的字段沿用已保存的值,企微提问用的 Provider 带上这些字段
    saved = await integration.save_config(admin, base)
    assert [saved[key] for key in window] == [262_144, 65_536, 0.6, 90, "low"]
    assert integration._provider_settings()["max_input_tokens"] == 262_144
    assert integration._provider_settings()["reasoning_effort"] == "low"
    # null = 按模型推断 / 用模型默认;0 = 关闭空闲自动开新对话
    saved = await integration.save_config(admin, {**base, "max_input_tokens": None, "max_output_tokens": None,
                                                  "compression_ratio": None, "session_idle_minutes": 0,
                                                  "reasoning_effort": None})
    assert [saved[key] for key in window] == [None, None, None, 0, None]
    for invalid in ({"max_input_tokens": 1024}, {"max_output_tokens": 512}, {"compression_ratio": 0.9},
                    {"session_idle_minutes": 10081}, {"session_idle_minutes": -1},
                    {"session_idle_minutes": True}, {"session_idle_minutes": "30"}, {"reasoning_effort": "medium"}):
        with pytest.raises(AIError):
            await integration.save_config(admin, {**base, **invalid})


def test_non_admin_cannot_read_or_change_wecom_settings():
    app = create_app(SimpleNamespace(), token="test-token")
    headers = {"X-Kubedoor-Token": "test-token", "X-User-Name": "reader", "X-User-Permission": "read"}
    from starlette.testclient import TestClient

    with TestClient(app) as client:
        assert client.get("/api/ai/wecom", headers=headers).status_code == 403
        assert client.put("/api/ai/wecom", headers=headers, json={"enabled": True}).status_code == 403
        assert client.put("/api/ai/wecom/unbound", headers=headers, json={"username": "reader"}).status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("idle_minutes, elapsed", [(120, "2 小时"), (90, "90 分钟")])
async def test_idle_conversation_moves_the_next_message_to_a_new_one(idle_minutes, elapsed):
    store = SessionStore(members={"wx-bob": {"session_id": "old"}}, idle_minutes=idle_minutes)
    own(store, "old", hours_ago=3, rounds=12)
    integration, _ = make_integration(store=store)
    session_id, notices, active = await integration._current_session("wx-bob", IDENTITY)
    assert session_id != "old" and active is None
    assert notices == [f"距上次对话已超过 {elapsed}，已开始新对话（发送「继续上次」可回到之前的对话）"]
    assert store.members["wx-bob"] == {"session_id": session_id, "previous_session_id": "old"}
    assert store.sessions["old"]["rounds"] == 12   # 旧对话保留
    assert await integration._current_session("wx-bob", IDENTITY) == (session_id, [], None)


@pytest.mark.asyncio
async def test_recent_conversation_or_disabled_timeout_keeps_the_conversation():
    store = SessionStore(members={"wx-bob": {"session_id": "old"}})
    own(store, "old", hours_ago=1, rounds=3)
    integration, _ = make_integration(store=store)
    assert await integration._current_session("wx-bob", IDENTITY) == ("old", [], None)
    store.idle_minutes = 0
    store.sessions["old"]["updated_at"] -= timedelta(days=30)
    assert await integration._current_session("wx-bob", IDENTITY) == ("old", [], None)


@pytest.mark.asyncio
async def test_idle_but_empty_conversation_is_kept_so_the_previous_one_stays_reachable():
    # 刚发过「新对话」然后离开:新对话还是空的，再来消息时继续用它，不要把真正的上一个对话挤掉
    store = SessionStore(members={"wx-bob": {"session_id": "empty", "previous_session_id": "old"}})
    own(store, "empty", hours_ago=5)
    integration, _ = make_integration(store=store)
    assert await integration._current_session("wx-bob", IDENTITY) == ("empty", [], None)
    assert store.members["wx-bob"] == {"session_id": "empty", "previous_session_id": "old"}


@pytest.mark.asyncio
@pytest.mark.parametrize("status, expires_in", [("running", []), ("waiting_approval", [timedelta(minutes=10), timedelta(minutes=-1)])])
async def test_running_task_or_pending_approval_keeps_an_idle_conversation(status, expires_in):
    store = SessionStore(members={"wx-bob": {"session_id": "old"}})
    own(store, "old", hours_ago=3)
    store.runs["run-1"] = {"id": "run-1", "session_id": "old", "status": status, "expires": [store.now + d for d in expires_in]}
    integration, service = make_integration(store=store)
    session_id, notices, active = await integration._current_session("wx-bob", IDENTITY)
    assert (session_id, notices, active["id"]) == ("old", [], "run-1")
    service.runtime.cancel.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("hours_ago, switched", [(1, False), (3, True)])
async def test_task_left_with_only_expired_approvals_is_stopped_before_the_new_message(hours_ago, switched):
    store = SessionStore(members={"wx-bob": {"session_id": "old"}})
    own(store, "old", hours_ago=hours_ago, rounds=2)
    store.runs["run-1"] = {"id": "run-1", "session_id": "old", "status": "waiting_approval", "expires": [store.now - timedelta(minutes=1)]}
    integration, service = make_integration(store=store)
    session_id, notices, active = await integration._current_session("wx-bob", IDENTITY)
    service.runtime.cancel.assert_awaited_once()
    assert store.runs["run-1"]["status"] == "cancelled" and active is None
    assert store.cancelled_while_locked == [True]   # 不在进程里运行，停止很快，在锁内完成免得重复停止
    assert notices[0] == "上一个待审批的操作已过期，已停止该任务。"
    assert (session_id != "old") is switched and len(notices) == 1 + switched


@pytest.mark.asyncio
async def test_changed_account_continues_in_a_new_conversation():
    store = SessionStore(members={"wx-bob": {"session_id": "old"}})
    own(store, "old", username="alice")
    integration, _ = make_integration(store=store)
    session_id, notices, _ = await integration._current_session("wx-bob", IDENTITY)
    assert session_id != "old" and notices == []
    assert store.members["wx-bob"]["previous_session_id"] == "old"


@pytest.mark.asyncio
async def test_concurrent_messages_open_only_one_new_conversation():
    store = SessionStore(members={"wx-bob": {"session_id": "old"}})
    own(store, "old", hours_ago=3, rounds=5)
    integration, _ = make_integration(store=store)
    results = await asyncio.gather(*(integration._current_session("wx-bob", IDENTITY) for _ in range(3)))
    assert len({session_id for session_id, _, _ in results}) == 1 and len(store.sessions) == 2
    assert sum(bool(notices) for _, notices, _ in results) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["新对话", "新会话", "开始新对话", "清空上下文", "重新开始", "/new", "/reset", "/NEW"])
async def test_new_conversation_command_stops_the_unfinished_task(command):
    integration, service, store, client = member_chat({"wx-bob": {"session_id": "old"}})
    own(store, "old", rounds=3)
    store.runs["run-1"] = {"id": "run-1", "session_id": "old", "status": "running"}
    await integration._handle_text(text_frame("wx-bob", command))
    assert last_reply(client) == "已开始新对话。上一个对话里未完成的任务已停止。"
    service.runtime.cancel.assert_awaited_once()
    assert store.cancelled_while_locked == [False]   # 运行中的任务在锁和事务外停止
    assert store.members["wx-bob"]["previous_session_id"] == "old" != store.members["wx-bob"]["session_id"]
    service.new_run.assert_not_awaited()
    # 切换对话只用本地账号名，不调 master
    service.gateway.master.assert_not_awaited()
    service.bootstrap.assert_not_awaited()
    # 新对话还是空的时候再发一次：直接用它，「继续上次」仍然回到 old
    members = dict(store.members["wx-bob"])
    await integration._handle_text(text_frame("wx-bob", command, "message-2"))
    assert last_reply(client) == "已开始新对话。"
    assert store.members["wx-bob"] == members and len(store.sessions) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["继续上次", "继续上次对话", "回到上次", "/resume", "/RESUME"])
async def test_resume_command_returns_to_the_previous_conversation_and_keeps_it(command):
    integration, service, store, client = member_chat({"wx-bob": {"session_id": "new", "previous_session_id": "old"}})
    own(store, "old", hours_ago=20, rounds=12)
    own(store, "new")
    last_active = store.sessions["old"]["updated_at"].astimezone(BEIJING_TZ)
    await integration._handle_text(text_frame("wx-bob", command))
    assert last_reply(client) == f"已回到上一个对话（最后活跃：{last_active:%m-%d %H:%M}，共 12 轮）。"
    assert store.members["wx-bob"] == {"session_id": "old", "previous_session_id": "new"}
    # 切回时刷新了最后活跃时间：紧接着的消息不会被空闲规则切走
    assert await integration._current_session("wx-bob", IDENTITY) == ("old", [], None)
    service.new_run.assert_not_awaited()
    service.gateway.master.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("previous", [None, "alice", "gone"])
async def test_resume_refuses_missing_or_other_account_conversations(previous):
    members = {"wx-bob": {"session_id": "new", "previous_session_id": None if previous is None else "old"}}
    integration, _, store, client = member_chat(members)
    own(store, "new")
    if previous == "alice":
        own(store, "old", username="alice")
    await integration._handle_text(text_frame("wx-bob", "继续上次"))
    assert last_reply(client) == "没有可以回到的对话。"
    assert store.members == members


@pytest.mark.asyncio
@pytest.mark.parametrize("status, expires_in, offered", [("waiting_approval", timedelta(minutes=10), True), ("running", None, False)])
async def test_busy_conversation_reply_offers_a_new_conversation_for_pending_approval(status, expires_in, offered):
    integration, service, store, client = member_chat({"wx-bob": {"session_id": "old"}})
    own(store, "old")
    store.runs["run-1"] = {"id": "run-1", "session_id": "old", "status": status, "expires": [store.now + expires_in] if expires_in else []}
    await integration._handle_text(text_frame("wx-bob", "查一下 Pod"))
    reply = last_reply(client)
    assert reply.startswith("该会话已有任务运行或等待审批，请稍后重试。")
    assert ("发送「新对话」可以放弃它并重新开始。" in reply) is offered
    service.new_run.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_run_conflict_is_reported_as_busy():
    integration, service, store, client = member_chat({"wx-bob": {"session_id": "old"}})
    own(store, "old")
    service.new_run.side_effect = AIError("该会话有正在运行或等待批准的任务", 409, "run_active")
    await integration._handle_text(text_frame("wx-bob", "查一下 Pod"))
    assert last_reply(client) == "该会话已有任务运行或等待审批，请稍后重试。"


@pytest.mark.asyncio
async def test_idle_switch_notice_leads_the_streamed_answer():
    integration, service, store, client = member_chat({"wx-bob": {"session_id": "old"}})
    own(store, "old", hours_ago=3, rounds=4)
    store.events = [{"seq": 1, "type": "message", "data": {"content": "共有 3 个 Pod。"}},
                    {"seq": 2, "type": "done", "data": {"status": "completed"}}]
    service.new_run.side_effect = lambda identity, sid, scope, **kwargs: ({"id": "run-1", "session_id": sid, "scope": scope}, True)
    await integration._handle_text(text_frame("wx-bob", "有几个 Pod"))
    session_id = store.members["wx-bob"]["session_id"]
    assert service.new_run.await_args.args[1] == session_id != "old"
    assert service.runtime.start.call_args.args[2].model == "model-1"
    final = last_reply(client)
    assert final.startswith("距上次对话已超过 2 小时，已开始新对话（发送「继续上次」可回到之前的对话）\n\n共有 3 个 Pod。")
    assert "集群：prod" in final


def enter_frame(userid="wx-bob", msgid="enter-1"):
    return {"headers": {"req_id": "req-enter"}, "body": {"msgid": msgid, "from": {"userid": userid}, "chattype": "single",
                                                          "event": {"eventtype": "enter_chat"}}}


def card_frame(task_id, key, msgid="card-1"):
    return {"body": {"msgid": msgid, "from": {"userid": "wx-bob"}, "event": {"task_id": task_id, "event_key": key}}}


def welcome_card(client):
    frame, body = client.reply_welcome.await_args.args
    assert body["msgtype"] == "template_card"
    return body["template_card"]


@pytest.mark.asyncio
async def test_welcome_card_shows_cluster_and_last_conversation_without_calling_master():
    store = SessionStore(mapping={"wecom_userid": "wx-bob", "username": "alice", "current_env": "prod", "session_id": "old"})
    own(store, "old", rounds=12, username="alice")
    yesterday = datetime.now(BEIJING_TZ).replace(hour=18, minute=20, second=0, microsecond=0) - timedelta(days=1)
    store.sessions["old"]["updated_at"] = yesterday.astimezone(timezone.utc)
    integration, service = make_integration(store=store)
    client = connect(integration)
    await integration._handle_enter_chat(enter_frame())
    card = welcome_card(client)
    assert card["card_type"] == "button_interaction"
    assert card["main_title"] == {"title": "花折 AI 助手", "desc": "当前集群：prod"}
    assert card["sub_title_text"] == "上次对话：昨天 18:20，共 12 轮"
    assert [b["key"] for b in card["button_list"]] == ["session_new", "session_resume"]
    assert card["task_id"].startswith("session_old_")
    service.gateway.master.assert_not_awaited()
    service.bootstrap.assert_not_awaited()
    # 同一事件重复投递不再回复
    await integration._handle_enter_chat(enter_frame())
    client.reply_welcome.assert_awaited_once()


@pytest.mark.asyncio
async def test_welcome_for_unbound_members_uses_default_account_or_asks_for_binding():
    store = SessionStore(mapping={"wecom_userid": "wx-bob", "username": None}, unbound="guest", unbound_env="test")
    integration, _ = make_integration(store=store)
    client = connect(integration)
    await integration._handle_enter_chat(enter_frame())
    card = welcome_card(client)
    assert card["main_title"]["desc"] == "当前集群：test" and card["sub_title_text"] == "直接发送问题即可开始对话。"
    assert [b["key"] for b in card["button_list"]] == ["session_new"] and card["task_id"].startswith("session_none_")

    store.unbound = None
    await integration._handle_enter_chat(enter_frame(msgid="enter-2"))
    body = client.reply_welcome.await_args.args[1]
    assert body["msgtype"] == "text" and "尚未绑定花折账号" in body["text"]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("current, previous", [("old", None), ("new", "old")])
async def test_welcome_continue_button_returns_to_the_conversation_on_the_card(current, previous):
    # 卡片显示的是 old；用户可能先发了消息、被空闲规则切到了 new，再点「继续上次对话」
    integration, service, store, client = member_chat({"wx-bob": {"session_id": current, "previous_session_id": previous}})
    own(store, "old", hours_ago=20, rounds=12)
    own(store, "new")
    await integration._handle_card_event(card_frame("session_old_ab12cd34ef56", "session_resume"))
    card = client.update_template_card.await_args.args[1]
    assert card == {"card_type": "text_notice", "main_title": {"title": "继续上次对话"},
                    "sub_title_text": "已回到上次的对话（共 12 轮），直接发送问题即可继续。", "task_id": "session_old_ab12cd34ef56"}
    assert store.members["wx-bob"]["session_id"] == "old"
    # 已经在 old 上时指针不变；从 new 切回时 new 成为上一个对话
    assert store.members["wx-bob"]["previous_session_id"] == (previous if current == "old" else current)
    assert await integration._current_session("wx-bob", IDENTITY) == ("old", [], None)
    # 同一个按钮事件重复投递只处理一次
    await integration._handle_card_event(card_frame("session_old_ab12cd34ef56", "session_resume"))
    client.update_template_card.assert_awaited_once()


@pytest.mark.asyncio
async def test_welcome_new_button_starts_a_conversation_and_checks_the_member_first():
    integration, service, store, client = member_chat({"wx-bob": {"session_id": "old"}})
    own(store, "old", rounds=3)
    await integration._handle_card_event(card_frame("session_old_ab12cd34ef56", "session_new"))
    card = client.update_template_card.await_args.args[1]
    assert (card["main_title"]["title"], card["sub_title_text"]) == ("已开始新对话", "已开始新对话。")
    assert store.members["wx-bob"]["previous_session_id"] == "old"
    service.gateway.master.assert_not_awaited()   # 按钮只读本地账号，保证 5 秒内更新卡片

    store.unbound = None   # 默认权限被关闭:不能再切换对话
    await integration._handle_card_event(card_frame("session_none_ab12cd34ef56", "session_new", "card-2"))
    card = client.update_template_card.await_args.args[1]
    assert card["main_title"]["title"] == "无法切换对话" and "尚未绑定" in card["sub_title_text"]


def test_friendly_time_uses_beijing_days():
    from kubedoor_ai.wecom import _card_session, _friendly_time
    now = datetime.now(BEIJING_TZ).replace(hour=9, minute=5, second=0, microsecond=0)
    assert _friendly_time(now.astimezone(timezone.utc).isoformat()) == "今天 09:05"
    assert _friendly_time(now - timedelta(days=1)) == "昨天 09:05"
    assert _friendly_time(now - timedelta(days=3)) == f"{now - timedelta(days=3):%m-%d} 09:05"
    assert _card_session("session_6f1c4b3e-1d2a-4c5b-9e8f-0a1b2c3d4e5f_ab12") == "6f1c4b3e-1d2a-4c5b-9e8f-0a1b2c3d4e5f"
    assert _card_session("session_none_ab12") is None and _card_session(None) is None and _card_session("task-1") is None


@pytest.mark.parametrize("value, days", [(None, 7), ("30", 30), ("0", 0), ("-3", 0), ("seven", 7)])
def test_checkpoint_retention_days_come_from_kubedoor_config(monkeypatch, value, days):
    if value is None:
        monkeypatch.delenv("RETENTION_AI_CHECKPOINT_DAYS", raising=False)
    else:
        monkeypatch.setenv("RETENTION_AI_CHECKPOINT_DAYS", value)
    assert _retention_days() == days


class PurgeStore:
    """候选对话 + 在成员锁里再确认一次;dead 是再确认时仍然可删的对话"""
    def __init__(self, candidates, dead):
        self.candidates, self.dead = candidates, dead
        self.calls = []

    async def fetch(self, sql, *args):
        assert "checkpoint_purged_at IS NULL" in sql and "previous_session_id=s.id" in sql and "waiting_approval" in sql
        self.calls.append(("candidates", args))
        return [{"id": sid, "channel_key": key} for sid, key in self.candidates]

    @asynccontextmanager
    async def transaction(self):
        yield self

    async def execute(self, sql, *args):
        self.calls.append(("lock" if "pg_advisory_xact_lock" in sql else "mark", args))

    async def one(self, sql, *args):
        self.calls.append(("recheck", args))
        return {"id": args[1]} if args[1] in self.dead else None


@pytest.mark.asyncio
async def test_unreachable_old_wecom_conversations_lose_only_their_checkpoints():
    # s2 在两次查询之间又被切回（比如旧欢迎卡片），锁里再确认时不能删
    store = PurgeStore([("s1", "wx-a:alice:s1"), ("s2", "wx-b:bob")], dead={"s1"})
    checkpointer = SimpleNamespace(adelete_thread=AsyncMock())
    integration = WeComIntegration(SimpleNamespace(store=store, runtime=SimpleNamespace(checkpointer=checkpointer)))
    assert await integration.purge_checkpoints(7) == 1
    checkpointer.adelete_thread.assert_awaited_once_with("s1")
    assert store.calls == [("candidates", (7, 200)),
                           ("lock", ("wecom:wx-a",)), ("recheck", (7, "s1")), ("mark", ("s1",)),
                           ("lock", ("wecom:wx-b",)), ("recheck", (7, "s2"))]
    store.calls.clear()
    assert await integration.purge_checkpoints(0) == 0 and store.calls == []


@pytest.mark.asyncio
async def test_old_welcome_card_cannot_continue_a_purged_conversation():
    integration, _, store, client = member_chat({"wx-bob": {"session_id": "new", "previous_session_id": None}})
    own(store, "new")
    own(store, "old", hours_ago=24 * 9, rounds=12)
    store.sessions["old"]["purged"] = True
    await integration._handle_card_event(card_frame("session_old_ab12cd34ef56", "session_resume"))
    assert client.update_template_card.await_args.args[1]["sub_title_text"] == "该对话已不可用，直接发送问题即可开始新对话。"
    assert store.members["wx-bob"] == {"session_id": "new", "previous_session_id": None}


PLANNED_APPROVAL = {
    "name": "scale_deployment", "env": "prod", "arguments": {"namespace": "demo", "deployment": "app", "replicas": 5},
    "preview": {"plan": {
        "control": {"controlled": True, "blocked": False, "message": "受花折资源管控：准入控制会改写副本数"},
        "steps": [{"kind": "master", "name": "写入管控副本数"}, {"kind": "master", "name": "扩缩容"}],
        "changes": [{"scope": "在线", "label": "副本数", "current": "2", "target": "5"},
                    {"scope": "管控表", "label": "管控副本数（指定 Pod）", "current": "-1（按 AI 推荐 / 当日 Pod）", "target": "5"}],
        "effects": ["管控副本数写为 5：以后每次发布、重启都按 5 个副本"], "warnings": ["之前临时扩过的副本会恢复"]}},
}


def test_approval_text_shows_control_steps_changes_and_effects_above_the_card():
    text = _approval_text(PLANNED_APPROVAL)
    assert text.startswith("**待审批：扩缩容**（集群 prod）") and "目标：demo/app" in text
    assert "管控：受花折资源管控" in text and "1. 写入管控副本数\n2. 扩缩容" in text
    assert "- 在线 副本数：2 → 5" in text and "- ⚠️ 之前临时扩过的副本会恢复" in text
    summary = _approval_summary(PLANNED_APPROVAL)
    assert summary.startswith("demo/app · 受花折资源管控 · 副本数 2→5") and len(summary) <= 140


def test_approval_without_a_plan_shows_the_command_or_diff():
    event = {"name": "kubectl", "env": "prod", "arguments": {"argv": ["set", "image", "deployment/app", "app=app:v2"]},
             "preview": {"command": ["kubectl", "set", "image", "deployment/app", "app=app:v2"]}}
    assert "```\nkubectl set image deployment/app app=app:v2\n```" in _approval_text(event)
    assert _approval_summary(event) == "kubectl set image deployment/app app=app:v2"
    diff = {"name": "api", "env": "prod", "arguments": {}, "preview": {"diff": "-a\n+b" + "x" * 3000}}
    detail = _approval_text(diff).split("```\n", 1)[1]
    assert len(detail) < 1600 and detail.startswith("-a\n+b")
