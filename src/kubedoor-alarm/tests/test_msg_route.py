"""/msg/kubedoor: 通过 master /api/internal/notify 转发干净结构化告警,屏蔽过滤,204/200/502 语义与旧路径拒绝。

kubedoor-alarm.py 用假的 PostgreSQL 连接池加载(见 conftest.alarm_app),屏蔽规则来自 pool.silence_rows。
"""
import json
import logging
import urllib.parse
from datetime import UTC, datetime, timedelta

import pytest
import requests

from conftest import MASTER_SETTINGS, FakeResponse, master_ok

SILENCE_RESTART = (7, [{'key': 'alertname', 'op': '=', 'value': 'K8S_Pod_Restart'}], None, None)


def make_alert(alertname='K8S_Pod_Restart', status='firing', env='prod-k8s', annotations=None, extra_labels=None):
    starts_at = (datetime.now(UTC) - timedelta(seconds=5)).strftime('%Y-%m-%dT%H:%M:%SZ')
    labels = {'alertname': alertname, 'k8s': env, 'namespace': 'default', 'pod': 'web-6d4cf56db6-abcde', **(extra_labels or {})}
    return {
        'status': status,
        'labels': labels,
        'annotations': {'description': f'{alertname} 描述', **(annotations or {})},
        'startsAt': starts_at,
        'endsAt': '0001-01-01T00:00:00Z',
        'fingerprint': f'fp-{alertname}-{status}',
    }


def make_payload(alerts, common=None, group=None):
    if common is None:
        common = {'k8s': 'prod-k8s'}
    return {
        'receiver': 'kubedoor-alarm', 'status': 'firing', 'alerts': alerts,
        'groupLabels': group or {}, 'commonLabels': common, 'commonAnnotations': {},
        'externalURL': 'http://alertmanager:9093', 'version': '4',
    }


def post(alarm_app, payload, path='/msg/kubedoor'):
    return alarm_app.app.test_client().post(path, json=payload)


def notify_ok(matched=1):
    return FakeResponse(200, {'success': True, 'data': {'matched': matched, 'queued': matched, 'trace_id': 'tr-1'}})


def test_forwards_clean_structured_items_without_html_tags(alarm_app, fake_http, caplog):
    fake_http.notify = [notify_ok(matched=2)]
    alert = make_alert(extra_labels={'severity': 'warning'})
    with caplog.at_level(logging.DEBUG):
        resp = post(alarm_app, make_payload([alert], group={'k8s': 'prod-k8s', 'alertname': 'K8S_Pod_Restart'}))
    assert resp.status_code == 200
    assert fake_http.gets == []
    [sent] = fake_http.posts
    assert sent['url'] == 'http://kubedoor-master/api/internal/notify'
    assert sent['headers'] == {'X-Kubedoor-Token': 'internal-secret', 'Content-Type': 'application/json'}
    assert sent['allow_redirects'] is False

    body = sent['json']
    assert body['env'] == 'prod-k8s'
    assert body['category'] == 'metric_alert'
    assert body['severity'] == 'warning'
    assert body['at'] == ''
    url = alarm_app.build_silence_url(alert['labels'])
    assert url.startswith('https://kubedoor.example.com/#/alarm/silence?prefill=')
    assert body['links'] == [{'label': '屏蔽: K8S_Pod_Restart,小于1分钟', 'url': url}]
    assert len(body['items']) == 1
    item = body['items'][0]
    assert item['status'] == 'firing'
    assert item['summary'] == 'K8S_Pod_Restart,小于1分钟'
    assert item['description'] == 'K8S_Pod_Restart 描述'
    assert item['silence_url'] == url
    assert item['severity'] == 'warning'
    assert item['alertname'] == 'K8S_Pod_Restart'
    assert item['labels'] == alert['labels']
    assert '<font' not in json.dumps(body, ensure_ascii=False)
    assert 'internal-secret' not in caplog.text


def test_resolved_alert_item_has_empty_silence_url_and_resolved_severity(alarm_app, fake_http):
    fake_http.notify = [notify_ok(1)]
    resp = post(alarm_app, make_payload([make_alert(status='resolved')]))
    assert resp.status_code == 200
    body = fake_http.posts[0]['json']
    assert body['severity'] == 'resolved'
    assert body['links'] == []
    assert body['items'][0]['status'] == 'resolved'
    assert body['items'][0]['summary'] == 'K8S_Pod_Restart,小于1分钟'
    assert body['items'][0]['silence_url'] == ''


