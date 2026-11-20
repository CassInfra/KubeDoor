"""链路追踪：Jaeger 过滤构造、时间切片和扫描、ClickHouse SQL 构造和参数绑定、选路、统计（tracing/filters.py、
search.py、clickhouse.py、jaeger.py）。Jaeger 和 ClickHouse 用本地起的 aiohttp 服务模拟。"""
import contextlib
import itertools
import json
import re
from datetime import datetime, timezone

import aiohttp
import pytest
from aiohttp import web

from tracing import clickhouse, common, config, filters, jaeger, ops, search
from tracing.context import Ctx
from tracing_fakes import (CAPS, FakeClickHouse, FakeJaeger, TraceBuilder, compact, parse_array_literal, serve,
                           summary, tsv_unescape)

NOW = 1_795_203_600.0  # 固定的「现在」
FIELDS = config.merged({})['fields']


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(common, '_clock', lambda: NOW)
    monkeypatch.setenv('AI_ENCRYPTION_KEY', '11' * 32)
    jaeger.reset_caches()
    clickhouse.reset_state()
    ops.reset_caches()
    search._trace_times.clear()


@contextlib.asynccontextmanager
async def make_ctx(jaeger_url='http://127.0.0.1:9', ch_url='', env='prod', aliases=None, **overrides):
    stored = {'enabled': True, 'url': jaeger_url, 'cluster_key': 'k8sname', **overrides}
    if ch_url:
        stored['clickhouse'] = {'enabled': True, 'url': ch_url, 'username': 'reader', 'password': 'ch-pass',
                                **overrides.pop('clickhouse', {})}
        stored = config.normalize_config(stored)
    async with aiohttp.ClientSession() as session:
        yield Ctx(config.merged(stored), env, aliases if aliases is not None else {'prod': ['蓬莱']}, session)


def spec_of(body):
    return search.parse_filters(body, search.SEARCH_PARAMS)


def seconds(text):
    return datetime.strptime(text, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc).timestamp()


# ---------------------------------------------------------------------------
# Jaeger 过滤（query.filter）
# ---------------------------------------------------------------------------
SPEC_EXAMPLE = {"op": "and", "args": [
    {"call": {"op": "eq", "args": [{"field": {"name": "service", "level": "resource"}},
                                   {"scalar": {"value": "deploy-gateway-api"}}]}},
    {"call": {"op": "eq", "args": [{"field": {"name": "status", "level": "span"}}, {"scalar": {"value": "error"}}]}},
    {"call": {"op": "or", "args": [
        {"call": {"op": "eq", "args": [{"attr": {"key": "http.response.status_code"}}, {"scalar": {"value": "504"}}]}},
        {"call": {"op": "eq", "args": [{"attr": {"key": "http.status_code"}}, {"scalar": {"value": "504"}}]}}]}},
    {"call": {"op": "eq", "args": [{"attr": {"key": "k8sname", "level": "resource"}},
                                   {"scalar": {"value": "cassmall-prod-penglai"}}]}}]}


def test_filter_matches_spec_example_exactly():
    spec = spec_of({'service': 'deploy-gateway-api', 'error': True, 'status_codes': [504]})
    flt, used, ops_, levels = filters.build_filter(spec, FIELDS, 'k8sname', ['cassmall-prod-penglai'])
    assert json.dumps(flt, separators=(',', ':')) == json.dumps(SPEC_EXAMPLE, separators=(',', ':'))
    assert used == {'status_code': ['http.response.status_code', 'http.status_code']}
    assert ops_ == {'and', 'or', 'eq'} and levels == {'span', 'resource'}


def test_filter_single_condition_and_all_kinds():
    flt, _, _, _ = filters.build_filter(spec_of({'service': 'svc'}), FIELDS)
    assert flt == {'op': 'eq', 'args': [{'field': {'name': 'service', 'level': 'resource'}}, {'scalar': {'value': 'svc'}}]}
    assert filters.build_filter(spec_of({}), FIELDS)[0] is None
    spec = spec_of({'operation': 'GET /x', 'span_kind': 'server', 'min_duration': '0.5s', 'max_duration': '1m30s',
                    'method': 'POST', 'exception_type': 'IOException', 'k8s_pod': 'p-1',
                    'attributes': {'biz.order_no': 'A1'}})
    flt, used, ops_, levels = filters.build_filter(spec, FIELDS, 'k8sname', ['prod', '蓬莱'])
    args = flt['args']
    assert args[0] == {'call': {'op': 'eq', 'args': [{'field': {'name': 'name', 'level': 'span'}},
                                                     {'scalar': {'value': 'GET /x'}}]}}
    assert args[1]['call']['args'][0] == {'field': {'name': 'kind', 'level': 'span'}}
    assert args[2] == {'call': {'op': 'gte', 'args': [{'field': {'name': 'duration', 'level': 'span'}},
                                                      {'scalar': {'value': '500ms'}}]}}
    assert args[3]['call']['op'] == 'lte' and args[3]['call']['args'][1] == {'scalar': {'value': '90000ms'}}
    assert args[4] == {'call': {'op': 'or', 'args': [
        {'call': {'op': 'eq', 'args': [{'attr': {'key': 'http.request.method'}}, {'scalar': {'value': 'POST'}}]}},
        {'call': {'op': 'eq', 'args': [{'attr': {'key': 'http.method'}}, {'scalar': {'value': 'POST'}}]}}]}}
    assert args[5] == {'call': {'op': 'eq', 'args': [{'attr': {'key': 'exception.type', 'level': 'event'}},
                                                     {'scalar': {'value': 'IOException'}}]}}
    assert args[6]['call']['op'] == 'or'  # Pod：k8s.pod.name / host.name
    assert args[7] == {'call': {'op': 'eq', 'args': [{'attr': {'key': 'biz.order_no'}}, {'scalar': {'value': 'A1'}}]}}
    assert args[8]['call']['op'] == 'or' and len(args[8]['call']['args']) == 2  # 集群名 + 别名
    assert used == {'method': ['http.request.method', 'http.method'], 'exception_type': ['exception.type'],
                    'pod': ['k8s.pod.name', 'host.name']}
    assert ops_ == {'and', 'or', 'eq', 'gte', 'lte'} and levels == {'span', 'resource', 'event'}


