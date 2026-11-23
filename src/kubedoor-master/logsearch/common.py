"""历史日志查询共用的小工具：错误类型、参数校验、时间（北京时间）、长文本截断、结果大小控制、每个连接的并发限制。"""

import asyncio
import json
import math
import re
import time
from datetime import datetime, timedelta, timezone

from tracing.common import BEIJING, TTLCache  # noqa: F401  同一套北京时间和缓存

MAX_WINDOW = 31 * 86400  # 时间范围最长 31 天
DEFAULT_LOOKBACK = '1h'
TIME_FORMAT_HINT = '北京时间 YYYY-MM-DD HH:mm[:ss]'
MAX_DATA_CHARS = 60000  # 整个 data 序列化后的上限（字符）
LINE_CHARS = 2000  # 单行最多保留
TEXT_CHARS = 3000  # 多行（异常栈）整体最多保留
HEAD_LINES = 10
TAIL_LINES = 2
CAUSE_PREFIXES = ('Caused by:', 'Suppressed:')
SHRINK_STEPS = (1000, 300, 100)  # 只剩 1 行还超限时，字符串值依次截到这些长度
_LOOKBACK = re.compile(r'(\d{1,7})([smhd])')
_UNITS = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}
_CONTROL = re.compile(r'[\x00-\x1f\x7f]')
_CONTROL_KEEP_LINES = re.compile(r'[\x00-\x08\x0b-\x1f\x7f]')

_clock = time.time  # 当前时间（秒），测试里替换


def now():
    return _clock()


class LogError(Exception):
    """带 HTTP 状态码和错误码的失败，handler 原样返回（错误码见约定 §4.9）"""

    def __init__(self, status, message, code='invalid_request', data=None):
        super().__init__(message)
        self.status, self.message, self.code, self.data = status, message, code, data


class ResponseTooLarge(LogError):
    """数据源的响应超过读取上限（16MB）：模式归类据此减小样本重试"""

    def __init__(self):
        super().__init__(400, '查询结果超过 16MB：减少返回字段、加条件或缩短时间窗口', 'too_expensive')


def invalid(message):
    return LogError(400, message, 'invalid_request')


def has_control(text):
    return bool(_CONTROL.search(text))


def has_control_multiline(text):
    """多行文本（说明、Markdown）：允许换行和制表符，不允许其他控制字符"""
    return bool(_CONTROL_KEEP_LINES.search(text))


# ---------------------------------------------------------------------------
# 参数
# ---------------------------------------------------------------------------
def check_keys(body, allowed, hints=None):
    """未声明的参数报错；常见误用给提示"""
    if not isinstance(body, dict):
        raise invalid('参数要是 JSON 对象')
    unknown = sorted(str(key) for key in body if key not in allowed)
    if unknown:
        tips = [f'{key} 请用 {hints[key]}' for key in unknown if hints and key in hints]
        raise invalid(f'不支持的参数：{"、".join(unknown[:10])}' + (f'（{"；".join(tips)}）' if tips else ''))


def text_param(body, key, max_len, required=False):
    value = body.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise invalid(f'{key} 必填')
        return ''
    if not isinstance(value, str):
        raise invalid(f'{key} 要是字符串')
    text = value.strip()
    if len(text) > max_len or has_control(text):
        raise invalid(f'{key} 不能换行，最长 {max_len} 个字符')
    return text


def int_param(body, key, low, high, default):
    value = body.get(key)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        if isinstance(value, str) and value.strip().isdigit():
            value = int(value.strip())
        elif isinstance(value, float) and value.is_integer():
            value = int(value)
        else:
            raise invalid(f'{key} 要是 {low}~{high} 的整数')
    if not low <= value <= high:
        raise invalid(f'{key} 要是 {low}~{high} 的整数')
    return value


def list_param(body, key, max_items):
    value = body.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise invalid(f'{key} 要是数组')
    if len(value) > max_items:
        raise invalid(f'{key} 最多 {max_items} 项')
    return value


# ---------------------------------------------------------------------------
# 时间
# ---------------------------------------------------------------------------
def parse_lookback(value, name='lookback', max_seconds=MAX_WINDOW):
    """15m / 1h / 2d 这类时长 → 秒"""
    text = value.strip().lower() if isinstance(value, str) else ''
    match = _LOOKBACK.fullmatch(text)
    if not match:
        raise invalid(f'{name} 的格式是数字加单位 s / m / h / d，例如 15m、1h、2d')
    seconds = int(match.group(1)) * _UNITS[match.group(2)]
    if seconds <= 0:
        raise invalid(f'{name} 要大于 0')
    if seconds > max_seconds:
        raise invalid(f'{name} 最长 {max_seconds // 86400} 天')
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


def given(value):
    return value is not None and value != ''


def resolve_window(params, default_lookback=DEFAULT_LOOKBACK, max_seconds=MAX_WINDOW):
    """start / end / lookback → (开始秒, 结束秒)：没给 start 时用 lookback，end 默认现在"""
    end = parse_time(params['end'], 'end') if given(params.get('end')) else now()
    if given(params.get('start')):
        start = parse_time(params['start'], 'start')
    else:
        lookback = params.get('lookback')
        start = end - parse_lookback(lookback if given(lookback) else default_lookback, max_seconds=max_seconds)
    if start >= end:
        raise invalid('开始时间要早于结束时间')
    if end - start > max_seconds + 1e-6:
        raise invalid(f'时间范围最长 {max_seconds // 86400} 天')
    return start, end


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def iso(seconds):
    """秒 → 北京时间 ISO（毫秒），例 2026-11-23T15:18:49.366+08:00。
    日志里可能有离谱的时间（负数、几千年后）：Windows 上 datetime.fromtimestamp 对负数会抛 OSError，
    这里用 1970 年加时间差，超出 datetime 范围的原样返回数字"""
    try:
        return (_EPOCH + timedelta(seconds=seconds)).astimezone(BEIJING).isoformat(timespec='milliseconds')
    except (OverflowError, ValueError, OSError, TypeError):
        return str(seconds)


