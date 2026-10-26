from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import time
import unicodedata
import uuid
from contextlib import suppress
from datetime import datetime
from types import MappingProxyType

import asyncpg
from aibot import WSClient, WSClientOptions, generate_req_id

from .domain import BEIJING_TZ, AIError, Identity, Provider, parse_model_limits, parse_reasoning_effort, redact

LOGGER = logging.getLogger(__name__)
CHANNEL = "wecom"
MAX_STREAM_BYTES = 18000
# 查看集群列表的命令;切换命令:「切换集群 2」「切换到：cassmall-kunlun」「/切换集群 beta 昆仑」
MENU_COMMANDS = {"集群", "当前集群", "查看集群", "集群列表", "选择集群", "切换集群", "帮助", "help", "?", "？"}
SWITCH_COMMAND = re.compile(r"^/?(切换到集群|切换集群|切换到|切换)\s*[:：]?\s*(\S.*)$")
# 企微只有一个聊天窗口:开新对话、回到上一个对话都靠聊天命令
NEW_SESSION_COMMANDS = {"新对话", "新会话", "开始新对话", "清空上下文", "重新开始", "/new", "/reset"}
RESUME_COMMANDS = {"继续上次", "继续上次对话", "回到上次", "/resume"}
# 自助绑定:花折用户在 Web 右上角用户菜单为自己的账号生成 6 位绑定码,成员在企微里发「绑定 123456」
# (匹配前做 NFKC,手机输入法打出的全角数字、冒号也认)
BIND_COMMAND = re.compile(r"^/?绑定\s*[:：]?\s*([0-9]{6})$")
BIND_CODE_MINUTES = 10
BIND_MAX_FAILURES = 5   # BIND_CODE_MINUTES 分钟内同一成员输错这么多次后暂停
BIND_MAX_TOTAL_FAILURES = 30   # 所有成员加起来输错这么多次也暂停,多个企微账号一起猜码也猜不完
# 成员自报名字(企微只给加密的 UserID,帮管理员认出是谁,不作为身份依据)。「我是」后面要有空格或冒号、
# 名字里不能有空白和标点,免得把「我是不是该扩容」「我是新来的，怎么…」这类提问当成名字
SELF_NAME_COMMAND = re.compile(r"^/?我是(?:\s*[:：]\s*|\s+)([^\s,，.。!！?？、;；:：]{1,16})$")
UNBOUND_HINT = ("该企业微信成员尚未绑定花折账号。请登录花折 Web，在右上角用户菜单「绑定企业微信」获取绑定码，"
                "再把「绑定 绑定码」发给我；也可以发送「我是 你的名字」后请管理员绑定。")
DEFAULT_IDLE_MINUTES = 120
# 还没选集群而被拦下的提问,成员在这么多分钟内选好集群就接着处理
PENDING_MINUTES = 10
# 进入会话、点卡片按钮后要在 5 秒内回复:取集群列表时调 master 最多等这么久,点审批卡片时整个处理最多等 CARD_SECONDS
CARD_MASTER_SECONDS = 3
CARD_SECONDS = 4
# 「切换集群 X」的 X 里去掉集群名后只剩这些(「切换到 beta 昆仑集群」)才是单纯切换;还有别的内容就是带着问题的提问
SWITCH_FILLER = re.compile(r"[\s,，.。:：;；!！?？、]|集群|环境|一下|吧|上|去")
# 卡片的集群下拉框最多 10 个选项,选项 id 最长 128 字节(企微限制)
CARD_OPTIONS = 10
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
# 解除绑定:账号、集群、对话和被拦下的提问一起清掉,下次发消息按默认权限(或要求绑定)重新开始
UNBIND_COLUMNS = ("username=NULL,default_env=NULL,current_env=NULL,session_id=NULL,previous_session_id=NULL,"
                  "bound_by=NULL,bound_at=NULL,pending_text=NULL,pending_at=NULL")
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


class _StreamReply:
    """回复成员发来的消息:进度在同一条流式消息里刷新,最后结束;卡片跟在后面回复"""

    def __init__(self, integration, frame, stream_id=None):
        self.integration, self.frame = integration, frame
        self.stream_id = stream_id or generate_req_id("stream")

    async def update(self, content):
        await self.integration._reply(self.frame, content, False, self.stream_id)

    async def finish(self, content):
        await self.integration._reply(self.frame, content, True, self.stream_id)

    async def say(self, content):
        """流式消息结束后另起一条"""
        await self.integration._reply(self.frame, content)

    async def card(self, card):
        client = self.integration.client
        if client and client.is_connected:
            await client.reply_template_card(self.frame, card)


