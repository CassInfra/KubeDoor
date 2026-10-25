from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import time
import uuid
from contextlib import suppress
from datetime import datetime
from types import MappingProxyType

from aibot import WSClient, WSClientOptions, generate_req_id

from .domain import BEIJING_TZ, AIError, Identity, Provider, parse_model_limits, parse_reasoning_effort, redact

LOGGER = logging.getLogger(__name__)
CHANNEL = "wecom"
MAX_STREAM_BYTES = 18000
# 查看集群列表的命令;切换命令:「切换集群 2」「切换到：cassmall-kunlun」「/切换集群 flink」
MENU_COMMANDS = {"集群", "当前集群", "查看集群", "集群列表", "切换集群", "帮助", "help", "?", "？"}
SWITCH_COMMAND = re.compile(r"^/?(切换到集群|切换集群|切换到|切换)\s*[:：]?\s*(\S+)$")
# 企微只有一个聊天窗口:开新对话、回到上一个对话都靠聊天命令
NEW_SESSION_COMMANDS = {"新对话", "新会话", "开始新对话", "清空上下文", "重新开始", "/new", "/reset"}
RESUME_COMMANDS = {"继续上次", "继续上次对话", "回到上次", "/resume"}
DEFAULT_IDLE_MINUTES = 120
# 上下文占用(最近一次输入 ÷ 模型窗口)达到这个比例时,回答末尾提示可以开新对话
CONTEXT_HINT_RATIO = 0.25
BUSY_HINT = "该会话已有任务运行或等待审批，请稍后重试。"
PROVIDER_KEYS = ("base_url", "api_key", "model", "max_input_tokens", "max_output_tokens", "compression_ratio", "reasoning_effort")
# 企微里再也不会继续的旧对话(不是任何成员的当前对话或上一个对话、没有进行中的任务),
# 最后活跃超过 RETENTION_AI_CHECKPOINT_DAYS 天后删除检查点;会话和消息记录保留用于审计
DEFAULT_RETENTION_DAYS = 7
PURGE_BATCH = 200
PURGE_FIRST_DELAY = 300
PURGE_INTERVAL = 6 * 3600
DEAD_WECOM_SESSION = (
    "s.channel='wecom' AND s.checkpoint_purged_at IS NULL AND s.updated_at < now() - make_interval(days => $1::int) "
    "AND NOT EXISTS (SELECT 1 FROM kubedoor_ai_wecom_users u WHERE u.session_id=s.id OR u.previous_session_id=s.id) "
    "AND NOT EXISTS (SELECT 1 FROM kubedoor_ai_runs r WHERE r.session_id=s.id AND r.status IN ('running','waiting_approval'))"
)
PERMISSION_LABELS = {"rw": "读写", "read": "只读"}


class _SdkLogger:
    def debug(self, message, *args):
        return

    def info(self, message, *args):
        LOGGER.info("企微 SDK: %s", message.split(":", 1)[0])

    def warn(self, message, *args):
        LOGGER.warning("企微 SDK: %s", message.split(":", 1)[0])

    def error(self, message, *args):
        LOGGER.error("企微 SDK: %s", message.split(":", 1)[0])


