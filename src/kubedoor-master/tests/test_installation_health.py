"""Installation health must verify live dependencies and retain normal auth gates."""
import json
from unittest.mock import AsyncMock

import pytest

import db
import general_settings
import installation_health
from auth import policy
from auth.principal import Principal


class Response:
    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def json(self):
        return {'status': 'success', 'data': {'result': []}}


class Session(Response):
    def __init__(self, **kwargs):
        pass

    def get(self, url, **kwargs):
        assert url == 'http://vm.example.com/prefix/api/v1/query'
        assert kwargs['params'] == {'query': 'vector(1)'}
        assert kwargs['allow_redirects'] is False
        return Response()


@pytest.mark.asyncio
async def test_health_reports_this_installation_only_after_dependency_checks(monkeypatch):
    # 查询地址取通用配置(安装向导调这个接口之前已经写好);末尾的 / 去掉
    monkeypatch.setattr(general_settings, 'value', {'prom_url': 'http://vm.example.com/prefix/'}.__getitem__)
    monkeypatch.setenv('KUBEDOOR_SETUP_ID', 'install-one')
    monkeypatch.setattr(db, 'pg_fetchval', AsyncMock(return_value=1))
    monkeypatch.setattr(installation_health.aiohttp, 'ClientSession', Session)
    response = await installation_health.check(None)
    assert response.status == 200
    assert json.loads(response.text) == {'ok': True, 'installation_id': 'install-one'}


@pytest.mark.asyncio
async def test_database_failure_is_not_ready_and_does_not_leak_secret(monkeypatch):
    monkeypatch.setattr(db, 'pg_fetchval', AsyncMock(side_effect=RuntimeError('secret-password')))
    response = await installation_health.check(None)
    assert response.status == 503
    assert 'secret-password' not in response.text


@pytest.mark.asyncio
async def test_unconfigured_metrics_url_is_not_ready(monkeypatch):
    monkeypatch.setattr(general_settings, 'value', {'prom_url': ''}.__getitem__)
    monkeypatch.setattr(db, 'pg_fetchval', AsyncMock(return_value=1))
    response = await installation_health.check(None)
    assert response.status == 503
    assert json.loads(response.text)['ok'] is False


@pytest.mark.asyncio
async def test_health_policy_rejects_even_admin_cookie_session():
    rule = policy.match('GET', '/api/internal/setup-health')
    assert rule.kind == 'internal'
    with pytest.raises(policy.Deny):
        await policy.authorize(rule, {}, Principal('admin', 'admin', via='session'))


@pytest.mark.asyncio
async def test_pool_uses_wizard_tls_setting(monkeypatch):
    monkeypatch.setenv('PG_SSLMODE', 'verify-full')
    monkeypatch.setattr(db, '_pool', None)
    create = AsyncMock(return_value=object())
    monkeypatch.setattr(db.asyncpg, 'create_pool', create)
    await db.init_pool()
    assert create.await_args.kwargs['ssl'] == 'verify-full'
