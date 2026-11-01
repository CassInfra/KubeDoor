"""获取代码(Deployment 页操作菜单「获取代码」)。

用户点按钮 → master 经 agent 读 Deployment,取第一个容器的镜像 → 带上管理员配置的请求头,把
{k8s, namespace, deployment, image} POST 到配置的接口 → 接口返回 {web_url, download_url, commits_url},
页面弹窗显示成三个链接(新窗口打开)。接口约定也写在配置页「系统管理 → 获取代码接口」上,改了要一起改。

- 配置存 auth_settings(key=code_info),只有系统管理员能看和改(/api/admin/code-info*);
  请求头的值用 AI_ENCRYPTION_KEY 加密(同 LDAP 绑定密码),接口只回显名称,保存时值留空表示沿用原值
- 链接只接受 http:// 或 https:// 开头的地址,别的(例如 javascript:)按无效处理,不交给页面
- 调用带超时、不跟随重定向、响应最多读 1MB;每次获取记审计 code_info.get,改配置记 code_info.update
- AI 助手经 /api/ai/internal/code-info 用同一套逻辑(可以按 Pod 查,用 Pod 实际运行的镜像);
  AI 要下载源码分析(purpose=fetch)还要管理员打开 ai_download,记审计 code_info.fetch

配置校验、请求头加解密、调用外部接口这些和具体功能无关的部分在 ext_api.py(获取运行配置也用)。
"""

import asyncio
import json
import time

import yaml
from aiohttp import web
from loguru import logger

from auth import audit, principal_of
from auth.context import client_ip
from auth.store import store
from func_manager import ext_api
from func_manager.ext_api import ExtApiError, is_http_url

SETTING_KEY = 'code_info'
# ai_download:允许 AI 助手下载源码分析(源码会作为对话上下文发给模型服务商),默认关闭
DEFAULTS = {'enabled': False, 'url': '', 'headers': [], 'timeout': 10, 'tls_verify': True, 'ai_download': False}
FLAGS = ('ai_download',)
TIMEOUT_RANGE = (1, 60)
URL_EXAMPLE = 'https://code.example.com/api/code-info'
PAYLOAD_FIELDS = ('k8s', 'namespace', 'deployment', 'image')
LINK_FIELDS = ('web_url', 'download_url', 'commits_url')
AUDIT_GET = 'code_info.get'
AUDIT_FETCH = 'code_info.fetch'
AUDIT_UPDATE = 'code_info.update'
MAX_RESPONSE = 1024 * 1024
BODY_PREVIEW = 4096  # 测试时回显的原始响应长度
AGENT_TIMEOUT = 30

_call_agent = None  # register_routes 时注入 kubedoor-master 的 call_agent_api


class CodeInfoError(ExtApiError):
    """读 Deployment / Pod 的镜像失败"""


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
def normalize_config(raw, existing=None):
    """校验并规范化页面提交的配置,返回存库格式(请求头的值已加密);没传的字段沿用原值,不合法抛 ValueError。"""
    return ext_api.normalize_endpoint(raw, existing, DEFAULTS, timeout_range=TIMEOUT_RANGE,
                                      url_example=URL_EXAMPLE, flags=FLAGS)


def public_config(cfg):
    """给页面看的配置:请求头只有名称"""
    return ext_api.public_endpoint(cfg, DEFAULTS, FLAGS)


def _audit_detail(cfg):
    return ext_api.audit_detail(public_config(cfg))


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


def check_response(status, text, location=''):
    """按接口约定检查响应,返回 (links, invalid, error);error 为空表示可用"""
    empty = dict.fromkeys(LINK_FIELDS, '')
    if 300 <= status < 400:
        return empty, {}, ext_api.redirect_error(status, location)
    if not 200 <= status < 300:
        return empty, {}, ext_api.status_error(status, text)
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        return empty, {}, '接口返回的不是 JSON'
    if not isinstance(data, dict):
        return empty, {}, '接口返回的 JSON 不是对象,应该是 {"web_url": ..., "download_url": ..., "commits_url": ...}'
    links, invalid = parse_links(data)
    if not any(links.values()):
        return links, invalid, '接口返回里没有可用的链接(web_url、download_url、commits_url 都为空或者不是 http(s) 地址)'
    return links, invalid, ''


