import asyncio
import json
import re
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import asyncpg
import pytest

from kubedoor_ai.domain import BEIJING_TZ, AIError, Identity
from kubedoor_ai.gateway import RunContext, request_cluster_switch
from kubedoor_ai.http import create_app
from kubedoor_ai.wecom import (WeComIntegration, _PushReply, _StreamReply, _approval_summary, _approval_text,
                               _limit_stream, _mentioned_cluster, _pick_cluster, _retention_days)


class Store:
    """企微用到的表的最小替身:inbox 去重、审批、成员行(mapping)、默认权限设置、被拦下的提问"""
    def __init__(self, approval=None, mapping=None, unbound=None, unbound_env=None):
        self.seen = set()
        self.approval = approval
        self.mapping = mapping
        self.unbound = unbound
        self.unbound_env = unbound_env
        self.events = []      # 任务事件;要按任务区分时用 {run_id: [...]}
        self.executes = []
        self.pending = None   # 因为还没选集群被拦下的提问

    async def one(self, sql, *args):
        if "INSERT INTO kubedoor_ai_wecom_inbox" in sql:
            msgid = args[0]
            if msgid in self.seen:
                return None
            self.seen.add(msgid)
            return {"msgid": msgid}
        if "FROM kubedoor_ai_wecom_approvals" in sql:
            return self.approval if self.approval and args[0] == self.approval["task_id"] else None
        if "SET pending_at=NULL" in sql:
            text, self.pending = self.pending, None
            return {"pending_text": text} if text else None
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
        if "SET pending_text=$2" in sql:
            self.pending = args[1]

    async def fetch(self, sql, *args):
        if "FROM kubedoor_ai_events" not in sql:
            return []
        return self.events.get(args[0], []) if isinstance(self.events, dict) else self.events


class SessionStore(Store):
    """会话边界用到的表放在内存里;transaction() 用每个成员一把锁模拟 pg_advisory_xact_lock。
    成员行 = mapping(账号、默认集群)+ members 里会变的部分(对话指针、当前集群)"""
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
        if "AS purged FROM kubedoor_ai_sessions" in sql:
            return {"purged": bool(self.sessions.get(args[0], {}).get("purged"))}
        if sql.startswith("SELECT * FROM kubedoor_ai_wecom_users"):
            return {**(self.mapping or {}), **self.members.get(args[0], {})}
        return await super().one(sql, *args)

    async def execute(self, sql, *args):
        await super().execute(sql, *args)
        if sql.startswith("UPDATE kubedoor_ai_wecom_users SET current_env="):
            self.members.setdefault(args[0], {})["current_env"] = args[1]

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
        if "pending_text=NULL" in sql:   # 新对话:被拦下的提问作废
            store.pending = None
        if "pg_advisory_xact_lock" in sql:
            lock = store.locks[args[0]]
            await lock.acquire()
            self.held.append(lock)
        elif sql.startswith("INSERT INTO kubedoor_ai_sessions"):
            store.sessions[args[0]] = {"username": args[1], "channel_key": args[2], "updated_at": store.now, "rounds": 0}
        elif "UPDATE kubedoor_ai_wecom_users SET session_id" in sql:
            store.member_updates.append((sql, args))
            values = {"session_id": args[1], "previous_session_id": args[2]}
            if "current_env=" in sql:
                values["current_env"] = args[3]
            store.members.setdefault(args[0], {}).update(values)
        elif sql.startswith("UPDATE kubedoor_ai_wecom_users SET current_env="):
            store.members.setdefault(args[0], {})["current_env"] = args[1]
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
        if "FROM kubedoor_ai_runs WHERE session_id" in sql:   # 对话最后一次提问用的集群
            runs = [run for run in store.runs.values() if run["session_id"] == args[0]]
            return {"env": runs[-1].get("env")} if runs else None
        if "FROM kubedoor_ai_runs r" in sql:
            active = [run for run in store.runs.values() if run["session_id"] == args[0] and run["status"] in ("running", "waiting_approval")]
            return {"id": active[-1]["id"], "status": active[-1]["status"],
                    "approvable": any(at > store.now for at in active[-1].get("expires", []))} if active else None
        raise AssertionError(sql)


IDENTITY = Identity("guest", "read")
PROVIDER_CONFIG = {"enabled": True, "base_url": "https://llm.example/v1", "api_key": "key", "model": "model-1"}


def own(store, session_id, *, hours_ago=0, rounds=0, username="guest", env=None):
    """wx-bob 的一个对话;env = 这个对话最后一次提问用的集群"""
    store.sessions[session_id] = {"username": username, "channel_key": f"wx-bob:{username}",
                                  "updated_at": store.now - timedelta(hours=hours_ago), "rounds": rounds}
    if env:
        store.runs[f"run-{session_id}"] = {"id": f"run-{session_id}", "session_id": session_id, "status": "completed", "env": env}


def pointers(store, userid="wx-bob"):
    member = store.members[userid]
    return {key: member.get(key) for key in ("session_id", "previous_session_id")}


def member_chat(members, unbound_env=None, **kwargs):
    """未绑定成员 wx-bob 按默认权限账号 guest 聊天,会话表在内存里"""
    store = SessionStore(members=members, mapping={"wecom_userid": "wx-bob", "username": None, "current_env": "prod"},
                         unbound="guest", unbound_env=unbound_env, **kwargs)
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
        reply_template_card=AsyncMock(),
        update_template_card=AsyncMock(),
        send_message=AsyncMock(),
    )
    return integration.client


def text_frame(userid, content, msgid="message-1"):
    return {"body": {"msgid": msgid, "from": {"userid": userid}, "chattype": "single",
                     "text": {"content": content}}}


def card_frame(task_id, key, msgid="card-1", userid="wx-bob", env=None):
    """企微的模板卡片事件(长连接 aibot_event_callback):按钮和下拉框的值都在 event.template_card_event 里"""
    detail = {"card_type": "button_interaction", "event_key": key, "task_id": task_id}
    if env:
        detail["selected_items"] = {"selected_item": [{"question_key": "env", "option_ids": {"option_id": [env]}}]}
    return {"cmd": "aibot_event_callback", "headers": {"req_id": "req-card"},
            "body": {"msgid": msgid, "from": {"userid": userid}, "chattype": "single", "msgtype": "event",
                     "event": {"eventtype": "template_card_event", "template_card_event": detail}}}


def last_reply(client):
    return client.reply_stream.await_args.args[2]


def last_card(client):
    """跟在流式回复后面的模板卡片"""
    return client.reply_template_card.await_args.args[1]


def updated_card(client):
    """点了按钮的那张卡片被换成的内容"""
    return client.update_template_card.await_args.args[1]


def done(content, seq=1, **extra):
    return [{"seq": seq, "type": "message", "data": {"content": content}},
            {"seq": seq + 1, "type": "done", "data": {"status": "completed", **extra}}]


CLUSTERS = [{"env": name, "permission": "read", "online": name != "tx-flink-prod"}
            for name in ("b2c-prod-kunlun", "cassmall-beta-kunlun", "cassmall-kunlun", "tx-flink-prod")]
CLUSTERS[1]["aliases"] = ["beta 昆仑"]


def unbound_chat(current_env=None, default_env=None):
    """未绑定成员按默认权限账号 guest 聊天;对话和 _run_message 换成桩,只看这条消息落到哪个集群"""
    store = Store(mapping={"wecom_userid": "wx-bob", "username": None, "current_env": current_env},
                  unbound="guest", unbound_env=default_env)
    master = AsyncMock(return_value={"username": "guest", "permission": "read", "clusters": {}})
    integration, service = make_integration(store=store, master=master)
    service.bootstrap.return_value = {"clusters": CLUSTERS}
    integration._current_session = AsyncMock(return_value=("session-1", [], None, False))
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
    sql, args = service.store.executes[-1]
    # 记下绑定人;默认集群不写进当前集群,管理员以后改默认集群立即生效;改绑时被拦下的提问作废
    assert args == ("wx-alice", "alice", "prod", "admin") and "INSERT INTO kubedoor_ai_wecom_users(wecom_userid,username,default_env,bound_by" in sql
    assert "pending_text=CASE WHEN kubedoor_ai_wecom_users.username IS DISTINCT FROM EXCLUDED.username THEN NULL" in sql
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
    await integration._handle_card_event(card_frame("task-1", "approve", userid="attacker"))
    service.decision.assert_not_awaited()
    card = updated_card(client)
    assert card["main_title"]["title"] == "审批卡片已失效" and card["task_id"] == "task-1"
    # 卡片事件只能更新那张卡片,不能当成消息来回复
    client.reply_stream.assert_not_awaited()


