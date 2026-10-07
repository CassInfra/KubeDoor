"""指定Pod 校验：新增必须填写(0 有效、-1 不行)；-1 只允许用于采集过高峰期数据或有 AI推荐的服务。"""
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


def res_data(pod_count_manual, **extra):
    return dict(env='prod', namespace='infra', deployment='demo-service', pod_count_manual=pod_count_manual,
                limit_mem_mb=2048, limit_cpu_m=1000, request_cpu_m=100, request_mem_mb=256, **extra)


def patch_db(monkeypatch, has_fallback=None):
    calls = {'execute': [], 'fetchval': []}

    async def execute(sql, *args):
        calls['execute'].append((sql, args))
        return 'OK'

    async def fetchval(sql, *args):
        calls['fetchval'].append((sql, args))
        return has_fallback

    monkeypatch.setattr(db_api, 'pg_execute', execute)
    monkeypatch.setattr(db_api, 'pg_fetchval', fetchval)
    monkeypatch.setattr(db_api.utils, 'invalidate_admis_cache', lambda: None)
    return calls


def rejected(handler, data):
    with pytest.raises(web.HTTPBadRequest) as error:
        asyncio.run(handler(Request(data)))
    assert error.value.content_type == 'application/json'
    return json.loads(error.value.text)['message']


def test_add_rejects_unset_pod_count_before_database_write(monkeypatch):
    calls = patch_db(monkeypatch)
    assert '不能为 -1' in rejected(db_api.res_add, res_data(-1))
    assert calls['execute'] == []


def test_add_accepts_zero_as_publish_without_pods(monkeypatch):
    calls = patch_db(monkeypatch)
    response = asyncio.run(db_api.res_add(Request(res_data('0'))))
    assert json.loads(response.text)['success'] is True
    sql, args = calls['execute'][0]
    assert sql.startswith('INSERT INTO k8s_res_control')
    assert args[3] == 0


@pytest.mark.parametrize('invalid', [-2, 'abc', None, '1.5'])
def test_invalid_pod_count_is_rejected_on_every_write(monkeypatch, invalid):
    calls = patch_db(monkeypatch, has_fallback=True)
    rejected(db_api.res_add, res_data(invalid))
    rejected(db_api.res_edit, res_data(invalid))
    rejected(db_api.res_update_pod_count,
             dict(env='prod', namespace='infra', deployment_name='demo-service', pod_count_manual=invalid))
    assert calls['execute'] == []


def test_edit_rejects_minus_one_for_service_without_peak_data(monkeypatch):
    calls = patch_db(monkeypatch, has_fallback=False)
    assert '还没有采集到高峰期数据' in rejected(db_api.res_edit, res_data(-1))
    assert calls['execute'] == []
    assert calls['fetchval'][0][1] == ('prod', 'infra', 'demo-service')


def test_edit_allows_minus_one_for_collected_service(monkeypatch):
    calls = patch_db(monkeypatch, has_fallback=True)
    response = asyncio.run(db_api.res_edit(Request(res_data(-1))))
    assert json.loads(response.text)['success'] is True
    assert calls['execute'][0][1][2] == -1


def test_edit_with_explicit_count_skips_fallback_lookup(monkeypatch):
    calls = patch_db(monkeypatch, has_fallback=False)
    asyncio.run(db_api.res_edit(Request(res_data(0))))
    assert calls['fetchval'] == []
    assert calls['execute'][0][1][2] == 0


def test_update_pod_count_guards_minus_one_the_same_way(monkeypatch):
    data = dict(env='prod', namespace='infra', deployment_name='demo-service', pod_count_manual=-1)
    calls = patch_db(monkeypatch, has_fallback=False)
    rejected(db_api.res_update_pod_count, data)
    assert calls['execute'] == []

    calls = patch_db(monkeypatch, has_fallback=True)
    asyncio.run(db_api.res_update_pod_count(Request(data)))
    assert calls['execute'][0][1] == (-1, 'prod', 'infra', 'demo-service')
