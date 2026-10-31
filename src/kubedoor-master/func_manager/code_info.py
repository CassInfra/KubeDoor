"""获取代码(Deployment 页操作菜单「获取代码」)。

用户点按钮 → master 经 agent 读 Deployment,取第一个容器的镜像 → 带上管理员配置的请求头,把
{k8s, namespace, deployment, image} POST 到配置的接口 → 接口返回 {web_url, download_url, commits_url},
页面弹窗显示成三个链接(新窗口打开)。接口约定也写在配置页「系统管理 → 获取代码接口」上,改了要一起改。

- 配置存 auth_settings(key=code_info),只有系统管理员能看和改(/api/admin/code-info*);
  请求头的值用 AI_ENCRYPTION_KEY 加密(同 LDAP 绑定密码),接口只回显名称,保存时值留空表示沿用原值
- 链接只接受 http:// 或 https:// 开头的地址,别的(例如 javascript:)按无效处理,不交给页面
- 调用带超时、不跟随重定向、响应最多读 1MB;每次获取记审计 code_info.get,改配置记 code_info.update
"""

import asyncio
import json
import re
import time
from datetime import datetime
from urllib.parse import urlsplit

import aiohttp
import yaml
from aiohttp import web
from loguru import logger

from auth import audit, principal_of
from auth.context import client_ip
from auth.crypto import decrypt_secret, encrypt_secret
from auth.store import store

SETTING_KEY = 'code_info'
DEFAULTS = {'enabled': False, 'url': '', 'headers': [], 'timeout': 10, 'tls_verify': True}
PAYLOAD_FIELDS = ('k8s', 'namespace', 'deployment', 'image')
LINK_FIELDS = ('web_url', 'download_url', 'commits_url')
AUDIT_GET = 'code_info.get'
AUDIT_UPDATE = 'code_info.update'
MAX_HEADERS = 10
MAX_HEADER_VALUE = 4096
MAX_URL = 8192  # 下载链接可能带很长的签名参数
MAX_RESPONSE = 1024 * 1024
BODY_PREVIEW = 4096  # 测试时回显的原始响应长度
AGENT_TIMEOUT = 30
USER_AGENT = 'KubeDoor'
# 这几个由 master 决定,不能在配置里覆盖
RESERVED_HEADERS = frozenset({'host', 'content-type', 'content-length', 'transfer-encoding', 'connection'})
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,100}")
_K8S_NAME = re.compile(r'[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?')

_call_agent = None  # register_routes 时注入 kubedoor-master 的 call_agent_api


class CodeInfoError(Exception):
    def __init__(self, status, message, code='bad_request'):
        super().__init__(message)
        self.status, self.message, self.code = status, message, code


def _fail(status, message, code='bad_request'):
    return web.json_response({'success': False, 'code': code, 'message': message, 'error': message}, status=status)


async def _json(request):
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _iso(value):
    return value.isoformat() if isinstance(value, datetime) else value


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
def _has_control_char(value):
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


def is_http_url(value):
    """http:// 或 https:// 开头、有主机名、没有空白和控制字符的地址"""
    if not isinstance(value, str) or not value or len(value) > MAX_URL:
        return False
    if any(ch.isspace() for ch in value) or _has_control_char(value):
        return False
    try:
        parts = urlsplit(value)
        return parts.scheme in ('http', 'https') and bool(parts.hostname)
    except ValueError:  # 例如方括号没闭合的 IPv6 地址
        return False


def _normalize_headers(items, saved):
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
        if len(value) > MAX_HEADER_VALUE or _has_control_char(value):
            raise ValueError(f'请求头 {name} 的值不能换行,最长 {MAX_HEADER_VALUE} 个字符')
        headers.append({'name': name, 'value': encrypt_secret(value)})
    return headers


