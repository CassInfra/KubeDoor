import asyncio
import base64
import copy
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer

from kubedoor_setup.api import SetupAPI, create_app
from kubedoor_setup.config import (alerting_choice, default_private, ensure_secrets, final_config, metrics_config,
                                   postgres_config, public_config, selected_components)
from kubedoor_setup.errors import SetupError
from kubedoor_setup.repository import Repository, new_state, public_marker

KEY = '42' * 32
ID = '85c38b85-749a-4630-bb21-0b7f7e024430'
PG = {'host': 'pg.example.com', 'port': 5432, 'database': 'kubedoor', 'user': 'kubedoor', 'password': 'super-secret-pg', 'sslmode': 'prefer'}
VM = {'type': 'Victoria-Metrics-Single', 'scheme': 'https', 'host': 'vm.example.com', 'tenant': '0', 'user': 'monit', 'password': 'super-secret-vm',
      'read_url': 'https://monit:super-secret-vm@vm.example.com:8428',
      'write_url': 'https://monit:super-secret-vm@vm.example.com:8428/api/v1/write', 'tag_key': 'origin_prometheus'}
MASKED_VM = {**VM, 'password': '', 'read_url': 'https://monit:******@vm.example.com:8428',
             'write_url': 'https://monit:******@vm.example.com:8428/api/v1/write'}
FINAL = {'admin': {'username': 'kubedoor', 'password': 'robust-admin-secret'}, 'public_url': 'https://kd.example.com', 'components': {'alerting': True}}


class FakeKubernetes:
    namespace = 'kubedoor'
    server = 'https://kubernetes.default.svc'

    def __init__(self):
        self.version = 1
        self.permissions = True
        self.resources = {
            '/api/v1/namespaces/kubedoor/configmaps/kubedoor-setup-state': {
                'apiVersion': 'v1', 'kind': 'ConfigMap',
                'metadata': {'name': 'kubedoor-setup-state', 'namespace': 'kubedoor', 'resourceVersion': '1',
                             'annotations': {'setup.kubedoor.io/installation-id': ID}},
                'data': {'state.json': '{}', 'status.json': json.dumps({'phase': 'draft', 'installation_id': ID})},
            }
        }

    async def allowed(self, *args, **kwargs):
        return self.permissions

    async def request(self, method, path, *, body=None, missing_ok=False, **kwargs):
        if path == '/version':
            return {'gitVersion': 'v1.30.0'}
        if method == 'GET':
            value = self.resources.get(path)
            if value is None and not missing_ok:
                raise SetupError('Missing', 404)
            return copy.deepcopy(value)
        if method == 'POST':
            path += '/' + body['metadata']['name']
            if path in self.resources:
                raise SetupError('Conflict', 409)
        elif method == 'PUT':
            if body['metadata'].get('resourceVersion') != self.resources[path]['metadata']['resourceVersion']:
                raise SetupError('Conflict', 409)
        else:
            raise AssertionError(method)
        self.version += 1
        value = copy.deepcopy(body)
        value['metadata']['resourceVersion'] = str(self.version)
        self.resources[path] = value
        return copy.deepcopy(value)


async def make_client(*, k8s=None, pg=None, vm=None, runner=None):
    repo = Repository(k8s or FakeKubernetes())
    app = create_app(repo, key=KEY, pg_probe=pg or AsyncMock(return_value={'status': 'success', 'message': 'PG verified'}),
                     metrics_probe=vm or AsyncMock(return_value={'status': 'success', 'message': 'VM verified'}),
                     runner_factory=runner)
    client = TestClient(TestServer(app), cookie_jar=CookieJar(unsafe=True))
    await client.start_server()
    return client, repo


async def authorize(client):
    response = await client.post('/setup-api/session', json={'key': KEY})
    assert response.status == 200


