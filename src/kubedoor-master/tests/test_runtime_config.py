"""获取运行配置(func_manager/runtime_config.py):配置校验与加密、请求体、调用外部接口(wait、202 重试、超时、
10MB 上限、大 JSON 在线程里处理)、网页 / AI / 管理接口和审计。外部接口用本地起的 aiohttp 服务模拟。"""
import asyncio
import contextlib
import inspect
import json
import re
import threading
import time
from datetime import datetime
from types import MappingProxyType

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from loguru import logger
from multidict import MultiDict

from auth.crypto import decrypt_secret
from auth.principal import SYSTEM, Principal
from func_manager import ext_api, runtime_config

SECRET = 'Pa55-w0rd!'
POD = 'user-service-7d9f8b6c5-x2x9k'
CONFIG = {
    'cluster': 'prod', 'namespace': 'vip', 'deployment': 'deploy-user-service', 'pod': POD,
    'container': 'user-service', 'lang': 'java', 'centers': ['config-server'], 'cached': False,
    'config': {'spring.datasource.url': 'jdbc:mysql://db.example.com:3306/user', 'spring.datasource.password': SECRET},
    'env': {'SPRING_PROFILES_ACTIVE': 'prod'},
}
QUERY = {'env': 'prod', 'namespace': 'vip', 'deployment': 'user-service'}
REQUEST = {'k8s': 'prod', 'namespace': 'vip', 'deployment': 'user-service'}
PENDING = {'status': 'pending', 'pod': POD, 'error': '还在采集,稍后用同样的请求体再请求'}
DEFAULTS = runtime_config.DEFAULTS


@pytest.fixture(autouse=True)
def encryption_key(monkeypatch):
    monkeypatch.setenv('AI_ENCRYPTION_KEY', '11' * 32)


@pytest.fixture
def audits(monkeypatch):
    records = []
    monkeypatch.setattr(runtime_config.audit, 'record_bg',
                        lambda username, action, **kwargs: records.append({'username': username, 'action': action, **kwargs}))
    return records


@pytest.fixture
def logs():
    messages = []
    handler_id = logger.add(lambda message: messages.append(str(message)), level='DEBUG')
    yield messages
    logger.remove(handler_id)


@pytest.fixture
def fake_clock(monkeypatch):
    """202 之后不真的等:记下等了几秒,把时钟往前拨"""
    state = {'offset': 0.0, 'delays': []}
    monkeypatch.setattr(runtime_config, '_clock', lambda: time.monotonic() + state['offset'])

    async def sleep(seconds):
        state['delays'].append(seconds)
        state['offset'] += seconds

    monkeypatch.setattr(runtime_config, '_sleep', sleep)
    return state


class FakeStore:
    """auth_settings 里只有 runtime_config 这一行"""

    def __init__(self, value=None):
        self.row = None if value is None else {
            'value': value, 'updated_by': 'admin', 'updated_at': datetime(2026, 10, 31, 10, 0)}

    async def get_setting(self, key, default=None):
        assert key == runtime_config.SETTING_KEY
        return self.row['value'] if self.row else default

    async def get_setting_row(self, key):
        assert key == runtime_config.SETTING_KEY
        return self.row

    async def put_setting_row(self, key, value, updated_by):
        self.row = {'value': json.loads(json.dumps(value)), 'updated_by': updated_by,
                    'updated_at': datetime(2026, 10, 31, 11, 0)}
        return self.row


def make_app(monkeypatch, store, principal=None):
    monkeypatch.setattr(runtime_config, 'store', store)
    principal = principal or Principal('alice', 'rw', user_id=7)

    @web.middleware
    async def inject(request, handler):
        request['principal'] = principal
        return await handler(request)

    app = web.Application(middlewares=[inject])
    runtime_config.register_routes(app)
    return app


def internal(role='rw', **kwargs):
    """内部 token + X-User-Name 解析出的用户"""
    return Principal('alice', role, user_id=7, via='internal', **kwargs)


# ---------------------------------------------------------------------------
# 假的获取运行配置接口
# ---------------------------------------------------------------------------
class Upstream:
    """按顺序回放响应(最后一个一直重复),记下收到的请求体和请求头"""

    def __init__(self, *handlers):
        self.handlers = handlers
        self.requests = []

    async def handle(self, request):
        self.requests.append({'body': await request.json(), 'headers': dict(request.headers)})
        response = self.handlers[min(len(self.requests), len(self.handlers)) - 1](request)
        return await response if inspect.isawaitable(response) else response

    @property
    def bodies(self):
        return [item['body'] for item in self.requests]


def reply(status=200, data=None, text=None, headers=None):
    def handler(request):
        if text is not None:
            return web.Response(status=status, text=text, headers=headers)
        return web.json_response(CONFIG if data is None else data, status=status, headers=headers)
    return handler


def pending(retry_after=None):
    return reply(202, PENDING, headers=None if retry_after is None else {'Retry-After': retry_after})


@contextlib.asynccontextmanager
async def serve(upstream, path='/pod-config'):
    app = web.Application()
    app.router.add_post(path, upstream.handle)
    async with TestServer(app) as server:
        yield str(server.make_url(path))


