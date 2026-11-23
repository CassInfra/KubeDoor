"""历史日志管理接口（logsearch/api.py、admin.py、scan.py）：配置读写和审计、测试连接、预览、验证关联字段、扫描与建议、
经过真实鉴权中间件的路由。全部用本地假服务。"""
import json
from datetime import datetime

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from auth.crypto import decrypt_secret
from auth.principal import Principal
from logsearch import api as log_api
from logsearch import clickhouse, common, elastic, ops, scan, schema
from logsearch import config as log_config
from logsearch_fakes import (CH_COLUMNS, ES_FIELDS, NOW, NOW_MS, FakeClickHouse, FakeElastic, caps, ch_error, compact,
                             hit, hits, sample_config, serve)
from tracing import fields as trace_fields

TRACE = '0af7651916cd43dd8448eb211c80319c'


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    monkeypatch.setattr(common, '_clock', lambda: NOW)
    monkeypatch.setenv('AI_ENCRYPTION_KEY', '66' * 32)

    async def aliases():
        return {'prod-a': ['生产A'], 'shop-east': []}

    async def tracing_cfg():
        return {'enabled': False, 'url': ''}

    monkeypatch.setattr(trace_fields, 'load_aliases', aliases)
    monkeypatch.setattr(ops.tracing_config, 'load_config', tracing_cfg)
    schema.reset()
    elastic.reset_caches()
    clickhouse.reset_state()
    common.reset_limiters()


@pytest.fixture
def audits(monkeypatch):
    records = []
    monkeypatch.setattr(log_api.audit, 'record_bg',
                        lambda username, action, **kwargs: records.append({'username': username, 'action': action,
                                                                           **kwargs}))
    return records


class FakeStore:
    def __init__(self, value=None):
        self.row = None if value is None else {'value': value, 'updated_by': 'admin',
                                               'updated_at': datetime(2026, 11, 23, 10, 0)}

    async def get_setting(self, key, default=None):
        assert key == log_config.SETTING_KEY
        return self.row['value'] if self.row else default

    async def get_setting_row(self, key):
        return self.row

    async def put_setting_row(self, key, value, updated_by):
        self.row = {'value': json.loads(json.dumps(value)), 'updated_by': updated_by,
                    'updated_at': datetime(2026, 11, 23, 11, 0)}
        return self.row


def admin():
    return Principal('root', 'admin', user_id=1)


def make_app(monkeypatch, store, principal=None, clients=None):
    monkeypatch.setattr(log_api, 'store', store)
    monkeypatch.setattr(log_config, 'store', store)
    principal = principal or admin()

    @web.middleware
    async def inject(request, handler):
        request['principal'] = principal
        return await handler(request)

    app = web.Application(middlewares=[inject])
    log_api.register_routes(app, clients)
    return app


async def post(client, path, body):
    resp = await client.post(f'/api/admin/log-sources/{path}', json=body)
    return resp.status, await resp.json()


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_admin_config_round_trip(monkeypatch, audits):
    store = FakeStore()
    clients = {'prod-a': {'online': True}, 'dev': {'online': False}}
    async with TestClient(TestServer(make_app(monkeypatch, store, clients=clients))) as client:
        empty = await (await client.get('/api/admin/log-sources')).json()
        schema._CACHE.put(('x', 'y'), {'a': 1})
        saved_resp = await client.put('/api/admin/log-sources', json=sample_config())
        saved = await saved_resp.json()
        toggled = await (await client.put('/api/admin/log-sources', json={'enabled': False})).json()
        bad = await client.put('/api/admin/log-sources', json={'connections': [{'id': 'BAD'}]})
        bad_data = await bad.json()
        not_json = await client.put('/api/admin/log-sources', data='x', headers={'Content-Type': 'application/json'})
    data = empty['data']
    assert data['enabled'] is False and data['connections'] == [] and data['updated_by'] is None
    assert data['semantic_fields'][0] == {'key': 'message', 'label': '正文'} and len(data['semantic_fields']) == 11
    assert 'passphrase' in data['mask_builtin'] and 'authorization' in data['mask_builtin']
    assert data['clusters_known'] == [{'env': 'dev', 'aliases': [], 'online': False},
                                      {'env': 'prod-a', 'aliases': ['生产A'], 'online': True},
                                      {'env': 'shop-east', 'aliases': [], 'online': False}]
    assert saved_resp.status == 200 and saved['data']['updated_by'] == 'root'
    assert saved['data']['updated_at'] == '2026-11-23T11:00:00'
    conn = saved['data']['connections'][0]
    assert conn['has_password'] is True and 'password' not in conn and conn['has_api_key'] is False
    assert 'ch-pass' not in json.dumps(saved, ensure_ascii=False) + json.dumps(audits, ensure_ascii=False)
    assert decrypt_secret(store.row['value']['connections'][0]['password']) == 'ch-pass'
    assert len(schema._CACHE) == 0  # 保存后字段表缓存清空
    assert toggled['data']['enabled'] is False and toggled['data']['sources'][0]['id'] == 'ingress'
    assert bad.status == 400 and bad_data['code'] == 'invalid_request' and '第 1 个连接的 ID' in bad_data['message']
    assert not_json.status == 400
    first, second = audits
    assert first['action'] == 'log_sources.update' and first['username'] == 'root'
    assert first['detail']['changed'][:4] == ['enabled', 'architecture', 'connections.ck', 'connections.es']
    assert second['detail'] == {'changed': ['enabled']}