@pytest.mark.parametrize('starts_at, duration', [
    (lambda: (datetime.now(UTC) - timedelta(minutes=10)).strftime('%Y-%m-%dT%H:%M:%S.%fZ'), '已持续10.0分钟'),
    # +08:00 的写法按本机时间算(容器里是 Asia/Shanghai)
    (lambda: (datetime.now() - timedelta(hours=2)).strftime('%Y-%m-%dT%H:%M:%S+08:00'), '已持续2.0小时'),
])
def test_duration_in_item_summary(alarm_app, fake_http, starts_at, duration):
    alert = make_alert()
    alert['startsAt'] = starts_at()
    assert post(alarm_app, make_payload([alert])).status_code == 200
    assert fake_http.posts[0]['json']['items'][0]['summary'] == f'K8S_Pod_Restart,{duration}'
    assert fake_http.posts[0]['json']['items'][0]['duration_text'] == duration


def test_cluster_from_group_labels_when_common_labels_lack_it(alarm_app, fake_http):
    payload = make_payload([make_alert(env='test-k8s')], common={'alertname': 'K8S_Pod_Restart'},
                           group={'k8s': 'test-k8s'})
    assert post(alarm_app, payload).status_code == 200
    assert fake_http.posts[0]['json']['env'] == 'test-k8s'


def test_without_cluster_label_passes_empty_env(alarm_app, fake_http):
    payload = make_payload([make_alert(), make_alert(env='test-k8s')], common={'alertname': 'K8S_Pod_Restart'})
    assert post(alarm_app, payload).status_code == 200
    assert fake_http.posts[0]['json']['env'] == ''
    assert len(fake_http.posts) == 1
    assert len(fake_http.posts[0]['json']['items']) == 2


def test_annotation_at_is_forwarded_to_master(alarm_app, fake_http):
    assert post(alarm_app, make_payload([make_alert(annotations={'at': 'lisi'})])).status_code == 200
    assert fake_http.posts[0]['json']['at'] == 'lisi'


def test_unmatched_route_returns_204(alarm_app, fake_http, caplog):
    fake_http.notify = [notify_ok(matched=0)]
    with caplog.at_level(logging.INFO):
        resp = post(alarm_app, make_payload([make_alert()]))
    assert resp.status_code == 204
    assert len(fake_http.posts) == 1
    assert '没有可用的消息通知配置' in caplog.text and '集群 prod-k8s' in caplog.text


@pytest.mark.parametrize('failure', [
    requests.ConnectionError('connection refused'),
    requests.Timeout('timed out'),
    FakeResponse(503, {'success': False, 'message': 'not ready'}),
    FakeResponse(500, text='Internal Error'),
], ids=['connect', 'timeout', 'http503', 'http500'])
def test_master_notify_error_returns_502_for_alertmanager_retry(alarm_app, fake_http, failure, caplog):
    fake_http.notify = [failure]
    with caplog.at_level(logging.ERROR):
        resp = post(alarm_app, make_payload([make_alert()]))
    assert resp.status_code == 502
    assert 'internal-secret' not in caplog.text


def test_all_silenced_sends_nothing_and_counts_hits(alarm_app, fake_http):
    alarm_app.pool.silence_rows = [SILENCE_RESTART]
    resp = post(alarm_app, make_payload([make_alert(), make_alert(status='resolved')]))
    assert resp.status_code == 204
    assert fake_http.gets == [] and fake_http.posts == []
    assert any(sql.startswith('UPDATE alert_silences SET match_count') for sql, _ in alarm_app.pool.executed)


def test_partially_silenced_sends_the_rest(alarm_app, fake_http):
    alarm_app.pool.silence_rows = [SILENCE_RESTART]
    resp = post(alarm_app, make_payload([make_alert(), make_alert(alertname='K8S_Pod_OOM')]))
    assert resp.status_code == 200
    items = fake_http.posts[0]['json']['items']
    assert [it['alertname'] for it in items] == ['K8S_Pod_OOM']


