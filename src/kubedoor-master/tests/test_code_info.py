"""获取代码(func_manager/code_info.py):配置校验、接口约定检查、调用外部接口、读 Deployment 镜像、handler。"""
import asyncio
import json
import re
from datetime import datetime

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from auth.crypto import decrypt_secret
from auth.principal import Principal
from func_manager import code_info

PAYLOAD = {'k8s': 'prod', 'namespace': 'vip', 'deployment': 'user-service',
           'image': 'registry.example.com/team/user-service:release-E14-1-b9342-9f9a76c5'}
COMMIT = '9f9a76c5f372e60a247af4b60e2ff131323d3231'
LINKS = {
    'web_url': f'https://gitlab.example.com/vip/user-service/tree/{COMMIT}',
    'download_url': f'https://code.example.com/archive/164/{COMMIT}/user-service-{COMMIT}.tar.gz?expires=1793543448&sig=ac17',
    'commits_url': f'https://code.example.com/commits/164/{COMMIT}?expires=1793543448&sig=ac17',
}
DEPLOYMENT_YAML = '''
apiVersion: apps/v1
kind: Deployment
metadata:
  name: user-service
  namespace: vip
spec:
  template:
    spec:
      containers:
      - name: user-service
        image: registry.example.com/team/user-service:release-E14-1-b9342-9f9a76c5
      - name: sidecar
        image: registry.example.com/infra/sidecar:1.0
'''


@pytest.fixture(autouse=True)
def encryption_key(monkeypatch):
    monkeypatch.setenv('AI_ENCRYPTION_KEY', '11' * 32)


@pytest.fixture
def audits(monkeypatch):
    records = []
    monkeypatch.setattr(code_info.audit, 'record_bg',
                        lambda username, action, **kwargs: records.append({'username': username, 'action': action, **kwargs}))
    return records


class FakeStore:
    """auth_settings 里只有 code_info 这一行"""

    def __init__(self, value=None):
        self.row = None if value is None else {
            'value': value, 'updated_by': 'admin', 'updated_at': datetime(2026, 10, 31, 10, 0)}

    async def get_setting(self, key, default=None):
        assert key == code_info.SETTING_KEY
        return self.row['value'] if self.row else default

    async def get_setting_row(self, key):
        return self.row

    async def put_setting_row(self, key, value, updated_by):
        self.row = {'value': json.loads(json.dumps(value)), 'updated_by': updated_by,
                    'updated_at': datetime(2026, 10, 31, 11, 0)}
        return self.row


def make_app(monkeypatch, store, call_agent, principal=None):
    monkeypatch.setattr(code_info, 'store', store)
    monkeypatch.setattr(code_info, '_call_agent', None)  # register_routes 会改,用例结束后恢复
    principal = principal or Principal('alice', 'read', user_id=7)

    @web.middleware
    async def inject(request, handler):
        request['principal'] = principal
        return await handler(request)

    app = web.Application(middlewares=[inject])
    code_info.register_routes(app, call_agent)
    return app


def upstream_app(handler, path='/code'):
    app = web.Application()
    app.router.add_post(path, handler)
    return app


async def agent_ok(env, path, query=None, timeout=30):
    return {'success': True, 'data': DEPLOYMENT_YAML}


async def agent_unused(*args, **kwargs):
    raise AssertionError('不应该访问 agent')


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
def test_normalize_config_validates_url_timeout_and_enabled():
    cfg = code_info.normalize_config(
        {'enabled': True, 'url': ' https://code.example.com/api?team=ops ', 'timeout': '15', 'tls_verify': False})
    assert cfg == {'enabled': True, 'url': 'https://code.example.com/api?team=ops', 'tls_verify': False,
                   'timeout': 15, 'headers': []}
    # 停用时可以不填地址
    assert code_info.normalize_config({'enabled': False, 'url': ''})['url'] == ''


@pytest.mark.parametrize('raw, error', [
    ({'enabled': True, 'url': ''}, '启用时必须填写接口地址'),
    ({'url': 'ftp://code.example.com/api'}, 'http:// 或 https://'),
    ({'url': 'javascript:alert(1)'}, 'http:// 或 https://'),
    ({'url': 'https://code.example.com/a b'}, 'http:// 或 https://'),
    ({'url': 'https://'}, 'http:// 或 https://'),
    ({'url': 'http://[::1/api'}, 'http:// 或 https://'),
    ({'timeout': 0}, '1~60'),
    ({'timeout': 61}, '1~60'),
    ({'timeout': 'abc'}, '整数'),
    ('not-a-dict', '格式不正确'),
])
def test_normalize_config_rejects_invalid_values(raw, error):
    with pytest.raises(ValueError, match=re.escape(error)):
        code_info.normalize_config(raw)


