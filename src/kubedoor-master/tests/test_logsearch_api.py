"""历史日志 AI 内部接口（logsearch/api.py、ops.py）：鉴权和错误码、7 个操作在 ClickHouse / Elasticsearch 上的查询构造
（每条查询都带集群绑定条件）、打码和截断、原生查询的校验和包装。全部用本地假服务。"""
import json
import re
from datetime import datetime
from types import MappingProxyType

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from auth.principal import SYSTEM, Principal
from logsearch import api as log_api
from logsearch import clickhouse, common, elastic, ops, schema
from logsearch import config as log_config
from logsearch_fakes import (CH_COLUMNS, ES_FIELDS, NOW, NOW_MS, FakeClickHouse, FakeElastic, ast_fixture,
                             ch_error, compact, hit, hits, sample_config, serve)
from tracing import fields as trace_fields

TRACE = '0af7651916cd43dd8448eb211c80319c'
ALIASES = {'prod-a': ['生产A'], 'prod-b': []}


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    monkeypatch.setattr(common, '_clock', lambda: NOW)
    monkeypatch.setenv('AI_ENCRYPTION_KEY', '44' * 32)

    async def aliases():
        return ALIASES

    async def tracing_cfg():
        return {'enabled': False, 'url': ''}

    monkeypatch.setattr(trace_fields, 'load_aliases', aliases)
    monkeypatch.setattr(ops.tracing_config, 'load_config', tracing_cfg)
    schema.reset()
    elastic.reset_caches()
    clickhouse.reset_state()
    common.reset_limiters()


class FakeStore:
    def __init__(self, value=None, broken=False):
        self.broken = broken
        self.row = None if value is None else {'value': value, 'updated_by': 'admin',
                                               'updated_at': datetime(2026, 11, 23, 10, 0)}

    async def get_setting(self, key, default=None):
        assert key == log_config.SETTING_KEY
        if self.broken:
            raise RuntimeError('db down')
        return self.row['value'] if self.row else default

    async def get_setting_row(self, key):
        return self.row

    async def put_setting_row(self, key, value, updated_by):
        self.row = {'value': json.loads(json.dumps(value)), 'updated_by': updated_by,
                    'updated_at': datetime(2026, 11, 23, 11, 0)}
        return self.row


def internal(role='read', **kwargs):
    return Principal('alice', role, user_id=7, via='internal', **kwargs)


def make_app(monkeypatch, store, principal=None, clients=None):
    monkeypatch.setattr(log_api, 'store', store)
    monkeypatch.setattr(log_config, 'store', store)
    principal = principal or internal()

    @web.middleware
    async def inject(request, handler):
        request['principal'] = principal
        return await handler(request)

    app = web.Application(middlewares=[inject])
    log_api.register_routes(app, clients)
    return app


def stored(ch_url='http://127.0.0.1:9', es_url='http://127.0.0.1:9', **overrides):
    return log_config.normalize_config(sample_config(ch_url, es_url, **overrides))


async def call(client, op, body=None, env='prod-a'):
    params = {'env': env} if env is not None else {}
    resp = await client.post(f'/api/ai/internal/logs/{op}', params=params, json=body if body is not None else {})
    return resp.status, await resp.json()


def select_columns(sql):
    head = sql.split('\nFROM', 1)[0]
    return ['kd_ms'] + re.findall(r'`([^`]+)`', head.split('AS kd_ms', 1)[1])


def rows_handler(rows):
    """明细查询：按 SELECT 的列返回 rows（dict 列表）里的值"""
    def handler(sql, query):
        names = select_columns(sql)
        return compact(names, [[row.get(name) for name in names] for row in rows])

    return handler


def ch_fake(rows=(), **routes):
    fake = FakeClickHouse(columns={'logs.access': CH_COLUMNS})
    fake.route('AS kd_ms', rows_handler(list(rows)))
    for fragment, response in routes.items():
        fake.route(fragment, response)
    return fake


def es_fake():
    fake = FakeElastic()
    fake.fields['app-logs-*'] = ES_FIELDS
    return fake


# ---------------------------------------------------------------------------
# 鉴权、开关、错误码
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize('principal, env, op, status, code', [
    (Principal('alice', 'admin', user_id=7), 'prod-a', 'sources', 403, 'forbidden'),  # 浏览器会话不是可信的 AI 回调
    (SYSTEM, 'prod-a', 'sources', 403, 'forbidden'),
    (internal(role='none'), 'prod-a', 'sources', 403, 'permission_denied'),
    (internal(clusters=MappingProxyType({'prod-a': 'none'})), 'prod-a', 'sources', 403, 'permission_denied'),
    (internal(), None, 'sources', 400, 'invalid_request'),
    (internal(), 'prod-a', 'dump', 400, 'invalid_request'),
])
async def test_internal_auth_and_validation(monkeypatch, principal, env, op, status, code):
    async with TestClient(TestServer(make_app(monkeypatch, FakeStore(stored()), principal))) as client:
        got_status, data = await call(client, op, env=env)
    assert (got_status, data['code'], data['success']) == (status, code, False)


@pytest.mark.asyncio
@pytest.mark.parametrize('store, status, code, message', [
    (FakeStore(None), 400, 'not_configured', '系统管理员还没有配置历史日志（系统管理 → 历史日志）'),
    (FakeStore({'enabled': False, 'connections': [{'id': 'x'}]}), 400, 'not_configured', '系统管理员还没有配置'),
    (FakeStore({'enabled': True}), 400, 'not_configured', '系统管理员还没有配置'),
    (FakeStore(broken=True), 503, 'config_unavailable', '读取历史日志配置失败'),
])
async def test_internal_requires_enabled_config(monkeypatch, store, status, code, message):
    async with TestClient(TestServer(make_app(monkeypatch, store))) as client:
        got_status, data = await call(client, 'search', {'source': 'ingress'})
    assert got_status == status and data['code'] == code and data['message'].startswith(message)


@pytest.mark.asyncio
async def test_bad_body_and_unexpected_errors(monkeypatch):
    from loguru import logger

    async def boom(ctx, body):
        secret = 'TOP-' + 'SECRET'  # 栈上的变量值不能进日志
        raise KeyError(len(secret))

    logs = []
    handler_id = logger.add(lambda message: logs.append(str(message)), level='DEBUG')
    monkeypatch.setitem(log_api.OPS, 'sources', boom)
    async with TestClient(TestServer(make_app(monkeypatch, FakeStore(stored())))) as client:
        resp = await client.post('/api/ai/internal/logs/search', params={'env': 'prod-a'}, data='not json',
                                 headers={'Content-Type': 'application/json'})
        bad = await resp.json()
        array = await client.post('/api/ai/internal/logs/search', params={'env': 'prod-a'}, json=[1])
        status, crash = await call(client, 'sources')
        empty = await client.post('/api/ai/internal/logs/search', params={'env': 'prod-a'})
        empty_data = await empty.json()
    logger.remove(handler_id)
    assert any('历史日志 sources 出错: KeyError' in line for line in logs)
    assert not any('TOP-SECRET' in line for line in logs)
    assert resp.status == 400 and '请求体要是 JSON 对象' in bad['message'] and array.status == 400
    assert status == 500 and crash['code'] == 'internal_error' and 'KeyError' in crash['message']
    assert empty.status == 400 and 'source 必填' in empty_data['message']