def test_status_expansion():
    spec = spec_of({'status_codes': ['5xx', 418], 'status_min': 300, 'status_max': 302})
    assert filters.expand_status(spec['status_codes'], spec['status_ranges']) == [
        300, 301, 302, 418, 500, 501, 502, 503, 504, 505, 506, 507, 508, 510, 511]
    # 标准状态码一共 63 个；展开后（× 候选属性名）超过 200 个 eq：拒绝
    assert len(filters.expand_status([], [(100, 599)])) == 63
    flt, _, _, _ = filters.build_filter(spec_of({'status_min': 100, 'status_max': 599}), FIELDS)
    assert len(flt['args']) == 126
    with pytest.raises(common.TracingError, match='超过 200 个'):
        filters.build_filter(spec_of({'status_min': 100, 'status_max': 599}),
                             {**FIELDS, 'status_code': ['a', 'b', 'c', 'd']})
    with pytest.raises(common.TracingError, match='没有属性名'):
        filters.build_filter(spec_of({'status_codes': [500]}), {**FIELDS, 'status_code': []})


def test_check_capabilities():
    filters.check_capabilities(CAPS, {'and', 'gte'}, {'event'})
    limited = {'search': {'filter': {'operators': ['and', 'or', 'eq'], 'levels': ['span', 'resource']}}}
    with pytest.raises(common.TracingError, match='按耗时过滤要 gte / lte'):
        filters.check_capabilities(limited, {'and', 'gte'}, set())
    with pytest.raises(common.TracingError, match='event'):
        filters.check_capabilities(limited, {'eq'}, {'event'})


# ---------------------------------------------------------------------------
# 参数校验
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('body, message', [
    ({'namespace': 'ns'}, 'namespace 请用 k8s_namespace'),
    ({'foo': 1}, '不支持的参数：foo'),
    ({'span_kind': 'gateway'}, 'span_kind 只能是'),
    ({'error': 'yes'}, 'error 要是 true 或 false'),
    ({'status_codes': 504}, 'status_codes 要是数组'),
    ({'status_codes': [99]}, '100~599'),
    ({'status_codes': ['6xx']}, '100~599'),
    ({'status_codes': [True]}, '100~599'),
    ({'status_codes': list(range(400, 421))}, '最多 20 个'),
    ({'status_min': 500, 'status_max': 400}, 'status_min 不能大于 status_max'),
    ({'status_min': 600}, '100~599'),
    ({'min_duration': '500'}, '带单位的时长'),
    ({'min_duration': '2s', 'max_duration': '1s'}, 'min_duration 不能大于 max_duration'),
    ({'min_duration': '25h'}, '最长 24h'),
    ({'service': 123}, 'service 要是字符串'),
    ({'service': 'a\nb'}, '不能换行'),
    ({'attributes': {'a b': 'x'}}, '不能有空格'),
    ({'attributes': {f'k{i}': 'v' for i in range(11)}}, '最多 10 项'),
    ({'attributes': {'k': ''}}, '不能为空'),
    ({'attributes': {'k': None}}, '值要是字符串'),
    ({'contains': {'a': 'x', 'b': 'y', 'c': 'z', 'd': 'w'}}, '最多 3 项'),
    ({'contains': ['x']}, '对象'),
    ({'contains': {'path': 'x' * 1025}}, '最长 1024'),
    ({'service': 'x' * 2049}, '最长 2048'),
    ({'limit': 51}, '1~50'),
    ({'limit': True}, '1~50'),
    ({'lookback': '9d'}, '最长 7 天'),
])
def test_search_params_rejected(body, message):
    with pytest.raises(common.TracingError, match=re.escape(message)) as info:
        spec_of(body)
    assert info.value.code == 'invalid_request'


