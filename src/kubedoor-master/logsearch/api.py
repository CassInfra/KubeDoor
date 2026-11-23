"""历史日志的接口（约定 §3、§4）。

- 系统管理（policy 的 /api/admin/.+ 规则，只有系统管理员）：
  GET/PUT /api/admin/log-sources、POST /api/admin/log-sources/{test,scan,preview,verify}
- AI 内部接口（policy 的 /api/ai/internal/.+ 规则，内部 token + X-User-Name）：
  POST /api/ai/internal/logs/{op}?env=，op ∈ sources / search / stats / patterns / context / correlate / query；
  按库里的用户权限要求该集群只读及以上；没启用或没有连接时返回 not_configured。不记查询审计（AI 动作表已有记录）
- 日志不记请求头的值、密码和日志内容
"""

import traceback

from aiohttp import web
from loguru import logger

from auth import audit, principal_of
from auth.context import client_ip
from auth.store import store
from func_manager import ext_api
from func_manager.ext_api import ExtApiError
from tracing import fields as trace_fields

from . import admin, clickhouse, elastic, ops, scan
from . import config as log_config
from . import schema as field_schema
from .common import LogError, invalid
from .context import Ctx
from .mask import BUILTIN

AUDIT_UPDATE = 'log_sources.update'
CLIENTS = web.AppKey('logsearch_clients', dict)
MAX_GLOBS = 20
OPS = ops.OPS


def _fail(exc):
    return ext_api.fail(exc.status, exc.message, exc.code, getattr(exc, 'data', None))


async def read_body(request):
    """请求体 JSON 对象；没有请求体当 {}（同链路追踪：用 body_exists 判断，鉴权中间件可能已经读过一遍）"""
    if not request.body_exists:
        return {}
    try:
        body = await request.json()
    except web.HTTPRequestEntityTooLarge:
        limit = getattr(request, '_client_max_size', 0) or 0
        size = f'（目前上限 {limit // (1024 * 1024)} MB）' if limit >= 1024 * 1024 else ''
        raise LogError(413, f'请求体太大{size}', 'invalid_request') from None
    except Exception:
        raise invalid('请求体要是 JSON 对象') from None
    if body is None:
        return {}
    if not isinstance(body, dict):
        raise invalid('请求体要是 JSON 对象')
    return body


def reset_caches():
    """配置变了：字段表、ES 版本、readonly 账号的判断都重新来"""
    field_schema.reset()
    elastic.reset_caches()
    clickhouse.reset_state()


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
            raise LogError(403, f'没有集群 {env} 的访问权限', 'permission_denied')
        handler = OPS.get(op)
        if handler is None:
            raise invalid(f'不支持的操作 {op[:50]}，可选：{" / ".join(OPS)}')
        body = await read_body(request)
        cfg = await log_config.load_config()
        if not cfg['enabled'] or not cfg['connections']:
            raise LogError(400, log_config.NOT_CONFIGURED, 'not_configured')
        async with ext_api.client_session() as session:
            ctx = Ctx(cfg, env, session)
            try:
                data = await handler(ctx, body)
            except LogError as exc:
                exc.message = ctx.timeout_hint(exc)
                raise
    except (LogError, ExtApiError) as exc:
        return _fail(exc)
    except Exception as exc:  # 兜底：返回 JSON（AI 那边按 JSON 解析）
        # 只记调用栈，不用 logger.exception：loguru 默认会把栈上的变量值（解密后的密码、日志内容）打进日志
        stack = ''.join(traceback.format_exception(type(exc), exc, exc.__traceback__)[-6:])
        logger.error(f'历史日志 {op} 出错: {type(exc).__name__}\n{stack}')
        return ext_api.fail(500, f'历史日志查询出错（{type(exc).__name__}），请联系管理员看 master 日志', 'internal_error')
    return await ext_api.json_response({'success': True, 'data': data}, large=True)


# ---------------------------------------------------------------------------
# 系统管理
# ---------------------------------------------------------------------------
async def clusters_known(request):
    """KubeDoor 已登记的集群（k8s_agent_status）和在线状态，按 env 排序"""
    aliases = await trace_fields.load_aliases()
    clients = request.app.get(CLIENTS) or {}
    envs = sorted(set(aliases) | set(clients))
    return [{'env': env, 'aliases': list(aliases.get(env) or []),
             'online': bool((clients.get(env) or {}).get('online'))} for env in envs]


async def _payload(request, row):
    data = log_config.public_config(row['value'] if row else {})
    data.update({
        'clusters_known': await clusters_known(request),
        'semantic_fields': [{'key': key, 'label': label} for key, label in log_config.SEMANTIC_FIELDS],
        'mask_builtin': list(BUILTIN),
        'updated_by': row['updated_by'] if row else None,
        'updated_at': ext_api.iso(row['updated_at']) if row else None,
    })
    return data


async def get_config(request):
    row = await store.get_setting_row(log_config.SETTING_KEY)
    return web.json_response({'success': True, 'data': await _payload(request, row)})