async def call_endpoint(cfg, payload):
    """把 payload POST 到配置的接口。不抛异常,返回 {ok, status, elapsed_ms, body, links, invalid, error}"""
    result = {'ok': False, 'status': None, 'elapsed_ms': 0, 'body': '',
              'links': dict.fromkeys(LINK_FIELDS, ''), 'invalid': {}, 'error': ''}
    started = time.monotonic()
    try:
        headers = ext_api.request_headers(cfg)
    except ExtApiError as exc:
        result['error'] = exc.message
    else:
        async with ext_api.client_session() as session:
            reply = await ext_api.post_json(session, cfg['url'], payload, headers, timeout=cfg['timeout'],
                                            tls_verify=cfg['tls_verify'], limit=MAX_RESPONSE)
        result['status'] = reply.status
        if reply.timed_out:
            result['error'] = f'请求超时({cfg["timeout"]} 秒内没有返回)'
        elif reply.error:
            result['error'] = reply.error
        else:
            text = reply.raw.decode('utf-8', errors='replace')
            result['body'] = text[:BODY_PREVIEW]
            result['links'], result['invalid'], result['error'] = check_response(
                reply.status, text, reply.headers.get('Location', ''))
    result['elapsed_ms'] = int((time.monotonic() - started) * 1000)
    result['ok'] = not result['error']
    return result


# ---------------------------------------------------------------------------
# 读镜像(Deployment 或 Pod)
# ---------------------------------------------------------------------------
def _dig(value, *keys):
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _first_image(pod_spec):
    containers = _dig(pod_spec, 'containers')
    if not isinstance(containers, list) or not containers or not isinstance(containers[0], dict):
        return ''
    image = containers[0].get('image')
    return image.strip() if isinstance(image, str) else ''


def first_container_image(doc):
    """Deployment 第一个容器的镜像"""
    return _first_image(_dig(doc, 'spec', 'template', 'spec'))


def pod_deployment(doc, pod):
    """Pod 所属的 Deployment:Deployment 建的 ReplicaSet 名称是「Deployment 名-pod-template-hash」;
    StatefulSet 等其他控制器管的 Pod 取控制器名称,没有控制器的取 Pod 名"""
    metadata = _dig(doc, 'metadata')
    metadata = metadata if isinstance(metadata, dict) else {}
    owners = [item for item in metadata.get('ownerReferences') or [] if isinstance(item, dict) and item.get('name')]
    owner = next((item for item in owners if item.get('controller')), owners[0] if owners else None)
    if owner is None:
        return pod
    name = str(owner['name'])
    labels = metadata.get('labels') if isinstance(metadata.get('labels'), dict) else {}
    template_hash = str(labels.get('pod-template-hash') or '')
    if owner.get('kind') == 'ReplicaSet' and template_hash and name.endswith('-' + template_hash):
        return name[:-len(template_hash) - 1]
    return name


async def read_resource(call_agent, env, namespace, kind, name):
    """经 agent 读 Deployment / Pod(编辑 YAML 用的接口,新旧 agent 都有),返回解析后的对象"""
    label = 'Deployment' if kind == 'deployment' else 'Pod'
    query = {'namespace': namespace, 'resource_type': kind, 'resource_name': name}
    try:
        response = await call_agent(env, '/api/agent/res/content', query=query, timeout=AGENT_TIMEOUT)
    except Exception as exc:
        raise CodeInfoError(503, f'无法从集群 {env} 读取 {label}:{exc}', 'agent_unavailable') from exc
    content = response.get('data') if isinstance(response, dict) else None
    if not isinstance(content, str) or response.get('success') is False or response.get('error'):
        error = response.get('error') if isinstance(response, dict) else ''
        raise CodeInfoError(502, f'读取 {label} {namespace}/{name} 失败:{error or "agent 返回的格式不正确"}',
                            'agent_error')
    try:
        return await asyncio.to_thread(yaml.safe_load, content)
    except yaml.YAMLError as exc:
        raise CodeInfoError(502, f'{label} {namespace}/{name} 的内容无法解析', 'agent_error') from exc


async def read_deployment_image(call_agent, env, namespace, deployment):
    """Deployment 第一个容器的镜像(同资源管控的口径)"""
    image = first_container_image(await read_resource(call_agent, env, namespace, 'deployment', deployment))
    if not image:
        raise CodeInfoError(502, f'Deployment {namespace}/{deployment} 里没有找到容器镜像', 'agent_error')
    return image


async def read_pod_image(call_agent, env, namespace, pod):
    """按 Pod 查,返回 (所属 Deployment, Pod 第一个容器的镜像)。
    用 Pod 实际运行的镜像:发布到一半时新旧 Pod 的镜像不一样,排障要看出问题的那个 Pod"""
    doc = await read_resource(call_agent, env, namespace, 'pod', pod)
    image = _first_image(_dig(doc, 'spec'))
    if not image:
        raise CodeInfoError(502, f'Pod {namespace}/{pod} 里没有找到容器镜像', 'agent_error')
    return pod_deployment(doc, pod), image


# ---------------------------------------------------------------------------
# 获取代码:读镜像 → 调接口 → 记审计(页面和 AI 共用)
# ---------------------------------------------------------------------------
NOT_CONFIGURED = '系统管理员还没有配置获取代码接口(系统管理 → 获取代码接口)'
AI_DOWNLOAD_DISABLED = ('系统管理员没有开启「允许 AI 下载并分析源码」(系统管理 → 获取代码接口),'
                        'AI 不能下载和分析源码;查镜像和代码链接不受影响')


