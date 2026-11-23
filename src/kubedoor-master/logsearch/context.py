"""一次请求用到的东西：配置、当前集群的绑定、日志源解析、客户端、行的整理（打码、截断、链路 ID）。"""

import re

from . import schema as field_schema
from .clickhouse import ClickHouseClient
from .common import LogError, clip_value, iso_ms
from .elastic import ElasticClient
from .mask import Masker

DEFAULT_TRACE_VALUE = r'[^\s"\',;\]\[]+'
RESERVED_KEYS = ('@time', 'trace_id', '_id', 'anchor')


class Bound:
    """一个日志源在当前集群的绑定：日志源配置、绑定、连接；schema 是实时字段表（查询前加载）"""

    def __init__(self, source, binding, conn, env, keys):
        self.source, self.binding, self.conn, self.env = source, binding, conn, env
        self.id, self.type, self.target = source['id'], conn['type'], binding['target']
        self.time_field = source['time_field']
        self.schema = None
        self.runtime_used = set()  # 这次查询用到的 ES 运行时字段（DslPlan.info 记下，超时时提示）
        self.masker = Masker(source['mask_fields'], source['mask_patterns'])
        self.trace = None  # 链路 ID 的关联方式 {'key', 'field' | 'text'}
        self.trace_pattern = None
        for item in source['correlations']:
            key = keys.get(item['key'])
            if key and key['trace']:
                self.trace = item
                if 'text' in item:
                    self.trace_pattern = text_regex(item['text'], key['pattern'])

    def semantic(self, key):
        return self.source['fields'].get(key, '')

    def correlation(self, key):
        return next((item for item in self.source['correlations'] if item['key'] == key), None)

    def has(self, name):
        return bool(name) and self.schema is not None and name in self.schema

    def timeout_hint(self, exc):
        """ES 超时且用到了运行时字段：在报错里点出来，让 AI 改用正文关键字"""
        if exc.code != 'timeout' or not self.runtime_used:
            return exc.message
        names = '、'.join(sorted(self.runtime_used)[:5])
        return (f'{exc.message}。用到的 {names} 是运行时字段（没有索引，按它过滤要逐条计算）：'
                '改用正文关键字（keyword）或缩短时间窗口')


def text_regex(text, pattern=''):
    """正文写法（例如 TID:{value}）→ 正则：字面部分转义，{value} 换成取值的模式（捕获组 1）"""
    prefix, _, suffix = text.partition('{value}')
    return re.compile(re.escape(prefix) + f'({pattern or DEFAULT_TRACE_VALUE})' + re.escape(suffix))


def flatten(doc, prefix='', out=None):
    """ES 的 _source 打平成「a.b.c」形式（数组原样保留）"""
    out = {} if out is None else out
    for key, value in (doc or {}).items():
        name = f'{prefix}{key}'
        if isinstance(value, dict) and value:
            flatten(value, f'{name}.', out)
        else:
            out[name] = value
    return out


def fields_value(value):
    """ES fields 返回的值都是数组：一个值时取出来"""
    if isinstance(value, list) and len(value) == 1:
        return value[0]
    return value


