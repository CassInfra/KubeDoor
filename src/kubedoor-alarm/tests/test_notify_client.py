"""notify_client:按集群取通知配置、缓存与过期、master 失败时沿用,以及各 IM 的消息格式。"""
import logging

import pytest
import requests

import notify_client
from conftest import FakeResponse, master_ok

WECOM = {'type': 'wecom', 'token': 'wecom-key', 'at': 'zhangsan'}
DING = {'type': 'dingding', 'token': 'ding-token', 'at': '13800000000'}


# ---------------------------------------------------------------------------
# 取配置
# ---------------------------------------------------------------------------
def test_fetch_calls_master_with_internal_token(fake_http, resolver, internal_env):
    fake_http.master = [master_ok(WECOM)]
    assert notify_client.resolve('prod-k8s') == WECOM
    assert fake_http.gets == [{
        'url': 'http://kubedoor-master/api/internal/notification',
        'params': {'env': 'prod-k8s'},
        'headers': {'X-Kubedoor-Token': 'internal-secret'},
        'timeout': 5,
    }]


def test_master_url_from_env_and_empty_env_means_global(fake_http, resolver, internal_env, monkeypatch):
    monkeypatch.setenv('MASTER_INTERNAL_URL', 'http://master.test:8080/')
    fake_http.master = [master_ok(None)]
    assert notify_client.resolve('') is None
    assert fake_http.gets[0]['url'] == 'http://master.test:8080/api/internal/notification'
    assert fake_http.gets[0]['params'] == {'env': ''}


def test_channel_fields_are_normalized(fake_http, resolver, internal_env):
    fake_http.master = [master_ok({'type': 'feishu', 'token': ' hook-id ', 'at': None, 'extra': 1})]
    assert notify_client.resolve('prod') == {'type': 'feishu', 'token': 'hook-id', 'at': ''}


def test_cache_hit_within_ttl_then_refetch_after_expiry(fake_http, resolver, clock, internal_env):
    changed = {**WECOM, 'token': 'new-key'}
    fake_http.master = [master_ok(WECOM), master_ok(changed)]
    assert notify_client.resolve('prod') == WECOM
    clock.now += 14.9
    assert notify_client.resolve('prod') == WECOM
    assert len(fake_http.gets) == 1
    clock.now += 0.2  # 距第一次取配置 15.1 秒,已过期
    assert notify_client.resolve('prod') == changed
    assert len(fake_http.gets) == 2


def test_cache_is_per_cluster(fake_http, resolver, internal_env):
    fake_http.master = [master_ok(WECOM), master_ok(DING)]
    assert notify_client.resolve('a') == WECOM
    assert notify_client.resolve('b') == DING
    assert notify_client.resolve('a') == WECOM
    assert [get['params'] for get in fake_http.gets] == [{'env': 'a'}, {'env': 'b'}]


def test_no_config_returns_none_and_is_cached(fake_http, resolver, clock, internal_env):
    fake_http.master = [master_ok(None)]
    assert notify_client.resolve('prod') is None
    clock.now += 5
    assert notify_client.resolve('prod') is None
    assert len(fake_http.gets) == 1


@pytest.mark.parametrize('failure', [
    requests.ConnectionError('connection refused'),
    requests.Timeout('read timed out'),
    FakeResponse(500, text='Internal Server Error'),
    FakeResponse(401, {'success': False, 'error': 'bad_token', 'message': '内部访问令牌无效'}),
    FakeResponse(200, {'success': False, 'message': 'boom'}),
    FakeResponse(200, text='<html>not json</html>'),
    master_ok({'type': 'unknown', 'token': 'x'}),
    master_ok({'type': 'wecom', 'token': ''}),
    master_ok('wecom'),
], ids=['connect', 'timeout', 'http500', 'http401', 'not-success', 'not-json', 'bad-type', 'no-token', 'bad-data'])
def test_master_failure_keeps_last_good_config(fake_http, resolver, clock, internal_env, failure, caplog):
    fake_http.master = [master_ok(WECOM), failure, master_ok(None)]
    assert notify_client.resolve('prod') == WECOM
    clock.now += 16
    with caplog.at_level(logging.WARNING):
        assert notify_client.resolve('prod') == WECOM
    assert '沿用上次取到的配置' in caplog.text
    # 失败后同样等一个 TTL 再试,不让每批告警都去等 master 超时
    clock.now += 10
    assert notify_client.resolve('prod') == WECOM
    assert len(fake_http.gets) == 2
    clock.now += 6
    assert notify_client.resolve('prod') is None  # master 恢复,说已经没有配置
    assert len(fake_http.gets) == 3