async def config_ready(client):
    await authorize(client)
    response = await client.get('/setup-api/state')
    state = (await response.json())['data']
    response = await client.post('/setup-api/check/postgres', json={'revision': state['revision'], 'postgres': PG})
    assert response.status == 200
    state = (await response.json())['data']
    response = await client.post('/setup-api/check/metrics', json={'revision': state['revision'], 'metrics': VM})
    assert response.status == 200
    state = (await response.json())['data']
    response = await client.put('/setup-api/configuration', json={'revision': state['revision'], **FINAL})
    assert response.status == 200
    return (await response.json())['data']


@pytest.mark.asyncio
async def test_auth_required_and_key_session_cookie_survives_restart():
    k8s = FakeKubernetes()
    client, repo = await make_client(k8s=k8s)
    async with client:
        assert (await client.get('/setup-api/state')).status == 401
        status = await (await client.get('/setup-api/status')).json()
        assert status['data'] == {'phase': 'draft', 'authenticated': False, 'installation_id': ID}
        response = await client.post('/setup-api/session', json={'key': KEY})
        cookie = response.cookies['kubedoor_setup_session']
        assert cookie['httponly'] and cookie['samesite'] == 'Strict'
        assert cookie['path'] == '/setup-api'
        token = cookie.value
    restarted, _ = await make_client(k8s=k8s)
    async with restarted:
        response = await restarted.get('/setup-api/state', headers={'Cookie': 'kubedoor_setup_session=' + token})
        assert response.status == 200


@pytest.mark.asyncio
async def test_rate_limit_and_cross_site_rejected():
    client, _ = await make_client()
    async with client:
        response = await client.post('/setup-api/session', json={'key': KEY}, headers={'Origin': 'https://attacker.example.com'})
        assert response.status == 403
        for _ in range(10):
            assert (await client.post('/setup-api/session', json={'key': 'incorrect'})).status == 401
        assert (await client.post('/setup-api/session', json={'key': KEY})).status == 429


@pytest.mark.asyncio
async def test_failed_check_persists_retryable_draft_without_exposing_password():
    pg = AsyncMock(side_effect=SetupError('数据库密码验证失败', 422))
    client, repo = await make_client(pg=pg)
    async with client:
        await authorize(client)
        response = await client.post('/setup-api/check/postgres', json={'revision': 0, 'postgres': PG})
        assert response.status == 422
        result = await response.json()
        assert result['data']['revision'] == 1
        assert result['data']['config']['postgres']['has_password'] is True
        assert 'super-secret' not in json.dumps(result)
        assert 'private_key' not in result['data']
        assert result['data']['checks'][0]['status'] == 'error'
        private = await repo.load_private()
        assert private['config']['postgres']['password'] == PG['password']
        state_cm = repo.k8s.resources[repo.base + '/configmaps/kubedoor-setup-state']
        assert PG['password'] not in json.dumps(state_cm)


@pytest.mark.asyncio
@pytest.mark.parametrize('section,expected_step', [('postgres', 2), ('metrics', 3)])
@pytest.mark.parametrize('target_locked', [False, True])
async def test_failed_recheck_restores_appropriate_step_without_resetting_active_install(section, expected_step, target_locked):
    pg = AsyncMock(return_value={'status': 'success', 'message': 'PG verified'})
    vm = AsyncMock(return_value={'status': 'success', 'message': 'VM verified'})
    client, repo = await make_client(pg=pg, vm=vm)
    async with client:
        await config_ready(client)
        state = await repo.load()
        state.update(step=5, target_locked=target_locked)
        if target_locked:
            state['phase'] = 'failed'
        await repo.save(state)
        (pg if section == 'postgres' else vm).side_effect = SetupError('连接检测失败', 422)
        response = await client.post('/setup-api/check/' + section,
                                     json={'revision': state['revision'], section: PG if section == 'postgres' else VM})
        assert response.status == 422
        reloaded = (await (await client.get('/setup-api/state')).json())['data']
        assert reloaded['step'] == (5 if target_locked else expected_step)
        assert reloaded['phase'] == ('failed' if target_locked else 'draft')


