"""搜索（约定 §5.3）和统计（§5.4）。

选路：配了 ClickHouse 加速、并且所有条件都能用加速表表达 → ClickHouse；否则 Jaeger。ClickHouse 出错（连不上、超时）
不自动改走 Jaeger，直接返回 clickhouse_unavailable（避免默默变慢、结果口径变化）。

Jaeger 路径（结果不保证最新、没有排序 / 分页、起止时间截断到整秒）：时间切片从新到旧——第一片是最后 5 分钟，
返回条数到了 searchDepth 说明这片被截断了（返回的是任意一批），对半切先查较新的一半；查完整了就往前、长度翻倍。
收集够 limit 条、请求到 10 次、窗口查完或总耗时超过 2×timeout 就停，按 traceId 去重、按开始时间倒序。
contains（包含匹配）Jaeger 不支持，改用 /api/v3/traces 取完整链路由 master 扫描，最多扫 200 条 / 64MB / 10 次请求。
"""

import asyncio
import re
import time

from . import model
from .clickhouse import Params, rows as ch_rows, to_float, to_int
from .common import (ResponseTooLarge, TTLCache, TracingError, ceil_s, floor_s, has_control, invalid, iso, iso_ms,
                     iso_ns, ms1, resolve_window)
from .fields import FIELD_LABELS, FIELD_LEVELS
from .filters import SEMANTIC_PARAMS, build_filter, check_capabilities
from .jaeger import JaegerClient, capabilities_problem

SPAN_KINDS = ('server', 'client', 'producer', 'consumer', 'internal')
STRING_PARAMS = ('service', 'operation', 'method', 'route', 'path', 'url', 'host', 'client_ip', 'exception_type',
                 'k8s_pod', 'k8s_namespace', 'k8s_deployment')
FILTER_PARAMS = frozenset((*STRING_PARAMS, 'span_kind', 'start', 'end', 'lookback', 'error', 'status_codes',
                           'status_min', 'status_max', 'min_duration', 'max_duration', 'attributes', 'contains'))
SEARCH_PARAMS = FILTER_PARAMS | {'limit'}
STATS_PARAMS = FILTER_PARAMS | {'group_by', 'interval', 'top'}
PARAM_HINTS = {'namespace': 'k8s_namespace', 'pod': 'k8s_pod', 'deployment': 'k8s_deployment',
               'status_code': 'status_codes', 'kind': 'span_kind'}
MAX_TEXT = 2048
MAX_STATUS_ITEMS = 20
MAX_ATTRIBUTES = 10
MAX_CONTAINS = 3
MAX_CONTAINS_TEXT = 1024
DEFAULT_LIMIT = 20
MAX_LIMIT = 50
MAX_REQUESTS = 10
FIRST_SLICE = 300
SCAN_DEPTH = 20
SCAN_MAX_TRACES = 200
SCAN_MAX_BYTES = 64 * 1024 * 1024
QUERY_CHARS = 200
# ClickHouse 加速表能表达的条件：语义字段 → 列，contains 的键 → 列
CH_FIELD_COLUMNS = {'method': 'http_method', 'path': 'url_path', 'host': 'peer', 'pod': 'pod', 'namespace': 'namespace'}
CH_CONTAINS_COLUMNS = {'operation': 'operation', 'path': 'url_path', 'query': 'url_query', 'host': 'peer',
                       'k8s_pod': 'pod'}
CONTAINS_FIELDS = {**{key: key for key in FIELD_LABELS}, 'k8s_pod': 'pod', 'k8s_namespace': 'namespace',
                   'k8s_deployment': 'deployment'}
GROUP_COLUMNS = {'service': 'service_name', 'operation': 'operation', 'path': 'url_path',
                 'status_code': 'http_status', 'k8s_pod': 'pod', 'k8s_namespace': 'namespace', 'host': 'peer',
                 'kind': 'kind', 'method': 'http_method', 'status': 'span_status'}
