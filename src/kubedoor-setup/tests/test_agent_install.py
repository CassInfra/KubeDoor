"""网页安装第 6 步：在本集群安装 Agent（复用 master 的安装流水线）、跳过、到期与重启恢复。"""
import asyncio
import base64
import copy
import json
import uuid
from datetime import datetime, timedelta, timezone

import aiohttp
import pytest
from aiohttp import CookieJar, web
from aiohttp.test_utils import TestClient, TestServer

from kubedoor_setup.agent import AgentInstall, installer_available, new_agent_state, normalize_options
from kubedoor_setup.api import SetupAPI, create_app
from kubedoor_setup.config import default_private, ensure_secrets
from kubedoor_setup.errors import SetupError
from kubedoor_setup.kubernetes import OWNER, Kubernetes, resource_path
from kubedoor_setup.repository import Repository, new_state, public_marker
from kubedoor_setup.runner import SERVICES
from test_runner import MemoryRepo, ObservedRunner, FakeKubernetes as RunnerKubernetes, setup_probes
from test_setup_api import FakeKubernetes, ID, KEY, PG, VM

CA = b'-----BEGIN CERTIFICATE-----\nMIIBkubedoor-test-ca\n-----END CERTIFICATE-----\n'
TOKEN_PATH = '/api/v1/namespaces/kubedoor/serviceaccounts/kubedoor-agent-installer/token'
SSAR = '/apis/authorization.k8s.io/v1/selfsubjectaccessreviews'
STATE = '/api/v1/namespaces/kubedoor/configmaps/kubedoor-setup-state'
JOB = '/apis/batch/v1/namespaces/kubedoor/jobs/kubedoor-setup-finalize'
SETUP = '/apis/apps/v1/namespaces/kubedoor/deployments/kubedoor-setup'
STEPS = ['验证安装计划', '创建命名空间和凭据', '应用安装资源', '等待工作负载就绪', '等待 Agent 上线', '验证指标采集']
OPTIONS = {'env': 'prod-k8s', 'aliases': ['生产', ' 生产 ', ''], 'namespace': '', 'mode': 'full', 'ksm': 'auto', 'node_exporter': 'install'}


class AgentKubernetes(FakeKubernetes):
    """在 test_setup_api 的假 K8S 上补 TokenRequest、SelfSubjectAccessReview 和收尾 Job。"""
    server = 'https://10.96.0.1:443'

    def __init__(self, tmp_path, granted=True):
        super().__init__()
        self.granted = granted
        self.issued = []
        self.reviews = []
        self.ca_path = tmp_path / 'ca.crt'
        self.ca_path.write_bytes(CA)
        self.resources[SETUP] = {
            'apiVersion': 'apps/v1', 'kind': 'Deployment',
            'metadata': {'name': 'kubedoor-setup', 'namespace': 'kubedoor', 'resourceVersion': '1', 'annotations': {OWNER: ID}},
            'spec': {'template': {'spec': {'containers': [{'name': 'kubedoor-setup', 'image': 'example/setup:1'}],
                                           'imagePullSecrets': [{'name': 'pull-key'}]}}}}

    allowed = Kubernetes.allowed

    async def request(self, method, path, *, body=None, missing_ok=False, token=None, **kwargs):
        if method == 'POST' and path == TOKEN_PATH:
            if not self.granted:
                raise SetupError('安装权限不足或准入策略拒绝（HTTP 403）', 403)
            value = 'installer-token-' + uuid.uuid4().hex
            self.issued.append((value, body['spec']['expirationSeconds']))
            return {'status': {'token': value}}
        if method == 'POST' and path == SSAR:
            if token is None:
                return {'status': {'allowed': self.permissions}}
            self.reviews.append((token, body['spec']['resourceAttributes']))
            return {'status': {'allowed': self.granted and token in {value for value, _ in self.issued}}}
        assert token is None
        return await super().request(method, path, body=body, missing_ok=missing_ok, **kwargs)

    async def get(self, obj, token=None):
        return await self.request('GET', resource_path(obj), missing_ok=True, token=token)

    async def create(self, obj):
        return await self.request('POST', resource_path(obj).rsplit('/', 1)[0], body=obj)

    async def delete(self, obj, token=None):
        self.resources.pop(resource_path(obj), None)