class _PushReply:
    """点卡片按钮之后没有能回复的消息(卡片事件只能更新那张卡片):结果和新卡片主动推送给成员,中间进度不发"""

    def __init__(self, integration, userid):
        self.integration, self.userid = integration, userid

    async def update(self, content):
        return

    async def finish(self, content):
        await self.integration._push(self.userid, {"msgtype": "markdown", "markdown": {"content": _limit_stream(content)}})

    say = finish

    async def card(self, card):
        await self.integration._push(self.userid, {"msgtype": "template_card", "template_card": card})


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
            return None, None, UNBOUND_HINT
        try:
            identity = await self._identity(setting["unbound_username"])
        except AIError:
            identity = None
        # 保存时已经不让选管理员账号;这里兜底账号之后被改成管理员的情况
        if identity is None or identity.is_admin:
            return None, None, "未绑定成员的默认权限当前不可用，请联系管理员绑定花折账号。"
        return identity, setting["unbound_default_env"], None

    async def _default_env(self, mapped):
        """新对话开始时用的集群:绑定的成员看自己的默认集群,未绑定的看默认权限里的(只读本地数据)"""
        if mapped.get("username"):
            return mapped.get("default_env")
        return (await self.unbound_setting())["unbound_default_env"]

    async def _member_clusters(self, identity):
        """成员能提问的集群(读写或只读),带管理员在 Agent 管理里设的别名"""
        return [item for item in (await self.service.bootstrap(identity))["clusters"]
                if item.get("permission") in ("rw", "read")]

    async def _quick_clusters(self, mapped):
        """进入会话、点卡片按钮后要在 5 秒内回复:取成员可访问的集群,取不到(master 慢或不可用)返回 None"""
        try:
            async with asyncio.timeout(CARD_MASTER_SECONDS):
                identity, _, problem = await self._member_identity(mapped)
                return None if problem else await self._member_clusters(identity)
        except Exception as exc:
            LOGGER.info("企微卡片没有带上集群列表: %s", type(exc).__name__)
            return None

    async def _reply(self, frame, content, finish=True, stream_id=None):
        if self.client and self.client.is_connected:
            await self.client.reply_stream(frame, stream_id or generate_req_id("stream"),
                                           _limit_stream(content), finish)

    async def _push(self, userid, body):
        """主动推送(aibot_send_msg),单聊的会话 ID 就是成员的 UserID。在后台任务里发,失败只记日志"""
        if not (self.client and self.client.is_connected):
            return
        try:
            await self.client.send_message(userid, {"chat_type": 1, **body})
        except Exception as exc:
            LOGGER.warning("企微主动推送失败: %s", str(exc)[:200])

    async def _update_card(self, frame, card):
        """更新被点的那张卡片(企微要求 5 秒内)。失败只记日志:SDK 把企微回执的 errcode 放在异常信息里"""
        if not (self.client and self.client.is_connected):
            return
        try:
            await self.client.update_template_card(frame, card)
        except Exception as exc:
            LOGGER.warning("企微卡片更新失败: %s", str(exc)[:200])

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
        reply = _StreamReply(self, frame)
        await reply.update("正在校验账号与访问权限...")
        mapped = await self._mapped_user(userid)
        text = ((body.get("text") or {}).get("content") or "").strip()
        if not text:
            await reply.finish("暂不支持空消息。")
            return
        try:
            await self._dispatch_text(reply, userid, mapped, text)
        except Exception as exc:
            LOGGER.warning("企业微信消息处理失败: %s", type(exc).__name__)
            await reply.finish("处理失败，请检查花折 AI 服务状态后重试。")

    async def _dispatch_text(self, reply, userid, mapped, text):
        command = text.lower()
        # 绑定、自报名字不需要账号权限,未绑定的成员也能用
        bind = BIND_COMMAND.match(unicodedata.normalize("NFKC", text))
        if bind:
            await reply.finish(await self._bind_by_code(userid, bind.group(1)))
            return
        named = SELF_NAME_COMMAND.match(text)
        if named:
            await reply.finish(await self._save_self_name(userid, named.group(1)))
            return
        if command in NEW_SESSION_COMMANDS | RESUME_COMMANDS:
            await self._switch_command(reply, userid, mapped, command)
            return
        identity, default_env, problem = await self._member_identity(mapped)
        if problem:
            await reply.finish(problem)
            return
        clusters = await self._member_clusters(identity)
        if not clusters:
            await reply.finish("当前花折账号没有任何 K8S 集群访问权限。")
            return
        if command in MENU_COMMANDS:
            notice, current = await self._settled_cluster(userid, identity, mapped, default_env, clusters)
            await reply.finish(_with_notice(notice, _cluster_menu(clusters, current)))
            await reply.card(_cluster_card(clusters, current))
            return
        switch = SWITCH_COMMAND.match(text)
        if switch and await self._switch_cluster(reply, userid, mapped, identity, default_env, clusters, switch):
            return
        await self._ask(reply, userid, mapped, identity, default_env, clusters, text)

    async def _settled_cluster(self, userid, identity, mapped, default_env, clusters):
        """查看集群前先定下对话(已经空闲超时的先换成新对话),返回 (回复开头的提示, 下一条提问会用的集群)"""
        _, notices, _, reset = await self._current_session(userid, identity)
        return "\n".join(notices), _usable(default_env if reset else mapped.get("current_env") or default_env, clusters)

    async def _switch_cluster(self, reply, userid, mapped, identity, default_env, clusters, match):
        """「切换集群 X」:X 可以是序号、集群名、别名或能唯一确定的一部分。有被拦下的提问就接着处理。
        返回 False 表示这其实是一条普通提问:「切换镜像到 v2」,或「切换到 beta 集群看一下 nginx 日志」这种带着问题的
        (当普通提问处理时同样会切过去再回答)"""
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
            # 先定下对话:当前对话已经空闲超时就先换成新对话,不然刚选的集群会在下一条提问开新对话时被换回默认集群
            _, notices, _, _ = await self._current_session(userid, identity)
            await self._set_current_env(userid, env)
            pending = await self._take_pending(userid)
            if not pending:
                await reply.finish(_with_notice("\n".join(notices), f"已切换到集群：{env}，之后的提问都在这个集群上执行。"))
                return True
            await self._ask(reply, userid, {**mapped, "current_env": env}, identity, default_env, clusters, pending,
                            status=f"已切换到集群 {env}，接着处理你刚才的问题...")
            return True
        if "集群" not in match.group(1) and not candidates:
            return False
        notice, current = await self._settled_cluster(userid, identity, mapped, default_env, clusters)
        hint = (f"「{target}」匹配到多个集群：{'、'.join(candidates)}，请写完整名称或序号。"
                if candidates else f"没有找到集群「{target}」。")
        await reply.finish(_with_notice(notice, hint + "\n\n" + _cluster_menu(clusters, current)))
        await reply.card(_cluster_card(clusters, current))
        return True

    async def _ask(self, reply, userid, mapped, identity, default_env, clusters, text, status=None):
        """一条提问:定下对话和集群后交给 AI。每个新对话从默认集群开始;还没有集群就先存下问题、
        发集群卡片让成员选,选好后接着处理。提问里提到另一个集群时切过去(集群名或别名)"""
        session_id, notices, active, reset = await self._current_session(userid, identity)
        if active:
            hint = BUSY_HINT + ("发送「新对话」可以放弃它并重新开始。" if active["status"] == "waiting_approval" else "")
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
            await reply.finish(_with_notice(notice, "请先选择这个对话要操作的集群，选好后接着处理你刚才的问题。"))
            await reply.card(_cluster_card(clusters, None, previous, "选好后接着处理你刚才的问题。"))
            return
        await self._run_message(reply, userid, identity, session_id, current, text,
                                notice=notice, status=status, clusters=clusters)

    async def _set_current_env(self, userid, env):
        await self.service.store.execute(
            "UPDATE kubedoor_ai_wecom_users SET current_env=$2,updated_at=now() WHERE wecom_userid=$1", userid, env,
        )

    async def _hold_question(self, userid, text):
        """存下因为还没选集群而没处理的提问(只留最近一条)"""
        await self.service.store.execute(
            "UPDATE kubedoor_ai_wecom_users SET pending_text=$2,pending_at=now() WHERE wecom_userid=$1", userid, redact(text),
        )

    async def _take_pending(self, userid):
        """取走被拦下的提问:并发时只有一处取得到,过期的不要"""
        store = self.service.store
        row = await store.one(
            "UPDATE kubedoor_ai_wecom_users SET pending_at=NULL WHERE wecom_userid=$1 "
            "AND pending_at > now() - make_interval(mins => $2) RETURNING pending_text", userid, PENDING_MINUTES,
        )
        await store.execute(
            "UPDATE kubedoor_ai_wecom_users SET pending_text=NULL,pending_at=NULL WHERE wecom_userid=$1 AND pending_text IS NOT NULL "
            "AND (pending_at IS NULL OR pending_at <= now() - make_interval(mins => $2))", userid, PENDING_MINUTES,
        )
        return (row or {}).get("pending_text")

    async def _continue_pending(self, userid, text):
        """在卡片里选好集群后接着处理被拦下的提问;卡片事件不能回复消息,结果主动推送"""
        reply = _PushReply(self, userid)
        try:
            mapped = await self._mapped_user(userid)
            identity, default_env, problem = await self._member_identity(mapped)
            if problem:
                await reply.finish(problem)
                return
            clusters = await self._member_clusters(identity)
            if not clusters:
                await reply.finish("当前花折账号没有任何 K8S 集群访问权限。")
                return
            await self._ask(reply, userid, mapped, identity, default_env, clusters, text)
        except Exception as exc:
            LOGGER.warning("企微接着处理提问失败: %s", type(exc).__name__)
            with suppress(Exception):
                await reply.finish("处理失败，请重新发送问题。")

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
    async def _open_session(tx, userid, identity, previous_id, reset=False, env=None):
        """新建对话并设为当前对话,原来的对话记为上一个;旧对话保留不删。
        reset 时新对话的集群换成 env(None = 默认集群),否则沿用成员当前选的集群"""
        session_id = str(uuid.uuid4())
        # channel_key 每个对话唯一;老对话的 channel_key 是 "{userid}:{username}"
        await tx.execute(
            "INSERT INTO kubedoor_ai_sessions(id,username,title,channel,channel_key) VALUES($1::uuid,$2,'企业微信会话','wecom',$3)",
            session_id, identity.username, f"{userid}:{identity.username}:{session_id}",
        )
        # 只记会话,不改 username:用默认权限的成员仍是未绑定,管理员同时改绑也不会被覆盖回去
        if reset:
            # 新对话:集群换成 env,上一个对话里被拦下的提问作废
            await tx.execute(
                "UPDATE kubedoor_ai_wecom_users SET session_id=$2::uuid,previous_session_id=$3::uuid,current_env=$4,"
                "pending_text=NULL,pending_at=NULL,updated_at=now() WHERE wecom_userid=$1", userid, session_id, previous_id, env,
            )
        else:
            await tx.execute(
                "UPDATE kubedoor_ai_wecom_users SET session_id=$2::uuid,previous_session_id=$3::uuid,updated_at=now() WHERE wecom_userid=$1",
                userid, session_id, previous_id,
            )
        return session_id

    async def _current_session(self, userid, identity):
        """返回 (当前对话, 回复开头的提示, 进行中的任务, 是否换了新对话的集群)。空闲超时后自动开新对话,
        新对话从默认集群开始;有进行中的任务或没过期的审批时不切换;只剩过期审批的任务先停掉,不让对话卡住"""
        notices = []
        async with self.service.store.transaction() as tx:
            member, idle, current = await self._session_state(tx, userid, identity)
            if current is None:
                # 第一次提问,或原来的对话不能用了(改绑、换了默认权限账号):成员刚选的集群照常用
                return await self._open_session(tx, userid, identity, member.get("session_id")), notices, None, False
            run = await self._active_run(tx, current["id"])
            if run and run["status"] == "waiting_approval" and not run["approvable"]:
                # 等待审批的任务不在进程里运行,停止它只是几条更新,放在锁里免得并发消息重复停止
                await self.service.runtime.cancel(run, identity)
                notices.append("上一个待审批的操作已过期，已停止该任务。")
                run = None
            # 空对话(比如刚发过「新对话」)空闲再久也不用换,换了反而把真正的上一个对话挤掉
            if run is None and idle > 0 and current["idle"] and current["rounds"]:
                session_id = await self._open_session(tx, userid, identity, current["id"], reset=True)
                notices.append(f"距上次对话已超过 {_idle_text(idle)}，已开始新对话（发送「继续上次」可回到之前的对话）")
                return session_id, notices, None, True
            return current["id"], notices, run, False

    async def _start_new_session(self, userid, identity, env=None, wait=True):
        """「新对话」:开一个新对话(集群换成 env,None = 默认集群),再停止原对话里进行中的任务。
        wait=False 时停止任务放到后台:点卡片按钮要在 5 秒内更新卡片"""
        async with self.service.store.transaction() as tx:
            member, _, current = await self._session_state(tx, userid, identity)
            run = await self._active_run(tx, current["id"]) if current else None
            if current and not current["rounds"] and run is None:
                # 当前对话还没有消息就直接用它,保留真正的上一个对话给「继续上次」;集群照样换成新对话的
                await tx.execute("UPDATE kubedoor_ai_wecom_users SET current_env=$2,pending_text=NULL,pending_at=NULL,updated_at=now() "
                                 "WHERE wecom_userid=$1", userid, env)
            else:
                await self._open_session(tx, userid, identity, member.get("session_id"), reset=True, env=env)
        if not run:
            return "已开始新对话。"
        # 停止运行中的任务可能要等 agent 侧取消,放在锁和事务外面,不挡这个成员的其他消息
        if wait:
            await self.service.runtime.cancel(run, identity)
            return "已开始新对话。上一个对话里未完成的任务已停止。"
        self._spawn(self._cancel_quietly(run, identity), "wecom-cancel")
        return "已开始新对话。上一个对话里未完成的任务正在停止。"

    async def _cancel_quietly(self, run, identity):
        """后台停止任务,失败只记日志"""
        try:
            await self.service.runtime.cancel(run, identity)
        except Exception as exc:
            LOGGER.warning("企微停止任务失败: %s", type(exc).__name__)

    async def _switch_identity(self, mapped):
        """切换对话只用本地记着的花折账号名,不调 master:切换不授予任何权限,下一次提问照常完整校验。
        返回 (只有账号名的身份, 不能使用时回给成员的提示)"""
        username = mapped.get("username") or (await self.unbound_setting())["unbound_username"]
        if not username:
            return None, UNBOUND_HINT
        return Identity(username, "none"), None

    async def _switch_command(self, reply, userid, mapped, command):
        """「新对话」「继续上次」及别名。新对话没有默认集群时附上集群卡片"""
        identity, problem = await self._switch_identity(mapped)
        if problem:
            await reply.finish(problem)
            return
        if command in RESUME_COMMANDS:
            await reply.finish(await self._resume_previous_session(userid, identity))
            return
        text = await self._start_new_session(userid, identity)
        default_env = await self._default_env(mapped)
        if default_env:
            await reply.finish(f"{text}这个对话使用默认集群 {default_env}。")
            return
        clusters = await self._quick_clusters(mapped)
        if not clusters:
            await reply.finish(text + "发送「集群」选择这个对话要操作的集群。")
            return
        await reply.finish(text + "请在下面的卡片里选择这个对话要操作的集群。")
        await reply.card(_cluster_card(clusters, None, mapped.get("current_env")))

    async def _resume_previous_session(self, userid, identity):
        """「继续上次」:和上一个对话互换;上一个对话必须属于同一个花折账号"""
        async with self.service.store.transaction() as tx:
            member, _, _ = await self._session_state(tx, userid, identity)
            previous = await self._own_session(tx, member.get("previous_session_id"), userid, identity)
            if previous is None:
                return "没有可以回到的对话。"
            env = await self._switch_to(tx, userid, previous["id"], member.get("session_id"))
        where = f"，集群：{env}" if env else ""
        return f"已回到上一个对话（最后活跃：{_beijing_time(previous['updated_at'])}，共 {previous['rounds']} 轮{where}）。"

    async def _continue_session(self, userid, identity, session_id):
        """欢迎卡片的「继续上次对话」:回到卡片上显示的那个对话(可能已被空闲规则切走),返回 (说明, 恢复的集群)"""
        async with self.service.store.transaction() as tx:
            member, _, _ = await self._session_state(tx, userid, identity)
            target = await self._own_session(tx, session_id, userid, identity)
            if target is None:
                return "该对话已不可用，直接发送问题即可开始新对话。", None
            env = await self._switch_to(tx, userid, target["id"], member.get("session_id"))
        return f"已回到上次的对话（共 {target['rounds']} 轮），直接发送问题即可继续。", env

    @staticmethod
    async def _switch_to(tx, userid, session_id, current_id):
        """切回已有对话,集群恢复成那个对话最后一次提问用的集群(没提问过就回到默认集群),返回恢复的集群。
        它早已超过空闲超时,要刷新最后活跃时间,否则下一条消息又会被切到新对话"""
        await tx.execute("UPDATE kubedoor_ai_sessions SET updated_at=now() WHERE id=$1::uuid", session_id)
        if session_id == current_id:
            return None
        row = await tx.one(
            "SELECT scope->>'env' AS env FROM kubedoor_ai_runs WHERE session_id=$1::uuid ORDER BY created_at DESC LIMIT 1", session_id,
        )
        env = (row or {}).get("env")
        await tx.execute(
            "UPDATE kubedoor_ai_wecom_users SET session_id=$2::uuid,previous_session_id=$3::uuid,current_env=$4,updated_at=now() "
            "WHERE wecom_userid=$1", userid, session_id, current_id, env,
        )
        return env

    async def _run_message(self, reply, userid, identity, session_id, env, text, notice="", status=None,
                           clusters=None, switched=False):
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
            await reply.finish(_with_notice(notice, BUSY_HINT))
            return
        if await self._session_purged(session_id):
            # 管理员恰好在这条消息定下对话之后清理了这个成员:不在已清理的对话上跑(它的检查点不会再有人清)
            await self.service.runtime.cancel(run, identity)
            await reply.finish("你的企业微信成员记录刚被管理员清理，请重新发送问题。")
            return
        # 本轮上下文里列出成员可访问的集群和别名,AI 发现问的是另一个集群时请求切换(gateway.request_cluster_switch)
        run["_clusters"], run["_switched"] = clusters or [], switched
        clean = redact(text, (provider.api_key,))
        await self.service.store.message(run, "user", clean)
        self.service.runtime.start(run, identity, provider, clean)
        target = await self._watch_run(reply, userid, identity, run["id"], 0, env=env,
                                       status=status or f"正在分析请求（集群：{env}）...", notice=notice,
                                       allow_switch=not switched)
        if target:
            # 切到那个集群重新处理:新的一轮只说明切换,问题本身就在上一条用户消息里
            await self._set_current_env(userid, target)
            await self._run_message(reply, userid, identity, session_id, target,
                                    f"已切换到集群 {target}，请在这个集群上回答我上一条消息里的问题。",
                                    notice=notice, status=f"已切换到集群 {target}，正在分析请求...",
                                    clusters=clusters, switched=True)

    def _provider_settings(self):
        return {key: self.config.get(key) for key in PROVIDER_KEYS}

    async def _session_purged(self, session_id):
        row = await self.service.store.one(
            "SELECT checkpoint_purged_at IS NOT NULL AS purged FROM kubedoor_ai_sessions WHERE id=$1::uuid", session_id,
        )
        return bool((row or {}).get("purged"))

    async def _watch_run(self, reply, userid, identity, run_id, after, env=None, status="正在分析请求...",
                         notice="", allow_switch=False):
        """把任务的进度和结果发给成员。AI 请求切换集群时返回目标集群:本轮已经结束,由调用方在那个集群上重新提问"""
        content = status
        await reply.update(_with_notice(notice, content))
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
                    await reply.finish(_with_notice(notice, (content + "\n\n" + _approval_text(data)).strip()))
                    await self._send_approval(reply, userid, identity, run_id, data, cursor)
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
            if updated and (time.monotonic() - last_send >= 0.7 or not rows):
                await reply.update(_limit_stream(_with_notice(notice, content)))
                last_send = time.monotonic()
            run = await self.service.store.one("SELECT status,event_seq,usage,switch_env FROM kubedoor_ai_runs WHERE id=$1::uuid", run_id)
            if not run or run["status"] in ("completed", "failed", "cancelled") and cursor >= run["event_seq"]:
                # done 事件在状态改成 completed 之后才写:只看到状态时,切换目标从任务行里取
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

    async def _send_approval(self, reply, userid, identity, run_id, event, after):
        action_id = event.get("action_id")
        action = await self.service.store.one(
            "SELECT * FROM kubedoor_ai_actions WHERE id=$1::uuid AND run_id=$2::uuid AND state='pending'",
            action_id, run_id,
        )
        run = await self.service.store.one("SELECT session_id,scope FROM kubedoor_ai_runs WHERE id=$1::uuid", run_id)
        if not action or not run:
            await reply.say("审批信息已过期，请重新发起操作。")
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
        await reply.card(card)

    async def _on_card_event(self, frame):
        self._spawn(self._handle_card_event(frame), "wecom-card")

    async def _on_enter_chat(self, frame):
        self._spawn(self._handle_enter_chat(frame), "wecom-welcome")

    async def _handle_enter_chat(self, frame):
        """成员当天首次进入单聊:5 秒内回欢迎卡片。集群下拉框要问 master,取不到就不放,权限在提问时再校验"""
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
            return {"msgtype": "text", "text": {"content": "你好，我是花折 AI 助手。" + UNBOUND_HINT}}
        default_env = mapped.get("default_env") if mapped.get("username") else setting["unbound_default_env"]
        last = await self._own_session(self.service.store, mapped.get("session_id"), userid, Identity(username, "none"))
        last = last if last and last["rounds"] else None
        buttons = [{"text": "开始新对话", "key": "session_new", "style": 1}]
        if last:
            buttons.append({"text": "继续上次对话", "key": "session_resume", "style": 2})
        card = {"card_type": "button_interaction",
                "main_title": {"title": "花折 AI 助手", "desc": f"默认集群：{default_env}" if default_env else "新对话要先选集群"},
                "sub_title_text": f"上次对话：{_friendly_time(last['updated_at'])}，共 {last['rounds']} 轮" if last else "直接发送问题即可开始对话。",
                "button_list": buttons,
                # 「继续上次对话」回到卡片上显示的这个对话,按钮事件从 task_id 取回它
                "task_id": f"session_{last['id'] if last else 'none'}_{secrets.token_hex(6)}"}
        clusters = await self._quick_clusters(mapped)
        if clusters:
            suggestion = _usable(default_env, clusters) or mapped.get("current_env")
            card["button_selection"] = _cluster_selection(clusters, suggestion, "新对话的集群")
        return {"msgtype": "template_card", "template_card": card}

    async def _handle_card_event(self, frame):
        """卡片按钮:处理完把这张卡片换成结果(企微要求 5 秒内),后续结果用主动推送"""
        body = frame.get("body") or {}
        userid = ((body.get("from") or {}).get("userid"))
        event = _card_event(body)
        task_id, key = event["task_id"], event["event_key"]
        # noop 是处理过的卡片上显示结果的按钮,点了不用处理
        if not isinstance(userid, str) or not userid or not isinstance(key, str) or key == "noop":
            return
        if not await self._claim_message(body, userid, "card"):
            return
        try:
            if key in ("approve", "reject"):
                await self._approval_card(frame, userid, task_id, key)
            elif key in ("session_new", "session_resume"):
                await self._session_card(frame, userid, task_id, key, _selected(event, "env"))
            elif key == "env_pick":
                await self._pick_card(frame, userid, task_id, _selected(event, "env"))
        except Exception as exc:
            LOGGER.warning("企微卡片事件处理失败: %s", type(exc).__name__)
            await self._update_card(frame, _result_card(task_id, "处理失败", detail="请稍后重试，或直接发消息给我。", status="失败"))

    async def _session_card(self, frame, userid, task_id, choice, env):
        """欢迎卡片的「开始新对话」(用下拉框选的集群)「继续上次对话」(恢复那个对话的集群)"""
        mapped = await self._mapped_user(userid)
        identity, problem = await self._switch_identity(mapped)
        if problem:
            await self._update_card(frame, _result_card(task_id, "无法切换对话", detail=problem, status="未完成"))
            return
        if choice == "session_new":
            default_env = await self._default_env(mapped)
            # 选的就是默认集群时不写进当前集群:管理员之后改默认集群,这个对话照样跟着变
            detail = await self._start_new_session(userid, identity, None if env == default_env else env, wait=False)
            env = env or default_env
            detail += "直接发送问题即可。" if env else "发送问题后会请你先选择集群。"
            await self._update_card(frame, _result_card(task_id, "已开始新对话", f"集群：{env}" if env else "未选择集群", detail))
            return
        detail, env = await self._continue_session(userid, identity, _card_session(task_id))
        await self._update_card(frame, _result_card(task_id, "继续上次对话", f"集群：{env}" if env else None, detail))

    async def _pick_card(self, frame, userid, task_id, env):
        """集群卡片的「切换到所选集群」:有被拦下的提问就接着处理(结果推送)。
        这里不调 master,所选集群的权限在处理提问时校验,没权限会再发一张集群卡片"""
        mapped = await self._mapped_user(userid)
        identity, problem = await self._switch_identity(mapped)
        if problem or not env:
            await self._update_card(frame, _result_card(task_id, "没有切换集群", detail=problem or "请在下拉框里选择集群。", status="未完成"))
            return
        # 先定下对话:当前对话已经空闲超时就先换成新对话,不然选的集群会在下一条提问开新对话时被换回默认集群
        await self._current_session(userid, identity)
        await self._set_current_env(userid, env)
        pending = await self._take_pending(userid)
        detail = f"正在处理你刚才的问题：{_clip(pending, 40)}，结果稍后发送。" if pending else "之后的提问都在这个集群上执行。"
        await self._update_card(frame, _result_card(task_id, "已切换集群", env, detail, "已切换"))
        if pending:
            self._spawn(self._continue_pending(userid, pending), "wecom-pending")

    async def _approval_card(self, frame, userid, task_id, choice):
        store = self.service.store
        approval = await store.one(
            "SELECT * FROM kubedoor_ai_wecom_approvals WHERE task_id=$1 AND expires_at>now()", task_id,
        ) if isinstance(task_id, str) else None
        if not approval or approval["wecom_userid"] != userid:
            await self._update_card(frame, _result_card(task_id, "审批卡片已失效", detail="该审批已处理、已过期或不属于当前账号。", status="已失效"))
            return
        mapped = await self._mapped_user(userid)
        if (mapped.get("username") or (await self.unbound_setting())["unbound_username"]) != approval["username"]:
            await self._update_card(frame, _result_card(task_id, "审批卡片已失效", detail="账号绑定已变更，该审批不能继续。", status="已失效"))
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + CARD_SECONDS
        try:
            async with asyncio.timeout_at(deadline):
                identity = await self._identity(approval["username"])
            env = (await store.one(
                "SELECT scope->>'env' AS env FROM kubedoor_ai_runs WHERE id=$1::uuid", approval["run_id"],
            ) or {}).get("env")
            if not identity.can_read(env):
                await self._update_card(frame, _result_card(task_id, "不能批准", f"集群：{env}", "该账号已失去目标集群权限，不能批准此操作。", "已失效"))
                return
            provider = Provider.parse(self._provider_settings())
            decision = asyncio.ensure_future(self.service.decision(identity, approval["run_id"], {
                "action_id": str(approval["action_id"]), "decision": choice, "provider": provider.__dict__,
            }))
            try:
                # 恢复任务前还要问 master 集群状态:master 慢时决定照常生效,卡片先写「处理中」,结果推送
                result = await asyncio.wait_for(asyncio.shield(decision), max(0.1, deadline - loop.time()))
            except TimeoutError:
                await self._update_card(frame, _result_card(task_id, "已收到", f"集群：{env}", "正在处理，结果稍后发送。", "处理中"))
                self._spawn(self._finish_decision(userid, identity, approval, decision, env), "wecom-decision")
                return
            await store.execute("DELETE FROM kubedoor_ai_wecom_approvals WHERE task_id=$1", task_id)
        except (AIError, TimeoutError) as exc:
            detail = str(exc) if isinstance(exc, AIError) else "校验账号超时，请稍后再点一次。"
            await self._update_card(frame, _result_card(task_id, "审批未完成", detail=detail, status="未完成"))
            return
        approved = choice == "approve"
        running = result.get("status") == "running"
        detail = ("正在执行，结果稍后发送。" if approved else "AI 会接着说明情况，稍后发送。") if running else None
        await self._update_card(frame, _result_card(task_id, "已批准" if approved else "已拒绝", f"集群：{env}", detail,
                                                    "已批准" if approved else "已拒绝"))
        if running:
            self._spawn(self._watch_run(_PushReply(self, userid), userid, identity, approval["run_id"],
                                        approval["resume_after_seq"], env=env), "wecom-run-resume")

    async def _finish_decision(self, userid, identity, approval, decision, env):
        """没赶上 5 秒更新卡片的审批:等决定处理完,结果推送给成员"""
        reply = _PushReply(self, userid)
        try:
            result = await decision
            await self.service.store.execute("DELETE FROM kubedoor_ai_wecom_approvals WHERE task_id=$1", approval["task_id"])
        except AIError as exc:
            await reply.finish(f"审批没有完成：{exc}")
            return
        except Exception as exc:
            LOGGER.warning("企微审批处理失败: %s", type(exc).__name__)
            await reply.finish("审批处理失败，请在花折 AI 会话中核验当前状态。")
            return
        if result.get("status") == "running":
            await self._watch_run(reply, userid, identity, approval["run_id"], approval["resume_after_seq"], env=env)

    async def _bind_by_code(self, userid, code):
        """成员发「绑定 123456」:绑定码是花折用户在 Web 上为自己的账号生成的,对上就绑定到那个账号"""
        store = self.service.store
        async with store.transaction() as tx:
            await tx.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", f"wecom:{userid}")
            member = await tx.one(
                "SELECT bind_failures,bind_failed_at > now() - make_interval(mins => $2) AS recent "
                "FROM kubedoor_ai_wecom_users WHERE wecom_userid=$1", userid, BIND_CODE_MINUTES,
            ) or {}
            if member.get("recent") and (member.get("bind_failures") or 0) >= BIND_MAX_FAILURES:
                return f"绑定码输错次数太多，请 {BIND_CODE_MINUTES} 分钟后再试。"
            total = await tx.one(
                "SELECT coalesce(sum(bind_failures),0) AS failures FROM kubedoor_ai_wecom_users "
                "WHERE bind_failed_at > now() - make_interval(mins => $1)", BIND_CODE_MINUTES,
            ) or {}
            if (total.get("failures") or 0) >= BIND_MAX_TOTAL_FAILURES:
                LOGGER.warning("企微绑定码 %s 分钟内输错 %s 次,暂停自助绑定", BIND_CODE_MINUTES, total.get("failures"))
                return f"绑定暂时不可用，请 {BIND_CODE_MINUTES} 分钟后再试或联系管理员。"
            row = await tx.one("DELETE FROM kubedoor_ai_wecom_bind_codes WHERE code=$1 AND expires_at>now() RETURNING username", code)
            if not row:
                await tx.execute(
                    "UPDATE kubedoor_ai_wecom_users SET bind_failures=CASE WHEN bind_failed_at > now() - make_interval(mins => $2) "
                    "THEN bind_failures+1 ELSE 1 END,bind_failed_at=now() WHERE wecom_userid=$1", userid, BIND_CODE_MINUTES,
                )
                return "绑定码不对或已过期，请在花折 Web 右上角用户菜单「绑定企业微信」重新生成。"
        username = row["username"]
        try:
            await self._identity(username)
        except AIError:
            return f"花折账号 {username} 不存在、已停用或不可用，不能绑定。"
        # 换了账号:原来选的默认集群和当前集群不再沿用,被拦下的提问作废;旧对话属于原来的账号,下次提问自动开新对话
        await store.execute(
            "UPDATE kubedoor_ai_wecom_users SET default_env=CASE WHEN username IS NOT DISTINCT FROM $2 THEN default_env END,"
            "current_env=CASE WHEN username IS NOT DISTINCT FROM $2 THEN current_env END,username=$2,bound_by=$2,bound_at=now(),"
            "pending_text=NULL,pending_at=NULL,bind_failures=0,bind_failed_at=NULL,updated_at=now() WHERE wecom_userid=$1", userid, username,
        )
        return f"已绑定花折账号 {username}，之后按这个账号的权限使用机器人。"

    async def _save_self_name(self, userid, name):
        await self.service.store.execute(
            "UPDATE kubedoor_ai_wecom_users SET self_name=$2,updated_at=now() WHERE wecom_userid=$1", userid, name,
        )
        return f"已记下你的名字「{name}」，管理员在花折里能看到，方便为你绑定账号（只用来辨认，不影响权限）。"

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
            "SELECT u.wecom_userid,u.username,u.default_env,u.current_env,u.remark,u.self_name,u.bound_by,u.bound_at,"
            "u.first_seen_at,u.last_seen_at,u.updated_at, "
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
        # 改绑到别的账号:重新记绑定人,原来选的集群不再沿用、被拦下的提问作废;只改默认集群时其他保持原样。
        # 默认集群不写进当前集群:没切换过集群的成员,管理员改了默认集群立即生效
        changed = "kubedoor_ai_wecom_users.username IS DISTINCT FROM EXCLUDED.username"
        await self.service.store.execute(
            "INSERT INTO kubedoor_ai_wecom_users(wecom_userid,username,default_env,bound_by,bound_at) VALUES($1,$2,$3,$4,now()) "
            "ON CONFLICT(wecom_userid) DO UPDATE SET username=EXCLUDED.username,default_env=EXCLUDED.default_env,"
            f"bound_by=CASE WHEN {changed} THEN EXCLUDED.bound_by ELSE kubedoor_ai_wecom_users.bound_by END,"
            f"bound_at=CASE WHEN {changed} THEN now() ELSE kubedoor_ai_wecom_users.bound_at END,"
            f"current_env=CASE WHEN {changed} THEN NULL ELSE kubedoor_ai_wecom_users.current_env END,"
            f"pending_text=CASE WHEN {changed} THEN NULL ELSE kubedoor_ai_wecom_users.pending_text END,"
            f"pending_at=CASE WHEN {changed} THEN NULL ELSE kubedoor_ai_wecom_users.pending_at END,"
            "updated_at=now()",
            userid.strip(), username, env, identity.username,
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
            f"UPDATE kubedoor_ai_wecom_users SET {UNBIND_COLUMNS},updated_at=now() WHERE wecom_userid=$1", userid,
        )

    async def save_remark(self, userid, raw):
        """管理员给成员写备注(比如姓名),只用于辨认"""
        remark = raw.get("remark")
        if remark is not None and not isinstance(remark, str):
            raise AIError("备注格式无效")
        remark = (remark or "").strip() or None
        if remark and len(remark) > 100:
            raise AIError("备注最多 100 个字")
        row = await self.service.store.one(
            "UPDATE kubedoor_ai_wecom_users SET remark=$2,updated_at=now() WHERE wecom_userid=$1 RETURNING wecom_userid", userid, remark,
        )
        if not row:
            raise AIError("企微成员不存在", 404, "not_found")
        return {"remark": remark}

    async def purge_member(self, userid):
        """清理企微成员,他下次发消息时按新成员接入:停掉进行中的任务,删掉待审批和成员记录(绑定、集群、对话指针);
        他的企微对话不能再继续(删检查点并打标记,旧欢迎卡片也回不去),会话和消息记录保留用于审计"""
        store = self.service.store
        async with store.transaction() as tx:
            await tx.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", f"wecom:{userid}")
            if not await tx.one("DELETE FROM kubedoor_ai_wecom_users WHERE wecom_userid=$1 RETURNING wecom_userid", userid):
                raise AIError("企微成员不存在", 404, "not_found")
            await tx.execute("DELETE FROM kubedoor_ai_wecom_approvals WHERE wecom_userid=$1", userid)
            sessions = await tx.fetch(
                "UPDATE kubedoor_ai_sessions SET checkpoint_purged_at=now() WHERE channel='wecom' AND starts_with(channel_key,$1) "
                "AND checkpoint_purged_at IS NULL RETURNING id", f"{userid}:",
            )
            runs = await tx.fetch(
                "SELECT r.id,r.status,r.username FROM kubedoor_ai_runs r JOIN kubedoor_ai_sessions s ON s.id=r.session_id "
                "WHERE s.channel='wecom' AND starts_with(s.channel_key,$1) AND r.status IN ('running','waiting_approval')", f"{userid}:",
            )
        # 停止运行中的任务可能要等 agent 侧取消,放在锁和事务外面
        for run in runs:
            await self._cancel_quietly(run, Identity(run["username"], "none"))
        for row in sessions:
            try:
                await self.service.runtime.checkpointer.adelete_thread(row["id"])
            except Exception as exc:
                LOGGER.warning("清理企微成员的对话检查点失败: %s", type(exc).__name__)
        return {"success": True, "sessions": len(sessions), "stopped": len(runs)}

    async def binding(self, identity):
        """右上角用户菜单「绑定企业微信」:当前花折账号绑定的企微成员,以及机器人是否可用"""
        members = await self.service.store.fetch(
            "SELECT wecom_userid,self_name,bound_by,bound_at,last_seen_at FROM kubedoor_ai_wecom_users WHERE username=$1 "
            "ORDER BY bound_at DESC NULLS LAST,last_seen_at DESC", identity.username,
        )
        return {"enabled": bool(self.config and self.config.get("enabled")),
                "connected": bool(self.status.get("authenticated")),
                "members": members, "code_minutes": BIND_CODE_MINUTES}

    async def create_bind_code(self, identity):
        """为当前花折账号生成自助绑定码:同时只有一个,BIND_CODE_MINUTES 分钟内有效,用过即删"""
        if not (self.config and self.config.get("enabled")):
            raise AIError("管理员还没有启用企业微信机器人", 409, "wecom_disabled")
        store = self.service.store
        await store.execute("DELETE FROM kubedoor_ai_wecom_bind_codes WHERE expires_at<now()")
        for _ in range(5):
            code = f"{secrets.randbelow(10 ** 6):06d}"
            try:
                row = await store.one(
                    "INSERT INTO kubedoor_ai_wecom_bind_codes(code,username,expires_at) VALUES($1,$2,now()+make_interval(mins => $3)) "
                    "ON CONFLICT(username) DO UPDATE SET code=EXCLUDED.code,expires_at=EXCLUDED.expires_at,created_at=now() "
                    "RETURNING code,expires_at", code, identity.username, BIND_CODE_MINUTES,
                )
            except asyncpg.UniqueViolationError:
                continue   # 和别人正在用的码撞了,换一个
            # 有效分钟数一起给前端:倒计时按浏览器自己的时钟算,不拿服务器时间比
            return {**row, "minutes": BIND_CODE_MINUTES}
        raise AIError("生成绑定码失败，请重试", 503, "bind_code_busy")

    async def unbind_self(self, identity, userid):
        """花折用户解除自己账号绑定的企微成员"""
        row = await self.service.store.one(
            f"UPDATE kubedoor_ai_wecom_users SET {UNBIND_COLUMNS},updated_at=now() WHERE wecom_userid=$1 AND username=$2 "
            "RETURNING wecom_userid", userid, identity.username,
        )
        if not row:
            raise AIError("没有找到这个企微成员的绑定", 404, "not_found")
        return {"success": True}


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


