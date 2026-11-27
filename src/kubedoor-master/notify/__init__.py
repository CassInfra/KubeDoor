"""消息通知:按集群选择 IM 群机器人发送(K8S 事件告警、agent 转来的运维操作通知、管控表通知)。

- channels.py:四种 IM 的 token 解析、消息格式和返回解读(纯函数,安装服务 kubedoor-setup 也按路径加载)
- service.py:配置缓存、resolve(env)、resolve_targets(...)、notify(content, env)、notify_message(msg)、发送线程池
- api.py:「系统管理 → 消息通知」v1/v2 接口,以及给 kubedoor-alarm 代理发送的内部接口

业务代码只需要 `from notify import notify`,`notify(content, env)` 在事件循环和普通线程里都能直接调用。
"""

import pathlib
import sys


def _ensure_kubedoor_tools_path() -> None:
    try:
        import kubedoor_tools  # noqa: F401
        return
    except ImportError:
        pass
    resolved = pathlib.Path(__file__).resolve()
    if len(resolved.parents) > 2:
        tools_dir = resolved.parents[2] / 'kubedoor-tools'
        if tools_dir.is_dir() and str(tools_dir) not in sys.path:
            sys.path.insert(0, str(tools_dir))


_ensure_kubedoor_tools_path()

from .service import deliver_message, notify, notify_message, resolve, resolve_targets  # noqa: E402

__all__ = ['notify', 'notify_message', 'resolve', 'resolve_targets', 'deliver_message']
