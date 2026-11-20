"""Jaeger api_v3 HTTP 客户端（约定 §1.1）。

- 每个请求带超时、不跟随重定向、响应读到上限（64MB）就停；请求头（配置里的，值已解密）照带
- 错误：连不上 / 超时 / 5xx → jaeger_unavailable（502），4xx → jaeger_rejected（400），都带 Jaeger 原话
  （body {"error": {"httpCode", "message"}}）；按 ID 取链路的 404 由调用方处理
- 服务列表缓存 60 秒，capabilities 缓存 10 分钟（按连接配置区分；测试连接不读缓存）
- 1MB 以上的响应在线程里解析 JSON
"""

import asyncio
import json
from urllib.parse import quote

import aiohttp
from loguru import logger

from func_manager import ext_api
from func_manager.ext_api import ExtApiError

from . import config as tracing_config
from .common import ResponseTooLarge, TTLCache, TracingError, rfc3339

MAX_RESPONSE = 64 * 1024 * 1024
LARGE_JSON = 1024 * 1024
SERVICES_TTL = 60
CAPABILITIES_TTL = 600
UNBOUNDED_TRACE_TIMEOUT = 60  # 不带时间范围按 ID 取链路：单独 60 秒超时

_services_cache = TTLCache(max_items=32, ttl=SERVICES_TTL)
_capabilities_cache = TTLCache(max_items=32, ttl=CAPABILITIES_TTL)
_NO_CAPABILITIES = object()  # 缓存「capabilities 接口 404」（Jaeger 2.23 之前）


def reset_caches():
    _services_cache.clear()
    _capabilities_cache.clear()


def jaeger_message(raw):
    """Jaeger 错误响应里的原话：{"error": {"message"}}，其次通用的 message / error，再其次开头一段纯文本"""
    text = raw.decode('utf-8', errors='replace') if isinstance(raw, (bytes, bytearray)) else str(raw or '')
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        data = None
    if isinstance(data, dict) and isinstance(data.get('error'), dict):
        message = data['error'].get('message')
        if isinstance(message, str) and message.strip():
            return message.strip()[:500]
    return ext_api.error_reason(text)


async def decode_json(raw):
    """响应体 → JSON（大的放线程里解析）"""
    def parse():
        return json.loads(raw.decode('utf-8', errors='replace')) if raw else {}

    try:
        return await asyncio.to_thread(parse) if len(raw) > LARGE_JSON else parse()
    except (ValueError, RecursionError) as exc:
        raise TracingError(502, f'Jaeger 返回的不是 JSON（{str(exc)[:80]}）', 'jaeger_unavailable') from None


