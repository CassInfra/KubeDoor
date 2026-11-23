"""ClickHouse HTTP 客户端（约定 §5.2）。

- SQL 里的值一律用 {kd_xxx:Type} 占位，值放在 URL 参数 param_kd_xxx 里；库名、表名、字段名校验后用反引号括起来
  （String 参数按 TSV 转义，Array(String) 写成 ['a','b']，元素在单引号里转义，同链路追踪）
- 设置（URL 参数）：readonly=2、max_execution_time=<超时>、max_rows_to_read=<连接配置>（0 不带）、
  output_format_json_quote_64bit_integers=0；账号是 readonly=1 时 ClickHouse 报「Cannot modify ... in readonly mode」，
  去掉全部设置重试一次，并记住这个连接配置以后不再带
- 认证：请求头 X-ClickHouse-User / X-ClickHouse-Key；结果格式 JSONCompact；响应上限 16MB；不跟随重定向
- 统计：响应头 X-ClickHouse-Summary（JSON，read_rows、elapsed_ns 是字符串），没有就用 JSONCompact 的 statistics
- 错误按 ClickHouse 错误码归类：158 / 307 / 241 / 396 → too_expensive，159 → timeout，其余 → query_rejected（带原话前 500 字）
"""

import asyncio
import json
import re
import time
from urllib.parse import quote

import aiohttp
from loguru import logger
from yarl import URL

from auth.crypto import decrypt_secret
from func_manager import ext_api
from func_manager.ext_api import ExtApiError

from . import config as log_config
from .common import LogError, ResponseTooLarge, limiter

MAX_RESPONSE = 16 * 1024 * 1024
NETWORK_SLACK = 3  # 客户端超时 = max_execution_time + 余量，让 ClickHouse 自己的超时错误先回来
ERROR_CHARS = 500
TOO_EXPENSIVE_CODES = frozenset({158, 307, 241, 396})
TIMEOUT_CODES = frozenset({159, 160})
AUTH_CODES = frozenset({192, 193, 194, 516})
# 服务端 / 网络问题（不是查询写错了）：分片连不上、并发太多等
SERVER_CODES = frozenset({202, 209, 210, 279, 425, 999, 1000})
_IDENT = re.compile(r'[A-Za-z_@][A-Za-z0-9_.@-]{0,199}')
_TABLE = re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,191}')
_READONLY = re.compile(r'Cannot modify .{0,200}? in readonly mode|\(READONLY\)|Code: 164\b', re.IGNORECASE)
_CODE = re.compile(r'Code:\s*(\d+)')
_PLACEHOLDER = re.compile(r'\{(kd_[A-Za-z0-9_]+):([^{}]+)\}')
_TSV_ESCAPES = {'\\': '\\\\', '\t': '\\t', '\n': '\\n', '\r': '\\r', '\0': '\\0'}
_QUOTED_ESCAPES = {**_TSV_ESCAPES, "'": "\\'"}
_TSV_UNESCAPES = {'\\': '\\', 't': '\t', 'n': '\n', 'r': '\r', '0': '\0', "'": "'", 'b': '\b', 'f': '\f'}

_no_settings = set()  # readonly=1 账号的连接配置：不再带设置


def reset_state():
    _no_settings.clear()


# ---------------------------------------------------------------------------
# 标识符和参数
# ---------------------------------------------------------------------------
def column(name):
    """字段名：校验后用反引号括起来（字段名格式见配置校验，不会有反引号）"""
    if not isinstance(name, str) or not _IDENT.fullmatch(name):
        raise LogError(400, f'字段名不合法：{str(name)[:60]}', 'invalid_request')
    return f'`{name}`'


def table_ref(target):
    """库.表 → `库`.`表`"""
    parts = target.split('.') if isinstance(target, str) else []
    if len(parts) != 2 or not all(_TABLE.fullmatch(part) for part in parts):
        raise LogError(400, f'ClickHouse 表名不合法：{str(target)[:80]}', 'invalid_request')
    return f'`{parts[0]}`.`{parts[1]}`'


