"""统一认证鉴权中间件。

每个请求:
  1. 拒绝浏览器发起的跨站请求(Sec-Fetch-Site 为 cross-site / same-site):防 CSRF 和跨站 WebSocket 劫持;
  2. 解析身份,优先级:内部 token(AI 服务 / CronJob)> 会话 Cookie > Basic 认证(只接受非浏览器客户端:
     agent、MCP 客户端、脚本;浏览器缓存的旧 Basic 凭据一律忽略,避免被跨站利用);
  3. 按 policy.RULES 校验权限;必须先改密的账号只能访问 /api/auth/*;
  4. 写操作记审计日志。
"""

import hmac
import os
import time

from aiohttp import web
from loguru import logger

from . import audit, policy
from .authn import AuthFailure, principal_from_basic
from .context import client_ip
from .principal import SYSTEM, Principal
from .store import SESSION_COOKIE, store

AGENT_AUTH_REQUIRED = os.environ.get('AGENT_AUTH_REQUIRED', 'true').strip().lower() not in ('false', '0', 'no', 'off')
ANONYMOUS_AGENT = Principal(username='anonymous-agent', role='agent', source='system', via='anonymous-agent')
_SAFE_METHODS = ('GET', 'HEAD', 'OPTIONS')
_VERIFY_PATH = '/api/auth/verify'


def deny(status, code, message, retry_after=0):
    response = web.json_response({'success': False, 'code': code, 'message': message, 'error': message}, status=status)
    if retry_after:
        response.headers['Retry-After'] = str(retry_after)
    return response


_agent_warned = {}


def _warn_agent_rejected(request, ip, reason):
    """agent 认证失败会每隔几秒重连一次,同一来源每分钟只记一条日志。"""
    now = time.monotonic()
    key = (ip, request.query.get('env', ''))
    if now - _agent_warned.get(key, 0) < 60:
        return
    if len(_agent_warned) > 1000:
        _agent_warned.clear()
    _agent_warned[key] = now
    logger.warning(f"[鉴权] 拒绝 agent 连接 env={request.query.get('env', '')} 来源={ip}: {reason}"
                   "(检查 agent 的 MASTER_WS 里的 Agent 账号口令)")


def is_browser(request):
    return bool(request.headers.get('Sec-Fetch-Site') or request.headers.get('Sec-Fetch-Mode'))


async def resolve_principal(request, ip):
    """返回 Principal 或 None(匿名)。内部 token 不对直接拒绝(抛 AuthFailure)。"""
    token = request.headers.get('X-Kubedoor-Token')
    if token is not None:
        expected = os.environ.get('AI_INTERNAL_TOKEN', '')
        if not expected or not hmac.compare_digest(token.encode(), expected.encode()):
            raise AuthFailure(401, 'bad_token', '内部访问令牌无效')
        username = request.headers.get('X-User-Name', '').strip()
        if not username:
            return SYSTEM
        principal = await store.principal_by_name(username, via='internal')
        if principal is None:
            raise AuthFailure(403, 'unknown_user', f'用户 {username} 不存在、已禁用或不是 Web 账号')
        return principal

    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie:
        session = await store.resolve_session(cookie)
        if session is not None:
            principal = await store.principal_for(session['user_id'], via='session',
                                                  session_hash=session['token_hash'])
            if principal is not None:
                return principal

    header = request.headers.get('Authorization', '')
    if header[:6].lower() == 'basic ' and not is_browser(request):
        return await principal_from_basic(header, ip)
    return None


def _is_write(rule, method):
    return rule.write or (rule.kind == 'admin' and not rule.methods and method not in _SAFE_METHODS)


def _audit_target(request):
    return f'{request.method} {request.path}'


async def _audit_env(rule, request):
    """审计里记录的集群:规则声明的集群参数,其次 query/body 里的 env。"""
    try:
        if rule.env:
            return ','.join(await policy.extract_envs(rule, request))[:200]
        env = request.query.get('env')
        if env:
            return env
        if request.method not in _SAFE_METHODS and request.body_exists and request.content_type == 'application/json':
            body = await request.json()
            if isinstance(body, dict) and isinstance(body.get('env'), str):
                return body['env']
    except Exception:
        pass
    return ''


def create_middleware():
    @web.middleware
    async def auth_middleware(request, handler):
        rule = policy.match(request.method, request.path)
        ip = client_ip(request)
        site = request.headers.get('Sec-Fetch-Site', '').lower()
        cross_site = site in ('cross-site', 'same-site')
        request['cross_site'] = cross_site
        if cross_site and request.path != _VERIFY_PATH:
            return deny(403, 'cross_site', '拒绝跨站请求')

        try:
            principal = await resolve_principal(request, ip)
        except AuthFailure as exc:
            if rule.kind in ('public',):
                principal = None
            else:
                if rule.kind == 'agent':
                    _warn_agent_rejected(request, ip, exc.message)
                return deny(exc.status, exc.code, exc.message, exc.retry_after)
        if principal is not None:
            request['principal'] = principal

        if rule.kind == 'public':
            return await handler(request)

        if rule.kind == 'agent':
            if principal is None:
                if AGENT_AUTH_REQUIRED:
                    _warn_agent_rejected(request, ip, '没有带账号口令')
                    return deny(401, 'unauthorized', 'agent 连接需要账号口令(MASTER_WS 里带 Agent 账号)')
                request['principal'] = ANONYMOUS_AGENT
            elif principal.via != 'basic' or not (principal.is_agent or principal.is_admin):
                _warn_agent_rejected(request, ip, f'账号 {principal.username} 不是 Agent 账号')
                return deny(403, 'agent_only', '只有 Agent 账号或管理员账号可以注册 agent')
            return await handler(request)

        if principal is None:
            return deny(401, 'unauthorized', '未登录或登录已过期,请重新登录')
        if principal.is_agent:
            return deny(403, 'agent_account', 'Agent 账号只能用于 agent 连接,不能访问其它接口')
        # 「先改初始口令」只拦用口令进来的请求。AI 服务代表用户的内部调用不拦:网页 AI 助手 / MCP 的入口
        # 已经在 /api/auth/verify 拦过,企微机器人用不到这个账号的口令(管理员新建、没人登录过的账号也要能用)
        if principal.must_change_password and rule.kind != 'session' and principal.via != 'internal':
            return deny(403, 'password_change_required', '请先修改初始口令')

        is_write = _is_write(rule, request.method)
        try:
            await policy.authorize(rule, request, principal)
        except policy.Deny as exc:
            if is_write:
                audit.record_bg(principal.username, 'api.denied', env=request.query.get('env', ''),
                                target=_audit_target(request), ip=ip, success=False,
                                detail={'status': exc.status, 'reason': exc.message,
                                        'query': audit.safe_params(request.query), 'via': principal.via})
            logger.info(f'[鉴权] 拒绝 {principal.username} {request.method} {request.path}: {exc.message}')
            return deny(exc.status, exc.code, exc.message)

        status = 500
        env_hint = await _audit_env(rule, request) if is_write else ''
        try:
            response = await handler(request)
            status = response.status
        except web.HTTPException as exc:
            status = exc.status
            raise
        finally:
            if is_write:
                audit.record_bg(principal.username, 'api.write', env=env_hint,
                                target=_audit_target(request), ip=ip, success=status < 400,
                                detail={'status': status, 'query': audit.safe_params(request.query),
                                        'via': principal.via})
        if not response.prepared:
            response.headers['X-Kubedoor-User'] = principal.username
        return response

    return auth_middleware
