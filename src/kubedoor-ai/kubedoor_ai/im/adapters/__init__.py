"""Platform-specific IM adapters."""
from .feishu import FeishuAdapter, FeishuCardStreamHandle
from .wecom import WeComAdapter

__all__ = ["FeishuAdapter", "FeishuCardStreamHandle", "WeComAdapter"]
