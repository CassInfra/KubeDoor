"""安装任务的真实 PostgreSQL 验证。

设置 KUBEDOOR_INSTALL_TEST_PG_DSN 指向有建库权限的测试 PostgreSQL 才运行。
每个用例都新建随机名称数据库，执行生产 db.sql，结束后仅删除该新建库；
不会清空或迁移 DSN 原来指向的数据库。
"""

import asyncio
import copy
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import uuid

import asyncpg
import pytest
import pytest_asyncio

import db
from agent_install.kubernetes import InstallError
from agent_install.repository import Repository
from auth.store import AuthStore


PG_DSN = os.environ.get('KUBEDOOR_INSTALL_TEST_PG_DSN')
pytestmark = pytest.mark.skipif(not PG_DSN, reason='设置 KUBEDOOR_INSTALL_TEST_PG_DSN 后运行独立测试数据库验证')
DB_SQL = Path(__file__).resolve().parent.parent / 'db.sql'


@pytest_asyncio.fixture
async def pg(monkeypatch):
    database = 'kubedoor_install_test_' + uuid.uuid4().hex
    admin = await asyncpg.connect(PG_DSN, command_timeout=30)
    pool = None
    created = False
    try:
        await admin.execute(f'CREATE DATABASE "{database}"')
        created = True
        pool = await asyncpg.create_pool(PG_DSN, database=database, min_size=1, max_size=3, command_timeout=30)
        monkeypatch.setattr(db, '_pool', pool)
        monkeypatch.setattr(db, '_loop', asyncio.get_running_loop())
        await pool.execute(DB_SQL.read_text(encoding='utf-8'))
        yield pool
    finally:
        if pool:
            await pool.close()
        if created:
            # database 只来自本夹具生成的 UUID，绝不使用外部数据库名做删除目标。
            await admin.execute(f'DROP DATABASE "{database}"')
        await admin.close()


def task(*, env='prod', target_uid='target-prod', status='ready', expired=False):
    return {
        'id': str(uuid.uuid4()), 'status': status, 'revision': 0,
        'env': env, 'target_uid': target_uid,
        'payload': {'context': '生产集群', 'options': {'env': env}, 'checks': [], 'created_by': 'test-admin'},
        'encrypted': 'encrypted-material-test',
        'expires_at': datetime.now(timezone.utc) + timedelta(hours=-1 if expired else 1),
    }


@pytest.mark.asyncio
async def test_install_schema_migration_is_repeatable(pg):
    await pg.execute(DB_SQL.read_text(encoding='utf-8'))
    names = await pg.fetch("SELECT tablename FROM pg_tables WHERE schemaname='public' "
                           "AND tablename IN ('agent_installations','auth_agent_bindings')")
    assert {row['tablename'] for row in names} == {'agent_installations', 'auth_agent_bindings'}


@pytest.mark.asyncio
async def test_repository_round_trip_uuid_json_and_revision_cas(pg):
    repository = Repository()
    original = task()
    await repository.create(original)
    left = await repository.get(original['id'])
    stale = copy.deepcopy(left)
    assert left['id'] == original['id'] and isinstance(left['id'], str)
    assert left['payload'] == original['payload']
    left['payload']['checks'].append({'name': '权限', 'status': 'success'})
    await repository.save(left)
    assert left['revision'] == 1
    stale['payload']['context'] = '不得覆盖'
    with pytest.raises(InstallError, match='安装任务已更新') as error:
        await repository.save(stale)
    assert error.value.status == 409
    final = await repository.get(original['id'])
    assert final['payload']['context'] == '生产集群'
    assert final['payload']['checks'] == [{'name': '权限', 'status': 'success'}]
    assert final['revision'] == 1
    assert [item['id'] for item in await repository.list()] == [original['id']]


@pytest.mark.asyncio
@pytest.mark.parametrize('other_env,other_uid', [('prod', 'other-target'), ('other-env', 'target-prod')])
async def test_running_tasks_hold_unique_env_and_target_locks(pg, other_env, other_uid):
    repository = Repository()
    first = task()
    second = task(env=other_env, target_uid=other_uid)
    await repository.create(first)
    await repository.create(second)
    first['status'] = 'running'
    await repository.save(first)
    second['status'] = 'running'
    with pytest.raises(InstallError, match='已有安装任务正在执行') as error:
        await repository.save(second)
    assert error.value.status == 409
    assert (await repository.get(second['id']))['status'] == 'ready'
    first['status'] = 'failed'
    await repository.save(first)
    await repository.save(second)
    assert [item['id'] for item in await repository.unfinished()] == [second['id']]


@pytest.mark.asyncio
async def test_expiration_erases_material_but_keeps_running_tasks(pg):
    repository = Repository()
    expired = task(status='draft', expired=True)
    failed = task(env='failed', target_uid='failed-target', status='failed', expired=True)
    running = task(env='running', target_uid='running-target', status='running', expired=True)
    for item in (expired, failed, running):
        await repository.create(item)
    await repository.expire()
    expired_row = await repository.get(expired['id'])
    failed_row = await repository.get(failed['id'])
    running_row = await repository.get(running['id'])
    assert expired_row['status'] == 'expired' and expired_row['encrypted'] == ''
    assert failed_row['status'] == 'failed' and failed_row['encrypted'] == ''
    assert expired_row['revision'] == 1 and failed_row['revision'] == 1
    assert running_row['status'] == 'running' and running_row['encrypted'] == 'encrypted-material-test'


@pytest.mark.asyncio
async def test_real_agent_binding_is_idempotent_rejects_rebind_and_cascades(pg):
    store = AuthStore()
    agent = await store.create_user('install-agent', 'local', 'agent', password_hash='test-hash')
    human = await store.create_user('human-reader', 'local', 'read', password_hash='test-hash')
    assert await store.agent_env(agent['id']) is None
    await store.bind_agent(agent['id'], 'prod')
    await store.bind_agent(agent['id'], 'prod')
    assert await store.agent_env(agent['id']) == 'prod'
    with pytest.raises(ValueError, match='已经绑定其它集群'):
        await store.bind_agent(agent['id'], 'staging')
    with pytest.raises(ValueError, match='账号不是 Agent'):
        await store.bind_agent(human['id'], 'prod')
    # 轮换凭据可同时存在两个同集群账号。
    next_agent = await store.create_user('next-agent', 'local', 'agent', password_hash='next-hash')
    await store.bind_agent(next_agent['id'], 'prod')
    await store.delete_user(agent['id'])
    assert await store.agent_env(agent['id']) is None
    assert await pg.fetchval('SELECT count(*) FROM auth_agent_bindings') == 1
    assert await store.agent_env(next_agent['id']) == 'prod'
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await pg.execute('INSERT INTO auth_agent_bindings(user_id,env) VALUES($1,$2)', 999999999, 'missing')
