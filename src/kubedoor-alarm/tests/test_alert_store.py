"""入库路径 /alert/store、自定义告警 /api/custom_alert 和安装向导健康接口(这次没改逻辑,补上回归测试)。"""
import hashlib

import pytest

SILENCE_RESTART = (7, [{'key': 'alertname', 'op': '=', 'value': 'K8S_Pod_Restart'}], None, None)


def store_alert(status='firing', **labels):
    return {
        'status': status,
        'labels': {'alertname': 'K8S_Pod_Restart', 'k8s': 'prod-k8s', 'namespace': 'default',
                   'pod': 'web-6d4cf56db6-abcde', 'container': 'web', 'severity': 'Warning',
                   'alertgroup': 'pod', **labels},
        'annotations': {'description': '摘要\n- 重启了 3 次'},
        'startsAt': '2026-11-07T04:00:00.123Z',
        'endsAt': '2026-11-07T04:30:00Z',
        'fingerprint': 'prom-fp',
    }


def fingerprint(env='prod-k8s', namespace='default', pod='web-6d4cf56db6-abcde', alertname='K8S_Pod_Restart'):
    return hashlib.md5((env + namespace + pod + alertname).encode('utf-8')).hexdigest()


def store(alarm_app, *alerts):
    return alarm_app.app.test_client().post('/alert/store', json={'alerts': list(alerts)})


def test_store_inserts_new_firing_alert(alarm_app):
    assert store(alarm_app, store_alert()).status_code == 200
    [(sql, params)] = alarm_app.pool.writes()
    assert sql.startswith('INSERT INTO k8s_pod_alert_days')
    assert params == (
        fingerprint(), True, '2026-11-07 12:00:00', 0, 'Warning', 'pod', 'K8S_Pod_Restart', 'prod-k8s',
        'default', 'web', 'web-6d4cf56db6-abcde', '重启了 3 次', False, None,
    )


def test_store_counts_existing_firing_alert(alarm_app):
    alarm_app.pool.alert_rows = [(1,)]
    assert store(alarm_app, store_alert()).status_code == 200
    [(sql, params)] = alarm_app.pool.writes()
    assert sql.startswith('UPDATE k8s_pod_alert_days SET count_firing = count_firing + 1')
    assert params[1:] == ('重启了 3 次', False, None, '2026-11-07', fingerprint())


def test_store_marks_resolved(alarm_app):
    alarm_app.pool.alert_rows = [(1,)]
    assert store(alarm_app, store_alert(status='resolved')).status_code == 200
    [(sql, params)] = alarm_app.pool.writes()
    assert "alert_status = 'resolved'" in sql
    assert params == ('2026-11-07 12:30:00', '重启了 3 次', False, None, '2026-11-07', fingerprint())


def test_store_skips_resolved_without_record_or_send_resolved(alarm_app):
    assert store(alarm_app, store_alert(status='resolved')).status_code == 200
    assert store(alarm_app, store_alert(status='resolved', send_resolved='false')).status_code == 200
    assert alarm_app.pool.writes() == []


def test_store_takes_cluster_from_label_in_general_settings(alarm_app, alarm_settings):
    """集群名取自通用配置里的集群指标标签 key,改了马上按新的入库(指纹也跟着变)"""
    alarm_settings['prom_k8s_tag_key'] = 'cluster'
    assert store(alarm_app, store_alert(cluster='prod-b')).status_code == 200
    [(_, params)] = alarm_app.pool.writes()
    assert params[0] == fingerprint(env='prod-b') and params[7] == 'prod-b'


def test_store_keeps_silenced_alert_with_mark_and_does_not_count_hit(alarm_app):
    alarm_app.pool.silence_rows = [SILENCE_RESTART]
    assert store(alarm_app, store_alert()).status_code == 200
    [(sql, params)] = alarm_app.pool.writes()
    assert params[-2:] == (True, 7)
    # 入库路径不累加命中次数(只在通知路径计数)
    assert alarm_app.silence.get_engine()._pending == {}