# ---------------------------------------------------------------------------
# 测试连接
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_admin_test_connection(monkeypatch):
    ch = FakeClickHouse(routes=[
        ('version()', compact(['version', 'timezone', 'user', 'readonly'], [['26.7.3.19', 'Asia/Shanghai', 'reader', '1']])),
        ('system.clusters', compact(['cluster'], [['ck1'], ['default']]))])
    es = FakeElastic()
    async with serve(ch, 'POST') as ch_url, serve(es) as es_url:
        saved = log_config.normalize_config(sample_config(ch_url, es_url))
        async with TestClient(TestServer(make_app(monkeypatch, FakeStore(saved)))) as client:
            form = sample_config(ch_url, es_url)
            form['connections'][0]['password'] = ''  # 留空：用已保存的
            ok_ch = await post(client, 'test', {'config': form, 'connection': 'ck'})
            ok_es = await post(client, 'test', {'config': form, 'connection': 'es'})
            no_form = await post(client, 'test', {'connection': 'es'})  # 没传 config：用已保存的
            es.distribution, es.version = 'opensearch', '2.17.0'
            es.fail['/_license'] = (400, {'error': 'no license api'})
            elastic.reset_caches()
            opensearch = await post(client, 'test', {'config': form, 'connection': 'es'})
            ch.route('version()', ch_error(516, 'Authentication failed: password is incorrect', 401))
            denied = await post(client, 'test', {'config': form, 'connection': 'ck'})
            unknown = await post(client, 'test', {'config': form, 'connection': 'zz'})
            invalid = await post(client, 'test', {'config': {'connections': 'x'}, 'connection': 'ck'})
            down_form = sample_config('http://127.0.0.1:9', es_url)
            down = await post(client, 'test', {'config': down_form, 'connection': 'ck'})
    assert ok_ch[0] == 200 and ok_ch[1]['data'] == {'type': 'clickhouse', 'version': '26.7.3.19',
                                                    'timezone': 'Asia/Shanghai', 'user': 'reader', 'readonly': 1,
                                                    'clusters': ['ck1', 'default']}
    first = ch.requests[0]
    assert 'readonly' not in first['query'] and first['headers']['X-ClickHouse-Key'] == 'ch-pass'  # 账号自己的设置
    assert ok_es[1]['data'] == {'type': 'elasticsearch', 'version': '9.5.2', 'distribution': 'elasticsearch',
                                'cluster_name': 'logs-es', 'license': 'basic', 'esql': True}
    assert no_form[0] == 200
    assert opensearch[1]['data']['esql'] is False and opensearch[1]['data']['license'] is None
    assert denied[0] == 502 and denied[1]['code'] == 'datasource_unavailable' and 'Authentication failed' in \
        denied[1]['message']
    assert unknown[0] == 400 and '没有连接 zz' in unknown[1]['message']
    assert invalid[0] == 400 and '连接要是列表' in invalid[1]['message']
    assert down[0] == 502 and '无法访问 ClickHouse' in down[1]['message']


