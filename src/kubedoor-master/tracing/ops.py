"""服务、接口、取链路、看 Span、字段（约定 §5.1、§5.2、§5.5~§5.7），以及管理页的识别字段和测试连接（§3）。"""

import asyncio
import re
import time

from . import analysis, model
from .clickhouse import DIM_COLUMNS, SEARCH_COLUMNS, ClickHouseClient, Params, rows as ch_rows, to_int
from .common import TTLCache, TracingError, has_control, invalid, iso_ns, ms1, now, parse_lookback, parse_time
from .fields import FIELD_DEFAULTS, resolve_cluster
from .filters import attr, call, combine, field, scalar
from .jaeger import JaegerClient, capabilities_problem
from .search import recall

SERVICES_DEFAULT, SERVICES_MAX = 200, 500
MAX_PATTERN = 100
DIM_ROWS = 5000
TRACE_CACHE = TTLCache(max_items=20, ttl=300, max_bytes=200 * 1024 * 1024)
LARGE_JSON = 1024 * 1024
MAX_SPAN_IDS = 20
VALUE_CHARS = 2000
STACK_HEAD, STACK_TAIL = 3000, 800
MAX_KEYS = 500
EXAMPLE_CHARS = 80
_TRACE_ID = re.compile(r'[0-9a-fA-F]{1,32}')
_SPAN_ID = re.compile(r'[0-9a-fA-F]{1,16}')
_LEVEL_ORDER = {'resource': 0, 'span': 1, 'event': 2, 'scope': 3}


def reset_caches():
    TRACE_CACHE.clear()


def _check_keys(body, allowed):
    if not isinstance(body, dict):
        raise invalid('参数要是 JSON 对象')
    unknown = sorted(key for key in body if key not in allowed)
    if unknown:
        raise invalid(f'不支持的参数：{"、".join(unknown[:10])}')


def _text(body, key, max_len=MAX_PATTERN):
    value = body.get(key)
    if value is None:
        return ''
    if not isinstance(value, str) or len(value.strip()) > max_len or has_control(value):
        raise invalid(f'{key} 要是不超过 {max_len} 个字符的字符串')
    return value.strip()


def _limit(body, key, low, high, default):
    value = body.get(key)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise invalid(f'{key} 要是 {low}~{high} 的整数')
    return value


def trace_id_of(value):
    text = value.strip() if isinstance(value, str) else ''
    if not _TRACE_ID.fullmatch(text):
        raise invalid('trace_id 要是 1~32 位十六进制')
    return text.lower().rjust(32, '0')


def span_id_of(value, name='span_id'):
    text = value.strip() if isinstance(value, str) else ''
    if not _SPAN_ID.fullmatch(text):
        raise invalid(f'{name} 要是 1~16 位十六进制')
    return text.lower().rjust(16, '0')


# ---------------------------------------------------------------------------
# 服务、接口
# ---------------------------------------------------------------------------
async def services(ctx, body):
    _check_keys(body, {'pattern', 'limit'})
    pattern = _text(body, 'pattern')
    limit = _limit(body, 'limit', 1, SERVICES_MAX, SERVICES_DEFAULT)
    if ctx.has_dim:
        client = ctx.clickhouse
        params = Params()
        conds = [f'bucket >= fromUnixTimestamp64Milli({params.add("s", "Int64", int((now() - 86400) * 1000))})']
        if ctx.cluster_key:
            conds.append(f'k8s_cluster IN {params.add("clusters", "Array(String)", ctx.cluster_values)}')
        if pattern:
            conds.append(f'positionCaseInsensitive(service_name, {params.add("p", "String", pattern)}) > 0')
        sql = (f'SELECT service_name, sum(spans) AS spans\nFROM {client.table("dim_table")}\nWHERE '
               + '\n  AND '.join(conds) + f'\nGROUP BY service_name\nORDER BY spans DESC\nLIMIT {DIM_ROWS}')
        items = [{'name': row.get('service_name') or '', 'spans_24h': to_int(row.get('spans'))}
                 for row in ch_rows(await client.query(sql, params))]
        return {'source': 'clickhouse', 'services': items[:limit], 'total': len(items),
                'truncated': len(items) > limit, 'cluster': ctx.cluster_info(),
                'note': '最近 24 小时当前集群有 Span 的服务，按 Span 数排序'}
    names = await ctx.jaeger.services()
    if pattern:
        names = [name for name in names if pattern.lower() in name.lower()]
    return {'source': 'jaeger', 'services': [{'name': name} for name in names[:limit]], 'total': len(names),
            'truncated': len(names) > limit, 'note': 'Jaeger 的服务列表是所有集群的'}