def without_wait(bodies):
    return [{key: value for key, value in body.items() if key != 'wait'} for body in bodies]


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
def test_normalize_config_defaults_ranges_and_ai_switch():
    cfg = runtime_config.normalize_config(
        {'enabled': True, 'url': ' https://config.example.com/pod-config ', 'timeout': '90', 'tls_verify': False})
    assert cfg == {'enabled': True, 'url': 'https://config.example.com/pod-config', 'tls_verify': False,
                   'ai_enabled': False, 'timeout': 90, 'headers': []}
    assert runtime_config.normalize_config({})['timeout'] == 60  # 默认 60 秒
    # 「允许 AI 获取运行配置」默认关闭,没传沿用原值
    allowed = runtime_config.normalize_config({'ai_enabled': True}, cfg)
    assert allowed == {**cfg, 'ai_enabled': True}
    assert runtime_config.normalize_config({'timeout': 5}, allowed) == {**allowed, 'timeout': 5}
    assert runtime_config.normalize_config({'timeout': 120}, allowed)['timeout'] == 120


@pytest.mark.parametrize('raw, error', [
    ({'timeout': 4}, '超时时间取 5~120 秒'),
    ({'timeout': 121}, '超时时间取 5~120 秒'),
    ({'timeout': 'abc'}, '整数'),
    ({'enabled': True, 'url': ''}, '启用时必须填写接口地址'),
    ({'url': 'ftp://config.example.com/pod-config'}, '例如 https://config.example.com/pod-config'),
    ({'url': 'javascript:alert(1)'}, 'http:// 或 https://'),
    ([], '配置格式不正确'),
])
def test_normalize_config_rejects_invalid_values(raw, error):
    with pytest.raises(ValueError, match=re.escape(error)):
        runtime_config.normalize_config(raw)


def test_header_values_are_encrypted_hidden_and_kept_when_blank():
    first = runtime_config.normalize_config({'url': 'https://config.example.com', 'headers': [
        {'name': 'Authorization', 'value': ' Bearer t0ken-123 '}]})
    stored = first['headers'][0]['value']
    assert stored.startswith('v1:') and 't0ken' not in stored and decrypt_secret(stored) == 'Bearer t0ken-123'
    # 值留空 = 沿用已保存的(名称不区分大小写);新加的必须填值
    second = runtime_config.normalize_config({'headers': [{'name': 'authorization', 'value': ''}]}, first)
    assert second['headers'] == [{'name': 'authorization', 'value': stored}]
    with pytest.raises(ValueError, match='请填写请求头 X-New 的值'):
        runtime_config.normalize_config({'headers': [{'name': 'X-New', 'value': ''}]}, first)
    with pytest.raises(ValueError, match='由系统设置'):
        runtime_config.normalize_config({'headers': [{'name': 'Content-Type', 'value': 'text/plain'}]})
    public = runtime_config.public_config(second)
    assert public['headers'] == [{'name': 'authorization', 'has_value': True}]
    assert 't0ken' not in json.dumps(public) and stored not in json.dumps(public)
    assert runtime_config.public_config(None) == {'enabled': False, 'url': '', 'headers': [], 'timeout': 60,
                                                  'tls_verify': True, 'ai_enabled': False}


# ---------------------------------------------------------------------------
# 请求体
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('query, expected', [
    (QUERY, REQUEST),
    ({'namespace': 'vip', 'pod': POD}, {'k8s': 'prod', 'namespace': 'vip', 'pod': POD}),
    # 可选字段有值才带;keys 是正则,原样交给接口
    ({**QUERY, 'container': 'user-service', 'keys': r' ^spring\.datasource\.(url|password)$ ', 'detail': '1',
      'refresh': 'true'},
     {**REQUEST, 'container': 'user-service', 'keys': r'^spring\.datasource\.(url|password)$', 'detail': True,
      'refresh': True}),
    ({**QUERY, 'container': ' ', 'keys': '', 'detail': '0', 'refresh': 'no'}, REQUEST),
    ({**QUERY, 'keys': 'k' * 500}, {**REQUEST, 'keys': 'k' * 500}),
])
def test_build_payload(query, expected):
    assert runtime_config.build_payload('prod', MultiDict(query)) == expected


@pytest.mark.parametrize('query, error', [
    ({'namespace': '', 'deployment': 'app'}, '命名空间名称不正确'),
    ({'namespace': 'Vip', 'deployment': 'app'}, '命名空间名称不正确'),
    ({'namespace': 'vip'}, '二选一'),
    ({'namespace': 'vip', 'deployment': 'app', 'pod': 'app-1'}, '二选一'),
    ({'namespace': 'vip', 'deployment': 'app/../x'}, 'Deployment 名称不正确'),
    ({'namespace': 'vip', 'pod': 'Bad_Pod'}, 'Pod 名称不正确'),
    ({'namespace': 'vip', 'deployment': 'app', 'container': 'a b'}, '容器名称不正确'),
    ({'namespace': 'vip', 'deployment': 'app', 'keys': 'k' * 501}, '最长 500 个字符'),
    ({'namespace': 'vip', 'deployment': 'app', 'keys': 'datasource\nredis'}, '控制字符'),
])
def test_build_payload_rejects_bad_values(query, error):
    with pytest.raises(ValueError, match=re.escape(error)):
        runtime_config.build_payload('prod', query)


@pytest.mark.parametrize('value, expected', [
    (None, 3), ('', 3), ('soon', 3), ('Wed, 21 Oct 2026 07:28:00 GMT', 3), ('nan', 3),
    ('2', 2), (' 5 ', 5), ('1.5', 1.5), ('0', 1), ('-3', 1), ('60', 10), ('inf', 10),
])
def test_retry_after_defaults_and_bounds(value, expected):
    assert runtime_config.retry_after(value) == expected


