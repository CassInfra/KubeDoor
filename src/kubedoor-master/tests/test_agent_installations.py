"""真实渲染器 + 假集群验证完整安装、可恢复失败和凭据生命周期。"""

import asyncio
import base64
import copy
import json
import sys
import time
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'kubedoor-tools'))

from agent_install import service as module
from agent_install.kubernetes import InstallError, resource_path
from agent_install.repository import unpack


CONFIG = json.dumps({
    'apiVersion': 'v1', 'kind': 'Config', 'current-context': 'production',
    'contexts': [{'name': 'production', 'context': {'cluster': 'cluster', 'user': 'admin'}}],
    'clusters': [{'name': 'cluster', 'cluster': {'server': 'https://k8s.example.test'}}],
    'users': [{'name': 'admin', 'user': {'token': 'private-kube-token'}}],
})
# 控制端配置的时序库写入地址：网页安装的 vmagent 默认直连它
WRITE_URL = 'http://monit:vm%40secret@vm.example.test:8428/api/v1/write'


class MemoryRepository:
    def __init__(self):
        self.data = {}

    async def create(self, task):
        self.data[task['id']] = copy.deepcopy(task)

    async def get(self, task_id):
        return copy.deepcopy(self.data.get(task_id))

    async def list(self):
        return copy.deepcopy(list(self.data.values()))

    async def unfinished(self):
        return [t for t in await self.list() if t['status'] == 'running']

    async def save(self, task):
        current = self.data[task['id']]
        if current['revision'] != task['revision']:
            raise InstallError('安装任务已更新', 409)
        if task['status'] == 'running':
            for other in self.data.values():
                if other['id'] != task['id'] and other['status'] == 'running' and (
                        other['env'] == task['env'] or other['target_uid'] == task['target_uid']):
                    raise InstallError('已有安装任务正在执行', 409)
        task['revision'] += 1
        task['updated_at'] = module.utcnow()
        self.data[task['id']] = copy.deepcopy(task)

    async def same_cluster(self, env, target_uid, exclude):
        return [t for t in await self.list() if t['id'] != exclude and (t['env'] == env or t['target_uid'] == target_uid)
                and t['status'] not in ('draft', 'expired')]

    async def expire(self):
        for task in self.data.values():
            if task['expires_at'] < module.utcnow() and task['status'] != 'running':
                task['encrypted'] = ''
                if task['status'] in ('draft', 'ready'):
                    task['status'] = 'expired'


class FakeCluster:
    def __init__(self):
        self.resources = {}
        self.applied = []
        self.dry_runs = []
        self.permitted = True
        self.uid = 'cluster-uid'
        self.is_ready = True
        self.clients = {}
        self.connection_user_id = 100
        # K8S 令牌控制器填进 Secret kubedoor-agent 的 Agent 服务账号令牌；空串表示还没填
        self.agent_token = 'agent-sa-token'
        # 带 force（强制接管其他字段管理器的字段）应用过的资源：(kind, name, dry_run)
        self.forced = []

    def __call__(self, parsed):
        assert parsed['config']['users'][0]['user']['token'] == 'private-kube-token'
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def request(self, method, path, **kwargs):
        if getattr(self, 'unauthorized', False):
            raise InstallError(f'Kubernetes {method} {path}：认证失败（HTTP 401）', http_status=401)
        if path == '/version':
            return {'gitVersion': 'v1.30.1'}
        if path == '/api/v1/namespaces/kube-system':
            return {'metadata': {'uid': self.uid}}
        if path.endswith('/configmaps/kubedoor-agent-config'):
            return copy.deepcopy(self.resources.get(('ConfigMap', 'kubedoor-agent-config')))
        if path == '/api/v1/namespaces/kubedoor/secrets/kubedoor-agent':
            secret = copy.deepcopy(self.resources.get(('Secret', 'kubedoor-agent')))
            if secret and self.agent_token:
                secret['data'] = {'token': base64.b64encode(self.agent_token.encode()).decode(), 'ca.crt': 'Q0E='}
            return secret
        raise AssertionError((method, path))

    async def list(self, path, params=None):
        if path == '/api/v1/nodes':
            return [{'metadata': {'name': 'worker'}}]
        return []

    async def get(self, resource):
        return copy.deepcopy(self.resources.get((resource['kind'], resource['metadata']['name'])))

    async def apply(self, resource, dry_run=False, force=False):
        namespace = resource['metadata'].get('namespace')
        if namespace and ('Namespace', namespace) not in self.resources:
            raise InstallError('不存在的 Namespace')
        if force:
            self.forced.append((resource['kind'], resource['metadata']['name'], dry_run))
        if dry_run:
            self.dry_runs.append(copy.deepcopy(resource))
        else:
            self.applied.append(copy.deepcopy(resource))
            self.resources[resource['kind'], resource['metadata']['name']] = copy.deepcopy(resource)
            if resource['kind'] == 'Deployment' and resource['metadata']['name'] == 'kubedoor-agent':
                self.clients['production'] = {'online': True, 'agent_user_id': self.connection_user_id,
                                              'last_heartbeat': time.time() + 1000, 'heartbeat_received': True}

    async def allowed(self, resource, verb):
        return self.permitted

    async def ready(self, resource):
        return self.is_ready

    async def workload_diagnosis(self, namespace):
        return 'kubedoor-agent: ImagePullBackOff'


