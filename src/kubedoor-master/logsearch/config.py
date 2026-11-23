"""历史日志配置（约定 §2）：存 auth_settings（key=log_sources），只有系统管理员能看和改。

- 顶层键 enabled / architecture / connections / correlation_keys / sources / clusters：传了的整体替换，没传的沿用
  （右上角开关只传 enabled）
- 连接的密码、API Key、请求头的值用 AI_ENCRYPTION_KEY 加密（同链路追踪）。页面提交明文 = 新填，空 = 沿用同 id
  已保存的；clear_password / clear_api_key 为 true 时清除；请求头的值留空按名称沿用；auth_type=none 时清掉账号和密钥
- 交叉引用（日志源的连接、绑定的日志源和连接、关联字段）在整份配置上检查，不合法抛 ValueError（说明是哪一项）
- 未知键丢弃
"""

import copy
import hashlib
import json
import re

from loguru import logger

from auth.crypto import encrypt_secret
from auth.store import store
from func_manager import ext_api

from .common import LogError, has_control, has_control_multiline

SETTING_KEY = 'log_sources'
NOT_CONFIGURED = '系统管理员还没有配置历史日志（系统管理 → 历史日志）'
MAX_CONFIG_CHARS = 1024 * 1024
MAX_URL = 2048
MAX_PASSWORD = 256
MAX_API_KEY = 1024
MAX_CONNECTIONS = 20
MAX_KEYS = 50
MAX_SOURCES = 200
MAX_CLUSTERS = 500
MAX_BINDINGS = 50
TIMEOUT_RANGE = (5, 120)
CONCURRENCY_RANGE = (1, 20)
MAX_ROWS_TO_READ = 10 ** 13
CLICKHOUSE_EXAMPLE = 'http://10.0.0.1:8123'
ELASTIC_EXAMPLE = 'http://10.0.0.1:9200'

CONN_TYPES = ('clickhouse', 'elasticsearch')
ES_AUTH_TYPES = ('none', 'basic', 'api_key')
KINDS = ('access', 'app', 'other')
DURATION_UNITS = ('ms', 's', 'us', 'ns')
LEVELS = ('error', 'warn', 'info', 'debug')
BINDING_OPS = ('eq', 'ne', 'in', 'not_in', 'prefix')
SEMANTIC_FIELDS = (
    ('message', '正文'), ('level', '级别'), ('namespace', '命名空间'), ('pod', 'Pod'), ('service', '服务 / 应用'),
    ('status', '状态码'), ('duration', '耗时'), ('method', '请求方法'), ('path', '路径'), ('host', '域名 / Host'),
    ('client_ip', '客户端 IP'),
)
SEMANTIC_KEYS = tuple(key for key, _ in SEMANTIC_FIELDS)
SEMANTIC_LABELS = dict(SEMANTIC_FIELDS)

CONNECTION_DEFAULTS = {
    'id': '', 'name': '', 'type': 'clickhouse', 'url': '', 'auth_type': 'basic', 'username': '',
    'password': '',  # 加密存
    'api_key': '',  # 加密存
    'headers': [],  # 值加密存
    'timeout': 30, 'tls_verify': True, 'max_concurrency': 4, 'max_rows_to_read': 1_000_000_000, 'allow_raw': True,
}
SOURCE_DEFAULTS = {
    'id': '', 'name': '', 'enabled': True, 'connection': '', 'kind': 'other', 'description': '', 'time_field': '',
    'fields': {}, 'duration_unit': 'ms', 'level_values': {}, 'correlations': [], 'columns': [], 'default_fields': [],
    'mask_fields': [], 'mask_patterns': [], 'default_target': '', 'cluster_field': '',
}
DEFAULTS = {'enabled': False, 'architecture': '', 'connections': [], 'correlation_keys': [], 'sources': [],
            'clusters': []}
SECRET_KEYS = ('password', 'api_key')

