"""实时字段表（约定 §5.1）：查询前用它校验字段名、按类型选参数类型和查询写法。

- ClickHouse：SELECT name, type FROM system.columns WHERE database=… AND table=… ORDER BY position
- Elasticsearch：GET /<target>/_field_caps?fields=*（去掉 _ 开头的元数据字段；多个索引类型不同时用 | 连接；
  text 字段有 <name>.keyword 子字段时记下）；最多 3000 个
- 缓存 10 分钟，键 = 连接指纹 + target；保存配置后清空
"""

import difflib

from . import config as log_config
from .clickhouse import Params, kind_of
from .common import LogError, TTLCache

MAX_FIELDS = 3000
_CACHE = TTLCache(max_items=300, ttl=600)
ES_TEXT = frozenset({'text', 'match_only_text', 'annotated_text', 'search_as_you_type'})
ES_KEYWORD = frozenset({'keyword', 'constant_keyword', 'wildcard', 'version', 'flattened'})
ES_NUMERIC = frozenset({'long', 'integer', 'short', 'byte', 'double', 'float', 'half_float', 'scaled_float',
                        'unsigned_long'})
ES_DATE = frozenset({'date', 'date_nanos'})


def reset():
    _CACHE.clear()


def es_kind(types):
    """field_caps 的类型 → text / keyword / number / date / bool / ip / object / other"""
    types = set(types)
    if types & ES_TEXT:
        return 'text'
    if types & ES_KEYWORD:
        return 'keyword'
    if types and types <= ES_NUMERIC:
        return 'number'
    if types and types <= ES_DATE:
        return 'date'
    if types == {'boolean'}:
        return 'bool'
    if types == {'ip'}:
        return 'ip'
    if types & {'object', 'nested'}:
        return 'object'
    return 'other'


async def _clickhouse(client, target):
    database, table = target.split('.', 1)
    params = Params()
    sql = ('SELECT name, type FROM system.columns\n'
           f'WHERE database = {params.add("db", "String", database)} AND table = {params.add("table", "String", table)}'
           '\nORDER BY position')
    result = await client.query(sql, params)
    fields = {}
    for row in result['data'][:MAX_FIELDS]:
        name, type_ = str(row[0]), str(row[1])
        fields[name] = {'name': name, 'type': type_, 'kind': kind_of(type_)}
    return fields


def parse_field_caps(data):
    raw = data.get('fields') if isinstance(data, dict) else None
    fields = {}
    for name, caps in (raw or {}).items():
        if not isinstance(name, str) or name.startswith('_') or not isinstance(caps, dict):
            continue
        types = sorted(kind for kind in caps if kind != 'unmapped')
        if not types:
            continue
        infos = [caps[kind] for kind in types if isinstance(caps[kind], dict)]
        fields[name] = {
            'name': name, 'type': '|'.join(types), 'kind': es_kind(types),
            'searchable': any(item.get('searchable') for item in infos),
            'aggregatable': any(item.get('aggregatable') for item in infos),
            'keyword': None,
        }
        if len(fields) >= MAX_FIELDS:
            break
    for name, info in fields.items():
        if info['kind'] == 'text':
            sub = fields.get(f'{name}.keyword')
            if sub and sub['kind'] == 'keyword':
                info['keyword'] = sub['name']
    return fields


async def mark_runtime(fields, client, target):
    """ES 的运行时字段（没有索引，按它过滤、聚合要逐条计算）标上 runtime=True；没有权限读映射时不标"""
    try:
        names = await client.runtime_fields(target)
    except LogError:
        return
    for name in names:
        if name in fields:
            fields[name]['runtime'] = True


async def load(bound, client, use_cache=True):
    """绑定的表 / 索引的字段表 {字段名: {name, type, kind, ...}}；表不存在或没有字段时抛 LogError"""
    key = (log_config.fingerprint(bound.conn), bound.target)
    if use_cache:
        cached = _CACHE.get(key)
        if cached is not None:
            return cached
    if bound.type == 'clickhouse':
        fields = await _clickhouse(client, bound.target)
        what = f'表 {bound.target}'
    else:
        fields = parse_field_caps(await client.field_caps(bound.target))
        what = f'索引 {bound.target}'
        await mark_runtime(fields, client, bound.target)
    if not fields:
        raise LogError(502, f'日志源 {bound.id} 在集群 {bound.env} 绑定的{what}没有字段（不存在、没有数据或账号没有权限），'
                            '请系统管理员检查配置', 'datasource_unavailable')
    _CACHE.put(key, fields)
    return fields


def similar(fields, name, count=5):
    names = list(fields)
    lower = {item.lower(): item for item in names}
    found = difflib.get_close_matches(str(name).lower(), list(lower), n=count, cutoff=0.5)
    result = [lower[item] for item in found]
    if len(result) < count:  # 包含关系（例如 status → response_status）
        needle = str(name).lower()
        result.extend(item for item in names if (needle in item.lower() or item.lower() in needle)
                      and item not in result)
    return result[:count]


def require(fields, name, what='', source_id=''):
    """字段要在实时字段表里，否则 400 field_not_found（带最多 5 个相近的字段名）"""
    info = fields.get(name)
    if info is not None:
        return info
    near = similar(fields, name)
    where = f'日志源 {source_id} 里' if source_id else ''
    hint = f'。相近的字段：{"、".join(near)}' if near else '。用 log_sources 带 source 参数看字段列表'
    raise LogError(400, f'{where}没有{what}字段 {name}{hint}', 'field_not_found')
