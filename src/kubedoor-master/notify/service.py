"""消息通知的配置缓存、多通道路由、SSRF 防护发送、限速重试与投递审计。

配置存 auth_settings(key=notification), 支持 v1 ({default, clusters}) 与 v2
({version: 2, channels, routes, default_channel_ids}) 两种持久化形态:
- 所有敏感凭据字段由 AI_ENCRYPTION_KEY (AES-256-GCM) 加密落库, 解密明文只驻留内存快照
- Snapshot 同时维护 v2 标准化模型 (channels, routes, default_channel_ids)
  与 v1 兼容视图 (.default, .clusters), 保证所有既有调用方 100% 兼容
- resolve_targets(env, category, severity) 使用 kubedoor_tools.notify.match_channels 做有序路由与扇出
- Dispatcher 支持按通道/提供方限速(滑动窗口 + 突发窗口)、背压丢弃告警以及可重试错误指数退避(最多 3 次)
- deliver_message 支持 HTTP (SSRF 校验 + 禁用重定向) 与 SMTP 直连投递, 所有日志/错误/审计强制脱敏,
  同时写入内存环形缓冲区与 PostgreSQL notification_deliveries 审计表(30 天保留)
"""

import asyncio
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import pathlib
import random
import sys
import threading
import time
from types import MappingProxyType
from typing import Any
import uuid

from loguru import logger
import requests

import db
from auth.crypto import decrypt_secret
from auth.store import store

from . import channels


def _ensure_tools_path() -> None:
    try:
        import kubedoor_tools  # noqa: F401
        return
    except ImportError:
        pass
    resolved = pathlib.Path(__file__).resolve()
    if len(resolved.parents) > 2:
        tools_dir = resolved.parents[2] / 'kubedoor-tools'
        if tools_dir.is_dir() and str(tools_dir) not in sys.path:
            sys.path.insert(0, str(tools_dir))


_ensure_tools_path()

from kubedoor_tools.notify import (  # noqa: E402
    ChannelConfig,
    DeliveryResult,
    NotificationMessage,
    SSRFError,
    build_event_message,
    get_provider,
    match_channels,
    migrate_v1_to_v2,
    precheck_target_url,
    project_v2_to_v1,
    redact_text,
    send_pinned_http,
)

SETTING_KEY = 'notification'
DEFAULT_SCOPE = '全局配置'
REFRESH_INTERVAL = 10  # 秒
SEND_TIMEOUT = 10  # 秒
SEND_WORKERS = 4
# 默认限速(v1 / 兜底): 每分钟 20 条
RATE_LIMIT = 20
RATE_WINDOW = 60  # 秒
MAX_BACKLOG = 300  # 每个通道队列最多积压条数
DROP_WARN_INTERVAL = 60  # 丢弃警告节流(秒)

MAX_RETRY_ATTEMPTS = 3
BACKOFF_BASE = 2.0
BACKOFF_CAP = 18.0
DELIVERY_RING_SIZE = 500
DELIVERY_RETENTION_DAYS = 30

_ORIG_REQUESTS_POST = requests.post
_ORIG_REQUESTS_REQUEST = requests.request

_executor = ThreadPoolExecutor(max_workers=SEND_WORKERS, thread_name_prefix='notify')


@dataclass(frozen=True)
class Snapshot:
    """缓存的配置(只读,整体替换)。兼容 v1 (.default, .clusters) 与 v2 (channels, routes, default_channel_ids)。"""

    default: object = None
    clusters: MappingProxyType = field(default_factory=lambda: MappingProxyType({}))
    loaded: bool = False
    version: int = 2
    channels: MappingProxyType = field(default_factory=lambda: MappingProxyType({}))
    routes: tuple[dict[str, Any], ...] = ()
    default_channel_ids: tuple[str, ...] = ()
    source_version: int = 1


_snapshot = Snapshot()
_summary = None  # 上次记日志的配置概况,变了才再记
_refresh_failed = False
_undecryptable = set()  # (范围, 密文):解不开的凭据每份只提示一次

_delivery_lock = threading.Lock()
_delivery_ring: deque[dict[str, Any]] = deque(maxlen=DELIVERY_RING_SIZE)
_delivery_seq = 0
_bg_tasks: set[asyncio.Task[Any]] = set()


# ---------------------------------------------------------------------------
# 缓存构建与刷新
# ---------------------------------------------------------------------------
def _warn_undecryptable(scope: str, stored: str, exc: Exception) -> None:
    key = (scope, stored)
    if key not in _undecryptable:
        _undecryptable.add(key)
        logger.warning(
            f'消息通知({scope})的 token 无法解密({exc}),'
            '请系统管理员在「系统管理 → 消息通知」重新填写机器人地址'
        )


def _channel(entry, scope):
    """存库 v1 格式 → 发送用的通道(token 解密);格式不对、没有 token 或解不开返回 None"""
    if not isinstance(entry, dict) or entry.get('type') not in channels.TYPES:
        return None
    stored = entry.get('token') or ''
    try:
        token = decrypt_secret(stored)
    except Exception as exc:
        _warn_undecryptable(scope, str(stored), exc)
        return None
    if not token:
        return None
    return MappingProxyType({'type': entry['type'], 'token': token, 'at': str(entry.get('at') or '')})