class FakeMaster:
    """master 的 /api/admin/agent-installations：只模拟本步骤用到的接口和返回格式。"""

    def __init__(self):
        self.calls, self.tasks = [], {}
        self.preflight_error = None
        self.reject = None
        self.outcome, self.error, self.polls = 'succeeded', '', 1
        # master 自动配置 AI Kubeconfig 的结果；None 表示旧版 master 没有这项
        self.ai_connection = None
        self.server = None

    async def __aenter__(self):
        app = web.Application()
        prefix = '/api/admin/agent-installations'
        app.router.add_post(prefix + '/inspect', self.inspect)
        app.router.add_post(prefix + '/{id}/preflight', self.preflight)
        app.router.add_post(prefix + '/{id}/start', self.start)
        app.router.add_get(prefix + '/{id}', self.get)
        self.server = TestServer(app)
        await self.server.start_server()
        return self

    async def __aexit__(self, *args):
        await self.server.close()

    def base(self):
        return str(self.server.make_url(''))

    async def record(self, request):
        body = await request.json() if request.can_read_body else None
        self.calls.append({'method': request.method, 'path': request.path, 'headers': dict(request.headers), 'body': body})
        return body

    @staticmethod
    def ok(task):
        return web.json_response({'success': True, 'data': {key: copy.deepcopy(value) for key, value in task.items() if key != 'polls'}})

    def named(self, name):
        return [call for call in self.calls if call['path'].endswith(name)]

    async def inspect(self, request):
        await self.record(request)
        task_id = str(uuid.uuid4())
        self.tasks[task_id] = {'id': task_id, 'status': 'draft', 'revision': 0, 'steps': [], 'checks': [], 'error': '', 'polls': 0}
        return self.ok(self.tasks[task_id])

    async def preflight(self, request):
        await self.record(request)
        if self.reject:
            status, message = self.reject
            return web.json_response({'success': False, 'error': message, 'message': message}, status=status)
        task = self.tasks[request.match_info['id']]
        task['revision'] += 1
        checks = [{'name': '集群连接', 'status': 'success', 'message': 'v1.30.0'}]
        if self.preflight_error:
            # 模拟“不守规矩”的 master：错误信息里回显令牌、远程写地址和密码
            token = json.loads(self.named('/inspect')[-1]['body']['kubeconfig'])['users'][0]['user']['token']
            message = self.preflight_error.format(token=token, password=VM['password'], url=VM['write_url'])
            task.update(status='draft', checks=checks + [{'name': '安装预检', 'status': 'error', 'message': message}])
        else:
            task.update(status='ready', checks=checks + [{'name': '返回控制端的网络', 'status': 'warning', 'message': '安装后确认'}])
        return self.ok(task)

    async def start(self, request):
        body = await self.record(request)
        task = self.tasks[request.match_info['id']]
        if task['status'] != 'ready' or body.get('revision') != task['revision']:
            return web.json_response({'success': False, 'error': '安装计划已变化或尚未通过预检，请重新检测'}, status=409)
        task.update(status='running', revision=task['revision'] + 1,
                    steps=[{'name': name, 'status': 'pending', 'message': ''} for name in STEPS])
        return self.ok(task)

    async def get(self, request):
        await self.record(request)
        task = self.tasks.get(request.match_info['id'])
        if task is None:
            return web.json_response({'success': False, 'error': '安装任务不存在'}, status=404)
        if task['status'] == 'running':
            task['polls'] += 1
            task['steps'][0].update(status='running')
            if task['polls'] > self.polls:
                last = 'success' if self.outcome == 'succeeded' else 'error'
                for step in task['steps']:
                    step.update(status='success')
                task['steps'][-1].update(status=last, message=self.error if last == 'error' else '')
                task.update(status=self.outcome, error=self.error)
                if self.ai_connection is not None:
                    task['ai_connection'] = copy.deepcopy(self.ai_connection)
        return self.ok(task)


def installer(master, **attributes):
    """master 可以是 FakeMaster，也可以是固定地址（例如已经关闭的服务）。"""
    base = (lambda self: master) if isinstance(master, str) else (lambda self: master.base())
    return type('TestInstall', (AgentInstall,), {'poll_interval': 0, 'api_base': base, **attributes})


async def enter_agent_phase(repo, *, write_url=True, agent=None):
    """把状态布置成最终检查已通过、正在询问是否安装 Agent。"""
    state = await repo.load()
    private = ensure_secrets(default_private())
    private['config'].update(postgres=dict(PG), metrics=dict(VM), admin={'username': 'kubedoor', 'password': 'robust-admin-secret'},
                             public_url='https://kd.example.com')
    if not write_url:
        private['config']['metrics']['write_url'] = ''
    rows = [{'id': sid, 'name': title, 'status': 'ready'} for sid, title, _ in SERVICES]
    checks = [{'id': name, 'name': name, 'status': 'success'} for name in ('postgres', 'metrics', 'resources', 'endpoints', 'write')]
    checks.append({'id': 'cleanup', 'name': 'cleanup', 'status': 'pending', 'message': '等待选择是否在本集群安装 Agent'})
    state.update(phase='agent', step=6, target_locked=True, run_id='run', services=rows, checks=checks,
                 selected_services=[row['id'] for row in rows], metrics_write_required=True,
                 agent=agent or new_agent_state('kubedoor', private['config']))
    await repo.write(state, private)
    return private


async def make_client(k8s, master=None, **attributes):
    repo = Repository(k8s)
    app = create_app(repo, key=KEY, agent_installer=installer(master, **attributes) if master else AgentInstall)
    client = TestClient(TestServer(app), cookie_jar=CookieJar(unsafe=True))
    await client.start_server()
    response = await client.post('/setup-api/session', json={'key': KEY})
    assert response.status == 200
    api = next(value for value in app.values() if isinstance(value, SetupAPI))
    return client, repo, api


async def settle(repo, predicate, attempts=800):
    for _ in range(attempts):
        state = await repo.load()
        if predicate(state):
            return state
        await asyncio.sleep(.01)
    raise AssertionError(await repo.load())


