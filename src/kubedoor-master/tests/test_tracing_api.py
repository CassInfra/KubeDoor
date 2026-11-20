"""链路追踪接口（tracing/api.py、ops.py）：AI 内部接口的鉴权和错误码、各操作（服务、接口、取链路、看 Span、字段）、
按 ID 取链路的时间范围逐级放大、管理接口（配置、测试连接、识别字段）、经过真实鉴权中间件的路由。"""
import json
from datetime import datetime
from types import MappingProxyType

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from auth.crypto import decrypt_secret
from auth.principal import SYSTEM, Principal
from tracing import api as tracing_api
from tracing import clickhouse, common, jaeger, ops, search
from tracing import config as tracing_config
from tracing import fields as field_map
from tracing_fakes import CAPS, FakeClickHouse, FakeJaeger, TraceBuilder, compact, serve, summary

NOW = 1_795_203_600.0
TRACE = '0af7651916cd43dd8448eb211c80319c'
ALIASES = {'prod': ['蓬莱'], 'test': []}


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    monkeypatch.setattr(common, '_clock', lambda: NOW)
    monkeypatch.setenv('AI_ENCRYPTION_KEY', '11' * 32)

    async def aliases():
        return ALIASES

    monkeypatch.setattr(field_map, 'load_aliases', aliases)
    jaeger.reset_caches()
    clickhouse.reset_state()
    ops.reset_caches()
    search._trace_times.clear()


@pytest.fixture
def audits(monkeypatch):
    records = []
    monkeypatch.setattr(tracing_api.audit, 'record_bg',
                        lambda username, action, **kwargs: records.append({'username': username, 'action': action,
                                                                           **kwargs}))
    return records


class FakeStore:
    def __init__(self, value=None, broken=False):
        self.broken = broken
        self.row = None if value is None else {'value': value, 'updated_by': 'admin',
                                               'updated_at': datetime(2026, 11, 21, 10, 0)}

    async def get_setting(self, key, default=None):
        assert key == tracing_config.SETTING_KEY
        if self.broken:
            raise RuntimeError('db down')
        return self.row['value'] if self.row else default

    async def get_setting_row(self, key):
        return self.row

    async def put_setting_row(self, key, value, updated_by):
        self.row = {'value': json.loads(json.dumps(value)), 'updated_by': updated_by,
                    'updated_at': datetime(2026, 11, 21, 11, 0)}
        return self.row


def internal(role='read', **kwargs):
    return Principal('alice', role, user_id=7, via='internal', **kwargs)


def make_app(monkeypatch, store, principal=None):
    monkeypatch.setattr(tracing_api, 'store', store)
    monkeypatch.setattr(tracing_config, 'store', store)
    principal = principal or internal()

    @web.middleware
    async def inject(request, handler):
        request['principal'] = principal
        return await handler(request)

    app = web.Application(middlewares=[inject])
    tracing_api.register_routes(app)
    return app


def enabled(url, **extra):
    return tracing_config.normalize_config({'enabled': True, 'url': url, 'cluster_key': 'k8sname', **extra})


async def call(client, op, body=None, env='prod'):
    params = {'env': env} if env is not None else {}
    resp = await client.post(f'/api/ai/internal/tracing/{op}', params=params, json=body if body is not None else {})
    return resp.status, await resp.json()


# ---------------------------------------------------------------------------
# AI 内部接口：鉴权、开关、错误码
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize('principal, env, op, status, code', [
    (Principal('alice', 'admin', user_id=7), 'prod', 'services', 403, 'forbidden'),  # 浏览器会话不是可信的 AI 回调
    (SYSTEM, 'prod', 'services', 403, 'forbidden'),
    (internal(role='none'), 'prod', 'services', 403, 'permission_denied'),
    (internal(clusters=MappingProxyType({'prod': 'none'})), 'prod', 'services', 403, 'permission_denied'),
    (internal(), None, 'services', 400, 'invalid_request'),
    (internal(), 'prod', 'dump', 400, 'invalid_request'),
])
async def test_internal_auth_and_validation(monkeypatch, principal, env, op, status, code):
    async with TestClient(TestServer(make_app(monkeypatch, FakeStore(enabled('http://127.0.0.1:9')), principal))) as c:
        got_status, data = await call(c, op, env=env)
    assert (got_status, data['code'], data['success']) == (status, code, False)