GROUP_KEYS = ('time', *GROUP_COLUMNS)
INTERVALS = {'1m': 60, '5m': 300, '10m': 600, '1h': 3600}
MAX_TOP = 100
DEFAULT_TOP = 30
TIME_ROWS_CAP = 2000
SAME_SPAN_NOTE = '所有条件要落在同一个 Span（含它的资源属性）上；「网关 504 且下游报错」这类条件要分两次查'
STATUS_TEXT = {'Error': 'error', 'Ok': 'ok', 'Unset': 'unset', '': 'unset'}
_GO_DURATION = re.compile(r'(?:(?:\d+(?:\.\d*)?|\.\d+)(?:ns|us|µs|μs|ms|s|m|h))+')
_GO_PART = re.compile(r'(\d+(?:\.\d*)?|\.\d+)(ns|us|µs|μs|ms|s|m|h)')
_GO_UNITS_MS = {'ns': 1e-6, 'us': 1e-3, 'µs': 1e-3, 'μs': 1e-3, 'ms': 1.0, 's': 1000.0, 'm': 60000.0, 'h': 3600000.0}
_SPACE = re.compile(r'\s')

_clock = time.monotonic  # 计时（测试里替换）
# 搜到的链路的时间（trace_id → 毫秒），给 trace / spans 定时间范围
_trace_times = TTLCache(max_items=5000, ttl=3600)


def remember(trace_id, millis):
    if trace_id and millis:
        _trace_times.put(trace_id, int(millis))


def recall(trace_id):
    return _trace_times.get(trace_id)


# ---------------------------------------------------------------------------
# 参数校验
# ---------------------------------------------------------------------------
def _text(body, key, max_len=MAX_TEXT):
    value = body.get(key)
    if value is None:
        return ''
    if not isinstance(value, str):
        raise invalid(f'{key} 要是字符串')
    text = value.strip()
    if len(text) > max_len or has_control(text):
        raise invalid(f'{key} 不能换行，最长 {max_len} 个字符')
    return text


def _int(body, key, low, high, default=None):
    value = body.get(key)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        if isinstance(value, str) and value.strip().isdigit():
            value = int(value.strip())
        else:
            raise invalid(f'{key} 要是 {low}~{high} 的整数')
    if not low <= value <= high:
        raise invalid(f'{key} 要是 {low}~{high} 的整数')
    return value


def _key(value, what):
    text = value.strip() if isinstance(value, str) else ''
    if not text or len(text) > 200 or _SPACE.search(text) or has_control(text):
        raise invalid(f'{what}不能为空、不能有空格，最长 200 个字符')
    return text


def parse_duration(value, name):
    """Go 时长（500ms、3s、1m30s）→ (毫秒, 规范写法)"""
    text = value.strip() if isinstance(value, str) else ''
    if not text or not _GO_DURATION.fullmatch(text):
        raise invalid(f'{name} 要写成带单位的时长，例如 500ms、3s、1m30s')
    millis = sum(float(number) * _GO_UNITS_MS[unit] for number, unit in _GO_PART.findall(text))
    if millis <= 0:
        raise invalid(f'{name} 要大于 0')
    if millis > 86400 * 1000:
        raise invalid(f'{name} 最长 24h')
    canonical = f'{int(millis)}ms' if millis == int(millis) else f'{round(millis * 1000)}us'
    return millis, canonical


def _status_items(body):
    items = body.get('status_codes')
    codes, ranges, labels = [], [], []
    if items is None:
        return codes, ranges, labels
    if not isinstance(items, list):
        raise invalid('status_codes 要是数组，例如 [504] 或 ["5xx"]')
    if len(items) > MAX_STATUS_ITEMS:
        raise invalid(f'status_codes 最多 {MAX_STATUS_ITEMS} 个')
    for item in items:
        text = item.strip().lower() if isinstance(item, str) else None
        if text in ('4xx', '5xx'):
            low = int(text[0]) * 100
            ranges.append((low, low + 99))
            labels.append(text)
            continue
        if isinstance(item, bool):
            raise invalid('status_codes 里只能是 100~599 的整数或 "4xx" / "5xx"')
        code = item if isinstance(item, int) else int(text) if text and text.isdigit() else None
        if code is None or not 100 <= code <= 599:
            raise invalid('status_codes 里只能是 100~599 的整数或 "4xx" / "5xx"')
        if code not in codes:
            codes.append(code)
            labels.append(str(code))
    return codes, ranges, labels


