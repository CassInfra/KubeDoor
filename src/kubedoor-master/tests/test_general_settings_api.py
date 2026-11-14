"""通用配置的接口（general_settings/api.py）：GET / PUT / 测试时序库 / 内部接口 / 启停。"""
import asyncio
import base64
import json
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import quote

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import general_settings
from agent_install import bundle
from auth.crypto import decrypt_secret
from auth.principal import SYSTEM, Principal
from general_settings import api as gs_api
from general_settings import fields, legacy, service
from test_general_settings import (  # noqa: F401
    KEY, PASSWORD, PROM_URL, UPDATED_AT, fresh_state, logs, storage, stored_value,
)

ADMIN = Principal('kubedoor', 'admin', user_id=1)
URL = '/api/admin/general-settings'
TEST_URL = '/api/admin/general-settings/test-metrics'
INTERNAL_URL = '/api/internal/settings'
DEFAULTS_JSON = json.loads(json.dumps(fields.DEFAULTS))


def make_app(principal=ADMIN):
    """返回 (app, who)：who['principal'] 可以在用例里换成别的账号"""
    who = {'principal': principal}

    @web.middleware
    async def inject(request, handler):
        request['principal'] = who['principal']
        return await handler(request)

    app = web.Application(middlewares=[inject])
    general_settings.register_routes(app)
    return app, who