@pytest.mark.asyncio
async def test_saved_password_preserved_and_stale_revision_rejected():
    client, repo = await make_client()
    async with client:
        state = await config_ready(client)
        assert state['phase'] == 'ready'
        assert state['config']['admin']['has_password'] is True
        assert 'robust-admin-secret' not in json.dumps(state)
        assert state['config']['metrics']['read_url'] == MASKED_VM['read_url']
        assert state['config']['metrics']['has_password'] is True
        assert 'super-secret-vm' not in json.dumps(state)
        response = await client.post('/setup-api/check/metrics', json={'revision': state['revision'], 'metrics': MASKED_VM})
        assert response.status == 200
        state = (await response.json())['data']
        saved = (await repo.load_private())['config']['metrics']
        assert (saved['read_url'], saved['write_url'], saved['password']) == (VM['read_url'], VM['write_url'], VM['password'])
        response = await client.post('/setup-api/check/postgres', json={'revision': 0, 'postgres': PG})
        assert response.status == 409
        response = await client.post('/setup-api/check/postgres', json={'revision': state['revision'], 'postgres': {**PG, 'password': ''}})
        assert response.status == 200
        assert (await repo.load_private())['config']['postgres']['password'] == PG['password']


@pytest.mark.asyncio
async def test_start_once_waits_for_runner_and_blocks_config_changes():
    release = asyncio.Event()
    runs = []

    class Runner:
        def __init__(self, repo, k8s):
            self.repo = repo

        async def run(self):
            runs.append(True)
            await release.wait()
            async with self.repo.lock:
                state = await self.repo.load()
                state['phase'] = 'succeeded'
                await self.repo.save(state)

    client, repo = await make_client(runner=Runner)
    async with client:
        state = await config_ready(client)
        response = await client.post('/setup-api/start', json={'revision': state['revision']})
        assert response.status == 202
        running = (await response.json())['data']
        assert running['phase'] == 'running'
        assert (await client.post('/setup-api/start', json={'revision': running['revision']})).status == 409
        assert (await client.put('/setup-api/configuration', json={'revision': running['revision'], **FINAL})).status == 409
        assert runs == [True]
        release.set()
        for _ in range(50):
            if (await repo.load())['phase'] == 'succeeded':
                break
            await asyncio.sleep(.01)
        assert (await repo.load())['phase'] == 'succeeded'
        assert (await client.post('/setup-api/session', json={'key': KEY})).status == 409


@pytest.mark.asyncio
async def test_retry_uses_new_private_run_token():
    run_tokens = []

    class FailingRunner:
        def __init__(self, repo, k8s):
            self.repo = repo

        async def run(self):
            async with self.repo.lock:
                state = await self.repo.load()
                run_tokens.append(state['run_id'])
                state['phase'] = 'failed'
                await self.repo.save(state)

    client, repo = await make_client(runner=FailingRunner)
    async with client:
        state = await config_ready(client)
        first = await client.post('/setup-api/start', json={'revision': state['revision']})
        assert first.status == 202
        assert 'run_id' not in (await first.json())['data']
        for _ in range(50):
            state = await repo.load()
            if state['phase'] == 'failed':
                break
            await asyncio.sleep(.01)
        retried = await client.post('/setup-api/retry', json={'revision': state['revision']})
        assert retried.status == 202
        assert 'run_id' not in (await retried.json())['data']
        for _ in range(50):
            if len(run_tokens) == 2:
                break
            await asyncio.sleep(.01)
        assert len(run_tokens) == 2
        assert len(run_tokens[0]) == len(run_tokens[1]) == 32
        assert run_tokens[0] != run_tokens[1]


@pytest.mark.asyncio
async def test_old_runner_failure_cannot_mark_new_attempt_failed():
    repo = Repository(FakeKubernetes())
    state = await repo.load()
    state.update(phase='running', run_id='new-run')
    await repo.save(state)
    api = SetupAPI(repo, key=KEY)
    await api._mark_failed('old worker error', run_id='old-run')
    assert (await repo.load())['phase'] == 'running'
    await api._mark_failed('new worker error', run_id='new-run')
    assert (await repo.load())['phase'] == 'failed'