# ---------------------------------------------------------------------------
# 预览、验证关联字段
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_admin_preview(monkeypatch):
    def rows(sql, query):
        names = ['kd_ms'] + [part.strip('`') for part in sql.split('\nFROM')[0].split(', ')[1:]]
        row = {'kd_ms': NOW_MS - 1000, 'path': '/a?token=x', 'host': 'h', 'message': f'TID:{TRACE}', 'status': 200}
        return compact(names, [[row.get(name) for name in names]])

    ch = FakeClickHouse(routes=[('AS kd_ms', rows)], columns={'logs.access': CH_COLUMNS})
    es = FakeElastic()
    es.fields['app-logs-*'] = ES_FIELDS
    es.on_search(lambda target, body: True, hits([hit('d1', NOW_MS - 5, {'message': 'm'})]))
    async with serve(ch, 'POST') as ch_url, serve(es) as es_url:
        form = sample_config(ch_url, es_url)
        form['sources'][0]['default_fields'] = ['path', 'host', 'gone']
        form['sources'][0]['columns'].append({'name': 'nothing', 'description': 'x'})
        async with TestClient(TestServer(make_app(monkeypatch, FakeStore()))) as client:
            ok = await post(client, 'preview', {'config': form, 'env': 'prod-a', 'source': 'ingress'})
            es_ok = await post(client, 'preview', {'config': form, 'env': 'prod-a', 'source': 'app'})
            disabled = await post(client, 'preview', {'config': form, 'env': 'prod-a', 'source': 'old'})
            unbound = await post(client, 'preview', {'config': form, 'env': 'prod-b', 'source': 'app'})
            missing = await post(client, 'preview', {'config': form, 'env': 'prod-a', 'source': 'nope'})
            no_env = await post(client, 'preview', {'config': form, 'source': 'app'})
            ch.route('AS kd_ms', ch_error(60, 'Table logs.access does not exist', 404))
            failed = await post(client, 'preview', {'config': form, 'env': 'prod-a', 'source': 'ingress'})
    assert ok[0] == 200
    data = ok[1]['data']
    assert data['ok'] is True and data['error'] is None and data['total'] is None and data['elapsed_ms'] == 15
    assert data['rows'] == [{'@time': common.iso_ms(NOW_MS - 1000), 'path': '/a?token=***', 'host': '***',
                             'trace_id': TRACE}]
    assert data['schema_problems'] == ['默认返回字段 gone 不存在', '字段说明里的字段 nothing 不存在']
    assert "`env` IN ['prod-a', 'prod-a2']" in data['query'] and 'LIMIT 5' in data['query']
    assert f'fromUnixTimestamp64Milli({NOW_MS - 15 * 60 * 1000})' in data['query']
    es_data = es_ok[1]['data']
    assert es_data['ok'] is True and es_data['total'] == 1 and json.loads(es_data['query'])['size'] == 5
    assert json.loads(es_data['query'])['query']['bool']['filter'][1] == {'term': {'env': 'prod-a'}}
    assert disabled[0] == 200 and disabled[1]['data']['ok'] is True  # 停用的日志源也能预览
    assert unbound[0] == 404 and unbound[1]['code'] == 'source_not_bound'
    assert missing[0] == 404 and missing[1]['code'] == 'source_not_found'
    assert no_env[0] == 400 and 'env 必填' in no_env[1]['message']
    assert failed[0] == 200 and failed[1]['data']['ok'] is False and 'does not exist' in failed[1]['data']['error']


@pytest.mark.asyncio
async def test_admin_verify(monkeypatch):
    def ch_handler(sql, query):
        if 'SELECT DISTINCT' in sql:  # request_id 列
            return compact(['v'], [['req-1'], ['req-2']])
        if 'multiSearchAnyCaseInsensitive' in sql:  # 在入口日志里找 ES 抽到的 trace_id
            return compact(['m'], [[f'GET /x TID:{TRACE}']])
        return compact(['m'], [[f'a TID:{TRACE}'], ['TID:not-hex'], [f'b TID:{"1" * 32}']])

    ch = FakeClickHouse(routes=[('SELECT', ch_handler)], columns={'logs.access': CH_COLUMNS})
    es = FakeElastic()
    es.fields['app-logs-*'] = ES_FIELDS
    es.on_search(lambda target, body: 'aggs' in body,
                 {'hits': {'hits': []}, 'aggregations': {'v': {'buckets': [{'key': TRACE}, {'key': 'f' * 32}]}}})
    async with serve(ch, 'POST') as ch_url, serve(es) as es_url:
        form = sample_config(ch_url, es_url)
        async with TestClient(TestServer(make_app(monkeypatch, FakeStore()))) as client:
            status, result = await post(client, 'verify', {'config': form, 'env': 'prod-a', 'key': 'trace_id'})
            single = await post(client, 'verify', {'config': form, 'env': 'prod-a', 'key': 'request_id'})
            bad = await post(client, 'verify', {'config': form, 'env': 'prod-a', 'key': 'nope'})
            no_key = await post(client, 'verify', {'config': form, 'env': 'prod-a'})
    assert status == 200, result
    pairs = {(item['from'], item['to']): item for item in result['data']['pairs']}
    assert pairs[('ingress', 'app')] == {'from': 'ingress', 'to': 'app', 'sampled': 2, 'matched': 1,
                                         'examples': [TRACE]}
    assert pairs[('app', 'ingress')]['sampled'] == 2 and pairs[('app', 'ingress')]['matched'] == 1
    es_terms = [body for body in es.bodies() if 'aggs' in body]
    assert {'terms': {'trace_id': [TRACE, '1' * 32]}} in es_terms[-1]['query']['bool']['filter']
    assert single[1]['data']['pairs'] == [] and '不到 2 个' in single[1]['data']['notes'][0]
    assert bad[0] == 400 and '没有关联字段 nope' in bad[1]['message']
    assert no_key[0] == 400


