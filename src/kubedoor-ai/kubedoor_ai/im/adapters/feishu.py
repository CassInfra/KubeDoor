"""Feishu / Lark long-connection adapter using official lark-oapi==1.5.5."""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import threading
import time
import uuid
from contextlib import suppress
from typing import Any

import lark_oapi as lark
import lark_oapi.ws.client as lark_ws_module
from lark_oapi.api.cardkit.v1 import (
    ContentCardElementRequest,
    ContentCardElementRequestBody,
    CreateCardRequest,
    CreateCardRequestBody,
    SettingsCardRequest,
    SettingsCardRequestBody,
)
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    PatchMessageRequest,
    PatchMessageRequestBody,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)
from lark_oapi.core.model import BaseRequest
from lark_oapi.core.enum import AccessTokenType, HttpMethod
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)
from lark_oapi.api.im.v1.model.p2_im_message_receive_v1 import P2ImMessageReceiveV1

from ...domain import AIError, redact
from ...wecom import _clip, _cluster_menu
from ..adapter import CardActionHandler, MessageHandler
from ..models import ApprovalPrompt, CardActionEvent, IMCapabilities, InboundEvent
from ..ssrf import validate_custom_domain

LOGGER = logging.getLogger(__name__)

FEISHU_CAPABILITIES = IMCapabilities(
    supports_native_stream=True,
    supports_message_edit=True,
    supports_interactive_card=True,
    supports_group_mention=True,
    max_message_bytes=28000,
    stream_throttle_ms=220,
    card_callback_sla_sec=2.2,
)

MENTION_PLACEHOLDER_RE = re.compile(r"@_user_\d+\s*")

# Dedicated daemon event-loop thread shared by real lark.ws.Client instances so
# lark_oapi.ws.client.loop never collides with the main FastAPI/uvicorn loop.
_WS_LOOP_LOCK = threading.Lock()
_WS_LOOP: asyncio.AbstractEventLoop | None = None
_WS_THREAD: threading.Thread | None = None


def _ensure_ws_loop() -> asyncio.AbstractEventLoop:
    global _WS_LOOP, _WS_THREAD
    with _WS_LOOP_LOCK:
        if _WS_LOOP is not None and _WS_THREAD is not None and _WS_THREAD.is_alive():
            return _WS_LOOP
        ready = threading.Event()
        holder: dict[str, asyncio.AbstractEventLoop] = {}

        def _runner() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            lark_ws_module.loop = loop
            holder["loop"] = loop
            ready.set()
            loop.run_forever()

        thread = threading.Thread(target=_runner, name="feishu-ws-loop", daemon=True)
        thread.start()
        ready.wait(timeout=5)
        _WS_LOOP = holder["loop"]
        _WS_THREAD = thread
        lark_ws_module.loop = _WS_LOOP
        return _WS_LOOP


def _safe_lark_error(exc: Any, secrets_tuple: tuple[str, ...] = ()) -> str:
    text = str(exc) if not isinstance(exc, str) else exc
    text = text.split("\n", 1)[0]
    cleaned = redact(text, secrets_tuple)
    if isinstance(cleaned, str) and len(cleaned) > 200:
        cleaned = cleaned[:199] + "…"
    return f"{type(exc).__name__}: {cleaned}" if not isinstance(exc, str) else str(cleaned)


def resolve_feishu_domain(credentials: dict[str, Any]) -> str:
    """Map domain_preset (feishu|lark|custom) to a verified base domain URL."""
    preset = (credentials.get("domain_preset") or "feishu").strip().lower()
    if preset == "feishu":
        return lark.FEISHU_DOMAIN
    if preset == "lark":
        return lark.LARK_DOMAIN
    if preset == "custom":
        return validate_custom_domain(credentials.get("custom_domain") or "")
    raise AIError("不支持的飞书域名预设，只能为 feishu、lark 或 custom", 400, "invalid_domain_preset")


def _clip_utf8(value: str, max_bytes: int) -> str:
    raw = value.encode("utf-8")
    if len(raw) <= max_bytes:
        return value
    return raw[: max(64, max_bytes - 40)].decode("utf-8", "ignore") + "\n\n（内容过长，已截断）"


