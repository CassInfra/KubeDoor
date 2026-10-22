from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
import uuid
from contextlib import suppress
from types import MappingProxyType

from aibot import WSClient, WSClientOptions, generate_req_id

from .domain import AIError, Identity, Provider, redact

LOGGER = logging.getLogger(__name__)
CHANNEL = "wecom"
MAX_STREAM_BYTES = 18000


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
        async with asyncio.timeout(10):
            raw = await self.service.gateway.master(hint, "GET", "/api/ai/internal/identity")
        permission = raw.get("permission")
        clusters = raw.get("clusters")
        if raw.get("username") != username or permission not in ("admin", "rw", "read", "none") or not isinstance(clusters, dict):
            raise AIError("花折用户身份无效", 403, "identity_invalid")
        return Identity(username, permission, MappingProxyType({} if permission == "admin" else clusters))

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
        if not mapped.get("username"):
            await self._reply(frame, "该企业微信成员尚未绑定花折账号，请联系管理员完成绑定。", True, stream_id)
            return
        try:
            identity = await self._identity(mapped["username"])
        except AIError:
            await self._reply(frame, "绑定的花折账号不存在、已停用或不可用，请联系管理员。", True, stream_id)
            return
        clusters = (await self.service.bootstrap(identity))["clusters"]
        if not clusters:
            await self._reply(frame, "当前花折账号没有任何 K8S 集群访问权限。", True, stream_id)
            return
        if text in ("当前集群", "查看集群"):
            await self._reply(frame, f"当前集群：{mapped.get('current_env') or mapped.get('default_env') or '未选择'}\n可访问：" + "、".join(item["env"] for item in clusters), True, stream_id)
            return
        if text.startswith("切换集群 "):
            env = text[len("切换集群 "):].strip()
            if env not in {item["env"] for item in clusters}:
                await self._reply(frame, "该集群不存在或当前账号无权访问。", True, stream_id)
                return
            await self.service.store.execute(
                "UPDATE kubedoor_ai_wecom_users SET current_env=$2,updated_at=now() WHERE wecom_userid=$1", userid, env,
            )
            await self._reply(frame, f"已切换到集群：{env}", True, stream_id)
            return
        envs = {item["env"] for item in clusters}
        env = mapped.get("current_env") or mapped.get("default_env")
        if env not in envs:
            env = next((item["env"] for item in clusters if item.get("permission") in ("rw", "read")), None)
            if env:
                await self.service.store.execute(
                    "UPDATE kubedoor_ai_wecom_users SET current_env=$2,updated_at=now() WHERE wecom_userid=$1",
                    userid, env,
                )
        if env is None:
            await self._reply(frame, "当前账号没有可访问的 K8S 集群。", True, stream_id)
            return
        try:
            await self._run_message(frame, userid, mapped, identity, env, text, stream_id)
        except Exception as exc:
            LOGGER.warning("企业微信消息处理失败: %s", type(exc).__name__)
            await self._reply(frame, "处理失败，请检查花折 AI 服务状态后重试。", True, stream_id)

    async def _get_session(self, userid, identity, mapped):
        channel_key = f"{userid}:{identity.username}"
        sid = mapped.get("session_id")
        if sid:
            row = await self.service.store.one(
                "SELECT id FROM kubedoor_ai_sessions WHERE id=$1::uuid AND username=$2 AND channel='wecom' AND channel_key=$3",
                sid, identity.username, channel_key,
            )
            if row:
                return sid
        row = await self.service.store.one(
            "INSERT INTO kubedoor_ai_sessions(id,username,title,channel,channel_key) VALUES($1::uuid,$2,'企业微信会话','wecom',$3) "
            "ON CONFLICT(channel,channel_key) WHERE channel_key IS NOT NULL DO UPDATE SET updated_at=now() RETURNING id",
            uuid.uuid4(), identity.username, channel_key,
        )
        await self.service.store.execute(
            "UPDATE kubedoor_ai_wecom_users SET username=$2,session_id=$3::uuid,updated_at=now() WHERE wecom_userid=$1",
            userid, identity.username, str(row["id"]),
        )
        return str(row["id"])

    async def _run_message(self, frame, userid, mapped, identity, env, text, stream_id):
        session_id = await self._get_session(userid, identity, mapped)
        provider = Provider.parse({key: self.config[key] for key in ("base_url", "api_key", "model")})
        run, created = await self.service.new_run(
            identity, session_id, {"env": env, "namespace": None, "deployment": None, "pod": None},
            origin="wecom", key=None, channel=CHANNEL,
        )
        if not created:
            await self._reply(frame, "该会话已有任务运行或等待审批，请稍后重试。", True, stream_id)
            return
        clean = redact(text, (provider.api_key,))
        await self.service.store.message(run, "user", clean)
        self.service.runtime.start(run, identity, provider, clean)
        await self._watch_run(frame, userid, identity, run["id"], 0, stream_id=stream_id)

    async def _watch_run(self, frame, userid, identity, run_id, after, stream_id=None):
        stream_id = stream_id or generate_req_id("stream")
        content = "正在分析请求..."
        await self._reply(frame, content, False, stream_id)
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
                    await self._reply(frame, (content + "\n\n操作需要审批，请查看下方审批卡片。").strip(), True, stream_id)
                    await self._send_approval(frame, userid, identity, run_id, data, cursor)
                    return
                elif kind == "message" and isinstance(data.get("content"), str):
                    content = data["content"]
                    updated = True
                elif kind == "done":
                    await self._reply(frame, content.strip() or "本轮操作已结束。", True, stream_id)
                    return
            if updated and (time.monotonic() - last_send >= 0.7 or not rows):
                await self._reply(frame, _limit_stream(content), False, stream_id)
                last_send = time.monotonic()
            run = await self.service.store.one("SELECT status,event_seq FROM kubedoor_ai_runs WHERE id=$1::uuid", run_id)
            if not run or run["status"] in ("completed", "failed", "cancelled") and cursor >= run["event_seq"]:
                await self._reply(frame, content.strip() or "本轮操作已结束。", True, stream_id)
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
        title = f"{action['operation']} / {env}"
        details = action.get("preview") or {}
        summary = details.get("command") or details.get("diff") or json.dumps(action.get("arguments") or {}, ensure_ascii=False)
        summary = _clip(str(summary).replace("\n", " "), 140)
        card = {"card_type": "button_interaction", "main_title": {"title": "K8S 操作待审批", "desc": _clip(title, 40)},
                "sub_title_text": summary or "请核对操作内容后决定。",
                "button_list": [{"text": "批准", "key": "approve", "style": 1},
                                {"text": "拒绝", "key": "reject", "style": 2}],
                "task_id": task_id}
        await self.client.reply_template_card(frame, card)

    async def _on_card_event(self, frame):
        self._spawn(self._handle_card_event(frame), "wecom-approval")

    async def _handle_card_event(self, frame):
        body = frame.get("body") or {}
        event = body.get("event") or {}
        userid = ((body.get("from") or {}).get("userid"))
        task_id, choice = event.get("task_id"), event.get("event_key")
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
        if mapped.get("username") != approval["username"]:
            await self._reply(frame, "账号绑定已变更，该审批不能继续。")
            return
        try:
            identity = await self._identity(approval["username"])
            if not identity.can_read((await self.service.store.one(
                "SELECT scope->>'env' AS env FROM kubedoor_ai_runs WHERE id=$1::uuid", approval["run_id"],
            ) or {}).get("env")):
                await self._reply(frame, "该账号已失去目标集群权限，不能批准此操作。")
                return
            provider = Provider.parse({key: self.config[key] for key in ("base_url", "api_key", "model")})
            result = await self.service.decision(identity, approval["run_id"], {
                "action_id": str(approval["action_id"]), "decision": choice, "provider": provider.__dict__,
            })
            await self.service.store.execute("DELETE FROM kubedoor_ai_wecom_approvals WHERE task_id=$1", task_id)
            card = {"card_type": "text_notice", "main_title": {"title": "已批准，正在执行" if choice == "approve" else "已拒绝"}, "task_id": task_id}
            with suppress(Exception):
                await self.client.update_template_card(frame, card)
            if result.get("status") == "running":
                self._spawn(self._watch_run(frame, userid, identity, approval["run_id"], approval["resume_after_seq"]), "wecom-run-resume")
        except AIError as exc:
            await self._reply(frame, str(exc))
        except Exception as exc:
            LOGGER.warning("企微审批处理失败: %s", type(exc).__name__)
            await self._reply(frame, "审批处理失败，请在花折 AI 会话中核验当前状态。")

    async def admin_config(self):
        row = await self.service.store.one("SELECT enabled,encrypted_config,updated_by,updated_at FROM kubedoor_ai_wecom_config WHERE singleton=true")
        if not row:
            return {**self.status, "has_bot_secret": False, "has_api_key": False}
        config = self.service.gateway.cipher.decrypt("wecom", bytes(row["encrypted_config"]))
        return {**self.status, "enabled": row["enabled"], "bot_id": config.get("bot_id", ""),
                "base_url": config.get("base_url", ""), "model": config.get("model", ""),
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
        enabled = config["enabled"]
        if enabled:
            if not config["bot_id"] or not config["bot_secret"]:
                raise AIError("启用机器人需要 Bot ID 和 Secret")
            Provider.parse({key: config[key] for key in ("base_url", "api_key", "model")})
        encrypted = self.service.gateway.cipher.encrypt("wecom", config)
        row = await self.service.store.one(
            "INSERT INTO kubedoor_ai_wecom_config(singleton,enabled,encrypted_config,updated_by) VALUES(true,$1,$2,$3) "
            "ON CONFLICT(singleton) DO UPDATE SET enabled=EXCLUDED.enabled,encrypted_config=EXCLUDED.encrypted_config,updated_by=EXCLUDED.updated_by,updated_at=now() "
            "RETURNING enabled,encrypted_config", enabled, encrypted, identity.username,
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

    async def delete_user(self, userid):
        await self.service.store.execute(
            "UPDATE kubedoor_ai_wecom_users SET username=NULL,default_env=NULL,current_env=NULL,session_id=NULL,updated_at=now() WHERE wecom_userid=$1",
            userid,
        )


def _clip(value, limit):
    return value if len(value) <= limit else value[:limit - 1] + "…"


def _limit_stream(value):
    data = value.encode("utf-8")
    if len(data) <= MAX_STREAM_BYTES:
        return value
    return data[:MAX_STREAM_BYTES - 40].decode("utf-8", "ignore") + "\n\n（内容过长，已截断）"