# ---------------------------------------------------------------------------
# 扫描
# ---------------------------------------------------------------------------
LOCAL_COLUMNS = [('start_time', 'DateTime64(3)'), ('env', 'LowCardinality(String)'), ('path', 'String'),
                 ('status', 'UInt16'), ('request_time', 'Float64'), ('method', 'LowCardinality(String)'),
                 ('trace_id', 'String'), ('message', 'String')]


def ch_scan_fake():
    tables = [
        ['logs', 'access', 'Distributed', "Distributed('ck1', 'logs', 'access_local', rand())", 0, 0, '', '', '', ''],
        ['logs', 'access_local', 'ReplicatedMergeTree', '', 1000, 2048, 'toYYYYMMDD(start_time)', 'env, start_time',
         'CREATE TABLE logs.access_local (...) ENGINE = ReplicatedMergeTree ORDER BY (env, start_time) '
         'TTL toDateTime(start_time) + toIntervalDay(15)', '入口访问日志'],
        ['logs', 'shop_east_access', 'MergeTree', '', 50, 100, '', 'start_time', 'CREATE TABLE x', ''],
        ['logs', 'access_view', 'View', '', 0, 0, '', '', '', ''],
        ['other', 'secret', 'MergeTree', '', 1, 1, '', '', '', ''],
    ]
    columns = [['logs', table, name, type_] for table in ('access', 'access_local', 'shop_east_access')
               for name, type_ in LOCAL_COLUMNS]

    def handler(sql, query):
        if 'FROM system.tables' in sql:
            return compact(['database', 'name', 'engine', 'engine_full', 'total_rows', 'total_bytes', 'partition_key',
                            'sorting_key', 'create_table_query', 'comment'], tables)
        if "clusterAllReplicas('ck1', system.parts)" in sql:
            return compact(['database', 'table', 'rows', 'first', 'last'],
                           [['logs', 'access_local', 3000, int(NOW) - 86400, int(NOW)]])
        if 'FROM system.parts' in sql:
            return ch_error(497, 'Not enough privileges', 403)
        if 'FROM system.columns' in sql:
            return compact(['database', 'table', 'name', 'type'], columns)
        if 'topK(10)' in sql:
            if '`logs`.`access`' in sql:
                return compact(['v0', 'v1'], [[['prod-a', '生产A', 'prod-b'], ['GET', 'POST']]])
            return compact(['v0'], [[['east']]])
        if sql.startswith('SELECT *'):
            return compact([name for name, _ in LOCAL_COLUMNS],
                           [['2026-11-23 15:59:00.000', 'prod-a', '/a?token=x', 200, 0.25, 'GET', TRACE,
                             f'ok TID:{TRACE}']])
        return compact(['version', 'timezone'], [['26.7.3.19', 'Asia/Shanghai']])

    return FakeClickHouse(routes=[('', handler)])