async def current(client):
    return (await (await client.get('/setup-api/state')).json())['data']


def stale(k8s, minutes=3, **agent):
    """模拟安装服务重启：把状态的 updated_at 往前拨，并按需改 agent 字段。"""
    cm = k8s.resources[STATE]
    stored = json.loads(cm['data']['state.json'])
    stored['updated_at'] = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    stored['agent'].update(agent)
    cm['data']['state.json'] = json.dumps(stored)


def no_secrets(value, private, k8s):
    text = json.dumps(value, ensure_ascii=False)
    assert VM['password'] not in text and private['secrets']['internal_token'] not in text
    assert all(token not in text for token, _ in k8s.issued)


@pytest.mark.asyncio
async def test_install_runs_master_pipeline_with_in_cluster_kubeconfig_then_finalizes(tmp_path, monkeypatch):
    monkeypatch.setenv('SETUP_DEFAULTS', json.dumps({'IMAGE_PULL_SECRET': 'pull-key'}))
    k8s = AgentKubernetes(tmp_path)
    async with FakeMaster() as master:
        master.polls = 10 ** 6  # 先让 master 任务一直运行，确认运行中的限制
        client, repo, api = await make_client(k8s, master)
        async with client:
            private = await enter_agent_phase(repo)
            state = await current(client)
            assert state['phase'] == 'agent' and state['step'] == 6
            agent = state['agent']
            assert agent['status'] == 'choice' and 'attempt' not in agent
            assert agent['defaults'] == {'namespace': 'kubedoor', 'mode': 'full', 'ksm': 'auto', 'node_exporter': 'auto', 'env': ''}
            assert datetime.fromisoformat(agent['deadline']) > datetime.now(timezone.utc) + timedelta(minutes=119)
            response = await client.post('/setup-api/agent/install', json={'revision': state['revision'], 'options': OPTIONS})
            assert response.status == 202
            running = (await response.json())['data']
            assert running['revision'] == state['revision'] + 1
            assert running['agent']['status'] == 'running' and 'deadline' not in running['agent']
            assert running['agent']['options'] == {'env': 'prod-k8s', 'aliases': ['生产'], 'namespace': 'kubedoor',
                                                   'mode': 'full', 'ksm': 'auto', 'node_exporter': 'install'}
            during = await settle(repo, lambda s: s['agent']['steps'] and s['agent']['steps'][0]['status'] == 'running')
            assert during['agent']['message'] == '正在安装 Agent：' + STEPS[0] and during['agent']['task_id']
            # 安装进行中不能重复提交，也不能跳过
            assert (await client.post('/setup-api/agent/install', json={'revision': running['revision'], 'options': OPTIONS})).status == 409
            assert (await client.post('/setup-api/agent/skip', json={'revision': running['revision']})).status == 409
            master.polls = 0
            final = await settle(repo, lambda s: s['phase'] == 'finalizing')
            public = await current(client)
    agent = final['agent']
    assert agent['status'] == 'succeeded' and 'prod-k8s' in agent['message']
    assert agent['task_id'] == next(iter(master.tasks))
    assert [step['name'] for step in agent['steps']] == STEPS and all(step['status'] == 'success' for step in agent['steps'])
    assert next(row for row in final['checks'] if row['name'] == 'cleanup')['status'] == 'running'
    # 用一小时令牌拼成的 kubeconfig：集群内 API 地址 + 本集群 CA + 固定 context
    [(token, seconds)] = k8s.issued
    assert seconds == 3600
    inspect = master.named('/inspect')[0]
    config = json.loads(inspect['body']['kubeconfig'])
    assert config['current-context'] == 'in-cluster'
    assert config['clusters'][0]['cluster'] == {'server': 'https://10.96.0.1:443', 'certificate-authority-data': base64.b64encode(CA).decode()}
    assert config['users'][0]['user'] == {'token': token}
    for call in master.calls:
        assert call['headers']['X-Kubedoor-Token'] == private['secrets']['internal_token']
        assert call['headers']['X-User-Name'] == 'kubedoor'
    preflight = master.named('/preflight')[0]['body']
    assert preflight['context'] == 'in-cluster'
    assert preflight['options'] == {'env': 'prod-k8s', 'aliases': ['生产'], 'namespace': 'kubedoor', 'mode': 'full', 'ksm': 'auto',
                                    'node_exporter': 'install', 'master_url': 'http://kubedoor-master.kubedoor',
                                    'remote_write_url': VM['write_url'], 'image_pull_secret': 'pull-key'}
    assert master.named('/start')[0]['body'] == {'revision': 1}
    # 收尾 Job 由安装服务创建，沿用自己的镜像和拉取凭据
    job = k8s.resources[JOB]
    assert job['metadata']['annotations'][OWNER] == ID
    assert job['spec']['template']['spec']['serviceAccountName'] == 'kubedoor-setup-finalizer'
    assert job['spec']['template']['spec']['imagePullSecrets'] == [{'name': 'pull-key'}]
    # 令牌、远程写密码、内部令牌都不进状态、公开状态和公开标记
    no_secrets(k8s.resources[STATE], private, k8s)
    no_secrets(public, private, k8s)
    marker = json.loads(k8s.resources[STATE]['data']['status.json'])
    assert marker['phase'] == 'finalizing' and marker['agent'] == {'status': 'succeeded', 'env': 'prod-k8s'}