_ID = re.compile(r'[a-z][a-z0-9_-]{0,31}')
_KEY = re.compile(r'[a-z][a-z0-9_]{0,31}')
FIELD_NAME = re.compile(r'[A-Za-z_@][A-Za-z0-9_.@-]{0,199}')
_CH_SEGMENT = re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,191}')
_ES_TARGET = re.compile(r'-?[a-z0-9*][a-z0-9_.*+-]{0,254}')
_SPACE = re.compile(r'\s')


def merged(stored):
    """存库的配置补上默认值（深拷贝；连接、日志源缺的键用默认补）"""
    stored = stored if isinstance(stored, dict) else {}
    cfg = copy.deepcopy(DEFAULTS)
    for key in DEFAULTS:
        if key in stored:
            cfg[key] = copy.deepcopy(stored[key])
    for key in ('connections', 'correlation_keys', 'sources', 'clusters'):
        if not isinstance(cfg[key], list):
            cfg[key] = []
    cfg['connections'] = [{**copy.deepcopy(CONNECTION_DEFAULTS), **item} for item in cfg['connections']
                          if isinstance(item, dict)]
    cfg['sources'] = [{**copy.deepcopy(SOURCE_DEFAULTS), **item} for item in cfg['sources'] if isinstance(item, dict)]
    cfg['clusters'] = [{'env': '', 'note': '', 'bindings': [], **item} for item in cfg['clusters']
                       if isinstance(item, dict)]
    return cfg


# ---------------------------------------------------------------------------
# 基础校验
# ---------------------------------------------------------------------------
def _text(value, label, max_len, required=False, multiline=False):
    text = value.strip() if isinstance(value, str) else ('' if value is None else None)
    if text is None:
        raise ValueError(f'{label}要是文字')
    if required and not text:
        raise ValueError(f'{label}不能为空')
    if len(text) > max_len:
        raise ValueError(f'{label}最长 {max_len} 个字符')
    if (has_control_multiline(text) if multiline else has_control(text)):
        raise ValueError(f'{label}不能有{"控制字符" if multiline else "换行和控制字符"}')
    return text


def _id(value, label, pattern=_ID, max_len=32):
    text = value.strip() if isinstance(value, str) else ''
    if not pattern.fullmatch(text):
        what = '小写字母开头，只能用小写字母、数字、_' + ('' if pattern is _KEY else ' 和 -')
        raise ValueError(f'{label}要{what}，最长 {max_len} 个字符（收到 {str(value)[:40] or "空"}）')
    return text


def field_name(value, label, allow_empty=False):
    text = value.strip() if isinstance(value, str) else ('' if value is None else None)
    if text == '' and allow_empty:
        return ''
    if text is None or not FIELD_NAME.fullmatch(text):
        raise ValueError(f'{label}不合法：{str(value)[:60] or "空"}（字段名只能用字母、数字和 _ . @ -，'
                         '以字母、_ 或 @ 开头，最长 200 个字符）')
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
        raise ValueError(f'{label}取 {low}~{high}')
    return number


def _bool(value, label):
    if not isinstance(value, bool):
        raise ValueError(f'{label}要是 true 或 false')
    return value


def _url(value, label, example):
    text = '' if value is None else str(value).strip().rstrip('/')
    if not text:
        raise ValueError(f'{label}不能为空，例如 {example}')
    if len(text) > MAX_URL or not ext_api.is_http_url(text, MAX_URL) or '?' in text or '#' in text:
        raise ValueError(f'{label}要以 http:// 或 https:// 开头，不能带查询参数和 #，最长 {MAX_URL} 个字符，'
                         f'例如 {example}')
    return text


def _name_list(items, label, max_items, check):
    if items is None:
        return []
    if not isinstance(items, list):
        raise ValueError(f'{label}要是列表')
    if len(items) > max_items:
        raise ValueError(f'{label}最多 {max_items} 项')
    result = []
    for item in items:
        text = check(item)
        if text not in result:
            result.append(text)
    return result


def compile_pattern(pattern, label):
    try:
        return re.compile(pattern)
    except (re.error, RecursionError, OverflowError) as exc:
        raise ValueError(f'{label}不是合法的正则表达式（{exc}）') from None