@pytest.mark.asyncio
async def test_source_resolution_errors(monkeypatch):
    async with TestClient(TestServer(make_app(monkeypatch, FakeStore(stored())))) as client:
        missing = await call(client, 'search', {'source': 'nope'})
        unbound = await call(client, 'search', {'source': 'other'})
        disabled = await call(client, 'search', {'source': 'old'})
        other_env = await call(client, 'search', {'source': 'app'}, env='prod-b')
        none_env = await call(client, 'search', {'source': 'app'}, env='dev')
    assert missing[0] == 404 and missing[1]['code'] == 'source_not_found'
    assert '当前集群 prod-a 可用的日志源：ingress、app' in missing[1]['message']
    assert unbound[0] == 404 and unbound[1]['code'] == 'source_not_bound' and 'ingress、app' in unbound[1]['message']
    assert disabled[0] == 400 and disabled[1]['code'] == 'source_disabled'
    assert other_env[1]['code'] == 'source_not_bound' and '可用的日志源：ingress' in other_env[1]['message']
    assert '当前集群 dev 还没有绑定日志源' in none_env[1]['message']


# ---------------------------------------------------------------------------
# sources
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_sources_list_and_detail(monkeypatch):
    async def tracing_cfg():
        return {'enabled': True, 'url': 'http://jaeger:16686', 'ui_url': ''}

    monkeypatch.setattr(ops.tracing_config, 'load_config', tracing_cfg)
    ch, es = ch_fake(), es_fake()
    async with serve(ch, 'POST') as ch_url, serve(es) as es_url:
        async with TestClient(TestServer(make_app(monkeypatch, FakeStore(stored(ch_url, es_url))))) as client:
            _, listing = await call(client, 'sources')
            _, detail = await call(client, 'sources', {'source': 'ingress'})
            _, app_detail = await call(client, 'sources', {'source': 'app'})
            es.version = '8.13.4'
            elastic.reset_caches()
            _, old_es = await call(client, 'sources')
            bad_status, bad = await call(client, 'sources', {'index': 'x'})
            _, empty = await call(client, 'sources', env='dev')
    data = listing['data']
    assert data['env'] == 'prod-a' and data['architecture'].startswith('- 入口网关') and data['cluster_note'] == '生产 A 集群'
    assert data['tracing'] == {'configured': True}
    assert data['correlation_keys'][0] == {'key': 'trace_id', 'label': '链路 ID', 'description': 'W3C',
                                           'pattern': '[0-9a-f]{32}', 'trace': True}
    ingress, app = data['sources']
    assert ingress['description'].endswith('…（看全文传 source）') and len(ingress['description']) < 620
    assert ingress == {**ingress, 'id': 'ingress', 'type': 'clickhouse', 'target': 'logs.access', 'binding_note': '共享表',
                       'correlations': ['request_id', 'trace_id'], 'raw_query': ['sql']}
    assert app['raw_query'] == ['dsl', 'esql'] and app['type'] == 'elasticsearch'
    assert data['unbound'] == [{'id': 'other', 'name': '没绑定的日志'}]  # 停用的 old 不列
    assert old_es['data']['sources'][1]['raw_query'] == ['dsl']  # 8.14 以下没有 ES|QL
    source = detail['data']['source']
    assert source['description'].startswith('入口网关') and len(source['description']) > 600
    assert source['conditions'] == ['集群绑定：env ∈ {prod-a, prod-a2}'] and source['time_field'] == 'start_time'
    assert source['correlations'] == [{'key': 'request_id', 'field': 'request_id'},
                                      {'key': 'trace_id', 'text': 'TID:{value}'}]
    names = [item['name'] for item in detail['data']['schema']]
    assert names[0] == 'start_time' and names.index('user_token') > names.index('message')
    upstream = next(item for item in detail['data']['schema'] if item['name'] == 'upstream_cluster')
    assert upstream['description'] == '语义：服务 / 应用；上游服务' and detail['data']['schema_truncated'] is False
    assert app_detail['data']['source']['raw_query'] == ['dsl', 'esql']
    assert any(item['name'] == 'message.keyword' for item in app_detail['data']['schema'])
    assert bad_status == 400 and 'index 请用 source' in bad['message']
    assert empty['data']['sources'] == [] and '还没有绑定日志源' in empty['data']['notes'][0]


@pytest.mark.asyncio
async def test_source_detail_schema_failure_is_a_note(monkeypatch):
    ch = FakeClickHouse()  # 没有这张表的字段
    async with serve(ch, 'POST') as ch_url:
        async with TestClient(TestServer(make_app(monkeypatch, FakeStore(stored(ch_url))))) as client:
            status, detail = await call(client, 'sources', {'source': 'ingress'})
    assert status == 200 and detail['data']['schema'] == []
    assert '取实时字段表失败' in detail['data']['notes'][0] and '没有字段' in detail['data']['notes'][0]


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------
def ingress_rows(count=3):
    return [{'kd_ms': NOW_MS - index * 1000, 'path': f'/order/{index}', 'status': 504, 'duration': 3012.5,
             'method': 'GET', 'host': 'api.example.com', 'message': f'upstream timeout TID:{TRACE}',
             'level': 'ERROR', 'namespace': 'shop', 'pod': 'gw-7d9f8-abcde', 'upstream_cluster': 'order',
             'request_id': f'req-{index}', 'user_token': 'tok-secret'} for index in range(count)]