@pytest.mark.asyncio
async def test_failed_preflight_reports_checks_and_retry_uses_new_token_and_task(tmp_path):
    k8s = AgentKubernetes(tmp_path)
    async with FakeMaster() as master:
        master.preflight_error = '这个集群标识已有 Agent 登记，请选择其它标识或核对目标集群'
        client, repo, _ = await make_client(k8s, master)
        async with client:
            private = await enter_agent_phase(repo)
            state = await current(client)
            response = await client.post('/setup-api/agent/install', json={'revision': state['revision'], 'options': OPTIONS})
            assert response.status == 202
            await settle(repo, lambda s: s['agent']['status'] == 'failed')
            failed = await current(client)
            agent = failed['agent']
            assert failed['phase'] == 'agent'
            assert agent['error'] == '安装预检未通过：' + master.preflight_error
            assert agent['checks'] == [{'name': '安装预检', 'status': 'error', 'message': master.preflight_error}]
            assert datetime.fromisoformat(agent['deadline']) > datetime.now(timezone.utc) + timedelta(minutes=119)
            assert next(row for row in failed['checks'] if row['name'] == 'cleanup')['status'] == 'pending'
            assert not master.named('/start') and JOB not in k8s.resources
            master.preflight_error = None
            retry = await client.post('/setup-api/agent/install', json={'revision': failed['revision'], 'options': {**OPTIONS, 'env': 'prod-2'}})
            assert retry.status == 202
            final = await settle(repo, lambda s: s['phase'] == 'finalizing')
    assert final['agent']['status'] == 'succeeded' and final['agent']['checks'] == []
    assert 'error' not in final['agent']
    assert len(k8s.issued) == 2 and k8s.issued[0][0] != k8s.issued[1][0]
    assert len(master.tasks) == 2 and final['agent']['task_id'] == list(master.tasks)[1]
    assert master.named('/preflight')[1]['body']['options']['env'] == 'prod-2'
    no_secrets(k8s.resources[STATE], private, k8s)


@pytest.mark.asyncio
async def test_echoed_credentials_are_removed_before_truncation(tmp_path):
    k8s = AgentKubernetes(tmp_path)
    async with FakeMaster() as master:
        # 令牌正好跨在 500 字截断处：必须先替换再截断，不能留下半截
        master.preflight_error = 'x' * 490 + '{token} 地址 {url} 密码 {password}'
        client, repo, _ = await make_client(k8s, master)
        async with client:
            private = await enter_agent_phase(repo)
            state = await current(client)
            await client.post('/setup-api/agent/install', json={'revision': state['revision'], 'options': OPTIONS})
            await settle(repo, lambda s: s['agent']['status'] == 'failed')
            failed = await current(client)
    [(token, _)] = k8s.issued
    text = json.dumps([failed, k8s.resources[STATE]], ensure_ascii=False)
    assert token.rsplit('-', 1)[1][:8] not in text and VM['password'] not in text
    assert failed['agent']['checks'][0]['message'].startswith('x' * 490 + '***')
    no_secrets(k8s.resources[STATE], private, k8s)


@pytest.mark.asyncio
async def test_master_rejection_before_preflight_is_reported(tmp_path):
    k8s = AgentKubernetes(tmp_path)
    async with FakeMaster() as master:
        master.reject = (409, '集群标识已被其它集群使用，或目标集群已使用另一标识接入')
        client, repo, _ = await make_client(k8s, master)
        async with client:
            await enter_agent_phase(repo)
            state = await current(client)
            await client.post('/setup-api/agent/install', json={'revision': state['revision'], 'options': OPTIONS})
            failed = await settle(repo, lambda s: s['agent']['status'] == 'failed')
    assert failed['agent']['error'] == '安装预检未通过：' + master.reject[1]
    assert failed['phase'] == 'agent'


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['failed', 'interrupted'])
async def test_failed_master_task_stays_in_agent_step_for_retry_or_skip(tmp_path, outcome):
    k8s = AgentKubernetes(tmp_path)
    async with FakeMaster() as master:
        master.outcome, master.error = outcome, '等待工作负载就绪超时'
        client, repo, _ = await make_client(k8s, master)
        async with client:
            await enter_agent_phase(repo)
            state = await current(client)
            await client.post('/setup-api/agent/install', json={'revision': state['revision'], 'options': OPTIONS})
            await settle(repo, lambda s: s['agent']['status'] == 'failed')
            failed = await current(client)
            assert failed['phase'] == 'agent'
            assert failed['agent']['error'] == '在本集群安装 Agent 失败：等待工作负载就绪超时'
            assert failed['agent']['steps'][-1] == {'name': STEPS[-1], 'status': 'error', 'message': '等待工作负载就绪超时'}
            assert JOB not in k8s.resources
            response = await client.post('/setup-api/agent/skip', json={'revision': failed['revision']})
            assert response.status == 200
            skipped = (await response.json())['data']
    assert skipped['phase'] == 'finalizing' and skipped['agent']['status'] == 'skipped'
    assert 'error' not in skipped['agent'] and 'deadline' not in skipped['agent']
    assert JOB in k8s.resources


