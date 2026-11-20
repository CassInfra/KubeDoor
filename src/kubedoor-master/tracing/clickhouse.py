"""ClickHouse HTTP 客户端和 SQL 构造（约定 §1.6、§5）。

- SQL 里的值一律用 {name:Type} 占位，值放在 URL 参数 param_name 里，任何值都不拼进 SQL；库名、表名先按
  [A-Za-z_][A-Za-z0-9_]{0,63} 校验再用反引号括起来
- 参数值的写法（在 ClickHouse 26.7 上验证过）：
  String 按 TSV 转义：反斜杠 → \\\\，制表符 → \\t，换行 → \\n（回车 → \\r）；
  Array(String) 只写数组字面量 ['a','b']，元素在单引号里转义（反斜杠、单引号、换行、制表符），外层不再套 TSV 转义；
  Array(Int64) 写 [500,502]
- 时间一律用毫秒时间戳传参和取值（服务端时区是 Asia/Shanghai，避免时区换算）
- 设置：URL 带 max_execution_time、max_result_rows=10000、readonly=2；账号是 readonly=1 时 ClickHouse 报
  「Cannot modify ... in readonly mode」，去掉这些设置重试一次，并记住这个连接配置以后不再带
- 认证：请求头 X-ClickHouse-User / X-ClickHouse-Key；结果格式 JSONCompact；响应上限 16MB
"""

import asyncio
import json
import re
from urllib.parse import quote

import aiohttp
from loguru import logger
from yarl import URL

from auth.crypto import decrypt_secret
from func_manager import ext_api
from func_manager.ext_api import ExtApiError

from . import config as tracing_config
from .common import TracingError

MAX_RESPONSE = 16 * 1024 * 1024
MAX_RESULT_ROWS = 10000
NETWORK_SLACK = 3  # 客户端超时 = max_execution_time + 余量，让 ClickHouse 自己的超时错误先回来
SEARCH_COLUMNS = ('start_time', 'trace_id', 'span_id', 'k8s_cluster', 'namespace', 'service_name', 'kind',
                  'span_status', 'operation', 'url_path', 'http_method', 'http_status', 'duration_ms', 'peer', 'pod',
                  'url_query')
DIM_COLUMNS = ('bucket', 'k8s_cluster', 'namespace', 'service_name', 'kind', 'operation', 'url_path', 'spans')
_IDENT = re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,63}')
_READONLY = re.compile(r'Cannot modify .{0,200}? in readonly mode|\(READONLY\)|Code: 164\b', re.IGNORECASE)
_TIMEOUT = re.compile(r'TIMEOUT_EXCEEDED|Timeout exceeded|Code: 159\b', re.IGNORECASE)
_TSV_ESCAPES = {'\\': '\\\\', '\t': '\\t', '\n': '\\n', '\r': '\\r', '\0': '\\0'}
_QUOTED_ESCAPES = {**_TSV_ESCAPES, "'": "\\'"}

_no_settings = set()  # readonly=1 账号的连接配置：不再带设置


def reset_state():
    _no_settings.clear()


# ---------------------------------------------------------------------------
# SQL 构造
# ---------------------------------------------------------------------------
def ident(name):
    """库名 / 表名：校验后用反引号括起来"""
    if not isinstance(name, str) or not _IDENT.fullmatch(name):
        raise TracingError(400, f'ClickHouse 库名或表名不合法：{str(name)[:64]}', 'invalid_request')
    return f'`{name}`'


def table_ref(database, table):
    return f'{ident(database)}.{ident(table)}'


def escape_string(value):
    """String 参数：TSV 转义"""
    return ''.join(_TSV_ESCAPES.get(ch, ch) for ch in str(value))


def array_literal(values):
    """Array(String) 参数：['a','b']，元素在单引号里转义，外层不再转义"""
    return '[' + ','.join("'" + ''.join(_QUOTED_ESCAPES.get(ch, ch) for ch in str(item)) + "'"
                          for item in values) + ']'


def encode_param(type_, value):
    if type_ == 'String':
        return escape_string(value)
    if type_ == 'Array(String)':
        return array_literal(value)
    if type_.startswith('Array('):
        return '[' + ','.join(str(int(item)) for item in value) + ']'
    if type_.startswith(('Int', 'UInt')):
        return str(int(value))
    if type_.startswith('Float'):
        return repr(float(value))
    raise ValueError(f'不支持的参数类型 {type_}')


