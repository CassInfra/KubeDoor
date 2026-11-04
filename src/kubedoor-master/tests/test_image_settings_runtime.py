"""Image operations consume database settings without requiring a restart."""
import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import pytest

import image_settings
import image_tags_fetcher
from auth.principal import Principal
from auth_fakes import FakeStore


@pytest.mark.asyncio
async def test_tag_queries_use_current_database_credentials(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(image_settings, 'store', store)
    monkeypatch.setenv('AI_ENCRYPTION_KEY', '23' * 32)
    monkeypatch.setenv('REGISTRY_SECRET', '{"stale": "environment configuration"}')
    seen = []

    def fetch(env, image, credentials):
        seen.append(credentials)
        fetcher = image_tags_fetcher.ImageTagsFetcher(credentials)
        return fetcher._get_credentials(env, 'registry.example.com')

    monkeypatch.setattr(image_tags_fetcher, 'get_image_tags', fetch)
    values = [dict(domain='registry.example.com', env='default', ak='first', sk='password')]
    store.settings[image_settings.REGISTRIES_KEY] = image_settings.normalize_registries(values)
    assert (await image_tags_fetcher.get_image_tags_async('prod', 'registry.example.com/app:v1'))['ak'] == 'first'
    values[0]['ak'] = 'replacement'
    store.settings[image_settings.REGISTRIES_KEY] = image_settings.normalize_registries(values)
    assert (await image_tags_fetcher.get_image_tags_async('prod', 'registry.example.com/app:v1'))['ak'] == 'replacement'
    store.settings[image_settings.REGISTRIES_KEY] = []
    assert await image_tags_fetcher.get_image_tags_async('prod', 'registry.example.com/app:v1') == {}
    assert len(seen) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize('direct', [False, True])
async def test_image_update_handler_checks_database_before_forwarding(monkeypatch, direct):
    path = Path(__file__).resolve().parents[1] / 'kubedoor-master.py'
    spec = importlib.util.spec_from_file_location('master_image_settings_runtime', path)
    master = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(master)
    check = AsyncMock(return_value=(False, 'outside allowed time'))
    monkeypatch.setattr(master.image_settings, 'check_update_allowed', check)
    principal = Principal('Alice', 'rw' if direct else 'read', user_id=1)

    @web.middleware
    async def identity(request, handler):
        request['principal'] = principal
        request['update_image_direct'] = direct
        return await handler(request)

    async def reply(message):
        env = master.clients['prod']
        request_id = message['request_id']
        env['response_queue'][request_id] = {'message': 'updated'}
        env['response_events'][request_id].set()

    socket = type('Socket', (), {})()
    socket.send_json = AsyncMock(side_effect=reply)
    monkeypatch.setattr(master, 'clients', {'prod': {'online': True, 'ws': socket}})
    app = web.Application(middlewares=[identity])
    app.router.add_post('/api/update-image', master.http_handler)
    async with TestClient(TestServer(app)) as client:
        response = await client.post('/api/update-image?env=prod', json={'image_tag': 'v2'})
        if direct:
            assert response.status == 200
            check.assert_not_awaited()
        else:
            assert response.status == 403
            socket.send_json.assert_not_awaited()
            check.assert_awaited_once_with('prod', 'alice')
            check.return_value = (True, '')
            response = await client.post('/api/update-image?env=prod', json={'image_tag': 'v2'})
            assert response.status == 200
            assert check.await_count == 2
        socket.send_json.assert_awaited_once()
