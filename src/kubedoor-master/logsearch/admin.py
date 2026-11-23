"""配置页的测试连接、预览绑定、验证关联字段（约定 §3）。都用页面上还没保存的表单（密码留空用已保存的）。"""

import asyncio
import json

from .build_dsl import DslPlan
from .build_sql import SqlPlan
from .clickhouse import ClickHouseClient, column
from .clickhouse import rows as ch_rows
from .common import LogError, clip_value, now, to_int
from .context import Bound, default_output, fetch_list, text_regex
from .elastic import ElasticClient
from . import config as log_config
from . import schema as field_schema
from .ops import _rows, fetch

PREVIEW_MINUTES = 15
PREVIEW_ROWS = 5
VERIFY_SOURCES = 8
VERIFY_VALUES = 20
VERIFY_MESSAGES = 200
HOUR_MS = 3600 * 1000


# ---------------------------------------------------------------------------
# 测试连接
# ---------------------------------------------------------------------------
async def test_connection(conn, session):
    """CH：{type, version, timezone, user, readonly, clusters}；
    ES：{type, version, distribution, cluster_name, license, esql}"""
    if conn['type'] == 'clickhouse':
        client = ClickHouseClient(conn, session)
        result = await client.query("SELECT version() AS version, timezone() AS timezone, currentUser() AS user, "
                                    "getSetting('readonly') AS readonly", use_settings=False)
        info = ch_rows(result)[0] if result['data'] else {}
        try:
            clusters = await client.query('SELECT DISTINCT cluster FROM system.clusters ORDER BY cluster LIMIT 100')
            names = [str(row[0]) for row in clusters['data']]
        except LogError:
            names = []
        return {'type': 'clickhouse', 'version': str(info.get('version') or ''),
                'timezone': str(info.get('timezone') or ''), 'user': str(info.get('user') or ''),
                'readonly': to_int(info.get('readonly'), 0), 'clusters': names}
    client = ElasticClient(conn, session)
    info = await client.info(use_cache=False)
    try:
        license_data = await client.call('GET', '/_license')
        license_type = str((license_data.get('license') or {}).get('type') or '') or None
    except LogError:
        license_type = None
    return {'type': 'elasticsearch', 'version': info['version'], 'distribution': info['distribution'],
            'cluster_name': info['cluster_name'], 'license': license_type, 'esql': info['esql']}


# ---------------------------------------------------------------------------
# 预览
# ---------------------------------------------------------------------------
def admin_bound(ctx, source_id):
    """管理员预览 / 验证用：停用的日志源也可以看；没绑定到这个集群返回 404"""
    source = ctx.sources.get(source_id)
    if source is None:
        raise LogError(404, f'没有日志源 {source_id}', 'source_not_found')
    binding = ctx.bindings.get(source_id)
    if binding is None:
        raise LogError(404, f'日志源 {source_id} 没有绑定到集群 {ctx.env}', 'source_not_bound')
    conn = ctx.connections[binding['connection'] or source['connection']]
    return Bound(source, binding, conn, ctx.env, ctx.keys)


def schema_problems(bound):
    """配置里引用了、实际不存在（或类型不对）的字段"""
    fields = bound.schema or {}
    source = bound.source
    problems = []

    def check(name, what):
        if name and name not in fields:
            problems.append(f'{what} {name} 不存在')

    check(source['time_field'], '时间字段')
    info = fields.get(source['time_field'])
    if info and info['kind'] != 'date':
        problems.append(f'时间字段 {source["time_field"]} 的类型是 {info["type"]}，不支持')
    for key, name in source['fields'].items():
        check(name, f'语义字段「{log_config.SEMANTIC_LABELS[key]}」')
    for item in source['correlations']:
        check(item.get('field'), f'关联字段 {item["key"]} 的列')
    for cond in bound.binding['conditions']:
        check(cond['field'], '集群过滤条件的字段')
    for name in source['default_fields']:
        check(name, '默认返回字段')
    for name in source['mask_fields']:
        check(name, '打码字段')
    for item in source['columns']:
        check(item['name'], '字段说明里的字段')
    check(source['cluster_field'], '集群字段')
    return problems


