"""Protocol contracts for platform IM adapters and 3-tier stream handles."""
from __future__ import annotations

from typing import Any, Callable, Coroutine, Protocol

from .models import ApprovalPrompt, CardActionEvent, IMCapabilities, InboundEvent

MessageHandler = Callable[[InboundEvent], Coroutine[Any, Any, None]]
CardActionHandler = Callable[[CardActionEvent], Coroutine[Any, Any, dict[str, Any] | None]]


class StreamHandle(Protocol):
    """Three-tier streaming output abstraction across IM platforms."""

    async def update(self, markdown: str) -> None: ...

    async def finish(self, markdown: str) -> None: ...

    async def send_notice(self, markdown: str) -> None: ...

    async def send_approval(self, prompt: ApprovalPrompt, fallback_text: str) -> str | None:
        """Send an approval card and return card_ref; fall back to plain text on failure."""
        ...

    async def send_cluster_menu(
        self,
        current_env: str | None,
        clusters: list[dict[str, Any]],
        fallback_text: str,
        *,
        suggestion: str | None = None,
        prompt_text: str | None = None,
    ) -> None: ...


class IMAdapter(Protocol):
    """Protocol implemented by each IM platform adapter (WeCom, Feishu, etc.)."""

    platform: str
    capabilities: IMCapabilities

    async def start(
        self,
        bot_cfg: dict[str, Any],
        on_message: MessageHandler,
        on_card_action: CardActionHandler,
    ) -> None: ...

    async def stop(self) -> None: ...

    async def status(self) -> dict[str, Any]: ...

    def open_reply_stream(self, event: InboundEvent) -> StreamHandle: ...

    def open_push_stream(self, external_user_id: str, chat_id: str = "") -> StreamHandle: ...

    async def update_approval_card(
        self,
        card_ref: str,
        decision: str,
        summary: str,
        raw_context: Any = None,
    ) -> None: ...