def test_search_params_normalized():
    spec = spec_of({'service': ' svc ', 'status_codes': ['5XX', '504', 504, 404], 'min_duration': '1.5ms',
                    'attributes': {'n': 7}, 'limit': '5', 'k8s_namespace': 'ns'})
    assert spec['service'] == 'svc' and spec['status_codes'] == [504, 404] and spec['status_ranges'] == [(500, 599)]
    assert spec['status_labels'] == ['5xx', '504', '404'] and spec['min_duration'] == '1500us'
    assert spec['min_duration_ms'] == 1.5 and spec['attributes'] == {'n': '7'} and spec['limit'] == 5
    assert spec['fields'] == {'namespace': 'ns'} and spec['end'] == NOW and spec['start'] == NOW - 3600
    assert search.describe(spec) == ['服务 = svc', 'HTTP 状态码 ∈ {5xx, 504, 404}', '耗时 ≥ 1500us', '命名空间 = ns',
                                     'n = 7']
    assert search.unsupported_by_clickhouse(spec) == ['attributes']
    assert search.unsupported_by_clickhouse(spec_of({'route': '/x', 'contains': {'query': 'a', 'db.statement': 'b'}})) \
        == ['route', 'contains.db.statement']


# ---------------------------------------------------------------------------
# 时间切片（假 Jaeger）
# ---------------------------------------------------------------------------
def dataset(starts, errors=0):
    """开始时间（秒）→ 摘要；Jaeger 返回「任意一批」：这里故意返回最旧的那些，逼切片算法去找最新的"""
    items = [summary(f'{i:032x}', start, errors=errors) for i, start in enumerate(starts)]

    def handler(query):
        low, high = seconds(query['query.startTimeMin']), seconds(query['query.startTimeMax'])
        depth = int(query['query.searchDepth'])
        hits = sorted((item for item in items if low <= int(item['minStartTimeUnixNano']) / 1e9 <= high + 0.999),
                      key=lambda item: int(item['minStartTimeUnixNano']))
        return hits[:depth]
    return items, handler


@pytest.mark.asyncio
async def test_slicing_finds_newest_traces():
    starts = [NOW - 1 - i * 3 for i in range(100)]  # 最近 300 秒里 100 条
    items, handler = dataset(starts)
    fake = FakeJaeger()
    fake.summaries = handler
    async with serve(fake) as url, make_ctx(url) as ctx:
        data = await search.search(ctx, {'service': 'svc-a', 'limit': 20})
    assert data['source'] == 'jaeger' and data['count'] == 20
    expected = sorted(items, key=lambda item: -int(item['minStartTimeUnixNano']))[:20]
    assert [t['trace_id'] for t in data['traces']] == [item['traceId'] for item in expected]
    coverage = data['coverage']
    assert coverage['complete'] is False and coverage['saturated'] is False and coverage['requests'] <= 10
    requests = [query for path, query in fake.requests if path == '/api/v3/trace-summaries']
    assert len(requests) == coverage['requests']
    # 第一片是最后 5 分钟；被截断就对半切先查较新的一半
    first, second = requests[0], requests[1]
    assert seconds(first['query.startTimeMax']) == NOW and seconds(first['query.startTimeMin']) == NOW - 300
    assert seconds(second['query.startTimeMin']) == NOW - 150 and first['query.searchDepth'] == '20'
    # 全部条件写进 filter，不用 query.serviceName
    assert all('query.serviceName' not in query and 'query.filter' in query for query in requests)
    assert json.loads(first['query.filter'])['args'][0]['call']['args'][1] == {'scalar': {'value': 'svc-a'}}
    # 结果格式；记住了链路时间（给 trace 定时间范围）
    top = data['traces'][0]
    assert set(top) == {'trace_id', 'start', 'duration_ms', 'root_service', 'root_operation', 'span_count',
                        'error_span_count', 'services', 'url'}
    assert top['error_span_count'] == 0 and top['services'][0]['error_span_count'] == 0  # 零值省略按 0
    assert top['url'] == f'{url}/trace/{top["trace_id"]}' and search.recall(top['trace_id']) == int((NOW - 1) * 1000)
    assert data['query']['cluster'] == {'key': 'k8sname', 'values': ['prod', '蓬莱']}
    assert search.SAME_SPAN_NOTE in data['notes']


@pytest.mark.asyncio
async def test_slicing_whole_window_and_saturation():
    items, handler = dataset([NOW - 100, NOW - 2000, NOW - 3500])
    fake = FakeJaeger()
    fake.summaries = handler
    async with serve(fake) as url, make_ctx(url) as ctx:
        data = await search.search(ctx, {'service': 'svc-a', 'limit': 10})
    assert data['count'] == 3 and data['coverage']['complete'] is True
    windows = [(seconds(q['query.startTimeMin']), seconds(q['query.startTimeMax']))
               for path, q in fake.requests if path == '/api/v3/trace-summaries']
    assert windows == [(NOW - 300, NOW), (NOW - 900, NOW - 300), (NOW - 2100, NOW - 900), (NOW - 3600, NOW - 2100)]
    # 同一秒里太多：切到 2 秒以内仍被截断，只能接受（saturated）
    items, handler = dataset([NOW - 1] * 30)
    fake.summaries = handler
    async with serve(fake) as url, make_ctx(url) as ctx:
        data = await search.search(ctx, {'service': 'svc-a', 'limit': 10, 'lookback': '5m'})
    assert data['coverage']['saturated'] is True and data['count'] == 10
    assert any('2 秒内的时间片仍被截断' in note for note in data['notes'])