@pytest.mark.asyncio
async def test_search_clickhouse(monkeypatch):
    ch = ch_fake(ingress_rows())
    async with serve(ch, 'POST') as ch_url:
        async with TestClient(TestServer(make_app(monkeypatch, FakeStore(stored(ch_url))))) as client:
            status, result = await call(client, 'search', {'source': 'ingress', 'keyword': 'timeout',
                                                           'filters': [{'field': 'status', 'op': 'gte', 'value': 500}],
                                                           'lookback': '2h', 'limit': 10})
            _, chosen = await call(client, 'search', {'source': 'ingress', 'fields': ['path', 'user_token'],
                                                      'order': 'asc'})
            bad = await call(client, 'search', {'source': 'ingress', 'fields': ['statuz']})
            bad_order = await call(client, 'search', {'source': 'ingress', 'order': 'up'})
            bad_limit = await call(client, 'search', {'source': 'ingress', 'limit': 201})
    assert status == 200
    data = result['data']
    assert data['source'] == 'ingress' and data['total'] is None and data['total_relation'] is None
    assert data['window'] == {'from': '2026-11-23T14:00:00.000+08:00', 'to': '2026-11-23T16:00:00.000+08:00'}
    assert data['conditions'] == ['集群绑定：env ∈ {prod-a, prod-a2}', '正文包含「timeout」', '状态码 ≥ 500']
    assert data['stats'] == {'elapsed_ms': 15, 'read_rows': 1234} and data['returned'] == 3
    row = data['rows'][0]
    assert row['@time'] == '2026-11-23T16:00:00.000+08:00' and row['path'] == '/order/0' and row['status'] == 504
    assert row['host'] == '***' and row['trace_id'] == TRACE and 'user_token' not in row  # host 在 mask_fields 里
    sql = ch.sqls('AS kd_ms')[0]
    request = next(item for item in ch.requests if 'AS kd_ms' in item['sql'])
    assert '`env` IN {kd_vs:Array(String)}' in sql and request['query']['param_kd_vs'] == "['prod-a','prod-a2']"
    assert request['query']['param_kd_from'] == str(NOW_MS - 7200 * 1000)
    assert request['query']['param_kd_to'] == str(NOW_MS) and request['query']['param_kd_limit'] == '10'
    assert request['query']['readonly'] == '2' and request['query']['max_execution_time'] == '10'
    assert request['query']['max_rows_to_read'] == '1000000'
    assert request['query']['output_format_json_quote_64bit_integers'] == '0'
    assert request['headers']['X-ClickHouse-User'] == 'reader' and request['headers']['X-ClickHouse-Key'] == 'ch-pass'
    assert sql.endswith('FORMAT JSONCompact') and 'ORDER BY `start_time` DESC' in sql
    assert chosen['data']['rows'][0] == {'@time': '2026-11-23T16:00:00.000+08:00', 'path': '/order/0',
                                         'user_token': '***', 'trace_id': TRACE}
    assert 'ORDER BY `start_time` ASC' in ch.sqls('AS kd_ms')[1]
    assert bad[0] == 400 and bad[1]['code'] == 'field_not_found' and '相近的字段：status' in bad[1]['message']
    assert bad_order[0] == 400 and bad_limit[0] == 400 and '1~200' in bad_limit[1]['message']
    # 字段表缓存：两次查询只读一次 system.columns
    assert len([item for item in ch.requests if 'system.columns' in item['sql']]) == 1


@pytest.mark.asyncio
async def test_search_elastic(monkeypatch):
    es = es_fake()
    docs = [hit('d1', NOW_MS - 5, {'message': 'NullPointerException at Foo', 'level': 'ERROR', 'podname': 'p-1',
                                   'trace_id': TRACE, 'kubernetes': {'node': 'n1'}, 'password': 'x'},
                {'appname': ['order-api']}),
            hit('d2', NOW_MS - 10, {'message': 'ok', 'level': 'INFO', 'podname': 'p-2'})]
    es.on_search(lambda target, body: target == 'app-logs-*', hits(docs, total=10000, relation='gte'))
    async with serve(es) as es_url:
        async with TestClient(TestServer(make_app(monkeypatch, FakeStore(stored(es_url=es_url))))) as client:
            status, result = await call(client, 'search', {'source': 'app', 'level': 'error', 'keyword': 'NPE',
                                                           'k8s_namespace': 'shop'})
            _, everything = await call(client, 'search', {'source': 'app', 'fields': ['message', 'password',
                                                                                     'appname']})
    assert status == 200
    data = result['data']
    assert data['total'] == 10000 and data['total_relation'] == 'gte' and data['stats'] == {'elapsed_ms': 7}
    first = data['rows'][0]
    assert first['@time'] == common.iso_ms(NOW_MS - 5) and first['_id'] == 'd1' and first['trace_id'] == TRACE
    assert first['message'] == 'NullPointerException at Foo' and first['pod'] if 'pod' in first else True
    assert first['podname'] == 'p-1' and first['appname'] == 'order-api'  # appname 是运行时字段，从 fields 取
    body = es.bodies()[0]
    filters = body['query']['bool']['filter']
    assert filters[0] == {'range': {'@timestamp': {'gte': NOW_MS - 3600 * 1000, 'lt': NOW_MS,
                                                   'format': 'epoch_millis'}}}
    assert filters[1] == {'term': {'env': 'prod-a'}}  # 集群绑定条件
    assert {'terms': {'level': ['error', 'ERROR', 'Error']}} in filters and {'term': {'namespace': 'shop'}} in filters
    assert body['query']['bool']['must'] == [{'match_phrase': {'message': 'NPE'}}]
    assert body['timeout'] == '10s' and body['track_total_hits'] == 10000
    assert '/app-logs-*/_search' in [path for _, path, _, _ in es.requests]
    _, path, query, _ = next(item for item in es.requests if item[1].endswith('_search'))
    assert query == {'ignore_unavailable': 'true', 'allow_no_indices': 'true', 'expand_wildcards': 'open'}
    row = everything['data']['rows'][0]
    assert row['password'] == '***' and row['appname'] == 'order-api'


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_stats_clickhouse(monkeypatch):
    t0 = NOW_MS - 3600 * 1000

    def grouped(sql, query):
        if 'AS kd_t' in sql:
            return compact(['kd_t', 'kd_g0', 'kd_m0', 'kd_m1'],
                           [[t0, 500, 12, 340.5], [t0, 404, 3, 10.0], [t0 + 300000, 500, 1, 99.0],
                            [t0, 999, 1, 1.0]])  # 999 不在 Top 里：丢掉
        return compact(['kd_g0', 'kd_m0', 'kd_m1'], [['/a', 30, 1.25], ['/b', 20, None]])

    def handler(sql, query):
        if 'kd_total' in sql:
            return compact(['kd_total', 'kd_m0', 'kd_m1'], [[45, 45, 120.333]])
        if 'AS kd_c' in sql:  # 第一步：Top 的状态码
            return compact(['kd_g0', 'kd_c'], [[500, 13], [404, 3]])
        return grouped(sql, query)

    ch = ch_fake()
    ch.route('SELECT', handler)
    async with serve(ch, 'POST') as ch_url:
        async with TestClient(TestServer(make_app(monkeypatch, FakeStore(stored(ch_url))))) as client:
            status, timed = await call(client, 'stats', {'source': 'ingress', 'group_by': ['time', 'status'],
                                                         'metrics': [{'op': 'count'}, {'op': 'p95', 'field': 'duration'}]})
            _, by_path = await call(client, 'stats', {'source': 'ingress', 'group_by': ['path'], 'top': 1,
                                                      'metrics': [{'op': 'count'}, {'op': 'avg', 'field': 'duration'}]})
            _, plain = await call(client, 'stats', {'source': 'ingress'})
            errors = [await call(client, 'stats', {'source': 'ingress', **body}) for body in (
                {'group_by': ['time'], 'interval': '1m', 'lookback': '2d'},
                {'group_by': ['time', 'time']}, {'group_by': ['x']}, {'metrics': [{'op': 'median', 'field': 'a'}]},
                {'metrics': [{'op': 'count', 'field': 'a'}]}, {'metrics': [{'op': 'avg'}]},
                {'metrics': [{'op': 'count'}, {'op': 'count'}]}, {'group_by': ['time'], 'interval': '2m'},
                {'metrics': [{'op': 'sum', 'field': 'start_time'}]})]
    assert status == 200
    data = timed['data']
    assert data['columns'] == ['time', 'status', 'count', 'p95_duration'] and data['interval'] == '1m'
    assert data['duration_unit'] == 'ms' and data['totals'] == {'count': 45, 'p95_duration': 120.333}
    assert len(data['rows']) == 60 * 2  # 每个桶 × Top 的两个状态码，空桶补 0
    assert data['rows'][0] == [common.iso_ms(t0), 500, 12, 340.5] and data['rows'][1] == [common.iso_ms(t0), 404, 3, 10]
    assert data['rows'][2] == [common.iso_ms(t0 + 60000), 500, 0, None]
    assert [row for row in data['rows'] if row[0] == common.iso_ms(t0 + 300000) and row[1] == 500][0][2] == 1
    assert '按时间和字段分组时只统计字段取值最多的 10 个' in data['notes'][0]
    top_sql, time_sql = ch.sqls('AS kd_c')[0], ch.sqls('AS kd_t')[0]
    assert 'LIMIT {kd_k:UInt32}' in top_sql and '`env` IN {kd_vs:Array(String)}' in time_sql
    assert 'toString(`status`) IN {kd_top_values:Array(String)}' in time_sql
    assert 'intDiv(toUnixTimestamp64Milli(toDateTime64(`start_time`, 3)) + 28800000, {kd_iv:Int64})' in time_sql
    request = next(item for item in ch.requests if 'AS kd_t' in item['sql'])
    assert request['query']['param_kd_top_values'] == "['404','500']" and request['query']['param_kd_iv'] == '60000'
    paths = by_path['data']
    assert paths['columns'] == ['path', 'count', 'avg_duration'] and paths['rows'] == [['/a', 30, 1.25]]
    assert 'duration_unit' in paths and 'interval' not in paths
    assert plain['data']['columns'] == ['count'] and plain['data']['rows'] == [[45]]
    assert [status for status, _ in errors] == [400] * 9
    assert '时间桶太多' in errors[0][1]['message'] and '重复' in errors[1][1]['message']
    assert errors[2][1]['code'] == 'field_not_found' and 'count 不带 field' in errors[4][1]['message']
    assert 'avg 要带 field' in errors[5][1]['message'] and 'interval 只能是' in errors[7][1]['message']
    assert '不能算 sum' in errors[8][1]['message']


