from types import SimpleNamespace
from unittest.mock import AsyncMock

from psycopg.conninfo import conninfo_to_dict
from starlette.testclient import TestClient

from kubedoor_ai.http import create_app
from kubedoor_ai.storage import pg_dsn, pg_options


def test_both_database_drivers_use_wizard_tls_and_preserve_password(monkeypatch):
    monkeypatch.setenv('PG_SSLMODE', 'verify-full')
    monkeypatch.setenv('PG_PASSWORD', "a' b$:&")
    assert pg_options()['ssl'] == 'verify-full'
    parsed = conninfo_to_dict(pg_dsn())
    assert parsed['sslmode'] == 'verify-full'
    assert parsed['password'] == "a' b$:&"


def test_internal_health_requires_identity_and_checks_database(monkeypatch):
    monkeypatch.setenv('KUBEDOOR_SETUP_ID', 'install-one')
    store = SimpleNamespace(one=AsyncMock(return_value={'ready': 1}))
    app = create_app(SimpleNamespace(store=store), token='setup-internal')
    headers = {'X-Kubedoor-Token': 'setup-internal', 'X-User-Name': 'kubedoor', 'X-User-Permission': 'admin'}
    with TestClient(app) as client:
        path = '/api/ai/internal/setup-health'
        assert client.get(path).status_code == 401
        response = client.get(path, headers=headers)
        assert response.json() == {'ok': True, 'installation_id': 'install-one'}
        store.one.side_effect = RuntimeError('secret-password')
        response = client.get(path, headers=headers)
        assert response.status_code == 503
        assert 'secret-password' not in response.text