@pytest.fixture
def environment(monkeypatch):
    monkeypatch.setenv('AI_ENCRYPTION_KEY', 'ab' * 32)
    monkeypatch.setenv('KUBEDOOR_EXTURL', 'https://kubedoor.example.test')
    monkeypatch.setenv('METRICS_WRITE_URL', WRITE_URL)
    monkeypatch.setattr(module.audit, 'record_bg', lambda *a, **kw: None)
    monkeypatch.setattr(module.db, 'pg_execute', AsyncMock())
    monkeypatch.setattr(module.auth_store, 'get_user_by_name', AsyncMock(return_value=None))
    monkeypatch.setattr(module.auth_store, 'create_user', AsyncMock(return_value={'id': 100}))
    monkeypatch.setattr(module.auth_store, 'bind_agent', AsyncMock())
    monkeypatch.setattr(module.auth_store, 'update_user', AsyncMock())
    monkeypatch.setattr(module, 'hash_password', AsyncMock(return_value='hashed-secret'))
    cluster, repo = FakeCluster(), MemoryRepository()
    clients = cluster.clients
    service = module.Service(clients, repo, cluster)
    service.poll_interval = .001
    service.online_timeout = .003
    service.metrics_timeout = .003
    service.ready_timeout = .003
    return service, cluster, repo


async def plan(service, mode='agent'):
    task = await service.inspect(CONFIG, 'admin')
    task = await service.preflight(task['id'], 'production', {**task['payload']['options'], 'mode': mode})
    assert task['status'] == 'ready', task['payload']['checks']
    return task


@pytest.mark.asyncio
async def test_installs_new_namespace_then_resources_and_destroys_credentials(environment):
    service, cluster, repo = environment
    # 首次预检尚未创建 Namespace，不执行不可能成功的 namespaced dry-run。
    service.clients.clear()
    task = await plan(service)
    assert not cluster.applied
    assert all('namespace' not in item['metadata'] for item in cluster.dry_runs)
    public = json.dumps(service.public(task))
    assert 'private-kube-token' not in public and 'kubeconfig' not in public
    assert 'private-kube-token' not in task['encrypted']
    await service.start(task['id'], task['revision'])
    await service.tasks[task['id']]
    done = await repo.get(task['id'])
    assert done['status'] == 'succeeded', done['payload']
    assert done['encrypted'] == ''
    assert cluster.applied[0]['kind'] == 'Namespace'
    assert done['payload']['steps'][-1]['status'] == 'skipped'
    credential = cluster.resources['Secret', 'kubedoor-agent-credentials']
    address = base64.b64decode(credential['data']['KUBEDOOR_MASTER']).decode()
    assert address.startswith('wss://agent-') and address.endswith('@kubedoor.example.test')


class FakeAI:
    """假 kubedoor-ai：记下保存 AI Kubeconfig 的请求，按 status / reply 回复"""

    def __init__(self, status=200, reply=None):
        self.status, self.reply, self.requests = status, reply, []

    async def handle(self, request):
        self.requests.append({'path': request.path, 'headers': dict(request.headers), 'body': await request.json()})
        reply = self.reply if self.reply is not None else {'env': 'production', 'context': 'kubedoor-agent', 'origin': 'agent-install'}
        return web.json_response(reply, status=self.status)

    def app(self):
        app = web.Application()
        app.router.add_put('/api/ai/connections/{env}', self.handle)
        return app


async def install_with_ai(environment, monkeypatch, ai):
    service, _, repo = environment
    service.clients.clear()
    async with TestServer(ai.app()) as server:
        monkeypatch.setattr(module, 'AI_URL', f'http://{server.host}:{server.port}')
        monkeypatch.setenv('AI_INTERNAL_TOKEN', 'internal-secret')
        task = await plan(service)
        await service.start(task['id'], task['revision'])
        await service.tasks[task['id']]
    return await repo.get(task['id'])