@pytest.mark.asyncio
async def test_partial_install_finishes_with_metrics_hint(tmp_path):
    k8s = AgentKubernetes(tmp_path)
    async with FakeMaster() as master:
        master.outcome, master.error = 'partial', 'Agent 已上线，但尚未验证到新指标；请检查采集目标和指标写入地址'
        client, repo, _ = await make_client(k8s, master)
        async with client:
            await enter_agent_phase(repo)
            state = await current(client)
            await client.post('/setup-api/agent/install', json={'revision': state['revision'], 'options': OPTIONS})
            final = await settle(repo, lambda s: s['phase'] == 'finalizing')
    agent = final['agent']
    assert agent['status'] == 'partial' and 'error' not in agent
    assert '指标采集验证未通过' in agent['message'] and master.error in agent['message']
    # 重新验证只查时序库：不再让用户上传本集群的 kubeconfig
    assert '重新验证指标采集' in agent['message'] and '上传本集群的 kubeconfig' not in agent['message']
    assert json.loads(k8s.resources[STATE]['data']['status.json'])['agent'] == {'status': 'partial', 'env': 'prod-k8s'}


@pytest.mark.asyncio
@pytest.mark.parametrize('ai_connection, note', [
    ({'status': 'saved', 'message': '已用 Agent 服务账号自动配置 AI Kubeconfig'}, '；已用 Agent 服务账号自动配置 AI Kubeconfig'),
    ({'status': 'failed', 'message': '连不上 kubedoor-ai，没有自动配置 AI Kubeconfig。AI 仍可经 Agent 执行'},
     '。连不上 kubedoor-ai，没有自动配置 AI Kubeconfig。AI 仍可经 Agent 执行'),
    ({'status': 'skipped', 'message': '该集群已有手动上传的 AI Kubeconfig，保留不变'}, ''),
    (None, ''),     # 旧版 master 没有这项
])
async def test_agent_result_mentions_the_ai_kubeconfig(tmp_path, ai_connection, note):
    k8s = AgentKubernetes(tmp_path)
    async with FakeMaster() as master:
        master.ai_connection = ai_connection
        client, repo, _ = await make_client(k8s, master)
        async with client:
            await enter_agent_phase(repo)
            state = await current(client)
            await client.post('/setup-api/agent/install', json={'revision': state['revision'], 'options': OPTIONS})
            final = await settle(repo, lambda s: s['phase'] == 'finalizing')
    assert final['agent']['message'] == 'Agent 已在本集群安装并上线，集群名称 prod-k8s' + note


@pytest.mark.asyncio
async def test_agent_only_mode_sends_no_remote_write_and_full_needs_write_url(tmp_path):
    k8s = AgentKubernetes(tmp_path)
    async with FakeMaster() as master:
        client, repo, _ = await make_client(k8s, master)
        async with client:
            await enter_agent_phase(repo, write_url=False)
            state = await current(client)
            assert state['agent']['defaults']['mode'] == 'agent'
            response = await client.post('/setup-api/agent/install', json={'revision': state['revision'], 'options': OPTIONS})
            assert response.status == 422 and '远程写' in (await response.json())['message']
            options = {key: value for key, value in OPTIONS.items() if key != 'mode'}
            response = await client.post('/setup-api/agent/install', json={'revision': state['revision'], 'options': options})
            assert response.status == 202
            await settle(repo, lambda s: s['phase'] == 'finalizing')
    sent = master.named('/preflight')[0]['body']['options']
    assert sent['mode'] == 'agent' and sent['remote_write_url'] == ''
    assert 'image_pull_secret' not in sent


@pytest.mark.asyncio
@pytest.mark.parametrize('options,field', [
    (None, '选项'), ({**OPTIONS, 'env': ''}, '集群名称'), ({**OPTIONS, 'env': 'prod k8s'}, '集群名称'),
    ({**OPTIONS, 'env': 'x' * 101}, '集群名称'), ({**OPTIONS, 'env': 7}, '集群名称'),
    ({**OPTIONS, 'namespace': 'Bad_NS'}, '命名空间'), ({**OPTIONS, 'namespace': 'a' * 64}, '命名空间'),
    ({**OPTIONS, 'aliases': 'prod'}, '别名'), ({**OPTIONS, 'aliases': ['a'] * 21}, '别名'),
    ({**OPTIONS, 'aliases': ['x' * 101]}, '别名'), ({**OPTIONS, 'aliases': ['line\nbreak']}, '别名'),
    ({**OPTIONS, 'mode': 'all'}, '安装方式'), ({**OPTIONS, 'ksm': 'yes'}, 'kube-state-metrics'),
    ({**OPTIONS, 'node_exporter': 'skip'}, 'node-exporter'),
])
async def test_invalid_options_are_rejected_without_changing_state(tmp_path, options, field):
    k8s = AgentKubernetes(tmp_path)
    client, repo, _ = await make_client(k8s)
    async with client:
        await enter_agent_phase(repo)
        state = await current(client)
        response = await client.post('/setup-api/agent/install', json={'revision': state['revision'], 'options': options})
        assert response.status == 422
        assert field in (await response.json())['message']
        after = await current(client)
    assert after['revision'] == state['revision'] and after['agent']['status'] == 'choice'
    assert k8s.issued == []


