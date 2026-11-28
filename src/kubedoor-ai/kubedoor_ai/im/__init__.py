"""Platform-agnostic Multi-IM AI Bot gateway package."""
from .adapter import IMAdapter, StreamHandle
from .engine import IMBotEngine
from .manager import IMBotManager, LegacyWeComBridge
from .models import (
    ALL_KNOWN_PLATFORMS,
    FEISHU_DOMAIN_PRESETS,
    GROUP_POLICIES,
    PROVIDER_KEYS,
    SUPPORTED_PLATFORMS,
    ApprovalPrompt,
    BotConfig,
    CardActionEvent,
    IMCapabilities,
    InboundEvent,
)
from .shared_model import SharedModelManager
from .ssrf import validate_custom_domain

__all__ = [
    "ALL_KNOWN_PLATFORMS",
    "ApprovalPrompt",
    "BotConfig",
    "CardActionEvent",
    "FEISHU_DOMAIN_PRESETS",
    "GROUP_POLICIES",
    "IMAdapter",
    "IMBotEngine",
    "IMBotManager",
    "IMCapabilities",
    "InboundEvent",
    "LegacyWeComBridge",
    "PROVIDER_KEYS",
    "SUPPORTED_PLATFORMS",
    "SharedModelManager",
    "StreamHandle",
    "validate_custom_domain",
]