def parse_filters(body, allowed, purpose='search'):
    """search / stats 的参数 → 规范化的条件；不合法抛 invalid_request"""
    if not isinstance(body, dict):
        raise invalid('参数要是 JSON 对象')
    unknown = sorted(key for key in body if key not in allowed)
    if unknown:
        hints = [f'{key} 请用 {PARAM_HINTS[key]}' for key in unknown if key in PARAM_HINTS]
        raise invalid(f'不支持的参数：{"、".join(unknown[:10])}' + (f'（{"；".join(hints)}）' if hints else ''))
    spec = {key: _text(body, key) for key in ('service', 'operation')}
    kind = _text(body, 'span_kind').lower()
    if kind and kind not in SPAN_KINDS:
        raise invalid(f'span_kind 只能是 {" / ".join(SPAN_KINDS)}')
    spec['span_kind'] = kind
    error = body.get('error')
    if error is not None and not isinstance(error, bool):
        raise invalid('error 要是 true 或 false')
    spec['error'] = bool(error)
    spec['status_codes'], spec['status_ranges'], labels = _status_items(body)
    low, high = _int(body, 'status_min', 100, 599), _int(body, 'status_max', 100, 599)
    if low is not None or high is not None:
        low, high = low or 100, high or 599
        if low > high:
            raise invalid('status_min 不能大于 status_max')
        spec['status_ranges'].append((low, high))
        labels.append(f'{low}~{high}')
    spec['status_labels'] = labels
    for key in ('min_duration', 'max_duration'):
        value = body.get(key)
        if value is None or value == '':
            spec[key], spec[key + '_ms'] = '', None
        else:
            spec[key + '_ms'], spec[key] = parse_duration(value, key)
    if spec['min_duration_ms'] and spec['max_duration_ms'] and spec['min_duration_ms'] > spec['max_duration_ms']:
        raise invalid('min_duration 不能大于 max_duration')
    spec['fields'] = {}
    for param, key in SEMANTIC_PARAMS:
        value = _text(body, param)
        if value:
            spec['fields'][key] = value
    spec['attributes'] = _mapping(body, 'attributes', MAX_ATTRIBUTES, MAX_TEXT)
    spec['contains'] = _mapping(body, 'contains', MAX_CONTAINS, MAX_CONTAINS_TEXT)
    spec['start'], spec['end'] = resolve_window(body)
    if purpose == 'search':
        spec['limit'] = _int(body, 'limit', 1, MAX_LIMIT, DEFAULT_LIMIT)
    return spec


def _mapping(body, key, max_items, max_value):
    value = body.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise invalid(f'{key} 要是 {{属性名: 值}} 对象')
    if len(value) > max_items:
        raise invalid(f'{key} 最多 {max_items} 项')
    result = {}
    for name, item in value.items():
        name = _key(name, f'{key} 的属性名')
        if isinstance(item, bool) or not isinstance(item, (str, int, float)):
            raise invalid(f'{key}.{name} 的值要是字符串')
        text = str(item).strip()
        if not text or len(text) > max_value or has_control(text):
            raise invalid(f'{key}.{name} 的值不能为空、不能换行，最长 {max_value} 个字符')
        result[name] = text
    return result


def unsupported_by_clickhouse(spec):
    """加速表表达不了的条件（参数名）"""
    names = [param for param, key in SEMANTIC_PARAMS if spec['fields'].get(key) and key not in CH_FIELD_COLUMNS]
    if spec['attributes']:
        names.append('attributes')
    names.extend(f'contains.{key}' for key in spec['contains'] if key not in CH_CONTAINS_COLUMNS)
    return names


def describe(spec):
    """条件的中文描述"""
    items = []
    if spec['service']:
        items.append(f'服务 = {spec["service"]}')
    if spec['operation']:
        items.append(f'Span 名 = {spec["operation"]}')
    if spec['span_kind']:
        items.append(f'Span 类型 = {spec["span_kind"]}')
    if spec['error']:
        items.append('Span 状态 = error')
    if spec['status_labels']:
        items.append(f'HTTP 状态码 ∈ {{{", ".join(spec["status_labels"])}}}')
    if spec['min_duration']:
        items.append(f'耗时 ≥ {spec["min_duration"]}')
    if spec['max_duration']:
        items.append(f'耗时 ≤ {spec["max_duration"]}')
    for _, key in SEMANTIC_PARAMS:
        if spec['fields'].get(key):
            items.append(f'{FIELD_LABELS[key]} = {spec["fields"][key]}')
    items.extend(f'{key} = {value}' for key, value in spec['attributes'].items())
    items.extend(f'{key} 包含 {value}（不区分大小写）' for key, value in spec['contains'].items())
    return items


