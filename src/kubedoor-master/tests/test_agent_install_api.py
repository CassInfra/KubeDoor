"""Agent 安装 HTTP 接口经过真实鉴权中间件，不连接目标 K8S 或数据库。"""

import copy
import json
import pathlib
import sys

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import auth

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'kubedoor-tools'))

from agent_install import api
from agent_install.service import Service
from test_auth_flow import add_user, basic, fake, login  # noqa: F401


class MemoryRepository:
    def __init__(self):
        self.rows = {}

    async def create(self, task):
        self.rows[task['id']] = copy.deepcopy(task)

    async def get(self, task_id):
        return copy.deepcopy(self.rows.get(task_id))

    async def list(self):
        return copy.deepcopy(list(self.rows.values()))


def kubeconfig():
    return json.dumps({
        'apiVersion': 'v1', 'kind': 'Config', 'current-context': 'production',
        'clusters': [{'name': 'cluster', 'cluster': {'server': 'https://10.0.0.1:6443'}}],
        'contexts': [{'name': 'production', 'context': {'cluster': 'cluster', 'user': 'installer'}}],
        'users': [{'name': 'installer', 'user': {'token': 'private-install-token-do-not-export'}}],
    })


async def make_api(fake, monkeypatch):
    monkeypatch.setenv('AI_ENCRYPTION_KEY', 'a' * 64)
    monkeypatch.setenv('KUBEDOOR_EXTURL', 'https://kubedoor.example.com')
    from auth.seed import seed_builtin_accounts

    await seed_builtin_accounts()
    app = web.Application()
    auth.setup(app, {})
    api.register_routes(app, {})
    repository = MemoryRepository()
    app[api.KEY] = Service({}, repository=repository)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client, repository


@pytest.mark.asyncio
async def test_install_api_requires_system_admin(fake, monkeypatch):
    client, _ = await make_api(fake, monkeypatch)
    try:
        endpoint = '/api/admin/agent-installations'
        assert (await client.get(endpoint)).status == 401
        await add_user(fake, 'reader', 'read', 'Reader-pass-1')
        read = {'Authorization': basic('reader', 'Reader-pass-1')}
        assert (await client.get(endpoint, headers=read)).status == 403
        assert (await client.post(endpoint + '/inspect', headers=read, json={'kubeconfig': kubeconfig()})).status == 403
        agent = {'Authorization': basic('Up4biLko1dNh', 'qCa22jDkfc9y')}
        assert (await client.get(endpoint, headers=agent)).status == 403
        await add_user(fake, 'installer', 'admin', 'Installer-pass-1')
        assert (await login(client, 'installer', 'Installer-pass-1')).status == 200
        assert (await client.get(endpoint + '/defaults')).status == 200
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_authenticated_upload_does_not_export_kubeconfig(fake, monkeypatch):
    client, repository = await make_api(fake, monkeypatch)
    try:
        await add_user(fake, 'installer', 'admin', 'Installer-pass-1')
        assert (await login(client, 'installer', 'Installer-pass-1')).status == 200
        response = await client.post('/api/admin/agent-installations/inspect', json={'kubeconfig': kubeconfig()})
        assert response.status == 200, await response.text()
        text = await response.text()
        data = (await response.json())['data']
        assert data['status'] == 'draft' and data['context'] == 'production'
        assert data['options']['env'] == 'production'
        assert 'private-install-token' not in text
        assert 'kubeconfig' not in data and 'encrypted' not in data
        stored = repository.rows[data['id']]
        assert stored['encrypted'] and 'private-install-token' not in stored['encrypted']
        fetched = await client.get('/api/admin/agent-installations/' + data['id'])
        assert fetched.status == 200 and 'private-install-token' not in await fetched.text()
        listing = await client.get('/api/admin/agent-installations')
        assert listing.status == 200 and 'private-install-token' not in await listing.text()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_install_upload_size_and_invalid_body(fake, monkeypatch):
    client, repository = await make_api(fake, monkeypatch)
    try:
        await add_user(fake, 'installer', 'admin', 'Installer-pass-1')
        headers = {'Authorization': basic('installer', 'Installer-pass-1')}
        endpoint = '/api/admin/agent-installations/inspect'
        for value in ({'kubeconfig': 'x' * (1024 * 1024 + 1)}, [], {'kubeconfig': 42}):
            response = await client.post(endpoint, headers=headers, json=value)
            assert response.status in (400, 413), await response.text()
        invalid = await client.post(endpoint, headers={**headers, 'Content-Type': 'application/json'}, data='{')
        assert invalid.status == 400
        assert not repository.rows
    finally:
        await client.close()
