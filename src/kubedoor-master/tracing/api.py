"""链路追踪的接口（约定 §3、§4）。

- 系统管理（policy 的 /api/admin/.+ 规则，只有系统管理员）：
  GET/PUT /api/admin/tracing、POST /api/admin/tracing/test、POST /api/admin/tracing/analyze
- AI 内部接口（policy 的 /api/ai/internal/.+ 规则，内部 token + X-User-Name）：
  POST /api/ai/internal/tracing/{op}?env=，op ∈ services / operations / search / stats / trace / spans / fields；
  按库里的用户权限要求该集群只读及以上；配置页的「启用」开关关着时返回 not_configured。不记审计（AI 动作表已有记录）
- 日志不记请求头的值、密码和链路内容
"""

import asyncio
import json
import traceback

from aiohttp import web
from loguru import logger

from auth import audit, principal_of
from auth.context import client_ip
from auth.store import store
from func_manager import ext_api
from func_manager.ext_api import ExtApiError

from . import clickhouse, jaeger, ops, search
from . import config as tracing_config
from . import fields as field_map
from .common import TracingError, invalid
from .context import Ctx
from .mask import BUILTIN

AUDIT_UPDATE = 'tracing.update'
MAX_TRACE_JSON = 5 * 1024 * 1024
MAX_ANALYZE_BODY = 6 * 1024 * 1024  # 识别字段的请求体（JSON 转义后比粘贴的内容大一些）
TRACE_JSON_TOO_LARGE = 'Trace JSON 最大 5MB'
OPS = {
    'services': ops.services,
    'operations': ops.operations,
    'search': search.search,
    'stats': search.stats,
    'trace': ops.trace,
    'spans': ops.spans_op,
    'fields': ops.fields_op,
}
LARGE_OPS = frozenset({'search', 'trace', 'spans', 'fields'})


def _fail(exc):
    return ext_api.fail(exc.status, exc.message, exc.code, getattr(exc, 'data', None))


async def read_body(request):
    """请求体 JSON 对象；没有请求体当 {}。
    用 body_exists 判断有没有请求体，不能用 can_read_body：它看的是请求体的流读完没有，写操作（测试连接、保存配置）的
    鉴权中间件为了取审计的集群会先 request.json() 读一遍，之后它就是 False（读到的内容 aiohttp 有缓存，再读照样拿得到）"""
    if not request.body_exists:
        return {}
    try:
        body = await request.json()
    except web.HTTPRequestEntityTooLarge:
        limit = getattr(request, '_client_max_size', 0) or 0
        size = f'（目前上限 {limit // (1024 * 1024)} MB）' if limit >= 1024 * 1024 else ''
        raise TracingError(413, f'请求体太大{size}', 'invalid_request') from None
    except Exception:
        raise invalid('请求体要是 JSON 对象') from None
    if body is None:
        return {}
    if not isinstance(body, dict):
        raise invalid('请求体要是 JSON 对象')
    return body


async def read_large_body(request, limit):
    """按流读请求体（不受全局 client_max_size 的 2MB 限制），超过 limit 返回 413；大的 JSON 在线程里解析。
    policy 里这个接口不算写操作，鉴权中间件不会先读请求体"""
    too_large = TracingError(413, TRACE_JSON_TOO_LARGE, 'invalid_request')
    raw = getattr(request, '_read_bytes', None)  # 已经被读过就用读好的
    if raw is None:
        if request.content_length is not None and request.content_length > limit:
            raise too_large
        buffer = bytearray()
        async for chunk in request.content.iter_chunked(65536):
            buffer.extend(chunk)
            if len(buffer) > limit:
                raise too_large
        raw = bytes(buffer)
    elif len(raw) > limit:
        raise too_large
    if not raw.strip():
        return {}

    def parse():
        return json.loads(raw.decode('utf-8'))

    try:
        body = await asyncio.to_thread(parse) if len(raw) > 1024 * 1024 else parse()
    except (ValueError, RecursionError):
        raise invalid('请求体要是 JSON 对象') from None
    if not isinstance(body, dict):
        raise invalid('请求体要是 JSON 对象')
    return body


