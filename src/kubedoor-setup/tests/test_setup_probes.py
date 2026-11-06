from unittest.mock import AsyncMock
import base64
import json

import asyncpg
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from kubedoor_setup.errors import SetupError
from kubedoor_setup.probes import check_metrics, check_postgres, create_database, pg_connect_options, reserve_database

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


def missing_database(*, maintenance=None, create=None):
    """目标库不存在：连目标库报 3D000，维护库上执行 CREATE DATABASE 之后目标库才能连上。"""
    exists = {'value': False}
    admin, target = Connection(), Connection()

    async def execute(sql, *args, **kwargs):
        if create is not None:
            await create(exists)
        else:
            exists['value'] = True
    admin.execute = AsyncMock(side_effect=execute)

    async def connect(**options):
        if options['database'] == PG['database']:
            if not exists['value']:
                raise asyncpg.InvalidCatalogNameError('database "kubedoor" does not exist')
            return target
        result = (maintenance or {}).get(options['database'], admin)
        if isinstance(result, Exception):
            raise result
        return result
    return AsyncMock(side_effect=connect), admin, target


def connected_databases(connect):
    return [call.kwargs['database'] for call in connect.await_args_list]


@pytest.mark.asyncio
async def test_missing_database_is_created_with_same_account_then_checked():
    connect, admin, target = missing_database()
    result = await check_postgres(PG, 'mine', connect=connect)
    assert result['status'] == 'success'
    assert '数据库 kubedoor 不存在，已用账号 kubedoor 自动创建' in result['message']
    assert connected_databases(connect) == ['kubedoor', 'postgres', 'kubedoor']
    assert connect.await_args_list[1].kwargs == {**pg_connect_options(PG), 'database': 'postgres'}
    admin.execute.assert_awaited_once_with('CREATE DATABASE "kubedoor"', timeout=30)
    admin.close.assert_awaited_once()
    target.tx.rollback.assert_awaited_once()
    target.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('error', [asyncpg.InvalidAuthorizationSpecificationError('no pg_hba.conf entry'),
                                   asyncpg.InvalidCatalogNameError('database "postgres" does not exist'),
                                   asyncpg.InsufficientPrivilegeError('permission denied for database postgres')])
async def test_missing_database_falls_back_to_template1(error):
    connect, admin, _ = missing_database(maintenance={'postgres': error})
    result = await check_postgres(PG, 'mine', connect=connect)
    assert '已用账号 kubedoor 自动创建' in result['message']
    assert connected_databases(connect) == ['kubedoor', 'postgres', 'template1', 'kubedoor']
    admin.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_database_without_createdb_tells_admin_to_create_it():
    async def denied(_):
        raise asyncpg.InsufficientPrivilegeError('permission denied to create database never-expose-this')
    connect, admin, _ = missing_database(create=denied)
    with pytest.raises(SetupError) as caught:
        await check_postgres(PG, 'mine', connect=connect)
    message = caught.value.message
    assert caught.value.status == 422
    assert '数据库 kubedoor 不存在，账号 kubedoor 没有建库权限' in message
    assert 'CREATE DATABASE "kubedoor" OWNER "kubedoor";' in message
    assert 'never-expose-this' not in message
    admin.close.assert_awaited_once()
    assert connected_databases(connect) == ['kubedoor', 'postgres']


@pytest.mark.asyncio
async def test_missing_database_without_maintenance_access_tells_admin_to_create_it():
    connect, admin, _ = missing_database(maintenance={
        'postgres': asyncpg.InvalidAuthorizationSpecificationError('no pg_hba.conf entry never-expose-this'),
        'template1': asyncpg.InvalidAuthorizationSpecificationError('no pg_hba.conf entry never-expose-this')})
    with pytest.raises(SetupError) as caught:
        await check_postgres(PG, 'mine', connect=connect)
    assert '无法连接 postgres 维护库' in caught.value.message
    assert 'CREATE DATABASE "kubedoor" OWNER "kubedoor";' in caught.value.message
    assert 'never-expose-this' not in caught.value.message
    admin.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_database_created_concurrently_is_used_without_claiming_creation():
    async def duplicate(exists):
        exists['value'] = True
        raise asyncpg.DuplicateDatabaseError('database "kubedoor" already exists')
    connect, _, target = missing_database(create=duplicate)
    result = await check_postgres(PG, 'mine', connect=connect)
    assert result['message'] == '连接、账号认证和 schema 建表读写权限检查通过'
    target.tx.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_database_failure_reports_sqlstate_only():
    async def busy(_):
        raise asyncpg.ObjectInUseError('source database "template1" is being accessed by never-expose-this')
    connect, _, _ = missing_database(create=busy)
    with pytest.raises(SetupError) as caught:
        await check_postgres(PG, 'mine', connect=connect)
    assert '自动创建失败（SQLSTATE 55006）' in caught.value.message
    assert 'never-expose-this' not in caught.value.message


