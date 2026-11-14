#!/usr/bin/python3
"""告警通知通道:按集群向 master 取消息通知配置(带缓存),把告警消息发到 IM。

通知配置统一在 master 的「系统管理 → 消息通知」里维护,token 加密保存,只有 master 能解密。
kubedoor-alarm 发送前按集群调 master 的内部接口取明文配置:

    GET {MASTER_INTERNAL_URL}/api/internal/notification?env=<集群>
    请求头 X-Kubedoor-Token: <AI_INTERNAL_TOKEN>
    返回 {"success": true, "data": {"type": "wecom", "token": "...", "at": ""}};没有配置时 data 为 null

- master 先看该集群的单独配置,再看全局配置;env 为空就是全局配置。
- MASTER_INTERNAL_URL 默认 http://kubedoor-master(与 master 同命名空间);两个环境变量都在调用时读取。
- 按集群缓存 CACHE_TTL 秒。master 暂时不可用时沿用该集群上一次成功取到的结果,之后照样每 CACHE_TTL 秒重试;
  一次都没取到时返回 None,本批告警不发送。
- 日志里不能出现 token:master 的成功返回里有明文 token,不记录;IM 地址里带着 token,
  requests 的异常信息会带上完整地址,所以发送失败只记异常类型。
"""

import json
import logging
import os
import threading
import time

import requests

DEFAULT_MASTER_URL = 'http://kubedoor-master'
# 通知配置的缓存秒数:在「消息通知」页面保存后,最长这么久生效
CACHE_TTL = 15
# 取配置的超时(秒)
FETCH_TIMEOUT = 5
# 发送到 IM 的超时(秒)
SEND_TIMEOUT = 10
TYPES = ('wecom', 'dingding', 'feishu', 'slack')
_HEADERS = {'Content-Type': 'application/json'}


class FetchError(Exception):
    """从 master 的内部接口取配置失败(连不上、鉴权失败、返回格式不对)。信息里不含 token。"""


class SendError(Exception):
    """发送到 IM 时网络异常。信息里不含 IM 地址(地址里带着 token)。"""


def _brief(value, limit=300):
    """压成一行并截断,用于日志和错误信息。"""
    text = ' '.join(str(value).split())
    return text if len(text) <= limit else text[:limit] + '...'


def scope_label(env):
    """日志里说明用的是哪份配置。"""
    return f'集群 {env}' if env else '全局配置'


def cluster_of(payload, tag_key):
    """一批告警所属的集群:commonLabels 里集群标签的值,没有就看 groupLabels。

    都没有时返回空串,表示用全局配置。Alertmanager 的 group_by 带着集群标签,一批告警属于同一个集群。
    """
    if not tag_key or not isinstance(payload, dict):
        return ''
    for field in ('commonLabels', 'groupLabels'):
        labels = payload.get(field)
        if not isinstance(labels, dict):
            continue
        value = str(labels.get(tag_key) or '').strip()
        if value:
            return value
    return ''


# ---------------------------------------------------------------------------
# 从 master 取通知配置
# ---------------------------------------------------------------------------
def _master_url():
    return (os.environ.get('MASTER_INTERNAL_URL', '').strip() or DEFAULT_MASTER_URL).rstrip('/')


def _error_detail(response):
    """master 失败返回里的错误说明(只取 error / message 字段,不会带出 token)。"""
    try:
        body = response.json()
    except ValueError:
        return ''
    if not isinstance(body, dict):
        return ''
    detail = body.get('message') or body.get('error')
    return f': {_brief(detail, 200)}' if detail else ''


def _channel_from(data):
    if not isinstance(data, dict):
        raise FetchError('master 返回的通知配置格式不对')
    kind = data.get('type')
    token = data.get('token')
    at = data.get('at')
    if kind not in TYPES:
        raise FetchError(f'master 返回了不认识的 IM 类型: {_brief(kind, 50)}')
    if not isinstance(token, str) or not token.strip():
        raise FetchError('master 返回的通知配置缺少 token')
    at = '' if at is None else str(at).strip()
    return {'type': kind, 'token': token.strip(), 'at': at}


