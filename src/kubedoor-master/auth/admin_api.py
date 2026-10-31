"""系统管理接口(/api/admin/*,仅系统管理员):用户、集群授权、LDAP 设置、操作审计、权限与审批。
获取代码接口的配置(/api/admin/code-info*)和功能放在一起,见 func_manager/code_info.py。

内置账号规则:
  - kubedoor(内置超级管理员):只能由它自己在「修改口令」里改密;不能删除、禁用、改权限、重置口令、强制下线;
  - 内置 Agent 账号:不能删除,系统管理员可以重置口令(改完要同步更新各集群 agent 的 MASTER_WS)。
系统管理员不能删除、禁用、降级自己,避免误操作把自己锁在外面。
"""

import asyncio
import json
from datetime import datetime

from aiohttp import web

from . import audit, ldap_auth, passwords, permissions
from .authn import valid_username
from .context import client_ip
from .principal import CLUSTER_PERMS, HUMAN_ROLES, ROLES
from .ratelimit import limiter
from .store import hash_password, store

# 由 auth.setup 注入:当前已知集群 [(env, online)]
cluster_list = None


def _fail(status, message, code='bad_request'):
    return web.json_response({'success': False, 'code': code, 'message': message, 'error': message}, status=status)


def _iso(value):
    return value.isoformat() if isinstance(value, datetime) else value


def is_super_admin(user):
    return bool(user.get('builtin')) and user.get('role') == 'admin'


def _public_user(user):
    data = {k: _iso(v) for k, v in user.items() if k not in ('password_hash',)}
    data['clusters'] = user.get('clusters', {})
    data['super_admin'] = is_super_admin(user)
    return data


async def _json(request):
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


async def _target(request):
    try:
        user_id = int(request.match_info['user_id'])
    except (KeyError, ValueError):
        return None, _fail(400, '用户 ID 不正确')
    user, _ = await store.get_user(user_id)
    if user is None:
        return None, _fail(404, '用户不存在', 'not_found')
    return user, None


def _protect_super_admin(user):
    if is_super_admin(user):
        return _fail(403, f'{user["username"]} 是内置超级管理员,只能由它自己登录后修改口令,不允许其它任何操作', 'builtin_protected')
    return None


def _role_error(source, role):
    if role not in ROLES:
        return '全局权限取值不正确'
    if source == 'ldap' and role == 'agent':
        return 'LDAP 账号不能设为 Agent 账号'
    return None


# ---------------------------------------------------------------------------
# 用户
# ---------------------------------------------------------------------------
async def list_users(request):
    users = await store.list_users()
    return web.json_response({'success': True, 'data': [_public_user(u) for u in users]})


async def create_user(request):
    principal = request['principal']
    body = await _json(request)
    username = str(body.get('username') or '').strip()
    source = body.get('source') or 'local'
    role = body.get('role') or 'read'
    if not valid_username(username):
        return _fail(400, '用户名只能包含字母、数字和 . _ @ -,以字母或数字开头,最长 64 位')
    if source not in ('local', 'ldap'):
        return _fail(400, '账号类型只能是本地或 LDAP')
    error = _role_error(source, role)
    if error:
        return _fail(400, error)
    password_hash = None
    if source == 'local':
        password = body.get('password')
        error = passwords.validate_new_password(password, username)
        if error:
            return _fail(400, error)
        password_hash = await hash_password(password)
    user = await store.create_user(
        username, source, role, password_hash,
        display_name=str(body.get('display_name') or '').strip()[:100],
        email=str(body.get('email') or '').strip()[:200],
        enabled=body.get('enabled', True) is not False,
        comment=str(body.get('comment') or '').strip()[:500],
        # 管理员代设的口令,人类账号首次登录必须自己改掉
        must_change=source == 'local' and role in HUMAN_ROLES,
    )
    if user is None:
        return _fail(409, f'用户名 {username} 已存在(不区分大小写)', 'conflict')
    audit.record_bg(principal.username, 'user.create', target=username, ip=client_ip(request),
                    detail={'source': source, 'role': role})
    return web.json_response({'success': True, 'data': _public_user(user)})