@pytest.fixture
def audits(monkeypatch):
    records = []
    monkeypatch.setattr(gs_api.audit, 'record_bg',
                        lambda username, action, **kwargs: records.append({'username': username, 'action': action,
                                                                           **kwargs}))
    return records


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(gs_api, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    return now


def no_secret(*items):
    text = json.dumps(items, ensure_ascii=False, default=str)
    return PASSWORD not in text and 'S3cret' not in text and 'wpass' not in text


async def call(app, method, path, **kwargs):
    async with TestClient(TestServer(app)) as client:
        resp = await client.request(method, path, **kwargs)
        return resp, await resp.json()


# ---------------------------------------------------------------------------
# GET
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_get_without_saved_config(storage):
    storage()
    app, _ = make_app()
    resp, data = await call(app, 'GET', URL)
    assert resp.status == 200
    assert data == {'success': True, 'data': {
        'values': DEFAULTS_JSON, 'defaults': DEFAULTS_JSON, 'stored': [], 'problems': {},
        'recommended_agent_tag': bundle.BASE['agent_tag'], 'updated_by': None, 'updated_at': None}}


@pytest.mark.asyncio
async def test_get_masks_passwords_and_lists_stored_keys(storage):
    storage(stored_value(prom_url=PROM_URL, metrics_write_url='http://w:wpass@vm:8428/api/v1/write',
                         oss_url='http://oss:9000/b', prom_type='Prometheus', retention_events_days=30,
                         auth_idle_hours=0.0))
    app, _ = make_app()
    resp, data = await call(app, 'GET', URL)
    values = data['data']['values']
    assert values['prom_url'] == 'http://monit:******@vm.example.com:8428'
    assert values['metrics_write_url'] == 'http://w:******@vm:8428/api/v1/write'
    assert values['oss_url'] == 'http://oss:9000/b'
    assert values['prom_type'] == 'Prometheus' and values['retention_events_days'] == 30
    assert values['auth_idle_hours'] == 0 and values['prom_k8s_tag_key'] == 'origin_prometheus'
    assert set(values) == set(fields.FIELDS)
    assert data['data']['stored'] == ['prom_type', 'prom_url', 'metrics_write_url', 'oss_url',
                                      'retention_events_days', 'auth_idle_hours']
    assert data['data']['problems'] == {}
    assert data['data']['updated_by'] == 'kubedoor'
    assert data['data']['updated_at'] == UPDATED_AT.astimezone().isoformat()
    assert no_secret(data)


@pytest.mark.asyncio
async def test_get_reports_undecryptable_address(storage, monkeypatch):
    monkeypatch.setenv('AI_ENCRYPTION_KEY', '22' * 32)
    value = stored_value(oss_url='http://u:p@oss/b')
    monkeypatch.setenv('AI_ENCRYPTION_KEY', KEY)
    storage(value)
    app, _ = make_app()
    _, data = await call(app, 'GET', URL)
    assert data['data']['problems'] == {'oss_url': '已保存的地址无法解密（加密密钥变了？），请重新填写'}
    assert data['data']['values']['oss_url'] == '' and data['data']['stored'] == ['oss_url']


@pytest.mark.asyncio
async def test_get_reports_database_error(storage):
    storage().error = RuntimeError('db down')
    app, _ = make_app()
    resp, data = await call(app, 'GET', URL)
    assert resp.status == 503 and data == {'success': False, 'code': 'db_unavailable',
                                           'message': '读取通用配置失败，请稍后重试',
                                           'error': '读取通用配置失败，请稍后重试'}


# ---------------------------------------------------------------------------
# PUT
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_put_merges_encrypts_and_takes_effect_immediately(storage, audits):
    fake = storage({**stored_value(prom_type='Prometheus', retention_events_days=30,
                                   kubedoor_exturl='https://kd.a.com'), '_legacy_imported': ['kubedoor_exturl']})
    app, _ = make_app()
    body = {'values': {'prom_url': PROM_URL + '/', 'retention_events_days': None, 'alert_dedup_window': '600',
                       'agent_auth_required': False, 'prom_type': '', 'auth_idle_hours': '0'}}
    async with TestClient(TestServer(app)) as client:
        resp = await client.put(URL, json=body)
        data = await resp.json()
        again = await (await client.get(URL)).json()
    assert resp.status == 200 and data['success'] is True and again == data
    saved = fake.row['value']
    assert set(saved) == {'prom_url', 'kubedoor_exturl', 'alert_dedup_window', 'agent_auth_required',
                          'auth_idle_hours', '_legacy_imported'}
    assert saved['_legacy_imported'] == ['kubedoor_exturl']  # 页面保存保留导入记录
    assert saved['prom_url'].startswith('v1:') and decrypt_secret(saved['prom_url']) == PROM_URL
    assert saved['alert_dedup_window'] == 600 and saved['agent_auth_required'] is False
    assert saved['auth_idle_hours'] == 0.0 and fake.row['updated_by'] == 'kubedoor'
    assert data['data']['values']['prom_url'] == 'http://monit:******@vm.example.com:8428'
    assert data['data']['values']['prom_type'] == 'Victoria-Metrics-Single'
    assert data['data']['stored'] == ['prom_url', 'kubedoor_exturl', 'auth_idle_hours', 'agent_auth_required',
                                      'alert_dedup_window']
    # 保存后马上生效,不等后台刷新
    assert general_settings.value('prom_url') == PROM_URL and general_settings.value('alert_dedup_window') == 600
    assert general_settings.value('retention_events_days') == 90 and general_settings.value('auth_idle_hours') == 0.0
    [record] = audits
    assert record['action'] == 'general_settings.update' and record['username'] == 'kubedoor'
    assert record['detail'] == {'changed': ['prom_type', 'prom_url', 'retention_events_days', 'auth_idle_hours',
                                            'agent_auth_required', 'alert_dedup_window']}
    assert no_secret(saved, data, audits)


@pytest.mark.asyncio
async def test_put_mask_keeps_saved_password(storage, audits):
    fake = storage(stored_value(prom_url=PROM_URL))
    before = fake.row['value']['prom_url']
    app, _ = make_app()
    async with TestClient(TestServer(app)) as client:
        same = await client.put(URL, json={'values': {'prom_url': 'http://monit:******@vm.example.com:8428'}})
        moved = await client.put(URL, json={'values': {
            'prom_url': 'http://monit:******@vm.example.com:8428/select/0/prometheus'}})
    assert same.status == moved.status == 200
    assert [record['detail'] for record in audits] == [{'changed': []}, {'changed': ['prom_url']}]
    assert fake.writes[0][0]['prom_url'] == before  # 没变:原密文保留
    assert decrypt_secret(fake.row['value']['prom_url']) == PROM_URL + '/select/0/prometheus'
    assert general_settings.value('prom_url') == PROM_URL + '/select/0/prometheus'


@pytest.mark.asyncio
@pytest.mark.parametrize('saved', [
    None,
    {'prom_url': 'http://monit@vm.example.com:8428'},
    {'prom_url': 'http://other:S3cret-P%40ss@vm.example.com:8428'},
    {'prom_url': 'http://monit:S3cret-P%40ss@vm2.example.com:8428'},
])
async def test_put_mask_without_matching_saved_password_is_rejected(storage, audits, saved):
    fake = storage(stored_value(**saved) if saved else None)
    app, _ = make_app()
    resp, data = await call(app, 'PUT', URL, json={'values': {'prom_url': 'http://monit:******@vm.example.com:8428'}})
    assert resp.status == 422 and data['code'] == 'invalid_config'
    assert data['message'].startswith('请重新填写查询地址的密码')
    assert fake.writes == [] and audits == []


@pytest.mark.asyncio
@pytest.mark.parametrize('values, message', [
    ({'PROM_URL': 'http://vm:8428'}, '不认识的配置项：PROM_URL'),
    ({'_legacy_imported': []}, '不认识的配置项：_legacy_imported'),  # 导入记录只能由迁移写
    ({'prom_url': 'http://vm:8428', 'zzz': 1, 'aaa': 2}, '不认识的配置项：aaa、zzz'),
    ({'retention_events_days': 5000}, 'K8S 事件保留天数必须是 0 到 3650 之间的整数'),
    ({'agent_auth_required': 'false'}, 'Agent 连接必须带账号只能是 true 或 false'),
    ({'kubedoor_exturl': 'https://kd.example.com/x'}, 'KubeDoor 访问地址只填写协议、主机和端口'),
    ({'prom_url': 'vm:8428'}, '查询地址必须是完整的 http:// 或 https:// 地址'),
    ({'auth_cookie_secure': 'yes'}, 'Cookie Secure 只能选 auto、true、false'),
    ({'prom_k8s_tag_key': 'k8s-name'}, '集群指标标签 key 只能包含字母、数字和下划线'),
    ({'auth_session_hours': 0}, '登录会话有效期（小时）必须大于 0'),
    # 有一项不对就整批不保存
    ({'prom_type': 'Prometheus', 'oss_url': 'http://oss/b?x=1'}, '对象存储上传地址不能包含查询参数'),
])
async def test_put_rejects_invalid_values(storage, audits, values, message):
    fake = storage(stored_value(prom_type='Victoria-Metrics-Cluster'))
    await general_settings.refresh()
    app, _ = make_app()
    resp, data = await call(app, 'PUT', URL, json={'values': values})
    assert resp.status == 422 and data['success'] is False and data['code'] == 'invalid_config'
    assert data['message'].startswith(message), data['message'] and data['error'] == data['message']
    assert fake.writes == [] and audits == []
    assert general_settings.value('prom_type') == 'Victoria-Metrics-Cluster'


@pytest.mark.asyncio
@pytest.mark.parametrize('raw, message', [
    ('not json', '请求体必须是 JSON 对象'),
    ('[1, 2]', '请求体必须是 JSON 对象'),
    ('{}', '请求体里的 values 必须是对象'),
    ('{"values": [1]}', '请求体里的 values 必须是对象'),
    ('{"values": null}', '请求体里的 values 必须是对象'),
])
async def test_put_requires_values_object(storage, audits, raw, message):
    fake = storage()
    app, _ = make_app()
    resp, data = await call(app, 'PUT', URL, data=raw, headers={'Content-Type': 'application/json'})
    assert resp.status == 400 and data['message'].startswith(message) and fake.writes == [] and audits == []


@pytest.mark.asyncio
async def test_put_value_equal_to_default_is_removed(storage, audits):
    fake = storage({'image_repo': 'harbor.local/kd', 'auth_session_hours': 24.0})
    app, _ = make_app()
    resp, data = await call(app, 'PUT', URL, json={'values': {
        'image_repo': bundle.BASE['image_repo'] + '/', 'auth_session_hours': 12}})
    assert resp.status == 200 and fake.row['value'] == {}
    assert data['data']['stored'] == [] and audits[0]['detail'] == {'changed': ['image_repo', 'auth_session_hours']}
    assert general_settings.value('image_repo') == bundle.BASE['image_repo']


@pytest.mark.asyncio
async def test_put_accepts_numbers_as_strings(storage, audits):
    storage()
    app, _ = make_app()
    resp, data = await call(app, 'PUT', URL, json={'values': {
        'auth_session_hours': '1.5', 'auth_lock_minutes': 30.0, 'retention_audit_days': ' 0 '}})
    assert resp.status == 200
    assert general_settings.value('auth_session_hours') == 1.5
    assert general_settings.value('auth_lock_minutes') == 30
    assert type(general_settings.value('auth_lock_minutes')) is int
    assert general_settings.value('retention_audit_days') == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['error', 'write_error'])
