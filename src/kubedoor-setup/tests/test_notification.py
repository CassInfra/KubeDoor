"""安装向导第 4 步的告警通知：配置校验、公开配置、测试接口、直接发送，以及安装最后保存到 master。"""
import asyncio
import json
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aiohttp import CookieJar, web
from aiohttp.test_utils import TestClient, TestServer

import kubedoor_setup.api as api_module
from kubedoor_setup.api import NOTIFY_TEST_INTERVAL, NOTIFY_TIMEOUT, SetupAPI, create_app, send_notification
from kubedoor_setup.config import (default_private, final_config, notification_config, notify_channels, public_config,
                                   redact_token)
from kubedoor_setup.errors import SetupError
from kubedoor_setup.repository import Repository
from kubedoor_setup.runner import InstallRunner, ServiceCheckError
from test_runner import ID, FakeKubernetes as RunnerKubernetes, MemoryRepo, ObservedRunner, check_row, setup_probes
from test_setup_api import FINAL, KEY, FakeKubernetes, authorize, config_ready

WECOM = 'wecom-test-key-0123456789abcdef'
WECOM_URL = 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=' + WECOM
DING = 'dingtalk-test-token-0123456789'
DING_URL = 'https://oapi.dingtalk.com/robot/send?access_token=' + DING
FEISHU = 'feishu-test-hook-0123456789'
SLACK = 'T0000AAAA/B0000BBBB/slacksecretpart0123456789'
TEST_PATH = '/setup-api/notification/test'


# ---------------------------------------------------------------------------
# 配置校验与公开配置
# ---------------------------------------------------------------------------
def test_channels_module_is_loaded_from_master_source():
    channels = notify_channels()
    assert channels.TYPES == ('wecom', 'dingding', 'feishu', 'slack')
    assert channels.__file__.replace('\\', '/').endswith('kubedoor-master/notify/channels.py')
    assert notify_channels() is channels


def test_notification_accepts_token_or_full_webhook_address():
    assert notification_config({'type': 'wecom', 'token': WECOM_URL, 'at': ' zhangsan '}, {}) == {
        'type': 'wecom', 'token': WECOM, 'at': 'zhangsan'}
    assert notification_config({'type': 'wecom', 'token': f'  {WECOM}  '}, {})['token'] == WECOM
    assert notification_config({'type': 'dingding', 'token': DING_URL, 'at': 13800000000}, {}) == {
        'type': 'dingding', 'token': DING, 'at': '13800000000'}
    slack = notification_config({'type': 'slack', 'token': 'https://hooks.slack.com/services/' + SLACK}, {})
    assert slack == {'type': 'slack', 'token': SLACK, 'at': ''}


@pytest.mark.parametrize('raw, reason', [
    ({'type': 'qq', 'token': WECOM}, 'IM 类型'),
    ({'type': 'wecom', 'token': DING_URL}, '钉钉'),
    ({'type': 'wecom', 'token': 'https://evil.example.com/hook?key=' + WECOM}, '不认识'),
    ({'type': 'wecom', 'token': WECOM + ' x'}, '空格'),
    ({'type': 'wecom', 'token': WECOM[:6] + '\x01' + WECOM[6:]}, '控制字符'),
    ({'type': 'wecom', 'token': 'x' * 513}, '太长'),
    ({'type': 'wecom', 'token': WECOM, 'at': 'a\nb'}, '控制字符'),
    ({'type': 'wecom', 'token': WECOM, 'at': 'x' * 129}, '128'),
    ({'type': 'wecom', 'token': 12345}, '格式不正确'),
    ({'type': 'wecom', 'token': WECOM, 'clear': 'yes'}, '格式不正确'),
    ('wecom', '格式不正确'),
    (None, '格式不正确'),
])
def test_notification_rejects_invalid_input_without_echoing_token(raw, reason):
    with pytest.raises(SetupError) as error:
        notification_config(raw, {})
    assert error.value.status == 422
    assert reason in error.value.message
    assert WECOM not in error.value.message and DING not in error.value.message


