"""7 个 AI 操作（约定 §4）：sources / search / stats / patterns / context / correlate / query。

每个操作返回 data（handler 包成 {"success": true, "data": …}）；失败抛 LogError。返回的字符串都已打码、截断，
整个 data 序列化后不超过 60000 字（超了从尾部丢行并标 truncated）。
"""

import asyncio
import math
import re

from tracing import config as tracing_config

from . import config as log_config
from . import patterns as pattern_mod
from . import raw
from . import schema as field_schema
from .build_dsl import DslPlan, es_time
from .build_sql import MS_OFFSET, SqlPlan
from .clickhouse import column
from .clickhouse import rows as ch_rows
from .common import (MAX_DATA_CHARS, LogError, ResponseTooLarge, check_keys, clip_value, first_line, fit_rows,
                     int_param, invalid, iso_ms, list_param, parse_time, resolve_window, size_of, text_param,
                     tidy_number, to_int, to_ms, window_text)
from .context import default_output, fetch_list, fields_value, flatten, make_row
from .elastic import total_of
from .params import (FILTER_KEYS, PARAM_HINTS, TIME_KEYS, describe, need_semantic, parse_exclude, parse_filters,
                     parse_spec, source_id)

SEARCH_KEYS = FILTER_KEYS | {'fields', 'limit', 'order'}
STATS_KEYS = FILTER_KEYS | {'group_by', 'interval', 'metrics', 'top'}
PATTERN_KEYS = FILTER_KEYS | {'exclude', 'sample', 'top'}
CONTEXT_KEYS = frozenset({'source', 'time', 'k8s_pod', 'anchor_id', 'before', 'after', 'filters'})
CORRELATE_KEYS = frozenset({'key', 'value', 'around', *TIME_KEYS, 'sources', 'limit'})
QUERY_KEYS = frozenset({'source', *TIME_KEYS, 'sql', 'dsl', 'esql', 'limit'})
QUERY_HINTS = {**PARAM_HINTS, 'query': 'sql / dsl / esql（按日志源类型三选一）'}
INTERVALS = {'1m': 60, '5m': 300, '10m': 600, '30m': 1800, '1h': 3600, '3h': 10800, '6h': 21600, '12h': 43200,
             '1d': 86400}
METRIC_OPS = ('count', 'avg', 'min', 'max', 'sum', 'p50', 'p90', 'p95', 'p99', 'uniq')
MAX_TIME_BUCKETS = 500
AUTO_BUCKETS = 60
TIME_TOP = 10
ES_MAX_BUCKETS = 500
DESCRIPTION_CHARS = 600
SCHEMA_ITEMS = 300
SUMMARY_CHARS = 200
TIMELINE_ITEMS = 100
PATTERN_CHARS = 1000  # 模式归类只看第一行（≤ 1000 字），正文只取这么多
CONTEXT_WINDOW_MS = 3600 * 1000
HINT_ROWS = '结果超过 60000 字，后面的行没有返回：加条件、减少 fields 或缩短时间窗口'
HINT_STATS = '结果超过 60000 字，后面的行没有返回：减少分组、加大 interval、减小 top 或缩短时间窗口'


async def _open(ctx, sid):
    bound = ctx.bound(sid)
    client = ctx.client(bound)
    await ctx.load_schema(bound, client)
    return bound, client


def _ms_window(start, end):
    return to_ms(start), int(math.ceil(end * 1000))


def _es_values(bound, hit):
    values = flatten(hit.get('_source') or {})
    for name, value in (hit.get('fields') or {}).items():
        if name not in values and name != bound.time_field:
            values[name] = fields_value(value)
    return values


async def fetch(bound, client, spec, start_ms, end_ms, names, limit, order='desc', track_total=10000):
    """明细查询 → {'items': [(毫秒, 原始值, _id)], 'total', 'relation', 'stats', 'query'}；names 为空取全部字段"""
    if bound.type == 'clickhouse':
        plan = SqlPlan(bound)
        cols = list(names) if names else [name for name in bound.schema if name != bound.time_field]
        sql = plan.select_rows(spec, start_ms, end_ms, cols, limit, order)
        result = await client.query(sql, plan.params)
        items = []
        for row in ch_rows(result):
            millis = row.pop('kd_ms', None)
            items.append((to_int(millis, None), row, None))
        return {'items': items, 'total': None, 'relation': None, 'stats': result['stats'],
                'query': plan.params.render(sql)}
    plan = DslPlan(bound)
    body = plan.search_body(spec, start_ms, end_ms, names, limit, order, track_total=track_total)
    data = await client.search(bound.target, body)
    items = [(es_time(hit, bound.time_field), _es_values(bound, hit), hit.get('_id'))
             for hit in (data.get('hits') or {}).get('hits') or []]
    total, relation = total_of(data)
    return {'items': items, 'total': total, 'relation': relation,
            'stats': {'elapsed_ms': data.get('took', data.get('_elapsed_ms'))}, 'query': body}


def _rows(bound, items, output):
    all_fields = not output
    return [make_row(bound, millis, values, output, all_fields, {'_id': doc_id} if doc_id else None)
            for millis, values, doc_id in items]


def _requested_fields(body, bound):
    items = list_param(body, 'fields', 50)
    names = []
    for item in items:
        if not isinstance(item, str) or not item.strip():
            raise invalid('fields 的每一项要是字段名')
        name = item.strip()
        field_schema.require(bound.schema, name, '', bound.id)
        if name != bound.time_field and name not in names:
            names.append(name)
    return names


