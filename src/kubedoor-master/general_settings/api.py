"""「系统管理 → 通用配置」的接口，以及给 kubedoor-alarm、kubedoor-ai 取配置的内部接口。

- GET /api/admin/general-settings：全部配置项的生效值（地址里的密码换成 ******）、默认值、库里存了哪些项、有问题的项
- PUT /api/admin/general-settings：{"values": {"<key>": <值或 null>}}，只带要改的项；null / 空串 = 删掉这一项，
  地址里的密码 ****** = 沿用已保存的密码；保存后立即生效，记审计 general_settings.update（只记改了哪些 key，不记值）
- POST /api/admin/general-settings/test-metrics：测试时序库查询地址和集群指标标签 key，同一账号 3 秒一次
- GET /api/internal/settings：kubedoor-alarm、kubedoor-ai 带内部 token（不带用户名）取几项不含密码的配置

/api/admin/.+ 已在 auth/policy.py 统一要求系统管理员；内部接口在 policy 登记为 internal，这里再限定为不带用户名的
内部服务身份。错误体格式同 notify/api.py：{"success": false, "code", "message", "error"}。
"""

import asyncio
import math
import time
from datetime import datetime

from aiohttp import web
from loguru import logger

from agent_install import bundle
from auth import audit, principal_of
from auth.context import client_ip

from . import fields, legacy, probe, service

AUDIT_UPDATE = 'general_settings.update'
TEST_INTERVAL = 3  # 秒:同一账号两次测试的最短间隔
# 内部接口给出的项:都不含密码
INTERNAL_KEYS = ('prom_k8s_tag_key', 'kubedoor_exturl', 'alertmanager_exturl', 'silence_cache_ttl',
                 'retention_ai_checkpoint_days')
REFRESH_TASK = web.AppKey('general_settings_refresh_task', asyncio.Task)

_last_test = {}  # 账号(小写) -> 上次测试的时间(monotonic)


def _fail(status, message, code='bad_request', retry_after=0):
    response = web.json_response({'success': False, 'code': code, 'message': message, 'error': message},
                                 status=status)
    if retry_after:
        response.headers['Retry-After'] = str(retry_after)
    return response


async def _read_body(request):
    """请求体必须是 JSON 对象，否则返回 None"""
    try:
        body = await request.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None


def _iso(value):
    if isinstance(value, datetime):
        return (value.astimezone() if value.tzinfo else value).isoformat()
    return value


async def _load_row():
    """读库里的配置行；失败返回 (None, 503 响应)"""
    try:
        return await service.storage.read(), None
    except Exception as exc:
        logger.error(f'读取通用配置失败: {type(exc).__name__}: {exc}')
        return None, _fail(503, '读取通用配置失败，请稍后重试', 'db_unavailable')


def payload(row):
    """给页面看的配置：values 是全部项的生效值（地址里的密码换成 ******，有问题的地址为空串）"""
    snapshot = service.build_snapshot(row)
    values = {key: fields.masked(item) if fields.FIELDS[key].secret else item
              for key, item in snapshot.values.items()}
    return {
        'values': values,
        'defaults': dict(fields.DEFAULTS),
        'stored': [key for key in fields.FIELDS if key in snapshot.stored],
        'problems': dict(snapshot.problems),
        'recommended_agent_tag': bundle.BASE['agent_tag'],
        'updated_by': row['updated_by'] if row else None,
        'updated_at': _iso(row['updated_at']) if row else None,
    }


# ---------------------------------------------------------------------------
# 管理接口（系统管理员）
# ---------------------------------------------------------------------------
async def get_settings(request):
    row, error = await _load_row()
    if error is not None:
        return error
    return web.json_response({'success': True, 'data': payload(row)})


async def put_settings(request):
    principal = principal_of(request)
    body = await _read_body(request)
    if body is None:
        return _fail(400, '请求体必须是 JSON 对象')
    changes = body.get('values')
    if not isinstance(changes, dict):
        return _fail(400, '请求体里的 values 必须是对象：{"values": {"配置项": 值}}')
    unknown = sorted(str(key) for key in changes if key not in fields.FIELDS)
    if unknown:
        return _fail(422, f'不认识的配置项：{"、".join(unknown[:10])}', 'invalid_config')
    try:
        row, changed = await service.save(changes, principal.username)
    except ValueError as exc:
        return _fail(422, str(exc), 'invalid_config')
    except Exception as exc:
        logger.error(f'保存通用配置失败: {type(exc).__name__}: {exc}')
        return _fail(503, '保存通用配置失败，请稍后重试', 'db_unavailable')
    audit.record_bg(principal.username, AUDIT_UPDATE, ip=client_ip(request), detail={'changed': changed})
    return web.json_response({'success': True, 'data': payload(row)})