def test_store_rejects_invalid_payload(alarm_app):
    assert alarm_app.app.test_client().post('/alert/store', json={'status': 'firing'}).status_code == 400


def custom(**overrides):
    data = {
        'start_time': '2026-11-07 12:00:00', 'end_time': '2026-11-07 12:05:00', 'severity': 'Critical',
        'alert_group': 'custom', 'alert_name': 'K8S_Pod_Restart', 'env': 'prod-k8s', 'namespace': 'default',
        'pod': 'web-6d4cf56db6-abcde', 'description': '自定义告警', 'send_resolved': True, 'alert_status': 'firing',
    }
    data.update(overrides)
    return data


def post_custom(alarm_app, data):
    return alarm_app.app.test_client().post('/api/custom_alert', json=data)


def test_custom_alert_inserts_with_container_from_pod(alarm_app):
    resp = post_custom(alarm_app, custom())
    assert resp.status_code == 200 and resp.get_json()['silenced'] is False
    [(sql, params)] = alarm_app.pool.writes()
    assert sql.startswith('INSERT INTO k8s_pod_alert_days')
    assert params[0] == fingerprint() and params[9] == 'web'


def test_custom_alert_silenced(alarm_app):
    alarm_app.pool.silence_rows = [SILENCE_RESTART]
    resp = post_custom(alarm_app, custom(container='api'))
    assert resp.status_code == 200 and resp.get_json()['silenced'] is True
    [(_, params)] = alarm_app.pool.writes()
    assert params[9] == 'api' and params[-2:] == (True, 7)


def test_custom_alert_resolved_without_record_is_rejected(alarm_app):
    resp = post_custom(alarm_app, custom(alert_status='resolved'))
    assert resp.status_code == 400 and '未找到对应告警记录' in resp.get_json()['message']


@pytest.mark.parametrize('data, message', [
    ({k: v for k, v in custom().items() if k != 'pod'}, '缺少必需字段: pod'),
    (custom(severity='High'), 'severity必须是以下值之一'),
    (custom(alert_status='pending'), 'alert_status必须是以下值之一'),
    (custom(start_time='2026/11/07 12:00'), '时间格式必须为'),
])
def test_custom_alert_validation(alarm_app, data, message):
    resp = post_custom(alarm_app, data)
    assert resp.status_code == 400 and message in resp.get_json()['message']
    assert alarm_app.pool.writes() == []


def test_custom_alert_rejects_empty_body(alarm_app):
    resp = alarm_app.app.test_client().post('/api/custom_alert', json={})
    assert resp.status_code == 400


@pytest.mark.parametrize('pod, container', [
    ('web-6d4cf56db6-abcde', 'web'),
    ('order-service-7f9b8c6d5-x2k4p', 'order-service'),
    ('job-abcde', 'job'),
    ('my-app-1', 'my'),
    ('single', ''),
])
def test_extract_container_from_pod(alarm_app, pod, container):
    assert alarm_app.extract_container_from_pod(pod) == container


def test_setup_health(alarm_app, monkeypatch):
    client = alarm_app.app.test_client()
    monkeypatch.setenv('KUBEDOOR_SETUP_ID', 'setup-1')
    assert client.get('/internal/setup-health').status_code == 403
    assert client.get('/internal/setup-health', headers={'X-Kubedoor-Token': 'wrong'}).status_code == 403
    resp = client.get('/internal/setup-health', headers={'X-Kubedoor-Token': 'internal-secret'})
    assert resp.status_code == 200 and resp.get_json() == {'ok': True, 'installation_id': 'setup-1'}
    alarm_app.pool.fail = True
    resp = client.get('/internal/setup-health', headers={'X-Kubedoor-Token': 'internal-secret'})
    assert resp.status_code == 503 and resp.get_json()['ok'] is False


