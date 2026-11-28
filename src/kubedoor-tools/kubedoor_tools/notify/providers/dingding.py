"""DingTalk (钉钉) group webhook notification provider with optional HMAC-SHA256 signing."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qs, quote_plus, urlsplit

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

DINGDING_PUBLIC_BASE = "https://oapi.dingtalk.com"
DINGDING_PATH = "/robot/send"
DINGDING_HINT = "自定义机器人 Webhook 地址(https://oapi.dingtalk.com/robot/send?access_token=…),或 access_token= 后面那一段"


def compute_dingding_sign(timestamp_ms: str, secret: str) -> str:
    """Compute URL-encoded DingTalk webhook HMAC-SHA256 signature."""
    msg_bytes = f"{timestamp_ms}\n{secret}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), msg_bytes, hashlib.sha256).digest()
    return quote_plus(base64.b64encode(digest).decode("utf-8"))


@register_provider
class DingDingProvider(NotificationProvider):
    """DingTalk group robot provider supporting HMAC-SHA256 signing and mobile @ mentions."""

    id = "dingding"
    label = "钉钉"
    description = "钉钉自定义群机器人（支持加签安全校验、专有钉钉私有域名与手机号 @ 提醒）"
    transport = "http"
    supports_at = True
    supports_markdown = True
    at_help = "钉钉成员手机号（多个用逗号分隔），或填 all @所有人"
    default_rate_limit = RateLimit(limit=20, window_seconds=60.0)
    fields = (
        FieldSpec(
            key="domain_preset",
            label="接入域名",
            kind="select",
            required=True,
            default="public",
            options=(
                {"value": "public", "label": "公有云 (oapi.dingtalk.com)"},
                {"value": "custom", "label": "专有钉钉 / 私有化域名"},
            ),
        ),
        FieldSpec(
            key="custom_domain",
            label="私有化网关地址",
            kind="text",
            required=False,
            placeholder="https://oapi.example.com",
            help="仅在选择私有化域名时填写",
            show_if={"domain_preset": "custom"},
        ),
        FieldSpec(
            key="token",
            label="机器人地址 / Access Token",
            kind="secret",
            required=True,
            secret=True,
            placeholder="粘贴机器人 Webhook 地址，或 access_token= 后面那一段",
            help="自定义机器人的 Webhook 地址，形如 https://oapi.dingtalk.com/robot/send?access_token=xxx",
        ),
        FieldSpec(
            key="sign_secret",
            label="加签密钥 (可选)",
            kind="secret",
            required=False,
            secret=True,
            placeholder="开启钉钉机器人「加签」时填写（SEC...），可留空",
            help="在钉钉机器人安全设置勾选「加签」时填写密钥",
        ),
        FieldSpec(
            key="at",
            label="默认 @ 成员",
            kind="text",
            required=False,
            max_length=128,
            placeholder="钉钉成员手机号（多个用逗号分隔），或填 all @所有人",
        ),
    )

    def match_url(self, url: str) -> bool:
        try:
            parts = urlsplit(url)
        except ValueError:
            return False
        return (parts.hostname or "").lower() == "oapi.dingtalk.com"

    def normalize(
        self,
        raw_settings: dict[str, Any] | None,
        raw_secrets: dict[str, Any] | None,
        saved_secrets: dict[str, str] | None = None,
        *,
        allow_reuse_saved: bool = True,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        settings = raw_settings or {}
        preset = clean_str(settings.get("domain_preset") or "public").lower()
        if preset == "default":
            preset = "public"
        if preset not in ("public", "custom"):
            raise ValueError(f"不支持的钉钉域名模式: {preset}")

        custom_domain = clean_str(settings.get("custom_domain"))
        at = normalize_at_value(settings.get("at"))

        raw_token = resolve_secret_value(
            "token",
            raw_secrets,
            saved_secrets,
            required=True,
            missing_error="请填写钉钉机器人地址",
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
        if not lowered.startswith(("http://", "https://")) and lowered.split("/", 1)[0].split("?", 1)[0] == "oapi.dingtalk.com":
            raw_token = "https://" + raw_token
            lowered = raw_token.lower()

        if lowered.startswith(("http://", "https://")) or "://" in raw_token:
            detected = detect_provider_from_url(raw_token)
            if detected and detected != self.id:
                other_label = {"wecom": "企业微信", "feishu": "飞书", "slack": "Slack"}.get(detected, detected)
                raise ValueError(f"这是{other_label}的机器人地址,和选择的 IM 类型(钉钉)不一致")
            try:
                parts = urlsplit(raw_token)
                host = (parts.hostname or "").lower()
            except ValueError:
                raise ValueError(f"不认识的机器人地址,钉钉请填写{DINGDING_HINT}") from None

            if parts.scheme.lower() not in ("http", "https") or not host or parts.path != DINGDING_PATH:
                raise ValueError(f"不认识的机器人地址,钉钉请填写{DINGDING_HINT}")

            if preset == "public" and host != "oapi.dingtalk.com":
                raise ValueError(f"不认识的机器人地址,钉钉请填写{DINGDING_HINT}")

            token = (parse_qs(parts.query).get("access_token") or [""])[0].strip()
            if not token:
                raise ValueError(f"机器人地址里没有 token,钉钉请填写{DINGDING_HINT}")

            if preset == "custom" and not custom_domain:
                port_part = f":{parts.port}" if parts.port is not None else ""
                custom_domain = f"{parts.scheme.lower()}://{host}{port_part}"

        if has_control_char(token) or not TOKEN_PART_RE.fullmatch(token):
            raise ValueError(f"机器人 token 格式不对,钉钉请填写{DINGDING_HINT}")

        normalized_custom = ""
        if preset == "custom":
            normalized_custom = normalize_custom_base_url(custom_domain, allow_http=False, field_label="钉钉私有化域名")

        out_settings: dict[str, Any] = {"domain_preset": preset, "at": at}
        if normalized_custom:
            out_settings["custom_domain"] = normalized_custom

        out_secrets: dict[str, str] = {"token": token}
        if sign_secret:
            out_secrets["sign_secret"] = sign_secret
        return out_settings, out_secrets

    def build_http(self, cfg: ChannelConfig, msg: NotificationMessage) -> HttpPreparedRequest:
        preset = clean_str(cfg.settings.get("domain_preset") or "public").lower()
        base = DINGDING_PUBLIC_BASE
        if preset == "custom":
            custom = clean_str(cfg.settings.get("custom_domain"))
            if not custom:
                raise ValueError("钉钉私有化域名未配置")
            base = custom.rstrip("/")

        token = clean_str(cfg.secrets.get("token"))
        if not token:
            raise ValueError("钉钉机器人 token 未配置")

        title = clean_str(msg.title) or "通知"
        body_text = msg.markdown or msg.plain_text or ""
        mentions, mention_all = resolve_mentions(cfg, msg)

        if mentions:
            missing_tokens = [f"@{mobile}" for mobile in mentions if f"@{mobile}" not in body_text]
            if missing_tokens:
                at_line = " ".join(missing_tokens)
                body_text = f"{body_text}\n{at_line}" if body_text else at_line

        payload: dict[str, Any] = {
            "msgtype": "markdown",
            "markdown": {"title": title, "text": body_text},
        }
        if mentions or mention_all:
            at_obj: dict[str, Any] = {}
            if mentions:
                at_obj["atMobiles"] = mentions
            if mention_all:
                at_obj["isAtAll"] = True
            payload["at"] = at_obj

        url = f"{base}{DINGDING_PATH}?access_token={token}"
        sign_secret = clean_str(cfg.secrets.get("sign_secret"))
        if sign_secret:
            ts_ms = str(int(time.time() * 1000))
            sign = compute_dingding_sign(ts_ms, sign_secret)
            url = f"{url}&timestamp={ts_ms}&sign={sign}"

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

        errcode = data.get("errcode")
        if is_zero(errcode):
            return make_delivery_result(True, OK_MESSAGE, status_code=status_code, provider_code="0")

        code_str = "" if errcode is None else str(errcode)
        retryable = status_code == 429 or status_code >= 500 or code_str in ("130101", "-1")
        return make_delivery_result(
            False,
            format_failure(status_code, errcode, data.get("errmsg"), text),
            status_code=status_code,
            provider_code=code_str,
            retryable=retryable,
            retry_after=retry_after,
        )