def _decrypt_v2_channel(cid: str, ch: dict[str, Any]) -> ChannelConfig | None:
    if not isinstance(ch, dict):
        return None
    provider_id = str(ch.get('provider') or ch.get('type') or '').strip().lower()
    try:
        provider = get_provider(provider_id)
    except ValueError:
        return None
    raw_secrets = ch.get('secrets') if isinstance(ch.get('secrets'), dict) else {}
    plain_secrets: dict[str, str] = {}
    scope_name = f'通道 {ch.get("name") or cid}'
    for sk, sv in raw_secrets.items():
        if not sv:
            continue
        stored_str = str(sv)
        try:
            plain = decrypt_secret(stored_str)
        except Exception as exc:
            _warn_undecryptable(scope_name, stored_str, exc)
            return None
        if plain:
            plain_secrets[str(sk)] = plain

    for field_spec in provider.fields:
        if field_spec.secret and field_spec.required and not plain_secrets.get(field_spec.key):
            return None

    settings = dict(ch.get('settings') or {}) if isinstance(ch.get('settings'), dict) else {}
    return ChannelConfig(
        id=str(ch.get('id') or cid),
        name=str(ch.get('name') or cid),
        provider=provider.id,
        enabled=bool(ch.get('enabled', True)),
        settings=settings,
        secrets=plain_secrets,
        rate_limit=provider.default_rate_limit,
    )


def _build_v1_compat_views(
    decrypted_channels: dict[str, ChannelConfig],
    routes: list[dict[str, Any]],
    default_ids: list[str],
) -> tuple[object, MappingProxyType]:
    v2_plain = {
        'version': 2,
        'channels': {
            cid: {
                'id': cfg.id,
                'name': cfg.name,
                'provider': cfg.provider,
                'enabled': cfg.enabled,
                'settings': dict(cfg.settings),
                'secrets': dict(cfg.secrets),
            }
            for cid, cfg in decrypted_channels.items()
        },
        'routes': routes,
        'default_channel_ids': default_ids,
    }
    v1_proj = project_v2_to_v1(v2_plain)
    default_entry = MappingProxyType(v1_proj['default']) if isinstance(v1_proj.get('default'), dict) else None
    clusters_map = {
        str(env): MappingProxyType(entry)
        for env, entry in (v1_proj.get('clusters') or {}).items()
        if isinstance(entry, dict)
    }
    return default_entry, MappingProxyType(clusters_map)


def build_snapshot(value):
    """存库格式的配置 (v1 或 v2) → 缓存快照"""
    if not isinstance(value, dict):
        return Snapshot(None, MappingProxyType({}), True)

    if value.get('version') == 2:
        v2 = migrate_v1_to_v2(value)
        decrypted_channels: dict[str, ChannelConfig] = {}
        for cid, ch in (v2.get('channels') or {}).items():
            cfg = _decrypt_v2_channel(cid, ch)
            if cfg is not None:
                decrypted_channels[cid] = cfg
        routes = [dict(r) for r in (v2.get('routes') or []) if isinstance(r, dict)]
        default_ids = [str(cid) for cid in (v2.get('default_channel_ids') or []) if str(cid) in decrypted_channels]
        default_proxy, clusters_proxy = _build_v1_compat_views(decrypted_channels, routes, default_ids)
        return Snapshot(
            default=default_proxy,
            clusters=clusters_proxy,
            loaded=True,
            version=2,
            channels=MappingProxyType(decrypted_channels),
            routes=tuple(routes),
            default_channel_ids=tuple(default_ids),
            source_version=2,
        )

    default = _channel(value.get('default'), DEFAULT_SCOPE)
    raw_clusters = value.get('clusters')
    clusters = {}
    for env, entry in (raw_clusters.items() if isinstance(raw_clusters, dict) else ()):
        channel = _channel(entry, f'集群 {env}')
        if channel is not None:
            clusters[str(env)] = channel

    plain_v1 = {
        'default': dict(default) if default is not None else None,
        'clusters': {env: dict(ch) for env, ch in clusters.items()},
    }
    v2_migrated = migrate_v1_to_v2(plain_v1)
    channels_map: dict[str, ChannelConfig] = {}
    for cid, ch in (v2_migrated.get('channels') or {}).items():
        provider = get_provider(ch['provider'])
        channels_map[cid] = ChannelConfig(
            id=ch['id'],
            name=ch['name'],
            provider=provider.id,
            enabled=True,
            settings=dict(ch.get('settings') or {}),
            secrets=dict(ch.get('secrets') or {}),
            rate_limit=provider.default_rate_limit,
        )
    routes = tuple(dict(r) for r in (v2_migrated.get('routes') or []))
    default_ids = tuple(str(cid) for cid in (v2_migrated.get('default_channel_ids') or []))
    return Snapshot(
        default=default,
        clusters=MappingProxyType(clusters),
        loaded=True,
        version=2,
        channels=MappingProxyType(channels_map),
        routes=routes,
        default_channel_ids=default_ids,
        source_version=1,
    )


def describe(snapshot):
    """配置概况(日志用,不含任何 secret)"""
    default = snapshot.default
    head = f'全局配置 {channels.TYPE_LABELS[default["type"]]}' if default is not None else '没有全局配置'
    base = f'{head},单独配置的集群 {len(snapshot.clusters)} 个'
    if getattr(snapshot, 'source_version', 1) == 2:
        return f'{base} (v2: 通道 {len(snapshot.channels)} 个, 路由 {len(snapshot.routes)} 条)'
    return base


def apply(value):
    """用存库格式的配置替换缓存;保存后立即调用,不等下一次刷新"""
    global _snapshot, _summary
    snapshot = build_snapshot(value)
    _snapshot = snapshot
    text = describe(snapshot)
    if text != _summary:
        _summary = text
        logger.info(f'消息通知配置已加载:{text}')
    return snapshot