async def test_put_reports_database_errors(storage, audits, failure):
    fake = storage(stored_value(prom_type='Prometheus'))
    await general_settings.refresh()
    setattr(fake, failure, RuntimeError('db down'))
    app, _ = make_app()
    resp, data = await call(app, 'PUT', URL, json={'values': {'prom_type': 'Victoria-Metrics-Cluster'}})
    assert resp.status == 503 and data['code'] == 'db_unavailable'
    assert audits == [] and general_settings.value('prom_type') == 'Prometheus'  # 没保存成功就不生效


# ---------------------------------------------------------------------------
# 测试时序库
# ---------------------------------------------------------------------------
class FakeVM:
    """假的时序库：检查 Basic 认证，vector(1) 返回 1，标签值接口返回 clusters"""

    def __init__(self, user='monit', password=PASSWORD, clusters=('a', 'b', 'c'), tag_key='origin_prometheus'):
        self.user, self.password, self.clusters, self.tag_key = user, password, list(clusters), tag_key
        self.status = None  # 设了就所有请求都返回这个状态码
        self.body = None  # 设了就 vector(1) 返回这段原文
        self.requests = []

    def app(self):
        app = web.Application()
        app.router.add_get('/{tail:.*}', self.handle)
        return app

    async def handle(self, request):
        self.requests.append({'path': request.path, 'query': dict(request.query),
                              'auth': request.headers.get('Authorization')})
        if self.status:
            headers = {'Location': 'http://elsewhere/'} if 300 <= self.status < 400 else None
            return web.Response(status=self.status, headers=headers)
        if self.user and request.headers.get('Authorization') != basic(self.user, self.password):
            return web.Response(status=401)
        if request.path.endswith('/api/v1/query'):
            if self.body is not None:
                return web.Response(body=self.body, content_type='text/html')
            return web.json_response({'status': 'success', 'data': {
                'resultType': 'vector', 'result': [{'metric': {}, 'value': [1763172000, '1']}]}})
        if request.path.endswith(f'/api/v1/label/{self.tag_key}/values'):
            return web.json_response({'status': 'success', 'data': self.clusters})
        return web.Response(status=404)


