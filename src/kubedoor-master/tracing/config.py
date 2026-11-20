"""链路追踪配置（约定 §2.1）：存 auth_settings（key=tracing），只有系统管理员能看和改。

- Jaeger 查询地址就是 Jaeger UI 的地址（含 base path）；ui_url 是给人点的地址，空 = 同查询地址
- 请求头的值、ClickHouse 密码用 AI_ENCRYPTION_KEY 加密（同获取代码接口），页面只回显名称 /「已配置」，
  保存时值留空表示沿用；clickhouse.clear_password=true 清除已保存的密码
- PUT 时没传的字段沿用原值（右上角开关只传 enabled）；clickhouse 里没传的子项同样沿用
- 未知键丢弃；不合法抛 ValueError（说明是哪一项）
"""

import copy
import hashlib
import json
import re

from loguru import logger

from auth.crypto import decrypt_secret, encrypt_secret
from auth.store import store
from func_manager import ext_api

from . import fields as field_map
from .common import TracingError, has_control

SETTING_KEY = 'tracing'
TIMEOUT_RANGE = (5, 120)
MAX_URL = 2048
MAX_CUSTOM_FIELDS = 50
MAX_MASK_KEYS = 50
MAX_PASSWORD = 256
JAEGER_EXAMPLE = 'http://10.0.0.1:16686'
CLICKHOUSE_EXAMPLE = 'http://10.0.0.1:8123'
NOT_CONFIGURED = '系统管理员还没有配置链路追踪（系统管理 → 链路追踪）'

CH_DEFAULTS = {
    'enabled': False,
    'url': '',
    'database': 'jaeger',
    'table': 'trace_search',
    'dim_table': 'trace_search_dim',  # 可空 = 没有汇总表
    'username': '',
    'password': '',  # 加密存
    'timeout': 20,  # 单条 SQL 的超时（秒），同时作为 max_execution_time
    'tls_verify': True,
}
DEFAULTS = {
    'enabled': False,
    'url': '',
    'ui_url': '',
    'headers': [],
    'timeout': 30,
    'tls_verify': True,
    'cluster_key': '',
    'fields': field_map.default_fields(),
    'custom_fields': [],
    'mask_keys': [],
    'clickhouse': CH_DEFAULTS,
}
_PLAIN_KEYS = ('enabled', 'url', 'ui_url', 'timeout', 'tls_verify', 'cluster_key', 'fields', 'custom_fields',
               'mask_keys')
_IDENT = re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,63}')
_CLUSTER_KEY = re.compile(r'[A-Za-z0-9_.\-/:@]{1,128}')
_SPACE = re.compile(r'\s')


def merged(stored):
    """存库的配置补上默认值（深拷贝，字段映射缺的项用默认补）"""
    stored = stored if isinstance(stored, dict) else {}
    cfg = copy.deepcopy(DEFAULTS)
    for key in DEFAULTS:
        if key in stored and key != 'clickhouse':
            cfg[key] = copy.deepcopy(stored[key])
    ch = stored.get('clickhouse')
    if isinstance(ch, dict):
        cfg['clickhouse'].update({key: copy.deepcopy(ch[key]) for key in CH_DEFAULTS if key in ch})
    cfg['fields'] = field_map.effective_fields(cfg['fields'])
    if not isinstance(cfg['headers'], list):
        cfg['headers'] = []
    return cfg


def clickhouse_ready(cfg):
    ch = cfg['clickhouse']
    return bool(ch['enabled'] and ch['url'])


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------
def _url(value, label, example):
    text = '' if value is None else str(value).strip()
    text = text.rstrip('/')
    if not text:
        return ''
    if len(text) > MAX_URL or not ext_api.is_http_url(text, MAX_URL) or '?' in text or '#' in text:
        raise ValueError(f'{label}要以 http:// 或 https:// 开头，不能带查询参数和 #，最长 {MAX_URL} 个字符，'
                         f'例如 {example}')
    return text


def _int(value, label, low, high):
    if isinstance(value, bool):
        raise ValueError(f'{label}必须是整数')
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f'{label}必须是整数') from None
    if isinstance(value, float) and value != number:
        raise ValueError(f'{label}必须是整数')
    if not low <= number <= high:
        raise ValueError(f'{label}取 {low}~{high} 秒')
    return number


def attr_name(value, what, max_len=200):
    """属性名：1~max_len 个字符，不能有空白和控制字符"""
    text = value.strip() if isinstance(value, str) else ''
    if not text or len(text) > max_len or _SPACE.search(text) or has_control(text):
        raise ValueError(f'{what}不能为空、不能有空格和换行，最长 {max_len} 个字符')
    return text