@pytest.mark.asyncio
async def test_card_buttons_are_read_from_the_template_card_event():
    integration, _, store, client = member_chat({"wx-bob": {"session_id": "old"}})
    own(store, "old", rounds=3)
    frame = card_frame("session_old_ab12cd34ef56", "session_new", env="prod")
    await integration._handle_card_event(frame)
    card = updated_card(client)
    assert client.update_template_card.await_args.args[0] is frame
    # 结果用按钮卡片:text_notice 卡片必须带跳转链接,企微会拒绝;task_id 必须和回调里的一致
    assert card["card_type"] == "button_interaction" and card["task_id"] == "session_old_ab12cd34ef56"
    assert card["main_title"] == {"title": "已开始新对话", "desc": "集群：prod"}
    assert card["button_list"] == [{"text": "已完成", "key": "noop", "style": 2}]
    # 状态按钮、重复投递的事件、旧版本的平铺格式都不处理
    for event in (card_frame("session_old_ab12cd34ef56", "noop", "card-2"), frame,
                  {"body": {"msgid": "card-3", "from": {"userid": "wx-bob"}, "event": {"task_id": "x", "event_key": "session_new"}}}):
        await integration._handle_card_event(event)
    client.update_template_card.assert_awaited_once()


@pytest.mark.asyncio
async def test_card_update_failure_is_logged_with_the_wecom_error(caplog):
    integration, _, store, client = member_chat({"wx-bob": {"session_id": "old"}})
    client.update_template_card.side_effect = RuntimeError("Reply ack error: errcode=40058, errmsg=invalid template_card")
    await integration._handle_card_event(card_frame("session_none_ab12cd34ef56", "session_new"))
    assert "errcode=40058" in caplog.text


@pytest.mark.asyncio
async def test_unbound_member_without_default_permission_must_be_bound():
    store = Store(mapping={"wecom_userid": "wx-bob", "username": None})
    integration, service = make_integration(store=store)
    client = connect(integration)
    await integration._handle_text(text_frame("wx-bob", "当前集群"))
    reply = last_reply(client)
    assert "尚未绑定" in reply and "绑定企业微信" in reply and "我是 你的名字" in reply
    integration.service.gateway.master.assert_not_awaited()
    service.bootstrap.assert_not_awaited()


@pytest.mark.asyncio
async def test_unbound_member_uses_live_permissions_of_default_account():
    master = AsyncMock(return_value={"username": "guest", "permission": "read", "clusters": {"prod": "none"}})
    store = Store(mapping={"wecom_userid": "wx-bob", "username": None}, unbound="guest")
    integration, service = make_integration(store=store, master=master)
    integration._current_session = AsyncMock(return_value=("session-1", [], None, False))
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
    integration._current_session = AsyncMock(return_value=("session-1", [], None, False))
    service.bootstrap.return_value = {"clusters": [{"env": "prod", "permission": "rw"}]}
    client = connect(integration)
    await integration._handle_text(text_frame("wx-alice", "当前集群"))
    assert master.await_args.args[0].username == "alice"
    assert "1. prod · 读写" in last_reply(client)


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
    integration.config = PROVIDER_CONFIG
    client = connect(integration)
    integration._spawn = Mock()
    integration._watch_run = Mock()
    await integration._handle_card_event(card_frame("task-1", "approve", "event-1"))
    service.decision.assert_not_awaited()
    assert updated_card(client)["sub_title_text"] == "账号绑定已变更，该审批不能继续。"

    store.unbound = "guest"
    await integration._handle_card_event(card_frame("task-1", "approve", "event-2"))
    service.decision.assert_awaited_once()
    assert service.decision.await_args.args[0].username == "guest"
    card = updated_card(client)
    assert card["main_title"] == {"title": "已批准", "desc": "集群：prod"} and card["sub_title_text"] == "正在执行，结果稍后发送。"
    assert card["button_list"] == [{"text": "已批准", "key": "noop", "style": 2}]
    client.reply_stream.assert_not_awaited()


@pytest.mark.asyncio
async def test_approval_result_is_pushed_because_card_events_cannot_reply():
    approval = {"task_id": "task-1", "wecom_userid": "wx-bob", "username": "guest", "run_id": "run-1",
                "action_id": "action-1", "resume_after_seq": 3}
    store = Store(approval=approval, mapping={"wecom_userid": "wx-bob", "username": None}, unbound="guest")
    store.events = done("已扩容到 5 个副本。", seq=4)
    master = AsyncMock(return_value={"username": "guest", "permission": "rw", "clusters": {}})
    integration, service = make_integration(store=store, master=master)
    integration.config = PROVIDER_CONFIG
    client = connect(integration)
    await integration._handle_card_event(card_frame("task-1", "reject"))
    assert updated_card(client)["main_title"]["title"] == "已拒绝"
    assert service.decision.await_args.args[2]["decision"] == "reject"
    await asyncio.gather(*integration._tasks)
    chatid, body = client.send_message.await_args.args
    assert chatid == "wx-bob" and body["chat_type"] == 1 and body["msgtype"] == "markdown"
    assert body["markdown"]["content"].startswith("已扩容到 5 个副本。") and "集群：prod" in body["markdown"]["content"]
    client.reply_stream.assert_not_awaited()


@pytest.mark.asyncio
async def test_approval_that_fails_is_shown_on_the_card():
    approval = {"task_id": "task-1", "wecom_userid": "wx-bob", "username": "guest", "run_id": "run-1",
                "action_id": "action-1", "resume_after_seq": 3}
    store = Store(approval=approval, mapping={"wecom_userid": "wx-bob", "username": None}, unbound="guest")
    master = AsyncMock(return_value={"username": "guest", "permission": "rw", "clusters": {}})
    integration, service = make_integration(store=store, master=master)
    integration.config = PROVIDER_CONFIG
    client = connect(integration)
    service.decision.side_effect = AIError("批准已过期，请停止本轮后重新生成操作", 409, "approval_expired")
    await integration._handle_card_event(card_frame("task-1", "approve"))
    card = updated_card(client)
    assert card["main_title"]["title"] == "审批未完成" and card["sub_title_text"] == "批准已过期，请停止本轮后重新生成操作"


@pytest.mark.asyncio
async def test_new_session_never_binds_member_to_default_account():
    store = SessionStore(members={"wx-bob": {}})
    integration, _ = make_integration(store=store)
    session_id, notices, active, reset = await integration._current_session("wx-bob", Identity("guest", "read"))
    assert (notices, active, reset) == ([], None, False)
    assert store.sessions[session_id]["channel_key"] == f"wx-bob:guest:{session_id}"
    sql, args = store.member_updates[-1]
    # 第一次提问开对话:成员刚选的集群照常用,不改
    assert "SET session_id=" in sql and "username" not in sql and "current_env" not in sql
    assert args == ("wx-bob", session_id, None)


@pytest.mark.asyncio
async def test_cluster_menu_numbers_clusters_and_explains_switching():
    integration, store, client = unbound_chat(default_env="cassmall-kunlun")
    await integration._handle_text(text_frame("wx-bob", "集群"))
    menu = last_reply(client)
    assert "当前集群：cassmall-kunlun" in menu
    assert "1. b2c-prod-kunlun · 只读\n" in menu and "3. cassmall-kunlun · 只读（当前）" in menu
    assert "2. cassmall-beta-kunlun（beta 昆仑） · 只读" in menu
    assert "4. tx-flink-prod · 只读 · agent 离线" in menu
    assert "切换集群 序号、名称或别名" in menu
    assert menu.endswith("发送「新对话」重新开始，「继续上次」回到上一个对话。")
    # 菜单后面跟一张选集群的卡片
    card = last_card(client)
    assert card["main_title"] == {"title": "选择集群", "desc": "当前集群：cassmall-kunlun"}
    assert card["button_selection"]["selected_id"] == "cassmall-kunlun"
    assert [option["id"] for option in card["button_selection"]["option_list"]] == [item["env"] for item in CLUSTERS]
    assert card["button_list"] == [{"text": "切换到所选集群", "key": "env_pick", "style": 1}]
    integration._run_message.assert_not_awaited()
    assert saved_envs(store) == []