NAME_SEPARATORS = re.compile(r"[\s\-_.]+")


def _alias_key(text):
    """集群名和别名的比较键:去掉空白和 -_.、不区分大小写(master 保存别名时按同一规则查重)"""
    return NAME_SEPARATORS.sub("", text).lower()


def _cluster_names(item):
    return [item["env"], *(item.get("aliases") or [])]


def _usable(env, clusters):
    """成员还能用的集群:不在可访问的集群里(没权限了、集群没了)就当没选"""
    return env if env and any(item["env"] == env for item in clusters) else None


def _name_pattern(name):
    """集群名或别名在提问里的写法:各段之间的空格和 -_. 可有可无,不区分大小写;以字母数字开头/结尾时,
    前后不能紧挨字母数字或 -_.(cassmall-kunlun 不会认成 cassmall-kunlun-2,prod 不会认成 production)"""
    parts = [part for part in NAME_SEPARATORS.split(name) if part]
    if not parts:
        return None
    body = r"[\s\-_.]*".join(re.escape(part) for part in parts)
    head = r"(?<![A-Za-z0-9_.-])" if re.match(r"[A-Za-z0-9]", parts[0]) else ""
    tail = r"(?![A-Za-z0-9_.-])" if re.search(r"[A-Za-z0-9]$", parts[-1]) else ""
    return re.compile(head + body + tail, re.IGNORECASE)