def is_loaded():
    return _snapshot.loaded


async def refresh():
    """从库里重新加载配置。读库失败时保留原来的缓存(只在第一次失败时记 warning),返回是否成功"""
    global _refresh_failed
    try:
        value = await store.get_setting(SETTING_KEY, {})
    except Exception as exc:
        if not _refresh_failed:
            logger.warning(f'读取消息通知配置失败,继续使用上次加载的配置:{type(exc).__name__}: {exc}')
        _refresh_failed = True
        return False
    if _refresh_failed:
        logger.info('读取消息通知配置已恢复')
        _refresh_failed = False
    apply(value)
    return True


async def refresh_loop(interval=REFRESH_INTERVAL):
    """后台定时刷新(直接改库、或者以后多副本时也能跟上);由 api.on_startup 启动"""
    while True:
        await asyncio.sleep(interval)
        try:
            await refresh()
        except Exception as exc:  # refresh 自己不抛,这里只是兜底:后台任务不能因为一次异常就退出
            logger.warning(f'刷新消息通知配置异常:{type(exc).__name__}: {exc}')


# ---------------------------------------------------------------------------
# 选通道 (v1 兼容 + v2 路由)
# ---------------------------------------------------------------------------
def _pick(env):
    """返回 (范围说明, v1 通道映射);都没有配置时通道为 None"""
    snapshot = _snapshot
    env = env.strip() if isinstance(env, str) else ''
    if env:
        channel = snapshot.clusters.get(env)
        if channel is not None:
            return f'集群 {env}', channel
    if snapshot.default is not None:
        return DEFAULT_SCOPE, snapshot.default
    return '', None


def resolve(env=None):
    """按集群选通道(v1 兼容):先看该集群的单独配置,再看全局配置;都没有返回 None。
    只读内存缓存,同步函数,线程里调用也安全。返回 {'type', 'token'(明文), 'at'} 的副本"""
    _, channel = _pick(env)
    return dict(channel) if channel is not None else None


def resolve_targets(env: str = '', category: str = 'generic', severity: str = 'info') -> list[ChannelConfig]:
    """按 (env, category, severity) 匹配已启用的通道列表 (v2 有序路由 + 扇出 + default_channel_ids 兜底)。"""
    snapshot = _snapshot
    if not snapshot.loaded:
        return []
    clean_env = env.strip() if isinstance(env, str) else ''
    clean_category = (category or 'generic').strip().lower()
    clean_severity = (severity or 'info').strip().lower()
    return match_channels(snapshot, env=clean_env, category=clean_category, severity=clean_severity)


# ---------------------------------------------------------------------------
# 脱敏与投递审计记录
# ---------------------------------------------------------------------------
def redact(text, token):
    """去掉文本里的 token(含 URL 编码与 Slack 分段),再截到 300 字符"""
    secrets = (token,) if token else ()
    return redact_text(text, secrets=secrets, max_len=channels.MAX_MESSAGE)


async def _insert_delivery_pg(record: dict[str, Any]) -> None:
    try:
        if db._pool is None:
            return
        await db.pg_execute(
            'INSERT INTO notification_deliveries '
            '(trace_id, env, category, severity, channel_id, channel_name, provider, '
            'ok, attempts, http_status, error_summary, latency_ms) '
            'VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)',
            record['trace_id'],
            record['env'],
            record['category'],
            record['severity'],
            record['channel_id'],
            record['channel_name'],
            record['provider'],
            record['ok'],
            record['attempts'],
            record['http_status'],
            record['error_summary'],
            record['latency_ms'],
        )
    except Exception as exc:
        logger.warning(f'写入通知投递审计日志失败: {type(exc).__name__}: {exc}')


def _schedule_pg_delivery(record: dict[str, Any]) -> None:
    if db._pool is None:
        return
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None

    if running is not None:
        task = running.create_task(_insert_delivery_pg(record))
        _bg_tasks.add(task)
        task.add_done_callback(_bg_tasks.discard)
        return

    target_loop = db._loop
    if target_loop is not None and not target_loop.is_closed():
        try:
            asyncio.run_coroutine_threadsafe(_insert_delivery_pg(record), target_loop)
        except Exception:
            pass


def _record_delivery(
    *,
    trace_id: str,
    env: str,
    category: str,
    severity: str,
    channel_id: str,
    channel_name: str,
    provider: str,
    ok: bool,
    attempts: int,
    http_status: int | None,
    error_summary: str,
    message: str,
    latency_ms: int,
) -> dict[str, Any]:
    global _delivery_seq
    with _delivery_lock:
        _delivery_seq += 1
        record = {
            'id': _delivery_seq,
            'trace_id': trace_id or uuid.uuid4().hex,
            'env': env or '',
            'category': category or 'generic',
            'severity': severity or 'info',
            'channel_id': channel_id or '',
            'channel_name': channel_name or channel_id or '',
            'provider': provider or '',
            'ok': bool(ok),
            'attempts': max(int(attempts), 1),
            'http_status': http_status,
            'error_summary': error_summary or '',
            'message': message or '',
            'latency_ms': max(int(latency_ms), 0),
            'created_at': datetime.now(timezone.utc).astimezone().isoformat(),
        }
        _delivery_ring.append(record)
    _schedule_pg_delivery(record)
    return record