@pytest.mark.asyncio
@pytest.mark.parametrize('store, status, code, message', [
    (FakeStore(None), 400, 'not_configured', '系统管理员还没有配置链路追踪（系统管理 → 链路追踪）'),
    (FakeStore({'enabled': False, 'url': 'http://j:16686'}), 400, 'not_configured', '系统管理员还没有配置链路追踪'),
    (FakeStore(broken=True), 503, 'config_unavailable', '读取链路追踪配置失败'),
])
async def test_internal_requires_enabled_config(monkeypatch, store, status, code, message):
    async with TestClient(TestServer(make_app(monkeypatch, store))) as client:
        got_status, data = await call(client, 'search', {'service': 'a'})
    assert got_status == status and data['code'] == code and data['message'].startswith(message)


@pytest.mark.asyncio
async def test_internal_bad_body_and_unexpected_errors(monkeypatch):
    from loguru import logger

    async def boom(ctx, body):
        secret = 'TOP-' + 'SECRET'  # 栈上的变量值不能进日志
        raise KeyError(len(secret))

    logs = []
    handler_id = logger.add(lambda message: logs.append(str(message)), level='DEBUG')

    monkeypatch.setitem(tracing_api.OPS, 'services', boom)
    async with TestClient(TestServer(make_app(monkeypatch, FakeStore(enabled('http://127.0.0.1:9'))))) as client:
        resp = await client.post('/api/ai/internal/tracing/fields', params={'env': 'prod'}, data='not json',
                                 headers={'Content-Type': 'application/json'})
        bad = await resp.json()
        array = await client.post('/api/ai/internal/tracing/fields', params={'env': 'prod'}, json=[1])
        array_data = await array.json()
        status, crash = await call(client, 'services')
    logger.remove(handler_id)
    assert any('链路追踪 services 出错: KeyError' in line for line in logs)
    assert not any('TOP-SECRET' in line for line in logs)
    assert resp.status == 400 and bad['code'] == 'invalid_request' and '请求体要是 JSON 对象' in bad['message']
    assert array.status == 400 and array_data['code'] == 'invalid_request'
    assert status == 500 and crash['code'] == 'internal_error' and 'KeyError' in crash['message']


# ---------------------------------------------------------------------------
# 服务、接口
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_services_and_operations_from_jaeger(monkeypatch):
    fake = FakeJaeger()
    fake.services = [f'svc-{i:03d}' for i in range(300)] + ['Gateway-API']
    async with serve(fake) as url:
        async with TestClient(TestServer(make_app(monkeypatch, FakeStore(enabled(url))))) as client:
            _, first = await call(client, 'services', {'pattern': 'GATEWAY'})
            _, second = await call(client, 'services', {'limit': 10})
            _, operations = await call(client, 'operations', {'service': 'svc-001', 'span_kind': 'server',
                                                              'pattern': 'get', 'lookback': '1h'})
            status, missing = await call(client, 'operations', {})
            bad_status, bad = await call(client, 'services', {'limit': 501})
    assert first['data'] == {'source': 'jaeger', 'services': [{'name': 'Gateway-API'}], 'total': 1, 'truncated': False,
                             'note': 'Jaeger 的服务列表是所有集群的'}
    assert second['data']['total'] == 301 and len(second['data']['services']) == 10 and second['data']['truncated']
    assert len(fake.paths('/api/v3/services')) == 1  # 缓存 60 秒
    assert operations['data']['operations'] == [{'name': 'GET /x', 'kind': 'server'}]
    assert 'lookback 只对 ClickHouse 加速有效' in operations['data']['note']
    assert ('/api/v3/operations', {'service': 'svc-001', 'spanKind': 'server'}) in fake.requests
    assert status == 400 and missing['message'] == 'service 必填'
    assert bad_status == 400 and '1~500' in bad['message']