@pytest.mark.asyncio
async def test_scan_clickhouse(monkeypatch):
    ch = ch_scan_fake()
    async with serve(ch, 'POST') as ch_url:
        form = sample_config(ch_url)
        async with TestClient(TestServer(make_app(monkeypatch, FakeStore()))) as client:
            status, result = await post(client, 'scan', {'config': form, 'connection': 'ck', 'exclude': ['other.*']})
            bad = await post(client, 'scan', {'config': form, 'connection': 'ck', 'include': 'x'})
    assert status == 200, result
    data = result['data']
    assert data['connection'] == {'id': 'ck', 'type': 'clickhouse', 'version': '26.7.3.19', 'timezone': 'Asia/Shanghai'}
    assert data['partial'] is False and any('system.parts' in note for note in data['notes'])
    [group] = data['groups']
    assert group['ref'] == 'g1' and group['name_guess'] == 'access' and group['kind_guess'] == 'access'
    assert group['fields_guess'] == {'time': 'start_time', 'message': 'message', 'status': 'status',
                                     'duration': 'request_time', 'method': 'method', 'path': 'path',
                                     'duration_unit': 's'}
    assert group['cluster_field_guess'] == 'env' and group['meta_description'] == '入口访问日志'
    assert group['id_fields'] == [{'field': 'trace_id', 'shape': 'hex32'}]
    assert group['text_ids'] == [{'text': 'TID:{value}', 'shape': 'hex32', 'count': 1}]
    first, second = group['targets']
    assert first['target'] == 'logs.access' and first['local'] == 'logs.access_local' and first['cluster'] == 'ck1'
    assert first['rows'] == 3000 and first['retention_days'] == 15 and first['last'] == common.iso_ms(NOW_MS)
    assert first['values'] == {'env': ['prod-a', '生产A', 'prod-b'], 'method': ['GET', 'POST']}
    assert first['samples'][0]['path'] == '/a?token=***' and first['sorting_key'] == 'env, start_time'
    assert second['target'] == 'logs.shop_east_access' and second['rows'] == 50 and second['first'] is None
    assert second['values'] == {'env': ['east']} and second['samples'] == []
    assert data['suggested_bindings'] == [
        {'env': 'prod-a', 'group': 'g1', 'target': 'logs.access', 'match': 'exact',
         'conditions': [{'field': 'env', 'op': 'in', 'values': ['prod-a', '生产A']}]},
        {'env': 'shop-east', 'group': 'g1', 'target': 'logs.shop_east_access', 'match': 'token',
         'conditions': [{'field': 'env', 'op': 'in', 'values': ['east']}]}]
    assert data['clusters'] == [{'env': 'prod-a', 'aliases': ['生产A']}, {'env': 'shop-east', 'aliases': []}]
    assert bad[0] == 400 and 'include 要是通配符列表' in bad[1]['message']
    assert all('other' not in item['sql'] or 'NOT IN' in item['sql'] for item in ch.requests)


@pytest.mark.asyncio
async def test_scan_elastic(monkeypatch):
    es = FakeElastic()
    es.resolve = {
        'data_streams': [{'name': 'app-logs-prod', 'backing_indices': ['.ds-app-logs-prod-2026.11.23-000001']}],
        'aliases': [{'name': 'audit', 'indices': ['audit-v1']}],
        'indices': [{'name': 'nginx-2026.11.22'}, {'name': 'nginx-2026.11.23'}, {'name': '.kibana_1'},
                    {'name': 'audit-v1', 'aliases': ['audit']},
                    {'name': '.ds-app-logs-prod-2026.11.23-000001', 'data_stream': 'app-logs-prod'}]}
    es.fields['app-logs-prod'] = ES_FIELDS
    es.fields['nginx-*'] = caps(**{'@timestamp': 'date', 'status': 'long', 'path': 'keyword', 'host': 'keyword',
                                   'request_time': 'float', 'k8sname': 'keyword'})
    es.fields['audit'] = {}
    es.counts = {'app-logs-prod': 1000, 'nginx-*': 20}
    es.index_templates = {'index_templates': [
        {'name': 'app', 'index_template': {'index_patterns': ['app-logs-*'], 'priority': 10, 'composed_of': ['c1']}}]}
    es.component_templates = {'component_templates': [
        {'name': 'c1', 'component_template': {'_meta': {'description': 'Java 应用日志'}}}]}
    es.data_streams = {'app-logs-prod': {'name': 'app-logs-prod', 'ilm_policy': 'logs-15d'}}
    es.ilm = {'logs-15d': {'policy': {'phases': {'delete': {'min_age': '15d'}}}}}
    es.on_search(lambda target, body: 'first' in (body.get('aggs') or {}),
                 {'hits': {'hits': []}, 'aggregations': {'first': {'value': NOW_MS - 86400000},
                                                         'last': {'value': NOW_MS}}})
    es.on_search(lambda target, body: 'v0' in (body.get('aggs') or {}),
                 {'hits': {'hits': []}, 'aggregations': {'v0': {'buckets': [{'key': 'shop-east-1'}]}}})
    es.on_search(lambda target, body: 'v0' in (body.get('aggs') or {}) and target == 'app-logs-prod',  # 后加的先匹配
                 {'hits': {'hits': []}, 'aggregations': {'v0': {'buckets': [{'key': 'prod-a'}, {'key': 'prod-b'}]},
                                                         'v1': {'buckets': [{'key': 'ERROR'}]}}})
    es.on_search(lambda target, body: body.get('size') == 2 and target == 'app-logs-prod',
                 hits([hit('d1', NOW_MS, {'message': f'x TID:{TRACE}', 'env': 'prod-a', 'password': 'p',
                                          'trace_id': TRACE})]))
    es.on_search(lambda target, body: body.get('size') == 50,
                 hits([hit(f'm{i}', NOW_MS, {'message': f'"traceId":"{TRACE}" TID:{TRACE}'}) for i in range(3)]))
    async with serve(es) as es_url:
        form = sample_config(es_url=es_url)
        async with TestClient(TestServer(make_app(monkeypatch, FakeStore()))) as client:
            status, result = await post(client, 'scan', {'config': form, 'connection': 'es'})
    assert status == 200, result
    data = result['data']
    assert data['connection'] == {'id': 'es', 'type': 'elasticsearch', 'version': '9.5.2',
                                  'distribution': 'elasticsearch'}
    names = [group['targets'][0]['target'] for group in data['groups']]
    assert names == ['app-logs-prod', 'nginx-*']  # audit 没有字段：跳过；. 开头的不列
    app, nginx = data['groups']
    target = app['targets'][0]
    assert target['kind'] == 'data_stream' and target['rows'] == 1000 and target['retention_days'] == 15
    assert target['first'] == common.iso_ms(NOW_MS - 86400000) and target['values'] == {
        'env': ['prod-a', 'prod-b'], 'level': ['ERROR']}
    assert target['samples'][0]['password'] == '***' and app['meta_description'] == 'Java 应用日志'
    assert app['kind_guess'] == 'app' and app['cluster_field_guess'] == 'env'
    assert app['fields_guess']['pod'] == 'podname' and app['fields_guess']['service'] == 'appname'
    assert {'text': 'TID:{value}', 'shape': 'hex32', 'count': 4} in app['text_ids']
    assert {'text': '"traceId":"{value}"', 'shape': 'hex32', 'count': 3} in app['text_ids']
    assert nginx['kind_guess'] == 'access' and nginx['cluster_field_guess'] == 'k8sname'
    assert nginx['targets'][0]['kind'] == 'pattern' and nginx['fields_guess']['duration_unit'] == 'ms'
    suggestions = {(item['env'], item['target']): item for item in data['suggested_bindings']}
    assert suggestions[('prod-a', 'app-logs-prod')]['match'] == 'exact'
    assert suggestions[('shop-east', 'nginx-*')] == {'env': 'shop-east', 'group': 'g2', 'target': 'nginx-*',
                                                    'match': 'token', 'conditions': [
                                                        {'field': 'k8sname', 'op': 'in', 'values': ['shop-east-1']}]}