def test_blank_token_keeps_saved_one_but_changed_im_type_needs_new_address():
    saved = {'type': 'wecom', 'token': WECOM, 'at': 'zhangsan'}
    assert notification_config({'type': 'wecom', 'token': '', 'at': 'lisi'}, saved) == {'type': 'wecom', 'token': WECOM, 'at': 'lisi'}
    # 没传 type / at 时沿用已保存的
    assert notification_config({}, saved) == saved
    with pytest.raises(SetupError, match='从企业微信改成了钉钉，请重新填写机器人地址') as error:
        notification_config({'type': 'dingding', 'token': ''}, saved)
    assert WECOM not in error.value.message
    assert notification_config({'type': 'dingding', 'token': DING_URL}, saved) == {'type': 'dingding', 'token': DING, 'at': 'zhangsan'}
    # 留空又没有保存过：不发通知（不是错误）
    assert notification_config({'type': 'feishu', 'token': '', 'at': ''}, {}) == {'type': 'feishu', 'token': '', 'at': ''}
    assert notification_config({'type': 'feishu', 'token': None}, None) == {'type': 'feishu', 'token': '', 'at': ''}
    # clear 清掉已保存的地址和 @ 人
    assert notification_config({'type': 'wecom', 'clear': True}, saved) == {'type': 'wecom', 'token': '', 'at': ''}
    assert notification_config({'clear': True, 'token': 'ignored'}, saved) == {'type': 'wecom', 'token': '', 'at': ''}


def test_final_config_saves_notification_or_keeps_the_saved_one():
    saved = {'type': 'wecom', 'token': WECOM, 'at': 'zhangsan'}
    existing = {'admin': {'username': 'kubedoor', 'password': 'robust-admin-secret'}, 'notification': saved}
    assert final_config(FINAL, existing)['notification'] == saved
    changed = final_config({**FINAL, 'notification': {'type': 'feishu', 'token': FEISHU, 'at': 'ou_x'}}, existing)
    assert changed['notification'] == {'type': 'feishu', 'token': FEISHU, 'at': 'ou_x'}
    cleared = final_config({**FINAL, 'notification': {'type': 'wecom', 'clear': True}}, existing)
    assert cleared['notification'] == {'type': 'wecom', 'token': '', 'at': ''}
    # 旧版向导保存的草稿里没有 notification
    assert final_config(FINAL, {})['notification'] == {'type': 'wecom', 'token': '', 'at': ''}
    with pytest.raises(SetupError, match='告警通知'):
        final_config({**FINAL, 'notification': {'type': 'wecom', 'token': 'bad token'}}, existing)


def test_public_config_says_whether_notification_is_configured_without_token():
    config = default_private()['config']
    assert config['notification'] == {'type': 'wecom', 'token': '', 'at': ''}
    assert public_config(config)['notification'] == {'type': 'wecom', 'at': '', 'configured': False}
    config['notification'] = {'type': 'feishu', 'token': FEISHU, 'at': 'ou_x'}
    public = public_config(config)
    assert public['notification'] == {'type': 'feishu', 'at': 'ou_x', 'configured': True}
    assert FEISHU not in json.dumps(public)
    assert config['notification']['token'] == FEISHU
    del config['notification']
    assert public_config(config)['notification'] == {'type': 'wecom', 'at': '', 'configured': False}


def test_redact_token_removes_whole_token_and_slack_segments():
    assert redact_token(f'bad key {WECOM}!', WECOM) == 'bad key ***!'
    assert redact_token('no_service for slacksecretpart0123456789', SLACK) == 'no_service for ***'
    assert redact_token('x' * 400, '') == 'x' * 299 + '…'


# ---------------------------------------------------------------------------
# 安装服务接口
# ---------------------------------------------------------------------------
async def notify_client(sender):
    repo = Repository(FakeKubernetes())
    app = create_app(repo, key=KEY, pg_probe=AsyncMock(return_value={'status': 'success', 'message': 'PG verified'}),
                     metrics_probe=AsyncMock(return_value={'status': 'success', 'message': 'VM verified'}),
                     notify_sender=sender)
    client = TestClient(TestServer(app), cookie_jar=CookieJar(unsafe=True))
    await client.start_server()
    return client, repo, next(value for value in app.values() if isinstance(value, SetupAPI))


def elapse(api):
    """当成已经过了测试间隔"""
    for session in api.notify_tests:
        api.notify_tests[session] -= NOTIFY_TEST_INTERVAL