# ---------------------------------------------------------------------------
# 接口约定
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('status, raw, location, error', [
    (302, b'', 'https://sso.example.com/login', '接口返回了重定向(HTTP 302 → https://sso.example.com/login),不会跟随'),
    (404, '{"error": "命名空间 vip 里没有 Deployment user-servic"}'.encode(), '',
     '接口返回 HTTP 404:命名空间 vip 里没有 Deployment user-servic'),
    (401, b'unauthorized', '', '接口返回 HTTP 401:unauthorized'),
    (502, b'<html><body>Bad Gateway</body></html>', '', '接口返回 HTTP 502'),  # HTML 错误页不带内容
    (500, b'{"code": 500, "error": ""}', '', '接口返回 HTTP 500'),  # JSON 里没有可用的原因
    (200, b'ok', '', '接口返回的不是 JSON'),
    (204, b'', '', '接口返回的不是 JSON'),
    (200, b'[1, 2]', '', '接口返回的 JSON 不是对象'),
])
def test_parse_reply_errors(status, raw, location, error):
    config, message = runtime_config.parse_reply(status, raw, location)
    assert config is None and message.startswith(error)
    if status in (500, 502, 200, 204):
        assert message == error


def test_parse_reply_rejects_too_deep_json():
    deep = b'[' * 100000 + b']' * 100000  # json.loads 抛 RecursionError,不能让它冒到 handler 变成 500
    assert runtime_config.parse_reply(200, deep) == (None, '接口返回的不是 JSON')
    assert runtime_config.parse_reply(500, deep)[1] == '接口返回 HTTP 500:' + deep.decode()[:200]


def test_parse_reply_accepts_any_2xx_json_object():
    assert runtime_config.parse_reply(200, json.dumps(CONFIG).encode()) == (CONFIG, '')
    assert runtime_config.parse_reply(201, b'{"config": {}}') == ({'config': {}}, '')


def test_size_limits():
    assert runtime_config.MAX_RESPONSE == 10 * 1024 * 1024
    assert ext_api.size_text(runtime_config.MAX_RESPONSE) == '10 MB'
    assert ext_api.size_text(1024 * 1024) == '1024 KB'  # 获取代码的提示保持原样


# ---------------------------------------------------------------------------
# 调用接口(本地起一个真的 HTTP 服务)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_call_endpoint_posts_body_with_wait_and_headers():
    upstream = Upstream(reply())
    async with serve(upstream) as url:
        cfg = runtime_config.normalize_config({'url': url, 'timeout': 30, 'headers': [
            {'name': 'Authorization', 'value': 'Bearer t0ken'}]})
        result = await runtime_config.call_endpoint(cfg, {**REQUEST, 'keys': 'datasource'})
    assert result['ok'] is True and result['code'] == '' and result['error'] == ''
    assert result['status'] == 200 and result['attempts'] == 1 and result['config'] == CONFIG and result['body'] == ''
    [received] = upstream.requests
    assert received['body'] == {**REQUEST, 'keys': 'datasource', 'wait': 30}  # wait:本次还能等的秒数
    assert received['headers']['Authorization'] == 'Bearer t0ken'
    assert received['headers']['Content-Type'].startswith('application/json')
    assert received['headers']['User-Agent'] == 'KubeDoor'


@pytest.mark.asyncio
async def test_call_endpoint_retries_202_with_same_body(fake_clock):
    upstream = Upstream(pending('2'), pending(), reply())
    async with serve(upstream) as url:
        result = await runtime_config.call_endpoint({**DEFAULTS, 'url': url}, REQUEST)
    assert result['ok'] is True and result['attempts'] == 3 and result['config'] == CONFIG
    assert fake_clock['delays'] == [2, 3]  # 按 Retry-After 等;没有时 3 秒
    assert without_wait(upstream.bodies) == [REQUEST] * 3  # 同样的请求体重发
    assert [body['wait'] for body in upstream.bodies] == [60, 58, 55]  # wait 每次重新计算
    assert result['elapsed_ms'] >= 5000  # 含等待的时间


@pytest.mark.asyncio
async def test_call_endpoint_times_out_when_still_collecting(fake_clock):
    upstream = Upstream(pending('2.9'))
    async with serve(upstream) as url:
        result = await runtime_config.call_endpoint({**DEFAULTS, 'url': url, 'timeout': 10}, REQUEST)
    assert result['ok'] is False and result['code'] == 'timeout' and result['status'] == 202
    assert result['error'] == '接口在 10 秒内没有采集完,请稍后再试'
    # 10 秒:等了三次 2.9 秒后只剩 1.3 秒,不够再等 2.9 秒并留 1 秒给接口,不再重发
    assert fake_clock['delays'] == [2.9, 2.9, 2.9] and result['attempts'] == 4
    assert [body['wait'] for body in upstream.bodies] == [10, 7, 4, 1]
    assert json.loads(result['body']) == PENDING and result['config'] is None


@pytest.mark.asyncio
async def test_call_endpoint_request_timeout(monkeypatch):
    async def hang(request):
        await asyncio.sleep(1)
        return web.json_response(CONFIG)

    monkeypatch.setattr(runtime_config, 'NETWORK_SLACK', 0)
    upstream = Upstream(hang)
    async with serve(upstream) as url:
        result = await runtime_config.call_endpoint({**DEFAULTS, 'url': url, 'timeout': 0.2}, REQUEST)
    assert result['code'] == 'timeout' and result['error'] == '接口在 0.2 秒内没有返回,请稍后再试'
    assert result['status'] is None and result['attempts'] == 1 and result['ok'] is False


async def _chunked(request):
    response = web.StreamResponse()
    response.enable_chunked_encoding()
    await response.prepare(request)
    for _ in range(3):
        await response.write(b'x' * 600)
    await response.write_eof()
    return response


async def _drop(request):
    request.transport.close()
    await asyncio.sleep(0.1)
    return web.Response()