def basic(user, password):
    """标准的 Basic 认证头(和 requests 一样按 latin-1 编码)"""
    return 'Basic ' + base64.b64encode(f'{user}:{password}'.encode('latin-1')).decode('ascii')


def vm_url(server, path='', user='monit', password=PASSWORD):
    auth = f'{user}:{quote(password, safe="")}@' if user else ''
    return f'http://{auth}{server.host}:{server.port}{path}'


@pytest.mark.asyncio
async def test_metrics_test_uses_saved_values_when_omitted(storage, clock):
    vm = FakeVM()
    async with TestServer(vm.app()) as server:
        storage(stored_value(prom_url=vm_url(server, '/select/0/prometheus')))
        app, _ = make_app()
        resp, data = await call(app, 'POST', TEST_URL, json={})
    assert resp.status == 200
    assert data == {'success': True, 'data': {'ok': True, 'level': 'success',
                                              'message': '连接正常，标签 origin_prometheus 下有 3 个集群：a、b、c'}}
    query, labels = vm.requests
    assert query['path'] == '/select/0/prometheus/api/v1/query' and query['query'] == {'query': 'vector(1)'}
    assert labels['path'] == '/select/0/prometheus/api/v1/label/origin_prometheus/values'
    assert labels['query']['limit'] == '1000' and labels['query']['start'].isdigit()
    assert query['auth'] == basic('monit', PASSWORD)  # 账号密码拆出来用 Basic 认证


@pytest.mark.asyncio
async def test_metrics_test_replaces_mask_and_uses_submitted_tag_key(storage, clock):
    vm = FakeVM(clusters=[f'c{index}' for index in range(7)], tag_key='cluster')
    async with TestServer(vm.app()) as server:
        storage(stored_value(prom_url=vm_url(server)))
        app, _ = make_app()
        resp, data = await call(app, 'POST', TEST_URL, json={
            'prom_url': vm_url(server, '/', password='******'), 'prom_k8s_tag_key': ' cluster '})
    assert data['data'] == {'ok': True, 'level': 'success',
                            'message': '连接正常，标签 cluster 下有 7 个集群：c0、c1、c2、c3、c4 等'}
    assert vm.requests[0]['auth'] == basic('monit', PASSWORD)