def get_internal(path, params=None, timeout=FETCH_TIMEOUT):
    """GET master 的内部接口,返回响应里的 data。

    只带内部令牌、不带 X-User-Name:master 认作系统身份(这类接口只对系统身份开放)。
    通知配置和通用配置(settings_client)都经这里取。

    Raises:
        FetchError: 没有内部令牌、连不上 master、鉴权失败、返回的不是成功的 JSON。信息里不含令牌。
    """
    token = os.environ.get('AI_INTERNAL_TOKEN', '')
    if not token:
        raise FetchError('没有环境变量 AI_INTERNAL_TOKEN,不能调用 master 的内部接口')
    url = f'{_master_url()}{path}'
    try:
        response = requests.get(url, params=params, headers={'X-Kubedoor-Token': token}, timeout=timeout)
    except requests.RequestException as exc:
        # 地址和查询参数(集群名)里都没有 token,可以记下来方便排查
        raise FetchError(f'请求 {url} 失败: {type(exc).__name__}: {_brief(exc, 200)}') from None
    if response.status_code != 200:
        raise FetchError(f'master 返回 HTTP {response.status_code}{_error_detail(response)}')
    try:
        body = response.json()
    except ValueError:
        raise FetchError('master 返回的不是 JSON') from None
    if not isinstance(body, dict) or body.get('success') is not True:
        raise FetchError(f'master 返回失败{_error_detail(response)}')
    return body.get('data')


def fetch_channel(env, timeout=FETCH_TIMEOUT):
    """向 master 取一次通知配置。

    Returns:
        {'type', 'token', 'at'};master 说没有配置时返回 None。
    Raises:
        FetchError: 连不上 master、鉴权失败或返回格式不对。
    """
    data = get_internal('/api/internal/notification', {'env': env}, timeout)
    if data is None:
        return None
    return _channel_from(data)


class ChannelResolver:
    """按集群缓存通知配置。线程安全:Flask 多线程处理请求时共用一个实例。"""

    def __init__(self, ttl=CACHE_TTL, fetch=fetch_channel, clock=time.monotonic):
        self._ttl = ttl
        self._fetch = fetch
        self._clock = clock
        self._lock = threading.Lock()
        # {env: (取到的时间, 配置或 None)};None 表示 master 说没有配置,同样缓存
        self._cache = {}

    def resolve(self, env):
        """返回该集群要用的通知配置 {'type', 'token', 'at'};没有配置、或一次都没取到时返回 None。"""
        env = env or ''
        now = self._clock()
        with self._lock:
            cached = self._cache.get(env)
        if cached is not None and now - cached[0] < self._ttl:
            return cached[1]
        try:
            channel = self._fetch(env)
        except Exception as exc:  # 取配置的任何异常都不能让告警请求报错,按失败处理
            reason = str(exc) if isinstance(exc, FetchError) else f'{type(exc).__name__}: {_brief(exc, 200)}'
            if cached is None:
                logging.error(f'【notify】从 master 获取通知配置失败({scope_label(env)}),之前也没有取到过: {reason}')
                return None
            logging.warning(f'【notify】从 master 获取通知配置失败({scope_label(env)}),沿用上次取到的配置: {reason}')
            channel = cached[1]
        with self._lock:
            # 失败时也记下这次的时间:master 不可用期间每 ttl 秒才重试一次,不让每批告警都等超时
            self._cache[env] = (now, channel)
        return channel


_resolver = ChannelResolver()


def resolve(env):
    """按集群取通知配置(模块级缓存),见 ChannelResolver.resolve。"""
    return _resolver.resolve(env)