def _cluster_key(value):
    text = '' if value is None else str(value).strip()
    if text and not _CLUSTER_KEY.fullmatch(text):
        raise ValueError('集群字段只能用字母、数字和 _ . - / : @，最长 128 个字符，例如 k8sname 或 k8s.cluster.name')
    return text


def _fields(raw, old):
    if not isinstance(raw, dict):
        raise ValueError('字段映射格式不正确')
    result = {key: list(values) for key, values in old.items()}
    for key, names in raw.items():
        if key not in field_map.FIELD_LABELS:
            raise ValueError(f'字段映射里没有 {str(key)[:50]} 这一项')
        label = field_map.FIELD_LABELS[key]
        if not isinstance(names, list):
            raise ValueError(f'字段映射「{label}」要是属性名列表')
        clean = []
        for name in names:
            text = attr_name(name, f'字段映射「{label}」的属性名')
            if text not in clean:
                clean.append(text)
        if len(clean) > field_map.MAX_CANDIDATES:
            raise ValueError(f'字段映射「{label}」最多 {field_map.MAX_CANDIDATES} 个属性名')
        result[key] = clean
    return result


def _custom_fields(items):
    if not isinstance(items, list):
        raise ValueError('业务字段格式不正确')
    if len(items) > MAX_CUSTOM_FIELDS:
        raise ValueError(f'业务字段最多 {MAX_CUSTOM_FIELDS} 个')
    result, seen = [], set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError('业务字段格式不正确')
        key = attr_name(item.get('key'), '业务字段的属性名')
        label = item.get('label').strip() if isinstance(item.get('label'), str) else ''
        if not label or len(label) > 50 or has_control(label):
            raise ValueError(f'业务字段 {key} 的说明不能为空，最长 50 个字符')
        if key in seen:
            raise ValueError(f'业务字段 {key} 重复了')
        seen.add(key)
        result.append({'key': key, 'label': label})
    return result


def _mask_keys(items):
    if not isinstance(items, list):
        raise ValueError('打码字段格式不正确')
    if len(items) > MAX_MASK_KEYS:
        raise ValueError(f'打码字段最多 {MAX_MASK_KEYS} 个')
    result, seen = [], set()
    for item in items:
        text = attr_name(item, '打码字段', 128)
        if text.lower() not in seen:
            seen.add(text.lower())
            result.append(text)
    return result


def _ident(value, label, allow_empty=False):
    text = '' if value is None else str(value).strip()
    if not text and allow_empty:
        return ''
    if not _IDENT.fullmatch(text):
        raise ValueError(f'ClickHouse {label}只能用字母、数字和下划线，不能以数字开头，最长 64 个字符')
    return text


def _clickhouse(raw, old):
    if raw is None:
        return dict(old)
    if not isinstance(raw, dict):
        raise ValueError('ClickHouse 配置格式不正确')

    def pick(key):
        return raw[key] if key in raw else old[key]

    username = pick('username')
    username = '' if username is None else str(username).strip()
    if len(username) > 128 or _SPACE.search(username) or has_control(username):
        raise ValueError('ClickHouse 账号不能有空格，最长 128 个字符')
    password = raw.get('password')
    if raw.get('clear_password') is True:
        password = ''  # 页面上的「清除」：忽略同时传来的 password
    elif password is None or password == '':
        password = old['password']  # 留空沿用
    else:
        if not isinstance(password, str) or len(password) > MAX_PASSWORD or has_control(password):
            raise ValueError(f'ClickHouse 密码不能换行，最长 {MAX_PASSWORD} 个字符')
        password = encrypt_secret(password)
    ch = {
        'enabled': bool(pick('enabled')),
        'url': _url(pick('url'), 'ClickHouse HTTP 地址', CLICKHOUSE_EXAMPLE),
        'database': _ident(pick('database'), '库名'),
        'table': _ident(pick('table'), '搜索表名'),
        'dim_table': _ident(pick('dim_table'), '汇总表名', allow_empty=True),
        'username': username,
        'password': password,
        'timeout': _int(pick('timeout'), 'ClickHouse 超时时间', *TIMEOUT_RANGE),
        'tls_verify': bool(pick('tls_verify')),
    }
    if ch['enabled'] and not ch['url']:
        raise ValueError('启用 ClickHouse 加速时必须填写 HTTP 地址')
    return ch