class Params:
    """绑定参数：add 返回 SQL 里的占位符 {name:Type}"""

    def __init__(self):
        self.values = {}

    def add(self, name, type_, value):
        base, number = name, 1
        while name in self.values:
            number += 1
            name = f'{base}{number}'
        self.values[name] = (type_, value)
        return f'{{{name}:{type_}}}'

    def url_params(self):
        return {f'param_{name}': encode_param(type_, value) for name, (type_, value) in self.values.items()}


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------
class ClickHouseClient:
    def __init__(self, cfg, session):
        ch = cfg['clickhouse']
        self.ch, self.session = ch, session
        self.key = tracing_config.clickhouse_fingerprint(cfg)
        self.headers = {'User-Agent': ext_api.USER_AGENT, 'Content-Type': 'text/plain; charset=utf-8'}
        if ch['username']:
            self.headers['X-ClickHouse-User'] = ch['username']
        if ch['password']:
            try:
                self.headers['X-ClickHouse-Key'] = decrypt_secret(ch['password'])
            except ValueError as exc:
                raise TracingError(500, f'ClickHouse 密码无法解密（{exc}），请系统管理员重新填写', 'decrypt_failed') from None

    def table(self, which='table'):
        return table_ref(self.ch['database'], self.ch[which])

    def _settings(self):
        if self.key in _no_settings:
            return {}
        return {'max_execution_time': str(self.ch['timeout']), 'max_result_rows': str(MAX_RESULT_ROWS),
                'readonly': '2'}

    def _url(self, query):
        """自己编码 URL 参数：空格是 %20、+ 是 %2B、% 是 %25，不依赖服务端怎么理解 +"""
        text = '&'.join(f'{quote(key, safe="")}={quote(value, safe="")}' for key, value in query.items())
        return URL(f'{self.ch["url"]}/?{text}', encoded=True)

    async def _post(self, sql, params, settings):
        query = {**settings, **params.url_params()}
        timeout = self.ch['timeout'] + NETWORK_SLACK
        try:
            async with self.session.post(self._url(query), data=sql.encode('utf-8'), headers=self.headers,
                                         allow_redirects=False, ssl=bool(self.ch['tls_verify']),
                                         timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                return resp.status, await ext_api.read_limited(resp, MAX_RESPONSE)
        except ExtApiError as exc:
            raise TracingError(502, f'ClickHouse {exc.message}', 'clickhouse_unavailable') from None
        except asyncio.TimeoutError:
            raise TracingError(502, f'ClickHouse 在 {timeout} 秒内没有返回', 'clickhouse_unavailable') from None
        except aiohttp.ClientConnectorCertificateError as exc:
            raise TracingError(502, f'ClickHouse 的 HTTPS 证书校验失败（{exc.certificate_error}），自签证书可以在配置里关闭'
                                    '「校验 HTTPS 证书」', 'clickhouse_unavailable') from None
        except aiohttp.ClientError as exc:
            raise TracingError(502, f'无法访问 ClickHouse：{str(exc) or type(exc).__name__}',
                               'clickhouse_unavailable') from None

    async def query(self, sql, params=None):
        """执行一条 SELECT，返回 JSONCompact 解析后的 {'meta', 'data', ...}"""
        params = params or Params()
        sql = sql.rstrip().rstrip(';') + '\nFORMAT JSONCompact'
        settings = self._settings()
        status, raw = await self._post(sql, params, settings)
        text = raw.decode('utf-8', errors='replace')
        if status != 200 and settings and _READONLY.search(text):
            # 账号是 readonly=1：不能改任何设置，去掉设置重试一次，以后这个连接配置都不再带
            _no_settings.add(self.key)
            logger.info('链路追踪：ClickHouse 账号是只读（readonly=1），以后查询不再带 max_execution_time 等设置')
            status, raw = await self._post(sql, params, {})
            text = raw.decode('utf-8', errors='replace')
        if status != 200:
            message = text.strip()[:300]
            if status >= 500 or status == 408 or _TIMEOUT.search(text):
                logger.warning(f'链路追踪：ClickHouse 返回 HTTP {status}: {message[:200]}')
                raise TracingError(502, f'ClickHouse 出错（HTTP {status}）：{message}', 'clickhouse_unavailable')
            raise TracingError(400, f'ClickHouse 拒绝了查询（HTTP {status}）：{message}', 'clickhouse_rejected')
        try:
            data = json.loads(text)
        except (ValueError, RecursionError):
            if 'DB::Exception' in text:
                raise TracingError(400, f'ClickHouse 查询出错：{text.strip()[:300]}', 'clickhouse_rejected') from None
            raise TracingError(502, 'ClickHouse 返回的不是 JSON', 'clickhouse_unavailable') from None
        if not isinstance(data, dict) or not isinstance(data.get('data'), list):
            raise TracingError(502, 'ClickHouse 返回的格式不对（没有 data）', 'clickhouse_unavailable')
        return data


def rows(result):
    """JSONCompact → [{列名: 值}]"""
    names = [item.get('name') for item in result.get('meta') or []]
    return [dict(zip(names, row)) for row in result['data']]


def to_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default


def to_float(value, default=0.0):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number == number else default  # NaN（没有数据时的分位数）
