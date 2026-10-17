"""账号、集群授权、会话、系统设置的数据库访问 + 进程内缓存。

master 是单实例,缓存放内存即可:管理员改了用户/权限会主动失效,所以权限变更立即生效;
缓存 TTL 只是兜底(比如直接改库)。会话 token 只在 Cookie 里出现,库里存它的 SHA-256。
"""

import asyncio
import hashlib
import json
import os
import secrets
import time
from datetime import datetime, timedelta, timezone

from loguru import logger

import db

from . import passwords
from .principal import make_principal

SESSION_COOKIE = 'kubedoor_session'
SESSION_HOURS = float(os.environ.get('AUTH_SESSION_HOURS', '12'))
REMEMBER_DAYS = float(os.environ.get('AUTH_REMEMBER_DAYS', '7'))
IDLE_HOURS = float(os.environ.get('AUTH_IDLE_HOURS', '4'))

CACHE_TTL = 30  # 秒
TOUCH_INTERVAL = 60  # last_seen_at 最多每分钟回写一次
BASIC_CACHE_TTL = 300  # Basic 认证(agent / MCP / 脚本)校验结果缓存,避免每个请求都做一次哈希或 LDAP 绑定

USER_COLUMNS = (
    'id, username, source, role, password_hash, display_name, email, enabled, builtin, '
    'must_change_password, password_default, last_login_at, last_login_ip, comment, created_at, updated_at'
)


def token_hash(token):
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def _now():
    return datetime.now(timezone.utc)


