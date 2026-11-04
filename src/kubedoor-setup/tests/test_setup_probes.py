from unittest.mock import AsyncMock
import json

import asyncpg
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from kubedoor_setup.errors import SetupError
from kubedoor_setup.probes import check_metrics, check_postgres, pg_connect_options, reserve_database

PG = {'host': 'pg.example.com', 'port': 5432, 'user': 'kubedoor', 'password': 'never-expose-this', 'database': 'kubedoor', 'sslmode': 'require'}


class Transaction:
    def __init__(self):
        self.start = AsyncMock()
        self.rollback = AsyncMock()
    async def __aenter__(self):
        return self
    async def __aexit__(self, *_):
        pass


class Connection:
    def __init__(self, *, existing=False, markers=()):
        self.existing, self.markers = existing, markers
        self.tx = Transaction()
        self.execute = AsyncMock()
        self.close = AsyncMock()
    def transaction(self):
        return self.tx
    async def fetchval(self, sql):
        if sql.startswith('SHOW'):
            return '180000'
        if 'auth_users' in sql:
            return self.existing
        if 'to_regclass' in sql:
            return bool(self.markers)
        return 1
    async def fetch(self, sql):
        return [{'id': value} for value in self.markers]


@pytest.mark.asyncio
async def test_pg_uses_real_transaction_and_rolls_back_probe():
    connection = Connection()
    connect = AsyncMock(return_value=connection)
    result = await check_postgres(PG, 'installation-1', connect=connect)
    assert result['status'] == 'success'
    connect.assert_awaited_once_with(**pg_connect_options(PG))
    connection.tx.start.assert_awaited_once()
    connection.tx.rollback.assert_awaited_once()
    connection.close.assert_awaited_once()
    assert any('CREATE TABLE public.' in call.args[0] for call in connection.execute.call_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize('existing,markers', [(True, ()), (True, ('other',)), (False, ('other',)), (True, ('mine', 'other'))])
async def test_existing_database_never_adopted(existing, markers):
    connection = Connection(existing=existing, markers=markers)
    with pytest.raises(SetupError) as error:
        await check_postgres(PG, 'mine', connect=AsyncMock(return_value=connection))
    assert error.value.status == 409
    connection.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_own_database_can_resume_without_reinitialization():
    connection = Connection(existing=True, markers=('mine',))
    await reserve_database(PG, 'mine', connect=AsyncMock(return_value=connection))
    assert connection.execute.await_count == 1
    assert 'pg_advisory_xact_lock' in connection.execute.call_args.args[0]


@pytest.mark.asyncio
async def test_reservation_locks_then_claims_empty_database():
    connection = Connection()
    await reserve_database(PG, 'mine', connect=AsyncMock(return_value=connection))
    sql = [call.args[0] for call in connection.execute.call_args_list]
    assert 'pg_advisory_xact_lock' in sql[0]
    assert 'CREATE TABLE IF NOT EXISTS' in sql[1]
    assert 'INSERT INTO' in sql[2]
    assert connection.execute.call_args.args[1] == 'mine'


@pytest.mark.asyncio
@pytest.mark.parametrize('error,expected', [(asyncpg.InvalidPasswordError('never-expose-this'), '密码验证失败'), (asyncpg.InsufficientPrivilegeError('never-expose-this'), '权限'), (asyncpg.InvalidCatalogNameError('never-expose-this'), '不存在')])
async def test_pg_errors_are_actionable_and_redacted(error, expected):
    with pytest.raises(SetupError) as caught:
        await check_postgres(PG, 'mine', connect=AsyncMock(side_effect=error))
    assert expected in caught.value.message
    assert 'never-expose-this' not in caught.value.message


@pytest.mark.asyncio
async def test_metrics_real_queries_accept_chunked_empty_new_store():
    queries = []
    async def query(request):
        queries.append(request.query['query'])
        assert request.headers['Authorization'].startswith('Basic ')
        body = {'status': 'success', 'data': {'resultType': 'vector', 'result': [
            {'metric': {}, 'value': [100, '1' if request.query['query'] == 'vector(1)' else '0']}
        ]}}
        response = web.StreamResponse(headers={'Content-Type': 'application/json'})
        await response.prepare(request)
        raw = json.dumps(body).encode()
        for chunk in (raw[:15], raw[15:40], raw[40:]):
            await response.write(chunk)
        await response.write_eof()
        return response
    app = web.Application()
    app.router.add_get('/prefix/api/v1/query', query)
    async with TestServer(app) as server:
        result = await check_metrics({'url': str(server.make_url('/prefix')), 'user': 'monit', 'password': 'secret', 'tag_key': 'cluster'})
    assert result['status'] == 'success'
    assert result['metrics_found'] is False
    assert queries == ['vector(1)', 'count(kube_node_info{cluster!=""})']


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [302, 401, 403, 500])
async def test_metrics_error_does_not_follow_redirect_or_return_secret_response(status):
    async def query(request):
        return web.Response(status=status, text='secret-upstream-error', headers={'Location': 'http://should-not-follow.invalid'})
    app = web.Application()
    app.router.add_get('/api/v1/query', query)
    async with TestServer(app) as server:
        with pytest.raises(SetupError) as error:
            await check_metrics({'url': str(server.make_url('')).rstrip('/'), 'user': '', 'password': '', 'tag_key': 'cluster'})
    assert 'secret-upstream-error' not in error.value.message
    assert str(status) in error.value.message or '认证失败' in error.value.message