@pytest.mark.asyncio
async def test_services_and_operations_from_clickhouse(monkeypatch):
    def handler(sql, query):
        if 'GROUP BY service_name' in sql:
            return compact(['service_name', 'spans'], [['gateway', '900'], ['stock', '100']])
        return compact(['kind', 'operation', 'url_path', 'spans'],
                       [['server', 'POST /a', '/a?token=x', '50'], ['', 'GET', '', '7']])

    fake_ch, fake_jaeger = FakeClickHouse(handler), FakeJaeger()
    async with serve(fake_ch, 'POST') as ch_url, serve(fake_jaeger) as url:
        store = FakeStore(enabled(url, clickhouse={'enabled': True, 'url': ch_url}))
        async with TestClient(TestServer(make_app(monkeypatch, store))) as client:
            _, services = await call(client, 'services', {'pattern': 'a', 'limit': 1})
            _, operations = await call(client, 'operations', {'service': 'gateway', 'span_kind': 'server',
                                                              'pattern': '/a', 'lookback': '2h'})
    assert services['data'] == {'source': 'clickhouse', 'services': [{'name': 'gateway', 'spans_24h': 900}],
                                'total': 2, 'truncated': True, 'cluster': {'key': 'k8sname', 'values': ['prod', '蓬莱']},
                                'note': '最近 24 小时当前集群有 Span 的服务，按 Span 数排序'}
    assert operations['data']['operations'] == [
        {'name': 'POST /a', 'kind': 'server', 'path': '/a?token=***', 'spans': 50},
        {'name': 'GET', 'kind': 'unspecified', 'path': '', 'spans': 7}]
    first, second = fake_ch.requests
    assert 'FROM `jaeger`.`trace_search_dim`' in first['sql'] and 'k8s_cluster IN {clusters:Array(String)}' in first['sql']
    assert first['query']['param_s'] == str(int((NOW - 86400) * 1000)) and first['query']['param_p'] == 'a'
    assert second['query']['param_s'] == str(int((NOW - 7200) * 1000)) and second['query']['param_kind'] == 'server'
    assert 'positionCaseInsensitive(url_path, {p:String})' in second['sql'] and fake_jaeger.requests == []


# ---------------------------------------------------------------------------
# 按 ID 取链路
# ---------------------------------------------------------------------------
def sample_trace():
    builder = TraceBuilder(TRACE)
    builder.span('a1', 'POST bizme', service='gateway', duration=7000, http=504, error=True)
    builder.span('a2', 'POST', parent='a1', service='gateway', kind='client', start=1, duration=6995,
                 attributes={'url.full': 'http://inventory/retry?token=abc', 'http.request.header.authorization': 'x'})
    builder.span('a3', 'POST /retry', parent='a2', service='inventory', start=2, duration=11000, http=200,
                 error=False, events=[('exception', {'exception.type': 'IOException',
                                                     'exception.stacktrace': 's' * 5000})],
                 attributes={'big': 'v' * 3000})
    return builder


@pytest.mark.asyncio
async def test_trace_time_ranges_escalate(monkeypatch):
    seen = []

    def handler(trace_id, query):
        seen.append(query)
        return sample_trace().doc() if len(seen) == 4 else None  # 前 3 个时间范围 404

    fake = FakeJaeger()
    fake.trace = handler
    search.remember(TRACE, int((NOW - 120) * 1000))
    async with serve(fake) as url:
        async with TestClient(TestServer(make_app(monkeypatch, FakeStore(enabled(url))))) as client:
            status, data = await call(client, 'trace', {'trace_id': TRACE.upper(), 'around': '2026-11-21 03:00'})
            # 再取同一条（看 Span 详情）用缓存，不再访问 Jaeger
            _, spans = await call(client, 'spans', {'trace_id': TRACE, 'errors': True})
    assert status == 200 and data['success'] is True
    around = datetime(2026, 11, 21, 3, 0, tzinfo=common.BEIJING).timestamp()
    expected = [(NOW - 720, NOW + 480), (around - 1800, around + 1800), (around - 21600, around + 21600),
                (NOW - 3600, NOW)]
    assert [(query['startTime'], query['endTime']) for query in seen] == [
        (common.rfc3339(start), common.rfc3339(end)) for start, end in expected]
    assert data['data']['trace']['time_range_used'] == '最近 1 小时'
    assert data['data']['trace']['url'] == f'{url}/trace/{TRACE}' and data['data']['trace']['outcome'] == 'failed'
    [cause] = data['data']['analysis']['root_cause']
    assert cause['service'] == 'inventory' and cause['reason'].startswith('父调用在等子调用')
    assert spans['data']['spans'][0]['span_id'] == '00000000000000a1' and len(seen) == 4