def ch_target(value, label):
    """ClickHouse：库.表，各段 [A-Za-z_][A-Za-z0-9_]{0,191}，库名 ≤ 64"""
    text = value.strip() if isinstance(value, str) else ''
    parts = text.split('.')
    if len(parts) != 2 or not all(_CH_SEGMENT.fullmatch(part) for part in parts) or len(parts[0]) > 64:
        raise ValueError(f'{label}要写成 库.表（只能用字母、数字和下划线，不能以数字开头），例如 logs.ingress_access'
                         f'（收到 {text[:80] or "空"}）')
    return text


def es_target(value, label):
    """Elasticsearch：逗号分隔 ≤ 20 项，每项小写的索引名 / 别名 / 通配符，可以用 - 开头表示排除；不能跨集群（:）"""
    text = value.strip() if isinstance(value, str) else ''
    items = [item.strip() for item in text.split(',')]
    if not text or len(items) > 20 or not all(_ES_TARGET.fullmatch(item) for item in items):
        raise ValueError(f'{label}要写索引名、别名或通配符（小写，逗号分隔最多 20 项，不能以 . 开头，不能带 :），'
                         f'例如 app-logs-*（收到 {text[:80] or "空"}）')
    return ','.join(items)


def check_target(conn_type, value, label):
    return ch_target(value, label) if conn_type == 'clickhouse' else es_target(value, label)


# ---------------------------------------------------------------------------
# 连接
# ---------------------------------------------------------------------------
def _secret(item, key, clear_key, saved, max_len, label):
    if item.get(clear_key) is True:
        return ''  # 页面上的「清除」：忽略同时传来的值
    value = item.get(key)
    if value is None or value == '':
        return saved or ''  # 留空沿用
    if not isinstance(value, str) or len(value) > max_len or has_control(value):
        raise ValueError(f'{label}不能换行，最长 {max_len} 个字符')
    return encrypt_secret(value)


def _connection(item, saved_by_id, index):
    if not isinstance(item, dict):
        raise ValueError(f'第 {index + 1} 个连接格式不正确')
    cid = _id(item.get('id'), f'第 {index + 1} 个连接的 ID')
    label = f'连接 {cid}'
    saved = saved_by_id.get(cid) or {}

    def pick(key):
        if key in item:
            return item[key]
        return copy.deepcopy(saved.get(key, CONNECTION_DEFAULTS[key]))

    ctype = pick('type')
    if ctype not in CONN_TYPES:
        raise ValueError(f'{label}：类型只能是 clickhouse 或 elasticsearch')
    example = CLICKHOUSE_EXAMPLE if ctype == 'clickhouse' else ELASTIC_EXAMPLE
    if ctype == 'clickhouse':
        auth_type = 'basic'  # ClickHouse 固定账号密码（账号可以留空 = default）
    else:
        auth_type = item.get('auth_type') if 'auth_type' in item else saved.get('auth_type', 'none')
        if auth_type not in ES_AUTH_TYPES:
            raise ValueError(f'{label}：认证方式只能是 none / basic / api_key')
    username = pick('username')
    username = '' if username is None else str(username).strip()
    if len(username) > 128 or _SPACE.search(username) or has_control(username):
        raise ValueError(f'{label}：账号不能有空格，最长 128 个字符')
    password = _secret(item, 'password', 'clear_password', saved.get('password'), MAX_PASSWORD, f'{label}的密码')
    api_key = _secret(item, 'api_key', 'clear_api_key', saved.get('api_key'), MAX_API_KEY, f'{label}的 API Key')
    if auth_type == 'none':
        username = password = api_key = ''
    elif ctype == 'elasticsearch' and auth_type == 'basic' and not username:
        raise ValueError(f'{label}：认证方式是账号密码时要填写账号')
    elif auth_type == 'api_key' and not api_key:
        raise ValueError(f'{label}：认证方式是 API Key 时要填写 API Key')
    saved_headers = [header for header in saved.get('headers') or [] if isinstance(header, dict) and header.get('name')]
    try:
        headers = ext_api.normalize_headers(item.get('headers'), saved_headers)
    except ValueError as exc:
        raise ValueError(f'{label}：{exc}') from None
    return {
        'id': cid,
        'name': _text(pick('name'), f'{label}的名称', 50, required=True),
        'type': ctype,
        'url': _url(pick('url'), f'{label}的地址', example),
        'auth_type': auth_type,
        'username': username,
        'password': password,
        'api_key': api_key,
        'headers': headers,
        'timeout': _int(pick('timeout'), f'{label}的超时时间（秒）', *TIMEOUT_RANGE),
        'tls_verify': _bool(pick('tls_verify'), f'{label}的「校验 HTTPS 证书」'),
        'max_concurrency': _int(pick('max_concurrency'), f'{label}的并发数', *CONCURRENCY_RANGE),
        'max_rows_to_read': _int(pick('max_rows_to_read'), f'{label}的单次查询读取行数上限', 0, MAX_ROWS_TO_READ),
        'allow_raw': _bool(pick('allow_raw'), f'{label}的「允许原生查询」'),
    }