def test_cluster_card_lists_at_most_ten_clusters_and_keeps_the_suggested_one():
    from kubedoor_ai.wecom import _cluster_card
    clusters = [{"env": f"c{index:02d}"} for index in range(12)]
    card = _cluster_card(clusters, None, "c11")
    options = [option["id"] for option in card["button_selection"]["option_list"]]
    assert len(options) == 10 and options[0] == "c11" and card["button_selection"]["selected_id"] == "c11"
    assert "其它集群请发送「切换集群 名称」" in card["sub_title_text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("command, expected", [
    ("切换集群 3", "cassmall-kunlun"),
    ("切换集群cassmall-kunlun", "cassmall-kunlun"),
    ("切换到：CASSMALL-KUNLUN", "cassmall-kunlun"),
    ("/切换集群　flink", "tx-flink-prod"),
    ("切换集群 Beta 昆仑", "cassmall-beta-kunlun"),
    ("切换到 beta 昆仑集群", "cassmall-beta-kunlun"),
])
async def test_switch_cluster_by_index_name_alias_or_unique_part(command, expected):
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
    assert last_card(client)["button_list"][0]["key"] == "env_pick"
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
async def test_question_naming_a_cluster_by_alias_switches_to_it():
    integration, store, _ = unbound_chat(current_env="cassmall-kunlun")
    await integration._handle_text(text_frame("wx-bob", "cassmall beta 昆仑集群的k8s版本是什么"))
    call = integration._run_message.await_args
    assert call.args[4] == "cassmall-beta-kunlun"
    assert call.kwargs["status"].startswith("已切换到集群 cassmall-beta-kunlun")
    assert call.kwargs["clusters"] == CLUSTERS   # AI 也拿到集群和别名,本地认不出的由它判断
    assert saved_envs(store) == ["cassmall-beta-kunlun"]


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
    integration._current_session = AsyncMock(return_value=("session-1", [], None, False))
    integration._run_message = AsyncMock()
    client = connect(integration)
    await integration._handle_text(text_frame("wx-bob", "order 服务有几个 Pod"))
    assert last_reply(client) == "请先选择这个对话要操作的集群，选好后接着处理你刚才的问题。"
    card = last_card(client)
    assert card["main_title"]["desc"] == "当前集群：未选择" and "selected_id" not in card["button_selection"]
    assert card["sub_title_text"] == "选好后接着处理你刚才的问题。"
    integration._run_message.assert_not_awaited()
    assert saved_envs(store) == [] and store.pending == "order 服务有几个 Pod"


@pytest.mark.asyncio
async def test_final_answer_names_its_cluster():
    store = Store()
    store.events = [{"seq": 1, "type": "message", "data": {"content": "共有 3 个 Pod 在运行。"}},
                    {"seq": 2, "type": "done", "data": {"status": "completed"}}]
    integration, _ = make_integration(store=store)
    client = connect(integration)
    reply = _StreamReply(integration, {"body": {}})
    await integration._watch_run(reply, "wx-bob", Identity("guest", "read"), "run-1", 0, env="cassmall-kunlun")
    first, final = client.reply_stream.await_args_list[0].args, client.reply_stream.await_args.args
    assert first[2] == "正在分析请求..." and first[3] is False
    assert final[2].startswith("共有 3 个 Pod 在运行。") and "集群：cassmall-kunlun" in final[2]
    assert final[3] is True and final[1] == first[1] == reply.stream_id


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
    await integration._watch_run(_StreamReply(integration, {"body": {}}), "wx-bob", IDENTITY, "run-1", 0, env="prod")
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


def test_member_cleanup_needs_admin_but_self_binding_does_not():
    from starlette.testclient import TestClient
    wecom = SimpleNamespace(purge_member=AsyncMock(), save_remark=AsyncMock(),
                            binding=AsyncMock(return_value={"enabled": True, "members": []}),
                            create_bind_code=AsyncMock(return_value={"code": "042137", "expires_at": "2026-10-26T10:10:00+00:00"}),
                            unbind_self=AsyncMock(return_value={"success": True}))
    app = create_app(SimpleNamespace(), token="test-token")
    reader = {"X-Kubedoor-Token": "test-token", "X-User-Name": "reader", "X-User-Permission": "read"}
    admin = {**reader, "X-User-Name": "admin", "X-User-Permission": "admin"}
    with TestClient(app) as client:
        app.state.wecom = wecom
        assert client.post("/api/ai/wecom/users/wx-bob/purge", headers=reader).status_code == 403
        assert client.put("/api/ai/wecom/users/wx-bob/remark", headers=reader, json={"remark": "张三"}).status_code == 403
        wecom.purge_member.assert_not_awaited()
        wecom.save_remark.assert_not_awaited()
        # 任何登录用户都能管理自己账号绑定的企微成员
        assert client.get("/api/ai/wecom/binding", headers=reader).json() == {"enabled": True, "members": []}
        assert client.post("/api/ai/wecom/binding/code", headers=reader).json()["code"] == "042137"
        assert client.delete("/api/ai/wecom/binding/wx-bob", headers=reader).status_code == 200
        identity, userid = wecom.unbind_self.await_args.args
        assert identity.username == "reader" and userid == "wx-bob"
        wecom.purge_member.return_value = {"success": True, "sessions": 2, "stopped": 1}
        assert client.post("/api/ai/wecom/users/wx-bob/purge", headers=admin).json()["sessions"] == 2
        wecom.save_remark.return_value = {"remark": "张三"}
        assert client.put("/api/ai/wecom/users/wx-bob/remark", headers=admin, json={"remark": "张三"}).status_code == 200
        assert wecom.save_remark.await_args.args == ("wx-bob", {"remark": "张三"})


@pytest.mark.asyncio
@pytest.mark.parametrize("idle_minutes, elapsed", [(120, "2 小时"), (90, "90 分钟")])
async def test_idle_conversation_moves_the_next_message_to_a_new_one(idle_minutes, elapsed):
    store = SessionStore(members={"wx-bob": {"session_id": "old", "current_env": "test"}}, idle_minutes=idle_minutes)
    own(store, "old", hours_ago=3, rounds=12)
    integration, _ = make_integration(store=store)
    session_id, notices, active, reset = await integration._current_session("wx-bob", IDENTITY)
    assert session_id != "old" and active is None and reset
    assert notices == [f"距上次对话已超过 {elapsed}，已开始新对话（发送「继续上次」可回到之前的对话）"]
    # 新对话从默认集群开始:上一个对话里选的集群清掉
    assert store.members["wx-bob"] == {"session_id": session_id, "previous_session_id": "old", "current_env": None}
    assert store.sessions["old"]["rounds"] == 12   # 旧对话保留
    assert await integration._current_session("wx-bob", IDENTITY) == (session_id, [], None, False)


@pytest.mark.asyncio
async def test_recent_conversation_or_disabled_timeout_keeps_the_conversation():
    store = SessionStore(members={"wx-bob": {"session_id": "old"}})
    own(store, "old", hours_ago=1, rounds=3)
    integration, _ = make_integration(store=store)
    assert await integration._current_session("wx-bob", IDENTITY) == ("old", [], None, False)
    store.idle_minutes = 0
    store.sessions["old"]["updated_at"] -= timedelta(days=30)
    assert await integration._current_session("wx-bob", IDENTITY) == ("old", [], None, False)


@pytest.mark.asyncio
async def test_idle_but_empty_conversation_is_kept_so_the_previous_one_stays_reachable():
    # 刚发过「新对话」然后离开:新对话还是空的，再来消息时继续用它，不要把真正的上一个对话挤掉
    store = SessionStore(members={"wx-bob": {"session_id": "empty", "previous_session_id": "old"}})
    own(store, "empty", hours_ago=5)
    integration, _ = make_integration(store=store)
    assert await integration._current_session("wx-bob", IDENTITY) == ("empty", [], None, False)
    assert store.members["wx-bob"] == {"session_id": "empty", "previous_session_id": "old"}


@pytest.mark.asyncio
@pytest.mark.parametrize("status, expires_in", [("running", []), ("waiting_approval", [timedelta(minutes=10), timedelta(minutes=-1)])])
async def test_running_task_or_pending_approval_keeps_an_idle_conversation(status, expires_in):
    store = SessionStore(members={"wx-bob": {"session_id": "old"}})
    own(store, "old", hours_ago=3)
    store.runs["run-1"] = {"id": "run-1", "session_id": "old", "status": status, "expires": [store.now + d for d in expires_in]}
    integration, service = make_integration(store=store)
    session_id, notices, active, reset = await integration._current_session("wx-bob", IDENTITY)
    assert (session_id, notices, active["id"], reset) == ("old", [], "run-1", False)
    service.runtime.cancel.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("hours_ago, switched", [(1, False), (3, True)])
async def test_task_left_with_only_expired_approvals_is_stopped_before_the_new_message(hours_ago, switched):
    store = SessionStore(members={"wx-bob": {"session_id": "old"}})
    own(store, "old", hours_ago=hours_ago, rounds=2)
    store.runs["run-1"] = {"id": "run-1", "session_id": "old", "status": "waiting_approval", "expires": [store.now - timedelta(minutes=1)]}
    integration, service = make_integration(store=store)
    session_id, notices, active, reset = await integration._current_session("wx-bob", IDENTITY)
    service.runtime.cancel.assert_awaited_once()
    assert store.runs["run-1"]["status"] == "cancelled" and active is None
    assert store.cancelled_while_locked == [True]   # 不在进程里运行，停止很快，在锁内完成免得重复停止
    assert notices[0] == "上一个待审批的操作已过期，已停止该任务。"
    assert (session_id != "old") is switched is reset and len(notices) == 1 + switched


