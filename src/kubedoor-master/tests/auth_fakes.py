"""认证相关测试共用的内存版用户库(替代 auth.store.AuthStore 里访问数据库的部分)。"""

import itertools
import secrets
from datetime import datetime, timedelta, timezone

from auth import store as store_module
from auth.principal import make_principal
from auth.store import AuthStore, token_hash


class FakeStore(AuthStore):
    def __init__(self):
        super().__init__()
        self.users = {}
        self.grants = {}
        self.sessions = {}
        self.settings = {}
        self._ids = itertools.count(1)

    # ---- 用户 ----
    async def get_user(self, user_id):
        user = self.users.get(user_id)
        return (dict(user), dict(self.grants.get(user_id, {}))) if user else (None, {})

    async def get_user_by_name(self, username):
        for user in self.users.values():
            if user['username'].lower() == username.lower():
                return dict(user)
        return None

    async def get_clusters(self, user_id):
        return dict(self.grants.get(user_id, {}))

    async def principal_for(self, user_id, via, session_hash=None, must_change=None):
        user, clusters = await self.get_user(user_id)
        if user is None or not user['enabled']:
            return None
        return make_principal(user, clusters, via=via, session_hash=session_hash, must_change=must_change)

    async def list_users(self):
        result = []
        for user in self.users.values():
            item = dict(user)
            item['clusters'] = dict(self.grants.get(user['id'], {}))
            item['session_count'] = sum(1 for s in self.sessions.values() if s['user_id'] == user['id'])
            result.append(item)
        return result

    async def create_user(self, username, source, role, password_hash=None, display_name='', email='',
                          enabled=True, comment='', builtin=False, must_change=False, password_default=False):
        if await self.get_user_by_name(username):
            return None
        user_id = next(self._ids)
        self.users[user_id] = {
            'id': user_id, 'username': username, 'source': source, 'role': role, 'password_hash': password_hash,
            'display_name': display_name, 'email': email, 'enabled': enabled, 'builtin': builtin,
            'must_change_password': must_change, 'password_default': password_default,
            'last_login_at': None, 'last_login_ip': '', 'comment': comment,
            'created_at': datetime.now(timezone.utc), 'updated_at': datetime.now(timezone.utc),
        }
        return dict(self.users[user_id])

    async def update_user(self, user_id, **fields):
        self.users[user_id].update(fields)
        if fields.get('role') in ('admin', 'agent'):
            self.grants.pop(user_id, None)
        self.invalidate_user(user_id)

    async def delete_user(self, user_id):
        if not self.users[user_id]['builtin']:
            self.users.pop(user_id)
            self.grants.pop(user_id, None)
            for key in [k for k, s in self.sessions.items() if s['user_id'] == user_id]:
                self.sessions.pop(key)

    async def set_password(self, user_id, password_hash, must_change=False, default=False):
        self.users[user_id].update(password_hash=password_hash, must_change_password=must_change,
                                   password_default=default)
        self.invalidate_user(user_id)

    async def update_profile(self, user_id, display_name=None, email=None):
        if display_name is not None:
            self.users[user_id]['display_name'] = display_name
        if email is not None:
            self.users[user_id]['email'] = email

    async def set_clusters(self, user_id, grants, updated_by):
        self.grants[user_id] = dict(grants)
        self.invalidate_user(user_id)

    async def record_login(self, user_id, ip):
        self.users[user_id]['last_login_ip'] = ip

    # ---- 会话 ----
    async def create_session(self, user_id, remember, ip, user_agent):
        token = secrets.token_urlsafe(32)
        lifetime = timedelta(days=store_module.REMEMBER_DAYS) if remember else timedelta(hours=store_module.SESSION_HOURS)
        now = datetime.now(timezone.utc)
        self.sessions[token_hash(token)] = {
            'token_hash': token_hash(token), 'user_id': user_id, 'created_at': now, 'last_seen_at': now,
            'expires_at': now + lifetime, 'remember': remember,
        }
        return token, lifetime

    async def resolve_session(self, token):
        session = self.sessions.get(token_hash(token or ''))
        if session is None or session['expires_at'] <= datetime.now(timezone.utc):
            return None
        return dict(session)

    async def delete_session(self, key):
        self.sessions.pop(key, None)

    async def delete_user_sessions(self, user_id, keep=None):
        for key in [k for k, s in self.sessions.items() if s['user_id'] == user_id and k != keep]:
            self.sessions.pop(key)

    # ---- 设置 ----
    async def get_setting(self, key, default=None):
        return self.settings.get(key, default)

    async def put_setting(self, key, value, updated_by):
        self.settings[key] = value


def install(monkeypatch, fake):
    """把各模块里引用的 store 单例换成内存版。"""
    from auth import admin_api, authn, handlers, middleware, seed

    for module in (store_module, admin_api, authn, handlers, middleware, seed):
        monkeypatch.setattr(module, 'store', fake)
    return fake