def test_header_values_are_encrypted_and_kept_when_left_blank():
    first = code_info.normalize_config({'url': 'https://c.example.com', 'headers': [
        {'name': 'Authorization', 'value': ' Bearer abc '}, {'name': 'X-Team', 'value': 'ops'}]})
    stored = first['headers'][0]['value']
    assert stored.startswith('v1:') and 'abc' not in stored
    assert decrypt_secret(stored) == 'Bearer abc'

    # 值留空 = 沿用已保存的(名称不区分大小写);列表里没有的删除;没传的字段沿用
    second = code_info.normalize_config(
        {'headers': [{'name': 'authorization', 'value': ''}, {'name': 'X-New', 'value': 'n'}]}, first)
    assert [item['name'] for item in second['headers']] == ['authorization', 'X-New']
    assert second['headers'][0]['value'] == stored
    assert second['url'] == 'https://c.example.com'
    # 新加的请求头必须填值
    with pytest.raises(ValueError, match='请填写请求头 X-Other 的值'):
        code_info.normalize_config({'headers': [{'name': 'X-Other', 'value': None}]}, first)
    # 不传 headers:保持原样
    assert code_info.normalize_config({'timeout': 5}, first)['headers'] == first['headers']


@pytest.mark.parametrize('headers, error', [
    ([{'name': 'Content-Type', 'value': 'text/plain'}], '由系统设置'),
    ([{'name': 'host', 'value': 'x'}], '由系统设置'),
    ([{'name': 'Bad Name', 'value': 'x'}], '名称不合法'),
    ([{'name': 'X-A:', 'value': 'x'}], '名称不合法'),
    ([{'name': '', 'value': 'x'}], '名称不合法'),
    ([{'name': 'X-A', 'value': '1'}, {'name': 'x-a', 'value': '2'}], '重复'),
    ([{'name': 'X-A', 'value': 'a\r\nX-Injected: 1'}], '不能换行'),
    ([{'name': f'X-{i}', 'value': 'v'} for i in range(11)], '最多 10 个'),
    ('Authorization: x', '格式不正确'),
    (['Authorization'], '格式不正确'),
])
def test_invalid_headers_are_rejected(headers, error):
    with pytest.raises(ValueError, match=error):
        code_info.normalize_config({'url': 'https://c.example.com', 'headers': headers})


def test_public_config_only_shows_header_names():
    cfg = code_info.normalize_config(
        {'url': 'https://c.example.com', 'headers': [{'name': 'Authorization', 'value': 'Bearer secret-token'}]})
    public = code_info.public_config(cfg)
    assert public['headers'] == [{'name': 'Authorization', 'has_value': True}]
    text = json.dumps(public)
    assert 'secret-token' not in text and cfg['headers'][0]['value'] not in text
    assert code_info.public_config(None) == {'enabled': False, 'url': '', 'headers': [], 'timeout': 10,
                                             'tls_verify': True}


# ---------------------------------------------------------------------------
# 接口约定
# ---------------------------------------------------------------------------
def test_links_must_be_http_urls():
    links, invalid = code_info.parse_links({
        'web_url': f' {LINKS["web_url"]} ', 'download_url': 'javascript:alert(1)', 'commits_url': None})
    assert links == {'web_url': LINKS['web_url'], 'download_url': '', 'commits_url': ''}
    assert invalid == {'download_url': 'javascript:alert(1)'}
    assert code_info.parse_links({'web_url': '  '}) == ({'web_url': '', 'download_url': '', 'commits_url': ''}, {})
    for bad in ['JavaScript:alert(1)', 'data:text/html,x', '//git.example.com/x', 'https://git.example.com/a\nb',
                'http://', 123, ['https://x.example.com']]:
        assert code_info.parse_links({'web_url': bad})[1] == {'web_url': str(bad)}, bad


