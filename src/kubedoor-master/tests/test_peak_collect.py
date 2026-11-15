"""高峰期数据写入管控表：同一天同一个服务有多行（多个 ReplicaSet、重复写入）时合成一行，同一个集群同时只跑一轮采集。"""
import asyncio
from datetime import datetime

import pytest

import utils

DAY = datetime(2026, 11, 14, 11, 30)


def row(deployment, pod_count, load, namespace='b2c', cpu_pct=40.0, request_cpu=500.0, heap=None):
    """get_list_from_resources 查询的列顺序"""
    return (DAY, 'cassmall-beta-kunlun', namespace, deployment, pod_count, cpu_pct, 55.0, request_cpu, 1024.0,
            2000.0, 4096.0, load, 900.0, heap, None)


def test_replicasets_of_one_deployment_become_one_row_with_the_total_pod_count():
    old = row('deploy-boss-user-bff', 1, 0.2, cpu_pct=30.0, request_cpu=500.0)
    new = row('deploy-boss-user-bff', 3, 0.5, cpu_pct=45.0, request_cpu=800.0)
    other = row('deploy-order', 2, 0.3)
    merged = utils.merge_deployment_rows([old, other, new])
    assert [r[3] for r in merged] == ['deploy-boss-user-bff', 'deploy-order']
    bff = merged[0]
    # Pod 数是两个 ReplicaSet 之和，指标和规格取 Pod 数多的那一份
    assert bff[4] == 4 and bff[5] == 45.0 and bff[7] == 800.0 and bff[11] == 0.5
    assert len(bff) == len(new) and bff[:4] == new[:4] and bff[5:] == new[5:]
    assert merged[1] == other


def test_equal_pod_counts_keep_the_busier_replicaset_and_exact_duplicates_count_once():
    quiet = row('svc', 2, 0.1, cpu_pct=10.0)
    busy = row('svc', 2, 0.9, cpu_pct=90.0)
    merged = utils.merge_deployment_rows([quiet, busy])
    assert merged == [(*busy[:4], 4, *busy[5:])]
    # 同一天被写了两遍：完全相同的行只算一次，Pod 数不翻倍
    assert utils.merge_deployment_rows([busy, busy]) == [busy]
    # 不同命名空间的同名服务是两个服务
    assert len(utils.merge_deployment_rows([row('svc', 1, 0.1), row('svc', 1, 0.1, namespace='b2b')])) == 2


@pytest.mark.asyncio
async def test_rows_written_to_the_control_table_are_unique(monkeypatch):
    rows = [row('deploy-boss-user-bff', 1, 0.2), row('deploy-boss-user-bff', 2, 0.4), row('deploy-order', 1, 0.3)]

    async def fetch(sql, env):
        assert env == 'cassmall-beta-kunlun'
        return rows

    copied = []

    async def copy_records(table, records, columns):
        assert table == 'k8s_res_control'
        copied.extend(records)
        return 'COPY'

    monkeypatch.setattr(utils, 'pg_fetch', fetch)
    monkeypatch.setattr(utils.db, 'pg_copy_records', copy_records)
    monkeypatch.setattr(utils, 'invalidate_admis_cache', lambda: None)
    resources = await utils.get_list_from_resources('cassmall-beta-kunlun')
    assert await utils.init_control_data(resources) is True
    keys = [(r[0], r[1], r[2]) for r in copied]
    assert keys == [('cassmall-beta-kunlun', 'b2c', 'deploy-boss-user-bff'), ('cassmall-beta-kunlun', 'b2c', 'deploy-order')]
    # pod_count_init、pod_count 都是合计的 3
    assert copied[0][3] == 3 and copied[0][4] == 3


@pytest.mark.asyncio
async def test_one_collection_per_cluster_at_a_time():
    lock = utils.peak_collect_lock('cassmall-beta-kunlun')
    assert utils.peak_collect_lock('cassmall-beta-kunlun') is lock
    assert utils.peak_collect_lock('another') is not lock
    async with lock:
        assert utils.peak_collect_lock('cassmall-beta-kunlun').locked()
    assert not lock.locked()
    await asyncio.sleep(0)