def _connections(items, saved):
    if not isinstance(items, list):
        raise ValueError('连接要是列表')
    if len(items) > MAX_CONNECTIONS:
        raise ValueError(f'连接最多 {MAX_CONNECTIONS} 个')
    saved_by_id = {item.get('id'): item for item in saved if isinstance(item, dict)}
    result, seen = [], set()
    for index, item in enumerate(items):
        conn = _connection(item, saved_by_id, index)
        if conn['id'] in seen:
            raise ValueError(f'连接 ID {conn["id"]} 重复了')
        seen.add(conn['id'])
        result.append(conn)
    return result


# ---------------------------------------------------------------------------
# 关联字段
# ---------------------------------------------------------------------------
def _key_pattern(value, label):
    text = _text(value, label, 200)
    if not text:
        return ''
    if text.startswith('^') or (text.endswith('$') and not text.endswith('\\$')):
        raise ValueError(f'{label}不要带 ^ 和 $（取值要完整匹配，系统会自动加上）')
    compiled = compile_pattern(text, label)
    if compiled.fullmatch(''):
        raise ValueError(f'{label}不能匹配空字符串')
    return text


def _correlation_keys(items):
    if not isinstance(items, list):
        raise ValueError('关联字段要是列表')
    if len(items) > MAX_KEYS:
        raise ValueError(f'关联字段最多 {MAX_KEYS} 个')
    result, seen, traces = [], set(), []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f'第 {index + 1} 个关联字段格式不正确')
        key = _id(item.get('key'), f'第 {index + 1} 个关联字段的 key', _KEY)
        label = f'关联字段 {key}'
        if key in seen:
            raise ValueError(f'关联字段 {key} 重复了')
        seen.add(key)
        trace = item.get('trace', False)
        entry = {
            'key': key,
            'label': _text(item.get('label'), f'{label}的名称', 30, required=True),
            'description': _text(item.get('description'), f'{label}的说明', 500, multiline=True),
            'pattern': _key_pattern(item.get('pattern'), f'{label}的取值格式'),
            'trace': _bool(trace, f'{label}的「链路 ID」'),
        }
        if entry['trace']:
            traces.append(key)
        result.append(entry)
    if len(traces) > 1:
        raise ValueError(f'只能有一个关联字段是链路 ID（现在是 {"、".join(traces)}）')
    return result


# ---------------------------------------------------------------------------
# 日志源
# ---------------------------------------------------------------------------
def _semantic_fields(raw, label):
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f'{label}的语义字段格式不正确')
    result = {}
    for key, value in raw.items():
        if key not in SEMANTIC_LABELS:
            raise ValueError(f'{label}的语义字段里没有 {str(key)[:40]} 这一项')
        name = field_name(value, f'{label}的语义字段「{SEMANTIC_LABELS[key]}」', allow_empty=True)
        if name:
            result[key] = name
    return {key: result[key] for key in SEMANTIC_KEYS if key in result}