@pytest.mark.asyncio
async def test_slicing_budget_and_partial_failure(monkeypatch):
    items, handler = dataset([NOW - 1 - i for i in range(3000)])  # 每次都被截断
    calls = []

    def flaky(query):
        calls.append(query)
        if len(calls) == 3:
            return web.json_response({'error': {'httpCode': 500, 'message': 'clickhouse timeout'}}, status=500)
        return handler(query)

    fake = FakeJaeger()
    fake.summaries = flaky
    async with serve(fake) as url, make_ctx(url) as ctx:
        data = await search.search(ctx, {'service': 'svc-a', 'limit': 50})
    assert data['coverage']['requests'] == 2 and len(calls) == 3
    assert any('第 3 次查询失败' in note and 'clickhouse timeout' in note for note in data['notes'])
    # 用完 10 次请求就停：每片都被截断，切到 2 秒以内才只有 1 条
    counter = itertools.count()

    def endless(query):
        low, high = seconds(query['query.startTimeMin']), seconds(query['query.startTimeMax'])
        count = int(query['query.searchDepth']) if high - low > 2 else 1
        return [summary(f'{next(counter):032x}', high - 0.5) for _ in range(count)]

    fake.summaries = endless
    async with serve(fake) as url, make_ctx(url) as ctx:
        data = await search.search(ctx, {'service': 'svc-a', 'limit': 50, 'lookback': '2d'})
    assert data['coverage']['requests'] == 10 and data['coverage']['complete'] is False
    assert data['coverage']['saturated'] is False and data['count'] < 50
    assert any('时间窗口没有查完' in note for note in data['notes'])
    fake.summaries = handler
    # 总耗时超过 2×timeout 就不再发新请求
    ticks = iter(range(0, 1000, 40))
    monkeypatch.setattr(search, '_clock', lambda: next(ticks))
    async with serve(fake) as url, make_ctx(url) as ctx:
        data = await search.search(ctx, {'service': 'svc-a', 'limit': 50})
    assert data['coverage']['requests'] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('caps, body, status, code, message', [
    (None, {'service': 'a'}, 409, 'version_unsupported', '版本低于 2.23'),
    ({'search': {}}, {'service': 'a'}, 409, 'version_unsupported', '没有 filter'),
    ({'search': {'filter': {'operators': ['and', 'eq'], 'levels': ['span', 'resource']}}}, {'service': 'a'}, 409,
     'version_unsupported', '不支持运算符 or'),
    ({'search': {'filter': {'operators': ['and', 'or', 'eq'], 'levels': ['span', 'resource']}}}, {}, 400,
     'invalid_request', '必须指定服务'),
    ({'search': {'filter': {'operators': ['and', 'or', 'eq'], 'levels': ['span', 'resource']}}},
     {'service': 'a', 'min_duration': '1s'}, 400, 'invalid_request', 'gte'),
])
async def test_capabilities_problems(caps, body, status, code, message):
    fake = FakeJaeger()
    fake.capabilities = caps
    async with serve(fake) as url, make_ctx(url) as ctx:
        with pytest.raises(common.TracingError) as info:
            await search.search(ctx, body)
    assert (info.value.status, info.value.code) == (status, code) and message in info.value.message
    assert fake.paths('/api/v3/trace-summaries') == []


@pytest.mark.asyncio
async def test_capabilities_cached_and_jaeger_errors():
    fake = FakeJaeger()
    fake.fail = {'/api/v3/trace-summaries': (400, {'error': {'httpCode': 400,
                                                             'message': 'it cannot be combined with [service_name]'}})}
    async with serve(fake) as url, make_ctx(url) as ctx:
        for _ in range(2):
            with pytest.raises(common.TracingError) as info:
                await search.search(ctx, {'service': 'a'})
            assert info.value.code == 'jaeger_rejected' and info.value.status == 400
            assert 'it cannot be combined with [service_name]' in info.value.message
    assert len(fake.paths('/api/v3/capabilities')) == 1  # 10 分钟缓存
    fake.fail = {'/api/v3/trace-summaries': (503, {'error': {'httpCode': 503, 'message': 'storage down'}})}
    async with serve(fake) as url, make_ctx(url) as ctx:
        with pytest.raises(common.TracingError) as info:
            await search.search(ctx, {'service': 'a'})
    assert info.value.code == 'jaeger_unavailable' and info.value.status == 502 and 'storage down' in info.value.message
    async with make_ctx('http://127.0.0.1:9') as ctx:  # 连不上
        with pytest.raises(common.TracingError) as info:
            await search.search(ctx, {'service': 'a'})
    assert info.value.code == 'jaeger_unavailable' and '无法访问 Jaeger' in info.value.message