@pytest.mark.asyncio
async def test_agent_online_configures_ai_kubeconfig_with_the_agent_token(environment, monkeypatch):
    ai = FakeAI()
    done = await install_with_ai(environment, monkeypatch, ai)
    assert done['status'] == 'succeeded', done['payload']
    assert done['payload']['ai_connection'] == {'status': 'saved', 'message': '已用 Agent 服务账号自动配置 AI Kubeconfig'}
    [request] = ai.requests
    assert request['path'] == '/api/ai/connections/production'
    headers = request['headers']
    assert (headers['X-Kubedoor-Token'], headers['X-User-Name'], headers['X-User-Permission']) == ('internal-secret', 'admin', 'admin')
    body = request['body']
    assert (body['context'], body['origin']) == ('kubedoor-agent', 'agent-install')
    config = json.loads(body['content'])
    # 集群地址沿用安装用的 kubeconfig；凭据是 Agent 服务账号的长期令牌，不是上传的个人令牌
    assert config['clusters'][0]['cluster'] == {'server': 'https://k8s.example.test'}
    assert config['users'] == [{'name': 'kubedoor-agent', 'user': {'token': 'agent-sa-token'}}]
    assert config['current-context'] == 'kubedoor-agent' and config['contexts'][0]['context']['user'] == 'kubedoor-agent'
    assert 'private-kube-token' not in body['content']
    # 安装记录里没有任何令牌，上传的 kubeconfig 照样销毁
    assert done['encrypted'] == ''
    public = json.dumps(environment[0].public(done))
    assert 'agent-sa-token' not in public and 'private-kube-token' not in public


@pytest.mark.asyncio
@pytest.mark.parametrize('status, reply, outcome, text', [
    (200, {'skipped': True}, 'skipped', '该集群已有手动上传的 AI Kubeconfig，保留不变'),
    (422, {'success': False, 'error': {'code': 'UNREACHABLE', 'message': 'dial tcp   10.0.0.1:6443:\n i/o timeout'}}, 'failed',
     'kubedoor-ai 用它连接集群的测试没有通过：dial tcp 10.0.0.1:6443: i/o timeout'),
    (500, {'error': {'code': 'internal', 'message': '数据库不可用'}}, 'failed', '没有自动配置 AI Kubeconfig（数据库不可用）'),
])
async def test_ai_kubeconfig_result_never_fails_the_install(environment, monkeypatch, status, reply, outcome, text):
    done = await install_with_ai(environment, monkeypatch, FakeAI(status, reply))
    assert done['status'] == 'succeeded', done['payload']
    result = done['payload']['ai_connection']
    assert result['status'] == outcome and text in result['message']
    if outcome == 'failed':
        assert '手动上传' in result['message']


@pytest.mark.asyncio
async def test_ai_kubeconfig_explains_why_it_was_not_configured(environment, monkeypatch):
    service, cluster, _ = environment
    cluster.resources['Secret', 'kubedoor-agent'] = {'kind': 'Secret', 'metadata': {'name': 'kubedoor-agent', 'namespace': 'kubedoor'}}
    task, options, parsed = {'env': 'production', 'payload': {'created_by': 'admin'}}, {'namespace': 'kubedoor'}, module.parse(CONFIG)
    monkeypatch.delenv('AI_INTERNAL_TOKEN', raising=False)
    result = await service.configure_ai_connection(task, options, parsed, cluster)
    assert result['status'] == 'failed' and 'AI_INTERNAL_TOKEN' in result['message']
    monkeypatch.setenv('AI_INTERNAL_TOKEN', 'internal-secret')
    async with TestServer(web.Application()) as server:
        closed = f'http://{server.host}:{server.port}'
    monkeypatch.setattr(module, 'AI_URL', closed)
    result = await service.configure_ai_connection(task, options, parsed, cluster)
    assert result['status'] == 'failed' and '连不上 kubedoor-ai' in result['message']
    # 令牌控制器还没填令牌：稍等几次仍然没有就放弃
    cluster.agent_token = ''
    result = await service.configure_ai_connection(task, options, parsed, cluster)
    assert result['status'] == 'failed' and 'Secret kubedoor-agent' in result['message']
    cluster.unauthorized = True
    result = await service.configure_ai_connection(task, options, parsed, cluster)
    assert result['status'] == 'failed' and '认证失败（HTTP 401）' in result['message']


def test_ai_kubeconfig_keeps_the_cluster_section_and_swaps_the_credential():
    config = json.loads(CONFIG)
    config['clusters'][0]['cluster'].update({'certificate-authority-data': 'Q0E=', 'tls-server-name': 'api.internal'})
    parsed = module.parse(json.dumps(config))
    built = module.ai_kubeconfig(parsed, 'agent-sa-token')
    assert built['clusters'][0]['cluster'] == {'server': 'https://k8s.example.test', 'certificate-authority-data': 'Q0E=',
                                               'tls-server-name': 'api.internal'}
    # 自动配置的 kubeconfig 要能通过 AI 那边同一个解析器（只接受内嵌凭据）
    reparsed = module.parse_kubeconfig(built)
    assert reparsed['context'] == 'kubedoor-agent' and reparsed['config']['users'][0]['user'] == {'token': 'agent-sa-token'}