def escape_string(value):
    return ''.join(_TSV_ESCAPES.get(ch, ch) for ch in str(value))


def quoted(value):
    return "'" + ''.join(_QUOTED_ESCAPES.get(ch, ch) for ch in str(value)) + "'"


def encode_param(type_, value):
    if type_ == 'String':
        return escape_string(value)
    if type_.startswith('Array('):
        inner = type_[6:-1]
        if inner == 'String':
            return '[' + ','.join(quoted(item) for item in value) + ']'
        return '[' + ','.join(encode_param(inner, item) for item in value) + ']'
    if type_.startswith(('Int', 'UInt')):
        return str(int(value))
    if type_.startswith('Float'):
        return repr(float(value))
    raise ValueError(f'不支持的参数类型 {type_}')


def literal(type_, value):
    """给人看的 SQL：参数代入后的字面量"""
    if type_ == 'String':
        return quoted(value)
    if type_.startswith('Array('):
        inner = type_[6:-1]
        return '[' + ', '.join(literal(inner, item) for item in value) + ']'
    return encode_param(type_, value)


class Params:
    """绑定参数：add 返回 SQL 里的占位符 {kd_name:Type}，名字统一加 kd_ 前缀"""

    def __init__(self):
        self.values = {}

    def add(self, name, type_, value):
        base = name if name.startswith('kd_') else f'kd_{name}'
        name, number = base, 1
        while name in self.values:
            number += 1
            name = f'{base}{number}'
        self.values[name] = (type_, value)
        return f'{{{name}:{type_}}}'

    def url_params(self):
        return {f'param_{name}': encode_param(type_, value) for name, (type_, value) in self.values.items()}

    def render(self, sql):
        """参数代入后的 SQL（给管理员 / AI 核对，不用于执行）"""
        def replace(match):
            item = self.values.get(match.group(1))
            return literal(item[0], item[1]) if item else match.group(0)

        return _PLACEHOLDER.sub(replace, sql)


# ---------------------------------------------------------------------------
# 字段类型
# ---------------------------------------------------------------------------
_WRAPPER = re.compile(r'(?:Nullable|LowCardinality)\((.*)\)')


def unwrap(type_):
    """去掉 Nullable(...)、LowCardinality(...)"""
    text = str(type_ or '').strip()
    while True:
        match = _WRAPPER.fullmatch(text)
        if not match:
            return text
        text = match.group(1).strip()


def kind_of(type_):
    """字段类型 → string / enum / uuid / ip / int / float / bool / date / other"""
    base = unwrap(type_)
    if base == 'String' or base.startswith('FixedString'):
        return 'string'
    if base.startswith('Enum'):
        return 'enum'
    if base == 'UUID':
        return 'uuid'
    if base in ('IPv4', 'IPv6'):
        return 'ip'
    if re.fullmatch(r'U?Int\d+', base):
        return 'int'
    if base.startswith(('Float', 'Decimal', 'BFloat')):
        return 'float'
    if base == 'Bool':
        return 'bool'
    if base.startswith(('Date', 'DateTime')):
        return 'date'
    return 'other'


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------
def _summary(headers, data, elapsed):
    stats = {'elapsed_ms': int(elapsed * 1000), 'read_rows': None}
    raw = headers.get('X-ClickHouse-Summary') if headers else None
    if raw:
        try:
            summary = json.loads(raw)
            if summary.get('read_rows') not in (None, ''):
                stats['read_rows'] = int(summary['read_rows'])
            if summary.get('elapsed_ns') not in (None, '', '0'):
                stats['elapsed_ms'] = int(int(summary['elapsed_ns']) / 1e6)
        except (ValueError, TypeError, AttributeError):
            pass
    statistics = data.get('statistics') if isinstance(data, dict) else None
    if stats['read_rows'] is None and isinstance(statistics, dict) and statistics.get('rows_read') is not None:
        try:
            stats['read_rows'] = int(statistics['rows_read'])
        except (TypeError, ValueError):
            pass
    return stats