def normalize_config(raw, existing=None):
    """校验并规范化页面提交的配置，返回存库格式（请求头的值、ClickHouse 密码已加密）；没传的字段沿用原值"""
    if not isinstance(raw, dict):
        raise ValueError('配置格式不正确')
    old = merged(existing)

    def pick(key):
        return raw[key] if key in raw else old[key]

    saved_headers = [item for item in old['headers'] if isinstance(item, dict) and item.get('name')]
    cfg = {
        'enabled': bool(pick('enabled')),
        'url': _url(pick('url'), 'Jaeger 查询地址', JAEGER_EXAMPLE),
        'ui_url': _url(pick('ui_url'), 'Jaeger UI 地址', JAEGER_EXAMPLE),
        'headers': ext_api.normalize_headers(raw.get('headers'), saved_headers),
        'timeout': _int(pick('timeout'), 'Jaeger 请求超时时间', *TIMEOUT_RANGE),
        'tls_verify': bool(pick('tls_verify')),
        'cluster_key': _cluster_key(pick('cluster_key')),
        'fields': _fields(raw['fields'], old['fields']) if 'fields' in raw else old['fields'],
        'custom_fields': _custom_fields(pick('custom_fields')),
        'mask_keys': _mask_keys(pick('mask_keys')),
        'clickhouse': _clickhouse(raw.get('clickhouse'), old['clickhouse']),
    }
    if cfg['enabled'] and not cfg['url']:
        raise ValueError('启用时必须填写 Jaeger 查询地址')
    return cfg


# ---------------------------------------------------------------------------
# 给页面看的配置、审计
# ---------------------------------------------------------------------------
def public_config(stored):
    """请求头只有名称，ClickHouse 密码换成 has_password"""
    cfg = merged(stored)
    data = {key: cfg[key] for key in _PLAIN_KEYS}
    data['headers'] = [{'name': item['name'], 'has_value': bool(item.get('value'))}
                       for item in cfg['headers'] if isinstance(item, dict) and item.get('name')]
    ch = {key: value for key, value in cfg['clickhouse'].items() if key != 'password'}
    ch['has_password'] = bool(cfg['clickhouse']['password'])
    data['clickhouse'] = ch
    return data


def _plain(secret):
    try:
        return decrypt_secret(secret or '')
    except ValueError:
        return None


def _secret_changed(old, new):
    if old == new:
        return False
    old_plain, new_plain = _plain(old), _plain(new)
    return old_plain is None or new_plain is None or old_plain != new_plain


def _headers_changed(old, new):
    if [item.get('name') for item in old] != [item.get('name') for item in new]:
        return True
    return any(_secret_changed(a.get('value'), b.get('value')) for a, b in zip(old, new))


def changed_items(existing, stored):
    """改了哪些项（审计用，只有名称，不含请求头的值和密码）"""
    old, new = merged(existing), merged(stored)
    changed = []
    for key in ('enabled', 'url', 'ui_url'):
        if old[key] != new[key]:
            changed.append(key)
    if _headers_changed(old['headers'], new['headers']):
        changed.append('headers')
    for key in ('timeout', 'tls_verify', 'cluster_key', 'fields', 'custom_fields', 'mask_keys'):
        if old[key] != new[key]:
            changed.append(key)
    for key in CH_DEFAULTS:
        a, b = old['clickhouse'][key], new['clickhouse'][key]
        if (_secret_changed(a, b) if key == 'password' else a != b):
            changed.append(f'clickhouse.{key}')
    return changed


def fingerprint(*parts):
    """缓存键：同一份连接配置（地址、请求头、账号等）得到同一个值"""
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode('utf-8')).hexdigest()[:24]


def jaeger_fingerprint(cfg):
    return fingerprint('jaeger', cfg['url'], cfg['headers'], cfg['tls_verify'])


def clickhouse_fingerprint(cfg):
    ch = cfg['clickhouse']
    return fingerprint('clickhouse', ch['url'], ch['username'], ch['password'], ch['tls_verify'])


async def load_config():
    """读配置（补上默认值）；读库失败抛 TracingError 503"""
    try:
        stored = await store.get_setting(SETTING_KEY, {}) or {}
    except Exception as exc:
        logger.error(f'读取链路追踪配置失败: {exc}')
        raise TracingError(503, '读取链路追踪配置失败，请稍后重试', 'config_unavailable') from exc
    return merged(stored)


def ui_base(cfg):
    return cfg['ui_url'] or cfg['url']


def trace_url(cfg, trace_id):
    base = ui_base(cfg)
    return f'{base}/trace/{trace_id}' if base else ''
