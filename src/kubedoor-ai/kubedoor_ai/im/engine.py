"""Platform-agnostic ChatOps engine serving any IMAdapter."""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
import unicodedata
import uuid
from contextlib import suppress
from types import MappingProxyType
from typing import Any

from ..domain import AIError, CLUSTER_PERMISSIONS, Identity, Provider, ROLES, redact
from ..runtime import escape_turn_context
from ..wecom import (
    APPROVAL_HINT,
    APPROVAL_WORDS,
    BIND_CODE_MINUTES,
    BIND_COMMAND,
    BIND_MAX_FAILURES,
    BIND_MAX_TOTAL_FAILURES,
    BUSY_HINT,
    CARD_MASTER_SECONDS,
    DEFAULT_IDLE_MINUTES,
    MENU_COMMANDS,
    NEW_SESSION_COMMANDS,
    OPERATION_NAMES,
    PENDING_MINUTES,
    PURGE_BATCH,
    RESUME_COMMANDS,
    SELF_NAME_COMMAND,
    SWITCH_COMMAND,
    SWITCH_FILLER,
    _approval_summary,
    _approval_text,
    _beijing_time,
    _brief,
    _card_session,
    _clip,
    _cluster_menu,
    _cluster_spans,
    _friendly_time,
    _idle_text,
    _mentioned_cluster,
    _pick_cluster,
    _usable,
    _with_cluster,
    _with_notice,
)
from .adapter import IMAdapter, StreamHandle
from .models import ApprovalPrompt, BotConfig, CardActionEvent, InboundEvent

LOGGER = logging.getLogger(__name__)

# Deterministic UUID reserved for the bot migrated from legacy kubedoor_ai_wecom_config.
LEGACY_WECOM_BOT_ID = "00000000-0000-4000-8000-000000000001"
UNBOUND_HINT = (
    "当前 IM 成员尚未绑定花折账号。请登录花折 Web，在右上角用户菜单「绑定 IM 账号」获取绑定码，"
    "再把「绑定 绑定码」发给我；也可以发送「我是 你的名字」后请管理员绑定。"
)
HELP_TEXT = (
    "可用命令：\n"
    "- 集群 / 帮助：查看可访问集群与帮助\n"
    "- 切换集群 <序号|名称|别名>：切换当前对话集群\n"
    "- 新对话 / 继续上次：开启新对话或回到上一个对话\n"
    "- 绑定 <6位绑定码>：绑定你的花折账号\n"
    "- 我是 <名字>：登记名字以便管理员识别绑定\n"
    "- 批准 / 拒绝：审批当前待确认的高危操作"
)
HELP_TRIGGERS = frozenset({"帮助", "help", "?", "？"})
DEAD_IM_SESSION = (
    "s.channel IN ('wecom', 'im', 'im:wecom', 'im:feishu', 'im:dingtalk', 'im:slack') "
    "AND s.checkpoint_purged_at IS NULL "
    "AND s.updated_at < now() - make_interval(days => $1::int) "
    "AND NOT EXISTS (SELECT 1 FROM kubedoor_ai_im_users u WHERE u.session_id=s.id OR u.previous_session_id=s.id) "
    "AND NOT EXISTS (SELECT 1 FROM kubedoor_ai_wecom_users wu WHERE wu.session_id=s.id OR wu.previous_session_id=s.id) "
    "AND NOT EXISTS (SELECT 1 FROM kubedoor_ai_runs r WHERE r.session_id=s.id AND r.status IN ('running','waiting_approval'))"
)