@pytest.mark.asyncio
async def test_trace_time_range_order_and_not_found(monkeypatch):
    seen = []
    fake = FakeJaeger()
    fake.trace = lambda trace_id, query: seen.append(query)
    search.remember('abc'.rjust(32, '0'), int((NOW - 120) * 1000))
    around = datetime(2026, 11, 21, 3, 0, tzinfo=common.BEIJING).timestamp()
    async with serve(fake) as url:
        async with TestClient(TestServer(make_app(monkeypatch, FakeStore(enabled(url))))) as client:
            status, data = await call(client, 'trace', {'trace_id': 'abc', 'around': '2026-11-21 03:00'})
    assert status == 404 and data['code'] == 'not_found' and '没有找到链路 ' + 'abc'.rjust(32, '0') in data['message']
    expected = [(NOW - 720, NOW + 480), (around - 1800, around + 1800), (around - 21600, around + 21600),
                (NOW - 3600, NOW), (NOW - 86400, NOW), (NOW - 7 * 86400, NOW)]
    assert [(query['startTime'], query['endTime']) for query in seen[:-1]] == [
        (common.rfc3339(start), common.rfc3339(end)) for start, end in expected]
    assert seen[-1] == {}  # 最后一次不带时间范围


@pytest.mark.asyncio
async def test_trace_other_errors_fail_fast(monkeypatch):
    fake = FakeJaeger()
    fake.fail = {'/api/v3/traces/': (500, {'error': {'httpCode': 500, 'message': 'clickhouse: too many queries'}})}
    async with serve(fake) as url:
        async with TestClient(TestServer(make_app(monkeypatch, FakeStore(enabled(url))))) as client:
            status, data = await call(client, 'trace', {'trace_id': TRACE})
            bad_status, bad = await call(client, 'trace', {'trace_id': 'xyz'})
            focus_status, focus = await call(client, 'trace', {'trace_id': TRACE, 'focus_span': 'zz'})
            nodes_status, nodes = await call(client, 'trace', {'trace_id': TRACE, 'max_nodes': 20})
    assert status == 502 and data['code'] == 'jaeger_unavailable' and 'too many queries' in data['message']
    assert len(fake.paths('/api/v3/traces/')) == 1  # 不是 404：不再换时间范围
    assert bad_status == 400 and 'trace_id 要是 1~32 位十六进制' in bad['message']
    assert focus_status == 400 and 'focus_span' in focus['message']
    assert nodes_status == 400 and '50~400' in nodes['message']


@pytest.mark.asyncio
async def test_spans_detail_masks_and_truncates(monkeypatch):
    fake = FakeJaeger()
    fake.trace = lambda trace_id, query: sample_trace().doc()
    async with serve(fake) as url:
        async with TestClient(TestServer(make_app(monkeypatch, FakeStore(enabled(url))))) as client:
            _, data = await call(client, 'spans', {'trace_id': TRACE, 'span_ids': ['a2', 'A3', 'ffff']})
            _, errors = await call(client, 'spans', {'trace_id': TRACE, 'errors': True})
            both_status, both = await call(client, 'spans', {'trace_id': TRACE, 'errors': True, 'span_ids': ['a1']})
            none_status, _ = await call(client, 'spans', {'trace_id': TRACE})
            many_status, _ = await call(client, 'spans', {'trace_id': TRACE, 'span_ids': ['a'] * 21})
    result = data['data']
    assert [span['span_id'] for span in result['spans']] == ['00000000000000a2', '00000000000000a3']
    assert result['missing'] == ['000000000000ffff'] and result['truncated'] is False
    client_span, server_span = result['spans']
    assert client_span['attributes']['url.full'] == 'http://inventory/retry?token=***'
    assert client_span['attributes']['http.request.header.authorization'] == '***'
    assert client_span['parent_id'] == '00000000000000a1' and client_span['kind'] == 'client'
    assert client_span['cluster'] == {'value': 'prod', 'env': 'prod'} and client_span['start'].endswith('+08:00')
    assert server_span['attributes']['big'].startswith('v' * 2000) and '原长 3000 字' in server_span['attributes']['big']
    stack = server_span['events'][0]['attributes']['exception.stacktrace']
    assert stack.startswith('s' * 3000) and stack.endswith('s' * 800) and '中间省略 1200 字' in stack
    assert server_span['resource']['service.name'] == 'inventory' and server_span['links'] == []
    assert [span['span_id'] for span in errors['data']['spans']] == ['00000000000000a1']
    assert both_status == 400 and '二选一' in both['message']
    assert none_status == 400 and many_status == 400


