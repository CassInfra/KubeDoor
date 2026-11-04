import base64
import copy
import json

import pytest

from kubedoor_setup.bootstrap_credentials import ensure_setup_key
from kubedoor_setup.errors import SetupError
from kubedoor_setup.kubernetes import OWNER
from kubedoor_setup.repository import Repository
from test_setup_api import FakeKubernetes, ID


AUTH = '/api/v1/namespaces/kubedoor/secrets/kubedoor-setup-auth'
STATE = '/api/v1/namespaces/kubedoor/configmaps/kubedoor-setup-state'


@pytest.mark.asyncio
async def test_first_boot_generates_persistent_key_inside_cluster_and_reuses_it():
    k8s = FakeKubernetes()
    repo = Repository(k8s)
    key = await ensure_setup_key(repo)
    assert len(key) == 64
    assert base64.b64decode(k8s.resources[AUTH]['data']['SETUP_KEY']).decode() == key
    assert k8s.resources[AUTH]['metadata']['annotations'][OWNER] == ID
    assert (await repo.load())['auth_initialized'] is True
    data = k8s.resources[STATE]['data']
    assert data['phase'] == 'draft'
    assert data['installation-id'] == ID
    assert data['public-url'] == ''
    assert key not in json.dumps(data)
    version = k8s.version
    assert await ensure_setup_key(Repository(k8s)) == key
    assert k8s.version == version


@pytest.mark.asyncio
async def test_initialized_key_loss_never_regenerates_even_before_configuration():
    k8s = FakeKubernetes()
    await ensure_setup_key(Repository(k8s))
    del k8s.resources[AUTH]
    with pytest.raises(SetupError, match='密钥丢失'):
        await ensure_setup_key(Repository(k8s))
    assert AUTH not in k8s.resources


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['draft', 'ready', 'running', 'failed', 'interrupted', 'finalizing', 'succeeded'])
async def test_existing_installation_without_key_is_not_reopened(phase):
    k8s = FakeKubernetes()
    repo = Repository(k8s)
    state = await repo.load()
    state['phase'] = phase
    await repo.save(state)
    with pytest.raises(SetupError):
        await ensure_setup_key(repo)
    assert AUTH not in k8s.resources


@pytest.mark.asyncio
@pytest.mark.parametrize('path', [
    '/apis/apps/v1/namespaces/kubedoor/deployments/kubedoor-master',
    '/api/v1/namespaces/kubedoor/secrets/kubedoor-setup-config',
])
async def test_existing_runtime_or_private_state_prevents_new_key(path):
    k8s = FakeKubernetes()
    k8s.resources[path] = {'metadata': {'name': 'existing'}}
    with pytest.raises(SetupError, match='已有控制端或安装配置'):
        await ensure_setup_key(Repository(k8s))
    assert AUTH not in k8s.resources


@pytest.mark.asyncio
async def test_concurrent_creation_reuses_winner_without_changing_its_key():
    k8s = FakeKubernetes()
    request = k8s.request
    key = 'winner-' + 'K' * 57

    async def race(method, path, **kwargs):
        if method == 'POST' and path == '/api/v1/namespaces/kubedoor/secrets':
            winner = copy.deepcopy(kwargs['body'])
            winner['data']['SETUP_KEY'] = base64.b64encode(key.encode()).decode()
            await request(method, path, body=winner)
            raise SetupError('Conflict', 409)
        return await request(method, path, **kwargs)

    k8s.request = race
    assert await ensure_setup_key(Repository(k8s)) == key


@pytest.mark.asyncio
async def test_corrupt_or_foreign_key_is_never_replaced():
    k8s = FakeKubernetes()
    await ensure_setup_key(Repository(k8s))
    k8s.resources[AUTH]['metadata']['annotations'][OWNER] = 'other-installation'
    old = copy.deepcopy(k8s.resources[AUTH])
    with pytest.raises(SetupError, match='归属不匹配'):
        await ensure_setup_key(Repository(k8s))
    assert k8s.resources[AUTH] == old


@pytest.mark.asyncio
async def test_completed_installation_rejects_even_existing_key():
    k8s = FakeKubernetes()
    repo = Repository(k8s)
    await ensure_setup_key(repo)
    state = await repo.load()
    state['phase'] = 'succeeded'
    state['public_url'] = 'https://kd.example.com'
    await repo.save(state)
    with pytest.raises(SetupError, match='安装已经完成'):
        await ensure_setup_key(repo)
    assert k8s.resources[STATE]['data']['public-url'] == 'https://kd.example.com'