# ---------------------------------------------------------------------------
# ClickHouse 路径
# ---------------------------------------------------------------------------
def ch_conditions(spec, params, cluster_values, cluster_key):
    """WHERE 条件（值全部绑定参数）"""
    start_ms, end_ms = int(spec['start'] * 1000), int(spec['end'] * 1000)
    conds = [f'start_time >= fromUnixTimestamp64Milli({params.add("s", "Int64", start_ms)})',
             f'start_time <= fromUnixTimestamp64Milli({params.add("e", "Int64", end_ms)})']
    if cluster_key:
        conds.append(f'k8s_cluster IN {params.add("clusters", "Array(String)", list(cluster_values))}')
    for key, column in (('service', 'service_name'), ('operation', 'operation'), ('span_kind', 'kind')):
        if spec[key]:
            conds.append(f'{column} = {params.add(key, "String", spec[key])}')
    if spec['error']:
        conds.append("span_status = 'Error'")
    parts = []
    if spec['status_codes']:
        parts.append(f'http_status IN {params.add("codes", "Array(Int64)", spec["status_codes"])}')
    for number, (low, high) in enumerate(spec['status_ranges']):
        parts.append(f'(http_status >= {params.add(f"smin{number}", "Int64", low)} '
                     f'AND http_status <= {params.add(f"smax{number}", "Int64", high)})')
    if parts:
        conds.append('(' + ' OR '.join(parts) + ')')
    if spec['min_duration_ms']:
        conds.append(f'duration_ms >= {params.add("dmin", "Float64", spec["min_duration_ms"])}')
    if spec['max_duration_ms']:
        conds.append(f'duration_ms <= {params.add("dmax", "Float64", spec["max_duration_ms"])}')
    for key, column in CH_FIELD_COLUMNS.items():
        if spec['fields'].get(key):
            conds.append(f'{column} = {params.add(key, "String", spec["fields"][key])}')
    for number, (key, value) in enumerate(spec['contains'].items()):
        conds.append(f'positionCaseInsensitive({CH_CONTAINS_COLUMNS[key]}, '
                     f'{params.add(f"c{number}", "String", value)}) > 0')
    return conds


def search_sql(spec, table, cluster_values, cluster_key):
    params = Params()
    where = ch_conditions(spec, params, cluster_values, cluster_key)
    sql = ('SELECT trace_id, toUnixTimestamp64Milli(start_time) AS ts, service_name, namespace, kind, span_status, '
           'operation, url_path, http_method, http_status, duration_ms, peer, pod, url_query, k8s_cluster\n'
           f'FROM {table}\nWHERE ' + '\n  AND '.join(where) +
           f'\nORDER BY start_time DESC\nLIMIT 1 BY trace_id\nLIMIT {params.add("limit", "UInt32", spec["limit"])}')
    return sql, params


def _status(value):
    return STATUS_TEXT.get(str(value or ''), str(value or '').lower() or 'unset')


async def clickhouse_search(ctx, spec):
    client = ctx.clickhouse
    sql, params = search_sql(spec, client.table(), ctx.cluster_values, ctx.cluster_key)
    result = await client.query(sql, params)
    traces = []
    for row in ch_rows(result):
        trace_id = model.norm_id(row.get('trace_id'), 32)
        ts = to_int(row.get('ts'))
        remember(trace_id, ts)
        status_code = to_int(row.get('http_status'))
        query = ctx.masker.url(str(row.get('url_query') or ''))
        traces.append({
            'trace_id': trace_id, 'start': iso_ms(ts), 'service': row.get('service_name') or '',
            'operation': row.get('operation') or '', 'kind': row.get('kind') or 'unspecified',
            'status': _status(row.get('span_status')), 'http_status': status_code or None,
            'method': row.get('http_method') or '', 'path': ctx.masker.url(str(row.get('url_path') or '')),
            'query': query[:QUERY_CHARS], 'duration_ms': ms1(to_float(row.get('duration_ms'))),
            'peer': row.get('peer') or '', 'k8s_pod': row.get('pod') or '', 'k8s_namespace': row.get('namespace') or '',
            'cluster': ctx.resolve(row.get('k8s_cluster') or ''), 'url': ctx.trace_url(trace_id),
        })
    window = {'from': iso(spec['start']), 'to': iso(spec['end'])}
    return {
        'source': 'clickhouse', 'traces': traces, 'count': len(traces),
        'query': {'window': window, 'conditions': describe(spec), 'cluster': ctx.cluster_info()},
        'coverage': {**window, 'complete': True},
        'notes': ['每条结果是命中条件的那个 Span（同一条链路只取最新的一个），按时间倒序', SAME_SPAN_NOTE],
    }