def normalize_config(raw, existing=None):
    """校验并规范化页面提交的配置,返回存库格式(请求头的值已加密);没传的字段沿用原值,不合法抛 ValueError。"""
    if not isinstance(raw, dict):
        raise ValueError('配置格式不正确')
    existing = {**DEFAULTS, **(existing or {})}
    url = raw.get('url', existing['url'])
    cfg = {
        'enabled': bool(raw.get('enabled', existing['enabled'])),
        'url': '' if url is None else str(url).strip(),
        'tls_verify': bool(raw.get('tls_verify', existing['tls_verify'])),
    }
    try:
        cfg['timeout'] = int(raw.get('timeout', existing['timeout']))
    except (TypeError, ValueError):
        raise ValueError('超时时间必须是整数') from None
    if not 1 <= cfg['timeout'] <= 60:
        raise ValueError('超时时间取 1~60 秒')
    if cfg['enabled'] and not cfg['url']:
        raise ValueError('启用时必须填写接口地址')
    if cfg['url'] and not is_http_url(cfg['url']):
        raise ValueError('接口地址要以 http:// 或 https:// 开头,例如 https://code.example.com/api/code-info')
    saved = [item for item in existing['headers'] if isinstance(item, dict) and item.get('name')]
    cfg['headers'] = _normalize_headers(raw.get('headers'), saved)
    return cfg


def public_config(cfg):
    """给页面看的配置:请求头只有名称"""
    cfg = {**DEFAULTS, **(cfg or {})}
    return {
        'enabled': bool(cfg['enabled']),
        'url': cfg['url'],
        'headers': [{'name': item['name'], 'has_value': bool(item.get('value'))}
                    for item in cfg['headers'] if isinstance(item, dict) and item.get('name')],
        'timeout': cfg['timeout'],
        'tls_verify': bool(cfg['tls_verify']),
    }


def _audit_detail(cfg):
    data = public_config(cfg)
    data['headers'] = [item['name'] for item in data['headers']]
    return data


def request_headers(cfg):
    """解密后的请求头;解不开(AI_ENCRYPTION_KEY 换过)抛 CodeInfoError"""
    headers = {}
    for item in cfg.get('headers') or []:
        try:
            headers[item['name']] = decrypt_secret(item.get('value') or '')
        except ValueError as exc:
            raise CodeInfoError(500, f'请求头 {item["name"]} 的值无法解密({exc}),请系统管理员重新填写',
                                'decrypt_failed') from exc
    if not any(name.lower() == 'user-agent' for name in headers):
        headers['User-Agent'] = USER_AGENT
    return headers


# ---------------------------------------------------------------------------
# 调用接口
# ---------------------------------------------------------------------------
def parse_links(data):
    """从接口返回里取三个链接,返回 (links, invalid):没返回或无效的字段在 links 里是空字符串,invalid 记下无效的原值"""
    links, invalid = {}, {}
    for key in LINK_FIELDS:
        value = data.get(key)
        text = value.strip() if isinstance(value, str) else value
        if text is None or text == '':
            links[key] = ''
        elif is_http_url(text):
            links[key] = text
        else:
            links[key] = ''
            invalid[key] = str(value)[:200]
    return links, invalid


def _error_reason(text):
    """非 2xx 时对方给的原因:JSON 里的 message / error / msg / detail,或者开头一段纯文本(HTML 错误页不要)"""
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if isinstance(data, dict):
        for key in ('message', 'error', 'msg', 'detail'):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:300]
        return ''
    text = text.strip()
    return '' if text.startswith('<') else text[:200]


def check_response(status, text, location=''):
    """按接口约定检查响应,返回 (links, invalid, error);error 为空表示可用"""
    empty = dict.fromkeys(LINK_FIELDS, '')
    if 300 <= status < 400:
        target = f' → {location}' if location else ''
        return empty, {}, f'接口返回了重定向(HTTP {status}{target}),不会跟随,请直接配置最终地址'
    if not 200 <= status < 300:
        reason = _error_reason(text)
        return empty, {}, f'接口返回 HTTP {status}' + (f':{reason}' if reason else '')
    try:
        data = json.loads(text)
    except ValueError:
        return empty, {}, '接口返回的不是 JSON'
    if not isinstance(data, dict):
        return empty, {}, '接口返回的 JSON 不是对象,应该是 {"web_url": ..., "download_url": ..., "commits_url": ...}'
    links, invalid = parse_links(data)
    if not any(links.values()):
        return links, invalid, '接口返回里没有可用的链接(web_url、download_url、commits_url 都为空或者不是 http(s) 地址)'
    return links, invalid, ''


