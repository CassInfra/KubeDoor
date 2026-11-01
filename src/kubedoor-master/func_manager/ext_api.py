"""调用管理员配置的外部 HTTP 接口:获取代码(code_info.py)和获取运行配置(runtime_config.py)共用的底层。

- 配置:接口地址只接受 http:// 或 https://;请求头的值用 AI_ENCRYPTION_KEY 加密(同 LDAP 绑定密码),
  给页面只回显名称,保存时值留空表示沿用已保存的同名请求头
- 调用:POST JSON,每次带超时、不跟随重定向、响应读到上限就停;失败原因取对方 JSON 里的
  message / error / msg / detail,或者开头一段纯文本(HTML 错误页不要)
- AI 服务回调(/api/ai/internal/*)只认内部 token 带来的真人账号(同 ai_api.authorize)

请求头的值、接口返回的内容都不写日志。
"""

import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web

from auth.crypto import decrypt_secret, encrypt_secret

USER_AGENT = 'KubeDoor'
MAX_URL = 8192  # 获取代码的下载链接可能带很长的签名参数
MAX_HEADERS = 10
MAX_HEADER_VALUE = 4096
# 这几个由 master 决定,不能在配置里覆盖
RESERVED_HEADERS = frozenset({'host', 'content-type', 'content-length', 'transfer-encoding', 'connection'})
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,100}")
_K8S_NAME = re.compile(r'[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?')


class ExtApiError(Exception):
    """带 HTTP 状态码和错误代码的失败,handler 原样返回给页面 / AI"""

    def __init__(self, status, message, code='bad_request'):
        super().__init__(message)
        self.status, self.message, self.code = status, message, code


def fail(status, message, code='bad_request', data=None):
    body = {'success': False, 'code': code, 'message': message, 'error': message}
    if data is not None:
        body['data'] = data
    return web.json_response(body, status=status)


def _dumps(payload):
    return json.dumps(payload).encode('utf-8')


async def json_response(payload, status=200, large=False):
    """large 时(几 MB 的配置)在线程里序列化,不阻塞 event loop"""
    body = await asyncio.to_thread(_dumps, payload) if large else _dumps(payload)
    return web.Response(body=body, status=status, content_type='application/json', charset='utf-8')


async def read_json(request):
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def iso(value):
    return value.isoformat() if isinstance(value, datetime) else value


def has_control_char(value):
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


def is_http_url(value, max_len=MAX_URL):
    """http:// 或 https:// 开头、有主机名、没有空白和控制字符的地址"""
    if not isinstance(value, str) or not value or len(value) > max_len:
        return False
    if any(ch.isspace() for ch in value) or has_control_char(value):
        return False
    try:
        parts = urlsplit(value)
        return parts.scheme in ('http', 'https') and bool(parts.hostname)
    except ValueError:  # 例如方括号没闭合的 IPv6 地址
        return False


def valid_k8s_name(value):
    """K8S 资源名称(小写字母、数字、- 和 .,最长 253)"""
    return isinstance(value, str) and len(value) <= 253 and _K8S_NAME.fullmatch(value) is not None


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
def normalize_headers(items, saved):
    """值留空表示沿用已保存的同名请求头(名称不区分大小写),新加的必须填值;列表里没有的即删除。"""
    if items is None:  # 没传:保持原样
        return [dict(item) for item in saved]
    if not isinstance(items, list):
        raise ValueError('请求头格式不正确')
    if len(items) > MAX_HEADERS:
        raise ValueError(f'请求头最多 {MAX_HEADERS} 个')
    saved_values = {item['name'].lower(): item.get('value') for item in saved}
    headers, seen = [], set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError('请求头格式不正确')
        name = str(item.get('name') or '').strip()
        if not _HEADER_NAME.fullmatch(name):
            raise ValueError(f'请求头名称不合法:{name or "(空)"}(只能用字母、数字和 - _ . 等符号,不能有空格和冒号)')
        lower = name.lower()
        if lower in RESERVED_HEADERS:
            raise ValueError(f'请求头 {name} 由系统设置,不能自定义')
        if lower in seen:
            raise ValueError(f'请求头 {name} 重复了')
        seen.add(lower)
        value = item.get('value')
        value = '' if value is None else str(value).strip()
        if not value:
            if not saved_values.get(lower):
                raise ValueError(f'请填写请求头 {name} 的值')
            headers.append({'name': name, 'value': saved_values[lower]})
            continue
        if len(value) > MAX_HEADER_VALUE or has_control_char(value):
            raise ValueError(f'请求头 {name} 的值不能换行,最长 {MAX_HEADER_VALUE} 个字符')
        headers.append({'name': name, 'value': encrypt_secret(value)})
    return headers


def normalize_endpoint(raw, existing, defaults, *, timeout_range, url_example, flags=()):
    """校验并规范化页面提交的接口配置,返回存库格式(请求头的值已加密);没传的字段沿用原值,不合法抛 ValueError。

    公共字段 enabled、url、headers、timeout、tls_verify;flags 是各功能自己的布尔开关(如 ai_download)。"""
    if not isinstance(raw, dict):
        raise ValueError('配置格式不正确')
    existing = {**defaults, **(existing or {})}
    url = raw.get('url', existing['url'])
    cfg = {
        'enabled': bool(raw.get('enabled', existing['enabled'])),
        'url': '' if url is None else str(url).strip(),
        'tls_verify': bool(raw.get('tls_verify', existing['tls_verify'])),
    }
    for flag in flags:
        cfg[flag] = bool(raw.get(flag, existing[flag]))
    low, high = timeout_range
    try:
        cfg['timeout'] = int(raw.get('timeout', existing['timeout']))
    except (TypeError, ValueError):
        raise ValueError('超时时间必须是整数') from None
    if not low <= cfg['timeout'] <= high:
        raise ValueError(f'超时时间取 {low}~{high} 秒')
    if cfg['enabled'] and not cfg['url']:
        raise ValueError('启用时必须填写接口地址')
    if cfg['url'] and not is_http_url(cfg['url']):
        raise ValueError(f'接口地址要以 http:// 或 https:// 开头,例如 {url_example}')
    saved = [item for item in existing['headers'] if isinstance(item, dict) and item.get('name')]
    cfg['headers'] = normalize_headers(raw.get('headers'), saved)
    return cfg