def _level_values(raw, label):
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f'{label}的级别取值格式不正确')
    result = {}
    for level, values in raw.items():
        if level not in LEVELS:
            raise ValueError(f'{label}的级别取值只能是 error / warn / info / debug')
        if values is None:
            continue
        if not isinstance(values, list) or len(values) > 20:
            raise ValueError(f'{label}的级别取值「{level}」要是列表，最多 20 个')
        clean = []
        for value in values:
            if isinstance(value, bool) or not isinstance(value, (str, int)):
                raise ValueError(f'{label}的级别取值「{level}」只能是文字或整数')
            text = _text(str(value), f'{label}的级别取值「{level}」', 50, required=True)
            if text not in clean:
                clean.append(text)
        if clean:
            result[level] = clean
    return {level: result[level] for level in LEVELS if level in result}


def _correlations(items, label):
    if items is None:
        return []
    if not isinstance(items, list) or len(items) > 10:
        raise ValueError(f'{label}的关联字段要是列表，最多 10 个')
    result, seen = [], set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError(f'{label}的关联字段格式不正确')
        key = _id(item.get('key'), f'{label}的关联字段 key', _KEY)
        where = f'{label}的关联字段 {key}'
        if key in seen:
            raise ValueError(f'{where}登记了两次')
        seen.add(key)
        has_field = item.get('field') not in (None, '')
        has_text = item.get('text') not in (None, '')
        if has_field == has_text:
            raise ValueError(f'{where}：「列」和「正文写法」二选一')
        if has_field:
            result.append({'key': key, 'field': field_name(item.get('field'), f'{where}的列')})
            continue
        text = item.get('text')
        if not isinstance(text, str) or len(text) > 100 or has_control(text):
            raise ValueError(f'{where}：正文写法最长 100 个字符，不能换行')
        if text.count('{value}') != 1:
            raise ValueError(f'{where}：正文写法要包含一个 {{value}}')
        if not text.replace('{value}', '').strip():
            raise ValueError(f'{where}：正文写法除了 {{value}} 还要有固定的文字，例如 TID:{{value}}')
        result.append({'key': key, 'text': text})
    return result


def _columns(items, label):
    if items is None:
        return []
    if not isinstance(items, list) or len(items) > 300:
        raise ValueError(f'{label}的字段说明要是列表，最多 300 项')
    result, seen = [], set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError(f'{label}的字段说明格式不正确')
        name = field_name(item.get('name'), f'{label}的字段说明里的字段名')
        if name in seen:
            raise ValueError(f'{label}的字段说明里 {name} 重复了')
        seen.add(name)
        result.append({'name': name,
                       'description': _text(item.get('description'), f'{label}的字段 {name} 的说明', 300)})
    return result


def _mask_patterns(items, label):
    if items is None:
        return []
    if not isinstance(items, list) or len(items) > 20:
        raise ValueError(f'{label}的打码正则要是列表，最多 20 个')
    result = []
    for item in items:
        text = _text(item, f'{label}的打码正则', 200, required=True)
        compile_pattern(text, f'{label}的打码正则「{text[:40]}」')
        if text not in result:
            result.append(text)
    return result


