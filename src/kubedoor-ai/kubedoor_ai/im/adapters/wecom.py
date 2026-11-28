"""WeCom (企业微信) WSClient adapter implementing IMAdapter & StreamHandle."""
from __future__ import annotations

import asyncio
import logging
import secrets
from contextlib import suppress
from typing import Any

from aibot import WSClient, WSClientOptions, generate_req_id

from ...domain import AIError
from ...wecom import (
    _SdkLogger,
    _brief,
    _card_event,
    _clip,
    _cluster_card,
    _cluster_selection,
    _friendly_time,
    _limit_stream,
    _result_card,
    _selected,
    _usable,
)
from ..adapter import CardActionHandler, MessageHandler
from ..models import ApprovalPrompt, CardActionEvent, IMCapabilities, InboundEvent

LOGGER = logging.getLogger(__name__)

WECOM_CAPABILITIES = IMCapabilities(
    supports_native_stream=True,
    supports_message_edit=False,
    supports_interactive_card=True,
    supports_group_mention=False,
    max_message_bytes=18000,
    stream_throttle_ms=700,
    card_callback_sla_sec=4.0,
)


class _WeComBaseStream:
    def __init__(self, adapter: "WeComAdapter", userid: str) -> None:
        self.adapter = adapter
        self.userid = userid

    async def _push_card(self, card: dict[str, Any]) -> bool:
        return await self.adapter._push(self.userid, {"msgtype": "template_card", "template_card": card})


class WeComReplyStreamHandle(_WeComBaseStream):
    """Reply handle backed by WeCom reply_stream + proactive template_card push."""

    def __init__(self, adapter: "WeComAdapter", event: InboundEvent) -> None:
        super().__init__(adapter, event.external_user_id)
        self.frame = event.raw_context
        self.stream_id = generate_req_id("stream")
        self._finished = False

    async def update(self, markdown: str) -> None:
        if self._finished:
            return
        await self.adapter._reply(self.frame, markdown, False, self.stream_id)

    async def finish(self, markdown: str) -> None:
        if self._finished:
            return
        self._finished = True
        await self.adapter._reply(self.frame, markdown, True, self.stream_id)

    async def send_notice(self, markdown: str) -> None:
        await self.adapter._reply(self.frame, markdown, True, generate_req_id("stream"))

    async def send_approval(self, prompt: ApprovalPrompt, fallback_text: str) -> str | None:
        card = {
            "card_type": "button_interaction",
            "source": {"desc": f"集群：{prompt.env}", "desc_color": 2},
            "main_title": {"title": "K8S 操作待审批", "desc": _clip(prompt.operation_label, 40)},
            "sub_title_text": prompt.summary_line or "请核对操作内容后决定。",
            "button_list": [
                {"text": "批准", "key": "approve", "style": 1},
                {"text": "拒绝", "key": "reject", "style": 2},
            ],
            "task_id": prompt.card_ref or prompt.action_id,
        }
        await self.update(prompt.summary_markdown)
        sent = await self._push_card(card)
        await self.finish(prompt.summary_markdown if sent else fallback_text)
        return card["task_id"] if sent else None

    async def send_cluster_menu(
        self,
        current_env: str | None,
        clusters: list[dict[str, Any]],
        fallback_text: str,
        *,
        suggestion: str | None = None,
        prompt_text: str | None = None,
    ) -> None:
        text = prompt_text if prompt_text is not None else fallback_text
        hint = "选好后接着处理你刚才的问题。" if "刚才的问题" in text else None
        card = _cluster_card(clusters, current_env, suggestion, hint)
        if text != fallback_text:
            await self.update(text)
            sent = await self._push_card(card)
            await self.finish(text if sent else fallback_text)
        else:
            await self.finish(fallback_text)
            await self._push_card(card)


class WeComPushStreamHandle(_WeComBaseStream):
    """Push handle used after card callbacks where reply_stream is unavailable."""

    async def update(self, markdown: str) -> None:
        return

    async def finish(self, markdown: str) -> None:
        await self.adapter._push(
            self.userid,
            {"msgtype": "markdown", "markdown": {"content": _limit_stream(markdown)}},
        )

    send_notice = finish

    async def send_approval(self, prompt: ApprovalPrompt, fallback_text: str) -> str | None:
        card = {
            "card_type": "button_interaction",
            "source": {"desc": f"集群：{prompt.env}", "desc_color": 2},
            "main_title": {"title": "K8S 操作待审批", "desc": _clip(prompt.operation_label, 40)},
            "sub_title_text": prompt.summary_line or "请核对操作内容后决定。",
            "button_list": [
                {"text": "批准", "key": "approve", "style": 1},
                {"text": "拒绝", "key": "reject", "style": 2},
            ],
            "task_id": prompt.card_ref or prompt.action_id,
        }
        await self.finish(prompt.summary_markdown)
        sent = await self._push_card(card)
        if not sent:
            await self.finish(fallback_text)
            return None
        return card["task_id"]

    async def send_cluster_menu(
        self,
        current_env: str | None,
        clusters: list[dict[str, Any]],
        fallback_text: str,
        *,
        suggestion: str | None = None,
        prompt_text: str | None = None,
    ) -> None:
        text = prompt_text if prompt_text is not None else fallback_text
        hint = "选好后接着处理你刚才的问题。" if "刚才的问题" in text else None
        card = _cluster_card(clusters, current_env, suggestion, hint)
        await self.finish(text)
        if not await self._push_card(card) and text != fallback_text:
            await self.finish(fallback_text)


