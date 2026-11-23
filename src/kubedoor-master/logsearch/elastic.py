"""Elasticsearch 客户端（约定 §5.3、§6.3）。

- 认证：basic（Authorization: Basic）、api_key（Authorization: ApiKey <值>）、自定义请求头；不跟随重定向
- 查询路径 /<target>/_search（各项 URL 编码后用逗号连接，保留 *），带 ignore_unavailable、allow_no_indices、
  expand_wildcards=open；超时写进请求体（"<n>s"），客户端超时 = 超时 + 3 秒
- 响应上限 16MB；错误原话取 error.root_cause[0].reason 或 error.reason
- GET / 的结果（版本、发行版）缓存 10 分钟：判断能不能用 ES|QL（8.14+ 且不是 OpenSearch）
"""

import asyncio
import base64
import json
import re
import time
from urllib.parse import quote

import aiohttp
from yarl import URL

from func_manager import ext_api
from func_manager.ext_api import ExtApiError

from . import config as log_config
from .common import LogError, ResponseTooLarge, TTLCache, limiter

MAX_RESPONSE = 16 * 1024 * 1024
LARGE_JSON = 1024 * 1024
NETWORK_SLACK = 3
ERROR_CHARS = 500
TARGET_PARAMS = {'ignore_unavailable': 'true', 'allow_no_indices': 'true', 'expand_wildcards': 'open'}
ESQL_MIN_VERSION = (8, 14)
_INFO = TTLCache(max_items=50, ttl=600)
_VERSION = re.compile(r'(\d+)\.(\d+)(?:\.(\d+))?')


def reset_caches():
    _INFO.clear()


def target_path(target):
    """逗号分隔的目标 → URL 路径段（各项编码，保留 * 和开头的 -）"""
    return ','.join(quote(item.strip(), safe='*-') for item in str(target).split(',') if item.strip())


def version_tuple(text):
    match = _VERSION.match(str(text or ''))
    if not match:
        return (0, 0, 0)
    return tuple(int(part or 0) for part in match.groups())


def describe_info(info):
    """GET / → {version, distribution, cluster_name, esql}"""
    version = info.get('version') if isinstance(info, dict) else None
    version = version if isinstance(version, dict) else {}
    number = str(version.get('number') or '')
    distribution = str(version.get('distribution') or 'elasticsearch')
    opensearch = distribution.lower() == 'opensearch'
    return {
        'version': number,
        'distribution': distribution,
        'cluster_name': str(info.get('cluster_name') or '') if isinstance(info, dict) else '',
        'esql': (not opensearch) and version_tuple(number)[:2] >= ESQL_MIN_VERSION,
    }


def reason_of(data, text=''):
    """出错响应里的原话"""
    if isinstance(data, dict):
        error = data.get('error')
        if isinstance(error, dict):
            causes = error.get('root_cause')
            if isinstance(causes, list) and causes and isinstance(causes[0], dict) and causes[0].get('reason'):
                return str(causes[0]['reason'])[:ERROR_CHARS], str(causes[0].get('type') or error.get('type') or '')
            if error.get('reason'):
                return str(error['reason'])[:ERROR_CHARS], str(error.get('type') or '')
            caused = error.get('caused_by')
            if isinstance(caused, dict) and caused.get('reason'):
                return str(caused['reason'])[:ERROR_CHARS], str(caused.get('type') or '')
        if isinstance(error, str):
            return error[:ERROR_CHARS], ''
        if isinstance(data.get('message'), str):
            return data['message'][:ERROR_CHARS], ''
    text = (text or '').strip()
    return ('' if text.startswith('<') else text[:ERROR_CHARS]), ''


def classify_error(status, data, text=''):
    reason, kind = reason_of(data, text)
    detail = f'：{reason}' if reason else ''
    whole = f'{kind} {reason}'.lower()
    if status == 401:
        return LogError(502, f'Elasticsearch 认证失败（HTTP 401），请系统管理员检查账号密码 / API Key{detail}',
                        'datasource_unavailable')
    if status == 403:
        return LogError(400, f'Elasticsearch 拒绝访问（HTTP 403，账号没有权限）{detail}', 'query_rejected')
    if status in (408, 504) or 'timeout' in kind.lower():
        return LogError(504, f'Elasticsearch 查询超时：加条件或缩短时间窗口{detail}', 'timeout')
    if 'too_many_buckets' in whole or 'too many buckets' in whole:
        return LogError(400, f'统计的桶太多：减少分组、加大 interval 或缩短时间窗口{detail}', 'too_expensive')
    if status == 429:
        return LogError(502, f'Elasticsearch 繁忙（HTTP 429），请稍后重试{detail}', 'datasource_unavailable')
    if status and 400 <= status < 500:
        return LogError(400, f'Elasticsearch 拒绝了查询（HTTP {status}）{detail}', 'query_rejected')
    return LogError(502, f'Elasticsearch 出错（HTTP {status}）{detail}', 'datasource_unavailable')


def _loads(raw):
    return json.loads(raw.decode('utf-8'))


