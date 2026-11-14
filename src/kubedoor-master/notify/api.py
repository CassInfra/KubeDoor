"""「系统管理 → 消息通知」的接口,以及给 kubedoor-alarm 取通道的内部接口。

- GET /api/admin/notification:配置(token 只回显是否已配置和最后 4 位)+ 已注册的集群列表
- PUT /api/admin/notification:保存。default 为 null 删除全局配置;clusters 是完整列表,整体替换;
  token 留空沿用已保存的(没有已保存的、或者 IM 类型换了必须重新填写,422);保存后立即生效,记审计 notification.update
- POST /api/admin/notification/test:发一条测试消息(token 留空用已保存的),同一账号 3 秒一次,记审计 notification.test
- GET /api/internal/notification?env=:kubedoor-alarm 带内部 token 取该集群的通道(token 明文):env 为空或不认识时
  按 resolve 的规则用全局配置,都没有时 data 为 null
- save_cluster_channel(env, entry, updated_by):网页安装 Agent 上线后,把安装时填的通知配置写进 clusters[env]
  (和 PUT 共用一把锁,校验 / 加密规则相同,保留全局配置和其他集群)

/api/admin/.+ 已在 auth/policy.py 统一要求系统管理员;内部接口在 policy 登记为 internal,这里再限定为
不带用户名的内部服务身份(AI 服务代表某个用户的调用拿不到明文 token)。
"""

import asyncio
import math
import re
import time
from datetime import datetime

from aiohttp import web
from loguru import logger

import db
from auth import audit, principal_of
from auth.context import client_ip
from auth.crypto import decrypt_secret, encrypt_secret
from auth.store import store

from . import channels, service

SETTING_KEY = service.SETTING_KEY
DEFAULT_SCOPE = service.DEFAULT_SCOPE
ENV_PATTERN = re.compile(r'[A-Za-z0-9_-]{1,100}')
MAX_CLUSTERS = 500
TEST_INTERVAL = 3  # 秒:同一账号两次测试的最短间隔
AUDIT_UPDATE = 'notification.update'
AUDIT_TEST = 'notification.test'
REFRESH_TASK = web.AppKey('notify_refresh_task', asyncio.Task)

_last_test = {}  # 账号(小写) -> 上次测试的时间(monotonic)
# 保存配置是「读出整行 → 合并 → 写回」:页面保存(PUT)和安装 Agent 写入集群配置共用这把锁,互相不覆盖
_save_lock = asyncio.Lock()


def _fail(status, message, code='bad_request', retry_after=0):
    response = web.json_response({'success': False, 'code': code, 'message': message, 'error': message},
                                 status=status)
    if retry_after:
        response.headers['Retry-After'] = str(retry_after)
    return response


async def _read_body(request):
    """请求体必须是 JSON 对象,否则返回 None"""
    try:
        body = await request.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None


def _iso(value):
    if isinstance(value, datetime):
        return (value.astimezone() if value.tzinfo else value).isoformat()
    return value


def _scope(env):
    return f'集群 {env}' if env else DEFAULT_SCOPE


# ---------------------------------------------------------------------------
# 配置校验
# ---------------------------------------------------------------------------
def saved_token(entry):
    """已保存的 token 明文;没有或解不开返回空串"""
    if not isinstance(entry, dict):
        return ''
    try:
        return decrypt_secret(entry.get('token') or '')
    except Exception:
        return ''