# ---------------------------------------------------------------------------
# contains：取完整链路扫描
# ---------------------------------------------------------------------------
def full_trace(trace_id, start_s, path, service='svc-a', pod='svc-a-pod-1'):
    builder = TraceBuilder(trace_id)
    offset = (start_s - 1_795_203_249.935) * 1000
    builder.span('a1', 'GET', service=service, start=offset, duration=5, attributes={'url.path': path}, pod=pod)
    builder.span('a2', 'SELECT x', parent='a1', kind='client', start=offset + 1, duration=1, service=service,
                 attributes={'db.statement': 'select * from t where code = ?'})
    return builder.doc()['result']['resourceSpans']


@pytest.mark.asyncio
async def test_contains_scans_full_traces():
    traces = [(f'{i:032x}', NOW - 10 - i, f'/orders/{"VIP" if i % 3 == 0 else "std"}-{i}') for i in range(12)]

    def handler(query):
        low, high = seconds(query['query.startTimeMin']), seconds(query['query.startTimeMax'])
        entries = []
        for trace_id, start, path in traces:
            if low <= start <= high + 0.999:
                entries += full_trace(trace_id, start, path)
        return {'result': {'resourceSpans': entries[:2 * int(query['query.searchDepth'])]}}

    fake = FakeJaeger()
    fake.traces = handler
    async with serve(fake) as url, make_ctx(url) as ctx:
        data = await search.search(ctx, {'service': 'svc-a', 'contains': {'path': 'VIP'}, 'lookback': '5m'})
    assert [t['trace_id'] for t in data['traces']] == [f'{i:032x}' for i in (0, 3, 6, 9)]
    assert data['scan'] == {'scanned': 12, 'matched': 4, 'complete': True}
    assert data['traces'][0]['span_count'] == 2 and data['traces'][0]['root_service'] == 'svc-a'
    queries = [query for path, query in fake.requests if path == '/api/v3/traces']
    assert queries and all(query['query.searchDepth'] == '20' for query in queries)
    assert any('contains 由 KubeDoor 扫描' in note for note in data['notes'])
    # 条件要落在同一个 Span 上：path 在 GET 上、db.statement 在 SELECT 上，不算命中；指定服务时只看该服务的 Span
    trace = ops.model.parse_document({'result': {'resourceSpans': full_trace('ab', NOW, '/vip')}})[0]
    assert search.contains_match(trace, {'path': 'VIP'}, 'svc-a', FIELDS)
    assert not search.contains_match(trace, {'path': 'vip', 'db.statement': 'code'}, '', FIELDS)
    assert not search.contains_match(trace, {'path': 'vip'}, 'svc-b', FIELDS)
    assert search.contains_match(trace, {'operation': 'select', 'k8s_pod': 'POD-1'}, '', FIELDS)


@pytest.mark.asyncio
async def test_contains_scan_limits(monkeypatch):
    counter = itertools.count()

    def many(query):  # 每次 20 条（新的）完整链路，开始时间都在这一片里
        low = seconds(query['query.startTimeMin'])
        entries = []
        for _ in range(20):
            entries += full_trace(f'{next(counter):032x}', low + 1, '/x')
        return {'result': {'resourceSpans': entries}}

    fake = FakeJaeger()
    fake.traces = many
    async with serve(fake) as url, make_ctx(url) as ctx:
        data = await search.search(ctx, {'service': 'svc-a', 'contains': {'path': 'nothing'}, 'lookback': '7d'})
    assert data['scan']['scanned'] == 200 and data['scan']['matched'] == 0 and data['scan']['complete'] is False
    # 累计超过 64MB 就停
    monkeypatch.setattr(search, 'SCAN_MAX_BYTES', 30000)
    async with serve(fake) as url, make_ctx(url) as ctx:
        data = await search.search(ctx, {'service': 'svc-a', 'contains': {'path': 'nothing'}, 'lookback': '7d'})
    assert any('超过 64 MB' in note for note in data['notes']) and data['scan']['complete'] is False