def iso_ms(millis):
    return iso(millis / 1000)


def window_text(start, end):
    return {'from': iso(start), 'to': iso(end)}


def to_ms(seconds):
    return int(math.floor(seconds * 1000))


# ---------------------------------------------------------------------------
# 数值
# ---------------------------------------------------------------------------
def to_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError, OverflowError):
            return default


def to_float(value, default=None):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def tidy_number(value, digits=1):
    """统计值：整数原样，小数保留 digits 位；NaN / 空 → None"""
    number = to_float(value)
    if number is None:
        return None
    if number.is_integer() and abs(number) < 2 ** 63:
        return int(number)
    return round(number, digits)


# ---------------------------------------------------------------------------
# 长文本截断（约定 §4.9）
# ---------------------------------------------------------------------------
def _clip_line(line, limit):
    if len(line) <= limit:
        return line
    return f'{line[:limit]}…（省略 {len(line) - limit} 字）'


def clip_text(text, line_chars=LINE_CHARS, max_chars=TEXT_CHARS):
    """单行超过 line_chars 截断；多行（异常栈）保留前 10 行、每个 Caused by: / Suppressed: 行及其后 2 行、
    最后 2 行，中间写「…（省略 N 行）」，总长再截到 max_chars"""
    if not isinstance(text, str):
        return text
    if '\n' not in text:
        return _clip_line(text, line_chars)
    lines = text.split('\n')
    total = len(lines)
    keep = set(range(min(HEAD_LINES, total))) | set(range(max(0, total - TAIL_LINES), total))
    for index, line in enumerate(lines):
        if line.lstrip().startswith(CAUSE_PREFIXES):
            keep.update(range(index, min(index + 3, total)))
    out, skipped = [], 0
    for index, line in enumerate(lines):
        if index in keep:
            if skipped:
                out.append(f'…（省略 {skipped} 行）')
                skipped = 0
            out.append(_clip_line(line, line_chars))
        else:
            skipped += 1
    result = '\n'.join(out)
    if len(result) > max_chars:
        result = f'{result[:max_chars]}…（省略 {len(result) - max_chars} 字）'
    return result


def clip_value(value, line_chars=LINE_CHARS, max_chars=TEXT_CHARS):
    """字符串截断；数组、对象里的字符串同样"""
    if isinstance(value, str):
        return clip_text(value, line_chars, max_chars)
    if isinstance(value, list):
        return [clip_value(item, line_chars, max_chars) for item in value]
    if isinstance(value, dict):
        return {key: clip_value(item, line_chars, max_chars) for key, item in value.items()}
    return value


def first_line(text, limit=200):
    line = (text or '').strip().split('\n', 1)[0].strip() if isinstance(text, str) else ''
    return line if len(line) <= limit else line[:limit - 1] + '…'


# ---------------------------------------------------------------------------
# 结果大小（约定 §4.9：整个 data 序列化后 ≤ 60000 字）
# ---------------------------------------------------------------------------
def dumps(data):
    return json.dumps(data, ensure_ascii=False, default=str)


def size_of(data):
    return len(dumps(data))


def _shrink_strings(value, limit):
    if isinstance(value, str):
        return value if len(value) <= limit else f'{value[:limit]}…（省略 {len(value) - limit} 字）'
    if isinstance(value, list):
        return [_shrink_strings(item, limit) for item in value]
    if isinstance(value, dict):
        return {key: _shrink_strings(item, limit) for key, item in value.items()}
    return value


def fit_rows(data, key='rows', hint='', limit=MAX_DATA_CHARS, count_key='returned'):
    """data[key] 是行列表：整个 data 超过 limit 字时从尾部丢行（至少留 1 行），标 truncated 并写 hint；
    只剩 1 行还超限时把字符串值截短。返回 data（原地修改）"""
    if size_of(data) <= limit:
        return data
    rows = data.get(key) or []
    data['truncated'] = True
    if hint:
        data['hint'] = hint
    low, high = 1, len(rows)
    while low < high:  # 能放下的最多行数（二分）
        middle = (low + high + 1) // 2
        data[key] = rows[:middle]
        if size_of(data) <= limit:
            low = middle
        else:
            high = middle - 1
    data[key] = rows[:max(low, 1)] if rows else rows
    for step in SHRINK_STEPS:
        if size_of(data) <= limit:
            break
        data[key] = _shrink_strings(data[key], step)
    if count_key in data:
        data[count_key] = len(data[key])
    return data


# ---------------------------------------------------------------------------
# 每个连接的并发限制（连接配置变了或 max_concurrency 变了重建）
# ---------------------------------------------------------------------------
_limiters = {}  # (连接 id, 指纹) → (event loop, 并发数, Semaphore)


def limiter(conn, fingerprint):
    loop = asyncio.get_running_loop()
    key = (conn.get('id', ''), fingerprint)
    size = max(1, int(conn.get('max_concurrency') or 1))
    item = _limiters.get(key)
    if item is None or item[0] is not loop or item[1] != size:
        if len(_limiters) > 200:
            _limiters.clear()
        item = (loop, size, asyncio.Semaphore(size))
        _limiters[key] = item
    return item[2]


def reset_limiters():
    _limiters.clear()