def check_entry(raw, saved, scope):
    """校验一个范围(全局 / 某个集群)页面提交的配置,返回 (type, token 明文, at, 是否沿用已保存的 token)。
    token 留空沿用已保存的:没有已保存的、IM 类型换了、或者已保存的解不开,都要重新填写。不合法抛 ValueError。"""
    if not isinstance(raw, dict):
        raise ValueError(f'{scope}的格式不正确')
    kind = raw.get('type')
    if kind not in channels.TYPES:
        raise ValueError(f'{scope}:请选择 IM 类型({"、".join(channels.TYPE_LABELS.values())})')
    label = channels.TYPE_LABELS[kind]
    try:
        at = channels.normalize_at(raw.get('at'))
    except ValueError as exc:
        raise ValueError(f'{scope}:{exc}') from None
    token = raw.get('token')
    token = '' if token is None else str(token).strip()
    if token:
        try:
            return kind, channels.normalize_token(kind, token), at, False
        except ValueError as exc:
            raise ValueError(f'{scope}:{exc}') from None
    saved = saved if isinstance(saved, dict) else {}
    if not saved.get('token'):
        raise ValueError(f'{scope}:请填写{label}机器人地址')
    if saved.get('type') != kind:
        before = channels.TYPE_LABELS.get(saved.get('type'), '其他 IM')
        raise ValueError(f'{scope}:IM 类型从{before}改成了{label},请重新填写机器人地址')
    plain = saved_token(saved)
    if not plain:
        raise ValueError(f'{scope}:已保存的机器人地址无法解密(AI_ENCRYPTION_KEY 换过),请重新填写')
    return kind, plain, at, True


def normalize_entry(raw, saved, scope):
    """页面提交的一个范围 → 存库格式(新 token 加密;沿用的保留原密文)"""
    kind, token, at, reused = check_entry(raw, saved, scope)
    return {'type': kind, 'token': saved['token'] if reused else encrypt_secret(token), 'at': at}


def _saved_clusters(existing):
    clusters = existing.get('clusters') if isinstance(existing, dict) else None
    return clusters if isinstance(clusters, dict) else {}


def check_env(env):
    """集群名称:字母、数字、- 和 _,最长 100;返回去掉首尾空白的名称"""
    env = env.strip() if isinstance(env, str) else ''
    if not ENV_PATTERN.fullmatch(env):
        raise ValueError(f'集群名称不正确:{env or "(空)"}(只能用字母、数字、- 和 _,最长 100 个字符)')
    return env


def normalize_config(raw, existing):
    """校验 PUT 的请求体,返回存库格式。default 为 null 表示删除全局配置;clusters 是完整列表,整体替换;
    没传的键沿用已保存的。不合法抛 ValueError。"""
    if not isinstance(raw, dict):
        raise ValueError('请求格式不正确')
    existing = existing if isinstance(existing, dict) else {}
    cfg = {}
    if 'default' in raw:
        if raw['default'] is not None:
            cfg['default'] = normalize_entry(raw['default'], existing.get('default'), DEFAULT_SCOPE)
    elif isinstance(existing.get('default'), dict):
        cfg['default'] = existing['default']
    saved_clusters = _saved_clusters(existing)
    if 'clusters' not in raw:
        cfg['clusters'] = dict(saved_clusters)
        return cfg
    items = raw['clusters'] if raw['clusters'] is not None else []
    if not isinstance(items, list):
        raise ValueError('按集群单独配置(clusters)应该是列表')
    if len(items) > MAX_CLUSTERS:
        raise ValueError(f'按集群单独配置最多 {MAX_CLUSTERS} 个')
    clusters = {}
    for item in items:
        if not isinstance(item, dict):
            raise ValueError('按集群单独配置的格式不正确')
        env = check_env(item.get('env'))
        if env in clusters:
            raise ValueError(f'集群 {env} 重复了,每个集群只能单独配置一次')
        clusters[env] = normalize_entry(item, saved_clusters.get(env), _scope(env))
    cfg['clusters'] = clusters
    return cfg


def _same(old, new):
    return (old.get('type') == new.get('type') and (old.get('at') or '') == (new.get('at') or '')
            and (old.get('token') == new.get('token') or saved_token(old) == saved_token(new)))


def diff_scopes(old, new):
    """审计内容:哪些范围新增 / 修改 / 删除了(只比较,不写 token)"""
    old = old if isinstance(old, dict) else {}
    old_default = old.get('default') if isinstance(old.get('default'), dict) else None
    new_default = new.get('default')
    if old_default is None:
        default = 'added' if new_default is not None else 'unchanged'
    elif new_default is None:
        default = 'removed'
    else:
        default = 'unchanged' if _same(old_default, new_default) else 'updated'
    old_clusters, new_clusters = _saved_clusters(old), new.get('clusters') or {}
    return {'default': default, 'clusters': {
        'added': sorted(set(new_clusters) - set(old_clusters)),
        'updated': sorted(env for env in set(new_clusters) & set(old_clusters)
                          if not isinstance(old_clusters[env], dict) or not _same(old_clusters[env], new_clusters[env])),
        'removed': sorted(set(old_clusters) - set(new_clusters)),
    }}