def test_option_defaults_and_normalization_follow_master_rules():
    defaults = {'namespace': 'kubedoor', 'mode': 'full'}
    result = normalize_options({'env': ' prod_1-a ', 'aliases': None}, defaults, 'https://vm/api/v1/write')
    assert result == {'env': 'prod_1-a', 'aliases': [], 'namespace': 'kubedoor', 'mode': 'full', 'ksm': 'auto', 'node_exporter': 'auto'}
    assert normalize_options({'env': 'a', 'namespace': 'agent-ns', 'mode': 'agent', 'ksm': 'reuse'}, defaults, '')['namespace'] == 'agent-ns'
    with pytest.raises(SetupError):
        normalize_options({'env': 'a'}, defaults, '')


@pytest.mark.asyncio
async def test_agent_endpoints_check_phase_and_revision(tmp_path):
    k8s = AgentKubernetes(tmp_path)
    client, repo, _ = await make_client(k8s)
    async with client:
        state = await current(client)
        assert state['phase'] == 'draft' and 'agent' not in state
        for path, body in (('/setup-api/agent/install', {'options': OPTIONS}), ('/setup-api/agent/skip', {})):
            assert (await client.post(path, json={'revision': state['revision'], **body})).status == 409
        await enter_agent_phase(repo)
        state = await current(client)
        for path, body in (('/setup-api/agent/install', {'options': OPTIONS}), ('/setup-api/agent/skip', {})):
            response = await client.post(path, json={'revision': state['revision'] - 1, **body})
            assert response.status == 409 and '刷新' in (await response.json())['message']
        # 其他步骤的接口在本阶段一律拒绝
        assert (await client.post('/setup-api/retry', json={'revision': state['revision']})).status == 409
        assert (await client.put('/setup-api/configuration', json={'revision': state['revision']})).status == 409
    assert (await repo.load())['revision'] == state['revision']


@pytest.mark.asyncio
async def test_skip_moves_to_finalizing_and_keeps_installer_untouched(tmp_path):
    k8s = AgentKubernetes(tmp_path)
    client, repo, _ = await make_client(k8s)
    async with client:
        await enter_agent_phase(repo)
        state = await current(client)
        response = await client.post('/setup-api/agent/skip', json={'revision': state['revision']})
        assert response.status == 200
        data = (await response.json())['data']
        assert data['phase'] == 'finalizing' and data['revision'] == state['revision'] + 1
        assert data['agent']['status'] == 'skipped' and 'deadline' not in data['agent']
        assert next(row for row in data['checks'] if row['name'] == 'cleanup')['status'] == 'running'
        assert (await client.post('/setup-api/agent/install', json={'revision': data['revision'], 'options': OPTIONS})).status == 409
    assert k8s.issued == [] and JOB in k8s.resources
    assert json.loads(k8s.resources[STATE]['data']['status.json'])['agent'] == {'status': 'skipped', 'env': ''}


@pytest.mark.asyncio
async def test_finalizer_creation_failure_turns_retryable_and_retry_skips_question(tmp_path):
    k8s = AgentKubernetes(tmp_path)
    original = k8s.create

    async def refuse(obj):
        raise SetupError('安装权限不足或准入策略拒绝（HTTP 403）', 403)
    k8s.create = refuse
    client, repo, _ = await make_client(k8s)
    async with client:
        await enter_agent_phase(repo)
        state = await current(client)
        response = await client.post('/setup-api/agent/skip', json={'revision': state['revision']})
        data = (await response.json())['data']
    assert data['phase'] == 'failed' and data['retryable'] is True and '收尾' in data['error']
    assert data['agent']['status'] == 'skipped'
    k8s.create = original
    # 重试安装走完后不再询问 Agent，直接收尾
    repo = MemoryRepo()
    repo.state['agent'] = {'status': 'skipped', 'message': '已跳过'}
    runner = AgentRunner(repo, RunnerKubernetes(), available=True)
    await runner.conclude()
    assert runner.finalized and repo.state['phase'] == 'finalizing' and repo.state['agent']['status'] == 'skipped'