async def put_config(request):
    """保存配置：只改传了的顶层键（右上角开关只传 enabled）；审计只记改了哪些项"""
    principal = principal_of(request)
    try:
        body = await read_body(request)
    except LogError as exc:
        return _fail(exc)
    existing = await store.get_setting(log_config.SETTING_KEY, {}) or {}
    try:
        cfg = log_config.normalize_config(body, existing)
    except ValueError as exc:
        return ext_api.fail(400, str(exc), 'invalid_request')
    row = await store.put_setting_row(log_config.SETTING_KEY, cfg, principal.username)
    reset_caches()
    audit.record_bg(principal.username, AUDIT_UPDATE, ip=client_ip(request),
                    detail={'changed': log_config.changed_items(existing, cfg)})
    return web.json_response({'success': True, 'data': await _payload(request, row)})


async def _form_config(body):
    """页面上还没保存的完整表单（密码留空用已保存的）→ 可用的配置；不检查「启用时必填」"""
    existing = await store.get_setting(log_config.SETTING_KEY, {}) or {}
    raw = body.get('config')
    if raw is None:
        return log_config.merged(existing)
    if not isinstance(raw, dict):
        raise invalid('config 要是配置对象')
    try:
        return log_config.merged(log_config.normalize_config(raw, existing, check_enabled=False))
    except ValueError as exc:
        raise invalid(str(exc)) from None


def _connection(cfg, body):
    cid = body.get('connection')
    conn = next((item for item in cfg['connections'] if item['id'] == cid), None)
    if conn is None:
        raise invalid(f'没有连接 {str(cid)[:40]}')
    return conn


def _globs(body, key):
    items = body.get(key)
    if items in (None, ''):
        return []
    if not isinstance(items, list) or len(items) > MAX_GLOBS:
        raise invalid(f'{key} 要是通配符列表，最多 {MAX_GLOBS} 个')
    result = []
    for item in items:
        if not isinstance(item, str) or not item.strip() or len(item) > 200:
            raise invalid(f'{key} 的每一项要是不超过 200 个字符的通配符，例如 logs.* 或 app-*')
        result.append(item.strip())
    return result


def _env(body):
    env = body.get('env')
    if not isinstance(env, str) or not env.strip():
        raise invalid('env 必填（集群名）')
    return env.strip()


async def _admin(request, work):
    """管理接口的公共部分：读请求体 → 页面表单配置 → work(body, cfg, session)"""
    try:
        body = await read_body(request)
        cfg = await _form_config(body)
        async with ext_api.client_session() as session:
            data = await work(body, cfg, session)
    except (LogError, ExtApiError) as exc:
        return _fail(exc)
    except Exception as exc:
        stack = ''.join(traceback.format_exception(type(exc), exc, exc.__traceback__)[-6:])
        logger.error(f'历史日志管理接口 {request.path} 出错: {type(exc).__name__}\n{stack}')
        return ext_api.fail(500, f'处理出错（{type(exc).__name__}），请看 master 日志', 'internal_error')
    return await ext_api.json_response({'success': True, 'data': data}, large=True)


async def run_test(request):
    async def work(body, cfg, session):
        conn = _connection(cfg, body)
        clickhouse.reset_state()  # 重新判断账号能不能带设置
        return await admin.test_connection(conn, session)

    return await _admin(request, work)


async def run_scan(request):
    async def work(body, cfg, session):
        conn = _connection(cfg, body)
        include, exclude = _globs(body, 'include'), _globs(body, 'exclude')
        aliases = await trace_fields.load_aliases()
        clusters = [{'env': env, 'aliases': list(names or [])} for env, names in sorted(aliases.items())]
        return await scan.run(conn, session, include, exclude, clusters)

    return await _admin(request, work)


async def run_preview(request):
    async def work(body, cfg, session):
        source = body.get('source')
        if not isinstance(source, str) or not source:
            raise invalid('source 必填（日志源 ID）')
        return await admin.preview(Ctx(cfg, _env(body), session), source)

    return await _admin(request, work)


async def run_verify(request):
    async def work(body, cfg, session):
        key = body.get('key')
        if not isinstance(key, str) or not key:
            raise invalid('key 必填（关联字段）')
        return await admin.verify(Ctx(cfg, _env(body), session), key)

    return await _admin(request, work)


def register_routes(app, clients=None):
    """clients：master 的 agent 连接表（kubedoor-master.py 的 clients），用来给页面标集群在线状态"""
    app[CLIENTS] = clients if clients is not None else {}
    app.router.add_get('/api/admin/log-sources', get_config)
    app.router.add_put('/api/admin/log-sources', put_config)
    app.router.add_post('/api/admin/log-sources/test', run_test)
    app.router.add_post('/api/admin/log-sources/scan', run_scan)
    app.router.add_post('/api/admin/log-sources/preview', run_preview)
    app.router.add_post('/api/admin/log-sources/verify', run_verify)
    app.router.add_post('/api/ai/internal/logs/{op}', ai_op)