@pytest.mark.asyncio
async def test_missing_environment_permission_blocks_start():
    client, repo = await make_client(runner=lambda *_: None)
    async with client:
        state = await config_ready(client)
        repo.k8s.permissions = False
        response = await client.post('/setup-api/start', json={'revision': state['revision']})
        assert response.status == 409
        assert (await repo.load())['phase'] == 'ready'


@pytest.mark.asyncio
async def test_alerting_switch_is_saved_and_validated():
    client, repo = await make_client()
    async with client:
        state = await config_ready(client)
        assert state['config']['components'] == {'alerting': True}
        response = await client.put('/setup-api/configuration', json={
            'revision': state['revision'], **FINAL, 'components': {'alerting': False}})
        assert response.status == 200
        saved = (await response.json())['data']
        assert saved['config']['components'] == {'alerting': False}
        assert (await repo.load_private(await repo.load()))['config']['components'] == {'alerting': False}
        # 不再接受逐项选择 Grafana / vmalert / Alertmanager
        for bad in ({'alerting': 'false'}, {'grafana': True, 'vmalert': True, 'alertmanager': True}, ['alerting']):
            response = await client.put('/setup-api/configuration', json={
                'revision': saved['revision'], **FINAL, 'components': bad})
            assert response.status == 422


@pytest.mark.asyncio
async def test_started_install_keeps_alerting_choice_including_older_saved_selection():
    client, repo = await make_client()
    async with client:
        await config_ready(client)
        state = await repo.load()
        state.update(phase='failed', target_locked=True)
        private = await repo.load_private(state)
        # 旧版向导逐项保存的选择：vmalert 和 Alertmanager 都选了就是安装告警组件
        private['config']['components'] = {'grafana': True, 'vmalert': True, 'alertmanager': True}
        await repo.write(state, private)
        response = await client.put('/setup-api/configuration', json={
            'revision': state['revision'], **FINAL, 'components': {'alerting': False}})
        assert response.status == 409
        response = await client.put('/setup-api/configuration', json={'revision': state['revision'], **FINAL})
        assert response.status == 200
        assert (await response.json())['data']['config']['components'] == {'alerting': True}


@pytest.mark.asyncio
async def test_oss_url_is_optional_saved_masked_and_keeps_saved_password():
    client, repo = await make_client()
    async with client:
        state = await config_ready(client)
        # 没提交这一项（旧版页面）：不配置
        assert state['config']['oss_url'] == ''
        response = await client.put('/setup-api/configuration', json={
            'revision': state['revision'], **FINAL, 'oss_url': ' https://ak:oss-secret@oss.example.com/bucket/ '})
        assert response.status == 200
        saved = (await response.json())['data']
        # 页面只拿到打码的地址（第 5 步摘要也显示它），去掉了末尾的 /
        assert saved['config']['oss_url'] == 'https://ak:******@oss.example.com/bucket'
        assert 'oss-secret' not in json.dumps(saved)
        stored = 'https://ak:oss-secret@oss.example.com/bucket'
        assert (await repo.load_private(await repo.load()))['config']['oss_url'] == stored
        # 原样提交打码的地址表示沿用已保存的密码；不提交这一项也沿用
        for body in ({'oss_url': saved['config']['oss_url']}, {}):
            response = await client.put('/setup-api/configuration', json={'revision': saved['revision'], **FINAL, **body})
            assert response.status == 200
            saved = (await response.json())['data']
            assert (await repo.load_private(await repo.load()))['config']['oss_url'] == stored
        # 换了账号要重新填密码；查询参数、本机地址不接受
        for bad, reason in (('https://other:******@oss.example.com/bucket', '密码需要重新填写'),
                            ('https://oss.example.com/upload?token=x', '不含查询参数'),
                            ('http://127.0.0.1:9000', '不能使用本机'), (7, '格式不正确')):
            response = await client.put('/setup-api/configuration', json={'revision': saved['revision'], **FINAL, 'oss_url': bad})
            assert response.status == 422 and reason in (await response.json())['message']
        # 清空表示不配置
        response = await client.put('/setup-api/configuration', json={'revision': saved['revision'], **FINAL, 'oss_url': ''})
        assert response.status == 200 and (await response.json())['data']['config']['oss_url'] == ''
        assert (await repo.load_private(await repo.load()))['config']['oss_url'] == ''