def state_secret_texts(repo):
    """安装状态 ConfigMap（含无需登录就能读的公开标记）"""
    return json.dumps(repo.k8s.resources[repo.base + '/configmaps/kubedoor-setup-state'], ensure_ascii=False)


@pytest.mark.asyncio
async def test_configuration_saves_notification_and_never_returns_token():
    client, repo, _ = await notify_client(AsyncMock())
    async with client:
        state = await config_ready(client)
        assert state['config']['notification'] == {'type': 'wecom', 'at': '', 'configured': False}
        response = await client.put('/setup-api/configuration', json={
            'revision': state['revision'], **FINAL, 'notification': {'type': 'dingding', 'token': DING_URL, 'at': '13800000000'}})
        assert response.status == 200
        result = await response.json()
        assert result['data']['config']['notification'] == {'type': 'dingding', 'at': '13800000000', 'configured': True}
        assert DING not in json.dumps(result)
        assert (await repo.load_private())['config']['notification'] == {'type': 'dingding', 'token': DING, 'at': '13800000000'}
        assert DING not in state_secret_texts(repo)
        state = (await (await client.get('/setup-api/state')).json())['data']
        assert DING not in json.dumps(state) and state['config']['notification']['configured'] is True
        # 留空保留已保存的地址
        response = await client.put('/setup-api/configuration', json={
            'revision': state['revision'], **FINAL, 'notification': {'type': 'dingding', 'token': '', 'at': ''}})
        state = (await response.json())['data']
        assert (await repo.load_private())['config']['notification'] == {'type': 'dingding', 'token': DING, 'at': ''}
        # 不合法的地址不保存
        response = await client.put('/setup-api/configuration', json={
            'revision': state['revision'], **FINAL, 'notification': {'type': 'feishu', 'token': DING_URL}})
        assert response.status == 422 and '钉钉' in (await response.json())['message']
        assert (await repo.load_private())['config']['notification']['token'] == DING
        # 清除后不再发通知
        response = await client.put('/setup-api/configuration', json={
            'revision': state['revision'], **FINAL, 'notification': {'type': 'dingding', 'clear': True}})
        assert (await response.json())['data']['config']['notification'] == {'type': 'dingding', 'at': '', 'configured': False}
        assert (await repo.load_private())['config']['notification']['token'] == ''


@pytest.mark.asyncio
async def test_failed_installation_can_still_change_notification_before_retry():
    client, repo, _ = await notify_client(AsyncMock())
    async with client:
        await config_ready(client)
        state = await repo.load()
        state.update(phase='failed', target_locked=True)
        await repo.save(state)
        response = await client.put('/setup-api/configuration', json={
            'revision': state['revision'], **FINAL, 'notification': {'type': 'wecom', 'token': WECOM}})
        assert response.status == 200
        assert (await response.json())['data']['phase'] == 'failed'
        assert (await repo.load_private())['config']['notification']['token'] == WECOM


@pytest.mark.asyncio
async def test_notification_test_needs_session_and_sends_without_saving():
    sender = AsyncMock(return_value=(True, '发送成功'))
    client, repo, _ = await notify_client(sender)
    async with client:
        body = {'notification': {'type': 'wecom', 'token': WECOM_URL, 'at': 'zhangsan'}}
        response = await client.post(TEST_PATH, json=body)
        assert response.status == 401
        sender.assert_not_awaited()
        await authorize(client)
        response = await client.post(TEST_PATH, json=body)
        assert response.status == 200
        assert await response.json() == {'success': True, 'data': {'ok': True, 'message': '发送成功'}}
        channel, content = sender.await_args.args
        assert channel == {'type': 'wecom', 'token': WECOM, 'at': 'zhangsan'}
        assert content.startswith('【KubeDoor】消息通知测试（全局配置），操作人 安装向导，20')
        # 只测试，不保存
        assert (await repo.load_private())['config']['notification']['token'] == ''