# ---------------------------------------------------------------------------
# 字段
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_fields_observed_keys(monkeypatch):
    seen = []

    def traces(query):
        seen.append(query)
        return sample_trace().doc() if query['query.filter'].count('svc-x') == 0 else None

    fake = FakeJaeger()
    fake.traces = traces
    async with serve(fake) as url:
        store = FakeStore(enabled(url, custom_fields=[{'key': 'biz.order_no', 'label': '订单号'}]))
        async with TestClient(TestServer(make_app(monkeypatch, store))) as client:
            _, plain = await call(client, 'fields')
            _, observed = await call(client, 'fields', {'service': 'gateway'})
            _, nothing = await call(client, 'fields', {'service': 'svc-x'})
    assert plain['data'] == {'cluster': {'key': 'k8sname', 'values': ['prod', '蓬莱']},
                             'fields': tracing_config.merged({})['fields'],
                             'custom_fields': [{'key': 'biz.order_no', 'label': '订单号'}], 'clickhouse': False}
    data = observed['data']['observed']
    assert data['service'] == 'gateway' and data['traces'] == 1
    keys = {(item['key'], item['level']): item for item in data['keys']}
    assert keys[('k8sname', 'resource')]['examples'] == ['prod'] and 'types' not in keys[('k8sname', 'resource')]
    assert keys[('http.request.header.authorization', 'span')]['examples'] == ['***']
    assert keys[('exception.type', 'event')]['count'] == 1
    # 取样加了集群过滤，不用 query.serviceName
    flt = json.loads(seen[1]['query.filter'])
    assert flt['args'][1]['call']['op'] == 'or' and 'query.serviceName' not in seen[1]
    assert seen[1]['query.searchDepth'] == '2'
    assert nothing['data']['observed'] is None and '最近 15 分钟没有找到服务 svc-x' in nothing['data']['notes'][0]


# ---------------------------------------------------------------------------
# 管理接口
# ---------------------------------------------------------------------------
def admin():
    return Principal('root', 'admin', user_id=1)


@pytest.mark.asyncio
async def test_admin_config_round_trip(monkeypatch, audits):
    store = FakeStore()
    async with TestClient(TestServer(make_app(monkeypatch, store, admin()))) as client:
        empty = await (await client.get('/api/admin/tracing')).json()
        saved_resp = await client.put('/api/admin/tracing', json={
            'enabled': True, 'url': 'http://jaeger:16686/', 'cluster_key': 'k8sname',
            'headers': [{'name': 'Authorization', 'value': 'Bearer s3cret'}],
            'clickhouse': {'enabled': True, 'url': 'http://ch:8123', 'username': 'reader', 'password': 'ch-s3cret'}})
        saved = await saved_resp.json()
        toggled = await (await client.put('/api/admin/tracing', json={'enabled': False})).json()
        bad = await client.put('/api/admin/tracing', json={'timeout': 1})
        bad_data = await bad.json()
        not_json = await client.put('/api/admin/tracing', data='x', headers={'Content-Type': 'application/json'})
    data = empty['data']
    assert data['enabled'] is False and data['updated_by'] is None and data['clickhouse']['has_password'] is False
    assert data['field_defaults']['status_code'] == ['http.response.status_code', 'http.status_code']
    assert data['field_labels']['host'] == '域名 / 被调方地址' and data['field_levels']['exception_type'] == 'event'
    assert data['clusters'] == [{'env': 'prod', 'aliases': ['蓬莱']}, {'env': 'test', 'aliases': []}]
    assert 'password' in data['mask_builtin']
    assert saved_resp.status == 200 and saved['data']['url'] == 'http://jaeger:16686'
    assert saved['data']['headers'] == [{'name': 'Authorization', 'has_value': True}]
    assert saved['data']['clickhouse']['has_password'] is True and saved['data']['updated_by'] == 'root'
    assert saved['data']['updated_at'] == '2026-11-21T11:00:00'
    text = json.dumps(saved) + json.dumps(audits)
    assert 's3cret' not in text
    assert decrypt_secret(store.row['value']['clickhouse']['password']) == 'ch-s3cret'
    assert toggled['data']['enabled'] is False and toggled['data']['url'] == 'http://jaeger:16686'
    assert decrypt_secret(store.row['value']['headers'][0]['value']) == 'Bearer s3cret'
    assert bad.status == 400 and bad_data['code'] == 'invalid_request' and '5~120' in bad_data['message']
    assert not_json.status == 400
    first, second = audits
    assert first['action'] == 'tracing.update' and first['username'] == 'root'
    assert first['detail'] == {'changed': ['enabled', 'url', 'headers', 'cluster_key', 'clickhouse.enabled',
                                           'clickhouse.url', 'clickhouse.username', 'clickhouse.password']}
    assert second['detail'] == {'changed': ['enabled']}


