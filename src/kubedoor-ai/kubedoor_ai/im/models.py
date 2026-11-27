"""Normalized Multi-IM models, capabilities, events, and configuration types."""
from __future__ import annotations

from dataclasses import dataclass, field
import threading
from typing import Any, Literal

SUPPORTED_PLATFORMS: tuple[str, ...] = ("wecom", "feishu")
ALL_KNOWN_PLATFORMS: tuple[str, ...] = ("wecom", "feishu", "dingtalk", "slack")
GROUP_POLICIES: tuple[str, ...] = ("disabled", "mention", "allowlist")
FEISHU_DOMAIN_PRESETS: tuple[str, ...] = ("feishu", "lark", "custom")
PROVIDER_KEYS: tuple[str, ...] = (
    "base_url",
    "api_key",
    "model",
    "max_input_tokens",
    "max_output_tokens",
    "compression_ratio",
    "reasoning_effort",
)


@dataclass(frozen=True)
class IMCapabilities:
    """Platform capability matrix used by IMBotEngine for streaming and callback SLAs."""

    supports_native_stream: bool
    supports_message_edit: bool
    supports_interactive_card: bool
    supports_group_mention: bool
    max_message_bytes: int
    stream_throttle_ms: int
    card_callback_sla_sec: float


@dataclass(frozen=True)
class InboundEvent:
    """Normalized inbound message or chat-entry event across IM platforms."""

    bot_id: str
    platform: Literal["wecom", "feishu", "dingtalk", "slack"]
    msgid: str
    external_user_id: str
    chat_id: str
    chat_type: Literal["single", "group"]
    text: str
    mentioned_bot: bool = False
    kind: str = "text"
    raw_context: Any = None


class CardCallbackSync:
    """Thread-safe handoff coordinator between sync adapter SLA wait and async engine."""

    def __init__(self, deadline: float) -> None:
        self.deadline = deadline
        self._lock = threading.Lock()
        self.sync_timed_out = False
        self.sync_claimed = False

    def mark_sync_timeout(self) -> bool:
        """Mark synchronous waiter as timed out unless engine already claimed inline return."""
        with self._lock:
            if self.sync_claimed:
                return False
            self.sync_timed_out = True
            return True

    def claim_sync(self) -> bool:
        """Claim inline synchronous response unless adapter already timed out."""
        with self._lock:
            if self.sync_timed_out:
                return False
            self.sync_claimed = True
            return True


@dataclass(frozen=True)
class CardActionEvent:
    """Normalized interactive card button / selection callback event."""

    bot_id: str
    platform: str
    action_id: str
    external_user_id: str
    selected_value: str = ""
    callback_token: str = ""
    msgid: str = ""
    chat_id: str = ""
    raw_context: Any = None
    callback_sync: CardCallbackSync | None = None


@dataclass(frozen=True)
class ApprovalPrompt:
    """Approval prompt details handed to IMAdapter/StreamHandle for card rendering."""

    action_id: str
    run_id: str
    env: str
    operation_label: str
    summary_markdown: str
    expires_at_text: str
    card_ref: str = ""
    summary_line: str = ""
    body_markdown: str = ""


@dataclass
class BotConfig:
    """Decrypted runtime configuration for one IM bot instance."""

    id: str
    platform: str
    name: str
    enabled: bool = False
    credentials: dict[str, Any] = field(default_factory=dict)
    unbound_username: str | None = None
    unbound_default_env: str | None = None
    session_idle_minutes: int = 120
    group_policy: str = "disabled"
    allowed_chat_ids: list[str] = field(default_factory=list)
    model_override: dict[str, Any] | None = None
    updated_by: str = ""
    updated_at: Any = None