@pytest.mark.parametrize('status, text, location, error', [
    (302, '', 'https://sso.example.com/login', '接口返回了重定向(HTTP 302 → https://sso.example.com/login)'),
    (404, '{"message": "找不到 user-service 的代码仓库"}', '', '接口返回 HTTP 404:找不到 user-service 的代码仓库'),
    (403, 'token expired', '', '接口返回 HTTP 403:token expired'),
    (500, '<html><body>Internal Server Error</body></html>', '', '接口返回 HTTP 500'),  # HTML 错误页不带内容
    (200, 'ok', '', '接口返回的不是 JSON'),
    (200, json.dumps([LINKS['web_url']]), '', '接口返回的 JSON 不是对象'),
    (200, '{"web_url": "", "download_url": "ftp://x"}', '', '接口返回里没有可用的链接'),
    (200, json.dumps({'url': LINKS['web_url']}), '', '接口返回里没有可用的链接'),  # 字段名是 web_url
])
def test_check_response_errors(status, text, location, error):
    _, _, message = code_info.check_response(status, text, location)
    assert message.startswith(error)
    if status == 500:
        assert message == error


def test_check_response_accepts_partial_links():
    links, invalid, error = code_info.check_response(
        200, json.dumps({'web_url': LINKS['web_url'], 'commits_url': 'http://code.example.com/commits/1', 'extra': 1}))
    assert error == '' and invalid == {}
    assert links == {'web_url': LINKS['web_url'], 'download_url': '', 'commits_url': 'http://code.example.com/commits/1'}


# ---------------------------------------------------------------------------
# 调用接口(本地起一个真的 HTTP 服务)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_call_endpoint_posts_fixed_body_with_configured_headers():
    received = {}

    async def handler(request):
        received['body'] = await request.json()
        received['headers'] = dict(request.headers)
        return web.json_response(LINKS)

    async with TestServer(upstream_app(handler)) as server:
        cfg = code_info.normalize_config({'url': str(server.make_url('/code')), 'headers': [
            {'name': 'Authorization', 'value': 'Bearer t0ken'}]})
        result = await code_info.call_endpoint(cfg, PAYLOAD)
    assert result['ok'] is True and result['status'] == 200 and result['error'] == ''
    assert result['links'] == LINKS and result['invalid'] == {}
    assert json.loads(result['body']) == LINKS
    assert received['body'] == PAYLOAD
    assert received['headers']['Authorization'] == 'Bearer t0ken'
    assert received['headers']['Content-Type'].startswith('application/json')
    assert received['headers']['User-Agent'] == 'KubeDoor'


@pytest.mark.asyncio
async def test_call_endpoint_reports_upstream_problems(monkeypatch):
    async def slow(request):
        await asyncio.sleep(0.5)
        return web.json_response(LINKS)

    async def redirect(request):
        raise web.HTTPFound('https://sso.example.com/login')

    async def huge(request):
        return web.Response(text='x' * 2000)

    async def not_found(request):
        return web.json_response({'message': '没有这个服务的代码'}, status=404)

    async def drop(request):
        request.transport.close()
        await asyncio.sleep(0.1)
        return web.Response()

    app = web.Application()
    for name, handler in (('slow', slow), ('redirect', redirect), ('huge', huge), ('not_found', not_found),
                          ('drop', drop)):
        app.router.add_post(f'/{name}', handler)
    monkeypatch.setattr(code_info, 'MAX_RESPONSE', 1000)
    results = {}
    async with TestServer(app) as server:
        for name in ('slow', 'redirect', 'huge', 'not_found', 'drop'):
            cfg = {**code_info.DEFAULTS, 'url': str(server.make_url(f'/{name}')), 'timeout': 0.1 if name == 'slow' else 5}
            results[name] = await code_info.call_endpoint(cfg, PAYLOAD)
    assert results['slow']['error'] == '请求超时(0.1 秒内没有返回)'
    assert results['redirect']['error'].startswith('接口返回了重定向(HTTP 302 → https://sso.example.com/login)')
    assert results['huge']['error'].startswith('接口返回的内容超过')
    assert results['not_found']['error'] == '接口返回 HTTP 404:没有这个服务的代码'
    assert results['not_found']['status'] == 404
    assert results['drop']['error'].startswith('无法访问接口:')
    assert not any(result['ok'] for result in results.values())


@pytest.mark.asyncio
async def test_call_endpoint_reports_undecryptable_header(monkeypatch):
    cfg = code_info.normalize_config(
        {'url': 'https://c.example.com', 'headers': [{'name': 'Authorization', 'value': 'x'}]})
    monkeypatch.setenv('AI_ENCRYPTION_KEY', '22' * 32)  # 密钥换过
    result = await code_info.call_endpoint(cfg, PAYLOAD)
    assert '请求头 Authorization 的值无法解密' in result['error']
    assert result['ok'] is False and result['status'] is None


