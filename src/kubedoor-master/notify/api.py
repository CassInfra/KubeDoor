"""「系统管理 → 消息通知」v1/v2 接口,投递审计查询以及 kubedoor-alarm 内部代理接口。

v1 兼容接口(保留 1 个大版本):
- GET /api/admin/notification: 配置(token 只回显是否已配置和最后 4 位)+ 已注册的集群列表
- PUT /api/admin/notification: 保存全局/按集群兼容配置;保持 v1/v2 存储互操作
- POST /api/admin/notification/test: 同时支持 v1 {env, type, token, at} 与 v2 {channel_id?, provider, name?, settings/config, secrets}
- GET /api/internal/notification?env=: 已弃用兼容接口(带 Deprecation: true),返回 legacy 4-IM 通道(仅限 system 身份)
- save_cluster_channel(env, entry, updated_by): 网页安装 Agent 上线后写入集群通知配置

v2 统一多通道接口:
- GET /api/admin/notification/providers: 返回 Schema 驱动的通知提供方元数据
- GET /api/admin/notification/v2: 返回标准化 v2 配置(channels[], routes[], default_channel_ids[], envs[])
- PUT /api/admin/notification/v2: Schema 校验 + 静态 SSRF 防护 + AES-GCM 加密 + 留空凭据沿用 + 立即生效 + 审计
- GET /api/admin/notification/deliveries: 查询投递审计日志(PostgreSQL + 内存环形缓冲兜底)
- POST /api/internal/notify: kubedoor-alarm 统一代理发送端点(严格限定 principal.via == 'system')
"""

import asyncio
from datetime import datetime
import math
import re
import time
import uuid

from aiohttp import web
from loguru import logger

import db
from auth import audit, principal_of
from auth.context import client_ip
from auth.crypto import decrypt_secret, encrypt_secret
from auth.store import store

from . import channels, service
from kubedoor_tools.notify import (
    ChannelConfig,
    SSRFError,
    build_alert_message,
    build_event_message,
    get_provider,
    mask_hint,
    migrate_v1_to_v2,
    normalize_v2_config,
    project_v2_to_v1,
    provider_schemas,
)

SETTING_KEY = service.SETTING_KEY
DEFAULT_SCOPE = service.DEFAULT_SCOPE
ENV_PATTERN = re.compile(r'[A-Za-z0-9_-]{1,100}')
MAX_CLUSTERS = 500
TEST_INTERVAL = 3  # 秒:同一账号两次测试的最短间隔
AUDIT_UPDATE = 'notification.update'
AUDIT_TEST = 'notification.test'
REFRESH_TASK = web.AppKey('notify_refresh_task', asyncio.Task)

_last_test = {}  # 账号(小写) -> 上次测试的时间(monotonic)
# 保存配置是「读出整行 → 合并 → 写回」:页面保存(PUT / PUT v2)和安装 Agent 写入集群配置共用这把锁,互相不覆盖
_save_lock = asyncio.Lock()


def _fail(status, message, code='bad_request', retry_after=0):
    response = web.json_response(
        {'success': False, 'code': code, 'message': message, 'error': message},
        status=status,
    )
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
# v1 配置校验与 v1/v2 存储互操作
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


def _existing_as_v1(existing):
    if isinstance(existing, dict) and existing.get('version') == 2:
        return project_v2_to_v1(existing)
    return existing if isinstance(existing, dict) else {}


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
    existing = _existing_as_v1(existing)
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


def _v1_default_settings(kind, at):
    if kind == 'feishu':
        return {'domain_preset': 'feishu', 'at': at}
    if kind in ('wecom', 'dingding'):
        return {'domain_preset': 'public', 'at': at}
    return {'at': at}


def _upsert_v2_from_v1_entry(v2_channels, cid, name, entry):
    kind = entry['type']
    at = str(entry.get('at') or '')
    prev = v2_channels.get(cid)
    same_provider = isinstance(prev, dict) and prev.get('provider') == kind
    settings = (
        dict(prev.get('settings') or {})
        if same_provider and isinstance(prev.get('settings'), dict)
        else _v1_default_settings(kind, at)
    )
    settings['at'] = at
    if kind == 'feishu' and 'domain_preset' not in settings:
        settings['domain_preset'] = 'feishu'
    if kind in ('wecom', 'dingding') and 'domain_preset' not in settings:
        settings['domain_preset'] = 'public'
    secrets = (
        dict(prev.get('secrets') or {})
        if same_provider and isinstance(prev.get('secrets'), dict)
        else {}
    )
    secrets['token'] = entry['token']
    v2_channels[cid] = {
        'id': cid,
        'name': str(prev.get('name') or name) if isinstance(prev, dict) else name,
        'provider': kind,
        'enabled': True,
        'settings': settings,
        'secrets': secrets,
    }