@pytest.mark.asyncio
async def test_no_master_key_rejects_before_persisting_upload(environment, monkeypatch):
    service, _, repo = environment
    monkeypatch.delenv('AI_ENCRYPTION_KEY')
    with pytest.raises(InstallError, match='加密密钥'):
        await service.inspect(CONFIG, 'admin')
    assert not repo.data


@pytest.mark.asyncio
async def test_preflight_denies_permission_without_changes(environment):
    service, cluster, _ = environment
    service.clients.clear()
    cluster.permitted = False
    task = await service.inspect(CONFIG, 'admin')
    task = await service.preflight(task['id'], 'production', {**task['payload']['options'], 'mode': 'agent'})
    assert task['status'] == 'draft'
    assert any('权限' in check['message'] for check in task['payload']['checks'])
    assert not cluster.applied


@pytest.mark.asyncio
async def test_existing_foreign_resource_not_overwritten(environment):
    service, cluster, _ = environment
    service.clients.clear()
    cluster.resources['Service', 'kubedoor-agent'] = {'metadata': {'name': 'kubedoor-agent'}}
    task = await service.inspect(CONFIG, 'admin')
    task = await service.preflight(task['id'], 'production', {**task['payload']['options'], 'mode': 'agent'})
    assert task['status'] == 'draft'
    assert any('不是网页安装创建的' in c['message'] for c in task['payload']['checks'])
    assert not cluster.applied


OTHER_INSTALL = '11111111-2222-3333-4444-555555555555'


@pytest.mark.asyncio
async def test_existing_agent_resources_are_listed_then_taken_over_after_confirmation(environment):
    service, cluster, repo = environment
    service.clients.clear()
    # 脚本安装留下的 Service；另一套 KubeDoor（安装记录不在当前控制端）网页安装的 Deployment 和配置
    cluster.resources['Service', 'kubedoor-agent'] = {'metadata': {'name': 'kubedoor-agent'}}
    foreign = {module.MANAGED: 'web-installer', module.OWNER: OTHER_INSTALL}
    cluster.resources['Deployment', 'kubedoor-agent'] = {'metadata': {'name': 'kubedoor-agent', 'labels': dict(foreign)}}
    cluster.resources['ConfigMap', 'kubedoor-agent-config'] = {'metadata': {'name': 'kubedoor-agent-config', 'labels': dict(foreign)},
                                                              'data': {'PROM_K8S_TAG_VALUE': 'old-name'}}
    task = await service.inspect(CONFIG, 'admin')
    options = {**task['payload']['options'], 'mode': 'agent'}
    task = await service.preflight(task['id'], 'production', options)
    assert task['status'] == 'draft' and task['payload']['force'] is False
    [error] = [check for check in task['payload']['checks'] if check['status'] == 'error']
    assert error['forceable'] is True and '强制覆盖安装' in error['message']
    takeover = {(item['kind'], item['name']): item['reason'] for item in task['payload']['takeover']}
    assert set(takeover) == {('Service', 'kubedoor-agent'), ('Deployment', 'kubedoor-agent'), ('ConfigMap', 'kubedoor-agent-config')}
    assert takeover['Service', 'kubedoor-agent'].startswith('不是网页安装创建的')
    assert takeover['Deployment', 'kubedoor-agent'].startswith('属于另一项安装')
    # 同一个资源只列一次，原因合并
    assert takeover['ConfigMap', 'kubedoor-agent-config'] == '现有 Agent 的集群名称是 old-name；属于另一项安装：安装记录不在当前控制端，或集群名称不同'
    assert not cluster.applied and not cluster.forced

    # 管理员确认后强制覆盖：预检通过并提示，安装时接管（服务端应用带 force，归属改成这次安装）
    task = await service.preflight(task['id'], 'production', options, force=True)
    assert task['status'] == 'ready' and task['payload']['force'] is True
    assert any(check['name'] == '强制覆盖' and check['status'] == 'warning' for check in task['payload']['checks'])
    await service.start(task['id'], task['revision'])
    await service.tasks[task['id']]
    done = await repo.get(task['id'])
    assert done['status'] == 'succeeded', done['payload']
    for kind, name in takeover:
        assert cluster.resources[kind, name]['metadata']['labels'][module.OWNER] == task['id']
        assert (kind, name, False) in cluster.forced
    assert cluster.resources['ConfigMap', 'kubedoor-agent-config']['data']['PROM_K8S_TAG_VALUE'] == 'production'