@pytest.mark.asyncio
@pytest.mark.parametrize('handler, status, error, body', [
    (reply(302, text='', headers={'Location': 'https://sso.example.com/login'}), 302,
     '接口返回了重定向(HTTP 302 → https://sso.example.com/login)', ''),
    (reply(400, {'error': 'Deployment 里没有容器 nope,可选:user-service'}), 400,
     '接口返回 HTTP 400:Deployment 里没有容器 nope,可选:user-service', '"error"'),
    (reply(500, text='<html>Internal Server Error</html>'), 500, '接口返回 HTTP 500', '<html>'),
    (reply(200, text='ok'), 200, '接口返回的不是 JSON', 'ok'),
    (reply(200, data=[CONFIG]), 200, '接口返回的 JSON 不是对象', '[{'),
    (reply(200, text='x' * 2000), 200, '接口返回的内容超过 0 KB', ''),  # 有 Content-Length:不读就拒绝
    (_chunked, 200, '接口返回的内容超过 0 KB', ''),  # 分块传输:读到上限就停
    (_drop, None, '无法访问接口:', ''),
])
async def test_call_endpoint_reports_upstream_problems(monkeypatch, handler, status, error, body):
    monkeypatch.setattr(runtime_config, 'MAX_RESPONSE', 1000)
    upstream = Upstream(handler)
    async with serve(upstream) as url:
        result = await runtime_config.call_endpoint({**DEFAULTS, 'url': url, 'timeout': 5}, REQUEST)
    assert result['ok'] is False and result['code'] == 'upstream_error' and result['config'] is None
    assert result['status'] == status and result['attempts'] == 1 and result['error'].startswith(error)
    assert body in result['body'] if body else result['body'] == ''


def self_signed_context(directory):
    """自签证书的 HTTPS 服务端(127.0.0.1)"""
    import ipaddress
    import ssl
    from datetime import timedelta, timezone

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'config.example.com')])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]),
                           critical=False)
            .sign(key, hashes.SHA256()))
    cert_file, key_file = directory / 'cert.pem', directory / 'key.pem'
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(cert_file, key_file)
    return context


@pytest.mark.asyncio
async def test_call_endpoint_follows_tls_verify(tmp_path):
    upstream = Upstream(reply())
    app = web.Application()
    app.router.add_post('/pod-config', upstream.handle)
    server = TestServer(app)
    await server.start_server(ssl=self_signed_context(tmp_path))
    async with server:
        url = str(server.make_url('/pod-config'))
        strict = await runtime_config.call_endpoint({**DEFAULTS, 'url': url, 'timeout': 5}, REQUEST)
        relaxed = await runtime_config.call_endpoint({**DEFAULTS, 'url': url, 'timeout': 5, 'tls_verify': False},
                                                     REQUEST)
    assert url.startswith('https://')
    assert strict['ok'] is False and strict['code'] == 'upstream_error' and strict['status'] is None
    assert strict['error'].startswith('HTTPS 证书校验失败') and '关闭「校验 HTTPS 证书」' in strict['error']
    assert relaxed['ok'] is True and relaxed['config'] == CONFIG and len(upstream.requests) == 1


@pytest.mark.asyncio
async def test_call_endpoint_reports_undecryptable_header(monkeypatch):
    cfg = runtime_config.normalize_config(
        {'url': 'https://config.example.com', 'headers': [{'name': 'Authorization', 'value': 'x'}]})
    monkeypatch.setenv('AI_ENCRYPTION_KEY', '22' * 32)  # 密钥换过
    result = await runtime_config.call_endpoint(cfg, REQUEST)
    assert '请求头 Authorization 的值无法解密' in result['error'] and result['code'] == 'upstream_error'
    assert result['status'] is None and result['attempts'] == 0


# ---------------------------------------------------------------------------
# 网页:GET /api/runtime-config
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_get_runtime_config_returns_config_and_audits_without_values(monkeypatch, audits, logs):
    upstream = Upstream(reply())
    async with serve(upstream) as url:
        store = FakeStore(runtime_config.normalize_config({'enabled': True, 'url': url, 'headers': [
            {'name': 'Authorization', 'value': 'Bearer t0ken'}]}))
        async with TestClient(TestServer(make_app(monkeypatch, store))) as client:
            resp = await client.get('/api/runtime-config', params={
                **QUERY, 'container': 'user-service', 'keys': 'datasource', 'detail': '1'})
            data = await resp.json()
    request = {**REQUEST, 'container': 'user-service', 'keys': 'datasource', 'detail': True}
    assert resp.status == 200 and data['success'] is True
    assert data['data']['request'] == request  # 不含 wait
    assert data['data']['config'] == CONFIG and data['data']['attempts'] == 1
    assert isinstance(data['data']['elapsed_ms'], int)
    assert upstream.bodies == [{**request, 'wait': 60}]
    [record] = audits
    assert record['username'] == 'alice' and record['action'] == 'runtime_config.get' and record['env'] == 'prod'
    assert record['target'] == 'vip/user-service' and record['success'] is True
    assert record['detail'] == {'deployment': 'user-service', 'container': 'user-service', 'keys': 'datasource',
                                'status': 200, 'elapsed_ms': record['detail']['elapsed_ms'], 'attempts': 1}
    # 审计和日志里没有任何配置值、请求头的值
    assert SECRET not in json.dumps(audits, ensure_ascii=False) and 't0ken' not in json.dumps(audits)
    assert not any(SECRET in line or 't0ken' in line for line in logs)