def _source(item, index):
    if not isinstance(item, dict):
        raise ValueError(f'第 {index + 1} 个日志源格式不正确')
    sid = _id(item.get('id'), f'第 {index + 1} 个日志源的 ID')
    label = f'日志源 {sid}'
    kind = item.get('kind') or 'other'
    if kind not in KINDS:
        raise ValueError(f'{label}：类型只能是 access / app / other')
    unit = item.get('duration_unit') or 'ms'
    if unit not in DURATION_UNITS:
        raise ValueError(f'{label}：耗时单位只能是 ms / s / us / ns')
    enabled = item.get('enabled', True)
    return {
        'id': sid,
        'name': _text(item.get('name'), f'{label}的名称', 50, required=True),
        'enabled': _bool(enabled, f'{label}的「启用」'),
        'connection': _id(item.get('connection'), f'{label}的连接'),
        'kind': kind,
        'description': _text(item.get('description'), f'{label}的说明', 8000, multiline=True),
        'time_field': field_name(item.get('time_field'), f'{label}的时间字段'),
        'fields': _semantic_fields(item.get('fields'), label),
        'duration_unit': unit,
        'level_values': _level_values(item.get('level_values'), label),
        'correlations': _correlations(item.get('correlations'), label),
        'columns': _columns(item.get('columns'), label),
        'default_fields': _name_list(item.get('default_fields'), f'{label}的默认返回字段', 50,
                                     lambda value: field_name(value, f'{label}的默认返回字段')),
        'mask_fields': _name_list(item.get('mask_fields'), f'{label}的打码字段', 50,
                                  lambda value: field_name(value, f'{label}的打码字段')),
        'mask_patterns': _mask_patterns(item.get('mask_patterns'), label),
        'default_target': _text(item.get('default_target'), f'{label}的默认表 / 索引', 2048),
        'cluster_field': field_name(item.get('cluster_field'), f'{label}的集群字段', allow_empty=True),
    }


def _sources(items):
    if not isinstance(items, list):
        raise ValueError('日志源要是列表')
    if len(items) > MAX_SOURCES:
        raise ValueError(f'日志源最多 {MAX_SOURCES} 个')
    result, seen = [], set()
    for index, item in enumerate(items):
        source = _source(item, index)
        if source['id'] in seen:
            raise ValueError(f'日志源 ID {source["id"]} 重复了')
        seen.add(source['id'])
        result.append(source)
    return result


# ---------------------------------------------------------------------------
# 集群
# ---------------------------------------------------------------------------
def _condition_value(value, label):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError(f'{label}只能是文字或数字')
    if isinstance(value, str):
        return _text(value, label, 200, required=True)
    if isinstance(value, float) and value != value:
        raise ValueError(f'{label}不是合法的数字')
    return value


def _conditions(items, label):
    if items is None:
        return []
    if not isinstance(items, list) or len(items) > 5:
        raise ValueError(f'{label}的过滤条件要是列表，最多 5 个')
    result = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError(f'{label}的过滤条件格式不正确')
        name = field_name(item.get('field'), f'{label}的过滤条件字段')
        op = item.get('op')
        if op not in BINDING_OPS:
            raise ValueError(f'{label}的过滤条件 {name}：op 只能是 eq / ne / in / not_in / prefix')
        values = item.get('values')
        if not isinstance(values, list):
            raise ValueError(f'{label}的过滤条件 {name}：values 要是列表')
        if op in ('in', 'not_in'):
            if not 1 <= len(values) <= 50:
                raise ValueError(f'{label}的过滤条件 {name}：{op} 要有 1~50 个值')
        elif len(values) != 1:
            raise ValueError(f'{label}的过滤条件 {name}：{op} 要正好 1 个值')
        clean = [_condition_value(value, f'{label}的过滤条件 {name} 的值') for value in values]
        result.append({'field': name, 'op': op, 'values': clean})
    return result


def _cluster(item, index):
    if not isinstance(item, dict):
        raise ValueError(f'第 {index + 1} 个集群格式不正确')
    env = item.get('env').strip() if isinstance(item.get('env'), str) else ''
    if not env or len(env) > 100 or _SPACE.search(env) or has_control(env):
        raise ValueError(f'第 {index + 1} 个集群的名称不能为空、不能有空格，最长 100 个字符')
    label = f'集群 {env}'
    bindings = item.get('bindings')
    if bindings is None:
        bindings = []
    if not isinstance(bindings, list) or len(bindings) > MAX_BINDINGS:
        raise ValueError(f'{label}的绑定要是列表，最多 {MAX_BINDINGS} 个')
    clean = []
    for binding in bindings:
        if not isinstance(binding, dict):
            raise ValueError(f'{label}的绑定格式不正确')
        source = _id(binding.get('source'), f'{label}的绑定的日志源')
        where = f'{label}的日志源 {source}'
        connection = binding.get('connection') or ''
        clean.append({
            'source': source,
            'connection': _id(connection, f'{where}的连接') if connection else '',
            'target': _text(binding.get('target'), f'{where}的表 / 索引', 2048, required=True),
            'conditions': _conditions(binding.get('conditions'), where),
            'note': _text(binding.get('note'), f'{where}的说明', 2000, multiline=True),
        })
    return {'env': env, 'note': _text(item.get('note'), f'{label}的说明', 4000, multiline=True), 'bindings': clean}