def _summary_snippet(markdown: str) -> str:
    first = " ".join((markdown or "").split())
    return _clip(first or "AI 回复完成", 60)


async def _call_sdk(fn: Any, req: Any) -> Any:
    if inspect.iscoroutinefunction(fn):
        return await fn(req)
    result = await asyncio.to_thread(fn, req)
    if inspect.isawaitable(result):
        return await result
    return result


def _resp_ok(resp: Any) -> bool:
    if resp is None:
        return False
    success_fn = getattr(resp, "success", None)
    if callable(success_fn):
        return bool(success_fn())
    return getattr(resp, "code", None) == 0


def _resp_err(resp: Any) -> str:
    if resp is None:
        return "empty_response"
    return f"code={getattr(resp, 'code', '?')}, msg={getattr(resp, 'msg', '')}"


def _build_streaming_card(markdown: str, *, streaming_mode: bool = True, summary: str = "正在思考…") -> dict[str, Any]:
    return {
        "schema": "2.0",
        "config": {
            "streaming_mode": streaming_mode,
            "summary": {"content": summary},
        },
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": markdown or "正在分析请求...",
                    "element_id": "content_md",
                }
            ]
        },
    }


def _build_markdown_card(markdown: str, *, title: str | None = None, template: str = "blue") -> dict[str, Any]:
    card: dict[str, Any] = {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": markdown or "本轮操作已结束。",
                }
            ]
        },
    }
    if title:
        card["header"] = {
            "title": {"tag": "plain_text", "content": title},
            "template": template,
        }
    return card


def _build_approval_card(prompt: ApprovalPrompt) -> dict[str, Any]:
    body_text = prompt.body_markdown or prompt.summary_line or "请核对操作内容后决定。"
    if prompt.expires_at_text:
        body_text += f"\n\n有效期至：{prompt.expires_at_text}"
    card_ref = prompt.card_ref or prompt.action_id
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "K8S 操作待审批"},
            "subtitle": {"tag": "plain_text", "content": f"集群：{prompt.env} · {prompt.operation_label}"},
            "template": "red",
        },
        "body": {
            "elements": [
                {"tag": "markdown", "content": body_text},
                {
                    "tag": "column_set",
                    "flex_mode": "none",
                    "horizontal_spacing": "default",
                    "columns": [
                        {
                            "tag": "column",
                            "width": "auto",
                            "elements": [
                                {
                                    "tag": "button",
                                    "text": {"tag": "plain_text", "content": "批准"},
                                    "type": "primary",
                                    "value": {"action_id": f"approval:approve:{card_ref}", "card_ref": card_ref},
                                }
                            ],
                        },
                        {
                            "tag": "column",
                            "width": "auto",
                            "elements": [
                                {
                                    "tag": "button",
                                    "text": {"tag": "plain_text", "content": "拒绝"},
                                    "type": "danger",
                                    "value": {"action_id": f"approval:reject:{card_ref}", "card_ref": card_ref},
                                }
                            ],
                        },
                    ],
                },
            ]
        },
    }


def _build_cluster_card(
    clusters: list[dict[str, Any]],
    current_env: str | None,
    suggestion: str | None = None,
    prompt_text: str | None = None,
) -> dict[str, Any]:
    options = [
        {"text": {"tag": "plain_text", "content": item["env"]}, "value": item["env"]}
        for item in clusters[:50]
    ]
    selected = current_env or suggestion
    select_el: dict[str, Any] = {
        "tag": "select_static",
        "placeholder": {"tag": "plain_text", "content": "请选择目标 K8S 集群"},
        "options": options,
        "value": {"action_id": "cluster:select", "card_ref": f"env_{uuid.uuid4().hex[:12]}"},
    }
    if selected and any(item["env"] == selected for item in clusters[:50]):
        select_el["initial_option"] = selected

    elements: list[dict[str, Any]] = []
    if prompt_text:
        elements.append({"tag": "markdown", "content": prompt_text})
    elements.append(select_el)
    if len(clusters) > 50:
        elements.append({"tag": "markdown", "content": "仅列出前 50 个集群，其它集群可发送「切换集群 名称」。"})

    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "选择 K8S 集群"},
            "subtitle": {"tag": "plain_text", "content": f"当前集群：{current_env or '未选择'}"},
            "template": "blue",
        },
        "body": {"elements": elements},
    }


