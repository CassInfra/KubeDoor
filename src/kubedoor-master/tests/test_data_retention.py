"""func_manager/data_retention.py 的单元测试。数据库调用全部 mock 掉,不需要真实 PG。

运行:
    cd src/kubedoor-master
    pip install pytest
    python -m pytest tests -q
"""
import asyncio
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import general_settings  # noqa: E402
from func_manager import data_retention  # noqa: E402


class FakeDB:
    """按预设序列返回 'DELETE n',并记录每次调用的 SQL 和参数。"""

    def __init__(self, deleted_seq):
        self.deleted_seq = list(deleted_seq)
        self.calls = []

    async def pg_execute(self, sql, *args, timeout=None):
        self.calls.append((sql, args))
        return f"DELETE {self.deleted_seq.pop(0)}"


@pytest.fixture(autouse=True)
def no_pause(monkeypatch):
    monkeypatch.setattr(data_retention, 'BATCH_PAUSE', 0)


def test_purge_table_deletes_in_batches_until_partial_batch(monkeypatch):
    fake = FakeDB([3, 3, 1])
    monkeypatch.setattr(data_retention, 'db', fake)

    total = asyncio.run(data_retention.purge_table('k8s_events', 'lasttimestamp', 90, batch_size=3))

    assert total == 7
    assert len(fake.calls) == 3
    sql, args = fake.calls[0]
    assert args == (90,)
    assert 'DELETE FROM k8s_events' in sql
    assert 'lasttimestamp < now() - make_interval(days => $1)' in sql
    assert 'LIMIT 3' in sql


def test_purge_table_runs_once_when_nothing_expired(monkeypatch):
    fake = FakeDB([0])
    monkeypatch.setattr(data_retention, 'db', fake)

    total = asyncio.run(data_retention.purge_table('k8s_resources', 'date', 365))

    assert total == 0
    assert len(fake.calls) == 1


def test_purge_expired_skips_disabled_tables_and_survives_errors(monkeypatch):
    called = []

    async def fake_purge(table, ts_column, days, batch_size=data_retention.BATCH_SIZE):
        called.append((table, days))
        if table == 'broken':
            raise RuntimeError('connection lost')
        return 5

    settings = {'days_broken': 30, 'days_disabled': 0, 'days_ok': 10}
    monkeypatch.setattr(data_retention, 'purge_table', fake_purge)
    monkeypatch.setattr(general_settings, 'value', lambda name: settings[name])
    monkeypatch.setattr(data_retention, 'RETENTION_RULES', [
        ('broken', 'ts', 'days_broken'),
        ('disabled', 'ts', 'days_disabled'),
        ('ok', 'ts', 'days_ok'),
    ])

    asyncio.run(data_retention.purge_expired())

    # 保留天数为 0 的表不清理;前一张表报错不影响后面的表
    assert called == [('broken', 30), ('ok', 10)]

    # 保留天数每一轮重新取通用配置:页面上改了,下一轮就按新值清理
    settings.update(days_disabled=7, days_ok=0)
    called.clear()
    asyncio.run(data_retention.purge_expired())
    assert called == [('broken', 30), ('disabled', 7)]


def test_default_retention_rules():
    rules = {table: (column, setting) for table, column, setting in data_retention.RETENTION_RULES}
    assert rules == {
        'k8s_events': ('lasttimestamp', 'retention_events_days'),
        'k8s_pod_alert_days': ('start_time', 'retention_alerts_days'),
        'k8s_resources': ('date', 'retention_resources_days'),
        'audit_logs': ('ts', 'retention_audit_days'),
    }
