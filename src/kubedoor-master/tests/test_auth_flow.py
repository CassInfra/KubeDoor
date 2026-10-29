"""认证中间件 + 登录 / 改密 / 管理接口的端到端测试(内存用户库,不需要数据库)。"""
import base64
import pathlib
import sys
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import auth  # noqa: E402
import utils  # noqa: E402
from auth import ldap_auth, passwords  # noqa: E402
from auth.context import principal_of  # noqa: E402
from auth.ratelimit import LoginLimiter  # noqa: E402
from auth_fakes import FakeStore, install  # noqa: E402

TOKEN = 'internal-test-token'


def basic(username, password):
    return 'Basic ' + base64.b64encode(f'{username}:{password}'.encode()).decode()


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setattr(passwords, 'SCRYPT_N', 2 ** 8)
    monkeypatch.setattr(passwords, 'SCRYPT_P', 1)
    monkeypatch.setenv('AI_INTERNAL_TOKEN', TOKEN)
    monkeypatch.setattr(utils, 'get_k8s_names', AsyncMock(return_value=['prod', 'staging']))
    from auth import admin_api, authn, handlers

    limiter = LoginLimiter(max_failures=3, user_max_failures=10)
    for module in (authn, handlers, admin_api):
        monkeypatch.setattr(module, 'limiter', limiter)
    return install(monkeypatch, FakeStore())


async def add_user(fake, username, role, password='Str0ng-pass!', source='local', **extra):
    password_hash = passwords.hash_password(password) if source == 'local' else None
    return await fake.create_user(username, source, role, password_hash, **extra)


async def make_client(fake):
    from auth.seed import seed_builtin_accounts

    await seed_builtin_accounts()
    app = web.Application()
    auth.setup(app, {'prod': {'online': True}})

    async def whoami(request):
        principal = principal_of(request)
        return web.json_response({'user': principal.username, 'via': principal.via})

    app.router.add_get('/api/agent/pods', whoami)
    app.router.add_get('/ws', whoami)
    app.router.add_route('*', '/api/{tail:.*}', whoami)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


async def login(client, username, password, **extra):
    return await client.post('/api/auth/login', json={'username': username, 'password': password, **extra})


