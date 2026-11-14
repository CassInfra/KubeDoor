"""消息通知的接口(notify/api.py):GET / PUT / 测试 / 内部接口,以及 policy 登记和启停。"""
import asyncio
import json
import threading
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
from notify import api as notify_api
from notify import channels, service

WECOM = 'a1b2c3d4-0000-4bc4-97a0-0ec2c3f5a1b2'
WECOM_URL = f'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={WECOM}'
DING = '0123456789abcdef' * 4
FEISHU = '3f8c1a2b-1111-2222-3333-444455556666'
ADMIN = Principal('kubedoor', 'admin', user_id=1)
UPDATED_AT = datetime(2026, 11, 7, 4, 0, tzinfo=timezone.utc)
UPDATED_TEXT = UPDATED_AT.astimezone().isoformat()
SECRETS = (WECOM, DING, FEISHU)


def stored(kind, token, at=''):
    return {'type': kind, 'token': encrypt_secret(token), 'at': at}


def saved_config():
    return {'default': stored('wecom', WECOM, 'zhangsan'),
            'clusters': {'prod-k8s': stored('dingding', DING, '13800000000')}}


class FakeStore:
    """auth_settings 里只有 notification 这一行(读写都做一次 JSON 往返,和数据库一样)"""

    def __init__(self, value=None):
        self.row = None if value is None else {'value': value, 'updated_by': 'kubedoor', 'updated_at': UPDATED_AT}
        self.puts = []
        self.error = None
        self.put_error = None

    def _copy(self):
        if self.row is None:
            return None
        return {**self.row, 'value': json.loads(json.dumps(self.row['value']))}

    async def get_setting(self, key, default=None):
        row = await self.get_setting_row(key)
        return row['value'] if row else default

    async def get_setting_row(self, key):
        assert key == notify_api.SETTING_KEY
        if self.error is not None:
            raise self.error
        return self._copy()

    async def put_setting_row(self, key, value, updated_by):
        assert key == notify_api.SETTING_KEY
        if self.error is not None or self.put_error is not None:
            raise self.error or self.put_error
        self.puts.append(json.loads(json.dumps(value)))
        self.row = {'value': json.loads(json.dumps(value)), 'updated_by': updated_by, 'updated_at': UPDATED_AT}
        return self._copy()


class FakeIM:
    def __init__(self, status=200, text='{"errcode":0,"errmsg":"ok"}'):
        self.status, self.text, self.calls = status, text, []

    def __call__(self, url, json=None, timeout=None, allow_redirects=True, **kwargs):
        self.calls.append({'url': url, 'json': json, 'timeout': timeout})
        return SimpleNamespace(status_code=self.status, text=self.text)


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    monkeypatch.setenv('AI_ENCRYPTION_KEY', '11' * 32)
    monkeypatch.setattr(service, '_snapshot', service.Snapshot())
    monkeypatch.setattr(service, '_summary', None)
    monkeypatch.setattr(service, '_refresh_failed', False)
    monkeypatch.setattr(service, '_undecryptable', set())
    monkeypatch.setattr(notify_api, '_last_test', {})
    # 每个用例一把新锁:锁在有人等待时会绑定当时的事件循环
    monkeypatch.setattr(notify_api, '_save_lock', asyncio.Lock())
    monkeypatch.setattr(db, 'pg_fetch', AsyncMock(return_value=[{'env': 'test-k8s'}, {'env': 'prod-k8s'}, {'env': ''}]))


@pytest.fixture
def audits(monkeypatch):
    records = []
    monkeypatch.setattr(notify_api.audit, 'record_bg',
                        lambda username, action, **kwargs: records.append({'username': username, 'action': action, **kwargs}))
    return records