class IMBotEngine:
    """Shared ChatOps state machine decoupled from IM wire protocols."""

    def __init__(
        self,
        service: Any,
        bot: BotConfig,
        adapter: IMAdapter,
        shared_model: Any = None,
    ) -> None:
        self.service = service
        self.bot = bot
        self.adapter = adapter
        self.shared_model = shared_model
        self._tasks: set[asyncio.Task[Any]] = set()

    @property
    def session_channel(self) -> str:
        return "wecom" if self.bot.platform == "wecom" else f"im:{self.bot.platform}"

    @property
    def lock_prefix(self) -> str:
        return f"im:{self.bot.id}"

    def _spawn(self, coroutine: Any, name: str) -> asyncio.Task[Any]:
        task = asyncio.create_task(coroutine, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    def resolve_provider(self) -> Provider:
        override = self.bot.model_override
        if isinstance(override, dict) and (override.get("base_url") or override.get("model")):
            return Provider.parse(override)
        if hasattr(self.service, "provider"):
            return self.service.provider({"source": "shared"})
        if self.shared_model is not None:
            return Provider.parse(self.shared_model.settings())
        raise AIError("管理员还没有配置公共默认模型，请在「模型设置」里改用自定义模型", 409, "shared_model_unavailable")

    def _group_allowed(self, event: InboundEvent) -> bool:
        if event.chat_type != "group":
            return True
        policy = self.bot.group_policy or "disabled"
        if policy == "disabled":
            return False
        if policy == "mention":
            return bool(event.mentioned_bot)
        if policy == "allowlist":
            return bool(event.mentioned_bot) and event.chat_id in set(self.bot.allowed_chat_ids or ())
        return False

    async def _claim_message(self, msgid: str, userid: str, kind: str) -> bool:
        if not isinstance(msgid, str) or not msgid:
            return True
        row = await self.service.store.one(
            "INSERT INTO kubedoor_ai_im_inbox(bot_id,msgid,external_user_id,kind) VALUES($1::uuid,$2,$3,$4) "
            "ON CONFLICT(bot_id,msgid) DO NOTHING RETURNING msgid",
            self.bot.id,
            msgid,
            userid,
            kind,
        )
        return bool(row)

    async def _mapped_user(self, userid: str) -> dict[str, Any]:
        await self.service.store.execute(
            "INSERT INTO kubedoor_ai_im_users(bot_id,external_user_id,platform) VALUES($1::uuid,$2,$3) "
            "ON CONFLICT(bot_id,external_user_id) DO UPDATE SET last_seen_at=now()",
            self.bot.id,
            userid,
            self.bot.platform,
        )
        return await self.service.store.one(
            "SELECT * FROM kubedoor_ai_im_users WHERE bot_id=$1::uuid AND external_user_id=$2",
            self.bot.id,
            userid,
        ) or {"bot_id": self.bot.id, "external_user_id": userid, "platform": self.bot.platform}

    async def _identity(self, username: str) -> Identity:
        hint = Identity(username, "none")
        try:
            async with asyncio.timeout(10):
                raw = await self.service.gateway.master(hint, "GET", "/api/ai/internal/identity")
        except AIError as exc:
            if exc.status == 403:
                raise AIError(f"花折账号 {username} 不存在、已停用或不是 Web 账号", 403, "identity_invalid") from None
            raise
        permission = raw.get("permission")
        clusters = raw.get("clusters")
        if (
            raw.get("username") != username
            or permission not in ROLES
            or not isinstance(clusters, dict)
            or any(not isinstance(k, str) or v not in CLUSTER_PERMISSIONS for k, v in clusters.items())
        ):
            raise AIError("花折用户身份无效", 403, "identity_invalid")
        return Identity(username, permission, MappingProxyType({} if permission == "admin" else clusters))

    def _unbound_setting(self) -> dict[str, str | None]:
        return {
            "unbound_username": self.bot.unbound_username,
            "unbound_default_env": self.bot.unbound_default_env,
        }

    async def _member_identity(self, mapped: dict[str, Any]) -> tuple[Identity | None, str | None, str | None]:
        if mapped.get("username"):
            try:
                return await self._identity(mapped["username"]), mapped.get("default_env"), None
            except AIError:
                return None, None, "绑定的花折账号不存在、已停用或不可用，请联系管理员。"
        setting = self._unbound_setting()
        if not setting["unbound_username"]:
            return None, None, UNBOUND_HINT
        try:
            identity = await self._identity(setting["unbound_username"])
        except AIError:
            identity = None
        if identity is None or identity.permission in ("admin", "cluster_admin"):
            return None, None, "未绑定成员的默认权限当前不可用，请联系管理员绑定花折账号。"
        return identity, setting["unbound_default_env"], None

    def _default_env(self, mapped: dict[str, Any]) -> str | None:
        if mapped.get("username"):
            return mapped.get("default_env")
        return self.bot.unbound_default_env

    async def _member_clusters(self, identity: Identity) -> list[dict[str, Any]]:
        return [
            item
            for item in (await self.service.bootstrap(identity))["clusters"]
            if item.get("permission") in ("admin", "rw", "read")
        ]

    async def _quick_clusters(self, mapped: dict[str, Any]) -> list[dict[str, Any]] | None:
        try:
            async with asyncio.timeout(CARD_MASTER_SECONDS):
                identity, _, problem = await self._member_identity(mapped)
                return None if problem or identity is None else await self._member_clusters(identity)
        except Exception as exc:
            LOGGER.info("IM 卡片没有带上集群列表: %s", type(exc).__name__)
            return None

    async def handle_message(self, event: InboundEvent) -> None:
        userid = event.external_user_id
        if not isinstance(userid, str) or not userid:
            return
        if not self._group_allowed(event):
            return
        if not await self._claim_message(event.msgid, userid, event.kind):
            return
        if event.kind == "enter_chat":
            await self._handle_enter_chat(event)
            return

        text = escape_turn_context((event.text or "").strip())
        reply = self.adapter.open_reply_stream(event)
        if not text:
            await reply.finish("暂不支持空消息。发送「帮助」查看可用命令。")
            return
        mapped = await self._mapped_user(userid)
        finished = False
        try:
            await reply.update("正在校验账号与访问权限...")
            await self._dispatch_text(reply, userid, mapped, text)
            finished = True
        except Exception as exc:
            LOGGER.warning("IM 消息处理失败 [%s]: %s: %s", self.bot.platform, type(exc).__name__, _brief(str(exc))[:160])
            with suppress(Exception):
                await reply.finish("处理失败，请检查花折 AI 服务状态后重试。")
                finished = True
        finally:
            if not finished and hasattr(reply, "close"):
                with suppress(Exception):
                    await reply.close()

    async def _handle_enter_chat(self, event: InboundEvent) -> None:
        if event.chat_type != "single":
            return
        welcome_fn = getattr(self.adapter, "send_welcome", None)
        if not welcome_fn:
            return
        payload = await self.build_welcome(event.external_user_id)
        try:
            await welcome_fn(event, payload)
        except Exception as exc:
            LOGGER.warning("IM 欢迎语发送失败 [%s]: %s", self.bot.platform, type(exc).__name__)

    async def build_welcome(self, userid: str) -> dict[str, Any]:
        mapped = await self._mapped_user(userid)
        username = mapped.get("username") or self.bot.unbound_username
        if not username:
            return {"kind": "text", "text": "你好，我是花折 AI 助手。" + UNBOUND_HINT}
        default_env = mapped.get("default_env") if mapped.get("username") else self.bot.unbound_default_env
        last = await self._own_session(self.service.store, mapped.get("session_id"), userid, Identity(username, "none"))
        last = last if last and last["rounds"] else None
        clusters = await self._quick_clusters(mapped)
        suggestion = (_usable(default_env, clusters) or mapped.get("current_env")) if clusters else None
        return {
            "kind": "welcome_card",
            "default_env": default_env,
            "last": last,
            "last_time_text": _friendly_time(last["updated_at"]) if last else None,
            "clusters": clusters,
            "suggestion": suggestion,
            "task_id": f"session_{last['id'] if last else 'none'}_{secrets.token_hex(6)}",
        }

    async def _dispatch_text(self, reply: StreamHandle, userid: str, mapped: dict[str, Any], text: str) -> None:
        command = text.lower()
        bind = BIND_COMMAND.match(unicodedata.normalize("NFKC", text))
        if bind:
            await reply.finish(await self._bind_by_code(userid, bind.group(1)))
            return
        named = SELF_NAME_COMMAND.match(text)
        if named:
            safe_name = escape_turn_context(named.group(1))
            await reply.finish(await self._save_self_name(userid, safe_name))
            return
        if command in NEW_SESSION_COMMANDS | RESUME_COMMANDS:
            await self._switch_command(reply, userid, mapped, command)
            return
        decision = APPROVAL_WORDS.get(command.rstrip("。.!！~～"))
        if decision and await self._text_decision(reply, userid, mapped, decision):
            return

        identity, default_env, problem = await self._member_identity(mapped)
        if problem or identity is None:
            if command in HELP_TRIGGERS:
                await reply.finish(HELP_TEXT + "\n\n" + (problem or UNBOUND_HINT))
                return
            await reply.finish(problem or UNBOUND_HINT)
            return

        clusters = await self._member_clusters(identity)
        if not clusters:
            await reply.finish("当前花折账号没有任何 K8S 集群访问权限。")
            return
        if command in MENU_COMMANDS:
            notice, current = await self._settled_cluster(userid, identity, mapped, default_env, clusters)
            body = _cluster_menu(clusters, current)
            if command in HELP_TRIGGERS:
                body = HELP_TEXT + "\n\n" + body
            menu_text = _with_notice(notice, body)
            await reply.send_cluster_menu(current, clusters, menu_text, prompt_text=menu_text)
            return
        switch = SWITCH_COMMAND.match(text)
        if switch and await self._switch_cluster(reply, userid, mapped, identity, default_env, clusters, switch):
            return
        await self._ask(reply, userid, mapped, identity, default_env, clusters, text)

    async def _settled_cluster(
        self,
        userid: str,
        identity: Identity,
        mapped: dict[str, Any],
        default_env: str | None,
        clusters: list[dict[str, Any]],
    ) -> tuple[str, str | None]:
        _, notices, _, reset = await self._current_session(userid, identity)
        return "\n".join(notices), _usable(default_env if reset else mapped.get("current_env") or default_env, clusters)

    async def _switch_cluster(
        self,
        reply: StreamHandle,
        userid: str,
        mapped: dict[str, Any],
        identity: Identity,
        default_env: str | None,
        clusters: list[dict[str, Any]],
        match: Any,
    ) -> bool:
        target = match.group(2).strip()
        env, candidates = _pick_cluster(target, clusters)
        if not env and not candidates:
            spans = _cluster_spans(target, clusters)
            found = {span[2] for span in spans}
            if len(found) == 1:
                covered = [False] * len(target)
                for start, end, _ in spans:
                    covered[start:end] = [True] * (end - start)
                if SWITCH_FILLER.sub("", "".join(ch for ch, hit in zip(target, covered) if not hit)):
                    return False
                env = found.pop()
        if env:
            _, notices, _, _ = await self._current_session(userid, identity)
            await self._set_current_env(userid, env)
            pending = await self._take_pending(userid)
            if not pending:
                await reply.finish(_with_notice("\n".join(notices), f"已切换到集群：{env}，之后的提问都在这个集群上执行。"))
                return True
            await self._ask(
                reply,
                userid,
                {**mapped, "current_env": env},
                identity,
                default_env,
                clusters,
                pending,
                status=f"已切换到集群 {env}，接着处理你刚才的问题...",
            )
            return True
        if "集群" not in match.group(1) and not candidates:
            return False
        notice, current = await self._settled_cluster(userid, identity, mapped, default_env, clusters)
        hint = (
            f"「{target}」匹配到多个集群：{'、'.join(candidates)}，请写完整名称或序号。"
            if candidates
            else f"没有找到集群「{target}」。"
        )
        menu_text = _with_notice(notice, hint + "\n\n" + _cluster_menu(clusters, current))
        await reply.send_cluster_menu(current, clusters, menu_text, prompt_text=menu_text)
        return True

    async def _ask(
        self,
        reply: StreamHandle,
        userid: str,
        mapped: dict[str, Any],
        identity: Identity,
        default_env: str | None,
        clusters: list[dict[str, Any]],
        text: str,
        status: str | None = None,
    ) -> None:
        session_id, notices, active, reset = await self._current_session(userid, identity)
        if active:
            hint = APPROVAL_HINT if active["status"] == "waiting_approval" else BUSY_HINT
            await reply.finish(_with_notice("\n".join(notices), hint))
            return
        previous = _usable(mapped.get("current_env"), clusters)
        current = _usable(default_env if reset else mapped.get("current_env") or default_env, clusters)
        mentioned = _mentioned_cluster(text, clusters)
        if mentioned and mentioned != current:
            current, status = mentioned, f"已切换到集群 {mentioned}，正在分析请求..."
            await self._set_current_env(userid, mentioned)
        elif reset and current and previous and previous != current:
            notices.append(f"新对话使用默认集群 {current}。")
        notice = "\n".join(notices)
        if current is None:
            await self._hold_question(userid, text)
            prompt_text = _with_notice(notice, "请在下方卡片里选择这个对话要操作的集群，选好后接着处理你刚才的问题。")
            fallback = _with_notice(notice, "请先选择这个对话要操作的集群，选好后接着处理你刚才的问题。\n\n" + _cluster_menu(clusters, None))
            await reply.send_cluster_menu(None, clusters, fallback, suggestion=previous, prompt_text=prompt_text)
            return
        await self._run_message(reply, userid, identity, session_id, current, text, notice=notice, status=status, clusters=clusters)

    async def _set_current_env(self, userid: str, env: str | None) -> None:
        await self.service.store.execute(
            "UPDATE kubedoor_ai_im_users SET current_env=$3,updated_at=now() WHERE bot_id=$1::uuid AND external_user_id=$2",
            self.bot.id,
            userid,
            env,
        )

    async def _hold_question(self, userid: str, text: str) -> None:
        await self.service.store.execute(
            "UPDATE kubedoor_ai_im_users SET pending_text=$3,pending_at=now() WHERE bot_id=$1::uuid AND external_user_id=$2",
            self.bot.id,
            userid,
            redact(text),
        )

    async def _take_pending(self, userid: str) -> str | None:
        store = self.service.store
        row = await store.one(
            "UPDATE kubedoor_ai_im_users SET pending_at=NULL WHERE bot_id=$1::uuid AND external_user_id=$2 "
            "AND pending_at > now() - make_interval(mins => $3) RETURNING pending_text",
            self.bot.id,
            userid,
            PENDING_MINUTES,
        )
        await store.execute(
            "UPDATE kubedoor_ai_im_users SET pending_text=NULL,pending_at=NULL "
            "WHERE bot_id=$1::uuid AND external_user_id=$2 AND pending_text IS NOT NULL "
            "AND (pending_at IS NULL OR pending_at <= now() - make_interval(mins => $3))",
            self.bot.id,
            userid,
            PENDING_MINUTES,
        )
        return (row or {}).get("pending_text")

    async def _continue_pending(self, userid: str, text: str, chat_id: str = "") -> None:
        reply = self.adapter.open_push_stream(userid, chat_id)
        try:
            mapped = await self._mapped_user(userid)
            identity, default_env, problem = await self._member_identity(mapped)
            if problem or identity is None:
                await reply.finish(problem or UNBOUND_HINT)
                return
            clusters = await self._member_clusters(identity)
            if not clusters:
                await reply.finish("当前花折账号没有任何 K8S 集群访问权限。")
                return
            await self._ask(reply, userid, mapped, identity, default_env, clusters, escape_turn_context(text))
        except Exception as exc:
            LOGGER.warning("IM 接着处理提问失败 [%s]: %s", self.bot.platform, type(exc).__name__)
            with suppress(Exception):
                await reply.finish("处理失败，请重新发送问题。")

    async def _session_state(self, tx: Any, userid: str, identity: Identity) -> tuple[dict[str, Any], int, dict[str, Any] | None]:
        await tx.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", f"{self.lock_prefix}:{userid}")
        member = await tx.one(
            "SELECT session_id,previous_session_id FROM kubedoor_ai_im_users WHERE bot_id=$1::uuid AND external_user_id=$2",
            self.bot.id,
            userid,
        ) or {}
        idle = self.bot.session_idle_minutes
        idle = DEFAULT_IDLE_MINUTES if idle is None else idle
        return member, idle, await self._own_session(tx, member.get("session_id"), userid, identity, idle)

    async def _own_session(
        self,
        tx: Any,
        session_id: str | None,
        userid: str,
        identity: Identity,
        idle: int = 0,
    ) -> dict[str, Any] | None:
        if not session_id:
            return None
        legacy_prefix = f"{userid}:" if self.bot.id == LEGACY_WECOM_BOT_ID else None
        return await tx.one(
            "SELECT s.id,s.updated_at,now()-s.updated_at > make_interval(mins => $4::int) AS idle,"
            "(SELECT count(*) FROM kubedoor_ai_messages m WHERE m.session_id=s.id AND m.role='user' AND m.replaced_at IS NULL) AS rounds "
            "FROM kubedoor_ai_sessions s WHERE s.id=$1::uuid AND s.username=$2 AND s.channel=$5 "
            "AND (starts_with(s.channel_key,$3) OR ($6::text IS NOT NULL AND starts_with(s.channel_key,$6))) "
            "AND s.checkpoint_purged_at IS NULL",
            session_id,
            identity.username,
            f"{self.bot.id}:{userid}:",
            idle,
            self.session_channel,
            legacy_prefix,
        )

    @staticmethod
    async def _active_run(tx: Any, session_id: str) -> dict[str, Any] | None:
        return await tx.one(
            "SELECT r.id,r.status,EXISTS(SELECT 1 FROM kubedoor_ai_actions a WHERE a.run_id=r.id AND a.state='pending' "
            "AND a.expires_at>now()) AS approvable FROM kubedoor_ai_runs r WHERE r.session_id=$1::uuid "
            "AND r.status IN ('running','waiting_approval') ORDER BY r.created_at DESC LIMIT 1",
            session_id,
        )

    async def _open_session(
        self,
        tx: Any,
        userid: str,
        identity: Identity,
        previous_id: str | None,
        reset: bool = False,
        env: str | None = None,
    ) -> str:
        session_id = str(uuid.uuid4())
        title = "企业微信会话" if self.bot.platform == "wecom" else f"{self.bot.platform} 会话"
        await tx.execute(
            "INSERT INTO kubedoor_ai_sessions(id,username,title,channel,channel_key) VALUES($1::uuid,$2,$3,$4,$5)",
            session_id,
            identity.username,
            title,
            self.session_channel,
            f"{self.bot.id}:{userid}:{identity.username}:{session_id}",
        )
        if reset:
            await tx.execute(
                "UPDATE kubedoor_ai_im_users SET session_id=$3::uuid,previous_session_id=$4::uuid,current_env=$5,"
                "pending_text=NULL,pending_at=NULL,updated_at=now() WHERE bot_id=$1::uuid AND external_user_id=$2",
                self.bot.id,
                userid,
                session_id,
                previous_id,
                env,
            )
        else:
            await tx.execute(
                "UPDATE kubedoor_ai_im_users SET session_id=$3::uuid,previous_session_id=$4::uuid,updated_at=now() "
                "WHERE bot_id=$1::uuid AND external_user_id=$2",
                self.bot.id,
                userid,
                session_id,
                previous_id,
            )
        return session_id

    async def _current_session(self, userid: str, identity: Identity) -> tuple[str, list[str], dict[str, Any] | None, bool]:
        notices: list[str] = []
        async with self.service.store.transaction() as tx:
            member, idle, current = await self._session_state(tx, userid, identity)
            if current is None:
                return await self._open_session(tx, userid, identity, member.get("session_id")), notices, None, False
            run = await self._active_run(tx, current["id"])
            if run and run["status"] == "waiting_approval" and not run["approvable"]:
                await self.service.runtime.cancel(run, identity)
                notices.append("上一个待审批的操作已过期，已停止该任务。")
                run = None
            if run is None and idle > 0 and current["idle"] and current["rounds"]:
                session_id = await self._open_session(tx, userid, identity, current["id"], reset=True)
                notices.append(f"距上次对话已超过 {_idle_text(idle)}，已开始新对话（发送「继续上次」可回到之前的对话）")
                return session_id, notices, None, True
            return current["id"], notices, run, False

    async def _start_new_session(self, userid: str, identity: Identity, env: str | None = None, wait: bool = True) -> str:
        async with self.service.store.transaction() as tx:
            member, _, current = await self._session_state(tx, userid, identity)
            run = await self._active_run(tx, current["id"]) if current else None
            if current and not current["rounds"] and run is None:
                await tx.execute(
                    "UPDATE kubedoor_ai_im_users SET current_env=$3,pending_text=NULL,pending_at=NULL,updated_at=now() "
                    "WHERE bot_id=$1::uuid AND external_user_id=$2",
                    self.bot.id,
                    userid,
                    env,
                )
            else:
                await self._open_session(tx, userid, identity, member.get("session_id"), reset=True, env=env)
        if not run:
            return "已开始新对话。"
        if wait:
            await self.service.runtime.cancel(run, identity)
            return "已开始新对话。上一个对话里未完成的任务已停止。"
        self._spawn(self._cancel_quietly(run, identity), "im-cancel")
        return "已开始新对话。上一个对话里未完成的任务正在停止。"

    async def _cancel_quietly(self, run: dict[str, Any], identity: Identity) -> None:
        try:
            await self.service.runtime.cancel(run, identity)
        except Exception as exc:
            LOGGER.warning("IM 停止任务失败 [%s]: %s", self.bot.platform, type(exc).__name__)

    def _switch_identity(self, mapped: dict[str, Any]) -> tuple[Identity | None, str | None]:
        username = mapped.get("username") or self.bot.unbound_username
        if not username:
            return None, UNBOUND_HINT
        return Identity(username, "none"), None

    async def _switch_command(self, reply: StreamHandle, userid: str, mapped: dict[str, Any], command: str) -> None:
        identity, problem = self._switch_identity(mapped)
        if problem or identity is None:
            await reply.finish(problem or UNBOUND_HINT)
            return
        if command in RESUME_COMMANDS:
            await reply.finish(await self._resume_previous_session(userid, identity))
            return
        text = await self._start_new_session(userid, identity)
        default_env = self._default_env(mapped)
        if default_env:
            await reply.finish(f"{text}这个对话使用默认集群 {default_env}。")
            return
        clusters = await self._quick_clusters(mapped)
        if not clusters:
            await reply.finish(text + "发送「集群」选择这个对话要操作的集群。")
            return
        prompt_text = text + "请在下面的卡片里选择这个对话要操作的集群。"
        fallback = text + "请选择这个对话要操作的集群。\n\n" + _cluster_menu(clusters, None)
        await reply.send_cluster_menu(None, clusters, fallback, suggestion=mapped.get("current_env"), prompt_text=prompt_text)

    async def _resume_previous_session(self, userid: str, identity: Identity) -> str:
        async with self.service.store.transaction() as tx:
            member, _, _ = await self._session_state(tx, userid, identity)
            previous = await self._own_session(tx, member.get("previous_session_id"), userid, identity)
            if previous is None:
                return "没有可以回到的对话。"
            env = await self._switch_to(tx, userid, previous["id"], member.get("session_id"))
        where = f"，集群：{env}" if env else ""
        return f"已回到上一个对话（最后活跃：{_beijing_time(previous['updated_at'])}，共 {previous['rounds']} 轮{where}）。"

    async def _continue_session(self, userid: str, identity: Identity, session_id: str | None) -> tuple[str, str | None]:
        async with self.service.store.transaction() as tx:
            member, _, _ = await self._session_state(tx, userid, identity)
            target = await self._own_session(tx, session_id, userid, identity)
            if target is None:
                return "该对话已不可用，直接发送问题即可开始新对话。", None
            env = await self._switch_to(tx, userid, target["id"], member.get("session_id"))
        return f"已回到上次的对话（共 {target['rounds']} 轮），直接发送问题即可继续。", env

    async def _switch_to(self, tx: Any, userid: str, session_id: str, current_id: str | None) -> str | None:
        await tx.execute("UPDATE kubedoor_ai_sessions SET updated_at=now() WHERE id=$1::uuid", session_id)
        if session_id == current_id:
            return None
        row = await tx.one(
            "SELECT scope->>'env' AS env FROM kubedoor_ai_runs WHERE session_id=$1::uuid ORDER BY created_at DESC LIMIT 1",
            session_id,
        )
        env = (row or {}).get("env")
        await tx.execute(
            "UPDATE kubedoor_ai_im_users SET session_id=$3::uuid,previous_session_id=$4::uuid,current_env=$5,updated_at=now() "
            "WHERE bot_id=$1::uuid AND external_user_id=$2",
            self.bot.id,
            userid,
            session_id,
            current_id,
            env,
        )
        return env

    async def _session_purged(self, session_id: str) -> bool:
        row = await self.service.store.one(
            "SELECT checkpoint_purged_at IS NOT NULL AS purged FROM kubedoor_ai_sessions WHERE id=$1::uuid",
            session_id,
        )
        return bool((row or {}).get("purged"))

    async def _run_message(
        self,
        reply: StreamHandle,
        userid: str,
        identity: Identity,
        session_id: str,
        env: str,
        text: str,
        notice: str = "",
        status: str | None = None,
        clusters: list[dict[str, Any]] | None = None,
        switched: bool = False,
    ) -> None:
        provider = self.resolve_provider()
        try:
            run, created = await self.service.new_run(
                identity,
                session_id,
                {"env": env, "namespace": None, "deployment": None, "pod": None},
                origin=self.bot.platform,
                key=None,
                channel=self.session_channel,
            )
        except AIError as exc:
            if exc.code != "run_active":
                raise
            created = False
        if not created:
            await reply.finish(_with_notice(notice, BUSY_HINT))
            return
        if await self._session_purged(session_id):
            await self.service.runtime.cancel(run, identity)
            await reply.finish("你的 IM 成员记录刚被管理员清理，请重新发送问题。")
            return
        run["_clusters"], run["_switched"] = clusters or [], switched
        clean = escape_turn_context(redact(text, (provider.api_key,)))
        await self.service.store.message(run, "user", clean)
        self.service.runtime.start(run, identity, provider, clean)
        target = await self._watch_run(
            reply,
            userid,
            identity,
            run["id"],
            0,
            env=env,
            status=status or f"正在分析请求（集群：{env}）...",
            notice=notice,
            allow_switch=not switched,
        )
        if target:
            await self._set_current_env(userid, target)
            await self._run_message(
                reply,
                userid,
                identity,
                session_id,
                target,
                f"已切换到集群 {target}，请在这个集群上回答我上一条消息里的问题。",
                notice=notice,
                status=f"已切换到集群 {target}，正在分析请求...",
                clusters=clusters,
                switched=True,
            )

    async def _watch_run(
        self,
        reply: StreamHandle,
        userid: str,
        identity: Identity,
        run_id: str,
        after: int,
        env: str | None = None,
        status: str = "正在分析请求...",
        notice: str = "",
        allow_switch: bool = False,
    ) -> str | None:
        content = status
        await reply.update(_with_notice(notice, content))
        cursor = after
        throttle_sec = max(0.1, (self.adapter.capabilities.stream_throttle_ms or 220) / 1000.0)
        last_send = time.monotonic()
        idle = 0
        while True:
            rows = await self.service.store.fetch(
                "SELECT seq,type,data FROM kubedoor_ai_events WHERE run_id=$1::uuid AND seq>$2 ORDER BY seq LIMIT 200",
                run_id,
                cursor,
            )
            updated = False
            for event in rows:
                cursor = event["seq"]
                kind, data = event["type"], event["data"] or {}
                if kind == "token" and isinstance(data.get("content"), str):
                    content += data["content"]
                    updated = True
                elif kind == "approval":
                    text = _with_notice(notice, (content + "\n\n" + _approval_text(data)).strip())
                    await self._send_approval(reply, userid, identity, run_id, data, cursor, text)
                    return None
                elif kind == "message" and isinstance(data.get("content"), str):
                    content = data["content"]
                    updated = True
                elif kind == "done":
                    if allow_switch and data.get("switch_env"):
                        return data["switch_env"]
                    final = _with_cluster(content.strip() or "本轮操作已结束。", env, data.get("usage"))
                    await reply.finish(_with_notice(notice, final))
                    return None
            if updated and (time.monotonic() - last_send >= throttle_sec or not rows):
                await reply.update(_with_notice(notice, content))
                last_send = time.monotonic()
            run = await self.service.store.one(
                "SELECT status,event_seq,usage,switch_env FROM kubedoor_ai_runs WHERE id=$1::uuid",
                run_id,
            )
            if not run or (run["status"] in ("completed", "failed", "cancelled") and cursor >= run["event_seq"]):
                if allow_switch and (run or {}).get("switch_env"):
                    return run["switch_env"]
                final = _with_cluster(content.strip() or "本轮操作已结束。", env, (run or {}).get("usage"))
                await reply.finish(_with_notice(notice, final))
                return None
            idle += 1
            if idle > 1200:
                await reply.finish("任务仍在处理中，稍后请发送“当前集群”确认状态。")
                return None
            await asyncio.sleep(0.35)

    async def _send_approval(
        self,
        reply: StreamHandle,
        userid: str,
        identity: Identity,
        run_id: str,
        event: dict[str, Any],
        after: int,
        text: str,
    ) -> None:
        action_id = event.get("action_id")
        action = await self.service.store.one(
            "SELECT * FROM kubedoor_ai_actions WHERE id=$1::uuid AND run_id=$2::uuid AND state='pending'",
            action_id,
            run_id,
        )
        run = await self.service.store.one("SELECT session_id,scope FROM kubedoor_ai_runs WHERE id=$1::uuid", run_id)
        if not action or not run:
            await reply.finish(text + "\n\n审批信息已过期，请重新发起操作。")
            return
        card_ref = secrets.token_urlsafe(18)
        await self.service.store.one(
            "INSERT INTO kubedoor_ai_im_approvals"
            "(bot_id,card_ref,action_id,run_id,external_user_id,username,session_id,resume_after_seq,expires_at) "
            "SELECT $1::uuid,$2,id,run_id,$4,$5,$6::uuid,$7,expires_at FROM kubedoor_ai_actions WHERE id=$3::uuid "
            "ON CONFLICT(action_id) DO UPDATE SET bot_id=EXCLUDED.bot_id,card_ref=EXCLUDED.card_ref RETURNING card_ref",
            self.bot.id,
            card_ref,
            action_id,
            userid,
            identity.username,
            run["session_id"],
            after,
        )
        env = run["scope"]["env"]
        preview = action.get("preview") or {}
        detail = {
            "name": action["operation"],
            "env": env,
            "arguments": action.get("arguments"),
            "preview": preview,
            "command": preview.get("command"),
            "diff": preview.get("diff"),
        }
        op_label = OPERATION_NAMES.get(action["operation"], action["operation"])
        summary_line = _approval_summary(detail) or "请核对操作内容后决定。"
        expires_text = _friendly_time(action["expires_at"]) if action.get("expires_at") else ""
        prompt = ApprovalPrompt(
            action_id=str(action_id),
            run_id=str(run_id),
            env=env,
            operation_label=_clip(op_label, 40),
            summary_markdown=text + "\n请核对后在下方卡片批准或拒绝，也可以直接回复「批准」或「拒绝」。",
            expires_at_text=expires_text,
            card_ref=card_ref,
            summary_line=summary_line,
            body_markdown=_approval_text(detail),
        )
        fallback = text + "\n审批卡片没有发出来，请核对后直接回复「批准」或「拒绝」。"
        await reply.send_approval(prompt, fallback)

    async def handle_card_action(self, event: CardActionEvent) -> dict[str, Any] | None:
        """Process card callbacks within the adapter's callback SLA."""
        userid = event.external_user_id
        action_id = event.action_id or ""
        if not isinstance(userid, str) or not userid or not action_id or action_id == "noop":
            return None
        if event.msgid and not await self._claim_message(event.msgid, userid, "card"):
            return None

        parts = action_id.split(":")
        kind = parts[0]
        try:
            if action_id in ("approve", "reject"):
                return await self._approval_card(event, event.callback_token, action_id)
            if kind == "approval" and len(parts) >= 3 and parts[1] in ("approve", "reject"):
                return await self._approval_card(event, parts[2], parts[1])
            if action_id in ("session_new", "session_resume"):
                return await self._session_card(event, event.callback_token, action_id, event.selected_value)
            if kind == "session" and len(parts) >= 2 and parts[1] in ("new", "resume"):
                choice = "session_new" if parts[1] == "new" else "session_resume"
                return await self._session_card(event, event.callback_token, choice, event.selected_value)
            if action_id in ("env_pick", "cluster:select", "cluster_select") or kind == "cluster":
                return await self._pick_card(event, event.callback_token, event.selected_value)
        except Exception as exc:
            LOGGER.warning("IM 卡片事件处理失败 [%s]: %s", self.bot.platform, type(exc).__name__)
            return self._card_outcome(
                event.callback_token,
                "处理失败",
                detail="请稍后重试，或直接发消息给我。",
                status="失败",
                toast_type="error",
                toast="处理失败，请稍后重试",
            )
        return None

    @staticmethod
    def _card_outcome(
        card_ref: str,
        title: str,
        desc: str | None = None,
        detail: str | None = None,
        status: str = "已完成",
        toast_type: str = "info",
        toast: str | None = None,
    ) -> dict[str, Any]:
        return {
            "card_ref": card_ref,
            "title": title,
            "desc": desc,
            "detail": detail,
            "status": status,
            "toast_type": toast_type,
            "toast": toast or title,
        }

    async def _session_card(self, event: CardActionEvent, card_ref: str, choice: str, env: str) -> dict[str, Any]:
        userid = event.external_user_id
        mapped = await self._mapped_user(userid)
        identity, problem = self._switch_identity(mapped)
        if problem or identity is None:
            return self._card_outcome(card_ref, "无法切换对话", detail=problem or UNBOUND_HINT, status="未完成", toast_type="warning")
        if choice == "session_new":
            default_env = self._default_env(mapped)
            chosen = env or None
            detail = await self._start_new_session(userid, identity, None if chosen == default_env else chosen, wait=False)
            effective = chosen or default_env
            detail += "直接发送问题即可。" if effective else "发送问题后会请你先选择集群。"
            return self._card_outcome(
                card_ref,
                "已开始新对话",
                f"集群：{effective}" if effective else "未选择集群",
                detail,
                toast_type="success",
            )
        detail, restored_env = await self._continue_session(userid, identity, _card_session(card_ref))
        return self._card_outcome(
            card_ref,
            "继续上次对话",
            f"集群：{restored_env}" if restored_env else None,
            detail,
            toast_type="success",
        )

    async def _pick_card(self, event: CardActionEvent, card_ref: str, env: str) -> dict[str, Any]:
        userid = event.external_user_id
        mapped = await self._mapped_user(userid)
        identity, problem = self._switch_identity(mapped)
        if problem or identity is None or not env:
            return self._card_outcome(
                card_ref,
                "没有切换集群",
                detail=problem or "请在下拉框里选择集群。",
                status="未完成",
                toast_type="warning",
            )
        await self._current_session(userid, identity)
        await self._set_current_env(userid, env)
        pending = await self._take_pending(userid)
        detail = f"正在处理你刚才的问题：{_clip(pending, 40)}，结果稍后发送。" if pending else "之后的提问都在这个集群上执行。"
        if pending:
            self._spawn(self._continue_pending(userid, pending, event.chat_id), "im-pending")
        return self._card_outcome(card_ref, "已切换集群", env, detail, "已切换", toast_type="success", toast=f"已切换到 {env}")

    async def _approval_card(self, event: CardActionEvent, card_ref: str, choice: str) -> dict[str, Any]:
        userid = event.external_user_id
        store = self.service.store
        callback_sync = event.callback_sync
        approval = None
        if isinstance(card_ref, str) and card_ref:
            approval = await store.one(
                "SELECT * FROM kubedoor_ai_im_approvals WHERE bot_id=$1::uuid AND (card_ref=$2 OR action_id::text=$2) AND expires_at>now()",
                self.bot.id,
                card_ref,
            )
        if not approval or approval["external_user_id"] != userid:
            outcome = self._card_outcome(
                card_ref,
                "审批卡片已失效",
                detail="该审批已处理、已过期或不属于当前账号。",
                status="已失效",
                toast_type="warning",
            )
            if callback_sync is not None and not callback_sync.claim_sync():
                await self._safe_update_card(card_ref, "failed", outcome["detail"] or outcome["title"], event.raw_context)
            return outcome
        problem = self._approval_problem(approval, await self._mapped_user(userid))
        if problem:
            outcome = self._card_outcome(card_ref, "审批卡片已失效", detail=problem, status="已失效", toast_type="warning")
            if callback_sync is not None and not callback_sync.claim_sync():
                await self._safe_update_card(approval["card_ref"], "failed", problem, event.raw_context)
            return outcome

        sla = max(0.05, min(float(self.adapter.capabilities.card_callback_sla_sec or 2.2), 4.0))
        # Leave margin before platform callback deadline (e.g. 1.7s for 2.2s Feishu SLA, 3.5s for 4.0s SLA)
        margin = min(0.5, max(0.05, sla * 0.25)) if sla <= 3.0 else 0.5
        budget = max(0.05, sla - margin)
        loop = asyncio.get_running_loop()
        if callback_sync is not None:
            deadline = loop.time() + max(0.0, callback_sync.deadline - time.monotonic() - margin)
        else:
            deadline = loop.time() + budget

        def _handoff_pending() -> dict[str, Any]:
            self._spawn(
                self._finish_pending_approval(userid, event.chat_id, approval, choice, event.raw_context),
                "im-decision",
            )
            return self._card_outcome(
                card_ref,
                "已收到",
                None,
                "正在处理，结果稍后发送。",
                "处理中",
                toast_type="info",
                toast="处理中…",
            )

        if callback_sync is not None and callback_sync.sync_timed_out:
            return _handoff_pending()

        try:
            async with asyncio.timeout_at(deadline):
                identity, env, allowed = await self._approver(approval)
            if not allowed:
                outcome = self._card_outcome(
                    card_ref,
                    "不能批准",
                    f"集群：{env}",
                    "该账号已失去目标集群权限，不能批准此操作。",
                    "已失效",
                    toast_type="error",
                )
                if callback_sync is not None and not callback_sync.claim_sync():
                    await self._deliver_missed_failure(
                        approval["card_ref"],
                        userid,
                        event.chat_id,
                        outcome["detail"] or outcome["title"],
                        event.raw_context,
                    )
                return outcome
            decision_fut = asyncio.ensure_future(self._decide(identity, approval, choice))
            remaining = deadline - loop.time()
            try:
                if remaining <= 0:
                    raise TimeoutError()
                result = await asyncio.wait_for(asyncio.shield(decision_fut), remaining)
            except TimeoutError:
                self._spawn(
                    self._finish_decision(userid, event.chat_id, identity, approval, decision_fut, env, event.raw_context),
                    "im-decision",
                )
                return self._card_outcome(
                    card_ref,
                    "已收到",
                    f"集群：{env}",
                    "正在处理，结果稍后发送。",
                    "处理中",
                    toast_type="info",
                    toast="处理中…",
                )
            await store.execute(
                "DELETE FROM kubedoor_ai_im_approvals WHERE bot_id=$1::uuid AND card_ref=$2",
                self.bot.id,
                approval["card_ref"],
            )
        except (AIError, TimeoutError) as exc:
            if isinstance(exc, TimeoutError) and callback_sync is not None and callback_sync.sync_timed_out:
                return _handoff_pending()
            detail = str(exc) if isinstance(exc, AIError) else "校验账号超时，请稍后再点一次。"
            outcome = self._card_outcome(card_ref, "审批未完成", detail=detail, status="未完成", toast_type="error")
            if callback_sync is not None and not callback_sync.claim_sync():
                await self._deliver_missed_failure(approval["card_ref"], userid, event.chat_id, detail, event.raw_context)
            return outcome

        approved = choice == "approve"
        running = result.get("status") == "running"
        detail = ("正在执行，结果稍后发送。" if approved else "AI 会接着说明情况，稍后发送。") if running else None
        label = "已批准" if approved else "已拒绝"
        outcome = self._card_outcome(card_ref, label, f"集群：{env}", detail, label, toast_type="success", toast=label)
        if callback_sync is not None and not callback_sync.claim_sync():
            await self._safe_update_card(approval["card_ref"], "done", f"已处理（集群：{env}）", event.raw_context)
            if running:
                push = self.adapter.open_push_stream(userid, event.chat_id)
                self._spawn(
                    self._watch_run(push, userid, identity, approval["run_id"], approval["resume_after_seq"], env=env),
                    "im-run-resume",
                )
            else:
                push = self.adapter.open_push_stream(userid, event.chat_id)
                self._spawn(push.finish(f"{label}（集群：{env}）。"), "im-run-resume")
            return outcome
        if running:
            push = self.adapter.open_push_stream(userid, event.chat_id)
            self._spawn(
                self._watch_run(push, userid, identity, approval["run_id"], approval["resume_after_seq"], env=env),
                "im-run-resume",
            )
        return outcome

    def _approval_problem(self, approval: dict[str, Any], mapped: dict[str, Any]) -> str | None:
        if (mapped.get("username") or self.bot.unbound_username) != approval["username"]:
            return "账号绑定已变更，该审批不能继续。"
        return None

    async def _approver(self, approval: dict[str, Any]) -> tuple[Identity, str | None, bool]:
        identity = await self._identity(approval["username"])
        env = (
            await self.service.store.one(
                "SELECT scope->>'env' AS env FROM kubedoor_ai_runs WHERE id=$1::uuid",
                approval["run_id"],
            )
            or {}
        ).get("env")
        return identity, env, identity.can_read(env)

    def _decide(self, identity: Identity, approval: dict[str, Any], choice: str):
        provider = self.resolve_provider()
        return self.service.decision(
            identity,
            approval["run_id"],
            {
                "action_id": str(approval["action_id"]),
                "decision": choice,
                "provider": provider.__dict__,
            },
        )

    async def _text_decision(self, reply: StreamHandle, userid: str, mapped: dict[str, Any], choice: str) -> bool:
        store = self.service.store
        approval = await store.one(
            "SELECT a.* FROM kubedoor_ai_im_approvals a JOIN kubedoor_ai_runs r ON r.id=a.run_id "
            "WHERE a.bot_id=$1::uuid AND a.external_user_id=$2 AND a.expires_at>now() AND r.status='waiting_approval' "
            "ORDER BY a.created_at DESC LIMIT 1",
            self.bot.id,
            userid,
        )
        if not approval:
            return False
        problem = self._approval_problem(approval, mapped)
        if problem:
            await reply.finish(problem)
            return True
        try:
            identity, env, allowed = await self._approver(approval)
            if not allowed:
                await reply.finish("该账号已失去目标集群权限，不能批准此操作。")
                return True
            result = await self._decide(identity, approval, choice)
            await store.execute(
                "DELETE FROM kubedoor_ai_im_approvals WHERE bot_id=$1::uuid AND card_ref=$2",
                self.bot.id,
                approval["card_ref"],
            )
        except AIError as exc:
            await reply.finish(f"审批没有完成：{exc}")
            return True
        approved = choice == "approve"
        if result.get("status") == "running":
            await self._watch_run(
                reply,
                userid,
                identity,
                approval["run_id"],
                approval["resume_after_seq"],
                env=env,
                status="已批准，正在执行..." if approved else "已拒绝，AI 正在说明情况...",
            )
        else:
            await reply.finish(f"{'已批准' if approved else '已拒绝'}（集群：{env}）。")
        return True

    async def _deliver_missed_failure(
        self,
        card_ref: str,
        userid: str,
        chat_id: str,
        detail: str,
        raw_context: Any,
    ) -> None:
        await self._safe_update_card(card_ref, "failed", detail, raw_context)
        with suppress(Exception):
            await self.adapter.open_push_stream(userid, chat_id).finish(detail)

    async def _finish_pending_approval(
        self,
        userid: str,
        chat_id: str,
        approval: dict[str, Any],
        choice: str,
        raw_context: Any = None,
    ) -> None:
        try:
            async with asyncio.timeout(10):
                identity, env, allowed = await self._approver(approval)
        except Exception as exc:
            detail = str(exc) if isinstance(exc, AIError) else "校验账号超时，请稍后再点一次。"
            await self._deliver_missed_failure(approval["card_ref"], userid, chat_id, detail, raw_context)
            return
        if not allowed:
            await self._deliver_missed_failure(
                approval["card_ref"],
                userid,
                chat_id,
                "该账号已失去目标集群权限，不能批准此操作。",
                raw_context,
            )
            return
        decision_fut = asyncio.ensure_future(self._decide(identity, approval, choice))
        await self._finish_decision(userid, chat_id, identity, approval, decision_fut, env, raw_context)

    async def _finish_decision(
        self,
        userid: str,
        chat_id: str,
        identity: Identity,
        approval: dict[str, Any],
        decision_fut: asyncio.Future[Any],
        env: str | None,
        raw_context: Any = None,
    ) -> None:
        reply = self.adapter.open_push_stream(userid, chat_id)
        try:
            result = await decision_fut
            await self.service.store.execute(
                "DELETE FROM kubedoor_ai_im_approvals WHERE bot_id=$1::uuid AND card_ref=$2",
                self.bot.id,
                approval["card_ref"],
            )
        except AIError as exc:
            await self._safe_update_card(approval["card_ref"], "failed", f"审批未完成：{exc}", raw_context)
            await reply.finish(f"审批没有完成：{exc}")
            return
        except Exception as exc:
            LOGGER.warning("IM 审批处理失败 [%s]: %s", self.bot.platform, type(exc).__name__)
            await self._safe_update_card(approval["card_ref"], "failed", "审批处理失败", raw_context)
            await reply.finish("审批处理失败，请在花折 AI 会话中核验当前状态。")
            return

        await self._safe_update_card(approval["card_ref"], "done", f"已处理（集群：{env}）", raw_context)
        if result.get("status") == "running":
            await self._watch_run(reply, userid, identity, approval["run_id"], approval["resume_after_seq"], env=env)

    async def _safe_update_card(self, card_ref: str, decision: str, summary: str, raw_context: Any) -> None:
        with suppress(Exception):
            await self.adapter.update_approval_card(card_ref, decision, summary, raw_context)

    async def _bind_by_code(self, userid: str, code: str) -> str:
        store = self.service.store
        async with store.transaction() as tx:
            await tx.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", f"{self.lock_prefix}:{userid}")
            member = await tx.one(
                "SELECT bind_failures,bind_failed_at > now() - make_interval(mins => $3) AS recent "
                "FROM kubedoor_ai_im_users WHERE bot_id=$1::uuid AND external_user_id=$2",
                self.bot.id,
                userid,
                BIND_CODE_MINUTES,
            ) or {}
            if member.get("recent") and (member.get("bind_failures") or 0) >= BIND_MAX_FAILURES:
                return f"绑定码输错次数太多，请 {BIND_CODE_MINUTES} 分钟后再试。"
            total = await tx.one(
                "SELECT coalesce(sum(bind_failures),0) AS failures FROM kubedoor_ai_im_users "
                "WHERE bind_failed_at > now() - make_interval(mins => $1)",
                BIND_CODE_MINUTES,
            ) or {}
            if (total.get("failures") or 0) >= BIND_MAX_TOTAL_FAILURES:
                LOGGER.warning("IM 绑定码 %s 分钟内输错 %s 次,暂停自助绑定", BIND_CODE_MINUTES, total.get("failures"))
                return f"绑定暂时不可用，请 {BIND_CODE_MINUTES} 分钟后再试或联系管理员。"
            row = await tx.one("DELETE FROM kubedoor_ai_im_bind_codes WHERE code=$1 AND expires_at>now() RETURNING username", code)
            if not row and self.bot.platform == "wecom":
                row = await tx.one(
                    "DELETE FROM kubedoor_ai_wecom_bind_codes WHERE code=$1 AND expires_at>now() RETURNING username",
                    code,
                )
            if not row:
                await tx.execute(
                    "UPDATE kubedoor_ai_im_users SET bind_failures=CASE WHEN bind_failed_at > now() - make_interval(mins => $3) "
                    "THEN bind_failures+1 ELSE 1 END,bind_failed_at=now() WHERE bot_id=$1::uuid AND external_user_id=$2",
                    self.bot.id,
                    userid,
                    BIND_CODE_MINUTES,
                )
                return "绑定码不对或已过期，请在花折 Web 右上角用户菜单「绑定 IM 账号」重新生成。"
        username = row["username"]
        try:
            await self._identity(username)
        except AIError:
            return f"花折账号 {username} 不存在、已停用或不可用，不能绑定。"
        await store.execute(
            "UPDATE kubedoor_ai_im_users SET default_env=CASE WHEN username IS NOT DISTINCT FROM $3 THEN default_env END,"
            "current_env=CASE WHEN username IS NOT DISTINCT FROM $3 THEN current_env END,username=$3,bound_by=$3,bound_at=now(),"
            "pending_text=NULL,pending_at=NULL,bind_failures=0,bind_failed_at=NULL,updated_at=now() "
            "WHERE bot_id=$1::uuid AND external_user_id=$2",
            self.bot.id,
            userid,
            username,
        )
        return f"已绑定花折账号 {username}，之后按这个账号的权限使用机器人。"

    async def _save_self_name(self, userid: str, name: str) -> str:
        await self.service.store.execute(
            "UPDATE kubedoor_ai_im_users SET self_name=$3,updated_at=now() WHERE bot_id=$1::uuid AND external_user_id=$2",
            self.bot.id,
            userid,
            name,
        )
        return f"已记下你的名字「{name}」，管理员在花折里能看到，方便为你绑定账号（只用来辨认，不影响权限）。"


async def purge_dead_im_checkpoints(service: Any, days: int, limit: int = PURGE_BATCH) -> int:
    """Purge LangGraph checkpoints for unreachable old IM conversations across all platforms."""
    if days <= 0:
        return 0
    store = service.store
    rows = await store.fetch(
        "SELECT s.id,s.channel,s.channel_key FROM kubedoor_ai_sessions s WHERE "
        + DEAD_IM_SESSION
        + " ORDER BY s.updated_at LIMIT $2",
        days,
        limit,
    )
    purged = 0
    for row in rows:
        parts = (row.get("channel_key") or "").split(":", 2)
        if len(parts) >= 3 and len(parts[0]) == 36 and "-" in parts[0]:
            lock_key = f"im:{parts[0]}:{parts[1]}"
        else:
            lock_key = f"wecom:{parts[0]}"
        async with store.transaction() as tx:
            await tx.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", lock_key)
            if not await tx.one(
                "SELECT s.id FROM kubedoor_ai_sessions s WHERE s.id=$2::uuid AND " + DEAD_IM_SESSION,
                days,
                row["id"],
            ):
                continue
            await service.runtime.checkpointer.adelete_thread(row["id"])
            await tx.execute("UPDATE kubedoor_ai_sessions SET checkpoint_purged_at=now() WHERE id=$1::uuid", row["id"])
        purged += 1
    return purged