# ---------------------------------------------------------------------------
# ClickHouse：SQL 构造、参数绑定、转义、readonly 重试
# ---------------------------------------------------------------------------
def test_search_sql_binds_every_value():
    spec = spec_of({'service': "svc'; DROP TABLE x --", 'operation': 'GET /x', 'span_kind': 'server', 'error': True,
                    'status_codes': [504, '4xx'], 'status_min': 500, 'min_duration': '500ms', 'max_duration': '3s',
                    'method': 'POST', 'path': '/a', 'host': 'api.example.com', 'k8s_pod': 'p-1',
                    'k8s_namespace': 'ns', 'contains': {'query': 'id=1', 'operation': 'GET'}, 'limit': 7})
    sql, params = search.search_sql(spec, clickhouse.table_ref('jaeger', 'trace_search'), ['prod', '蓬莱'], 'k8sname')
    assert 'FROM `jaeger`.`trace_search`' in sql and 'LIMIT 1 BY trace_id' in sql and 'ORDER BY start_time DESC' in sql
    for value in ("svc'", 'DROP TABLE', 'GET /x', 'POST', '/a', 'api.example.com', 'p-1', 'id=1', '504', '500',
                  'prod', '蓬莱', '3000'):
        assert value not in sql, value  # 值一律不进 SQL
    assert "span_status = 'Error'" in sql
    assert 'k8s_cluster IN {clusters:Array(String)}' in sql and 'service_name = {service:String}' in sql
    assert '(http_status IN {codes:Array(Int64)} OR (http_status >= {smin0:Int64} AND http_status <= {smax0:Int64})' \
           ' OR (http_status >= {smin1:Int64} AND http_status <= {smax1:Int64}))' in sql
    assert 'positionCaseInsensitive(url_query, {c0:String}) > 0' in sql and 'peer = {host:String}' in sql
    assert params.values['service'] == ('String', "svc'; DROP TABLE x --")
    assert params.values['clusters'] == ('Array(String)', ['prod', '蓬莱'])
    assert params.values['smin0'] == ('Int64', 400) and params.values['smax1'] == ('Int64', 599)
    assert params.values['dmin'] == ('Float64', 500.0) and params.values['dmax'] == ('Float64', 3000.0)
    assert params.values['limit'] == ('UInt32', 7)
    assert params.values['s'] == ('Int64', int((NOW - 3600) * 1000)) and params.values['e'] == ('Int64', int(NOW * 1000))
    # 没配集群字段：不按集群过滤
    sql, params = search.search_sql(spec_of({}), clickhouse.table_ref('jaeger', 'trace_search'), [], '')
    assert 'k8s_cluster' not in sql.split('FROM')[1] and 'clusters' not in params.values


@pytest.mark.parametrize('name', ['a`b', '1abc', 'a.b', '', 'x' * 65, None])
def test_identifiers_are_validated(name):
    with pytest.raises(common.TracingError):
        clickhouse.ident(name)


def test_param_encoding():
    assert clickhouse.escape_string('a\\b\tc\nd\re\'f%中') == 'a\\\\b\\tc\\nd\\re\'f%中'
    assert clickhouse.array_literal(["it's", 'a\\b', 'x\ny', '\t%中']) == "['it\\'s','a\\\\b','x\\ny','\\t%中']"
    assert clickhouse.encode_param('Array(Int64)', [500, 502]) == '[500,502]'
    assert clickhouse.encode_param('UInt32', 7) == '7' and clickhouse.encode_param('Float64', 1.5) == '1.5'
    with pytest.raises(ValueError):
        clickhouse.encode_param('Map(String, String)', {})


SPECIALS = ['back\\slash', 'new\nline', "single'quote", 'tab\there', 'per%cent+plus space', '中文值', '&=?#']


@pytest.mark.asyncio
async def test_clickhouse_params_round_trip_and_auth():
    fake = FakeClickHouse(lambda sql, query: compact(['ok'], [[1]]))
    async with serve(fake, 'POST') as url, make_ctx(ch_url=url) as ctx:
        params = clickhouse.Params()
        names = [params.add('v', 'String', value) for value in SPECIALS]
        array = params.add('arr', 'Array(String)', SPECIALS)
        result = await ctx.clickhouse.query(f'SELECT {", ".join(names)}, {array}', params)
    assert clickhouse.rows(result) == [{'ok': 1}]
    [request] = fake.requests
    # ClickHouse 按 TSV 规则解析 String 参数、按数组字面量解析 Array(String)：用独立实现解回来要和原值一样
    keys = ['v'] + [f'v{i}' for i in range(2, len(SPECIALS) + 1)]
    assert [tsv_unescape(request['query'][f'param_{key}']) for key in keys] == SPECIALS
    assert parse_array_literal(request['query']['param_arr']) == SPECIALS
    raw = request['raw_query']
    assert '%2B' in raw and '%20' in raw and '+' not in raw  # 空格编码成 %20、+ 编码成 %2B
    assert request['sql'].endswith('FORMAT JSONCompact') and '{v:String}' in request['sql']
    assert request['query']['readonly'] == '2' and request['query']['max_execution_time'] == '20'
    assert request['query']['max_result_rows'] == '10000'
    assert request['headers']['X-ClickHouse-User'] == 'reader' and request['headers']['X-ClickHouse-Key'] == 'ch-pass'