def public_endpoint(cfg, defaults, flags=()):
    """给页面看的配置:请求头只有名称"""
    cfg = {**defaults, **(cfg or {})}
    data = {
        'enabled': bool(cfg['enabled']),
        'url': cfg['url'],
        'headers': [{'name': item['name'], 'has_value': bool(item.get('value'))}
                    for item in cfg['headers'] if isinstance(item, dict) and item.get('name')],
        'timeout': cfg['timeout'],
        'tls_verify': bool(cfg['tls_verify']),
    }
    for flag in flags:
        data[flag] = bool(cfg[flag])
    return data


def audit_detail(public):
    """改配置的审计内容:public_endpoint 的结果,请求头只记名称"""
    return {**public, 'headers': [item['name'] for item in public['headers']]}


def request_headers(cfg):
    """解密后的请求头;解不开(AI_ENCRYPTION_KEY 换过)抛 ExtApiError"""
    headers = {}
    for item in cfg.get('headers') or []:
        try:
            headers[item['name']] = decrypt_secret(item.get('value') or '')
        except ValueError as exc:
            raise ExtApiError(500, f'请求头 {item["name"]} 的值无法解密({exc}),请系统管理员重新填写',
                              'decrypt_failed') from exc
    if not any(name.lower() == 'user-agent' for name in headers):
        headers['User-Agent'] = USER_AGENT
    return headers


# ---------------------------------------------------------------------------
# 调用
# ---------------------------------------------------------------------------
def error_reason(text):
    """非 2xx 时对方给的原因:JSON 里的 message / error / msg / detail,或者开头一段纯文本(HTML 错误页不要)"""
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        data = None
    if isinstance(data, dict):
        for key in ('message', 'error', 'msg', 'detail'):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:300]
        return ''
    text = text.strip()
    return '' if text.startswith('<') else text[:200]


def redirect_error(status, location=''):
    target = f' → {location}' if location else ''
    return f'接口返回了重定向(HTTP {status}{target}),不会跟随,请直接配置最终地址'


def status_error(status, text):
    reason = error_reason(text)
    return f'接口返回 HTTP {status}' + (f':{reason}' if reason else '')


def size_text(size):
    mb = 1024 * 1024
    return f'{size // mb} MB' if size > mb and size % mb == 0 else f'{size // 1024} KB'


async def read_limited(resp, limit):
    """读响应体,超过 limit 字节就停(抛 ExtApiError)"""
    too_large = ExtApiError(502, f'接口返回的内容超过 {size_text(limit)}', 'upstream_error')
    if resp.content_length is not None and resp.content_length > limit:
        raise too_large
    raw = bytearray()
    async for chunk in resp.content.iter_chunked(65536):
        raw.extend(chunk)
        if len(raw) > limit:
            raise too_large
    return bytes(raw)


@dataclass
class Reply:
    """一次 POST 的结果。拿到完整响应时 error 为空、timed_out 为 False;status 是对方的 HTTP 状态码(没收到时为 None)"""
    status: int | None = None
    raw: bytes = b''
    headers: object = field(default_factory=dict)
    error: str = ''
    timed_out: bool = False


def client_session():
    """调外部接口用的会话(超时在每个请求上单独设)"""
    return aiohttp.ClientSession()


async def post_json(session, url, payload, headers, *, timeout, tls_verify, limit):
    """把 payload 以 JSON POST 到 url:不跟随重定向,整个请求(连同读响应)最多 timeout 秒,响应最多读 limit 字节。
    不抛异常:连不上、证书校验失败、响应太大写进 error;超时只置 timed_out,由调用方按自己的口径说明。"""
    reply = Reply()
    try:
        async with session.post(url, json=payload, headers=headers, allow_redirects=False, ssl=bool(tls_verify),
                                timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            reply.status, reply.headers = resp.status, resp.headers
            reply.raw = await read_limited(resp, limit)
    except ExtApiError as exc:
        reply.error = exc.message
    except asyncio.TimeoutError:
        reply.timed_out = True
    except aiohttp.ClientConnectorCertificateError as exc:
        reply.error = f'HTTPS 证书校验失败({exc.certificate_error}),自签证书可以在配置里关闭「校验 HTTPS 证书」'
    except aiohttp.ClientError as exc:
        reply.error = f'无法访问接口:{str(exc) or type(exc).__name__}'
    return reply


# ---------------------------------------------------------------------------
# AI 服务回调
# ---------------------------------------------------------------------------
def trusted_principal(request):
    """只认内部 token + X-User-Name 解析出的真人账号(同 ai_api.authorize);权限由调用方按库里的用户再判断"""
    principal = request.get('principal')
    if principal is None or principal.via != 'internal' or not principal.is_human:
        raise ExtApiError(403, '缺少可信用户身份', 'forbidden')
    return principal
