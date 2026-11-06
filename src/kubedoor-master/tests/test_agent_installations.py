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

    def __call__(self, parsed):
        assert parsed['config']['users'][0]['user']['token'] == 'private-kube-token'
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def request(self, method, path, **kwargs):
        if path == '/version':
            return {'gitVersion': 'v1.30.1'}
        if path == '/api/v1/namespaces/kube-system':
            return {'metadata': {'uid': self.uid}}
        if path.endswith('/configmaps/kubedoor-agent-config'):
            return copy.deepcopy(self.resources.get(('ConfigMap', 'kubedoor-agent-config')))
        raise AssertionError((method, path))

    async def list(self, path, params=None):
        if path == '/api/v1/nodes':
            return [{'metadata': {'name': 'worker'}}]
        return []

    async def get(self, resource):
        return copy.deepcopy(self.resources.get((resource['kind'], resource['metadata']['name'])))

    async def apply(self, resource, dry_run=False):
        namespace = resource['metadata'].get('namespace')
        if namespace and ('Namespace', namespace) not in self.resources:
            raise InstallError('不存在的 Namespace')
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
    assert any('不属于网页安装' in c['message'] for c in task['payload']['checks'])
    assert not cluster.applied


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
async def test_partial_metrics_failure_retries_same_account_and_keeps_agent(environment, monkeypatch):
    service, cluster, repo = environment
    service.clients.clear()
    task = await plan(service, 'full')
    monkeypatch.setattr(service, 'metrics_received', AsyncMock(return_value=False))
    await service.start(task['id'], task['revision'])
    await service.tasks[task['id']]
    failed = await repo.get(task['id'])
    assert failed['status'] == 'partial'
    assert service.public(failed)['retryable']
    credentials = module.unseal(failed)['credential']
    monkeypatch.setattr(module.auth_store, 'get_user_by_name', AsyncMock(return_value={'id': 100, 'role': 'agent'}))
    monkeypatch.setattr(module, 'verify_local', AsyncMock(return_value=True))
    monkeypatch.setattr(service, 'metrics_received', AsyncMock(return_value=True))
    await service.start(task['id'], None, retry=True)
    await service.tasks[task['id']]
    assert (await repo.get(task['id']))['status'] == 'succeeded'
    assert module.auth_store.create_user.await_count == 1
    assert module.auth_store.get_user_by_name.call_args.args == (credentials['username'],)


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