class WeComIntegration:
    def __init__(self, service):
        self.service = service
        self.client = None
        self.config = None
        self.status = {"configured": False, "connected": False, "authenticated": False,
                       "reconnecting": False, "error": None}
        self._lock = asyncio.Lock()
        self._tasks = set()

    async def start(self):
        await self.reload()
        # 机器人停用后旧对话照样要清理,不依赖长连接
        self._spawn(self._purge_loop(), "wecom-checkpoint-retention")

    async def purge_checkpoints(self, days, limit=PURGE_BATCH):
        """删除企微里再也不会继续的旧对话的 LangGraph 检查点,返回删除的对话数。

        整条线程删除,不涉及 DeltaChannel 只能从快照重放的问题。每个对话在它成员的
        锁里再确认一次并打上标记:旧欢迎卡片的「继续上次对话」看到标记会拒绝,
        不会切回一个已经没有检查点的对话。
        """
        if days <= 0:
            return 0
        store = self.service.store
        rows = await store.fetch(
            "SELECT s.id,s.channel_key FROM kubedoor_ai_sessions s WHERE " + DEAD_WECOM_SESSION
            + " ORDER BY s.updated_at LIMIT $2", days, limit,
        )
        purged = 0
        for row in rows:
            userid = (row["channel_key"] or "").split(":", 1)[0]
            async with store.transaction() as tx:
                await tx.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", f"wecom:{userid}")
                if not await tx.one("SELECT s.id FROM kubedoor_ai_sessions s WHERE s.id=$2::uuid AND " + DEAD_WECOM_SESSION, days, row["id"]):
                    continue
                await self.service.runtime.checkpointer.adelete_thread(row["id"])
                await tx.execute("UPDATE kubedoor_ai_sessions SET checkpoint_purged_at=now() WHERE id=$1::uuid", row["id"])
            purged += 1
        return purged

    async def _purge_loop(self):
        days = _retention_days()
        if days <= 0:
            return
        await asyncio.sleep(PURGE_FIRST_DELAY)
        while True:
            try:
                while await self.purge_checkpoints(days) == PURGE_BATCH:
                    pass
            except Exception as exc:
                LOGGER.warning("企微旧对话检查点清理失败: %s", type(exc).__name__)
            await asyncio.sleep(PURGE_INTERVAL)

    async def close(self):
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        async with self._lock:
            await self._disconnect()

    async def reload(self):
        try:
            row = await self.service.store.one(
                "SELECT enabled,encrypted_config FROM kubedoor_ai_wecom_config WHERE singleton=true"
            )
            if not row:
                await self.configure(None)
                return
            config = self.service.gateway.cipher.decrypt("wecom", bytes(row["encrypted_config"]))
            config["enabled"] = row["enabled"]
            await self.configure(config)
        except Exception as exc:
            self.status = {"configured": False, "connected": False, "authenticated": False,
                           "reconnecting": False, "error": "企微配置无法读取或解密"}
            LOGGER.warning("企微配置加载失败: %s", type(exc).__name__)

    async def configure(self, config):
        async with self._lock:
            await self._disconnect()
            self.config = config
            self.status = {"configured": bool(config and config.get("enabled")),
                           "connected": False, "authenticated": False,
                           "reconnecting": False, "error": None}
            if not config or not config.get("enabled"):
                return
            if not all(config.get(key) for key in ("bot_id", "bot_secret", "base_url", "api_key", "model")):
                self.status["error"] = "企微机器人或模型配置不完整"
                return
            client = WSClient(WSClientOptions(
                bot_id=config["bot_id"], secret=config["bot_secret"],
                max_reconnect_attempts=-1, logger=_SdkLogger(),
            ))
            client.on("connected", lambda: self._connection_state(connected=True, reconnecting=False))
            client.on("authenticated", lambda: self._connection_state(authenticated=True, error=None))
            client.on("disconnected", lambda _: self._connection_state(connected=False, authenticated=False))
            client.on("reconnecting", lambda _: self._connection_state(connected=False, authenticated=False, reconnecting=True))
            client.on("error", self._sdk_error)
            client.on("message.text", self._on_text)
            client.on("event.template_card_event", self._on_card_event)
            client.on("event.enter_chat", self._on_enter_chat)
            self.client = client
            try:
                await client.connect()
            except Exception as exc:
                self.status["error"] = f"长连接启动失败（{type(exc).__name__}）"
                LOGGER.warning("企业微信机器人连接初始化失败: %s", type(exc).__name__)

    async def _disconnect(self):
        client, self.client = self.client, None
        if client:
            with suppress(Exception):
                client.disconnect()
            await asyncio.sleep(0)

    def _connection_state(self, **values):
        self.status.update(values)

    def _sdk_error(self, error):
        self.status["error"] = "企业微信长连接错误"
        LOGGER.warning("企业微信机器人连接错误: %s", type(error).__name__)

    def _spawn(self, coroutine, name):
        task = asyncio.create_task(coroutine, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _claim_message(self, body, userid, kind):
        msgid = body.get("msgid")
        if not isinstance(msgid, str) or not msgid:
            return True
        row = await self.service.store.one(
            "INSERT INTO kubedoor_ai_wecom_inbox(msgid,wecom_userid,kind) VALUES($1,$2,$3) "
            "ON CONFLICT(msgid) DO NOTHING RETURNING msgid", msgid, userid, kind,
        )
        return bool(row)

    async def _mapped_user(self, userid):
        await self.service.store.execute(
            "INSERT INTO kubedoor_ai_wecom_users(wecom_userid) VALUES($1) "
            "ON CONFLICT(wecom_userid) DO UPDATE SET last_seen_at=now()", userid,
        )
        return await self.service.store.one(
            "SELECT * FROM kubedoor_ai_wecom_users WHERE wecom_userid=$1", userid,
        )

    async def _identity(self, username):
        hint = Identity(username, "none")
        try:
            async with asyncio.timeout(10):
                raw = await self.service.gateway.master(hint, "GET", "/api/ai/internal/identity")
        except AIError as exc:
            # master 不认这个用户名(不存在、已停用或是 Agent 账号)时回 403,网关只带回了状态码
            if exc.status == 403:
                raise AIError(f"花折账号 {username} 不存在、已停用或不是 Web 账号", 403, "identity_invalid") from None
            raise
        permission = raw.get("permission")
        clusters = raw.get("clusters")
        if raw.get("username") != username or permission not in ("admin", "rw", "read", "none") or not isinstance(clusters, dict):
            raise AIError("花折用户身份无效", 403, "identity_invalid")
        return Identity(username, permission, MappingProxyType({} if permission == "admin" else clusters))

    async def unbound_setting(self):
        """未绑定成员的默认权限账号和默认集群:没设账号 = 必须先绑定,没设集群 = 先选集群才能提问"""
        row = await self.service.store.one(
            "SELECT unbound_username,unbound_default_env FROM kubedoor_ai_wecom_config WHERE singleton=true"
        ) or {}
        return {"unbound_username": row.get("unbound_username"), "unbound_default_env": row.get("unbound_default_env")}

    async def _member_identity(self, mapped):
        """返回 (身份, 默认集群, 不能使用时回给成员的提示)。绑定了的成员用绑定的花折账号和他的默认集群,
        未绑定的用默认权限账号和默认集群;权限都由 master 按账号实时计算。"""
        if mapped.get("username"):
            try:
                return await self._identity(mapped["username"]), mapped.get("default_env"), None
            except AIError:
                return None, None, "绑定的花折账号不存在、已停用或不可用，请联系管理员。"
        setting = await self.unbound_setting()
        if not setting["unbound_username"]:
            return None, None, "该企业微信成员尚未绑定花折账号，请联系管理员完成绑定。"
        try:
            identity = await self._identity(setting["unbound_username"])
        except AIError:
            identity = None
        # 保存时已经不让选管理员账号;这里兜底账号之后被改成管理员的情况
        if identity is None or identity.is_admin:
            return None, None, "未绑定成员的默认权限当前不可用，请联系管理员绑定花折账号。"
        return identity, setting["unbound_default_env"], None

    async def _reply(self, frame, content, finish=True, stream_id=None):
        if self.client and self.client.is_connected:
            await self.client.reply_stream(frame, stream_id or generate_req_id("stream"),
                                           _limit_stream(content), finish)

    async def _on_text(self, frame):
        self._spawn(self._handle_text(frame), "wecom-message")

    async def _handle_text(self, frame):
        body = frame.get("body") or {}
        userid = ((body.get("from") or {}).get("userid"))
        if not isinstance(userid, str) or not userid or body.get("chattype", "single") != "single":
            await self._reply(frame, "花折 AI 助手首版仅支持单聊。")
            return
        if not await self._claim_message(body, userid, "text"):
            await self._reply(frame, "该消息已在处理中或已经处理。")
            return
        stream_id = generate_req_id("stream")
        await self._reply(frame, "正在校验账号与访问权限...", False, stream_id)
        mapped = await self._mapped_user(userid)
        text = ((body.get("text") or {}).get("content") or "").strip()
        if not text:
            await self._reply(frame, "暂不支持空消息。", True, stream_id)
            return
        if text.lower() in NEW_SESSION_COMMANDS | RESUME_COMMANDS:
            await self._reply(frame, await self._switch_command(userid, mapped, text.lower()), True, stream_id)
            return
        identity, default_env, problem = await self._member_identity(mapped)
        if problem:
            await self._reply(frame, problem, True, stream_id)
            return
        clusters = [item for item in (await self.service.bootstrap(identity))["clusters"]
                    if item.get("permission") in ("rw", "read")]
        if not clusters:
            await self._reply(frame, "当前花折账号没有任何 K8S 集群访问权限。", True, stream_id)
            return
        names = [item["env"] for item in clusters]
        # 当前集群 = 成员自己切换过的,否则用默认集群;都没有(默认集群选了「无」)就要先选,不替成员挑
        current = mapped.get("current_env") or default_env
        if current not in names:
            current = None
        if text.lower() in MENU_COMMANDS:
            await self._reply(frame, _cluster_menu(clusters, current), True, stream_id)
            return
        switch = SWITCH_COMMAND.match(text)
        if switch:
            target = switch.group(2)
            env, candidates = _pick_cluster(target, names)
            if env:
                await self._set_current_env(userid, env)
                await self._reply(frame, f"已切换到集群：{env}，之后的提问都在这个集群上执行。", True, stream_id)
                return
            # 只写了「切换 / 切换到」又对不上任何集群的(如「切换镜像到 v2」)是普通提问,交给 AI
            if "集群" in switch.group(1) or candidates:
                hint = (f"「{target}」匹配到多个集群：{'、'.join(candidates)}，请写完整名称或序号。"
                        if candidates else f"没有找到集群「{target}」。")
                await self._reply(frame, hint + "\n\n" + _cluster_menu(clusters, current), True, stream_id)
                return
        status = None
        mentioned = _mentioned_cluster(text, names)
        if mentioned and mentioned != current:
            current, status = mentioned, f"已切换到集群 {mentioned}，正在分析请求..."
            await self._set_current_env(userid, mentioned)
        if current is None:
            await self._reply(frame, "还没有选择集群，请先选一个再提问。\n\n" + _cluster_menu(clusters, None), True, stream_id)
            return
        try:
            await self._run_message(frame, userid, mapped, identity, current, text, stream_id, status=status)
        except Exception as exc:
            LOGGER.warning("企业微信消息处理失败: %s", type(exc).__name__)
            await self._reply(frame, "处理失败，请检查花折 AI 服务状态后重试。", True, stream_id)

    async def _set_current_env(self, userid, env):
        await self.service.store.execute(
            "UPDATE kubedoor_ai_wecom_users SET current_env=$2,updated_at=now() WHERE wecom_userid=$1", userid, env,
        )

    async def _session_state(self, tx, userid, identity):
        """加锁后读取成员行、空闲超时分钟数和有效的当前对话;同一成员的并发消息在这里排队"""
        await tx.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", f"wecom:{userid}")
        member = await tx.one(
            "SELECT session_id,previous_session_id FROM kubedoor_ai_wecom_users WHERE wecom_userid=$1", userid,
        ) or {}
        setting = await tx.one("SELECT session_idle_minutes FROM kubedoor_ai_wecom_config WHERE singleton=true") or {}
        idle = setting.get("session_idle_minutes")
        idle = DEFAULT_IDLE_MINUTES if idle is None else idle
        return member, idle, await self._own_session(tx, member.get("session_id"), userid, identity, idle)

    @staticmethod
    async def _own_session(tx, session_id, userid, identity, idle=0):
        """属于这个企微成员和花折账号的对话,带最后活跃时间、是否已空闲超时和轮数;
        成员改绑或默认权限账号变了之后,原来的对话不再有效"""
        if not session_id:
            return None
        return await tx.one(
            "SELECT s.id,s.updated_at,now()-s.updated_at > make_interval(mins => $4::int) AS idle,"
            "(SELECT count(*) FROM kubedoor_ai_messages m WHERE m.session_id=s.id AND m.role='user' AND m.replaced_at IS NULL) AS rounds "
            "FROM kubedoor_ai_sessions s WHERE s.id=$1::uuid AND s.username=$2 AND s.channel='wecom' AND starts_with(s.channel_key,$3) "
            "AND s.checkpoint_purged_at IS NULL",
            session_id, identity.username, f"{userid}:", idle,
        )

    @staticmethod
    async def _active_run(tx, session_id):
        """对话里还在进行中的任务;approvable = 还有没过期的待审批操作"""
        return await tx.one(
            "SELECT r.id,r.status,EXISTS(SELECT 1 FROM kubedoor_ai_actions a WHERE a.run_id=r.id AND a.state='pending' "
            "AND a.expires_at>now()) AS approvable FROM kubedoor_ai_runs r WHERE r.session_id=$1::uuid "
            "AND r.status IN ('running','waiting_approval') ORDER BY r.created_at DESC LIMIT 1", session_id,
        )

    @staticmethod
    async def _open_session(tx, userid, identity, previous_id):
        """新建对话并设为当前对话,原来的对话记为上一个;旧对话保留不删"""
        session_id = str(uuid.uuid4())
        # channel_key 每个对话唯一;老对话的 channel_key 是 "{userid}:{username}"
        await tx.execute(
            "INSERT INTO kubedoor_ai_sessions(id,username,title,channel,channel_key) VALUES($1::uuid,$2,'企业微信会话','wecom',$3)",
            session_id, identity.username, f"{userid}:{identity.username}:{session_id}",
        )
        # 只记会话,不改 username:用默认权限的成员仍是未绑定,管理员同时改绑也不会被覆盖回去
        await tx.execute(
            "UPDATE kubedoor_ai_wecom_users SET session_id=$2::uuid,previous_session_id=$3::uuid,updated_at=now() WHERE wecom_userid=$1",
            userid, session_id, previous_id,
        )
        return session_id

    async def _current_session(self, userid, identity):
        """返回 (当前对话, 回复开头的提示, 进行中的任务)。空闲超时后自动开新对话;
        有进行中的任务或没过期的审批时不切换;只剩过期审批的任务先停掉,不让对话卡住"""
        notices = []
        async with self.service.store.transaction() as tx:
            member, idle, current = await self._session_state(tx, userid, identity)
            if current is None:
                return await self._open_session(tx, userid, identity, member.get("session_id")), notices, None
            run = await self._active_run(tx, current["id"])
            if run and run["status"] == "waiting_approval" and not run["approvable"]:
                # 等待审批的任务不在进程里运行,停止它只是几条更新,放在锁里免得并发消息重复停止
                await self.service.runtime.cancel(run, identity)
                notices.append("上一个待审批的操作已过期，已停止该任务。")
                run = None
            # 空对话(比如刚发过「新对话」)空闲再久也不用换,换了反而把真正的上一个对话挤掉
            if run is None and idle > 0 and current["idle"] and current["rounds"]:
                session_id = await self._open_session(tx, userid, identity, current["id"])
                notices.append(f"距上次对话已超过 {_idle_text(idle)}，已开始新对话（发送「继续上次」可回到之前的对话）")
                return session_id, notices, None
            return current["id"], notices, run

    async def _start_new_session(self, userid, identity):
        """「新对话」:开一个新对话,再停止原对话里进行中的任务"""
        async with self.service.store.transaction() as tx:
            member, _, current = await self._session_state(tx, userid, identity)
            run = await self._active_run(tx, current["id"]) if current else None
            # 当前对话还没有消息就直接用它,保留真正的上一个对话给「继续上次」
            if not (current and not current["rounds"] and run is None):
                await self._open_session(tx, userid, identity, member.get("session_id"))
        if run:
            # 停止运行中的任务可能要等 agent 侧取消,放在锁和事务外面,不挡这个成员的其他消息
            await self.service.runtime.cancel(run, identity)
        return "已开始新对话。" + ("上一个对话里未完成的任务已停止。" if run else "")

    async def _switch_identity(self, mapped):
        """切换对话只用本地记着的花折账号名,不调 master:切换不授予任何权限,下一次提问照常完整校验。
        返回 (只有账号名的身份, 不能使用时回给成员的提示)"""
        username = mapped.get("username") or (await self.unbound_setting())["unbound_username"]
        if not username:
            return None, "该企业微信成员尚未绑定花折账号，请联系管理员完成绑定。"
        return Identity(username, "none"), None

    async def _switch_command(self, userid, mapped, command):
        """「新对话」「继续上次」及别名"""
        identity, problem = await self._switch_identity(mapped)
        if problem:
            return problem
        if command in NEW_SESSION_COMMANDS:
            return await self._start_new_session(userid, identity)
        return await self._resume_previous_session(userid, identity)

    async def _resume_previous_session(self, userid, identity):
        """「继续上次」:和上一个对话互换;上一个对话必须属于同一个花折账号"""
        async with self.service.store.transaction() as tx:
            member, _, _ = await self._session_state(tx, userid, identity)
            previous = await self._own_session(tx, member.get("previous_session_id"), userid, identity)
            if previous is None:
                return "没有可以回到的对话。"
            await self._switch_to(tx, userid, previous["id"], member.get("session_id"))
        return f"已回到上一个对话（最后活跃：{_beijing_time(previous['updated_at'])}，共 {previous['rounds']} 轮）。"

    async def _continue_session(self, userid, identity, session_id):
        """欢迎卡片的「继续上次对话」:回到卡片上显示的那个对话(可能已被空闲规则切走)"""
        async with self.service.store.transaction() as tx:
            member, _, _ = await self._session_state(tx, userid, identity)
            target = await self._own_session(tx, session_id, userid, identity)
            if target is None:
                return "该对话已不可用，直接发送问题即可开始新对话。"
            await self._switch_to(tx, userid, target["id"], member.get("session_id"))
        return f"已回到上次的对话（共 {target['rounds']} 轮），直接发送问题即可继续。"

    @staticmethod
    async def _switch_to(tx, userid, session_id, current_id):
        """切回已有对话。它早已超过空闲超时,要刷新最后活跃时间,否则下一条消息又会被切到新对话"""
        await tx.execute("UPDATE kubedoor_ai_sessions SET updated_at=now() WHERE id=$1::uuid", session_id)
        if session_id != current_id:
            await tx.execute(
                "UPDATE kubedoor_ai_wecom_users SET session_id=$2::uuid,previous_session_id=$3::uuid,updated_at=now() WHERE wecom_userid=$1",
                userid, session_id, current_id,
            )

    async def _run_message(self, frame, userid, mapped, identity, env, text, stream_id, status=None):
        session_id, notices, active = await self._current_session(userid, identity)
        notice = "\n".join(notices)
        if active:
            hint = BUSY_HINT + ("发送「新对话」可以放弃它并重新开始。" if active["status"] == "waiting_approval" else "")
            await self._reply(frame, _with_notice(notice, hint), True, stream_id)
            return
        provider = Provider.parse(self._provider_settings())
        try:
            run, created = await self.service.new_run(
                identity, session_id, {"env": env, "namespace": None, "deployment": None, "pod": None},
                origin="wecom", key=None, channel=CHANNEL,
            )
        except AIError as exc:
            # 并发消息抢先建了任务:库里的唯一索引保证一个对话只有一个进行中的任务
            if exc.code != "run_active":
                raise
            created = False
        if not created:
            await self._reply(frame, _with_notice(notice, BUSY_HINT), True, stream_id)
            return
        clean = redact(text, (provider.api_key,))
        await self.service.store.message(run, "user", clean)
        self.service.runtime.start(run, identity, provider, clean)
        await self._watch_run(frame, userid, identity, run["id"], 0, stream_id=stream_id, env=env,
                              status=status or f"正在分析请求（集群：{env}）...", notice=notice)

    def _provider_settings(self):
        return {key: self.config.get(key) for key in PROVIDER_KEYS}

    async def _watch_run(self, frame, userid, identity, run_id, after, stream_id=None, env=None,
                         status="正在分析请求...", notice=""):
        stream_id = stream_id or generate_req_id("stream")
        content = status
        await self._reply(frame, _with_notice(notice, content), False, stream_id)
        cursor = after
        last_send = time.monotonic()
        idle = 0
        while True:
            rows = await self.service.store.fetch(
                "SELECT seq,type,data FROM kubedoor_ai_events WHERE run_id=$1::uuid AND seq>$2 ORDER BY seq LIMIT 200",
                run_id, cursor,
            )
            updated = False
            for event in rows:
                cursor = event["seq"]
                kind, data = event["type"], event["data"] or {}
                if kind == "token" and isinstance(data.get("content"), str):
                    content += data["content"]
                    updated = True
                elif kind == "approval":
                    # 卡片只放得下一行摘要：完整的管控状态、步骤、变化和差异写在卡片上方
                    await self._reply(frame, _with_notice(notice, (content + "\n\n" + _approval_text(data)).strip()), True, stream_id)
                    await self._send_approval(frame, userid, identity, run_id, data, cursor)
                    return
                elif kind == "message" and isinstance(data.get("content"), str):
                    content = data["content"]
                    updated = True
                elif kind == "done":
                    final = _with_cluster(content.strip() or "本轮操作已结束。", env, data.get("usage"))
                    await self._reply(frame, _with_notice(notice, final), True, stream_id)
                    return
            if updated and (time.monotonic() - last_send >= 0.7 or not rows):
                await self._reply(frame, _limit_stream(_with_notice(notice, content)), False, stream_id)
                last_send = time.monotonic()
            run = await self.service.store.one("SELECT status,event_seq,usage FROM kubedoor_ai_runs WHERE id=$1::uuid", run_id)
            if not run or run["status"] in ("completed", "failed", "cancelled") and cursor >= run["event_seq"]:
                final = _with_cluster(content.strip() or "本轮操作已结束。", env, (run or {}).get("usage"))
                await self._reply(frame, _with_notice(notice, final), True, stream_id)
                return
            idle += 1
            if idle > 1200:
                await self._reply(frame, "任务仍在处理中，稍后请发送“当前集群”确认状态。", True, stream_id)
                return
            await asyncio.sleep(0.35)

    async def _send_approval(self, frame, userid, identity, run_id, event, after):
        action_id = event.get("action_id")
        action = await self.service.store.one(
            "SELECT * FROM kubedoor_ai_actions WHERE id=$1::uuid AND run_id=$2::uuid AND state='pending'",
            action_id, run_id,
        )
        run = await self.service.store.one("SELECT session_id,scope FROM kubedoor_ai_runs WHERE id=$1::uuid", run_id)
        if not action or not run:
            await self._reply(frame, "审批信息已过期，请重新发起操作。")
            return
        task_id = secrets.token_urlsafe(18)
        await self.service.store.one(
            "INSERT INTO kubedoor_ai_wecom_approvals(task_id,action_id,run_id,wecom_userid,username,session_id,resume_after_seq,expires_at) "
            "VALUES($1,$2::uuid,$3::uuid,$4,$5,$6::uuid,$7,$8) ON CONFLICT(action_id) DO UPDATE SET task_id=EXCLUDED.task_id "
            "RETURNING task_id", task_id, action_id, run_id, userid, identity.username, run["session_id"], after, action["expires_at"],
        )
        env = run["scope"]["env"]
        preview = action.get("preview") or {}
        detail = {"name": action["operation"], "env": env, "arguments": action.get("arguments"), "preview": preview,
                  "command": preview.get("command"), "diff": preview.get("diff")}
        name = OPERATION_NAMES.get(action["operation"], action["operation"])
        card = {"card_type": "button_interaction", "main_title": {"title": "K8S 操作待审批", "desc": _clip(f"{name} / {env}", 40)},
                "sub_title_text": _approval_summary(detail) or "请核对操作内容后决定。",
                "button_list": [{"text": "批准", "key": "approve", "style": 1},
                                {"text": "拒绝", "key": "reject", "style": 2}],
                "task_id": task_id}
        await self.client.reply_template_card(frame, card)

    async def _on_card_event(self, frame):
        self._spawn(self._handle_card_event(frame), "wecom-approval")

    async def _on_enter_chat(self, frame):
        self._spawn(self._handle_enter_chat(frame), "wecom-welcome")

    async def _handle_enter_chat(self, frame):
        """成员当天首次进入单聊:5 秒内回欢迎卡片。只读本地数据库、不调用 master,权限在提问时再校验"""
        body = frame.get("body") or {}
        userid = ((body.get("from") or {}).get("userid"))
        if not isinstance(userid, str) or not userid or body.get("chattype", "single") != "single":
            return
        if not await self._claim_message(body, userid, "enter_chat"):
            return
        try:
            await self.client.reply_welcome(frame, await self._welcome(userid))
        except Exception as exc:
            LOGGER.warning("企微欢迎语发送失败: %s", type(exc).__name__)

    async def _welcome(self, userid):
        mapped = await self._mapped_user(userid)
        setting = await self.unbound_setting()
        username = mapped.get("username") or setting["unbound_username"]
        if not username:
            return {"msgtype": "text", "text": {"content": "你好，我是花折 AI 助手。该企业微信成员尚未绑定花折账号，请联系管理员完成绑定后再提问。"}}
        env = mapped.get("current_env") or (mapped.get("default_env") if mapped.get("username") else setting["unbound_default_env"])
        last = await self._own_session(self.service.store, mapped.get("session_id"), userid, Identity(username, "none"))
        last = last if last and last["rounds"] else None
        buttons = [{"text": "开始新对话", "key": "session_new"}]
        if last:
            buttons.append({"text": "继续上次对话", "key": "session_resume"})
        card = {"card_type": "button_interaction",
                "main_title": {"title": "花折 AI 助手", "desc": f"当前集群：{env or '未选择'}"},
                "sub_title_text": f"上次对话：{_friendly_time(last['updated_at'])}，共 {last['rounds']} 轮" if last else "直接发送问题即可开始对话。",
                "button_list": buttons,
                # 「继续上次对话」回到卡片上显示的这个对话,按钮事件从 task_id 取回它
                "task_id": f"session_{last['id'] if last else 'none'}_{secrets.token_hex(6)}"}
        return {"msgtype": "template_card", "template_card": card}

    async def _handle_session_card(self, frame, body, userid, task_id, choice):
        """欢迎卡片的「开始新对话」「继续上次对话」按钮,处理完把卡片换成文本通知(企微要求 5 秒内)"""
        if not await self._claim_message(body, userid, "session"):
            return
        identity, problem = await self._switch_identity(await self._mapped_user(userid))
        if problem:
            title, detail = "无法切换对话", problem
        elif choice == "session_new":
            title, detail = "已开始新对话", await self._start_new_session(userid, identity)
        else:
            title, detail = "继续上次对话", await self._continue_session(userid, identity, _card_session(task_id))
        card = {"card_type": "text_notice", "main_title": {"title": title}, "sub_title_text": detail, "task_id": task_id}
        with suppress(Exception):
            await self.client.update_template_card(frame, card)

    async def _handle_card_event(self, frame):
        body = frame.get("body") or {}
        event = body.get("event") or {}
        userid = ((body.get("from") or {}).get("userid"))
        task_id, choice = event.get("task_id"), event.get("event_key")
        if isinstance(userid, str) and choice in ("session_new", "session_resume"):
            await self._handle_session_card(frame, body, userid, task_id, choice)
            return
        if not isinstance(userid, str) or choice not in ("approve", "reject"):
            return
        if not await self._claim_message(body, userid, "approval"):
            await self._reply(frame, "该审批事件已处理。")
            return
        approval = await self.service.store.one(
            "SELECT * FROM kubedoor_ai_wecom_approvals WHERE task_id=$1 AND expires_at>now()", task_id,
        ) if isinstance(task_id, str) else None
        if not approval or approval["wecom_userid"] != userid:
            await self._reply(frame, "审批卡片已过期或不属于当前账号。")
            return
        mapped = await self._mapped_user(userid)
        if (mapped.get("username") or (await self.unbound_setting())["unbound_username"]) != approval["username"]:
            await self._reply(frame, "账号绑定已变更，该审批不能继续。")
            return
        try:
            identity = await self._identity(approval["username"])
            env = (await self.service.store.one(
                "SELECT scope->>'env' AS env FROM kubedoor_ai_runs WHERE id=$1::uuid", approval["run_id"],
            ) or {}).get("env")
            if not identity.can_read(env):
                await self._reply(frame, "该账号已失去目标集群权限，不能批准此操作。")
                return
            provider = Provider.parse(self._provider_settings())
            result = await self.service.decision(identity, approval["run_id"], {
                "action_id": str(approval["action_id"]), "decision": choice, "provider": provider.__dict__,
            })
            await self.service.store.execute("DELETE FROM kubedoor_ai_wecom_approvals WHERE task_id=$1", task_id)
            card = {"card_type": "text_notice", "main_title": {"title": "已批准，正在执行" if choice == "approve" else "已拒绝"}, "task_id": task_id}
            with suppress(Exception):
                await self.client.update_template_card(frame, card)
            if result.get("status") == "running":
                self._spawn(self._watch_run(frame, userid, identity, approval["run_id"], approval["resume_after_seq"],
                                            env=env), "wecom-run-resume")
        except AIError as exc:
            await self._reply(frame, str(exc))
        except Exception as exc:
            LOGGER.warning("企微审批处理失败: %s", type(exc).__name__)
            await self._reply(frame, "审批处理失败，请在花折 AI 会话中核验当前状态。")

    async def admin_config(self):
        row = await self.service.store.one("SELECT enabled,encrypted_config,session_idle_minutes,updated_by,updated_at FROM kubedoor_ai_wecom_config WHERE singleton=true")
        if not row:
            return {**self.status, "has_bot_secret": False, "has_api_key": False, "session_idle_minutes": DEFAULT_IDLE_MINUTES}
        config = self.service.gateway.cipher.decrypt("wecom", bytes(row["encrypted_config"]))
        return {**self.status, "enabled": row["enabled"], "bot_id": config.get("bot_id", ""),
                "base_url": config.get("base_url", ""), "model": config.get("model", ""),
                "max_input_tokens": config.get("max_input_tokens"), "max_output_tokens": config.get("max_output_tokens"),
                "compression_ratio": config.get("compression_ratio"), "reasoning_effort": config.get("reasoning_effort"),
                "session_idle_minutes": row.get("session_idle_minutes", DEFAULT_IDLE_MINUTES),
                "has_bot_secret": bool(config.get("bot_secret")), "has_api_key": bool(config.get("api_key")),
                "updated_by": row["updated_by"], "updated_at": row["updated_at"]}

    async def save_config(self, identity, raw):
        if not isinstance(raw, dict):
            raise AIError("配置格式无效")
        current = self.config or {}
        config = {"bot_id": raw.get("bot_id", current.get("bot_id", "")),
                  "bot_secret": raw.get("bot_secret") or current.get("bot_secret", ""),
                  "base_url": raw.get("base_url", current.get("base_url", "")),
                  "api_key": raw.get("api_key") or current.get("api_key", ""),
                  "model": raw.get("model", current.get("model", "")),
                  "enabled": raw.get("enabled") is True}
        # 上下文窗口、最大输出、压缩阈值、思考强度:不传沿用已保存的值,null 表示按模型推断 / 用模型默认
        for key in ("max_input_tokens", "max_output_tokens", "compression_ratio", "reasoning_effort"):
            config[key] = raw.get(key, current.get(key))
        parse_model_limits(config)
        parse_reasoning_effort(config)
        idle = raw.get("session_idle_minutes")
        if idle is not None and (type(idle) is not int or not 0 <= idle <= 10080):
            raise AIError("对话空闲超时必须为 0–10080 之间的整数（分钟），0 表示关闭")
        enabled = config["enabled"]
        if enabled:
            if not config["bot_id"] or not config["bot_secret"]:
                raise AIError("启用机器人需要 Bot ID 和 Secret")
            Provider.parse({key: config[key] for key in PROVIDER_KEYS})
        encrypted = self.service.gateway.cipher.encrypt("wecom", config)
        row = await self.service.store.one(
            "INSERT INTO kubedoor_ai_wecom_config(singleton,enabled,encrypted_config,updated_by,session_idle_minutes) "
            "VALUES(true,$1,$2,$3,COALESCE($4::int,$5::int)) "
            "ON CONFLICT(singleton) DO UPDATE SET enabled=EXCLUDED.enabled,encrypted_config=EXCLUDED.encrypted_config,updated_by=EXCLUDED.updated_by,"
            "session_idle_minutes=COALESCE($4::int,kubedoor_ai_wecom_config.session_idle_minutes),updated_at=now() "
            "RETURNING enabled,encrypted_config", enabled, encrypted, identity.username, idle, DEFAULT_IDLE_MINUTES,
        )
        saved = self.service.gateway.cipher.decrypt("wecom", bytes(row["encrypted_config"]))
        saved["enabled"] = row["enabled"]
        await self.configure(saved)
        return await self.admin_config()

    async def users(self):
        return await self.service.store.fetch(
            "SELECT u.wecom_userid,u.username,u.default_env,u.current_env,u.first_seen_at,u.last_seen_at,u.updated_at, "
            "(SELECT count(*) FROM kubedoor_ai_wecom_approvals a WHERE a.wecom_userid=u.wecom_userid AND a.expires_at>now()) AS pending_approvals "
            "FROM kubedoor_ai_wecom_users u ORDER BY u.last_seen_at DESC"
        )

    async def save_user(self, identity, raw):
        userid, username = raw.get("wecom_userid"), raw.get("username")
        if not isinstance(userid, str) or not userid.strip() or len(userid) > 256:
            raise AIError("企业微信 UserID 无效")
        if not isinstance(username, str) or not username.strip():
            raise AIError("请选择花折用户")
        username = username.strip()
        live = await self._identity(username)
        visible_clusters = (await self.service.bootstrap(live))["clusters"]
        if not visible_clusters:
            raise AIError("该花折用户没有任何 K8S 访问权限", 403, "permission_denied")
        env = raw.get("default_env")
        if env is not None and (not isinstance(env, str) or env not in {item["env"] for item in visible_clusters}):
            raise AIError("默认集群不存在或用户无权访问", 403, "permission_denied")
        await self.service.store.execute(
            "INSERT INTO kubedoor_ai_wecom_users(wecom_userid,username,default_env,current_env) VALUES($1,$2,$3,$3) "
            "ON CONFLICT(wecom_userid) DO UPDATE SET username=EXCLUDED.username,default_env=EXCLUDED.default_env, "
            "current_env=CASE WHEN kubedoor_ai_wecom_users.current_env IS NULL THEN EXCLUDED.current_env ELSE kubedoor_ai_wecom_users.current_env END,updated_at=now()",
            userid.strip(), username, env,
        )
        return {"success": True}

    async def save_unbound(self, identity, raw):
        """保存未绑定成员的默认权限账号和默认集群(集群为空 = 「无」,成员要先选集群才能提问)"""
        username, env = raw.get("username"), raw.get("default_env")
        if username is not None and not isinstance(username, str):
            raise AIError("请选择花折账号")
        if env is not None and not isinstance(env, str):
            raise AIError("默认集群无效")
        username = (username or "").strip() or None
        env = ((env or "").strip() or None) if username else None
        if username:
            live = await self._identity(username)
            if live.is_admin:
                raise AIError("不能用管理员账号作为未绑定成员的默认权限")
            visible = {item["env"] for item in (await self.service.bootstrap(live))["clusters"]}
            if not visible:
                raise AIError("该花折账号没有任何 K8S 集群访问权限", 403, "permission_denied")
            if env is not None and env not in visible:
                raise AIError("默认集群不存在或该账号无权访问", 403, "permission_denied")
        # 还没保存过机器人配置时先插入一行空配置(不启用),机器人配置照常在「保存配置」里填
        await self.service.store.execute(
            "INSERT INTO kubedoor_ai_wecom_config(singleton,enabled,encrypted_config,unbound_username,unbound_default_env,updated_by) "
            "VALUES(true,false,$1,$2,$3,$4) ON CONFLICT(singleton) DO UPDATE SET unbound_username=EXCLUDED.unbound_username,"
            "unbound_default_env=EXCLUDED.unbound_default_env,updated_by=EXCLUDED.updated_by,updated_at=now()",
            self.service.gateway.cipher.encrypt("wecom", {}), username, env, identity.username,
        )
        return {"unbound_username": username, "unbound_default_env": env}

    async def delete_user(self, userid):
        await self.service.store.execute(
            "UPDATE kubedoor_ai_wecom_users SET username=NULL,default_env=NULL,current_env=NULL,session_id=NULL,"
            "previous_session_id=NULL,updated_at=now() WHERE wecom_userid=$1",
            userid,
        )


def _clip(value, limit):
    return value if len(value) <= limit else value[:limit - 1] + "…"


# 审批说明和卡片里的操作中文名，没列出的显示原名
OPERATION_NAMES = {
    "scale_deployment": "扩缩容", "restart_deployment": "滚动重启", "update_deployment_image": "更新镜像",
    "update_deployment_resources": "调整 requests/limits", "update_deployment_jvm": "调整 JVM 参数",
    "resource_config_update": "修改管控配置", "resource_pod_count": "修改管控副本数", "resource_control_add": "登记到资源管控",
    "schedule_deployment_scale": "定时扩缩容", "cron_deployment_scale": "周期扩缩容",
    "schedule_deployment_restart": "定时重启", "cron_deployment_restart": "周期重启",
    "delete_pod": "删除 Pod", "delete_pods": "批量删除 Pod", "isolate_pod": "隔离 Pod",
    "resource_apply": "应用 YAML", "resource_delete": "删除资源",
    "kubectl": "kubectl 命令", "api": "K8S API 请求", "istioctl": "istioctl 命令", "pod_exec": "Pod 内执行命令",
}
# 审批说明里命令 / 差异最多展示的字数，完整内容在网页的审批记录里
APPROVAL_DETAIL_LIMIT = 1500


def _approval_target(event):
    arguments = event.get("arguments") if isinstance(event.get("arguments"), dict) else {}
    namespace = arguments.get("namespace")
    name = next((arguments[key] for key in ("deployment", "pod", "statefulset", "daemonset", "resource_name") if arguments.get(key)), None)
    return f"{namespace}/{name}" if namespace and name else str(name or namespace or "")


def _approval_plan(event):
    preview = event.get("preview") if isinstance(event.get("preview"), dict) else {}
    plan = preview.get("plan")
    return plan if isinstance(plan, dict) and isinstance(plan.get("steps"), list) else None


def _control_label(plan):
    control = plan.get("control") or {}
    if control.get("blocked"):
        return "未登记管控，修改会被拒绝"
    return "受花折资源管控" if control.get("controlled") else "不受花折资源管控"


def _approval_detail(event):
    """命令、差异或参数原文：没有执行计划的操作靠它让成员看清要执行什么"""
    plan = _approval_plan(event)
    if plan:
        return "\n".join(step["diff"] for step in plan["steps"] if isinstance(step, dict) and step.get("diff"))
    preview = event.get("preview") if isinstance(event.get("preview"), dict) else {}
    detail = event.get("command") or preview.get("command") or event.get("diff") or preview.get("diff")
    if isinstance(detail, list):
        detail = " ".join(map(str, detail))
    return str(detail) if detail else json.dumps(event.get("arguments") or {}, ensure_ascii=False, indent=2)


def _approval_summary(event):
    """卡片副标题，一行：目标、管控状态和主要变化；没有计划时是命令或参数"""
    plan = _approval_plan(event)
    if not plan:
        return _clip(_approval_detail(event).replace("\n", " "), 140)
    changes = [f"{c.get('label')} {c.get('current')}→{c.get('target')}" for c in plan.get("changes") or [] if isinstance(c, dict)]
    return _clip(" · ".join(part for part in (_approval_target(event), _control_label(plan), *changes[:3]) if part), 140)


def _approval_text(event):
    """卡片上方的完整说明（Markdown）：卡片放不下的管控状态、步骤、变化、影响和差异"""
    name = OPERATION_NAMES.get(event.get("name"), event.get("name") or "操作")
    lines = [f"**待审批：{name}**（集群 {event.get('env') or '-'}）"]
    target = _approval_target(event)
    if target:
        lines.append(f"目标：{target}")
    plan = _approval_plan(event)
    if plan:
        message = (plan.get("control") or {}).get("message")
        lines.append(f"管控：{_control_label(plan)}" + (f"（{message}）" if message else ""))
        steps = [step for step in plan["steps"] if isinstance(step, dict)]
        if steps:
            lines += ["步骤："] + [f"{index}. {step.get('name')}" for index, step in enumerate(steps, 1)]
        changes = [c for c in plan.get("changes") or [] if isinstance(c, dict)]
        if changes:
            lines += ["变化："] + [f"- {c.get('scope')} {c.get('label')}：{c.get('current')} → {c.get('target')}" for c in changes]
        for title, items, mark in (("影响：", plan.get("effects"), "-"), ("注意：", plan.get("warnings"), "- ⚠️")):
            items = [item for item in items or [] if isinstance(item, str)]
            if items:
                lines += [title] + [f"{mark} {item}" for item in items]
    detail = _approval_detail(event)
    if detail:
        lines.append("```\n" + _clip(detail, APPROVAL_DETAIL_LIMIT).replace("```", "'''") + "\n```")
    lines.append("请核对后在下方卡片批准或拒绝。")
    return "\n".join(lines)


def _retention_days():
    """kubedoor-config 里的 RETENTION_AI_CHECKPOINT_DAYS;0 = 不清理,填错按默认值"""
    try:
        return max(0, int(os.getenv("RETENTION_AI_CHECKPOINT_DAYS", DEFAULT_RETENTION_DAYS)))
    except ValueError:
        return DEFAULT_RETENTION_DAYS


def _pick_cluster(target, names):
    """按序号、完整名称(不区分大小写)或唯一的部分名称找集群,返回 (集群, 有歧义时的候选)"""
    if target.isdigit():
        index = int(target)
        return (names[index - 1] if 1 <= index <= len(names) else None), []
    lowered = target.lower()
    for name in names:
        if name.lower() == lowered:
            return name, []
    partial = [name for name in names if lowered in name.lower()]
    return (partial[0], []) if len(partial) == 1 else (None, partial)


def _mentioned_cluster(text, names):
    """提问里写出了完整名称、且只写了一个的集群(集群名只含字母、数字、- 和 _,前后不能紧挨这些字符)"""
    found = {name for name in names
             if re.search(rf"(?<![A-Za-z0-9_.-]){re.escape(name)}(?![A-Za-z0-9_.-])", text, re.IGNORECASE)}
    return found.pop() if len(found) == 1 else None


def _cluster_menu(clusters, current):
    lines = [f"当前集群：{current or '未选择'}", "可访问的集群："]
    for index, item in enumerate(clusters, 1):
        tags = [PERMISSION_LABELS.get(item.get("permission"), item.get("permission"))]
        if item.get("online") is False:
            tags.append("agent 离线")
        mark = "（当前）" if item["env"] == current else ""
        lines.append(f"{index}. {item['env']} · {' · '.join(tags)}{mark}")
    lines.append("发送「切换集群 序号或名称」切换，如「切换集群 2」；提问时写出集群全名也会自动切换过去。")
    lines.append("发送「新对话」重新开始，「继续上次」回到上一个对话。")
    return "\n".join(lines)


def _with_notice(notice, content):
    """会话切换等提示放在本次回复的最前面"""
    return f"{notice}\n\n{content}" if notice else content


def _idle_text(minutes):
    return f"{minutes // 60} 小时" if minutes % 60 == 0 else f"{minutes} 分钟"


def _beijing_time(value):
    moment = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    return moment.astimezone(BEIJING_TZ).strftime("%m-%d %H:%M")


def _friendly_time(value):
    """今天 18:20 / 昨天 18:20 / 10-23 18:20(北京时间)"""
    moment = (value if isinstance(value, datetime) else datetime.fromisoformat(value)).astimezone(BEIJING_TZ)
    days = (datetime.now(BEIJING_TZ).date() - moment.date()).days
    day = {0: "今天", 1: "昨天"}.get(days) or moment.strftime("%m-%d")
    return f"{day} {moment:%H:%M}"


def _card_session(task_id):
    """欢迎卡片 task_id 里记着的对话:session_<对话 ID>_<随机串>"""
    parts = task_id.split("_") if isinstance(task_id, str) else []
    return parts[1] if len(parts) == 3 and parts[0] == "session" and parts[1] != "none" else None


def _with_cluster(content, env, usage=None):
    if not env:
        return content
    # 上下文较大时提示占用比例:越长越慢、越贵,企微里只能靠「新对话」重新开始
    usage = usage if isinstance(usage, dict) else {}
    used, window = usage.get("last_input_tokens"), usage.get("max_input_tokens")
    if isinstance(used, int) and isinstance(window, int) and window > 0 and used / window >= CONTEXT_HINT_RATIO:
        return f"{content}\n\n—— 集群：{env} · 上下文 {int(used * 100 / window)}%（发送「新对话」可重新开始）"
    return f"{content}\n\n—— 集群：{env}（发送「集群」查看或切换）"


def _limit_stream(value):
    data = value.encode("utf-8")
    if len(data) <= MAX_STREAM_BYTES:
        return value
    return data[:MAX_STREAM_BYTES - 40].decode("utf-8", "ignore") + "\n\n（内容过长，已截断）"