def _route_match_parts(route):
    match = route.get('match') if isinstance(route.get('match'), dict) else {}
    envs = [str(x).strip() for x in (match.get('envs') or []) if str(x).strip()]
    cats = [str(x).strip() for x in (match.get('categories') or []) if str(x).strip()]
    sevs = [str(x).strip() for x in (match.get('severities') or []) if str(x).strip()]
    return envs, cats, sevs


def _upsert_cluster_route(routes, env, cid):
    rid = f'rt_cluster_{env}'
    existing_route = next((r for r in routes if r.get('id') == rid), None)
    if existing_route is not None:
        existing_route['name'] = existing_route.get('name') or f'集群 {env}'
        existing_route['enabled'] = True
        existing_route['match'] = {'envs': [env], 'categories': [], 'severities': []}
        existing_route['channel_ids'] = [cid]
        existing_route['continue'] = False
        return

    target_route = {
        'id': rid,
        'name': f'集群 {env}',
        'enabled': True,
        'match': {'envs': [env], 'categories': [], 'severities': []},
        'channel_ids': [cid],
        'continue': False,
    }
    last_specific = -1
    for idx, route in enumerate(routes):
        envs, cats, sevs = _route_match_parts(route)
        if env in envs or ((not envs or '*' in envs) and (cats or sevs)):
            last_specific = idx

    insert_idx = len(routes)
    for idx in range(last_specific + 1, len(routes)):
        envs, cats, sevs = _route_match_parts(routes[idx])
        if (not envs or '*' in envs) and not cats and not sevs:
            insert_idx = idx
            break
    routes.insert(insert_idx, target_route)


def _merge_v1_into_v2(existing_v2, v1_cfg):
    """将 v1 PUT 变更回写进既有 v2 配置(不丢失 v2 扩展提供方和自定义路由)."""
    base = migrate_v1_to_v2(existing_v2)
    channels_map = dict(base.get('channels') or {})
    routes = [dict(r) for r in (base.get('routes') or [])]
    default_ids = list(base.get('default_channel_ids') or [])

    # default 处理
    new_default = v1_cfg.get('default') if isinstance(v1_cfg, dict) else None
    if isinstance(new_default, dict):
        _upsert_v2_from_v1_entry(channels_map, 'ch_default', '全局默认通道', new_default)
        if 'ch_default' not in default_ids:
            default_ids.insert(0, 'ch_default')
    else:
        default_ids = [cid for cid in default_ids if cid != 'ch_default']
        # 若其它路由不再引用 ch_default 则清理
        still_referenced = any('ch_default' in (r.get('channel_ids') or []) for r in routes)
        if not still_referenced:
            channels_map.pop('ch_default', None)

    # clusters 处理
    new_clusters = _saved_clusters(v1_cfg)
    for env, entry in new_clusters.items():
        cid = f'ch_cluster_{env}'
        _upsert_v2_from_v1_entry(channels_map, cid, f'集群 {env}', entry)
        _upsert_cluster_route(routes, env, cid)

    # 删除已从 v1 clusters 移除的自动生成集群路由(仅在无其它路由或 default_channel_ids 引用时删除通道)
    filtered_routes = []
    removed_cluster_cids = set()
    for route in routes:
        rid = str(route.get('id') or '')
        if rid.startswith('rt_cluster_'):
            env_name = rid[len('rt_cluster_'):]
            if env_name not in new_clusters:
                cid = f'ch_cluster_{env_name}'
                if route.get('channel_ids') == [cid]:
                    removed_cluster_cids.add(cid)
                    continue
        filtered_routes.append(route)

    for cid in removed_cluster_cids:
        still_used = cid in default_ids or any(
            cid in (r.get('channel_ids') or []) for r in filtered_routes
        )
        if not still_used:
            channels_map.pop(cid, None)

    return {
        'version': 2,
        'channels': channels_map,
        'routes': filtered_routes,
        'default_channel_ids': [cid for cid in default_ids if cid in channels_map],
    }


