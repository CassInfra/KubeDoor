#!/usr/bin/python3
"""通用配置:向 master 取 kubedoor-alarm 要用的几项(带缓存),取不到时有后备值。

这几项以前是 kubedoor-config 里的环境变量,现在在 master 的「系统管理 → 通用配置」里维护:

    GET {MASTER_INTERNAL_URL}/api/internal/settings
    请求头 X-Kubedoor-Token: <AI_INTERNAL_TOKEN>(不带 X-User-Name,master 认作系统身份)
    返回 {"success": true, "data": {"prom_k8s_tag_key": "origin_prometheus", "kubedoor_exturl": "",
          "alertmanager_exturl": "", "silence_cache_ttl": 15, "retention_ai_checkpoint_days": 7}}

- 用到的地方在处理告警时调 current() / value() 取,不要在 import 时拷贝成模块常量。
- 缓存 CACHE_TTL 秒:在通用配置页保存后,最长这么久生效。过期后只让一个请求去 master 取,
  其他请求先用正在用的配置,不一起等 master。
- master 暂时不可用(或返回的格式不对)时沿用上次取到的配置,之后照样每 CACHE_TTL 秒重试;
  一次都没取到时每一项依次用环境变量旧值(升级后清理 kubedoor-config 之前还在)、默认值。
- master 的返回里没有密码;日志里不带内部令牌。
"""

import logging
import os
import threading
import time

import notify_client

# 在「通用配置」页保存后,最长这么久生效
CACHE_TTL = 15
DEFAULTS = {
    # 集群指标标签 key:告警标签里它的值就是集群名
    'prom_k8s_tag_key': 'origin_prometheus',
    # KubeDoor 访问地址:IM 通知里【屏蔽】链接指向它的告警屏蔽页
    'kubedoor_exturl': '',
    # Alertmanager 外部地址:没有 KubeDoor 访问地址时【屏蔽】链接退回 Alertmanager 页面
    'alertmanager_exturl': '',
    # 告警屏蔽规则缓存秒数:新建 / 解除屏蔽后最长多久生效
    'silence_cache_ttl': 15,
}
# 每一项原来的环境变量
LEGACY_ENV = {
    'prom_k8s_tag_key': 'PROM_K8S_TAG_KEY',
    'kubedoor_exturl': 'KUBEDOOR_EXTURL',
    'alertmanager_exturl': 'ALERTMANAGER_EXTURL',
    'silence_cache_ttl': 'SILENCE_CACHE_TTL',
}
MAX_SILENCE_CACHE_TTL = 3600


def _normalize(key, raw):
    """把 master 返回的值或环境变量的值整理成要用的值;不合法返回 None。

    地址去掉首尾空白和末尾的 /;屏蔽规则缓存秒数是 0~MAX_SILENCE_CACHE_TTL 的整数(环境变量是数字字符串)。
    """
    if key == 'silence_cache_ttl':
        if isinstance(raw, str):
            raw = raw.strip()
            if not (raw.isascii() and raw.isdigit()):
                return None
            raw = int(raw)
        if type(raw) is not int or not 0 <= raw <= MAX_SILENCE_CACHE_TTL:
            return None
        return raw
    if raw is None:
        raw = ''
    if not isinstance(raw, str):
        return None
    if key == 'prom_k8s_tag_key':
        return raw.strip() or None
    return raw.strip().rstrip('/')


def _settings_from(data):
    """检查 master 返回的配置,只留 alarm 用的几项;缺项或不合法都按返回格式不对处理(沿用上次取到的)。"""
    if not isinstance(data, dict):
        raise notify_client.FetchError('master 返回的通用配置格式不对')
    values = {}
    for key in DEFAULTS:
        value = _normalize(key, data[key]) if key in data else None
        if value is None:
            raise notify_client.FetchError(f'master 返回的通用配置里 {key} 缺失或无效')
        values[key] = value
    return values


def fetch_settings(timeout=notify_client.FETCH_TIMEOUT):
    """向 master 取一次通用配置,返回 DEFAULTS 里的几项。

    Raises:
        notify_client.FetchError: 连不上 master、鉴权失败或返回格式不对。
    """
    return _settings_from(notify_client.get_internal('/api/internal/settings', timeout=timeout))


def fallback_values():
    """一次都没从 master 取到时用的配置:每一项先看环境变量旧值(没设或不合法就不用),再用默认值。"""
    values = dict(DEFAULTS)
    for key, name in LEGACY_ENV.items():
        raw = os.environ.get(name, '')
        value = _normalize(key, raw) if raw.strip() else None
        if value is not None:
            values[key] = value
    return values


class SettingsResolver:
    """缓存通用配置。线程安全:Flask 多线程处理请求时共用一个实例。"""

    def __init__(self, ttl=CACHE_TTL, fetch=fetch_settings, clock=time.monotonic):
        self._ttl = ttl
        self._fetch = fetch
        self._clock = clock
        # _lock 保护下面三个字段;_fetching 保证同一时间只有一个请求去 master 取
        self._lock = threading.Lock()
        self._fetching = threading.Lock()
        self._good = None        # 上次从 master 取到的配置;None = 一次都没取到
        self._values = None      # 正在用的配置
        self._checked_at = None  # 上次去 master 取(不论成败)的时间

    def _cached(self):
        """没过期就返回正在用的配置(副本),否则返回 None。调用方持有 _lock。"""
        if self._checked_at is not None and self._clock() - self._checked_at < self._ttl:
            return dict(self._values)
        return None

    def current(self):
        """返回正在用的配置(dict 副本)。不会抛异常:取不到时用上次的或后备值。"""
        with self._lock:
            cached = self._cached()
            stale = self._values
        if cached is not None:
            return cached
        # 过期了,只让一个请求去 master 取:已经有在用的配置时其他请求不等,先用它;
        # 一次都还没有(刚启动)时等这次取完,免得先按默认的集群标签处理了告警
        if not self._fetching.acquire(blocking=stale is None):
            return dict(stale)
        try:
            with self._lock:
                cached = self._cached()  # 等锁期间别的请求已经取过了
            return cached if cached is not None else self._refresh()
        finally:
            self._fetching.release()

    def _refresh(self):
        now = self._clock()
        try:
            values = self._fetch()
        except Exception as exc:  # 取配置的任何异常都不能让告警请求报错,按失败处理
            if isinstance(exc, notify_client.FetchError):
                reason = str(exc)
            else:
                reason = f"{type(exc).__name__}: {' '.join(str(exc).split())[:200]}"
            with self._lock:
                good = self._good
            if good is None:
                values = fallback_values()
                logging.warning(f'【settings】从 master 获取通用配置失败,之前也没有取到过,'
                                f'先用环境变量旧值或默认值: {reason}')
            else:
                values = good
                logging.warning(f'【settings】从 master 获取通用配置失败,沿用上次取到的配置: {reason}')
        else:
            with self._lock:
                self._good = values
        with self._lock:
            # 失败时也记下这次的时间:master 不可用期间每 ttl 秒才重试一次,不让每条告警都等超时
            self._values, self._checked_at = values, now
        return dict(values)


_resolver = SettingsResolver()


def current():
    """正在用的通用配置(dict 副本):prom_k8s_tag_key、kubedoor_exturl、alertmanager_exturl、silence_cache_ttl。"""
    return _resolver.current()


def value(name):
    """某一项的值,见 current()。"""
    return current()[name]
