"""Pure-stdlib data models for unified KubeDoor notification providers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

FIELD_KINDS: tuple[str, ...] = (
    "text",
    "secret",
    "select",
    "number",
    "boolean",
    "textarea",
    "headers",
)

FieldKind = Literal["text", "secret", "select", "number", "boolean", "textarea", "headers"]
TransportKind = Literal["http", "smtp"]


@dataclass(frozen=True)
class FieldSpec:
    """Self-describing field metadata shared by backend validation and Schema-driven UI."""

    key: str
    label: str
    kind: FieldKind = "text"
    required: bool = False
    secret: bool | None = None
    default: Any = None
    placeholder: str = ""
    help: str = ""
    options: tuple[dict[str, str], ...] = ()
    pattern: str = ""
    max_length: int = 512
    min_value: int | float | None = None
    max_value: int | float | None = None
    show_if: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.kind not in FIELD_KINDS:
            raise ValueError(f"Unsupported FieldSpec.kind: {self.kind!r}")
        if self.secret is None:
            object.__setattr__(self, "secret", self.kind == "secret")
        elif self.kind == "secret" and not self.secret:
            object.__setattr__(self, "secret", True)

    def to_dict(self) -> dict[str, Any]:
        """Serialize field metadata into a JSON-ready dictionary."""
        data: dict[str, Any] = {
            "key": self.key,
            "label": self.label,
            "kind": self.kind,
            "required": self.required,
            "secret": bool(self.secret),
            "default": self.default,
            "placeholder": self.placeholder,
            "help": self.help,
            "options": [dict(option) for option in self.options],
            "pattern": self.pattern,
            "max_length": self.max_length,
        }
        if self.min_value is not None:
            data["min_value"] = self.min_value
        if self.max_value is not None:
            data["max_value"] = self.max_value
        if self.show_if is not None:
            data["show_if"] = dict(self.show_if)
        return data


@dataclass(frozen=True)
class RateLimit:
    """Per-channel sliding-window and optional short-burst rate limit."""

    limit: int
    window_seconds: float
    burst_limit: int | None = None
    burst_window_seconds: float = 1.0

    def __post_init__(self) -> None:
        if self.limit < 1 or self.window_seconds <= 0:
            raise ValueError("RateLimit requires limit >= 1 and window_seconds > 0")
        if self.burst_limit is not None and (self.burst_limit < 1 or self.burst_window_seconds <= 0):
            raise ValueError("RateLimit burst requires burst_limit >= 1 and burst_window_seconds > 0")

    def to_dict(self) -> dict[str, Any]:
        """Serialize rate limit metadata into a JSON-ready dictionary."""
        data: dict[str, Any] = {
            "limit": self.limit,
            "window_seconds": self.window_seconds,
        }
        if self.burst_limit is not None:
            data["burst_limit"] = self.burst_limit
            data["burst_window_seconds"] = self.burst_window_seconds
        return data


@dataclass(frozen=True)
class AlertItem:
    """Structured alert entry carried inside a NotificationMessage."""

    summary: str
    message: str = ""
    status: str = "firing"
    severity: str = ""
    alertname: str = ""
    duration_text: str = ""
    silence_url: str = ""
    labels: dict[str, str] = field(default_factory=dict)
    annotations: dict[str, str] = field(default_factory=dict)
    starts_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize alert item for JSON webhook payloads and structured templates."""
        return {
            "summary": self.summary,
            "message": self.message,
            "status": self.status,
            "severity": self.severity,
            "alertname": self.alertname,
            "duration_text": self.duration_text,
            "silence_url": self.silence_url,
            "labels": dict(self.labels),
            "annotations": dict(self.annotations),
            "starts_at": self.starts_at,
        }


@dataclass(frozen=True)
class NotificationMessage:
    """Normalized semantic message rendered cleanly by every notification provider."""

    title: str = "通知"
    markdown: str = ""
    plain_text: str = ""
    severity: str = "info"
    category: str = "generic"
    env: str = ""
    at: str = ""
    mentions: tuple[str, ...] = ()
    mention_all: bool = False
    items: tuple[AlertItem, ...] = ()
    links: tuple[dict[str, str], ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    event_id: str = ""
    timestamp: str = ""


@dataclass(frozen=True, init=False)
class ChannelConfig:
    """Resolved channel configuration (plaintext secrets live only in memory)."""

    id: str
    name: str
    provider: str
    enabled: bool = True
    settings: dict[str, Any] = field(default_factory=dict)
    secrets: dict[str, str] = field(default_factory=dict)
    rate_limit: RateLimit | None = None

    def __init__(
        self,
        id: str,
        name: str = "",
        provider: str = "",
        enabled: bool = True,
        settings: dict[str, Any] | None = None,
        secrets: dict[str, str] | None = None,
        rate_limit: RateLimit | tuple[int, float] | None = None,
        *,
        type: str = "",
    ) -> None:
        resolved = (provider or type or "").strip()
        if not resolved:
            raise ValueError("ChannelConfig requires provider")
        if isinstance(rate_limit, tuple):
            rate_limit = RateLimit(limit=int(rate_limit[0]), window_seconds=float(rate_limit[1]))
        object.__setattr__(self, "id", id)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "provider", resolved)
        object.__setattr__(self, "enabled", bool(enabled))
        object.__setattr__(self, "settings", dict(settings or {}))
        object.__setattr__(self, "secrets", dict(secrets or {}))
        object.__setattr__(self, "rate_limit", rate_limit)

    @property
    def type(self) -> str:
        """Backward-compatible alias for provider."""
        return self.provider


@dataclass(frozen=True)
class HttpPreparedRequest:
    """Prepared outbound HTTP request emitted by HTTP-based notification providers."""

    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes | None = None
    timeout: float = 10.0
    allow_http: bool = False
    allow_redirects: bool = False


@dataclass(frozen=True)
class DeliveryResult:
    """Outcome of a single provider delivery attempt."""

    ok: bool
    message: str
    status_code: int | None = None
    provider_code: str = ""
    retryable: bool = False
    retry_after: float | None = None
    attempts: int = 1
    latency_ms: int | None = None
