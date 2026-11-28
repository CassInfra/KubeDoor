"""Shared internal token authentication between kubedoor-agent, pod-mgr, and agent CronJobs."""

from __future__ import annotations

import hmac
import os
import secrets
from pathlib import Path

HEADER = "X-Kubedoor-Internal-Token"
DEFAULT_TOKEN_FILE = "/run/kubedoor/internal.token"


def generate_token() -> str:
    """Generate a fresh 32-byte (64-hex) internal shared secret."""
    return secrets.token_hex(32)


def load_internal_token() -> str:
    """Load the internal shared token from env or runtime token file."""
    token = os.environ.get("KUBEDOOR_INTERNAL_TOKEN", "").strip()
    if token:
        return token
    token_file = (os.environ.get("KUBEDOOR_INTERNAL_TOKEN_FILE") or "").strip() or DEFAULT_TOKEN_FILE
    try:
        return Path(token_file).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def verify_internal_token(supplied: str | None) -> bool:
    """Fail-closed constant-time verification of the internal token header."""
    expected = load_internal_token()
    return (
        bool(expected)
        and bool(supplied)
        and isinstance(supplied, str)
        and hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))
    )


def auth_headers() -> dict[str, str]:
    """Build internal authentication headers when a token is configured."""
    token = load_internal_token()
    return {HEADER: token} if token else {}
