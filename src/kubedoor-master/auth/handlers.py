"""登录、退出、当前用户、自助改密,以及给 nginx auth_request 用的身份校验接口。"""

import json
import os

from aiohttp import web

from . import audit, passwords, permissions
from .authn import AuthFailure, check_credentials
from .context import client_ip
from .ratelimit import limiter
from .store import SESSION_COOKIE, hash_password, store, token_hash, verify_local

_COOKIE_SECURE = os.environ.get('AUTH_COOKIE_SECURE', 'auto').strip().lower()

# 由 auth.setup 注入:返回当前已知的集群名(库里登记过的 + 在线的)
known_envs = None


def _fail(status, code, message, retry_after=0):
    response = web.json_response({'success': False, 'code': code, 'message': message, 'error': message}, status=status)
    if retry_after:
        response.headers['Retry-After'] = str(retry_after)
    return response


def _secure(request):
    if _COOKIE_SECURE in ('true', '1', 'yes'):
        return True
    if _COOKIE_SECURE in ('false', '0', 'no'):
        return False
    return request.secure or request.headers.get('X-Forwarded-Proto', '').lower() == 'https'


def _set_cookie(response, request, token, max_age=None):
    response.set_cookie(SESSION_COOKIE, token, max_age=max_age, path='/', httponly=True,
                        samesite='Strict', secure=_secure(request))


def _clear_cookie(response, request):
    response.del_cookie(SESSION_COOKIE, path='/')


async def me_payload(principal):
    policy = await permissions.load_policy()  # 登录接口不经过鉴权中间件的刷新
    envs = set(await known_envs()) if known_envs else set()
    envs |= set(principal.clusters)
    data = principal.to_public()
    data['can_change_password'] = principal.source == 'local'
    data['cluster_permissions'] = {env: principal.permission(env) for env in sorted(envs)}
    # 操作权限表:{操作: 允许的集群权限级别},前端 can(op, env) = 列表里有该集群的有效权限;
    # 与集群无关的配置按 default_permission 查。只给权限,不含审批和命令清单
    data['operations'] = permissions.operations_levels(policy)
    # Grafana 看板按集群隔离不了,只给能看到全部集群的人用(前端据此隐藏看板菜单)
    data['all_clusters'] = principal.env_scope()[0] == 'all'
    return data


async def _json(request):
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


async def login(request):
    body = await _json(request)
    username = str(body.get('username') or '').strip()
    password = body.get('password')
    remember = bool(body.get('remember'))
    ip = client_ip(request)
    try:
        user = await check_credentials(username, password, ip)
    except AuthFailure as exc:
        audit.record_bg(username[:64], 'login', ip=ip, success=False, detail={'reason': exc.code})
        return _fail(exc.status, exc.code, exc.message, exc.retry_after)
    if user['role'] == 'agent':
        audit.record_bg(user['username'], 'login', ip=ip, success=False, detail={'reason': 'agent_account'})
        return _fail(403, 'agent_account', 'Agent 账号只能用于 agent 连接,不能登录 Web')

    # 每次登录都发新会话(防会话固定);浏览器里原来的会话一并作废
    previous = request.cookies.get(SESSION_COOKIE)
    if previous:
        await store.delete_session(token_hash(previous))
    token, lifetime = await store.create_session(user['id'], remember, ip, request.headers.get('User-Agent', ''))
    await store.record_login(user['id'], ip)
    principal = await store.principal_for(user['id'], via='session', session_hash=token_hash(token))
    audit.record_bg(principal.username, 'login', ip=ip, detail={'source': principal.source, 'remember': remember})
    response = web.json_response({'success': True, 'data': await me_payload(principal)})
    _set_cookie(response, request, token, int(lifetime.total_seconds()) if remember else None)
    return response


async def logout(request):
    principal = request.get('principal')
    if principal is not None and principal.session_hash:
        await store.delete_session(principal.session_hash)
        audit.record_bg(principal.username, 'logout', ip=client_ip(request))
    response = web.json_response({'success': True})
    _clear_cookie(response, request)
    return response


