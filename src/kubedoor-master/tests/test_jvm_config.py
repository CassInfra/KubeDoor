"""JVM 批量补采测试，不需要真实 Kubernetes / PostgreSQL。"""

import asyncio
import pathlib
import sys
from unittest.mock import AsyncMock, Mock

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import jvm_config  # noqa: E402


def config(deployment='web', **values):
    return {'namespace': 'ns1', 'deployment': deployment, **values}


def test_normalize_accepts_partial_config_and_preserves_zero():
    rows, invalid = jvm_config.normalize_configs([
        config(jvm_xms_bytes=0, jvm_xmx_bytes=1024**3),
        config('unparsed'),
        config('safe-max', jvm_xss_bytes=jvm_config.MAX_JVM_BYTES),
    ])
    assert rows == [
        ('ns1', 'web', 0, 1024**3, None, None),
        ('ns1', 'safe-max', None, None, jvm_config.MAX_JVM_BYTES, None),
    ]
    assert invalid == 0


@pytest.mark.parametrize('value', [True, False, 1.5, '1024', -1, 1 << 53])
def test_normalize_rejects_unsafe_or_noninteger_bytes(value):
    rows, invalid = jvm_config.normalize_configs([config(jvm_xmx_bytes=value)])
    assert rows == []
    assert invalid == 1


def test_normalize_rejects_bad_keys_records_and_duplicate_services():
    rows, invalid = jvm_config.normalize_configs([
        config(jvm_xmx_bytes=1024),
        config(jvm_xmx_bytes=2048),
        {'namespace': 'ns1', 'deployment': '', 'jvm_xms_bytes': 1},
        {'namespace': None, 'deployment': 'bad', 'jvm_xms_bytes': 1},
        None,
    ])
    assert rows == [('ns1', 'web', None, 1024, None, None)]
    assert invalid == 4


def test_fill_missing_configs_uses_one_update_for_600_services(monkeypatch):
    execute = AsyncMock(return_value='UPDATE 599')
    monkeypatch.setattr(jvm_config.db, 'pg_execute', execute)
    configs, _ = jvm_config.normalize_configs([
        config(f'service-{i}', jvm_xms_bytes=1024, jvm_xmx_bytes=2048)
        for i in range(600)
    ])

    assert asyncio.run(jvm_config.fill_missing_configs('prod', configs)) == 599
    execute.assert_awaited_once()
    sql, env, namespaces, deployments, xms, xmx, xss, metaspace = execute.call_args.args
    assert env == 'prod'
    assert namespaces == ['ns1'] * 600
    assert deployments == [f'service-{i}' for i in range(600)]
    assert xms == [1024] * 600
    assert xmx == [2048] * 600
    assert xss == metaspace == [None] * 600
    assert 'INSERT' not in sql.upper()
    assert 'c.env = $1 AND c.namespace = v.namespace AND c.deployment = v.deployment' in sql
    for field in jvm_config.JVM_FIELDS:
        assert f'{field} = COALESCE(c.{field}, v.{field})' in sql
        assert f'(c.{field} IS NULL AND v.{field} IS NOT NULL)' in sql


def test_collect_reports_statistics_and_invalid_records_without_overwriting(monkeypatch):
    execute = AsyncMock(return_value='UPDATE 1')
    agent = AsyncMock(return_value={'success': True, 'data': [
        config(jvm_xmx_bytes=2048),
        config('bad', jvm_xms_bytes=True),
    ]})
    invalidate = Mock()
    monkeypatch.setattr(jvm_config.db, 'pg_execute', execute)

    stats = asyncio.run(jvm_config.collect_current_configs('prod', agent, invalidate))

    assert stats['success'] is True
    assert {key: stats[key] for key in ('received', 'eligible', 'updated', 'invalid')} == {
        'received': 2, 'eligible': 1, 'updated': 1, 'invalid': 1,
    }
    assert '1' in stats['warning']
    agent.assert_awaited_once_with('prod', '/api/agent/jvm/configs', timeout=60)
    execute.assert_awaited_once()
    invalidate.assert_called_once_with()


@pytest.mark.parametrize('response', [
    {'success': False, 'error': '404: Not Found'},
    {'error': '旧版本 agent 不支持接口'},
    {'success': True, 'data': {}},
    None,
])
def test_unsupported_or_invalid_response_returns_warning_without_db_write(monkeypatch, response):
    execute = AsyncMock()
    invalidate = Mock()
    monkeypatch.setattr(jvm_config.db, 'pg_execute', execute)
    stats = asyncio.run(jvm_config.collect_current_configs('prod', AsyncMock(return_value=response), invalidate))

    assert stats['success'] is False
    assert '下一轮重试' in stats['warning']
    execute.assert_not_awaited()
    invalidate.assert_not_called()


def test_offline_agent_can_retry_on_next_round(monkeypatch):
    execute = AsyncMock(return_value='UPDATE 1')
    agent = AsyncMock(side_effect=[RuntimeError('目标客户端 prod 不在线'), {
        'success': True, 'data': [config(jvm_xss_bytes=512 * 1024)],
    }])
    invalidate = Mock()
    monkeypatch.setattr(jvm_config.db, 'pg_execute', execute)

    async def collect_twice():
        return (
            await jvm_config.collect_current_configs('prod', agent, invalidate),
            await jvm_config.collect_current_configs('prod', agent, invalidate),
        )

    failed, retried = asyncio.run(collect_twice())
    assert failed['success'] is False
    assert '不在线' in failed['warning']
    assert retried['success'] is True
    assert retried['updated'] == 1
    assert agent.await_count == 2
    execute.assert_awaited_once()
    invalidate.assert_called_once()


def test_db_failure_does_not_invalidate_or_hide_warning(monkeypatch):
    execute = AsyncMock(side_effect=RuntimeError('database unavailable'))
    invalidate = Mock()
    monkeypatch.setattr(jvm_config.db, 'pg_execute', execute)
    agent = AsyncMock(return_value={'success': True, 'data': [config(jvm_xms_bytes=1024)]})

    stats = asyncio.run(jvm_config.collect_current_configs('prod', agent, invalidate))

    assert stats['success'] is False
    assert 'database unavailable' in stats['warning']
    invalidate.assert_not_called()


def test_empty_collection_skips_database_and_cache_invalidation(monkeypatch):
    execute = AsyncMock()
    invalidate = Mock()
    monkeypatch.setattr(jvm_config.db, 'pg_execute', execute)

    stats = asyncio.run(jvm_config.collect_current_configs(
        'prod', AsyncMock(return_value={'success': True, 'data': []}), invalidate,
    ))

    assert stats == {'success': True, 'received': 0, 'eligible': 0, 'updated': 0, 'invalid': 0}
    execute.assert_not_awaited()
    invalidate.assert_not_called()
