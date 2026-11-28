"""Shared validation, formatting, truncation, mention, and response helpers for providers."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit

from ..models import ChannelConfig, DeliveryResult, HttpPreparedRequest, NotificationMessage
from ..ssrf import precheck_target_url

OK_MESSAGE = "发送成功"
MAX_TOKEN = 512
MAX_AT = 128
MAX_MESSAGE = 300
TOKEN_PART_RE = re.compile(r"[A-Za-z0-9_-]+")


def clean_str(value: Any) -> str:
    """Convert optional value to stripped string."""
    return "" if value is None else str(value).strip()


def has_control_char(value: str) -> bool:
    """Return True if value contains ASCII control characters."""
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


def has_crlf(value: str) -> bool:
    """Return True if value contains CR, LF, or NUL bytes."""
    return "\r" in value or "\n" in value or "\x00" in value


def normalize_at_value(value: Any) -> str:
    """Validate and normalize channel default @ mention text."""
    if value is None:
        return ""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("默认 @ 人的格式不正确")
    text = str(value).strip()
    if len(text) > MAX_AT:
        raise ValueError(f"默认 @ 人最多 {MAX_AT} 个字符")
    if has_control_char(text):
        raise ValueError("默认 @ 人不能包含换行等控制字符")
    return text


def fit_utf8_bytes(text: str, limit: int) -> str:
    """Truncate text cleanly at UTF-8 byte boundary with trailing ellipsis."""
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    if limit <= 0:
        return ""
    ellipsis = "…"
    ell_len = len(ellipsis.encode("utf-8"))
    if limit <= ell_len:
        return data[:limit].decode("utf-8", errors="ignore")
    return data[: limit - ell_len].decode("utf-8", errors="ignore") + ellipsis


def clip_text(text: object, limit: int = MAX_MESSAGE) -> str:
    """Clip text to at most limit characters with trailing ellipsis."""
    value = str(text)
    if len(value) <= limit:
        return value
    return value[: max(limit - 1, 0)] + "…" if limit > 1 else value[:limit]


def is_zero(value: Any) -> bool:
    """Check whether JSON error code equals zero without treating False as 0."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value == 0
    return isinstance(value, str) and value.strip() == "0"


def header_get(headers: Mapping[str, str] | None, name: str) -> str | None:
    """Case-insensitive HTTP header lookup."""
    if not headers:
        return None
    target = name.lower()
    for key, val in headers.items():
        if str(key).lower() == target:
            return str(val)
    return None


def parse_retry_after(headers: Mapping[str, str] | None) -> float | None:
    """Parse Retry-After or X-RateLimit-Reset-After into seconds if present."""
    for header_name in ("Retry-After", "X-RateLimit-Reset-After"):
        raw = header_get(headers, header_name)
        if raw is None:
            continue
        text = raw.strip()
        if not text:
            continue
        try:
            seconds = float(text)
            return max(seconds, 0.0)
        except ValueError:
            pass
        if header_name == "Retry-After":
            try:
                dt = parsedate_to_datetime(text)
                from datetime import datetime, timezone

                delta = (dt - datetime.now(timezone.utc)).total_seconds()
                return max(delta, 0.0)
            except Exception:
                continue
    return None


def brief_body(text: str) -> str:
    """Return short non-HTML response snippet for failure diagnostics."""
    stripped = (text or "").strip()
    return "" if stripped.startswith("<") else stripped[:200]


def decode_body(body_text: str | bytes | bytearray | None) -> str:
    """Decode bytes/bytearray HTTP bodies cleanly."""
    if isinstance(body_text, (bytes, bytearray)):
        return bytes(body_text).decode("utf-8", errors="replace")
    return body_text or ""


def format_failure(status: int, code: Any, msg: Any, text: str) -> str:
    """Format standardized Chinese error summary capped to MAX_MESSAGE chars."""
    head = f"HTTP {status}" if status != 200 else ""
    detail = ""
    if code is not None:
        detail = f"错误码 {code}" + (f":{msg}" if msg not in (None, "") else "")
    elif msg not in (None, ""):
        detail = str(msg)
    elif brief_body(text):
        detail = brief_body(text)
    if head and detail:
        return clip_text(f"{head},{detail}")
    return clip_text(head or detail or "IM 接口没有返回内容")


