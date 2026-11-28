"""Provider ABC, registration decorator, schema export, and URL auto-detection."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Any, TypeVar
from urllib.parse import urlsplit

from .models import (
    ChannelConfig,
    DeliveryResult,
    FieldSpec,
    HttpPreparedRequest,
    NotificationMessage,
    RateLimit,
    TransportKind,
)

TProvider = TypeVar("TProvider", bound="NotificationProvider")


class NotificationProvider(ABC):
    """Abstract base class implemented by all one-way notification channel providers."""

    id: str
    label: str
    description: str = ""
    transport: TransportKind = "http"
    supports_at: bool = True
    supports_markdown: bool = True
    at_help: str = ""
    default_rate_limit: RateLimit = RateLimit(limit=20, window_seconds=60.0)
    fields: tuple[FieldSpec, ...] = ()

    @property
    def type(self) -> str:
        """Backward-compatible alias for provider id."""
        return self.id

    def to_schema(self) -> dict[str, Any]:
        """Serialize provider metadata and FieldSpecs for Schema-driven UI."""
        return {
            "id": self.id,
            "type": self.id,
            "label": self.label,
            "description": self.description,
            "transport": self.transport,
            "default_rate_limit": self.default_rate_limit.to_dict(),
            "supports_at": self.supports_at,
            "supports_markdown": self.supports_markdown,
            "at_help": self.at_help,
            "fields": [field.to_dict() for field in self.fields],
        }

    @abstractmethod
    def normalize(
        self,
        raw_settings: dict[str, Any] | None,
        raw_secrets: dict[str, Any] | None,
        saved_secrets: dict[str, str] | None = None,
        *,
        allow_reuse_saved: bool = True,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """Validate and normalize (settings, secrets), preserving saved secrets when blank."""

    def build_http(self, cfg: ChannelConfig, msg: NotificationMessage) -> HttpPreparedRequest:
        """Build outbound HTTP request when transport == 'http'."""
        raise NotImplementedError(f"Provider {self.id!r} does not use HTTP transport")

    def parse_http_response(
        self,
        status_code: int,
        headers: Mapping[str, str],
        body_text: str | bytes,
    ) -> DeliveryResult:
        """Parse HTTP response into DeliveryResult when transport == 'http'."""
        raise NotImplementedError(f"Provider {self.id!r} does not use HTTP transport")

    def send_direct(self, cfg: ChannelConfig, msg: NotificationMessage) -> DeliveryResult:
        """Execute direct delivery (e.g. SMTP) when transport != 'http'."""
        raise NotImplementedError(f"Provider {self.id!r} does not use direct transport")

    def redact_values(self, cfg: ChannelConfig) -> tuple[str, ...]:
        """Return secret strings from cfg that must always be redacted from logs/errors."""
        values: list[str] = []
        for field in self.fields:
            if field.secret:
                raw = cfg.secrets.get(field.key)
                if raw:
                    values.append(str(raw))
        for raw in cfg.secrets.values():
            if raw and str(raw) not in values:
                values.append(str(raw))
        return tuple(values)

    def match_url(self, url: str) -> bool:
        """Return True if a pasted webhook URL belongs to this provider."""
        return False


_REGISTRY: dict[str, NotificationProvider] = {}
_PROVIDERS_LOADED = False


def _ensure_providers_loaded() -> None:
    global _PROVIDERS_LOADED
    if not _PROVIDERS_LOADED:
        _PROVIDERS_LOADED = True
        from . import providers  # noqa: F401


def register_provider(cls: type[TProvider]) -> type[TProvider]:
    """Instantiate and register a NotificationProvider subclass by its id."""
    instance = cls()
    provider_id = getattr(instance, "id", "").strip()
    if not provider_id:
        raise ValueError(f"Provider class {cls.__name__} is missing a non-empty id")
    _REGISTRY[provider_id] = instance
    return cls


def get_provider(provider_id: str) -> NotificationProvider:
    """Return registered NotificationProvider instance for provider_id."""
    _ensure_providers_loaded()
    key = (provider_id or "").strip().lower()
    provider = _REGISTRY.get(key)
    if provider is None:
        supported = "、".join(p.label for p in _REGISTRY.values())
        raise ValueError(f"不支持的 IM 类型:{provider_id}(可选 {supported})")
    return provider


def list_providers() -> list[NotificationProvider]:
    """Return all registered NotificationProvider instances in registration order."""
    _ensure_providers_loaded()
    return list(_REGISTRY.values())


def provider_schemas() -> list[dict[str, Any]]:
    """Return serialized Schema metadata dictionaries for all registered providers."""
    return [provider.to_schema() for provider in list_providers()]


_KNOWN_WEBHOOK_HOSTS: dict[str, str] = {
    "qyapi.weixin.qq.com": "wecom",
    "open.feishu.cn": "feishu",
    "open.larksuite.com": "feishu",
    "oapi.dingtalk.com": "dingding",
    "hooks.slack.com": "slack",
}


def detect_provider_from_url(url: str) -> str | None:
    """Detect built-in IM provider id from a full or scheme-omitted Webhook URL."""
    if not isinstance(url, str):
        return None
    text = url.strip()
    if not text:
        return None
    lowered = text.lower()
    if not lowered.startswith(("http://", "https://")) and "://" not in text:
        host_guess = lowered.split("/", 1)[0].split("?", 1)[0]
        if host_guess in _KNOWN_WEBHOOK_HOSTS:
            return _KNOWN_WEBHOOK_HOSTS[host_guess]
        return None
    if not lowered.startswith(("http://", "https://")):
        return None
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if host in _KNOWN_WEBHOOK_HOSTS:
        return _KNOWN_WEBHOOK_HOSTS[host]
    _ensure_providers_loaded()
    for provider in _REGISTRY.values():
        if provider.match_url(text):
            return provider.id
    return None
