"""消息通知:四种 IM 群机器人的 token 解析、消息格式和返回解读。

向后兼容垫片:委托给共享核心 `kubedoor_tools.notify.compat`（纯标准库实现，
不依赖第三方包）。安装服务 kubedoor-setup 仍可按路径单独加载本文件。
"""

import importlib
import pathlib
import sys


def _load_compat():
    try:
        return importlib.import_module('kubedoor_tools.notify.compat')
    except ImportError:
        resolved = pathlib.Path(__file__).resolve()
        if len(resolved.parents) > 2:
            tools_dir = resolved.parents[2] / 'kubedoor-tools'
            if tools_dir.is_dir():
                tools_str = str(tools_dir)
                if tools_str not in sys.path:
                    sys.path.insert(0, tools_str)
        return importlib.import_module('kubedoor_tools.notify.compat')


_compat = _load_compat()

TYPES = _compat.LEGACY_TYPES
TYPE_LABELS = _compat.LEGACY_TYPE_LABELS

MAX_TOKEN = _compat.MAX_TOKEN
MAX_AT = _compat.MAX_AT
MAX_MESSAGE = _compat.MAX_MESSAGE
OK_MESSAGE = _compat.OK_MESSAGE
WECOM_MAX_BYTES = _compat.WECOM_MAX_BYTES
TOKEN_HINTS = _compat.TOKEN_HINTS

normalize_token = _compat.normalize_legacy_token
normalize_at = _compat.normalize_at
build_request = _compat.build_legacy_request
parse_response = _compat.parse_legacy_response
test_content = _compat.test_content
test_content.__test__ = False

__all__ = [
    'TYPES',
    'TYPE_LABELS',
    'MAX_TOKEN',
    'MAX_AT',
    'MAX_MESSAGE',
    'OK_MESSAGE',
    'WECOM_MAX_BYTES',
    'TOKEN_HINTS',
    'normalize_token',
    'normalize_at',
    'build_request',
    'parse_response',
    'test_content',
]
