"""用户名口令校验:本地账号优先,其次 LDAP。登录接口和 Basic 认证共用。"""

import asyncio
import base64
import binascii
import re

from loguru import logger

from . import audit, ldap_auth, passwords
from .principal import make_principal
from .ratelimit import limiter
from .store import hash_password, store, verify_local

# 用户名字符集:字母数字开头,允许 . _ @ -;同时挡住 LDAP 过滤器 / DN 里的特殊字符
USERNAME_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._@-]{0,63}')
GENERIC_FAILURE = '用户名或口令错误'


class AuthFailure(Exception):
    def __init__(self, status, code, message, retry_after=0):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retry_after = retry_after


def valid_username(username):
    return isinstance(username, str) and USERNAME_RE.fullmatch(username) is not None


async def _ldap_login(username, password, user, ip):
    """返回 (校验是否通过, 用户行)。LDAP 未启用或不可用抛 AuthFailure。"""
    cfg = await store.get_setting('ldap', {}) or {}
    if not cfg.get('enabled'):
        if user is not None:
            raise AuthFailure(503, 'ldap_disabled', 'LDAP 认证未启用,请联系管理员')
        await asyncio.to_thread(passwords.burn_time, password)
        return False, None
    try:
        info = await asyncio.to_thread(ldap_auth.authenticate, cfg, username, password)
    except ldap_auth.LdapError as exc:
        logger.error(f'LDAP 认证出错 user={username}: {exc}')
        raise AuthFailure(503, 'ldap_unavailable', 'LDAP 服务暂时不可用,请稍后再试或联系管理员')
    if info is None:
        return False, user
    if user is None:
        if not cfg.get('auto_create', True):
            raise AuthFailure(403, 'not_provisioned', '该 LDAP 账号还没有开通 KubeDoor,请联系管理员')
        role = cfg.get('default_role') if cfg.get('default_role') in ('read', 'none') else 'read'
        user = await store.create_user(username, 'ldap', role, display_name=info.get('display_name') or '',
                                       email=info.get('email') or '')
        if user is None:  # 并发登录时被别的请求先建了
            user = await store.get_user_by_name(username)
        else:
            logger.info(f'LDAP 用户首次登录,自动开通: {username} 全局权限={role}')
            audit.record_bg(username, 'user.auto_create', target=username, ip=ip,
                            detail={'source': 'ldap', 'role': role})
    elif (info.get('display_name') or '') != (user.get('display_name') or '') or \
            (info.get('email') or '') != (user.get('email') or ''):
        await store.update_profile(user['id'], info.get('display_name') or '', info.get('email') or '')
    return True, user


async def check_credentials(username, password, ip):
    """校验用户名口令。成功返回用户行(dict);失败抛 AuthFailure。"""
    username = (username or '').strip()
    if not valid_username(username) or not isinstance(password, str) or not password or len(password) > 1024:
        raise AuthFailure(401, 'invalid_credentials', GENERIC_FAILURE)
    wait = limiter.retry_after(username, ip)
    if wait:
        raise AuthFailure(429, 'locked', f'失败次数过多,账号已临时锁定,请 {wait // 60 + 1} 分钟后再试', wait)

    user = await store.get_user_by_name(username)
    if user is not None and user['source'] == 'local':
        ok = await verify_local(user, password)
        if ok and passwords.needs_rehash(user['password_hash']):
            await store.set_password(user['id'], await hash_password(password),
                                     user['must_change_password'], user['password_default'])
    else:
        ok, user = await _ldap_login(username, password, user, ip)

    if not ok or user is None:
        limiter.failure(username, ip)
        raise AuthFailure(401, 'invalid_credentials', GENERIC_FAILURE)
    limiter.success(username, ip)
    if not user['enabled']:
        raise AuthFailure(403, 'disabled', '账号已被禁用,请联系管理员')
    return user


def parse_basic(header):
    """解析 Authorization: Basic,失败返回 (None, None)。"""
    if not header or header[:6].lower() != 'basic ':
        return None, None
    try:
        decoded = base64.b64decode(header[6:].strip(), validate=True).decode('utf-8')
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None, None
    username, sep, password = decoded.partition(':')
    if not sep:
        return None, None
    return username, password


async def principal_from_basic(header, ip):
    """Basic 认证(agent、MCP 客户端、脚本)。校验结果缓存几分钟,口令变了缓存自动失效。"""
    username, password = parse_basic(header)
    if username is None:
        raise AuthFailure(401, 'invalid_credentials', GENERIC_FAILURE)
    key, user_id, cached_hash = store.basic_cached(username, password)
    if user_id is not None:
        user, clusters = await store.get_user(user_id)
        if user and user['enabled'] and (user['source'] == 'ldap' or user['password_hash'] == cached_hash):
            return make_principal(user, clusters, via='basic')
    user = await check_credentials(username, password, ip)
    store.remember_basic(key, user['id'], user.get('password_hash'))
    user, clusters = await store.get_user(user['id'])
    return make_principal(user, clusters, via='basic')