@pytest.mark.asyncio
async def test_builtin_admin_must_change_factory_password_first(fake):
    client = await make_client(fake)
    try:
        response = await login(client, 'kubedoor', 'kubedoor')
        assert response.status == 200
        cookie = response.cookies['kubedoor_session']
        assert cookie['httponly'] and cookie['samesite'] == 'Strict'
        assert (await response.json())['data']['must_change_password'] is True
        blocked = await client.get('/api/agent/pods', params={'env': 'prod'})
        assert blocked.status == 403 and (await blocked.json())['code'] == 'password_change_required'

        weak = await client.post('/api/auth/password', json={'old_password': 'kubedoor', 'new_password': '12345678'})
        assert weak.status == 400
        wrong_old = await client.post('/api/auth/password', json={'old_password': 'nope', 'new_password': 'N3w-strong-pass'})
        assert wrong_old.status == 400
        ok = await client.post('/api/auth/password', json={'old_password': 'kubedoor', 'new_password': 'N3w-strong-pass'})
        assert ok.status == 200
        assert (await client.get('/api/agent/pods', params={'env': 'prod'})).status == 200
        me = await (await client.get('/api/auth/me')).json()
        # 系统管理员对所有集群都是「管理员」权限
        assert me['data']['role'] == 'admin' and me['data']['cluster_permissions'] == {'prod': 'admin', 'staging': 'admin'}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_login_failures_lock_account_and_generic_message(fake):
    client = await make_client(fake)
    try:
        await add_user(fake, 'alice', 'read', 'Alice-pass-1')
        for _ in range(3):
            response = await login(client, 'alice', 'wrong-pass')
            assert response.status == 401 and (await response.json())['message'] == '用户名或口令错误'
        locked = await login(client, 'alice', 'Alice-pass-1')
        assert locked.status == 429 and locked.headers['Retry-After']
        # 不存在的用户与口令错误返回同样的提示
        response = await login(client, 'nobody', 'whatever-1')
        assert response.status == 401 and (await response.json())['message'] == '用户名或口令错误'
        assert (await login(client, "bad'name", 'x')).status == 401
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_agent_account_cannot_use_web_but_can_register_agent(fake):
    client = await make_client(fake)
    try:
        response = await login(client, 'Up4biLko1dNh', 'qCa22jDkfc9y')
        assert response.status == 403
        assert (await client.get('/ws')).status == 401
        ok = await client.get('/ws', headers={'Authorization': basic('Up4biLko1dNh', 'qCa22jDkfc9y')})
        assert ok.status == 200 and (await ok.json())['user'] == 'Up4biLko1dNh'
        await add_user(fake, 'alice', 'rw', 'Alice-pass-1')
        human = await client.get('/ws', headers={'Authorization': basic('alice', 'Alice-pass-1')})
        assert human.status == 403
        api = await client.get('/api/agent/pods', params={'env': 'prod'},
                               headers={'Authorization': basic('Up4biLko1dNh', 'qCa22jDkfc9y')})
        assert api.status == 403
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_cluster_permissions_enforced_on_requests(fake):
    client = await make_client(fake)
    try:
        carol = await add_user(fake, 'carol', 'none', 'Carol-pass-1')
        await fake.set_clusters(carol['id'], {'prod': 'rw'}, 'test')
        assert (await login(client, 'carol', 'Carol-pass-1')).status == 200
        assert (await client.get('/api/agent/pods', params={'env': 'prod'})).status == 200
        assert (await client.get('/api/agent/pods', params={'env': 'staging'})).status == 403
        assert (await client.post('/api/scale', params={'env': 'prod'})).status == 200
        assert (await client.post('/api/scale', params={'env': 'staging'})).status == 403
        me = await (await client.get('/api/auth/me')).json()
        assert me['data']['cluster_permissions'] == {'prod': 'rw', 'staging': 'none'}
        # 操作权限表:{操作: 允许的集群权限级别}(默认 CoreDNS / Istio 修改只有管理员)
        operations = me['data']['operations']
        assert operations['workload.scale'] == ['rw', 'admin'] and operations['network.coredns'] == ['admin']
        # 改授权立即生效
        await fake.set_clusters(carol['id'], {'prod': 'read'}, 'test')
        denied = await client.post('/api/scale', params={'env': 'prod'})
        body = await denied.json()
        assert denied.status == 403 and body['code'] == 'operation_forbidden'
        assert body['message'] == '当前账号在集群 prod 是「只读」权限,不能执行「扩缩容」'
        # 集群「管理员」权限能改 CoreDNS,读写不能
        await fake.set_clusters(carol['id'], {'prod': 'admin', 'staging': 'rw'}, 'test')
        assert (await client.post('/api/agent/coredns/save', params={'env': 'prod'})).status == 200
        assert (await client.post('/api/agent/coredns/save', params={'env': 'staging'})).status == 403
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_anonymous_cross_site_and_browser_basic_are_rejected(fake):
    client = await make_client(fake)
    try:
        await add_user(fake, 'alice', 'read', 'Alice-pass-1')
        assert (await client.get('/api/agent/pods', params={'env': 'prod'})).status == 401
        # 脚本 / MCP 客户端可以用 Basic 认证
        scripted = await client.get('/api/agent/pods', params={'env': 'prod'},
                                    headers={'Authorization': basic('alice', 'Alice-pass-1')})
        assert scripted.status == 200 and (await scripted.json())['via'] == 'basic'
        # 浏览器缓存的 Basic 凭据不认(防跨站利用),必须走登录
        browser = await client.get('/api/agent/pods', params={'env': 'prod'},
                                   headers={'Authorization': basic('alice', 'Alice-pass-1'), 'Sec-Fetch-Mode': 'cors',
                                            'Sec-Fetch-Site': 'same-origin'})
        assert browser.status == 401
        assert (await login(client, 'alice', 'Alice-pass-1')).status == 200
        cross = await client.get('/api/agent/pods', params={'env': 'prod'}, headers={'Sec-Fetch-Site': 'cross-site'})
        assert cross.status == 403
        same = await client.get('/api/agent/pods', params={'env': 'prod'}, headers={'Sec-Fetch-Site': 'same-origin'})
        assert same.status == 200
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_internal_token_acts_as_named_user(fake):
    client = await make_client(fake)
    try:
        await add_user(fake, 'alice', 'read', 'Alice-pass-1')
        headers = {'X-Kubedoor-Token': TOKEN, 'X-User-Name': 'alice', 'X-User-Permission': 'rw'}
        assert (await client.get('/api/agent/pods', params={'env': 'prod'}, headers=headers)).status == 200
        assert (await client.post('/api/scale', params={'env': 'prod'}, headers=headers)).status == 403
        assert (await client.get('/api/agent/pods', params={'env': 'prod'},
                                 headers={'X-Kubedoor-Token': 'bad', 'X-User-Name': 'alice'})).status == 401
        # 只有 token 没有用户 = 系统调用(CronJob)
        system = await client.get('/api/cron_peak_data', headers={'X-Kubedoor-Token': TOKEN})
        assert system.status == 200
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_initial_password_only_blocks_password_entry_points(fake):
    client = await make_client(fake)
    try:
        guest = await add_user(fake, 'wecom-guest', 'read', 'Guest-pass-1', must_change=True)
        internal = {'X-Kubedoor-Token': TOKEN, 'X-User-Name': 'wecom-guest'}
        # AI 服务代表账号调用(企微机器人):不用这个账号的口令,不要求先改初始口令,权限照常按库里计算
        allowed = await client.get('/api/agent/pods', params={'env': 'prod'}, headers=internal)
        assert allowed.status == 200 and (await allowed.json())['via'] == 'internal'
        assert (await client.post('/api/scale', params={'env': 'prod'}, headers=internal)).status == 403
        # 用口令进来的照旧要先改:Basic(脚本 / MCP 客户端)、nginx 的 verify、Web 登录后的会话
        password = {'Authorization': basic('wecom-guest', 'Guest-pass-1')}
        blocked = await client.get('/api/agent/pods', params={'env': 'prod'}, headers=password)
        assert blocked.status == 403 and (await blocked.json())['code'] == 'password_change_required'
        assert (await client.get('/api/auth/verify?for=mcp', headers=password)).status == 403
        assert (await login(client, 'wecom-guest', 'Guest-pass-1')).status == 200
        assert (await client.get('/api/auth/verify?for=session')).status == 403
        # 停用的账号内部调用也拒绝
        await fake.update_user(guest['id'], enabled=False)
        assert (await client.get('/api/agent/pods', params={'env': 'prod'}, headers=internal)).status == 403
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_verify_endpoint_for_nginx_auth_request(fake):
    client = await make_client(fake)
    try:
        await add_user(fake, 'alice', 'read', 'Alice-pass-1')
        assert (await client.get('/api/auth/verify?for=session')).status == 401
        alert = await client.get('/api/auth/verify?for=alert',
                                 headers={'Authorization': basic('Up4biLko1dNh', 'qCa22jDkfc9y')})
        assert alert.status == 204
        reader_alert = await client.get('/api/auth/verify?for=alert',
                                        headers={'Authorization': basic('alice', 'Alice-pass-1')})
        assert reader_alert.status == 403
        mcp = await client.get('/api/auth/verify?for=mcp', headers={'Authorization': basic('alice', 'Alice-pass-1')})
        assert mcp.status == 204 and mcp.headers['X-Kubedoor-User'] == 'alice'
        assert (await client.get('/api/auth/verify?for=grafana',
                                 headers={'Authorization': basic('alice', 'Alice-pass-1')})).status == 401
        await login(client, 'alice', 'Alice-pass-1')
        grafana = await client.get('/api/auth/verify?for=grafana')
        assert grafana.status == 204 and grafana.headers['X-Kubedoor-Grafana-Role'] == 'Viewer'
        assert grafana.headers['X-Kubedoor-Role'] == 'read' and grafana.headers['X-Kubedoor-Clusters'] == '{}'
        cross = await client.get('/api/auth/verify?for=grafana', headers={'Sec-Fetch-Site': 'cross-site'})
        assert cross.status == 401
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_logout_and_password_change_revoke_sessions(fake):
    client = await make_client(fake)
    other = None
    try:
        await add_user(fake, 'alice', 'read', 'Alice-pass-1')
        assert (await login(client, 'alice', 'Alice-pass-1')).status == 200
        other = TestClient(client.server)
        await other.start_server()
        assert (await login(other, 'alice', 'Alice-pass-1')).status == 200
        changed = await client.post('/api/auth/password', json={'old_password': 'Alice-pass-1', 'new_password': 'Alice-pass-2'})
        assert changed.status == 200
        assert (await client.get('/api/auth/me')).status == 200  # 当前会话保留
        assert (await other.get('/api/auth/me')).status == 401  # 其它终端下线
        assert (await client.post('/api/auth/logout')).status == 200
        assert (await client.get('/api/auth/me')).status == 401
    finally:
        if other:
            await other.close()
        await client.close()