async def update_user(request):
    principal = request['principal']
    user, error = await _target(request)
    if error:
        return error
    error = _protect_super_admin(user)
    if error:
        return error
    body = await _json(request)
    fields = {}
    if 'role' in body and body['role'] != user['role']:
        error = _role_error(user['source'], body['role'])
        if error:
            return _fail(400, error)
        if user['id'] == principal.user_id:
            return _fail(400, '不能修改自己的全局权限')
        if user['builtin']:
            return _fail(400, '内置账号的权限不能修改')
        fields['role'] = body['role']
    if 'enabled' in body and bool(body['enabled']) != user['enabled']:
        if user['id'] == principal.user_id:
            return _fail(400, '不能禁用自己')
        fields['enabled'] = bool(body['enabled'])
    for key, limit in (('display_name', 100), ('email', 200), ('comment', 500)):
        if key in body:
            fields[key] = str(body[key] or '').strip()[:limit]
    await store.update_user(user['id'], **fields)
    if fields.get('enabled') is False or fields.get('role') == 'agent':
        await store.delete_user_sessions(user['id'])
    changed = {k: {'from': _iso(user.get(k)), 'to': v} for k, v in fields.items() if user.get(k) != v}
    if changed:
        audit.record_bg(principal.username, 'user.update', target=user['username'], ip=client_ip(request),
                        detail=changed)
    return web.json_response({'success': True})


async def delete_user(request):
    principal = request['principal']
    user, error = await _target(request)
    if error:
        return error
    error = _protect_super_admin(user)
    if error:
        return error
    if user['builtin']:
        return _fail(400, '内置账号不能删除')
    if user['id'] == principal.user_id:
        return _fail(400, '不能删除自己')
    await store.delete_user(user['id'])
    audit.record_bg(principal.username, 'user.delete', target=user['username'], ip=client_ip(request),
                    detail={'source': user['source'], 'role': user['role']})
    return web.json_response({'success': True})


async def reset_password(request):
    principal = request['principal']
    user, error = await _target(request)
    if error:
        return error
    error = _protect_super_admin(user)
    if error:
        return error
    if user['source'] != 'local':
        return _fail(400, 'LDAP 账号的口令在 LDAP 里管理')
    if user['id'] == principal.user_id:
        return _fail(400, '修改自己的口令请用右上角的「修改口令」')
    body = await _json(request)
    password = body.get('password')
    error = passwords.validate_new_password(password, user['username'])
    if error:
        return _fail(400, error)
    await store.set_password(user['id'], await hash_password(password),
                             must_change=user['role'] in HUMAN_ROLES, default=False)
    await store.delete_user_sessions(user['id'])
    limiter.reset(user['username'])
    audit.record_bg(principal.username, 'user.reset_password', target=user['username'], ip=client_ip(request))
    return web.json_response({'success': True})


async def logout_user(request):
    principal = request['principal']
    user, error = await _target(request)
    if error:
        return error
    error = _protect_super_admin(user)
    if error:
        return error
    await store.delete_user_sessions(user['id'])
    audit.record_bg(principal.username, 'user.logout', target=user['username'], ip=client_ip(request))
    return web.json_response({'success': True})


async def get_user_clusters(request):
    user, error = await _target(request)
    if error:
        return error
    _, grants = await store.get_user(user['id'])
    return web.json_response({'success': True, 'data': {
        'user': _public_user(user),
        'clusters': [{'env': env, 'permission': perm} for env, perm in sorted(grants.items())],
        'known': [{'env': env, 'online': online} for env, online in await cluster_list()],
    }})


async def set_user_clusters(request):
    principal = request['principal']
    user, error = await _target(request)
    if error:
        return error
    error = _protect_super_admin(user)
    if error:
        return error
    if user['role'] == 'admin':
        return _fail(400, '系统管理员对所有集群都是管理员权限,不能再单独指定集群权限')
    if user['role'] == 'agent':
        return _fail(400, 'Agent 账号不访问集群数据,不能指定集群权限')
    body = await _json(request)
    items = body.get('clusters')
    if not isinstance(items, list) or len(items) > 2000:
        return _fail(400, 'clusters 必须是数组')
    grants = {}
    for item in items:
        env = str((item or {}).get('env') or '').strip() if isinstance(item, dict) else ''
        perm = item.get('permission') if isinstance(item, dict) else None
        if not env or len(env) > 253:
            return _fail(400, '集群名不能为空')
        if perm not in CLUSTER_PERMS:
            return _fail(400, f'集群 {env} 的权限取值不正确(admin / rw / read / none)')
        if env in grants:
            return _fail(400, f'集群 {env} 重复')
        grants[env] = perm
    _, before = await store.get_user(user['id'])
    await store.set_clusters(user['id'], grants, principal.username)
    diff = {env: {'from': before.get(env), 'to': grants.get(env)}
            for env in sorted(set(before) | set(grants)) if before.get(env) != grants.get(env)}
    if diff:
        audit.record_bg(principal.username, 'user.clusters', target=user['username'], ip=client_ip(request),
                        detail=diff)
    return web.json_response({'success': True})


async def list_clusters(request):
    return web.json_response({'success': True, 'data': [
        {'env': env, 'online': online} for env, online in await cluster_list()]})


