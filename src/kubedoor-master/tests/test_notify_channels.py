"""消息通知 notify/channels.py(纯函数):四种 IM 的 token / Webhook 地址解析、消息格式、返回解读。

安装服务 kubedoor-setup 会单独拷贝这个文件、按路径加载,所以还要检查它只用标准库。
"""
import ast
import importlib.util
import json
import pathlib
import sys

import pytest

from notify import channels

CHANNELS_FILE = pathlib.Path(channels.__file__)

WECOM = 'a1b2c3d4-0000-4bc4-97a0-0ec2c3f5a1b2'
DING = '0123456789abcdef' * 4
FEISHU = '3f8c1a2b-1111-2222-3333-444455556666'
SLACK = 'T00000000/B00000000/XXXXXXXXXXXXXXXXXXXXXXXX'

URLS = {
    'wecom': f'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={WECOM}',
    'dingding': f'https://oapi.dingtalk.com/robot/send?access_token={DING}',
    'feishu': f'https://open.feishu.cn/open-apis/bot/v2/hook/{FEISHU}',
    'slack': f'https://hooks.slack.com/services/{SLACK}',
}
TOKENS = {'wecom': WECOM, 'dingding': DING, 'feishu': FEISHU, 'slack': SLACK}


# ---------------------------------------------------------------------------
# 只用标准库(setup 镜像按路径单独加载)
# ---------------------------------------------------------------------------
def test_module_only_imports_standard_library():
    tree = ast.parse(CHANNELS_FILE.read_text(encoding='utf-8'))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split('.')[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, '不能相对导入 master 的其他模块'
            names.add(node.module.split('.')[0])
    assert names and names <= set(sys.stdlib_module_names), names


def test_module_loads_standalone_by_path():
    """同 setup 加载 auth/passwords.py 的写法:按文件路径、换个模块名加载"""
    spec = importlib.util.spec_from_file_location('_kubedoor_setup_notify_channels', CHANNELS_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.TYPES == ('wecom', 'dingding', 'feishu', 'slack')
    assert module.TYPE_LABELS == {'wecom': '企业微信', 'dingding': '钉钉', 'feishu': '飞书', 'slack': 'Slack'}
    assert module.normalize_token('wecom', URLS['wecom']) == WECOM


# ---------------------------------------------------------------------------
# normalize_token
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('kind', channels.TYPES)
def test_token_or_full_webhook_url(kind):
    token = TOKENS[kind]
    assert channels.normalize_token(kind, token) == token
    assert channels.normalize_token(kind, f'  {token}\t') == token  # 去掉首尾空白
    assert channels.normalize_token(kind, URLS[kind]) == token
    assert channels.normalize_token(kind, f' {URLS[kind]} ') == token
    # 粘贴时漏了 https://、主机名大写、用的 http:// 也认
    assert channels.normalize_token(kind, URLS[kind].replace('https://', '')) == token
    assert channels.normalize_token(kind, URLS[kind].replace('https://', 'HTTP://')) == token


def test_webhook_url_variants():
    # 钉钉加签地址多带的参数不要;企微多带的参数不要
    assert channels.normalize_token('dingding', URLS['dingding'] + '&timestamp=1&sign=abc') == DING
    assert channels.normalize_token('wecom', f'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?debug=1&key={WECOM}') == WECOM
    # 路径末尾多了 /
    assert channels.normalize_token('feishu', URLS['feishu'] + '/') == FEISHU
    assert channels.normalize_token('slack', URLS['slack'] + '/') == SLACK
    assert channels.normalize_token('wecom', URLS['wecom'].replace('qyapi.weixin.qq.com', 'QYAPI.WEIXIN.QQ.COM')) == WECOM


@pytest.mark.parametrize('kind, value, error', [
    ('wecom', '', '请填写企业微信机器人地址'),
    ('dingding', None, '请填写钉钉机器人地址'),
    ('feishu', '   ', '请填写飞书机器人地址'),
    ('wecom', f'{WECOM}\n', None),  # 首尾的换行属于空白,去掉即可
    ('wecom', f'abc\x00def', '控制字符'),
    ('wecom', 'a\x7fb', '控制字符'),
    ('wecom', 'a' * 513, '最多 512 个字符'),
    ('wecom', 'abc def', '不能有空格'),
    ('wecom', 'abc　def', '不能有空格'),
    ('wecom', 'https://example.com/cgi-bin/webhook/send?key=abc', '不认识的机器人地址'),
    ('wecom', 'ftp://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc', '不认识的机器人地址'),
    ('wecom', 'http://[::1/x', '不认识的机器人地址'),
    ('wecom', 'https://qyapi.weixin.qq.com/cgi-bin/other?key=abc', '不认识的机器人地址'),
    ('wecom', URLS['dingding'], '这是钉钉的机器人地址,和选择的 IM 类型(企业微信)不一致'),
    ('slack', URLS['feishu'], '这是飞书的机器人地址'),
    ('wecom', 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?foo=1', '机器人地址里没有 token'),
    ('dingding', 'https://oapi.dingtalk.com/robot/send?access_token=', '机器人地址里没有 token'),
    ('feishu', 'https://open.feishu.cn/open-apis/bot/v2/hook/', '机器人地址里没有 token'),
    ('slack', 'https://hooks.slack.com/workflows/T0/A0/x', '不认识的机器人地址'),
    # token 会拼进地址:不能带改变地址结构的字符
    ('wecom', 'abc&debug=1', '格式不对'),
    ('wecom', 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc%26x', '格式不对'),
    ('feishu', 'https://open.feishu.cn/open-apis/bot/v2/hook/../../x', '格式不对'),
    ('feishu', 'abc/def', '格式不对'),
    ('slack', 'T00000000/B00000000', '格式不对'),  # 少了最后一段
    ('slack', 'T0/B0/x/y', '格式不对'),
    ('slack', 'T0//x', '格式不对'),
    ('qq', 'abc', '不支持的 IM 类型'),
])
def test_invalid_tokens(kind, value, error):
    if error is None:
        assert channels.normalize_token(kind, value) == value.strip()
        return
    with pytest.raises(ValueError, match=error.replace('(', r'\(').replace(')', r'\)')):
        channels.normalize_token(kind, value)


def test_token_length_limit_is_512():
    assert channels.normalize_token('wecom', 'a' * 512) == 'a' * 512


# ---------------------------------------------------------------------------
# normalize_at
# ---------------------------------------------------------------------------
def test_normalize_at():
    assert channels.normalize_at('  zhangsan ') == 'zhangsan'
    assert channels.normalize_at('') == ''
    assert channels.normalize_at(None) == ''
    assert channels.normalize_at(13800000000) == '13800000000'  # 钉钉手机号写成了数字
    assert channels.normalize_at('x' * 128) == 'x' * 128
    with pytest.raises(ValueError, match='最多 128 个字符'):
        channels.normalize_at('x' * 129)
    with pytest.raises(ValueError, match='控制字符'):
        channels.normalize_at('zhang\nsan')
    for bad in (True, ['zhangsan'], {'id': 1}):
        with pytest.raises(ValueError, match='格式不正确'):
            channels.normalize_at(bad)


# ---------------------------------------------------------------------------
# build_request:照搬原来 master utils.py 的格式,at 为空时不 @
# ---------------------------------------------------------------------------
def test_wecom_request():
    url, payload = channels.build_request('wecom', WECOM, '**告警** 内容', 'zhangsan')
    assert url == URLS['wecom']
    assert payload == {'msgtype': 'markdown', 'markdown': {'content': '**告警** 内容<@zhangsan>'}}
    # 原来企微无条件拼 <@{at}>,at 为空时会发出 <@>
    assert channels.build_request('wecom', WECOM, '内容', '')[1] == {'msgtype': 'markdown', 'markdown': {'content': '内容'}}
    assert channels.build_request('wecom', WECOM, '内容')[1]['markdown']['content'] == '内容'


def test_dingding_request():
    url, payload = channels.build_request('dingding', DING, '内容', '13800000000')
    assert url == URLS['dingding']
    assert payload == {'msgtype': 'markdown', 'markdown': {'title': '告警', 'text': '内容'},
                       'at': {'atMobiles': ['13800000000']}}
    assert channels.build_request('dingding', DING, '内容', '')[1] == {
        'msgtype': 'markdown', 'markdown': {'title': '告警', 'text': '内容'}}


def test_feishu_request():
    url, payload = channels.build_request('feishu', FEISHU, '内容', 'ou_123')
    assert url == URLS['feishu']
    header = {'title': {'tag': 'plain_text', 'content': '告警通知'}, 'template': 'red'}
    assert payload == {'msg_type': 'interactive', 'card': {
        'header': header, 'elements': [{'tag': 'markdown', 'content': '内容\n<at id=ou_123></at>'}]}}
    assert channels.build_request('feishu', FEISHU, '内容', '')[1] == {'msg_type': 'interactive', 'card': {
        'header': header, 'elements': [{'tag': 'markdown', 'content': '内容'}]}}


def test_slack_request():
    url, payload = channels.build_request('slack', SLACK, '内容', 'U123')
    assert url == URLS['slack']
    assert payload == {'text': '内容 <@U123>'}
    assert channels.build_request('slack', SLACK, '内容', '')[1] == {'text': '内容'}


def test_request_payload_is_json_serializable():
    for kind in channels.TYPES:
        _, payload = channels.build_request(kind, TOKENS[kind], '中文 **markdown**\n第二行', 'x')
        assert json.loads(json.dumps(payload)) == payload


def test_wecom_long_content_is_truncated_to_4096_bytes():
    """企微 markdown 内容超过 4096 字节整条发不出去:截断(不截半个汉字),@ 人留在最后"""
    content = '告警' * 3000  # 18000 字节
    body = channels.build_request('wecom', WECOM, content, 'zhangsan')[1]['markdown']['content']
    assert len(body.encode('utf-8')) <= channels.WECOM_MAX_BYTES
    assert body.endswith('…<@zhangsan>') and body.startswith('告警告警')
    short = '告警' * 10
    assert channels.build_request('wecom', WECOM, short)[1]['markdown']['content'] == short
    exact = 'a' * 4096
    assert channels.build_request('wecom', WECOM, exact)[1]['markdown']['content'] == exact
    # 其他 IM 不截断
    assert channels.build_request('dingding', DING, content)[1]['markdown']['text'] == content


def test_build_request_rejects_unknown_type():
    with pytest.raises(ValueError, match='不支持的 IM 类型'):
        channels.build_request('qq', 'x', '内容')


# ---------------------------------------------------------------------------
# parse_response
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('kind, status, body, expected', [
    ('wecom', 200, '{"errcode":0,"errmsg":"ok"}', (True, '发送成功')),
    ('wecom', 200, '{"errcode":93000,"errmsg":"invalid webhook url, hint: [x]"}',
     (False, '错误码 93000:invalid webhook url, hint: [x]')),
    ('wecom', 200, '{"errcode":40058,"errmsg":""}', (False, '错误码 40058')),
    ('wecom', 200, '{"errmsg":"no code"}', (False, 'no code')),
    ('wecom', 200, '{"errcode":false}', (False, '错误码 False')),  # 布尔值不算 0
    ('wecom', 502, '<html><body>Bad Gateway</body></html>', (False, 'HTTP 502')),
    ('wecom', 200, 'not json', (False, 'not json')),
    ('wecom', 200, '', (False, 'IM 接口没有返回内容')),
    ('wecom', 200, '[1]', (False, '[1]')),
    ('dingding', 200, '{"errcode":0,"errmsg":"ok"}', (True, '发送成功')),
    ('dingding', 200, '{"errcode":310000,"errmsg":"keywords not in content"}',
     (False, '错误码 310000:keywords not in content')),
    ('dingding', 200, '{"errcode":"0"}', (True, '发送成功')),
    ('feishu', 200, '{"StatusCode":0,"StatusMessage":"success","code":0,"data":{},"msg":"success"}', (True, '发送成功')),
    ('feishu', 200, '{"StatusCode":0,"StatusMessage":"success"}', (True, '发送成功')),
    ('feishu', 200, '{"code":0,"msg":"success"}', (True, '发送成功')),
    ('feishu', 200, '{"code":19024,"data":{},"msg":"Key Words Not Found"}', (False, '错误码 19024:Key Words Not Found')),
    ('feishu', 400, '{"code":9499,"msg":"Bad Request"}', (False, 'HTTP 400,错误码 9499:Bad Request')),
    ('feishu', 200, '{"StatusCode":19001,"StatusMessage":"param invalid"}', (False, '错误码 19001:param invalid')),
    ('slack', 200, 'ok', (True, '发送成功')),
    ('slack', 200, 'ok\n', (True, '发送成功')),
    ('slack', 404, 'no_service', (False, 'HTTP 404,no_service')),
    ('slack', 403, 'invalid_token', (False, 'HTTP 403,invalid_token')),
    ('slack', 500, '<html>error</html>', (False, 'HTTP 500')),
    ('slack', 200, 'something else', (False, 'something else')),
])
def test_parse_response(kind, status, body, expected):
    assert channels.parse_response(kind, status, body) == expected


def test_parse_response_accepts_bytes_and_truncates_to_300():
    assert channels.parse_response('wecom', 200, b'{"errcode":0}') == (True, '发送成功')
    ok, message = channels.parse_response('dingding', 200, json.dumps({'errcode': 1, 'errmsg': 'x' * 1000}))
    assert ok is False and len(message) == 300 and message.startswith('错误码 1:xxx') and message.endswith('…')
    ok, message = channels.parse_response('slack', 400, 'y' * 1000)
    assert ok is False and len(message) <= 300
    with pytest.raises(ValueError, match='不支持的 IM 类型'):
        channels.parse_response('qq', 200, '{}')


# ---------------------------------------------------------------------------
# test_content
# ---------------------------------------------------------------------------
def test_test_message_content():
    assert channels.test_content('全局配置', 'kubedoor', '2026-11-07 12:00:00') == \
        '【KubeDoor】消息通知测试（全局配置），操作人 kubedoor，2026-11-07 12:00:00'
    assert channels.test_content('集群 prod-k8s', 'alice', '2026-11-07 12:00:01') == \
        '【KubeDoor】消息通知测试（集群 prod-k8s），操作人 alice，2026-11-07 12:00:01'
    assert channels.test_content.__test__ is False  # 被直接 import 时不会被 pytest 当成用例