@pytest.mark.asyncio
async def test_changed_account_continues_in_a_new_conversation():
    store = SessionStore(members={"wx-bob": {"session_id": "old", "current_env": "test"}})
    own(store, "old", username="alice")
    integration, _ = make_integration(store=store)
    session_id, notices, _, reset = await integration._current_session("wx-bob", IDENTITY)
    assert session_id != "old" and notices == [] and not reset
    assert store.members["wx-bob"]["previous_session_id"] == "old"


@pytest.mark.asyncio
async def test_concurrent_messages_open_only_one_new_conversation():
    store = SessionStore(members={"wx-bob": {"session_id": "old"}})
    own(store, "old", hours_ago=3, rounds=5)
    integration, _ = make_integration(store=store)
    results = await asyncio.gather(*(integration._current_session("wx-bob", IDENTITY) for _ in range(3)))
    assert len({session_id for session_id, _, _, _ in results}) == 1 and len(store.sessions) == 2
    assert sum(bool(notices) for _, notices, _, _ in results) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["新对话", "新会话", "开始新对话", "清空上下文", "重新开始", "/new", "/reset", "/NEW"])
async def test_new_conversation_command_stops_the_unfinished_task(command):
    integration, service, store, client = member_chat({"wx-bob": {"session_id": "old", "current_env": "test"}}, unbound_env="prod")
    own(store, "old", rounds=3)
    store.runs["run-1"] = {"id": "run-1", "session_id": "old", "status": "running"}
    await integration._handle_text(text_frame("wx-bob", command))
    assert last_reply(client) == "已开始新对话。上一个对话里未完成的任务已停止。这个对话使用默认集群 prod。"
    service.runtime.cancel.assert_awaited_once()
    assert store.cancelled_while_locked == [False]   # 运行中的任务在锁和事务外停止
    assert pointers(store)["previous_session_id"] == "old" != pointers(store)["session_id"]
    assert store.members["wx-bob"]["current_env"] is None   # 新对话回到默认集群
    service.new_run.assert_not_awaited()
    # 有默认集群时，切换对话只用本地数据，不调 master
    service.gateway.master.assert_not_awaited()
    service.bootstrap.assert_not_awaited()
    # 新对话还是空的时候再发一次：直接用它，「继续上次」仍然回到 old
    before = pointers(store)
    await integration._handle_text(text_frame("wx-bob", command, "message-2"))
    assert last_reply(client) == "已开始新对话。这个对话使用默认集群 prod。"
    assert pointers(store) == before and len(store.sessions) == 2


@pytest.mark.asyncio
async def test_new_conversation_without_default_cluster_comes_with_a_cluster_card():
    integration, service, store, client = member_chat({"wx-bob": {"session_id": "old", "current_env": "prod"}})
    own(store, "old", rounds=3)
    await integration._handle_text(text_frame("wx-bob", "新对话"))
    assert last_reply(client) == "已开始新对话。请在下面的卡片里选择这个对话要操作的集群。"
    card = last_card(client)
    # 下拉框默认选中上一个对话用的集群,点一下就行
    assert card["button_selection"]["selected_id"] == "prod" and card["main_title"]["desc"] == "当前集群：未选择"
    assert store.members["wx-bob"]["current_env"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["继续上次", "继续上次对话", "回到上次", "/resume", "/RESUME"])
async def test_resume_command_returns_to_the_previous_conversation_and_keeps_it(command):
    integration, service, store, client = member_chat({"wx-bob": {"session_id": "new", "previous_session_id": "old"}})
    own(store, "old", hours_ago=20, rounds=12)
    own(store, "new")
    last_active = store.sessions["old"]["updated_at"].astimezone(BEIJING_TZ)
    await integration._handle_text(text_frame("wx-bob", command))
    assert last_reply(client) == f"已回到上一个对话（最后活跃：{last_active:%m-%d %H:%M}，共 12 轮）。"
    assert pointers(store) == {"session_id": "old", "previous_session_id": "new"}
    # 切回时刷新了最后活跃时间：紧接着的消息不会被空闲规则切走
    assert await integration._current_session("wx-bob", IDENTITY) == ("old", [], None, False)
    service.new_run.assert_not_awaited()
    service.gateway.master.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_restores_the_cluster_of_that_conversation():
    integration, _, store, client = member_chat({"wx-bob": {"session_id": "new", "previous_session_id": "old", "current_env": "prod"}})
    own(store, "old", hours_ago=20, rounds=12, env="cassmall-kunlun")
    own(store, "new", env="prod")
    await integration._handle_text(text_frame("wx-bob", "继续上次"))
    assert last_reply(client).endswith("共 12 轮，集群：cassmall-kunlun）。")
    assert store.members["wx-bob"]["current_env"] == "cassmall-kunlun"
    await integration._handle_text(text_frame("wx-bob", "继续上次", "message-2"))
    assert store.members["wx-bob"]["current_env"] == "prod"


@pytest.mark.asyncio
@pytest.mark.parametrize("previous", [None, "alice", "gone"])
async def test_resume_refuses_missing_or_other_account_conversations(previous):
    members = {"wx-bob": {"session_id": "new", "previous_session_id": None if previous is None else "old"}}
    integration, _, store, client = member_chat(members)
    own(store, "new")
    if previous == "alice":
        own(store, "old", username="alice")
    before = {key: dict(value) for key, value in members.items()}
    await integration._handle_text(text_frame("wx-bob", "继续上次"))
    assert last_reply(client) == "没有可以回到的对话。"
    assert store.members == before


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
    integration, service, store, client = member_chat({"wx-bob": {"session_id": "old"}}, unbound_env="prod")
    own(store, "old", hours_ago=3, rounds=4)
    store.events = done("共有 3 个 Pod。")
    service.new_run.side_effect = lambda identity, sid, scope, **kwargs: ({"id": "run-1", "session_id": sid, "scope": scope}, True)
    await integration._handle_text(text_frame("wx-bob", "有几个 Pod"))
    session_id = store.members["wx-bob"]["session_id"]
    assert service.new_run.await_args.args[1] == session_id != "old"
    assert service.runtime.start.call_args.args[2].model == "model-1"
    final = last_reply(client)
    assert final.startswith("距上次对话已超过 2 小时，已开始新对话（发送「继续上次」可回到之前的对话）\n\n共有 3 个 Pod。")
    assert "集群：prod" in final


@pytest.mark.asyncio
async def test_new_conversation_goes_back_to_the_default_cluster():
    integration, service, store, client = member_chat({"wx-bob": {"session_id": "old", "current_env": "test"}}, unbound_env="prod")
    service.bootstrap.return_value = {"clusters": [{"env": "prod", "permission": "read"}, {"env": "test", "permission": "read"}]}
    own(store, "old", hours_ago=3, rounds=4)
    store.events = done("共有 3 个 Pod。")
    service.new_run.side_effect = lambda identity, sid, scope, **kwargs: ({"id": "run-1", "session_id": sid, "scope": scope}, True)
    await integration._handle_text(text_frame("wx-bob", "有几个 Pod"))
    assert service.new_run.await_args.args[2]["env"] == "prod"
    assert last_reply(client).startswith("距上次对话已超过 2 小时，已开始新对话（发送「继续上次」可回到之前的对话）\n"
                                         "新对话使用默认集群 prod。\n\n共有 3 个 Pod。")


@pytest.mark.asyncio
async def test_held_question_continues_after_a_cluster_is_picked_on_the_card():
    integration, service, store, client = member_chat({"wx-bob": {"session_id": "old", "current_env": "prod"}})
    own(store, "old", hours_ago=3, rounds=4)
    await integration._handle_text(text_frame("wx-bob", "order 服务有几个 Pod"))
    reply = last_reply(client)
    assert reply.startswith("距上次对话已超过 2 小时，已开始新对话") and reply.endswith("请先选择这个对话要操作的集群，选好后接着处理你刚才的问题。")
    card = last_card(client)
    assert card["button_selection"]["selected_id"] == "prod"   # 下拉框默认选中上一个对话用的集群
    assert store.pending == "order 服务有几个 Pod"
    service.new_run.assert_not_awaited()

    store.events = done("order 有 3 个 Pod。")
    service.new_run.side_effect = lambda identity, sid, scope, **kwargs: ({"id": "run-1", "session_id": sid, "scope": scope}, True)
    await integration._handle_card_event(card_frame(card["task_id"], "env_pick", env="prod"))
    picked = updated_card(client)
    assert picked["main_title"] == {"title": "已切换集群", "desc": "prod"}
    assert picked["sub_title_text"] == "正在处理你刚才的问题：order 服务有几个 Pod，结果稍后发送。"
    await asyncio.gather(*integration._tasks)
    assert service.new_run.await_args.args[2]["env"] == "prod"
    assert service.runtime.start.call_args.args[3] == "order 服务有几个 Pod"
    chatid, body = client.send_message.await_args.args
    assert chatid == "wx-bob" and body["markdown"]["content"].startswith("order 有 3 个 Pod。")
    assert store.pending is None


