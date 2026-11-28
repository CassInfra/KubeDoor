"""Credential redaction and secret masking utilities for notification logs and errors."""

from __future__ import annotations

import re
from typing import Iterable
from urllib.parse import quote, quote_plus

_SLACK_URL_RE = re.compile(
    r"(https?://hooks\.slack\.com/services/)([A-Za-z0-9_-]+)/([A-Za-z0-9_-]+)/([A-Za-z0-9_-]+)",
    re.IGNORECASE,
)
_SLACK_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_/-])([A-Za-z0-9_-]{6,})/([A-Za-z0-9_-]{6,})/([A-Za-z0-9_-]{6,})(?![A-Za-z0-9_/-])"
)
_QUERY_CRED_RE = re.compile(
    r"([?&](?:key|access_token|sign|sig|token|secret|api_key|password)=)([^&\s'\"#<>]+)",
    re.IGNORECASE,
)
_HEADER_JSON_RE = re.compile(
    r"(['\"]?(?:authorization|x-api-key|x-secret|x-auth-token|x-kubedoor-token|proxy-authorization)['\"]?\s*[:=]\s*['\"])([^'\"]+)(['\"])",
    re.IGNORECASE,
)
_HEADER_LINE_RE = re.compile(
    r"(?im)\b(authorization|x-api-key|x-secret|x-auth-token|x-kubedoor-token|proxy-authorization)\b(\s*:\s*)(?!(?:(?:Bearer|Basic|Token)\s+)?(?:\*\*\*|\[REDACTED\])(?:\s|$))((?:(?:Bearer|Basic|Token)\s+)?[^\s,;'\}\]\"]+)"
)
_BEARER_RE = re.compile(
    r"\bBearer\s+(?!\*\*\*|\[REDACTED\])[A-Za-z0-9._~+/=-]{6,}",
    re.IGNORECASE,
)
_USERINFO_URL_RE = re.compile(r"(https?://)([^:/\s@]+):([^@/\s]+)@", re.IGNORECASE)


def _expand_secrets(secrets: Iterable[str]) -> list[str]:
    tokens: set[str] = set()
    for item in secrets:
        if item is None:
            continue
        raw = str(item).strip()
        if not raw:
            continue
        tokens.add(raw)
        if len(raw) >= 4:
            q1 = quote(raw, safe="")
            q2 = quote_plus(raw)
            if q1 != raw:
                tokens.add(q1)
            if q2 != raw:
                tokens.add(q2)
        for part in raw.split("/"):
            if len(part) >= 6:
                tokens.add(part)
    # Longest first prevents partial substrings from leaving fragments unredacted.
    return sorted(tokens, key=len, reverse=True)


def redact_text(text: object, secrets: Iterable[str] = (), max_len: int | None = 300) -> str:
    """Scrub explicit secrets, Slack 3-part tokens, query credentials, and auth headers."""
    result = "" if text is None else str(text)
    for token in _expand_secrets(secrets):
        result = result.replace(token, "***")

    result = _SLACK_URL_RE.sub(r"\1***", result)
    result = _SLACK_TOKEN_RE.sub("***", result)
    result = _QUERY_CRED_RE.sub(r"\1***", result)
    result = _HEADER_JSON_RE.sub(r"\1***\3", result)
    result = _HEADER_LINE_RE.sub(r"\1\2***", result)
    result = _BEARER_RE.sub("Bearer ***", result)
    result = _USERINFO_URL_RE.sub(r"\1***:***@", result)

    if max_len is not None:
        limit = int(max_len)
        if limit <= 0:
            return ""
        if len(result) > limit:
            return result[: limit - 1] + "…" if limit > 1 else result[:1]
    return result


def mask_hint(secret: object) -> str:
    """Return last-4 hint for secrets >= 8 chars, or '****' / ''."""
    if secret is None:
        return ""
    text = str(secret).strip()
    if not text:
        return ""
    return "****" + text[-4:] if len(text) >= 8 else "****"
