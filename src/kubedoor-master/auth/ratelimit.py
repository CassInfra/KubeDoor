"""登录失败限流(内存,master 单实例)。

同一账号 + 同一来源 IP 在窗口内连续失败 N 次锁 M 分钟;同一账号不分来源失败 4N 次也锁(防分布式撞库),
上限放宽一些,降低被恶意锁号的影响。N(登录失败锁定次数)、M(锁定时长)在「系统管理 → 通用配置 →
登录与安全」设置,每次用时再取,改了马上生效(已经锁上的按锁定时的时长解锁)。
"""

import time
from collections import deque

import general_settings

USER_FAILURES_FACTOR = 4  # 同一账号不分来源的失败上限 = 单来源上限 × 4
_MAX_KEYS = 20000


class LoginLimiter:
    """参数不传(None)时每次用时取通用配置;测试可以传固定值。窗口默认等于锁定时长。"""

    def __init__(self, max_failures=None, user_max_failures=None, window=None, lock_seconds=None,
                 clock=time.monotonic):
        self._max_failures = max_failures
        self._user_max_failures = user_max_failures
        self._window = window
        self._lock_seconds = lock_seconds
        self.clock = clock
        self._failures = {}  # key -> deque[时间]
        self._locked = {}  # key -> 解锁时间

    @property
    def max_failures(self):
        if self._max_failures is not None:
            return self._max_failures
        return int(general_settings.value('auth_login_max_failures'))

    @property
    def user_max_failures(self):
        if self._user_max_failures is not None:
            return self._user_max_failures
        return self.max_failures * USER_FAILURES_FACTOR

    @property
    def lock_seconds(self):
        if self._lock_seconds is not None:
            return self._lock_seconds
        return int(general_settings.value('auth_lock_minutes')) * 60

    @property
    def window(self):
        return self._window if self._window is not None else self.lock_seconds

    @staticmethod
    def _keys(username, ip):
        name = (username or '').strip().lower()
        return (('u', name), ('ui', name, ip or ''))

    def retry_after(self, username, ip):
        """被锁返回剩余秒数,未锁返回 0。"""
        now = self.clock()
        remaining = 0
        for key in self._keys(username, ip):
            until = self._locked.get(key)
            if until is None:
                continue
            if until <= now:
                self._locked.pop(key, None)
                continue
            remaining = max(remaining, int(until - now) + 1)
        return remaining

    def failure(self, username, ip):
        now = self.clock()
        window, lock_seconds = self.window, self.lock_seconds
        if len(self._failures) > _MAX_KEYS:
            self._prune(now, window)
        for key, limit in zip(self._keys(username, ip), (self.user_max_failures, self.max_failures)):
            items = self._failures.setdefault(key, deque())
            items.append(now)
            while items and items[0] <= now - window:
                items.popleft()
            if len(items) >= limit:
                self._locked[key] = now + lock_seconds
                items.clear()

    def success(self, username, ip):
        """登录成功只清本来源的计数;按账号累计的失败继续保留,防止攻击者夹带一次成功清零。"""
        _, user_ip_key = self._keys(username, ip)
        self._failures.pop(user_ip_key, None)
        self._locked.pop(user_ip_key, None)

    def reset(self, username):
        """管理员重置口令后解锁该账号。"""
        name = (username or '').strip().lower()
        for store in (self._failures, self._locked):
            for key in [k for k in store if k[1] == name]:
                store.pop(key, None)

    def _prune(self, now, window):
        for key in [k for k, items in self._failures.items() if not items or items[-1] <= now - window]:
            self._failures.pop(key, None)
        for key in [k for k, until in self._locked.items() if until <= now]:
            self._locked.pop(key, None)
        while len(self._failures) > _MAX_KEYS:
            self._failures.pop(next(iter(self._failures)))


limiter = LoginLimiter()