async def operations(ctx, body):
    _check_keys(body, {'service', 'span_kind', 'pattern', 'limit', 'lookback'})
    service = _text(body, 'service', 2048)
    if not service:
        raise invalid('service 必填')
    kind = _text(body, 'span_kind').lower()
    if kind and kind not in ('server', 'client', 'producer', 'consumer', 'internal'):
        raise invalid('span_kind 只能是 server / client / producer / consumer / internal')
    pattern = _text(body, 'pattern')
    limit = _limit(body, 'limit', 1, SERVICES_MAX, SERVICES_DEFAULT)
    lookback = body.get('lookback')
    if ctx.has_dim:
        seconds = parse_lookback(lookback) if lookback not in (None, '') else 86400
        client = ctx.clickhouse
        params = Params()
        conds = [f'bucket >= fromUnixTimestamp64Milli({params.add("s", "Int64", int((now() - seconds) * 1000))})']
        if ctx.cluster_key:
            conds.append(f'k8s_cluster IN {params.add("clusters", "Array(String)", ctx.cluster_values)}')
        conds.append(f'service_name = {params.add("service", "String", service)}')
        if kind:
            conds.append(f'kind = {params.add("kind", "String", kind)}')
        if pattern:
            placeholder = params.add('p', 'String', pattern)
            conds.append(f'(positionCaseInsensitive(operation, {placeholder}) > 0 '
                         f'OR positionCaseInsensitive(url_path, {placeholder}) > 0)')
        sql = (f'SELECT kind, operation, url_path, sum(spans) AS spans\nFROM {client.table("dim_table")}\nWHERE '
               + '\n  AND '.join(conds) + f'\nGROUP BY kind, operation, url_path\nORDER BY spans DESC\nLIMIT {DIM_ROWS}')
        items = [{'name': row.get('operation') or '', 'kind': row.get('kind') or 'unspecified',
                  'path': ctx.masker.url(row.get('url_path') or ''), 'spans': to_int(row.get('spans'))}
                 for row in ch_rows(await client.query(sql, params))]
        return {'source': 'clickhouse', 'service': service, 'operations': items[:limit], 'total': len(items),
                'truncated': len(items) > limit, 'note': f'最近 {lookback or "24h"} 当前集群的接口，按 Span 数排序'}
    items = await ctx.jaeger.operations(service, kind)
    if pattern:
        items = [item for item in items if pattern.lower() in item['name'].lower()]
    items.sort(key=lambda item: (item['kind'], item['name']))
    data = {'source': 'jaeger', 'service': service, 'operations': items[:limit], 'total': len(items),
            'truncated': len(items) > limit}
    if lookback not in (None, ''):
        data['note'] = 'lookback 只对 ClickHouse 加速有效，Jaeger 的接口列表不分时间'
    return data


# ---------------------------------------------------------------------------
# 按 ID 取链路
# ---------------------------------------------------------------------------
async def _parse(raw):
    if len(raw) > LARGE_JSON:
        return await asyncio.to_thread(model.parse_text, raw)
    return model.parse_text(raw)


def time_ranges(trace_id, around):
    """按顺序试的时间范围：(说明, 开始秒, 结束秒)；最后一个不带时间范围"""
    ranges = []
    remembered = recall(trace_id)
    if remembered:
        center = remembered / 1000
        ranges.append(('搜索结果里记下的时间 ±10 分钟', center - 600, center + 600))
    if around is not None:
        ranges.append(('around ±30 分钟', around - 1800, around + 1800))
        ranges.append(('around ±6 小时', around - 6 * 3600, around + 6 * 3600))
    current = now()
    ranges.append(('最近 1 小时', current - 3600, current))
    ranges.append(('最近 24 小时', current - 86400, current))
    ranges.append(('最近 7 天', current - 7 * 86400, current))
    ranges.append(('不限时间', None, None))
    return ranges