def _clusters(items):
    if not isinstance(items, list):
        raise ValueError('集群要是列表')
    if len(items) > MAX_CLUSTERS:
        raise ValueError(f'集群最多 {MAX_CLUSTERS} 个')
    result, seen = [], set()
    for index, item in enumerate(items):
        cluster = _cluster(item, index)
        if cluster['env'] in seen:
            raise ValueError(f'集群 {cluster["env"]} 重复了')
        seen.add(cluster['env'])
        result.append(cluster)
    return result


# ---------------------------------------------------------------------------
# 整份配置
# ---------------------------------------------------------------------------
def _cross_check(cfg):
    connections = {item['id']: item for item in cfg['connections']}
    keys = {item['key'] for item in cfg['correlation_keys']}
    sources = {}
    for source in cfg['sources']:
        label = f'日志源 {source["id"]}'
        conn = connections.get(source['connection'])
        if conn is None:
            raise ValueError(f'{label}：连接 {source["connection"]} 不存在')
        for item in source['correlations']:
            if item['key'] not in keys:
                raise ValueError(f'{label}的关联字段 {item["key"]}：关联字段里没有登记这个 key')
            if 'text' in item and not source['fields'].get('message'):
                raise ValueError(f'{label}的关联字段 {item["key"]}：用正文写法要先配置正文字段（语义字段「正文」）')
        if source['default_target']:
            source['default_target'] = check_target(conn['type'], source['default_target'], f'{label}的默认表 / 索引')
        sources[source['id']] = (source, conn)
    for cluster in cfg['clusters']:
        seen = set()
        for binding in cluster['bindings']:
            where = f'集群 {cluster["env"]} 的日志源 {binding["source"]}'
            if binding['source'] not in sources:
                raise ValueError(f'{where}：日志源不存在')
            if binding['source'] in seen:
                raise ValueError(f'{where}：同一个集群里绑定了两次')
            seen.add(binding['source'])
            _, source_conn = sources[binding['source']]
            conn = source_conn
            if binding['connection']:
                conn = connections.get(binding['connection'])
                if conn is None:
                    raise ValueError(f'{where}：连接 {binding["connection"]} 不存在')
                if conn['type'] != source_conn['type']:
                    raise ValueError(f'{where}：连接 {conn["id"]} 和日志源的连接类型不同')
            binding['target'] = check_target(conn['type'], binding['target'], f'{where}的表 / 索引')


def normalize_config(raw, existing=None, check_enabled=True):
    """校验并规范化页面提交的配置，返回存库格式（密码、API Key、请求头的值已加密）；没传的顶层键沿用原值。
    check_enabled=False 时不检查「启用时必填」（测试连接、扫描、预览用页面上还没保存的表单）"""
    if not isinstance(raw, dict):
        raise ValueError('配置格式不正确')
    old = merged(existing)
    enabled = raw.get('enabled', old['enabled'])
    cfg = {
        'enabled': _bool(enabled, '「启用」'),
        'architecture': (_text(raw.get('architecture'), '网络架构说明', 20000, multiline=True)
                         if 'architecture' in raw else old['architecture']),
        'connections': _connections(raw['connections'], old['connections']) if 'connections' in raw
        else old['connections'],
        'correlation_keys': _correlation_keys(raw['correlation_keys']) if 'correlation_keys' in raw
        else old['correlation_keys'],
        'sources': _sources(raw['sources']) if 'sources' in raw else old['sources'],
        'clusters': _clusters(raw['clusters']) if 'clusters' in raw else old['clusters'],
    }
    _cross_check(cfg)
    if check_enabled and cfg['enabled'] and not cfg['connections']:
        raise ValueError('启用前请至少添加一个连接')
    if len(json.dumps(cfg, ensure_ascii=False)) > MAX_CONFIG_CHARS:
        raise ValueError('配置太大（序列化后最多 1MB），请精简说明和字段说明')
    return cfg