@pytest.mark.asyncio
async def test_clickhouse_readonly_retry_and_errors():
    def handler(sql, query):
        if 'readonly' in query:
            return web.Response(status=500, text="Code: 164. DB::Exception: Cannot modify 'readonly' setting in "
                                                 'readonly mode. (READONLY) (version 26.7.1.1)')
        return compact(['version'], [['26.7.1.1']])

    fake = FakeClickHouse(handler)
    async with serve(fake, 'POST') as url, make_ctx(ch_url=url) as ctx:
        first = await ctx.clickhouse.query('SELECT version() AS version')
        second = await ctx.clickhouse.query('SELECT version() AS version')
    assert clickhouse.rows(first) == clickhouse.rows(second) == [{'version': '26.7.1.1'}]
    # 第一次带设置被拒、去掉设置重试；之后这个连接配置不再带设置
    assert ['readonly' in request['query'] for request in fake.requests] == [True, False, False]

    cases = [
        (web.Response(status=404, text='Code: 60. DB::Exception: Unknown table expression identifier'), 400,
         'clickhouse_rejected', 'Unknown table'),
        (web.Response(status=500, text='Code: 241. DB::Exception: Memory limit exceeded'), 502,
         'clickhouse_unavailable', 'Memory limit'),
        (web.Response(status=408, text='Code: 159. DB::Exception: Timeout exceeded: elapsed 20 seconds'), 502,
         'clickhouse_unavailable', 'Timeout exceeded'),
        (web.Response(status=200, text='not json'), 502, 'clickhouse_unavailable', '不是 JSON'),
        (web.Response(status=200, text='{"data": [1]}\nCode: 241. DB::Exception: oops'), 400, 'clickhouse_rejected',
         'oops'),
        (web.json_response({'rows': 0}), 502, 'clickhouse_unavailable', '没有 data'),
    ]
    for response, status, code, message in cases:
        fake = FakeClickHouse(lambda sql, query, response=response: response)
        async with serve(fake, 'POST') as url, make_ctx(ch_url=url) as ctx:
            with pytest.raises(common.TracingError) as info:
                await ctx.clickhouse.query('SELECT 1')
        assert (info.value.status, info.value.code) == (status, code) and message in info.value.message
    async with make_ctx(ch_url='http://127.0.0.1:9') as ctx:
        with pytest.raises(common.TracingError) as info:
            await ctx.clickhouse.query('SELECT 1')
    assert info.value.code == 'clickhouse_unavailable'


def ch_search_row(trace_id, ts, **values):
    row = {'trace_id': trace_id, 'ts': str(ts), 'service_name': 'gateway', 'namespace': 'infra', 'kind': 'server',
           'span_status': 'Error', 'operation': 'POST bizme', 'url_path': '/a', 'http_method': 'POST',
           'http_status': '504', 'duration_ms': 7001.53, 'peer': 'api.example.com', 'pod': 'gateway-1',
           'url_query': 'id=1&token=abc', 'k8s_cluster': '蓬莱'}
    row.update(values)
    return row


def ch_result(rows_):
    names = list(rows_[0]) if rows_ else []
    return compact(names, [[row[name] for name in names] for row in rows_])


@pytest.mark.asyncio
async def test_routing_clickhouse_vs_jaeger():
    rows_ = [ch_search_row('ABC', 1_795_203_500_123), ch_search_row('def', 1_795_203_400_000, http_status='0',
                                                                      span_status='Unset', kind='', k8s_cluster='x')]
    fake_ch = FakeClickHouse(lambda sql, query: ch_result(rows_))
    fake_jaeger = FakeJaeger()
    async with serve(fake_ch, 'POST') as ch_url, serve(fake_jaeger) as jaeger_url, \
            make_ctx(jaeger_url, ch_url=ch_url) as ctx:
        data = await search.search(ctx, {'service': 'gateway', 'status_codes': ['5xx'], 'contains': {'query': 'ID='}})
        assert data['source'] == 'clickhouse' and data['count'] == 2 and fake_jaeger.requests == []
        first, second = data['traces']
        assert first == {'trace_id': '00000000000000000000000000000abc', 'start': '2026-11-21T03:38:20.123+08:00',
                         'service': 'gateway', 'operation': 'POST bizme', 'kind': 'server', 'status': 'error',
                         'http_status': 504, 'method': 'POST', 'path': '/a', 'query': 'id=1&token=***',
                         'duration_ms': 7001.5, 'peer': 'api.example.com', 'k8s_pod': 'gateway-1',
                         'k8s_namespace': 'infra', 'cluster': {'value': '蓬莱', 'env': 'prod'},
                         'url': f'{jaeger_url}/trace/00000000000000000000000000000abc'}
        assert second['http_status'] is None and second['status'] == 'unset' and second['kind'] == 'unspecified'
        assert second['cluster'] == {'value': 'x', 'env': None}
        assert data['coverage']['complete'] is True and search.recall(first['trace_id']) == 1_795_203_500_123
        # 加速表没有的条件：改走 Jaeger，并说明
        fake_jaeger.summaries = lambda query: [summary('f' * 32, NOW - 5)]
        data = await search.search(ctx, {'service': 'gateway', 'route': '/x'})
        assert data['source'] == 'jaeger' and data['notes'][0] == '这些条件加速表没有，改用 Jaeger 查询：route'
        assert len(fake_ch.requests) == 1
    # ClickHouse 出错：不自动改走 Jaeger
    fake_ch = FakeClickHouse(lambda sql, query: web.Response(status=500, text='Code: 210. Connection refused'))
    fake_jaeger = FakeJaeger()
    async with serve(fake_ch, 'POST') as ch_url, serve(fake_jaeger) as jaeger_url, \
            make_ctx(jaeger_url, ch_url=ch_url) as ctx:
        with pytest.raises(common.TracingError) as info:
            await search.search(ctx, {'service': 'gateway'})
    assert info.value.code == 'clickhouse_unavailable' and fake_jaeger.requests == []


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------
def test_stats_params_and_sql():
    with pytest.raises(common.TracingError, match='统计只支持加速表里有的条件'):
        search.parse_stats({'route': '/x'})
    for body, message in (({'group_by': ['a', 'b', 'c']}, '最多 2 个'), ({'group_by': ['nope']}, 'group_by 只能是'),
                          ({'group_by': ['time', 'time']}, '不能重复'), ({'interval': '2m'}, 'interval 只能是'),
                          ({'top': 0}, '1~100'), ({'limit': 5}, '不支持的参数')):
        with pytest.raises(common.TracingError, match=re.escape(message)):
            search.parse_stats(body)
    assert search.parse_stats({})['interval'] == 60
    assert search.parse_stats({'lookback': '6h'})['interval'] == 300
    assert search.parse_stats({'lookback': '1d'})['interval'] == 600
    assert search.parse_stats({'lookback': '3d'})['interval'] == 3600
    table = clickhouse.table_ref('jaeger', 'trace_search')
    spec = search.parse_stats({'service': 'gw', 'group_by': ['k8s_pod', 'status_code'], 'top': 5})
    sql, params, totals = search.stats_sql(spec, table, ['prod'], 'k8sname')
    assert 'pod AS g_k8s_pod, http_status AS g_status_code' in sql and 'GROUP BY g_k8s_pod, g_status_code' in sql
    assert 'ORDER BY spans DESC\nLIMIT {top:UInt32}' in sql and 'quantile(0.95)(duration_ms) AS p95_ms' in sql
    assert totals.startswith('SELECT count() AS spans') and 'GROUP BY' not in totals and 'gw' not in sql
    spec = search.parse_stats({'group_by': ['time', 'k8s_pod'], 'interval': '5m'})
    sql, params, _ = search.stats_sql(spec, table, ['prod'], 'k8sname')
    assert 'toStartOfInterval(start_time, INTERVAL {iv:UInt32} SECOND)' in sql and params.values['iv'] == ('UInt32', 300)
    # 时间序列不按 top 截断；另一个维度只取 Span 数最多的 top 个
    assert 'pod IN (SELECT pod FROM `jaeger`.`trace_search` WHERE' in sql and 'LIMIT {top:UInt32})' in sql
    assert 'ORDER BY g_time ASC, spans DESC\nLIMIT {cap:UInt32}' in sql
    sql, _, totals = search.stats_sql(search.parse_stats({}), table, [], '')
    assert sql is None and 'k8s_cluster' not in totals