async def _read_limited(resp):
    raw = bytearray()
    async for chunk in resp.content.iter_chunked(65536):
        raw.extend(chunk)
        if len(raw) > MAX_RESPONSE:
            raise CodeInfoError(502, f'接口返回的内容超过 {MAX_RESPONSE // 1024} KB', 'upstream_error')
    return bytes(raw)


async def call_endpoint(cfg, payload):
    """把 payload POST 到配置的接口。不抛异常,返回 {ok, status, elapsed_ms, body, links, invalid, error}"""
    result = {'ok': False, 'status': None, 'elapsed_ms': 0, 'body': '',
              'links': dict.fromkeys(LINK_FIELDS, ''), 'invalid': {}, 'error': ''}
    started = time.monotonic()
    try:
        headers = request_headers(cfg)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=cfg['timeout'])) as session:
            async with session.post(cfg['url'], json=payload, headers=headers, allow_redirects=False,
                                    ssl=bool(cfg['tls_verify'])) as resp:
                result['status'] = resp.status
                raw = await _read_limited(resp)
                location = resp.headers.get('Location', '')
        text = raw.decode('utf-8', errors='replace')
        result['body'] = text[:BODY_PREVIEW]
        result['links'], result['invalid'], result['error'] = check_response(result['status'], text, location)
    except CodeInfoError as exc:
        result['error'] = exc.message
    except asyncio.TimeoutError:
        result['error'] = f'请求超时({cfg["timeout"]} 秒内没有返回)'
    except aiohttp.ClientConnectorCertificateError as exc:
        result['error'] = f'HTTPS 证书校验失败({exc.certificate_error}),自签证书可以在配置里关闭「校验 HTTPS 证书」'
    except aiohttp.ClientError as exc:
        result['error'] = f'无法访问接口:{str(exc) or type(exc).__name__}'
    result['elapsed_ms'] = int((time.monotonic() - started) * 1000)
    result['ok'] = not result['error']
    return result


# ---------------------------------------------------------------------------
# 读 Deployment 的镜像
# ---------------------------------------------------------------------------
def _dig(value, *keys):
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def first_container_image(doc):
    containers = _dig(doc, 'spec', 'template', 'spec', 'containers')
    if not isinstance(containers, list) or not containers or not isinstance(containers[0], dict):
        return ''
    image = containers[0].get('image')
    return image.strip() if isinstance(image, str) else ''


async def read_deployment_image(call_agent, env, namespace, deployment):
    """经 agent 读 Deployment(编辑 YAML 用的接口,新旧 agent 都有),返回第一个容器的镜像(同资源管控的口径)"""
    query = {'namespace': namespace, 'resource_type': 'deployment', 'resource_name': deployment}
    try:
        response = await call_agent(env, '/api/agent/res/content', query=query, timeout=AGENT_TIMEOUT)
    except Exception as exc:
        raise CodeInfoError(503, f'无法从集群 {env} 读取 Deployment:{exc}', 'agent_unavailable') from exc
    content = response.get('data') if isinstance(response, dict) else None
    if not isinstance(content, str) or response.get('success') is False or response.get('error'):
        error = response.get('error') if isinstance(response, dict) else ''
        raise CodeInfoError(502, f'读取 Deployment {namespace}/{deployment} 失败:{error or "agent 返回的格式不正确"}',
                            'agent_error')
    try:
        doc = await asyncio.to_thread(yaml.safe_load, content)
    except yaml.YAMLError as exc:
        raise CodeInfoError(502, f'Deployment {namespace}/{deployment} 的内容无法解析', 'agent_error') from exc
    image = first_container_image(doc)
    if not image:
        raise CodeInfoError(502, f'Deployment {namespace}/{deployment} 里没有找到容器镜像', 'agent_error')
    return image


# ---------------------------------------------------------------------------
# 接口
# ---------------------------------------------------------------------------
def _valid_name(value):
    return len(value) <= 253 and _K8S_NAME.fullmatch(value) is not None