class AuthStore:
    def __init__(self):
        self._users = {}  # user_id -> (过期时间, user dict, clusters dict)
        self._sessions = {}  # token_hash -> (过期时间, session dict)
        self._basic = {}  # 凭据哈希 -> (过期时间, user_id, 校验时的 password_hash)
        self._tasks = set()

    # ------------------------------------------------------------------ 缓存
    def invalidate_user(self, user_id):
        self._users.pop(user_id, None)
        for key in [k for k, (_, s) in self._sessions.items() if s['user_id'] == user_id]:
            self._sessions.pop(key, None)
        for key in [k for k, (_, uid, _) in self._basic.items() if uid == user_id]:
            self._basic.pop(key, None)

    def invalidate_all(self):
        self._users.clear()
        self._sessions.clear()
        self._basic.clear()

    def _spawn(self, coro):
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ------------------------------------------------------------------ 用户
    async def get_user(self, user_id):
        cached = self._users.get(user_id)
        if cached and cached[0] > time.monotonic():
            return cached[1], cached[2]
        row = await db.pg_fetchrow(f'SELECT {USER_COLUMNS} FROM auth_users WHERE id=$1', user_id)
        if row is None:
            self._users.pop(user_id, None)
            return None, {}
        user = dict(row)
        clusters = await self.get_clusters(user_id)
        self._users[user_id] = (time.monotonic() + CACHE_TTL, user, clusters)
        return user, clusters

    async def get_user_by_name(self, username):
        row = await db.pg_fetchrow(
            f'SELECT {USER_COLUMNS} FROM auth_users WHERE lower(username)=lower($1)', username)
        return dict(row) if row else None

    async def get_clusters(self, user_id):
        rows = await db.pg_fetch('SELECT env, permission FROM auth_user_clusters WHERE user_id=$1', user_id)
        return {row['env']: row['permission'] for row in rows}

    async def principal_for(self, user_id, via, session_hash=None, must_change=None):
        user, clusters = await self.get_user(user_id)
        if user is None or not user['enabled']:
            return None
        return make_principal(user, clusters, via=via, session_hash=session_hash, must_change=must_change)

    async def principal_by_name(self, username, via):
        """内部 token 代表某个用户调用时用:按用户名加载(只认启用的人类账号)。"""
        user = await self.get_user_by_name(username)
        if user is None:
            return None
        principal = await self.principal_for(user['id'], via)
        return principal if principal is not None and principal.is_human else None

    async def list_users(self):
        rows = await db.pg_fetch(
            f'SELECT {USER_COLUMNS}, '
            '(SELECT count(*) FROM auth_sessions s WHERE s.user_id=u.id AND s.expires_at>now()) AS session_count '
            'FROM auth_users u ORDER BY builtin DESC, lower(username)')
        users = [dict(r) for r in rows]
        grants = await db.pg_fetch('SELECT user_id, env, permission FROM auth_user_clusters ORDER BY env')
        by_user = {}
        for row in grants:
            by_user.setdefault(row['user_id'], {})[row['env']] = row['permission']
        for user in users:
            user['clusters'] = by_user.get(user['id'], {})
        return users

    async def create_user(self, username, source, role, password_hash=None, display_name='', email='',
                          enabled=True, comment='', builtin=False, must_change=False, password_default=False):
        row = await db.pg_fetchrow(
            'INSERT INTO auth_users (username, source, role, password_hash, display_name, email, enabled, '
            'comment, builtin, must_change_password, password_default, password_changed_at) '
            'VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11, CASE WHEN $4::text IS NULL THEN NULL ELSE now() END) '
            'ON CONFLICT ((lower(username))) DO NOTHING '
            f'RETURNING {USER_COLUMNS}',
            username, source, role, password_hash, display_name, email, enabled, comment, builtin,
            must_change, password_default,
        )
        return dict(row) if row else None

    async def update_user(self, user_id, **fields):
        allowed = ('role', 'enabled', 'display_name', 'email', 'comment')
        sets, args = [], []
        for key in allowed:
            if key in fields:
                args.append(fields[key])
                sets.append(f'{key}=${len(args)}')
        if not sets:
            return
        args.append(user_id)
        await db.pg_execute(f"UPDATE auth_users SET {', '.join(sets)}, updated_at=now() WHERE id=${len(args)}", *args)
        if fields.get('role') in ('admin', 'agent'):
            # 管理员拥有全部权限,Agent 账号不访问集群数据,两者都不保留集群授权
            await db.pg_execute('DELETE FROM auth_user_clusters WHERE user_id=$1', user_id)
        self.invalidate_user(user_id)

    async def delete_user(self, user_id):
        await db.pg_execute('DELETE FROM auth_users WHERE id=$1 AND NOT builtin', user_id)
        self.invalidate_user(user_id)

    async def set_password(self, user_id, password_hash, must_change=False, default=False):
        await db.pg_execute(
            'UPDATE auth_users SET password_hash=$1, must_change_password=$2, password_default=$3, '
            'password_changed_at=now(), updated_at=now() WHERE id=$4',
            password_hash, must_change, default, user_id,
        )
        self.invalidate_user(user_id)

    async def update_profile(self, user_id, display_name=None, email=None):
        """LDAP 登录时同步姓名、邮箱。"""
        await db.pg_execute(
            'UPDATE auth_users SET display_name=COALESCE($1, display_name), email=COALESCE($2, email) WHERE id=$3',
            display_name, email, user_id,
        )
        self._users.pop(user_id, None)

    async def set_clusters(self, user_id, grants, updated_by):
        """整体替换某个用户的集群授权。grants: {env: rw/read/none}"""
        async with db.get_pool().acquire() as conn:
            async with conn.transaction():
                await conn.execute('DELETE FROM auth_user_clusters WHERE user_id=$1', user_id)
                if grants:
                    await conn.executemany(
                        'INSERT INTO auth_user_clusters (user_id, env, permission, updated_by) VALUES ($1,$2,$3,$4)',
                        [(user_id, env, perm, updated_by) for env, perm in sorted(grants.items())],
                    )
        self.invalidate_user(user_id)

    async def record_login(self, user_id, ip):
        await db.pg_execute('UPDATE auth_users SET last_login_at=now(), last_login_ip=$1 WHERE id=$2', ip, user_id)

    # ------------------------------------------------------------------ 会话
    async def create_session(self, user_id, remember, ip, user_agent):
        token = secrets.token_urlsafe(32)
        lifetime = timedelta(days=REMEMBER_DAYS) if remember else timedelta(hours=SESSION_HOURS)
        await db.pg_execute(
            'INSERT INTO auth_sessions (token_hash, user_id, expires_at, remember, ip, user_agent) '
            'VALUES ($1,$2,$3,$4,$5,$6)',
            token_hash(token), user_id, _now() + lifetime, bool(remember), ip, (user_agent or '')[:300],
        )
        return token, lifetime

    async def resolve_session(self, token):
        """返回有效会话的 dict;过期、空闲超时、不存在返回 None。"""
        if not token or len(token) > 200:
            return None
        key = token_hash(token)
        now = _now()
        cached = self._sessions.get(key)
        if cached and cached[0] > time.monotonic():
            session = cached[1]
        else:
            row = await db.pg_fetchrow(
                'SELECT token_hash, user_id, created_at, last_seen_at, expires_at, remember FROM auth_sessions '
                'WHERE token_hash=$1', key)
            if row is None:
                self._sessions.pop(key, None)
                return None
            session = dict(row)
            self._sessions[key] = (time.monotonic() + CACHE_TTL, session)
        if session['expires_at'] <= now:
            await self.delete_session(key)
            return None
        if not session['remember'] and IDLE_HOURS > 0 and session['last_seen_at'] + timedelta(hours=IDLE_HOURS) <= now:
            await self.delete_session(key)
            return None
        if (now - session['last_seen_at']).total_seconds() > TOUCH_INTERVAL:
            session['last_seen_at'] = now
            self._spawn(self._touch(key, now))
        return session

    async def _touch(self, key, when):
        try:
            await db.pg_execute('UPDATE auth_sessions SET last_seen_at=$1 WHERE token_hash=$2', when, key)
        except Exception as exc:
            logger.warning(f'更新会话活跃时间失败: {exc}')

    async def delete_session(self, key):
        self._sessions.pop(key, None)
        await db.pg_execute('DELETE FROM auth_sessions WHERE token_hash=$1', key)

    async def delete_user_sessions(self, user_id, keep=None):
        await db.pg_execute(
            'DELETE FROM auth_sessions WHERE user_id=$1 AND token_hash IS DISTINCT FROM $2', user_id, keep)
        for key in [k for k, (_, s) in self._sessions.items() if s['user_id'] == user_id and k != keep]:
            self._sessions.pop(key, None)

    async def cleanup_sessions(self):
        idle = timedelta(hours=IDLE_HOURS) if IDLE_HOURS > 0 else timedelta(days=36500)
        status = await db.pg_execute(
            'DELETE FROM auth_sessions WHERE expires_at <= now() OR (NOT remember AND last_seen_at <= now() - $1::interval)',
            idle,
        )
        return status

    # ------------------------------------------------------------------ Basic 认证缓存
    def basic_cached(self, username, password):
        key = hashlib.sha256(f'{username.lower()}\0{password}'.encode('utf-8')).hexdigest()
        item = self._basic.get(key)
        if item and item[0] > time.monotonic():
            return key, item[1], item[2]
        self._basic.pop(key, None)
        return key, None, None

    def remember_basic(self, key, user_id, password_hash):
        if len(self._basic) > 5000:
            self._basic.clear()
        self._basic[key] = (time.monotonic() + BASIC_CACHE_TTL, user_id, password_hash)

    # ------------------------------------------------------------------ 设置
    async def get_setting(self, key, default=None):
        value = await db.pg_fetchval('SELECT value FROM auth_settings WHERE key=$1', key)
        if value is None:
            return default
        return json.loads(value) if isinstance(value, str) else value

    async def put_setting(self, key, value, updated_by):
        await db.pg_execute(
            'INSERT INTO auth_settings (key, value, updated_by) VALUES ($1, $2::jsonb, $3) '
            'ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_by=EXCLUDED.updated_by, updated_at=now()',
            key, json.dumps(value, ensure_ascii=False), updated_by,
        )


store = AuthStore()


async def verify_local(user, password):
    """在线程池里校验本地口令(scrypt 约 0.3 秒,不能卡主 loop)。"""
    return await asyncio.to_thread(passwords.verify_password, password, user.get('password_hash'))


async def hash_password(password):
    return await asyncio.to_thread(passwords.hash_password, password)