class JaegerClient:
    def __init__(self, cfg, session):
        self.cfg, self.session = cfg, session
        self.base = cfg['url']
        self.key = tracing_config.jaeger_fingerprint(cfg)
        try:
            self.headers = ext_api.request_headers(cfg)
        except ExtApiError as exc:
            raise TracingError(exc.status, exc.message, exc.code) from None
        self.headers.setdefault('Accept', 'application/json')

    async def request(self, path, params=None, *, timeout=None, limit=MAX_RESPONSE, allow_404=False):
        """GET 一个 api_v3 接口，返回 (状态码, 响应体)；失败抛 TracingError（allow_404 时 404 原样返回）"""
        timeout = timeout or self.cfg['timeout']
        url = self.base + path
        try:
            async with self.session.get(url, params=params, headers=self.headers, allow_redirects=False,
                                        ssl=bool(self.cfg['tls_verify']),
                                        timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                status = resp.status
                raw = await ext_api.read_limited(resp, limit)
                location = resp.headers.get('Location', '')
        except ExtApiError as exc:  # read_limited：响应超过上限
            raise ResponseTooLarge(502, f'Jaeger {exc.message}', 'jaeger_unavailable') from None
        except asyncio.TimeoutError:
            raise TracingError(502, f'Jaeger 在 {timeout} 秒内没有返回（{path}）', 'jaeger_unavailable') from None
        except aiohttp.ClientConnectorCertificateError as exc:
            raise TracingError(502, f'Jaeger 的 HTTPS 证书校验失败（{exc.certificate_error}），自签证书可以在配置里关闭'
                                    '「校验 HTTPS 证书」', 'jaeger_unavailable') from None
        except aiohttp.ClientError as exc:
            raise TracingError(502, f'无法访问 Jaeger：{str(exc) or type(exc).__name__}', 'jaeger_unavailable') from None
        if status == 404 and allow_404:
            return status, raw
        if 300 <= status < 400:
            raise TracingError(502, f'Jaeger 返回了重定向（HTTP {status}{" → " + location if location else ""}），'
                                    '不会跟随：请检查查询地址（含 base path）', 'jaeger_unavailable')
        if status >= 500:
            message = jaeger_message(raw)
            logger.warning(f'链路追踪：Jaeger {path} 返回 HTTP {status}: {message[:200]}')
            raise TracingError(502, f'Jaeger 出错（HTTP {status}）{"：" + message if message else ""}',
                               'jaeger_unavailable')
        if status >= 400:
            message = jaeger_message(raw)
            raise TracingError(400, f'Jaeger 拒绝了请求（HTTP {status}）{"：" + message if message else ""}',
                               'jaeger_rejected')
        return status, raw

    async def get_json(self, path, params=None, **kwargs):
        _, raw = await self.request(path, params, **kwargs)
        return await decode_json(raw)

    # ---- 接口 ----
    async def services(self, use_cache=True):
        """所有服务（全局，含其他集群的），缓存 60 秒"""
        if use_cache:
            cached = _services_cache.get(self.key)
            if cached is not None:
                return cached
        data = await self.get_json('/api/v3/services')
        services = sorted({str(item) for item in (data.get('services') or []) if item} if isinstance(data, dict)
                          else set())
        _services_cache.put(self.key, services)
        return services

    async def operations(self, service, span_kind=''):
        params = {'service': service}
        if span_kind:
            params['spanKind'] = span_kind
        data = await self.get_json('/api/v3/operations', params)
        items = data.get('operations') if isinstance(data, dict) else None
        return [{'name': str(item.get('name') or ''), 'kind': str(item.get('spanKind') or '')}
                for item in items or [] if isinstance(item, dict) and item.get('name')]

    async def capabilities(self, use_cache=True):
        """/api/v3/capabilities；Jaeger 2.23 之前没有这个接口（404）返回 None"""
        if use_cache:
            cached = _capabilities_cache.get(self.key)
            if cached is not None:
                return None if cached is _NO_CAPABILITIES else cached
        status, raw = await self.request('/api/v3/capabilities', allow_404=True)
        caps = None if status == 404 else await decode_json(raw)
        caps = caps if isinstance(caps, dict) or caps is None else {}
        _capabilities_cache.put(self.key, _NO_CAPABILITIES if caps is None else caps)
        return caps

    @staticmethod
    def search_params(start, end, depth, filter_call=None, service=''):
        params = {'query.startTimeMin': rfc3339(start), 'query.startTimeMax': rfc3339(end),
                  'query.searchDepth': str(depth)}
        if filter_call is not None:
            params['query.filter'] = json.dumps(filter_call, ensure_ascii=False, separators=(',', ':'))
        elif service:
            params['query.serviceName'] = service
        return params

    async def summaries(self, params, timeout=None):
        data = await self.get_json('/api/v3/trace-summaries', params, timeout=timeout)
        items = data.get('summaries') if isinstance(data, dict) else None
        return [item for item in items or [] if isinstance(item, dict) and item.get('traceId')]

    async def traces(self, params, timeout=None, limit=MAX_RESPONSE):
        """搜索并返回完整链路（原始响应体）；没有结果时 Jaeger 可能返回 404"""
        status, raw = await self.request('/api/v3/traces', params, timeout=timeout, limit=limit, allow_404=True)
        return b'' if status == 404 else raw

    async def trace(self, trace_id, start=None, end=None, timeout=None):
        """按 ID 取链路（原始响应体）；找不到返回 None"""
        params = {}
        if start is not None:
            params = {'startTime': rfc3339(start), 'endTime': rfc3339(end)}
        status, raw = await self.request(f'/api/v3/traces/{quote(trace_id, safe="")}', params, timeout=timeout,
                                         allow_404=True)
        return None if status == 404 else raw


def capabilities_problem(caps):
    """按约定 §1.4：capabilities 404，或没有 filter / 缺 and、or、eq → 不能搜索，返回中文原因；能搜索返回空串"""
    if caps is None:
        return 'Jaeger 没有 /api/v3/capabilities 接口（版本低于 2.23）'
    flt = (caps.get('search') or {}).get('filter') if isinstance(caps.get('search'), dict) else None
    if not isinstance(flt, dict):
        return 'Jaeger 的存储后端不支持结构化过滤（capabilities 里没有 filter）'
    missing = [op for op in ('and', 'or', 'eq') if op not in (flt.get('operators') or [])]
    if missing:
        return f'Jaeger 的存储后端不支持运算符 {"、".join(missing)}'
    return ''