@pytest.mark.asyncio
async def test_metrics_test_without_label_data_is_a_warning(storage, clock):
    vm = FakeVM(user='', clusters=[])
    async with TestServer(vm.app()) as server:
        storage()
        app, _ = make_app()
        _, data = await call(app, 'POST', TEST_URL, json={'prom_url': vm_url(server, user=''),
                                                           'prom_k8s_tag_key': ''})  # 空串按默认值
    assert data['data']['ok'] is True and data['data']['level'] == 'warning'
    assert data['data']['message'].startswith('连接正常，但最近一天没有查到带 origin_prometheus 标签的数据')
    assert vm.requests[0]['auth'] is None


@pytest.mark.asyncio
@pytest.mark.parametrize('setup, message', [
    (lambda vm: setattr(vm, 'password', 'other'), '时序库认证失败（HTTP 401），请检查账号、密码和查询权限'),
    (lambda vm: setattr(vm, 'status', 302), '时序库返回了重定向（HTTP 302），请直接填写最终的查询地址'),
    (lambda vm: setattr(vm, 'status', 404), '时序库查询接口返回 HTTP 404，请检查查询地址'),
    (lambda vm: setattr(vm, 'body', b'<html>login</html>'), '时序库没有返回有效的 Prometheus 查询结果'),
    (lambda vm: setattr(vm, 'body', b'{"status": "success", "data": {"resultType": "vector", "result": []}}'),
     '时序库查询验证失败：测试表达式 vector(1) 没有返回预期结果'),
    (lambda vm: setattr(vm, 'body', b'{"status": "success", "data": "x" }' + b' ' * 1_000_001),
     '时序库返回的数据过大，请确认填写的是查询地址'),
    (lambda vm: setattr(vm, 'clusters', [1, 2]), '时序库标签值接口返回的格式不正确，请检查查询地址'),
])
async def test_metrics_test_failures_are_reported_without_secrets(storage, clock, logs, setup, message):
    vm = FakeVM()
    setup(vm)
    async with TestServer(vm.app()) as server:
        storage(stored_value(prom_url=vm_url(server)))
        app, _ = make_app()
        resp, data = await call(app, 'POST', TEST_URL, json={})
    assert resp.status == 200 and data['success'] is True
    assert data['data']['ok'] is False and data['data']['level'] == 'error'
    assert data['data']['message'].startswith(message), data['data']['message']
    assert len(vm.requests) <= 2 and no_secret(data, logs)


@pytest.mark.asyncio
async def test_metrics_test_reports_unreachable_server(storage, clock):
    with socket.socket() as sock:  # 拿一个没人监听的端口
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    storage()
    app, _ = make_app()
    _, data = await call(app, 'POST', TEST_URL, json={'prom_url': f'http://monit:{quote(PASSWORD)}@127.0.0.1:{port}'})
    assert data['data']['ok'] is False
    assert data['data']['message'] == '连接时序库失败或超时（12 秒），请检查地址和 master 到时序库的网络'


@pytest.mark.asyncio
@pytest.mark.parametrize('body, saved, message', [
    ({}, None, '还没有填写时序库查询地址'),
    ({'prom_url': '  '}, {'prom_url': PROM_URL}, '还没有填写时序库查询地址'),  # 页面上清空了
    ({'prom_url': 'vm:8428'}, None, '查询地址必须是完整的 http:// 或 https:// 地址'),
    ({'prom_url': 'http://monit:******@evil.example.com:8428'}, {'prom_url': PROM_URL}, '请重新填写查询地址的密码'),
    ({'prom_url': 'http://vm:8428', 'prom_k8s_tag_key': '1x'}, None, '集群指标标签 key 只能包含'),
])
async def test_metrics_test_rejects_invalid_input(storage, clock, body, saved, message):
    storage(stored_value(**saved) if saved else None)
    app, _ = make_app()
    resp, data = await call(app, 'POST', TEST_URL, json=body)
    assert resp.status == 422 and data['code'] == 'invalid_config' and data['message'].startswith(message)
    assert gs_api._last_test == {}  # 参数不对不占测试次数