def _pick_cluster(target, clusters):
    """按序号、集群名或别名(比较时忽略大小写、空格和 -_.)、能唯一确定的一部分找集群,返回 (集群, 有歧义时的候选)"""
    names = [item["env"] for item in clusters]
    if target.isdigit():
        index = int(target)
        return (names[index - 1] if 1 <= index <= len(names) else None), []
    key = _alias_key(target)
    if not key:
        return None, []
    for item in clusters:
        if key in {_alias_key(name) for name in _cluster_names(item)}:
            return item["env"], []
    partial = [item["env"] for item in clusters if any(key in _alias_key(name) for name in _cluster_names(item))]
    return (partial[0], []) if len(partial) == 1 else (None, partial)


def _cluster_spans(text, clusters):
    """提问里说到集群的位置 [(开始, 结束, 集群)],集群名或别名都算(写法见 _name_pattern)。
    一处写法被另一个更长的写法盖住时只算长的(别名「昆仑」和「beta 昆仑」都命中时算后者)"""
    spans = []
    for item in clusters:
        for name in _cluster_names(item):
            pattern = _name_pattern(name)
            if pattern:
                spans += [(match.start(), match.end(), item["env"]) for match in pattern.finditer(text)]
    return [(start, end, env) for start, end, env in spans
            if not any(s <= start and end <= e and e - s > end - start for s, e, _ in spans)]