# ---------------------------------------------------------------------------
# 读 Deployment 的镜像
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_read_deployment_image_uses_first_container():
    calls = []

    async def call_agent(env, path, query=None, timeout=30):
        calls.append((env, path, query, timeout))
        return {'success': True, 'data': DEPLOYMENT_YAML}

    image = await code_info.read_deployment_image(call_agent, 'prod', 'vip', 'user-service')
    assert image == PAYLOAD['image']
    assert calls == [('prod', '/api/agent/res/content',
                      {'namespace': 'vip', 'resource_type': 'deployment', 'resource_name': 'user-service'}, 30)]


@pytest.mark.asyncio
@pytest.mark.parametrize('behavior, status, message', [
    (Exception('目标客户端 prod 不在线'), 503, '目标客户端 prod 不在线'),
    ({'error': "Deployment 'app' 在命名空间 'default' 中不存在"}, 502, '中不存在'),
    ({'success': False, 'error': 'agent 本地处理请求超时'}, 502, 'agent 本地处理请求超时'),
    ({'success': True, 'data': None}, 502, 'agent 返回的格式不正确'),
    ('not-a-dict', 502, 'agent 返回的格式不正确'),
    ({'success': True, 'data': 'spec: {template: {spec: {containers: []}}}'}, 502, '没有找到容器镜像'),
    ({'success': True, 'data': 'spec: [unclosed'}, 502, '无法解析'),
])
async def test_read_deployment_image_errors(behavior, status, message):
    async def call_agent(env, path, query=None, timeout=30):
        if isinstance(behavior, Exception):
            raise behavior
        return behavior

    with pytest.raises(code_info.CodeInfoError) as info:
        await code_info.read_deployment_image(call_agent, 'prod', 'default', 'app')
    assert info.value.status == status and message in info.value.message


# ---------------------------------------------------------------------------
# handler
# ---------------------------------------------------------------------------
QUERY = {'env': 'prod', 'namespace': 'vip', 'deployment': 'user-service'}


@pytest.mark.asyncio
async def test_get_code_info_reads_image_posts_and_audits(monkeypatch, audits):
    received = []

    async def handler(request):
        received.append(await request.json())
        return web.json_response({**LINKS, 'download_url': 'javascript:alert(1)'})

    async with TestServer(upstream_app(handler)) as server:
        store = FakeStore(code_info.normalize_config({'enabled': True, 'url': str(server.make_url('/code'))}))
        async with TestClient(TestServer(make_app(monkeypatch, store, agent_ok))) as client:
            resp = await client.get('/api/code-info', params=QUERY)
            data = await resp.json()
    assert resp.status == 200 and data['success'] is True
    assert data['data']['request'] == PAYLOAD
    assert data['data']['links'] == {**LINKS, 'download_url': ''}
    assert data['data']['invalid'] == {'download_url': 'javascript:alert(1)'}
    assert received == [PAYLOAD]
    [record] = audits
    assert record['username'] == 'alice' and record['action'] == 'code_info.get'
    assert record['env'] == 'prod' and record['target'] == 'vip/user-service' and record['success'] is True
    assert record['detail']['image'] == PAYLOAD['image'] and record['detail']['status'] == 200
    assert 'error' not in record['detail']


@pytest.mark.asyncio
@pytest.mark.parametrize('value', [None, {'enabled': False, 'url': 'https://c.example.com'}])
async def test_get_code_info_requires_enabled_config(monkeypatch, audits, value):
    async with TestClient(TestServer(make_app(monkeypatch, FakeStore(value), agent_unused))) as client:
        resp = await client.get('/api/code-info', params=QUERY)
        data = await resp.json()
    assert resp.status == 400 and data['code'] == 'not_configured'
    assert '系统管理 → 获取代码接口' in data['message']
    assert audits == []


@pytest.mark.asyncio
@pytest.mark.parametrize('query', [
    {'env': 'prod', 'namespace': '', 'deployment': 'app'},
    {'env': 'prod', 'namespace': 'default', 'deployment': 'App/../x'},
    {'env': 'prod', 'namespace': 'default'},
])
async def test_get_code_info_rejects_bad_names(monkeypatch, audits, query):
    store = FakeStore({'enabled': True, 'url': 'https://c.example.com'})
    async with TestClient(TestServer(make_app(monkeypatch, store, agent_unused))) as client:
        resp = await client.get('/api/code-info', params=query)
    assert resp.status == 400 and audits == []