def _build_result_card(outcome: dict[str, Any]) -> dict[str, Any]:
    title = outcome.get("title") or "已处理"
    desc = outcome.get("desc")
    detail = outcome.get("detail")
    status = outcome.get("status") or "已完成"
    toast_type = outcome.get("toast_type", "info")
    template = {"success": "green", "error": "red", "warning": "orange"}.get(toast_type, "blue")
    md_parts = [f"**{title}**（{status}）"]
    if desc:
        md_parts.append(desc)
    if detail:
        md_parts.append(detail)
    return _build_markdown_card("\n\n".join(md_parts), title=title, template=template)


def _extract_feishu_text(message: Any) -> str:
    msg_type = getattr(message, "message_type", "text") or "text"
    raw_content = getattr(message, "content", "") or ""
    if msg_type == "text":
        try:
            parsed = json.loads(raw_content)
            if isinstance(parsed, dict):
                return str(parsed.get("text") or "")
        except ValueError:
            return str(raw_content)
    if msg_type == "post":
        try:
            parsed = json.loads(raw_content)
            texts: list[str] = []
            for para in parsed.get("content") or []:
                if isinstance(para, list):
                    for node in para:
                        if isinstance(node, dict) and node.get("tag") == "text":
                            texts.append(str(node.get("text") or ""))
            return " ".join(texts)
        except ValueError:
            return ""
    return ""