def _merge_cluster_into_v2(existing_v2, env, norm_entry):
    """save_cluster_channel 在已有 v2 存储时,仅更新/插入该集群的通道与路由."""
    base = migrate_v1_to_v2(existing_v2)
    channels_map = dict(base.get('channels') or {})
    routes = [dict(r) for r in (base.get('routes') or [])]
    default_ids = list(base.get('default_channel_ids') or [])

    cid = f'ch_cluster_{env}'
    _upsert_v2_from_v1_entry(channels_map, cid, f'集群 {env}', norm_entry)
    _upsert_cluster_route(routes, env, cid)

    return {
        'version': 2,
        'channels': channels_map,
        'routes': routes,
        'default_channel_ids': [c for c in default_ids if c in channels_map],
    }


def _same(old, new):
    return (
        old.get('type') == new.get('type')
        and (old.get('at') or '') == (new.get('at') or '')
        and (old.get('token') == new.get('token') or saved_token(old) == saved_token(new))
    )


def diff_scopes(old, new):
    """审计内容:哪些范围新增 / 修改 / 删除了(只比较,不写 token)"""
    old = _existing_as_v1(old)
    new = _existing_as_v1(new)
    old_default = old.get('default') if isinstance(old.get('default'), dict) else None
    new_default = new.get('default')
    if old_default is None:
        default = 'added' if new_default is not None else 'unchanged'
    elif new_default is None:
        default = 'removed'
    else:
        default = 'unchanged' if _same(old_default, new_default) else 'updated'
    old_clusters, new_clusters = _saved_clusters(old), new.get('clusters') or {}
    return {
        'default': default,
        'clusters': {
            'added': sorted(set(new_clusters) - set(old_clusters)),
            'updated': sorted(
                env
                for env in set(new_clusters) & set(old_clusters)
                if not isinstance(old_clusters[env], dict) or not _same(old_clusters[env], new_clusters[env])
            ),
            'removed': sorted(set(old_clusters) - set(new_clusters)),
        },
    }


def _diff_v2(old_v2, new_v2):
    """v2 配置审计对比(仅记录通道/路由 ID 变化,绝不记录敏感凭据)."""
    old_ch = (old_v2 or {}).get('channels') or {}
    new_ch = (new_v2 or {}).get('channels') or {}
    old_ids = set(old_ch)
    new_ids = set(new_ch)
    updated_ch = []
    for cid in sorted(old_ids & new_ids):
        o, n = old_ch[cid], new_ch[cid]
        if (
            o.get('provider') != n.get('provider')
            or o.get('name') != n.get('name')
            or bool(o.get('enabled', True)) != bool(n.get('enabled', True))
            or (o.get('settings') or {}) != (n.get('settings') or {})
            or (o.get('secrets') or {}) != (n.get('secrets') or {})
        ):
            updated_ch.append(cid)
    return {
        'version': 2,
        'channels': {
            'added': sorted(new_ids - old_ids),
            'updated': updated_ch,
            'removed': sorted(old_ids - new_ids),
        },
        'routes_count': len((new_v2 or {}).get('routes') or []),
        'default_channel_ids': list((new_v2 or {}).get('default_channel_ids') or []),
    }


# ---------------------------------------------------------------------------
# 给页面看的配置 (v1 + v2)
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
    return {
        'type': entry['type'],
        'at': str(entry.get('at') or ''),
        'configured': bool(token),
        'token_hint': token_hint(token),
    }


def public_config(value):
    value = _existing_as_v1(value)
    clusters = []
    for env, entry in sorted(_saved_clusters(value).items()):
        item = public_entry(entry)
        if item is not None:
            clusters.append({'env': env, **item})
    return {'default': public_entry(value.get('default')), 'clusters': clusters}


def _schema_for_web(raw_schema):
    schema = dict(raw_schema)
    fields = []
    for raw_field in raw_schema.get('fields') or []:
        field = dict(raw_field)
        if 'min_value' in field:
            field['min'] = field.pop('min_value')
        if 'max_value' in field:
            field['max'] = field.pop('max_value')
        if 'show_if' in field:
            field['visible_when'] = field.pop('show_if')
        fields.append(field)
    schema['fields'] = fields
    return schema