def _order(body):
    order = body.get('order') or 'desc'
    if order not in ('desc', 'asc'):
        raise invalid('order 只能是 desc 或 asc')
    return order


# ---------------------------------------------------------------------------
# sources
# ---------------------------------------------------------------------------
async def _tracing_enabled():
    try:
        cfg = await tracing_config.load_config()
    except Exception:
        return None
    return cfg if cfg['enabled'] and cfg['url'] else None


async def _raw_languages(ctx, conn):
    if not conn['allow_raw']:
        return []
    if conn['type'] == 'clickhouse':
        return ['sql']
    try:
        info = await ctx.client(conn, timeout=5).info()
    except LogError:
        return ['dsl']
    return ['dsl', 'esql'] if info['esql'] else ['dsl']


def _correlation_items(source):
    return [{'key': item['key'], **({'field': item['field']} if 'field' in item else {'text': item['text']})}
            for item in source['correlations']]


async def sources(ctx, body):
    check_keys(body, {'source'}, PARAM_HINTS)
    if body.get('source') not in (None, ''):
        return await _source_detail(ctx, source_id(body))
    bound_list = ctx.bound_sources()
    languages = {}
    for source in bound_list:
        binding = ctx.bindings[source['id']]
        conn = ctx.connections.get(binding['connection'] or source['connection'])
        if conn and conn['id'] not in languages:
            languages[conn['id']] = None
    conns = [ctx.connections[cid] for cid in languages]
    for conn, result in zip(conns, await asyncio.gather(*[_raw_languages(ctx, conn) for conn in conns])):
        languages[conn['id']] = result
    items = []
    for source in bound_list:
        binding = ctx.bindings[source['id']]
        conn = ctx.connections.get(binding['connection'] or source['connection']) or {}
        description = source['description']
        if len(description) > DESCRIPTION_CHARS:
            description = description[:DESCRIPTION_CHARS] + '…（看全文传 source）'
        items.append({
            'id': source['id'], 'name': source['name'], 'kind': source['kind'], 'type': conn.get('type'),
            'description': description, 'target': binding['target'], 'binding_note': binding['note'],
            'correlations': [item['key'] for item in source['correlations']],
            'raw_query': languages.get(conn.get('id'), []),
        })
    bound_ids = {item['id'] for item in items}
    data = {
        'env': ctx.env,
        'architecture': ctx.cfg['architecture'],
        'cluster_note': (ctx.cluster or {}).get('note', ''),
        'correlation_keys': [{key: item[key] for key in ('key', 'label', 'description', 'pattern', 'trace')}
                             for item in ctx.cfg['correlation_keys']],
        'tracing': {'configured': await _tracing_enabled() is not None},
        'sources': items,
        'unbound': [{'id': source['id'], 'name': source['name']} for source in ctx.cfg['sources']
                    if source['enabled'] and source['id'] not in bound_ids],
    }
    if not items:
        data['notes'] = [f'集群 {ctx.env} 还没有绑定日志源（系统管理 → 历史日志 → 集群）']
    return fit_rows(data, 'sources', '日志源太多，后面的没有列出：用 source 参数看单个日志源')


def _schema_list(bound):
    described = {item['name']: item['description'] for item in bound.source['columns']}
    semantic = {name: key for key, name in bound.source['fields'].items()}
    items = []
    for position, (name, info) in enumerate(bound.schema.items()):
        text = described.get(name, '')
        if name in semantic:
            label = f'语义：{log_config.SEMANTIC_LABELS[semantic[name]]}'
            text = f'{label}；{text}' if text else label
        elif name == bound.time_field:
            text = f'时间字段；{text}' if text else '时间字段'
        rank = 0 if (name in semantic or name == bound.time_field) else (1 if text else 2)
        item = {'name': name, 'type': info['type'], 'description': text}
        if info.get('runtime'):
            item['runtime'] = True  # ES 运行时字段：没有索引，按它过滤、统计很慢
        items.append((rank, position, item))
    items.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in items]


async def _source_detail(ctx, sid):
    bound = ctx.bound(sid)
    notes = []
    try:
        await ctx.load_schema(bound)
        schema_items = _schema_list(bound)
    except LogError as exc:
        schema_items = []
        notes.append(f'取实时字段表失败：{exc.message}')
    source = bound.source
    raw_query = await _raw_languages(ctx, bound.conn)
    data = {
        'source': {
            'id': source['id'], 'name': source['name'], 'kind': source['kind'], 'type': bound.type,
            'description': source['description'], 'target': bound.target,
            'conditions': describe(bound, {}), 'binding_note': bound.binding['note'],
            'time_field': source['time_field'], 'fields': dict(source['fields']),
            'duration_unit': source['duration_unit'], 'level_values': dict(source['level_values']),
            'correlations': _correlation_items(source), 'default_fields': list(source['default_fields']),
            'raw_query': raw_query,
        },
        'schema': schema_items[:SCHEMA_ITEMS],
        'schema_truncated': len(schema_items) > SCHEMA_ITEMS,
    }
    if notes:
        data['notes'] = notes
    return fit_rows(data, 'schema', '字段太多，后面的没有列出', count_key='')


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------
async def search(ctx, body):
    bound, client = await _open(ctx, source_id(body))
    spec = parse_spec(body, bound, SEARCH_KEYS)
    requested = _requested_fields(body, bound)
    limit = int_param(body, 'limit', 1, 200, 50)
    order = _order(body)
    output = requested or default_output(bound)
    start_ms, end_ms = _ms_window(spec['start'], spec['end'])
    result = await fetch(bound, client, spec, start_ms, end_ms, fetch_list(bound, output) if output else [],
                         limit, order)
    rows = _rows(bound, result['items'], output)
    data = {
        'source': bound.id, 'window': window_text(spec['start'], spec['end']), 'conditions': describe(bound, spec),
        'total': result['total'], 'total_relation': result['relation'], 'rows': rows, 'returned': len(rows),
        'truncated': False, 'hint': '', 'stats': result['stats'],
    }
    return fit_rows(data, hint=HINT_ROWS)


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------
def _metric_name(metric):
    return 'count' if metric['op'] == 'count' else f'{metric["op"]}_{metric["field"]}'