@pytest.mark.asyncio
async def test_held_question_continues_after_switch_command():
    integration, service, store, client = member_chat({"wx-bob": {"session_id": "old", "current_env": "prod"}})
    own(store, "old", hours_ago=3, rounds=4)
    await integration._handle_text(text_frame("wx-bob", "order 服务有几个 Pod"))
    store.events = done("order 有 3 个 Pod。")
    service.new_run.side_effect = lambda identity, sid, scope, **kwargs: ({"id": "run-1", "session_id": sid, "scope": scope}, True)
    await integration._handle_text(text_frame("wx-bob", "切换集群 1", "message-2"))
    assert service.runtime.start.call_args.args[3] == "order 服务有几个 Pod"
    assert last_reply(client).startswith("order 有 3 个 Pod。")
    assert store.members["wx-bob"]["current_env"] == "prod"


@pytest.mark.asyncio
async def test_ai_switch_request_reruns_the_question_on_that_cluster():
    integration, service, store, client = member_chat({"wx-bob": {"session_id": "old", "current_env": "prod"}})
    own(store, "old", rounds=1)
    clusters = [{"env": "prod", "permission": "read"},
                {"env": "cassmall-beta-kunlun", "permission": "read", "aliases": ["beta昆仑"]}]
    service.bootstrap.return_value = {"clusters": clusters}
    runs = iter(["run-1", "run-2"])
    service.new_run.side_effect = lambda identity, sid, scope, **kwargs: ({"id": next(runs), "session_id": sid, "scope": scope}, True)
    store.events = {"run-1": [{"seq": 1, "type": "done", "data": {"status": "completed", "switch_env": "cassmall-beta-kunlun"}}],
                    "run-2": done("版本是 v1.30.2。")}
    # 本地认不出「cassmall 测试环境」是哪个集群,AI 判断后请求切换
    await integration._handle_text(text_frame("wx-bob", "cassmall 测试环境的 k8s 版本是什么"))
    first, second = [call.args for call in service.runtime.start.call_args_list]
    assert first[0]["scope"]["env"] == "prod" and first[0]["_clusters"] == clusters and first[0]["_switched"] is False
    assert first[3] == "cassmall 测试环境的 k8s 版本是什么"
    assert second[0]["scope"]["env"] == "cassmall-beta-kunlun" and second[0]["_switched"] is True
    assert second[3] == "已切换到集群 cassmall-beta-kunlun，请在这个集群上回答我上一条消息里的问题。"
    assert store.members["wx-bob"]["current_env"] == "cassmall-beta-kunlun"
    final = last_reply(client)
    assert final.startswith("版本是 v1.30.2。") and "集群：cassmall-beta-kunlun" in final
    # 两轮都在同一条流式消息里
    assert len({call.args[1] for call in client.reply_stream.await_args_list}) == 1


def two_clusters(service, *extra):
    service.bootstrap.return_value = {"clusters": [{"env": env, "permission": "read"} for env in ("prod", "test", *extra)]}


def answers(service, store, content="共有 3 个 Pod。"):
    store.events = done(content)
    service.new_run.side_effect = lambda identity, sid, scope, **kwargs: ({"id": "run-1", "session_id": sid, "scope": scope}, True)


@pytest.mark.asyncio
async def test_cluster_switched_after_an_idle_gap_is_kept_for_the_next_question():
    # 隔天先切集群再提问:空闲的对话在切换时就换成新对话,刚选的集群不会被下一条提问换回默认集群
    integration, service, store, client = member_chat({"wx-bob": {"session_id": "old", "current_env": "prod"}}, unbound_env="prod")
    two_clusters(service)
    own(store, "old", hours_ago=3, rounds=4)
    await integration._handle_text(text_frame("wx-bob", "切换集群 test"))
    assert last_reply(client) == ("距上次对话已超过 2 小时，已开始新对话（发送「继续上次」可回到之前的对话）\n\n"
                                  "已切换到集群：test，之后的提问都在这个集群上执行。")
    answers(service, store)
    await integration._handle_text(text_frame("wx-bob", "有几个 Pod", "message-2"))
    assert service.new_run.await_args.args[2]["env"] == "test"
    assert last_reply(client).startswith("共有 3 个 Pod。")


@pytest.mark.asyncio
async def test_cluster_picked_on_a_card_after_an_idle_gap_is_kept_for_the_next_question():
    integration, service, store, client = member_chat({"wx-bob": {"session_id": "old", "current_env": "prod"}})   # 没有默认集群
    two_clusters(service)
    own(store, "old", hours_ago=3, rounds=4)
    await integration._handle_card_event(card_frame("env_ab12cd34ef56ab12", "env_pick", env="test"))
    assert updated_card(client)["main_title"] == {"title": "已切换集群", "desc": "test"}
    assert pointers(store)["previous_session_id"] == "old"   # 选集群时空闲的对话就换成了新对话
    service.gateway.master.assert_not_awaited()
    answers(service, store)
    await integration._handle_text(text_frame("wx-bob", "有几个 Pod"))
    assert service.new_run.await_args.args[2]["env"] == "test"   # 不会再被拦下要求选集群


@pytest.mark.asyncio
async def test_menu_after_an_idle_gap_shows_the_cluster_the_next_question_will_use():
    integration, service, store, client = member_chat({"wx-bob": {"session_id": "old", "current_env": "test"}}, unbound_env="prod")
    two_clusters(service)
    own(store, "old", hours_ago=3, rounds=4)
    await integration._handle_text(text_frame("wx-bob", "集群"))
    menu = last_reply(client)
    assert menu.startswith("距上次对话已超过 2 小时，已开始新对话") and "\n当前集群：prod\n" in menu
    assert last_card(client)["main_title"]["desc"] == "当前集群：prod"


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["切换到 cassmall-kunlun 集群看一下 nginx 的日志", "切换到cassmall-kunlun 看下 pod"])
async def test_switch_wording_followed_by_a_question_is_answered_on_that_cluster(text):
    integration, store, client = unbound_chat(current_env="tx-flink-prod")
    await integration._handle_text(text_frame("wx-bob", text))
    call = integration._run_message.await_args
    assert call.args[4] == "cassmall-kunlun" and call.args[5] == text
    assert call.kwargs["status"].startswith("已切换到集群 cassmall-kunlun")
    assert saved_envs(store) == ["cassmall-kunlun"]


@pytest.mark.asyncio
async def test_question_naming_another_cluster_in_a_new_conversation_does_not_claim_the_default():
    integration, service, store, client = member_chat({"wx-bob": {"session_id": "old", "current_env": "test"}}, unbound_env="prod")
    two_clusters(service, "cassmall-kunlun")
    own(store, "old", hours_ago=3, rounds=4)
    answers(service, store)
    await integration._handle_text(text_frame("wx-bob", "cassmall-kunlun 有几个 Pod"))
    assert service.new_run.await_args.args[2]["env"] == "cassmall-kunlun"
    assert "新对话使用默认集群" not in last_reply(client)


@pytest.mark.asyncio
async def test_switch_is_not_lost_when_the_watcher_only_sees_the_finished_status():
    # done 事件在状态改成 completed 之后才写:轮询刚好只看到状态时,切换目标从任务行里取
    class RacingStore(Store):
        async def one(self, sql, *args):
            if sql.startswith("SELECT status,event_seq,usage,switch_env FROM kubedoor_ai_runs"):
                return {"status": "completed", "event_seq": 2, "usage": None, "switch_env": "cassmall-kunlun"}
            return await super().one(sql, *args)

    store = RacingStore()
    store.events = [{"seq": 1, "type": "token", "data": {"content": "正在切换到 cassmall-kunlun 查询。"}},
                    {"seq": 2, "type": "status", "data": {"status": "completed"}}]
    integration, _ = make_integration(store=store)
    client = connect(integration)
    reply = _StreamReply(integration, {"body": {}})
    assert await integration._watch_run(reply, "wx-bob", IDENTITY, "run-1", 0, env="prod", allow_switch=True) == "cassmall-kunlun"
    assert all(call.args[3] is False for call in client.reply_stream.await_args_list)   # 流式消息留给重跑的那一轮结束
    # 不允许切换的轮次(审批后、重跑那一轮)照常结束
    assert await integration._watch_run(reply, "wx-bob", IDENTITY, "run-1", 0, env="prod") is None
    assert client.reply_stream.await_args.args[3] is True