# ---------------------------------------------------------------------------
# 给页面看的配置、审计
# ---------------------------------------------------------------------------
def public_config(stored):
    """连接去掉密码和 API Key（换成 has_password / has_api_key），请求头只有名称"""
    cfg = merged(stored)
    connections = []
    for conn in cfg['connections']:
        item = {key: value for key, value in conn.items() if key not in SECRET_KEYS}
        item['has_password'] = bool(conn.get('password'))
        item['has_api_key'] = bool(conn.get('api_key'))
        item['headers'] = [{'name': header['name'], 'has_value': bool(header.get('value'))}
                           for header in conn.get('headers') or [] if isinstance(header, dict) and header.get('name')]
        connections.append(item)
    cfg['connections'] = connections
    return cfg


def _plain(secret):
    from auth.crypto import decrypt_secret

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
    old, new = old or [], new or []
    if [item.get('name') for item in old] != [item.get('name') for item in new]:
        return True
    return any(_secret_changed(a.get('value'), b.get('value')) for a, b in zip(old, new))


def _by(items, key):
    return {item.get(key): item for item in items}


def changed_items(existing, stored):
    """改了哪些项（审计用，只有名称，不含值和密码）"""
    old, new = merged(existing), merged(stored)
    changed = [key for key in ('enabled', 'architecture') if old[key] != new[key]]
    old_conns, new_conns = _by(old['connections'], 'id'), _by(new['connections'], 'id')
    for cid in [*new_conns, *[cid for cid in old_conns if cid not in new_conns]]:
        a, b = old_conns.get(cid), new_conns.get(cid)
        if a is None or b is None:
            changed.append(f'connections.{cid}')
            continue
        plain_keys = [key for key in CONNECTION_DEFAULTS if key not in (*SECRET_KEYS, 'headers')]
        if any(a.get(key) != b.get(key) for key in plain_keys):
            changed.append(f'connections.{cid}')
        changed.extend(f'connections.{cid}.{key}' for key in SECRET_KEYS if _secret_changed(a.get(key), b.get(key)))
        if _headers_changed(a.get('headers'), b.get('headers')):
            changed.append(f'connections.{cid}.headers')
    if old['correlation_keys'] != new['correlation_keys']:
        changed.append('correlation_keys')
    for section, key in (('sources', 'id'), ('clusters', 'env')):
        old_items, new_items = _by(old[section], key), _by(new[section], key)
        for name in [*new_items, *[name for name in old_items if name not in new_items]]:
            if old_items.get(name) != new_items.get(name):
                changed.append(f'{section}.{name}')
    return changed


def fingerprint(conn):
    """缓存键：同一份连接配置（地址、认证、请求头、证书校验）得到同一个值"""
    parts = [conn.get('type'), conn.get('url'), conn.get('auth_type'), conn.get('username'), conn.get('password'),
             conn.get('api_key'), conn.get('headers'), conn.get('tls_verify')]
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode('utf-8')).hexdigest()[:24]


async def load_config():
    """读配置（补上默认值）；读库失败抛 LogError 503"""
    try:
        stored = await store.get_setting(SETTING_KEY, {}) or {}
    except Exception as exc:
        logger.error(f'读取历史日志配置失败: {exc}')
        raise LogError(503, '读取历史日志配置失败，请稍后重试', 'config_unavailable') from exc
    return merged(stored)
