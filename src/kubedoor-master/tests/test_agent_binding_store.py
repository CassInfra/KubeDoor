"""安装账号绑定的数据库访问和缓存失效，不使用实际数据库。"""

from unittest.mock import AsyncMock

import pytest

from auth.store import AuthStore


@pytest.mark.asyncio
async def test_binding_invalidates_negative_cache(monkeypatch):
    import auth.store as module

    query = AsyncMock(side_effect=[None, {'env': 'prod'}, {'env': 'prod'}])
    monkeypatch.setattr(module.db, 'pg_fetchrow', query)
    store = AuthStore()
    assert await store.agent_env(3) is None
    assert await store.agent_env(3) is None
    assert query.await_count == 1
    await store.bind_agent(3, 'prod')
    assert await store.agent_env(3) == 'prod'
    assert query.await_count == 3
    assert await store.agent_env(3) == 'prod'
    assert query.await_count == 3
    store.invalidate_all()
    query.return_value = {'env': 'prod'}
    query.side_effect = None
    assert await store.agent_env(3) == 'prod'
    assert query.await_count == 4


@pytest.mark.asyncio
async def test_binding_conflicts_do_not_discard_cached_value(monkeypatch):
    import auth.store as module

    query = AsyncMock(side_effect=[{'env': 'prod'}, None])
    monkeypatch.setattr(module.db, 'pg_fetchrow', query)
    store = AuthStore()
    assert await store.agent_env(3) == 'prod'
    with pytest.raises(ValueError, match='已经绑定其它集群'):
        await store.bind_agent(3, 'staging')
    assert await store.agent_env(3) == 'prod'
    assert query.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('env', ['', None, 'prod/other', 'with spaces', 'a' * 201])
async def test_binding_rejects_invalid_cluster_identifiers(monkeypatch, env):
    import auth.store as module

    query = AsyncMock()
    monkeypatch.setattr(module.db, 'pg_fetchrow', query)
    with pytest.raises(ValueError, match='Agent 集群标识'):
        await AuthStore().bind_agent(3, env)
    query.assert_not_awaited()