@pytest.mark.asyncio
async def test_get_runtime_config_by_pod(monkeypatch, audits):
    upstream = Upstream(reply())
    async with serve(upstream) as url:
        store = FakeStore({'enabled': True, 'url': url})
        async with TestClient(TestServer(make_app(monkeypatch, store))) as client:
            resp = await client.get('/api/runtime-config', params={'env': 'prod', 'namespace': 'vip', 'pod': POD})
            data = await resp.json()
    assert resp.status == 200 and data['data']['request'] == {'k8s': 'prod', 'namespace': 'vip', 'pod': POD}
    [record] = audits
    assert record['target'] == f'vip/{POD}' and record['detail']['pod'] == POD and 'deployment' not in record['detail']


@pytest.mark.asyncio
@pytest.mark.parametrize('value', [None, {'enabled': False, 'url': 'https://config.example.com'},
                                   {'enabled': True, 'url': ''}])
async def test_get_runtime_config_requires_enabled_config(monkeypatch, audits, value):
    async with TestClient(TestServer(make_app(monkeypatch, FakeStore(value)))) as client:
        resp = await client.get('/api/runtime-config', params=QUERY)
        data = await resp.json()
    assert resp.status == 400 and data['code'] == 'not_configured' and data['success'] is False
    assert data['message'] == '系统管理员还没有配置获取运行配置接口(系统管理 → 获取运行配置接口)'
    assert audits == []


@pytest.mark.asyncio
@pytest.mark.parametrize('query', [
    {'env': 'prod', 'namespace': 'vip'},
    {**QUERY, 'pod': POD},
    {**QUERY, 'namespace': 'Bad'},
    {**QUERY, 'container': 'Bad Name'},
    {**QUERY, 'keys': 'k' * 501},
])
async def test_get_runtime_config_rejects_bad_params(monkeypatch, audits, query):
    store = FakeStore({'enabled': True, 'url': 'https://config.example.com'})  # 校验不过不会访问接口
    async with TestClient(TestServer(make_app(monkeypatch, store))) as client:
        resp = await client.get('/api/runtime-config', params=query)
        data = await resp.json()
    assert resp.status == 400 and data['code'] == 'bad_request' and audits == []


@pytest.mark.asyncio
async def test_get_runtime_config_failures_are_audited(monkeypatch, audits, fake_clock):
    failing = Upstream(reply(404, {'error': '命名空间 vip 里没有 Deployment user-service'}))
    collecting = Upstream(pending('3'))
    async with serve(failing) as failing_url, serve(collecting) as collecting_url:
        async with TestClient(TestServer(make_app(monkeypatch, FakeStore({'enabled': True, 'url': failing_url})))) as client:
            upstream = await client.get('/api/runtime-config', params=QUERY)
            upstream_data = await upstream.json()
        store = FakeStore({'enabled': True, 'url': collecting_url, 'timeout': 5})
        async with TestClient(TestServer(make_app(monkeypatch, store))) as client:
            timeout = await client.get('/api/runtime-config', params=QUERY)
            timeout_data = await timeout.json()
    assert upstream.status == 502 and upstream_data['code'] == 'upstream_error' and upstream.content_type == 'application/json'
    assert upstream_data['message'] == '获取运行配置接口出错:接口返回 HTTP 404:命名空间 vip 里没有 Deployment user-service'
    assert upstream_data['data'] == {'request': REQUEST}  # 带上发出去的请求体(不含 wait)
    assert timeout.status == 504 and timeout_data['code'] == 'timeout'
    assert timeout_data['message'] == '获取运行配置超时:接口在 5 秒内没有采集完,请稍后再试'
    assert [record['success'] for record in audits] == [False, False]
    assert audits[0]['detail']['status'] == 404 and '没有 Deployment' in audits[0]['detail']['error']
    assert audits[1]['detail']['status'] == 202 and audits[1]['detail']['attempts'] == 2


@pytest.mark.asyncio
async def test_large_config_is_parsed_and_serialized_in_threads(monkeypatch, audits):
    big = {**CONFIG, 'files': [{'path': f'/etc/app/{i}.yml', 'content': 'key: value\n' * 100} for i in range(400)]}
    threads = {}
    parse, dumps = runtime_config.parse_reply, ext_api._dumps

    def record_parse(*args):
        threads.setdefault('parse', []).append(threading.get_ident())
        return parse(*args)

    def record_dumps(payload):
        threads.setdefault('dumps', []).append(threading.get_ident())
        return dumps(payload)

    monkeypatch.setattr(runtime_config, 'parse_reply', record_parse)
    monkeypatch.setattr(ext_api, '_dumps', record_dumps)
    loop_thread = threading.get_ident()
    upstream = Upstream(reply(data=big), reply())
    async with serve(upstream) as url:
        async with TestClient(TestServer(make_app(monkeypatch, FakeStore({'enabled': True, 'url': url})))) as client:
            large = await client.get('/api/runtime-config', params=QUERY)
            large_data = await large.json()
            small = await client.get('/api/runtime-config', params=QUERY)
    assert large.status == 200 and large_data['data']['config'] == big and small.status == 200
    # 几百 KB 以上的解析和序列化都不在 event loop 线程里;小响应直接处理
    assert threads['parse'][0] != loop_thread and threads['dumps'][0] != loop_thread
    assert threads['parse'][1] == loop_thread and threads['dumps'][1] == loop_thread


# ---------------------------------------------------------------------------
# AI 助手:GET /api/ai/internal/runtime-config
# ---------------------------------------------------------------------------
AI_ON = {'enabled': True, 'ai_enabled': True}