def auto_interval(seconds):
    for name, length in INTERVALS.items():
        if math.ceil(seconds / length) <= AUTO_BUCKETS:
            return name
    return '1d'


def parse_stats_args(body, bound, spec):
    group_by = []
    for item in list_param(body, 'group_by', 3):
        if not isinstance(item, str) or not item.strip():
            raise invalid('group_by 的每一项要是 time 或字段名')
        name = item.strip()
        if name in group_by:
            raise invalid(f'group_by 里 {name} 重复了')
        if name != 'time':
            field_schema.require(bound.schema, name, '', bound.id)
        group_by.append(name)
    metrics = []
    for item in list_param(body, 'metrics', 5) or [{'op': 'count'}]:
        if not isinstance(item, dict) or any(key not in ('op', 'field') for key in item):
            raise invalid('metrics 的每一项要是 {op, field}')
        op = item.get('op')
        if op not in METRIC_OPS:
            raise invalid(f'metrics 的 op 只能是 {" / ".join(METRIC_OPS)}')
        if op == 'count':
            if item.get('field') not in (None, ''):
                raise invalid('count 不带 field')
            metric = {'op': 'count'}
        else:
            name = item.get('field')
            if not isinstance(name, str) or not name.strip():
                raise invalid(f'{op} 要带 field')
            field_schema.require(bound.schema, name.strip(), '', bound.id)
            metric = {'op': op, 'field': name.strip()}
        if any(_metric_name(existing) == _metric_name(metric) for existing in metrics):
            raise invalid(f'metrics 里 {_metric_name(metric)} 重复了')
        metrics.append(metric)
    top = int_param(body, 'top', 1, 200, 20)
    interval = None
    if 'time' in group_by:
        seconds = spec['end'] - spec['start']
        given = body.get('interval')
        if given not in (None, ''):
            if given not in INTERVALS:
                raise invalid(f'interval 只能是 {" / ".join(INTERVALS)}')
            interval = given
        else:
            interval = auto_interval(seconds)
        if math.ceil(seconds / INTERVALS[interval]) > MAX_TIME_BUCKETS:
            raise invalid(f'时间桶太多（超过 {MAX_TIME_BUCKETS} 个）：加大 interval 或缩短时间窗口')
    return {'group_by': group_by, 'metrics': metrics, 'top': top, 'interval': interval}


def _bucket_starts(start_ms, end_ms, interval_ms):
    first = (start_ms + MS_OFFSET) // interval_ms * interval_ms - MS_OFFSET
    return list(range(first, end_ms, interval_ms))


def _empty_metric(metric):
    return 0 if metric['op'] in ('count', 'uniq', 'sum') else None


def _metric_value(metric, value):
    if metric['op'] in ('count', 'uniq'):
        return to_int(value, 0)
    if isinstance(value, str):
        return value  # 时间字段的 min / max
    return tidy_number(value, 3)


def _ch_text(value):
    if isinstance(value, bool):
        return 'true' if value else 'false'
    return str(value)