async def preview(ctx, source_id):
    """按这个集群的绑定查最近 15 分钟最新 5 条：{ok, rows, total, elapsed_ms, query, schema_problems, error}"""
    bound = admin_bound(ctx, source_id)
    client = ctx.client(bound)
    data = {'ok': False, 'rows': [], 'total': None, 'elapsed_ms': None, 'query': '', 'schema_problems': [],
            'error': None}
    try:
        await ctx.load_schema(bound, client)
        data['schema_problems'] = schema_problems(bound)
        end_ms = int(now() * 1000)
        start_ms = end_ms - PREVIEW_MINUTES * 60 * 1000
        spec = {'filters': [], 'k8s': [], 'level': None, 'keyword': '', 'exclude': []}
        output = default_output(bound)
        names = fetch_list(bound, output) if output else []
        result = await fetch(bound, client, spec, start_ms, end_ms, names, PREVIEW_ROWS)
        data['rows'] = _rows(bound, result['items'], output)
        data['total'] = result['total']
        data['elapsed_ms'] = (result['stats'] or {}).get('elapsed_ms')
        query = result['query']
        data['query'] = query if isinstance(query, str) else json.dumps(query, ensure_ascii=False, indent=2)
        data['ok'] = True
    except LogError as exc:
        data['error'] = exc.message
    return data


# ---------------------------------------------------------------------------
# 验证关联字段
# ---------------------------------------------------------------------------
async def _ch_values(bound, client, item, pattern, start_ms, end_ms, values=None):
    """ClickHouse：列 → 不同取值；正文写法 → 从正文里提取。values 给了时只看这些值命中了哪些"""
    plan = SqlPlan(bound)
    spec = {'filters': [], 'k8s': [], 'level': None, 'keyword': '', 'exclude': []}
    where = plan.where(spec, start_ms, end_ms)
    if 'field' in item:
        col = column(item['field'])
        cond = f"isNotNull({col}) AND toString({col}) != ''"
        if values:
            cond += f' AND toString({col}) IN {plan.params.add("vals", "Array(String)", list(values))}'
        sql = (f'SELECT DISTINCT toString({col}) AS v\nFROM {plan.table}\nWHERE {where}\n  AND {cond}\n'
               f'LIMIT {plan.params.add("n", "UInt32", VERIFY_VALUES)}')
        result = await client.query(sql, plan.params)
        return [str(row[0]) for row in result['data']]
    message = column(bound.semantic('message'))
    if values:
        phrases = [item['text'].replace('{value}', value) for value in values]
        cond = f'multiSearchAnyCaseInsensitive(toString({message}), {plan.params.add("ph", "Array(String)", phrases)})'
    else:
        literal = item['text'].split('{value}')[0] or item['text'].split('{value}')[1]
        cond = f'positionCaseInsensitive(toString({message}), {plan.params.add("lit", "String", literal)}) > 0'
    sql = (f'SELECT toString({message}) AS m\nFROM {plan.table}\nWHERE {where}\n  AND {cond}\n'
           f'ORDER BY {plan.time} DESC\nLIMIT {plan.params.add("n", "UInt32", VERIFY_MESSAGES)}')
    result = await client.query(sql, plan.params)
    return _extract([row[0] for row in result['data']], item, pattern, values)


def _extract(messages, item, pattern, values=None):
    found = []
    if values:
        lowered = [str(message).lower() for message in messages]
        for value in values:
            phrase = item['text'].replace('{value}', value).lower()
            if any(phrase in message for message in lowered) and value not in found:
                found.append(value)
        return found
    regex = text_regex(item['text'], pattern)
    for message in messages:
        for match in regex.finditer(str(message or '')):
            if match.group(1) not in found:
                found.append(match.group(1))
            if len(found) >= VERIFY_VALUES:
                return found
    return found


async def _es_values(bound, client, item, pattern, start_ms, end_ms, values=None):
    plan = DslPlan(bound)
    extra = []
    if 'field' in item:
        name = item['field']
        exact = plan.exact(name)
        if values:
            bucket, query = plan.clause(name, 'in', list(values))
            extra.append((bucket, query))
        else:
            extra.append(('filter', {'exists': {'field': name}}))
        spec = {'filters': [], 'k8s': [], 'level': None, 'keyword': '', 'exclude': []}
        if exact:
            body = {'query': plan.query(spec, start_ms, end_ms, extra), 'size': 0,
                    'aggs': {'v': {'terms': {'field': exact, 'size': VERIFY_VALUES}}}}
            data = await client.search(bound.target, body)
            buckets = ((data.get('aggregations') or {}).get('v') or {}).get('buckets') or []
            found = [str(bucket.get('key_as_string', bucket.get('key'))) for bucket in buckets]
            return [value for value in found if not values or value in values]
        body = plan.search_body(spec, start_ms, end_ms, [name], VERIFY_MESSAGES, extra=extra)
        data = await client.search(bound.target, body)
        found = []
        for hit in (data.get('hits') or {}).get('hits') or []:
            value = (hit.get('_source') or {}).get(name)
            if value not in (None, '') and str(value) not in found and (not values or str(value) in values):
                found.append(str(value))
        return found[:VERIFY_VALUES]
    message = bound.semantic('message')
    if values:
        should = [{'match_phrase': {message: item['text'].replace('{value}', value)}} for value in values]
        extra.append(('filter', {'bool': {'should': should, 'minimum_should_match': 1}}))
    else:
        literal = item['text'].split('{value}')[0] or item['text'].split('{value}')[1]
        extra.append(('filter', {'match_phrase': {message: literal}}))
    spec = {'filters': [], 'k8s': [], 'level': None, 'keyword': '', 'exclude': []}
    body = plan.search_body(spec, start_ms, end_ms, [message], VERIFY_MESSAGES, extra=extra)
    data = await client.search(bound.target, body)
    messages = [(hit.get('_source') or {}).get(message) for hit in (data.get('hits') or {}).get('hits') or []]
    return _extract(messages, item, pattern, values)