@pytest.mark.asyncio
async def test_notification_test_uses_saved_address_when_blank():
    sender = AsyncMock(return_value=(True, '发送成功'))
    client, _, api = await notify_client(sender)
    async with client:
        state = await config_ready(client)
        response = await client.post(TEST_PATH, json={'notification': {'type': 'wecom', 'token': '', 'at': ''}})
        assert response.status == 422 and '请填写企业微信机器人地址' in (await response.json())['message']
        await client.put('/setup-api/configuration', json={
            'revision': state['revision'], **FINAL, 'notification': {'type': 'wecom', 'token': WECOM, 'at': 'zhangsan'}})
        response = await client.post(TEST_PATH, json={'notification': {'type': 'wecom', 'token': '', 'at': 'lisi'}})
        assert response.status == 200
        # 留空用已保存的地址，@ 人以页面上的为准
        assert sender.await_args.args[0] == {'type': 'wecom', 'token': WECOM, 'at': 'lisi'}
        elapse(api)
        response = await client.post(TEST_PATH, json={'notification': {'type': 'slack', 'token': ''}})
        assert response.status == 422 and '重新填写' in (await response.json())['message']
        assert sender.await_count == 1


@pytest.mark.asyncio
async def test_notification_test_is_limited_per_session():
    sender = AsyncMock(return_value=(False, '错误码 93000:invalid webhook url'))
    client, _, api = await notify_client(sender)
    async with client:
        await authorize(client)
        body = {'notification': {'type': 'wecom', 'token': WECOM}}
        # 不合格的请求不占用次数
        assert (await client.post(TEST_PATH, json={'notification': {'type': 'wecom', 'token': 'bad token'}})).status == 422
        response = await client.post(TEST_PATH, json=body)
        assert response.status == 200
        assert (await response.json())['data'] == {'ok': False, 'message': '错误码 93000:invalid webhook url'}
        response = await client.post(TEST_PATH, json=body)
        assert response.status == 429 and '秒后再试' in (await response.json())['message']
        assert sender.await_count == 1
        # 另一个安装会话不受影响
        await authorize(client)
        assert (await client.post(TEST_PATH, json=body)).status == 200
        assert sender.await_count == 2
        # 过了间隔后同一会话可以再测
        elapse(api)
        assert (await client.post(TEST_PATH, json=body)).status == 200
        assert sender.await_count == 3


@pytest.mark.asyncio
async def test_notification_test_response_never_contains_token():
    sender = AsyncMock(return_value=(False, f'invalid webhook url, key={WECOM}'))
    client, _, _ = await notify_client(sender)
    async with client:
        await authorize(client)
        response = await client.post(TEST_PATH, json={'notification': {'type': 'wecom', 'token': WECOM_URL}})
        text = await response.text()
        assert response.status == 200 and WECOM not in text
        assert (await response.json())['data'] == {'ok': False, 'message': 'invalid webhook url, key=***'}


# ---------------------------------------------------------------------------
# 直接发送测试消息
# ---------------------------------------------------------------------------
def forwarding(server, calls):
    """session_factory：把发往 IM 的请求转到本地测试服务，记下原地址、会话参数和请求参数"""
    class Session:
        def __init__(self, **options):
            self.options = options
            self.session = aiohttp.ClientSession(timeout=options['timeout'])

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            await self.session.close()

        def post(self, url, **kwargs):
            calls.append({'url': url, 'options': self.options, **kwargs})
            return self.session.post(server.make_url('/hook'), **kwargs)
    return Session


def im_app(handler, hits):
    async def hook(request):
        hits.append(await request.json())
        return await handler(request)
    app = web.Application()
    app.router.add_post('/hook', hook)
    return app


@pytest.mark.asyncio
async def test_send_posts_channel_message_with_timeout_and_without_redirects():
    async def ok(request):
        return web.json_response({'errcode': 0, 'errmsg': 'ok'})
    hits, calls = [], []
    async with TestServer(im_app(ok, hits)) as server:
        result = await send_notification({'type': 'wecom', 'token': WECOM, 'at': 'zhangsan'}, '测试内容',
                                         session_factory=forwarding(server, calls))
    assert result == (True, '发送成功')
    assert hits == [{'msgtype': 'markdown', 'markdown': {'content': '测试内容<@zhangsan>'}}]
    call = calls[0]
    assert call['url'] == WECOM_URL and call['allow_redirects'] is False
    assert call['options']['trust_env'] is False and call['options']['timeout'].total == NOTIFY_TIMEOUT