class FeishuCardStreamHandle:
    """3-Tier CardKit v1 Schema 2.0 streaming handle with guaranteed try/finally closure."""

    def __init__(
        self,
        adapter: "FeishuAdapter",
        *,
        external_user_id: str,
        chat_id: str,
        reply_to_message_id: str = "",
        chat_type: str = "single",
        throttle_ms: int = 220,
        max_bytes: int = 28000,
    ) -> None:
        self.adapter = adapter
        self.external_user_id = external_user_id
        self.chat_id = chat_id
        self.reply_to_message_id = reply_to_message_id
        self.chat_type = chat_type
        self._throttle_sec = max(0.05, throttle_ms / 1000.0)
        self._max_bytes = max_bytes

        self._lock = asyncio.Lock()
        self._tier = "A"  # A: CardKit streaming, B: message.patch, C: single final msg
        self._card_id: str | None = None
        self._message_id: str | None = None
        self._streaming_open = False
        self._sequence = 0
        self._last_sent_at = 0.0
        self._last_sent_text = ""
        self._closed = False

    def _next_seq(self) -> int:
        self._sequence += 1
        return self._sequence

    async def _send_raw_message(self, msg_type: str, content_dict: dict[str, Any]) -> str | None:
        client = self.adapter.client
        if client is None:
            return None
        content_json = json.dumps(content_dict, ensure_ascii=False)
        if self.reply_to_message_id:
            req = (
                ReplyMessageRequest.builder()
                .message_id(self.reply_to_message_id)
                .request_body(
                    ReplyMessageRequestBody.builder()
                    .msg_type(msg_type)
                    .content(content_json)
                    .uuid(uuid.uuid4().hex)
                    .build()
                )
                .build()
            )
            resp = await _call_sdk(client.im.v1.message.reply, req)
            if _resp_ok(resp):
                return getattr(getattr(resp, "data", None), "message_id", None)
            LOGGER.warning("飞书 reply 消息失败，尝试 create: %s", _resp_err(resp))

        receive_id_type = "chat_id" if self.chat_id else "open_id"
        receive_id = self.chat_id or self.external_user_id
        req = (
            CreateMessageRequest.builder()
            .receive_id_type(receive_id_type)
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(receive_id)
                .msg_type(msg_type)
                .content(content_json)
                .uuid(uuid.uuid4().hex)
                .build()
            )
            .build()
        )
        resp = await _call_sdk(client.im.v1.message.create, req)
        if not _resp_ok(resp):
            raise RuntimeError(f"Feishu send message failed: {_resp_err(resp)}")
        return getattr(getattr(resp, "data", None), "message_id", None)

    async def _create_streaming_card(self, initial_md: str) -> None:
        client = self.adapter.client
        if client is None:
            self._tier = "C"
            return
        card_json = json.dumps(_build_streaming_card(initial_md, streaming_mode=True), ensure_ascii=False)
        req = (
            CreateCardRequest.builder()
            .request_body(CreateCardRequestBody.builder().type("card_json").data(card_json).build())
            .build()
        )
        try:
            resp = await _call_sdk(client.cardkit.v1.card.create, req)
            if not _resp_ok(resp) or not getattr(getattr(resp, "data", None), "card_id", None):
                raise RuntimeError(_resp_err(resp))
            card_id = resp.data.card_id
            self._card_id = card_id
            self._streaming_open = True
            self._last_sent_text = initial_md
            self._last_sent_at = time.monotonic()
            self._message_id = await self._send_raw_message("card", {"type": "card", "data": {"card_id": card_id}})
        except Exception as exc:
            LOGGER.warning("飞书 CardKit 流式卡片创建失败，降级为普通消息: %s", _safe_lark_error(exc))
            if self._card_id and self._streaming_open:
                await self._close_streaming(_summary_snippet(initial_md))
            self._card_id = None
            self._streaming_open = False
            await self._fallback_to_tier_b(initial_md)

    async def _fallback_to_tier_b(self, initial_md: str) -> None:
        try:
            msg_id = await self._send_raw_message("interactive", _build_markdown_card(initial_md))
            self._message_id = msg_id
            self._tier = "B" if msg_id else "C"
            self._last_sent_text = initial_md
            self._last_sent_at = time.monotonic()
        except Exception as exc:
            LOGGER.warning("飞书 Tier B 卡片发送失败，降级 Tier C: %s", _safe_lark_error(exc))
            self._tier = "C"

    async def _push_cardkit_content(self, markdown: str) -> None:
        client = self.adapter.client
        if client is None or not self._card_id:
            return
        seq = self._next_seq()
        req = (
            ContentCardElementRequest.builder()
            .card_id(self._card_id)
            .element_id("content_md")
            .request_body(
                ContentCardElementRequestBody.builder()
                .content(markdown)
                .sequence(seq)
                .uuid(uuid.uuid4().hex)
                .build()
            )
            .build()
        )
        resp = await _call_sdk(client.cardkit.v1.card_element.content, req)
        if not _resp_ok(resp):
            raise RuntimeError(f"CardKit content update failed: {_resp_err(resp)}")
        self._last_sent_text = markdown
        self._last_sent_at = time.monotonic()

    async def _patch_message_card(self, markdown: str) -> None:
        client = self.adapter.client
        if client is None or not self._message_id:
            return
        req = (
            PatchMessageRequest.builder()
            .message_id(self._message_id)
            .request_body(
                PatchMessageRequestBody.builder()
                .content(json.dumps(_build_markdown_card(markdown), ensure_ascii=False))
                .build()
            )
            .build()
        )
        resp = await _call_sdk(client.im.v1.message.patch, req)
        if not _resp_ok(resp):
            raise RuntimeError(f"Feishu message.patch failed: {_resp_err(resp)}")
        self._last_sent_text = markdown
        self._last_sent_at = time.monotonic()

    async def _close_streaming(self, summary: str) -> None:
        if not self._card_id or not self._streaming_open:
            return
        client = self.adapter.client
        self._streaming_open = False
        if client is None:
            return
        settings_json = json.dumps({"config": {"streaming_mode": False, "summary": {"content": summary}}}, ensure_ascii=False)
        req = (
            SettingsCardRequest.builder()
            .card_id(self._card_id)
            .request_body(
                SettingsCardRequestBody.builder()
                .settings(settings_json)
                .sequence(self._next_seq())
                .uuid(uuid.uuid4().hex)
                .build()
            )
            .build()
        )
        try:
            resp = await _call_sdk(client.cardkit.v1.card.settings, req)
            if not _resp_ok(resp):
                LOGGER.warning("飞书关闭流式卡片失败: %s", _resp_err(resp))
        except Exception as exc:
            LOGGER.warning("飞书关闭流式卡片异常: %s", _safe_lark_error(exc))

    async def update(self, markdown: str) -> None:
        text = _clip_utf8(markdown, self._max_bytes)
        async with self._lock:
            if self._closed or text == self._last_sent_text:
                return
            if self._tier == "A":
                if self._card_id is None:
                    await self._create_streaming_card(text)
                    return
                if time.monotonic() - self._last_sent_at < self._throttle_sec:
                    return
                try:
                    await self._push_cardkit_content(text)
                except Exception as exc:
                    LOGGER.warning("飞书 CardKit 增量更新失败，尝试降级: %s", _safe_lark_error(exc))
                    await self._close_streaming(_summary_snippet(text))
                    if self._message_id:
                        self._tier = "B"
                        with suppress(Exception):
                            await self._patch_message_card(text)
                    else:
                        self._tier = "C"
            elif self._tier == "B" and self._message_id:
                if time.monotonic() - self._last_sent_at < self._throttle_sec:
                    return
                with suppress(Exception):
                    await self._patch_message_card(text)

    async def finish(self, markdown: str) -> None:
        text = _clip_utf8(markdown or "本轮操作已结束。", self._max_bytes)
        async with self._lock:
            if self._closed:
                return
            try:
                if self._tier == "A" and self._card_id:
                    if text != self._last_sent_text:
                        await self._push_cardkit_content(text)
                elif self._tier == "B" and self._message_id:
                    if text != self._last_sent_text:
                        await self._patch_message_card(text)
                else:
                    try:
                        await self._send_raw_message("interactive", _build_markdown_card(text))
                    except Exception:
                        await self._send_raw_message("text", {"text": text})
            except Exception as exc:
                LOGGER.warning("飞书最终结果发送异常，尝试文本兜底: %s", _safe_lark_error(exc))
                with suppress(Exception):
                    await self._send_raw_message("text", {"text": text})
            finally:
                self._closed = True
                if self._card_id and self._streaming_open:
                    await self._close_streaming(_summary_snippet(text))

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._card_id and self._streaming_open:
                await self._close_streaming(_summary_snippet(self._last_sent_text or "已结束"))

    async def send_notice(self, markdown: str) -> None:
        text = _clip_utf8(markdown, self._max_bytes)
        try:
            await self._send_raw_message("interactive", _build_markdown_card(text))
        except Exception:
            with suppress(Exception):
                await self._send_raw_message("text", {"text": text})

    async def send_approval(self, prompt: ApprovalPrompt, fallback_text: str) -> str | None:
        await self.finish(prompt.summary_markdown)
        card = _build_approval_card(prompt)
        try:
            await self._send_raw_message("interactive", card)
            return prompt.card_ref or prompt.action_id
        except Exception as exc:
            LOGGER.warning("飞书审批卡片发送失败，降级文本: %s", _safe_lark_error(exc))
            with suppress(Exception):
                await self._send_raw_message("text", {"text": _clip_utf8(fallback_text, self._max_bytes)})
            return None

    async def send_cluster_menu(
        self,
        current_env: str | None,
        clusters: list[dict[str, Any]],
        fallback_text: str,
        *,
        suggestion: str | None = None,
        prompt_text: str | None = None,
    ) -> None:
        card_prompt = prompt_text
        if self._card_id is not None:
            await self.finish(prompt_text or "请选择目标 K8S 集群：")
            card_prompt = None
        else:
            await self.close()
        card = _build_cluster_card(clusters, current_env, suggestion, card_prompt)
        try:
            await self._send_raw_message("interactive", card)
        except Exception as exc:
            LOGGER.warning("飞书集群选择卡片发送失败，降级文本: %s", _safe_lark_error(exc))
            with suppress(Exception):
                await self._send_raw_message("text", {"text": _clip_utf8(fallback_text, self._max_bytes)})


