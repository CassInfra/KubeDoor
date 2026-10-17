"""登录失败限流(内存,master 单实例)。

同一账号 + 同一来源 IP 在窗口内连续失败 MAX_FAILURES 次锁 LOCK_SECONDS;
同一账号不分来源失败 USER_MAX_FAILURES 次也锁(防分布式撞库),上限放宽一些,降低被恶意锁号的影响。
"""

import os
import time
from collections import deque

MAX_FAILURES = int(os.environ.get('AUTH_LOGIN_MAX_FAILURES', '5'))
LOCK_SECONDS = int(os.environ.get('AUTH_LOCK_MINUTES', '15')) * 60
USER_MAX_FAILURES = MAX_FAILURES * 4
WINDOW_SECONDS = LOCK_SECONDS
_MAX_KEYS = 20000


class LoginLimiter:
    def __init__(self, max_failures=MAX_FAILURES, user_max_failures=USER_MAX_FAILURES,
                 window=WINDOW_SECONDS, lock_seconds=LOCK_SECONDS, clock=time.monotonic):
        self.max_failures = max_failures
        self.user_max_failures = user_max_failures
        self.window = window
        self.lock_seconds = lock_seconds
        self.clock = clock
        self._failures = {}  # key -> deque[时间]
        self._locked = {}  # key -> 解锁时间

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
        if len(self._failures) > _MAX_KEYS:
            self._prune(now)
        for key, limit in zip(self._keys(username, ip), (self.user_max_failures, self.max_failures)):
            items = self._failures.setdefault(key, deque())
            items.append(now)
            while items and items[0] <= now - self.window:
                items.popleft()
            if len(items) >= limit:
                self._locked[key] = now + self.lock_seconds
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

    def _prune(self, now):
        for key in [k for k, items in self._failures.items() if not items or items[-1] <= now - self.window]:
            self._failures.pop(key, None)
        for key in [k for k, until in self._locked.items() if until <= now]:
            self._locked.pop(key, None)
        while len(self._failures) > _MAX_KEYS:
            self._failures.pop(next(iter(self._failures)))


limiter = LoginLimiter()