def test_master_down_without_cache_returns_none_and_retries_next_time(fake_http, resolver, internal_env, caplog):
    fake_http.master = [requests.ConnectionError('connection refused'), master_ok(WECOM)]
    with caplog.at_level(logging.ERROR):
        assert notify_client.resolve('prod') is None
    assert '之前也没有取到过' in caplog.text
    # 没有可沿用的配置时不缓存失败,下一批告警马上重试
    assert notify_client.resolve('prod') == WECOM
    assert len(fake_http.gets) == 2


def test_missing_internal_token_does_not_call_master(fake_http, resolver, monkeypatch, caplog):
    monkeypatch.delenv('AI_INTERNAL_TOKEN', raising=False)
    with caplog.at_level(logging.ERROR):
        assert notify_client.resolve('prod') is None
    assert fake_http.gets == []
    assert 'AI_INTERNAL_TOKEN' in caplog.text


def test_unexpected_fetch_error_is_treated_as_failure(caplog):
    def broken(env):
        raise RuntimeError('bug')

    fresh = notify_client.ChannelResolver(fetch=broken)
    with caplog.at_level(logging.ERROR):
        assert fresh.resolve('prod') is None
    assert 'RuntimeError' in caplog.text


def test_fetch_logs_never_contain_tokens(fake_http, resolver, clock, internal_env, caplog):
    fake_http.master = [master_ok(WECOM), FakeResponse(503, {'success': False, 'message': 'unavailable'})]
    with caplog.at_level(logging.DEBUG):
        notify_client.resolve('prod')
        clock.now += 20
        notify_client.resolve('prod')
    assert 'unavailable' in caplog.text
    assert 'wecom-key' not in caplog.text and 'internal-secret' not in caplog.text


# ---------------------------------------------------------------------------
# 集群名
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


# ---------------------------------------------------------------------------
# 发送
# ---------------------------------------------------------------------------
def test_wecom_payload(fake_http):
    notify_client.send({'type': 'wecom', 'token': 'KEY', 'at': 'ignored'}, '### 故障', 'zhangsan')
    [post] = fake_http.posts
    assert post['url'] == 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=KEY'
    assert post['json'] == {'msgtype': 'markdown', 'markdown': {'content': '### 故障<@zhangsan>'}}
    assert post['headers'] == {'Content-Type': 'application/json'}
    assert post['timeout'] == 10


def test_dingding_payload(fake_http):
    notify_client.send({'type': 'dingding', 'token': 'TOKEN', 'at': ''}, 'md', '13800000000')
    [post] = fake_http.posts
    assert post['url'] == 'https://oapi.dingtalk.com/robot/send?access_token=TOKEN'
    assert post['json'] == {
        'msgtype': 'markdown',
        'markdown': {'title': '告警', 'text': 'md'},
        'at': {'atMobiles': ['13800000000']},
    }


def test_feishu_payload(fake_http):
    fake_http.im = FakeResponse(200, {'code': 0, 'msg': 'success'})
    notify_client.send({'type': 'feishu', 'token': 'HOOK', 'at': ''}, 'md', 'ou_123')
    [post] = fake_http.posts
    assert post['url'] == 'https://open.feishu.cn/open-apis/bot/v2/hook/HOOK'
    assert post['json'] == {
        'msg_type': 'interactive',
        'card': {
            'header': {'title': {'tag': 'plain_text', 'content': '告警通知'}, 'template': 'red'},
            'elements': [{'tag': 'markdown', 'content': 'md\n<at id=ou_123></at>'}],
        },
    }


