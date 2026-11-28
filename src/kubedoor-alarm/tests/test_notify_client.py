"""notify_client: 集群识别、get_internal 兼容以及转发告警到 master /api/internal/notify 的代理逻辑。"""
import logging

import pytest
import requests

import notify_client
from conftest import FakeResponse, master_ok


# ---------------------------------------------------------------------------
# 安全边界: 不再本地解析/缓存明文通道或拼装 IM Webhook
# ---------------------------------------------------------------------------
def test_plaintext_token_fetching_and_im_builders_removed():
    for removed in ('fetch_channel', 'ChannelResolver', 'resolve', 'send', 'wecom', 'dingding', 'feishu', 'slack', 'parse_response'):
        assert not hasattr(notify_client, removed)


# ---------------------------------------------------------------------------
# get_internal (供 settings_client 共用)
# ---------------------------------------------------------------------------
def test_get_internal_calls_master_with_internal_token(fake_http, internal_env):
    fake_http.master = [master_ok({'prom_k8s_tag_key': 'k8s'})]
    data = notify_client.get_internal('/api/internal/settings', {'env': 'prod-k8s'})
    assert data == {'prom_k8s_tag_key': 'k8s'}
    assert fake_http.gets == [{
        'url': 'http://kubedoor-master/api/internal/settings',
        'params': {'env': 'prod-k8s'},
        'headers': {'X-Kubedoor-Token': 'internal-secret'},
        'timeout': 5,
    }]


def test_get_internal_requires_token_and_raises_fetch_error_on_bad_response(fake_http, monkeypatch):
    monkeypatch.delenv('AI_INTERNAL_TOKEN', raising=False)
    with pytest.raises(notify_client.FetchError, match='AI_INTERNAL_TOKEN'):
        notify_client.get_internal('/api/internal/settings')
    assert fake_http.gets == []

    monkeypatch.setenv('AI_INTERNAL_TOKEN', 'internal-secret')
    fake_http.master = [FakeResponse(503, {'success': False, 'message': 'unavailable'})]
    with pytest.raises(notify_client.FetchError, match='HTTP 503'):
        notify_client.get_internal('/api/internal/settings')


# ---------------------------------------------------------------------------
# 集群名识别与 scope_label
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('payload, expected', [
    ({'commonLabels': {'k8s': 'prod'}, 'groupLabels': {'k8s': 'other'}}, 'prod'),
    ({'commonLabels': {'alertname': 'X'}, 'groupLabels': {'k8s': 'test'}}, 'test'),
    ({'commonLabels': {'k8s': ''}, 'groupLabels': {'k8s': 'test'}}, 'test'),
    ({'commonLabels': {'k8s': ' prod '}}, 'prod'),
    ({'commonLabels': {}, 'groupLabels': {}}, ''),
    ({'commonLabels': None}, ''),
    ({}, ''),
    (None, ''),
])
def test_cluster_of(payload, expected):
    assert notify_client.cluster_of(payload, 'k8s') == expected


def test_cluster_of_without_tag_key_uses_global():
    assert notify_client.cluster_of({'commonLabels': {'k8s': 'prod'}}, None) == ''
    assert notify_client.cluster_of({'commonLabels': {'k8s': 'prod'}}, '') == ''


def test_scope_label():
    assert notify_client.scope_label('prod') == '集群 prod'
    assert notify_client.scope_label('') == '全局配置'


# ---------------------------------------------------------------------------
# post_internal_notify / send_alert 统一代理发送
# ---------------------------------------------------------------------------
def test_send_alert_posts_structured_payload_to_master(fake_http, internal_env, monkeypatch, caplog):
    monkeypatch.setenv('MASTER_INTERNAL_URL', 'http://master.test:8080/')
    fake_http.notify = [FakeResponse(200, {'success': True, 'data': {'matched': 2, 'queued': 2, 'trace_id': 'tr-1'}})]
    items = [{'status': 'firing', 'summary': 'PodCrash,已持续5分钟', 'description': 'oom', 'severity': 'critical',
              'silence_url': 'https://kd.example/#/alarm/silence', 'labels': {'alertname': 'PodCrash'}}]
    links = [{'label': '屏蔽', 'url': 'https://kd.example/#/alarm/silence'}]

    with caplog.at_level(logging.DEBUG):
        res = notify_client.send_alert('prod-k8s', items, at='zhangsan', severity='critical', links=links)

    assert res == {'matched': 2, 'queued': 2, 'trace_id': 'tr-1'}
    assert fake_http.gets == []
    [post] = fake_http.posts
    assert post['url'] == 'http://master.test:8080/api/internal/notify'
    assert post['headers'] == {'X-Kubedoor-Token': 'internal-secret', 'Content-Type': 'application/json'}
    assert post['timeout'] == notify_client.SEND_TIMEOUT
    assert post['allow_redirects'] is False
    assert post['json'] == {
        'env': 'prod-k8s',
        'category': 'metric_alert',
        'severity': 'critical',
        'title': '',
        'body': '',
        'at': 'zhangsan',
        'items': items,
        'links': links,
    }
    assert 'internal-secret' not in caplog.text


def test_post_internal_notify_missing_token_raises_send_error(fake_http, monkeypatch):
    monkeypatch.delenv('AI_INTERNAL_TOKEN', raising=False)
    with pytest.raises(notify_client.SendError, match='AI_INTERNAL_TOKEN'):
        notify_client.post_internal_notify({'env': 'prod', 'category': 'metric_alert', 'items': []})
    assert fake_http.posts == []


@pytest.mark.parametrize('failure', [
    requests.ConnectionError('connection refused: http://kubedoor-master/api/internal/notify'),
    requests.Timeout('read timed out'),
    FakeResponse(503, {'success': False, 'message': 'not ready'}),
    FakeResponse(403, {'success': False, 'message': 'forbidden'}),
    FakeResponse(500, text='Internal Server Error'),
    FakeResponse(200, {'success': False, 'message': 'dispatch error'}),
    FakeResponse(200, text='<html>not json</html>'),
    FakeResponse(200, {'success': True, 'data': 'bad-shape'}),
], ids=['connect', 'timeout', 'http503', 'http403', 'http500', 'not-success', 'not-json', 'bad-data'])
def test_post_internal_notify_failures_raise_send_error(fake_http, internal_env, failure):
    fake_http.notify = [failure]
    with pytest.raises(notify_client.SendError) as info:
        notify_client.post_internal_notify({'env': 'prod', 'category': 'metric_alert', 'items': [{'summary': 'A'}]})
    assert info.value.__cause__ is None
    if isinstance(failure, Exception):
        assert info.value.__suppress_context__ is True
    assert 'internal-secret' not in str(info.value)