@pytest.mark.asyncio
async def test_stats_elastic(monkeypatch):
    t0 = (NOW_MS - 3600 * 1000 + 28800000) // 60000 * 60000 - 28800000
    aggs = {'g0': {'buckets': [
        {'key': 'order-api', 'doc_count': 5, 'kd_t': {'buckets': [
            {'key': t0, 'doc_count': 5, 'm1': {'values': {'99.0': 12.5}}},
            {'key': t0 + 60000, 'doc_count': 0, 'm1': {'values': {'99.0': None}}}]}}]},
        'total_m1': {'values': {'99.0': 30.0}}}
    es = es_fake()
    es.on_search(lambda target, body: 'aggs' in body and 'g0' in body['aggs'],
                 {'hits': {'total': {'value': 77, 'relation': 'eq'}, 'hits': []}, 'aggregations': aggs})
    es.on_search(lambda target, body: 'aggs' in body and 'g0' not in body['aggs'],
                 {'hits': {'total': {'value': 9, 'relation': 'eq'}, 'hits': []},
                  'aggregations': {'g0': {'buckets': []},
                                   'total_m1': {'value': 3}}})
    async with serve(es) as es_url:
        async with TestClient(TestServer(make_app(monkeypatch, FakeStore(stored(es_url=es_url))))) as client:
            status, result = await call(client, 'stats', {'source': 'app', 'group_by': ['appname', 'time'],
                                                          'metrics': [{'op': 'count'}, {'op': 'p99', 'field': 'latency'}]})
            text_group = await call(client, 'stats', {'source': 'app', 'group_by': ['path']})
    assert status == 200
    data = result['data']
    assert data['columns'] == ['appname', 'time', 'count', 'p99_latency'] and data['totals'] == {'count': 77,
                                                                                               'p99_latency': 30}
    assert data['rows'] == [['order-api', common.iso_ms(t0), 5, 12.5], ['order-api', common.iso_ms(t0 + 60000), 0, None]]
    body = es.bodies()[0]
    terms = body['aggs']['g0']
    assert terms['terms'] == {'field': 'appname', 'size': 10}
    histogram = terms['aggs']['kd_t']['date_histogram']
    assert histogram == {'field': '@timestamp', 'fixed_interval': '1m', 'time_zone': '+08:00', 'min_doc_count': 0,
                         'extended_bounds': {'min': t0, 'max': NOW_MS - 1}}
    assert terms['aggs']['kd_t']['aggs'] == {'m1': {'percentiles': {'field': 'latency', 'percents': [99.0]}}}
    assert body['size'] == 0 and body['track_total_hits'] is True and body['query']['bool']['filter'][1] == \
        {'term': {'env': 'prod-a'}}
    assert text_group[0] == 400 and '不能分组' in text_group[1]['message']