async def admin_client(fake):
    client = await make_client(fake)
    kubedoor = await fake.get_user_by_name('kubedoor')
    await fake.set_password(kubedoor['id'], passwords.hash_password('Admin-pass-1'))
    assert (await login(client, 'kubedoor', 'Admin-pass-1')).status == 200
    return client, kubedoor


@pytest.mark.asyncio
async def test_super_admin_cannot_be_managed_by_anyone(fake):
    client, kubedoor = await admin_client(fake)
    second = None
    try:
        uid = kubedoor['id']
        assert (await client.put(f'/api/admin/users/{uid}', json={'role': 'read'})).status == 403
        assert (await client.put(f'/api/admin/users/{uid}', json={'enabled': False})).status == 403
        assert (await client.delete(f'/api/admin/users/{uid}')).status == 403
        assert (await client.put(f'/api/admin/users/{uid}/password', json={'password': 'Other-pass-1'})).status == 403
        assert (await client.put(f'/api/admin/users/{uid}/clusters', json={'clusters': []})).status == 403
        assert (await client.post(f'/api/admin/users/{uid}/logout')).status == 403
        # 另一个管理员同样不能动 kubedoor
        await add_user(fake, 'ops-admin', 'admin', 'Ops-admin-1')
        second = TestClient(client.server)
        await second.start_server()
        assert (await login(second, 'ops-admin', 'Ops-admin-1')).status == 200
        assert (await second.put(f'/api/admin/users/{uid}/password', json={'password': 'Other-pass-1'})).status == 403
        assert (await second.delete(f'/api/admin/users/{uid}')).status == 403
    finally:
        if second:
            await second.close()
        await client.close()