def classify_error(status, text, headers=None):
    """ClickHouse 的出错响应 → LogError（错误码见约定 §4.9）"""
    code = None
    header_code = (headers or {}).get('X-ClickHouse-Exception-Code')
    match = _CODE.search(text or '')
    for candidate in (header_code, match.group(1) if match else None):
        try:
            code = int(candidate)
            break
        except (TypeError, ValueError):
            continue
    message = (text or '').strip()[:ERROR_CHARS]
    if code in TOO_EXPENSIVE_CODES:
        return LogError(400, '查询要读的数据超过了上限（单次查询读取行数 / 内存）：加条件（尤其是排序键里的字段）或缩短时间窗口。'
                             f'ClickHouse：{message}', 'too_expensive')
    if code in TIMEOUT_CODES:
        return LogError(504, f'ClickHouse 查询超时：加条件或缩短时间窗口。ClickHouse：{message}', 'timeout')
    if code in AUTH_CODES:
        return LogError(502, f'ClickHouse 认证失败，请系统管理员检查账号密码：{message}', 'datasource_unavailable')
    if code in SERVER_CODES:
        return LogError(502, f'ClickHouse 暂时不可用：{message}', 'datasource_unavailable')
    if code is not None or (status and 400 <= status < 500 and 'DB::Exception' in (text or '')):
        return LogError(400, f'ClickHouse 拒绝了查询：{message}', 'query_rejected')
    if status and status >= 500 or not message or message.startswith('<'):
        return LogError(502, f'ClickHouse 出错（HTTP {status}）' + (f'：{message[:200]}' if message and
                                                                   not message.startswith('<') else ''),
                        'datasource_unavailable')
    return LogError(400, f'ClickHouse 拒绝了查询（HTTP {status}）：{message}', 'query_rejected')


def unescape_tsv(text):
    out, i = [], 0
    while i < len(text):
        ch = text[i]
        if ch == '\\' and i + 1 < len(text):
            out.append(_TSV_UNESCAPES.get(text[i + 1], text[i + 1]))
            i += 2
        else:
            out.append(ch)
            i += 1
    return ''.join(out)