# ---------------------------------------------------------------------------
# patterns
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_patterns_clickhouse_and_elastic(monkeypatch):
    rows = [[NOW_MS - index, f'Timeout calling order {index} password=p{index}', 'ERROR'] for index in range(30)]
    rows += [[NOW_MS - 100 - index, 'Started ok', 'INFO'] for index in range(10)]
    ch = ch_fake(kd_count=compact(['kd_count'], [[4000]]))
    ch.route('AS kd_msg', compact(['kd_ms', 'kd_msg', 'kd_lv'], rows))
    es = es_fake()
    es.on_search(lambda target, body: True, hits([hit(f'd{i}', NOW_MS - i, {'message': f'GC pause {i}ms',
                                                                          'level': 'WARN'}) for i in range(5)],
                                                 total=50))
    async with serve(ch, 'POST') as ch_url, serve(es) as es_url:
        async with TestClient(TestServer(make_app(monkeypatch, FakeStore(stored(ch_url, es_url))))) as client:
            status, result = await call(client, 'patterns', {'source': 'ingress', 'exclude': ['health check'],
                                                             'sample': 100, 'top': 5})
            _, es_result = await call(client, 'patterns', {'source': 'app'})
            bad = await call(client, 'patterns', {'source': 'ingress', 'sample': 50})
    assert status == 200
    data = result['data']
    assert data['sample'] == {'size': 40, 'total': 4000, 'method': 'latest'} and data['excluded'] == ['health check']
    first = data['patterns'][0]
    assert first['pattern'] == 'Timeout calling order <NUM> password=***' and first['count'] == 30
    assert first['estimated'] == 3000 and first['levels'] == {'ERROR': 30} and 'p1' not in first['example']
    assert data['conditions'][-1] == '正文不包含「health check」'
    sql = ch.sqls('AS kd_msg')[0]
    assert 'ifNull(positionCaseInsensitive(toString(`message`), {kd_ex:String}), 0) = 0' in sql
    assert 'substringUTF8(toString(`message`), 1, 1000) AS kd_msg, toString(`level`) AS kd_lv' in sql
    assert '`env` IN' in ch.sqls('kd_count')[0]
    assert es_result['data']['patterns'][0]['pattern'] == 'GC pause <NUM>ms'
    assert es_result['data']['sample'] == {'size': 5, 'total': 50, 'method': 'latest'}
    body = es.bodies()[0]
    assert body['track_total_hits'] is True and body['size'] == 2000 and body['_source'] == ['@timestamp', 'message', 'level']
    assert body['fields'] == [{'field': '@timestamp', 'format': 'epoch_millis'}]  # 正文不在 fields 里重复取
    assert bad[0] == 400 and '100~5000' in bad[1]['message']


@pytest.mark.asyncio
async def test_patterns_shrink_sample_when_response_too_large(monkeypatch):
    monkeypatch.setattr(elastic, 'MAX_RESPONSE', 20000)

    def answer(target, body):
        docs = [hit(f'd{i}', NOW_MS - i, {'message': f'slow query {i}', 'level': 'WARN'}) for i in range(body['size'])]
        return hits(docs, total=900)

    es = es_fake()
    es.on_search(lambda target, body: True, answer)
    async with serve(es) as es_url:
        async with TestClient(TestServer(make_app(monkeypatch, FakeStore(stored(es_url=es_url))))) as client:
            status, result = await call(client, 'patterns', {'source': 'app', 'sample': 500})
            monkeypatch.setattr(elastic, 'MAX_RESPONSE', 3000)  # 100 条也放不下：不再重试
            failed = await call(client, 'patterns', {'source': 'app', 'sample': 100, 'lookback': '2h'})
    assert status == 200 and [body['size'] for body in es.bodies()][:2] == [500, 100]
    assert result['data']['sample']['size'] == 100 and '样本从 500 条减到 100 条' in result['data']['notes'][0]
    assert failed[0] == 400 and failed[1]['code'] == 'too_expensive' and '16MB' in failed[1]['message']


# ---------------------------------------------------------------------------
# context
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_context_clickhouse(monkeypatch):
    anchor = NOW_MS - 60000

    def handler(sql, query):
        names = select_columns(sql)
        if 'DESC' in sql:
            data = [{'kd_ms': anchor, 'message': 'boom', 'pod': 'p'}, {'kd_ms': anchor - 5, 'message': 'before'}]
        else:
            data = [{'kd_ms': anchor + 5, 'message': 'after'}]
        return compact(names, [[row.get(name) for name in names] for row in data])

    ch = ch_fake()
    ch.route('AS kd_ms', handler)
    async with serve(ch, 'POST') as ch_url:
        async with TestClient(TestServer(make_app(monkeypatch, FakeStore(stored(ch_url))))) as client:
            status, result = await call(client, 'context', {'source': 'ingress', 'time': common.iso_ms(anchor),
                                                            'k8s_pod': 'p', 'before': 1, 'after': 1})
            no_pod = await call(client, 'context', {'source': 'ingress', 'time': common.iso_ms(anchor)})
            no_time = await call(client, 'context', {'source': 'ingress', 'k8s_pod': 'p'})
            bad = await call(client, 'context', {'source': 'ingress', 'time': 'x', 'k8s_pod': 'p', 'keyword': 'a'})
    assert status == 200
    data = result['data']
    assert [row['message'] for row in data['rows']] == ['before', 'boom', 'after']
    assert data['rows'][1]['anchor'] is True and 'anchor' not in data['rows'][0]
    assert data['anchor'] == {'time': common.iso_ms(anchor), 'pod': 'p'}
    first, second = [item for item in ch.requests if 'AS kd_ms' in item['sql']]
    assert first['query']['param_kd_from'] == str(anchor - 3600000) and first['query']['param_kd_to'] == str(anchor + 1)
    assert first['query']['param_kd_limit'] == '2' and first['query']['param_kd_v'] == 'p'
    assert second['query']['param_kd_from'] == str(anchor + 1) and 'ASC' in second['sql']
    assert '`env` IN' in first['sql'] and '`env` IN' in second['sql']
    assert no_pod[0] == 400 and '要带 k8s_pod' in no_pod[1]['message']
    assert no_time[0] == 400 and 'time 必填' in no_time[1]['message']
    assert bad[0] == 400 and '不支持的参数：keyword' in bad[1]['message']


@pytest.mark.asyncio
async def test_context_elastic_anchor_id(monkeypatch):
    es = es_fake()
    es.on_search(lambda target, body: body['sort'][0]['@timestamp']['order'] == 'desc',
                 hits([hit('a', NOW_MS - 10, {'message': 'x'}), hit('b', NOW_MS - 10, {'message': 'y'})]))
    async with serve(es) as es_url:
        async with TestClient(TestServer(make_app(monkeypatch, FakeStore(stored(es_url=es_url))))) as client:
            status, result = await call(client, 'context', {'source': 'app', 'time': common.iso_ms(NOW_MS - 10),
                                                            'k8s_pod': 'p-1', 'anchor_id': 'b', 'after': 0})
    assert status == 200
    rows = result['data']['rows']
    assert [row['_id'] for row in rows] == ['b', 'a'] and rows[0]['anchor'] is True and 'anchor' not in rows[1]
    assert len(es.bodies()) == 1  # after=0：只查前面


