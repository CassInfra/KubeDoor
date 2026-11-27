"""v2 统一多通道通知系统集成测试: Provider 元数据、v1/v2 配置与密钥复用、SSRF、路由扇出、重试限速、审计日志与内部代理。"""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from loguru import logger

import db
from auth import policy
from auth.crypto import decrypt_secret, encrypt_secret
from auth.principal import SYSTEM, Principal
from kubedoor_tools.notify import ChannelConfig, DeliveryResult, build_event_message
from notify import api as notify_api
from notify import service

WECOM_TOKEN = 'a1b2c3d4-0000-4bc4-97a0-0ec2c3f5a1b2'
DING_TOKEN = '0123456789abcdef' * 4
WEBHOOK_AUTH = 'Bearer secret-webhook-token-9999'
WEBHOOK_HMAC = 'hmac-signing-secret-8888'
SMTP_PASS = 'smtp-app-password-7777'
ALL_SECRETS = (WECOM_TOKEN, DING_TOKEN, WEBHOOK_AUTH, 'secret-webhook-token-9999', WEBHOOK_HMAC, SMTP_PASS)

ADMIN = Principal('kubedoor', 'admin', user_id=1)
UPDATED_AT = datetime(2026, 11, 27, 10, 0, tzinfo=timezone.utc)


def _copy_json(value):
    import json
    return json.loads(json.dumps(value))


class FakeStore:
    def __init__(self, value=None):
        self.row = None if value is None else {'value': value, 'updated_by': 'kubedoor', 'updated_at': UPDATED_AT}
        self.puts = []
        self.error = None

    async def get_setting(self, key, default=None):
        row = await self.get_setting_row(key)
        return row['value'] if row else default

    async def get_setting_row(self, key):
        assert key == notify_api.SETTING_KEY
        if self.error is not None:
            raise self.error
        if self.row is None:
            return None
        return {**self.row, 'value': _copy_json(self.row['value'])}

    async def put_setting_row(self, key, value, updated_by):
        assert key == notify_api.SETTING_KEY
        if self.error is not None:
            raise self.error
        copied = _copy_json(value)
        self.puts.append(copied)
        self.row = {'value': copied, 'updated_by': updated_by, 'updated_at': UPDATED_AT}
        return {**self.row, 'value': _copy_json(self.row['value'])}


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    monkeypatch.setenv('AI_ENCRYPTION_KEY', '11' * 32)
    monkeypatch.setattr(service, '_snapshot', service.Snapshot())
    monkeypatch.setattr(service, '_summary', None)
    monkeypatch.setattr(service, '_refresh_failed', False)
    monkeypatch.setattr(service, '_undecryptable', set())
    monkeypatch.setattr(notify_api, '_last_test', {})
    monkeypatch.setattr(notify_api, '_save_lock', asyncio.Lock())
    with service._delivery_lock:
        service._delivery_ring.clear()
        service._delivery_seq = 0
    monkeypatch.setattr(db, '_pool', None)
    monkeypatch.setattr(db, '_loop', None)
    monkeypatch.setattr(db, 'pg_fetch', AsyncMock(return_value=[{'env': 'prod-k8s'}, {'env': 'test-k8s'}]))


@pytest.fixture
def audits(monkeypatch):
    records = []
    monkeypatch.setattr(
        notify_api.audit,
        'record_bg',
        lambda username, action, **kwargs: records.append({'username': username, 'action': action, **kwargs}),
    )
    return records


