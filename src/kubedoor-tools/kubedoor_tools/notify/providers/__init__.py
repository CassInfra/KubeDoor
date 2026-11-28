"""Built-in M1 notification providers (auto-registered on import)."""

from __future__ import annotations

from .wecom import WeComProvider
from .feishu import FeishuProvider
from .dingding import DingDingProvider
from .slack import SlackProvider
from .webhook import WebhookProvider
from .smtp import SmtpProvider

__all__ = [
    "WeComProvider",
    "FeishuProvider",
    "DingDingProvider",
    "SlackProvider",
    "WebhookProvider",
    "SmtpProvider",
]