@pytest.mark.asyncio
async def test_metrics_test_is_limited_to_once_per_3_seconds_per_account(storage, clock, monkeypatch):
    check = AsyncMock(return_value={'ok': True, 'level': 'success', 'message': 'ok'})
    monkeypatch.setattr(gs_api.probe, 'check_metrics', check)
    storage(stored_value(prom_url=PROM_URL))
    app, who = make_app()
    async with TestClient(TestServer(app)) as client:
        assert (await client.post(TEST_URL, json={'prom_url': 'bad'})).status == 422  # 不占次数
        first = await client.post(TEST_URL, json={})
        clock[0] += 1
        limited = await client.post(TEST_URL, json={})
        limited_data = await limited.json()
        who['principal'] = Principal('root', 'admin', user_id=2)  # 别的账号不受影响
        other = await client.post(TEST_URL, json={})
        who['principal'] = ADMIN
        clock[0] += 2
        after = await client.post(TEST_URL, json={})
        bad_body = await client.post(TEST_URL, data='x', headers={'Content-Type': 'application/json'})
    assert first.status == other.status == after.status == 200 and bad_body.status == 400
    assert limited.status == 429 and limited.headers['Retry-After'] == '2'
    assert limited_data['code'] == 'too_many_requests' and '2 秒后再试' in limited_data['message']
    assert check.await_count == 3 and check.await_args.args == (PROM_URL, 'origin_prometheus')


@pytest.mark.asyncio
async def test_metrics_test_reports_database_error(storage, clock):
    storage().error = RuntimeError('db down')
    app, _ = make_app()
    resp, data = await call(app, 'POST', TEST_URL, json={'prom_url': 'http://vm:8428'})
    assert resp.status == 503 and data['code'] == 'db_unavailable'


def test_rate_limit_table_is_pruned(clock, monkeypatch):
    monkeypatch.setattr(gs_api, '_last_test', {f'user{index}': 0.0 for index in range(1001)})
    assert gs_api._test_wait('kubedoor') == 0
    assert gs_api._last_test == {'kubedoor': 1000.0}


# ---------------------------------------------------------------------------
# 内部接口
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_internal_returns_only_password_free_items(storage):
    storage(stored_value(prom_url=PROM_URL, prom_k8s_tag_key='cluster', kubedoor_exturl='https://kd.example.com',
                         silence_cache_ttl=30, oss_url='http://u:wpass@oss/b'))
    app, _ = make_app(SYSTEM)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(INTERNAL_URL)  # 快照还没加载:先从库里加载
        data = await resp.json()
    assert resp.status == 200 and resp.headers['Cache-Control'] == 'no-store'
    assert data == {'success': True, 'data': {'prom_k8s_tag_key': 'cluster', 'kubedoor_exturl': 'https://kd.example.com',
                                              'alertmanager_exturl': '', 'silence_cache_ttl': 30,
                                              'retention_ai_checkpoint_days': 7}}
    assert no_secret(data)


@pytest.mark.asyncio
async def test_internal_is_unavailable_until_loaded(storage):
    fake = storage(stored_value(silence_cache_ttl=30))
    fake.error = RuntimeError('db down')
    app, _ = make_app(SYSTEM)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(INTERNAL_URL)
        data = await resp.json()
        fake.error = None
        ok = await client.get(INTERNAL_URL)
        ok_data = await ok.json()
        fake.error = RuntimeError('db down again')  # 加载过以后读内存快照,不再查库
        cached = await client.get(INTERNAL_URL)
    assert resp.status == 503 and data['code'] == 'not_ready'
    assert ok.status == cached.status == 200 and ok_data['data']['silence_cache_ttl'] == 30


@pytest.mark.asyncio
@pytest.mark.parametrize('principal', [
    Principal('alice', 'admin', user_id=7, via='internal'),  # AI 服务代表某个用户
    ADMIN,  # 浏览器会话
])
async def test_internal_only_for_service_identity(storage, principal):
    storage()
    app, _ = make_app(principal)
    resp, data = await call(app, 'GET', INTERNAL_URL)
    assert resp.status == 403 and data['code'] == 'forbidden'


