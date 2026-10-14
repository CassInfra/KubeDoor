"""资源管控保存时跨环境补写 request CPU/内存的接口测试。"""
import asyncio
import json
import pathlib
import sys

import pytest
from aiohttp import web

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from func_manager import db_api  # noqa: E402


class Request:
    def __init__(self, data):
        self.data = data

    async def json(self):
        return self.data


def resource_data(**extra):
    data = dict(
        env='prod', namespace='infra', deployment='java-service',
        limit_mem_mb=2048, limit_cpu_m=1000, pod_count_manual=3,
        request_cpu_m=100, request_mem_mb=256,
    )
    data.update(extra)
    return data


def patch_sync(monkeypatch, *, target_exists=True):
    calls = {'fetchval': [], 'transaction': [], 'execute': []}

    async def fetchval(sql, *args):
        calls['fetchval'].append((sql, args))
        # The first lookup validates k8s_agent_status, the second finds the target row.
        return 1 if len(calls['fetchval']) == 1 or target_exists else None

    async def transaction(statements):
        calls['transaction'].append(statements)
        return ['UPDATE 1', f"UPDATE {1 if target_exists else 0}"]

    async def execute(sql, *args):
        calls['execute'].append((sql, args))
        return 'UPDATE 1'

    monkeypatch.setattr(db_api, 'pg_fetchval', fetchval)
    monkeypatch.setattr(db_api, 'pg_execute_transaction', transaction)
    monkeypatch.setattr(db_api, 'pg_execute', execute)
    monkeypatch.setattr(db_api.utils, 'invalidate_admis_cache', lambda: None)
    return calls


def test_edit_syncs_all_control_values_and_only_fills_empty_requests(monkeypatch):
    calls = patch_sync(monkeypatch)
    response = asyncio.run(db_api.res_edit(Request(resource_data(
        sync_env='staging', max_surge='35%', max_unavailable='30%',
        jvm_xms_bytes=128 * 1024 * 1024, jvm_xmx_bytes=256 * 1024 * 1024,
        jvm_xss_bytes=512 * 1024, jvm_max_metaspace_bytes=200 * 1024 * 1024,
    ))))
    payload = json.loads(response.text)

    assert payload == {
        'success': True, 'msg': '更新成功', 'sync_env': 'staging',
        'sync_updated': 1, 'sync_target_found': True,
    }
    assert calls['execute'] == []
    assert len(calls['transaction']) == 1
    source_sql, source_args = calls['transaction'][0][0]
    target_sql, target_args = calls['transaction'][0][1]
    assert 'UPDATE k8s_res_control SET' in source_sql
    assert source_args[-3:] == ('prod', 'infra', 'java-service')
    for field in (
        'pod_count_manual', 'limit_cpu_m', 'limit_mem_mb',
        'max_unavailable', 'max_surge', 'jvm_xms_bytes', 'jvm_xmx_bytes',
        'jvm_xss_bytes', 'jvm_max_metaspace_bytes',
    ):
        assert f'{field}=$' in target_sql
    assert 'WHEN $10::integer IS NOT NULL' in target_sql
    assert 'THEN $10::integer ELSE request_cpu_m END' in target_sql
    assert 'WHEN $11::integer IS NOT NULL' in target_sql
    assert 'THEN $11::integer ELSE request_mem_mb END' in target_sql
    assert 'request_cpu_m IS NULL OR request_cpu_m=-1' in target_sql
    assert 'request_mem_mb IS NULL OR request_mem_mb=-1' in target_sql
    assert target_args == (
        3, 1000, 2048, '30%', '35%',
        128 * 1024 * 1024, 256 * 1024 * 1024, 512 * 1024, 200 * 1024 * 1024,
        100, 256, 'staging', 'infra', 'java-service',
    )


def test_edit_sync_target_missing_does_not_insert(monkeypatch):
    calls = patch_sync(monkeypatch, target_exists=False)
    response = asyncio.run(db_api.res_edit(Request(resource_data(sync_env='staging'))))
    payload = json.loads(response.text)
    assert payload['sync_target_found'] is False
    assert payload['sync_updated'] == 0
    assert len(calls['transaction']) == 1
    assert calls['transaction'][0][1][0].lstrip().startswith('UPDATE k8s_res_control')


def test_sync_rejects_unknown_or_same_environment_before_writing(monkeypatch):
    calls = patch_sync(monkeypatch, target_exists=True)
    calls['fetchval'].clear()

    async def unknown(sql, *args):
        calls['fetchval'].append((sql, args))
        return None

    monkeypatch.setattr(db_api, 'pg_fetchval', unknown)
    with pytest.raises(web.HTTPBadRequest):
        asyncio.run(db_api.res_edit(Request(resource_data(sync_env='missing'))))
    assert calls['transaction'] == []
    assert calls['execute'] == []

    with pytest.raises(web.HTTPBadRequest):
        asyncio.run(db_api.res_edit(Request(resource_data(sync_env='prod'))))
    assert calls['transaction'] == []
    assert calls['execute'] == []


def test_add_accepts_sync_target_and_uses_same_transaction(monkeypatch):
    calls = patch_sync(monkeypatch)
    response = asyncio.run(db_api.res_add(Request(resource_data(
        sync_env='staging', max_surge='35%', max_unavailable='30%',
        jvm_xms_bytes=128 * 1024 * 1024,
    ))))
    payload = json.loads(response.text)
    assert payload['success'] is True
    assert payload['sync_env'] == 'staging'
    assert len(calls['transaction']) == 1
    assert calls['transaction'][0][0][0].startswith('INSERT INTO k8s_res_control')
    target_sql, target_args = calls['transaction'][0][1]
    assert 'pod_count_manual=$1' in target_sql
    assert 'limit_cpu_m=$2' in target_sql
    assert 'limit_mem_mb=$3' in target_sql
    assert 'max_unavailable=$4' in target_sql
    assert 'max_surge=$5' in target_sql
    assert 'jvm_xms_bytes=$6' in target_sql
    assert target_args == (
        3, 1000, 2048, '30%', '35%', 128 * 1024 * 1024,
        100, 256, 'staging', 'infra', 'java-service',
    )

