"""链路追踪共用的小工具：错误类型、时间解析和格式化（北京时间）、带过期的 LRU 缓存。"""

import math
import re
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

BEIJING = timezone(timedelta(hours=8))  # 北京时间没有夏令时，固定 +08:00（不依赖系统时区数据）
MAX_WINDOW = 7 * 86400  # 时间范围最长 7 天（ClickHouse 加速表保留 10 天）
DEFAULT_LOOKBACK = '1h'
TIME_FORMAT_HINT = '北京时间 YYYY-MM-DD HH:mm[:ss]'
_LOOKBACK = re.compile(r'(\d{1,7})([smhd])')
_UNITS = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}
_CONTROL = re.compile(r'[\x00-\x1f\x7f]')

_clock = time.time  # 当前时间（秒），测试里替换


def now():
    return _clock()


class TracingError(Exception):
    """带 HTTP 状态码和错误码的失败，handler 原样返回（错误码见约定 §4）"""

    def __init__(self, status, message, code='invalid_request', data=None):
        super().__init__(message)
        self.status, self.message, self.code, self.data = status, message, code, data


class ResponseTooLarge(TracingError):
    """响应超过读取上限（扫描模式据此停下，不算失败）"""


def invalid(message):
    return TracingError(400, message, 'invalid_request')


def has_control(text):
    return bool(_CONTROL.search(text))


# ---------------------------------------------------------------------------
# 时间
# ---------------------------------------------------------------------------
def parse_lookback(value, name='lookback'):
    """15m / 1h / 2d 这类时长 → 秒"""
    text = value.strip().lower() if isinstance(value, str) else ''
    match = _LOOKBACK.fullmatch(text)
    if not match:
        raise invalid(f'{name} 的格式是数字加单位 s / m / h / d，例如 15m、1h、2d')
    seconds = int(match.group(1)) * _UNITS[match.group(2)]
    if seconds <= 0:
        raise invalid(f'{name} 要大于 0')
    if seconds > MAX_WINDOW:
        raise invalid(f'{name} 最长 7 天')
    return seconds


def parse_time(value, name):
    """北京时间 YYYY-MM-DD HH:mm[:ss]（不带时区按北京时间），带时区的 ISO 时间也认 → 秒（浮点）"""
    if not isinstance(value, str) or not value.strip() or len(value) > 64 or has_control(value):
        raise invalid(f'{name} 要写{TIME_FORMAT_HINT}')
    text = value.strip().replace('/', '-')
    if text.endswith(('Z', 'z')):
        text = text[:-1] + '+00:00'
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise invalid(f'{name} 要写{TIME_FORMAT_HINT}，收到的是 {value.strip()[:40]}') from None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=BEIJING)
    return moment.timestamp()


def _given(value):
    return value is not None and value != ''


def resolve_window(params, default_lookback=DEFAULT_LOOKBACK, max_seconds=MAX_WINDOW):
    """start / end / lookback → (开始秒, 结束秒)：没给 start 时用 lookback（默认 1h），end 默认现在"""
    end = parse_time(params['end'], 'end') if _given(params.get('end')) else now()
    if _given(params.get('start')):
        start = parse_time(params['start'], 'start')
    else:
        lookback = params.get('lookback')
        start = end - parse_lookback(lookback if _given(lookback) else default_lookback)
    if start >= end:
        raise invalid('开始时间要早于结束时间')
    if end - start > max_seconds + 1e-6:
        raise invalid(f'时间范围最长 {max_seconds // 86400} 天')
    return start, end


def iso(seconds):
    """秒 → 北京时间 ISO（毫秒），例 2026-11-21T03:24:05.123+08:00"""
    return datetime.fromtimestamp(seconds, BEIJING).isoformat(timespec='milliseconds')


def iso_ns(nanos):
    return iso(nanos / 1e9)


def iso_ms(millis):
    return iso(millis / 1000)


def rfc3339(seconds):
    """给 Jaeger 的时间参数：UTC、整秒"""
    return datetime.fromtimestamp(int(seconds), timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def floor_s(seconds):
    return int(math.floor(seconds))


def ceil_s(seconds):
    return int(math.ceil(seconds))


def window_text(start, end):
    return {'from': iso(start), 'to': iso(end)}


def ms1(value):
    """数值保留 1 位小数"""
    return round(float(value), 1)


# ---------------------------------------------------------------------------
# 缓存
# ---------------------------------------------------------------------------
class TTLCache:
    """LRU + 过期时间；max_bytes 不为 0 时按调用方给的估算大小限制总量"""

    def __init__(self, max_items, ttl, max_bytes=0, clock=None):
        self.max_items, self.ttl, self.max_bytes = max_items, ttl, max_bytes
        self._clock = clock or time.monotonic
        self._data = OrderedDict()  # key -> (value, size, expires)
        self._bytes = 0

    def get(self, key):
        item = self._data.get(key)
        if item is None:
            return None
        if item[2] <= self._clock():
            self._drop(key)
            return None
        self._data.move_to_end(key)
        return item[0]

    def put(self, key, value, size=0):
        if key in self._data:
            self._drop(key)
        if self.max_bytes and size > self.max_bytes:
            return
        self._data[key] = (value, size, self._clock() + self.ttl)
        self._bytes += size
        while len(self._data) > self.max_items or (self.max_bytes and self._bytes > self.max_bytes):
            self._drop(next(iter(self._data)))

    def _drop(self, key):
        _, size, _ = self._data.pop(key)
        self._bytes -= size

    def clear(self):
        self._data.clear()
        self._bytes = 0

    def __len__(self):
        return len(self._data)