async def _stats_clickhouse(bound, client, spec, args, start_ms, end_ms):
    plan = SqlPlan(bound)
    metrics, group_by = args['metrics'], args['group_by']
    fields = [name for name in group_by if name != 'time']
    where = plan.where(spec, start_ms, end_ms)
    metric_sql = ', '.join(f'{plan.metric_expr(metric)} AS kd_m{index}' for index, metric in enumerate(metrics))
    totals_sql = f'SELECT count() AS kd_total, {metric_sql}\nFROM {plan.table}\nWHERE {where}'
    group_sql = ', '.join(f'{column(name)} AS kd_g{index}' for index, name in enumerate(fields))
    group_names = ', '.join(f'kd_g{index}' for index in range(len(fields)))
    result = {'rows': [], 'stats': None}
    if not group_by:
        totals = await client.query(totals_sql, plan.params)
        grouped = None
    elif 'time' not in group_by:
        sql = (f'SELECT {group_sql}, {metric_sql}\nFROM {plan.table}\nWHERE {where}\nGROUP BY {group_names}'
               f'\nORDER BY count() DESC\nLIMIT {plan.params.add("top", "UInt32", args["top"])}')
        grouped, totals = await asyncio.gather(client.query(sql, plan.params), client.query(totals_sql, plan.params))
    else:
        interval_ms = INTERVALS[args['interval']] * 1000
        bucket = plan.bucket_expr(interval_ms)
        extra, combos = '', None
        if fields:
            top_sql = (f'SELECT {group_sql}, count() AS kd_c\nFROM {plan.table}\nWHERE {where}\nGROUP BY {group_names}'
                       f'\nORDER BY kd_c DESC\nLIMIT {plan.params.add("k", "UInt32", min(args["top"], TIME_TOP))}')
            top_rows = await client.query(top_sql, plan.params)
            combos = [tuple(row[:len(fields)]) for row in top_rows['data']]
            for index, name in enumerate(fields):
                values = sorted({_ch_text(combo[index]) for combo in combos if combo[index] is not None})
                cond = f'toString({column(name)}) IN {plan.params.add("top_values", "Array(String)", values)}'
                if any(combo[index] is None for combo in combos):
                    cond = f'({cond} OR isNull({column(name)}))'
                extra += f'\n  AND {cond}'
        select_groups = f', {group_sql}' if fields else ''
        sql = (f'SELECT {bucket} AS kd_t{select_groups}, {metric_sql}\nFROM {plan.table}\nWHERE {where}{extra}'
               f'\nGROUP BY kd_t{", " + group_names if fields else ""}\nORDER BY kd_t')
        if combos == []:
            grouped, totals = None, await client.query(totals_sql, plan.params)
        else:
            grouped, totals = await asyncio.gather(client.query(sql, plan.params),
                                                   client.query(totals_sql, plan.params))
    total_row = ch_rows(totals)[0] if totals['data'] else {}
    result['totals'] = {'count': to_int(total_row.get('kd_total'), 0)}
    for index, metric in enumerate(metrics):
        result['totals'][_metric_name(metric)] = _metric_value(metric, total_row.get(f'kd_m{index}'))
    result['stats'] = totals['stats']
    if not group_by:
        result['raw'] = [((), None, [total_row.get(f'kd_m{index}') for index in range(len(metrics))])]
        return result
    raw_rows = []
    for row in ch_rows(grouped) if grouped else []:
        keys = tuple(row.get(f'kd_g{index}') for index in range(len(fields)))
        raw_rows.append((keys, to_int(row.get('kd_t'), None) if 'time' in group_by else None,
                         [row.get(f'kd_m{index}') for index in range(len(metrics))]))
    result['raw'] = raw_rows  # 非时间分组：SQL 已按条数降序
    if 'time' in group_by:
        result['buckets'] = _bucket_starts(start_ms, end_ms, INTERVALS[args['interval']] * 1000)
        result['combos'] = combos if fields else [()]
    return result


def _es_metric(bucket, index, metric, kind):
    if metric['op'] == 'count':
        return bucket.get('doc_count', 0)
    agg = bucket.get(f'm{index}') or {}
    if metric['op'].startswith('p'):
        values = agg.get('values') or {}
        return next(iter(values.values()), None) if values else None
    if metric['op'] in ('min', 'max') and kind == 'date' and agg.get('value') is not None:
        return iso_ms(agg['value'])
    return agg.get('value')


