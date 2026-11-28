"""Custom Generic Webhook provider with Fail-Closed SSRF protection and optional HMAC-SHA256 signing."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from ..models import ChannelConfig, DeliveryResult, FieldSpec, HttpPreparedRequest, NotificationMessage, RateLimit
from ..registry import NotificationProvider, register_provider
from ..ssrf import precheck_target_url
from ._common import (
    OK_MESSAGE,
    clean_str,
    decode_body,
    format_failure,
    has_crlf,
    make_delivery_result,
    parse_retry_after,
    resolve_secret_value,
)

_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]{1,128}$")
_FORBIDDEN_HEADERS = frozenset({"host", "content-length", "transfer-encoding", "connection"})
_DEFAULT_HMAC_HEADER = "X-KubeDoor-Signature-256"


def _normalize_headers(raw_headers: Any) -> dict[str, str]:
    if raw_headers is None or raw_headers == "":
        return {}
    if isinstance(raw_headers, str):
        stripped = raw_headers.strip()
        if not stripped:
            return {}
        try:
            parsed = json.loads(stripped)
        except ValueError as exc:
            raise ValueError("自定义请求头必须是合法的 JSON 对象") from exc
    elif isinstance(raw_headers, dict):
        parsed = raw_headers
    else:
        raise ValueError("自定义请求头格式不正确")

    if not isinstance(parsed, dict):
        raise ValueError("自定义请求头必须是 JSON 对象")

    out: dict[str, str] = {}
    for k, v in parsed.items():
        key = str(k).strip()
        val = "" if v is None else str(v)
        if not _HEADER_NAME_RE.fullmatch(key) or has_crlf(key) or has_crlf(val):
            raise ValueError(f"自定义请求头不合法: {key!r}")
        if key.lower() in _FORBIDDEN_HEADERS:
            raise ValueError(f"不允许覆盖受保护请求头: {key}")
        out[key] = val
    return out


@register_provider
class WebhookProvider(NotificationProvider):
    """Custom generic HTTP webhook provider emitting structured JSON event envelopes."""

    id = "webhook"
    label = "自定义 Webhook"
    description = "通用 JSON Webhook（默认阻断私网/云元数据 SSRF，支持自定义请求头与 HMAC-SHA256 签名）"
    transport = "http"
    supports_at = False
    supports_markdown = True
    default_rate_limit = RateLimit(limit=30, window_seconds=60.0)
    fields = (
        FieldSpec(
            key="url",
            label="Webhook URL",
            kind="text",
            required=True,
            max_length=1024,
            placeholder="https://hooks.example.com/kubedoor/events",
            help="默认仅允许公网 HTTPS；如需 HTTP 或内网目标请在管理员白名单显式放行",
        ),
        FieldSpec(
            key="allow_http",
            label="允许明文 HTTP",
            kind="boolean",
            required=False,
            default=False,
            help="仅建议内网受信服务开启，默认关闭并强制 HTTPS",
        ),
        FieldSpec(
            key="method",
            label="HTTP 方法",
            kind="select",
            required=True,
            default="POST",
            options=(
                {"value": "POST", "label": "POST"},
                {"value": "PUT", "label": "PUT"},
            ),
        ),
        FieldSpec(
            key="headers_json",
            label="自定义请求头",
            kind="headers",
            required=False,
            default={},
            placeholder='{"X-Source": "kubedoor"}',
            help="可选 JSON 对象或键值对，禁止换行注入与 Host 覆盖",
        ),
        FieldSpec(
            key="auth_header_name",
            label="认证请求头名称 (可选)",
            kind="text",
            required=False,
            max_length=128,
            placeholder="Authorization 或 X-Api-Key",
        ),
        FieldSpec(
            key="auth_header_value",
            label="认证请求头值 (可选)",
            kind="secret",
            required=False,
            secret=True,
            max_length=1024,
            placeholder="Bearer xxx 或 API Token",
        ),
        FieldSpec(
            key="hmac_header",
            label="HMAC 签名请求头",
            kind="text",
            required=False,
            default=_DEFAULT_HMAC_HEADER,
            max_length=128,
            placeholder=_DEFAULT_HMAC_HEADER,
        ),
        FieldSpec(
            key="hmac_secret",
            label="HMAC-SHA256 签名密钥 (可选)",
            kind="secret",
            required=False,
            secret=True,
            max_length=512,
            placeholder="填写后将在请求头附带 sha256=<hex> 签名",
        ),
    )

    def normalize(
        self,
        raw_settings: dict[str, Any] | None,
        raw_secrets: dict[str, Any] | None,
        saved_secrets: dict[str, str] | None = None,
        *,
        allow_reuse_saved: bool = True,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        settings = raw_settings or {}
        allow_http = settings.get("allow_http", False)
        if not isinstance(allow_http, bool):
            raise ValueError("allow_http 必须是布尔值")

        url = clean_str(settings.get("url"))
        if not url:
            raise ValueError("请填写 Webhook URL")
        if len(url) > 1024:
            raise ValueError("Webhook URL 太长(最多 1024 个字符)")
        precheck_target_url(url, allow_http=allow_http)

        method = clean_str(settings.get("method") or "POST").upper()
        if method not in ("POST", "PUT"):
            raise ValueError("Webhook HTTP 方法仅支持 POST 或 PUT")

        headers_map = _normalize_headers(settings.get("headers_json"))

        auth_header_name = clean_str(settings.get("auth_header_name"))
        if auth_header_name:
            if not _HEADER_NAME_RE.fullmatch(auth_header_name) or has_crlf(auth_header_name):
                raise ValueError("认证请求头名称格式不正确")
            if auth_header_name.lower() in _FORBIDDEN_HEADERS:
                raise ValueError(f"不允许使用受保护请求头: {auth_header_name}")

        hmac_header = clean_str(settings.get("hmac_header")) or _DEFAULT_HMAC_HEADER
        if not _HEADER_NAME_RE.fullmatch(hmac_header) or has_crlf(hmac_header):
            raise ValueError("HMAC 签名请求头名称格式不正确")
        if hmac_header.lower() in _FORBIDDEN_HEADERS:
            raise ValueError(f"不允许使用受保护请求头: {hmac_header}")

        auth_header_value = resolve_secret_value(
            "auth_header_value",
            raw_secrets,
            saved_secrets,
            required=False,
            missing_error="",
            allow_reuse_saved=allow_reuse_saved,
            max_len=1024,
            allow_whitespace=True,
        )
        if auth_header_value and not auth_header_name:
            auth_header_name = "Authorization"

        hmac_secret = resolve_secret_value(
            "hmac_secret",
            raw_secrets,
            saved_secrets,
            required=False,
            missing_error="",
            allow_reuse_saved=allow_reuse_saved,
            max_len=512,
        )

        out_settings: dict[str, Any] = {
            "url": url,
            "allow_http": allow_http,
            "method": method,
            "headers_json": headers_map,
            "auth_header_name": auth_header_name,
            "hmac_header": hmac_header,
        }
        out_secrets: dict[str, str] = {}
        if auth_header_value:
            out_secrets["auth_header_value"] = auth_header_value
        if hmac_secret:
            out_secrets["hmac_secret"] = hmac_secret
        return out_settings, out_secrets

    def build_http(self, cfg: ChannelConfig, msg: NotificationMessage) -> HttpPreparedRequest:
        url = clean_str(cfg.settings.get("url"))
        if not url:
            raise ValueError("Webhook URL 未配置")
        allow_http = bool(cfg.settings.get("allow_http", False))
        method = clean_str(cfg.settings.get("method") or "POST").upper()
        if method not in ("POST", "PUT"):
            raise ValueError(f"不支持的 Webhook 方法: {method}")

        envelope = {
            "schema_version": 1,
            "event_id": msg.event_id or str(uuid.uuid4()),
            "timestamp": msg.timestamp or datetime.now(timezone.utc).isoformat(),
            "env": msg.env,
            "category": msg.category,
            "severity": msg.severity,
            "title": clean_str(msg.title) or "通知",
            "body": msg.markdown or msg.plain_text or "",
            "at": msg.at or clean_str(cfg.settings.get("at")),
            "items": [item.to_dict() for item in msg.items],
            "links": [dict(link) for link in msg.links],
        }
        raw_bytes = json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

        headers: dict[str, str] = {"Content-Type": "application/json; charset=utf-8"}
        custom_headers = cfg.settings.get("headers_json")
        if isinstance(custom_headers, dict):
            for k, v in custom_headers.items():
                headers[str(k)] = str(v)

        auth_name = clean_str(cfg.settings.get("auth_header_name"))
        auth_val = clean_str(cfg.secrets.get("auth_header_value"))
        if auth_name and auth_val:
            headers[auth_name] = auth_val

        hmac_secret = clean_str(cfg.secrets.get("hmac_secret"))
        if hmac_secret:
            hmac_header = clean_str(cfg.settings.get("hmac_header")) or _DEFAULT_HMAC_HEADER
            sig = hmac.new(hmac_secret.encode("utf-8"), raw_bytes, hashlib.sha256).hexdigest()
            headers[hmac_header] = f"sha256={sig}"

        return HttpPreparedRequest(
            method=method,
            url=url,
            headers=headers,
            body=raw_bytes,
            timeout=10.0,
            allow_http=allow_http,
            allow_redirects=False,
        )

    def parse_http_response(
        self,
        status_code: int,
        headers: Mapping[str, str],
        body_text: str | bytes,
    ) -> DeliveryResult:
        text = decode_body(body_text)
        retry_after = parse_retry_after(headers)
        if 200 <= status_code < 300:
            return make_delivery_result(
                True,
                OK_MESSAGE,
                status_code=status_code,
                provider_code=str(status_code),
            )
        retryable = status_code == 429 or status_code >= 500
        return make_delivery_result(
            False,
            format_failure(status_code, None, None, text),
            status_code=status_code,
            provider_code=str(status_code),
            retryable=retryable,
            retry_after=retry_after,
        )

    def redact_values(self, cfg: ChannelConfig) -> tuple[str, ...]:
        values = list(super().redact_values(cfg))
        headers = cfg.settings.get("headers_json")
        if isinstance(headers, dict):
            for k, v in headers.items():
                key_low = str(k).lower()
                if v and any(marker in key_low for marker in ("auth", "token", "key", "secret")):
                    val_str = str(v)
                    if val_str not in values:
                        values.append(val_str)
        return tuple(values)