def _public_v2_channel(cid, ch, updated_at=None):
    provider_id = str(ch.get('provider') or ch.get('type') or '').strip().lower()
    settings = dict(ch.get('settings') or {}) if isinstance(ch.get('settings'), dict) else {}
    raw_secrets = ch.get('secrets') if isinstance(ch.get('secrets'), dict) else {}

    secret_hints = {}
    configured_secrets = []
    undecryptable = False

    for sk, sv in raw_secrets.items():
        if not sv:
            continue
        try:
            plain = decrypt_secret(str(sv))
        except Exception:
            undecryptable = True
            continue
        if plain:
            key = str(sk)
            secret_hints[key] = mask_hint(plain)
            configured_secrets.append(key)

    configured = not undecryptable
    try:
        provider = get_provider(provider_id)
        req_secrets = [f.key for f in provider.fields if f.secret and f.required]
        req_settings = [f.key for f in provider.fields if not f.secret and f.required]
        if req_secrets:
            configured = configured and all(k in configured_secrets for k in req_secrets)
        elif req_settings:
            configured = configured and all(bool(settings.get(k)) for k in req_settings)
    except ValueError:
        configured = False

    return {
        'id': str(ch.get('id') or cid),
        'name': str(ch.get('name') or cid),
        'provider': provider_id,
        'enabled': bool(ch.get('enabled', True)),
        'settings': settings,
        'secret_hints': secret_hints,
        'configured_secrets': configured_secrets,
        'configured': configured,
        'updated_at': updated_at,
    }


def public_config_v2(value, updated_at=None):
    v2 = migrate_v1_to_v2(value if isinstance(value, dict) else {})
    raw_channels = v2.get('channels') or {}
    channel_items = [
        _public_v2_channel(cid, ch, updated_at=updated_at)
        for cid, ch in raw_channels.items()
    ]
    return {
        'version': 2,
        'channels': channel_items,
        'routes': list(v2.get('routes') or []),
        'default_channel_ids': list(v2.get('default_channel_ids') or []),
    }


async def registered_envs():
    """k8s_agent_status 里已注册的集群(给页面下拉用),按名称排序;读不到返回空列表"""
    try:
        rows = await db.pg_fetch('SELECT env FROM k8s_agent_status ORDER BY env')
    except Exception as exc:
        logger.warning(f'读取已注册的集群失败: {exc}')
        return []
    return sorted({row['env'] for row in rows if row['env']})


async def _payload(row):
    return {
        **public_config(row['value'] if row else {}),
        'envs': await registered_envs(),
        'updated_by': row['updated_by'] if row else None,
        'updated_at': _iso(row['updated_at']) if row else None,
    }


async def _payload_v2(row):
    updated_at = _iso(row['updated_at']) if row else None
    return {
        **public_config_v2(row['value'] if row else {}, updated_at=updated_at),
        'envs': await registered_envs(),
        'updated_by': row['updated_by'] if row else None,
        'updated_at': updated_at,
    }


async def _load_row():
    """读库里的配置行;失败返回 (None, 503 响应)"""
    try:
        return await store.get_setting_row(SETTING_KEY), None
    except Exception as exc:
        logger.error(f'读取消息通知配置失败: {exc}')
        return None, _fail(503, '读取消息通知配置失败,请稍后重试', 'db_unavailable')


def _prepare_v2_raw(body):
    """兼容通道里传 config 或 settings 字段的归一化预处理."""
    if not isinstance(body, dict):
        return body
    prepared = dict(body)
    raw_channels = prepared.get('channels')
    if isinstance(raw_channels, list):
        fixed = []
        for entry in raw_channels:
            if isinstance(entry, dict) and 'settings' not in entry and isinstance(entry.get('config'), dict):
                copy_entry = dict(entry)
                copy_entry['settings'] = copy_entry.pop('config')
                fixed.append(copy_entry)
            else:
                fixed.append(entry)
        prepared['channels'] = fixed
    elif isinstance(raw_channels, dict):
        fixed = {}
        for cid, entry in raw_channels.items():
            if isinstance(entry, dict) and 'settings' not in entry and isinstance(entry.get('config'), dict):
                copy_entry = dict(entry)
                copy_entry['settings'] = copy_entry.pop('config')
                fixed[cid] = copy_entry
            else:
                fixed[cid] = entry
        prepared['channels'] = fixed
    return prepared