def test_fake_pool_discards_uncommitted_dml_and_counts_rollback():
    from conftest import FakePool

    pool = FakePool()
    pool.silence_rows = [SILENCE_RESTART]
    pool.alert_rows = [(1,)]

    # 未调 commit() 也未用 conn.transaction() 的写入,退出 connection() 时模拟 putconn() 隐式回滚
    with pool.connection() as conn:
        conn.execute('INSERT INTO k8s_pod_alert_days (fingerprint) VALUES (%s)', ('fp-1',))
        conn.execute('UPDATE k8s_pod_alert_days SET count_firing = 2 WHERE fingerprint = %s', ('fp-1',))
        conn.execute('DELETE FROM k8s_pod_alert_days WHERE fingerprint = %s', ('fp-1',))

    assert pool.writes() == []
    assert pool.executed == []
    assert pool.commits == 0
    assert pool.rollbacks == 1

    # 只读 SELECT 不触发回滚,并正常返回配置好的结果集和锁查询空结果
    with pool.connection() as conn:
        assert conn.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', ('2026-11-07:fp-1',)).fetchall() == []
        assert conn.execute('SELECT id FROM alert_silences').fetchall() == [SILENCE_RESTART]
        assert conn.execute('SELECT 1 FROM k8s_pod_alert_days').fetchone() == (1,)

    assert pool.rollbacks == 1

    # 显式 commit() 与 conn.transaction() 正常退出均会提交
    with pool.connection() as conn:
        conn.execute('INSERT INTO k8s_pod_alert_days (fingerprint) VALUES (%s)', ('fp-commit',))
        conn.commit()
    with pool.connection() as conn, conn.transaction():
        conn.execute('UPDATE k8s_pod_alert_days SET count_firing = 3 WHERE fingerprint = %s', ('fp-tx',))

    assert len(pool.writes()) == 2
    assert pool.commits == 2
    assert pool.rollbacks == 1

    # conn.transaction() 发生异常时丢弃待提交写入并传播异常
    with pytest.raises(RuntimeError, match='boom'):
        with pool.connection() as conn, conn.transaction():
            conn.execute('INSERT INTO k8s_pod_alert_days (fingerprint) VALUES (%s)', ('fp-abort',))
            raise RuntimeError('boom')

    assert len(pool.writes()) == 2
    assert pool.commits == 2
    assert pool.rollbacks == 2


def _alert_data(fp=None, status='firing'):
    return {
        'fingerprint': fp or fingerprint(),
        'start_time': '2026-11-07 12:00:00',
        'end_time': '2026-11-07 12:30:00',
        'severity': 'Warning',
        'alert_group': 'pod',
        'alert_name': 'K8S_Pod_Restart',
        'env': 'prod-k8s',
        'namespace': 'default',
        'container': 'web',
        'pod': 'web-6d4cf56db6-abcde',
        'description': '重启了 3 次',
        'alert_status': status,
    }


def test_firing_insert_and_update_acquire_advisory_lock_and_commit(alarm_app):
    fp = fingerprint()
    assert alarm_app.handle_firing_alert(_alert_data(fp), True, None) == (True, '')
    assert alarm_app.pool.rollbacks == 0
    assert alarm_app.pool.commits == 1
    assert [sql.split()[0:2] for sql, _ in alarm_app.pool.executed] == [
        ['SELECT', 'pg_advisory_xact_lock(hashtext(%s))'],
        ['SELECT', '1'],
        ['INSERT', 'INTO'],
    ]
    assert alarm_app.pool.executed[0] == (
        'SELECT pg_advisory_xact_lock(hashtext(%s))',
        (f'2026-11-07:{fp}',),
    )

    alarm_app.pool.executed.clear()
    alarm_app.pool.alert_rows = [(1,)]
    assert alarm_app.handle_firing_alert(_alert_data(fp), True, 7) == (True, '')
    assert alarm_app.pool.rollbacks == 0
    assert alarm_app.pool.commits == 2
    assert [sql.split()[0:2] for sql, _ in alarm_app.pool.executed] == [
        ['SELECT', 'pg_advisory_xact_lock(hashtext(%s))'],
        ['SELECT', '1'],
        ['UPDATE', 'k8s_pod_alert_days'],
    ]
    assert alarm_app.pool.executed[0] == (
        'SELECT pg_advisory_xact_lock(hashtext(%s))',
        (f'2026-11-07:{fp}',),
    )


