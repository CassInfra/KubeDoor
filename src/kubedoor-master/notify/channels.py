"""消息通知:四种 IM 群机器人的 token 解析、消息格式和返回解读。

纯函数,只用标准库,不发任何网络请求。master(notify/service.py)和安装服务 kubedoor-setup 共用:
setup 镜像单独拷贝这个文件、按文件路径加载,所以这里不能 import master 的其他模块。

- 库里只存机器人地址里的 token,发送时由 build_request 拼回完整地址(主机写死,token 换不了目标主机)
- 页面上可以粘贴整条 Webhook 地址,normalize_token 自动截出 token
"""

import json
import re
from urllib.parse import parse_qs, urlsplit

TYPES = ('wecom', 'dingding', 'feishu', 'slack')
TYPE_LABELS = {'wecom': '企业微信', 'dingding': '钉钉', 'feishu': '飞书', 'slack': 'Slack'}

MAX_TOKEN = 512
MAX_AT = 128
MAX_MESSAGE = 300  # parse_response 返回的说明最长这么多字符
OK_MESSAGE = '发送成功'
WECOM_MAX_BYTES = 4096  # 企微 markdown 内容的上限(UTF-8 字节),超了整条发不出去

_WECOM_URL = 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key='
_DINGDING_URL = 'https://oapi.dingtalk.com/robot/send?access_token='
_FEISHU_URL = 'https://open.feishu.cn/open-apis/bot/v2/hook/'
_SLACK_URL = 'https://hooks.slack.com/services/'

# 粘贴的 Webhook 地址:主机 -> IM 类型(没带 https:// 也认)
_HOSTS = {
    'qyapi.weixin.qq.com': 'wecom',
    'oapi.dingtalk.com': 'dingding',
    'open.feishu.cn': 'feishu',
    'hooks.slack.com': 'slack',
}
_FEISHU_PATH = '/open-apis/bot/v2/hook/'
_SLACK_PATH = '/services/'

TOKEN_HINTS = {
    'wecom': '群机器人 Webhook 地址(https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=…),或 key= 后面那一段',
    'dingding': '自定义机器人 Webhook 地址(https://oapi.dingtalk.com/robot/send?access_token=…),'
                '或 access_token= 后面那一段',
    'feishu': '自定义机器人 Webhook 地址(https://open.feishu.cn/open-apis/bot/v2/hook/…),或 /hook/ 后面那一段',
    'slack': 'Incoming Webhook 地址(https://hooks.slack.com/services/T…/B…/…),或 /services/ 后面那一段',
}

# token 会拼进地址的查询参数或路径,只允许不需要转义的字符(Slack 是 T…/B…/… 三段)
_TOKEN_PART = re.compile(r'[A-Za-z0-9_-]+')


def _has_control_char(value):
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


def _label(kind):
    return TYPE_LABELS.get(kind, str(kind))


def _check_kind(kind):
    if kind not in TYPES:
        raise ValueError(f'不支持的 IM 类型:{kind}(可选 {"、".join(TYPE_LABELS.values())})')


def _valid_token(kind, token):
    if kind == 'slack':
        parts = token.split('/')
        return len(parts) == 3 and all(_TOKEN_PART.fullmatch(part) for part in parts)
    return _TOKEN_PART.fullmatch(token) is not None


def _token_from_url(kind, value):
    """从粘贴的 Webhook 地址里截出 token;不认识的地址、和 kind 不是同一种 IM 的地址抛 ValueError"""
    hint = TOKEN_HINTS[kind]
    try:
        parts = urlsplit(value)
        host = (parts.hostname or '').lower()
    except ValueError:  # 例如方括号没闭合的 IPv6 地址
        raise ValueError(f'不认识的机器人地址,{_label(kind)}请填写{hint}') from None
    owner = _HOSTS.get(host) if parts.scheme.lower() in ('http', 'https') else None
    if owner is None:
        raise ValueError(f'不认识的机器人地址,{_label(kind)}请填写{hint}')
    if owner != kind:
        raise ValueError(f'这是{_label(owner)}的机器人地址,和选择的 IM 类型({_label(kind)})不一致')
    path = parts.path
    if kind == 'wecom' and path == '/cgi-bin/webhook/send':
        return (parse_qs(parts.query).get('key') or [''])[0].strip()
    if kind == 'dingding' and path == '/robot/send':
        return (parse_qs(parts.query).get('access_token') or [''])[0].strip()
    if kind == 'feishu' and path.startswith(_FEISHU_PATH):
        return path[len(_FEISHU_PATH):].strip('/')
    if kind == 'slack' and path.startswith(_SLACK_PATH):
        return path[len(_SLACK_PATH):].strip('/')
    raise ValueError(f'不认识的机器人地址,{_label(kind)}请填写{hint}')


def normalize_token(kind, value):
    """页面填的机器人地址 → 存库的 token:可以填 token,也可以粘贴整条 Webhook 地址(自动截出 token)。
    为空、带控制字符、超过 512 字符、含空白、不认识的地址或 token 格式不对,抛 ValueError(中文说明)。"""
    _check_kind(kind)
    label = _label(kind)
    text = '' if value is None else str(value).strip()
    if not text:
        raise ValueError(f'请填写{label}机器人地址')
    if _has_control_char(text):
        raise ValueError('机器人地址不能包含换行等控制字符')
    if len(text) > MAX_TOKEN:
        raise ValueError(f'机器人地址太长(最多 {MAX_TOKEN} 个字符)')
    if any(ch.isspace() for ch in text):
        raise ValueError('机器人地址中间不能有空格')
    lowered = text.lower()
    if not lowered.startswith(('http://', 'https://')) and lowered.split('/', 1)[0].split('?', 1)[0] in _HOSTS:
        text = 'https://' + text  # 粘贴时漏了 https://
        lowered = text.lower()
    if lowered.startswith(('http://', 'https://')) or '://' in text:
        token = _token_from_url(kind, text)
        if not token:
            raise ValueError(f'机器人地址里没有 token,{label}请填写{TOKEN_HINTS[kind]}')
    else:
        token = text
    if not _valid_token(kind, token):
        raise ValueError(f'机器人 token 格式不对,{label}请填写{TOKEN_HINTS[kind]}')
    return token