# ---------------------------------------------------------------------------
# 给页面看的配置
# ---------------------------------------------------------------------------
def token_hint(token):
    """只给最后 4 位;太短的 token 不给"""
    if not token:
        return ''
    return '****' + token[-4:] if len(token) >= 8 else '****'


def public_entry(entry):
    if not isinstance(entry, dict) or entry.get('type') not in channels.TYPES:
        return None
    token = saved_token(entry)
    return {'type': entry['type'], 'at': str(entry.get('at') or ''), 'configured': bool(token),
            'token_hint': token_hint(token)}


def public_config(value):
    value = value if isinstance(value, dict) else {}
    clusters = []
    for env, entry in sorted(_saved_clusters(value).items()):
        item = public_entry(entry)
        if item is not None:
            clusters.append({'env': env, **item})
    return {'default': public_entry(value.get('default')), 'clusters': clusters}


async def registered_envs():
    """k8s_agent_status 里已注册的集群(给页面下拉用),按名称排序;读不到返回空列表"""
    try:
        rows = await db.pg_fetch('SELECT env FROM k8s_agent_status ORDER BY env')
    except Exception as exc:
        logger.warning(f'读取已注册的集群失败: {exc}')
        return []
    return sorted({row['env'] for row in rows if row['env']})


async def _payload(row):
    return {**public_config(row['value'] if row else {}), 'envs': await registered_envs(),
            'updated_by': row['updated_by'] if row else None,
            'updated_at': _iso(row['updated_at']) if row else None}


async def _load_row():
    """读库里的配置行;失败返回 (None, 503 响应)"""
    try:
        return await store.get_setting_row(SETTING_KEY), None
    except Exception as exc:
        logger.error(f'读取消息通知配置失败: {exc}')
        return None, _fail(503, '读取消息通知配置失败,请稍后重试', 'db_unavailable')


# ---------------------------------------------------------------------------
# 管理接口(系统管理员)
# ---------------------------------------------------------------------------
async def get_config(request):
    row, error = await _load_row()
    if error is not None:
        return error
    return web.json_response({'success': True, 'data': await _payload(row)})


async def put_config(request):
    principal = principal_of(request)
    body = await _read_body(request)
    if body is None:
        return _fail(400, '请求体必须是 JSON 对象')
    async with _save_lock:
        row, error = await _load_row()
        if error is not None:
            return error
        existing = row['value'] if row and isinstance(row['value'], dict) else {}
        try:
            cfg = normalize_config(body, existing)
        except ValueError as exc:
            return _fail(422, str(exc), 'invalid_config')
        changes = diff_scopes(existing, cfg)
        try:
            saved = await store.put_setting_row(SETTING_KEY, cfg, principal.username)
        except Exception as exc:
            logger.error(f'保存消息通知配置失败: {exc}')
            return _fail(503, '保存消息通知配置失败,请稍后重试', 'db_unavailable')
        service.apply(saved['value'])  # 保存后立即生效,不等后台刷新
    audit.record_bg(principal.username, AUDIT_UPDATE, ip=client_ip(request), detail=changes)
    return web.json_response({'success': True, 'data': await _payload(saved)})


async def save_cluster_channel(env, entry, updated_by):
    """网页安装 Agent 上线后,把安装时填的通知配置写进 clusters[env]:已有该集群的单独配置就覆盖,
    全局配置和其他集群原样保留。entry 是 {type, token, at}(token 明文;留空沿用该集群已保存的,规则同 PUT)。

    校验 / 加密和 PUT 一样,与 PUT 共用一把锁;写完立即生效,记审计 notification.update(detail 带 source: agent-install)。
    不合法抛 ValueError(中文说明,不含 token),读写库失败抛原来的异常。"""
    env = check_env(env)
    async with _save_lock:
        row = await store.get_setting_row(SETTING_KEY)
        existing = row['value'] if row and isinstance(row['value'], dict) else {}
        clusters = dict(_saved_clusters(existing))
        if env not in clusters and len(clusters) >= MAX_CLUSTERS:
            raise ValueError(f'按集群单独配置最多 {MAX_CLUSTERS} 个')
        clusters[env] = normalize_entry(entry, clusters.get(env), _scope(env))
        cfg = {'default': existing['default']} if isinstance(existing.get('default'), dict) else {}
        cfg['clusters'] = clusters
        changes = diff_scopes(existing, cfg)
        saved = await store.put_setting_row(SETTING_KEY, cfg, updated_by)
        service.apply(saved['value'])
    audit.record_bg(updated_by, AUDIT_UPDATE, env=env, detail={**changes, 'source': 'agent-install'})