class FeishuAdapter:
    """IMAdapter implementation for Feishu / Lark over official lark.ws.Client."""

    platform = "feishu"
    capabilities = FEISHU_CAPABILITIES

    def __init__(self, bot_id: str) -> None:
        self.bot_id = bot_id
        self.client: Any = None
        self.ws_client: Any = None
        self._main_loop: asyncio.AbstractEventLoop | None = None
        self._ws_thread: threading.Thread | None = None
        self._ws_connect_task: asyncio.Future[Any] | None = None
        self._ws_ping_task: asyncio.Future[Any] | None = None
        self._on_message: MessageHandler | None = None
        self._on_card_action: CardActionHandler | None = None
        self._app_secret: str = ""
        self._bot_open_id: str | None = None
        self._bot_name: str | None = None
        self._lock = asyncio.Lock()
        self._state: dict[str, Any] = {
            "configured": False,
            "connected": False,
            "authenticated": False,
            "reconnecting": False,
            "error": None,
        }

    async def start(
        self,
        bot_cfg: dict[str, Any],
        on_message: MessageHandler,
        on_card_action: CardActionHandler,
    ) -> None:
        async with self._lock:
            await self._stop_unlocked()
            self._main_loop = asyncio.get_running_loop()
            self._on_message = on_message
            self._on_card_action = on_card_action

            enabled = bool(bot_cfg.get("enabled"))
            creds = bot_cfg.get("credentials") if isinstance(bot_cfg.get("credentials"), dict) else bot_cfg
            app_id = (creds.get("app_id") or "").strip()
            app_secret = (creds.get("app_secret") or "").strip()
            self._app_secret = app_secret

            self._state = {
                "configured": bool(enabled and app_id and app_secret),
                "connected": False,
                "authenticated": False,
                "reconnecting": False,
                "error": None,
            }
            if not enabled:
                return
            domain = resolve_feishu_domain(creds)
            if not app_id or not app_secret:
                self._state["error"] = "飞书机器人配置缺少 App ID 或 App Secret"
                raise AIError("启用飞书机器人需要 App ID 和 App Secret")

            self.client = (
                lark.Client.builder()
                .app_id(app_id)
                .app_secret(app_secret)
                .domain(domain)
                .log_level(lark.LogLevel.WARNING)
                .build()
            )
            await self._fetch_bot_info()

            handler = (
                lark.EventDispatcherHandler.builder("", "")
                .register_p2_im_message_receive_v1(self._on_im_message_sync)
                .register_p2_card_action_trigger(self._on_card_action_sync)
                .build()
            )
            ws_client = lark.ws.Client(
                app_id,
                app_secret,
                event_handler=handler,
                log_level=lark.LogLevel.WARNING,
                domain=domain,
                auto_reconnect=True,
            )
            self.ws_client = ws_client
            await self._launch_ws_client(ws_client)

    async def _fetch_bot_info(self) -> None:
        if self.client is None or not hasattr(self.client, "request"):
            return
        req = (
            BaseRequest.builder()
            .http_method(HttpMethod.GET)
            .uri("/open-apis/bot/v3/info")
            .token_types({AccessTokenType.TENANT})
            .build()
        )
        try:
            resp = await _call_sdk(self.client.request, req)
            if _resp_ok(resp) and getattr(getattr(resp, "raw", None), "content", None):
                data = json.loads(resp.raw.content.decode("utf-8"))
                bot = data.get("bot") if isinstance(data, dict) else None
                if isinstance(bot, dict):
                    self._bot_open_id = bot.get("open_id") or None
                    self._bot_name = bot.get("app_name") or None
        except Exception:
            return

    async def _launch_ws_client(self, ws_client: Any) -> None:
        # Real lark.ws.Client uses module-level `lark_oapi.ws.client.loop`, so we host
        # it on a dedicated daemon loop thread to keep SDK heartbeats non-blocking.
        real_ws_cls = getattr(lark.ws, "Client", None)
        is_native_ws = (
            real_ws_cls is not None
            and type(ws_client).__module__.startswith("lark_oapi")
            and hasattr(ws_client, "_connect")
            and hasattr(ws_client, "_ping_loop")
        )
        if not is_native_ws:
            thread = threading.Thread(target=ws_client.start, name=f"feishu-ws-{self.bot_id[:8]}", daemon=True)
            self._ws_thread = thread
            thread.start()
            self._state.update(connected=True, authenticated=True, error=None)
            return

        ws_loop = _ensure_ws_loop()

        async def _boot() -> None:
            await ws_client._connect()
            self._ws_ping_task = asyncio.create_task(ws_client._ping_loop())

        fut = asyncio.run_coroutine_threadsafe(_boot(), ws_loop)
        self._ws_connect_task = fut
        try:
            await asyncio.wait_for(asyncio.wrap_future(fut), timeout=10)
            self._state.update(connected=True, authenticated=True, reconnecting=False, error=None)
        except Exception as exc:
            self._state.update(
                connected=False,
                authenticated=False,
                error=f"飞书长连接失败（{_safe_lark_error(exc, (self._app_secret,))}）",
            )
            LOGGER.warning("飞书长连接启动失败: %s", _safe_lark_error(exc, (self._app_secret,)))

    async def stop(self) -> None:
        async with self._lock:
            await self._stop_unlocked()

    async def _stop_unlocked(self) -> None:
        ws_client, self.ws_client = self.ws_client, None
        ping_task, self._ws_ping_task = self._ws_ping_task, None
        if ws_client is not None:
            with suppress(Exception):
                ws_client._auto_reconnect = False
            if ping_task is not None:
                with suppress(Exception):
                    ping_task.cancel()
            if _WS_LOOP is not None and hasattr(ws_client, "_disconnect"):
                with suppress(Exception):
                    fut = asyncio.run_coroutine_threadsafe(ws_client._disconnect(), _WS_LOOP)
                    await asyncio.wait_for(asyncio.wrap_future(fut), timeout=3)
            elif hasattr(ws_client, "stop"):
                with suppress(Exception):
                    ws_client.stop()
        self.client = None
        self._ws_thread = None
        self._state.update(connected=False, authenticated=False, reconnecting=False)

    async def status(self) -> dict[str, Any]:
        return dict(self._state)

    def open_reply_stream(self, event: InboundEvent) -> FeishuCardStreamHandle:
        return FeishuCardStreamHandle(
            self,
            external_user_id=event.external_user_id,
            chat_id=event.chat_id,
            reply_to_message_id=event.msgid,
            chat_type=event.chat_type,
            throttle_ms=self.capabilities.stream_throttle_ms,
            max_bytes=self.capabilities.max_message_bytes,
        )

    def open_push_stream(self, external_user_id: str, chat_id: str = "") -> FeishuCardStreamHandle:
        return FeishuCardStreamHandle(
            self,
            external_user_id=external_user_id,
            chat_id=chat_id,
            reply_to_message_id="",
            chat_type="group" if chat_id else "single",
            throttle_ms=self.capabilities.stream_throttle_ms,
            max_bytes=self.capabilities.max_message_bytes,
        )

    async def update_approval_card(
        self,
        card_ref: str,
        decision: str,
        summary: str,
        raw_context: Any = None,
    ) -> None:
        if self.client is None or not isinstance(raw_context, dict):
            return
        open_message_id = raw_context.get("open_message_id")
        if not open_message_id:
            return
        title = "已批准" if decision == "done" else "审批未完成"
        template = "green" if decision == "done" else "red"
        req = (
            PatchMessageRequest.builder()
            .message_id(open_message_id)
            .request_body(
                PatchMessageRequestBody.builder()
                .content(json.dumps(_build_markdown_card(summary, title=title, template=template), ensure_ascii=False))
                .build()
            )
            .build()
        )
        with suppress(Exception):
            await _call_sdk(self.client.im.v1.message.patch, req)

    def _is_bot_mentioned(self, mentions: list[Any] | None) -> tuple[bool, set[str]]:
        if not mentions:
            return False, set()
        bot_keys: set[str] = set()
        if self._bot_open_id:
            for item in mentions:
                uid = getattr(item, "id", None)
                if getattr(uid, "open_id", None) == self._bot_open_id:
                    if getattr(item, "key", None):
                        bot_keys.add(item.key)
            return bool(bot_keys), bot_keys
        if self._bot_name:
            for item in mentions:
                if getattr(item, "name", None) == self._bot_name and getattr(item, "key", None):
                    bot_keys.add(item.key)
            return bool(bot_keys), bot_keys
        for item in mentions:
            if getattr(item, "key", None) and item.key != "@_all":
                bot_keys.add(item.key)
        return bool(bot_keys), bot_keys

    def _on_im_message_sync(self, data: P2ImMessageReceiveV1) -> None:
        if self._on_message is None or self._main_loop is None:
            return
        ev = getattr(data, "event", None)
        if ev is None:
            return
        sender = getattr(ev, "sender", None)
        if getattr(sender, "sender_type", None) == "app":
            return
        sender_id = getattr(sender, "sender_id", None)
        open_id = getattr(sender_id, "open_id", None) or getattr(sender_id, "user_id", None) or ""
        msg = getattr(ev, "message", None)
        if not open_id or msg is None:
            return
        raw_chat_type = getattr(msg, "chat_type", "p2p") or "p2p"
        chat_type = "single" if raw_chat_type == "p2p" else "group"
        mentions = getattr(msg, "mentions", None) or []
        mentioned_bot, bot_keys = self._is_bot_mentioned(mentions)
        text = _extract_feishu_text(msg)
        for key in bot_keys:
            text = text.replace(key, "")
        text = MENTION_PLACEHOLDER_RE.sub("", text).strip()

        inbound = InboundEvent(
            bot_id=self.bot_id,
            platform="feishu",
            msgid=str(getattr(msg, "message_id", "") or ""),
            external_user_id=str(open_id),
            chat_id=str(getattr(msg, "chat_id", "") or ""),
            chat_type=chat_type,  # type: ignore[arg-type]
            text=text,
            mentioned_bot=mentioned_bot,
            kind="text",
            raw_context=data,
        )
        asyncio.run_coroutine_threadsafe(self._on_message(inbound), self._main_loop)

    def _on_card_action_sync(self, data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
        if self._on_card_action is None or self._main_loop is None:
            return P2CardActionTriggerResponse({"toast": {"type": "warning", "content": "机器人未就绪"}})
        ev = getattr(data, "event", None)
        operator = getattr(ev, "operator", None)
        open_id = getattr(operator, "open_id", None) or getattr(operator, "user_id", None) or ""
        action = getattr(ev, "action", None)
        value = getattr(action, "value", None)
        value_dict = value if isinstance(value, dict) else {}
        option = getattr(action, "option", None) or ""
        options = getattr(action, "options", None) or []
        selected_value = option or (options[0] if options else "") or value_dict.get("env", "")
        action_id = value_dict.get("action_id") or getattr(action, "name", None) or ""
        card_ref = value_dict.get("card_ref") or ""
        ctx = getattr(ev, "context", None)
        open_message_id = getattr(ctx, "open_message_id", None) or ""
        open_chat_id = getattr(ctx, "open_chat_id", None) or ""
        token = getattr(ev, "token", None) or ""
        header = getattr(data, "header", None)
        msgid = getattr(header, "event_id", None) or token

        event = CardActionEvent(
            bot_id=self.bot_id,
            platform="feishu",
            action_id=str(action_id),
            external_user_id=str(open_id),
            selected_value=str(selected_value or ""),
            callback_token=str(card_ref),
            msgid=str(msgid or ""),
            chat_id=str(open_chat_id),
            raw_context={"open_message_id": open_message_id, "open_chat_id": open_chat_id, "token": token},
        )
        fut = asyncio.run_coroutine_threadsafe(self._on_card_action(event), self._main_loop)
        try:
            outcome = fut.result(timeout=self.capabilities.card_callback_sla_sec)
        except TimeoutError:
            return P2CardActionTriggerResponse({"toast": {"type": "info", "content": "处理中…"}})
        except Exception as exc:
            LOGGER.warning("飞书卡片回调执行异常: %s", _safe_lark_error(exc, (self._app_secret,)))
            return P2CardActionTriggerResponse({"toast": {"type": "error", "content": "处理失败，请重试"}})

        if not outcome:
            return P2CardActionTriggerResponse({"toast": {"type": "info", "content": "已收到"}})
        resp_dict: dict[str, Any] = {
            "toast": {"type": outcome.get("toast_type", "info"), "content": outcome.get("toast", "已处理")}
        }
        if outcome.get("title"):
            resp_dict["card"] = {"type": "raw", "data": _build_result_card(outcome)}
        return P2CardActionTriggerResponse(resp_dict)