def _assert_saved_secrets_decryptable(raw_body, existing_v2):
    """若请求对已保存凭据留空且同 provider,预先校验密文可解密;避免密钥轮换后静默丢弃可选凭据."""
    if not isinstance(raw_body, dict) or not isinstance(existing_v2, dict):
        return
    saved_channels = existing_v2.get('channels') or {}
    raw_channels = raw_body.get('channels')
    items = []
    if isinstance(raw_channels, list):
        items = [(str(c.get('id') or '').strip(), c) for c in raw_channels if isinstance(c, dict)]
    elif isinstance(raw_channels, dict):
        items = [(str(cid), c) for cid, c in raw_channels.items() if isinstance(c, dict)]

    for cid, ch in items:
        saved = saved_channels.get(cid)
        if not isinstance(saved, dict):
            continue
        provider_id = str(ch.get('provider') or ch.get('type') or '').strip().lower()
        saved_provider = str(saved.get('provider') or saved.get('type') or '').strip().lower()
        if not provider_id or provider_id != saved_provider:
            continue
        raw_secrets = ch.get('secrets') if isinstance(ch.get('secrets'), dict) else {}
        saved_secrets = saved.get('secrets') if isinstance(saved.get('secrets'), dict) else {}
        name = str(ch.get('name') or cid).strip() or cid
        for sk, sv in saved_secrets.items():
            if not sv:
                continue
            submitted = str(raw_secrets.get(sk) or '').strip()
            if submitted:
                continue
            try:
                plain = decrypt_secret(str(sv))
            except Exception:
                plain = ''
            if not plain:
                raise ValueError(f'通道 {name} 已保存的 {sk} 无法解密(AI_ENCRYPTION_KEY 换过),请重新填写')


# ---------------------------------------------------------------------------
# 管理接口(系统管理员)
# ---------------------------------------------------------------------------
async def get_providers(request):
    """GET /api/admin/notification/providers: 返回支持的通知提供方元数据"""
    return web.json_response({'success': True, 'data': [_schema_for_web(item) for item in provider_schemas()]})


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
            v1_cfg = normalize_config(body, existing)
        except ValueError as exc:
            return _fail(422, str(exc), 'invalid_config')
        changes = diff_scopes(existing, v1_cfg)
        to_save = _merge_v1_into_v2(existing, v1_cfg) if existing.get('version') == 2 else v1_cfg
        try:
            saved = await store.put_setting_row(SETTING_KEY, to_save, principal.username)
        except Exception as exc:
            logger.error(f'保存消息通知配置失败: {exc}')
            return _fail(503, '保存消息通知配置失败,请稍后重试', 'db_unavailable')
        service.apply(saved['value'])  # 保存后立即生效,不等后台刷新
    audit.record_bg(principal.username, AUDIT_UPDATE, ip=client_ip(request), detail=changes)
    return web.json_response({'success': True, 'data': await _payload(saved)})


async def get_config_v2(request):
    """GET /api/admin/notification/v2: 返回标准化 v2 多通道配置"""
    row, error = await _load_row()
    if error is not None:
        return error
    return web.json_response({'success': True, 'data': await _payload_v2(row)})


async def put_config_v2(request):
    """PUT /api/admin/notification/v2: 校验并保存 v2 多通道配置与路由"""
    principal = principal_of(request)
    body = await _read_body(request)
    if body is None:
        return _fail(400, '请求体必须是 JSON 对象')
    prepared = _prepare_v2_raw(body)
    async with _save_lock:
        row, error = await _load_row()
        if error is not None:
            return error
        existing = row['value'] if row and isinstance(row['value'], dict) else {}
        existing_v2 = migrate_v1_to_v2(existing)
        try:
            _assert_saved_secrets_decryptable(prepared, existing_v2)
            cfg = normalize_v2_config(
                prepared,
                existing_v2,
                secret_decryptor=decrypt_secret,
                secret_encryptor=encrypt_secret,
                allow_reuse_saved=True,
            )
        except SSRFError as exc:
            return _fail(422, str(exc), 'ssrf_blocked')
        except ValueError as exc:
            return _fail(422, str(exc), 'invalid_config')
        changes = _diff_v2(existing_v2, cfg)
        try:
            saved = await store.put_setting_row(SETTING_KEY, cfg, principal.username)
        except Exception as exc:
            logger.error(f'保存 v2 消息通知配置失败: {exc}')
            return _fail(503, '保存消息通知配置失败,请稍后重试', 'db_unavailable')
        service.apply(saved['value'])
    audit.record_bg(principal.username, AUDIT_UPDATE, ip=client_ip(request), detail=changes)
    return web.json_response({'success': True, 'data': await _payload_v2(saved)})


