"""获取运行配置(Deployment / Pod 页操作菜单「获取运行配置」,AI 助手的 runtime_config)。

用户点按钮 → master 带上管理员配置的请求头,把 {k8s, namespace, deployment 或 pod, 可选的 container / keys /
detail / refresh, wait} POST 到配置的接口(用户自己实现) → 接口返回这个 Pod 实际运行的配置(JSON 对象,
含明文密码),页面原样展示。接口约定也写在配置页「系统管理 → 获取运行配置接口」上,改了要一起改。

- 要「查看运行配置」权限(config.view,默认读写及以上);配置存 auth_settings(key=runtime_config),只有系统管理员
  能看和改(/api/admin/runtime-config*);请求头的值加密、只回显名称(同获取代码,见 ext_api.py)
- 接口还在采集时返回 202(可带 Retry-After 秒数,缺省 3,取 1~10):等一会儿用同样的请求体重发,直到配置的
  总时长用完(504 timeout);每次都带 wait = 本次还能等的秒数,接口可以据此多等一会儿再回 202
- 不跟随重定向,响应最多 10MB;几 MB 的 JSON 放线程里解析和序列化,不阻塞 event loop
- 每次获取记审计 runtime_config.get(只记请求条件和结果状态,不记任何配置值),改配置记 runtime_config.update;
  配置值、请求头的值不写日志
- AI 助手经 /api/ai/internal/runtime-config 用同一套逻辑:按库里的权限查 config.view,还要管理员打开 ai_enabled;
  执行数据查询前换回密码(purpose=resolve)还要 data.query(从 kubedoor-ai 本地发起的查询不经过 agent-execute,
  master 只能在这一步复核);返回原文(只在集群内部网络里传),密码换成占位符在 kubedoor-ai 里做
"""

import asyncio
import json
import time

from aiohttp import web
from loguru import logger

from auth import audit, permissions, principal_of
from auth.context import client_ip
from auth.policy import need_op
from auth.store import store
from func_manager import ext_api
from func_manager.ext_api import ExtApiError, has_control_char

SETTING_KEY = 'runtime_config'
# ai_enabled:允许 AI 助手获取运行配置(AI 看到的密码、密钥是占位符),默认关闭
DEFAULTS = {'enabled': False, 'url': '', 'headers': [], 'timeout': 60, 'tls_verify': True, 'ai_enabled': False}
FLAGS = ('ai_enabled',)
TIMEOUT_RANGE = (5, 120)  # 一次获取的总时长(秒,含 202 重试)
URL_EXAMPLE = 'https://config.example.com/pod-config'
OP = 'config.view'
DATA_OP = 'data.query'
AUDIT_GET = 'runtime_config.get'
AUDIT_UPDATE = 'runtime_config.update'
# AI 的 purpose → 要查的操作:view 是查看配置(kubedoor-ai 打码后给模型);resolve 是执行数据查询前把占位符换回原文,
# 还要「AI 查询数据库 / Redis / ES」
PURPOSE_OPS = {'view': (OP,), 'resolve': (OP, DATA_OP)}
TARGET_FIELDS = ('pod', 'deployment', 'container', 'keys')  # 审计里记的请求条件
MAX_RESPONSE = 10 * 1024 * 1024
LARGE_JSON = 256 * 1024  # 响应超过这么大就放到线程里解析和序列化
BODY_PREVIEW = 64 * 1024  # 失败时回显的原始响应长度
MAX_KEYS = 500
MAX_TEST_FIELD = 1024
MAX_WAIT = 120
NETWORK_SLACK = 5  # 每次请求的超时 = 剩余时长 + 网络余量(接口可以按 wait 等满了再回 202)
MIN_WAIT = 1  # 202 之后至少还剩这么多秒才重发
DEFAULT_RETRY_AFTER = 3
RETRY_AFTER_RANGE = (1, 10)
NOT_CONFIGURED = '系统管理员还没有配置获取运行配置接口(系统管理 → 获取运行配置接口)'
AI_DISABLED = '系统管理员没有开启「允许 AI 获取运行配置」(系统管理 → 获取运行配置接口)'

# 计时和等待(测试里换成假时钟)
_clock = time.monotonic
_sleep = asyncio.sleep


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


async def _load_config():
    return {**DEFAULTS, **(await store.get_setting(SETTING_KEY, {}) or {})}