@pytest.mark.asyncio
async def test_get_code_info_failures_are_audited(monkeypatch, audits):
    async def handler(request):
        return web.json_response({'message': '没有这个服务的代码'}, status=404)

    async def agent_offline(env, path, query=None, timeout=30):
        raise Exception('目标客户端 prod 不在线')

    async with TestServer(upstream_app(handler)) as server:
        store = FakeStore({'enabled': True, 'url': str(server.make_url('/code'))})
        async with TestClient(TestServer(make_app(monkeypatch, store, agent_ok))) as client:
            upstream = await client.get('/api/code-info', params=QUERY)
            upstream_data = await upstream.json()
        async with TestClient(TestServer(make_app(monkeypatch, store, agent_offline))) as client:
            offline = await client.get('/api/code-info', params=QUERY)
            offline_data = await offline.json()
    assert upstream.status == 502 and upstream_data['code'] == 'upstream_error'
    assert upstream_data['message'] == '获取代码接口出错:接口返回 HTTP 404:没有这个服务的代码'
    assert upstream_data['data']['request'] == PAYLOAD  # 页面上显示用的是哪个镜像
    assert offline.status == 503 and '目标客户端 prod 不在线' in offline_data['message']
    assert [record['success'] for record in audits] == [False, False]
    assert audits[0]['detail']['status'] == 404 and audits[0]['detail']['image'] == PAYLOAD['image']
    assert '不在线' in audits[1]['detail']['error']


@pytest.mark.asyncio
async def test_admin_config_round_trip_hides_header_values(monkeypatch, audits):
    store = FakeStore()
    app = make_app(monkeypatch, store, agent_unused, Principal('root', 'admin', user_id=1))
    async with TestClient(TestServer(app)) as client:
        empty = await (await client.get('/api/admin/code-info')).json()
        saved = await (await client.put('/api/admin/code-info', json={
            'enabled': True, 'url': 'https://c.example.com/api', 'timeout': 20, 'tls_verify': False,
            'headers': [{'name': 'Authorization', 'value': 'Bearer secret'}]})).json()
        bad = await client.put('/api/admin/code-info', json={'enabled': True, 'url': 'ftp://c.example.com'})
        bad_data = await bad.json()
        again = await (await client.get('/api/admin/code-info')).json()
    assert empty['data'] == {**code_info.public_config(None), 'updated_by': None, 'updated_at': None}
    assert saved['data'] == {'enabled': True, 'url': 'https://c.example.com/api', 'timeout': 20, 'tls_verify': False,
                             'headers': [{'name': 'Authorization', 'has_value': True}],
                             'updated_by': 'root', 'updated_at': '2026-10-31T11:00:00'}
    assert bad.status == 400 and 'http:// 或 https://' in bad_data['message']
    assert again['data'] == saved['data']
    assert decrypt_secret(store.row['value']['headers'][0]['value']) == 'Bearer secret'
    assert 'secret' not in json.dumps(again) and 'secret' not in json.dumps(audits)
    [record] = audits
    assert record['action'] == 'code_info.update' and record['username'] == 'root'
    assert record['detail'] == {'enabled': True, 'url': 'https://c.example.com/api', 'headers': ['Authorization'],
                                'timeout': 20, 'tls_verify': False}


@pytest.mark.asyncio
async def test_admin_test_uses_form_config_and_saved_header_values(monkeypatch):
    received = []

    async def handler(request):
        received.append((await request.json(), request.headers.get('Authorization')))
        return web.json_response({'web_url': LINKS['web_url']})

    saved = code_info.normalize_config(
        {'url': 'https://old.example.com', 'headers': [{'name': 'Authorization', 'value': 'Bearer saved'}]})
    store = FakeStore(saved)
    async with TestServer(upstream_app(handler)) as server:
        form = {'url': str(server.make_url('/code')), 'headers': [{'name': 'Authorization', 'value': ''}]}
        async with TestClient(TestServer(make_app(monkeypatch, store, agent_unused))) as client:
            resp = await client.post('/api/admin/code-info/test', json={'config': form, 'request': PAYLOAD})
            data = (await resp.json())['data']
            missing = await client.post('/api/admin/code-info/test',
                                        json={'config': form, 'request': {'k8s': 'prod'}})
            missing_data = await missing.json()
    assert data['ok'] is True and data['status'] == 200 and data['request'] == PAYLOAD
    assert data['links'] == {'web_url': LINKS['web_url'], 'download_url': '', 'commits_url': ''}
    assert json.loads(data['body']) == {'web_url': LINKS['web_url']}
    assert received == [(PAYLOAD, 'Bearer saved')]
    assert store.row['value'] == saved  # 测试不保存
    assert missing.status == 400 and missing_data['message'] == '请填写测试入参 namespace'
