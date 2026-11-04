"""镜像更新白名单与仓库凭据：管理员维护，数据库持久化。

凭据使用现有 AI_ENCRYPTION_KEY 加密；页面和审计均不返回密文或明文。
旧环境配置仅在设置尚未入库时导入一次，运行时不再依赖环境变量。
"""
from __future__ import annotations

import asyncio
import copy
import ipaddress
import json
import os
import re
import sys
from datetime import datetime
from urllib.parse import urlsplit
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from aiohttp import web
from loguru import logger

import db
from auth import audit, principal_of
from auth import crypto
from auth.authn import valid_username
from auth.context import client_ip
from auth.store import store


RULES_KEY = 'image_update_rules'
REGISTRIES_KEY = 'image_registry_credentials'
BEIJING = ZoneInfo('Asia/Shanghai')
DEFAULT_RULE = {'env': 'default', 'enabled': False, 'start_time': '20:00',
                'end_time': '08:00', 'users': []}
MAX_ITEMS = 1000
MAX_SECRET = 8192
_ENV = re.compile(r'[A-Za-z0-9_-]{1,200}')
_TIME = re.compile(r'(?:[01]\d|2[0-3]):[0-5]\d')
_writes = asyncio.Lock()


def _scope(value):
    if not isinstance(value, str) or not _ENV.fullmatch(value):
        raise ValueError('集群标识只能包含字母、数字、下划线和中划线，最长 200 个字符')
    return value


def normalize_rules(items):
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_ITEMS:
        raise ValueError('镜像更新规则必须为非空列表，最多 1000 项')
    rules, seen = [], set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError('镜像更新规则格式不正确')
        env = _scope(item.get('env'))
        if env in seen:
            raise ValueError('同一集群只能配置一条镜像更新规则')
        seen.add(env)
        enabled = item.get('enabled', False)
        if not isinstance(enabled, bool):
            raise ValueError('启用状态必须为布尔值')
        start, end = item.get('start_time', '20:00'), item.get('end_time', '08:00')
        if not isinstance(start, str) or not isinstance(end, str) or not _TIME.fullmatch(start) or not (end == '24:00' or _TIME.fullmatch(end)):
            raise ValueError('操作时段必须使用 HH:MM 格式，结束时间可填 24:00 表示当天结束')
        if start == end:
            raise ValueError('操作时段的开始和结束时间不能相同')
        users = item.get('users', [])
        if not isinstance(users, list) or len(users) > MAX_ITEMS or any(not isinstance(user, str) or not valid_username(user) for user in users):
            raise ValueError('用户白名单必须是有效用户名列表，最多 1000 项')
        users = list(dict.fromkeys(user.lower() for user in users))
        if enabled and not users:
            raise ValueError('启用镜像更新规则时必须填写用户白名单')
        rules.append({'env': env, 'enabled': enabled, 'start_time': start, 'end_time': end, 'users': users})
    if 'default' not in seen:
        raise ValueError('请保留 default 默认规则，未单独配置的集群使用此规则')
    return rules