async def _load_enabled_config(need_ai=False):
    """获取运行配置时读配置:读库失败、没启用、AI 没打开开关都抛 ExtApiError(返回 JSON,AI 那边按 JSON 解析)"""
    try:
        cfg = await _load_config()
    except Exception as exc:
        logger.error(f'读取获取运行配置接口的配置失败: {exc}')
        raise ExtApiError(503, '读取获取运行配置接口的配置失败,请稍后重试', 'config_unavailable') from exc
    if not cfg['enabled'] or not cfg['url']:
        raise ExtApiError(400, NOT_CONFIGURED, 'not_configured')
    if need_ai and not cfg['ai_enabled']:
        raise ExtApiError(403, AI_DISABLED, 'ai_config_disabled')
    return cfg


# ---------------------------------------------------------------------------
# 请求体
# ---------------------------------------------------------------------------
def _flag(value):
    """detail=1 / refresh=true 这类开关:查询参数是字符串,测试接口的 JSON 里是布尔"""
    return value is not None and str(value).strip().lower() in ('1', 'true', 'yes', 'on')


def _check_keys(keys):
    if len(keys) > MAX_KEYS:
        raise ValueError(f'配置名过滤(keys)最长 {MAX_KEYS} 个字符')
    if has_control_char(keys):
        raise ValueError('配置名过滤(keys)不能有换行等控制字符')


def _with_options(payload, keys, detail, refresh):
    """可选字段只在有值时带(正则 keys 不做语法校验,交给接口)"""
    if keys:
        payload['keys'] = keys
    if detail:
        payload['detail'] = True
    if refresh:
        payload['refresh'] = True
    return payload


def build_payload(env, query):
    """页面 / AI 的查询参数 → 发给接口的请求体(不含 wait,每次请求现算);不合法抛 ValueError"""
    namespace, deployment, pod, container, keys = (
        (query.get(key) or '').strip() for key in ('namespace', 'deployment', 'pod', 'container', 'keys'))
    if not ext_api.valid_k8s_name(namespace):
        raise ValueError('命名空间名称不正确')
    if bool(deployment) == bool(pod):
        raise ValueError('需要 Deployment 或 Pod 名称(二选一)')
    kind, label, name = ('deployment', 'Deployment', deployment) if deployment else ('pod', 'Pod', pod)
    if not ext_api.valid_k8s_name(name):
        raise ValueError(f'{label} 名称不正确')
    if container and not ext_api.valid_k8s_name(container):
        raise ValueError('容器名称不正确')
    _check_keys(keys)
    payload = {'k8s': env, 'namespace': namespace, kind: name}
    if container:
        payload['container'] = container
    return _with_options(payload, keys, _flag(query.get('detail')), _flag(query.get('refresh')))


def _test_payload(raw):
    """配置页「测试」的入参都可以不填,没填的字段不放进请求体;名称格式不校验(正好看看接口自己怎么报错)"""
    raw = raw if isinstance(raw, dict) else {}
    payload = {}
    for key in ('k8s', 'namespace', 'deployment', 'pod', 'container'):
        value = raw.get(key)
        value = '' if value is None else str(value).strip()
        if not value:
            continue
        if len(value) > MAX_TEST_FIELD or has_control_char(value):
            raise ValueError(f'测试入参 {key} 不正确(不能换行,最长 {MAX_TEST_FIELD} 个字符)')
        payload[key] = value
    keys = raw.get('keys')
    keys = '' if keys is None else str(keys).strip()
    _check_keys(keys)
    return _with_options(payload, keys, _flag(raw.get('detail')), _flag(raw.get('refresh')))


# ---------------------------------------------------------------------------
# 调用接口
# ---------------------------------------------------------------------------
def retry_after(value):
    """202 的 Retry-After(秒):没有或不是数字(包括 HTTP 日期格式)按 3 秒,限制在 1~10 秒"""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return DEFAULT_RETRY_AFTER
    if seconds != seconds:  # NaN
        return DEFAULT_RETRY_AFTER
    low, high = RETRY_AFTER_RANGE
    return min(max(seconds, low), high)


def _wait_seconds(remaining):
    """本次还能等的秒数(1~120 的整数),随请求发给接口"""
    return max(1, min(MAX_WAIT, int(remaining + 0.5)))


def parse_reply(status, raw, location=''):
    """按接口约定检查最终的响应,返回 (config, error):成功时 config 是接口返回的 JSON 对象、error 为空"""
    if 300 <= status < 400:
        return None, ext_api.redirect_error(status, location)
    text = raw.decode('utf-8', errors='replace')
    if not 200 <= status < 300:
        return None, ext_api.status_error(status, text)
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        return None, '接口返回的不是 JSON'
    if not isinstance(data, dict):
        return None, '接口返回的 JSON 不是对象'
    return data, ''


def _preview(raw):
    return raw[:BODY_PREVIEW].decode('utf-8', errors='replace')


def _finish(result, started, error='', code='upstream_error'):
    result.update(ok=not error, error=error, code=code if error else '',
                  elapsed_ms=int((_clock() - started) * 1000))
    return result