async def get_deliveries(request):
    """GET /api/admin/notification/deliveries: 查询通知投递审计日志"""
    env = (request.query.get('env') or '').strip()
    provider = (request.query.get('provider') or '').strip().lower()
    raw_ok = (request.query.get('ok') or '').strip().lower()
    ok_filter = None
    if raw_ok in ('true', '1', 'yes'):
        ok_filter = True
    elif raw_ok in ('false', '0', 'no'):
        ok_filter = False
    elif raw_ok:
        return _fail(422, f'无效的 ok 过滤参数: {raw_ok}', 'invalid_query')

    raw_limit = (request.query.get('limit') or '').strip()
    raw_offset = (request.query.get('offset') or '').strip()
    try:
        limit = int(raw_limit) if raw_limit else 100
        offset = int(raw_offset) if raw_offset else 0
    except ValueError:
        return _fail(422, 'limit 与 offset 必须是整数', 'invalid_query')

    items, total = await service.query_deliveries(
        env=env,
        provider=provider,
        ok=ok_filter,
        limit=limit,
        offset=offset,
    )
    return web.json_response({'success': True, 'data': {'items': items, 'total': total}})


async def save_cluster_channel(env, entry, updated_by):
    """网页安装 Agent 上线后,把安装时填的通知配置写进 clusters[env]:已有该集群的单独配置就覆盖,
    全局配置和其他集群原样保留。entry 是 {type, token, at}(token 明文;留空沿用该集群已保存的,规则同 PUT)。

    校验 / 加密和 PUT 一样,与 PUT 共用一把锁;写完立即生效,记审计 notification.update(detail 带 source: agent-install)。
    不合法抛 ValueError(中文说明,不含 token),读写库失败抛原来的异常。"""
    env = check_env(env)
    async with _save_lock:
        row = await store.get_setting_row(SETTING_KEY)
        existing = row['value'] if row and isinstance(row['value'], dict) else {}
        if existing.get('version') == 2:
            v1_existing = _existing_as_v1(existing)
            clusters = dict(_saved_clusters(v1_existing))
            if env not in clusters and len(clusters) >= MAX_CLUSTERS:
                raise ValueError(f'按集群单独配置最多 {MAX_CLUSTERS} 个')
            norm_entry = normalize_entry(entry, clusters.get(env), _scope(env))
            clusters[env] = norm_entry
            new_v1 = {'default': v1_existing['default']} if isinstance(v1_existing.get('default'), dict) else {}
            new_v1['clusters'] = clusters
            changes = diff_scopes(v1_existing, new_v1)
            to_save = _merge_cluster_into_v2(existing, env, norm_entry)
        else:
            clusters = dict(_saved_clusters(existing))
            if env not in clusters and len(clusters) >= MAX_CLUSTERS:
                raise ValueError(f'按集群单独配置最多 {MAX_CLUSTERS} 个')
            clusters[env] = normalize_entry(entry, clusters.get(env), _scope(env))
            to_save = {'default': existing['default']} if isinstance(existing.get('default'), dict) else {}
            to_save['clusters'] = clusters
            changes = diff_scopes(existing, to_save)
        saved = await store.put_setting_row(SETTING_KEY, to_save, updated_by)
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