# ---------------------------------------------------------------------------
# Jaeger 路径
# ---------------------------------------------------------------------------
class Slicer:
    """时间切片（整秒）：从新到旧；截断时对半切先查较新的一半，查完整了往前、长度翻倍"""

    def __init__(self, start, end, depth, first=FIRST_SLICE):
        self.start, self.end, self.depth = start, end, depth
        self.cursor = end  # 这之后（到 end）已经完整查过
        self.length = max(1, min(first, end - start))
        self.saturated = False

    @property
    def done(self):
        return self.cursor <= self.start

    def next(self):
        return max(self.start, self.cursor - self.length), self.cursor

    def feed(self, start, end, count):
        """记下一片的结果；返回这片是否完整（没被截断）"""
        span = end - start
        if count >= self.depth and span > 2:
            self.length = max(1, span // 2)
            return False
        if count >= self.depth:
            self.saturated = True  # 2 秒以内的片仍被截断：只能接受
        self.cursor = start
        self.length = max(1, min(2 * span, self.cursor - self.start))
        return True

    def coverage(self, requests):
        return {'from': iso(self.start), 'to': iso(self.end), 'checked_from': iso(self.cursor),
                'complete': self.done, 'saturated': self.saturated, 'requests': requests}


def summary_entry(ctx, item):
    trace_id = model.norm_id(item.get('traceId'), 32)
    start, end = to_int(item.get('minStartTimeUnixNano')), to_int(item.get('maxEndTimeUnixNano'))
    services = [{'name': str(svc.get('name') or ''), 'span_count': to_int(svc.get('spanCount')),
                 'error_span_count': to_int(svc.get('errorSpanCount'))}
                for svc in item.get('services') or [] if isinstance(svc, dict)]
    services.sort(key=lambda svc: (-svc['span_count'], svc['name']))
    entry = {'trace_id': trace_id, 'start': iso_ns(start), 'duration_ms': ms1(max(0, end - start) / 1e6),
             'root_service': item.get('rootServiceName') or '', 'root_operation': item.get('rootOperationName') or '',
             'span_count': to_int(item.get('spanCount')), 'error_span_count': to_int(item.get('errorSpanCount')),
             'services': services, 'url': ctx.trace_url(trace_id)}
    orphans = to_int(item.get('orphanSpanCount'))
    if orphans:
        entry['orphan_span_count'] = orphans
    return entry, start


def _trace_entry(ctx, trace):
    summary = model.summarize(trace)
    entry = {'trace_id': trace.trace_id, 'start': iso_ns(summary['start_ns']),
             'duration_ms': ms1((summary['end_ns'] - summary['start_ns']) / 1e6),
             'root_service': summary['root_service'], 'root_operation': summary['root_operation'],
             'span_count': summary['span_count'], 'error_span_count': summary['error_span_count'],
             'services': summary['services'], 'url': ctx.trace_url(trace.trace_id)}
    if summary['orphan_span_count']:
        entry['orphan_span_count'] = summary['orphan_span_count']
    return entry, summary['start_ns']


async def _prepare_jaeger(ctx, spec):
    caps = await ctx.jaeger.capabilities()
    problem = capabilities_problem(caps)
    if problem:
        raise TracingError(409, f'搜索需要 Jaeger 2.23 及以上：{problem}；服务列表、接口列表和按 Trace ID 取链路不受影响',
                           'version_unsupported')
    if not spec['service'] and not (caps.get('search') or {}).get('withoutServiceName'):
        raise invalid('这个 Jaeger 的存储后端搜索时必须指定服务（service）')
    filter_call, used, ops, levels = build_filter(spec, ctx.fields, ctx.cluster_key, ctx.cluster_values)
    check_capabilities(caps, ops, levels)
    return filter_call, used


def _budget(ctx):
    return _clock() + 2 * ctx.cfg['timeout']


async def jaeger_search(ctx, spec):
    filter_call, used = await _prepare_jaeger(ctx, spec)
    start, end = floor_s(spec['start']), ceil_s(spec['end'])
    notes = [SAME_SPAN_NOTE]
    if spec['contains']:
        traces, coverage, scan = await _scan(ctx, spec, filter_call, start, end, notes)
    else:
        traces, coverage = await _summaries(ctx, spec, filter_call, start, end, notes)
        scan = None
    data = {
        'source': 'jaeger', 'traces': traces, 'count': len(traces),
        'query': {'window': {'from': iso(start), 'to': iso(end)}, 'conditions': describe(spec), 'fields_used': used,
                  'cluster': ctx.cluster_info()},
        'coverage': coverage, 'notes': notes,
    }
    if scan is not None:
        data['scan'] = scan
    if not coverage['complete'] and len(traces) < spec['limit']:
        notes.append('时间窗口没有查完（请求次数或耗时到了上限），可以缩小时间范围或加条件再查')
    if coverage['saturated']:
        notes.append('有 2 秒内的时间片仍被截断（这段时间命中的链路太多），结果可能漏掉一部分；加条件可以减少命中数')
    return data


async def _summaries(ctx, spec, filter_call, start, end, notes):
    limit = spec['limit']
    depth = min(max(limit, 10), 50)
    slicer, complete, extra = Slicer(start, end, depth), {}, {}
    deadline, requests = _budget(ctx), 0
    while not slicer.done and requests < MAX_REQUESTS:
        if requests and _clock() > deadline:
            break
        a, b = slicer.next()
        params = JaegerClient.search_params(a, b, depth, filter_call)
        try:
            items = await ctx.jaeger.summaries(params)
        except TracingError as exc:
            if not requests:
                raise
            notes.append(f'第 {requests + 1} 次查询失败，结果不完整：{exc.message}')
            break
        requests += 1
        target = complete if slicer.feed(a, b, len(items)) else extra
        for item in items:
            entry, start_ns = summary_entry(ctx, item)
            target.setdefault(entry['trace_id'], (start_ns, entry))
        if len(complete) >= limit:
            break
    ordered = sorted(complete.values(), key=lambda item: -item[0])
    if len(ordered) < limit:  # 没查完时，用被截断的那几批里更早的补上（标了 complete=false）
        cursor_ns = slicer.cursor * 1_000_000_000
        ordered += sorted((item for trace_id, item in extra.items()
                           if trace_id not in complete and item[0] < cursor_ns), key=lambda item: -item[0])
    traces = [entry for _, entry in ordered[:limit]]
    for start_ns, entry in ordered[:limit]:
        remember(entry['trace_id'], start_ns // 1_000_000)
    return traces, slicer.coverage(requests)


def _contains_values(span, key, field_cfg):
    if key == 'operation':
        return [span.name]
    field_key = CONTAINS_FIELDS.get(key)
    names = (field_cfg.get(field_key) or []) if field_key else [key]
    if field_key and FIELD_LEVELS.get(field_key) == 'event':
        return [event['attrs'].get(name) for event in span.events for name in names]
    return [source.get(name) for name in names for source in (span.attrs, span.resource)]


def contains_match(trace, contains, service, field_cfg):
    """链路里有一个 Span（给了 service 时只看该服务的）同时满足所有 contains（不区分大小写）"""
    needles = [(key, value.lower()) for key, value in contains.items()]
    for span in trace.spans:
        if service and span.service != service:
            continue
        if all(any(needle in str(value).lower() for value in _contains_values(span, key, field_cfg)
                   if value not in (None, '')) for key, needle in needles):
            return True
    return False


async def _parse_traces(raw):
    if not raw:
        return []
    if len(raw) > 1024 * 1024:
        return await asyncio.to_thread(model.parse_text, raw)
    return model.parse_text(raw)


async def _scan(ctx, spec, filter_call, start, end, notes):
    limit = spec['limit']
    slicer = Slicer(start, end, SCAN_DEPTH)
    matched, seen = {}, set()
    deadline, requests, used_bytes, stopped = _budget(ctx), 0, 0, False
    while not slicer.done and requests < MAX_REQUESTS:
        if requests and _clock() > deadline:
            stopped = True
            break
        a, b = slicer.next()
        params = JaegerClient.search_params(a, b, SCAN_DEPTH, filter_call)
        try:
            raw = await ctx.jaeger.traces(params, limit=SCAN_MAX_BYTES - used_bytes)
            traces = await _parse_traces(raw)
        except ResponseTooLarge:
            if not requests:
                raise
            notes.append('扫描的链路数据超过 64 MB，停止扫描')
            stopped = True
            break
        except ValueError as exc:
            raise TracingError(502, f'Jaeger 返回的链路无法解析：{exc}', 'jaeger_unavailable') from None
        except TracingError as exc:
            if not requests:
                raise
            notes.append(f'第 {requests + 1} 次查询失败，结果不完整：{exc.message}')
            stopped = True
            break
        requests += 1
        used_bytes += len(raw)
        slicer.feed(a, b, len(traces))
        for trace in traces:
            if trace.trace_id in seen:
                continue
            seen.add(trace.trace_id)
            if contains_match(trace, spec['contains'], spec['service'], ctx.fields):
                matched[trace.trace_id] = _trace_entry(ctx, trace)
            if len(seen) >= SCAN_MAX_TRACES:
                break
        if len(matched) >= limit or len(seen) >= SCAN_MAX_TRACES or used_bytes >= SCAN_MAX_BYTES:
            stopped = stopped or not slicer.done
            break
    ordered = sorted(matched.values(), key=lambda item: -item[1])[:limit]
    for entry, start_ns in ordered:
        remember(entry['trace_id'], start_ns // 1_000_000)
    scan = {'scanned': len(seen), 'matched': len(matched),
            'complete': slicer.done and not slicer.saturated and not stopped}
    notes.append(f'contains 由 KubeDoor 扫描完整链路匹配（最多 {SCAN_MAX_TRACES} 条），比其他条件慢')
    return [entry for entry, _ in ordered], slicer.coverage(requests), scan


async def search(ctx, body):
    spec = parse_filters(body, SEARCH_PARAMS)
    if ctx.has_clickhouse:
        missing = unsupported_by_clickhouse(spec)
        if not missing:
            return await clickhouse_search(ctx, spec)
        data = await jaeger_search(ctx, spec)
        data['notes'].insert(0, f'这些条件加速表没有，改用 Jaeger 查询：{"、".join(missing)}')
        return data
    return await jaeger_search(ctx, spec)


# ---------------------------------------------------------------------------
# 统计（只有 ClickHouse）
# ---------------------------------------------------------------------------
AGGREGATES = ("count() AS spans, countIf(span_status = 'Error') AS errors, "
              'countIf(http_status >= 400 AND http_status < 500) AS http_4xx, countIf(http_status >= 500) AS http_5xx, '
              'quantile(0.5)(duration_ms) AS p50_ms, quantile(0.95)(duration_ms) AS p95_ms, '
              'quantile(0.99)(duration_ms) AS p99_ms, max(duration_ms) AS max_ms')
METRICS = ('spans', 'errors', 'http_4xx', 'http_5xx', 'p50_ms', 'p95_ms', 'p99_ms', 'max_ms')


def auto_interval(seconds):
    if seconds <= 2 * 3600:
        return 60
    if seconds <= 12 * 3600:
        return 300
    if seconds <= 2 * 86400:
        return 600
    return 3600


def parse_stats(body):
    spec = parse_filters(body, STATS_PARAMS, purpose='stats')
    missing = unsupported_by_clickhouse(spec)
    if missing:
        raise invalid(f'统计只支持加速表里有的条件，这些不行：{"、".join(missing)}')
    group_by = body.get('group_by') or []
    if not isinstance(group_by, list) or len(group_by) > 2 or len(set(map(str, group_by))) != len(group_by):
        raise invalid('group_by 最多 2 个、不能重复')
    for key in group_by:
        if key not in GROUP_KEYS:
            raise invalid(f'group_by 只能是 {" / ".join(GROUP_KEYS)}')
    spec['group_by'] = list(group_by)
    interval = body.get('interval')
    if interval not in (None, ''):
        if interval not in INTERVALS:
            raise invalid(f'interval 只能是 {" / ".join(INTERVALS)}')
        spec['interval'] = INTERVALS[interval]
    else:
        spec['interval'] = auto_interval(spec['end'] - spec['start'])
    spec['top'] = _int(body, 'top', 1, MAX_TOP, DEFAULT_TOP)
    return spec


def stats_sql(spec, table, cluster_values, cluster_key):
    """返回 (分组查询, 参数, 汇总查询)：两条查询共用同一组参数"""
    params = Params()
    where = ch_conditions(spec, params, cluster_values, cluster_key)
    where_sql = '\n  AND '.join(where)
    totals = f'SELECT {AGGREGATES}\nFROM {table}\nWHERE {where_sql}'
    group_by = spec['group_by']
    if not group_by:
        return None, params, totals
    exprs, names = [], []
    for key in group_by:
        if key == 'time':
            exprs.append('toInt64(toUnixTimestamp(toStartOfInterval(start_time, INTERVAL '
                         f'{params.add("iv", "UInt32", spec["interval"])} SECOND))) * 1000 AS g_time')
        else:
            exprs.append(f'{GROUP_COLUMNS[key]} AS g_{key}')
        names.append(f'g_{key}')
    top = params.add('top', 'UInt32', spec['top'])
    others = [key for key in group_by if key != 'time']
    if 'time' in group_by:
        # 时间序列不按 top 截断；另一个维度只取 Span 数最多的 top 个
        if others:
            column = GROUP_COLUMNS[others[0]]
            where_sql += (f'\n  AND {column} IN (SELECT {column} FROM {table} WHERE {" AND ".join(where)} '
                          f'GROUP BY {column} ORDER BY count() DESC LIMIT {top})')
        order = 'g_time ASC' + (', spans DESC' if others else '')
        limit = params.add('cap', 'UInt32', TIME_ROWS_CAP)
    else:
        order, limit = 'spans DESC', top
    sql = (f'SELECT {", ".join(exprs)}, {AGGREGATES}\nFROM {table}\nWHERE {where_sql}\n'
           f'GROUP BY {", ".join(names)}\nORDER BY {order}\nLIMIT {limit}')
    return sql, params, totals


def _metrics(row):
    return {name: (to_int(row.get(name)) if name in ('spans', 'errors', 'http_4xx', 'http_5xx')
                   else ms1(to_float(row.get(name)))) for name in METRICS}


def _group_value(key, value):
    if key == 'time':
        return iso_ms(to_int(value))
    if key == 'status_code':
        return to_int(value) or None
    if key == 'status':
        return _status(value)
    if key == 'kind':
        return value or 'unspecified'
    return value


async def stats(ctx, body):
    if not ctx.has_clickhouse:
        raise TracingError(409, '没有配置 ClickHouse 加速，不能统计；可以用 metrics 查 HTTP 指标', 'not_supported')
    spec = parse_stats(body)
    client = ctx.clickhouse
    sql, params, totals_sql = stats_sql(spec, client.table(), ctx.cluster_values, ctx.cluster_key)
    if sql:
        grouped, totals = await asyncio.gather(client.query(sql, params), client.query(totals_sql, params))
    else:
        grouped, totals = None, await client.query(totals_sql, params)
    result_rows = []
    for row in ch_rows(grouped) if grouped else []:
        item = {key: _group_value(key, row.get(f'g_{key}')) for key in spec['group_by']}
        item.update(_metrics(row))
        result_rows.append(item)
    total_rows = ch_rows(totals)
    notes = ['统计的是命中条件的 Span 数；看接口请求量请加 span_kind=server']
    if 'time' in spec['group_by'] and len(result_rows) >= TIME_ROWS_CAP:
        notes.append(f'行数到了上限 {TIME_ROWS_CAP}，后面的没有返回：加大 interval 或减小 top')
    data = {'source': 'clickhouse', 'window': {'from': iso(spec['start']), 'to': iso(spec['end'])},
            'group_by': spec['group_by'], 'rows': result_rows,
            'totals': _metrics(total_rows[0]) if total_rows else _metrics({}),
            'conditions': describe(spec), 'cluster': ctx.cluster_info(), 'notes': notes}
    if 'time' in spec['group_by']:
        data['interval'] = next(name for name, seconds in INTERVALS.items() if seconds == spec['interval'])
    return data