@pytest.mark.asyncio
async def test_admin_test_jaeger(monkeypatch):
    fake = FakeJaeger()
    saved = enabled('http://old:1', headers=[{'name': 'X-Token', 'value': 'saved'}])
    async with serve(fake) as url:
        async with TestClient(TestServer(make_app(monkeypatch, FakeStore(saved), admin()))) as client:
            form = {'url': url, 'headers': [{'name': 'X-Token', 'value': ''}], 'enabled': True}
            ok = await (await client.post('/api/admin/tracing/test', json={'target': 'jaeger', 'config': form})).json()
            fake.capabilities = None
            old = await (await client.post('/api/admin/tracing/test', json={'target': 'jaeger', 'config': form})).json()
            down = await (await client.post('/api/admin/tracing/test', json={
                'target': 'jaeger', 'config': {'url': 'http://127.0.0.1:9'}})).json()
            bad = await client.post('/api/admin/tracing/test', json={'target': 'mysql'})
            no_url = await client.post('/api/admin/tracing/test', json={'target': 'jaeger', 'config': {'url': ''}})
    data = ok['data']
    assert data['ok'] is True and data['services_count'] == 3 and data['services'] == ['gateway', 'svc-a', 'svc-b']
    assert data['capabilities'] == CAPS and data['search_ok'] is True
    assert '支持结构化搜索（Jaeger 2.23 及以上）' in data['messages']
    assert any('运算符：and、or、not、eq' in message for message in data['messages'])
    assert old['data']['ok'] is True and old['data']['search_ok'] is False and old['data']['capabilities'] is None
    assert any('版本低于 2.23' in message for message in old['data']['messages'])
    assert down['data']['ok'] is False and '无法访问 Jaeger' in down['data']['error']
    assert bad.status == 400 and no_url.status == 400
    assert all(path in ('/api/v3/services', '/api/v3/capabilities') for path in fake.paths())


@pytest.mark.asyncio
async def test_admin_test_clickhouse(monkeypatch):
    def handler(sql, query):
        if 'version()' in sql:
            return compact(['version'], [['26.7.1.1']])
        if sql.startswith('DESCRIBE TABLE `jaeger`.`trace_search_dim`'):
            return compact(['name', 'type'], [['bucket', 'DateTime'], ['spans', 'UInt64']])
        if sql.startswith('DESCRIBE'):
            return compact(['name', 'type'], [[name, 'String'] for name in clickhouse.SEARCH_COLUMNS])
        return compact(['k8s_cluster', 'spans'], [['蓬莱', '780000'], ['unknown-cluster', '5']])

    fake = FakeClickHouse(handler)
    async with serve(fake, 'POST') as url:
        async with TestClient(TestServer(make_app(monkeypatch, FakeStore(), admin()))) as client:
            resp = await client.post('/api/admin/tracing/test', json={'target': 'clickhouse', 'config': {
                'clickhouse': {'enabled': True, 'url': url, 'password': 'pw'}}})
            data = (await resp.json())['data']
            no_url = await client.post('/api/admin/tracing/test', json={'target': 'clickhouse', 'config': {}})
    assert data['ok'] is False and data['version'] == '26.7.1.1'
    assert data['missing_columns'] == {'trace_search_dim': ['k8s_cluster', 'namespace', 'service_name', 'kind',
                                                            'operation', 'url_path']}
    assert data['cluster_values'] == [{'value': '蓬莱', 'spans': 780000, 'env': 'prod'},
                                      {'value': 'unknown-cluster', 'spans': 5, 'env': None}]
    assert any('对不上 KubeDoor 的集群名或别名：unknown-cluster' in message for message in data['messages'])
    assert fake.requests[-1]['query']['param_s'] == str(int((NOW - 600) * 1000))
    assert fake.requests[0]['headers']['X-ClickHouse-Key'] == 'pw'
    assert no_url.status == 400