def _mentioned_cluster(text, clusters):
    """提问里提到的集群:只认出一个集群才切换,认不出或说到多个的交给 AI 判断"""
    found = {env for _, _, env in _cluster_spans(text, clusters)}
    return found.pop() if len(found) == 1 else None


def _cluster_menu(clusters, current):
    lines = [f"当前集群：{current or '未选择'}", "可访问的集群："]
    for index, item in enumerate(clusters, 1):
        tags = [PERMISSION_LABELS.get(item.get("permission"), item.get("permission"))]
        if item.get("online") is False:
            tags.append("agent 离线")
        mark = "（当前）" if item["env"] == current else ""
        aliases = "（" + "、".join(item["aliases"]) + "）" if item.get("aliases") else ""
        lines.append(f"{index}. {item['env']}{aliases} · {' · '.join(tags)}{mark}")
    lines.append("发送「切换集群 序号、名称或别名」切换，如「切换集群 2」；提问时说到别的集群也会自动切换过去。")
    lines.append("发送「新对话」重新开始，「继续上次」回到上一个对话。")
    return "\n".join(lines)


def _card_event(body):
    """模板卡片事件的内容在 body.event.template_card_event 里;按钮 key 兼容文档参数表里的 eventkey 写法"""
    event = body.get("event") if isinstance(body.get("event"), dict) else {}
    detail = event.get("template_card_event") if isinstance(event.get("template_card_event"), dict) else {}
    return {"task_id": detail.get("task_id"), "event_key": detail.get("event_key") or detail.get("eventkey"),
            "selected_items": detail.get("selected_items")}