def test_component_selection_keeps_grafana_and_ties_alerting_services():
    assert selected_components({'alerting': False}) == {'grafana': True, 'alarm': False, 'vmalert': False, 'alertmanager': False}
    assert selected_components({'alerting': True}) == {'grafana': True, 'alarm': True, 'vmalert': True, 'alertmanager': True}
    assert selected_components(None) == selected_components({}) == selected_components({'alerting': True})
    # 旧版逐项保存：Grafana 不再可选；vmalert、Alertmanager 都没选才是不装告警组件
    assert selected_components({'grafana': False, 'vmalert': False, 'alertmanager': False})['grafana'] is True
    assert alerting_choice({'grafana': True, 'vmalert': False, 'alertmanager': False}) is False
    assert alerting_choice({'grafana': True, 'vmalert': False, 'alertmanager': True}) is True
    assert public_config(default_private()['config'])['components'] == {'alerting': True}


class ForbiddenPods(FakeKubernetes):
    async def request(self, method, path, **kwargs):
        if '/pods/' in path:
            raise SetupError('安装权限不足或准入策略拒绝（HTTP 403）', 403)
        return await super().request(method, path, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize('k8s_class, host_ip, node_port, expected', [
    (FakeKubernetes, '192.0.2.10', 30080, 'http://192.0.2.10:30080'),
    (FakeKubernetes, 'fd00::10', 30080, 'http://[fd00::10]:30080'),
    (FakeKubernetes, '', 30080, ''),
    (FakeKubernetes, '192.0.2.10', None, ''),
    (ForbiddenPods, '192.0.2.10', 30080, ''),
])
async def test_environment_reports_web_nodeport_address(monkeypatch, k8s_class, host_ip, node_port, expected):
    monkeypatch.setenv('HOSTNAME', 'kubedoor-setup-abc')
    k8s = k8s_class()
    port = {'name': 'http', 'port': 80, **({'nodePort': node_port} if node_port else {})}
    k8s.resources['/api/v1/namespaces/kubedoor/services/kubedoor-web'] = {'spec': {'type': 'NodePort', 'ports': [port]}}
    k8s.resources['/api/v1/namespaces/kubedoor/pods/kubedoor-setup-abc'] = {'status': {'hostIP': host_ip}}
    client, _ = await make_client(k8s=k8s)
    async with client:
        await authorize(client)
        state = (await (await client.get('/setup-api/state')).json())['data']
        assert state['environment']['web_url'] == expected
        assert state['environment_checked'] is True


@pytest.mark.asyncio
async def test_environment_checked_by_older_installer_gets_web_address(monkeypatch):
    monkeypatch.setenv('HOSTNAME', 'kubedoor-setup-abc')
    k8s = FakeKubernetes()
    k8s.resources['/api/v1/namespaces/kubedoor/services/kubedoor-web'] = {'spec': {'ports': [{'port': 80, 'nodePort': 31234}]}}
    k8s.resources['/api/v1/namespaces/kubedoor/pods/kubedoor-setup-abc'] = {'status': {'hostIP': '192.0.2.10'}}
    repo = Repository(k8s)
    state = await repo.load()
    state.update(environment_checked=True, environment={'namespace': 'kubedoor', 'server': k8s.server, 'version': 'v1.30.0'})
    await repo.save(state)
    client, _ = await make_client(k8s=k8s)
    async with client:
        await authorize(client)
        data = (await (await client.get('/setup-api/state')).json())['data']
        assert data['environment']['web_url'] == 'http://192.0.2.10:31234'
        assert data['environment']['version'] == 'v1.30.0'


@pytest.mark.asyncio
async def test_compare_and_swap_does_not_mix_secrets_from_concurrent_edit():
    k8s = FakeKubernetes()
    a, b = Repository(k8s), Repository(k8s)
    state_a, state_b = await a.load(), await b.load()
    private_a, private_b = default_private(), default_private()
    private_a['config']['postgres']['password'] = 'password-A'
    private_b['config']['postgres']['password'] = 'password-B'
    await a.write(state_a, private_a)
    with pytest.raises(SetupError) as error:
        await b.write(state_b, private_b)
    assert error.value.status == 409
    assert (await b.load_private())['config']['postgres']['password'] == 'password-A'


@pytest.mark.asyncio
async def test_private_secret_conflict_not_adopted():
    k8s = FakeKubernetes()
    path = '/api/v1/namespaces/kubedoor/secrets/kubedoor-setup-config'
    k8s.resources[path] = {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': 'kubedoor-setup-config', 'resourceVersion': '2'}, 'data': {'unrelated': 'c2VjcmV0'}}
    repo = Repository(k8s)
    with pytest.raises(SetupError) as error:
        await repo.write(await repo.load(), default_private())
    assert error.value.status == 409
    assert k8s.resources[path]['data'] == {'unrelated': 'c2VjcmV0'}


@pytest.mark.asyncio
async def test_recent_running_not_marked_interrupted_but_stale_is():
    repo = Repository(FakeKubernetes())
    state = await repo.load()
    state['phase'] = 'running'
    await repo.save(state)
    api = SetupAPI(repo, key=KEY)
    await api._recover_once()
    assert (await repo.load())['phase'] == 'running'
    cm = repo.k8s.resources[repo.base + '/configmaps/kubedoor-setup-state']
    stored = json.loads(cm['data']['state.json'])
    stored['updated_at'] = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
    cm['data']['state.json'] = json.dumps(stored)
    await api._recover_once()
    assert (await repo.load())['phase'] == 'interrupted'


@pytest.mark.asyncio
@pytest.mark.parametrize('job_status,expected_phase', [
    ({'failed': 1, 'active': 1}, 'finalizing'),
    ({'conditions': [{'type': 'Failed', 'status': 'True'}]}, 'failed'),
    ({'conditions': [{'type': 'Complete', 'status': 'True'}]}, 'finalizing'),
])
async def test_finalizer_watchdog_only_retries_terminal_failure(job_status, expected_phase):
    repo = Repository(FakeKubernetes())
    state = await repo.load()
    state['phase'] = 'finalizing'
    await repo.save(state)
    repo.k8s.resources['/apis/batch/v1/namespaces/kubedoor/jobs/kubedoor-setup-finalize'] = {'status': job_status}
    api = SetupAPI(repo, key=KEY)
    await api._recover_once()
    assert (await repo.load())['phase'] == expected_phase


@pytest.mark.asyncio
async def test_missing_finalizer_job_given_creation_grace_period():
    repo = Repository(FakeKubernetes())
    state = await repo.load()
    state['phase'] = 'finalizing'
    await repo.save(state)
    api = SetupAPI(repo, key=KEY)
    await api._recover_once()
    assert (await repo.load())['phase'] == 'finalizing'
    cm = repo.k8s.resources[repo.base + '/configmaps/kubedoor-setup-state']
    stored = json.loads(cm['data']['state.json'])
    stored['updated_at'] = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
    cm['data']['state.json'] = json.dumps(stored)
    await api._recover_once()
    assert (await repo.load())['phase'] == 'failed'


@pytest.mark.parametrize('url', ['http://localhost:8428', 'http://127.0.0.1', 'http://169.254.169.254', 'https://vm.example.com?q=secret',
                                 'https://vm.example.com#top', 'https://vm.example.com:99999', 'file:///etc/passwd', 'https://:pw@vm.example.com', ''])
@pytest.mark.parametrize('field', ['read_url', 'write_url'])
def test_metrics_rejects_unsafe_or_ambiguous_endpoints(field, url):
    with pytest.raises(SetupError):
        metrics_config({**VM, field: url}, {})


@pytest.mark.parametrize('host', ['vm.example.com:8428', 'http://vm.example.com', 'vm.example.com/prefix', 'monit@vm.example.com',
                                  'localhost', '127.0.0.1', '[::1]', ''])
def test_metrics_host_has_no_scheme_port_or_path(host):
    with pytest.raises(SetupError):
        metrics_config({**VM, 'host': host}, {})


def test_metrics_config_keeps_victoriametrics_only_and_normalizes_inputs():
    with pytest.raises(SetupError, match='VictoriaMetrics'):
        metrics_config({**VM, 'type': 'Prometheus'}, {})
    with pytest.raises(SetupError, match='协议'):
        metrics_config({**VM, 'scheme': 'ftp'}, {})
    assert metrics_config({**VM, 'host': '[fd00::1]'}, {})['host'] == 'fd00::1'
    assert metrics_config({**VM, 'read_url': VM['read_url'] + '/'}, {})['read_url'] == VM['read_url']
    cluster = {**VM, 'type': 'Victoria-Metrics-Cluster', 'read_url': 'https://monit:super-secret-vm@vm.example.com:8481/select/7:8/prometheus',
               'write_url': 'https://monit:super-secret-vm@vm.example.com:8480/insert/7:8/prometheus'}
    assert metrics_config({**cluster, 'tenant': '7:8'}, {})['tenant'] == '7:8'
    assert metrics_config({**cluster, 'tenant': '007'}, {})['tenant'] == '7'
    assert metrics_config({**cluster, 'tenant': 3}, {})['tenant'] == '3'
    for tenant in ('abc', '-1', '4294967296', '1:2:3'):
        with pytest.raises(SetupError, match='租户'):
            metrics_config({**cluster, 'tenant': tenant}, {})


def test_metrics_endpoint_credentials_are_encoded_and_masked_values_reuse_saved_password():
    # 地址里直接写 @ 和 : 也能解析，统一保存成 URL 编码；/ 会被当成路径开头，必须先编码
    raw = metrics_config({**VM, 'password': 'p@ss:w/rd', 'read_url': 'https://monit:p@ss:word@vm.example.com:8428',
                          'write_url': 'https://monit:p%40ss%3Aw%2Frd@vm.example.com:8428/api/v1/write'}, {})
    assert raw['read_url'] == 'https://monit:p%40ss%3Aword@vm.example.com:8428'
    assert raw['write_url'] == 'https://monit:p%40ss%3Aw%2Frd@vm.example.com:8428/api/v1/write'
    with pytest.raises(SetupError, match='格式不正确'):
        metrics_config({**VM, 'read_url': 'https://monit:p@ss:w/rd@vm.example.com:8428'}, {})
    saved = metrics_config(VM, {})
    assert metrics_config(MASKED_VM, saved)['read_url'] == VM['read_url']
    typed = metrics_config({**MASKED_VM, 'password': 'new-secret'}, saved)
    assert typed['read_url'] == 'https://monit:new-secret@vm.example.com:8428' and typed['password'] == 'new-secret'
    reader = metrics_config({**VM, 'read_url': 'https://reader:read-secret@vm.example.com:8428'}, {})
    reused = metrics_config({**MASKED_VM, 'read_url': 'https://reader:******@vm.example.com:8428'}, reader)
    assert reused['read_url'] == 'https://reader:read-secret@vm.example.com:8428'
    with pytest.raises(SetupError, match='重新填写'):
        metrics_config({**MASKED_VM, 'read_url': 'https://other:******@vm.example.com:8428'}, saved)
    with pytest.raises(SetupError, match='请填写密码'):
        metrics_config(MASKED_VM, {})


def test_metrics_account_and_password_go_together_and_can_be_cleared():
    plain = {**VM, 'user': '', 'password': '', 'read_url': 'https://vm.example.com:8428', 'write_url': 'https://vm.example.com:8428/api/v1/write'}
    assert metrics_config(plain, metrics_config(VM, {}))['password'] == ''
    with pytest.raises(SetupError, match='请填写账号'):
        metrics_config({**plain, 'password': 'orphan'}, {})
    with pytest.raises(SetupError, match='请填写密码'):
        metrics_config({**VM, 'password': ''}, {})


def test_public_config_masks_passwords_inside_metrics_endpoints():
    private = default_private()['config']
    private['metrics'] = metrics_config(VM, {})
    public = public_config(private)
    assert public['metrics']['read_url'] == MASKED_VM['read_url']
    assert public['metrics']['write_url'] == MASKED_VM['write_url']
    assert public['metrics']['has_password'] is True
    assert 'super-secret-vm' not in json.dumps(public)
    assert private['metrics']['read_url'] == VM['read_url']


def test_config_handles_quotes_without_shell_and_vm_prefix_derivation():
    pg = postgres_config({**PG, 'password': "quotes' $(not-a-command) & $value"}, {})
    assert pg['password'] == "quotes' $(not-a-command) & $value"
    with pytest.raises(SetupError):
        final_config({**FINAL, 'admin': {'username': 'kubedoor', 'password': 'password1'}}, {})


def test_postgres_ssl_defaults_to_disable_but_keeps_explicit_choice():
    assert default_private()['config']['postgres']['sslmode'] == 'disable'
    without_mode = {key: value for key, value in PG.items() if key != 'sslmode'}
    assert postgres_config(without_mode, {})['sslmode'] == 'disable'
    assert postgres_config(without_mode, {'sslmode': 'require'})['sslmode'] == 'require'
    assert postgres_config({**PG, 'sslmode': 'verify-full'}, {})['sslmode'] == 'verify-full'


@pytest.mark.parametrize('phase', ['finalizing', 'succeeded'])
def test_public_marker_keeps_progress_without_private_draft(phase):
    state = new_state(ID, 'kubedoor')
    state.update(phase=phase, private_key='private-secret.json', checked={'postgres': 'private-digest'},
                 services=[{'id': 'master', 'name': '控制服务', 'status': 'ready', 'message': 'details omitted'}],
                 checks=[{'name': 'cleanup', 'status': 'running', 'message': 'details omitted'}],
                 error='收尾权限检查失败，将重试')
    marker = public_marker(state)
    assert marker['services'] == [{'id': 'master', 'name': '控制服务', 'status': 'ready'}]
    assert marker['checks'] == [{'name': 'cleanup', 'status': 'running'}]
    assert marker['error'] == '收尾权限检查失败，将重试'
    assert 'private' not in json.dumps(marker)
    assert 'details omitted' not in json.dumps(marker)


@pytest.mark.parametrize('phase', ['finalizing', 'succeeded'])
def test_public_marker_names_deployments_but_keeps_failure_details_private(phase):
    state = new_state(ID, 'kubedoor')
    state.update(phase=phase, services=[
        {'id': 'alarm', 'name': '告警服务', 'resource': 'kubedoor-alarm', 'status': 'ready',
         'message': '本次部署副本及健康检查均已通过',
         'details': ['期望镜像：registry.example.invalid/kubedoor-alarm:2.5.0', 'Pod kubedoor-alarm-abc：Running']},
        {'id': 'grafana', 'name': 'Grafana 看板', 'status': 'ready'},
    ])
    marker = public_marker(state)
    # 安装服务关闭后完成页也能显示 Deployment 名；旧状态没有 resource 时不补
    assert marker['services'] == [
        {'id': 'alarm', 'name': '告警服务', 'resource': 'kubedoor-alarm', 'status': 'ready'},
        {'id': 'grafana', 'name': 'Grafana 看板', 'status': 'ready'},
    ]
    text = json.dumps(marker, ensure_ascii=False)
    assert 'registry.example.invalid' not in text and 'Pod kubedoor-alarm-abc' not in text


@pytest.mark.asyncio
async def test_status_serves_completed_rows_during_projection_delay():
    client, repo = await make_client()
    async with client:
        state = await repo.load()
        state.update(phase='succeeded', services=[{'id': 'web', 'name': 'Web', 'status': 'ready'}])
        await repo.save(state)
        response = await client.get('/setup-api/status')
        data = (await response.json())['data']
        assert data['phase'] == 'succeeded'
        assert data['services'] == [{'id': 'web', 'name': 'Web', 'status': 'ready'}]
