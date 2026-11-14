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
from loguru import logger

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'kubedoor-tools'))

import general_settings
from agent_install import service as module
from agent_install.kubernetes import InstallError, resource_path
from agent_install.repository import unpack
from auth.crypto import decrypt_secret, encrypt_secret
from notify import api as notify_api
from notify import service as notify_service


CONFIG = json.dumps({
    'apiVersion': 'v1', 'kind': 'Config', 'current-context': 'production',
    'contexts': [{'name': 'production', 'context': {'cluster': 'cluster', 'user': 'admin'}}],
    'clusters': [{'name': 'cluster', 'cluster': {'server': 'https://k8s.example.test'}}],
    'users': [{'name': 'admin', 'user': {'token': 'private-kube-token'}}],
})
# 通用配置里的时序库远程写地址：网页安装的 vmagent 默认直连它
WRITE_URL = 'http://monit:vm%40secret@vm.example.test:8428/api/v1/write'
# 通用配置（测试里替换 general_settings.value）；没列出的项用 bundle.BASE 的默认值
SETTINGS = {
    'kubedoor_exturl': 'https://kubedoor.example.test', 'metrics_write_url': WRITE_URL, 'prom_url': '',
    'prom_k8s_tag_key': 'origin_prometheus', 'oss_url': '', 'image_repo': module.defaults()['image_repo'],
    'image_vmagent': module.defaults()['image_vmagent'],
    'image_kube_state_metrics': module.defaults()['image_kube_state_metrics'],
    'image_node_exporter': module.defaults()['image_node_exporter'],
}
WECOM = 'a1b2c3d4-0000-4bc4-97a0-0ec2c3f5a1b2'
WECOM_URL = f'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={WECOM}'
DING = '0123456789abcdef' * 4
FEISHU = '3f8c1a2b-1111-2222-3333-444455556666'


def masked(url):
    """测试用的 general_settings.masked：把密码换成 ******"""
    head, sep, tail = url.partition('@')
    return f'{head.rsplit(":", 1)[0]}:******@{tail}' if sep and head.count(':') > 1 else url


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
def settings(monkeypatch):
    """通用配置：用例里改这个字典就是改通用配置（安装服务每次用的时候取）；别的项照常用 general_settings 的值"""
    values = dict(SETTINGS)
    original = general_settings.value
    monkeypatch.setattr(general_settings, 'value', lambda name: values[name] if name in values else original(name))
    monkeypatch.setattr(general_settings, 'masked', masked)
    return values


class NotifyStore:
    """auth_settings 里只有 notification 这一行（读写都做一次 JSON 往返，和数据库一样）"""

    def __init__(self, value=None):
        self.value, self.puts, self.put_error = value, [], None

    async def get_setting_row(self, key):
        assert key == notify_api.SETTING_KEY
        return None if self.value is None else {'value': json.loads(json.dumps(self.value)), 'updated_by': 'kubedoor',
                                                'updated_at': None}

    async def put_setting_row(self, key, value, updated_by):
        assert key == notify_api.SETTING_KEY
        if self.put_error is not None:
            raise self.put_error
        self.value = json.loads(json.dumps(value))
        self.puts.append((self.value, updated_by))
        return {'value': json.loads(json.dumps(value)), 'updated_by': updated_by, 'updated_at': None}


@pytest.fixture
def environment(monkeypatch, settings):
    monkeypatch.setenv('AI_ENCRYPTION_KEY', 'ab' * 32)
    # 旧的环境变量不再起作用：安装参数只从通用配置取
    monkeypatch.setenv('KUBEDOOR_EXTURL', 'https://env-ignored.example.test')
    monkeypatch.setenv('METRICS_WRITE_URL', 'http://env-ignored.example.test/api/v1/write')
    monkeypatch.setenv('AGENT_INSTALL_DEFAULTS', json.dumps({'image_repo': 'env-ignored.example.test/kd', 'oss_url': 'https://env-ignored.example.test'}))
    monkeypatch.setenv('PROM_URL', 'http://env-ignored.example.test')
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


@pytest.fixture
def audits(monkeypatch, environment):
    """审计记录（auth.audit 是安装服务和消息通知共用的同一个模块）"""
    records = []
    monkeypatch.setattr(module.audit, 'record_bg', lambda username, action, **kw: records.append({'username': username, 'action': action, **kw}))
    return records