@pytest.mark.asyncio
async def test_ai_runtime_config_returns_plaintext_and_audits_purpose(monkeypatch, audits):
    upstream = Upstream(reply())
    async with serve(upstream) as url:
        store = FakeStore({**AI_ON, 'url': url})
        async with TestClient(TestServer(make_app(monkeypatch, store, internal()))) as client:
            view = await client.get('/api/ai/internal/runtime-config', params={
                'env': 'prod', 'namespace': 'vip', 'pod': POD, 'keys': 'datasource|redis'})
            view_data = await view.json()
            resolve = await client.get('/api/ai/internal/runtime-config', params={
                **QUERY, 'container': 'user-service', 'keys': r'^(spring\.datasource\.password)$', 'purpose': 'resolve'})
            resolve_data = await resolve.json()
    assert view.status == 200 and view_data['success'] is True
    assert view_data['data']['config'] == CONFIG  # 原文(含明文密码),打码在 kubedoor-ai 里做
    assert view_data['data']['request'] == {'k8s': 'prod', 'namespace': 'vip', 'pod': POD, 'keys': 'datasource|redis'}
    assert resolve.status == 200 and resolve_data['data']['request']['container'] == 'user-service'
    assert without_wait(upstream.bodies) == [view_data['data']['request'], resolve_data['data']['request']]
    assert [(record['action'], record['detail']['source'], record['detail']['purpose'], record['target'])
            for record in audits] == [('runtime_config.get', 'ai', 'view', f'vip/{POD}'),
                                      ('runtime_config.get', 'ai', 'resolve', 'vip/user-service')]
    assert SECRET not in json.dumps(audits)


@pytest.mark.asyncio
@pytest.mark.parametrize('principal, query, status, code, message', [
    # 浏览器会话、没有用户身份的系统调用都不是可信的 AI 回调
    (Principal('alice', 'admin', user_id=7), QUERY, 403, 'forbidden', '缺少可信用户身份'),
    (SYSTEM, QUERY, 403, 'forbidden', '缺少可信用户身份'),
    (internal(), {**QUERY, 'env': ''}, 400, 'bad_request', '缺少 K8S 集群参数'),
    # 按库里的权限查「查看运行配置」:默认只读不行,看不到的集群按无权访问
    (internal(role='read'), QUERY, 403, 'permission_denied', '当前账号在集群 prod 是「只读」权限,不能执行「查看运行配置」'),
    (internal(clusters=MappingProxyType({'prod': 'none'})), QUERY, 403, 'permission_denied', '没有集群 prod 的访问权限'),
    (internal(), {**QUERY, 'purpose': 'dump'}, 400, 'bad_request', 'purpose 只能是 view 或 resolve'),
    (internal(), {**QUERY, 'pod': POD}, 400, 'bad_request', '二选一'),
    (internal(), {**QUERY, 'keys': 'a\tb'}, 400, 'bad_request', '控制字符'),
])
async def test_ai_runtime_config_rejects_requests_with_json(monkeypatch, audits, principal, query, status, code,
                                                            message):
    store = FakeStore({**AI_ON, 'url': 'https://config.example.com'})
    async with TestClient(TestServer(make_app(monkeypatch, store, principal))) as client:
        resp = await client.get('/api/ai/internal/runtime-config', params=query)
        data = await resp.json()  # 一律 JSON,不是 aiohttp 的纯文本
    assert resp.status == status and resp.content_type == 'application/json'
    assert data['success'] is False and data['code'] == code and message in data['message'] and audits == []


@pytest.mark.asyncio
async def test_ai_runtime_config_follows_permission_table(monkeypatch, audits, set_policy):
    upstream = Upstream(reply())
    async with serve(upstream) as url:
        store = FakeStore({**AI_ON, 'url': url})
        async with TestClient(TestServer(make_app(monkeypatch, store, internal(role='read')))) as client:
            set_policy({'config.view': {'read': True}})  # 勾上只读列
            allowed = await client.get('/api/ai/internal/runtime-config', params=QUERY)
            set_policy({'config.view': {'read': False, 'rw': False, 'admin': False}})  # 全站关闭
            closed = await client.get('/api/ai/internal/runtime-config', params=QUERY)
            closed_data = await closed.json()
    assert allowed.status == 200 and closed.status == 403 and closed_data['code'] == 'permission_denied'
    assert len(upstream.requests) == 1 and len(audits) == 1


@pytest.mark.asyncio
async def test_ai_resolve_also_needs_data_query(monkeypatch, audits, set_policy):
    """purpose=resolve 是执行数据查询前把占位符换回密码:从 kubedoor-ai 本地发起的查询不经过 agent-execute,
    master 在这一步再按库里的权限查「AI 查询数据库 / Redis / ES」;purpose=view 不需要"""
    upstream = Upstream(reply())
    async with serve(upstream) as url:
        store = FakeStore({**AI_ON, 'url': url})
        async with TestClient(TestServer(make_app(monkeypatch, store, internal()))) as client:
            async def get(purpose):
                resp = await client.get('/api/ai/internal/runtime-config', params={**QUERY, 'purpose': purpose})
                return resp.status, await resp.json()

            set_policy({'data.query': {'rw': False}})  # 读写只有「查看运行配置」
            view, only_view = await get('view'), await get('resolve')
            set_policy({})  # 默认:读写两项都有
            both = await get('resolve')
            set_policy({'config.view': {'rw': False}})  # 只有「AI 查询数据库」也不行
            only_query = await get('resolve')
    assert view[0] == 200 and view[1]['data']['config'] == CONFIG
    assert only_view[0] == 403 and only_view[1]['code'] == 'permission_denied'
    assert only_view[1]['message'] == '当前账号在集群 prod 是「读写」权限,不能执行「AI 查询数据库 / Redis / ES」'
    assert both[0] == 200 and both[1]['data']['config'] == CONFIG
    assert only_query[0] == 403 and only_query[1]['code'] == 'permission_denied'
    assert only_query[1]['message'] == '当前账号在集群 prod 是「读写」权限,不能执行「查看运行配置」'
    # 被拒的不访问接口、不记审计
    assert len(upstream.requests) == 2
    assert [record['detail']['purpose'] for record in audits] == ['view', 'resolve']