@pytest.mark.asyncio
async def test_internal_through_auth_middleware(storage, monkeypatch):
    """经过真实的认证中间件：没带或带错内部 token 拒绝，带对的（不带用户名）才能取"""
    from auth.middleware import create_middleware

    monkeypatch.setenv('AI_INTERNAL_TOKEN', 'internal-secret')
    storage(stored_value(prom_k8s_tag_key='cluster'))
    app = web.Application(middlewares=[create_middleware()])
    general_settings.register_routes(app)
    async with TestClient(TestServer(app)) as client:
        anonymous = await client.get(INTERNAL_URL)
        wrong = await client.get(INTERNAL_URL, headers={'X-Kubedoor-Token': 'wrong'})
        ok = await client.get(INTERNAL_URL, headers={'X-Kubedoor-Token': 'internal-secret'})
        data = await ok.json()
    assert anonymous.status == 401 and wrong.status == 401
    assert ok.status == 200 and data['data']['prom_k8s_tag_key'] == 'cluster'


# ---------------------------------------------------------------------------
# 启停
# ---------------------------------------------------------------------------
LEGACY_NAMES = ('PROM_TYPE', 'PROM_URL', 'METRICS_WRITE_URL', 'PROM_K8S_TAG_KEY', 'KUBEDOOR_EXTURL',
                'ALERTMANAGER_EXTURL', 'OSS_URL', 'AGENT_INSTALL_DEFAULTS', 'NODE_SHELL_IMAGE', 'MSG_TYPE',
                'MSG_TOKEN', 'DEFAULT_AT') + tuple(spec.env for spec in fields.FIELDS.values())


@pytest.fixture
def clean_env(monkeypatch):
    for name in LEGACY_NAMES:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.mark.asyncio
async def test_startup_imports_legacy_config_loads_snapshot_and_cleanup_stops_refresh(storage, clean_env):
    fake = storage()
    clean_env.setenv('PROM_URL', PROM_URL)
    clean_env.setenv('RETENTION_EVENTS_DAYS', '30')
    clean_env.setenv('RETENTION_ALERTS_DAYS', '365')  # 等于默认值:不写
    app = web.Application()
    await general_settings.on_startup(app)
    task = app[gs_api.REFRESH_TASK]
    try:
        assert general_settings.is_loaded() and not task.done()
        assert general_settings.value('prom_url') == PROM_URL and general_settings.value('retention_events_days') == 30
        [(value, author)] = fake.writes
        assert author == 'legacy-import'
        assert set(value) == {'prom_url', 'retention_events_days', '_legacy_imported'}
    finally:
        await general_settings.on_cleanup(app)
    assert task.cancelled()
    await general_settings.on_cleanup(web.Application())  # 没启动过也不报错


@pytest.mark.asyncio
async def test_startup_survives_database_failure(storage, clean_env, logs):
    storage().error = RuntimeError('db down')
    clean_env.setenv('PROM_URL', PROM_URL)
    app = web.Application()
    await general_settings.on_startup(app)
    try:
        assert not general_settings.is_loaded() and general_settings.value('prom_url') == ''
        assert any('导入旧配置失败' in line and 'general_settings migrate' in line for line in logs)
    finally:
        await general_settings.on_cleanup(app)
    assert no_secret(logs)


@pytest.mark.asyncio
async def test_startup_import_is_serialized_with_page_saves(storage, clean_env, monkeypatch, audits):
    """启动导入和页面保存用同一把锁:谁先谁后都不会覆盖对方"""
    fake = storage()
    clean_env.setenv('RETENTION_EVENTS_DAYS', '30')
    gate = asyncio.Event()
    real_update = fake.update

    async def slow_update(change, updated_by):
        if updated_by == 'legacy-import':
            await gate.wait()
        return await real_update(change, updated_by)

    monkeypatch.setattr(fake, 'update', slow_update)
    importing = asyncio.create_task(legacy.import_legacy())
    await asyncio.sleep(0)
    saving = asyncio.create_task(service.save({'prom_type': 'Prometheus'}, 'kubedoor'))
    await asyncio.sleep(0.01)
    assert not saving.done()  # 在等导入
    gate.set()
    await asyncio.gather(importing, saving)
    assert fake.row['value'] == {'retention_events_days': 30, 'prom_type': 'Prometheus',
                                 '_legacy_imported': ['retention_events_days']}