@pytest.mark.asyncio
async def test_send_reports_im_error_code_without_token():
    async def rejected(request):
        return web.json_response({'errcode': 93000, 'errmsg': f'invalid webhook url, key={WECOM}'})
    hits, calls = [], []
    async with TestServer(im_app(rejected, hits)) as server:
        ok, message = await send_notification({'type': 'wecom', 'token': WECOM, 'at': ''}, '测试',
                                               session_factory=forwarding(server, calls))
    assert ok is False and '错误码 93000' in message and WECOM not in message


@pytest.mark.asyncio
async def test_send_slack_failure_hides_every_token_segment():
    async def rejected(request):
        return web.Response(status=404, text='no_service for slacksecretpart0123456789')
    hits, calls = [], []
    async with TestServer(im_app(rejected, hits)) as server:
        ok, message = await send_notification({'type': 'slack', 'token': SLACK, 'at': 'U012AB3CD'}, '测试',
                                               session_factory=forwarding(server, calls))
    assert calls[0]['url'] == 'https://hooks.slack.com/services/' + SLACK
    assert hits == [{'text': '测试 <@U012AB3CD>'}]
    assert ok is False and message == 'HTTP 404,no_service for ***'


@pytest.mark.asyncio
async def test_send_does_not_follow_redirects():
    async def moved(request):
        raise web.HTTPFound('http://127.0.0.1:9/steal')
    hits, calls = [], []
    async with TestServer(im_app(moved, hits)) as server:
        ok, message = await send_notification({'type': 'dingding', 'token': DING, 'at': ''}, '测试',
                                               session_factory=forwarding(server, calls))
    assert ok is False and 'HTTP 302' in message
    assert len(hits) == 1


@pytest.mark.asyncio
async def test_send_timeout_names_the_im_host(monkeypatch):
    release = asyncio.Event()

    async def slow(request):
        await release.wait()
        return web.json_response({'errcode': 0})
    monkeypatch.setattr(api_module, 'NOTIFY_TIMEOUT', 0.2)
    hits, calls = [], []
    async with TestServer(im_app(slow, hits)) as server:
        ok, message = await send_notification({'type': 'wecom', 'token': WECOM, 'at': ''}, '测试',
                                               session_factory=forwarding(server, calls))
        release.set()
    assert ok is False
    assert message == '0.2 秒内没有收到企业微信接口的返回，请检查集群能否访问 qyapi.weixin.qq.com'


@pytest.mark.asyncio
async def test_send_connection_failure_never_includes_the_address():
    class Unreachable:
        def __init__(self, **options):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            pass

        def post(self, url, **kwargs):
            raise aiohttp.ClientConnectionError(f'Cannot connect to {url}')
    ok, message = await send_notification({'type': 'feishu', 'token': FEISHU, 'at': ''}, '测试', session_factory=Unreachable)
    assert ok is False
    assert message == '无法连接飞书接口（open.feishu.cn），请检查集群能否访问外网，或是否需要代理'


# ---------------------------------------------------------------------------
# 安装最后：管理员登录后保存到 master 的「系统管理 → 消息通知」
# ---------------------------------------------------------------------------
class FakeKubeDoor:
    """假的 KubeDoor 对外入口（nginx → master）：安装标识、管理员登录、Grafana 数据源、消息通知保存、退出登录。"""

    def __init__(self, status=200, body=None):
        self.status, self.body = status, body if body is not None else {'success': True, 'data': {}}
        self.calls, self.saved = [], []

    def app(self):
        app = web.Application()
        app.router.add_route('*', '/{tail:.*}', self.handle)
        return app

    async def handle(self, request):
        self.calls.append((request.method, request.path))
        if request.path == '/setup-status.json':
            return web.json_response({'installation_id': ID})
        if request.path == '/api/auth/login':
            response = web.json_response({'success': True})
            response.set_cookie('kubedoor_session', 'admin-session')
            return response
        if request.path == '/api/admin/notification':
            self.saved.append({'cookie': request.cookies.get('kubedoor_session'), 'body': await request.json()})
            return web.json_response(self.body, status=self.status)
        if request.path.startswith('/grafana/'):
            return web.json_response({'status': 'OK'})
        return web.json_response({'success': True})