class ElasticClient:
    def __init__(self, conn, session, timeout=None):
        self.conn, self.session = conn, session
        self.timeout = int(timeout or conn['timeout'])
        self.key = log_config.fingerprint(conn)
        try:
            headers = ext_api.request_headers(conn)
        except ExtApiError as exc:
            raise LogError(500, f'连接 {conn["id"]} 的{exc.message}', 'decrypt_failed') from None
        auth_type = conn.get('auth_type')
        try:
            from auth.crypto import decrypt_secret

            if auth_type == 'basic':
                password = decrypt_secret(conn.get('password') or '')
                token = base64.b64encode(f'{conn.get("username") or ""}:{password}'.encode('utf-8')).decode('ascii')
                headers['Authorization'] = f'Basic {token}'
            elif auth_type == 'api_key':
                headers['Authorization'] = f'ApiKey {decrypt_secret(conn.get("api_key") or "")}'
        except ValueError as exc:
            raise LogError(500, f'连接 {conn["id"]} 的密钥无法解密（{exc}），请系统管理员重新填写',
                           'decrypt_failed') from None
        headers['Content-Type'] = 'application/json'
        headers.setdefault('Accept', 'application/json')
        self.headers = headers

    def _url(self, path, params):
        query = '&'.join(f'{quote(str(key), safe="")}={quote(str(value), safe="")}'
                         for key, value in (params or {}).items())
        return URL(f'{self.conn["url"]}{path}' + (f'?{query}' if query else ''), encoded=True)

    async def request(self, method, path, params=None, body=None, timeout=None):
        """返回 (HTTP 状态码, 解析后的 JSON 或 None, 原文)；网络错误、超时抛 LogError"""
        timeout = (timeout or self.timeout) + NETWORK_SLACK
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode('utf-8')
        try:
            async with limiter(self.conn, self.key):
                async with self.session.request(method, self._url(path, params), data=data, headers=self.headers,
                                                allow_redirects=False, ssl=bool(self.conn['tls_verify']),
                                                timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                    status = resp.status
                    raw = await ext_api.read_limited(resp, MAX_RESPONSE)
        except ExtApiError:
            raise ResponseTooLarge() from None
        except asyncio.TimeoutError:
            raise LogError(504, f'Elasticsearch 在 {timeout} 秒内没有返回：加条件或缩短时间窗口', 'timeout') from None
        except aiohttp.ClientConnectorCertificateError as exc:
            raise LogError(502, f'Elasticsearch 的 HTTPS 证书校验失败（{exc.certificate_error}），自签证书可以在连接配置里'
                                '关闭「校验 HTTPS 证书」', 'datasource_unavailable') from None
        except aiohttp.ClientError as exc:
            raise LogError(502, f'无法访问 Elasticsearch：{str(exc) or type(exc).__name__}',
                           'datasource_unavailable') from None
        try:
            parsed = await asyncio.to_thread(_loads, raw) if len(raw) > LARGE_JSON else _loads(raw)
        except (ValueError, RecursionError, UnicodeDecodeError):
            parsed = None
        return status, parsed, raw[:2000].decode('utf-8', errors='replace')

    async def call(self, method, path, params=None, body=None, timeout=None):
        """成功返回 JSON；HTTP 出错按约定归类抛 LogError"""
        status, data, text = await self.request(method, path, params, body, timeout)
        if 300 <= status < 400:
            raise LogError(502, f'Elasticsearch 返回了重定向（HTTP {status}），不会跟随，请直接配置最终地址',
                           'datasource_unavailable')
        if status >= 400:
            raise classify_error(status, data, text)
        if data is None:
            raise LogError(502, 'Elasticsearch 返回的不是 JSON', 'datasource_unavailable')
        return data

    async def info(self, use_cache=True):
        """GET /（缓存 10 分钟）→ describe_info 的结果"""
        if use_cache:
            cached = _INFO.get(self.key)
            if cached is not None:
                return cached
        data = await self.call('GET', '/')
        result = describe_info(data)
        _INFO.put(self.key, result)
        return result

    async def search(self, target, body, timeout=None):
        """POST /<target>/_search；timed_out 按超时处理"""
        body = {**body, 'timeout': f'{timeout or self.timeout}s'}
        started = time.monotonic()
        data = await self.call('POST', f'/{target_path(target)}/_search', TARGET_PARAMS, body, timeout)
        if data.get('timed_out'):
            raise LogError(504, 'Elasticsearch 查询超时（timed_out）：加条件或缩短时间窗口', 'timeout')
        data['_elapsed_ms'] = int((time.monotonic() - started) * 1000)
        return data

    async def count(self, target, query, timeout=None):
        data = await self.call('POST', f'/{target_path(target)}/_count', TARGET_PARAMS, {'query': query}, timeout)
        return int(data.get('count') or 0)

    async def esql(self, query, flt, timeout=None):
        """POST /_query?format=json，filter 是 Query DSL（时间、集群条件）"""
        started = time.monotonic()
        data = await self.call('POST', '/_query', {'format': 'json'}, {'query': query, 'filter': flt}, timeout)
        data['_elapsed_ms'] = int((time.monotonic() - started) * 1000)
        return data

    async def field_caps(self, target, timeout=None):
        params = {'fields': '*', **TARGET_PARAMS}
        return await self.call('GET', f'/{target_path(target)}/_field_caps', params, None, timeout)

    async def runtime_fields(self, target, timeout=None):
        """各索引映射里 runtime 段的字段名（dynamic: runtime 时没登记的字段都在这里）；要 view_index_metadata 权限"""
        params = {'filter_path': '*.mappings.runtime', **TARGET_PARAMS}
        data = await self.call('GET', f'/{target_path(target)}/_mapping', params, None, timeout)
        names = set()
        for body in (data or {}).values() if isinstance(data, dict) else ():
            runtime = ((body or {}).get('mappings') or {}).get('runtime') if isinstance(body, dict) else None
            if isinstance(runtime, dict):
                names.update(name for name in runtime if isinstance(name, str))
        return names


def total_of(data):
    """hits.total → (数量, eq / gte)"""
    total = (data.get('hits') or {}).get('total')
    if isinstance(total, dict):
        return int(total.get('value') or 0), str(total.get('relation') or 'eq')
    if isinstance(total, int):
        return total, 'eq'
    return None, None