@pytest.fixture
def im(monkeypatch):
    fake = FakeIM()
    monkeypatch.setattr(service.requests, 'post', fake)
    return fake


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(notify_api, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    return now


@pytest.fixture
def logs():
    lines = []
    handler_id = logger.add(lambda message: lines.append(str(message)), level='DEBUG')
    yield lines
    logger.remove(handler_id)


def make_app(monkeypatch, store, principal=ADMIN):
    """返回 (app, who):who['principal'] 可以在用例里换成别的账号"""
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


def no_secret(*values):
    text = json.dumps(values, ensure_ascii=False, default=str)
    return not any(secret in text for secret in SECRETS)


# ---------------------------------------------------------------------------
# policy 登记
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('method, path, kind', [
    ('GET', '/api/admin/notification', 'admin'),
    ('PUT', '/api/admin/notification', 'admin'),
    ('POST', '/api/admin/notification/test', 'admin'),
    ('GET', '/api/internal/notification', 'internal'),
])
def test_policy_rules(method, path, kind):
    assert policy.match(method, path).kind == kind


# ---------------------------------------------------------------------------
# GET
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_get_without_config(monkeypatch):
    app, _ = make_app(monkeypatch, FakeStore())
    async with TestClient(TestServer(app)) as client:
        resp = await client.get('/api/admin/notification')
        data = await resp.json()
    assert resp.status == 200
    assert data == {'success': True, 'data': {'default': None, 'clusters': [], 'envs': ['prod-k8s', 'test-k8s'],
                                              'updated_by': None, 'updated_at': None}}


@pytest.mark.asyncio
async def test_get_shows_only_hints(monkeypatch):
    value = saved_config()
    value['clusters']['a-k8s'] = stored('feishu', FEISHU)
    value['clusters']['bad'] = {'type': 'qq', 'token': 'x'}  # 类型不对的条目不显示
    app, _ = make_app(monkeypatch, FakeStore(value))
    async with TestClient(TestServer(app)) as client:
        data = await (await client.get('/api/admin/notification')).json()
    assert data['data'] == {
        'default': {'type': 'wecom', 'at': 'zhangsan', 'configured': True, 'token_hint': '****a1b2'},
        'clusters': [
            {'env': 'a-k8s', 'type': 'feishu', 'at': '', 'configured': True, 'token_hint': '****6666'},
            {'env': 'prod-k8s', 'type': 'dingding', 'at': '13800000000', 'configured': True, 'token_hint': '****cdef'},
        ],
        'envs': ['prod-k8s', 'test-k8s'], 'updated_by': 'kubedoor', 'updated_at': UPDATED_TEXT,
    }
    assert no_secret(data)


@pytest.mark.asyncio
async def test_get_marks_undecryptable_token_as_not_configured(monkeypatch):
    monkeypatch.setenv('AI_ENCRYPTION_KEY', '22' * 32)
    value = {'default': stored('wecom', WECOM)}
    monkeypatch.setenv('AI_ENCRYPTION_KEY', '11' * 32)  # 密钥换过
    app, _ = make_app(monkeypatch, FakeStore(value))
    async with TestClient(TestServer(app)) as client:
        data = await (await client.get('/api/admin/notification')).json()
    assert data['data']['default'] == {'type': 'wecom', 'at': '', 'configured': False, 'token_hint': ''}


@pytest.mark.asyncio
async def test_get_and_put_report_database_errors(monkeypatch, audits):
    store = FakeStore()
    store.error = RuntimeError('db down')
    app, _ = make_app(monkeypatch, store)
    async with TestClient(TestServer(app)) as client:
        get = await client.get('/api/admin/notification')
        put = await client.put('/api/admin/notification', json={'default': None, 'clusters': []})
        body = await put.json()
    assert get.status == 503 and put.status == 503 and body['code'] == 'db_unavailable'
    assert audits == []


@pytest.mark.asyncio
async def test_envs_are_empty_when_agent_table_unreadable(monkeypatch):
    monkeypatch.setattr(db, 'pg_fetch', AsyncMock(side_effect=RuntimeError('db down')))
    app, _ = make_app(monkeypatch, FakeStore())
    async with TestClient(TestServer(app)) as client:
        data = await (await client.get('/api/admin/notification')).json()
    assert data['success'] is True and data['data']['envs'] == []


@pytest.mark.asyncio
async def test_save_failure_and_cluster_limit(monkeypatch, audits, im, clock):
    store = FakeStore(saved_config())
    store.put_error = RuntimeError('db down')
    monkeypatch.setattr(notify_api, 'MAX_CLUSTERS', 2)
    app, _ = make_app(monkeypatch, store)
    clusters = [{'env': f'k8s-{index}', 'type': 'wecom', 'token': WECOM} for index in range(3)]
    async with TestClient(TestServer(app)) as client:
        failed = await client.put('/api/admin/notification', json={'default': None, 'clusters': clusters[:2]})
        failed_data = await failed.json()
        too_many = await client.put('/api/admin/notification', json={'clusters': clusters})
        too_many_data = await too_many.json()
        store.error = RuntimeError('db down')
        test = await client.post('/api/admin/notification/test', json={'type': 'wecom', 'token': WECOM})
    assert failed.status == 503 and failed_data['code'] == 'db_unavailable'
    assert service.resolve('k8s-0') is None  # 没保存成功就不生效
    assert too_many.status == 422 and '最多 2 个' in too_many_data['message']
    assert test.status == 503 and im.calls == [] and audits == []


def test_rate_limit_table_is_pruned(monkeypatch, clock):
    monkeypatch.setattr(notify_api, '_last_test', {f'user{index}': 0.0 for index in range(1001)})
    assert notify_api._test_wait('kubedoor') == 0
    assert notify_api._last_test == {'kubedoor': 1000.0}


# ---------------------------------------------------------------------------
# PUT
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_put_encrypts_tokens_and_takes_effect_immediately(monkeypatch, audits):
    store = FakeStore()
    app, _ = make_app(monkeypatch, store)
    body = {'default': {'type': 'wecom', 'token': f' {WECOM_URL} ', 'at': ' zhangsan '},
            'clusters': [{'env': 'prod-k8s', 'type': 'dingding', 'token': DING, 'at': ''}]}
    async with TestClient(TestServer(app)) as client:
        resp = await client.put('/api/admin/notification', json=body)
        data = await resp.json()
        again = await (await client.get('/api/admin/notification')).json()
    assert resp.status == 200 and data['success'] is True
    assert data['data'] == {
        'default': {'type': 'wecom', 'at': 'zhangsan', 'configured': True, 'token_hint': '****a1b2'},
        'clusters': [{'env': 'prod-k8s', 'type': 'dingding', 'at': '', 'configured': True, 'token_hint': '****cdef'}],
        'envs': ['prod-k8s', 'test-k8s'], 'updated_by': 'kubedoor', 'updated_at': UPDATED_TEXT,
    }
    assert again == data
    saved = store.row['value']
    assert saved['default']['token'].startswith('v1:') and decrypt_secret(saved['default']['token']) == WECOM
    assert decrypt_secret(saved['clusters']['prod-k8s']['token']) == DING
    assert no_secret(saved, data)
    # 保存后马上生效,不等后台刷新
    assert service.resolve('prod-k8s') == {'type': 'dingding', 'token': DING, 'at': ''}
    assert service.resolve('test-k8s') == {'type': 'wecom', 'token': WECOM, 'at': 'zhangsan'}
    [record] = audits
    assert record['action'] == 'notification.update' and record['username'] == 'kubedoor'
    assert record['detail'] == {'default': 'added', 'clusters': {'added': ['prod-k8s'], 'updated': [], 'removed': []}}
    assert no_secret(audits)


@pytest.mark.asyncio
async def test_blank_token_keeps_saved_token(monkeypatch, audits):
    store = FakeStore(saved_config())
    before = json.loads(json.dumps(store.row['value']))
    app, _ = make_app(monkeypatch, store)
    body = {'default': {'type': 'wecom', 'token': '', 'at': 'lisi'},
            'clusters': [{'env': 'prod-k8s', 'type': 'dingding', 'token': '  ', 'at': '13800000000'}]}
    async with TestClient(TestServer(app)) as client:
        first = await client.put('/api/admin/notification', json=body)
        second = await client.put('/api/admin/notification', json=body)
        # token 不传(null)也算留空
        third = await client.put('/api/admin/notification', json={
            'default': {'type': 'wecom', 'token': None, 'at': 'lisi'},
            'clusters': [{'env': 'prod-k8s', 'type': 'dingding', 'at': '13800000000'}]})
    assert first.status == second.status == third.status == 200
    saved = store.row['value']
    assert saved['default'] == {**before['default'], 'at': 'lisi'}  # 密文原样保留
    assert saved['clusters'] == before['clusters']
    assert [record['detail'] for record in audits] == [
        {'default': 'updated', 'clusters': {'added': [], 'updated': [], 'removed': []}},
        {'default': 'unchanged', 'clusters': {'added': [], 'updated': [], 'removed': []}},
        {'default': 'unchanged', 'clusters': {'added': [], 'updated': [], 'removed': []}},
    ]


@pytest.mark.asyncio
async def test_new_token_with_same_value_is_not_a_change(monkeypatch, audits):
    """重新填了同一个 token(密文每次不同):按明文比较,不算修改;@ 人变了才算"""
    store = FakeStore(saved_config())
    app, _ = make_app(monkeypatch, store)
    async with TestClient(TestServer(app)) as client:
        await client.put('/api/admin/notification', json={
            'default': {'type': 'wecom', 'token': WECOM, 'at': 'zhangsan'},
            'clusters': [{'env': 'prod-k8s', 'type': 'dingding', 'token': DING, 'at': '1'}]})
    assert audits[0]['detail'] == {'default': 'unchanged',
                                   'clusters': {'added': [], 'updated': ['prod-k8s'], 'removed': []}}


@pytest.mark.asyncio
@pytest.mark.parametrize('saved, body, message', [
    # 没有已保存的 token
    (None, {'default': {'type': 'wecom', 'token': '', 'at': ''}}, '全局配置:请填写企业微信机器人地址'),
    # IM 类型换了
    (saved_config, {'default': {'type': 'dingding', 'token': '', 'at': ''}},
     '全局配置:IM 类型从企业微信改成了钉钉,请重新填写机器人地址'),
    (saved_config, {'clusters': [{'env': 'prod-k8s', 'type': 'feishu', 'token': ''}]},
     '集群 prod-k8s:IM 类型从钉钉改成了飞书,请重新填写机器人地址'),
    # 新加的集群必须填
    (saved_config, {'clusters': [{'env': 'new-k8s', 'type': 'feishu', 'token': ''}]}, '集群 new-k8s:请填写飞书机器人地址'),
    # 地址 / 类型 / @ 人不合法
    (None, {'default': {'type': 'qq', 'token': WECOM}}, '全局配置:请选择 IM 类型'),
    (None, {'default': {'type': 'wecom', 'token': 'https://example.com/hook'}}, '全局配置:不认识的机器人地址'),
    (None, {'default': {'type': 'wecom', 'token': WECOM, 'at': 'x' * 129}}, '全局配置:默认 @ 人最多 128 个字符'),
    (None, {'default': 'wecom'}, '全局配置的格式不正确'),
    # 集群名称
    (None, {'clusters': [{'env': 'bad env', 'type': 'wecom', 'token': WECOM}]}, '集群名称不正确:bad env'),
    (None, {'clusters': [{'env': '', 'type': 'wecom', 'token': WECOM}]}, '集群名称不正确:(空)'),
    (None, {'clusters': [{'env': 'a' * 101, 'type': 'wecom', 'token': WECOM}]}, '集群名称不正确'),
    (None, {'clusters': [{'env': 'prod/x', 'type': 'wecom', 'token': WECOM}]}, '集群名称不正确'),
    (None, {'clusters': [{'type': 'wecom', 'token': WECOM}]}, '集群名称不正确'),
    (None, {'clusters': [{'env': 'prod-k8s', 'type': 'wecom', 'token': WECOM},
                         {'env': 'prod-k8s', 'type': 'dingding', 'token': DING}]}, '集群 prod-k8s 重复了'),
    (None, {'clusters': 'prod-k8s'}, '按集群单独配置(clusters)应该是列表'),
    (None, {'clusters': ['prod-k8s']}, '按集群单独配置的格式不正确'),
])
async def test_put_rejects_invalid_config(monkeypatch, audits, saved, body, message):
    store = FakeStore(saved() if saved else None)
    before = store._copy()
    app, _ = make_app(monkeypatch, store)
    async with TestClient(TestServer(app)) as client:
        resp = await client.put('/api/admin/notification', json=body)
        data = await resp.json()
    assert resp.status == 422 and data['success'] is False and data['code'] == 'invalid_config'
    assert data['message'].startswith(message), data['message']
    assert store.puts == [] and store._copy() == before and audits == []


@pytest.mark.asyncio
async def test_put_rejects_undecryptable_saved_token(monkeypatch, audits):
    monkeypatch.setenv('AI_ENCRYPTION_KEY', '22' * 32)
    value = {'default': stored('wecom', WECOM)}
    monkeypatch.setenv('AI_ENCRYPTION_KEY', '11' * 32)
    app, _ = make_app(monkeypatch, FakeStore(value))
    async with TestClient(TestServer(app)) as client:
        resp = await client.put('/api/admin/notification', json={'default': {'type': 'wecom', 'token': ''}})
        data = await resp.json()
    assert resp.status == 422 and '无法解密' in data['message']


@pytest.mark.asyncio
@pytest.mark.parametrize('raw', ['not json', '[1, 2]', '"text"'])
async def test_put_requires_json_object(monkeypatch, audits, raw):
    app, _ = make_app(monkeypatch, FakeStore())
    async with TestClient(TestServer(app)) as client:
        resp = await client.put('/api/admin/notification', data=raw, headers={'Content-Type': 'application/json'})
    assert resp.status == 400 and audits == []


@pytest.mark.asyncio
async def test_clusters_are_replaced_as_a_whole(monkeypatch, audits):
    value = saved_config()
    value['clusters']['test-k8s'] = stored('feishu', FEISHU)
    store = FakeStore(value)
    app, _ = make_app(monkeypatch, store)
    async with TestClient(TestServer(app)) as client:
        resp = await client.put('/api/admin/notification', json={
            'default': {'type': 'wecom', 'token': ''},
            'clusters': [{'env': ' dev-k8s ', 'type': 'feishu', 'token': FEISHU, 'at': 'ou_1'}]})
        data = await resp.json()
        cleared = await client.put('/api/admin/notification', json={'default': {'type': 'wecom', 'token': ''},
                                                                     'clusters': []})
    assert resp.status == 200
    assert [item['env'] for item in data['data']['clusters']] == ['dev-k8s']
    assert audits[0]['detail'] == {'default': 'updated',  # at 从 zhangsan 改成了空
                                   'clusters': {'added': ['dev-k8s'], 'updated': [], 'removed': ['prod-k8s', 'test-k8s']}}
    assert cleared.status == 200 and store.row['value']['clusters'] == {}
    assert audits[1]['detail']['clusters'] == {'added': [], 'updated': [], 'removed': ['dev-k8s']}
    # 删掉单独配置的集群改用全局配置
    assert service.resolve('prod-k8s') == {'type': 'wecom', 'token': WECOM, 'at': ''}


@pytest.mark.asyncio
async def test_default_null_deletes_global_config(monkeypatch, audits):
    store = FakeStore(saved_config())
    app, _ = make_app(monkeypatch, store)
    async with TestClient(TestServer(app)) as client:
        resp = await client.put('/api/admin/notification', json={
            'default': None, 'clusters': [{'env': 'prod-k8s', 'type': 'dingding', 'token': '', 'at': ''}]})
        data = await resp.json()
    assert resp.status == 200 and data['data']['default'] is None
    assert 'default' not in store.row['value']
    assert service.resolve('other-k8s') is None and service.resolve('prod-k8s')['token'] == DING
    assert audits[0]['detail'] == {'default': 'removed', 'clusters': {'added': [], 'updated': ['prod-k8s'], 'removed': []}}


@pytest.mark.asyncio
async def test_missing_keys_keep_saved_values(monkeypatch, audits):
    store = FakeStore(saved_config())
    before = store._copy()['value']
    app, _ = make_app(monkeypatch, store)
    async with TestClient(TestServer(app)) as client:
        resp = await client.put('/api/admin/notification', json={})
    assert resp.status == 200 and store.row['value'] == before
    assert audits[0]['detail'] == {'default': 'unchanged', 'clusters': {'added': [], 'updated': [], 'removed': []}}


# ---------------------------------------------------------------------------
# 测试接口
# ---------------------------------------------------------------------------
TEST_PREFIX = '【KubeDoor】消息通知测试'


@pytest.mark.asyncio
async def test_send_test_with_unsaved_token(monkeypatch, audits, im, clock):
    store = FakeStore()
    app, _ = make_app(monkeypatch, store)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post('/api/admin/notification/test',
                                 json={'env': '', 'type': 'wecom', 'token': WECOM_URL, 'at': 'zhangsan'})
        data = await resp.json()
    assert resp.status == 200 and data == {'success': True, 'data': {'ok': True, 'message': '发送成功'}}
    [call] = im.calls
    assert call['url'] == WECOM_URL and call['timeout'] == 10
    content = call['json']['markdown']['content']
    assert content.startswith(f'{TEST_PREFIX}（全局配置），操作人 kubedoor，') and content.endswith('<@zhangsan>')
    assert store.puts == []  # 测试不保存
    [record] = audits
    assert record['action'] == 'notification.test' and record['success'] is True and record['env'] == ''
    assert record['detail'] == {'scope': 'default', 'type': 'wecom', 'saved_token': False, 'message': '发送成功'}


@pytest.mark.asyncio
async def test_send_test_with_saved_token_uses_request_type_and_at(monkeypatch, audits, im, clock):
    app, _ = make_app(monkeypatch, FakeStore(saved_config()))
    async with TestClient(TestServer(app)) as client:
        resp = await client.post('/api/admin/notification/test',
                                 json={'env': 'prod-k8s', 'type': 'dingding', 'token': '', 'at': '13900000000'})
        data = await resp.json()
    assert data['data'] == {'ok': True, 'message': '发送成功'}
    [call] = im.calls
    assert call['url'] == f'https://oapi.dingtalk.com/robot/send?access_token={DING}'
    assert call['json']['at'] == {'atMobiles': ['13900000000']}  # @ 人以请求里的为准
    assert call['json']['markdown']['text'].startswith(f'{TEST_PREFIX}（集群 prod-k8s），操作人 kubedoor，')
    assert audits[0]['env'] == 'prod-k8s' and audits[0]['detail']['saved_token'] is True
    assert audits[0]['detail']['scope'] == 'prod-k8s'


@pytest.mark.asyncio
@pytest.mark.parametrize('body, message', [
    ({'env': 'prod-k8s', 'type': 'wecom', 'token': ''}, '集群 prod-k8s:IM 类型从钉钉改成了企业微信'),
    ({'env': 'new-k8s', 'type': 'wecom', 'token': ''}, '集群 new-k8s:请填写企业微信机器人地址'),
    ({'env': '', 'type': 'slack', 'token': 'bad'}, '全局配置:机器人 token 格式不对'),
    ({'env': 'bad env', 'type': 'wecom', 'token': WECOM}, '集群名称不正确'),
    ({'type': 'qq', 'token': WECOM}, '全局配置:请选择 IM 类型'),
])
async def test_send_test_rejects_invalid_input(monkeypatch, audits, im, clock, body, message):
    app, _ = make_app(monkeypatch, FakeStore(saved_config()))
    async with TestClient(TestServer(app)) as client:
        resp = await client.post('/api/admin/notification/test', json=body)
        data = await resp.json()
    assert resp.status == 422 and data['message'].startswith(message), data['message']
    assert im.calls == [] and audits == []


@pytest.mark.asyncio
async def test_send_test_reports_im_error_without_token(monkeypatch, audits, clock, logs):
    fake = FakeIM(text='{"errcode":93000,"errmsg":"invalid webhook url"}')
    monkeypatch.setattr(service.requests, 'post', fake)
    app, _ = make_app(monkeypatch, FakeStore(saved_config()))
    async with TestClient(TestServer(app)) as client:
        resp = await client.post('/api/admin/notification/test', json={'type': 'wecom', 'token': '', 'at': ''})
        data = await resp.json()
    assert resp.status == 200
    assert data == {'success': True, 'data': {'ok': False, 'message': '错误码 93000:invalid webhook url'}}
    assert audits[0]['success'] is False and audits[0]['detail']['message'] == '错误码 93000:invalid webhook url'
    assert any('消息通知测试失败(全局配置,企业微信):错误码 93000' in line for line in logs)
    assert no_secret(data, audits, logs)


@pytest.mark.asyncio
async def test_send_test_is_limited_to_once_per_3_seconds_per_account(monkeypatch, audits, im, clock):
    app, who = make_app(monkeypatch, FakeStore(saved_config()))
    body = {'type': 'wecom', 'token': '', 'at': ''}
    async with TestClient(TestServer(app)) as client:
        # 参数不对没有发出去,不占次数
        assert (await client.post('/api/admin/notification/test', json={**body, 'type': 'qq'})).status == 422
        first = await client.post('/api/admin/notification/test', json=body)
        clock[0] += 1
        limited = await client.post('/api/admin/notification/test', json=body)
        limited_data = await limited.json()
        who['principal'] = Principal('root', 'admin', user_id=2)  # 别的账号不受影响
        other = await client.post('/api/admin/notification/test', json=body)
        who['principal'] = ADMIN
        clock[0] += 2
        after = await client.post('/api/admin/notification/test', json=body)
    assert first.status == 200 and other.status == 200 and after.status == 200
    assert limited.status == 429 and limited.headers['Retry-After'] == '2'
    assert limited_data['code'] == 'too_many_requests' and '2 秒后再试' in limited_data['message']
    assert len(im.calls) == 3 and [record['username'] for record in audits] == ['kubedoor', 'root', 'kubedoor']


@pytest.mark.asyncio
async def test_send_test_requires_json_object(monkeypatch, audits, im, clock):
    app, _ = make_app(monkeypatch, FakeStore())
    async with TestClient(TestServer(app)) as client:
        resp = await client.post('/api/admin/notification/test', data='x', headers={'Content-Type': 'application/json'})
    assert resp.status == 400 and im.calls == []


@pytest.mark.asyncio
async def test_send_test_runs_off_the_event_loop(monkeypatch, clock):
    """测试发送是阻塞的 requests 调用:必须在线程里执行"""
    threads = []

    def fake_post(url, json=None, timeout=None, allow_redirects=True):
        threads.append(threading.current_thread() is threading.main_thread())
        return SimpleNamespace(status_code=200, text='{"errcode":0}')

    monkeypatch.setattr(service.requests, 'post', fake_post)
    app, _ = make_app(monkeypatch, FakeStore(saved_config()))
    async with TestClient(TestServer(app)) as client:
        await client.post('/api/admin/notification/test', json={'type': 'wecom', 'token': ''})
    assert threads == [False]


# ---------------------------------------------------------------------------
# 内部接口(kubedoor-alarm)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_internal_returns_plain_token_by_cluster(monkeypatch):
    store = FakeStore(saved_config())
    app, _ = make_app(monkeypatch, store, SYSTEM)
    async with TestClient(TestServer(app)) as client:
        # 缓存还没加载时先从库里加载
        prod = await client.get('/api/internal/notification', params={'env': 'prod-k8s'})
        prod_data = await prod.json()
        other = await (await client.get('/api/internal/notification', params={'env': 'other-k8s'})).json()
        default = await (await client.get('/api/internal/notification')).json()
    assert prod.status == 200 and prod.headers['Cache-Control'] == 'no-store'
    assert prod_data == {'success': True, 'data': {'type': 'dingding', 'token': DING, 'at': '13800000000'}}
    assert other == default == {'success': True, 'data': {'type': 'wecom', 'token': WECOM, 'at': 'zhangsan'}}


@pytest.mark.asyncio
async def test_internal_returns_null_when_not_configured(monkeypatch):
    app, _ = make_app(monkeypatch, FakeStore({'clusters': {'prod-k8s': stored('wecom', WECOM)}}), SYSTEM)
    async with TestClient(TestServer(app)) as client:
        data = await (await client.get('/api/internal/notification', params={'env': 'test-k8s'})).json()
    assert data == {'success': True, 'data': None}


@pytest.mark.asyncio
async def test_internal_is_unavailable_until_config_loaded(monkeypatch):
    store = FakeStore(saved_config())
    store.error = RuntimeError('db down')
    app, _ = make_app(monkeypatch, store, SYSTEM)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get('/api/internal/notification', params={'env': 'prod-k8s'})
        data = await resp.json()
        store.error = None
        ok = await client.get('/api/internal/notification', params={'env': 'prod-k8s'})
    # 没加载过就返回 503(kubedoor-alarm 会沿用上次取到的),不能当成「没有配置」
    assert resp.status == 503 and data['code'] == 'not_ready'
    assert ok.status == 200


@pytest.mark.asyncio
@pytest.mark.parametrize('principal', [
    Principal('alice', 'admin', user_id=7, via='internal'),  # AI 服务代表某个用户
    ADMIN,  # 浏览器会话
])
async def test_internal_only_for_service_identity(monkeypatch, principal):
    app, _ = make_app(monkeypatch, FakeStore(saved_config()), principal)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get('/api/internal/notification', params={'env': 'prod-k8s'})
        text = await resp.text()
    assert resp.status == 403 and DING not in text and WECOM not in text


@pytest.mark.asyncio
@pytest.mark.parametrize('env', ['', 'other-k8s', 'bad env!', 'x' * 300, '../prod-k8s', 'PROD-K8S'])
async def test_internal_falls_back_to_default_for_empty_or_unknown_env(monkeypatch, env):
    """kubedoor-alarm 总是带 env:空串要全局配置;格式不对或不认识的集群按 resolve 规则退回全局配置,照样 200
    (alarm 把非 200 当成取不到,这批告警就发不出去了)"""
    app, _ = make_app(monkeypatch, FakeStore(saved_config()), SYSTEM)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get('/api/internal/notification', params={'env': env})
        data = await resp.json()
    assert resp.status == 200
    assert data == {'success': True, 'data': {'type': 'wecom', 'token': WECOM, 'at': 'zhangsan'}}


@pytest.mark.asyncio
async def test_internal_body_shape(monkeypatch):
    """返回体固定:data 是 {type: 四种之一, token: 非空明文, at: 字符串},没有可用的配置时为 null"""
    value = {'default': {'type': 'slack', 'token': encrypt_secret('T0/B0/x')},  # 没有 at 字段
             'clusters': {'empty-k8s': {'type': 'wecom', 'token': '', 'at': ''},  # 没有 token:不可用,退回全局配置
                          'bad-k8s': {'type': 'qq', 'token': encrypt_secret(WECOM)}}}
    app, _ = make_app(monkeypatch, FakeStore(value), SYSTEM)
    async with TestClient(TestServer(app)) as client:
        bodies = [await (await client.get('/api/internal/notification', params={'env': env})).json()
                  for env in ('', 'empty-k8s', 'bad-k8s')]
    for body in bodies:
        assert body == {'success': True, 'data': {'type': 'slack', 'token': 'T0/B0/x', 'at': ''}}
        assert set(body['data']) == {'type', 'token', 'at'} and body['data']['type'] in channels.TYPES
        assert isinstance(body['data']['at'], str) and body['data']['token']


@pytest.mark.asyncio
async def test_internal_through_auth_middleware(monkeypatch):
    """经过真实的认证中间件:没带或带错内部 token 拒绝,带对的(不带用户名)才能取"""
    from auth.middleware import create_middleware

    monkeypatch.setenv('AI_INTERNAL_TOKEN', 'internal-secret')
    store = FakeStore(saved_config())
    monkeypatch.setattr(notify_api, 'store', store)
    monkeypatch.setattr(service, 'store', store)
    app = web.Application(middlewares=[create_middleware()])
    notify_api.register_routes(app)
    async with TestClient(TestServer(app)) as client:
        anonymous = await client.get('/api/internal/notification', params={'env': 'prod-k8s'})
        wrong = await client.get('/api/internal/notification', params={'env': 'prod-k8s'},
                                 headers={'X-Kubedoor-Token': 'wrong'})
        ok = await client.get('/api/internal/notification', params={'env': 'prod-k8s'},
                              headers={'X-Kubedoor-Token': 'internal-secret'})
        data = await ok.json()
        admin_page = await client.get('/api/admin/notification', headers={'X-Kubedoor-Token': 'internal-secret'})
        admin_data = await admin_page.json()
    assert anonymous.status == 401 and wrong.status == 401
    assert ok.status == 200 and data['data'] == {'type': 'dingding', 'token': DING, 'at': '13800000000'}
    assert admin_page.status == 200  # 系统身份是管理员,页面接口照常(没有明文 token)
    assert admin_data['success'] is True and no_secret(admin_data)


# ---------------------------------------------------------------------------
# 安装 Agent 写入集群配置(save_cluster_channel)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cluster_channel_is_added_and_keeps_default_and_other_clusters(monkeypatch, audits):
    store = FakeStore(saved_config())
    before = json.loads(json.dumps(store.row['value']))
    make_app(monkeypatch, store)
    await notify_api.save_cluster_channel(' new-k8s ', {'type': 'feishu', 'token': f' https://open.feishu.cn/open-apis/bot/v2/hook/{FEISHU} ',
                                                        'at': ' ou_1 '}, 'installer')
    saved = store.row['value']
    assert saved['default'] == before['default'] and saved['clusters']['prod-k8s'] == before['clusters']['prod-k8s']
    entry = saved['clusters']['new-k8s']
    assert entry['type'] == 'feishu' and entry['at'] == 'ou_1' and entry['token'].startswith('v1:')
    assert decrypt_secret(entry['token']) == FEISHU and store.row['updated_by'] == 'installer'
    # 马上生效
    assert service.resolve('new-k8s') == {'type': 'feishu', 'token': FEISHU, 'at': 'ou_1'}
    assert service.resolve('prod-k8s')['token'] == DING and service.resolve('other-k8s')['token'] == WECOM
    [record] = audits
    assert record['action'] == 'notification.update' and record['username'] == 'installer' and record['env'] == 'new-k8s'
    assert record['detail'] == {'default': 'unchanged', 'clusters': {'added': ['new-k8s'], 'updated': [], 'removed': []},
                                'source': 'agent-install'}
    assert no_secret(saved, audits)


@pytest.mark.asyncio
async def test_cluster_channel_overwrites_the_existing_cluster_config(monkeypatch, audits):
    store = FakeStore(saved_config())
    make_app(monkeypatch, store)
    await notify_api.save_cluster_channel('prod-k8s', {'type': 'wecom', 'token': WECOM_URL, 'at': ''}, 'installer')
    entry = store.row['value']['clusters']['prod-k8s']
    assert entry['type'] == 'wecom' and entry['at'] == '' and decrypt_secret(entry['token']) == WECOM
    assert audits[0]['detail']['clusters'] == {'added': [], 'updated': ['prod-k8s'], 'removed': []}
    # 没有全局配置时也不凭空加一个
    store = FakeStore()
    make_app(monkeypatch, store)
    await notify_api.save_cluster_channel('prod-k8s', {'type': 'dingding', 'token': DING}, 'installer')
    assert store.row['value'] == {'clusters': {'prod-k8s': {'type': 'dingding', 'token': store.row['value']['clusters']['prod-k8s']['token'],
                                                            'at': ''}}}
    assert service.resolve('other-k8s') is None


@pytest.mark.asyncio
@pytest.mark.parametrize('env, entry, message', [
    ('prod-k8s', {'type': 'qq', 'token': WECOM}, '集群 prod-k8s:请选择 IM 类型'),
    ('prod-k8s', {'type': 'wecom', 'token': 'https://example.com/hook'}, '集群 prod-k8s:不认识的机器人地址'),
    ('prod-k8s', {'type': 'wecom', 'token': ''}, '集群 prod-k8s:IM 类型从钉钉改成了企业微信'),
    ('new-k8s', {'type': 'wecom', 'token': ''}, '集群 new-k8s:请填写企业微信机器人地址'),
    ('bad env', {'type': 'wecom', 'token': WECOM}, '集群名称不正确'),
    ('', {'type': 'wecom', 'token': WECOM}, '集群名称不正确'),
])
async def test_cluster_channel_is_validated_like_put(monkeypatch, audits, env, entry, message):
    store = FakeStore(saved_config())
    before = store._copy()
    make_app(monkeypatch, store)
    with pytest.raises(ValueError) as error:
        await notify_api.save_cluster_channel(env, entry, 'installer')
    assert str(error.value).startswith(message), str(error.value)
    assert store.puts == [] and store._copy() == before and audits == [] and not service.is_loaded()


@pytest.mark.asyncio
async def test_cluster_channel_blank_token_keeps_the_saved_one_and_limit_applies(monkeypatch, audits):
    store = FakeStore(saved_config())
    make_app(monkeypatch, store)
    await notify_api.save_cluster_channel('prod-k8s', {'type': 'dingding', 'token': '', 'at': '13900000000'}, 'installer')
    entry = store.row['value']['clusters']['prod-k8s']
    assert entry['at'] == '13900000000' and decrypt_secret(entry['token']) == DING
    monkeypatch.setattr(notify_api, 'MAX_CLUSTERS', 1)
    with pytest.raises(ValueError, match='按集群单独配置最多 1 个'):
        await notify_api.save_cluster_channel('new-k8s', {'type': 'wecom', 'token': WECOM}, 'installer')
    # 已有的集群照样能覆盖
    await notify_api.save_cluster_channel('prod-k8s', {'type': 'wecom', 'token': WECOM}, 'installer')
    assert list(store.row['value']['clusters']) == ['prod-k8s']


@pytest.mark.asyncio
async def test_cluster_channel_database_failure_raises_and_changes_nothing(monkeypatch, audits):
    store = FakeStore(saved_config())
    store.put_error = RuntimeError('db down')
    make_app(monkeypatch, store)
    with pytest.raises(RuntimeError):
        await notify_api.save_cluster_channel('new-k8s', {'type': 'wecom', 'token': WECOM}, 'installer')
    assert audits == [] and service.resolve('new-k8s') is None
    store.put_error, store.error = None, RuntimeError('db down')
    with pytest.raises(RuntimeError):
        await notify_api.save_cluster_channel('new-k8s', {'type': 'wecom', 'token': WECOM}, 'installer')


@pytest.mark.asyncio
async def test_cluster_channel_and_put_share_one_lock(monkeypatch, audits):
    """两边都是「读出整行 → 合并 → 写回」:共用一把锁,页面保存不会冲掉安装 Agent 刚写入的集群配置"""
    store = FakeStore(saved_config())
    app, _ = make_app(monkeypatch, store)
    async with TestClient(TestServer(app)) as client:
        async with notify_api._save_lock:  # 另一次保存正在进行
            installing = asyncio.create_task(
                notify_api.save_cluster_channel('new-k8s', {'type': 'feishu', 'token': FEISHU}, 'installer'))
            saving = asyncio.create_task(client.put('/api/admin/notification',
                                                    json={'default': {'type': 'wecom', 'token': '', 'at': 'lisi'}}))
            await asyncio.sleep(0.05)
            assert not installing.done() and not saving.done() and store.puts == []
        await installing
        response = await saving
        assert response.status == 200
    # 先进先出:安装 Agent 先写,页面保存读到的是写入之后的配置
    assert [sorted(put['clusters']) for put in store.puts] == [['new-k8s', 'prod-k8s'], ['new-k8s', 'prod-k8s']]
    assert store.row['value']['default']['at'] == 'lisi' and 'new-k8s' in store.row['value']['clusters']
    assert service.resolve('new-k8s')['token'] == FEISHU


# ---------------------------------------------------------------------------
# 启停
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_startup_loads_config_and_cleanup_stops_refresh(monkeypatch):
    store = FakeStore(saved_config())
    monkeypatch.setattr(service, 'store', store)
    app = web.Application()
    await notify_api.on_startup(app)
    task = app[notify_api.REFRESH_TASK]
    assert service.resolve('prod-k8s')['token'] == DING and not task.done()
    await notify_api.on_cleanup(app)
    assert task.cancelled()
    await notify_api.on_cleanup(web.Application())  # 没启动过也不报错


@pytest.mark.asyncio
async def test_startup_survives_database_failure(monkeypatch):
    store = FakeStore(saved_config())
    store.error = RuntimeError('db down')
    monkeypatch.setattr(service, 'store', store)
    app = web.Application()
    await notify_api.on_startup(app)
    assert not service.is_loaded()
    await notify_api.on_cleanup(app)


def test_helpers():
    assert notify_api.token_hint('') == '' and notify_api.token_hint('abc') == '****'
    assert notify_api.token_hint(WECOM) == '****a1b2'
    assert notify_api.saved_token(None) == '' and notify_api.saved_token({'token': 'garbage'}) == ''
    assert channels.TYPE_LABELS['wecom'] == '企业微信'