# ---------------------------------------------------------------------------
# LDAP
# ---------------------------------------------------------------------------
async def get_ldap(request):
    cfg = await store.get_setting('ldap', {})
    return web.json_response({'success': True, 'data': ldap_auth.public_config(cfg)})


async def put_ldap(request):
    principal = request['principal']
    body = await _json(request)
    existing = await store.get_setting('ldap', {}) or {}
    try:
        cfg = ldap_auth.normalize_config(body, existing)
    except ValueError as exc:
        return _fail(400, str(exc))
    await store.put_setting('ldap', cfg, principal.username)
    public = ldap_auth.public_config(cfg)
    audit.record_bg(principal.username, 'ldap.update', ip=client_ip(request),
                    detail={k: v for k, v in public.items() if k != 'ca_cert'})
    return web.json_response({'success': True, 'data': public})


async def test_ldap(request):
    body = await _json(request)
    existing = await store.get_setting('ldap', {}) or {}
    raw = body.get('config')
    try:
        cfg = ldap_auth.normalize_config(raw, existing) if isinstance(raw, dict) else {**ldap_auth.DEFAULTS, **existing}
    except ValueError as exc:
        return _fail(400, str(exc))
    username = str(body.get('username') or '').strip()
    if username and not valid_username(username):
        return _fail(400, '测试用户名格式不正确')
    password = body.get('password') if isinstance(body.get('password'), str) else ''
    result = await asyncio.to_thread(ldap_auth.test_connection, cfg, username, password)
    return web.json_response({'success': True, 'data': result})


# ---------------------------------------------------------------------------
# 权限与审批(操作权限表 + 命令清单,见 permissions.py)
# ---------------------------------------------------------------------------
async def get_permission_policy(request):
    """行定义、当前值、默认值(「恢复默认」用)、命令清单、内置参数检查说明。"""
    policy = await permissions.load_policy(force=True)
    return web.json_response({'success': True, 'data': permissions.admin_payload(policy)})


async def put_permission_policy(request):
    principal = request['principal']
    body = await _json(request)
    before = await permissions.load_policy(force=True)
    try:
        saved = await permissions.save_policy(body.get('ops'), body.get('commands'), principal.username)
    except permissions.PolicyError as exc:
        return _fail(400, str(exc))
    changes = permissions.policy_diff(before, saved)
    if changes:
        audit.record_bg(principal.username, 'permission.policy', ip=client_ip(request), detail=changes)
    return web.json_response({'success': True, 'data': permissions.admin_payload(saved)})


# ---------------------------------------------------------------------------
# 审计
# ---------------------------------------------------------------------------
async def list_audit(request):
    q = request.query
    try:
        page = max(1, int(q.get('page') or 1))
        page_size = min(200, max(1, int(q.get('pageSize') or 20)))
        success = None if q.get('success') in (None, '') else q.get('success') in ('1', 'true')
        rows, total = await audit.query(
            username=q.get('username', '').strip(), action=q.get('action', '').strip(),
            env=q.get('env', '').strip(), keyword=q.get('keyword', '').strip(),
            start=q.get('start') or None, end=q.get('end') or None, success=success,
            page=page, page_size=page_size,
        )
    except ValueError:
        return _fail(400, '查询参数不正确')
    data = []
    for row in rows:
        item = {k: _iso(v) for k, v in dict(row).items()}
        if isinstance(item.get('detail'), str):
            try:
                item['detail'] = json.loads(item['detail'])
            except ValueError:
                pass
        data.append(item)
    return web.json_response({'success': True, 'data': data, 'total': total})


async def list_audit_actions(request):
    return web.json_response({'success': True, 'data': await audit.actions()})


def register_routes(app):
    app.router.add_get('/api/admin/users', list_users)
    app.router.add_post('/api/admin/users', create_user)
    app.router.add_put('/api/admin/users/{user_id}', update_user)
    app.router.add_delete('/api/admin/users/{user_id}', delete_user)
    app.router.add_put('/api/admin/users/{user_id}/password', reset_password)
    app.router.add_post('/api/admin/users/{user_id}/logout', logout_user)
    app.router.add_get('/api/admin/users/{user_id}/clusters', get_user_clusters)
    app.router.add_put('/api/admin/users/{user_id}/clusters', set_user_clusters)
    app.router.add_get('/api/admin/clusters', list_clusters)
    app.router.add_get('/api/admin/ldap', get_ldap)
    app.router.add_put('/api/admin/ldap', put_ldap)
    app.router.add_post('/api/admin/ldap/test', test_ldap)
    app.router.add_get('/api/admin/permission-policy', get_permission_policy)
    app.router.add_put('/api/admin/permission-policy', put_permission_policy)
    app.router.add_get('/api/admin/audit', list_audit)
    app.router.add_get('/api/admin/audit/actions', list_audit_actions)