async def _load_config():
    return {**DEFAULTS, **(await store.get_setting(SETTING_KEY, {}) or {})}


async def lookup(principal, ip, action, cfg, env, namespace, deployment='', pod='', source='', need_download=False):
    """按 Deployment 或 Pod 读镜像,调获取代码接口,记审计;返回 (payload, result)。
    读镜像失败记审计后抛 CodeInfoError;need_download 时接口没给 download_url 也算失败"""
    detail = {key: value for key, value in (('source', source), ('pod', pod)) if value}
    try:
        if pod:
            deployment, image = await read_pod_image(_call_agent, env, namespace, pod)
        else:
            image = await read_deployment_image(_call_agent, env, namespace, deployment)
    except CodeInfoError as exc:
        audit.record_bg(principal.username, action, env=env, target=f'{namespace}/{deployment or pod}', ip=ip,
                        success=False, detail={**detail, 'error': exc.message})
        raise
    payload = {'k8s': env, 'namespace': namespace, 'deployment': deployment, 'image': image}
    result = await call_endpoint(cfg, payload)
    if result['ok'] and need_download and not result['links']['download_url']:
        result['error'] = '接口没有返回下载链接(download_url),不能下载源码'
        result['ok'] = False
    detail.update(image=image, status=result['status'], elapsed_ms=result['elapsed_ms'])
    if result['error']:
        detail['error'] = result['error']
        logger.warning(f'获取代码失败 {env}/{namespace}/{deployment}: {result["error"]}')
    audit.record_bg(principal.username, action, env=env, target=f'{namespace}/{deployment}', ip=ip,
                    success=result['ok'], detail=detail)
    return payload, result


def _upstream_failed(payload, result):
    # 带上发出去的请求体,能看到用的是哪个镜像,方便排查
    return ext_api.fail(502, f'获取代码接口出错:{result["error"]}', 'upstream_error', data={'request': payload})


async def get_code_info(request):
    """Deployment 页「获取代码」:GET /api/code-info?env=&namespace=&deployment=(对该集群可读即可,policy 已校验)"""
    principal = principal_of(request)
    env = (request.query.get('env') or '').strip()
    namespace = (request.query.get('namespace') or '').strip()
    deployment = (request.query.get('deployment') or '').strip()
    if not ext_api.valid_k8s_name(namespace) or not ext_api.valid_k8s_name(deployment):
        return ext_api.fail(400, '命名空间或 Deployment 名称不正确')
    cfg = await _load_config()
    if not cfg['enabled'] or not cfg['url']:
        return ext_api.fail(400, NOT_CONFIGURED, 'not_configured')
    try:
        payload, result = await lookup(principal, client_ip(request), AUDIT_GET, cfg, env, namespace, deployment)
    except CodeInfoError as exc:
        return ext_api.fail(exc.status, exc.message, exc.code)
    if not result['ok']:
        return _upstream_failed(payload, result)
    return web.json_response({'success': True, 'data': {
        'request': payload, 'links': result['links'], 'invalid': result['invalid']}})


# ---------------------------------------------------------------------------
# AI 助手(kubedoor-ai 网关回调:内部 token + X-User-Name,policy 的 internal 规则)
# ---------------------------------------------------------------------------
def _ai_principal(request, env):
    """只认内部 token 带来的真人账号(同 ai_api.authorize),再按集群读权限校验(同页面:集群只读及以上)"""
    principal = ext_api.trusted_principal(request)
    if not principal.can_read(env):
        raise ExtApiError(403, f'没有集群 {env} 的访问权限', 'permission_denied')
    return principal