def resolve_secret_value(
    key: str,
    raw_secrets: dict[str, Any] | None,
    saved_secrets: dict[str, str] | None,
    *,
    required: bool,
    missing_error: str,
    allow_reuse_saved: bool = True,
    max_len: int = MAX_TOKEN,
    allow_whitespace: bool = False,
) -> str:
    """Extract and validate a secret field, reusing saved_secrets[key] when left blank."""
    raw_val = (raw_secrets or {}).get(key)
    if raw_val is not None and not isinstance(raw_val, str):
        raise ValueError(f"凭据字段 {key} 格式不正确")
    text = "" if raw_val is None else raw_val.strip()
    if text:
        if has_control_char(text):
            raise ValueError(f"凭据字段 {key} 不能包含换行等控制字符")
        if not allow_whitespace and any(ch.isspace() for ch in text):
            raise ValueError(f"凭据字段 {key} 中间不能有空格")
        if len(text) > max_len:
            raise ValueError(f"凭据字段 {key} 太长(最多 {max_len} 个字符)")
        return text

    if allow_reuse_saved and isinstance(saved_secrets, dict):
        saved = clean_str(saved_secrets.get(key))
        if saved:
            return saved

    if required:
        raise ValueError(missing_error)
    return ""


def normalize_custom_base_url(
    raw_url: Any,
    *,
    allow_http: bool = False,
    field_label: str = "自定义域名",
) -> str:
    """Validate custom base URL with static SSRF precheck and strip trailing slash."""
    text = clean_str(raw_url)
    if not text:
        raise ValueError(f"请填写{field_label}")
    precheck_target_url(text, allow_http=allow_http)
    parts = urlsplit(text)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower().rstrip(".")
    port = parts.port
    default_port = 443 if scheme == "https" else 80
    host_part = f"[{host}]" if ":" in host else host
    port_suffix = f":{port}" if port is not None and port != default_port else ""
    path_prefix = parts.path.rstrip("/")
    return f"{scheme}://{host_part}{port_suffix}{path_prefix}"


def resolve_mentions(cfg: ChannelConfig, msg: NotificationMessage) -> tuple[list[str], bool]:
    """Resolve ordered mention tokens and mention_all flag from msg or channel default."""
    mention_all = bool(msg.mention_all)
    tokens: list[str] = []

    def _consume(value: str) -> None:
        nonlocal mention_all
        for piece in re.split(r"[,;\s]+", value.strip()):
            item = piece.strip()
            if not item:
                continue
            if item.lower() in ("all", "@all"):
                mention_all = True
                continue
            cleaned = item[1:] if item.startswith("@") else item
            if cleaned and cleaned not in tokens:
                tokens.append(cleaned)

    if msg.mentions or msg.at or msg.mention_all:
        for m in msg.mentions:
            _consume(str(m))
        if msg.at:
            _consume(msg.at)
    else:
        default_at = clean_str(cfg.settings.get("at"))
        if default_at:
            _consume(default_at)

    return tokens, mention_all


def json_http_request(
    method: str,
    url: str,
    payload: Any,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 10.0,
    allow_http: bool = False,
) -> HttpPreparedRequest:
    """Build JSON HttpPreparedRequest with deterministic UTF-8 body."""
    merged_headers = {"Content-Type": "application/json; charset=utf-8"}
    if headers:
        merged_headers.update(headers)
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return HttpPreparedRequest(
        method=method.upper(),
        url=url,
        headers=merged_headers,
        body=body,
        timeout=timeout,
        allow_http=allow_http,
        allow_redirects=False,
    )


def parse_json_dict(text: str) -> dict[str, Any] | None:
    """Safely parse JSON object body, returning None on invalid or non-dict JSON."""
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


def make_delivery_result(
    ok: bool,
    message: str,
    *,
    status_code: int | None = None,
    provider_code: str = "",
    retryable: bool = False,
    retry_after: float | None = None,
) -> DeliveryResult:
    """Construct DeliveryResult with standardized fields."""
    return DeliveryResult(
        ok=ok,
        message=message,
        status_code=status_code,
        provider_code=provider_code,
        retryable=retryable,
        retry_after=retry_after,
    )
