"""silence.py: 屏蔽规则匹配与命中计数批量回写事务回归测试。"""
from datetime import datetime, timezone

import silence
from conftest import FakePool


RULE_RESTART = (7, [{'key': 'alertname', 'op': '=', 'value': 'K8S_Pod_Restart'}], None, None)
RULE_OOM = (8, [{'key': 'alertname', 'op': '=', 'value': 'K8S_Pod_OOM'}], None, None)


def test_fake_pool_catches_uncommitted_silence_update():
    pool = FakePool()
    with pool.connection() as conn:
        conn.execute(
            'UPDATE alert_silences SET match_count = match_count + %s WHERE id = %s',
            (1, 7),
        )
    assert pool.executed == []
    assert pool.commits == 0
    assert pool.rollbacks == 1


def test_flush_counts_commits_updates_with_zero_rollbacks():
    pool = FakePool()
    engine = silence.SilenceEngine(pool, ttl=0)
    t1 = datetime(2026, 11, 7, 12, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 11, 7, 12, 1, 0, tzinfo=timezone.utc)
    engine._pending = {7: [3, t1], 8: [2, t2]}

    engine._flush_counts()

    assert pool.rollbacks == 0
    assert pool.commits == 1
    assert engine._pending == {}
    assert len(pool.executed) == 2
    assert pool.executed[0][0].startswith('UPDATE alert_silences SET match_count')
    assert pool.executed[0][1] == (3, t1, t1, 7)
    assert pool.executed[1][1] == (2, t2, t2, 8)


def test_flush_counts_mid_transaction_error_rolls_back_and_restores_pending():
    pool = FakePool()
    engine = silence.SilenceEngine(pool, ttl=0)
    t1 = datetime(2026, 11, 7, 12, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 11, 7, 12, 1, 0, tzinfo=timezone.utc)
    t3 = datetime(2026, 11, 7, 12, 2, 0, tzinfo=timezone.utc)
    engine._pending = {7: [2, t1], 8: [5, t2]}

    update_calls = {'count': 0}

    def fail_second_update(sql, params):
        if sql.startswith('UPDATE alert_silences'):
            update_calls['count'] += 1
            if update_calls['count'] == 2:
                # 模拟回写期间并发线程又命中了一次规则 #7,同时第 2 条 UPDATE 失败
                engine._pending[7] = [1, t3]
                raise RuntimeError('db write interrupted')

    pool.on_execute = fail_second_update
    engine._flush_counts()

    # 同一事务内的第 1 条 UPDATE 也必须一并回滚,不能部分提交
    assert not any(sql.startswith('UPDATE alert_silences') for sql, _ in pool.executed)
    assert pool.commits == 0
    assert pool.rollbacks == 1
    # 失败后将原增量放回并与并发期间的新增命中合并
    assert engine._pending == {7: [3, t3], 8: [5, t2]}

    # 恢复后重试,全部计数在一个事务内干净提交
    pool.on_execute = None
    engine._flush_counts()

    assert pool.commits == 1
    assert pool.rollbacks == 1
    assert engine._pending == {}
    assert [params for sql, params in pool.executed if sql.startswith('UPDATE alert_silences')] == [
        (3, t3, t3, 7),
        (5, t2, t2, 8),
    ]


def test_flush_counts_when_pool_unavailable_restores_pending():
    pool = FakePool()
    pool.fail = True
    engine = silence.SilenceEngine(pool, ttl=0)
    ts = datetime(2026, 11, 7, 12, 0, 0, tzinfo=timezone.utc)
    engine._pending = {7: [4, ts]}

    engine._flush_counts()

    assert engine._pending == {7: [4, ts]}
    assert pool.commits == 0


def test_match_counts_only_when_requested_and_commits_on_next_refresh():
    pool = FakePool()
    pool.silence_rows = [RULE_RESTART, RULE_OOM]
    engine = silence.SilenceEngine(pool, ttl=0)

    # count_hit=False 不记命中次数
    assert engine.match({'alertname': 'K8S_Pod_Restart'}, count_hit=False) == 7
    assert engine._pending == {}

    # count_hit=True 先记在内存,下次刷新时在事务中批量提交
    assert engine.match({'alertname': 'K8S_Pod_Restart'}, count_hit=True) == 7
    assert 7 in engine._pending and engine._pending[7][0] == 1
    assert not any(sql.startswith('UPDATE alert_silences') for sql, _ in pool.executed)

    assert engine.match({'alertname': 'K8S_Pod_OOM'}, count_hit=True) == 8
    assert pool.rollbacks == 0
    assert pool.commits == 1
    updates = [(sql, params) for sql, params in pool.executed if sql.startswith('UPDATE alert_silences')]
    assert len(updates) == 1 and updates[0][1][0] == 1 and updates[0][1][3] == 7
    assert list(engine._pending.keys()) == [8]