async def get_code_info(request):
    """Deployment 页「获取代码」:GET /api/code-info?env=&namespace=&deployment=(对该集群可读即可,policy 已校验)"""
    principal = principal_of(request)
    env = (request.query.get('env') or '').strip()
    namespace = (request.query.get('namespace') or '').strip()
    deployment = (request.query.get('deployment') or '').strip()
    if not _valid_name(namespace) or not _valid_name(deployment):
        return _fail(400, '命名空间或 Deployment 名称不正确')
    cfg = {**DEFAULTS, **(await store.get_setting(SETTING_KEY, {}) or {})}
    if not cfg['enabled'] or not cfg['url']:
        return _fail(400, '系统管理员还没有配置获取代码接口(系统管理 → 获取代码接口)', 'not_configured')

    target, ip = f'{namespace}/{deployment}', client_ip(request)
    try:
        image = await read_deployment_image(_call_agent, env, namespace, deployment)
    except CodeInfoError as exc:
        audit.record_bg(principal.username, AUDIT_GET, env=env, target=target, ip=ip, success=False,
                        detail={'error': exc.message})
        return _fail(exc.status, exc.message, exc.code)

    payload = {'k8s': env, 'namespace': namespace, 'deployment': deployment, 'image': image}
    result = await call_endpoint(cfg, payload)
    detail = {'image': image, 'status': result['status'], 'elapsed_ms': result['elapsed_ms']}
    if result['error']:
        detail['error'] = result['error']
        logger.warning(f'获取代码失败 {env}/{target}: {result["error"]}')
    audit.record_bg(principal.username, AUDIT_GET, env=env, target=target, ip=ip, success=result['ok'], detail=detail)
    if not result['ok']:
        # 带上发出去的请求体,页面上能看到用的是哪个镜像,方便排查
        message = f'获取代码接口出错:{result["error"]}'
        return web.json_response({'success': False, 'code': 'upstream_error', 'message': message, 'error': message,
                                  'data': {'request': payload}}, status=502)
    return web.json_response({'success': True, 'data': {
        'request': payload, 'links': result['links'], 'invalid': result['invalid']}})


def _config_payload(row):
    return {**public_config(row['value'] if row else {}),
            'updated_by': row['updated_by'] if row else None,
            'updated_at': _iso(row['updated_at']) if row else None}


async def get_config(request):
    return web.json_response({'success': True, 'data': _config_payload(await store.get_setting_row(SETTING_KEY))})


async def put_config(request):
    principal = principal_of(request)
    existing = await store.get_setting(SETTING_KEY, {}) or {}
    try:
        cfg = normalize_config(await _json(request), existing)
    except ValueError as exc:
        return _fail(400, str(exc))
    row = await store.put_setting_row(SETTING_KEY, cfg, principal.username)
    audit.record_bg(principal.username, AUDIT_UPDATE, ip=client_ip(request), detail=_audit_detail(cfg))
    return web.json_response({'success': True, 'data': _config_payload(row)})


def _test_payload(raw):
    raw = raw if isinstance(raw, dict) else {}
    payload = {}
    for key in PAYLOAD_FIELDS:
        value = raw.get(key)
        value = '' if value is None else str(value).strip()
        if not value:
            raise ValueError(f'请填写测试入参 {key}')
        if len(value) > 1024:
            raise ValueError(f'测试入参 {key} 太长')
        payload[key] = value
    return payload


async def run_config_test(request):
    """配置页的「测试」:用页面上的配置(不用先保存,请求头的值留空就用已保存的)和手填的入参调一次接口,
    返回状态码、耗时、原始响应和解析出的链接。"""
    body = await _json(request)
    existing = await store.get_setting(SETTING_KEY, {}) or {}
    raw = body.get('config')
    try:
        cfg = normalize_config(raw, existing) if isinstance(raw, dict) else {**DEFAULTS, **existing}
        if not cfg['url']:
            raise ValueError('请填写接口地址')
        payload = _test_payload(body.get('request'))
    except ValueError as exc:
        return _fail(400, str(exc))
    result = await call_endpoint(cfg, payload)
    return web.json_response({'success': True, 'data': {**result, 'request': payload}})


def register_routes(app, call_agent_api):
    global _call_agent
    _call_agent = call_agent_api
    app.router.add_get('/api/code-info', get_code_info)
    app.router.add_get('/api/admin/code-info', get_config)
    app.router.add_put('/api/admin/code-info', put_config)
    app.router.add_post('/api/admin/code-info/test', run_config_test)