@pytest.mark.asyncio
async def test_takeover_is_not_offered_for_conflicts_in_the_control_plane_records(environment):
    service, cluster, _ = environment
    # 这个集群名称已被另一个在线的集群使用（目标集群里没有 Agent 配置）：强制覆盖也不能解决
    service.clients['production'] = {'online': True}
    task = await service.inspect(CONFIG, 'admin')
    task = await service.preflight(task['id'], 'production', {**task['payload']['options'], 'mode': 'agent'}, force=True)
    assert task['status'] == 'draft' and task['payload']['force'] is False
    [error] = [check for check in task['payload']['checks'] if check['status'] == 'error']
    assert '已有 Agent 登记' in error['message'] and 'forceable' not in error


@pytest.mark.asyncio
async def test_resources_appearing_after_preflight_are_not_taken_over_silently(environment):
    service, cluster, repo = environment
    service.clients.clear()
    task = await plan(service)
    assert task['payload']['force'] is False
    cluster.resources['Service', 'kubedoor-agent'] = {'metadata': {'name': 'kubedoor-agent'}}
    await service.start(task['id'], task['revision'])
    await service.tasks[task['id']]
    failed = await repo.get(task['id'])
    assert failed['status'] == 'failed' and '强制覆盖安装' in failed['payload']['error']
    assert not cluster.applied and not cluster.forced


@pytest.mark.asyncio
async def test_wrong_online_account_does_not_count_as_install_success(environment):
    service, cluster, repo = environment
    service.clients.clear()
    task = await plan(service)
    cluster.connection_user_id = 99
    await service.start(task['id'], task['revision'])
    await service.tasks[task['id']]
    done = await repo.get(task['id'])
    assert done['status'] == 'failed'
    assert done['encrypted']
    assert done['payload']['steps'][4]['status'] == 'error'
    assert cluster.resources['Namespace', 'kubedoor']


@pytest.mark.asyncio
async def test_partial_metrics_failure_reverifies_only_metrics_without_credentials(environment, monkeypatch):
    service, cluster, repo = environment
    service.clients.clear()
    task = await plan(service, 'full')
    monkeypatch.setattr(service, 'metrics_received', AsyncMock(return_value=False))
    await service.start(task['id'], task['revision'])
    await service.tasks[task['id']]
    failed = await repo.get(task['id'])
    assert failed['status'] == 'partial'
    assert failed['payload']['steps'][-1]['status'] == 'error'
    # 安装凭据失效（例如安装向导的临时令牌收尾时已撤销）也能重试：只重新查指标，不再连接目标集群
    repo.data[task['id']]['encrypted'] = ''
    cluster.unauthorized = True
    assert service.public(await repo.get(task['id']))['retryable']
    applied = len(cluster.applied)
    monkeypatch.setattr(module.auth_store, 'get_user_by_name', AsyncMock(side_effect=AssertionError('不应再取安装账号')))
    monkeypatch.setattr(service, 'metrics_received', AsyncMock(return_value=True))
    await service.start(task['id'], None, retry=True)
    await service.tasks[task['id']]
    done = await repo.get(task['id'])
    assert done['status'] == 'succeeded', done['payload']
    assert [step['status'] for step in done['payload']['steps']] == ['success'] * len(module.STEP_NAMES)
    assert len(cluster.applied) == applied
    assert done['encrypted'] == ''


@pytest.mark.asyncio
async def test_partial_reverify_keeps_partial_and_explains_when_metrics_still_missing(environment, monkeypatch):
    service, _, repo = environment
    service.clients.clear()
    task = await plan(service, 'full')
    monkeypatch.setattr(service, 'metrics_received', AsyncMock(return_value=False))
    await service.start(task['id'], task['revision'])
    await service.tasks[task['id']]

    async def seen_some(task, options):
        service.metrics_seen[task['id']] = {'vmagent', 'kube-state-metrics'}
        return False

    monkeypatch.setattr(service, 'metrics_received', seen_some)
    await service.start(task['id'], None, retry=True)
    await service.tasks[task['id']]
    done = await repo.get(task['id'])
    assert done['status'] == 'partial'
    assert 'k8s-node-exporter、k8s-cadvisor' in done['payload']['error']
    assert '已正常：vmagent、kube-state-metrics' in done['payload']['error']