@pytest.mark.asyncio
async def test_stats_results():
    def handler(sql, query):
        if 'g_time' in sql:
            return compact(['g_time', 'g_status', 'spans', 'errors', 'http_4xx', 'http_5xx', 'p50_ms', 'p95_ms',
                            'p99_ms', 'max_ms'],
                           [['1795203540000', 'Error', '12', '12', '0', '12', 7001.04, 7002.5, 7003.33, 7004],
                            ['1795203600000', 'Ok', '3', '0', '1', '0', 1.26, 2, 3, 'nan']])
        return compact(['spans', 'errors', 'http_4xx', 'http_5xx', 'p50_ms', 'p95_ms', 'p99_ms', 'max_ms'],
                       [['15', '12', '1', '12', 7000.0, 7002.5, 7003.3, 7004.0]])

    fake = FakeClickHouse(handler)
    async with serve(fake, 'POST') as url, make_ctx(ch_url=url) as ctx:
        data = await search.stats(ctx, {'service': 'gw', 'group_by': ['time', 'status'], 'lookback': '15m'})
    assert data['source'] == 'clickhouse' and data['interval'] == '1m' and data['group_by'] == ['time', 'status']
    assert data['rows'][0] == {'time': '2026-11-21T03:39:00.000+08:00', 'status': 'error', 'spans': 12, 'errors': 12,
                               'http_4xx': 0, 'http_5xx': 12, 'p50_ms': 7001.0, 'p95_ms': 7002.5, 'p99_ms': 7003.3,
                               'max_ms': 7004.0}
    assert data['rows'][1]['status'] == 'ok' and data['rows'][1]['max_ms'] == 0.0
    assert data['totals']['spans'] == 15 and data['conditions'] == ['服务 = gw']
    assert data['notes'] == ['统计的是命中条件的 Span 数；看接口请求量请加 span_kind=server']
    assert len(fake.requests) == 2 and all(r['query']['param_service'] == 'gw' for r in fake.requests)
    # 不分组（group_by 不传或传空数组）：rows 为空，只返回 totals
    for body in ({}, {'group_by': []}):
        fake.requests.clear()
        async with serve(fake, 'POST') as url, make_ctx(ch_url=url) as ctx:
            data = await search.stats(ctx, body)
        assert data['rows'] == [] and data['group_by'] == [] and data['totals']['spans'] == 15
        assert 'interval' not in data and len(fake.requests) == 1 and 'GROUP BY' not in fake.requests[0]['sql']
    async with make_ctx() as ctx:  # 没配 ClickHouse
        with pytest.raises(common.TracingError) as info:
            await search.stats(ctx, {})
    assert info.value.status == 409 and info.value.code == 'not_supported' and 'metrics' in info.value.message