def _test_wait(username):
    """同一账号 TEST_INTERVAL 秒内只能测一次：返回还要等的秒数，0 表示可以测（同时记下这次）"""
    now = time.monotonic()
    key = (username or '').lower()
    last = _last_test.get(key)
    if last is not None and now - last < TEST_INTERVAL:
        return max(math.ceil(TEST_INTERVAL - (now - last)), 1)
    if len(_last_test) > 1000:
        for name in [name for name, at in _last_test.items() if now - at >= TEST_INTERVAL]:
            _last_test.pop(name, None)
    _last_test[key] = now
    return 0


def _test_target(body, row):
    """测试用的 (查询地址, 标签 key)：没传（或 null）用当前生效值；地址里的 ****** 换成已保存的密码。
    标签 key 传空串按默认值；地址最后为空时抛 ValueError"""
    stored = row['value'] if row and isinstance(row.get('value'), dict) else {}
    snapshot = service.build_snapshot(row)
    raw_url, raw_key = body.get('prom_url'), body.get('prom_k8s_tag_key')
    if raw_url is None:
        url = snapshot.values['prom_url']
    elif fields.is_blank(raw_url):
        url = ''
    else:
        url = fields.clean('prom_url', raw_url, previous=service.plain('prom_url', stored.get('prom_url')))
    if raw_key is None:
        tag_key = snapshot.values['prom_k8s_tag_key']
    elif fields.is_blank(raw_key):
        tag_key = fields.DEFAULTS['prom_k8s_tag_key']
    else:
        tag_key = fields.clean('prom_k8s_tag_key', raw_key)
    if not url:
        raise ValueError(f'还没有填写时序库{fields.LABELS["prom_url"]}')
    return url, tag_key


async def test_metrics(request):
    """测试时序库：先查 vector(1)，再查最近一天的集群标签值。结果写进 data.ok / data.message，
    时序库连不上不返回非 200；参数不对 422（不占测试次数），太频繁 429"""
    principal = principal_of(request)
    body = await _read_body(request)
    if body is None:
        return _fail(400, '请求体必须是 JSON 对象')
    row, error = await _load_row()
    if error is not None:
        return error
    try:
        url, tag_key = _test_target(body, row)
    except ValueError as exc:
        return _fail(422, str(exc), 'invalid_config')
    wait = _test_wait(principal.username)
    if wait:
        return _fail(429, f'测试太频繁，请 {wait} 秒后再试', 'too_many_requests', retry_after=wait)
    result = await probe.check_metrics(url, tag_key)
    if not result['ok']:
        logger.warning(f'通用配置:测试时序库失败({principal.username}):{result["message"]}')
    return web.json_response({'success': True, 'data': result})


# ---------------------------------------------------------------------------
# 内部接口（kubedoor-alarm、kubedoor-ai）
# ---------------------------------------------------------------------------
async def internal_settings(request):
    """GET /api/internal/settings：只认不带用户名的内部 token（系统身份）；读内存快照，不含任何密码。
    快照还没加载时先从库里加载一次，仍失败返回 503（调用方沿用上次取到的值）"""
    principal = principal_of(request)
    if principal.via != 'system':
        return _fail(403, '仅限集群内服务用内部 token 调用', 'forbidden')
    if not service.is_loaded() and not await service.refresh():
        return _fail(503, '通用配置还没有加载，请稍后重试', 'not_ready')
    response = web.json_response({'success': True, 'data': {key: service.value(key) for key in INTERNAL_KEYS}})
    response.headers['Cache-Control'] = 'no-store'
    return response


# ---------------------------------------------------------------------------
# 注册与启停
# ---------------------------------------------------------------------------
def register_routes(app):
    app.router.add_get('/api/admin/general-settings', get_settings)
    app.router.add_put('/api/admin/general-settings', put_settings)
    app.router.add_post('/api/admin/general-settings/test-metrics', test_metrics)
    app.router.add_get('/api/internal/settings', internal_settings)


async def on_startup(app):
    """导入旧配置（环境变量 → 通用配置、MSG_* → 消息通知）→ 加载快照 → 起 10 秒刷新的后台任务。
    必须排在建表（init_db_and_schema）之后。导入、加载失败都不影响启动：导入可以用
    `python3 -m general_settings migrate` 重试，快照由后台任务继续重试"""
    try:
        await legacy.import_legacy()
    except Exception as exc:
        logger.error(f'导入旧配置失败(不影响启动,可以执行 python3 -m general_settings migrate 重试):'
                     f'{type(exc).__name__}: {exc}')
    await service.refresh()
    app[REFRESH_TASK] = asyncio.create_task(service.refresh_loop())


async def on_cleanup(app):
    task = app.get(REFRESH_TASK)
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