@pytest.fixture
def clock(monkeypatch):
    now = [2000.0]
    monkeypatch.setattr(notify_api, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    return now


def make_app(monkeypatch, store, principal=ADMIN):
    monkeypatch.setattr(notify_api, 'store', store)
    monkeypatch.setattr(service, 'store', store)
    who = {'principal': principal}

    @web.middleware
    async def inject(request, handler):
        request['principal'] = who['principal']
        return await handler(request)

    app = web.Application(middlewares=[inject])
    notify_api.register_routes(app)
    return app, who


def contains_secret(*objects):
    import json
    blob = json.dumps(objects, ensure_ascii=False, default=str)
    return any(secret in blob for secret in ALL_SECRETS)


# ---------------------------------------------------------------------------
# Policy & Providers Schema
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('method, path, kind', [
    ('GET', '/api/admin/notification/providers', 'admin'),
    ('GET', '/api/admin/notification/v2', 'admin'),
    ('PUT', '/api/admin/notification/v2', 'admin'),
    ('GET', '/api/admin/notification/deliveries', 'admin'),
    ('POST', '/api/internal/notify', 'internal'),
])
def test_policy_rules_v2(method, path, kind):
    assert policy.match(method, path).kind == kind


@pytest.mark.asyncio
async def test_get_providers_maps_web_schema_fields(monkeypatch):
    app, _ = make_app(monkeypatch, FakeStore())
    async with TestClient(TestServer(app)) as client:
        resp = await client.get('/api/admin/notification/providers')
        payload = await resp.json()

    assert resp.status == 200
    assert payload['success'] is True
    by_id = {p['id']: p for p in payload['data']}
    assert {'wecom', 'dingding', 'feishu', 'slack', 'webhook', 'smtp'} <= set(by_id)
    for schema in payload['data']:
        for field in schema['fields']:
            assert 'min_value' not in field
            assert 'max_value' not in field
            assert 'show_if' not in field
    smtp_fields = {f['key']: f for f in by_id['smtp']['fields']}
    assert smtp_fields['port']['min'] == 1
    assert smtp_fields['port']['max'] == 65535
    wecom_fields = {f['key']: f for f in by_id['wecom']['fields']}
    assert wecom_fields['custom_domain']['visible_when'] == {'domain_preset': 'custom'}


# ---------------------------------------------------------------------------
# GET / PUT /v2 + v1 迁移 + 秘密复用 + 密钥轮换保护 + SSRF 防护
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_get_v2_migrates_v1_cleanly_and_masks_secrets(monkeypatch):
    v1 = {
        'default': {'type': 'wecom', 'token': encrypt_secret(WECOM_TOKEN), 'at': 'zhangsan'},
        'clusters': {'prod-k8s': {'type': 'dingding', 'token': encrypt_secret(DING_TOKEN), 'at': '13800000000'}},
    }
    app, _ = make_app(monkeypatch, FakeStore(v1))
    async with TestClient(TestServer(app)) as client:
        resp = await client.get('/api/admin/notification/v2')
        payload = await resp.json()

    assert resp.status == 200
    data = payload['data']
    assert data['version'] == 2
    assert data['default_channel_ids'] == ['ch_default']
    assert data['envs'] == ['prod-k8s', 'test-k8s']
    by_id = {ch['id']: ch for ch in data['channels']}
    assert set(by_id) == {'ch_default', 'ch_cluster_prod-k8s'}
    assert by_id['ch_default']['configured'] is True
    assert by_id['ch_default']['configured_secrets'] == ['token']
    assert by_id['ch_default']['secret_hints']['token'].startswith('****')
    assert len(data['routes']) == 1
    assert data['routes'][0]['match']['envs'] == ['prod-k8s']
    assert not contains_secret(payload)


@pytest.mark.asyncio
async def test_put_v2_encrypts_applies_reuses_and_guards_key_rotation(monkeypatch, audits):
    store = FakeStore()
    app, _ = make_app(monkeypatch, store)
    req_v2 = {
        'version': 2,
        'channels': [
            {
                'id': 'ch_wecom',
                'name': '企微值班',
                'provider': 'wecom',
                'enabled': True,
                'settings': {'domain_preset': 'public', 'at': 'alice'},
                'secrets': {'token': f'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={WECOM_TOKEN}'},
            },
            {
                'id': 'ch_hook',
                'name': '告警总线',
                'provider': 'webhook',
                'enabled': True,
                'settings': {
                    'url': 'https://hooks.example.com/kubedoor/notify',
                    'allow_http': False,
                    'method': 'POST',
                    'auth_header_name': 'Authorization',
                    'hmac_header': 'X-KubeDoor-Signature-256',
                },
                'secrets': {'auth_header_value': WEBHOOK_AUTH, 'hmac_secret': WEBHOOK_HMAC},
            },
            {
                'id': 'ch_smtp',
                'name': '值班邮件',
                'provider': 'smtp',
                'enabled': True,
                'settings': {
                    'host': 'smtp.example.com',
                    'port': 465,
                    'tls_mode': 'ssl',
                    'username': 'bot@example.com',
                    'from_addr': 'bot@example.com',
                    'to_addrs': 'oncall@example.com',
                },
                'secrets': {'password': SMTP_PASS},
            },
        ],
        'routes': [
            {
                'id': 'rt_prod_fanout',
                'name': '生产告警双通道',
                'enabled': True,
                'match': {'envs': ['prod-k8s'], 'categories': ['metric_alert'], 'severities': ['critical']},
                'channel_ids': ['ch_wecom', 'ch_hook'],
                'continue': True,
            },
            {
                'id': 'rt_prod_mail',
                'name': '生产邮件兜底',
                'enabled': True,
                'match': {'envs': ['prod-k8s'], 'categories': [], 'severities': []},
                'channel_ids': ['ch_smtp'],
                'continue': False,
            },
        ],
        'default_channel_ids': ['ch_wecom'],
    }

    async with TestClient(TestServer(app)) as client:
        resp = await client.put('/api/admin/notification/v2', json=req_v2)
        body = await resp.json()
        assert resp.status == 200
        assert not contains_secret(body)
        by_id = {ch['id']: ch for ch in body['data']['channels']}
        assert by_id['ch_wecom']['secret_hints'] == {'token': '****a1b2'}
        assert sorted(by_id['ch_hook']['configured_secrets']) == ['auth_header_value', 'hmac_secret']

        # 数据库落盘已加密, 快照立即更新
        stored_hook = store.row['value']['channels']['ch_hook']['secrets']
        assert stored_hook['auth_header_value'] != WEBHOOK_AUTH
        assert decrypt_secret(stored_hook['auth_header_value']) == WEBHOOK_AUTH
        assert decrypt_secret(stored_hook['hmac_secret']) == WEBHOOK_HMAC
        assert service.is_loaded()
        targets = service.resolve_targets(env='prod-k8s', category='metric_alert', severity='critical')
        assert [t.id for t in targets] == ['ch_wecom', 'ch_hook', 'ch_smtp']
        assert audits and audits[-1]['action'] == notify_api.AUDIT_UPDATE
        assert not contains_secret(audits)

        # 留空凭据保存 -> 沿用已保存密文
        reuse_req = _copy_json(req_v2)
        reuse_req['channels'][0]['secrets'] = {'token': ''}
        reuse_req['channels'][1]['secrets'] = {'auth_header_value': '', 'hmac_secret': ''}
        reuse_req['channels'][2]['secrets'] = {'password': ''}
        resp2 = await client.put('/api/admin/notification/v2', json=reuse_req)
        assert resp2.status == 200
        assert store.row['value']['channels']['ch_hook']['secrets']['auth_header_value'] == stored_hook['auth_header_value']

        # 切换同 ID 通道的 provider 且留空必填凭据 -> 422
        switch_req = _copy_json(reuse_req)
        switch_req['channels'][0]['provider'] = 'dingding'
        resp3 = await client.put('/api/admin/notification/v2', json=switch_req)
        assert resp3.status == 422

        # 密钥轮换后,可选凭据无法解密且请求留空 -> 422 而不是静默清空
        monkeypatch.setenv('AI_ENCRYPTION_KEY', '22' * 32)
        resp4 = await client.put('/api/admin/notification/v2', json=reuse_req)
        assert resp4.status == 422


@pytest.mark.asyncio
@pytest.mark.parametrize('channel_payload', [
    {
        'id': 'ch_ssrf_loopback',
        'name': '回环攻击',
        'provider': 'webhook',
        'enabled': True,
        'settings': {'url': 'http://127.0.0.1:8080/internal', 'allow_http': True, 'method': 'POST'},
        'secrets': {},
    },
    {
        'id': 'ch_ssrf_meta',
        'name': '云元数据',
        'provider': 'webhook',
        'enabled': True,
        'settings': {'url': 'https://169.254.169.254/latest/meta-data', 'allow_http': False, 'method': 'POST'},
        'secrets': {},
    },
    {
        'id': 'ch_ssrf_smtp',
        'name': '内网 SMTP',
        'provider': 'smtp',
        'enabled': True,
        'settings': {
            'host': '127.0.0.1',
            'port': 25,
            'tls_mode': 'none',
            'from_addr': 'a@example.com',
            'to_addrs': 'b@example.com',
        },
        'secrets': {},
    },
    {
        'id': 'ch_ssrf_custom_im',
        'name': '私网自定义企微',
        'provider': 'wecom',
        'enabled': True,
        'settings': {'domain_preset': 'custom', 'custom_domain': 'https://10.0.0.5', 'at': ''},
        'secrets': {'token': WECOM_TOKEN},
    },
])
async def test_put_v2_and_test_v2_block_ssrf_without_outbound_call(monkeypatch, channel_payload):
    called = []
    monkeypatch.setattr(service.requests, 'post', lambda *a, **kw: called.append(a))
    monkeypatch.setattr(service.requests, 'request', lambda *a, **kw: called.append(a))

    app, _ = make_app(monkeypatch, FakeStore())
    async with TestClient(TestServer(app)) as client:
        put_resp = await client.put('/api/admin/notification/v2', json={
            'version': 2,
            'channels': [channel_payload],
            'routes': [],
            'default_channel_ids': [],
        })
        assert put_resp.status == 422
        put_body = await put_resp.json()
        assert put_body['code'] in ('ssrf_blocked', 'invalid_config')

        test_resp = await client.post('/api/admin/notification/test', json={
            'provider': channel_payload['provider'],
            'name': channel_payload['name'],
            'settings': channel_payload['settings'],
            'secrets': channel_payload['secrets'],
        })
        assert test_resp.status == 422
    assert called == []


# ---------------------------------------------------------------------------
# 重试退避 & SMTP 直连 & 投递审计 (内存回退 + PG 查询 + TTL 清理)
# ---------------------------------------------------------------------------
def test_dispatcher_retries_retryable_only_and_honors_retry_after(monkeypatch):
    now = [100.0]

    class InlineExecutor:
        def submit(self, fn, *args, **kwargs):
            fn(*args, **kwargs)

    dispatcher = service.Dispatcher(
        InlineExecutor(),
        rate_limit=100,
        window=60,
        clock=lambda: now[0],
        jitter=0.0,
    )
    monkeypatch.setattr(dispatcher, '_start', lambda: None)

    # 1) 429 retry_after=3 -> 503 (2 * 3^1 = 6s 退避) -> 200 OK (共 3 次尝试)
    responses = [
        SimpleNamespace(status_code=429, headers={'Retry-After': '3'}, text='rate limited'),
        SimpleNamespace(status_code=503, headers={}, text='upstream busy'),
        SimpleNamespace(status_code=200, headers={}, text='{"ok":true}'),
    ]
    calls = []

    def fake_request(method, url, headers=None, data=None, timeout=None, allow_redirects=False):
        calls.append(url)
        return responses.pop(0)

    monkeypatch.setattr(service.requests, 'request', fake_request)
    ch = ChannelConfig(
        id='c1',
        name='hook1',
        provider='webhook',
        settings={'url': 'https://hooks.example.com/a', 'method': 'POST'},
        secrets={},
    )
    msg = build_event_message('标题', '正文', env='prod', category='ops', severity='info')

    dispatcher.submit(ch, msg, 'prod', max_attempts=3, trace_id='trace-retry')
    with dispatcher._cond:
        assert dispatcher._dispatch([]) is None  # attempt 1 -> 429 (retry_after=3s)
        wait1 = dispatcher._dispatch([])
        assert round(wait1, 2) == 3.0
        now[0] += wait1
        dispatcher._dispatch([])  # attempt 2 -> 503 (backoff=6s)
        wait2 = dispatcher._dispatch([])
        assert round(wait2, 2) == 6.0
        now[0] += wait2
        dispatcher._dispatch([])  # attempt 3 -> 200 OK

    assert len(calls) == 3
    assert service._delivery_ring[-1]['trace_id'] == 'trace-retry'
    assert service._delivery_ring[-1]['attempts'] == 3
    assert service._delivery_ring[-1]['ok'] is True

    # 2) HTTP 400 不可重试 -> 只试 1 次且不触发退避等待
    calls.clear()
    responses.append(SimpleNamespace(status_code=400, headers={}, text='bad request'))
    dispatcher.submit(ch, msg, 'prod', max_attempts=3, trace_id='trace-400')
    with dispatcher._cond:
        dispatcher._dispatch([])
        assert dispatcher.pending() == 0
    assert len(calls) == 1
    assert service._delivery_ring[-1]['trace_id'] == 'trace-400'
    assert service._delivery_ring[-1]['attempts'] == 1
    assert service._delivery_ring[-1]['ok'] is False


def test_smtp_direct_delivery_records_redacted_audit(monkeypatch):
    ch = ChannelConfig(
        id='smtp1',
        name='邮箱告警',
        provider='smtp',
        settings={'host': 'smtp.example.com', 'port': 465, 'from_addr': 'a@example.com', 'to_addrs': 'b@example.com'},
        secrets={'password': SMTP_PASS},
    )
    provider = service.get_provider('smtp')
    monkeypatch.setattr(
        provider,
        'send_direct',
        lambda cfg, m: DeliveryResult(ok=False, message=f'auth failed with {SMTP_PASS}', provider_code='535'),
    )
    msg = build_event_message('故障', '详细内容', env='prod-k8s', category='metric_alert', severity='critical')
    res = service.deliver_message(ch, msg, trace_id='trace-smtp-1')
    assert res.ok is False
    assert SMTP_PASS not in res.message
    assert '***' in res.message
    assert service._delivery_ring[-1]['trace_id'] == 'trace-smtp-1'
    assert SMTP_PASS not in service._delivery_ring[-1]['error_summary']


@pytest.mark.asyncio
async def test_deliveries_endpoint_ring_fallback_filtering_and_pg_cleanup(monkeypatch, clock, audits):
    # 通过 POST /test (v2) 写入一条失败和一条成功投递记录(同时测空白秘密复用 + 限速)
    saved_v2 = {
        'version': 2,
        'channels': {
            'ch_hook': {
                'id': 'ch_hook',
                'name': 'Webhook通道',
                'provider': 'webhook',
                'enabled': True,
                'settings': {'url': 'https://hooks.example.com/test', 'method': 'POST', 'auth_header_name': 'Authorization'},
                'secrets': {'auth_header_value': encrypt_secret(WEBHOOK_AUTH)},
            }
        },
        'routes': [],
        'default_channel_ids': ['ch_hook'],
    }
    sent_headers = []

    def fake_request(method, url, headers=None, data=None, timeout=None, allow_redirects=False):
        sent_headers.append(dict(headers or {}))
        if len(sent_headers) == 1:
            return SimpleNamespace(status_code=200, headers={}, text='ok')
        return SimpleNamespace(status_code=500, headers={}, text=f'leak {WEBHOOK_AUTH}')

    monkeypatch.setattr(service.requests, 'request', fake_request)
    app, _ = make_app(monkeypatch, FakeStore(saved_v2))

    async with TestClient(TestServer(app)) as client:
        test_req = {
            'channel_id': 'ch_hook',
            'provider': 'webhook',
            'name': 'Webhook通道',
            'settings': {'url': 'https://hooks.example.com/test', 'method': 'POST', 'auth_header_name': 'Authorization'},
            'secrets': {'auth_header_value': ''},
            'env': 'prod-k8s',
        }
        r1 = await client.post('/api/admin/notification/test', json=test_req)
        assert r1.status == 200
        assert (await r1.json())['data'] == {'ok': True, 'message': '发送成功'}
        assert sent_headers[0]['Authorization'] == WEBHOOK_AUTH

        # 3 秒限速
        r_429 = await client.post('/api/admin/notification/test', json=test_req)
        assert r_429.status == 429

        clock[0] += 3.1
        r2 = await client.post('/api/admin/notification/test', json={**test_req, 'env': 'test-k8s'})
        assert r2.status == 200
        d2 = (await r2.json())['data']
        assert d2['ok'] is False and WEBHOOK_AUTH not in d2['message']

        # 查 deliveries (无 PG 池走内存环形缓冲区回退)
        d_all = await (await client.get('/api/admin/notification/deliveries?provider=webhook')).json()
        assert d_all['success'] is True
        assert d_all['data']['total'] == 2
        assert len(d_all['data']['items']) == 2
        assert not contains_secret(d_all)

        d_fail = await (await client.get('/api/admin/notification/deliveries?ok=false&env=test-k8s&limit=10')).json()
        assert d_fail['data']['total'] == 1
        assert d_fail['data']['items'][0]['env'] == 'test-k8s'

    # 30 天 TTL 清理批处理
    calls = []

    async def fake_exec(sql, days, batch):
        calls.append((days, batch))
        return 'DELETE 5000' if len(calls) == 1 else 'DELETE 12'

    monkeypatch.setattr(db, 'pg_execute', fake_exec)
    deleted = await service.cleanup_deliveries(days=30, batch_size=5000)
    assert deleted == 5012
    assert calls == [(30, 5000), (30, 5000)]


# ---------------------------------------------------------------------------
# POST /api/internal/notify + deprecated GET /api/internal/notification + save_cluster_channel v2
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_internal_notify_auth_503_unmatched_and_sync(monkeypatch):
    v2_saved = {
        'version': 2,
        'channels': {
            'ch_a': {
                'id': 'ch_a',
                'name': '企微',
                'provider': 'wecom',
                'enabled': True,
                'settings': {'domain_preset': 'public', 'at': 'zhangsan'},
                'secrets': {'token': encrypt_secret(WECOM_TOKEN)},
            },
            'ch_b': {
                'id': 'ch_b',
                'name': 'Webhook',
                'provider': 'webhook',
                'enabled': True,
                'settings': {'url': 'https://hooks.example.com/alerts', 'method': 'POST'},
                'secrets': {},
            },
        },
        'routes': [
            {
                'id': 'rt_metric_prod',
                'name': '生产指标多通道',
                'enabled': True,
                'match': {'envs': ['prod-k8s'], 'categories': ['metric_alert'], 'severities': []},
                'channel_ids': ['ch_a', 'ch_b'],
                'continue': False,
            },
            {
                'id': 'rt_cluster_ops',
                'name': '集群全分类兜底',
                'enabled': True,
                'match': {'envs': ['ops-k8s'], 'categories': [], 'severities': []},
                'channel_ids': ['ch_a'],
                'continue': False,
            },
        ],
        'default_channel_ids': [],
    }
    sent = []

    def fake_request(method, url, headers=None, data=None, timeout=None, allow_redirects=False):
        sent.append({'url': url, 'data': data})
        return SimpleNamespace(status_code=200, headers={}, text='{"errcode":0,"errmsg":"ok"}')

    monkeypatch.setattr(service.requests, 'request', fake_request)
    store = FakeStore(v2_saved)
    app, who = make_app(monkeypatch, store, principal=Principal('alice', 'internal'))

    async with TestClient(TestServer(app)) as client:
        # 1) 用户委托 internal 调用 (via='internal') -> 必须 403
        r_403 = await client.post('/api/internal/notify', json={'env': 'prod-k8s', 'title': 'hi', 'body': 'test'})
        assert r_403.status == 403

        # 2) system 身份但在未加载且 DB 失败时 -> 503
        who['principal'] = SYSTEM
        store.error = RuntimeError('pg down')
        r_503 = await client.post('/api/internal/notify', json={'env': 'prod-k8s', 'title': 'hi', 'body': 'test'})
        assert r_503.status == 503
        store.error = None

        # 3) system 身份发送干净结构化指标告警 (sync=True)
        items = [{
            'status': 'firing',
            'summary': 'K8S_Pod_Restart,已持续5分钟',
            'description': 'pod web-1 restarted',
            'silence_url': 'https://kd.example.com/#/alarm/silence?prefill=1',
            'severity': 'critical',
            'labels': {'alertname': 'K8S_Pod_Restart'},
        }]
        r_sync = await client.post('/api/internal/notify', json={
            'env': 'prod-k8s',
            'category': 'metric_alert',
            'severity': 'critical',
            'at': 'lisi',
            'items': items,
            'links': [{'label': '屏蔽', 'url': 'https://kd.example.com/#/alarm/silence?prefill=1'}],
            'sync': True,
        })
        assert r_sync.status == 200
        d_sync = (await r_sync.json())['data']
        assert d_sync['matched'] == 2
        assert d_sync['queued'] == 2
        assert d_sync['trace_id']
        assert len(d_sync['results']) == 2
        assert all(item['ok'] for item in d_sync['results'])
        assert len(sent) == 2
        assert '<font' not in sent[0]['data'].decode('utf-8')
        assert '<font' not in sent[1]['data'].decode('utf-8')

        # 4) 未匹配到任何通道 (test-k8s, 无默认兜底) -> matched: 0
        r_none = await client.post('/api/internal/notify', json={
            'env': 'test-k8s',
            'category': 'metric_alert',
            'items': items,
        })
        assert r_none.status == 200
        assert (await r_none.json())['data']['matched'] == 0

        # 5) 异步提交 (sync=False)
        submitted = []
        monkeypatch.setattr(
            service._dispatcher,
            'submit',
            lambda ch, m, sc, **kw: submitted.append((ch.id, m.category, kw.get('trace_id'))),
        )
        r_async = await client.post('/api/internal/notify', json={
            'env': 'prod-k8s',
            'category': 'metric_alert',
            'items': items,
        })
        assert r_async.status == 200
        d_async = (await r_async.json())['data']
        assert d_async['matched'] == 2 and d_async['queued'] == 2
        assert [cid for cid, _, _ in submitted] == ['ch_a', 'ch_b']

        # 6) 旧版 GET /api/internal/notification 返回 Deprecation: true (映射全分类集群路由)
        r_dep = await client.get('/api/internal/notification?env=ops-k8s')
        assert r_dep.status == 200
        assert r_dep.headers.get('Deprecation') == 'true'
        assert r_dep.headers.get('Cache-Control') == 'no-store'
        assert (await r_dep.json())['data'] == {'type': 'wecom', 'token': WECOM_TOKEN, 'at': 'zhangsan'}


@pytest.mark.asyncio
async def test_save_cluster_channel_preserves_v2_channels_and_routes(monkeypatch, audits):
    existing_v2 = {
        'version': 2,
        'channels': {
            'ch_hook': {
                'id': 'ch_hook',
                'name': '扩展 Webhook',
                'provider': 'webhook',
                'enabled': True,
                'settings': {'url': 'https://hooks.example.com/audit', 'method': 'POST'},
                'secrets': {},
            }
        },
        'routes': [
            {
                'id': 'rt_ops',
                'name': '运维审计',
                'enabled': True,
                'match': {'envs': [], 'categories': ['ops'], 'severities': []},
                'channel_ids': ['ch_hook'],
                'continue': False,
            }
        ],
        'default_channel_ids': ['ch_hook'],
    }
    store = FakeStore(existing_v2)
    monkeypatch.setattr(notify_api, 'store', store)
    monkeypatch.setattr(service, 'store', store)

    await notify_api.save_cluster_channel('edge-k8s', {'type': 'dingding', 'token': DING_TOKEN, 'at': '13800000000'}, 'kubedoor')

    saved = store.row['value']
    assert saved['version'] == 2
    assert 'ch_hook' in saved['channels']
    assert 'ch_cluster_edge-k8s' in saved['channels']
    assert any(r['id'] == 'rt_ops' for r in saved['routes'])
    assert any(r['id'] == 'rt_cluster_edge-k8s' for r in saved['routes'])
    assert audits[-1]['detail']['source'] == 'agent-install'


@pytest.mark.asyncio
async def test_deliveries_pg_query_branch(monkeypatch):
    seen_sql = []

    async def fake_fetchval(sql, *params):
        seen_sql.append(('val', sql, params))
        return 1

    async def fake_fetch(sql, *params):
        seen_sql.append(('rows', sql, params))
        return [{
            'id': 42,
            'trace_id': 'tr-pg',
            'env': 'prod-k8s',
            'category': 'metric_alert',
            'severity': 'critical',
            'channel_id': 'ch_1',
            'channel_name': '企微',
            'provider': 'wecom',
            'ok': True,
            'attempts': 1,
            'http_status': 200,
            'error_summary': '',
            'latency_ms': 38,
            'created_at': UPDATED_AT,
        }]

    monkeypatch.setattr(db, 'pg_fetchval', fake_fetchval)
    monkeypatch.setattr(db, 'pg_fetch', fake_fetch)
    app, _ = make_app(monkeypatch, FakeStore())

    async with TestClient(TestServer(app)) as client:
        resp = await client.get('/api/admin/notification/deliveries?env=prod-k8s&provider=wecom&ok=true&limit=20&offset=5')
        data = await resp.json()

    assert resp.status == 200
    assert data['data']['total'] == 1
    assert data['data']['items'][0]['id'] == 42
    assert data['data']['items'][0]['message'] == '发送成功'
    assert seen_sql[0][2] == ('prod-k8s', 'wecom', True)
    assert seen_sql[1][2] == ('prod-k8s', 'wecom', True, 20, 5)
