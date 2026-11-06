"""消息通知:按集群选择 IM 群机器人发送(K8S 事件告警、agent 转来的运维操作通知、管控表通知)。

- channels.py:四种 IM 的 token 解析、消息格式和返回解读(纯函数,安装服务 kubedoor-setup 也按路径加载)
- service.py:配置缓存、resolve(env)、notify(content, env)、发送线程池
- api.py:「系统管理 → 消息通知」的接口,以及给 kubedoor-alarm 取通道的内部接口

业务代码只需要 `from notify import notify`,`notify(content, env)` 在事件循环和普通线程里都能直接调用。
"""

from .service import notify, resolve

__all__ = ['notify', 'resolve']