# ---------------------------------------------------------------------------
# correlate
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_correlate_across_sources(monkeypatch):
    async def tracing_cfg():
        return {'enabled': True, 'url': 'http://jaeger:16686', 'ui_url': 'https://jaeger.example.com'}

    monkeypatch.setattr(ops.tracing_config, 'load_config', tracing_cfg)
    ch = ch_fake([{'kd_ms': NOW_MS - 3000, 'method': 'GET', 'host': 'api.example.com', 'path': '/order/1',
                   'status': 504, 'duration': 3012, 'message': f'TID:{TRACE}'}])
    es = es_fake()
    es.on_search(lambda target, body: True, hits([hit('d1', NOW_MS - 2000, {'message': 'NPE\n at x', 'level': 'ERROR',
                                                                         'podname': 'order-1', 'trace_id': TRACE})]))
    async with serve(ch, 'POST') as ch_url, serve(es) as es_url:
        async with TestClient(TestServer(make_app(monkeypatch, FakeStore(stored(ch_url, es_url))))) as client:
            status, result = await call(client, 'correlate', {'key': 'trace_id', 'value': TRACE,
                                                              'around': '2026-11-23 15:59'})
            errors = [await call(client, 'correlate', body) for body in (
                {'key': 'trace_id', 'value': 'not-hex'}, {'key': 'nope', 'value': 'x'},
                {'key': 'trace_id', 'value': TRACE, 'around': '2026-11-23 15:00', 'lookback': '1h'},
                {'key': 'trace_id', 'value': TRACE, 'sources': ['other']},
                {'key': 'request_id', 'value': 'a\nb'})]
            _, only = await call(client, 'correlate', {'key': 'request_id', 'value': 'req-1', 'sources': ['ingress']})
    assert status == 200
    data = result['data']
    assert data['window'] == {'from': '2026-11-23T15:29:00.000+08:00', 'to': '2026-11-23T16:29:00.000+08:00'}
    ingress, app = data['sources']
    assert ingress['count'] == 1 and ingress['rows'][0]['trace_id'] == TRACE and ingress['error'] is None
    assert app['count'] == 1 and app['rows'][0]['_id'] == 'd1'
    assert data['timeline'] == [
        {'@time': common.iso_ms(NOW_MS - 3000), 'source': 'ingress', 'summary': 'GET ***/order/1 504 3012ms'},
        {'@time': common.iso_ms(NOW_MS - 2000), 'source': 'app', 'summary': 'ERROR order-1 NPE'}]
    assert data['missing'] == [] and data['trace'] == {'trace_id': TRACE,
                                                       'url': f'https://jaeger.example.com/trace/{TRACE}'}
    sql = ch.sqls('AS kd_ms')[0]
    request = next(item for item in ch.requests if 'AS kd_ms' in item['sql'])
    assert 'positionCaseInsensitive(toString(`message`), {kd_kw:String}) > 0' in sql and '`env` IN' in sql
    assert request['query']['param_kd_kw'] == f'TID:{TRACE}' and 'ORDER BY `start_time` ASC' in sql
    assert {'term': {'trace_id': TRACE}} in es.bodies()[0]['query']['bool']['filter']
    assert [status for status, _ in errors] == [400] * 5
    assert '不符合关联字段 trace_id 的取值格式' in errors[0][1]['message'] and '可选：trace_id、request_id' in errors[1][1]['message']
    assert '不能同时给' in errors[2][1]['message'] and '没有登记关联字段 trace_id：other' in errors[3][1]['message']
    assert '不能换行' in errors[4][1]['message']
    assert [item['source'] for item in only['data']['sources']] == ['ingress'] and 'trace' not in only['data']
    assert only['data']['window']['from'] == '2026-11-22T16:00:00.000+08:00'  # 默认 24 小时


@pytest.mark.asyncio
async def test_correlate_one_source_failing(monkeypatch):
    ch = ch_fake()
    es = es_fake()
    es.fail['/app-logs-*/_search'] = (500, {'error': {'root_cause': [{'type': 'x', 'reason': 'shard failure'}]}})
    async with serve(ch, 'POST') as ch_url, serve(es) as es_url:
        async with TestClient(TestServer(make_app(monkeypatch, FakeStore(stored(ch_url, es_url))))) as client:
            status, result = await call(client, 'correlate', {'key': 'trace_id', 'value': TRACE})
    assert status == 200
    ingress, app = result['data']['sources']
    assert ingress['error'] is None and ingress['rows'] == []
    assert app['error'] == {'code': 'datasource_unavailable', 'message': 'Elasticsearch 出错（HTTP 500）：shard failure'}
    assert result['data']['missing'] == ['ingress'] and result['data']['trace'] == {'trace_id': TRACE}