def _test_wait(username):
    """同一账号 TEST_INTERVAL 秒内只能测一次:返回还要等的秒数,0 表示可以测(同时记下这次)"""
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


async def run_test(request):
    """发一条测试消息:token 非空用页面上还没保存的值,为空用已保存的(type / at 以请求里的为准)。
    结果写进 data.ok / data.message(IM 返回的错误),不因为 IM 失败返回非 200。"""
    principal = principal_of(request)
    body = await _read_body(request)
    if body is None:
        return _fail(400, '请求体必须是 JSON 对象')
    raw_env = body.get('env')
    try:
        env = '' if raw_env is None or (isinstance(raw_env, str) and not raw_env.strip()) else check_env(raw_env)
    except ValueError as exc:
        return _fail(422, str(exc), 'invalid_config')
    scope = _scope(env)
    row, error = await _load_row()
    if error is not None:
        return error
    existing = row['value'] if row and isinstance(row['value'], dict) else {}
    saved = _saved_clusters(existing).get(env) if env else existing.get('default')
    try:
        kind, token, at, reused = check_entry(body, saved, scope)
    except ValueError as exc:
        return _fail(422, str(exc), 'invalid_config')
    wait = _test_wait(principal.username)
    if wait:
        return _fail(429, f'测试太频繁,请 {wait} 秒后再试', 'too_many_requests', retry_after=wait)
    content = channels.test_content(scope, principal.username, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    ok, message = await asyncio.to_thread(service.deliver, {'type': kind, 'token': token, 'at': at}, content)
    label = channels.TYPE_LABELS[kind]
    if not ok:
        logger.warning(f'消息通知测试失败({scope},{label}):{message}')
    audit.record_bg(principal.username, AUDIT_TEST, env=env, ip=client_ip(request), success=ok,
                    detail={'scope': env or 'default', 'type': kind, 'saved_token': reused, 'message': message})
    return web.json_response({'success': True, 'data': {'ok': ok, 'message': message}})


# ---------------------------------------------------------------------------
# 内部接口(kubedoor-alarm)
# ---------------------------------------------------------------------------
async def internal_channel(request):
    """kubedoor-alarm 取通道:GET /api/internal/notification?env=,返回 {type, token(明文), at},没有配置时 data 为 null。
    env 为空表示全局配置;格式不对或不认识的集群按 resolve 的规则退回全局配置,照样返回 200
    (kubedoor-alarm 把非 200 当成取不到,沿用上次的配置,一次都没取到就不发)。
    只认不带用户名的内部 token(系统身份);读的是内存缓存,和 master 自己发通知用的同一份"""
    principal = principal_of(request)
    if principal.via != 'system':
        return _fail(403, '仅限集群内服务用内部 token 调用', 'forbidden')
    env = (request.query.get('env') or '').strip()
    if not service.is_loaded() and not await service.refresh():
        return _fail(503, '消息通知配置还没有加载,请稍后重试', 'not_ready')
    response = web.json_response({'success': True, 'data': service.resolve(env)})
    response.headers['Cache-Control'] = 'no-store'
    return response


# ---------------------------------------------------------------------------
# 注册与启停
# ---------------------------------------------------------------------------
def register_routes(app):
    app.router.add_get('/api/admin/notification', get_config)
    app.router.add_put('/api/admin/notification', put_config)
    app.router.add_post('/api/admin/notification/test', run_test)
    app.router.add_get('/api/internal/notification', internal_channel)


async def on_startup(app):
    """加载配置并启动后台刷新;必须排在建表(init_db_and_schema)之后。加载失败不影响启动,后台会继续重试"""
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