@pytest.fixture
def notify_store(monkeypatch):
    """消息通知配置：假的 auth_settings，缓存从空开始"""
    store = NotifyStore()
    monkeypatch.setattr(notify_api, 'store', store)
    monkeypatch.setattr(notify_service, 'store', store)
    monkeypatch.setattr(notify_service, '_snapshot', notify_service.Snapshot())
    monkeypatch.setattr(notify_service, '_summary', None)
    monkeypatch.setattr(notify_api, '_save_lock', asyncio.Lock())
    return store


@pytest.fixture
def logs():
    lines = []
    handler_id = logger.add(lambda message: lines.append(str(message)), level='DEBUG')
    yield lines
    logger.remove(handler_id)


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


@pytest.mark.asyncio
@pytest.mark.parametrize('extra, written', [({}, None), ({'aliases': ['生产']}, ['生产'])])
async def test_aliases_written_only_when_given_and_image_repo_from_general_settings(environment, settings, extra, written):
    """网页「安装 Agent」不提交别名、镜像仓库和镜像拉取 Secret：镜像仓库用通用配置，页面提交了也不认；
    重装不清掉 Agent 列表里编辑过的别名，安装向导第 6 步填了别名才写"""
    service, cluster, repo = environment
    service.clients.clear()
    settings['image_repo'] = 'registry.example.test/kd'
    task = await service.inspect(CONFIG, 'admin')
    assert 'image_repo' not in task['payload']['options']
    page = {k: v for k, v in task['payload']['options'].items() if k not in ('aliases', 'image_pull_secret')}
    task = await service.preflight(task['id'], 'production', {**page, 'mode': 'agent', 'image_repo': 'evil.example.test/x', **extra})
    assert task['status'] == 'ready', task['payload']['checks']
    await service.start(task['id'], task['revision'])
    await service.tasks[task['id']]
    assert (await repo.get(task['id']))['status'] == 'succeeded'
    agent = cluster.resources['Deployment', 'kubedoor-agent']['spec']['template']['spec']
    assert agent['containers'][0]['image'].startswith('registry.example.test/kd/kubedoor-agent:')
    assert 'imagePullSecrets' not in agent
    updates = [call.args for call in module.db.pg_execute.await_args_list if 'aliases' in call.args[0]]
    assert updates == ([('UPDATE k8s_agent_status SET aliases=$2 WHERE env=$1', 'production', written)] if written else [])


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
async def test_metrics_fresh_queries_latest_samples_and_keeps_new_jobs(environment, settings):
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
        # 查询地址取通用配置的 prom_url（环境变量 PROM_URL 不再起作用）
        settings['prom_url'] = str(server.make_url('/select/0/prometheus'))
        task = {'id': 'task-1', 'payload': {'metrics_started_at': now - 60}}
        fresh, error = await service.metrics_fresh(task, {'prom_k8s_tag_key': 'origin_prometheus', 'env': 'prod-k8s'})
    finally:
        await server.close()
    assert (fresh, error) == ({'vmagent'}, '')
    # nocache=1：不让 VictoriaMetrics 隐藏最近 30 秒的数据
    assert seen[0]['nocache'] == '1'
    assert seen[0]['query'] == module.metrics_query('origin_prometheus', 'prod-k8s', 'task-1')


@pytest.mark.asyncio
async def test_metrics_query_failure_is_reported_instead_of_missing_jobs(environment, settings):
    service, _, _ = environment
    settings['prom_url'] = ''
    task = {'id': 'task-2', 'payload': {}}
    assert await service.metrics_received(task, {'prom_k8s_tag_key': 'origin_prometheus', 'env': 'prod-k8s'}) is False
    message = await service.metrics_diagnosis(task, {'namespace': 'kubedoor'})
    assert '通用配置里还没有时序库查询地址' in message and '重新验证指标采集' in message


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
async def test_oss_url_is_not_written_into_the_agent_secret(environment, settings):
    """对象存储地址由 master 每次转发 /api/pod/* 时带给 Agent：网页安装的 Secret 里 OSS_URL 为空串，页面提交了也不认"""
    service, cluster, repo = environment
    service.clients.clear()
    settings['oss_url'] = 'https://oss:oss-secret@objects.example.test/upload'
    task = await service.inspect(CONFIG, 'admin')
    task = await service.preflight(task['id'], 'production', {
        **task['payload']['options'], 'mode': 'agent', 'oss_url': 'https://page:page-secret@page.example.test/upload'})
    assert task['status'] == 'ready', task['payload']['checks']
    assert 'oss_url' not in task['payload']['options']
    assert module.unseal(task)['options']['oss_url'] == ''
    await service.start(task['id'], task['revision'])
    await service.tasks[task['id']]
    done = await repo.get(task['id'])
    assert done['status'] == 'succeeded', done['payload']
    secret = cluster.resources['Secret', 'kubedoor-agent-credentials']['data']
    assert set(secret) == {'KUBEDOOR_MASTER', 'OSS_URL'} and base64.b64decode(secret['OSS_URL']).decode() == ''
    applied = json.dumps(cluster.applied)
    assert 'objects.example.test' not in applied and 'page.example.test' not in applied


