"""请求上下文里的登录身份。

中间件给每个请求放一个 Principal(request['principal']);业务 handler 用 principal_of 取。
单测或内部伪造的请求对象(不是 aiohttp Request)没有这个字段,按系统身份处理,保持老测试可用;
真实请求缺身份(不应该发生)按匿名处理,什么都看不到。
"""

from collections.abc import Mapping
from types import MappingProxyType

from .principal import SYSTEM, Principal

ANONYMOUS = Principal(username='', role='none', source='anonymous', via='anonymous',
                      clusters=MappingProxyType({}))


def principal_of(request):
    if isinstance(request, Mapping):
        return request.get('principal') or ANONYMOUS
    return getattr(request, 'principal', None) or SYSTEM


def client_ip(request):
    """nginx 透传的真实来源 IP;直连 master 时用对端地址。仅用于审计和限流,不做信任判断。"""
    headers = getattr(request, 'headers', {}) or {}
    real = headers.get('X-Real-IP', '').strip()
    if real:
        return real[:64]
    forwarded = headers.get('X-Forwarded-For', '')
    if forwarded:
        return forwarded.split(',')[0].strip()[:64]
    return (getattr(request, 'remote', '') or '')[:64]