@pytest.mark.asyncio
async def test_admin_analyze_json_and_sample(monkeypatch):
    builder = sample_trace()
    builder.span('b1', 'GET', parent='a1', kind='client', start=3, duration=1, cluster='蓬莱',
                 attributes={'http.method': 'GET', 'db.password': 'p'})
    fake = FakeJaeger()
    seen = []
    fake.traces = lambda query: seen.append(query) or builder.doc()
    async with serve(fake) as url:
        async with TestClient(TestServer(make_app(monkeypatch, FakeStore(), admin()))) as client:
            pasted = await (await client.post('/api/admin/tracing/analyze', json={
                'mode': 'json', 'trace_json': json.dumps(builder.doc())})).json()
            sample = await (await client.post('/api/admin/tracing/analyze', json={
                'mode': 'sample', 'service': 'gateway', 'config': {'url': url}})).json()
            bad = []
            for body in ({'mode': 'x'}, {'mode': 'json', 'trace_json': ''}, {'mode': 'json', 'trace_json': '{"a": 1}'},
                         {'mode': 'json', 'trace_json': '{}'}, {'mode': 'sample', 'service': 'a'},
                         {'mode': 'json', 'trace_json': ' ' * (5 * 1024 * 1024 + 1)}):
                resp = await client.post('/api/admin/tracing/analyze', json=body)
                bad.append((resp.status, await resp.json()))
    data = pasted['data']
    assert data['traces'] == 1 and data['spans'] == 4
    assert data['cluster_candidates'][0] == {
        'key': 'k8sname', 'values': [{'value': 'prod', 'env': 'prod'}, {'value': '蓬莱', 'env': 'prod'}],
        'matched_envs': ['prod']}
    assert data['suggestions']['cluster_key'] == 'k8sname'
    assert data['suggestions']['fields']['status_code'] == ['http.response.status_code']
    assert data['suggestions']['fields']['method'] == ['http.method'] and data['suggestions']['fields']['route'] == []
    keys = {(item['key'], item['level']): item for item in data['keys']}
    assert keys[('db.password', 'span')]['examples'] == ['***'] and keys[('http.method', 'span')]['types'] == ['string']
    assert keys[('http.response.status_code', 'span')]['types'] == ['int']
    assert keys[('exception.stacktrace', 'event')]['examples'][0].endswith('…')
    assert len(keys[('exception.stacktrace', 'event')]['examples'][0]) == 80
    assert sample['data']['traces'] == 1 and seen[0]['query.serviceName'] == 'gateway'
    assert seen[0]['query.searchDepth'] == '3' and 'query.filter' not in seen[0]  # 识别字段不加集群过滤
    assert [status for status, _ in bad] == [400] * 5 + [413]
    assert bad[-1][1]['message'] == 'Trace JSON 最大 5MB'


@pytest.mark.asyncio
async def test_too_large_bodies(monkeypatch):
    app = make_app(monkeypatch, FakeStore(), admin())
    app._client_max_size = 1024  # 其他接口受全局上限约束
    async with TestClient(TestServer(app)) as client:
        put = await client.put('/api/admin/tracing', json={'url': 'http://j:1', 'pad': 'x' * 5000})
        put_data = await put.json()
        # 识别字段自己按流读，不受全局上限约束；超过 6MB 返回 413
        small = await client.post('/api/admin/tracing/analyze', json={'mode': 'json', 'trace_json': 'x' * 5000})
        small_data = await small.json()
        huge = await client.post('/api/admin/tracing/analyze', data=b'{"mode": "json", "trace_json": "' + b'x' * (
            6 * 1024 * 1024) + b'"}', headers={'Content-Type': 'application/json'})
        huge_data = await huge.json()
    assert put.status == 413 and put_data['code'] == 'invalid_request' and '请求体太大' in put_data['message']
    assert small.status == 400 and '不是合法的 JSON' in small_data['message']
    assert huge.status == 413 and huge_data['message'] == 'Trace JSON 最大 5MB'


