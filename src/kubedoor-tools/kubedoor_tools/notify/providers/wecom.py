"""WeCom (企业微信) group webhook notification provider."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qs, urlsplit

from ..models import ChannelConfig, DeliveryResult, FieldSpec, HttpPreparedRequest, NotificationMessage, RateLimit
from ..registry import detect_provider_from_url, register_provider
from ..registry import NotificationProvider
from ._common import (
    OK_MESSAGE,
    TOKEN_PART_RE,
    clean_str,
    decode_body,
    fit_utf8_bytes,
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

WECOM_PUBLIC_BASE = "https://qyapi.weixin.qq.com"
WECOM_PATH = "/cgi-bin/webhook/send"
WECOM_MAX_BYTES = 4096
WECOM_HINT = "群机器人 Webhook 地址(https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=…),或 key= 后面那一段"


@register_provider
class WeComProvider(NotificationProvider):
    """WeCom group robot provider supporting public cloud and custom private domains."""

    id = "wecom"
    label = "企业微信"
    description = "企业微信群机器人 Webhook（支持公有云与私有化域名，UTF-8 4096 字节安全截断）"
    transport = "http"
    supports_at = True
    supports_markdown = True
    at_help = "企业微信成员的 userid（账号，不是姓名），多个用逗号分隔"
    default_rate_limit = RateLimit(limit=20, window_seconds=60.0)
    fields = (
        FieldSpec(
            key="domain_preset",
            label="接入域名",
            kind="select",
            required=True,
            default="public",
            options=(
                {"value": "public", "label": "公有云 (qyapi.weixin.qq.com)"},
                {"value": "custom", "label": "私有化域名"},
            ),
        ),
        FieldSpec(
            key="custom_domain",
            label="私有化网关地址",
            kind="text",
            required=False,
            placeholder="https://qyapi.example.com",
            help="仅在选择私有化域名时填写，必须为 HTTPS（或已放行的内网白名单）",
            show_if={"domain_preset": "custom"},
        ),
        FieldSpec(
            key="token",
            label="机器人地址 / Key",
            kind="secret",
            required=True,
            secret=True,
            placeholder="粘贴群机器人 Webhook 地址，或 key= 后面那一段",
            help="群机器人的 Webhook 地址，形如 https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx",
        ),
        FieldSpec(
            key="at",
            label="默认 @ 成员",
            kind="text",
            required=False,
            max_length=128,
            placeholder="企业微信成员的 userid（多个用逗号分隔），可以不填",
        ),
    )

    def match_url(self, url: str) -> bool:
        try:
            parts = urlsplit(url)
        except ValueError:
            return False
        return (parts.hostname or "").lower() == "qyapi.weixin.qq.com"

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
            raise ValueError(f"不支持的企业微信域名模式: {preset}")

        custom_domain = clean_str(settings.get("custom_domain"))
        at = normalize_at_value(settings.get("at"))

        raw_token = resolve_secret_value(
            "token",
            raw_secrets,
            saved_secrets,
            required=True,
            missing_error="请填写企业微信机器人地址",
            allow_reuse_saved=allow_reuse_saved,
        )

        token = raw_token
        lowered = raw_token.lower()
        if not lowered.startswith(("http://", "https://")) and lowered.split("/", 1)[0].split("?", 1)[0] == "qyapi.weixin.qq.com":
            raw_token = "https://" + raw_token
            lowered = raw_token.lower()

        if lowered.startswith(("http://", "https://")) or "://" in raw_token:
            detected = detect_provider_from_url(raw_token)
            if detected and detected != self.id:
                other_label = {"dingding": "钉钉", "feishu": "飞书", "slack": "Slack"}.get(detected, detected)
                raise ValueError(f"这是{other_label}的机器人地址,和选择的 IM 类型(企业微信)不一致")
            try:
                parts = urlsplit(raw_token)
                host = (parts.hostname or "").lower()
            except ValueError:
                raise ValueError(f"不认识的机器人地址,企业微信请填写{WECOM_HINT}") from None

            if parts.scheme.lower() not in ("http", "https") or not host or parts.path != WECOM_PATH:
                raise ValueError(f"不认识的机器人地址,企业微信请填写{WECOM_HINT}")

            if preset == "public" and host != "qyapi.weixin.qq.com":
                raise ValueError(f"不认识的机器人地址,企业微信请填写{WECOM_HINT}")

            token = (parse_qs(parts.query).get("key") or [""])[0].strip()
            if not token:
                raise ValueError(f"机器人地址里没有 token,企业微信请填写{WECOM_HINT}")

            if preset == "custom" and not custom_domain:
                port_part = f":{parts.port}" if parts.port is not None else ""
                custom_domain = f"{parts.scheme.lower()}://{host}{port_part}"

        if has_control_char(token) or not TOKEN_PART_RE.fullmatch(token):
            raise ValueError(f"机器人 token 格式不对,企业微信请填写{WECOM_HINT}")

        normalized_custom = ""
        if preset == "custom":
            normalized_custom = normalize_custom_base_url(custom_domain, allow_http=False, field_label="企业微信私有化域名")

        out_settings: dict[str, Any] = {"domain_preset": preset, "at": at}
        if normalized_custom:
            out_settings["custom_domain"] = normalized_custom
        return out_settings, {"token": token}

    def build_http(self, cfg: ChannelConfig, msg: NotificationMessage) -> HttpPreparedRequest:
        preset = clean_str(cfg.settings.get("domain_preset") or "public").lower()
        base = WECOM_PUBLIC_BASE
        if preset == "custom":
            custom = clean_str(cfg.settings.get("custom_domain"))
            if not custom:
                raise ValueError("企业微信私有化域名未配置")
            base = custom.rstrip("/")

        token = clean_str(cfg.secrets.get("token"))
        if not token:
            raise ValueError("企业微信机器人 token 未配置")

        body_text = msg.markdown or msg.plain_text or ""
        title = clean_str(msg.title)
        if title and title != "通知":
            first_line = body_text.splitlines()[0] if body_text else ""
            if title not in first_line:
                body_text = f"**{title}**\n{body_text}" if body_text else f"**{title}**"

        mentions, _ = resolve_mentions(cfg, msg)
        suffix = "".join(f"<@{uid}>" for uid in mentions)
        body = fit_utf8_bytes(body_text, WECOM_MAX_BYTES - len(suffix.encode("utf-8"))) + suffix

        url = f"{base}{WECOM_PATH}?key={token}"
        payload = {"msgtype": "markdown", "markdown": {"content": body}}
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
        retryable = status_code == 429 or status_code >= 500 or code_str in ("45009", "-1")
        return make_delivery_result(
            False,
            format_failure(status_code, errcode, data.get("errmsg"), text),
            status_code=status_code,
            provider_code=code_str,
            retryable=retryable,
            retry_after=retry_after,
        )