@pytest.mark.asyncio
async def test_failed_retry_reuses_same_agent_account(environment, monkeypatch):
    service, cluster, repo = environment
    service.clients.clear()
    task = await plan(service)
    cluster.connection_user_id = 99
    await service.start(task['id'], task['revision'])
    await service.tasks[task['id']]
    failed = await repo.get(task['id'])
    assert failed['status'] == 'failed'
    credentials = module.unseal(failed)['credential']
    cluster.connection_user_id = 100
    monkeypatch.setattr(module.auth_store, 'get_user_by_name', AsyncMock(return_value={'id': 100, 'role': 'agent'}))
    monkeypatch.setattr(module, 'verify_local', AsyncMock(return_value=True))
    await service.start(task['id'], None, retry=True)
    await service.tasks[task['id']]
    assert (await repo.get(task['id']))['status'] == 'succeeded'
    assert module.auth_store.create_user.await_count == 1
    assert module.auth_store.get_user_by_name.call_args.args == (credentials['username'],)


@pytest.mark.asyncio
async def test_retry_with_expired_kubeconfig_says_to_upload_again(environment):
    service, cluster, repo = environment
    service.clients.clear()
    task = await plan(service)
    cluster.connection_user_id = 99
    await service.start(task['id'], task['revision'])
    await service.tasks[task['id']]
    cluster.unauthorized = True
    await service.start(task['id'], None, retry=True)
    await service.tasks[task['id']]
    done = await repo.get(task['id'])
    assert done['status'] == 'failed'
    assert 'kubeconfig 认证失败（HTTP 401）' in done['payload']['error']


def test_auth_failure_says_which_credentials_expired():
    exc = InstallError('Kubernetes GET /api/v1/namespaces/kube-system：认证失败（HTTP 401）', http_status=401)
    wizard = str(module.auth_hint({'payload': {'context': module.SETUP_CONTEXT}}, exc))
    assert '安装向导' in wizard and '临时令牌' in wizard and '安装新集群' in wizard
    upload = str(module.auth_hint({'payload': {'context': 'production'}}, exc))
    assert 'kubeconfig 认证失败（HTTP 401）' in upload
    other = InstallError('Kubernetes GET /x：权限不足（HTTP 403）', http_status=403)
    assert module.auth_hint({'payload': {'context': module.SETUP_CONTEXT}}, other) is other


def test_metrics_query_takes_timestamps_from_raw_series():
    # VictoriaMetrics 对比较运算的结果取 timestamp() 得到的是按 5 分钟对齐的计算点，不能写成 timestamp(up == 1)
    selector = 'up{origin_prometheus="prod-k8s",kubedoor_installation="task-1"}'
    assert module.metrics_query('origin_prometheus', 'prod-k8s', 'task-1') == (
        f'max by (job) (timestamp({selector}) and ({selector} == 1))')


@pytest.mark.asyncio
async def test_metrics_fresh_queries_latest_samples_and_keeps_new_jobs(environment, monkeypatch):
    from aiohttp import web
    from aiohttp.test_utils import TestServer
    service, _, _ = environment
    seen = []
    now = time.time()

    async def handler(request):
        seen.append(dict(request.query))
        return web.json_response({'status': 'success', 'data': {'resultType': 'vector', 'result': [
            {'metric': {'job': 'vmagent'}, 'value': [now, str(now - 5)]},
            {'metric': {'job': 'k8s-cadvisor'}, 'value': [now, str(now - 3600)]}]}})

    app = web.Application()
    app.router.add_get('/select/0/prometheus/api/v1/query', handler)
    server = TestServer(app)
    await server.start_server()
    try:
        monkeypatch.setenv('PROM_URL', str(server.make_url('/select/0/prometheus')))
        task = {'id': 'task-1', 'payload': {'metrics_started_at': now - 60}}
        fresh, error = await service.metrics_fresh(task, {'prom_k8s_tag_key': 'origin_prometheus', 'env': 'prod-k8s'})
    finally:
        await server.close()
    assert (fresh, error) == ({'vmagent'}, '')
    # nocache=1：不让 VictoriaMetrics 隐藏最近 30 秒的数据
    assert seen[0]['nocache'] == '1'
    assert seen[0]['query'] == module.metrics_query('origin_prometheus', 'prod-k8s', 'task-1')


@pytest.mark.asyncio
async def test_metrics_query_failure_is_reported_instead_of_missing_jobs(environment, monkeypatch):
    service, _, _ = environment
    monkeypatch.delenv('PROM_URL', raising=False)
    task = {'id': 'task-2', 'payload': {}}
    assert await service.metrics_received(task, {'prom_k8s_tag_key': 'origin_prometheus', 'env': 'prod-k8s'}) is False
    message = await service.metrics_diagnosis(task, {'namespace': 'kubedoor'})
    assert 'PROM_URL' in message and '重新验证指标采集' in message