@pytest.mark.asyncio
async def test_install_parameters_come_from_general_settings(environment, settings, monkeypatch):
    """KubeDoor 地址、标签 key、镜像仓库、三个采集镜像、远程写地址都取通用配置；旧环境变量不起作用"""
    service, cluster, repo = environment
    service.clients.clear()
    settings.update({
        'kubedoor_exturl': 'https://kd.example.test', 'prom_k8s_tag_key': 'cluster_name',
        'image_repo': 'registry.example.test/kd', 'image_vmagent': 'registry.example.test/vmagent:v1',
        'image_kube_state_metrics': 'registry.example.test/ksm:v2', 'image_node_exporter': 'registry.example.test/ne:v3',
        'metrics_write_url': 'https://writer:w%21x@vm.settings.test/api/v1/write',
    })
    task = await service.inspect(CONFIG, 'admin')
    options = task['payload']['options']
    assert options['master_url'] == 'https://kd.example.test' and options['mode'] == 'full'
    assert options['agent_tag'] == module.defaults()['agent_tag']
    task = await service.preflight(task['id'], 'production', {**options, 'mode': 'full'})
    assert task['status'] == 'ready', task['payload']['checks']
    monkeypatch.setattr(service, 'metrics_received', AsyncMock(return_value=True))
    await service.start(task['id'], task['revision'])
    await service.tasks[task['id']]
    assert (await repo.get(task['id']))['status'] == 'succeeded'
    images = {(r['kind'], r['metadata']['name']): r['spec']['template']['spec']['containers'][0]['image']
              for r in cluster.applied if r['kind'] in ('Deployment', 'DaemonSet')}
    assert images['Deployment', 'kubedoor-agent'].startswith('registry.example.test/kd/kubedoor-agent:')
    assert images['Deployment', 'vmagent'] == 'registry.example.test/vmagent:v1'
    assert images['Deployment', 'kube-state-metrics'] == 'registry.example.test/ksm:v2'
    assert images['DaemonSet', 'node-exporter'] == 'registry.example.test/ne:v3'
    vmagent = cluster.resources['Deployment', 'vmagent']['spec']['template']['spec']['containers'][0]['args']
    assert '-remoteWrite.url=https://vm.settings.test/api/v1/write' in vmagent
    scrape = cluster.resources['ConfigMap', 'vmagent-config']['data']['scrape.yml']
    assert 'cluster_name: production' in scrape
    master = base64.b64decode(cluster.resources['Secret', 'kubedoor-agent-credentials']['data']['KUBEDOOR_MASTER']).decode()
    assert master.endswith('@kd.example.test')
    assert 'env-ignored' not in json.dumps(cluster.applied)


@pytest.mark.asyncio
async def test_defaults_show_settings_in_use_without_passwords(environment, settings):
    settings['oss_url'] = 'https://oss:oss-secret@objects.example.test/upload'
    data = module.public_defaults()
    assert data['settings'] == {
        'metrics_write_url': 'http://monit:******@vm.example.test:8428/api/v1/write',
        'oss_url': 'https://oss:******@objects.example.test/upload',
        'image_repo': SETTINGS['image_repo'], 'kubedoor_exturl': 'https://kubedoor.example.test',
    }
    assert data['metrics_available'] is True and data['master_url'] == 'https://kubedoor.example.test'
    for key in ('oss_url', 'remote_write_url', 'image_repo', 'metrics_write_url'):
        assert key not in data
    assert 'vm%40secret' not in json.dumps(data) and 'oss-secret' not in json.dumps(data)
    settings.update(metrics_write_url='', oss_url='')
    data = module.public_defaults()
    assert data['metrics_available'] is False and data['settings']['metrics_write_url'] == data['settings']['oss_url'] == ''