class WeComAdapter:
    """IMAdapter implementation wrapping `wecom-aibot-python-sdk` `WSClient`."""

    platform = "wecom"
    capabilities = WECOM_CAPABILITIES

    def __init__(self, bot_id: str) -> None:
        self.bot_id = bot_id
        self.client: Any = None
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._on_message: MessageHandler | None = None
        self._on_card_action: CardActionHandler | None = None
        self._state: dict[str, Any] = {
            "configured": False,
            "connected": False,
            "authenticated": False,
            "reconnecting": False,
            "error": None,
        }

    def _spawn(self, coro: Any, name: str) -> None:
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def start(
        self,
        bot_cfg: dict[str, Any],
        on_message: MessageHandler,
        on_card_action: CardActionHandler,
    ) -> None:
        async with self._lock:
            await self._disconnect()
            self._on_message = on_message
            self._on_card_action = on_card_action
            enabled = bool(bot_cfg.get("enabled"))
            creds = bot_cfg.get("credentials") if isinstance(bot_cfg.get("credentials"), dict) else bot_cfg
            wecom_bot_id = (creds.get("bot_id") or "").strip()
            bot_secret = (creds.get("bot_secret") or "").strip()
            self._state = {
                "configured": bool(enabled and wecom_bot_id and bot_secret),
                "connected": False,
                "authenticated": False,
                "reconnecting": False,
                "error": None,
            }
            if not enabled:
                return
            if not wecom_bot_id or not bot_secret:
                self._state["error"] = "企业微信机器人配置缺少 Bot ID 或 Secret"
                raise AIError("启用企业微信机器人需要 Bot ID 和 Secret")

            client = WSClient(
                WSClientOptions(
                    bot_id=wecom_bot_id,
                    secret=bot_secret,
                    max_reconnect_attempts=-1,
                    logger=_SdkLogger(),
                )
            )
            client.on("connected", lambda: self._set_state(connected=True, reconnecting=False))
            client.on("authenticated", lambda: self._set_state(authenticated=True, error=None))
            client.on("disconnected", lambda _: self._set_state(connected=False, authenticated=False))
            client.on("reconnecting", lambda _: self._set_state(connected=False, authenticated=False, reconnecting=True))
            client.on("error", self._on_sdk_error)
            client.on("message.text", self._handle_sdk_text)
            client.on("event.template_card_event", self._handle_sdk_card)
            client.on("event.enter_chat", self._handle_sdk_enter)
            self.client = client
            try:
                await client.connect()
            except Exception as exc:
                self._state["error"] = f"长连接启动失败（{type(exc).__name__}）"
                LOGGER.warning("企业微信机器人连接初始化失败: %s", type(exc).__name__)

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        async with self._lock:
            await self._disconnect()
        self._state.update(connected=False, authenticated=False, reconnecting=False)

    async def _disconnect(self) -> None:
        client, self.client = self.client, None
        if client:
            with suppress(Exception):
                client.disconnect()
            await asyncio.sleep(0)

    def _set_state(self, **values: Any) -> None:
        self._state.update(values)

    def _on_sdk_error(self, error: Any) -> None:
        self._state["error"] = "企业微信长连接错误"
        LOGGER.warning("企业微信机器人连接错误: %s", type(error).__name__)

    async def status(self) -> dict[str, Any]:
        return dict(self._state)

    def open_reply_stream(self, event: InboundEvent) -> WeComReplyStreamHandle:
        return WeComReplyStreamHandle(self, event)

    def open_push_stream(self, external_user_id: str, chat_id: str = "") -> WeComPushStreamHandle:
        return WeComPushStreamHandle(self, external_user_id)

    async def _reply(self, frame: Any, content: str, finish: bool = True, stream_id: str | None = None) -> None:
        if self.client and self.client.is_connected:
            await self.client.reply_stream(
                frame,
                stream_id or generate_req_id("stream"),
                _limit_stream(content),
                finish,
            )

    async def _push(self, userid: str, body: dict[str, Any]) -> bool:
        if not (self.client and self.client.is_connected and userid):
            return False
        try:
            await self.client.send_message(userid, {"chat_type": 1, **body})
            return True
        except Exception as exc:
            LOGGER.warning("企微主动推送 %s 失败: %s", body.get("msgtype"), _brief(str(exc)))
            return False

    async def update_approval_card(
        self,
        card_ref: str,
        decision: str,
        summary: str,
        raw_context: Any = None,
    ) -> None:
        if not (self.client and self.client.is_connected and raw_context is not None):
            return
        title = "已处理" if decision == "done" else "审批未完成"
        status = "已完成" if decision == "done" else "未完成"
        card = _result_card(card_ref, title, detail=summary, status=status)
        try:
            await self.client.update_template_card(raw_context, card)
        except Exception as exc:
            LOGGER.warning("企微卡片更新失败: %s", _brief(str(exc)))

    async def send_welcome(self, event: InboundEvent, payload: dict[str, Any]) -> None:
        if not (self.client and self.client.is_connected):
            return
        if payload.get("kind") == "text":
            body: dict[str, Any] = {"msgtype": "text", "text": {"content": payload["text"]}}
        else:
            last = payload.get("last")
            default_env = payload.get("default_env")
            buttons = [{"text": "开始新对话", "key": "session_new", "style": 1}]
            if last:
                buttons.append({"text": "继续上次对话", "key": "session_resume", "style": 2})
            card: dict[str, Any] = {
                "card_type": "button_interaction",
                "main_title": {
                    "title": "花折 AI 助手",
                    "desc": f"默认集群：{default_env}" if default_env else "新对话要先选集群",
                },
                "sub_title_text": (
                    f"上次对话：{payload.get('last_time_text')}，共 {last['rounds']} 轮"
                    if last
                    else "直接发送问题即可开始对话。"
                ),
                "button_list": buttons,
                "task_id": payload.get("task_id") or f"session_none_{secrets.token_hex(6)}",
            }
            clusters = payload.get("clusters")
            if clusters:
                suggestion = _usable(default_env, clusters) or payload.get("suggestion")
                card["button_selection"] = _cluster_selection(clusters, suggestion, "新对话的集群")
            body = {"msgtype": "template_card", "template_card": card}
        await self.client.reply_welcome(event.raw_context, body)

    async def _handle_sdk_text(self, frame: dict[str, Any]) -> None:
        body = frame.get("body") or {}
        userid = (body.get("from") or {}).get("userid")
        chattype = body.get("chattype", "single")
        if not isinstance(userid, str) or not userid or chattype != "single":
            await self._reply(frame, "企业微信 AI 助手当前仅支持单聊。")
            return
        content = ((body.get("text") or {}).get("content") or "").strip()
        event = InboundEvent(
            bot_id=self.bot_id,
            platform="wecom",
            msgid=str(body.get("msgid") or ""),
            external_user_id=userid,
            chat_id=userid,
            chat_type="single",
            text=content,
            mentioned_bot=False,
            kind="text",
            raw_context=frame,
        )
        if self._on_message:
            self._spawn(self._on_message(event), "wecom-message")

    async def _handle_sdk_enter(self, frame: dict[str, Any]) -> None:
        body = frame.get("body") or {}
        userid = (body.get("from") or {}).get("userid")
        if not isinstance(userid, str) or not userid or body.get("chattype", "single") != "single":
            return
        event = InboundEvent(
            bot_id=self.bot_id,
            platform="wecom",
            msgid=str(body.get("msgid") or ""),
            external_user_id=userid,
            chat_id=userid,
            chat_type="single",
            text="",
            mentioned_bot=False,
            kind="enter_chat",
            raw_context=frame,
        )
        if self._on_message:
            self._spawn(self._on_message(event), "wecom-welcome")

    async def _handle_sdk_card(self, frame: dict[str, Any]) -> None:
        body = frame.get("body") or {}
        userid = (body.get("from") or {}).get("userid")
        detail = _card_event(body)
        task_id, key = detail.get("task_id"), detail.get("event_key")
        if not isinstance(userid, str) or not userid or not isinstance(key, str) or key == "noop":
            return
        card_event = CardActionEvent(
            bot_id=self.bot_id,
            platform="wecom",
            action_id=key,
            external_user_id=userid,
            selected_value=_selected(detail, "env") or "",
            callback_token=str(task_id or ""),
            msgid=str(body.get("msgid") or ""),
            chat_id=userid,
            raw_context=frame,
        )
        if self._on_card_action:
            self._spawn(self._dispatch_card_and_update(frame, str(task_id or ""), card_event), "wecom-card")

    async def _dispatch_card_and_update(
        self,
        frame: dict[str, Any],
        task_id: str,
        card_event: CardActionEvent,
    ) -> None:
        assert self._on_card_action is not None
        outcome = await self._on_card_action(card_event)
        if not outcome or not (self.client and self.client.is_connected):
            return
        card = _result_card(
            outcome.get("card_ref") or task_id,
            outcome["title"],
            outcome.get("desc"),
            outcome.get("detail"),
            outcome.get("status", "已完成"),
        )
        try:
            await self.client.update_template_card(frame, card)
        except Exception as exc:
            LOGGER.warning("企微卡片更新失败: %s", _brief(str(exc)))
