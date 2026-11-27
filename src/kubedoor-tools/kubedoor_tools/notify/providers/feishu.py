"""Feishu / Lark (飞书 / 国际版 Lark) group webhook notification provider."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from ..models import ChannelConfig, DeliveryResult, FieldSpec, HttpPreparedRequest, NotificationMessage, RateLimit
from ..registry import NotificationProvider, detect_provider_from_url, register_provider
from ._common import (
    OK_MESSAGE,
    TOKEN_PART_RE,
    clean_str,
    decode_body,
    format_failure,
    has_control_char,
    is_zero,
    json_http_request,
    make_delivery_result,
    normalize_at_value,
    normalize_custom_base_url,
    parse_json_dict,
    parse_retry_after,
    resolve_mentions,
    resolve_secret_value,
)

FEISHU_BASE = "https://open.feishu.cn"
LARK_BASE = "https://open.larksuite.com"
FEISHU_HOOK_PATH = "/open-apis/bot/v2/hook/"
FEISHU_HINT = "自定义机器人 Webhook 地址(https://open.feishu.cn/open-apis/bot/v2/hook/…),或 /hook/ 后面那一段"

_SEVERITY_TEMPLATE: dict[str, str] = {
    "critical": "red",
    "firing": "red",
    "error": "red",
    "high": "red",
    "warning": "orange",
    "warn": "orange",
    "resolved": "green",
    "ok": "green",
    "success": "green",
    "info": "blue",
    "ops": "blue",
    "test": "blue",
    "generic": "blue",
}


def compute_feishu_sign(timestamp_sec: str, secret: str) -> str:
    """Compute Feishu webhook HMAC-SHA256 signature using key=f'{ts}\\n{secret}' and empty msg."""
    key = f"{timestamp_sec}\n{secret}".encode("utf-8")
    digest = hmac.new(key, b"", hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


@register_provider
class FeishuProvider(NotificationProvider):
    """Feishu / Lark interactive card webhook provider with optional HMAC-SHA256 signing."""

    id = "feishu"
    label = "飞书 / Lark"
    description = "飞书国内版 / Lark 国际版 / 私有化机器人（支持签名校验、动态标题与分级卡片配色）"
    transport = "http"
    supports_at = True
    supports_markdown = True
    at_help = "飞书用户的 open_id（ou_ 开头，多个用逗号分隔），或填 all @所有人"
    default_rate_limit = RateLimit(limit=100, window_seconds=60.0, burst_limit=5, burst_window_seconds=1.0)
    fields = (
        FieldSpec(
            key="domain_preset",
            label="平台版本",
            kind="select",
            required=True,
            default="feishu",
            options=(
                {"value": "feishu", "label": "飞书国内版 (open.feishu.cn)"},
                {"value": "lark", "label": "Lark 国际版 (open.larksuite.com)"},
                {"value": "custom", "label": "私有化域名"},
            ),
        ),
        FieldSpec(
            key="custom_domain",
            label="私有化网关地址",
            kind="text",
            required=False,
            placeholder="https://open.example.com",
            help="仅在选择私有化域名时填写",
            show_if={"domain_preset": "custom"},
        ),
        FieldSpec(
            key="token",
            label="机器人地址 / Hook ID",
            kind="secret",
            required=True,
            secret=True,
            placeholder="粘贴自定义机器人 Webhook 地址，或 /hook/ 后面那一段",
            help="支持 open.feishu.cn、open.larksuite.com 完整地址或 hook ID",
        ),
        FieldSpec(
            key="sign_secret",
            label="签名密钥 (可选)",
            kind="secret",
            required=False,
            secret=True,
            placeholder="开启飞书机器人「签名校验」时填写，可留空",
            help="在飞书机器人安全设置开启「签名校验」时填写，发送时自动计算 HMAC-SHA256 签名",
        ),
        FieldSpec(
            key="at",
            label="默认 @ 成员",
            kind="text",
            required=False,
            max_length=128,
            placeholder="飞书用户的 open_id（ou_ 开头，多个用逗号分隔），或填 all @所有人",
        ),
    )

    def match_url(self, url: str) -> bool:
        try:
            parts = urlsplit(url)
        except ValueError:
            return False
        return (parts.hostname or "").lower() in ("open.feishu.cn", "open.larksuite.com")

    def normalize(
        self,
        raw_settings: dict[str, Any] | None,
        raw_secrets: dict[str, Any] | None,
        saved_secrets: dict[str, str] | None = None,
        *,
        allow_reuse_saved: bool = True,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        settings = raw_settings or {}
        preset = clean_str(settings.get("domain_preset") or "feishu").lower()
        if preset == "public":
            preset = "feishu"
        if preset not in ("feishu", "lark", "custom"):
            raise ValueError(f"不支持的飞书平台版本: {preset}")

        custom_domain = clean_str(settings.get("custom_domain"))
        at = normalize_at_value(settings.get("at"))

        raw_token = resolve_secret_value(
            "token",
            raw_secrets,
            saved_secrets,
            required=True,
            missing_error="请填写飞书机器人地址",
            allow_reuse_saved=allow_reuse_saved,
        )
        sign_secret = resolve_secret_value(
            "sign_secret",
            raw_secrets,
            saved_secrets,
            required=False,
            missing_error="",
            allow_reuse_saved=allow_reuse_saved,
        )

        token = raw_token
        lowered = raw_token.lower()
        host_head = lowered.split("/", 1)[0].split("?", 1)[0]
        if not lowered.startswith(("http://", "https://")) and host_head in ("open.feishu.cn", "open.larksuite.com"):
            raw_token = "https://" + raw_token
            lowered = raw_token.lower()

        if lowered.startswith(("http://", "https://")) or "://" in raw_token:
            detected = detect_provider_from_url(raw_token)
            if detected and detected != self.id:
                other_label = {"wecom": "企业微信", "dingding": "钉钉", "slack": "Slack"}.get(detected, detected)
                raise ValueError(f"这是{other_label}的机器人地址,和选择的 IM 类型(飞书)不一致")
            try:
                parts = urlsplit(raw_token)
                host = (parts.hostname or "").lower()
            except ValueError:
                raise ValueError(f"不认识的机器人地址,飞书请填写{FEISHU_HINT}") from None

            if parts.scheme.lower() not in ("http", "https") or not host or not parts.path.startswith(FEISHU_HOOK_PATH):
                raise ValueError(f"不认识的机器人地址,飞书请填写{FEISHU_HINT}")

            if host == "open.feishu.cn" and preset in ("feishu", "lark"):
                preset = "feishu"
            elif host == "open.larksuite.com" and preset in ("feishu", "lark"):
                preset = "lark"
            elif preset in ("feishu", "lark"):
                raise ValueError(f"不认识的机器人地址,飞书请填写{FEISHU_HINT}")
            elif preset == "custom" and not custom_domain:
                port_part = f":{parts.port}" if parts.port is not None else ""
                custom_domain = f"{parts.scheme.lower()}://{host}{port_part}"

            token = parts.path[len(FEISHU_HOOK_PATH) :].strip("/")
            if not token:
                raise ValueError(f"机器人地址里没有 token,飞书请填写{FEISHU_HINT}")

        if has_control_char(token) or not TOKEN_PART_RE.fullmatch(token):
            raise ValueError(f"机器人 token 格式不对,飞书请填写{FEISHU_HINT}")

        normalized_custom = ""
        if preset == "custom":
            normalized_custom = normalize_custom_base_url(custom_domain, allow_http=False, field_label="飞书私有化域名")

        out_settings: dict[str, Any] = {"domain_preset": preset, "at": at}
        if normalized_custom:
            out_settings["custom_domain"] = normalized_custom

        out_secrets: dict[str, str] = {"token": token}
        if sign_secret:
            out_secrets["sign_secret"] = sign_secret
        return out_settings, out_secrets

    def build_http(self, cfg: ChannelConfig, msg: NotificationMessage) -> HttpPreparedRequest:
        preset = clean_str(cfg.settings.get("domain_preset") or "feishu").lower()
        if preset == "lark":
            base = LARK_BASE
        elif preset == "custom":
            custom = clean_str(cfg.settings.get("custom_domain"))
            if not custom:
                raise ValueError("飞书私有化域名未配置")
            base = custom.rstrip("/")
        else:
            base = FEISHU_BASE

        token = clean_str(cfg.secrets.get("token"))
        if not token:
            raise ValueError("飞书机器人 token 未配置")

        title = clean_str(msg.title) or "通知"
        severity_key = clean_str(msg.severity or "info").lower()
        template_color = _SEVERITY_TEMPLATE.get(severity_key, "blue")

        body_text = msg.markdown or msg.plain_text or ""
        mentions, mention_all = resolve_mentions(cfg, msg)
        at_parts: list[str] = []
        if mention_all:
            at_parts.append("<at id=all></at>")
        for uid in mentions:
            at_parts.append(f"<at id={uid}></at>")
        if at_parts:
            at_line = " ".join(at_parts)
            body_text = f"{body_text}\n{at_line}" if body_text else at_line

        payload: dict[str, Any] = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": title},
                    "template": template_color,
                },
                "elements": [{"tag": "markdown", "content": body_text}],
            },
        }

        sign_secret = clean_str(cfg.secrets.get("sign_secret"))
        if sign_secret:
            ts = str(int(time.time()))
            payload["timestamp"] = ts
            payload["sign"] = compute_feishu_sign(ts, sign_secret)

        url = f"{base}{FEISHU_HOOK_PATH}{token}"
        return json_http_request("POST", url, payload)

    def parse_http_response(
        self,
        status_code: int,
        headers: Mapping[str, str],
        body_text: str | bytes,
    ) -> DeliveryResult:
        text = decode_body(body_text)
        retry_after = parse_retry_after(headers)
        data = parse_json_dict(text)
        if data is None:
            retryable = status_code == 429 or status_code >= 500
            return make_delivery_result(
                False,
                format_failure(status_code, None, None, text),
                status_code=status_code,
                retryable=retryable,
                retry_after=retry_after,
            )

        if is_zero(data.get("code")) or is_zero(data.get("StatusCode")):
            return make_delivery_result(True, OK_MESSAGE, status_code=status_code, provider_code="0")

        code = data["code"] if "code" in data else data.get("StatusCode")
        msg = data["msg"] if "msg" in data else data.get("StatusMessage")
        code_str = "" if code is None else str(code)
        retryable = status_code == 429 or status_code >= 500 or code_str in ("11232", "99991400")
        return make_delivery_result(
            False,
            format_failure(status_code, code, msg, text),
            status_code=status_code,
            provider_code=code_str,
            retryable=retryable,
            retry_after=retry_after,
        )
