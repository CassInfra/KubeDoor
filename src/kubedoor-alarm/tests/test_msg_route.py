"""/msg/kubedoor:按集群取通知配置、at 覆盖、屏蔽、没有配置不发送、旧地址不再支持。

kubedoor-alarm.py 用假的 PostgreSQL 连接池加载(见 conftest.alarm_app),屏蔽规则来自 pool.silence_rows。
"""
import json
import logging
import urllib.parse
from datetime import UTC, datetime, timedelta

import pytest
import requests

from conftest import FakeResponse, master_ok

WECOM = {'type': 'wecom', 'token': 'wecom-key', 'at': 'zhangsan'}
SILENCE_RESTART = (7, [{'key': 'alertname', 'op': '=', 'value': 'K8S_Pod_Restart'}], None, None)


def make_alert(alertname='K8S_Pod_Restart', status='firing', env='prod-k8s', annotations=None):
    starts_at = (datetime.now(UTC) - timedelta(seconds=5)).strftime('%Y-%m-%dT%H:%M:%SZ')
    return {
        'status': status,
        'labels': {'alertname': alertname, 'k8s': env, 'namespace': 'default', 'pod': 'web-6d4cf56db6-abcde'},
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


def wecom_content(fake_http):
    [sent] = fake_http.posts
    return sent['json']['markdown']['content']


def test_sends_to_cluster_channel_with_current_format(alarm_app, fake_http, caplog):
    fake_http.master = [master_ok(WECOM)]
    alert = make_alert()
    with caplog.at_level(logging.DEBUG):
        resp = post(alarm_app, make_payload([alert], group={'k8s': 'prod-k8s', 'alertname': 'K8S_Pod_Restart'}))
    assert resp.status_code == 200
    [get] = fake_http.gets
    assert get['params'] == {'env': 'prod-k8s'}
    assert get['headers'] == {'X-Kubedoor-Token': 'internal-secret'}
    assert fake_http.posts[0]['url'] == 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=wecom-key'
    url = alarm_app.build_silence_url(alert['labels'])
    assert url.startswith('https://kubedoor.example.com/#/alarm/silence?prefill=')
    assert wecom_content(fake_http) == (
        f'### 故障<font color="#ff0000">K8S_Pod_Restart,小于1分钟</font>\n- K8S_Pod_Restart 描述[【屏蔽】]({url})\n\n'
        '<@zhangsan>')
    # token 不进日志
    assert 'wecom-key' not in caplog.text and 'internal-secret' not in caplog.text


def test_resolved_alert_format(alarm_app, fake_http):
    fake_http.master = [master_ok(WECOM)]
    assert post(alarm_app, make_payload([make_alert(status='resolved')])).status_code == 200
    assert wecom_content(fake_http) == (
        '### 恢复<font color="#6aa84f">K8S_Pod_Restart,小于1分钟</font>\n- K8S_Pod_Restart 描述\n\n<@zhangsan>')


@pytest.mark.parametrize('starts_at, duration', [
    (lambda: (datetime.now(UTC) - timedelta(minutes=10)).strftime('%Y-%m-%dT%H:%M:%S.%fZ'), '已持续10.0分钟'),
    # +08:00 的写法按本机时间算(容器里是 Asia/Shanghai)
    (lambda: (datetime.now() - timedelta(hours=2)).strftime('%Y-%m-%dT%H:%M:%S+08:00'), '已持续2.0小时'),
])
def test_duration_in_summary(alarm_app, fake_http, starts_at, duration):
    fake_http.master = [master_ok(WECOM)]
    alert = make_alert()
    alert['startsAt'] = starts_at()
    assert post(alarm_app, make_payload([alert])).status_code == 200
    assert f'K8S_Pod_Restart,{duration}</font>' in wecom_content(fake_http)


def test_cluster_from_group_labels_when_common_labels_lack_it(alarm_app, fake_http):
    fake_http.master = [master_ok(WECOM)]
    payload = make_payload([make_alert(env='test-k8s')], common={'alertname': 'K8S_Pod_Restart'},
                           group={'k8s': 'test-k8s'})
    assert post(alarm_app, payload).status_code == 200
    assert fake_http.gets[0]['params'] == {'env': 'test-k8s'}


def test_without_cluster_label_uses_global_config(alarm_app, fake_http):
    fake_http.master = [master_ok(WECOM)]
    payload = make_payload([make_alert(), make_alert(env='test-k8s')], common={'alertname': 'K8S_Pod_Restart'})
    assert post(alarm_app, payload).status_code == 200
    assert fake_http.gets[0]['params'] == {'env': ''}
    assert len(fake_http.posts) == 1


def test_channel_is_cached_between_batches(alarm_app, fake_http, clock):
    fake_http.master = [master_ok(WECOM), master_ok({**WECOM, 'token': 'new-key'})]
    payload = make_payload([make_alert()])
    assert post(alarm_app, payload).status_code == 200
    assert post(alarm_app, payload).status_code == 200
    assert len(fake_http.gets) == 1
    clock.now += 15
    assert post(alarm_app, payload).status_code == 200
    assert len(fake_http.gets) == 2
    assert [p['url'].rsplit('=', 1)[1] for p in fake_http.posts] == ['wecom-key', 'wecom-key', 'new-key']


def test_master_down_keeps_last_channel(alarm_app, fake_http, clock):
    fake_http.master = [master_ok(WECOM), requests.ConnectionError('connection refused')]
    payload = make_payload([make_alert()])
    assert post(alarm_app, payload).status_code == 200
    clock.now += 20
    assert post(alarm_app, payload).status_code == 200
    assert len(fake_http.gets) == 2
    assert [p['url'].rsplit('=', 1)[1] for p in fake_http.posts] == ['wecom-key', 'wecom-key']


def test_annotation_at_overrides_channel_at(alarm_app, fake_http):
    fake_http.master = [master_ok(WECOM)]
    assert post(alarm_app, make_payload([make_alert(annotations={'at': 'lisi'})])).status_code == 200
    assert wecom_content(fake_http).endswith('\n\n<@lisi>')


def test_no_mention_when_neither_annotation_nor_channel_has_at(alarm_app, fake_http):
    fake_http.master = [master_ok({**WECOM, 'at': ''})]
    assert post(alarm_app, make_payload([make_alert()])).status_code == 200
    content = wecom_content(fake_http)
    assert content.endswith('\n\n') and '<@' not in content


def test_no_channel_configured_returns_204(alarm_app, fake_http, caplog):
    fake_http.master = [master_ok(None)]
    with caplog.at_level(logging.INFO):
        resp = post(alarm_app, make_payload([make_alert()]))
    assert resp.status_code == 204
    assert fake_http.posts == []
    assert '没有可用的消息通知配置' in caplog.text and '集群 prod-k8s' in caplog.text


def test_master_unreachable_without_cache_returns_204(alarm_app, fake_http):
    fake_http.master = [requests.ConnectionError('connection refused')]
    resp = post(alarm_app, make_payload([make_alert()]))
    assert resp.status_code == 204
    assert fake_http.posts == []


def test_all_silenced_sends_nothing_and_counts_hits(alarm_app, fake_http):
    alarm_app.pool.silence_rows = [SILENCE_RESTART]
    resp = post(alarm_app, make_payload([make_alert(), make_alert(status='resolved')]))
    assert resp.status_code == 204
    # 全部被屏蔽时不用去 master 取配置
    assert fake_http.gets == [] and fake_http.posts == []
    # 通知路径会累加命中次数(第二条告警匹配前把第一条的命中回写)
    assert any(sql.startswith('UPDATE alert_silences SET match_count') for sql, _ in alarm_app.pool.executed)


def test_partially_silenced_sends_the_rest(alarm_app, fake_http):
    alarm_app.pool.silence_rows = [SILENCE_RESTART]
    fake_http.master = [master_ok(WECOM)]
    resp = post(alarm_app, make_payload([make_alert(), make_alert(alertname='K8S_Pod_OOM')]))
    assert resp.status_code == 200
    content = wecom_content(fake_http)
    assert 'K8S_Pod_OOM' in content and 'K8S_Pod_Restart' not in content


def test_slack_channel_keeps_list_format(alarm_app, fake_http):
    fake_http.master = [master_ok({'type': 'slack', 'token': 'T1/B2/xyz', 'at': 'U1'})]
    fake_http.im = FakeResponse(200, text='ok')
    alerts = [make_alert(), make_alert(alertname='K8S_Pod_OOM', status='resolved')]
    assert post(alarm_app, make_payload(alerts)).status_code == 200
    assert [p['url'] for p in fake_http.posts] == ['https://hooks.slack.com/services/T1/B2/xyz'] * 2
    firing = fake_http.posts[0]['json']['blocks']
    assert firing[0]['elements'][0]['elements'][0]['text'] == '💥故障: K8S_Pod_Restart,小于1分钟'
    assert firing[1]['text']['text'] == 'K8S_Pod_Restart 描述 <@U1>'
    assert firing[2]['elements'][0]['elements'][0] == {
        'type': 'link', 'text': '【屏蔽】', 'url': alarm_app.build_silence_url(alerts[0]['labels'])}
    resolved = fake_http.posts[1]['json']['blocks']
    assert resolved[0]['elements'][0]['elements'][0]['text'] == '🎉恢复: K8S_Pod_OOM,小于1分钟'
    assert [block['type'] for block in resolved] == ['rich_text', 'section', 'divider']


def test_im_network_error_returns_502_without_leaking_token(alarm_app, fake_http, caplog):
    fake_http.master = [master_ok(WECOM)]
    fake_http.im = requests.ConnectionError('Max retries exceeded with url: /cgi-bin/webhook/send?key=wecom-key')
    with caplog.at_level(logging.INFO):
        resp = post(alarm_app, make_payload([make_alert()]))
    assert resp.status_code == 502
    assert 'ConnectionError' in caplog.text and 'wecom-key' not in caplog.text


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


def test_silence_link(alarm_app, monkeypatch):
    labels = make_alert()['labels']
    url = alarm_app.build_silence_url(labels)
    prefill = json.loads(urllib.parse.unquote(url.split('prefill=', 1)[1]))
    assert prefill == {'alertname': 'K8S_Pod_Restart', 'env': 'prod-k8s', 'namespace': 'default',
                       'pod': 'web-6d4cf56db6-abcde'}
    # 没配 KUBEDOOR_EXTURL 时退回 Alertmanager 页面
    monkeypatch.setattr(alarm_app, 'KUBEDOOR_EXTURL', '')
    assert alarm_app.build_silence_url(labels) == (
        'http://alertmanager.example.com/#/alerts?silenced=false&inhibited=false&active=true&filter='
        + urllib.parse.quote('{alertname="K8S_Pod_Restart"}'))
