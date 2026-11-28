#!/usr/bin/python3
"""告警通知代理客户端: 将告警/事件结构化负载安全转发给 kubedoor-master。

所有通道配置、AES-256-GCM 凭据解密、SSRF 校验、速率限制与多种 IM/Webhook/SMTP
渲染均集中在 kubedoor-master 内部完成; kubedoor-alarm 不再拉取、缓存或接触任何
明文机器人 token:

    POST {MASTER_INTERNAL_URL}/api/internal/notify
    请求头 X-Kubedoor-Token: <AI_INTERNAL_TOKEN>
    请求体 {"env", "category", "severity", "title", "body", "at", "items", "links"}
    返回 {"success": true, "data": {"matched": N, "queued": N, "trace_id": "..."}}

- 保留 get_internal(path, params=None, timeout=FETCH_TIMEOUT), FetchError, SendError,
  cluster_of, scope_label 供 settings_client 与 kubedoor-alarm 共用。
- 调用 POST /api/internal/notify 禁用重定向 (allow_redirects=False);
  网络异常、非 200、非 JSON 或失败响应均抛 SendError, 让 Alertmanager 原生重试。
"""

import logging
import os

import requests

DEFAULT_MASTER_URL = 'http://kubedoor-master'
# 取配置的超时(秒)
FETCH_TIMEOUT = 5
# 发送到 master 内部代理接口的超时(秒)
SEND_TIMEOUT = 10


class FetchError(Exception):
    """从 master 的内部接口取配置失败(连不上、鉴权失败、返回格式不对)。信息里不含令牌。"""


class SendError(Exception):
    """通过 master 代理发送告警通知失败(未配置内部令牌、网络异常、master 返回错误)。信息里不含敏感令牌。"""


def _brief(value, limit=300):
    """压成一行并截断,用于日志和错误信息。"""
    text = ' '.join(str(value).split())
    return text if len(text) <= limit else text[:limit] + '...'


def scope_label(env):
    """日志里说明作用范围。"""
    return f'集群 {env}' if env else '全局配置'


def cluster_of(payload, tag_key):
    """一批告警所属的集群:commonLabels 里集群标签的值,没有就看 groupLabels。

    都没有时返回空串,表示无集群标签(交由 master 按默认规则路由)。
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


def _master_url():
    return (os.environ.get('MASTER_INTERNAL_URL', '').strip() or DEFAULT_MASTER_URL).rstrip('/')


def _error_detail(response):
    """master 失败返回里的错误说明(只取 message / error 字段)。"""
    try:
        body = response.json()
    except ValueError:
        return ''
    if not isinstance(body, dict):
        return ''
    detail = body.get('message') or body.get('error')
    return f': {_brief(detail, 200)}' if detail else ''


def get_internal(path, params=None, timeout=FETCH_TIMEOUT):
    """GET master 的内部接口,返回响应里的 data。

    只带内部令牌、不带 X-User-Name:master 认作系统身份(这类接口只对系统身份开放)。
    通用配置(settings_client)经这里取。

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


def post_internal_notify(payload, timeout=SEND_TIMEOUT):
    """将结构化通知负载 POST 到 master 的 /api/internal/notify。

    Returns:
        dict: {'matched': int, 'queued': int, 'trace_id': str, ...}
    Raises:
        SendError: 缺少 AI_INTERNAL_TOKEN、网络异常、非 200 状态或非成功 JSON。
    """
    token = os.environ.get('AI_INTERNAL_TOKEN', '')
    if not token:
        raise SendError('没有环境变量 AI_INTERNAL_TOKEN,不能调用 master 的内部通知接口')
    url = f'{_master_url()}/api/internal/notify'
    try:
        response = requests.post(
            url,
            json=payload,
            headers={'X-Kubedoor-Token': token, 'Content-Type': 'application/json'},
            timeout=timeout,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        raise SendError(f'请求 {url} 失败: {type(exc).__name__}') from None

    if response.status_code != 200:
        raise SendError(f'master 通知接口返回 HTTP {response.status_code}{_error_detail(response)}')
    try:
        body = response.json()
    except ValueError:
        raise SendError('master 通知接口返回的不是 JSON') from None
    if not isinstance(body, dict) or body.get('success') is not True:
        raise SendError(f'master 通知接口返回失败{_error_detail(response)}')
    data = body.get('data')
    if not isinstance(data, dict):
        raise SendError('master 通知接口返回的 data 格式不正确')
    return data


def send_alert(
    env,
    items,
    *,
    at='',
    severity='',
    title='',
    body='',
    links=None,
    category='metric_alert',
    timeout=SEND_TIMEOUT,
):
    """发送结构化指标告警批次到 master, 返回 master 的路由与入队摘要 dict."""
    payload = {
        'env': '' if env is None else str(env).strip(),
        'category': (category or 'metric_alert').strip().lower() or 'metric_alert',
        'severity': '' if severity is None else str(severity).strip().lower(),
        'title': '' if title is None else str(title).strip(),
        'body': '' if body is None else str(body).strip(),
        'at': '' if at is None else str(at).strip(),
        'items': list(items or []),
        'links': list(links or []),
    }
    logging.debug(
        f"转发 {len(payload['items'])} 条指标告警到 master ({scope_label(payload['env'])})"
    )
    return post_internal_notify(payload, timeout=timeout)