def _selected(event, question_key):
    """卡片下拉框里选中的选项 id(单选):selected_items.selected_item[].option_ids.option_id[]"""
    items = event.get("selected_items")
    items = items.get("selected_item") if isinstance(items, dict) else None
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and item.get("question_key") == question_key:
            ids = item.get("option_ids")
            ids = ids.get("option_id") if isinstance(ids, dict) else None
            if isinstance(ids, list) and ids and isinstance(ids[0], str):
                return ids[0]
    return None


def _cluster_selection(clusters, selected=None, title="选择集群"):
    """卡片里的集群下拉框,选项 id 就是集群名;最多 10 项,超出时保证建议的集群在里面"""
    names = [item["env"] for item in clusters if len(item["env"].encode()) <= 128]
    if selected in names[CARD_OPTIONS:]:
        names.remove(selected)
        names.insert(0, selected)
    names = names[:CARD_OPTIONS]
    selection = {"question_key": "env", "title": title, "option_list": [{"id": name, "text": name} for name in names]}
    if selected in names:
        selection["selected_id"] = selected
    return selection


def _cluster_card(clusters, current, suggestion=None, hint=None):
    """选择集群的卡片:在下拉框里选好后点按钮切换;suggestion 是没有当前集群时默认选中的集群"""
    tips = [hint] if hint else []
    if len(clusters) > CARD_OPTIONS:
        tips.append(f"下拉框只列出 {CARD_OPTIONS} 个集群，其它集群请发送「切换集群 名称」。")
    card = {"card_type": "button_interaction",
            "main_title": {"title": "选择集群", "desc": f"当前集群：{current or '未选择'}"},
            "button_selection": _cluster_selection(clusters, current or suggestion),
            "button_list": [{"text": "切换到所选集群", "key": "env_pick", "style": 1}],
            "task_id": f"env_{secrets.token_hex(8)}"}
    if tips:
        card["sub_title_text"] = " ".join(tips)
    return card


def _result_card(task_id, title, desc=None, detail=None, status="已完成"):
    """处理过的卡片:标题和说明写结果,按钮只显示状态(点了不处理)。
    text_notice 卡片必须带跳转链接(card_action),所以结果也用按钮交互卡片"""
    card = {"card_type": "button_interaction", "main_title": {"title": title},
            "button_list": [{"text": status, "key": "noop", "style": 2}], "task_id": task_id}
    if desc:
        card["main_title"]["desc"] = _clip(desc, 30)
    if detail:
        card["sub_title_text"] = _clip(detail, 112)
    return card


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