@pytest.mark.asyncio
@pytest.mark.parametrize('value, status, code, message', [
    ({'enabled': True, 'url': 'https://config.example.com'}, 403, 'ai_config_disabled',
     '系统管理员没有开启「允许 AI 获取运行配置」(系统管理 → 获取运行配置接口)'),
    ({'enabled': False, 'ai_enabled': True, 'url': 'https://config.example.com'}, 400, 'not_configured',
     '系统管理员还没有配置获取运行配置接口'),
])
async def test_ai_runtime_config_switches(monkeypatch, audits, value, status, code, message):
    async with TestClient(TestServer(make_app(monkeypatch, FakeStore(value), internal()))) as client:
        resp = await client.get('/api/ai/internal/runtime-config', params=QUERY)
        data = await resp.json()
    assert resp.status == status and data['code'] == code and data['message'].startswith(message) and audits == []


class BrokenStore(FakeStore):
    async def get_setting(self, key, default=None):
        raise RuntimeError('db down')


@pytest.mark.asyncio
@pytest.mark.parametrize('path, principal, query', [
    ('/api/runtime-config', None, QUERY),
    ('/api/ai/internal/runtime-config', internal(), QUERY),
])
async def test_config_read_failure_returns_json(monkeypatch, audits, path, principal, query):
    async with TestClient(TestServer(make_app(monkeypatch, BrokenStore(), principal))) as client:
        resp = await client.get(path, params=query)
        data = await resp.json()
    assert resp.status == 503 and data['code'] == 'config_unavailable' and audits == []


@pytest.mark.asyncio
async def test_ai_runtime_config_fails_closed_without_policy(monkeypatch, audits):
    async def broken(force=False):
        raise RuntimeError('db down')

    monkeypatch.setattr(runtime_config.permissions, 'load_policy', broken)
    store = FakeStore({**AI_ON, 'url': 'https://config.example.com'})
    async with TestClient(TestServer(make_app(monkeypatch, store, internal()))) as client:
        resp = await client.get('/api/ai/internal/runtime-config', params=QUERY)
        data = await resp.json()
    assert resp.status == 503 and data['code'] == 'policy_unavailable' and audits == []


@pytest.mark.asyncio
async def test_ai_runtime_config_upstream_failure(monkeypatch, audits):
    upstream = Upstream(reply(502, {'error': '访问集群出错:kubectl 超时'}))
    async with serve(upstream) as url:
        store = FakeStore({**AI_ON, 'url': url})
        async with TestClient(TestServer(make_app(monkeypatch, store, internal()))) as client:
            resp = await client.get('/api/ai/internal/runtime-config', params=QUERY)
            data = await resp.json()
    assert resp.status == 502 and data['code'] == 'upstream_error' and data['data'] == {'request': REQUEST}
    assert data['error'] == '获取运行配置接口出错:接口返回 HTTP 502:访问集群出错:kubectl 超时'
    [record] = audits
    assert record['success'] is False and record['detail']['source'] == 'ai' and record['detail']['purpose'] == 'view'


# ---------------------------------------------------------------------------
# 系统管理:配置和测试
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_admin_config_round_trip_hides_header_values(monkeypatch, audits):
    store = FakeStore()
    app = make_app(monkeypatch, store, Principal('root', 'admin', user_id=1))
    async with TestClient(TestServer(app)) as client:
        empty = await (await client.get('/api/admin/runtime-config')).json()
        saved = await (await client.put('/api/admin/runtime-config', json={
            'enabled': True, 'url': 'https://config.example.com/pod-config', 'timeout': 90, 'tls_verify': False,
            'headers': [{'name': 'Authorization', 'value': 'Bearer s3cret'}]})).json()
        bad = await client.put('/api/admin/runtime-config', json={'timeout': 3})
        bad_data = await bad.json()
        # 右上角开关:只传 ai_enabled,地址、请求头(连同值)都沿用
        toggled = await (await client.put('/api/admin/runtime-config', json={'ai_enabled': True})).json()
        # 请求体不是 JSON 对象:当作什么都没传
        unchanged = await (await client.put('/api/admin/runtime-config', data='not json')).json()
        again = await (await client.get('/api/admin/runtime-config')).json()
    assert empty['data'] == {**runtime_config.public_config(None), 'updated_by': None, 'updated_at': None}
    assert saved['data'] == {'enabled': True, 'url': 'https://config.example.com/pod-config', 'timeout': 90,
                             'tls_verify': False, 'ai_enabled': False,
                             'headers': [{'name': 'Authorization', 'has_value': True}],
                             'updated_by': 'root', 'updated_at': '2026-10-31T11:00:00'}
    assert bad.status == 400 and bad_data['message'] == '超时时间取 5~120 秒'
    assert toggled['data'] == unchanged['data'] == again['data'] == {**saved['data'], 'ai_enabled': True}
    assert decrypt_secret(store.row['value']['headers'][0]['value']) == 'Bearer s3cret'
    assert 's3cret' not in json.dumps(again) and 's3cret' not in json.dumps(audits)
    first, second, third = audits
    assert first['action'] == second['action'] == 'runtime_config.update' and first['username'] == 'root'
    assert third['detail'] == second['detail']
    assert first['detail'] == {'enabled': True, 'url': 'https://config.example.com/pod-config',
                               'headers': ['Authorization'], 'timeout': 90, 'tls_verify': False, 'ai_enabled': False}
    assert second['detail'] == {**first['detail'], 'ai_enabled': True}