async def verify(kubedoor, notification=None):
    async with TestServer(kubedoor.app()) as server, \
            aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as session:
        runner = ObservedRunner(MemoryRepo(), RunnerKubernetes())
        runner.config, runner.installation_id, runner.http = runner.repo.private['config'], ID, session
        runner.config.update(public_url=f'http://{server.host}:{server.port}')
        runner.config['metrics']['write_url'] = ''
        if notification:
            runner.config['notification'] = notification
        await InstallRunner.verify_endpoints(runner)
        return runner


@pytest.mark.asyncio
async def test_configured_notification_is_saved_as_global_config_after_admin_login():
    kubedoor = FakeKubeDoor()
    runner = await verify(kubedoor, {'type': 'dingding', 'token': DING, 'at': '13800000000'})
    assert kubedoor.saved == [{'cookie': 'admin-session', 'body': {
        'default': {'type': 'dingding', 'token': DING, 'at': '13800000000'}}}]
    order = [call for call in kubedoor.calls if call[1].startswith('/api/')]
    assert order == [('POST', '/api/auth/login'), ('GET', '/api/auth/me'), ('PUT', '/api/admin/notification'),
                     ('POST', '/api/auth/logout')]
    assert any(row['name'] == 'endpoints' and '保存告警通知配置' in row['message']
               for state in runner.repo.history for row in state['checks'])
    assert DING not in json.dumps(runner.repo.history, ensure_ascii=False)


@pytest.mark.asyncio
async def test_notification_is_not_saved_when_left_blank():
    kubedoor = FakeKubeDoor()
    await verify(kubedoor)
    assert kubedoor.saved == [] and ('PUT', '/api/admin/notification') not in kubedoor.calls
    assert ('POST', '/api/auth/logout') in kubedoor.calls


@pytest.mark.asyncio
@pytest.mark.parametrize('status, body, expected', [
    (422, {'success': False, 'message': f'全局配置:机器人 token 格式不对 {DING}'}, '保存告警通知配置失败：全局配置:机器人 token 格式不对 ***'),
    (503, {'success': False, 'message': '保存消息通知配置失败,请稍后重试'}, '保存告警通知配置失败：保存消息通知配置失败,请稍后重试'),
    (403, {'success': False, 'code': 'password_change_required', 'message': '请先修改初始口令'}, '请先修改初始口令'),
    (404, {'success': False}, '没有消息通知接口（HTTP 404），镜像版本过旧'),
    (500, {'success': False}, '控制服务返回 HTTP 500'),
])
async def test_failed_save_fails_endpoint_check_with_reason_but_no_token(status, body, expected):
    kubedoor = FakeKubeDoor(status, body)
    with pytest.raises(SetupError) as error:
        await verify(kubedoor, {'type': 'dingding', 'token': DING, 'at': ''})
    assert expected in error.value.message and DING not in error.value.message
    # 失败也要退出管理员登录
    assert kubedoor.calls[-1] == ('POST', '/api/auth/logout')


@pytest.mark.asyncio
async def test_unreachable_master_while_saving_notification_is_reported():
    runner = ObservedRunner(MemoryRepo(), RunnerKubernetes())
    runner.config, runner.installation_id = runner.repo.private['config'], ID
    runner.config['notification'] = {'type': 'wecom', 'token': WECOM, 'at': ''}

    async def request(method, url, **kwargs):
        if method == 'PUT':
            raise ServiceCheckError('服务连接超时或证书验证失败，请检查网络与服务状态', probe=f'PUT {url} → 连接失败或超时')
        return {'installation_id': ID} if url.endswith('/setup-status.json') else {'success': True, 'status': 'ok'}
    runner.request = request
    with pytest.raises(SetupError, match='保存告警通知配置失败：连接控制服务超时或失败') as error:
        await InstallRunner.verify_endpoints(runner)
    assert WECOM not in error.value.message


@pytest.mark.asyncio
@pytest.mark.parametrize('token, suffix', [(WECOM, '，告警通知已保存为全局配置'), ('', '')])
async def test_endpoint_check_result_mentions_saved_notification(monkeypatch, token, suffix):
    setup_probes(monkeypatch)
    repo = MemoryRepo()
    repo.private['config']['notification'] = {'type': 'wecom', 'token': token, 'at': ''}
    runner = ObservedRunner(repo, RunnerKubernetes())
    await runner.run()
    assert runner.verified
    assert check_row(repo, 'endpoints')['message'] == '对外入口、登录与所选指标链路验证通过' + suffix