async def ai_code_info(request):
    """AI 查代码信息 / 取源码下载地址:GET /api/ai/internal/code-info?env=&namespace=&deployment=|pod=&purpose=info|fetch。
    deployment 和 pod 二选一,按 Pod 查用 Pod 实际运行的镜像;purpose=fetch 还要管理员打开 ai_download,
    返回的 download_url 由 kubedoor-ai 下载(模型拿不到也传不进下载地址),记审计 code_info.fetch"""
    env = (request.query.get('env') or '').strip()
    namespace = (request.query.get('namespace') or '').strip()
    deployment = (request.query.get('deployment') or '').strip()
    pod = (request.query.get('pod') or '').strip()
    purpose = request.query.get('purpose') or 'info'
    try:
        principal = _ai_principal(request, env)
    except ExtApiError as exc:
        return ext_api.fail(exc.status, exc.message, exc.code)
    if purpose not in ('info', 'fetch'):
        return ext_api.fail(400, 'purpose 只能是 info 或 fetch')
    if not ext_api.valid_k8s_name(namespace) or bool(deployment) == bool(pod) or not ext_api.valid_k8s_name(deployment or pod):
        return ext_api.fail(400, '需要命名空间,以及 Deployment 或 Pod 名称(二选一)')
    cfg = await _load_config()
    if not cfg['enabled'] or not cfg['url']:
        return ext_api.fail(400, NOT_CONFIGURED, 'not_configured')
    fetch = purpose == 'fetch'
    if fetch and not cfg['ai_download']:
        return ext_api.fail(403, AI_DOWNLOAD_DISABLED, 'ai_download_disabled')
    try:
        payload, result = await lookup(principal, client_ip(request), AUDIT_FETCH if fetch else AUDIT_GET, cfg, env,
                                       namespace, deployment, pod, source='ai', need_download=fetch)
    except CodeInfoError as exc:
        return ext_api.fail(exc.status, exc.message, exc.code)
    if not result['ok']:
        return _upstream_failed(payload, result)
    data = {'request': payload, 'links': result['links'], 'invalid': result['invalid']}
    if pod:
        data['pod'] = pod
    if fetch:
        # 下载链接多半和接口是同一个服务,沿用「校验 HTTPS 证书」
        data['tls_verify'] = bool(cfg['tls_verify'])
    return web.json_response({'success': True, 'data': data})


async def ai_code_access(request):
    """AI 搜索 / 读取已下载的源码前确认:用户能读这个集群,管理员仍然允许 AI 分析源码。
    GET /api/ai/internal/code-access?env=;停用功能或关掉 ai_download 后,已下载的源码也不能再用"""
    env = (request.query.get('env') or '').strip()
    try:
        _ai_principal(request, env)
    except ExtApiError as exc:
        return ext_api.fail(exc.status, exc.message, exc.code)
    cfg = await _load_config()
    if not cfg['enabled'] or not cfg['url']:
        return ext_api.fail(400, NOT_CONFIGURED, 'not_configured')
    if not cfg['ai_download']:
        return ext_api.fail(403, AI_DOWNLOAD_DISABLED, 'ai_download_disabled')
    return web.json_response({'success': True, 'data': {'ai_download': True}})


def _config_payload(row):
    return {**public_config(row['value'] if row else {}),
            'updated_by': row['updated_by'] if row else None,
            'updated_at': ext_api.iso(row['updated_at']) if row else None}


async def get_config(request):
    return web.json_response({'success': True, 'data': _config_payload(await store.get_setting_row(SETTING_KEY))})


async def put_config(request):
    principal = principal_of(request)
    existing = await store.get_setting(SETTING_KEY, {}) or {}
    try:
        cfg = normalize_config(await ext_api.read_json(request), existing)
    except ValueError as exc:
        return ext_api.fail(400, str(exc))
    row = await store.put_setting_row(SETTING_KEY, cfg, principal.username)
    audit.record_bg(principal.username, AUDIT_UPDATE, ip=client_ip(request), detail=_audit_detail(cfg))
    return web.json_response({'success': True, 'data': _config_payload(row)})


def _test_payload(raw):
    """测试入参都可以不填,没填的字段不放进请求体(实际调用时 4 个字段都会传)"""
    raw = raw if isinstance(raw, dict) else {}
    payload = {}
    for key in PAYLOAD_FIELDS:
        value = raw.get(key)
        value = '' if value is None else str(value).strip()
        if not value:
            continue
        if len(value) > 1024:
            raise ValueError(f'测试入参 {key} 太长')
        payload[key] = value
    return payload


async def run_config_test(request):
    """配置页的「测试」:用页面上的配置(不用先保存,请求头的值留空就用已保存的)和手填的入参调一次接口,
    返回状态码、耗时、原始响应和解析出的链接。"""
    body = await ext_api.read_json(request)
    existing = await store.get_setting(SETTING_KEY, {}) or {}
    raw = body.get('config')
    try:
        cfg = normalize_config(raw, existing) if isinstance(raw, dict) else {**DEFAULTS, **existing}
        if not cfg['url']:
            raise ValueError('请填写接口地址')
        payload = _test_payload(body.get('request'))
    except ValueError as exc:
        return ext_api.fail(400, str(exc))
    result = await call_endpoint(cfg, payload)
    return web.json_response({'success': True, 'data': {**result, 'request': payload}})


def register_routes(app, call_agent_api):
    global _call_agent
    _call_agent = call_agent_api
    app.router.add_get('/api/code-info', get_code_info)
    app.router.add_get('/api/admin/code-info', get_config)
    app.router.add_put('/api/admin/code-info', put_config)
    app.router.add_post('/api/admin/code-info/test', run_config_test)
    app.router.add_get('/api/ai/internal/code-info', ai_code_info)
    app.router.add_get('/api/ai/internal/code-access', ai_code_access)