@pytest.mark.asyncio
async def test_create_database_quotes_names():
    config = {**PG, 'database': 'kube"door', 'user': 'ops"user'}
    admin = Connection()
    admin.execute = AsyncMock(side_effect=asyncpg.InsufficientPrivilegeError('denied'))
    with pytest.raises(SetupError) as caught:
        await create_database(config, connect=AsyncMock(return_value=admin))
    admin.execute.assert_awaited_once_with('CREATE DATABASE "kube""door"', timeout=30)
    assert 'CREATE DATABASE "kube""door" OWNER "ops""user";' in caught.value.message


@pytest.mark.asyncio
async def test_maintenance_database_target_skips_itself():
    config = {**PG, 'database': 'postgres'}
    connect = AsyncMock(side_effect=asyncpg.InvalidCatalogNameError('missing'))
    with pytest.raises(SetupError):
        await create_database(config, connect=connect)
    assert connected_databases(connect) == ['template1']


def metrics_app(values, seen):
    """模拟 VictoriaMetrics：查询接口分块返回，标签值接口返回给定的值。"""
    async def query(request):
        seen.append((request.query['query'], request.headers.get('Authorization')))
        body = {'status': 'success', 'data': {'resultType': 'vector', 'result': [{'metric': {}, 'value': [100, '1']}]}}
        response = web.StreamResponse(headers={'Content-Type': 'application/json'})
        await response.prepare(request)
        raw = json.dumps(body).encode()
        for chunk in (raw[:15], raw[15:40], raw[40:]):
            await response.write(chunk)
        await response.write_eof()
        return response

    async def label_values(request):
        assert int(request.query['start']) > 0 and request.query['limit'] == '1000'
        seen.append(('label:' + request.match_info['name'], request.headers.get('Authorization')))
        return web.json_response({'status': 'success', 'data': values})
    app = web.Application()
    app.router.add_get('/select/0/prometheus/api/v1/query', query)
    app.router.add_get('/select/0/prometheus/api/v1/label/{name}/values', label_values)
    return app


@pytest.mark.asyncio
async def test_metrics_uses_read_url_credentials_and_new_store_without_label_is_a_warning():
    seen = []
    async with TestServer(metrics_app([], seen)) as server:
        read_url = str(server.make_url('/select/0/prometheus')).replace('http://', 'http://monit:p%40ss@')
        result = await check_metrics({'read_url': read_url, 'write_url': '', 'tag_key': 'cluster'})
    auth = 'Basic ' + base64.b64encode(b'monit:p@ss').decode()
    assert seen == [('vector(1)', auth), ('label:cluster', auth)]
    assert result['status'] == 'warning'
    assert result['metrics_found'] is False
    assert '没有查到带 cluster 标签的数据' in result['message']
    assert '已有完整监控应能查到' in result['message'] and '新部署的时序库查不到是正常的' in result['message']


@pytest.mark.asyncio
@pytest.mark.parametrize('count,expected', [(2, '标签值：k8s-0、k8s-1'), (7, 'k8s-0、k8s-1、k8s-2、k8s-3、k8s-4 等 7 个'),
                                            (1000, '等 1000 个以上')])
async def test_metrics_lists_cluster_label_values_found(count, expected):
    seen = []
    async with TestServer(metrics_app([f'k8s-{i}' for i in range(count)], seen)) as server:
        result = await check_metrics({'read_url': str(server.make_url('/select/0/prometheus')), 'tag_key': 'origin_prometheus'})
    assert [query for query, _ in seen] == ['vector(1)', 'label:origin_prometheus']
    assert all(header is None for _, header in seen)
    assert result['status'] == 'success' and result['metrics_found'] is True
    assert expected in result['message']


@pytest.mark.asyncio
async def test_metrics_label_values_must_be_a_string_list():
    async with TestServer(metrics_app({'unexpected': 'object'}, [])) as server:
        with pytest.raises(SetupError) as error:
            await check_metrics({'read_url': str(server.make_url('/select/0/prometheus')), 'tag_key': 'cluster'})
    assert '标签值接口' in error.value.message


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [302, 401, 403, 500])
async def test_metrics_error_does_not_follow_redirect_or_return_secret_response(status):
    async def query(request):
        return web.Response(status=status, text='secret-upstream-error', headers={'Location': 'http://should-not-follow.invalid'})
    app = web.Application()
    app.router.add_get('/api/v1/query', query)
    async with TestServer(app) as server:
        with pytest.raises(SetupError) as error:
            await check_metrics({'read_url': str(server.make_url('')).rstrip('/'), 'tag_key': 'cluster'})
    assert 'secret-upstream-error' not in error.value.message
    assert str(status) in error.value.message or '认证失败' in error.value.message
