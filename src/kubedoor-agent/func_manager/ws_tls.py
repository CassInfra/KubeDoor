"""WebSocket TLS verification, Authorization header extraction, and credential redaction."""

from __future__ import annotations

import base64
import os
import re
import ssl
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from loguru import logger

_URL_CRED_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://)([^/\s@:?#]*):([^/\s@?#]*)@")


def prepare_master_ws_connection(
    master_url: str,
    query_params: dict | None = None,
) -> tuple[str, dict[str, str]]:
    """Strip userinfo from master WebSocket URL and emit a Basic Authorization header."""
    parts = urlsplit(master_url)
    headers: dict[str, str] = {}
    if parts.username is not None or parts.password is not None:
        username = unquote(parts.username or "")
        password = unquote(parts.password or "")
        token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {token}"

    clean_netloc = parts.netloc.rsplit("@", 1)[-1]
    if query_params is not None:
        path = parts.path if parts.path.endswith("/ws") else parts.path.rstrip("/") + "/ws"
        merged = dict(parse_qsl(parts.query, keep_blank_values=True))
        for key, value in query_params.items():
            if value is not None:
                merged[str(key)] = str(value)
        query = urlencode(merged)
    else:
        path = parts.path
        query = parts.query

    clean_uri = urlunsplit((parts.scheme, clean_netloc, path, query, parts.fragment))
    return clean_uri, headers


def _decode_ca_data(raw: str) -> str:
    text = raw.strip()
    if "BEGIN CERTIFICATE" in text:
        return text
    return base64.b64decode(text, validate=True).decode("utf-8")


def build_master_ssl_context(uri: str) -> ssl.SSLContext | None:
    """Build an SSLContext for wss:// master connections (None for ws://)."""
    scheme = urlsplit(uri).scheme.lower()
    if scheme != "wss":
        return None

    skip_verify = os.environ.get("MASTER_TLS_INSECURE_SKIP_VERIFY", "").strip().lower() in ("1", "true", "yes")
    if skip_verify:
        logger.warning("MASTER_TLS_INSECURE_SKIP_VERIFY 已启用，跳过对 kubedoor-master 的 TLS 证书校验")
        return ssl._create_unverified_context()

    ca_file = os.environ.get("MASTER_CA_CERT", "").strip()
    if ca_file:
        return ssl.create_default_context(cafile=ca_file)

    ca_data = os.environ.get("MASTER_CA_CERT_DATA", "").strip()
    if ca_data:
        return ssl.create_default_context(cadata=_decode_ca_data(ca_data))

    return ssl.create_default_context()


def redact_credentials(text: str | None) -> str:
    """Mask scheme://user:pass@host credentials in log strings and exceptions."""
    if not text:
        return ""
    return _URL_CRED_RE.sub(r"\1***:***@", str(text))