@pytest.mark.asyncio
async def test_question_on_a_conversation_purged_meanwhile_is_not_run():
    integration, service, store, client = member_chat({"wx-bob": {"session_id": "old"}}, unbound_env="prod")
    own(store, "old")
    # 这条消息定下对话之后,管理员恰好清理了这个成员
    integration._current_session = AsyncMock(return_value=("old", [], None, False))
    store.sessions["old"]["purged"] = True
    answers(service, store)
    service.runtime.cancel.side_effect = None
    await integration._handle_text(text_frame("wx-bob", "有几个 Pod"))
    service.runtime.start.assert_not_called()
    assert service.runtime.cancel.await_args.args[0]["id"] == "run-1"
    assert last_reply(client) == "你的企业微信成员记录刚被管理员清理，请重新发送问题。"


@pytest.mark.asyncio
async def test_new_conversation_drops_the_question_held_for_the_previous_one():
    integration, service, store, client = member_chat({"wx-bob": {"session_id": "old", "current_env": "prod"}})
    two_clusters(service)
    own(store, "old", hours_ago=3, rounds=4)
    await integration._handle_text(text_frame("wx-bob", "order 服务有几个 Pod"))
    assert store.pending == "order 服务有几个 Pod"
    await integration._handle_text(text_frame("wx-bob", "新对话", "message-2"))
    assert store.pending is None
    # 选集群后不会把作废的问题拿出来执行
    await integration._handle_card_event(card_frame("env_ab12cd34ef56ab12", "env_pick", env="test"))
    assert updated_card(client)["sub_title_text"] == "之后的提问都在这个集群上执行。"
    service.new_run.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_conversation_button_does_not_pin_the_default_cluster():
    integration, service, store, client = member_chat({"wx-bob": {"session_id": "old", "current_env": "test"}}, unbound_env="prod")
    own(store, "old", rounds=3)
    await integration._handle_card_event(card_frame("session_old_ab12cd34ef56", "session_new", env="prod"))
    assert updated_card(client)["main_title"] == {"title": "已开始新对话", "desc": "集群：prod"}
    # 选的就是默认集群:不写进当前集群,管理员之后改默认集群照样跟着变
    assert store.members["wx-bob"]["current_env"] is None


@pytest.mark.asyncio
async def test_new_conversation_button_stops_the_running_task_in_the_background():
    integration, service, store, client = member_chat({"wx-bob": {"session_id": "old"}}, unbound_env="prod")
    own(store, "old", rounds=3)
    store.runs["run-1"] = {"id": "run-1", "session_id": "old", "status": "running"}
    released = asyncio.Event()

    async def slow_cancel(run, identity):
        await released.wait()
        store.cancel(run)

    service.runtime.cancel.side_effect = slow_cancel
    await integration._handle_card_event(card_frame("session_old_ab12cd34ef56", "session_new"))
    # 不等任务停下就更新卡片(企微要求 5 秒内)
    assert updated_card(client)["sub_title_text"] == "已开始新对话。上一个对话里未完成的任务正在停止。直接发送问题即可。"
    assert store.runs["run-1"]["status"] == "running"
    released.set()
    await asyncio.gather(*integration._tasks)
    assert store.runs["run-1"]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_slow_approval_still_updates_the_card_in_time_and_pushes_the_result(monkeypatch):
    monkeypatch.setattr("kubedoor_ai.wecom.CARD_SECONDS", 0.2)
    approval = {"task_id": "task-1", "wecom_userid": "wx-bob", "username": "guest", "run_id": "run-1",
                "action_id": "action-1", "resume_after_seq": 3}
    store = Store(approval=approval, mapping={"wecom_userid": "wx-bob", "username": None}, unbound="guest")
    store.events = done("已扩容到 5 个副本。", seq=4)
    master = AsyncMock(return_value={"username": "guest", "permission": "rw", "clusters": {}})
    integration, service = make_integration(store=store, master=master)
    integration.config = PROVIDER_CONFIG
    client = connect(integration)
    released = asyncio.Event()

    async def slow_decision(*args):
        await released.wait()   # 恢复任务前要问 master 集群状态,master 很慢
        return {"status": "running"}

    service.decision.side_effect = slow_decision
    await integration._handle_card_event(card_frame("task-1", "approve"))
    card = updated_card(client)
    assert (card["main_title"]["title"], card["sub_title_text"]) == ("已收到", "正在处理，结果稍后发送。")
    assert not any("DELETE FROM kubedoor_ai_wecom_approvals" in sql for sql, _ in store.executes)
    released.set()
    await asyncio.gather(*integration._tasks)
    assert ("DELETE FROM kubedoor_ai_wecom_approvals WHERE task_id=$1", ("task-1",)) in store.executes
    assert client.send_message.await_args.args[1]["markdown"]["content"].startswith("已扩容到 5 个副本。")


ALIASED = [{"env": "cassmall-kunlun", "aliases": ["昆仑"]}, {"env": "cassmall-beta-kunlun", "aliases": ["beta 昆仑"]},
           {"env": "prod"}]


@pytest.mark.parametrize("text, expected", [
    ("cassmall beta 昆仑集群的k8s版本是什么", "cassmall-beta-kunlun"),   # 别名,写法里的空格可有可无
    ("Beta昆仑有几个节点", "cassmall-beta-kunlun"),                      # 「昆仑」被更长的「beta 昆仑」盖住
    ("昆仑的 order 服务", "cassmall-kunlun"),
    ("CASSMALL_KUNLUN 的 Pod", "cassmall-kunlun"),                       # 集群名不区分大小写和分隔符
    ("cassmallkunlun 的 Pod", "cassmall-kunlun"),
    ("prod环境的 Pod", "prod"),
    ("production 的 Pod", None),                                         # 不认半个单词
    ("cassmall-kunlun-2 的 Pod", None),
    ("对比 prod 和昆仑", None),                                           # 说到两个集群:不替成员挑
    ("查一下 Pod", None),
])
def test_mentioned_cluster_accepts_aliases_and_loose_spelling(text, expected):
    assert _mentioned_cluster(text, ALIASED) == expected


@pytest.mark.parametrize("target, expected", [
    ("2", ("cassmall-beta-kunlun", [])),
    ("Beta 昆仑", ("cassmall-beta-kunlun", [])),
    ("cassmall kunlun", ("cassmall-kunlun", [])),
    ("beta", ("cassmall-beta-kunlun", [])),
    ("kunlun", (None, ["cassmall-kunlun", "cassmall-beta-kunlun"])),
    ("9", (None, [])),
])
def test_pick_cluster_by_index_name_alias_or_unique_part(target, expected):
    assert _pick_cluster(target, ALIASED) == expected


def test_only_wecom_questions_can_request_a_cluster_switch():
    clusters = [{"env": "prod"}, {"env": "beta", "aliases": ["测试"]}]
    run = {"id": "run-1", "origin": "wecom", "scope": {"env": "prod"}, "_clusters": clusters}
    ctx = RunContext(run, IDENTITY)
    result = request_cluster_switch(ctx, {"env": "beta"})
    assert result["success"] and ctx.switch_env == "beta" and "不要再调用工具" in result["message"]
    # 同一轮里不能再切一次
    with pytest.raises(AIError) as error:
        request_cluster_switch(ctx, {"env": "beta"})
    assert error.value.code == "switch_repeated" and ctx.switch_env == "beta"
    for changes, env, code in (({"origin": "chat"}, "beta", "switch_unsupported"),
                               ({"origin": "mcp"}, "beta", "switch_unsupported"),
                               ({"_clusters": []}, "beta", "switch_unsupported"),   # 审批恢复的轮次没有集群列表
                               ({"_switched": True}, "beta", "switch_repeated"),
                               ({}, "hidden", "cluster_not_found"),
                               ({}, None, "cluster_not_found"),
                               ({}, "prod", "switch_same")):
        ctx = RunContext({**run, **changes}, IDENTITY)
        with pytest.raises(AIError) as error:
            request_cluster_switch(ctx, {"env": env})
        assert error.value.code == code and ctx.switch_env is None


def enter_frame(userid="wx-bob", msgid="enter-1"):
    return {"headers": {"req_id": "req-enter"}, "body": {"msgid": msgid, "from": {"userid": userid}, "chattype": "single",
                                                          "event": {"eventtype": "enter_chat"}}}


def welcome_card(client):
    frame, body = client.reply_welcome.await_args.args
    assert body["msgtype"] == "template_card"
    return body["template_card"]