@pytest.mark.asyncio
async def test_admin_user_management_rules(fake):
    client, _ = await admin_client(fake)
    try:
        created = await client.post('/api/admin/users', json={
            'username': 'bob', 'source': 'local', 'role': 'read', 'password': 'Bob-pass-12'})
        assert created.status == 200
        bob = (await created.json())['data']
        assert bob['must_change_password'] is True and 'password_hash' not in bob
        duplicate = await client.post('/api/admin/users', json={
            'username': 'BOB', 'source': 'local', 'role': 'read', 'password': 'Bob-pass-12'})
        assert duplicate.status == 409
        assert (await client.post('/api/admin/users', json={'username': 'x', 'source': 'ldap', 'role': 'agent'})).status == 400

        grant = await client.put(f"/api/admin/users/{bob['id']}/clusters",
                                 json={'clusters': [{'env': 'prod', 'permission': 'rw'}, {'env': 'staging', 'permission': 'none'}]})
        assert grant.status == 200
        bad = await client.put(f"/api/admin/users/{bob['id']}/clusters", json={'clusters': [{'env': 'prod', 'permission': 'owner'}]})
        assert bad.status == 400
        # 集群权限可以是「管理员」;全局角色可以是「集群管理员」(集群授权保留)
        admin_grant = await client.put(f"/api/admin/users/{bob['id']}/clusters",
                                       json={'clusters': [{'env': 'prod', 'permission': 'admin'}]})
        assert admin_grant.status == 200 and fake.grants[bob['id']] == {'prod': 'admin'}
        assert (await client.put(f"/api/admin/users/{bob['id']}", json={'role': 'cluster_admin'})).status == 200
        assert fake.grants[bob['id']] == {'prod': 'admin'} and fake.users[bob['id']]['role'] == 'cluster_admin'
        assert (await client.put(f"/api/admin/users/{bob['id']}", json={'role': 'owner'})).status == 400
        # 设为管理员后集群授权清空,且不能再指定
        assert (await client.put(f"/api/admin/users/{bob['id']}", json={'role': 'admin'})).status == 200
        assert fake.grants.get(bob['id']) is None
        refused = await client.put(f"/api/admin/users/{bob['id']}/clusters", json={'clusters': [{'env': 'prod', 'permission': 'read'}]})
        assert refused.status == 400

        agent = await fake.get_user_by_name('Up4biLko1dNh')
        assert (await client.delete(f"/api/admin/users/{agent['id']}")).status == 400  # 内置账号不能删
        reset = await client.put(f"/api/admin/users/{agent['id']}/password", json={'password': 'New-agent-pass-1'})
        assert reset.status == 200
        assert (await fake.get_user(agent['id']))[0]['must_change_password'] is False  # Agent 账号不需要改密

        me = await fake.get_user_by_name('kubedoor')
        assert (await client.put(f"/api/admin/users/{bob['id']}", json={'enabled': False})).status == 200
        assert (await client.delete(f"/api/admin/users/{bob['id']}")).status == 200
        assert me['builtin']
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_disabled_user_and_reset_password_kick_sessions(fake):
    client, _ = await admin_client(fake)
    user_client = TestClient(client.server)
    await user_client.start_server()
    try:
        alice = await add_user(fake, 'alice', 'read', 'Alice-pass-1')
        assert (await login(user_client, 'alice', 'Alice-pass-1')).status == 200
        assert (await client.put(f"/api/admin/users/{alice['id']}/password", json={'password': 'Reset-pass-1'})).status == 200
        assert (await user_client.get('/api/auth/me')).status == 401
        response = await login(user_client, 'alice', 'Reset-pass-1')
        assert (await response.json())['data']['must_change_password'] is True
        assert (await client.put(f"/api/admin/users/{alice['id']}", json={'enabled': False})).status == 200
        assert (await user_client.get('/api/auth/me')).status == 401
        assert (await login(user_client, 'alice', 'Reset-pass-1')).status == 403
    finally:
        await user_client.close()
        await client.close()