def test_legacy_path_is_rejected_without_logging_token(alarm_app, fake_http, caplog):
    with caplog.at_level(logging.DEBUG):
        resp = post(alarm_app, make_payload([make_alert()]), path='/msg/wecom=legacy-secret')
    assert resp.status_code == 404
    assert fake_http.gets == [] and fake_http.posts == []
    assert '/msg/kubedoor' in caplog.text and 'legacy-secret' not in caplog.text


def test_invalid_payload_returns_400(alarm_app, fake_http):
    client = alarm_app.app.test_client()
    assert client.post('/msg/kubedoor', data='not json', content_type='application/json').status_code == 400
    assert client.post('/msg/kubedoor', json={'status': 'firing'}).status_code == 400
    assert fake_http.gets == [] and fake_http.posts == []


def test_legacy_notification_env_vars_are_gone(alarm_app):
    import utils

    for name in ('MSG_TOKEN', 'MSG_TYPE', 'DEFAULT_AT'):
        assert not hasattr(utils, name) and not hasattr(alarm_app, name)


def test_general_settings_are_not_copied_at_import(alarm_app):
    """迁到「通用配置」的几项处理告警时才取,不再是 import 时拷贝的模块常量"""
    import silence
    import utils

    for name in ('PROM_K8S_TAG_KEY', 'KUBEDOOR_EXTURL', 'ALERTMANAGER_EXTURL', 'SILENCE_CACHE_TTL'):
        assert not hasattr(utils, name) and not hasattr(alarm_app, name) and not hasattr(silence, name)


def test_silence_link(alarm_app, alarm_settings):
    labels = make_alert()['labels']
    url = alarm_app.build_silence_url(labels)
    prefill = json.loads(urllib.parse.unquote(url.split('prefill=', 1)[1]))
    assert prefill == {'alertname': 'K8S_Pod_Restart', 'env': 'prod-k8s', 'namespace': 'default',
                       'pod': 'web-6d4cf56db6-abcde'}
    alarm_settings['kubedoor_exturl'] = ''
    assert alarm_app.build_silence_url(labels) == (
        'http://alertmanager.example.com/#/alerts?silenced=false&inhibited=false&active=true&filter='
        + urllib.parse.quote('{alertname="K8S_Pod_Restart"}'))


def test_settings_changes_apply_without_restart(alarm_app, fake_http, alarm_settings):
    """通用配置里改了集群标签名和 KubeDoor 地址,下一批告警就按新的处理"""
    alarm_settings.update(prom_k8s_tag_key='cluster', kubedoor_exturl='https://kd.example.com/')
    alert = make_alert()
    alert['labels']['cluster'] = 'prod-b'
    assert post(alarm_app, make_payload([alert], common={'cluster': 'prod-b'})).status_code == 200
    body = fake_http.posts[0]['json']
    assert body['env'] == 'prod-b'
    url = alarm_app.build_silence_url(alert['labels'])
    assert url.startswith('https://kd.example.com/#/alarm/silence?prefill=')
    assert json.loads(urllib.parse.unquote(url.split('prefill=', 1)[1]))['env'] == 'prod-b'
    assert body['items'][0]['silence_url'] == url


def test_settings_come_from_master_and_are_cached(alarm_app, fake_http, settings_resolver, clock):
    """真实的取配置流程:向 master 取通用配置并缓存 15 秒,同时直接 POST /api/internal/notify"""
    changed = {**MASTER_SETTINGS, 'kubedoor_exturl': 'https://kd-new.example.com'}
    fake_http.master = [master_ok(MASTER_SETTINGS), master_ok(changed)]
    payload = make_payload([make_alert()])
    assert post(alarm_app, payload).status_code == 200
    assert post(alarm_app, payload).status_code == 200
    assert fake_http.gets == [{
        'url': 'http://kubedoor-master/api/internal/settings',
        'params': None,
        'headers': {'X-Kubedoor-Token': 'internal-secret'},
        'timeout': 5,
    }]
    first = fake_http.posts[0]['json']['items'][0]['silence_url']
    second = fake_http.posts[1]['json']['items'][0]['silence_url']
    assert first.startswith('https://kubedoor.example.com/#/alarm/silence?prefill=') and first == second
    clock.now += 15
    assert post(alarm_app, payload).status_code == 200
    assert len(fake_http.gets) == 2
    assert fake_http.posts[2]['json']['items'][0]['silence_url'].startswith(
        'https://kd-new.example.com/#/alarm/silence?prefill='
    )