async def preflight_with(service, notification, mode='agent', task=None):
    task = task or await service.inspect(CONFIG, 'admin')
    options = {key: value for key, value in task['payload']['options'].items() if key != 'notification'}
    return await service.preflight(task['id'], 'production', {**options, 'mode': mode, 'notification': notification})


@pytest.mark.asyncio
@pytest.mark.parametrize('notification, message', [
    ({'type': 'qq', 'token': WECOM}, '通知配置：请选择 IM 类型'),
    ({'type': 'wecom', 'token': ''}, '通知配置：请填写企业微信机器人地址'),
    ({'type': 'wecom'}, '通知配置：请填写企业微信机器人地址'),
    ({'type': 'wecom', 'token': 'https://example.com/hook'}, '通知配置：不认识的机器人地址'),
    ({'type': 'dingding', 'token': WECOM_URL}, '通知配置：这是企业微信的机器人地址'),
    ({'type': 'wecom', 'token': WECOM, 'at': 'x' * 129}, '通知配置：默认 @ 人最多 128 个字符'),
    ('wecom', '通知配置的格式不正确'),
])
async def test_notification_is_validated_at_preflight(environment, notification, message):
    service, _, _ = environment
    service.clients.clear()
    with pytest.raises(InstallError) as error:
        await preflight_with(service, notification)
    assert str(error.value).startswith(message), str(error.value)
    assert WECOM not in str(error.value)


@pytest.mark.asyncio
async def test_notification_token_only_lives_in_the_sealed_material(environment, audits, logs):
    service, _, repo = environment
    service.clients.clear()
    task = await preflight_with(service, {'type': 'wecom', 'token': f' {WECOM_URL} ', 'at': ' zhangsan '})
    assert task['status'] == 'ready', task['payload']['checks']
    assert task['payload']['options']['notification'] == {'type': 'wecom', 'at': 'zhangsan', 'configured': True}
    assert module.unseal(task)['options']['notification'] == {'type': 'wecom', 'token': WECOM, 'at': 'zhangsan'}
    stored = repo.data[task['id']]
    assert WECOM not in json.dumps(stored['payload']) and WECOM not in stored['encrypted']
    assert WECOM not in json.dumps(service.public(task))
    assert WECOM not in json.dumps(audits) and all(WECOM not in line for line in logs)
    # 没填通知：没有这个键或为 null 都表示不配置
    task = await preflight_with(service, None)
    assert task['payload']['options']['notification'] is None and module.unseal(task)['options']['notification'] is None


@pytest.mark.asyncio
async def test_repreflight_keeps_the_sealed_token_unless_the_im_type_changes(environment):
    service, _, _ = environment
    service.clients.clear()
    task = await preflight_with(service, {'type': 'wecom', 'token': WECOM, 'at': 'zhangsan'})
    # 页面只拿到「已填写」：机器人地址留空重新检测，沿用加密保存的；@ 人以这次的为准
    task = await preflight_with(service, {'type': 'wecom', 'token': '', 'at': 'lisi'}, task=task)
    assert task['status'] == 'ready', task['payload']['checks']
    assert module.unseal(task)['options']['notification'] == {'type': 'wecom', 'token': WECOM, 'at': 'lisi'}
    with pytest.raises(InstallError, match='通知配置：换了 IM 类型，请重新填写机器人地址'):
        await preflight_with(service, {'type': 'dingding', 'token': ''}, task=task)
    # 被拒的那次不影响已保存的；换类型时填了新地址就用新的
    assert module.unseal(await service.get(task['id']))['options']['notification']['token'] == WECOM
    task = await preflight_with(service, {'type': 'dingding', 'token': DING}, task=task)
    assert module.unseal(task)['options']['notification'] == {'type': 'dingding', 'token': DING, 'at': ''}
    # 去掉通知配置
    task = await preflight_with(service, None, task=task)
    assert module.unseal(task)['options']['notification'] is None


async def install(service, notification):
    task = await preflight_with(service, notification)
    assert task['status'] == 'ready', task['payload']['checks']
    await service.start(task['id'], task['revision'])
    await service.tasks[task['id']]
    return await service.repo.get(task['id'])