async def _run_test_v2(request, principal, body, env):
    provider_id = str(body.get('provider') or '').strip().lower()
    channel_id = str(body.get('channel_id') or '').strip()
    channel_name = str(body.get('name') or channel_id or '').strip()
    raw_settings = (
        body.get('settings')
        if isinstance(body.get('settings'), dict)
        else (body.get('config') if isinstance(body.get('config'), dict) else {})
    )
    raw_secrets = body.get('secrets') if isinstance(body.get('secrets'), dict) else {}

    row, error = await _load_row()
    if error is not None:
        return error
    existing = row['value'] if row and isinstance(row['value'], dict) else {}
    existing_v2 = migrate_v1_to_v2(existing)
    saved_ch = (existing_v2.get('channels') or {}).get(channel_id) if channel_id else None

    try:
        provider = get_provider(provider_id)
        saved_plain = None
        can_reuse = (
            isinstance(saved_ch, dict)
            and str(saved_ch.get('provider') or saved_ch.get('type') or '').strip().lower() == provider.id
        )
        if can_reuse:
            saved_plain = {}
            raw_saved_secrets = saved_ch.get('secrets') if isinstance(saved_ch.get('secrets'), dict) else {}
            for sk, sv in raw_saved_secrets.items():
                if not sv:
                    continue
                submitted = str(raw_secrets.get(sk) or '').strip()
                try:
                    plain = decrypt_secret(str(sv))
                except Exception:
                    plain = ''
                if not plain and not submitted:
                    raise ValueError(
                        f'通道 {channel_name or channel_id} 已保存的 {sk} 无法解密(AI_ENCRYPTION_KEY 换过),请重新填写'
                    )
                if plain:
                    saved_plain[str(sk)] = plain
        norm_settings, norm_secrets = provider.normalize(
            raw_settings,
            raw_secrets,
            saved_secrets=saved_plain,
            allow_reuse_saved=can_reuse,
        )
    except SSRFError as exc:
        return _fail(422, str(exc), 'ssrf_blocked')
    except ValueError as exc:
        return _fail(422, str(exc), 'invalid_config')

    wait = _test_wait(principal.username)
    if wait:
        return _fail(429, f'测试太频繁,请 {wait} 秒后再试', 'too_many_requests', retry_after=wait)

    scope = channel_name or (f'集群 {env}' if env else provider.label)
    test_body = channels.test_content(scope, principal.username, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    category = str(body.get('category') or 'test').strip().lower() or 'test'
    severity = str(body.get('severity') or 'test').strip().lower() or 'test'
    msg = build_event_message(
        title='KubeDoor 消息通知测试',
        body=test_body,
        env=env,
        category=category,
        severity=severity,
        at=str(norm_settings.get('at') or ''),
    )
    channel_cfg = ChannelConfig(
        id=channel_id or f'test_{provider.id}',
        name=channel_name or provider.label,
        provider=provider.id,
        enabled=True,
        settings=norm_settings,
        secrets=norm_secrets,
        rate_limit=provider.default_rate_limit,
    )
    res = await asyncio.to_thread(service.deliver_message, channel_cfg, msg, record=True)
    if not res.ok:
        logger.warning(f'v2 消息通知测试失败({scope},{provider.label}):{res.message}')
    reused_keys = sorted(
        k
        for k in norm_secrets
        if can_reuse and not str(raw_secrets.get(k) or '').strip()
    )
    audit.record_bg(
        principal.username,
        AUDIT_TEST,
        env=env,
        ip=client_ip(request),
        success=res.ok,
        detail={
            'version': 2,
            'channel_id': channel_id or None,
            'provider': provider.id,
            'saved_secrets': reused_keys,
            'message': res.message,
        },
    )
    return web.json_response({'success': True, 'data': {'ok': res.ok, 'message': res.message}})


async def run_test(request):
    """发一条测试消息:兼容 v1 {env, type, token, at} 与 v2 {channel_id?, provider, name?, settings/config, secrets}。
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

    is_v2 = ('provider' in body) or ('settings' in body) or ('config' in body) or ('channel_id' in body)
    if is_v2:
        return await _run_test_v2(request, principal, body, env)

    scope = _scope(env)
    row, error = await _load_row()
    if error is not None:
        return error
    existing = _existing_as_v1(row['value'] if row and isinstance(row['value'], dict) else {})
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
    audit.record_bg(
        principal.username,
        AUDIT_TEST,
        env=env,
        ip=client_ip(request),
        success=ok,
        detail={'scope': env or 'default', 'type': kind, 'saved_token': reused, 'message': message},
    )
    return web.json_response({'success': True, 'data': {'ok': ok, 'message': message}})


# ---------------------------------------------------------------------------
# 内部接口(kubedoor-alarm)
# ---------------------------------------------------------------------------
async def internal_channel(request):
    """[Deprecated] kubedoor-alarm 旧版取明文通道接口:保留 1 个大版本供滚动升级过渡。"""
    principal = principal_of(request)
    if principal.via != 'system':
        return _fail(403, '仅限集群内服务用内部 token 调用', 'forbidden')
    env = (request.query.get('env') or '').strip()
    if not service.is_loaded() and not await service.refresh():
        return _fail(503, '消息通知配置还没有加载,请稍后重试', 'not_ready')
    response = web.json_response({'success': True, 'data': service.resolve(env)})
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Deprecation'] = 'true'
    return response


async def internal_notify(request):
    """POST /api/internal/notify: kubedoor-alarm 统一代理投递告警/事件通知。
    仅限不带用户名的系统内部服务身份(principal.via == 'system')调用,确保明文凭据永不出 master。"""
    principal = principal_of(request)
    if principal.via != 'system':
        return _fail(403, '仅限集群内服务用内部 token 调用', 'forbidden')

    body = await _read_body(request)
    if body is None:
        return _fail(400, '请求体必须是 JSON 对象')

    if not service.is_loaded() and not await service.refresh():
        return _fail(503, '消息通知配置还没有加载,请稍后重试', 'not_ready')

    env = str(body.get('env') or '').strip()
    if len(env) > 200:
        return _fail(422, 'env 参数过长', 'invalid_config')
    category = str(body.get('category') or '').strip().lower()
    severity = str(body.get('severity') or '').strip().lower()
    title = str(body.get('title') or '').strip()
    text_body = str(body.get('body') or '').strip()
    at = str(body.get('at') or '').strip()
    raw_items = body.get('items')
    raw_links = body.get('links')
    sync = bool(body.get('sync', False))

    links_tuple = ()
    if raw_links is not None:
        if not isinstance(raw_links, list):
            return _fail(422, 'links 必须是列表', 'invalid_config')
        cleaned_links = []
        for link in raw_links:
            if not isinstance(link, dict):
                return _fail(422, 'links 项必须是对象', 'invalid_config')
            url = str(link.get('url') or '').strip()
            if url:
                cleaned_links.append({'label': str(link.get('label') or url).strip(), 'url': url})
        links_tuple = tuple(cleaned_links)

    if raw_items is not None:
        if not isinstance(raw_items, list):
            return _fail(422, 'items 必须是列表', 'invalid_config')
        if raw_items:
            try:
                msg = build_alert_message(
                    raw_items,
                    env=env,
                    at=at,
                    category=category or 'metric_alert',
                    title=title,
                    metadata={'links': list(links_tuple)} if links_tuple else None,
                )
            except ValueError as exc:
                return _fail(422, str(exc), 'invalid_config')
        else:
            if not (title or text_body):
                return _fail(400, '缺少通知内容', 'bad_request')
            msg = build_event_message(
                title=title or '通知',
                body=text_body,
                env=env,
                category=category or 'generic',
                severity=severity or 'info',
                at=at,
                links=links_tuple,
            )
    else:
        if not (title or text_body):
            return _fail(400, '缺少通知内容', 'bad_request')
        msg = build_event_message(
            title=title or '通知',
            body=text_body,
            env=env,
            category=category or 'generic',
            severity=severity or 'info',
            at=at,
            links=links_tuple,
        )

    trace_id = uuid.uuid4().hex
    targets = service.resolve_targets(env=msg.env, category=msg.category, severity=msg.severity)
    if not targets:
        data = {'matched': 0, 'queued': 0, 'trace_id': trace_id}
        if sync:
            data['results'] = []
        resp = web.json_response({'success': True, 'data': data})
        resp.headers['Cache-Control'] = 'no-store'
        return resp

    if sync:
        deliveries = await asyncio.gather(
            *(
                asyncio.to_thread(service.deliver_message, ch, msg, trace_id=trace_id, record=True)
                for ch in targets
            )
        )
        results = [
            {
                'channel_id': ch.id,
                'channel_name': ch.name,
                'provider': ch.provider,
                'ok': res.ok,
                'message': res.message,
                'status_code': res.status_code,
                'attempts': res.attempts,
            }
            for ch, res in zip(targets, deliveries)
        ]
        resp = web.json_response({
            'success': True,
            'data': {
                'matched': len(targets),
                'queued': len(targets),
                'trace_id': trace_id,
                'results': results,
            },
        })
        resp.headers['Cache-Control'] = 'no-store'
        return resp

    for ch in targets:
        service._dispatcher.submit(ch, msg, service._scope_for_channel(ch, msg.env), trace_id=trace_id)

    resp = web.json_response({
        'success': True,
        'data': {
            'matched': len(targets),
            'queued': len(targets),
            'trace_id': trace_id,
        },
    })
    resp.headers['Cache-Control'] = 'no-store'
    return resp


# ---------------------------------------------------------------------------
# 注册与启停
# ---------------------------------------------------------------------------
def register_routes(app):
    app.router.add_get('/api/admin/notification', get_config)
    app.router.add_put('/api/admin/notification', put_config)
    app.router.add_get('/api/admin/notification/providers', get_providers)
    app.router.add_get('/api/admin/notification/v2', get_config_v2)
    app.router.add_put('/api/admin/notification/v2', put_config_v2)
    app.router.add_get('/api/admin/notification/deliveries', get_deliveries)
    app.router.add_post('/api/admin/notification/test', run_test)
    app.router.add_get('/api/internal/notification', internal_channel)
    app.router.add_post('/api/internal/notify', internal_notify)


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