# ---------------------------------------------------------------------------
# AI 内部接口
# ---------------------------------------------------------------------------
async def ai_op(request):
    op = request.match_info.get('op', '')
    env = (request.query.get('env') or '').strip()
    try:
        principal = ext_api.trusted_principal(request)
        if not env:
            raise invalid('缺少集群参数 env')
        if not principal.can_read(env):
            raise TracingError(403, f'没有集群 {env} 的访问权限', 'permission_denied')
        handler = OPS.get(op)
        if handler is None:
            raise invalid(f'不支持的操作 {op[:50]}，可选：{" / ".join(OPS)}')
        body = await read_body(request)
        cfg = await tracing_config.load_config()
        if not cfg['enabled'] or not cfg['url']:
            raise TracingError(400, tracing_config.NOT_CONFIGURED, 'not_configured')
        aliases = await field_map.load_aliases()
        async with ext_api.client_session() as session:
            data = await handler(Ctx(cfg, env, aliases, session), body)
    except (TracingError, ExtApiError) as exc:
        return _fail(exc)
    except Exception as exc:  # 兜底：返回 JSON（AI 那边按 JSON 解析）
        # 只记调用栈，不用 logger.exception：loguru 默认会把栈上的变量值（解密后的请求头、密码）打进日志
        stack = ''.join(traceback.format_exception(type(exc), exc, exc.__traceback__)[-6:])
        logger.error(f'链路追踪 {op} 出错: {type(exc).__name__}\n{stack}')
        return ext_api.fail(500, f'链路追踪处理出错（{type(exc).__name__}），请联系管理员看 master 日志', 'internal_error')
    return await ext_api.json_response({'success': True, 'data': data}, large=op in LARGE_OPS)


# ---------------------------------------------------------------------------
# 系统管理
# ---------------------------------------------------------------------------
def _payload(row, aliases):
    data = tracing_config.public_config(row['value'] if row else {})
    data.update({
        'field_defaults': field_map.FIELD_DEFAULTS,
        'field_labels': field_map.FIELD_LABELS,
        'field_levels': field_map.FIELD_LEVELS,
        'mask_builtin': list(BUILTIN),
        'clusters': [{'env': env, 'aliases': list(names or [])} for env, names in sorted((aliases or {}).items())],
        'updated_by': row['updated_by'] if row else None,
        'updated_at': ext_api.iso(row['updated_at']) if row else None,
    })
    return data


async def get_config(request):
    row = await store.get_setting_row(tracing_config.SETTING_KEY)
    return web.json_response({'success': True, 'data': _payload(row, await field_map.load_aliases())})


async def put_config(request):
    """保存配置：只改传了的字段（右上角开关只传 enabled）；审计只记改了哪些项"""
    principal = principal_of(request)
    try:
        body = await read_body(request)
    except TracingError as exc:
        return _fail(exc)
    existing = await store.get_setting(tracing_config.SETTING_KEY, {}) or {}
    try:
        cfg = tracing_config.normalize_config(body, existing)
    except ValueError as exc:
        return ext_api.fail(400, str(exc), 'invalid_request')
    row = await store.put_setting_row(tracing_config.SETTING_KEY, cfg, principal.username)
    jaeger.reset_caches()  # 地址或 Jaeger 版本可能变了：服务列表和 capabilities 重新取
    audit.record_bg(principal.username, AUDIT_UPDATE, ip=client_ip(request),
                    detail={'changed': tracing_config.changed_items(existing, cfg)})
    return web.json_response({'success': True, 'data': _payload(row, await field_map.load_aliases())})