@pytest.mark.asyncio
async def test_analyze_large_paste_behind_auth_middleware(monkeypatch, audits):
    """master 全局 client_max_size 是 2MB：3MB 的 Trace JSON 经过真实的鉴权中间件也要能识别"""
    from auth import middleware as auth_middleware

    monkeypatch.setenv('AI_INTERNAL_TOKEN', 'secret-test-token')

    async def principal_by_name(username, via):
        return Principal('root', 'admin', user_id=1, via='internal')

    monkeypatch.setattr(auth_middleware.store, 'principal_by_name', principal_by_name)
    store = FakeStore()
    monkeypatch.setattr(tracing_api, 'store', store)
    monkeypatch.setattr(tracing_config, 'store', store)
    builder = TraceBuilder(TRACE)
    for i in range(2500):
        builder.span(f'{i + 1:x}', f'call-{i}', parent='1' if i else '', start=i, duration=1,
                     attributes={'db.statement': 'select ' + 'c' * 900})
    text = json.dumps(builder.doc())
    assert 2 * 1024 * 1024 < len(text) < 5 * 1024 * 1024
    app = web.Application(client_max_size=2 * 1024 * 1024, middlewares=[auth_middleware.create_middleware()])
    tracing_api.register_routes(app)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post('/api/admin/tracing/analyze', json={'mode': 'json', 'trace_json': text}, headers={
            'X-Kubedoor-Token': 'secret-test-token', 'X-User-Name': 'root'})
        data = await resp.json()
    assert resp.status == 200 and data['data']['spans'] == 2500
    assert audits == []  # 只读分析，不当写操作记 api.write


# ---------------------------------------------------------------------------
# 经过真实的鉴权中间件（policy.RULES）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_routes_behind_auth_middleware(monkeypatch, audits):
    from auth import middleware as auth_middleware

    monkeypatch.setenv('AI_INTERNAL_TOKEN', 'secret-test-token')
    users = {'reader': Principal('reader', 'read', user_id=2, via='internal'),
             'nobody': Principal('nobody', 'none', user_id=3, via='internal')}

    async def principal_by_name(username, via):
        return users.get(username)

    monkeypatch.setattr(auth_middleware.store, 'principal_by_name', principal_by_name)
    fake = FakeJaeger()
    fake.summaries = lambda query: [summary('a' * 32, NOW - 10)]
    async with serve(fake) as url:
        store = FakeStore(enabled(url))
        monkeypatch.setattr(tracing_api, 'store', store)
        monkeypatch.setattr(tracing_config, 'store', store)
        app = web.Application(middlewares=[auth_middleware.create_middleware()])
        tracing_api.register_routes(app)
        async with TestClient(TestServer(app)) as client:
            async def post(path, user, body=None, params=None):
                resp = await client.post(path, params=params, json=body or {}, headers={
                    'X-Kubedoor-Token': 'secret-test-token', 'X-User-Name': user})
                return resp.status, await resp.json()

            results = [
                await post('/api/ai/internal/tracing/search', 'reader', {'service': 'svc-a'}, {'env': 'prod'}),
                await post('/api/ai/internal/tracing/search', 'nobody', {'service': 'svc-a'}, {'env': 'prod'}),
                await post('/api/admin/tracing/test', 'reader', {'target': 'jaeger'}),
            ]
            browser = await client.post('/api/ai/internal/tracing/search', params={'env': 'prod'}, json={})
    assert [(status, data.get('code')) for status, data in results] == [
        (200, None), (403, 'permission_denied'), (403, 'admin_required')]
    assert results[0][1]['data']['count'] == 1 and results[0][1]['data']['source'] == 'jaeger'
    assert browser.status == 401
    # AI 查链路不记审计（AI 动作表已有记录）；被拒的管理接口由中间件按 api.denied 记
    assert [record['action'] for record in audits] == ['api.denied']
