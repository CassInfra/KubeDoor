"""镜像设置的持久化、授权、迁移与凭据保密测试，不访问外部仓库。"""
import copy
import io
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import auth
import image_settings as settings
from auth.crypto import decrypt_secret
from auth.principal import Principal
from auth_fakes import FakeStore
from test_auth_flow import add_user, basic, fake  # noqa: F401


@pytest.fixture
def memory(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(settings, 'store', store)
    monkeypatch.setenv('AI_ENCRYPTION_KEY', '12' * 32)
    return store


@pytest.fixture
def audits(monkeypatch):
    records = []
    monkeypatch.setattr(settings.audit, 'record_bg', lambda *args, **kwargs: records.append((args, kwargs)))
    return records


def rules(*extra):
    return [copy.deepcopy(settings.DEFAULT_RULE), *extra]


def allowed(env='prod', **changes):
    return {'env': env, 'enabled': True, 'start_time': '20:00', 'end_time': '08:00',
            'users': ['Alice'], **changes}


def registry(**changes):
    return {'domain': 'REGISTRY.example.com:5000', 'env': 'default', 'ak': 'private-access-key',
            'sk': 'private-secret-key', **changes}


async def client_for(monkeypatch, memory, role='admin'):
    @web.middleware
    async def principal(request, handler):
        request['principal'] = Principal('tester', role, user_id=1)
        return await handler(request)

    app = web.Application(middlewares=[principal])
    settings.register_routes(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


def test_rule_validation_and_username_normalization():
    value = settings.normalize_rules(rules(allowed(users=['Alice', 'alice', 'BOB'])))
    assert value[1]['users'] == ['alice', 'bob']
    for invalid in ([], [allowed()], rules(allowed(), allowed()),
                    rules(allowed(start_time='24:00')), rules(allowed(end_time='24:01')),
                    rules(allowed(end_time='20:00')),
                    rules(allowed(enabled='true')), rules(allowed(users=[])),
                    rules(allowed(users=['a b'])), rules(allowed(env='bad/name'))):
        with pytest.raises(ValueError):
            settings.normalize_rules(invalid)


@pytest.mark.asyncio
@pytest.mark.parametrize('hour,minute,expected', [(19, 59, False), (20, 0, True), (23, 59, True),
                                               (0, 0, True), (7, 59, True), (8, 0, False)])
async def test_overnight_period_boundaries(memory, hour, minute, expected):
    memory.settings[settings.RULES_KEY] = settings.normalize_rules(rules(allowed()))
    result, _ = await settings.check_update_allowed('prod', 'ALICE', datetime(2026, 11, 2, hour, minute))
    assert result is expected


@pytest.mark.asyncio
async def test_rules_use_beijing_time_scope_fallback_and_live_database(memory):
    memory.settings[settings.RULES_KEY] = settings.normalize_rules([
        allowed('default', users=['bob'], start_time='09:00', end_time='18:00'),
        allowed('prod'), {'env': 'blocked', **settings.DEFAULT_RULE, 'env': 'blocked'},
    ])
    assert (await settings.check_update_allowed('prod', 'alice', datetime(2026, 11, 2, 12, tzinfo=timezone.utc)))[0]
    assert (await settings.check_update_allowed('other', 'bob', datetime(2026, 11, 2, 9)))[0]
    assert not (await settings.check_update_allowed('blocked', 'bob', datetime(2026, 11, 2, 9)))[0]
    assert not (await settings.check_update_allowed('prod', 'bob', datetime(2026, 11, 2, 20)))[0]
    memory.settings[settings.RULES_KEY] = rules()
    assert not (await settings.check_update_allowed('prod', 'alice', datetime(2026, 11, 2, 20)))[0]


@pytest.mark.asyncio
@pytest.mark.parametrize('start,hour,minute,expected', [
    ('00:00', 0, 0, True), ('00:00', 12, 0, True), ('00:00', 23, 59, True),
    ('20:00', 19, 59, False), ('20:00', 20, 0, True), ('20:00', 23, 59, True),
    ('20:00', 0, 0, False),
])
async def test_end_of_day_accepts_24_00(memory, start, hour, minute, expected):
    memory.settings[settings.RULES_KEY] = settings.normalize_rules(rules(allowed(start_time=start, end_time='24:00')))
    assert (await settings.check_update_allowed('prod', 'alice', datetime(2026, 11, 2, hour, minute)))[0] is expected


@pytest.mark.asyncio
async def test_missing_invalid_or_unavailable_rule_configuration_denies(memory, monkeypatch):
    assert not (await settings.check_update_allowed('prod', 'alice'))[0]
    assert memory.settings == {}
    memory.settings[settings.RULES_KEY] = {'invalid': True}
    assert not (await settings.check_update_allowed('prod', 'alice'))[0]
    monkeypatch.setattr(memory, 'get_setting', AsyncMock(side_effect=RuntimeError('do not expose details')))
    ok, reason = await settings.check_update_allowed('prod', 'alice')
    assert not ok and 'do not expose' not in reason


def test_credentials_are_encrypted_and_blank_fields_preserve_them(memory):
    items = settings.normalize_registries([registry()])
    first = items[0]
    assert first['domain'] == 'registry.example.com:5000'
    assert first['ak'].startswith('v1:') and first['sk'].startswith('v1:')
    assert decrypt_secret(first['ak']) == 'private-access-key'
    assert decrypt_secret(first['sk']) == 'private-secret-key'
    public = settings.public_registries(items)
    assert public == [{'id': first['id'], 'domain': first['domain'], 'env': 'default', 'has_ak': True, 'has_sk': True}]
    changed = settings.normalize_registries([{**public[0], 'ak': '', 'sk': '', 'env': 'prod'}], items)
    assert changed[0]['ak'] == first['ak'] and changed[0]['sk'] == first['sk']
    assert changed[0]['env'] == 'prod'
    replaced = settings.normalize_registries([{**public[0], 'ak': 'new-ak'}], items)
    assert decrypt_secret(replaced[0]['ak']) == 'new-ak' and replaced[0]['sk'] == first['sk']
    assert settings.normalize_registries([], items) == []


@pytest.mark.parametrize('domain', ['https://registry.example.com', 'registry.example.com/repo',
                                  'user:pass@registry.example.com', 'host:99999', 'host?query=1',
                                  'host#fragment', 'bad host', '-invalid.example', 'host:'])
def test_registry_domain_validation(memory, domain):
    with pytest.raises(ValueError):
        settings.normalize_registries([registry(domain=domain)])


def test_duplicate_scopes_and_bad_credentials_rejected_without_echo(memory, monkeypatch):
    for items in ([registry(), registry(domain='registry.example.com:5000')],
                  [registry(ak='')], [registry(sk=42)], [registry(sk='secret\nvalue')],
                  [registry(id='not-a-known-id')]):
        with pytest.raises(ValueError) as exc:
            settings.normalize_registries(items)
        assert 'private-access-key' not in str(exc.value)
        assert 'private-secret-key' not in str(exc.value)
    monkeypatch.delenv('AI_ENCRYPTION_KEY')
    with pytest.raises(ValueError, match='AI_ENCRYPTION_KEY'):
        settings.normalize_registries([registry()])


@pytest.mark.asyncio
async def test_api_roundtrip_secret_redaction_atomic_validation_and_deletion(memory, monkeypatch, audits):
    client = await client_for(monkeypatch, memory)
    try:
        response = await client.get('/api/admin/image-update-rules')
        assert (await response.json())['data']['rules'] == rules()
        assert memory.settings == {}
        response = await client.put('/api/admin/image-update-rules', json={'rules': rules(allowed())})
        assert response.status == 200
        assert (await response.json())['data']['updated_by'] == 'tester'
        response = await client.put('/api/admin/image-registries', json={'registries': [registry()]})
        assert response.status == 200
        body = await response.json()
        public = body['data']['registries'][0]
        before = copy.deepcopy(memory.settings[settings.REGISTRIES_KEY])
        text = json.dumps(body)
        for value in ('private-access-key', 'private-secret-key', before[0]['ak'], before[0]['sk']):
            assert value not in text and value not in repr(audits)
        response = await client.put('/api/admin/image-registries', json={'registries': [public, registry(domain='new.example', sk='')]})
        assert response.status == 400
        assert memory.settings[settings.REGISTRIES_KEY] == before
        response = await client.put('/api/admin/image-registries', json={'registries': [{**public, 'ak': '', 'sk': ''}]})
        assert response.status == 200
        assert memory.settings[settings.REGISTRIES_KEY] == before
        assert await settings.registry_credentials() == {
            'registry.example.com:5000': {'default': {'ak': 'private-access-key', 'sk': 'private-secret-key'}}}
        response = await client.put('/api/admin/image-registries', json={'registries': []})
        assert response.status == 200 and await settings.registry_credentials() == {}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_real_auth_middleware_limits_settings_to_system_admin(fake, monkeypatch, audits):
    monkeypatch.setattr(settings, 'store', fake)
    monkeypatch.setenv('AI_ENCRYPTION_KEY', '12' * 32)
    await add_user(fake, 'admin-user', 'admin')
    await add_user(fake, 'cluster-user', 'cluster_admin')
    app = web.Application()
    auth.setup(app, {})
    settings.register_routes(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        for endpoint in ('image-update-rules', 'image-registries'):
            path = '/api/admin/' + endpoint
            assert (await client.get(path)).status == 401
            headers = {'Authorization': basic('cluster-user', 'Str0ng-pass!')}
            assert (await client.get(path, headers=headers)).status == 403
            assert (await client.put(path, headers=headers, json={})).status == 403
            headers = {'Authorization': basic('admin-user', 'Str0ng-pass!')}
            assert (await client.get(path, headers=headers)).status == 200
        response = await client.put('/api/admin/image-registries', headers=headers, json={'registries': [registry()]})
        assert response.status == 200
        assert any(action == 'api.write' for (_, action), _ in audits)
        for value in ('private-access-key', 'private-secret-key'):
            assert value not in repr(audits) and value not in await response.text()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_handlers_defensively_reject_non_admin(memory, monkeypatch, audits):
    client = await client_for(monkeypatch, memory, role='read')
    try:
        for endpoint in ('image-update-rules', 'image-registries'):
            path = '/api/admin/' + endpoint
            assert (await client.get(path)).status == 403
            assert (await client.put(path, json={})).status == 403
        assert not audits and not memory.settings
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_legacy_import_is_once_and_saved_empty_registries_stay_empty(memory, monkeypatch):
    async def insert(key, value):
        if key in memory.settings:
            return False
        await memory.put_setting_row(key, value, 'legacy-import')
        return True

    monkeypatch.setattr(settings, '_insert_absent', insert)
    legacy_rules = {'default': {'isOperationAllowed': False}, 'prod': {
        'isOperationAllowed': True, 'allowedOperationPeriod': '20:00-08:00', 'user': ['Alice']}}
    legacy_registry = {'registry.example.com': {'default': {'ak': 'legacy-access', 'sk': 'legacy-secret'}}}
    assert await settings.import_legacy() == {'rules': 'absent', 'registries': 'absent'}
    assert memory.settings == {}
    assert await settings.import_legacy(json.dumps(legacy_rules), legacy_registry) == {'rules': 'imported', 'registries': 'imported'}
    assert memory.settings[settings.RULES_KEY][1]['users'] == ['alice']
    assert 'legacy-secret' not in json.dumps(memory.settings)
    memory.settings[settings.REGISTRIES_KEY] = []
    assert await settings.import_legacy(legacy_rules, legacy_registry) == {'rules': 'present', 'registries': 'present'}
    assert await settings.registry_credentials() == {}


def test_legacy_full_day_and_equal_times_preserve_previous_behavior():
    imported = settings._legacy_rules({
        'default': {'isOperationAllowed': False},
        'full-day': {'isOperationAllowed': True, 'allowedOperationPeriod': '00:00-24:00', 'user': ['alice']},
        'disabled': {'isOperationAllowed': True, 'allowedOperationPeriod': '08:00-08:00', 'user': ['alice']},
    })
    by_env = {rule['env']: rule for rule in imported}
    assert by_env['full-day']['enabled'] is True and by_env['full-day']['end_time'] == '24:00'
    assert by_env['disabled']['enabled'] is False
    assert by_env['disabled']['start_time'] != by_env['disabled']['end_time']


@pytest.mark.asyncio
async def test_invalid_legacy_import_never_exposes_input_or_writes(memory, monkeypatch):
    insert = AsyncMock()
    monkeypatch.setattr(settings, '_insert_absent', insert)
    assert await settings.import_legacy('bad-private-secret-json', {'bad/path': {}}) == {
        'rules': 'invalid', 'registries': 'invalid'}
    result = await settings.import_legacy('bad-private-secret-json', {'bad/path': {'default': {'ak': 'a', 'sk': 'b'}}})
    assert result == {'rules': 'invalid', 'registries': 'invalid'}
    assert await settings.import_legacy({'prod': {'isOperationAllowed': True, 'user': ['alice']}}, None) == {
        'rules': 'invalid', 'registries': 'absent'}
    insert.assert_not_awaited()
    assert not memory.settings


@pytest.mark.asyncio
async def test_import_sql_never_updates_existing_database_row(monkeypatch):
    query = AsyncMock(return_value=None)
    monkeypatch.setattr(settings.db, 'pg_fetchrow', query)
    assert not await settings._insert_absent(settings.RULES_KEY, rules())
    sql, key, value, author = query.call_args.args
    assert 'ON CONFLICT (key) DO NOTHING' in sql
    assert key == settings.RULES_KEY and json.loads(value) == rules() and author == 'legacy-import'


@pytest.mark.asyncio
async def test_cli_closes_database_and_reports_only_status(monkeypatch):
    initialize, close = AsyncMock(), AsyncMock()
    monkeypatch.setattr(settings.db, 'init_pool', initialize)
    monkeypatch.setattr(settings.db, 'close_pool', close)
    migrate = AsyncMock(return_value={'rules': 'imported', 'registries': 'present'})
    monkeypatch.setattr(settings, 'import_legacy', migrate)
    assert await settings._import_cli({'update_image': {'default': {}}, 'registry_secret': None}) == {
        'success': True, 'rules': 'imported', 'registries': 'present'}
    initialize.assert_awaited_once()
    close.assert_awaited_once()
    migrate.assert_awaited_once_with({'default': {}}, None)


@pytest.mark.asyncio
async def test_registry_read_errors_are_sanitized_and_key_rotation_requires_resave(memory, monkeypatch):
    memory.settings[settings.REGISTRIES_KEY] = settings.normalize_registries([registry()])
    monkeypatch.setenv('AI_ENCRYPTION_KEY', '34' * 32)
    with pytest.raises(ValueError, match='重新保存') as exc:
        await settings.registry_credentials()
    assert 'private-secret-key' not in str(exc.value)
    monkeypatch.setattr(memory, 'get_setting', AsyncMock(side_effect=RuntimeError('private-database-details')))
    with pytest.raises(ValueError, match='暂不可用') as exc:
        await settings.registry_credentials()
    assert 'private-database-details' not in str(exc.value)


@pytest.mark.asyncio
async def test_cli_closes_database_when_initialization_fails(monkeypatch):
    monkeypatch.setattr(settings.db, 'init_pool', AsyncMock(side_effect=RuntimeError('database unavailable')))
    close = AsyncMock()
    monkeypatch.setattr(settings.db, 'close_pool', close)
    with pytest.raises(RuntimeError, match='database unavailable'):
        await settings._import_cli({})
    close.assert_awaited_once()


def test_cli_does_not_echo_invalid_secret_input(monkeypatch, capsys):
    monkeypatch.setattr(settings.sys, 'stdin', io.StringIO('private-secret-input'))
    assert settings.main(['import-legacy']) == 1
    output = capsys.readouterr().out
    assert json.loads(output)['success'] is False and 'private-secret-input' not in output