# ---------------------------------------------------------------------------
# query（原生查询）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_query_clickhouse_sql(monkeypatch):
    _, simple = ast_fixture('ph_simple')
    _, direct = ast_fixture('ph_direct_table')

    def explain(sql, query):
        if 'logs.access' in sql:
            return direct
        return simple

    ch = ch_fake()
    ch.route('GROUP BY path', compact(['path', 'c', 'password'], [['/a?token=1', 5, 'x'], ['/b', 3, 'y']]))
    ch.route('EXPLAIN AST', explain)  # 后加的先匹配
    async with serve(ch, 'POST') as ch_url:
        async with TestClient(TestServer(make_app(monkeypatch, FakeStore(stored(ch_url))))) as client:
            sql = ('SELECT path, count() AS c, any(user_token) AS password FROM $src WHERE status >= 500 '
                   'GROUP BY path ORDER BY c DESC LIMIT 20;')
            status, result = await call(client, 'query', {'source': 'ingress', 'sql': sql, 'limit': 1})
            rejected = await call(client, 'query', {'source': 'ingress',
                                                    'sql': 'SELECT * FROM $src AS a JOIN logs.access AS b USING id'})
            ch.route('EXPLAIN AST', ch_error(62, 'Syntax error: failed at position 15 (__kd_src__)', 400,
                                             'SYNTAX_ERROR'))
            syntax = await call(client, 'query', {'source': 'ingress', 'sql': 'SELECT FROM FROM $src'})
            wrong = await call(client, 'query', {'source': 'ingress', 'dsl': {'query': {}}})
            both = await call(client, 'query', {'source': 'ingress', 'sql': 'x', 'esql': 'y'})
            hint = await call(client, 'query', {'source': 'ingress', 'query': 'x'})
    assert status == 200, result
    data = result['data']
    assert data['language'] == 'sql' and data['columns'] == ['path', 'c', 'password']
    assert data['rows'] == [['/a?token=***', 5, '***']] and data['truncated'] is True and data['returned'] == 1
    explained = next(item for item in ch.requests if item['sql'].startswith('EXPLAIN AST'))
    assert explained['sql'] == ('EXPLAIN AST SELECT path, count() AS c, any(user_token) AS password FROM __kd_src__ '
                                'WHERE status >= 500 GROUP BY path ORDER BY c DESC LIMIT 20')
    final = next(item for item in ch.requests if 'GROUP BY path' in item['sql'] and not
                 item['sql'].startswith('EXPLAIN'))
    assert '__kd_src__' not in final['sql'] and '$src' not in final['sql']
    assert ('FROM (SELECT * FROM `logs`.`access` WHERE `start_time` >= fromUnixTimestamp64Milli({kd_from:Int64}) AND '
            '`start_time` < fromUnixTimestamp64Milli({kd_to:Int64}) AND `env` IN {kd_vs:Array(String)}) WHERE') in \
        final['sql']
    assert final['query']['max_result_rows'] == '1' and final['query']['result_overflow_mode'] == 'break'
    assert final['query']['max_result_bytes'] == str(16 * 1024 * 1024) and final['query']['readonly'] == '2'
    assert final['query']['param_kd_vs'] == "['prod-a','prod-a2']"
    assert "`env` IN ['prod-a', 'prod-a2']" in data['query'] and f'fromUnixTimestamp64Milli({NOW_MS})' in data['query']
    assert rejected[0] == 400 and rejected[1]['code'] == 'raw_invalid' and '不能直接查表 logs.access' in \
        rejected[1]['message']
    assert syntax[0] == 400 and syntax[1]['code'] == 'query_rejected' and '($src)' in syntax[1]['message']
    assert wrong[0] == 400 and '原生查询用 sql' in wrong[1]['message']
    assert both[0] == 400 and '三选一' in both[1]['message']
    assert hint[0] == 400 and 'query 请用 sql / dsl / esql' in hint[1]['message']


@pytest.mark.asyncio
async def test_query_raw_not_allowed(monkeypatch):
    cfg = sample_config()
    cfg['connections'][0]['allow_raw'] = False
    async with TestClient(TestServer(make_app(monkeypatch, FakeStore(log_config.normalize_config(cfg))))) as client:
        status, data = await call(client, 'query', {'source': 'ingress', 'sql': 'SELECT 1 FROM $src'})
        _, listing = await call(client, 'sources')
    assert status == 403 and data['code'] == 'raw_not_allowed'
    assert listing['data']['sources'][0]['raw_query'] == []


@pytest.mark.asyncio
async def test_query_elastic_dsl_and_esql(monkeypatch):
    es = es_fake()
    es.on_search(lambda target, body: True,
                 {**hits([hit('d1', NOW_MS - 1, {'message': 'x', 'nested': {'token': 't', 'k': 'v'}})], total=3),
                  'aggregations': {'by': {'buckets': [{'key': 'ERROR', 'doc_count': 3}]}}})
    es.esql = lambda body: {'columns': [{'name': 'level', 'type': 'keyword'}, {'name': 'c', 'type': 'long'},
                                        {'name': 'secret', 'type': 'keyword'}],
                            'values': [['ERROR', 3, 's'], ['INFO', 1, 's']]}
    async with serve(es) as es_url:
        async with TestClient(TestServer(make_app(monkeypatch, FakeStore(stored(es_url=es_url))))) as client:
            status, dsl = await call(client, 'query', {'source': 'app', 'dsl': {
                'query': {'match': {'message': 'x'}}, 'aggs': {'by': {'terms': {'field': 'level'}}}, 'size': 5}})
            _, esql = await call(client, 'query', {'source': 'app', 'esql': '| STATS c = COUNT(*) BY level',
                                                   'limit': 1})
            bad_dsl = await call(client, 'query', {'source': 'app', 'dsl': {'aggs': {'g': {'global': {}}}}})
            bad_esql = await call(client, 'query', {'source': 'app', 'esql': '| LOOKUP JOIN other ON x'})
            sql_on_es = await call(client, 'query', {'source': 'app', 'sql': 'SELECT 1 FROM $src'})
            es.version = '8.13.0'
            elastic.reset_caches()
            old = await call(client, 'query', {'source': 'app', 'esql': '| LIMIT 1'})
            es.version, es.distribution = '2.17.0', 'opensearch'
            elastic.reset_caches()
            opensearch = await call(client, 'query', {'source': 'app', 'esql': '| LIMIT 1'})
    assert status == 200, dsl
    data = dsl['data']
    assert data['language'] == 'dsl' and data['total'] == 3 and data['rows'][0]['nested.token'] == '***'
    assert data['rows'][0]['nested.k'] == 'v' and data['rows'][0]['_id'] == 'd1'
    assert data['aggregations'] == {'by': {'buckets': [{'key': 'ERROR', 'doc_count': 3}]}}
    body = es.bodies()[0]
    assert body['query']['bool']['filter'][1] == {'term': {'env': 'prod-a'}}
    assert body['query']['bool']['must'] == [{'match': {'message': 'x'}}] and body['size'] == 5
    assert body['track_total_hits'] == 10000 and body['fields'] == [{'field': '@timestamp', 'format': 'epoch_millis'}]
    result = esql['data']
    assert result['columns'] == ['level', 'c', 'secret'] and result['rows'] == [['ERROR', 3, '***']]
    assert result['truncated'] is True and result['query'] == 'FROM app-logs-* | STATS c = COUNT(*) BY level | LIMIT 1'
    query_body = next(body for _, path, _, body in es.requests if path == '/_query')
    assert query_body['filter']['bool']['filter'][1] == {'term': {'env': 'prod-a'}}  # ES|QL 也带集群过滤
    assert query_body['filter']['bool']['filter'][0]['range']['@timestamp']['lt'] == NOW_MS
    assert bad_dsl[0] == 400 and '不能用 global' in bad_dsl[1]['message']
    assert bad_esql[0] == 400 and bad_esql[1]['code'] == 'raw_invalid'
    assert sql_on_es[0] == 400 and '原生查询用 dsl 或 esql' in sql_on_es[1]['message']
    assert old[0] == 409 and old[1]['code'] == 'version_unsupported'
    assert opensearch[0] == 409 and 'OpenSearch 不支持' in opensearch[1]['message']