async def fetch_trace(ctx, trace_id, around=None):
    """按 ID 取链路（逐级放大时间范围，404 换下一个，其他错误直接失败），返回 (Span 列表, 用的时间范围说明)"""
    key = (ctx.jaeger.key, trace_id)
    cached = TRACE_CACHE.get(key)
    if cached is not None:
        return cached
    for label, start, end in time_ranges(trace_id, around):
        timeout = 60 if start is None else None
        raw = await ctx.jaeger.trace(trace_id, start, end, timeout=timeout)
        if raw is None:
            continue
        try:
            traces = await _parse(raw)
        except ValueError as exc:
            raise TracingError(502, f'Jaeger 返回的链路无法解析：{exc}', 'jaeger_unavailable') from None
        spans = next((trace.spans for trace in traces if trace.trace_id == trace_id), None)
        if spans is None:
            spans = [span for trace in traces for span in trace.spans]
        if not spans:
            continue
        result = (spans, label)
        TRACE_CACHE.put(key, result, size=len(raw) * 3)
        return result
    raise TracingError(404, f'在 Jaeger 里没有找到链路 {trace_id}（可能已过期、没被采样，或者 ID 不对）', 'not_found')


def _derive(ctx, spans):
    return model.derive(spans, ctx.fields, ctx.cluster_key, ctx.lookup)


async def trace(ctx, body):
    _check_keys(body, {'trace_id', 'around', 'max_nodes', 'focus_span'})
    trace_id = trace_id_of(body.get('trace_id'))
    around = parse_time(body['around'], 'around') if body.get('around') not in (None, '') else None
    max_nodes = _limit(body, 'max_nodes', 50, 400, 200)
    focus = span_id_of(body['focus_span'], 'focus_span') if body.get('focus_span') not in (None, '') else ''
    spans, label = await fetch_trace(ctx, trace_id, around)
    extra = {'url': ctx.trace_url(trace_id), 'time_range_used': label}

    def work():
        _derive(ctx, spans)
        return analysis.analyze(spans, trace_id, max_nodes=max_nodes, focus_span=focus, extra=extra)

    data = await asyncio.to_thread(work)
    if focus:
        data['focus_span'] = focus
    return data


# ---------------------------------------------------------------------------
# 看 Span 详情
# ---------------------------------------------------------------------------
def clip_value(key, value):
    """属性值超过 2000 字截断；exception.stacktrace 保留开头 3000 字 + 结尾 800 字"""
    if isinstance(value, str):
        if key == 'exception.stacktrace' and len(value) > STACK_HEAD + STACK_TAIL:
            return f'{value[:STACK_HEAD]}\n…（中间省略 {len(value) - STACK_HEAD - STACK_TAIL} 字）…\n{value[-STACK_TAIL:]}'
        if key != 'exception.stacktrace' and len(value) > VALUE_CHARS:
            return f'{value[:VALUE_CHARS]}…（截断，原长 {len(value)} 字）'
        return value
    if isinstance(value, list):
        return [clip_value(key, item) for item in value]
    if isinstance(value, dict):
        return {name: clip_value(name, item) for name, item in value.items()}
    return value


def _attrs(ctx, attrs):
    return {key: clip_value(key, value) for key, value in ctx.masker.attrs(attrs).items()}


def span_detail(ctx, span):
    return {
        'span_id': span.span_id, 'parent_id': span.parent_id, 'service': span.service, 'name': span.name,
        'kind': span.kind, 'start': iso_ns(span.start_ns), 'duration_ms': ms1(span.duration_ns / 1e6),
        'status': span.status, 'status_message': span.status_message, 'cluster': span.cluster,
        'attributes': _attrs(ctx, span.attrs), 'resource': _attrs(ctx, span.resource),
        'events': [{'name': event['name'], 'time': iso_ns(event['time_ns']) if event['time_ns'] else None,
                    'attributes': _attrs(ctx, event['attrs'])} for event in span.events],
        'links': [{'trace_id': link['trace_id'], 'span_id': link['span_id']} for link in span.links],
        'warnings': list(span.warnings),
    }


