from __future__ import annotations

import hmac
import json
import os
import re
import yaml
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from kubedoor_tools import redact_export

# 定时/周期任务的 CronJob 由 agent 固定 spec.timeZone=Asia/Shanghai。
# 中国 1991 年后不实行夏令时，用固定 UTC+8，不依赖系统 tzdata。
BEIJING_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def beijing_now() -> datetime:
    return datetime.now(BEIJING_TZ)


class AIError(Exception):
    def __init__(self, message: str, status: int = 400, code: str = "invalid_request", details: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.details = details


@dataclass(frozen=True)
class Identity:
    username: str
    permission: str

    def require_write(self) -> None:
        if self.permission != "rw":
            raise AIError("当前用户只有查询权限", 403, "permission_denied")


def authenticate(headers: Any, expected_token: str) -> Identity:
    token = headers.get("x-kubedoor-token", "")
    if not expected_token or not hmac.compare_digest(token, expected_token):
        raise AIError("内部访问令牌无效", 401, "unauthorized")
    username = headers.get("x-user-name", "").strip()
    permission = headers.get("x-user-permission", "")
    if not username or len(username) > 200 or permission not in ("read", "rw"):
        raise AIError("缺少可信用户身份", 401, "unauthorized")
    return Identity(username, permission)


@dataclass(frozen=True)
class Provider:
    base_url: str
    api_key: str
    model: str

    @classmethod
    def parse(cls, raw: dict) -> "Provider":
        if not isinstance(raw, dict):
            raise AIError("请配置模型地址、API key 和模型名")
        base_url = normalize_provider_url(raw.get("base_url"))
        key, model = raw.get("api_key"), raw.get("model")
        if key is None:
            key = ""
        if not isinstance(key, str) or len(key) > 8192:
            raise AIError("API key 必须为字符串，最多 8192 字符")
        if not isinstance(model, str) or not model.strip() or len(model) > 256:
            raise AIError("模型名不能为空")
        return cls(base_url, key, model.strip())


def normalize_provider_url(raw: Any) -> str:
    """Accept the API base or its complete Chat Completions endpoint."""
    message = "Base URL 必须为 HTTP/HTTPS API 基址，不能包含账号密码、查询参数或片段（例如 https://example.com/v1）。"
    if not isinstance(raw, str):
        raise AIError(message, 400, "provider_address_invalid")
    value = raw.strip()
    if not value or len(value) > 8192 or any(c.isspace() or ord(c) < 32 for c in value) or "\\" in value:
        raise AIError(message, 400, "provider_address_invalid")
    try:
        parsed = urlsplit(value)
        parsed.port  # Validate a supplied port and malformed IPv6 addresses.
    except ValueError:
        raise AIError(message, 400, "provider_address_invalid") from None
    if (parsed.scheme not in ("https", "http") or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment):
        raise AIError(message, 400, "provider_address_invalid")
    path = parsed.path.rstrip("/")
    if path.endswith("/chat/completions"):
        path = path[:-len("/chat/completions")]
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def normalize_scope(raw: dict) -> dict:
    if not isinstance(raw, dict) or not isinstance(raw.get("env"), str) or not raw["env"].strip():
        raise AIError("请选择一个 K8S 集群")
    scope = {"env": raw["env"], "namespace": raw.get("namespace"), "deployment": raw.get("deployment"), "pod": raw.get("pod")}
    if len(scope["env"]) > 253 or any(ord(c) < 32 for c in scope["env"]):
        raise AIError("集群名称无效")
    ns = scope["namespace"]
    if ns is not None and (not isinstance(ns, str) or not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", ns)):
        raise AIError("命名空间无效")
    for kind in ("deployment", "pod"):
        item = scope[kind]
        if item is None:
            continue
        if not isinstance(item, dict) or set(item) != {"namespace", "name"}:
            raise AIError(f"{kind} 必须包含 namespace 和 name")
        if not all(isinstance(item[k], str) and re.fullmatch(r"[a-z0-9]([-a-z0-9.]*[a-z0-9])?", item[k]) for k in item):
            raise AIError(f"{kind} 名称无效")
        if ns is not None and item["namespace"] != ns:
            raise AIError("所选资源不属于当前命名空间")
    if scope["deployment"] and scope["pod"] and scope["deployment"]["namespace"] != scope["pod"]["namespace"]:
        raise AIError("Deployment 与 Pod 必须属于同一命名空间")
    return scope


def redact_values(value: Any, secrets: tuple[str, ...] = ()) -> Any:
    """Remove ephemeral provider values without altering execution arguments."""
    if isinstance(value, dict):
        return {k: redact_values(v, secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_values(v, secrets) for v in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[redacted]")
        return value
    return value


def redact(value: Any, secrets: tuple[str, ...] = ()) -> Any:
    """Export safe UI/model evidence; exact action arguments stay internal."""
    value = redact_export(redact_values(value, secrets))
    if isinstance(value, dict):
        clean = {k: "[redacted]" if str(k).lower() in {"api_key", "apikey", "authorization", "kubeconfig", "client-key-data", "token", "password", "files", "stdin"} else redact(v, secrets) for k, v in value.items()}
        # Existing KubeDoor resource APIs transport manifests as YAML strings.
        # Export safe manifests without altering the frozen execution payload.
        if isinstance(value.get("yaml_content"), str):
            try:
                documents = list(yaml.safe_load_all(value["yaml_content"]))
                clean["yaml_content"] = yaml.safe_dump_all([redact(doc, secrets) for doc in documents], allow_unicode=True, sort_keys=False)
            except (yaml.YAMLError, RecursionError):
                clean["yaml_content"] = "[redacted]"
        # A PATCH body may omit kind=Secret. The API path still identifies it.
        if isinstance(value.get("path"), str) and re.search(r"/secrets(?:/|$)", value["path"]) and "body" in value:
            body = value["body"]
            if isinstance(body, dict):
                clean["body"] = redact_export(body, True)
            elif isinstance(body, list):
                clean["body"] = [{**item, "value": "[redacted]"} if isinstance(item, dict) and "value" in item else redact(item, secrets) for item in body]
            else:
                clean["body"] = "[redacted]"
        return clean
    if isinstance(value, (list, tuple)):
        return [redact(v, secrets) for v in value]
    if isinstance(value, str):
        return re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/-]+", r"\1[redacted]", value)
    return value


class CredentialCipher:
    def __init__(self, key_hex: str):
        try:
            key = bytes.fromhex(key_hex)
        except ValueError:
            key = b""
        if len(key) != 32:
            raise RuntimeError("AI_ENCRYPTION_KEY 必须是 64 位十六进制字符串")
        self.cipher = AESGCM(key)

    def encrypt(self, env: str, content: dict) -> bytes:
        nonce = os.urandom(12)
        return nonce + self.cipher.encrypt(nonce, json.dumps(content, ensure_ascii=False).encode(), env.encode())

    def decrypt(self, env: str, content: bytes) -> dict:
        return json.loads(self.cipher.decrypt(content[:12], content[12:], env.encode()))
