"""JVM 管控接口：精确字节、字段隔离、旧表单兼容，不连接真实数据库。"""
import asyncio
import json
import pathlib
import sys

import pytest
from aiohttp import web

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from func_manager import db_api


class Request:
    def __init__(self, data):
        self.data = data

    async def json(self):
        return self.data


def edit_data(**extra):
    return dict(env='prod', namespace='infra', deployment='java-service',
                limit_mem_mb=2048, limit_cpu_m=1000, pod_count_manual=3, **extra)


def save(monkeypatch, data, handler=db_api.res_edit):
    calls = []
    invalidations = []

    async def execute(sql, *args):
        calls.append((sql, args))
        return 'UPDATE 1'

    monkeypatch.setattr(db_api, 'pg_execute', execute)
    monkeypatch.setattr(db_api.utils, 'invalidate_admis_cache', lambda: invalidations.append(True))
    response = asyncio.run(handler(Request(data)))
    assert json.loads(response.text)['success'] is True
    assert invalidations == [True]
    return calls[0]


def test_old_edit_does_not_overwrite_jvm_values(monkeypatch):
    sql, args = save(monkeypatch, edit_data())
    assert not any(field in sql for field in db_api._JVM_FIELDS)
    assert args == (2048, 1000, 3, 'prod', 'infra', 'java-service')
    assert 'env=$4 AND namespace=$5 AND deployment=$6' in sql


def test_partial_edit_keeps_xss_exact_and_other_jvm_fields_unchanged(monkeypatch):
    sql, args = save(monkeypatch, edit_data(jvm_xss_bytes=512 * 1024))
    assert 'jvm_xss_bytes=$4' in sql
    assert args == (2048, 1000, 3, 524288, 'prod', 'infra', 'java-service')
    assert 'env=$5 AND namespace=$6 AND deployment=$7' in sql
    assert 'jvm_xms_bytes' not in sql
    assert 'jvm_xmx_bytes' not in sql


def test_zero_and_explicit_null_are_distinct(monkeypatch):
    sql, args = save(monkeypatch, edit_data(jvm_xms_bytes=0, jvm_xss_bytes=None))
    assert args[3:5] == (0, None)
    assert 'jvm_xms_bytes=$4' in sql and 'jvm_xss_bytes=$5' in sql


def test_add_keeps_field_and_placeholder_order(monkeypatch):
    data = edit_data(request_cpu_m=100, request_mem_mb=256,
                     jvm_xmx_bytes=1024 * 1024 * 1024, jvm_xss_bytes=512 * 1024)
    sql, args = save(monkeypatch, data, db_api.res_add)
    assert 'request_mem_mb,jvm_xmx_bytes,jvm_xss_bytes)' in sql
    assert 'VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)' in sql
    assert args[-2:] == (1073741824, 524288)


@pytest.mark.parametrize('invalid', [True, -1, 0.5, '512k', 2 ** 53])
def test_invalid_byte_values_fail_before_database_write(monkeypatch, invalid):
    async def unexpected_execute(*args):
        pytest.fail('invalid JVM fields must not reach PostgreSQL')

    monkeypatch.setattr(db_api, 'pg_execute', unexpected_execute)
    with pytest.raises(web.HTTPBadRequest):
        asyncio.run(db_api.res_edit(Request(edit_data(jvm_xss_bytes=invalid))))


def test_heap_bounds_reject_xms_larger_than_xmx():
    with pytest.raises(web.HTTPBadRequest) as error:
        db_api._jvm_updates({'jvm_xms_bytes': 2 * 1024 ** 3,
                            'jvm_xmx_bytes': 1024 ** 3})
    assert 'Xms' in error.value.text


def test_jvm_list_preserves_null_and_fractional_megabyte_as_exact_bytes():
    row = [None] * len(db_api.RES_CONTROL_COLUMNS)
    row[db_api.RES_CONTROL_COLUMNS.index('jvm_xss_bytes')] = 524288
    response = db_api._table_response([row], db_api.RES_CONTROL_COLUMNS)
    payload = json.loads(response.text)
    indexes = {column['name']: i for i, column in enumerate(payload['meta'])}
    assert payload['data'][0][indexes['jvm_xms_bytes']] is None
    assert payload['data'][0][indexes['jvm_xss_bytes']] == 524288