# ---------------------------------------------------------------------------
# 数据源的错误
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_clickhouse_error_codes_and_readonly_retry(monkeypatch):
    ch = ch_fake()
    async with serve(ch, 'POST') as ch_url:
        async with TestClient(TestServer(make_app(monkeypatch, FakeStore(stored(ch_url))))) as client:
            results = {}
            for name, response in (('rows', ch_error(158, 'Limit for rows (controlled by max_rows_to_read)')),
                                   ('timeout', ch_error(159, 'Timeout exceeded: elapsed 10 seconds')),
                                   ('rejected', ch_error(47, 'Unknown identifier', 404)),
                                   ('down', web.Response(status=503, text='<html>busy</html>'))):
                ch.route('AS kd_ms', response)
                results[name] = await call(client, 'search', {'source': 'ingress'})
            calls = []

            def readonly(sql, query):
                calls.append(dict(query))
                if 'readonly' in query:
                    return ch_error(164, "Cannot modify 'readonly' setting in readonly mode", 500, 'READONLY')
                return compact(['kd_ms'], [])

            ch.route('AS kd_ms', readonly)
            ok = await call(client, 'search', {'source': 'ingress'})
            again = await call(client, 'search', {'source': 'ingress'})
    assert (results['rows'][0], results['rows'][1]['code']) == (400, 'too_expensive')
    assert '加条件' in results['rows'][1]['message'] and 'max_rows_to_read' in results['rows'][1]['message']
    assert (results['timeout'][0], results['timeout'][1]['code']) == (504, 'timeout')
    assert (results['rejected'][0], results['rejected'][1]['code']) == (400, 'query_rejected')
    assert 'Unknown identifier' in results['rejected'][1]['message']
    assert (results['down'][0], results['down'][1]['code']) == (502, 'datasource_unavailable')
    assert ok[0] == 200 and again[0] == 200
    assert 'readonly' in calls[0] and 'readonly' not in calls[1] and 'readonly' not in calls[2]  # 记住了
    assert calls[1]['param_kd_limit'] == '50'


@pytest.mark.asyncio
async def test_elastic_runtime_fields_are_marked_and_hinted(monkeypatch):
    """dynamic: runtime 的索引里没登记的字段是运行时字段：字段表标出来；用它过滤超时时报错里点出来"""
    log_api.reset_caches()
    es = es_fake()
    es.runtime['app-logs-*'] = ['appname']
    es.on_search(lambda target, body: True, {'timed_out': True, 'hits': {'hits': []}})
    async with serve(es) as es_url:
        async with TestClient(TestServer(make_app(monkeypatch, FakeStore(stored(es_url=es_url))))) as client:
            _, detail = await call(client, 'sources', {'source': 'app'})
            status, result = await call(client, 'search', {
                'source': 'app', 'filters': [{'field': 'appname', 'op': 'eq', 'value': 'order-api'}]})
            plain = await call(client, 'search', {'source': 'app', 'keyword': 'boom'})
    schema = {item['name']: item for item in detail['data']['schema']}
    assert schema['appname']['runtime'] is True and 'runtime' not in schema['message']
    assert (status, result['code']) == (504, 'timeout')
    assert 'appname 是运行时字段' in result['message'] and '正文关键字' in result['message']
    assert plain[0] == 504 and '运行时字段' not in plain[1]['message']  # 没用到运行时字段：原来的提示
    # 没有权限读映射（403）：不标运行时字段，查询照常
    log_api.reset_caches()
    es.fail['/app-logs-*/_mapping'] = (403, {'error': {'type': 'security_exception', 'reason': 'no privilege'}})
    async with serve(es) as es_url:
        async with TestClient(TestServer(make_app(monkeypatch, FakeStore(stored(es_url=es_url))))) as client:
            _, detail = await call(client, 'sources', {'source': 'app'})
    assert all('runtime' not in item for item in detail['data']['schema'])


@pytest.mark.asyncio
async def test_elastic_errors_and_unreachable(monkeypatch):
    es = es_fake()
    async with serve(es) as es_url:
        async with TestClient(TestServer(make_app(monkeypatch, FakeStore(stored(es_url=es_url))))) as client:
            es.on_search(lambda target, body: True, {'timed_out': True, 'hits': {'hits': []}})
            timed_out = await call(client, 'search', {'source': 'app'})
            es.fail['/app-logs-*/_search'] = (400, {'error': {'root_cause': [
                {'type': 'parsing_exception', 'reason': 'unknown query [x]'}], 'type': 'search_phase_execution_exception'}})
            rejected = await call(client, 'search', {'source': 'app'})
            es.fail['/app-logs-*/_search'] = (401, {'error': 'unauthorized'})
            unauthorized = await call(client, 'search', {'source': 'app'})
            es.fail['/app-logs-*/_search'] = (400, {'error': {'type': 'too_many_buckets_exception', 'reason': 'x'}})
            buckets = await call(client, 'search', {'source': 'app'})
    assert (timed_out[0], timed_out[1]['code']) == (504, 'timeout')
    assert (rejected[0], rejected[1]['code']) == (400, 'query_rejected') and 'unknown query [x]' in rejected[1]['message']
    assert (unauthorized[0], unauthorized[1]['code']) == (502, 'datasource_unavailable')
    assert (buckets[0], buckets[1]['code']) == (400, 'too_expensive')
    async with TestClient(TestServer(make_app(monkeypatch, FakeStore(stored())))) as client:
        down = await call(client, 'search', {'source': 'app'})
    assert (down[0], down[1]['code']) == (502, 'datasource_unavailable') and '无法访问 Elasticsearch' in down[1]['message']


@pytest.mark.asyncio
async def test_decrypt_failed(monkeypatch):
    cfg = stored()
    monkeypatch.setenv('AI_ENCRYPTION_KEY', '55' * 32)  # 密钥换过
    async with TestClient(TestServer(make_app(monkeypatch, FakeStore(cfg)))) as client:
        status, data = await call(client, 'search', {'source': 'ingress'})
    assert status == 500 and data['code'] == 'decrypt_failed' and '连接 ck 的密码无法解密' in data['message']


@pytest.mark.asyncio
async def test_large_results_are_truncated(monkeypatch):
    rows = [{'kd_ms': NOW_MS - index, 'message': 'x' * 1990 + str(index), 'path': '/p'} for index in range(200)]
    ch = ch_fake(rows)
    async with serve(ch, 'POST') as ch_url:
        async with TestClient(TestServer(make_app(monkeypatch, FakeStore(stored(ch_url))))) as client:
            status, result = await call(client, 'search', {'source': 'ingress', 'fields': ['message', 'path'],
                                                           'limit': 200})
    data = result['data']
    assert status == 200 and data['truncated'] is True and 20 <= data['returned'] < 40
    assert '减少 fields' in data['hint'] and len(json.dumps(data, ensure_ascii=False)) <= 60000