@pytest.mark.asyncio
async def test_scan_deadline_returns_partial(monkeypatch):
    es = FakeElastic()
    es.resolve = {'data_streams': [{'name': 'a-logs'}, {'name': 'b-logs'}], 'aliases': [], 'indices': []}
    es.fields['a-logs'] = es.fields['b-logs'] = ES_FIELDS
    conn = log_config.normalize_config(sample_config(es_url='http://x'))['connections'][1]
    async with serve(es) as es_url:
        conn = {**conn, 'url': es_url}
        from func_manager import ext_api

        async with ext_api.client_session() as session:
            result = await scan.run(conn, session, [], [], [], seconds=0)  # 一开始就到点
    assert result['partial'] is True and result['groups'] == []


def test_scan_rules():
    assert scan.pattern_of('nginx-2026.11.23') == 'nginx-*' and scan.pattern_of('app-000001') == 'app-*'
    assert scan.name_guess('logs.dist_gateway_access') == 'gateway_access'
    assert scan.name_guess('logs.ingress_local') == 'ingress' and scan.name_guess('nginx-*') == 'nginx'
    assert scan.retention_from_ttl('TTL ts + INTERVAL 30 DAY') == 30 and scan.retention_from_ttl('') is None
    assert scan.duration_days('720h') == 30 and scan.duration_days('15d') == 15 and scan.duration_days('x') is None
    assert scan.selected('logs.a', ['logs.*'], ['*.b']) and not scan.selected('logs.b', ['logs.*'], ['*.b'])
    assert scan.kind_guess({'message': 'm'}) == 'app' and scan.kind_guess({}) == 'other'
    assert scan.tokens('prod-shop_east.logs') == {'shop', 'east'}
    groups = [{'ref': 'g1', 'cluster_field_guess': 'env', 'targets': [
        {'target': 'logs.prod_shop', 'values': {'env': ['SHOP-alias']}}, {'target': 'logs.x', 'values': {}}]}]
    clusters = [{'env': 'shop', 'aliases': ['shop-alias']}]
    assert scan.suggest_bindings(groups, clusters, 'clickhouse') == [
        {'env': 'shop', 'group': 'g1', 'target': 'logs.prod_shop', 'match': 'alias',
         'conditions': [{'field': 'env', 'op': 'in', 'values': ['SHOP-alias']}]}]
    groups[0]['targets'][0]['values'] = {}
    groups[0]['cluster_field_guess'] = ''
    assert scan.suggest_bindings(groups, clusters, 'clickhouse')[0]['match'] == 'table'
    # 一个集群一个索引同样按名字建议（ES 也常见）
    assert scan.suggest_bindings(groups, clusters, 'elasticsearch')[0]['target'] == 'logs.prod_shop'


