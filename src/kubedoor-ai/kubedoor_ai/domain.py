from __future__ import annotations

import hmac
import json
import os
import re
import yaml
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Any, Mapping
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


ROLES = ("admin", "rw", "read", "none")
CLUSTER_PERMISSIONS = ("rw", "read", "none")


@dataclass(frozen=True)
class Identity:
    """KubeDoor 登录用户。permission 是全局角色(admin/rw/read/none),clusters 是单独授权的集群。

    某个集群的有效权限:管理员一律读写;有单独授权按授权;否则按全局角色兜底(与 master 的规则一致)。
    """
    username: str
    permission: str
    clusters: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))

    @property
    def is_admin(self) -> bool:
        return self.permission == "admin"

    def permission_for(self, env: str) -> str:
        if self.permission == "admin":
            return "rw"
        if env in self.clusters:
            return self.clusters[env]
        return self.permission if self.permission in ("rw", "read") else "none"

    def can_read(self, env: str) -> bool:
        return self.permission_for(env) in ("read", "rw")

    def require_write(self, env: str | None = None) -> None:
        """env 为空时检查全局读写(与集群无关的资源,如共享记忆)。"""
        if env is None:
            if self.permission not in ("admin", "rw"):
                raise AIError("当前用户没有全局读写权限", 403, "permission_denied")
            return
        if self.permission_for(env) != "rw":
            raise AIError(f"当前用户对集群 {env} 只有查询权限", 403, "permission_denied")

    def require_admin(self) -> None:
        if not self.is_admin:
            raise AIError("需要 KubeDoor 管理员权限", 403, "permission_denied")


def authenticate(headers: Any, expected_token: str) -> Identity:
    token = headers.get("x-kubedoor-token", "")
    if not expected_token or not hmac.compare_digest(token, expected_token):
        raise AIError("内部访问令牌无效", 401, "unauthorized")
    username = headers.get("x-user-name", "").strip()
    permission = headers.get("x-user-permission", "")
    if not username or len(username) > 200 or permission not in ROLES:
        raise AIError("缺少可信用户身份", 401, "unauthorized")
    try:
        clusters = json.loads(headers.get("x-user-clusters", "") or "{}")
    except ValueError:
        clusters = None
    if (not isinstance(clusters, dict) or len(clusters) > 5000
            or any(not isinstance(k, str) or v not in CLUSTER_PERMISSIONS for k, v in clusters.items())):
        raise AIError("集群权限格式无效", 401, "unauthorized")
    return Identity(username, permission, MappingProxyType({} if permission == "admin" else clusters))


@dataclass(frozen=True)
class Provider:
    base_url: str
    api_key: str
    model: str
    max_input_tokens: int | None = None     # 用户填写的上下文窗口；None 表示按模型推断
    max_output_tokens: int | None = None    # 用户填写的最大输出；None 表示按模型推断
    compression_ratio: float | None = None  # 压缩阈值（0.2–0.8）；None 表示默认 0.5
    reasoning_effort: str | None = None     # DeepSeek 思考强度 off/low/high/max；None 表示用模型默认（high）

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
        return cls(base_url, key, model.strip(), *parse_model_limits(raw), parse_reasoning_effort(raw))


REASONING_EFFORTS = ("off", "low", "high", "max")


def parse_reasoning_effort(raw: Mapping) -> str | None:
    effort = raw.get("reasoning_effort")
    if effort is not None and effort not in REASONING_EFFORTS:
        raise AIError("思考强度只能是 off、low、high、max")
    return effort


def parse_model_limits(raw: Mapping) -> tuple[int | None, int | None, float | None]:
    """校验可选的上下文窗口、最大输出和压缩阈值；不填的项返回 None，由后端按模型推断。"""
    max_input, max_output, ratio = (raw.get(key) for key in ("max_input_tokens", "max_output_tokens", "compression_ratio"))
    if max_input is not None and (type(max_input) is not int or not 4096 <= max_input <= 10_000_000):
        raise AIError("上下文窗口必须为 4096–10000000 之间的整数（token）")
    if max_output is not None and (type(max_output) is not int or not 1024 <= max_output <= (max_input or 10_000_000)):
        raise AIError("最大输出必须为 1024 到上下文窗口之间的整数（token）")
    if ratio is not None and (type(ratio) not in (int, float) or not 0.2 <= ratio <= 0.8):
        raise AIError("压缩阈值必须在 20%–80% 之间")
    return max_input, max_output, None if ratio is None else float(ratio)


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