def test_resolved_alert_commits_with_zero_rollbacks(alarm_app):
    fp = fingerprint()
    assert alarm_app.handle_resolved_alert(_alert_data(fp), True, None)[0] is False
    assert alarm_app.pool.writes() == []
    assert alarm_app.pool.rollbacks == 0
    assert alarm_app.pool.commits == 1
    assert [sql.split()[0:2] for sql, _ in alarm_app.pool.executed] == [
        ['SELECT', 'pg_advisory_xact_lock(hashtext(%s))'],
        ['SELECT', '1'],
    ]
    assert alarm_app.pool.executed[0] == (
        'SELECT pg_advisory_xact_lock(hashtext(%s))',
        (f'2026-11-07:{fp}',),
    )

    alarm_app.pool.executed.clear()
    alarm_app.pool.alert_rows = [(1,)]
    assert alarm_app.handle_resolved_alert(_alert_data(fp), True, None) == (True, '')
    assert len(alarm_app.pool.writes()) == 1
    assert alarm_app.pool.writes()[0][0].startswith('UPDATE k8s_pod_alert_days SET')
    assert alarm_app.pool.rollbacks == 0
    assert alarm_app.pool.commits == 2
    assert [sql.split()[0:2] for sql, _ in alarm_app.pool.executed] == [
        ['SELECT', 'pg_advisory_xact_lock(hashtext(%s))'],
        ['SELECT', '1'],
        ['UPDATE', 'k8s_pod_alert_days'],
    ]
    assert alarm_app.pool.executed[0] == (
        'SELECT pg_advisory_xact_lock(hashtext(%s))',
        (f'2026-11-07:{fp}',),
    )


def test_firing_and_resolved_rollback_on_mid_transaction_exception(alarm_app, monkeypatch):
    def fail_info(*args, **kwargs):
        raise RuntimeError('mid-transaction failure')

    monkeypatch.setattr(alarm_app.logging, 'info', fail_info)

    # firing INSERT 在提交前异常:已执行的 INSERT 必须回滚
    with pytest.raises(RuntimeError, match='mid-transaction failure'):
        alarm_app.handle_firing_alert(_alert_data(), True, None)
    assert alarm_app.pool.writes() == []
    assert alarm_app.pool.commits == 0
    assert alarm_app.pool.rollbacks == 1

    # firing UPDATE 在提交前异常:已执行的 UPDATE 必须回滚
    alarm_app.pool.alert_rows = [(1,)]
    with pytest.raises(RuntimeError, match='mid-transaction failure'):
        alarm_app.handle_firing_alert(_alert_data(), True, None)
    assert alarm_app.pool.writes() == []
    assert alarm_app.pool.commits == 0
    assert alarm_app.pool.rollbacks == 2

    # resolved UPDATE 在提交前异常:已执行的 UPDATE 必须回滚
    with pytest.raises(RuntimeError, match='mid-transaction failure'):
        alarm_app.handle_resolved_alert(_alert_data(), True, None)
    assert alarm_app.pool.writes() == []
    assert alarm_app.pool.commits == 0
    assert alarm_app.pool.rollbacks == 3


def test_store_isolates_failed_alert_transaction_from_subsequent_alert(alarm_app):
    calls = {'inserts': 0}

    def fail_first_insert(sql, params):
        if sql.startswith('INSERT INTO k8s_pod_alert_days'):
            calls['inserts'] += 1
            if calls['inserts'] == 1:
                raise RuntimeError('unique violation or timeout')

    alarm_app.pool.on_execute = fail_first_insert
    resp = store(alarm_app, store_alert(pod='pod-fail'), store_alert(pod='pod-ok'))
    assert resp.status_code == 200
    assert alarm_app.pool.rollbacks == 1
    assert alarm_app.pool.commits == 1
    assert len(alarm_app.pool.writes()) == 1
    assert alarm_app.pool.writes()[0][1][10] == 'pod-ok'