@pytest.mark.asyncio
async def test_welcome_card_shows_the_last_conversation_and_a_cluster_for_the_new_one():
    store = SessionStore(mapping={"wecom_userid": "wx-bob", "username": "alice", "default_env": "prod", "current_env": "test",
                                  "session_id": "old"})
    own(store, "old", rounds=12, username="alice")
    yesterday = datetime.now(BEIJING_TZ).replace(hour=18, minute=20, second=0, microsecond=0) - timedelta(days=1)
    store.sessions["old"]["updated_at"] = yesterday.astimezone(timezone.utc)
    master = AsyncMock(return_value={"username": "alice", "permission": "read", "clusters": {}})
    integration, service = make_integration(store=store, master=master)
    service.bootstrap.return_value = {"clusters": [{"env": "prod", "permission": "read"}, {"env": "test", "permission": "read"}]}
    client = connect(integration)
    await integration._handle_enter_chat(enter_frame())
    card = welcome_card(client)
    assert card["card_type"] == "button_interaction"
    assert card["main_title"] == {"title": "花折 AI 助手", "desc": "默认集群：prod"}
    assert card["sub_title_text"] == "上次对话：昨天 18:20，共 12 轮"
    assert [b["key"] for b in card["button_list"]] == ["session_new", "session_resume"]
    assert card["task_id"].startswith("session_old_")
    # 新对话从默认集群开始,下拉框默认选中它
    selection = card["button_selection"]
    assert selection["question_key"] == "env" and selection["title"] == "新对话的集群" and selection["selected_id"] == "prod"
    assert [option["id"] for option in selection["option_list"]] == ["prod", "test"]
    # 同一事件重复投递不再回复
    await integration._handle_enter_chat(enter_frame())
    client.reply_welcome.assert_awaited_once()


@pytest.mark.asyncio
async def test_welcome_card_does_not_wait_for_a_slow_master(monkeypatch):
    monkeypatch.setattr("kubedoor_ai.wecom.CARD_MASTER_SECONDS", 0.05)

    async def slow(*args):
        await asyncio.sleep(5)

    store = SessionStore(mapping={"wecom_userid": "wx-bob", "username": None}, unbound="guest", unbound_env="test")
    integration, _ = make_integration(store=store, master=AsyncMock(side_effect=slow))
    client = connect(integration)
    started = time.monotonic()
    await integration._handle_enter_chat(enter_frame())
    assert time.monotonic() - started < 2
    card = welcome_card(client)
    assert "button_selection" not in card and card["main_title"]["desc"] == "默认集群：test"


@pytest.mark.asyncio
async def test_welcome_for_unbound_members_uses_default_account_or_asks_for_binding():
    store = SessionStore(mapping={"wecom_userid": "wx-bob", "username": None}, unbound="guest", unbound_env=None)
    master = AsyncMock(return_value={"username": "guest", "permission": "read", "clusters": {}})
    integration, service = make_integration(store=store, master=master)
    service.bootstrap.return_value = {"clusters": [{"env": "test", "permission": "read"}]}
    client = connect(integration)
    await integration._handle_enter_chat(enter_frame())
    card = welcome_card(client)
    assert card["main_title"]["desc"] == "新对话要先选集群" and card["sub_title_text"] == "直接发送问题即可开始对话。"
    assert [b["key"] for b in card["button_list"]] == ["session_new"] and card["task_id"].startswith("session_none_")
    assert "selected_id" not in card["button_selection"]

    store.unbound = None
    await integration._handle_enter_chat(enter_frame(msgid="enter-2"))
    body = client.reply_welcome.await_args.args[1]
    assert body["msgtype"] == "text" and "尚未绑定花折账号" in body["text"]["content"]
    assert "绑定 绑定码" in body["text"]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("current, previous", [("old", None), ("new", "old")])
async def test_welcome_continue_button_returns_to_the_conversation_on_the_card(current, previous):
    # 卡片显示的是 old；用户可能先发了消息、被空闲规则切到了 new，再点「继续上次对话」
    integration, service, store, client = member_chat({"wx-bob": {"session_id": current, "previous_session_id": previous}})
    own(store, "old", hours_ago=20, rounds=12, env="prod")
    own(store, "new")
    await integration._handle_card_event(card_frame("session_old_ab12cd34ef56", "session_resume"))
    card = updated_card(client)
    # 从 new 切回 old 时集群恢复成 old 用的;本来就在 old 上不用动
    title = {"title": "继续上次对话"} if current == "old" else {"title": "继续上次对话", "desc": "集群：prod"}
    assert card == {"card_type": "button_interaction", "main_title": title,
                    "sub_title_text": "已回到上次的对话（共 12 轮），直接发送问题即可继续。",
                    "button_list": [{"text": "已完成", "key": "noop", "style": 2}], "task_id": "session_old_ab12cd34ef56"}
    assert store.members["wx-bob"]["session_id"] == "old"
    # 已经在 old 上时指针不变；从 new 切回时 new 成为上一个对话
    assert store.members["wx-bob"]["previous_session_id"] == (previous if current == "old" else current)
    assert await integration._current_session("wx-bob", IDENTITY) == ("old", [], None, False)
    # 同一个按钮事件重复投递只处理一次
    await integration._handle_card_event(card_frame("session_old_ab12cd34ef56", "session_resume"))
    client.update_template_card.assert_awaited_once()


@pytest.mark.asyncio
async def test_welcome_new_button_uses_the_picked_cluster_and_checks_the_member_first():
    integration, service, store, client = member_chat({"wx-bob": {"session_id": "old", "current_env": "prod"}})
    own(store, "old", rounds=3)
    await integration._handle_card_event(card_frame("session_old_ab12cd34ef56", "session_new", env="test"))
    card = updated_card(client)
    assert (card["main_title"], card["sub_title_text"]) == ({"title": "已开始新对话", "desc": "集群：test"}, "已开始新对话。直接发送问题即可。")
    assert pointers(store)["previous_session_id"] == "old" and store.members["wx-bob"]["current_env"] == "test"
    service.gateway.master.assert_not_awaited()   # 按钮只读本地账号，保证 5 秒内更新卡片

    # 卡片上没有下拉框(当时 master 太慢)又没有默认集群:提问时再选
    await integration._handle_card_event(card_frame("session_none_ab12cd34ef56", "session_new", "card-2"))
    card = updated_card(client)
    assert card["main_title"] == {"title": "已开始新对话", "desc": "未选择集群"}
    assert card["sub_title_text"] == "已开始新对话。发送问题后会请你先选择集群。"
    assert store.members["wx-bob"]["current_env"] is None

    store.unbound = None   # 默认权限被关闭:不能再切换对话
    await integration._handle_card_event(card_frame("session_none_ab12cd34ef56", "session_new", "card-3"))
    card = updated_card(client)
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
    assert updated_card(client)["sub_title_text"] == "该对话已不可用，直接发送问题即可开始新对话。"
    assert pointers(store) == {"session_id": "new", "previous_session_id": None}


class MemberPurgeStore:
    """清理企微成员:在成员锁里删成员行和待审批,给他的对话打标记,找出进行中的任务"""
    def __init__(self, exists=True):
        self.exists, self.calls = exists, []

    @asynccontextmanager
    async def transaction(self):
        yield self

    async def execute(self, sql, *args):
        self.calls.append((sql.split(" WHERE")[0], args))

    async def one(self, sql, *args):
        self.calls.append((sql.split(" WHERE")[0], args))
        return {"wecom_userid": args[0]} if self.exists else None

    async def fetch(self, sql, *args):
        self.calls.append((sql.split(" WHERE")[0], args))
        if sql.startswith("UPDATE kubedoor_ai_sessions"):
            return [{"id": "s1"}, {"id": "s2"}]
        return [{"id": "run-1", "status": "running", "username": "alice"}]


@pytest.mark.asyncio
async def test_purged_member_starts_over_as_a_new_member():
    store = MemberPurgeStore()
    checkpointer = SimpleNamespace(adelete_thread=AsyncMock())
    runtime = SimpleNamespace(checkpointer=checkpointer, cancel=AsyncMock())
    integration = WeComIntegration(SimpleNamespace(store=store, runtime=runtime))
    assert await integration.purge_member("wx-bob") == {"success": True, "sessions": 2, "stopped": 1}
    assert [call[0] for call in store.calls] == [
        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
        "DELETE FROM kubedoor_ai_wecom_users",
        "DELETE FROM kubedoor_ai_wecom_approvals",
        "UPDATE kubedoor_ai_sessions SET checkpoint_purged_at=now()",
        "SELECT r.id,r.status,r.username FROM kubedoor_ai_runs r JOIN kubedoor_ai_sessions s ON s.id=r.session_id",
    ]
    assert store.calls[0][1] == ("wecom:wx-bob",) and store.calls[3][1] == ("wx-bob:",)
    # 任务和检查点在锁外处理;会话和消息记录不删
    assert runtime.cancel.await_args.args[0]["id"] == "run-1"
    assert [call.args[0] for call in checkpointer.adelete_thread.await_args_list] == ["s1", "s2"]
    assert not any(sql.startswith("DELETE FROM kubedoor_ai_sessions") for sql, _ in store.calls)

    store.exists = False
    with pytest.raises(AIError) as error:
        await integration.purge_member("wx-gone")
    assert error.value.status == 404