def normalize_at(value):
    """默认 @ 的人(企微 userid、钉钉手机号、飞书 open_id、Slack 用户 ID):去首尾空白,最多 128 字符,
    不能含控制字符;空串表示不 @ 人。不合法抛 ValueError。"""
    if value is None:
        return ''
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError('默认 @ 人的格式不正确')
    text = str(value).strip()
    if len(text) > MAX_AT:
        raise ValueError(f'默认 @ 人最多 {MAX_AT} 个字符')
    if _has_control_char(text):
        raise ValueError('默认 @ 人不能包含换行等控制字符')
    return text


def _fit_bytes(text, limit):
    """按 UTF-8 字节截断,截掉的部分用 … 表示(不截半个字符)"""
    data = text.encode('utf-8')
    if len(data) <= limit:
        return text
    ellipsis = '…'
    return data[:max(limit - len(ellipsis.encode('utf-8')), 0)].decode('utf-8', errors='ignore') + ellipsis


def build_request(kind, token, content, at=''):
    """拼出要 POST 的 (url, payload):消息格式照搬原来 master utils.py 的四个发送函数,
    只有 at 非空时才 @ 人;企微超过 4096 字节的内容截断(否则整条被拒)。"""
    _check_kind(kind)
    text = '' if content is None else str(content)
    at = (at or '').strip()
    if kind == 'wecom':
        suffix = f'<@{at}>' if at else ''
        body = _fit_bytes(text, WECOM_MAX_BYTES - len(suffix.encode('utf-8'))) + suffix
        return _WECOM_URL + token, {'msgtype': 'markdown', 'markdown': {'content': body}}
    if kind == 'dingding':
        payload = {'msgtype': 'markdown', 'markdown': {'title': '告警', 'text': text}}
        if at:
            payload['at'] = {'atMobiles': [at]}
        return _DINGDING_URL + token, payload
    if kind == 'feishu':
        card = {
            'header': {'title': {'tag': 'plain_text', 'content': '告警通知'}, 'template': 'red'},
            'elements': [{'tag': 'markdown', 'content': f'{text}\n<at id={at}></at>' if at else text}],
        }
        return _FEISHU_URL + token, {'msg_type': 'interactive', 'card': card}
    return _SLACK_URL + token, {'text': f'{text} <@{at}>' if at else text}


def _is_zero(value):
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value == 0
    return isinstance(value, str) and value.strip() == '0'


def _clip(text):
    text = str(text)
    return text if len(text) <= MAX_MESSAGE else text[:MAX_MESSAGE - 1] + '…'


def _brief(text):
    """对方返回的原文(HTML 错误页不要)"""
    text = (text or '').strip()
    return '' if text.startswith('<') else text[:200]


def _failure(status, code, msg, text):
    head = f'HTTP {status}' if status != 200 else ''
    detail = ''
    if code is not None:
        detail = f'错误码 {code}' + (f':{msg}' if msg not in (None, '') else '')
    elif msg not in (None, ''):
        detail = str(msg)
    elif _brief(text):
        detail = _brief(text)
    if head and detail:
        return _clip(f'{head},{detail}')
    return _clip(head or detail or 'IM 接口没有返回内容')


def parse_response(kind, status, body_text):
    """解读 IM 接口的返回,返回 (ok, message)。
    企微、钉钉看 errcode == 0;飞书看 code == 0 或 StatusCode == 0;Slack 看 HTTP 200 且返回 ok。
    失败时 message 带上对方返回的错误码和错误信息,最长 300 字符。"""
    _check_kind(kind)
    if isinstance(body_text, (bytes, bytearray)):
        body_text = bytes(body_text).decode('utf-8', errors='replace')
    text = body_text or ''
    if kind == 'slack':
        if status == 200 and text.strip() == 'ok':
            return True, OK_MESSAGE
        return False, _failure(status, None, None, text)
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        data = None
    if not isinstance(data, dict):
        return False, _failure(status, None, None, text)
    if kind == 'feishu':
        if _is_zero(data.get('code')) or _is_zero(data.get('StatusCode')):
            return True, OK_MESSAGE
        code = data['code'] if 'code' in data else data.get('StatusCode')
        msg = data['msg'] if 'msg' in data else data.get('StatusMessage')
        return False, _failure(status, code, msg, text)
    if _is_zero(data.get('errcode')):
        return True, OK_MESSAGE
    return False, _failure(status, data.get('errcode'), data.get('errmsg'), text)


def test_content(scope_label, username, now_text):
    """测试消息的正文,scope_label 是「全局配置」或「集群 prod-k8s」"""
    return f'【KubeDoor】消息通知测试（{scope_label}），操作人 {username}，{now_text}'


test_content.__test__ = False  # 名字以 test_ 开头:被测试文件直接 import 时别让 pytest 当成用例收集