def test_value_suggestions_never_borrow_other_clusters_values():
    """集群字段的取值只要和某个集群名一样，就只属于那个集群：不会因为有共同的词（east）被建议给别的集群"""
    clusters = [{'env': 'shop-east'}, {'env': 'shop-west'}, {'env': 'pay-east'}, {'env': 'shop-beta-east'}]
    groups = [{'ref': 'g1', 'cluster_field_guess': 'env', 'targets': [
        {'target': 'logs.prod_access', 'values': {'env': ['shop-east', 'shop-west', 'pay-east-2']}},
        {'target': 'logs.beta_access', 'values': {'env': ['shop-beta-east']}}]}]
    picked = {item['env']: (item['target'], item['match'], item['conditions'][0]['values'])
              for item in scan.suggest_bindings(groups, clusters, 'clickhouse')}
    assert picked == {'shop-east': ('logs.prod_access', 'exact', ['shop-east']),
                      'shop-west': ('logs.prod_access', 'exact', ['shop-west']),
                      'pay-east': ('logs.prod_access', 'token', ['pay-east-2']),
                      'shop-beta-east': ('logs.beta_access', 'exact', ['shop-beta-east'])}
    weights = scan.name_weights(clusters)
    assert scan.value_owners(['shop-east', 'east-x'], clusters, weights, {'shop-east'}) == {}
    # Elasticsearch：同一组里这个集群的数据分在几个流里，合成一条逗号分隔的绑定；ClickHouse 只取第一张表
    streams = [{'ref': 'g1', 'cluster_field_guess': 'env', 'targets': [
        {'target': 'app-java-a', 'values': {'env': ['shop-east']}}, {'target': 'app-java-b', 'values': {'env': ['shop-east']}}]}]
    [merged] = scan.suggest_bindings(streams, [{'env': 'shop-east'}], 'elasticsearch')
    assert merged['target'] == 'app-java-a,app-java-b' and merged['conditions'][0]['values'] == ['shop-east']
    [single] = scan.suggest_bindings(streams, [{'env': 'shop-east'}], 'clickhouse')
    assert single['target'] == 'app-java-a'


def test_table_suggestions_score_names():
    """没有集群字段时按表名打分：共同词按区分度加分、beta 这类环境词一边有一边没有扣分、一张表只配一个集群、
    只有所有集群都有的词或并列时不建议"""
    clusters = [{'env': 'shop-east'}, {'env': 'shop-west'}, {'env': 'shop-beta-east'}, {'env': 'pay-east'}]
    names = ['logs.shop_cnprod_east_ingress', 'logs.shop_cnbeta_east_ingress', 'logs.shop_prod_west_ingress',
             'logs.pay_prod_east_ingress', 'logs.shop_edge_nginx']
    groups = [{'ref': 'g1', 'cluster_field_guess': '', 'targets': [{'target': name} for name in names]}]
    picked = {item['env']: item['target'] for item in scan.suggest_bindings(groups, clusters, 'clickhouse')}
    assert picked == {'shop-east': 'logs.shop_cnprod_east_ingress', 'shop-beta-east': 'logs.shop_cnbeta_east_ingress',
                      'shop-west': 'logs.shop_prod_west_ingress', 'pay-east': 'logs.pay_prod_east_ingress'}
    weights = scan.name_weights(clusters)
    assert scan.table_score({'env': 'shop-east'}, 'logs.shop_cnbeta_east_ingress', weights) < scan.MIN_TABLE_SCORE
    assert scan.table_score({'env': 'shop-east'}, 'logs.shop_edge_nginx', weights) < scan.MIN_TABLE_SCORE
    # 别名同样参与打分
    assert scan.table_score({'env': 'x1', 'aliases': ['pay-east']}, 'logs.pay_prod_east_ingress', weights) > 2
    # 并列：分不清是哪个集群，不建议
    tie = [{'ref': 'g1', 'cluster_field_guess': '', 'targets': [{'target': 'logs.east_ingress'}]}]
    assert scan.suggest_bindings(tie, [{'env': 'east-1'}, {'env': 'east-2'}], 'clickhouse') == []
    # 只有通用词相同（prod）不算
    assert scan.suggest_bindings([{'ref': 'g1', 'cluster_field_guess': '', 'targets': [{'target': 'logs.prod_app'}]}],
                                 [{'env': 'other-prod'}], 'clickhouse') == []