@pytest.mark.asyncio
async def test_members_can_tell_the_admin_who_they_are():
    store = Store(mapping={"wecom_userid": "wx-bob", "username": None})
    integration, service = make_integration(store=store)
    client = connect(integration)
    await integration._handle_text(text_frame("wx-bob", "我是 张三"))
    assert "已记下你的名字「张三」" in last_reply(client)
    sql, args = store.executes[-1]
    assert "SET self_name=$2" in sql and args == ("wx-bob", "张三")
    service.gateway.master.assert_not_awaited()   # 未绑定、没有默认权限也能自报
    # 提问不会被当成名字
    for index, text in enumerate(("我是不是该扩容", "我是新来的，怎么查 Pod", "我是张三"), 2):
        await integration._handle_text(text_frame("wx-bob", text, f"message-{index}"))
        assert "尚未绑定" in last_reply(client)
    assert sum("SET self_name" in sql for sql, _ in store.executes) == 1


class BindStore(Store):
    """绑定码表 + 成员的输错计数(total 是所有成员加起来的);transaction() 直接用自己"""
    def __init__(self, codes=None):
        super().__init__(mapping={"wecom_userid": "wx-bob", "username": None})
        self.codes = dict(codes or {})
        self.failures, self.recent, self.total = 0, False, 0

    @asynccontextmanager
    async def transaction(self):
        yield self

    async def one(self, sql, *args):
        if sql.startswith("SELECT bind_failures"):
            return {"bind_failures": self.failures, "recent": self.recent}
        if "sum(bind_failures)" in sql:
            return {"failures": self.total}
        if sql.startswith("DELETE FROM kubedoor_ai_wecom_bind_codes"):
            username = self.codes.pop(args[0], None)
            return {"username": username} if username else None
        return await super().one(sql, *args)

    async def execute(self, sql, *args):
        await super().execute(sql, *args)
        if "bind_failures=CASE" in sql:
            self.failures, self.recent = (self.failures + 1 if self.recent else 1), True


@pytest.mark.asyncio
async def test_member_binds_itself_with_a_code_from_the_web():
    store = BindStore(codes={"042137": "alice"})
    master = AsyncMock(return_value={"username": "alice", "permission": "read", "clusters": {}})
    integration, _ = make_integration(store=store, master=master)
    client = connect(integration)
    # 手机输入法打出的全角数字和冒号也认
    await integration._handle_text(text_frame("wx-bob", "绑定：０４２１３７"))
    assert last_reply(client) == "已绑定花折账号 alice，之后按这个账号的权限使用机器人。"
    sql, args = store.executes[-1]
    assert "username=$2,bound_by=$2,bound_at=now()" in sql and args == ("wx-bob", "alice")
    # 换账号时原来的集群不沿用,被拦下的提问作废
    assert "default_env=CASE WHEN username IS NOT DISTINCT FROM $2" in sql and "current_env=CASE" in sql
    assert "pending_text=NULL,pending_at=NULL" in sql
    assert master.await_args.args[0].username == "alice"
    assert "042137" not in store.codes   # 绑定码只能用一次
    await integration._handle_text(text_frame("wx-bob", "绑定 042137", "message-2"))
    assert last_reply(client).startswith("绑定码不对或已过期")


@pytest.mark.asyncio
async def test_failed_push_is_logged_instead_of_breaking_the_background_task(caplog):
    integration, _ = make_integration()
    client = connect(integration)
    client.send_message.side_effect = RuntimeError("Reply ack error: errcode=45009, errmsg=api freq out of limit")
    await _PushReply(integration, "wx-bob").finish("执行完成")
    assert "errcode=45009" in caplog.text


@pytest.mark.asyncio
async def test_too_many_wrong_codes_pause_binding():
    store = BindStore(codes={"042137": "alice"})
    master = AsyncMock(return_value={"username": "alice", "permission": "read", "clusters": {}})
    integration, _ = make_integration(store=store, master=master)
    for _ in range(5):
        assert (await integration._bind_by_code("wx-bob", "000000")).startswith("绑定码不对或已过期")
    # 输错 5 次后,对的码也要等一会儿
    assert await integration._bind_by_code("wx-bob", "042137") == "绑定码输错次数太多，请 10 分钟后再试。"
    assert "042137" in store.codes
    store.recent = False   # 过了 10 分钟
    assert (await integration._bind_by_code("wx-bob", "042137")).startswith("已绑定花折账号 alice")


@pytest.mark.asyncio
async def test_binding_refuses_accounts_that_cannot_log_in():
    store = BindStore(codes={"042137": "ghost"})
    master = AsyncMock(side_effect=AIError("KubeDoor 接口返回 HTTP 403", 403, "upstream_error"))
    integration, _ = make_integration(store=store, master=master)
    assert await integration._bind_by_code("wx-bob", "042137") == "花折账号 ghost 不存在、已停用或不可用，不能绑定。"
    assert not any("bound_by=$2" in sql for sql, _ in store.executes)


@pytest.mark.asyncio
async def test_binding_pauses_for_everyone_after_many_wrong_codes(caplog):
    # 多个企微账号一起猜码:所有成员加起来输错太多次也暂停,对的码也不收
    store = BindStore(codes={"042137": "alice"})
    store.total = 30
    integration, _ = make_integration(store=store, master=AsyncMock())
    assert (await integration._bind_by_code("wx-bob", "042137")).startswith("绑定暂时不可用")
    assert "042137" in store.codes and "暂停自助绑定" in caplog.text


@pytest.mark.asyncio
async def test_bind_codes_need_the_bot_and_are_one_per_account():
    store = SimpleNamespace(execute=AsyncMock(),
                            one=AsyncMock(return_value={"code": "042137", "expires_at": "2026-10-26T10:10:00+00:00"}))
    integration = WeComIntegration(SimpleNamespace(store=store))
    alice = Identity("alice", "read")
    with pytest.raises(AIError, match="还没有启用"):
        await integration.create_bind_code(alice)
    integration.config = {"enabled": True}
    assert (await integration.create_bind_code(alice))["code"] == "042137"
    sql, code, username, minutes = store.one.await_args.args
    assert "ON CONFLICT(username)" in sql and re.fullmatch(r"\d{6}", code) and (username, minutes) == ("alice", 10)
    assert "expires_at<now()" in store.execute.await_args.args[0]   # 先清掉过期的码
    # 和别人正在用的码撞了就换一个
    store.one.side_effect = [asyncpg.UniqueViolationError("duplicate key"), {"code": "731009", "expires_at": "x"}]
    assert (await integration.create_bind_code(alice))["code"] == "731009"


@pytest.mark.asyncio
async def test_users_see_and_remove_only_their_own_wecom_bindings():
    store = SimpleNamespace(fetch=AsyncMock(return_value=[{"wecom_userid": "wx-bob"}]), one=AsyncMock(return_value=None))
    integration = WeComIntegration(SimpleNamespace(store=store))
    alice = Identity("alice", "read")
    result = await integration.binding(alice)
    assert result == {"enabled": False, "connected": False, "members": [{"wecom_userid": "wx-bob"}], "code_minutes": 10}
    assert store.fetch.await_args.args[1] == "alice" and "remark" not in store.fetch.await_args.args[0]
    with pytest.raises(AIError) as error:
        await integration.unbind_self(alice, "wx-carol")
    assert error.value.status == 404
    sql, *args = store.one.await_args.args
    assert "WHERE wecom_userid=$1 AND username=$2" in sql and args == ["wx-carol", "alice"]
    store.one.return_value = {"wecom_userid": "wx-bob"}
    assert await integration.unbind_self(alice, "wx-bob") == {"success": True}


@pytest.mark.asyncio
async def test_admin_remark_helps_to_recognise_members():
    store = SimpleNamespace(one=AsyncMock(return_value={"wecom_userid": "wx-bob"}))
    integration = WeComIntegration(SimpleNamespace(store=store))
    assert await integration.save_remark("wx-bob", {"remark": " 张三（运维） "}) == {"remark": "张三（运维）"}
    assert await integration.save_remark("wx-bob", {"remark": ""}) == {"remark": None}
    with pytest.raises(AIError, match="最多 100"):
        await integration.save_remark("wx-bob", {"remark": "长" * 101})
    store.one.return_value = None
    with pytest.raises(AIError, match="不存在"):
        await integration.save_remark("wx-gone", {"remark": "张三"})


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