@pytest.mark.asyncio
async def test_metrics_timeout_names_missing_jobs_with_vmagent_target_errors(environment):
    service, _, _ = environment

    class Kube:
        async def list(self, path, params=None):
            assert path == '/api/v1/namespaces/kubedoor/pods' and params == {'labelSelector': 'app=vmagent'}
            return [{'metadata': {'name': 'vmagent-1'}, 'status': {'phase': 'Running'}}]

        async def request(self, method, path, **kwargs):
            assert path == '/api/v1/namespaces/kubedoor/pods/vmagent-1:8429/proxy/api/v1/targets'
            return {'data': {'activeTargets': [
                {'labels': {'job': 'k8s-cadvisor'}, 'health': 'down', 'lastError': 'server returned\nHTTP status 403 Forbidden'},
                {'labels': {'job': 'k8s-cadvisor'}, 'health': 'down', 'lastError': ''},
                {'labels': {'job': 'k8s-node-exporter'}, 'health': 'up'}]}}

    task = {'id': 'task-3', 'payload': {}}
    service.metrics_seen['task-3'] = {'vmagent'}
    message = await service.metrics_diagnosis(task, {'namespace': 'kubedoor'}, Kube())
    assert 'k8s-cadvisor（2 个目标都抓取失败：server returned HTTP status 403 Forbidden）' in message
    assert 'kube-state-metrics（没有发现抓取目标）' in message
    assert 'k8s-node-exporter（1/1 个目标抓取正常' in message
    assert '已正常：vmagent' in message
    assert 'task-3' not in service.metrics_seen


@pytest.mark.asyncio
async def test_unreadable_vmagent_targets_still_name_missing_jobs(environment):
    service, _, _ = environment

    class Kube:
        async def list(self, path, params=None):
            raise InstallError('Kubernetes GET pods：权限不足（HTTP 403）', http_status=403)

    service.metrics_seen['task-4'] = {'vmagent', 'kube-state-metrics', 'k8s-node-exporter'}
    message = await service.metrics_diagnosis({'id': 'task-4', 'payload': {}}, {'namespace': 'kubedoor'}, Kube())
    assert '没有采到新指标：k8s-cadvisor。' in message.replace('；已正常：vmagent、kube-state-metrics、k8s-node-exporter', '')


@pytest.mark.asyncio
async def test_revision_and_expired_credentials_block_start(environment):
    service, _, repo = environment
    service.clients.clear()
    task = await plan(service)
    with pytest.raises(InstallError, match='计划已变化'):
        await service.start(task['id'], task['revision'] - 1)
    repo.data[task['id']]['expires_at'] = module.utcnow() - timedelta(seconds=1)
    with pytest.raises(InstallError, match='已过期'):
        await service.start(task['id'], task['revision'])


@pytest.mark.asyncio
async def test_restart_marks_running_tasks_interrupted_without_deleting_resources(environment):
    service, cluster, repo = environment
    service.clients.clear()
    task = await plan(service)
    task['status'] = 'running'
    task['payload']['steps'] = [{'name': '应用资源', 'status': 'running'}]
    await repo.save(task)
    await service.open()
    try:
        assert (await repo.get(task['id']))['status'] == 'interrupted'
        assert not cluster.applied
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_identity_change_between_preview_and_start_stops_install(environment):
    service, cluster, repo = environment
    service.clients.clear()
    task = await plan(service)
    cluster.uid = 'different-cluster'
    await service.start(task['id'], task['revision'])
    await service.tasks[task['id']]
    assert (await repo.get(task['id']))['status'] == 'failed'
    assert not cluster.applied


def test_repo_public_id_is_json_serializable():
    import uuid
    row = unpack({'id': uuid.uuid4(), 'payload': '{}'})
    assert isinstance(row['id'], str)