async def _stats_elastic(bound, client, spec, args, start_ms, end_ms):
    plan = DslPlan(bound)
    metrics, group_by = args['metrics'], args['group_by']
    fields = [name for name in group_by if name != 'time']
    has_time = 'time' in group_by
    metric_aggs = {f'm{index}': plan.metric_agg(metric) for index, metric in enumerate(metrics)
                   if metric['op'] != 'count'}
    leaf = dict(metric_aggs)
    if has_time:
        interval_ms = INTERVALS[args['interval']] * 1000
        first = (start_ms + MS_OFFSET) // interval_ms * interval_ms - MS_OFFSET
        histogram = {'date_histogram': {'field': plan.time, 'fixed_interval': args['interval'], 'time_zone': '+08:00',
                                        'min_doc_count': 0, 'extended_bounds': {'min': first, 'max': end_ms - 1}}}
        if leaf:
            histogram['aggs'] = leaf
        leaf = {'kd_t': histogram}
    if has_time:
        sizes = [min(args['top'], TIME_TOP)] + [min(args['top'], 5)] * (len(fields) - 1)
    else:
        sizes, product = [], 1
        for _ in fields:
            size = max(1, min(args['top'], ES_MAX_BUCKETS // product))
            sizes.append(size)
            product *= size
    aggs = leaf
    for level in reversed(range(len(fields))):
        terms = {'terms': {'field': plan.agg_field(fields[level]), 'size': sizes[level]}}
        if aggs:
            terms['aggs'] = aggs
        aggs = {f'g{level}': terms}
    totals_aggs = {f'total_m{index}': plan.metric_agg(metric) for index, metric in enumerate(metrics)
                   if metric['op'] != 'count'}
    body = {'query': plan.query(spec, start_ms, end_ms), 'size': 0, 'track_total_hits': True,
            'aggs': {**(aggs if group_by else {}), **totals_aggs}}
    data = await client.search(bound.target, body)
    aggregations = data.get('aggregations') or {}
    total, _ = total_of(data)
    kinds = {metric.get('field'): plan.info(metric['field'])['kind'] for metric in metrics if metric.get('field')}
    result = {'totals': {'count': total or 0}, 'stats': {'elapsed_ms': data.get('took', data.get('_elapsed_ms'))}}
    top_level = {f'm{index}': aggregations.get(f'total_m{index}') for index in range(len(metrics))}
    top_level['doc_count'] = total or 0
    for index, metric in enumerate(metrics):
        result['totals'][_metric_name(metric)] = _metric_value(
            metric, _es_metric(top_level, index, metric, kinds.get(metric.get('field'))))
    if not group_by:
        result['raw'] = [((), None, [_es_metric(top_level, index, metric, kinds.get(metric.get('field')))
                                     for index, metric in enumerate(metrics)])]
        return result
    field_kinds = [plan.info(name)['kind'] for name in fields]
    raw_rows = []

    def walk(node, level, keys):
        if level < len(fields):
            for bucket in (node.get(f'g{level}') or {}).get('buckets') or []:
                key = bucket.get('key_as_string') if field_kinds[level] in ('bool', 'date') and \
                    'key_as_string' in bucket else bucket.get('key')
                walk(bucket, level + 1, keys + (key,))
        elif has_time:
            for bucket in (node.get('kd_t') or {}).get('buckets') or []:
                raw_rows.append((keys, to_int(bucket.get('key'), None), bucket))
        else:
            raw_rows.append((keys, None, node))

    walk(aggregations, 0, ())
    values = [(keys, millis, [_es_metric(bucket, index, metric, kinds.get(metric.get('field')))
                              for index, metric in enumerate(metrics)], bucket.get('doc_count', 0))
              for keys, millis, bucket in raw_rows]
    if not has_time:
        values.sort(key=lambda item: -item[3])
        result['raw'] = [item[:3] for item in values]
        return result
    result['raw'] = [item[:3] for item in values]
    result['buckets'] = sorted({item[1] for item in values if item[1] is not None})
    combos = []
    for keys, _, _, _ in values:
        if keys not in combos:
            combos.append(keys)
    result['combos'] = combos if fields else [()]
    return result


def _assemble_stats(bound, args, result):
    """原始分组结果 → (列名, 行)：时间分组按「桶 × 字段取值」补齐空桶，非时间分组取前 top 个"""
    group_by, metrics = args['group_by'], args['metrics']
    fields = [name for name in group_by if name != 'time']
    columns = [*group_by, *[_metric_name(metric) for metric in metrics]]

    def output(keys, millis, values):
        mapping = dict(zip(fields, keys))
        row = []
        for name in group_by:
            if name == 'time':
                row.append(iso_ms(millis) if millis is not None else None)
            else:
                row.append(clip_value(bound.masker.value(name, mapping.get(name)), 300, 300))
        row.extend(_metric_value(metric, value) for metric, value in zip(metrics, values))
        return row

    raw_rows = result['raw']
    if not group_by:
        return columns, [output((), None, raw_rows[0][2])] if raw_rows else []
    if 'time' not in group_by:
        return columns, [output(keys, None, values) for keys, _, values in raw_rows[:args['top']]]
    by_key = {(tuple(keys), millis): values for keys, millis, values in raw_rows}
    rows = []
    for millis in result['buckets']:
        for keys in result['combos']:
            values = by_key.get((tuple(keys), millis))
            if values is None:
                values = [_empty_metric(metric) for metric in metrics]
            rows.append(output(tuple(keys), millis, values))
    return columns, rows


async def stats(ctx, body):
    bound, client = await _open(ctx, source_id(body))
    spec = parse_spec(body, bound, STATS_KEYS)
    args = parse_stats_args(body, bound, spec)
    start_ms, end_ms = _ms_window(spec['start'], spec['end'])
    if bound.type == 'clickhouse':
        result = await _stats_clickhouse(bound, client, spec, args, start_ms, end_ms)
    else:
        result = await _stats_elastic(bound, client, spec, args, start_ms, end_ms)
    columns, rows = _assemble_stats(bound, args, result)
    data = {'source': bound.id, 'window': window_text(spec['start'], spec['end']), 'conditions': describe(bound, spec),
            'group_by': args['group_by'], 'columns': columns, 'rows': rows, 'totals': result['totals'],
            'truncated': False, 'stats': result['stats']}
    if args['interval']:
        data['interval'] = args['interval']
        if [name for name in args['group_by'] if name != 'time']:
            data['notes'] = [f'按时间和字段分组时只统计字段取值最多的 {min(args["top"], TIME_TOP)} 个']
    duration = bound.semantic('duration')
    if duration and any(metric.get('field') == duration for metric in args['metrics']):
        data['duration_unit'] = bound.source['duration_unit']
    return fit_rows(data, hint=HINT_STATS, count_key='')


# ---------------------------------------------------------------------------
# patterns
# ---------------------------------------------------------------------------
async def _pattern_rows(bound, client, spec, start_ms, end_ms, message, level, sample):
    """最新 sample 条的 (毫秒, 正文, 级别) 和窗口内总条数。ClickHouse 只取正文前 1000 字；ES 的正文只从 _source 取一次"""
    if bound.type == 'clickhouse':
        plan = SqlPlan(bound)
        where = plan.where(spec, start_ms, end_ms)
        level_sql = f', toString({column(level)}) AS kd_lv' if level else ''
        sql = (f'SELECT {plan.ms} AS kd_ms, substringUTF8(toString({column(message)}), 1, {PATTERN_CHARS}) AS kd_msg'
               f'{level_sql}\nFROM {plan.table}\nWHERE {where}\nORDER BY {plan.time} DESC\n'
               f'LIMIT {plan.params.add("limit", "UInt32", sample)}')
        count_sql = f'SELECT count() AS kd_count\nFROM {plan.table}\nWHERE {where}'
        result, counted = await asyncio.gather(client.query(sql, plan.params), client.query(count_sql, plan.params))
        items = [(to_int(row.get('kd_ms'), None), row.get('kd_msg'), row.get('kd_lv')) for row in ch_rows(result)]
        return items, to_int((counted['data'] or [[0]])[0][0], 0)
    plan = DslPlan(bound)
    body = plan.search_body(spec, start_ms, end_ms, [message] + ([level] if level else []), sample, track_total=True)
    body['fields'] = plan.time_fields()
    data = await client.search(bound.target, body)
    items = []
    for hit in (data.get('hits') or {}).get('hits') or []:
        values = flatten(hit.get('_source') or {})
        items.append((es_time(hit, bound.time_field), values.get(message), values.get(level) if level else None))
    return items, total_of(data)[0]


async def patterns(ctx, body):
    bound, client = await _open(ctx, source_id(body))
    spec = parse_spec(body, bound, PATTERN_KEYS)
    message = need_semantic(bound, 'message', '归类正文')
    spec['exclude'] = parse_exclude(body)
    sample = int_param(body, 'sample', 100, 5000, 2000)
    top = int_param(body, 'top', 1, 50, 20)
    level = bound.semantic('level') if bound.has(bound.semantic('level')) else ''
    level = '' if level == message else level
    start_ms, end_ms = _ms_window(spec['start'], spec['end'])
    notes = []
    try:
        items, total = await _pattern_rows(bound, client, spec, start_ms, end_ms, message, level, sample)
    except ResponseTooLarge:
        if sample <= 100:
            raise
        smaller = max(100, sample // 5)
        items, total = await _pattern_rows(bound, client, spec, start_ms, end_ms, message, level, smaller)
        notes.append(f'正文太大（响应超过 16MB），样本从 {sample} 条减到 {smaller} 条')

    def work():
        samples = []
        for millis, text, lvl in items:
            text = '' if text is None else bound.masker.value(message, text if isinstance(text, str) else str(text))
            samples.append((millis, text if isinstance(text, str) else str(text), lvl))
        return pattern_mod.group(samples, total, top)

    groups = await asyncio.to_thread(work) if len(items) > 1000 else work()
    data = {'source': bound.id, 'window': window_text(spec['start'], spec['end']), 'conditions': describe(bound, spec),
            'sample': {'size': len(items), 'total': total, 'method': 'latest'}, 'patterns': groups,
            'excluded': spec['exclude'], 'truncated': False}
    if notes:
        data['notes'] = notes
    return fit_rows(data, 'patterns', '结果超过 60000 字，后面的模式没有返回：减小 top 或用 exclude 排除已知的模式',
                    count_key='')


# ---------------------------------------------------------------------------
# context
# ---------------------------------------------------------------------------
async def context(ctx, body):
    check_keys(body, CONTEXT_KEYS, PARAM_HINTS)
    bound, client = await _open(ctx, source_id(body))
    if body.get('time') in (None, ''):
        raise invalid('time 必填（锚点日志的时间，北京时间，最好带毫秒）')
    anchor = parse_time(body.get('time'), 'time')
    pod_field = bound.semantic('pod')
    pod = text_param(body, 'k8s_pod', 253)
    if pod_field:
        field_schema.require(bound.schema, pod_field, 'Pod', bound.id)
        if not pod:
            raise invalid(f'日志源 {bound.id} 有 Pod 字段，log_context 要带 k8s_pod（锚点日志所在的 Pod）')
    elif pod:
        raise invalid(f'日志源 {bound.id} 没有配置 Pod 字段，不能按 k8s_pod 过滤；用 filters 限定同一个来源')
    anchor_id = text_param(body, 'anchor_id', 512)
    before = int_param(body, 'before', 0, 100, 20)
    after = int_param(body, 'after', 0, 100, 20)
    spec = {'filters': parse_filters(body.get('filters'), bound), 'k8s': [], 'level': None, 'keyword': '',
            'exclude': []}
    if pod:
        spec['k8s'].append({'param': 'k8s_pod', 'field': pod_field, 'value': pod})
    at_ms = int(round(anchor * 1000))
    output = default_output(bound)
    names = fetch_list(bound, output) if output else []
    tasks = [fetch(bound, client, spec, at_ms - CONTEXT_WINDOW_MS, at_ms + 1, names, before + 1, 'desc')]
    if after:
        tasks.append(fetch(bound, client, spec, at_ms + 1, at_ms + 1 + CONTEXT_WINDOW_MS, names, after, 'asc'))
    results = await asyncio.gather(*tasks)
    items = list(reversed(results[0]['items'])) + (results[1]['items'] if after else [])
    rows = _rows(bound, items, output)
    marked = False
    if anchor_id:
        for row, item in zip(rows, items):
            if item[2] == anchor_id:
                row['anchor'] = marked = True
    if not marked:
        for row, item in zip(rows, items):
            if item[0] == at_ms:
                row['anchor'] = True
    data = {'source': bound.id, 'anchor': {'time': iso_ms(at_ms), 'pod': pod or None}, 'rows': rows,
            'returned': len(rows), 'truncated': False}
    return fit_rows(data, hint='结果超过 60000 字，后面的行没有返回：减小 before / after')


# ---------------------------------------------------------------------------
# correlate
# ---------------------------------------------------------------------------
def summary(bound, values):
    """时间线上的一行摘要（≤ 200 字，已打码）"""
    def get(key):
        name = bound.semantic(key)
        value = values.get(name) if name else None
        if value in (None, '') or isinstance(value, (dict, list)):
            return ''
        return str(bound.masker.value(name, str(value)))

    kind = bound.source['kind']
    if kind == 'access':
        duration = get('duration')
        unit = bound.source['duration_unit'] if duration else ''
        text = f'{get("method")} {get("host")}{get("path")} {get("status")} {duration}{unit}'
    elif kind == 'app':
        text = f'{get("level")} {get("pod")} {first_line(get("message"), SUMMARY_CHARS)}'
    else:
        parts = [get(key) for key in ('level', 'service', 'pod', 'method', 'path', 'status')]
        text = ' '.join(part for part in parts if part) or first_line(get('message'), SUMMARY_CHARS)
    text = ' '.join(text.split())
    return text if len(text) <= SUMMARY_CHARS else text[:SUMMARY_CHARS - 1] + '…'


def _correlate_value(body, key):
    value = body.get('value')
    if not isinstance(value, str) or not value.strip():
        raise invalid('value 必填（关联字段的取值）')
    value = value.strip()
    if key['pattern']:
        if not re.fullmatch(key['pattern'], value):
            raise invalid(f'value 不符合关联字段 {key["key"]} 的取值格式 {key["pattern"]}')
    elif len(value) > 200 or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise invalid('value 最长 200 个字符，不能换行')
    return value


def _trim_correlate(data):
    """整个结果超过 60000 字：先缩短时间线，再按同一个上限减少每个日志源的行"""
    if size_of(data) <= MAX_DATA_CHARS:
        return data
    data['truncated'] = True
    data['hint'] = '结果超过 60000 字，部分行没有返回：用 sources 只查几个日志源，或减小 limit'
    full = [list(item['rows']) for item in data['sources']]
    timeline = list(data['timeline'])
    low, high = 1, max([len(rows) for rows in full] + [1])
    while low < high:
        middle = (low + high + 1) // 2
        for item, rows in zip(data['sources'], full):
            item['rows'] = rows[:middle]
        if size_of(data) <= MAX_DATA_CHARS:
            low = middle
        else:
            high = middle - 1
    for item, rows in zip(data['sources'], full):
        item['rows'] = rows[:low]
        if len(rows) > low:
            item['truncated'] = True
    while size_of(data) > MAX_DATA_CHARS and len(data['timeline']) > 1:
        data['timeline'] = data['timeline'][:max(1, len(data['timeline']) // 2)]
    if len(data['timeline']) < len(timeline):
        data['timeline_truncated'] = True
    return data


async def correlate(ctx, body):
    check_keys(body, CORRELATE_KEYS, PARAM_HINTS)
    key_name = text_param(body, 'key', 32, required=True)
    key = ctx.keys.get(key_name)
    if key is None:
        names = '、'.join(ctx.keys) or '（还没有配置）'
        raise invalid(f'没有关联字段 {key_name}，可选：{names}')
    value = _correlate_value(body, key)
    if body.get('around') not in (None, ''):
        if any(body.get(name) not in (None, '') for name in TIME_KEYS):
            raise invalid('around 和 start / end / lookback 不能同时给')
        moment = parse_time(body['around'], 'around')
        start, end = moment - 1800, moment + 1800
    else:
        start, end = resolve_window(body, '24h')
    limit = int_param(body, 'limit', 1, 100, 20)
    candidates = [source for source in ctx.bound_sources()
                  if any(item['key'] == key_name for item in source['correlations'])]
    wanted = list_param(body, 'sources', 20)
    if wanted:
        ids = [source['id'] for source in candidates]
        unknown = [str(item) for item in wanted if item not in ids]
        if unknown:
            raise invalid(f'这些日志源在当前集群没有登记关联字段 {key_name}：{"、".join(unknown[:10])}；'
                          f'可选：{"、".join(ids) or "（无）"}')
        candidates = [source for source in candidates if source['id'] in wanted]
    if not candidates:
        raise invalid(f'集群 {ctx.env} 没有日志源登记关联字段 {key_name}（用 log_sources 查看）')
    start_ms, end_ms = _ms_window(start, end)

    async def one(source):
        entry = {'source': source['id'], 'name': source['name'], 'kind': source['kind'], 'count': 0, 'rows': [],
                 'truncated': False, 'error': None}
        bound = None
        try:
            bound, client = await _open(ctx, source['id'])
            corr = bound.correlation(key_name)
            spec = {'filters': [], 'k8s': [], 'level': None, 'keyword': '', 'exclude': []}
            if 'field' in corr:
                field_schema.require(bound.schema, corr['field'], '关联', bound.id)
                spec['filters'].append({'field': corr['field'], 'op': 'eq', 'value': value})
            else:
                spec['keyword'] = corr['text'].replace('{value}', value)
                spec['message'] = need_semantic(bound, 'message', '按正文写法关联')
            output = default_output(bound)
            result = await fetch(bound, client, spec, start_ms, end_ms, fetch_list(bound, output) if output else [],
                                 limit + 1, 'asc')
            items = result['items']
            entry['truncated'] = len(items) > limit
            items = items[:limit]
            entry['rows'] = _rows(bound, items, output)
            entry['count'] = result['total'] if result['total'] is not None else len(items)
            entry['_timeline'] = [{'@time': iso_ms(millis) if millis is not None else None, 'source': bound.id,
                                   'summary': summary(bound, values), '_ms': millis or 0}
                                  for millis, values, _ in items]
        except LogError as exc:
            entry['error'] = {'code': exc.code, 'message': bound.timeout_hint(exc) if bound else exc.message}
        return entry

    results = await asyncio.gather(*[one(source) for source in candidates])
    timeline = sorted((item for entry in results for item in entry.pop('_timeline', [])),
                      key=lambda item: item['_ms'])
    for item in timeline:
        item.pop('_ms', None)
    data = {
        'key': key_name, 'value': value, 'window': window_text(start, end), 'sources': list(results),
        'timeline': timeline[:TIMELINE_ITEMS],
        'missing': [entry['source'] for entry in results if entry['error'] is None and not entry['rows']],
    }
    if len(timeline) > TIMELINE_ITEMS:
        data['timeline_truncated'] = True
    if key['trace']:
        tracing = await _tracing_enabled()
        data['trace'] = {'trace_id': value}
        if tracing:
            data['trace']['url'] = tracing_config.trace_url(tracing, value)
    return _trim_correlate(data)


# ---------------------------------------------------------------------------
# query（原生查询）
# ---------------------------------------------------------------------------
async def query(ctx, body):
    check_keys(body, QUERY_KEYS, QUERY_HINTS)
    bound = ctx.bound(source_id(body))
    if not bound.conn['allow_raw']:
        raise LogError(403, f'连接 {bound.conn["id"]} 没有允许原生查询：用 log_search / log_stats / log_patterns',
                       'raw_not_allowed')
    given = [name for name in ('sql', 'dsl', 'esql') if body.get(name) not in (None, '', {})]
    if len(given) != 1:
        raise invalid('sql / dsl / esql 三选一（按日志源类型：ClickHouse 用 sql，Elasticsearch 用 dsl 或 esql）')
    language = given[0]
    if bound.type == 'clickhouse' and language != 'sql':
        raise invalid(f'日志源 {bound.id} 是 ClickHouse，原生查询用 sql')
    if bound.type == 'elasticsearch' and language == 'sql':
        raise invalid(f'日志源 {bound.id} 是 Elasticsearch，原生查询用 dsl 或 esql')
    limit = int_param(body, 'limit', 1, 500, 100)
    start, end = resolve_window(body)
    start_ms, end_ms = _ms_window(start, end)
    client = ctx.client(bound)
    if language == 'esql':
        info = await client.info()
        if not info['esql']:
            raise LogError(409, f'连接 {bound.conn["id"]} 的 {info["distribution"]} {info["version"]} 不支持 ES|QL'
                                '（要 Elasticsearch 8.14 及以上，OpenSearch 不支持）；用 dsl', 'version_unsupported')
    await ctx.load_schema(bound, client)
    data = {'source': bound.id, 'window': window_text(start, end), 'language': language}
    if language == 'sql':
        plan = SqlPlan(bound)
        result, final = await raw.run_sql(client, plan, body['sql'], start_ms, end_ms, limit)
        columns, rows, more = raw.result_rows(result, limit)
        data.update(columns=columns, rows=[[clip_value(bound.masker.value(name, value)) for name, value in
                                            zip(columns, row)] for row in rows],
                    returned=len(rows), truncated=more, stats=result['stats'], query=final)
    elif language == 'dsl':
        plan = DslPlan(bound)
        dsl = raw.check_dsl(body['dsl'], limit)
        request = raw.wrap_dsl(dsl, plan.base_filter(start_ms, end_ms))
        request['fields'] = plan.time_fields()
        result = await client.search(bound.target, request)
        hits = (result.get('hits') or {}).get('hits') or []
        rows = []
        for hit in hits:
            values = flatten(hit.get('_source') or {})
            rows.append(make_row(bound, es_time(hit, bound.time_field), values, [], True, {'_id': hit.get('_id')}))
        total, relation = total_of(result)
        aggregations, cut = raw.trim_json(raw.mask_tree(bound.masker, result.get('aggregations') or {}), 30000)
        data.update(total=total, total_relation=relation, rows=rows, returned=len(rows),
                    aggregations=aggregations, truncated=cut,
                    stats={'elapsed_ms': result.get('took', result.get('_elapsed_ms'))})
    else:
        plan = DslPlan(bound)
        pipeline = raw.check_esql(body['esql'])
        text = raw.esql_query(bound.target, pipeline, limit)
        result = await client.esql(text, plan.base_filter(start_ms, end_ms))
        columns = [str(item.get('name')) for item in result.get('columns') or [] if isinstance(item, dict)]
        values = result.get('values') or []
        data.update(columns=columns, rows=[[clip_value(bound.masker.value(name, value)) for name, value in
                                            zip(columns, row)] for row in values[:limit]],
                    returned=min(len(values), limit), truncated=len(values) > limit,
                    stats={'elapsed_ms': result.get('took', result.get('_elapsed_ms'))}, query=text)
    return fit_rows(data, hint=HINT_ROWS)


OPS = {
    'sources': sources,
    'search': search,
    'stats': stats,
    'patterns': patterns,
    'context': context,
    'correlate': correlate,
    'query': query,
}