class Ctx:
    def __init__(self, cfg, env, session, aliases=None):
        self.cfg, self.env, self.session = cfg, env, session
        self.aliases = aliases or {}
        self.connections = {item['id']: item for item in cfg['connections']}
        self.sources = {item['id']: item for item in cfg['sources']}
        self.keys = {item['key']: item for item in cfg['correlation_keys']}
        self.cluster = next((item for item in cfg['clusters'] if item['env'] == env), None)
        self.opened = []  # 这次请求打开过的绑定（超时时据此提示运行时字段）
        self.bindings = {item['source']: item for item in (self.cluster or {}).get('bindings', [])}

    @property
    def trace_key(self):
        return next((item for item in self.cfg['correlation_keys'] if item['trace']), None)

    def bound_sources(self, enabled_only=True):
        """绑定到当前集群的日志源（按配置顺序）"""
        result = []
        for source in self.cfg['sources']:
            if source['id'] in self.bindings and (source['enabled'] or not enabled_only):
                result.append(source)
        return result

    def available_ids(self):
        return [source['id'] for source in self.bound_sources()]

    def _available_text(self):
        ids = self.available_ids()
        return f'当前集群 {self.env} 可用的日志源：{"、".join(ids)}' if ids else f'当前集群 {self.env} 还没有绑定日志源'

    def bound(self, source_id):
        """日志源 ID → Bound；不存在 404 source_not_found、没绑定到当前集群 404 source_not_bound、停用 400 source_disabled"""
        source = self.sources.get(source_id)
        if source is None:
            raise LogError(404, f'没有日志源 {source_id}。{self._available_text()}（用 log_sources 查看）',
                           'source_not_found')
        binding = self.bindings.get(source_id)
        if binding is None:
            raise LogError(404, f'日志源 {source_id} 没有绑定到集群 {self.env}。{self._available_text()}',
                           'source_not_bound')
        if not source['enabled']:
            raise LogError(400, f'日志源 {source_id} 已停用', 'source_disabled')
        conn = self.connections.get(binding['connection'] or source['connection'])
        if conn is None:  # 配置保存时校验过，正常不会出现
            raise LogError(400, f'日志源 {source_id} 的连接不存在，请系统管理员检查配置', 'invalid_request')
        bound = Bound(source, binding, conn, self.env, self.keys)
        self.opened.append(bound)
        return bound

    def timeout_hint(self, exc):
        """整个请求失败时的报错：超时且用到了运行时字段时点出来"""
        for bound in self.opened:
            if bound.runtime_used:
                return bound.timeout_hint(exc)
        return exc.message

    def client(self, bound_or_conn, **kwargs):
        conn = bound_or_conn.conn if isinstance(bound_or_conn, Bound) else bound_or_conn
        if conn['type'] == 'clickhouse':
            return ClickHouseClient(conn, self.session, **kwargs)
        return ElasticClient(conn, self.session, **kwargs)

    async def load_schema(self, bound, client=None):
        if bound.schema is None:
            bound.schema = await field_schema.load(bound, client or self.client(bound))
        return bound.schema


# ---------------------------------------------------------------------------
# 返回的字段和行
# ---------------------------------------------------------------------------
def default_output(bound):
    """没指定 fields 时返回的字段：default_fields > 已配置的语义字段 + 关联列；都没有时 []（= 全部字段）"""
    schema = bound.schema or {}
    names = [name for name in bound.source['default_fields'] if name in schema]
    if not names:
        for name in bound.source['fields'].values():
            if name in schema and name not in names:
                names.append(name)
        for item in bound.source['correlations']:
            if item.get('field') in schema and item['field'] not in names:
                names.append(item['field'])
    return [name for name in names if name != bound.time_field]


def fetch_list(bound, output):
    """实际要取的字段：返回的字段 + 取链路 ID 要用的列或正文"""
    names = list(output)
    if bound.trace:
        extra = bound.trace.get('field') or bound.semantic('message')
        if extra and bound.has(extra) and extra not in names and extra != bound.time_field:
            names.append(extra)
    return names


def trace_of(bound, values):
    """从行里取链路 ID：列取值，或按正文写法从正文里提取"""
    if not bound.trace:
        return None
    if bound.trace.get('field'):
        value = values.get(bound.trace['field'])
        return str(value) if value not in (None, '') and not isinstance(value, (dict, list)) else None
    message = values.get(bound.semantic('message'))
    if isinstance(message, str) and bound.trace_pattern is not None:
        match = bound.trace_pattern.search(message)
        if match:
            return match.group(1)
    return None


def make_row(bound, millis, values, output, all_fields=False, extra=None):
    """一行结果：@time + 选中的字段（打码、截断）+ trace_id；all_fields 时返回全部字段（值照样截断）"""
    row = {'@time': iso_ms(millis) if millis is not None else None}
    names = [name for name in values if name != bound.time_field] if all_fields else output
    for name in names:
        if name in RESERVED_KEYS and name in row:
            continue
        row[name] = clip_value(bound.masker.value(name, values.get(name)))
    trace_id = trace_of(bound, values)
    if trace_id and not row.get('trace_id'):
        row['trace_id'] = clip_value(bound.masker.text(trace_id), 200, 200)
    row.update(extra or {})
    return row