async def spans_op(ctx, body):
    _check_keys(body, {'trace_id', 'span_ids', 'errors', 'around'})
    trace_id = trace_id_of(body.get('trace_id'))
    raw_ids = body.get('span_ids')
    errors = body.get('errors')
    if errors is not None and not isinstance(errors, bool):
        raise invalid('errors 要是 true 或 false')
    if raw_ids not in (None, []):
        if not isinstance(raw_ids, list) or len(raw_ids) > MAX_SPAN_IDS:
            raise invalid(f'span_ids 要是数组，最多 {MAX_SPAN_IDS} 个')
        if errors:
            raise invalid('span_ids 和 errors=true 只能二选一')
        span_ids = list(dict.fromkeys(span_id_of(item, 'span_ids 里的') for item in raw_ids))
    elif errors:
        span_ids = None
    else:
        raise invalid('要给 span_ids（最多 20 个），或者 errors=true 取全部出错的 Span')
    around = parse_time(body['around'], 'around') if body.get('around') not in (None, '') else None
    spans, label = await fetch_trace(ctx, trace_id, around)

    def work():
        _derive(ctx, spans)
        by_id = {}
        for span in spans:
            by_id.setdefault(span.span_id, span)
        if span_ids is None:
            index = analysis.Index(spans)
            chosen = sorted((span for span in index.spans if span.is_error),
                            key=lambda span: (-index.depth.get(span.span_id, 0), span.start_ns))
            total = len(chosen)
            return [span_detail(ctx, span) for span in chosen[:MAX_SPAN_IDS]], [], total > MAX_SPAN_IDS
        found = [span_detail(ctx, by_id[span_id]) for span_id in span_ids if span_id in by_id]
        return found, [span_id for span_id in span_ids if span_id not in by_id], False

    details, missing, truncated = await asyncio.to_thread(work)
    return {'trace_id': trace_id, 'spans': details, 'missing': missing, 'truncated': truncated,
            'time_range_used': label, 'url': ctx.trace_url(trace_id)}


# ---------------------------------------------------------------------------
# 字段：观察链路里实际出现的属性
# ---------------------------------------------------------------------------
def _type_name(value):
    if isinstance(value, bool):
        return 'bool'
    if isinstance(value, int):
        return 'int'
    if isinstance(value, float):
        return 'double'
    if isinstance(value, list):
        return 'array'
    if isinstance(value, dict):
        return 'kvlist'
    return 'string'


def observe_keys(traces, masker, with_types=True):
    """链路里出现过的属性：[{key, level, types, count, examples}]，示例最多 3 个、每个最多 80 字、敏感值打码"""
    stats = {}

    def add(key, level, value):
        item = stats.get((key, level))
        if item is None:
            item = stats[(key, level)] = {'key': key, 'level': level, 'types': set(), 'count': 0, 'examples': []}
        item['count'] += 1
        item['types'].add(_type_name(value))
        if len(item['examples']) < 3 and value not in (None, '', [], {}):
            masked = masker.value(key, value)
            text = masked if isinstance(masked, str) else str(masked)
            text = text if len(text) <= EXAMPLE_CHARS else text[:EXAMPLE_CHARS - 1] + '…'
            if text not in item['examples']:
                item['examples'].append(text)

    for trace in traces:
        for span in trace.spans:
            for key, value in span.attrs.items():
                add(key, 'span', value)
            for key, value in span.resource.items():
                add(key, 'resource', value)
            for event in span.events:
                for key, value in event['attrs'].items():
                    add(key, 'event', value)
            for key, value in (span.scope or {}).get('attrs', {}).items():
                add(key, 'scope', value)
    items = sorted(stats.values(), key=lambda item: (_LEVEL_ORDER.get(item['level'], 9), -item['count'], item['key']))
    for item in items:
        if with_types:
            item['types'] = sorted(item['types'])
        else:
            item.pop('types')
    return items[:MAX_KEYS]