async def verify(ctx, key_name):
    """当前集群里登记了这个 key 的每个日志源，各抽最近 1 小时最多 20 个取值，到其他日志源里查（±1 小时）"""
    key = ctx.keys.get(key_name)
    if key is None:
        raise LogError(400, f'没有关联字段 {key_name}', 'invalid_request')
    notes = []
    candidates = [source for source in ctx.cfg['sources'] if source['id'] in ctx.bindings
                  and any(item['key'] == key_name for item in source['correlations'])]
    if len(candidates) > VERIFY_SOURCES:
        notes.append(f'登记了 {key_name} 的日志源有 {len(candidates)} 个，只验证前 {VERIFY_SOURCES} 个')
        candidates = candidates[:VERIFY_SOURCES]
    if len(candidates) < 2:
        notes.append(f'集群 {ctx.env} 里登记了 {key_name} 的日志源不到 2 个，没有可以互相验证的')
    end_ms = int(now() * 1000)
    bounds = {}
    for source in candidates:
        bound = admin_bound(ctx, source['id'])
        try:
            client = ctx.client(bound)
            await ctx.load_schema(bound, client)
            item = bound.correlation(key_name)
            if 'field' in item:
                field_schema.require(bound.schema, item['field'], '关联', bound.id)
            elif not bound.has(bound.semantic('message')):
                raise LogError(400, f'日志源 {bound.id} 的正文字段不存在', 'field_not_found')
            bounds[source['id']] = (bound, client, item)
        except LogError as exc:
            notes.append(f'日志源 {source["id"]}：{exc.message}')

    async def values_of(source_id, values=None, window=None):
        bound, client, item = bounds[source_id]
        start_ms, finish_ms = window
        fetcher = _ch_values if bound.type == 'clickhouse' else _es_values
        return await fetcher(bound, client, item, key['pattern'], start_ms, finish_ms, values)

    sampled = {}
    for source_id, result in zip(bounds, await asyncio.gather(
            *[values_of(source_id, window=(end_ms - HOUR_MS, end_ms)) for source_id in bounds],
            return_exceptions=True)):
        if isinstance(result, LogError):
            notes.append(f'日志源 {source_id} 抽取取值失败：{bounds[source_id][0].timeout_hint(result)}')
        elif isinstance(result, Exception):
            raise result
        else:
            sampled[source_id] = result[:VERIFY_VALUES]
            if not result:
                notes.append(f'日志源 {source_id} 最近 1 小时没有取到 {key_name} 的值')
    pairs_todo = [(a, b) for a in sampled if sampled[a] for b in bounds if b != a]
    results = await asyncio.gather(*[values_of(b, sampled[a], (end_ms - 2 * HOUR_MS, end_ms + HOUR_MS))
                                     for a, b in pairs_todo], return_exceptions=True)
    pairs = []
    for (a, b), result in zip(pairs_todo, results):
        if isinstance(result, Exception) and not isinstance(result, LogError):
            raise result
        entry = {'from': a, 'to': b, 'sampled': len(sampled[a]), 'matched': 0, 'examples': []}
        if isinstance(result, LogError):
            entry['error'] = bounds[b][0].timeout_hint(result)
        else:
            matched = [value for value in sampled[a] if value in result]
            entry['matched'] = len(matched)
            entry['examples'] = [clip_value(value, 200, 200) for value in matched[:3]]
        pairs.append(entry)
    return {'pairs': pairs, 'notes': notes}