def test_scan_grouping_helpers():
    assert scan.name_guess('logs.b2b_prod_ingress') == 'b2b_prod_ingress'
    assert scan.name_guess('app-logs-2026.11.23') == 'app-logs' and scan.name_guess('.ds-app-std') == 'app-std'
    assert scan.group_name([{'target': 'logs.shop_east_ingress'}, {'target': 'logs.pay_east_ingress'}]) == 'east_ingress'
    assert scan.group_name([{'target': 'a.dist_gw'}, {'target': 'b.dist_gw'}]) == 'gw'
    assert scan.group_name([{'target': 'logs.x1'}, {'target': 'logs.y2'}]) == 'x1'
    targets = [{'target': 'a', 'rows': 0}, {'target': 'b', 'rows': 10}, {'target': 'c', 'rows': None}]
    assert scan.representative_first(targets)['target'] == 'b' and targets[0]['target'] == 'b'
    by_env = {'columns': [], 'targets': [{'target': f'{db}.{table}'} for db in ('prod', 'test')
                                          for table in ('gw_access', 'web_access')]}
    per_cluster = {'columns': [], 'targets': [{'target': 'logs.a_ingress'}, {'target': 'logs.b_ingress'}]}
    split = scan.split_by_table([by_env, per_cluster])
    assert [[item['target'] for item in group['targets']] for group in split] == [
        ['prod.gw_access', 'test.gw_access'], ['prod.web_access', 'test.web_access'],
        ['logs.a_ingress', 'logs.b_ingress']]
    # 名字以 time 结尾的浮点耗时按秒，整数的按毫秒
    assert scan.guess_fields([('responsetime', 'float')])['duration_unit'] == 's'
    assert scan.guess_fields([('upstream_service_time', 'int')])['duration_unit'] == 'ms'


# ---------------------------------------------------------------------------
# 经过真实的鉴权中间件（policy.RULES）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_routes_behind_auth_middleware(monkeypatch, audits):
    from auth import middleware as auth_middleware

    monkeypatch.setenv('AI_INTERNAL_TOKEN', 'secret-test-token')
    users = {'reader': Principal('reader', 'read', user_id=2, via='internal'),
             'nobody': Principal('nobody', 'none', user_id=3, via='internal'),
             'root': Principal('root', 'admin', user_id=1, via='internal')}

    async def principal_by_name(username, via):
        return users.get(username)

    monkeypatch.setattr(auth_middleware.store, 'principal_by_name', principal_by_name)
    ch = FakeClickHouse(columns={'logs.access': CH_COLUMNS})
    async with serve(ch, 'POST') as ch_url:
        store = FakeStore(log_config.normalize_config(sample_config(ch_url)))
        monkeypatch.setattr(log_api, 'store', store)
        monkeypatch.setattr(log_config, 'store', store)
        app = web.Application(middlewares=[auth_middleware.create_middleware()])
        log_api.register_routes(app)
        async with TestClient(TestServer(app)) as client:
            async def call(method, path, user, body=None, params=None):
                resp = await client.request(method, path, params=params, json=body or {}, headers={
                    'X-Kubedoor-Token': 'secret-test-token', 'X-User-Name': user})
                return resp.status, await resp.json()

            results = [
                await call('POST', '/api/ai/internal/logs/search', 'reader', {'source': 'ingress'}, {'env': 'prod-a'}),
                await call('POST', '/api/ai/internal/logs/search', 'nobody', {'source': 'ingress'}, {'env': 'prod-a'}),
                await call('POST', '/api/admin/log-sources/test', 'reader', {'connection': 'ck'}),
                await call('GET', '/api/admin/log-sources', 'reader'),
                await call('PUT', '/api/admin/log-sources', 'root', {'enabled': False}),
            ]
            browser = await client.post('/api/ai/internal/logs/search', params={'env': 'prod-a'}, json={})
    assert [(status, data.get('code')) for status, data in results] == [
        (200, None), (403, 'permission_denied'), (403, 'admin_required'), (403, 'admin_required'), (200, None)]
    assert browser.status == 401
    # AI 查日志不记审计；被拒的管理写接口按 api.denied 记，保存配置记 api.write + log_sources.update
    assert [record['action'] for record in audits] == ['api.denied', 'log_sources.update', 'api.write']


def test_routes_have_policy_rules():
    from auth import policy

    app = web.Application()
    log_api.register_routes(app)
    fallback = policy.RULES[-1]
    routes = [(route.method, route.resource.canonical) for route in app.router.routes() if route.method != 'HEAD']
    assert len(routes) == 7
    for method, path in routes:
        sample = path.replace('{op}', 'search')
        rule = policy.match(method, sample)
        assert rule is not fallback and rule.kind in ('admin', 'internal'), (method, path)