@pytest.mark.asyncio
async def test_admin_test_uses_form_config_and_saved_header_values(monkeypatch, fake_clock):
    upstream = Upstream(pending('1'), reply())
    saved = runtime_config.normalize_config(
        {'url': 'https://old.example.com', 'headers': [{'name': 'Authorization', 'value': 'Bearer saved'}]})
    store = FakeStore(saved)
    async with serve(upstream) as url:
        form = {'url': url, 'timeout': 30, 'headers': [{'name': 'Authorization', 'value': ''}]}
        async with TestClient(TestServer(make_app(monkeypatch, store, Principal('root', 'admin', user_id=1)))) as client:
            resp = await client.post('/api/admin/runtime-config/test', json={'config': form, 'request': {
                'k8s': 'prod', 'namespace': 'vip', 'deployment': ' user-service ', 'pod': ' ', 'container': None,
                'keys': 'datasource', 'detail': True, 'refresh': False}})
            data = (await resp.json())['data']
            # 入参全空也能测:请求体里只有 wait
            empty = await client.post('/api/admin/runtime-config/test', json={'config': form, 'request': {}})
            empty_data = (await empty.json())['data']
    request = {**REQUEST, 'keys': 'datasource', 'detail': True}
    assert data == {'ok': True, 'status': 200, 'elapsed_ms': data['elapsed_ms'], 'attempts': 2, 'error': '',
                    'config': CONFIG, 'body': '', 'request': request}
    assert empty_data['ok'] is True and empty_data['request'] == {}
    assert upstream.bodies == [{**request, 'wait': 30}, {**request, 'wait': 29}, {'wait': 30}]
    assert {item['headers']['Authorization'] for item in upstream.requests} == {'Bearer saved'}
    assert store.row['value'] == saved  # 测试不保存


@pytest.mark.asyncio
async def test_admin_test_reports_failures_and_bad_input(monkeypatch):
    upstream = Upstream(reply(200, text='<html>login</html>'))
    store = FakeStore()
    async with serve(upstream) as url:
        async with TestClient(TestServer(make_app(monkeypatch, store, Principal('root', 'admin', user_id=1)))) as client:
            failed = await (await client.post('/api/admin/runtime-config/test', json={
                'config': {'url': url}, 'request': {'pod': POD}})).json()
            no_url = await client.post('/api/admin/runtime-config/test', json={'config': {'url': ''}})
            no_url_data = await no_url.json()
            bad_keys = await client.post('/api/admin/runtime-config/test', json={
                'config': {'url': url}, 'request': {'keys': 'k' * 501}})
            bad_field = await client.post('/api/admin/runtime-config/test', json={
                'config': {'url': url}, 'request': {'namespace': 'a\nb'}})
            bad_field_data = await bad_field.json()
    assert failed['success'] is True and failed['data']['ok'] is False
    assert failed['data']['error'] == '接口返回的不是 JSON' and failed['data']['status'] == 200
    assert failed['data']['config'] is None and failed['data']['body'] == '<html>login</html>'
    assert failed['data']['request'] == {'pod': POD}
    assert no_url.status == 400 and no_url_data['message'] == '请填写接口地址'
    assert bad_keys.status == 400 and bad_field.status == 400 and '测试入参 namespace' in bad_field_data['message']
    assert len(upstream.requests) == 1 and store.row is None


# ---------------------------------------------------------------------------
# 经过真实的鉴权中间件(policy.RULES)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_routes_behind_auth_middleware(monkeypatch, audits):
    from auth import middleware as auth_middleware

    monkeypatch.setenv('AI_INTERNAL_TOKEN', 'secret-test-token')
    users = {'writer': Principal('writer', 'rw', user_id=1, via='internal'),
             'reader': Principal('reader', 'read', user_id=2, via='internal')}

    async def principal_by_name(username, via):
        return users.get(username)

    monkeypatch.setattr(auth_middleware.store, 'principal_by_name', principal_by_name)
    upstream = Upstream(reply())
    async with serve(upstream) as url:
        monkeypatch.setattr(runtime_config, 'store', FakeStore({**AI_ON, 'url': url}))
        app = web.Application(middlewares=[auth_middleware.create_middleware()])
        runtime_config.register_routes(app)
        async with TestClient(TestServer(app)) as client:
            async def get(path, user, params=None):
                resp = await client.get(path, params=params, headers={
                    'X-Kubedoor-Token': 'secret-test-token', 'X-User-Name': user})
                return resp.status, await resp.json()

            results = [
                await get('/api/runtime-config', 'writer', QUERY),
                await get('/api/runtime-config', 'reader', QUERY),  # policy:只读没有「查看运行配置」
                await get('/api/runtime-config', 'writer', {'namespace': 'vip', 'deployment': 'app'}),  # 缺集群
                await get('/api/ai/internal/runtime-config', 'reader', QUERY),  # handler 按库里的权限判断
                await get('/api/admin/runtime-config', 'writer'),  # 配置只有系统管理员能看
            ]
    assert [(status, data.get('code')) for status, data in results] == [
        (200, None), (403, 'operation_forbidden'), (400, 'missing_env'), (403, 'permission_denied'),
        (403, 'admin_required')]
    assert results[0][1]['data']['config'] == CONFIG and len(upstream.requests) == 1
    # 查看不另记 api.write / api.denied:只有 handler 记的那一条
    assert [(record['action'], record['username']) for record in audits] == [('runtime_config.get', 'writer')]