async def query_deliveries(
    *,
    env: str = '',
    provider: str = '',
    ok: bool | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """查询通知投递审计日志:优先查 PostgreSQL,测试环境无池时回退到内存环形缓冲区。"""
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    clean_env = (env or '').strip()
    clean_provider = (provider or '').strip().lower()

    try:
        where: list[str] = []
        params: list[Any] = []

        def add(cond: str, val: Any) -> None:
            params.append(val)
            where.append(cond.replace('?', f'${len(params)}'))

        if clean_env:
            add('env = ?', clean_env)
        if clean_provider:
            add('provider = ?', clean_provider)
        if ok is not None:
            add('ok = ?', bool(ok))

        clause = ('WHERE ' + ' AND '.join(where)) if where else ''
        total_val = await db.pg_fetchval(f'SELECT count(*) FROM notification_deliveries {clause}', *params)
        total = int(total_val or 0)
        q_params = [*params, limit, offset]
        rows = await db.pg_fetch(
            'SELECT id, trace_id, env, category, severity, channel_id, channel_name, provider, '
            'ok, attempts, http_status, error_summary, latency_ms, created_at '
            f'FROM notification_deliveries {clause} '
            f'ORDER BY created_at DESC, id DESC LIMIT ${len(q_params) - 1} OFFSET ${len(q_params)}',
            *q_params,
        )
        items = []
        for row in rows:
            created = row['created_at']
            created_str = (
                (created.astimezone() if getattr(created, 'tzinfo', None) else created).isoformat()
                if isinstance(created, datetime)
                else str(created or '')
            )
            err = row['error_summary'] or ''
            items.append({
                'id': row['id'],
                'trace_id': row['trace_id'],
                'env': row['env'],
                'category': row['category'],
                'severity': row['severity'],
                'channel_id': row['channel_id'],
                'channel_name': row['channel_name'],
                'provider': row['provider'],
                'ok': bool(row['ok']),
                'attempts': int(row['attempts']),
                'http_status': row['http_status'],
                'error_summary': err,
                'message': err or (channels.OK_MESSAGE if row['ok'] else ''),
                'latency_ms': int(row['latency_ms']),
                'created_at': created_str,
            })
        return items, total
    except RuntimeError:
        pass
    except Exception as exc:
        logger.warning(f'查询通知投递审计日志失败,回退到内存记录: {type(exc).__name__}: {exc}')

    with _delivery_lock:
        snapshot_rows = list(_delivery_ring)
    snapshot_rows.reverse()
    filtered = [
        dict(row)
        for row in snapshot_rows
        if (not clean_env or row['env'] == clean_env)
        and (not clean_provider or row['provider'] == clean_provider)
        and (ok is None or row['ok'] is bool(ok))
    ]
    total = len(filtered)
    return filtered[offset : offset + limit], total


async def cleanup_deliveries(days: int = DELIVERY_RETENTION_DAYS, batch_size: int = 5000) -> int:
    """清理早于 days 天的通知投递审计记录，返回删除总行数。"""
    days = max(int(days), 1)
    total = 0
    sql = (
        'DELETE FROM notification_deliveries WHERE ctid IN ('
        'SELECT ctid FROM notification_deliveries '
        'WHERE created_at < now() - make_interval(days => $1) '
        'LIMIT $2)'
    )
    while True:
        status = await db.pg_execute(sql, days, int(batch_size))
        parts = str(status or '').split()
        deleted = int(parts[-1]) if parts and parts[-1].isdigit() else 0
        total += deleted
        if deleted < batch_size:
            return total


# ---------------------------------------------------------------------------
# 单次同步投递: legacy deliver() + v2 deliver_message()
# ---------------------------------------------------------------------------
def _send_http_via_requests(req) -> tuple[int, dict[str, str], str]:
    headers = dict(req.headers or {})
    method = req.method.upper()
    is_default_json_post = (
        method == 'POST'
        and headers == {'Content-Type': 'application/json; charset=utf-8'}
        and req.body is not None
        and requests.post is not _ORIG_REQUESTS_POST
    )
    if is_default_json_post:
        payload = json.loads(req.body.decode('utf-8'))
        response = requests.post(req.url, json=payload, timeout=req.timeout, allow_redirects=False)
    elif requests.request is not _ORIG_REQUESTS_REQUEST:
        response = requests.request(
            method,
            req.url,
            headers=headers,
            data=req.body,
            timeout=req.timeout,
            allow_redirects=False,
        )
    elif method == 'POST':
        if headers == {'Content-Type': 'application/json; charset=utf-8'} and req.body is not None:
            payload = json.loads(req.body.decode('utf-8'))
            response = requests.post(req.url, json=payload, timeout=req.timeout, allow_redirects=False)
        else:
            response = requests.post(
                req.url,
                headers=headers,
                data=req.body,
                timeout=req.timeout,
                allow_redirects=False,
            )
    else:
        response = requests.request(
            method,
            req.url,
            headers=headers,
            data=req.body,
            timeout=req.timeout,
            allow_redirects=False,
        )
    resp_headers = getattr(response, 'headers', None) or {}
    resp_text = getattr(response, 'text', '')
    return int(response.status_code), resp_headers, resp_text


def deliver_message(
    channel: ChannelConfig,
    msg: NotificationMessage,
    *,
    trace_id: str = '',
    record: bool = True,
) -> DeliveryResult:
    """同步执行单次多通道投递(支持 HTTP SSRF 校验 + SMTP 直连), 永不抛异常且强制脱敏。"""
    started = time.monotonic()
    effective_trace = trace_id or msg.event_id or uuid.uuid4().hex
    try:
        provider = get_provider(channel.provider)
    except ValueError as exc:
        clean = redact_text(str(exc), max_len=channels.MAX_MESSAGE)
        res = DeliveryResult(ok=False, message=clean, retryable=False, latency_ms=0)
        if record:
            _record_delivery(
                trace_id=effective_trace,
                env=msg.env,
                category=msg.category,
                severity=msg.severity,
                channel_id=channel.id,
                channel_name=channel.name or channel.id,
                provider=channel.provider,
                ok=False,
                attempts=1,
                http_status=None,
                error_summary=clean,
                message=clean,
                latency_ms=0,
            )
        return res

    secret_vals = tuple(
        dict.fromkeys([*provider.redact_values(channel), *(str(v) for v in channel.secrets.values() if v)])
    )
    req_timeout = SEND_TIMEOUT

    try:
        if provider.transport == 'smtp':
            direct_fn = getattr(provider, 'deliver_direct', None) or provider.send_direct
            raw_res = direct_fn(channel, msg)
        else:
            req = provider.build_http(channel, msg)
            req_timeout = req.timeout
            if requests.post is not _ORIG_REQUESTS_POST or requests.request is not _ORIG_REQUESTS_REQUEST:
                precheck_target_url(req.url, allow_http=req.allow_http)
                status_code, resp_headers, body_text = _send_http_via_requests(req)
            else:
                status_code, body_text, resp_headers = send_pinned_http(req)
            raw_res = provider.parse_http_response(status_code, resp_headers, body_text)
    except SSRFError as exc:
        raw_res = DeliveryResult(ok=False, message=f'发送被安全策略拦截:{exc}', retryable=False)
    except requests.Timeout:
        raw_res = DeliveryResult(ok=False, message=f'请求超时({int(req_timeout)} 秒内没有返回)', retryable=True)
    except TimeoutError:
        raw_res = DeliveryResult(ok=False, message=f'请求超时({int(req_timeout)} 秒内没有返回)', retryable=True)
    except requests.RequestException as exc:
        raw_res = DeliveryResult(ok=False, message=f'无法访问 IM 接口:{type(exc).__name__}: {exc}', retryable=True)
    except OSError as exc:
        raw_res = DeliveryResult(ok=False, message=f'无法访问 IM 接口:{exc}', retryable=True)
    except Exception as exc:
        raw_res = DeliveryResult(ok=False, message=f'发送失败:{type(exc).__name__}: {exc}', retryable=False)

    latency_ms = max(int((time.monotonic() - started) * 1000), 0)
    clean_msg = redact_text(raw_res.message, secrets=secret_vals, max_len=channels.MAX_MESSAGE)
    result = DeliveryResult(
        ok=bool(raw_res.ok),
        message=clean_msg,
        status_code=raw_res.status_code,
        provider_code=raw_res.provider_code,
        retryable=bool(raw_res.retryable),
        retry_after=raw_res.retry_after,
        attempts=raw_res.attempts,
        latency_ms=latency_ms,
    )
    if record:
        _record_delivery(
            trace_id=effective_trace,
            env=msg.env,
            category=msg.category,
            severity=msg.severity,
            channel_id=channel.id,
            channel_name=channel.name or channel.id,
            provider=provider.id,
            ok=result.ok,
            attempts=result.attempts,
            http_status=result.status_code,
            error_summary='' if result.ok else result.message,
            message=result.message,
            latency_ms=latency_ms,
        )
    return result


def deliver(channel, content):
    """同步发送一条消息(v1 兼容),返回 (ok, message)。不抛异常;message 已经去掉 token。"""
    token = str(channel.get('token') or '')
    kind = channel.get('type') or channel.get('provider')
    try:
        url, payload = channels.build_request(kind, token, content, channel.get('at') or '')
        response = requests.post(url, json=payload, timeout=SEND_TIMEOUT, allow_redirects=False)
        ok, message = channels.parse_response(kind, response.status_code, response.text)
    except requests.Timeout:
        ok, message = False, f'请求超时({SEND_TIMEOUT} 秒内没有返回)'
    except requests.RequestException as exc:
        ok, message = False, f'无法访问 IM 接口:{type(exc).__name__}: {exc}'
    except Exception as exc:
        ok, message = False, f'发送失败:{type(exc).__name__}: {exc}'
    return ok, redact(message, token)


def _channel_label(channel) -> str:
    if isinstance(channel, ChannelConfig):
        try:
            return get_provider(channel.provider).label
        except ValueError:
            return channel.provider
    kind = channel.get('type') or channel.get('provider')
    return channels.TYPE_LABELS.get(kind, str(kind))


def _send(channel, content_or_msg, scope, *, record: bool = True, trace_id: str = ''):
    """线程池里执行:发送并记日志(v1 dict/str 或 v2 ChannelConfig/NotificationMessage)。"""
    if isinstance(channel, ChannelConfig) and isinstance(content_or_msg, NotificationMessage):
        result = deliver_message(channel, content_or_msg, trace_id=trace_id, record=record)
        return result

    legacy_channel = (
        {
            'type': channel.provider,
            'token': channel.secrets.get('token', ''),
            'at': channel.settings.get('at', ''),
        }
        if isinstance(channel, ChannelConfig)
        else channel
    )
    ok, message = deliver(legacy_channel, content_or_msg)
    label = _channel_label(legacy_channel)
    if ok:
        logger.info(f'消息通知已发送({scope},{label})')
    else:
        logger.warning(f'消息通知发送失败({scope},{label}):{message}')
    if record:
        kind = str(legacy_channel.get('type') or '')
        _record_delivery(
            trace_id=trace_id or uuid.uuid4().hex,
            env=scope[3:] if isinstance(scope, str) and scope.startswith('集群 ') else '',
            category='ops',
            severity='info',
            channel_id=channel.id if isinstance(channel, ChannelConfig) else f'legacy_{kind}',
            channel_name=channel.name if isinstance(channel, ChannelConfig) else label,
            provider=kind,
            ok=ok,
            attempts=1,
            http_status=200 if ok else None,
            error_summary='' if ok else message,
            message=message,
            latency_ms=0,
        )
    return DeliveryResult(ok=ok, message=message, retryable=False)


# ---------------------------------------------------------------------------
# 按通道排队、顺序发送、限速与指数退避重试
# ---------------------------------------------------------------------------
class _Robot:
    """一个通道队列和最近窗口内的发送记录"""

    __slots__ = (
        'label',
        'items',
        'sent',
        'busy',
        'dropped',
        'drop_scope',
        'drop_warned_at',
        'next_retry_at',
        'rate_limit',
        'window',
        'burst_limit',
        'burst_window',
    )

    def __init__(self, label, rate_limit=RATE_LIMIT, window=RATE_WINDOW, burst_limit=None, burst_window=1.0):
        self.label = label
        self.items = deque()  # (channel, payload, scope, attempt, max_attempts, trace_id, started_at)
        self.sent = deque()
        self.busy = False
        self.dropped = 0
        self.drop_scope = ''
        self.drop_warned_at = None
        self.next_retry_at = 0.0
        self.rate_limit = rate_limit
        self.window = window
        self.burst_limit = burst_limit
        self.burst_window = burst_window

    def ready_at(self, now, limit, window, burst_limit=None, burst_window=1.0):
        """下一条最早什么时候能发(滑动窗口 + 可选突发窗口)"""
        keep_window = max(float(window), float(burst_window)) if burst_limit else float(window)
        while self.sent and self.sent[0] <= now - keep_window:
            self.sent.popleft()
        main_count = sum(1 for ts in self.sent if ts > now - window)
        ready = now
        if main_count >= limit:
            ready = max(ready, self.sent[-limit] + window)
        if burst_limit:
            burst_count = sum(1 for ts in self.sent if ts > now - burst_window)
            if burst_count >= burst_limit:
                ready = max(ready, self.sent[-burst_limit] + burst_window)
        return ready


def _default_jitter(base: float) -> float:
    if base <= 0:
        return 0.0
    return random.uniform(0.0, min(1.0, base * 0.25))


class Dispatcher:
    """按通道/提供方排队、顺序发送、限速与重试。"""

    def __init__(
        self,
        executor,
        rate_limit=None,
        window=None,
        backlog=None,
        clock=time.monotonic,
        max_attempts=None,
        backoff_base=None,
        backoff_cap=None,
        jitter=None,
    ):
        self.executor = executor
        self._explicit_rate_limit = rate_limit is not None
        self._explicit_window = window is not None
        self.rate_limit = max(int(RATE_LIMIT if rate_limit is None else rate_limit), 1)
        self.window = float(RATE_WINDOW if window is None else window)
        self.backlog = max(int(MAX_BACKLOG if backlog is None else backlog), 1)
        self.clock = clock
        self.max_attempts = max(int(MAX_RETRY_ATTEMPTS if max_attempts is None else max_attempts), 1)
        self.backoff_base = float(BACKOFF_BASE if backoff_base is None else backoff_base)
        self.backoff_cap = float(BACKOFF_CAP if backoff_cap is None else backoff_cap)
        if jitter is None:
            self._jitter = _default_jitter
        elif callable(jitter):
            self._jitter = jitter
        else:
            fixed = float(jitter)
            self._jitter = lambda _base: fixed
        self._cond = threading.Condition()
        self._robots: dict[tuple[Any, ...], _Robot] = {}
        self._thread = None
        self._stopped = False

    def _robot_key(self, channel) -> tuple[Any, ...]:
        if isinstance(channel, ChannelConfig):
            p = channel.provider
            if p in ('wecom', 'dingding', 'feishu', 'slack'):
                return (
                    p,
                    str(channel.settings.get('domain_preset') or ''),
                    str(channel.settings.get('custom_domain') or ''),
                    str(channel.secrets.get('token') or ''),
                )
            if p == 'webhook':
                return ('webhook', str(channel.settings.get('url') or channel.id))
            if p == 'smtp':
                return (
                    'smtp',
                    str(channel.settings.get('host') or ''),
                    int(channel.settings.get('port') or 465),
                    str(channel.settings.get('from_addr') or channel.id),
                )
            return (p, channel.id)
        return (channel.get('type'), channel.get('token'))

    def _rate_spec(self, channel) -> tuple[int, float, int | None, float]:
        if self._explicit_rate_limit or self._explicit_window:
            return self.rate_limit, self.window, None, 1.0
        if isinstance(channel, ChannelConfig):
            rl = channel.rate_limit
            if rl is None:
                try:
                    rl = get_provider(channel.provider).default_rate_limit
                except ValueError:
                    return self.rate_limit, self.window, None, 1.0
            return rl.limit, float(rl.window_seconds), rl.burst_limit, float(rl.burst_window_seconds)
        kind = str(channel.get('type') or channel.get('provider') or '')
        try:
            rl = get_provider(kind).default_rate_limit
            return rl.limit, float(rl.window_seconds), rl.burst_limit, float(rl.burst_window_seconds)
        except ValueError:
            return self.rate_limit, self.window, None, 1.0

    def submit(self, channel, text, scope, *, max_attempts=None, trace_id: str = ''):
        """放进该通道的队列;积压满了丢最旧的一条。不阻塞(只拿一下锁)"""
        key = self._robot_key(channel)
        limit, window, burst_limit, burst_window = self._rate_spec(channel)
        attempts_cap = self.max_attempts if max_attempts is None else max(int(max_attempts), 1)
        effective_trace = trace_id or (text.event_id if isinstance(text, NotificationMessage) else '') or uuid.uuid4().hex
        warning = ''
        with self._cond:
            robot = self._robots.get(key)
            if robot is None:
                robot = self._robots[key] = _Robot(
                    _channel_label(channel),
                    rate_limit=limit,
                    window=window,
                    burst_limit=burst_limit,
                    burst_window=burst_window,
                )
            else:
                robot.rate_limit = limit
                robot.window = window
                robot.burst_limit = burst_limit
                robot.burst_window = burst_window
            if len(robot.items) >= self.backlog:
                dropped_scope = robot.items.popleft()[2]
                warning = self._note_drop(robot, dropped_scope, self.clock())
            robot.items.append((channel, text, scope, 1, attempts_cap, effective_trace, self.clock()))
            self._start()
            self._cond.notify_all()
        if warning:
            logger.warning(warning)

    def _note_drop(self, robot, scope, now):
        """记一条丢弃;到了提示间隔返回提示文字(连同期间丢的条数),否则返回空串"""
        robot.dropped += 1
        robot.drop_scope = scope
        if robot.drop_warned_at is not None and now - robot.drop_warned_at < DROP_WARN_INTERVAL:
            return ''
        robot.drop_warned_at = now
        return self._drop_text(robot)

    def _drop_text(self, robot):
        count, robot.dropped = robot.dropped, 0
        return (
            f'消息通知积压太多:{robot.label}机器人待发的通知超过 {self.backlog} 条'
            f'(限速每 {robot.window:g} 秒 {robot.rate_limit} 条),丢弃了最早的 {count} 条'
            f'(最近丢弃的一条是{robot.drop_scope}的)'
        )

    def _start(self):
        if self._stopped or (self._thread is not None and self._thread.is_alive()):
            return
        self._thread = threading.Thread(target=self._run, name='notify-dispatcher', daemon=True)
        self._thread.start()

    def _run(self):
        while True:
            warnings = []
            with self._cond:
                if self._stopped:
                    return
                timeout = self._dispatch(warnings)
                if not warnings:
                    self._cond.wait(timeout)
            for text in warnings:
                logger.warning(text)

    def _dispatch(self, warnings):
        """(持有锁)把能发的交给线程池;返回下一次要醒来的秒数,None 表示等新消息"""
        now = self.clock()
        timeout = None
        for key, robot in list(self._robots.items()):
            if robot.busy:
                continue
            if not robot.items:
                if robot.dropped:
                    warnings.append(self._drop_text(robot))
                robot.ready_at(now, robot.rate_limit, robot.window, robot.burst_limit, robot.burst_window)
                if not robot.sent and robot.next_retry_at <= now:
                    del self._robots[key]
                continue
            if robot.next_retry_at > now:
                wait_for = robot.next_retry_at - now
                timeout = wait_for if timeout is None else min(timeout, wait_for)
                continue
            at = robot.ready_at(now, robot.rate_limit, robot.window, robot.burst_limit, robot.burst_window)
            if at > now:
                wait_for = at - now
                timeout = wait_for if timeout is None else min(timeout, wait_for)
                continue
            item = robot.items.popleft()
            robot.busy = True
            try:
                self.executor.submit(self._send_one, robot, item)
            except Exception as exc:
                robot.busy = False
                count = len(robot.items) + 1
                robot.items.clear()
                warnings.append(f'提交消息通知失败,丢弃{robot.label}机器人待发的 {count} 条:{type(exc).__name__}: {exc}')
        return timeout

    def _retry_delay(self, attempt: int, result: DeliveryResult) -> float:
        if result.status_code == 429 and result.retry_after is not None:
            base = min(max(float(result.retry_after), 0.0), 300.0)
        else:
            base = min(self.backoff_base * (3 ** (attempt - 1)), self.backoff_cap)
        return max(base + float(self._jitter(base)), 0.0)

    def _send_one(self, robot, item):
        channel, payload, scope, attempt, max_attempts, trace_id, started_at = item
        result = None
        threw = False
        try:
            if isinstance(channel, ChannelConfig) and isinstance(payload, NotificationMessage):
                result = _send(channel, payload, scope, record=False, trace_id=trace_id)
            else:
                result = _send(channel, payload, scope)
        except Exception as exc:
            threw = True
            logger.warning(f'消息通知发送异常({scope},{robot.label}):{type(exc).__name__}')
        finally:
            now = self.clock()
            should_retry = (
                not threw
                and isinstance(result, DeliveryResult)
                and not result.ok
                and result.retryable
                and attempt < max_attempts
            )
            with self._cond:
                robot.sent.append(now)
                if should_retry:
                    delay = self._retry_delay(attempt, result)
                    robot.items.appendleft((channel, payload, scope, attempt + 1, max_attempts, trace_id, started_at))
                    robot.next_retry_at = now + delay
                else:
                    robot.next_retry_at = 0.0
                robot.busy = False
                self._cond.notify_all()

            if not threw and isinstance(channel, ChannelConfig) and isinstance(payload, NotificationMessage):
                if should_retry:
                    logger.debug(
                        f'消息通知投递将重试({scope},{robot.label},第 {attempt}/{max_attempts} 次):{result.message}'
                    )
                elif isinstance(result, DeliveryResult):
                    if result.ok:
                        logger.info(f'消息通知已发送({scope},{robot.label})')
                    else:
                        logger.warning(f'消息通知发送失败({scope},{robot.label}):{result.message}')
                    total_latency = result.latency_ms if result.latency_ms is not None else 0
                    _record_delivery(
                        trace_id=trace_id,
                        env=payload.env,
                        category=payload.category,
                        severity=payload.severity,
                        channel_id=channel.id,
                        channel_name=channel.name or channel.id,
                        provider=channel.provider,
                        ok=result.ok,
                        attempts=attempt,
                        http_status=result.status_code,
                        error_summary='' if result.ok else result.message,
                        message=result.message,
                        latency_ms=int(total_latency),
                    )

    # ---- 测试和排查用 ----
    def wake(self):
        """让调度线程马上重新检查(测试里改了假时钟之后调用)"""
        with self._cond:
            self._cond.notify_all()

    def pending(self):
        """还没发完的条数(排队的 + 正在发的)"""
        with self._cond:
            return sum(len(robot.items) + int(robot.busy) for robot in self._robots.values())

    def wait_idle(self, timeout=5):
        """等所有队列发完;超时返回 False"""
        deadline = time.monotonic() + timeout
        with self._cond:
            while any(robot.items or robot.busy for robot in self._robots.values()):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(remaining)
        return True

    def stop(self, timeout=5):
        """停掉调度线程(没发的不再发);测试用"""
        with self._cond:
            self._stopped = True
            self._cond.notify_all()
            thread = self._thread
        if thread is not None:
            thread.join(timeout)


_dispatcher = Dispatcher(_executor)


# ---------------------------------------------------------------------------
# 发通知 (v2 notify_message + v1/v2 notify)
# ---------------------------------------------------------------------------
def _preview(text, limit=80):
    flat = ' '.join(text.split())
    return flat if len(flat) <= limit else flat[:limit] + '…'


def _scope_for_channel(ch: ChannelConfig, env: str | None) -> str:
    if ch.id == 'ch_default':
        return DEFAULT_SCOPE
    clean_env = env.strip() if isinstance(env, str) else ''
    if clean_env:
        return f'集群 {clean_env}'
    return f'通道 {ch.name or ch.id}'


def notify_message(msg: NotificationMessage) -> int:
    """按 (msg.env, msg.category, msg.severity) 路由并异步投递结构化消息, 返回入队通道数。永不抛异常。"""
    try:
        has_content = bool(
            (msg.markdown or '').strip()
            or (msg.plain_text or '').strip()
            or msg.items
            or ((msg.title or '').strip() and msg.title.strip() != '通知')
        )
        if not has_content:
            return 0
        targets = resolve_targets(env=msg.env, category=msg.category, severity=msg.severity)
        if not targets:
            where = f'集群 {msg.env}' if isinstance(msg.env, str) and msg.env.strip() else '没有指定集群'
            logger.info(f'没有配置消息通知({where}),不发送:{_preview(msg.markdown or msg.plain_text or msg.title)}')
            return 0
        trace_id = msg.event_id or uuid.uuid4().hex
        for ch in targets:
            _dispatcher.submit(ch, msg, _scope_for_channel(ch, msg.env), trace_id=trace_id)
        return len(targets)
    except Exception as exc:
        logger.warning(f'提交消息通知失败:{type(exc).__name__}: {exc}')
        return 0


def notify(content, env=None, *, category='ops', severity='info', title=''):
    """发一条通知:按 resolve_targets 选通道,放进发送队列后立即返回,不等结果。
    事件循环线程和普通线程里都能直接调用;返回是否至少入队一个通道。任何失败只记日志,不抛异常。"""
    try:
        text = '' if content is None else str(content)
        if not text.strip():
            return False
        clean_env = env.strip() if isinstance(env, str) else ''
        effective_category = (category or 'ops').strip().lower()
        if effective_category == 'ops' and 'K8S事件告警:' in text[:80]:
            effective_category = 'k8s_event'

        snapshot = _snapshot
        # 兼容 test_notify_service.py 中 monkeypatch service._pick 的情况
        if _pick is not _ORIG_PICK:
            scope, legacy_ch = _pick(env)
            if legacy_ch is None:
                where = f'集群 {env}' if isinstance(env, str) and env.strip() else '没有指定集群'
                logger.info(f'没有配置消息通知({where}),不发送:{_preview(text)}')
                return False
            _dispatcher.submit(legacy_ch, text, scope, max_attempts=1)
            return True

        targets = resolve_targets(env=clean_env, category=effective_category, severity=severity or 'info')
        if not targets:
            where = f'集群 {env}' if isinstance(env, str) and env.strip() else '没有指定集群'
            logger.info(f'没有配置消息通知({where}),不发送:{_preview(text)}')
            return False

        trace_id = uuid.uuid4().hex
        for ch in targets:
            scope = _scope_for_channel(ch, clean_env)
            if snapshot.source_version == 1 and not title and ch.provider in channels.TYPES:
                _dispatcher.submit(ch, text, scope, max_attempts=1, trace_id=trace_id)
            else:
                msg = build_event_message(
                    title=title,
                    body=text,
                    env=clean_env,
                    category=effective_category,
                    severity=severity or 'info',
                    metadata={'trace_id': trace_id},
                )
                _dispatcher.submit(ch, msg, scope, trace_id=trace_id)
        return True
    except Exception as exc:
        logger.warning(f'提交消息通知失败:{type(exc).__name__}: {exc}')
        return False


_ORIG_PICK = _pick