# ---------------------------------------------------------------------------
# 发送到 IM:消息格式与原来一致,只有 at 非空时才 @ 人
# ---------------------------------------------------------------------------
def _post(kind, url, payload):
    """POST 一条消息。网络异常改抛 SendError:原异常信息带着完整地址(含 token),不能进日志。"""
    try:
        return requests.post(url, headers=_HEADERS, data=json.dumps(payload).encode('utf-8'), timeout=SEND_TIMEOUT)
    except requests.RequestException as exc:
        raise SendError(f'【{kind}】发送失败: {type(exc).__name__}') from None


def parse_response(kind, response):
    """解读 IM 接口的返回:(是否成功, 返回内容)。

    企微、钉钉看 errcode == 0;飞书看 code == 0 或 StatusCode == 0;Slack 看 HTTP 200 且返回 ok。
    """
    text = _brief(response.text, 300)
    if kind == 'slack':
        return response.status_code == 200 and response.text.strip() == 'ok', f'HTTP {response.status_code}: {text}'
    try:
        body = response.json()
    except ValueError:
        return False, f'HTTP {response.status_code}: {text}'
    if not isinstance(body, dict):
        return False, f'HTTP {response.status_code}: {text}'
    if kind == 'feishu':
        ok = body.get('code') == 0 or body.get('StatusCode') == 0
    else:
        ok = body.get('errcode') == 0
    return ok, text


def _log_result(kind, response):
    ok, message = parse_response(kind, response)
    if ok:
        logging.info(f'【{kind}】{message}')
    else:
        logging.warning(f'【{kind}】发送失败,IM 返回: {message}')


def wecom(token, content, at=''):
    url = 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=' + token
    text = f'{content}<@{at}>' if at else content
    _log_result('wecom', _post('wecom', url, {'msgtype': 'markdown', 'markdown': {'content': text}}))


def dingding(token, content, at=''):
    url = 'https://oapi.dingtalk.com/robot/send?access_token=' + token
    params = {'msgtype': 'markdown', 'markdown': {'title': '告警', 'text': content}}
    if at:
        params['at'] = {'atMobiles': [at]}
    _log_result('dingding', _post('dingding', url, params))


def feishu(token, content, at=''):
    url = f'https://open.feishu.cn/open-apis/bot/v2/hook/{token}'
    text = f'{content}\n<at id={at}></at>' if at else content
    params = {
        'msg_type': 'interactive',
        'card': {
            'header': {'title': {'tag': 'plain_text', 'content': '告警通知'}, 'template': 'red'},
            'elements': [{'tag': 'markdown', 'content': text}],
        },
    }
    _log_result('feishu', _post('feishu', url, params))


def slack(token, content, at=''):
    """content 是 [[标题, 内容, 屏蔽链接(可选)], ...],每条告警单独发一条。"""
    url = f'https://hooks.slack.com/services/{token}'
    for message in content:
        # 标题(加粗)
        blocks = [{
            'type': 'rich_text',
            'elements': [{'type': 'rich_text_section',
                          'elements': [{'type': 'text', 'text': message[0], 'style': {'bold': True}}]}],
        }]
        # 消息内容(包含 @ 用户)
        content_text = message[1]
        if at:
            content_text += f' <@{at}>'
        blocks.append({'type': 'section', 'text': {'type': 'mrkdwn', 'text': content_text}})
        # 有第三个字段(故障)时加屏蔽链接
        if len(message) >= 3:
            blocks.append({
                'type': 'rich_text',
                'elements': [{'type': 'rich_text_section',
                              'elements': [{'type': 'link', 'text': '【屏蔽】', 'url': message[2]}]}],
            })
        blocks.append({'type': 'divider'})
        _log_result('slack', _post('slack', url, {'blocks': blocks}))


_SENDERS = {'wecom': wecom, 'dingding': dingding, 'feishu': feishu, 'slack': slack}


def send(channel, content, at=''):
    """按通道类型发送。content:Slack 为 [[标题, 内容, 屏蔽链接(可选)], ...],其余为 markdown 字符串。

    IM 返回错误只记日志;网络异常抛 SendError。
    """
    _SENDERS[channel['type']](channel['token'], content, at)