@pytest.mark.asyncio
@pytest.mark.parametrize('status,word', [('choice', '没有选择'), ('failed', '没有重试')])
async def test_watchdog_finishes_expired_choice_as_not_installed(tmp_path, status, word):
    k8s = AgentKubernetes(tmp_path)
    repo = Repository(k8s)
    agent = {'status': status, 'deadline': (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
             'defaults': {'namespace': 'kubedoor'}, 'steps': [], 'checks': [], 'error': 'old failure'}
    await enter_agent_phase(repo, agent=agent)
    before = (await repo.load())['revision']
    api = SetupAPI(repo, key=KEY)
    await api._recover_once()
    await api.agent_task
    state = await repo.load()
    assert state['phase'] == 'finalizing' and state['revision'] == before + 1
    assert state['agent']['status'] == 'skipped' and word in state['agent']['message']
    assert 'error' not in state['agent'] and JOB in k8s.resources


@pytest.mark.asyncio
async def test_watchdog_leaves_open_choice_and_recent_install_alone(tmp_path):
    k8s = AgentKubernetes(tmp_path)
    repo = Repository(k8s)
    await enter_agent_phase(repo)
    api = SetupAPI(repo, key=KEY)
    await api._recover_once()
    assert api.agent_task is None and (await repo.load())['agent']['status'] == 'choice'
    state = await repo.load()
    state['agent'].update(status='running', attempt='a' * 32, task_id=str(uuid.uuid4()), started_at=datetime.now(timezone.utc).isoformat())
    await repo.save(state)
    await api._recover_once()
    assert api.agent_task is None and (await repo.load())['agent']['status'] == 'running'


@pytest.mark.asyncio
async def test_watchdog_resumes_master_task_after_restart_without_new_token(tmp_path):
    k8s = AgentKubernetes(tmp_path)
    async with FakeMaster() as master:
        task_id = str(uuid.uuid4())
        master.tasks[task_id] = {'id': task_id, 'status': 'running', 'revision': 2, 'error': '', 'polls': 0, 'checks': [],
                                 'steps': [{'name': name, 'status': 'pending', 'message': ''} for name in STEPS]}
        repo = Repository(k8s)
        await enter_agent_phase(repo)
        state = await repo.load()
        state['agent'].update(status='running', attempt='b' * 32, task_id=task_id, options=normalize_options(
            OPTIONS, state['agent']['defaults'], VM['write_url']), started_at=datetime.now(timezone.utc).isoformat())
        await repo.save(state)
        stale(k8s)
        api = SetupAPI(repo, key=KEY, agent_installer=installer(master))
        await api._recover_once()
        await api.agent_task
    state = await repo.load()
    assert state['phase'] == 'finalizing' and state['agent']['status'] == 'succeeded'
    assert k8s.issued == [] and not master.named('/inspect')
    assert {call['path'] for call in master.calls} == {'/api/admin/agent-installations/' + task_id}


@pytest.mark.asyncio
async def test_watchdog_fails_install_that_never_reached_master(tmp_path):
    k8s = AgentKubernetes(tmp_path)
    repo = Repository(k8s)
    await enter_agent_phase(repo)
    state = await repo.load()
    state['agent'].update(status='running', attempt='c' * 32, started_at=datetime.now(timezone.utc).isoformat())
    await repo.save(state)
    stale(k8s)
    api = SetupAPI(repo, key=KEY)
    await api._recover_once()
    state = await repo.load()
    assert api.agent_task is None and state['phase'] == 'agent'
    assert state['agent']['status'] == 'failed' and '重启' in state['agent']['error']
    assert datetime.fromisoformat(state['agent']['deadline']) > datetime.now(timezone.utc) + timedelta(minutes=119)


@pytest.mark.asyncio
async def test_watchdog_retries_finalizer_when_concluded_step_was_interrupted(tmp_path):
    k8s = AgentKubernetes(tmp_path)
    repo = Repository(k8s)
    await enter_agent_phase(repo, agent={'status': 'succeeded', 'options': {'env': 'prod'}, 'steps': [], 'checks': []})
    api = SetupAPI(repo, key=KEY)
    await api._recover_once()
    assert api.agent_task is None  # 刚写入的状态可能有别的进程在处理，等它过期
    stale(k8s)
    await api._recover_once()
    await api.agent_task
    state = await repo.load()
    assert state['phase'] == 'finalizing' and state['agent']['status'] == 'succeeded' and JOB in k8s.resources


@pytest.mark.asyncio
async def test_total_timeout_marks_install_failed(tmp_path):
    k8s = AgentKubernetes(tmp_path)
    async with FakeMaster() as master:
        master.polls = 10 ** 6
        client, repo, _ = await make_client(k8s, master, timeout=0)
        async with client:
            await enter_agent_phase(repo)
            state = await current(client)
            await client.post('/setup-api/agent/install', json={'revision': state['revision'], 'options': OPTIONS})
            failed = await settle(repo, lambda s: s['agent']['status'] == 'failed')
    assert '超过 20 分钟' in failed['agent']['error'] and failed['phase'] == 'agent'
    assert failed['agent']['steps'][0]['status'] == 'running'


@pytest.mark.asyncio
async def test_unreachable_master_is_reported_without_credentials(tmp_path):
    k8s = AgentKubernetes(tmp_path)
    async with FakeMaster() as master:
        base = master.base()
    client, repo, _ = await make_client(k8s, base)  # 服务已关闭：连接被拒绝
    async with client:
        private = await enter_agent_phase(repo)
        state = await current(client)
        await client.post('/setup-api/agent/install', json={'revision': state['revision'], 'options': OPTIONS})
        failed = await settle(repo, lambda s: s['agent']['status'] == 'failed')
    assert '无法连接控制服务' in failed['agent']['error']
    no_secrets(k8s.resources[STATE], private, k8s)


@pytest.mark.asyncio
async def test_token_signing_failure_is_reported(tmp_path):
    k8s = AgentKubernetes(tmp_path, granted=False)
    async with FakeMaster() as master:
        client, repo, _ = await make_client(k8s, master)
        async with client:
            await enter_agent_phase(repo)
            state = await current(client)
            await client.post('/setup-api/agent/install', json={'revision': state['revision'], 'options': OPTIONS})
            failed = await settle(repo, lambda s: s['agent']['status'] == 'failed')
    assert '临时令牌' in failed['agent']['error'] and not master.calls


@pytest.mark.asyncio
@pytest.mark.parametrize('granted', [True, False])
async def test_installer_probe_uses_its_own_short_token(tmp_path, granted):
    k8s = AgentKubernetes(tmp_path, granted=granted)
    assert await installer_available(k8s) is granted
    if granted:
        [(token, seconds)] = k8s.issued
        assert seconds == 600
        assert k8s.reviews == [(token, {'group': 'rbac.authorization.k8s.io', 'resource': 'clusterrolebindings', 'verb': 'create'})]

    class Broken(AgentKubernetes):
        async def request(self, *args, **kwargs):
            raise RuntimeError('boom')
    assert await installer_available(Broken(tmp_path)) is False


@pytest.mark.asyncio
@pytest.mark.parametrize('granted,status', [(True, 'success'), (False, 'warning')])
async def test_environment_reports_agent_installer_without_blocking(tmp_path, granted, status):
    k8s = AgentKubernetes(tmp_path, granted=granted)
    repo = Repository(k8s)
    state = await repo.load()
    await SetupAPI(repo, key=KEY).environment(state)
    assert state['environment_checked'] is True
    rows = {row['name']: row for row in state['checks']}
    assert rows['environment']['status'] == 'success'
    assert rows['agent_installer']['id'] == 'agent_installer' and rows['agent_installer']['status'] == status
    assert ('撤销' if granted else 'kubeconfig') in rows['agent_installer']['message']


class AgentRunner(ObservedRunner):
    def __init__(self, repo, k8s, available):
        super().__init__(repo, k8s)
        self.available = available

    async def agent_installer_available(self):
        return self.available

    async def verify_metrics(self):
        # 写入验证由 test_runner.py 的专门用例覆盖；这里只关心最终检查之后的 Agent 步骤
        await self.update(check='write', status='success', message='时序库写入及查询验证通过')


@pytest.mark.asyncio
async def test_runner_asks_about_agent_after_final_checks(monkeypatch):
    setup_probes(monkeypatch)
    repo = MemoryRepo()
    repo.private['config']['metrics']['write_url'] = VM['write_url']
    runner = AgentRunner(repo, RunnerKubernetes(), available=True)
    await runner.run()
    assert not runner.finalized
    state = repo.state
    assert state['phase'] == 'agent' and state['step'] == 6
    assert state['agent']['status'] == 'choice' and state['agent']['defaults']['mode'] == 'full'
    assert all(row['status'] == 'ready' for row in state['services'])
    cleanup = next(row for row in state['checks'] if row['name'] == 'cleanup')
    assert cleanup['status'] == 'pending' and 'Agent' in cleanup['message']


@pytest.mark.asyncio
async def test_runner_finalizes_directly_when_installer_unavailable(monkeypatch):
    setup_probes(monkeypatch)
    repo = MemoryRepo()
    runner = AgentRunner(repo, RunnerKubernetes(), available=False)
    await runner.run()
    assert runner.finalized and repo.state['phase'] == 'finalizing'
    assert repo.state['agent']['status'] == 'unavailable' and 'kubeconfig' in repo.state['agent']['message']
    assert all(state['phase'] != 'agent' for state in repo.history)


@pytest.mark.asyncio
async def test_old_runner_failure_never_overrides_agent_step(tmp_path):
    repo = Repository(AgentKubernetes(tmp_path))
    await enter_agent_phase(repo)
    await SetupAPI(repo, key=KEY)._mark_failed('old worker error', run_id='run')
    assert (await repo.load())['phase'] == 'agent'


def test_public_marker_exposes_only_agent_result_after_completion():
    state = new_state(ID, 'kubedoor')
    state.update(phase='agent', agent={'status': 'running', 'attempt': 'secret-attempt', 'options': {'env': 'prod'}})
    assert 'agent' not in public_marker(state)
    for phase in ('finalizing', 'succeeded'):
        state['phase'] = phase
        state['agent'].update(status='succeeded', task_id='task', message='done')
        assert public_marker(state)['agent'] == {'status': 'succeeded', 'env': 'prod'}


@pytest.mark.asyncio
async def test_kubernetes_requests_can_use_a_signed_token(tmp_path):
    seen = []

    async def handler(request):
        seen.append(request.headers['Authorization'])
        return web.json_response({'status': {'allowed': True}})
    app = web.Application()
    app.router.add_route('*', '/{tail:.*}', handler)
    (tmp_path / 'token').write_text('own-identity\n')
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        k8s = Kubernetes('kubedoor', server=str(server.make_url('')).rstrip('/'), token_path=tmp_path / 'token')
        k8s.session = session
        await k8s.request('GET', '/version')
        assert await k8s.allowed('ClusterRoleBinding', 'create', version='rbac.authorization.k8s.io/v1', token='installer-token')
        with pytest.raises(SetupError):
            await k8s.request('GET', '/version', token='')
    assert seen == ['Bearer own-identity', 'Bearer installer-token']