@pytest.mark.asyncio
async def test_non_admin_cannot_use_admin_api(fake):
    client = await make_client(fake)
    try:
        await add_user(fake, 'bob', 'rw', 'Bob-pass-12')
        await login(client, 'bob', 'Bob-pass-12')
        assert (await client.get('/api/admin/users')).status == 403
        assert (await client.post('/api/db/agent/collect', json={'env': 'prod'})).status == 403  # Agent 管理
        assert (await client.post('/api/scale', params={'env': 'prod'})).status == 200
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_ldap_login_auto_creates_user_with_default_role(fake, monkeypatch):
    client = await make_client(fake)
    try:
        calls = []

        def fake_authenticate(cfg, username, password):
            calls.append(username)
            return {'dn': f'uid={username},dc=x', 'username': username, 'display_name': 'Dave D', 'email': 'd@x'} \
                if password == 'Ldap-pass-1' else None

        monkeypatch.setattr(ldap_auth, 'authenticate', fake_authenticate)
        # LDAP 未启用:不存在的用户直接失败
        assert (await login(client, 'dave', 'Ldap-pass-1')).status == 401
        fake.settings['ldap'] = {**ldap_auth.DEFAULTS, 'enabled': True, 'default_role': 'none'}
        assert (await login(client, 'dave', 'bad-pass-1')).status == 401
        response = await login(client, 'dave', 'Ldap-pass-1')
        assert response.status == 200
        data = (await response.json())['data']
        assert data['source'] == 'ldap' and data['role'] == 'none' and data['display_name'] == 'Dave D'
        assert data['can_change_password'] is False
        # LDAP 用户不能在 KubeDoor 改密;本地同名账号优先于 LDAP
        assert (await client.post('/api/auth/password', json={'old_password': 'x', 'new_password': 'y'})).status == 400
        await add_user(fake, 'erin', 'read', 'Erin-local-1')
        assert (await login(client, 'erin', 'Ldap-pass-1')).status == 401
        assert 'erin' not in calls
        fake.settings['ldap']['auto_create'] = False
        assert (await login(client, 'frank', 'Ldap-pass-1')).status == 403
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_grafana_only_for_users_who_can_see_every_cluster(fake):
    client = await make_client(fake)
    try:
        dave = await add_user(fake, 'dave', 'read', 'Dave-pass-12')
        await fake.set_clusters(dave['id'], {'staging': 'none'}, 'test')
        response = await login(client, 'dave', 'Dave-pass-12')
        assert (await response.json())['data']['all_clusters'] is False
        # Grafana 的看板和 SQL 数据源做不到按集群隔离,有「无」权限集群的人不能用
        assert (await client.get('/api/auth/verify?for=grafana')).status == 403
        assert (await client.get('/api/auth/verify?for=session')).status == 204
        await fake.set_clusters(dave['id'], {}, 'test')
        assert (await client.get('/api/auth/verify?for=grafana')).status == 204
        assert (await (await client.get('/api/auth/me')).json())['data']['all_clusters'] is True
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_permission_policy_admin_api(fake, monkeypatch):
    from auth import admin_api

    audits = []
    monkeypatch.setattr(admin_api.audit, 'record_bg', lambda username, action, **kw: audits.append((username, action, kw)))
    client, _ = await admin_client(fake)
    user_client = TestClient(client.server)
    await user_client.start_server()
    try:
        await add_user(fake, 'bob', 'rw', 'Bob-pass-12')
        assert (await login(user_client, 'bob', 'Bob-pass-12')).status == 200
        assert (await user_client.get('/api/admin/permission-policy')).status == 403  # 只有系统管理员
        assert (await user_client.post('/api/scale', params={'env': 'prod'})).status == 200

        data = (await (await client.get('/api/admin/permission-policy')).json())['data']
        assert [d['key'] for d in data['definitions']][:3] == ['pod.shell', 'node.shell', 'pod.exec.view']
        assert data['policy'] == data['defaults'] and data['updated_at'] is None and data['version'] == 0
        assert 'find -delete' in data['policy']['commands']['danger'] and 'curl' in data['builtin_checks']

        ops, commands = data['policy']['ops'], data['policy']['commands']
        bad = await client.put('/api/admin/permission-policy', json={
            'ops': {**ops, 'workload.scale': {'read': True, 'rw': False, 'admin': True, 'approval': True}},
            'commands': commands})
        assert bad.status == 400 and '只读 ⊆ 读写 ⊆ 管理员' in (await bad.json())['message']
        bad = await client.put('/api/admin/permission-policy', json={'ops': ops, 'commands': {
            'view': commands['view'], 'danger': [*commands['danger'], 'rm -rf; reboot']}})
        assert bad.status == 400 and 'shell 元字符' in (await bad.json())['message']

        # 去掉读写列的扩缩容、加一个查看命令:立即生效,审计记差异
        ops['workload.scale']['rw'] = False
        saved = await client.put('/api/admin/permission-policy', json={
            'ops': ops, 'commands': {'view': [*commands['view'], 'jcmd'], 'danger': commands['danger']}})
        assert saved.status == 200
        saved_data = (await saved.json())['data']
        assert saved_data['policy']['ops']['workload.scale']['rw'] is False and saved_data['updated_at']
        assert saved_data['version'] > 0
        denied = await user_client.post('/api/scale', params={'env': 'prod'})
        assert denied.status == 403 and (await denied.json())['code'] == 'operation_forbidden'
        me = (await (await user_client.get('/api/auth/me')).json())['data']
        assert me['operations']['workload.scale'] == ['admin']
        policy_audits = [kw for _, action, kw in audits if action == 'permission.policy']
        assert len(policy_audits) == 1  # 校验失败的不记
        assert policy_audits[0]['detail'] == {
            'ops': {'workload.scale': {'name': '扩缩容', 'rw': {'from': True, 'to': False}}},
            'commands': {'view': ['+jcmd']}}
        again = (await (await client.get('/api/admin/permission-policy')).json())['data']
        assert again['policy']['commands']['view'][-1] == 'jcmd' and again['updated_by'] == 'kubedoor'
    finally:
        await user_client.close()
        await client.close()