def cluster_candidates(traces, lookup):
    """资源属性里取值能对上 KubeDoor 集群名或别名的键，对上的多的排前面"""
    values = {}
    for trace in traces:
        for span in trace.spans:
            for key, value in span.resource.items():
                if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value):
                    values.setdefault(key, {}).setdefault(str(value), 0)
                    values[key][str(value)] += 1
    result = []
    for key, counter in values.items():
        resolved = [resolve_cluster(value, lookup) for value in sorted(counter, key=lambda v: -counter[v])]
        matched = sorted({item['env'] for item in resolved if item['env']})
        if matched:
            result.append({'key': key, 'values': resolved[:20], 'matched_envs': matched})
    result.sort(key=lambda item: (-len(item['matched_envs']), -len(item['values']), item['key']))
    return result


def suggestions(keys, candidates):
    present = {item['key'] for item in keys}
    data = {'fields': {key: [name for name in names if name in present] for key, names in FIELD_DEFAULTS.items()}}
    if candidates:
        data['cluster_key'] = candidates[0]['key']
    return data


def _sample_filter(ctx, service, with_cluster):
    conditions = [call('eq', field('service', 'resource'), scalar(service))]
    if with_cluster and ctx.cluster_key and ctx.cluster_values:
        conditions.append(combine('or', [call('eq', attr(ctx.cluster_key, 'resource'), scalar(value))
                                         for value in ctx.cluster_values]))
    return combine('and', conditions)['call']


async def sample_traces(ctx, service, seconds, count, with_cluster):
    """在 Jaeger 上按服务取最近的几条完整链路；返回 (链路, 说明)"""
    notes = []
    filter_call = None
    if with_cluster and ctx.cluster_key:
        caps = await ctx.jaeger.capabilities()
        if capabilities_problem(caps):
            notes.append('Jaeger 低于 2.23，取样没有按集群过滤')
        else:
            filter_call = _sample_filter(ctx, service, True)
    end = now()
    params = JaegerClient.search_params(int(end - seconds), int(end) + 1, count, filter_call, service)
    raw = await ctx.jaeger.traces(params)
    try:
        traces = await _parse(raw) if raw else []
    except ValueError as exc:
        raise TracingError(502, f'Jaeger 返回的链路无法解析：{exc}', 'jaeger_unavailable') from None
    return traces[:count], notes


async def fields_op(ctx, body):
    _check_keys(body, {'service'})
    service = _text(body, 'service', 2048)
    data = {'cluster': ctx.cluster_info(), 'fields': ctx.fields, 'custom_fields': ctx.cfg['custom_fields'],
            'clickhouse': ctx.has_clickhouse}
    if not service:
        return data
    traces, notes = await sample_traces(ctx, service, 15 * 60, 2, with_cluster=True)
    if traces:
        keys = await asyncio.to_thread(observe_keys, traces, ctx.masker, False)
        data['observed'] = {'service': service, 'traces': len(traces), 'keys': keys}
    else:
        data['observed'] = None
        notes.append(f'最近 15 分钟没有找到服务 {service} 在当前集群的链路（服务名不对，或者没有流量 / 没被采样）')
    if notes:
        data['notes'] = notes
    return data


# ---------------------------------------------------------------------------
# 管理页：识别字段、测试连接
# ---------------------------------------------------------------------------
def _analyze_traces(traces, ctx):
    keys = observe_keys(traces, ctx.masker)
    candidates = cluster_candidates(traces, ctx.lookup)
    return {'traces': len(traces), 'spans': sum(len(trace.spans) for trace in traces), 'keys': keys,
            'cluster_candidates': candidates, 'suggestions': suggestions(keys, candidates)}


async def analyze_sample(ctx, service, lookback):
    seconds = parse_lookback(lookback or '15m')
    traces, notes = await sample_traces(ctx, service, seconds, 3, with_cluster=False)
    data = await asyncio.to_thread(_analyze_traces, traces, ctx)
    if not traces:
        notes.append(f'最近 {lookback or "15m"} 在 Jaeger 上没有找到服务 {service} 的链路')
    data['notes'] = notes
    return data


async def analyze_json(ctx, text):
    def work():
        traces = model.parse_text(text)
        return _analyze_traces(traces, ctx)

    try:
        data = await asyncio.to_thread(work)
    except ValueError as exc:
        raise invalid(str(exc)) from None
    if not data['traces']:
        raise invalid('JSON 里没有找到链路（Span）')
    data['notes'] = []
    return data