def normalize_domain(value):
    if not isinstance(value, str) or not value or len(value) > 300 or any(c.isspace() or ord(c) < 32 for c in value):
        raise ValueError('仓库地址应为域名或 IP，可包含端口，不含协议和路径')
    try:
        parsed = urlsplit('//' + value)
        host, port = parsed.hostname, parsed.port
        if not host or parsed.netloc.endswith(':') or parsed.path or parsed.query or parsed.fragment or parsed.username is not None or parsed.password is not None:
            raise ValueError
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
        try:
            ipaddress.ip_address(host)
        except ValueError:
            labels = host.rstrip('.').split('.')
            if any(not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', label) for label in labels):
                raise ValueError
        return parsed.netloc.lower()
    except ValueError:
        raise ValueError('仓库地址应为域名或 IP，可包含端口，不含协议和路径') from None


def _sealed(value, previous):
    if value is None or value == '':
        if previous:
            return previous
        raise ValueError('新仓库凭据必须填写账号或 Access Key，以及密码或 Secret Key')
    if not isinstance(value, str) or len(value) > MAX_SECRET or any(ord(c) < 32 for c in value):
        raise ValueError('仓库凭据必须为不含换行的字符串，最长 8192 个字符')
    if crypto._key() is None:
        raise ValueError('未配置有效的 AI_ENCRYPTION_KEY，无法加密保存仓库凭据')
    return crypto.encrypt_secret(value)


def normalize_registries(items, existing=None):
    if not isinstance(items, list) or len(items) > MAX_ITEMS:
        raise ValueError('仓库凭据必须为列表，最多 1000 项')
    saved = {item['id']: item for item in (existing or []) if isinstance(item, dict) and item.get('id')}
    registries, scopes, ids = [], set(), set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError('仓库凭据格式不正确')
        domain, env = normalize_domain(item.get('domain')), _scope(item.get('env', 'default'))
        key = (domain, env)
        if key in scopes:
            raise ValueError('同一仓库与集群只能配置一组凭据')
        scopes.add(key)
        item_id = item.get('id')
        if item_id is not None and not isinstance(item_id, str):
            raise ValueError('仓库凭据记录标识无效')
        if item_id:
            if not isinstance(item_id, str) or item_id not in saved:
                raise ValueError('仓库凭据记录不存在，请刷新页面后重试')
            try:
                UUID(item_id)
            except ValueError:
                raise ValueError('仓库凭据记录标识无效') from None
        else:
            item_id = str(uuid4())
        if item_id in ids:
            raise ValueError('仓库凭据记录重复')
        ids.add(item_id)
        old = saved.get(item_id, {})
        registries.append({'id': item_id, 'domain': domain, 'env': env,
                           'ak': _sealed(item.get('ak'), old.get('ak')),
                           'sk': _sealed(item.get('sk'), old.get('sk'))})
    return registries


def public_registries(items):
    return [{'id': item['id'], 'domain': item['domain'], 'env': item['env'],
             'has_ak': bool(item.get('ak')), 'has_sk': bool(item.get('sk'))} for item in items]


def _iso(value):
    return value.isoformat() if isinstance(value, datetime) else value


def _response(row, field):
    value = (row or {}).get('value')
    if field == 'rules':
        items = normalize_rules(value) if value is not None else [copy.deepcopy(DEFAULT_RULE)]
    else:
        items = public_registries(value or [])
    return {field: items, 'updated_by': (row or {}).get('updated_by'),
            'updated_at': _iso((row or {}).get('updated_at'))}


def _admin(request):
    return principal_of(request).is_admin


def _fail(status, message):
    return web.json_response({'success': False, 'message': message, 'error': message}, status=status)


async def get_rules(request):
    if not _admin(request):
        return _fail(403, '只有系统管理员可以管理镜像设置')
    row = await store.get_setting_row(RULES_KEY)
    return web.json_response({'success': True, 'data': _response(row, 'rules')})


async def put_rules(request):
    if not _admin(request):
        return _fail(403, '只有系统管理员可以管理镜像设置')
    try:
        body = await request.json()
    except Exception:
        return _fail(400, '请求必须为有效 JSON')
    try:
        rules = normalize_rules(body.get('rules') if isinstance(body, dict) else None)
    except ValueError as exc:
        return _fail(400, str(exc))
    principal = principal_of(request)
    async with _writes:
        row = await store.put_setting_row(RULES_KEY, rules, principal.username)
    audit.record_bg(principal.username, 'image_rules.update', ip=client_ip(request),
                    detail={'rules': rules})
    return web.json_response({'success': True, 'data': _response(row, 'rules')})


async def get_registries(request):
    if not _admin(request):
        return _fail(403, '只有系统管理员可以管理镜像设置')
    row = await store.get_setting_row(REGISTRIES_KEY)
    return web.json_response({'success': True, 'data': _response(row, 'registries')})


async def put_registries(request):
    if not _admin(request):
        return _fail(403, '只有系统管理员可以管理镜像设置')
    try:
        body = await request.json()
    except Exception:
        return _fail(400, '请求必须为有效 JSON')
    principal = principal_of(request)
    async with _writes:
        existing = await store.get_setting(REGISTRIES_KEY, [])
        try:
            registries = normalize_registries(body.get('registries') if isinstance(body, dict) else None, existing)
        except ValueError as exc:
            return _fail(400, str(exc))
        row = await store.put_setting_row(REGISTRIES_KEY, registries, principal.username)
    audit.record_bg(principal.username, 'image_registries.update', ip=client_ip(request),
                    detail={'registries': [{'domain': item['domain'], 'env': item['env']} for item in registries]})
    return web.json_response({'success': True, 'data': _response(row, 'registries')})


async def check_update_allowed(env, username, now=None):
    """网页只读账号的额外白名单；不替代上层的操作权限校验。"""
    try:
        saved = await store.get_setting(RULES_KEY)
        rules = normalize_rules(saved) if saved is not None else [copy.deepcopy(DEFAULT_RULE)]
    except Exception:
        logger.warning('读取镜像更新规则失败，拒绝本次操作')
        return False, '镜像更新规则暂不可用，请联系系统管理员'
    by_env = {item['env']: item for item in rules}
    rule = by_env.get(env, by_env['default'])
    if not rule['enabled']:
        return False, '当前集群未启用镜像更新白名单'
    if not isinstance(username, str) or username.lower() not in rule['users']:
        return False, '当前用户不在镜像更新白名单中'
    current = now or datetime.now(BEIJING)
    if current.tzinfo is not None:
        current = current.astimezone(BEIJING)
    minute = current.hour * 60 + current.minute
    start, end = (int(value[:2]) * 60 + int(value[3:]) for value in (rule['start_time'], rule['end_time']))
    allowed = start <= minute < end if start < end else minute >= start or minute < end
    if not allowed:
        return False, f"当前集群仅允许在北京时间 {rule['start_time']}–{rule['end_time']} 更新镜像"
    return True, ''


async def registry_credentials():
    """仅供服务端查询镜像标签使用，返回兼容旧获取器的凭据映射。"""
    try:
        entries = await store.get_setting(REGISTRIES_KEY, [])
    except Exception:
        raise ValueError('仓库凭据暂不可用，请稍后重试') from None
    if not isinstance(entries, list):
        raise ValueError('仓库凭据配置格式不正确，请系统管理员重新保存')
    result = {}
    for item in entries:
        try:
            if not isinstance(item, dict) or not isinstance(item.get('ak'), str) or not isinstance(item.get('sk'), str):
                raise ValueError
            domain, env = normalize_domain(item.get('domain')), _scope(item.get('env'))
            credentials = {'ak': crypto.decrypt_secret(item['ak']), 'sk': crypto.decrypt_secret(item['sk'])}
        except (KeyError, ValueError, TypeError):
            raise ValueError('仓库凭据无法解密，请系统管理员重新保存') from None
        result.setdefault(domain, {})[env] = credentials
    return result


async def _insert_absent(key, value):
    row = await db.pg_fetchrow(
        'INSERT INTO auth_settings (key, value, updated_by) VALUES ($1, $2::jsonb, $3) '
        'ON CONFLICT (key) DO NOTHING RETURNING key',
        key, json.dumps(value, ensure_ascii=False), 'legacy-import')
    return row is not None


def _legacy_rules(raw):
    value = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(value, dict):
        raise ValueError('旧规则格式不正确')
    result = []
    for env, rule in value.items():
        if not isinstance(rule, dict):
            raise ValueError('旧规则格式不正确')
        if rule.get('isOperationAllowed') is True and 'allowedOperationPeriod' not in rule:
            raise ValueError('已启用的旧规则缺少操作时段')
        period = rule.get('allowedOperationPeriod', '20:00-08:00')
        if not isinstance(period, str) or len(period.split('-')) != 2:
            raise ValueError('旧规则时段格式不正确')
        start, end = period.split('-')
        enabled = rule.get('isOperationAllowed', False)
        # 旧实现的相同起止时间永远不放行，迁移后保留为禁用，避免意外变成全天允许。
        if start == end and _TIME.fullmatch(start):
            enabled, start, end = False, DEFAULT_RULE['start_time'], DEFAULT_RULE['end_time']
        result.append({'env': env, 'enabled': enabled,
                       'start_time': start, 'end_time': end, 'users': rule.get('user', [])})
    if 'default' not in value:
        result.insert(0, copy.deepcopy(DEFAULT_RULE))
    return normalize_rules(result)


def _legacy_registries(raw):
    value = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(value, dict):
        raise ValueError('旧仓库凭据格式不正确')
    entries = []
    for domain, scopes in value.items():
        domain = normalize_domain(domain)
        if not isinstance(scopes, dict):
            raise ValueError('旧仓库凭据格式不正确')
        for env, credentials in scopes.items():
            if not isinstance(credentials, dict):
                raise ValueError('旧仓库凭据格式不正确')
            entries.append({'domain': domain, 'env': env, 'ak': credentials.get('ak'), 'sk': credentials.get('sk')})
    return normalize_registries(entries)


async def import_legacy(update_raw=None, registry_raw=None):
    """只导入尚不存在的设置；返回状态，不输出凭据或旧配置内容。"""
    result = {}
    async with _writes:
        for name, key, raw, normalize in (
                ('rules', RULES_KEY, update_raw, _legacy_rules),
                ('registries', REGISTRIES_KEY, registry_raw, _legacy_registries)):
            if await store.get_setting_row(key) is not None:
                result[name] = 'present'
                continue
            if raw is None or raw == '':
                result[name] = 'absent'
                continue
            try:
                value = normalize(raw)
            except (ValueError, TypeError, KeyError):
                result[name] = 'invalid'
                logger.warning('旧镜像设置 {} 未导入：格式无效或加密配置不可用', name)
                continue
            result[name] = 'imported' if await _insert_absent(key, value) else 'present'
    return result


async def on_startup(app):
    result = await import_legacy(os.environ.get('UPDATE_IMAGE'), os.environ.get('REGISTRY_SECRET'))
    for name, status in result.items():
        if status == 'imported':
            logger.info('已将旧镜像设置 {} 导入数据库', name)


def register_routes(app):
    app.router.add_get('/api/admin/image-update-rules', get_rules)
    app.router.add_put('/api/admin/image-update-rules', put_rules)
    app.router.add_get('/api/admin/image-registries', get_registries)
    app.router.add_put('/api/admin/image-registries', put_registries)


async def _import_cli(payload):
    try:
        await db.init_pool()
        result = await import_legacy(payload.get('update_image'), payload.get('registry_secret'))
        return {'success': 'invalid' not in result.values(), **result}
    finally:
        await db.close_pool()


def main(argv=None):
    """安装脚本通过 stdin 发送旧配置，stdout 只输出状态 JSON。"""
    if (sys.argv[1:] if argv is None else argv) != ['import-legacy']:
        print(json.dumps({'success': False, 'error': '用法: python -m image_settings import-legacy'}))
        return 2
    try:
        raw = sys.stdin.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError
        result = asyncio.run(_import_cli(payload))
    except Exception:
        result = {'success': False, 'error': '旧镜像设置导入失败，请检查数据库连接、加密密钥及旧配置格式'}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['success'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