async def call_endpoint(cfg, payload):
    """把 payload(每次加上 wait)POST 到配置的接口;202 时按 Retry-After 等待后重发,直到 cfg['timeout'] 秒用完。

    不抛异常,返回 {ok, code, error, status, elapsed_ms, attempts, config, body, size}:code 是失败类型
    (upstream_error / timeout),config 是成功时接口返回的对象,body 是失败时的原始响应(最多 64KB),
    size 是最后一个响应的字节数(决定要不要在线程里序列化)。"""
    result = {'ok': False, 'code': '', 'error': '', 'status': None, 'elapsed_ms': 0, 'attempts': 0,
              'config': None, 'body': '', 'size': 0}
    started = _clock()
    deadline = started + cfg['timeout']
    try:
        headers = ext_api.request_headers(cfg)
    except ExtApiError as exc:
        return _finish(result, started, exc.message)
    async with ext_api.client_session() as session:
        while True:
            remaining = max(deadline - _clock(), 0)
            result['attempts'] += 1
            reply = await ext_api.post_json(
                session, cfg['url'], {**payload, 'wait': _wait_seconds(remaining)}, headers,
                timeout=remaining + NETWORK_SLACK, tls_verify=cfg['tls_verify'], limit=MAX_RESPONSE)
            result['status'] = reply.status
            if reply.timed_out:
                return _finish(result, started, f'接口在 {cfg["timeout"]} 秒内没有返回,请稍后再试', 'timeout')
            if reply.error:
                return _finish(result, started, reply.error)
            if reply.status != 202:
                break
            delay = retry_after(reply.headers.get('Retry-After'))
            if deadline - _clock() < delay + MIN_WAIT:
                result['body'] = _preview(reply.raw)
                return _finish(result, started, f'接口在 {cfg["timeout"]} 秒内没有采集完,请稍后再试', 'timeout')
            await _sleep(delay)
    result['size'] = len(reply.raw)
    location = reply.headers.get('Location', '')
    if result['size'] > LARGE_JSON:
        config, error = await asyncio.to_thread(parse_reply, reply.status, reply.raw, location)
    else:
        config, error = parse_reply(reply.status, reply.raw, location)
    if error:
        result['body'] = _preview(reply.raw)
        return _finish(result, started, error)
    result['config'] = config
    return _finish(result, started)


# ---------------------------------------------------------------------------
# 获取运行配置:调接口 → 记审计(页面和 AI 共用)
# ---------------------------------------------------------------------------
def _target(payload):
    return f'{payload["namespace"]}/{payload.get("deployment") or payload.get("pod")}'


async def lookup(principal, ip, cfg, env, payload, extra=None):
    """调接口并记审计 runtime_config.get,返回 call_endpoint 的结果。审计和日志只有请求条件和结果状态,没有配置值"""
    result = await call_endpoint(cfg, payload)
    detail = dict(extra or {})
    detail.update({key: payload[key] for key in TARGET_FIELDS if payload.get(key)})
    detail.update(status=result['status'], elapsed_ms=result['elapsed_ms'], attempts=result['attempts'])
    if result['error']:
        detail['error'] = result['error']
        logger.warning(f'获取运行配置失败 {env}/{_target(payload)}: {result["error"]}')
    audit.record_bg(principal.username, AUDIT_GET, env=env, target=_target(payload), ip=ip,
                    success=result['ok'], detail=detail)
    return result


def _failed(payload, result):
    """接口出错 502 / 超时 504,带上发出去的请求体(不含 wait),方便排查"""
    if result['code'] == 'timeout':
        return ext_api.fail(504, f'获取运行配置超时:{result["error"]}', 'timeout', data={'request': payload})
    return ext_api.fail(502, f'获取运行配置接口出错:{result["error"]}', 'upstream_error', data={'request': payload})


async def _succeeded(payload, result):
    data = {'request': payload, 'config': result['config'],
            'elapsed_ms': result['elapsed_ms'], 'attempts': result['attempts']}
    return await ext_api.json_response({'success': True, 'data': data}, large=result['size'] > LARGE_JSON)


async def get_runtime_config(request):
    """「获取运行配置」:GET /api/runtime-config?env=&namespace=&deployment=|pod=&container=&keys=&detail=1&refresh=1
    (policy 已校验该集群的「查看运行配置」)"""
    principal = principal_of(request)
    env = (request.query.get('env') or '').strip()
    try:
        payload = build_payload(env, request.query)
    except ValueError as exc:
        return ext_api.fail(400, str(exc))
    try:
        cfg = await _load_enabled_config()
    except ExtApiError as exc:
        return ext_api.fail(exc.status, exc.message, exc.code)
    result = await lookup(principal, client_ip(request), cfg, env, payload)
    if not result['ok']:
        return _failed(payload, result)
    return await _succeeded(payload, result)