async def me(request):
    return web.json_response({'success': True, 'data': await me_payload(request['principal'])})


async def change_password(request):
    principal = request['principal']
    ip = client_ip(request)
    if principal.via != 'session':
        return _fail(403, 'session_required', '请在 Web 页面登录后修改口令')
    if principal.source != 'local':
        return _fail(400, 'not_local', 'LDAP 账号的口令请在 LDAP 里修改')
    body = await _json(request)
    old, new = body.get('old_password'), body.get('new_password')
    wait = limiter.retry_after(principal.username, ip)
    if wait:
        return _fail(429, 'locked', f'失败次数过多,请 {wait // 60 + 1} 分钟后再试', wait)
    user, _ = await store.get_user(principal.user_id)
    if user is None or not isinstance(old, str) or not await verify_local(user, old):
        limiter.failure(principal.username, ip)
        audit.record_bg(principal.username, 'password.change', ip=ip, success=False, detail={'reason': 'bad_old'})
        return _fail(400, 'bad_old_password', '原口令不正确')
    error = passwords.validate_new_password(new, principal.username)
    if error:
        return _fail(400, 'weak_password', error)
    if new == old:
        return _fail(400, 'same_password', '新口令不能和原口令相同')
    await store.set_password(principal.user_id, await hash_password(new), must_change=False, default=False)
    # 其它终端的会话全部下线,当前会话保留
    await store.delete_user_sessions(principal.user_id, keep=principal.session_hash)
    limiter.success(principal.username, ip)
    audit.record_bg(principal.username, 'password.change', target=principal.username, ip=ip)
    return web.json_response({'success': True, 'message': '口令已修改,其它终端已下线'})


_GRAFANA_ROLES = {'admin': 'Admin'}


async def verify(request):
    """nginx auth_request 子请求:判断能否访问 AI 助手 / MCP / Grafana / 自定义告警。

    for=session  浏览器里的 AI 助手(只认会话 Cookie)
    for=mcp      外部 MCP 客户端(会话 Cookie 或 Basic 认证)
    for=grafana  Grafana 看板(会话 Cookie),返回 Grafana auth proxy 用的用户名和角色
    for=alert    自定义告警投递(Agent 账号或全局读写账号,Basic 认证)
    通过返回 204 并在响应头里带身份;未登录 401,权限不足 403(nginx 只认这三种)。
    """
    purpose = request.query.get('for', 'session')
    principal = request.get('principal')
    if principal is None or request.get('cross_site'):
        return web.Response(status=401)
    if purpose == 'alert':
        if not (principal.is_agent or (principal.is_human and principal.global_write
                                       and not principal.must_change_password)):
            return web.Response(status=403)
    else:
        if not principal.is_human or principal.must_change_password:
            return web.Response(status=403)
        if purpose == 'session' and principal.via not in ('session',):
            return web.Response(status=401)
        if purpose == 'grafana':
            if principal.via != 'session':
                return web.Response(status=401)
            # Grafana 的看板和数据源能查所有集群,做不到按集群隔离:对任何集群是「无」权限的人不开放
            if principal.env_scope()[0] != 'all':
                return web.Response(status=403)
    response = web.Response(status=204)
    response.headers['X-Kubedoor-User'] = principal.username
    response.headers['X-Kubedoor-Role'] = principal.role
    response.headers['X-Kubedoor-Clusters'] = json.dumps(dict(principal.clusters), separators=(',', ':'))
    response.headers['X-Kubedoor-Grafana-Role'] = _GRAFANA_ROLES.get(principal.role, 'Viewer')
    return response


def register_routes(app):
    app.router.add_post('/api/auth/login', login)
    app.router.add_post('/api/auth/logout', logout)
    app.router.add_get('/api/auth/me', me)
    app.router.add_post('/api/auth/password', change_password)
    # nginx 子请求一般是 GET,个别版本/配置会沿用原请求的方法,所以不限方法
    app.router.add_route('*', '/api/auth/verify', verify)