def _form_config(raw, existing):
    """测试 / 识别字段用页面上的配置（不用先保存；请求头的值、密码留空用已保存的）；不检查「启用时必填」"""
    if not isinstance(raw, dict):
        return tracing_config.merged(existing)
    raw = {**raw, 'enabled': False}
    if isinstance(raw.get('clickhouse'), dict):
        raw['clickhouse'] = {**raw['clickhouse'], 'enabled': False}
    return tracing_config.merged(tracing_config.normalize_config(raw, existing))


async def run_test(request):
    """配置页的「测试连接」：target=jaeger 查服务列表和 capabilities；target=clickhouse 查版本、表结构、各集群取值"""
    try:
        body = await read_body(request)
        target = body.get('target')
        if target not in ('jaeger', 'clickhouse'):
            raise invalid('target 只能是 jaeger 或 clickhouse')
        existing = await store.get_setting(tracing_config.SETTING_KEY, {}) or {}
        cfg = _form_config(body.get('config'), existing)
    except TracingError as exc:
        return _fail(exc)
    except ValueError as exc:
        return ext_api.fail(400, str(exc), 'invalid_request')
    async with ext_api.client_session() as session:
        if target == 'jaeger':
            if not cfg['url']:
                return ext_api.fail(400, '请填写 Jaeger 查询地址', 'invalid_request')
            data = await ops.test_jaeger(cfg, session)
        else:
            if not cfg['clickhouse']['url']:
                return ext_api.fail(400, '请填写 ClickHouse HTTP 地址', 'invalid_request')
            clickhouse.reset_state()  # 重新判断账号能不能带设置
            lookup = field_map.cluster_lookup(await field_map.load_aliases())
            data = await ops.test_clickhouse(cfg, session, lookup)
    return await ext_api.json_response({'success': True, 'data': data}, large=True)


async def run_analyze(request):
    """识别字段：mode=sample 在 Jaeger 上按服务取最近的链路（不加集群过滤，最多 3 条）；mode=json 解析粘贴的 Trace JSON"""
    try:
        body = await read_large_body(request, MAX_ANALYZE_BODY)
        existing = await store.get_setting(tracing_config.SETTING_KEY, {}) or {}
        cfg = _form_config(body.get('config'), existing)
        mode = body.get('mode')
        aliases = await field_map.load_aliases()
        async with ext_api.client_session() as session:
            ctx = Ctx(cfg, '', aliases, session)
            if mode == 'sample':
                service = body.get('service').strip() if isinstance(body.get('service'), str) else ''
                if not service or len(service) > 2048:
                    raise invalid('请选择或填写服务名')
                if not cfg['url']:
                    raise invalid('请填写 Jaeger 查询地址')
                lookback = body.get('lookback') if body.get('lookback') not in (None, '') else '15m'
                data = await ops.analyze_sample(ctx, service, lookback)
            elif mode == 'json':
                text = body.get('trace_json')
                if isinstance(text, str) and (len(text) > MAX_TRACE_JSON or len(text.encode('utf-8')) > MAX_TRACE_JSON):
                    raise TracingError(413, TRACE_JSON_TOO_LARGE, 'invalid_request')
                if not isinstance(text, str) or not text.strip():
                    raise invalid('请粘贴 Trace JSON')
                data = await ops.analyze_json(ctx, text)
            else:
                raise invalid('mode 只能是 sample 或 json')
    except (TracingError, ExtApiError) as exc:
        return _fail(exc)
    except ValueError as exc:
        return ext_api.fail(400, str(exc), 'invalid_request')
    return await ext_api.json_response({'success': True, 'data': data}, large=True)


def register_routes(app):
    app.router.add_get('/api/admin/tracing', get_config)
    app.router.add_put('/api/admin/tracing', put_config)
    app.router.add_post('/api/admin/tracing/test', run_test)
    app.router.add_post('/api/admin/tracing/analyze', run_analyze)
    app.router.add_post('/api/ai/internal/tracing/{op}', ai_op)