def test_resource_path_rejects_unknown_manifest():
    with pytest.raises(InstallError, match='不支持'):
        resource_path({'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'name': 'shell'}})


@pytest.mark.asyncio
async def test_repreflight_keeps_credentials_in_unchanged_sanitized_url(environment):
    service, _, _ = environment
    task = await service.inspect(CONFIG, 'admin')
    options = {**task['payload']['options'], 'mode': 'agent', 'oss_url': 'https://user:pass@example.test/upload?key=private'}
    task = await service.preflight(task['id'], 'production', options)
    assert task['status'] == 'ready'
    assert task['payload']['options']['oss_url'] == 'https://example.test/upload'
    task = await service.preflight(task['id'], 'production', task['payload']['options'])
    assert module.unseal(task)['options']['oss_url'] == options['oss_url']


@pytest.mark.asyncio
async def test_partial_task_can_adjust_address_but_cannot_move_target(environment, monkeypatch):
    service, _, repo = environment
    task = await plan(service, 'full')
    monkeypatch.setattr(service, 'metrics_received', AsyncMock(return_value=False))
    await service.start(task['id'], task['revision'])
    await service.tasks[task['id']]
    task = await repo.get(task['id'])
    assert service.public(task)['target_locked']
    with pytest.raises(InstallError, match='不能更换目标'):
        await service.preflight(task['id'], 'production', {**task['payload']['options'], 'env': 'other'})
    task = await service.preflight(task['id'], 'production', {**task['payload']['options'], 'master_url': 'https://new.example.test'})
    assert task['status'] == 'ready'


@pytest.mark.asyncio
async def test_new_marker_is_part_of_frozen_manifest(environment):
    import yaml
    service, cluster, repo = environment
    task = await plan(service, 'full')
    material = module.unseal(task)
    resources = module.render_agent(material['options'], username='preflight', password='preflight')
    config = next(r for r in resources if r['kind'] == 'ConfigMap' and r['metadata']['name'] == 'vmagent-config')
    assert yaml.safe_load(config['data']['scrape.yml'])['global']['external_labels']['kubedoor_installation'] == task['id']


@pytest.mark.asyncio
async def test_full_install_writes_straight_to_time_series_without_exposing_credentials(environment, monkeypatch):
    service, cluster, repo = environment
    service.clients.clear()
    inspected = await service.inspect(CONFIG, 'admin')
    assert inspected['payload']['options']['metrics_available'] is True
    assert inspected['payload']['options']['mode'] == 'full'
    task = await plan(service, 'full')
    monkeypatch.setattr(service, 'metrics_received', AsyncMock(return_value=True))
    await service.start(task['id'], task['revision'])
    await service.tasks[task['id']]
    done = await repo.get(task['id'])
    assert done['status'] == 'succeeded', done['payload']
    # vmagent 直连控制端配置的时序库写入地址，账号口令在目标集群的 Secret 里
    vmagent = cluster.resources['Deployment', 'vmagent']['spec']['template']['spec']['containers'][0]
    assert '-remoteWrite.url=http://vm.example.test:8428/api/v1/write' in vmagent['args']
    assert '-remoteWrite.basicAuth.username=monit' in vmagent['args']
    secret = cluster.resources['Secret', 'kubedoor-metrics-credentials']['data']
    assert base64.b64decode(secret['password']).decode() == 'vm@secret'
    applied = json.dumps(cluster.applied)
    assert 'agent/metrics/write' not in applied
    # 页面拿到的安装任务里没有时序库写入地址和口令
    for public in (service.public(inspected), service.public(task), service.public(done)):
        text = json.dumps(public)
        assert 'vm.example.test' not in text and 'vm%40secret' not in text and 'vm@secret' not in text
        assert 'metrics_write_url' not in text


@pytest.mark.asyncio
async def test_full_mode_needs_master_write_url_or_override(environment, monkeypatch):
    service, cluster, _ = environment
    service.clients.clear()
    monkeypatch.delenv('METRICS_WRITE_URL')
    task = await service.inspect(CONFIG, 'admin')
    assert task['payload']['options']['metrics_available'] is False
    assert task['payload']['options']['mode'] == 'agent'
    with pytest.raises(InstallError, match='时序库写入地址'):
        await service.preflight(task['id'], 'production', {**task['payload']['options'], 'mode': 'full'})
    override = 'https://writer:w%21x@other.example.test/api/v1/write'
    task = await service.preflight(task['id'], 'production',
                                   {**task['payload']['options'], 'mode': 'full', 'remote_write_url': override})
    assert task['status'] == 'ready', task['payload']['checks']
    assert task['payload']['options']['remote_write_url'] == 'https://other.example.test/api/v1/write'
    resources = module.render_agent(module.unseal(task)['options'], username='preflight', password='preflight')
    vmagent = next(r for r in resources if r['kind'] == 'Deployment' and r['metadata']['name'] == 'vmagent')
    assert '-remoteWrite.url=https://other.example.test/api/v1/write' in vmagent['spec']['template']['spec']['containers'][0]['args']


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['full', 'agent'])
async def test_plan_made_by_older_master_must_be_checked_again(environment, mode):
    service, cluster, repo = environment
    service.clients.clear()
    task = await plan(service, mode)
    material = module.unseal(task)
    material['options'].pop('metrics_write_url')
    repo.data[task['id']]['encrypted'] = module.seal(material)
    await service.start(task['id'], task['revision'])
    await service.tasks[task['id']]
    done = await repo.get(task['id'])
    assert done['status'] == 'failed'
    assert '重新检测' in done['payload']['error']
    assert not cluster.applied