def test_slack_sends_one_message_per_alert(fake_http):
    fake_http.im = FakeResponse(200, text='ok')
    content = [['💥故障: A,小于1分钟', 'desc A', 'https://silence.example'], ['🎉恢复: B,小于1分钟', 'desc B']]
    notify_client.send({'type': 'slack', 'token': 'T1/B2/xyz', 'at': ''}, content, 'U123')
    assert [post['url'] for post in fake_http.posts] == ['https://hooks.slack.com/services/T1/B2/xyz'] * 2
    title = {'type': 'rich_text', 'elements': [{'type': 'rich_text_section', 'elements': [
        {'type': 'text', 'text': '💥故障: A,小于1分钟', 'style': {'bold': True}}]}]}
    link = {'type': 'rich_text', 'elements': [{'type': 'rich_text_section', 'elements': [
        {'type': 'link', 'text': '【屏蔽】', 'url': 'https://silence.example'}]}]}
    assert fake_http.posts[0]['json'] == {'blocks': [
        title,
        {'type': 'section', 'text': {'type': 'mrkdwn', 'text': 'desc A <@U123>'}},
        link,
        {'type': 'divider'},
    ]}
    resolved = fake_http.posts[1]['json']['blocks']
    assert resolved[0]['elements'][0]['elements'][0]['text'] == '🎉恢复: B,小于1分钟'
    assert [block['type'] for block in resolved] == ['rich_text', 'section', 'divider']  # 恢复不带屏蔽链接


@pytest.mark.parametrize('kind, check', [
    ('wecom', lambda body: body['markdown']['content'] == 'md'),
    ('dingding', lambda body: 'at' not in body),
    ('feishu', lambda body: body['card']['elements'][0]['content'] == 'md'),
])
def test_no_mention_when_at_is_empty(fake_http, kind, check):
    notify_client.send({'type': kind, 'token': 'T', 'at': ''}, 'md', '')
    assert check(fake_http.posts[0]['json'])


def test_slack_no_mention_when_at_is_empty(fake_http):
    fake_http.im = FakeResponse(200, text='ok')
    notify_client.send({'type': 'slack', 'token': 'T/B/x', 'at': ''}, [['t', 'desc']], '')
    assert fake_http.posts[0]['json']['blocks'][1]['text']['text'] == 'desc'


@pytest.mark.parametrize('kind, response, ok', [
    ('wecom', FakeResponse(200, {'errcode': 0, 'errmsg': 'ok'}), True),
    ('wecom', FakeResponse(200, {'errcode': 93000, 'errmsg': 'invalid webhook url'}), False),
    ('wecom', FakeResponse(502, text='Bad Gateway'), False),
    ('dingding', FakeResponse(200, {'errcode': 0, 'errmsg': 'ok'}), True),
    ('dingding', FakeResponse(200, {'errcode': 310000, 'errmsg': 'keywords not in content'}), False),
    ('feishu', FakeResponse(200, {'code': 0, 'msg': 'success'}), True),
    ('feishu', FakeResponse(200, {'StatusCode': 0, 'StatusMessage': 'success'}), True),
    ('feishu', FakeResponse(200, {'code': 19001, 'msg': 'param invalid'}), False),
    ('slack', FakeResponse(200, text='ok'), True),
    ('slack', FakeResponse(404, text='no_service'), False),
])
def test_parse_response(kind, response, ok):
    assert notify_client.parse_response(kind, response)[0] is ok


def test_im_error_is_logged_not_raised(fake_http, caplog):
    fake_http.im = FakeResponse(200, {'errcode': 93000, 'errmsg': 'invalid webhook url'})
    with caplog.at_level(logging.WARNING):
        notify_client.send({'type': 'wecom', 'token': 'SECRET-KEY', 'at': ''}, 'md', '')
    assert '93000' in caplog.text and 'SECRET-KEY' not in caplog.text


def test_network_error_hides_token(fake_http):
    fake_http.im = requests.ConnectionError(
        "HTTPSConnectionPool(host='qyapi.weixin.qq.com', port=443): "
        "Max retries exceeded with url: /cgi-bin/webhook/send?key=SECRET-KEY")
    with pytest.raises(notify_client.SendError) as info:
        notify_client.send({'type': 'wecom', 'token': 'SECRET-KEY', 'at': ''}, 'md', '')
    assert 'SECRET-KEY' not in str(info.value)
    assert 'ConnectionError' in str(info.value)
    assert info.value.__cause__ is None and info.value.__suppress_context__