async def test_jaeger(cfg, session):
    started = time.monotonic()
    result = {'ok': False, 'elapsed_ms': 0, 'services_count': 0, 'services': [], 'capabilities': None,
              'search_ok': False, 'messages': []}
    messages = result['messages']
    try:
        client = JaegerClient(cfg, session)
        names = await client.services(use_cache=False)
        result.update(services_count=len(names), services=names[:1000])
        messages.append(f'连接成功，共 {len(names)} 个服务（所有集群的）')
        caps = await client.capabilities(use_cache=False)
        result['capabilities'] = caps
        problem = capabilities_problem(caps)
        result['search_ok'] = not problem
        if problem:
            messages.append(f'{problem}：不能搜索链路（搜索需要 Jaeger 2.23 及以上）；服务列表、接口列表和按 Trace ID 取链路照常')
        else:
            search_caps = caps.get('search') or {}
            flt = search_caps.get('filter') or {}
            messages.append('支持结构化搜索（Jaeger 2.23 及以上）')
            messages.append(f'不填服务名搜索：{"支持" if search_caps.get("withoutServiceName") else "不支持"}；'
                            f'运算符：{"、".join(flt.get("operators") or [])}；层级：{"、".join(flt.get("levels") or [])}')
        result['ok'] = True
    except TracingError as exc:
        result['error'] = exc.message
    result['elapsed_ms'] = int((time.monotonic() - started) * 1000)
    return result


async def test_clickhouse(cfg, session, lookup):
    started = time.monotonic()
    result = {'ok': False, 'elapsed_ms': 0, 'version': '', 'missing_columns': {}, 'cluster_values': [],
              'messages': []}
    messages = result['messages']
    try:
        client = ClickHouseClient(cfg, session)
        version = await client.query('SELECT version() AS version')
        result['version'] = str((ch_rows(version) or [{}])[0].get('version') or '')
        messages.append(f'连接成功，ClickHouse {result["version"]}')
        tables = [('table', SEARCH_COLUMNS)]
        if cfg['clickhouse']['dim_table']:
            tables.append(('dim_table', DIM_COLUMNS))
        else:
            messages.append('没有填汇总表：服务列表、接口列表改用 Jaeger 接口')
        columns = {}
        for which, required in tables:
            described = await client.query(f'DESCRIBE TABLE {client.table(which)}')
            names = {str(row[0]) for row in described['data'] if row}
            columns[which] = names
            missing = [name for name in required if name not in names]
            if missing:
                result['missing_columns'][cfg['clickhouse'][which]] = missing
        for table, missing in result['missing_columns'].items():
            messages.append(f'表 {table} 缺少列：{"、".join(missing)}（建表语句见帮助文档）')
        if {'k8s_cluster', 'start_time'} <= columns.get('table', set()):
            params = Params()
            sql = (f'SELECT k8s_cluster, count() AS spans\nFROM {client.table()}\n'
                   f'WHERE start_time >= fromUnixTimestamp64Milli({params.add("s", "Int64", int((now() - 600) * 1000))})'
                   '\nGROUP BY k8s_cluster\nORDER BY spans DESC\nLIMIT 100')
            values = [{'value': row.get('k8s_cluster') or '', 'spans': to_int(row.get('spans')),
                       'env': resolve_cluster(row.get('k8s_cluster') or '', lookup)['env']}
                      for row in ch_rows(await client.query(sql, params))]
            result['cluster_values'] = values
            if not values:
                messages.append('最近 10 分钟搜索表里没有数据：检查物化视图是否在写入')
            unmatched = [item['value'] for item in values if not item['env']]
            if unmatched:
                messages.append(f'这些集群取值对不上 KubeDoor 的集群名或别名：{"、".join(unmatched[:10])}'
                                '（可以在 Agent 管理页加别名）')
        result['ok'] = not result['missing_columns']
    except TracingError as exc:
        result['error'] = exc.message
    result['elapsed_ms'] = int((time.monotonic() - started) * 1000)
    return result