# ---------------------------------------------------------------------------
# AI 助手(kubedoor-ai 网关回调:内部 token + X-User-Name,policy 的 internal 规则)
# ---------------------------------------------------------------------------
async def _ai_principal(request, env, purpose):
    """只认内部 token 带来的真人账号,再按库里的权限查该集群的「查看运行配置」(同网页);purpose=resolve 还要
    「AI 查询数据库 / Redis / ES」(从 kubedoor-ai 本地发起的查询不经过 agent-execute,只能在这里复核)。不通过抛 ExtApiError"""
    principal = ext_api.trusted_principal(request)
    if not env:
        raise ExtApiError(400, '缺少 K8S 集群参数')
    if purpose not in PURPOSE_OPS:
        raise ExtApiError(400, 'purpose 只能是 view 或 resolve')
    try:
        policy = await permissions.load_policy()  # 鉴权中间件已刷新过,没过期不查库
    except Exception as exc:
        logger.error(f'读取权限与审批配置失败: {exc}')
        raise ExtApiError(503, '读取权限配置失败,请稍后重试', 'policy_unavailable') from exc
    for op in PURPOSE_OPS[purpose]:
        if not principal.can(op, env, policy):
            raise ExtApiError(403, need_op(principal, op, env).message, 'permission_denied')
    return principal


async def ai_runtime_config(request):
    """AI 获取运行配置:GET /api/ai/internal/runtime-config?env=&namespace=&deployment=|pod=&container=&keys=
    &detail=1&refresh=1&purpose=view|resolve。purpose=view 是给模型看(kubedoor-ai 打码),resolve 是执行数据查询前
    把占位符换回原文(还要 data.query);两种都返回接口的原文,审计里分开记。错误一律返回 JSON"""
    env = (request.query.get('env') or '').strip()
    purpose = request.query.get('purpose') or 'view'
    try:
        principal = await _ai_principal(request, env, purpose)
        try:
            payload = build_payload(env, request.query)
        except ValueError as exc:
            raise ExtApiError(400, str(exc)) from None
        cfg = await _load_enabled_config(need_ai=True)
    except ExtApiError as exc:
        return ext_api.fail(exc.status, exc.message, exc.code)
    result = await lookup(principal, client_ip(request), cfg, env, payload, {'source': 'ai', 'purpose': purpose})
    if not result['ok']:
        return _failed(payload, result)
    return await _succeeded(payload, result)


# ---------------------------------------------------------------------------
# 系统管理:配置和测试(/api/admin/*,policy 已校验系统管理员)
# ---------------------------------------------------------------------------
def _config_payload(row):
    return {**public_config(row['value'] if row else {}),
            'updated_by': row['updated_by'] if row else None,
            'updated_at': ext_api.iso(row['updated_at']) if row else None}


async def get_config(request):
    return web.json_response({'success': True, 'data': _config_payload(await store.get_setting_row(SETTING_KEY))})


async def put_config(request):
    """保存配置:只改传了的字段(右上角开关只传 enabled / ai_enabled);审计里请求头只记名称"""
    principal = principal_of(request)
    existing = await store.get_setting(SETTING_KEY, {}) or {}
    try:
        cfg = normalize_config(await ext_api.read_json(request), existing)
    except ValueError as exc:
        return ext_api.fail(400, str(exc))
    row = await store.put_setting_row(SETTING_KEY, cfg, principal.username)
    audit.record_bg(principal.username, AUDIT_UPDATE, ip=client_ip(request),
                    detail=ext_api.audit_detail(public_config(cfg)))
    return web.json_response({'success': True, 'data': _config_payload(row)})


async def run_config_test(request):
    """配置页的「测试」:用页面上的配置(不用先保存,请求头的值留空就用已保存的)和手填的入参调一次接口(同样带 wait、
    同样按 202 重试),返回状态码、耗时、重试次数、发出的请求体、返回的对象或原始响应。不保存。"""
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
    data = {key: result[key] for key in ('ok', 'status', 'elapsed_ms', 'attempts', 'error', 'config', 'body')}
    return await ext_api.json_response({'success': True, 'data': {**data, 'request': payload}},
                                       large=result['size'] > LARGE_JSON)


def register_routes(app):
    app.router.add_get('/api/runtime-config', get_runtime_config)
    app.router.add_get('/api/admin/runtime-config', get_config)
    app.router.add_put('/api/admin/runtime-config', put_config)
    app.router.add_post('/api/admin/runtime-config/test', run_config_test)
    app.router.add_get('/api/ai/internal/runtime-config', ai_runtime_config)