@pytest.mark.asyncio
async def test_agent_online_saves_the_cluster_notification_and_keeps_other_config(environment, audits, notify_store):
    service, _, _ = environment
    service.clients.clear()
    default = {'type': 'feishu', 'token': encrypt_secret(FEISHU), 'at': 'ou_1'}
    other = {'type': 'dingding', 'token': encrypt_secret(DING), 'at': '13800000000'}
    notify_store.value = {'default': default, 'clusters': {'test-k8s': other,
                                                           'production': {'type': 'slack', 'token': encrypt_secret('T0/B0/x'), 'at': ''}}}
    done = await install(service, {'type': 'wecom', 'token': WECOM_URL, 'at': 'zhangsan'})
    assert done['status'] == 'succeeded', done['payload']
    assert done['payload']['notification_result'] == {'status': 'saved', 'message': '已在消息通知里为集群 production 单独配置企业微信机器人'}
    [(saved, updated_by)] = notify_store.puts
    assert updated_by == 'admin'
    # 全局配置和其他集群原样保留，这个集群的单独配置被覆盖，token 加密保存
    assert saved['default'] == default and saved['clusters']['test-k8s'] == other
    production = saved['clusters']['production']
    assert production['type'] == 'wecom' and production['at'] == 'zhangsan' and decrypt_secret(production['token']) == WECOM
    assert WECOM not in json.dumps(saved)
    # 马上生效
    assert notify_service.resolve('production') == {'type': 'wecom', 'token': WECOM, 'at': 'zhangsan'}
    [record] = [r for r in audits if r['action'] == 'notification.update']
    assert record['username'] == 'admin' and record['env'] == 'production'
    assert record['detail'] == {'default': 'unchanged', 'clusters': {'added': [], 'updated': ['production'], 'removed': []},
                                'source': 'agent-install'}
    assert WECOM not in json.dumps(audits) and WECOM not in json.dumps(service.public(done))


@pytest.mark.asyncio
async def test_notification_save_failure_does_not_fail_the_install(environment, notify_store, logs):
    service, _, _ = environment
    service.clients.clear()
    notify_store.put_error = RuntimeError(f'db down while saving {WECOM}')
    done = await install(service, {'type': 'wecom', 'token': WECOM})
    assert done['status'] == 'succeeded', done['payload']
    result = done['payload']['notification_result']
    assert result['status'] == 'failed' and '没有写入消息通知配置' in result['message'] and '系统管理 → 消息通知' in result['message']
    assert WECOM not in json.dumps(done['payload']) and all(WECOM not in line for line in logs)
    assert notify_service.resolve('production') is None


@pytest.mark.asyncio
async def test_no_notification_result_without_notification(environment, notify_store):
    service, _, _ = environment
    service.clients.clear()
    done = await install(service, None)
    assert done['status'] == 'succeeded', done['payload']
    assert 'notification_result' not in done['payload'] and notify_store.puts == []


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
async def test_full_mode_needs_the_write_url_in_general_settings(environment, settings):
    service, cluster, _ = environment
    service.clients.clear()
    settings['metrics_write_url'] = ''
    task = await service.inspect(CONFIG, 'admin')
    assert task['payload']['options']['metrics_available'] is False
    assert task['payload']['options']['mode'] == 'agent'
    # 页面不能再填写指标写入地址覆盖：提交了也不认
    override = 'https://writer:w%21x@other.example.test/api/v1/write'
    with pytest.raises(InstallError) as error:
        await service.preflight(task['id'], 'production',
                                {**task['payload']['options'], 'mode': 'full', 'remote_write_url': override})
    assert str(error.value) == '通用配置里还没有时序库远程写地址，请在系统管理 → 通用配置填写，或选择仅安装 Agent'
    # 在通用配置填好后重新检测就能完整接入（检测时取当时的通用配置）
    settings['metrics_write_url'] = WRITE_URL
    task = await service.preflight(task['id'], 'production', {**task['payload']['options'], 'mode': 'full'})
    assert task['status'] == 'ready', task['payload']['checks']
    resources = module.render_agent(module.unseal(task)['options'], username='preflight', password='preflight')
    vmagent = next(r for r in resources if r['kind'] == 'Deployment' and r['metadata']['name'] == 'vmagent')
    assert '-remoteWrite.url=http://vm.example.test:8428/api/v1/write' in vmagent['spec']['template']['spec']['containers'][0]['args']
    assert 'other.example.test' not in json.dumps(resources)


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