class ClickHouseClient:
    def __init__(self, conn, session, timeout=None, max_rows_to_read=None):
        self.conn, self.session = conn, session
        self.timeout = int(timeout or conn['timeout'])
        self.max_rows_to_read = conn['max_rows_to_read'] if max_rows_to_read is None else max_rows_to_read
        self.key = log_config.fingerprint(conn)
        try:
            headers = ext_api.request_headers(conn)
        except ExtApiError as exc:
            raise LogError(500, f'连接 {conn["id"]} 的{exc.message}', 'decrypt_failed') from None
        headers['Content-Type'] = 'text/plain; charset=utf-8'
        if conn.get('username'):
            headers['X-ClickHouse-User'] = conn['username']
        if conn.get('password'):
            try:
                headers['X-ClickHouse-Key'] = decrypt_secret(conn['password'])
            except ValueError as exc:
                raise LogError(500, f'连接 {conn["id"]} 的密码无法解密（{exc}），请系统管理员重新填写',
                               'decrypt_failed') from None
        self.headers = headers

    def settings(self, extra=None):
        if self.key in _no_settings:
            return {}
        result = {'readonly': '2', 'max_execution_time': str(self.timeout),
                  'output_format_json_quote_64bit_integers': '0'}
        if self.max_rows_to_read:
            result['max_rows_to_read'] = str(int(self.max_rows_to_read))
        result.update({key: str(value) for key, value in (extra or {}).items()})
        return result

    def _url(self, query):
        """自己编码 URL 参数：空格是 %20、+ 是 %2B，不依赖服务端怎么理解 +"""
        text = '&'.join(f'{quote(key, safe="")}={quote(value, safe="")}' for key, value in query.items())
        return URL(f'{self.conn["url"]}/?{text}' if text else f'{self.conn["url"]}/', encoded=True)

    async def _post(self, sql, params, settings):
        query = {**settings, **params.url_params()}
        timeout = self.timeout + NETWORK_SLACK
        try:
            async with limiter(self.conn, self.key):
                async with self.session.post(self._url(query), data=sql.encode('utf-8'), headers=self.headers,
                                             allow_redirects=False, ssl=bool(self.conn['tls_verify']),
                                             timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                    return resp.status, await ext_api.read_limited(resp, MAX_RESPONSE), resp.headers
        except ExtApiError:
            raise ResponseTooLarge() from None
        except asyncio.TimeoutError:
            raise LogError(504, f'ClickHouse 在 {timeout} 秒内没有返回：加条件或缩短时间窗口', 'timeout') from None
        except aiohttp.ClientConnectorCertificateError as exc:
            raise LogError(502, f'ClickHouse 的 HTTPS 证书校验失败（{exc.certificate_error}），自签证书可以在连接配置里关闭'
                                '「校验 HTTPS 证书」', 'datasource_unavailable') from None
        except aiohttp.ClientError as exc:
            raise LogError(502, f'无法访问 ClickHouse：{str(exc) or type(exc).__name__}',
                           'datasource_unavailable') from None

    async def _send(self, sql, params, extra_settings, use_settings=True):
        settings = self.settings(extra_settings) if use_settings else {}
        status, raw, headers = await self._post(sql, params, settings)
        text = raw.decode('utf-8', errors='replace')
        if status != 200 and settings and _READONLY.search(text):
            # 账号是 readonly=1：不能改任何设置，去掉设置重试一次，以后这个连接配置都不再带
            _no_settings.add(self.key)
            logger.info(f'历史日志：连接 {self.conn["id"]} 的 ClickHouse 账号是只读（readonly=1），以后查询不再带设置')
            status, raw, headers = await self._post(sql, params, {})
            text = raw.decode('utf-8', errors='replace')
        return status, text, headers

    async def query(self, sql, params=None, extra_settings=None, use_settings=True):
        """执行一条 SELECT，返回 {'meta', 'data', 'stats'}"""
        params = params or Params()
        sql = sql.rstrip().rstrip(';') + '\nFORMAT JSONCompact'
        started = time.monotonic()
        status, text, headers = await self._send(sql, params, extra_settings, use_settings)
        elapsed = time.monotonic() - started
        if status != 200:
            raise classify_error(status, text, headers)
        try:
            data = json.loads(text)
        except (ValueError, RecursionError):
            # 结果已经开始输出后才出错时，状态码是 200，错误信息接在结果后面
            raise classify_error(None if 'DB::Exception' in text else 502, text, headers) from None
        if not isinstance(data, dict) or not isinstance(data.get('data'), list):
            raise LogError(502, 'ClickHouse 返回的格式不对（没有 data）', 'datasource_unavailable')
        data['stats'] = _summary(headers, data, elapsed)
        return data

    async def explain_ast(self, sql, params=None):
        """EXPLAIN AST 的输出（每行一个节点，按缩进表示层级）。不带 FORMAT（会被当成被解释的查询的一部分），
        服务端默认 TSV：每行按 TSV 反转义；服务端默认格式被改成 JSON 系列时按 JSON 取各行"""
        params = params or Params()
        status, text, headers = await self._send(f'EXPLAIN AST {sql}', params, None)
        if status != 200:
            raise classify_error(status, text, headers)
        stripped = text.strip()
        if stripped.startswith('{'):
            try:
                data = json.loads(stripped)
                return [str(row[0] if isinstance(row, list) else next(iter(row.values()), ''))
                        for row in data.get('data') or []]
            except (ValueError, AttributeError, StopIteration, IndexError):
                pass
        return [unescape_tsv(line) for line in text.split('\n') if line.strip()]


def rows(result):
    """JSONCompact → [{列名: 值}]"""
    names = [item.get('name') for item in result.get('meta') or []]
    return [dict(zip(names, row)) for row in result['data']]
