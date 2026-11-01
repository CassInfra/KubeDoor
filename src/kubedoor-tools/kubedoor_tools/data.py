"""AI 只读查询 MySQL / Redis / Elasticsearch,agent 和 kubedoor-ai 共用。

- check_data_query:参数和只读检查,不合格返回给模型看的原因(规则见各段注释);
- run_data_query:执行。阻塞的客户端调用放线程,整体有超时;结果里像密码的列 / 字段打码,
  单个值最多 MAX_VALUE 字符,整个 data 序列化后约 MAX_OUTPUT 字符,超过就停止并标记 truncated。
参数里有密码:错误信息里去掉 password / sentinel_password / api_key 的值,本模块不记日志。
驱动(PyMySQL、redis-py)在执行时才导入,没装驱动时 import kubedoor_tools 和参数检查照常可用。
"""

from __future__ import annotations

import asyncio
import base64
import codecs
import datetime
import decimal
import inspect
import json
import math
import re
import socket
import ssl
import time
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

from .config import _SECRET_KEY

DATA_OPERATIONS = ("mysql", "redis", "elasticsearch")

MAX_VALUE = 1000       # 单个值最多字符数
MAX_OUTPUT = 60_000    # 整个 data 序列化后的字符数上限(约)
TRUNCATED = "…[截断]"
REDACTED = "[REDACTED]"


class _Invalid(Exception):
    """参数或只读检查不合格,消息直接给模型看"""


class _QueryError(Exception):
    """执行失败:code + 给模型看的消息(返回前还会去掉密码)"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------- 参数


def _known(arguments: dict, allowed: tuple, operation: str) -> None:
    unknown = sorted(str(key) for key in arguments if key not in allowed)
    if unknown:
        raise _Invalid(f"{operation} 不支持参数 {'、'.join(unknown[:5])}；可用参数：{'、'.join(allowed)}")


def _text(arguments: dict, name: str, *, required: bool = False, limit: int = 256) -> str | None:
    value = arguments.get(name)
    if value is None or value == "":
        if required:
            raise _Invalid(f"缺少 {name}")
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise _Invalid(f"{name} 必须是字符串")
    value = str(value)   # 配置里纯数字的用户名 / 密码可能被解析成数字
    if len(value) > limit or "\x00" in value:
        raise _Invalid(f"{name} 太长或含有非法字符")
    return value


def _number(arguments: dict, name: str, default, low, high, *, integer: bool = True):
    value = arguments.get(name)
    if value is None or value == "":
        return default
    if isinstance(value, str) and re.fullmatch(r"\s*-?\d+(?:\.\d+)?\s*", value):
        value = float(value) if "." in value else int(value)
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or (isinstance(value, float) and not math.isfinite(value)) or (integer and value != int(value))):
        raise _Invalid(f"{name} 必须是{'整数' if integer else '数字'}")
    if not low <= value <= high:
        raise _Invalid(f"{name} 取 {low}～{high}")
    return int(value) if integer else float(value)


def _flag(arguments: dict, name: str, default: bool) -> bool:
    value = arguments.get(name)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise _Invalid(f"{name} 必须是 true / false")
    return value


_HOST = re.compile(r"[A-Za-z0-9._:%-]{1,253}")


def _host(arguments: dict) -> str:
    host = _text(arguments, "host", required=True).strip()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]   # [::1]
    if not _HOST.fullmatch(host):
        raise _Invalid("host 只能是主机名或 IP（不带协议、端口和路径）")
    if host.count(":") == 1:
        raise _Invalid("host 不要带端口，端口写在 port")
    return host


def _int_word(word) -> int | None:
    return int(word) if isinstance(word, str) and re.fullmatch(r"-?\d{1,18}", word) else None


# ---------------------------------------------------------------- MySQL
# 只读检查在引号外判断('…'、"…"、`…` 里的内容跳过,处理转义):只能一条语句(末尾一个分号可以)、不能有注释,
# 第一个关键字是 SELECT / SHOW / DESC / DESCRIBE / EXPLAIN / WITH / TABLE,不能写文件、加锁读、调用会等待 /
# 加锁 / 读文件的函数,任何位置都不能出现 INSERT / UPDATE / DELETE / REPLACE(INSERT( / REPLACE( 是字符串函数,
# t.update 这种限定名是列)。执行时还套一层只读事务,结束回滚。

_SQL_START = frozenset({"SELECT", "SHOW", "DESC", "DESCRIBE", "EXPLAIN", "WITH", "TABLE"})
_SQL_FUNCTIONS = ("SLEEP", "BENCHMARK", "GET_LOCK", "RELEASE_LOCK", "RELEASE_ALL_LOCKS", "LOAD_FILE", "MASTER_POS_WAIT",
                  "SOURCE_POS_WAIT", "WAIT_FOR_EXECUTED_GTID_SET", "WAIT_UNTIL_SQL_THREAD_AFTER_GTIDS", "SYS_EXEC",
                  "SYS_EVAL")
_SQL_OUTFILE = re.compile(r"(?<![\w$])INTO\s+(?:OUTFILE|DUMPFILE)(?![\w$])", re.I)
_SQL_LOCKING = re.compile(r"(?<![\w$])(?:FOR\s+(?:UPDATE|SHARE)|LOCK\s+IN\s+SHARE\s+MODE)(?![\w$])", re.I)
_SQL_FUNCTION = re.compile(r"(?<![\w$])(" + "|".join(_SQL_FUNCTIONS) + r")\s*\(", re.I)
_SQL_WRITE = re.compile(r"(?<![\w$.])(UPDATE|DELETE)(?![\w$])|(?<![\w$.])(INSERT|REPLACE)(?![\w$])(?!\s*\()", re.I)
_SQL_LIMIT = 20000


def _mask_sql(sql: str, backslash: bool) -> str:
    """引号里的内容换成占位(字符串 ?,反引号标识符 _),同时拒绝注释;引号没闭合时抛 _Invalid。"""
    out, i, n = [], 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch in "'\"`":
            j = i + 1
            while True:
                if j >= n:
                    raise _Invalid("SQL 的引号没有闭合（字符串里的引号写成两个，例如 'O''Brien'，不要用反斜杠转义引号）")
                if sql[j] == "\\" and backslash and ch != "`":
                    j += 2
                elif sql[j] == ch:
                    if sql[j + 1:j + 2] != ch:
                        break
                    j += 2   # '' 是转义的引号
                else:
                    j += 1
            out.append(" _ " if ch == "`" else " ? ")
            i = j + 1
        elif ch == "#" or sql.startswith(("/*", "--"), i):
            raise _Invalid("SQL 里不能有注释（--、#、/* */，包括 /*! */ 和 /*+ */）")
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _sql_problem(sql: str) -> str | None:
    """只读检查,不合格返回原因。"""
    if "\x00" in sql:
        return "SQL 里不能有 NUL 字符"
    # 服务器开了 NO_BACKSLASH_ESCAPES / ANSI_QUOTES 时反斜杠不转义:两种理解都要通过,引号边界不能有歧义
    for backslash in (True, False):
        try:
            body = _mask_sql(sql, backslash).strip()
        except _Invalid as exc:
            return str(exc)
        if body.endswith(";"):
            body = body[:-1].rstrip()
        if ";" in body:
            return "只能执行一条语句（分号只能出现在末尾）"
        first = re.match(r"[\s(]*([A-Za-z]+)", body)
        word = first.group(1).upper() if first else ""
        if word not in _SQL_START:
            return "只能执行 SELECT / SHOW / DESC / DESCRIBE / EXPLAIN / WITH / TABLE 开头的查询语句" + (f"，不能执行 {word[:20]}" if word else "")
        if _SQL_OUTFILE.search(body):
            return "不能用 INTO OUTFILE / INTO DUMPFILE 写文件"
        if _SQL_LOCKING.search(body):
            return "不能加锁读（FOR UPDATE / FOR SHARE / LOCK IN SHARE MODE）"
        match = _SQL_FUNCTION.search(body)
        if match:
            return f"不能调用 {match.group(1).upper()}()（会等待、加锁或读服务器文件）"
        match = _SQL_WRITE.search(body)
        if match:
            return f"只能查询，不能 {(match.group(1) or match.group(2)).upper()}"
    return None


_MYSQL_ARGS = ("host", "port", "user", "password", "database", "sql", "max_rows", "timeout", "ssl")


def _mysql_args(arguments: dict) -> dict:
    _known(arguments, _MYSQL_ARGS, "mysql")
    args = {"host": _host(arguments), "port": _number(arguments, "port", 3306, 1, 65535),
            "user": _text(arguments, "user", required=True), "password": _text(arguments, "password", limit=4096) or "",
            "database": _text(arguments, "database"), "max_rows": _number(arguments, "max_rows", 100, 1, 1000),
            "timeout": _number(arguments, "timeout", 15, 1, 60, integer=False), "ssl": _flag(arguments, "ssl", False)}
    sql = arguments.get("sql")
    if not isinstance(sql, str) or not sql.strip():
        raise _Invalid("缺少 sql（一条 SELECT / SHOW / DESC / EXPLAIN 查询语句）")
    if len(sql) > _SQL_LIMIT:
        raise _Invalid(f"sql 最长 {_SQL_LIMIT} 字符")
    problem = _sql_problem(sql)
    if problem:
        raise _Invalid(problem)
    sql = sql.strip()
    args["sql"] = sql[:-1].rstrip() if sql.endswith(";") else sql   # 检查过:末尾的分号一定在引号外
    return args


def _load_pymysql():
    try:
        import pymysql
        import pymysql.cursors  # noqa: F401
    except ImportError:
        raise _QueryError("PROGRAM_UNAVAILABLE", "当前环境没有安装 PyMySQL，不能查询 MySQL") from None
    return pymysql


def _tls_context(verify: bool) -> ssl.SSLContext:
    context = ssl.create_default_context()
    if not verify:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


def _mysql_error(exc: Exception, phase: str, timeout: float) -> _QueryError:
    args = getattr(exc, "args", ())
    errno = args[0] if args and isinstance(args[0], int) else None
    text = str(args[1]) if len(args) > 1 else str(exc)
    message = f"MySQL 错误 {errno}：{text}" if errno else f"MySQL 错误：{text}"
    if phase == "connect":
        return _QueryError("CONNECT_FAILED", f"连接 MySQL 失败：{message}")
    if phase == "session":
        return _QueryError("QUERY_FAILED", f"开启只读事务失败，为保证只读没有执行查询：{message}")
    # 3024:MySQL MAX_EXECUTION_TIME;1969:MariaDB max_statement_time;读超时是 2013 ... (timed out)
    if errno in (3024, 1969) or "timed out" in text.lower():
        return _QueryError("TIMEOUT", f"查询超过 {timeout:g} 秒被中止（{message}）")
    return _QueryError("QUERY_FAILED", message)


def _mysql_rows(columns: list[str], rows: list, max_rows: int) -> tuple[list, bool]:
    """转换值、按列名给像密码的列打码,按总量上限截断。"""
    secret = [bool(_SECRET_KEY.match(name)) for name in columns]
    truncated = len(rows) > max_rows
    out, used = [], _json_len(columns) + 100
    for row in rows[:max_rows]:
        values = [REDACTED if hide and value is not None else _plain(value) for hide, value in zip(secret, row)]
        size = _json_len(values) + 1
        if used + size > MAX_OUTPUT:
            return out, True
        out.append(values)
        used += size
    return out, truncated


def _mysql_query(args: dict) -> dict:
    pymysql = _load_pymysql()
    timeout = args["timeout"]
    options = {
        "host": args["host"], "port": args["port"], "user": args["user"], "password": args["password"].encode("utf-8"),
        "database": args["database"], "charset": "utf8mb4", "connect_timeout": min(timeout, 10),
        "read_timeout": timeout, "write_timeout": timeout,
        # 不开 CLIENT.MULTI_STATEMENTS(PyMySQL 默认也不开)、不允许 LOAD DATA LOCAL;无缓冲游标只读需要的行数
        "client_flag": 0, "local_infile": False, "cursorclass": pymysql.cursors.SSCursor, "program_name": "kubedoor",
    }
    if args["ssl"]:
        options["ssl"] = _tls_context(verify=False)   # 和 JDBC useSSL=true 一样只加密、不校验证书
    try:
        connection = pymysql.connect(**options)
    except pymysql.MySQLError as exc:
        raise _mysql_error(exc, "connect", timeout) from None
    except Exception as exc:
        hint = "（MySQL 8 的 caching_sha2_password 首次认证需要 cryptography，可以把 ssl 设为 true 重试）" if "cryptography" in str(exc) else ""
        raise _QueryError("CONNECT_FAILED", f"连接 MySQL 失败：{type(exc).__name__}: {exc}{hint}") from None
    phase, cursor = "session", None
    try:
        cursor = connection.cursor()
        cursor.execute("SET SESSION TRANSACTION READ ONLY")
        for statement in (f"SET SESSION MAX_EXECUTION_TIME={int(timeout * 1000)}", f"SET SESSION max_statement_time={timeout:g}"):
            try:
                cursor.execute(statement)
                break
            except pymysql.MySQLError:
                continue   # MySQL 5.7.8 之前没有 MAX_EXECUTION_TIME,MariaDB 用 max_statement_time;都不支持就算了
        cursor.execute("START TRANSACTION READ ONLY")
        phase = "query"
        started = time.monotonic()
        cursor.execute(args["sql"])
        columns = [str(item[0]) for item in cursor.description or ()]
        rows = list(cursor.fetchmany(args["max_rows"] + 1)) if cursor.description else []
        elapsed = int((time.monotonic() - started) * 1000)
        if len(rows) <= args["max_rows"]:
            # 结果已经读完才回滚;没读完时回滚会先把剩下的行全部读完,直接断开(服务端自动回滚)
            connection.rollback()
    except pymysql.MySQLError as exc:
        raise _mysql_error(exc, phase, timeout) from None
    finally:
        # 没读完的结果标记成已结束:不然 PyMySQL 回收结果对象时会去读已经关闭的连接(往 stderr 打异常)
        result = getattr(cursor, "_result", None)
        if getattr(result, "unbuffered_active", False):
            result.unbuffered_active = False
        try:
            connection.close()
        except Exception:
            pass
    values, truncated = _mysql_rows(columns, rows, args["max_rows"])
    return {"columns": [_plain(name) for name in columns], "rows": values, "row_count": len(values),
            "truncated": truncated, "elapsed_ms": elapsed}


# ---------------------------------------------------------------- Redis
# 只读命令白名单(不区分大小写;两段的命令检查子命令),其余一律拒绝。KEYS 提示改用 SCAN,SCAN 类的 COUNT 最大 1000;
# CONFIG GET 不能用通配符,也不能查密码 / 认证 / ACL 相关参数。执行时一次可能返回整个集合的命令先在服务端限定数量。
# 集群的 MOVED / ASK 在内部跟随(最多 3 次);给了 sentinel_master 时 host:port 是哨兵,先向哨兵查主节点再执行。
# 跳转目标、哨兵返回的主节点都来自 Redis 本身,模型不用(网关也不让它)拿着密码去连配置里没有的地址。

_REDIS_ARGS = ("host", "port", "username", "password", "db", "tls", "command", "max_items", "timeout",
               "sentinel_master", "sentinel_username", "sentinel_password")
_NAME_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_REDIS_READ = frozenset("""
GET MGET STRLEN GETRANGE EXISTS TYPE TTL PTTL EXPIRETIME PEXPIRETIME
HGET HMGET HGETALL HKEYS HVALS HLEN HEXISTS HSTRLEN HRANDFIELD HSCAN
LRANGE LLEN LINDEX LPOS
SMEMBERS SCARD SISMEMBER SMISMEMBER SRANDMEMBER SSCAN
ZRANGE ZRANGEBYSCORE ZREVRANGE ZREVRANGEBYSCORE ZRANGEBYLEX ZREVRANGEBYLEX ZSCORE ZMSCORE ZCARD ZCOUNT ZLEXCOUNT
ZRANK ZREVRANK ZRANDMEMBER ZSCAN
XRANGE XREVRANGE XLEN XPENDING
BITCOUNT BITPOS GETBIT PFCOUNT GEOPOS GEODIST GEOHASH GEOSEARCH GEORADIUS_RO GEORADIUSBYMEMBER_RO SORT_RO
SCAN DBSIZE INFO PING TIME ROLE LASTSAVE
JSON.GET JSON.TYPE JSON.STRLEN JSON.OBJKEYS JSON.ARRLEN
""".split())
_REDIS_SUBCOMMANDS = {
    "XINFO": ("STREAM", "GROUPS", "CONSUMERS"), "MEMORY": ("USAGE", "STATS"),
    "OBJECT": ("ENCODING", "FREQ", "IDLETIME", "REFCOUNT"), "SLOWLOG": ("GET", "LEN"), "CLIENT": ("LIST", "INFO"),
    "CLUSTER": ("INFO", "NODES", "SHARDS", "SLOTS", "KEYSLOT", "COUNTKEYSINSLOT"),
    "LATENCY": ("LATEST", "HISTORY", "DOCTOR"), "CONFIG": ("GET",),
}
_REDIS_SCAN = {"SCAN": 1, "HSCAN": 2, "SSCAN": 2, "ZSCAN": 2}   # 游标的位置
_REDIS_SECRET_CONFIG = re.compile(r"pass|auth|acl", re.I)


def _redis_problem(words: list[str]) -> str | None:
    name = words[0].upper()
    if name == "KEYS":
        return '不能用 KEYS（会遍历整个库、阻塞 Redis），请改用 SCAN 分批查找，例如 ["SCAN", "0", "MATCH", "user:*", "COUNT", "100"]'
    if name in _REDIS_SUBCOMMANDS:
        sub = words[1].upper() if len(words) > 1 else ""
        if sub not in _REDIS_SUBCOMMANDS[name]:
            return f"{name} 只能用只读子命令 {' / '.join(_REDIS_SUBCOMMANDS[name])}"
        if name == "CONFIG":
            if len(words) < 3:
                return "CONFIG GET 要写完整的参数名，例如 maxmemory"
            for param in words[2:]:
                if "*" in param or "?" in param:
                    return "CONFIG GET 不能用通配符（* ?），请写完整的参数名"
                if param.lower() in ("requirepass", "masterauth", "masteruser") or _REDIS_SECRET_CONFIG.search(param):
                    return f"CONFIG GET 不能查看密码 / 认证 / ACL 相关的参数（{param[:40]}）"
        if name == "MEMORY" and sub == "USAGE":
            for index in range(3, len(words)):
                if words[index].upper() == "SAMPLES":
                    count = _int_word(words[index + 1]) if index + 1 < len(words) else None
                    if count is None or not 1 <= count <= 1000:
                        return "MEMORY USAGE 的 SAMPLES 取 1～1000（0 表示遍历整个 key，会阻塞 Redis）"
        return None
    if name not in _REDIS_READ:
        return f"Redis 命令 {name[:40]} 不在只读白名单里（只能查询，不能修改数据、配置或执行脚本）"
    if name in _REDIS_SCAN:
        index = _REDIS_SCAN[name] + 1
        while index < len(words):
            option = words[index].upper()
            if option == "COUNT":
                count = _int_word(words[index + 1]) if index + 1 < len(words) else None
                if count is None or not 1 <= count <= 1000:
                    return f"{name} 的 COUNT 取 1～1000"
            index += 1 if option == "NOVALUES" else 2   # MATCH / COUNT / TYPE 都带一个值
    return None


def _sentinel_name(arguments: dict) -> str | None:
    """sentinel_master:哨兵里的主节点名(spring.redis.sentinel.master);没给(或 null)表示不用哨兵"""
    value = arguments.get("sentinel_master")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise _Invalid("sentinel_master 必须是字符串（哨兵里的主节点名，对应 spring.redis.sentinel.master）")
    value = str(value).strip()
    if not value:
        raise _Invalid("sentinel_master 不能为空（不用哨兵时不要传这个参数）")
    if len(value) > 200 or _NAME_CONTROL.search(value):
        raise _Invalid("sentinel_master 最多 200 个字符，不能含控制字符")
    return value


def _redis_args(arguments: dict) -> dict:
    _known(arguments, _REDIS_ARGS, "redis")
    sentinel = _sentinel_name(arguments)
    args = {"host": _host(arguments), "port": _number(arguments, "port", 26379 if sentinel else 6379, 1, 65535),
            "username": _text(arguments, "username"), "password": _text(arguments, "password", limit=4096),
            "db": _number(arguments, "db", 0, 0, 65535), "tls": _flag(arguments, "tls", False),
            "max_items": _number(arguments, "max_items", 100, 1, 1000),
            "timeout": _number(arguments, "timeout", 10, 1, 60, integer=False),
            "sentinel_master": sentinel, "sentinel_username": _text(arguments, "sentinel_username"),
            "sentinel_password": _text(arguments, "sentinel_password", limit=4096)}
    if not sentinel and (args["sentinel_username"] or args["sentinel_password"]):
        raise _Invalid("sentinel_username / sentinel_password 是哨兵自己的认证，只在用哨兵（给了 sentinel_master）时使用")
    command = arguments.get("command")
    if not isinstance(command, list) or not command:
        raise _Invalid('缺少 command（字符串数组，例如 ["GET", "user:1"]）')
    if len(command) > 1000:
        raise _Invalid("command 最多 1000 项")
    words = []
    for item in command:
        if isinstance(item, float) and math.isfinite(item):
            item = str(int(item)) if item.is_integer() else repr(item)
        elif isinstance(item, int) and not isinstance(item, bool):
            item = str(item)
        if not isinstance(item, str) or len(item) > 10000:
            raise _Invalid('command 的每一项都必须是字符串，例如 ["HGET", "user:1", "name"]')
        words.append(item)
    if not words[0]:
        raise _Invalid("command 第一项是命令名")
    problem = _redis_problem(words)
    if problem:
        raise _Invalid(problem)
    args["command"] = words
    return args


def _load_redis():
    try:
        import redis
        from redis.backoff import NoBackoff
        from redis.retry import Retry
    except ImportError:
        raise _QueryError("PROGRAM_UNAVAILABLE", "当前环境没有安装 redis-py，不能查询 Redis") from None
    return SimpleNamespace(Redis=redis.Redis, Retry=Retry, NoBackoff=NoBackoff, exceptions=redis.exceptions)


# 一次返回整个集合、没有数量参数的命令:先查元素个数(O(1)),太大就拒绝
_REDIS_CARDINALITY = {"HGETALL": "HLEN", "HKEYS": "HLEN", "HVALS": "HLEN", "SMEMBERS": "SCARD"}
_REDIS_FULL_LIMIT = 10_000
_REDIS_INDEX_RANGE = {"LRANGE": "LLEN", "ZRANGE": "ZCARD", "ZREVRANGE": "ZCARD"}
# 命令 → (选项开始的位置, 选项带几个值)
_REDIS_LIMIT = {name: (4, {"LIMIT": 2}) for name in ("ZRANGE", "ZRANGEBYSCORE", "ZREVRANGEBYSCORE", "ZRANGEBYLEX", "ZREVRANGEBYLEX")}
_REDIS_LIMIT["SORT_RO"] = (2, {"BY": 1, "LIMIT": 2, "GET": 1})
# 命令 → (选项开始的位置, 选项带几个值, 没写 COUNT 时补上)
_REDIS_COUNT = {
    "XRANGE": (4, {"COUNT": 1}, True), "XREVRANGE": (4, {"COUNT": 1}, True),
    "GEOSEARCH": (2, {"FROMMEMBER": 1, "FROMLONLAT": 2, "BYRADIUS": 2, "BYBOX": 3, "COUNT": 1}, True),
    "GEORADIUS_RO": (6, {"COUNT": 1}, True), "GEORADIUSBYMEMBER_RO": (5, {"COUNT": 1}, True),
    "LPOS": (3, {"RANK": 1, "COUNT": 1, "MAXLEN": 1}, False),
}
_REDIS_RANDOM = frozenset({"SRANDMEMBER", "ZRANDMEMBER", "HRANDFIELD"})
# 顶层返回长文本的报告类命令(INFO、LATENCY DOCTOR 等):文本不按单个值截断,只受总量限制
_REDIS_REPORTS = frozenset({"INFO", "LATENCY", "CLIENT", "CLUSTER", "MEMORY", "XINFO", "SLOWLOG"})


def _find_option(words: list[str], start: int, name: str, takes: dict) -> int | None:
    index = start
    while index < len(words):
        option = words[index].upper()
        if option == name:
            return index
        index += 1 + takes.get(option, 0)
    return None


def _redis_bound(client, words: list[str], limit: int) -> list[str]:
    """一次可能返回整个集合的命令在服务端限定数量(多取一个,用来判断截断),保护 Redis 和本进程的内存。"""
    words, name, want = list(words), words[0].upper(), limit + 1
    by_score = any(word.upper() in ("BYSCORE", "BYLEX") for word in words[4:])
    if name in _REDIS_CARDINALITY and len(words) == 2:
        size = client.execute_command(_REDIS_CARDINALITY[name], words[1])
        if isinstance(size, int) and size > _REDIS_FULL_LIMIT:
            scan = "HSCAN" if _REDIS_CARDINALITY[name] == "HLEN" else "SSCAN"
            raise _QueryError("TOO_LARGE", f"这个 key 有 {size} 个元素，{name} 会一次返回全部并阻塞 Redis；"
                                           f"请改用 {scan}（COUNT 不超过 1000）分批查看，或者只查需要的字段 / 成员")
    elif name in _REDIS_INDEX_RANGE and len(words) >= 4 and not by_score:
        start, stop = _int_word(words[2]), _int_word(words[3])
        if start is not None and stop is not None:
            if start < 0 or stop < 0:
                length = client.execute_command(_REDIS_INDEX_RANGE[name], words[1])
                if not isinstance(length, int):
                    return words
                start = max(length + start, 0) if start < 0 else start
                stop = length + stop if stop < 0 else stop
            if stop - start + 1 > want:
                words[2], words[3] = str(start), str(start + want - 1)
    elif name in _REDIS_LIMIT and (name != "ZRANGE" or by_score):
        start, takes = _REDIS_LIMIT[name]
        index = _find_option(words, start, "LIMIT", takes)
        if index is None:
            words += ["LIMIT", "0", str(want)]
        elif index + 2 < len(words):
            count = _int_word(words[index + 2])
            if count is not None and (count < 0 or count > want):
                words[index + 2] = str(want)
    elif name in _REDIS_COUNT:
        start, takes, append = _REDIS_COUNT[name]
        index = _find_option(words, start, "COUNT", takes)
        if index is None:
            if append:
                words += ["COUNT", str(want)]
        elif index + 1 < len(words):
            count = _int_word(words[index + 1])
            if count is not None and (count > want or (name == "LPOS" and count == 0)):   # LPOS 的 COUNT 0 是全部
                words[index + 1] = str(want)
    elif name in _REDIS_RANDOM and len(words) >= 3:
        count = _int_word(words[2])
        if count is not None and abs(count) > want:   # 负数允许重复,-100000000 会生成上亿个元素
            words[2] = str(want if count > 0 else -want)
    elif name == "XPENDING" and len(words) > 3:
        index = 5 if words[3].upper() == "IDLE" else 3
        if index + 2 < len(words):
            count = _int_word(words[index + 2])
            if count is not None and count > want:
                words[index + 2] = str(want)
    return words


def _redis_result(words: list[str], result: Any, max_items: int) -> tuple[Any, bool]:
    name = words[0].upper()
    if name == "HGET" and len(words) == 3 and result is not None and _SECRET_KEY.match(words[2]):
        result = REDACTED
    elif name == "HMGET" and isinstance(result, list):
        result = [REDACTED if value is not None and _SECRET_KEY.match(field) else value
                  for field, value in zip(words[2:], result)]
    elif name == "JSON.GET" and isinstance(result, str):
        try:
            result = json.loads(result)   # 文档里像密码的字段也要打码
        except (ValueError, RecursionError):
            pass
    if isinstance(result, str) and name in _REDIS_REPORTS:
        return (result, False) if len(result) <= MAX_OUTPUT else (result[:MAX_OUTPUT] + TRUNCATED, True)
    value, truncated = _plain(result), False
    if name in _REDIS_SCAN and isinstance(value, list) and len(value) == 2:
        items, cut = _first(value[1], max_items)
        items, over = _fit(items, MAX_OUTPUT - 100)
        return [str(value[0]), items], cut or over   # 游标保留,用它继续 SCAN
    if isinstance(value, list) or (isinstance(value, dict) and name == "HGETALL"):
        value, truncated = _first(value, max_items)
    value, over = _fit(value, MAX_OUTPUT)
    return value, truncated or over


_REDIS_MAX_HOPS = 3   # 集群 MOVED / ASK 最多跟随几次


def _endpoint(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def _redis_options(lib, args: dict, host: str, port: int, timeout: float, *, db: int, username, password) -> dict:
    options = {
        "host": host, "port": port, "db": db, "username": username, "password": password,
        "socket_timeout": timeout, "socket_connect_timeout": min(timeout, 10),
        # redis-py 新版默认 RESP3(HELLO,Redis 6 之前没有)、失败重试 10 次、连上后发 CLIENT SETINFO:都关掉
        "retry": lib.Retry(lib.NoBackoff(), 0), "protocol": 2, "health_check_interval": 0,
        "decode_responses": True, "encoding": "utf-8", "encoding_errors": "replace",
    }
    try:
        new_style = "driver_info" in inspect.signature(lib.Redis).parameters
    except (TypeError, ValueError):
        new_style = False
    # 新版 redis-py 用 driver_info=None 关掉 CLIENT SETINFO(lib_name / lib_version 已弃用,以后会删掉);旧版只认后者
    options.update({"driver_info": None} if new_style else {"lib_name": None, "lib_version": None})
    if args["tls"]:   # 哨兵和数据节点都用(哨兵开 TLS 的部署通常整套都开)
        options.update(ssl=True, ssl_cert_reqs="none", ssl_check_hostname=False)   # 只加密、不校验证书
    return options


def _redis_failure(errors, exc: BaseException, where: str, timeout: float) -> BaseException:
    """redis-py 的异常换成给模型看的 _QueryError(where 是出错的节点);不是 redis-py 的异常原样返回"""
    if isinstance(exc, errors.AuthenticationError):
        return _QueryError("CONNECT_FAILED", f"{where} 认证失败：{exc}")
    if isinstance(exc, (errors.TimeoutError, TimeoutError)):
        return _QueryError("TIMEOUT", f"{where} 在 {timeout:g} 秒内没有响应：{exc}")
    if isinstance(exc, errors.ConnectionError):
        return _QueryError("CONNECT_FAILED", f"{where} 连接失败：{exc}")
    if isinstance(exc, errors.RedisError):
        return _QueryError("QUERY_FAILED", f"{where} 返回错误：{exc}")
    return exc


def _close(client) -> None:
    if client is not None:
        try:
            client.close()
        except Exception:
            pass


class _RedisNode:
    """一个节点上的客户端。ask=True(ASK 跳过来的)时每条命令前先发 ASKING:正在导入槽的节点只接受紧跟在
    ASKING 后面的一条命令。两条命令走同一个连接(客户端只有这一个连接,用完放回、下一条再取出来)。"""

    def __init__(self, client, ask: bool):
        self.client, self.ask = client, ask

    def execute_command(self, *command):
        if self.ask:
            self.client.execute_command("ASKING")
        return self.client.execute_command(*command)


def _redis_node_query(lib, args: dict, host: str, port: int, timeout: float, ask: bool, where: str) -> tuple[Any, int]:
    """在一个节点上执行(含限量用的预查询),返回 (结果, 耗时毫秒);MOVED / ASK 原样抛出,由调用方跳转。"""
    errors, client = lib.exceptions, None
    try:
        client = lib.Redis(**_redis_options(lib, args, host, port, timeout, db=args["db"],
                                            username=args["username"], password=args["password"]))
        node = _RedisNode(client, ask)
        words = _redis_bound(node, args["command"], args["max_items"])
        started = time.monotonic()
        result = node.execute_command(*words)
        return result, int((time.monotonic() - started) * 1000)
    except errors.AskError:   # MovedError 是 AskError 的子类
        raise
    except Exception as exc:
        failure = _redis_failure(errors, exc, where, args["timeout"])
        if failure is exc:
            raise
        raise failure from None
    finally:
        _close(client)


def _sentinel_master(lib, args: dict, timeout: float) -> tuple[str, int]:
    """向哨兵(host:port)查主节点地址。哨兵的认证用 sentinel_username / sentinel_password(没给就不认证),不选库。"""
    errors, name, client = lib.exceptions, args["sentinel_master"], None
    where = f"哨兵 {_endpoint(args['host'], args['port'])}"
    try:
        client = lib.Redis(**_redis_options(lib, args, args["host"], args["port"], timeout, db=0,
                                            username=args["sentinel_username"], password=args["sentinel_password"]))
        reply = client.execute_command("SENTINEL", "GET-MASTER-ADDR-BY-NAME", name)
    except errors.ResponseError as exc:   # 认证失败是 ConnectionError,不在这里
        hint = "（host / port 要填哨兵的地址，哨兵端口通常是 26379）" if "unknown command" in str(exc).lower() else ""
        raise _QueryError("QUERY_FAILED", f"{where} 查询主节点失败：{exc}{hint}") from None
    except Exception as exc:
        failure = _redis_failure(errors, exc, where, args["timeout"])
        if failure is exc:
            raise
        raise failure from None
    finally:
        _close(client)
    if not reply:
        raise _QueryError("QUERY_FAILED", f"哨兵上没有这个主节点名：{name}（请核对 sentinel_master，对应 spring.redis.sentinel.master）")
    host = port = None
    if isinstance(reply, (list, tuple)) and len(reply) == 2:
        host, port = (item.decode("utf-8", "replace") if isinstance(item, bytes) else item for item in reply)
        port = _int_word(port) if isinstance(port, str) else port
    if (not isinstance(host, str) or not _HOST.fullmatch(host) or isinstance(port, bool) or not isinstance(port, int)
            or not 1 <= port <= 65535):
        raise _QueryError("QUERY_FAILED", f"{where} 返回的主节点地址无法识别：{str(reply)[:200]}")
    return host, port


def _redirect_target(errors, exc: Exception, current_host: str) -> tuple[str, str, int, Any]:
    """MOVED / ASK 指向的 (类型, host, port, 槽)。主机名为空表示和当前节点同一个主机、只换端口
    (Redis 7 的 cluster-preferred-endpoint-type unknown-endpoint);是 ? 之类认不出的地址就报错。"""
    kind = "MOVED" if isinstance(exc, errors.MovedError) else "ASK"
    host, port, slot = getattr(exc, "host", None), getattr(exc, "port", None), getattr(exc, "slot_id", None)
    if host is None or port is None:
        match = re.search(r"(\d+)\s+(\S*):(\d+)\s*$", str(exc))
        if match:
            slot, host, port = match.group(1), match.group(2), int(match.group(3))
    if host == "":
        host = current_host
    if isinstance(host, str) and host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if (not isinstance(host, str) or not _HOST.fullmatch(host) or isinstance(port, bool) or not isinstance(port, int)
            or not 1 <= port <= 65535):
        raise _QueryError("REDIS_MOVED", f"Redis 集群的 {kind} 跳转目标无法识别：{str(exc)[:200]}")
    return kind, host, port, slot


def _redis_query(args: dict) -> dict:
    lib = _load_redis()
    errors, timeout = lib.exceptions, args["timeout"]
    deadline = time.monotonic() + timeout   # 查哨兵、各次跳转共用一个总时限

    def remaining() -> float:
        left = deadline - time.monotonic()
        if left <= 0:
            raise _QueryError("TIMEOUT", f"Redis 查询在 {timeout:g} 秒内没有完成（含查哨兵和集群跳转）")
        return left

    extra: dict = {}
    host, port, where = args["host"], args["port"], "Redis"
    if args["sentinel_master"]:
        host, port = _sentinel_master(lib, args, remaining())
        extra["master"] = _endpoint(host, port)
        where = f"哨兵返回的主节点 {extra['master']}"
    path, ask = [_endpoint(host, port)], False
    while True:
        try:
            result, elapsed = _redis_node_query(lib, args, host, port, remaining(), ask, where)
            break
        except errors.AskError as exc:   # 集群:用同样的账号、TLS 连目标节点重发(ASK 先发 ASKING)
            kind, host, port, slot = _redirect_target(errors, exc, host)
            target = _endpoint(host, port)
            if len(path) > _REDIS_MAX_HOPS:
                raise _QueryError("REDIS_MOVED", f"Redis 集群跳转 {_REDIS_MAX_HOPS} 次后仍然返回 {kind}，最后指向节点 "
                                                 f"{target}（槽 {slot}；依次经过 {' → '.join(path)}），"
                                                 "集群可能正在迁移槽，请稍后重试") from None
            path.append(target)
            ask, where = kind == "ASK", f"集群跳转的目标节点 {target}"
    value, truncated = _redis_result(args["command"], result, args["max_items"])
    data = {"result": value, "truncated": truncated, "elapsed_ms": elapsed, **extra}
    if len(path) > 1:
        data["redirected_to"] = path[-1]
    return data


# ---------------------------------------------------------------- Elasticsearch
# 只允许 GET / HEAD / POST 白名单里的查询接口(<index> 不以 _ 开头、不含 /,可以带通配符和逗号);
# 带 scroll 参数、scroll / PIT 接口一律拒绝;SQL 接口的 query 按上面 MySQL 的只读规则检查。不跟随重定向、不走代理。

_ES_ARGS = ("url", "username", "password", "api_key", "method", "path", "params", "body", "tls_verify", "timeout")
_INDEX = r"(?:/[^/_][^/]*)"
_ID = r"/[^/]+"
_SUB = r"(?:/[^/]+)*"
_ES_READ = re.compile("(?:" + "|".join([
    r"/", r"/_cluster/health" + _SUB, r"/_cluster/stats", r"/_cat" + _SUB, r"/_nodes" + _SUB,
    r"/_alias" + _SUB, r"/_aliases",
    _INDEX + r"?/_mapping" + _SUB, _INDEX + r"?/_settings" + _SUB, _INDEX + r"?/_stats" + _SUB,
    _INDEX + r"?/_count", _INDEX + r"?/_search",
    _INDEX + r"/_doc" + _ID, _INDEX + r"/_source" + _ID, _INDEX, r"/_resolve/index" + _ID, r"/_tasks" + _SUB,
]) + ")")
_ES_SQL = frozenset({"/_sql", "/_sql/translate", "/_plugins/_sql", "/_opendistro/_sql"})
_ES_POST = re.compile("(?:" + "|".join([
    _INDEX + r"?/_search", _INDEX + r"?/_count", _INDEX + r"?/_msearch", _INDEX + r"?/_mget",
    _INDEX + r"/_explain" + _ID, _INDEX + r"?/_validate/query", _INDEX + r"?/_field_caps",
    *(re.escape(path) for path in sorted(_ES_SQL)),
]) + ")")
_ES_BAD_PATH = re.compile(r"\.\.|//|\\|%(?:2f|5c|2e)|[\s?#\x00-\x1f\x7f]", re.I)
_ES_READ_LIMIT = 4 * 1024 * 1024   # 最多读这么多;再大的响应只能当文本截断
_ES_BODY_LIMIT = 1024 * 1024


def _es_args(arguments: dict) -> dict:
    _known(arguments, _ES_ARGS, "elasticsearch")
    url = _text(arguments, "url", required=True, limit=2048).strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or re.search(r"[\s\x00-\x1f\x7f]", url):
        raise _Invalid("url 要写成 http(s)://host:port，可以带路径前缀")
    if "@" in parts.netloc:
        raise _Invalid("url 里不能带账号密码，请放到 username / password（或 api_key）")
    if "?" in url or "#" in url:
        raise _Invalid("url 不能带查询参数，查询参数放到 params")
    try:
        parts.port
    except ValueError:
        raise _Invalid("url 的端口不对") from None
    if ".." in parts.path or "//" in parts.path:
        raise _Invalid("url 的路径前缀不能包含 .. 或 //")
    method = arguments.get("method") or "GET"
    method = method.upper() if isinstance(method, str) else ""
    if method not in ("GET", "HEAD", "POST"):
        raise _Invalid("method 只能是 GET / POST / HEAD（只读查询，不能 PUT / DELETE / PATCH）")
    path = arguments.get("path")
    if not isinstance(path, str) or not path.startswith("/"):
        raise _Invalid("path 必须以 / 开头，例如 /_cat/indices")
    if len(path) > 2048 or _ES_BAD_PATH.search(path):
        raise _Invalid("path 不能包含 ..、//、%2f、反斜杠、空白和 ? #（查询参数放到 params）")
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    if path.endswith("/_search/scroll") or "/_pit" in path:
        raise _Invalid("不能用 scroll / PIT（会在集群里保留搜索上下文），请用 from + size 或 search_after 分页")
    if not (_ES_POST if method == "POST" else _ES_READ).fullmatch(path):
        other = _ES_READ if method == "POST" else _ES_POST
        hint = f"，这个接口要用 {'GET' if method == 'POST' else 'POST'}" if other.fullmatch(path) else ""
        raise _Invalid(f"{method} {path[:100]} 不在只读查询接口白名单里{hint}（不能写入、更新、删除或管理集群）")
    params = arguments.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise _Invalid("params 必须是对象，例如 {\"format\": \"json\"}")
    query = {}
    for key, value in params.items():
        if not isinstance(key, str) or not key:
            raise _Invalid("params 的键必须是字符串")
        if key == "scroll":
            raise _Invalid("不能用 scroll（会在集群里保留搜索上下文），请用 from + size 或 search_after 分页")
        if isinstance(value, bool):
            value = "true" if value else "false"
        elif isinstance(value, (int, float)) and math.isfinite(value):
            value = str(int(value)) if float(value).is_integer() else repr(value)
        elif not isinstance(value, str):
            raise _Invalid("params 的值只能是字符串、数字或布尔")
        query[key] = value
    body = arguments.get("body")
    if body is not None:
        if method != "POST":
            raise _Invalid("body 只能和 POST 一起用（带查询条件时 method 写 POST）")
        if not isinstance(body, (dict, list)):
            raise _Invalid("body 必须是 JSON 对象或数组")
        try:
            size = len(json.dumps(body, ensure_ascii=False))
        except (TypeError, ValueError, RecursionError):
            raise _Invalid("body 不是合法的 JSON") from None
        if size > _ES_BODY_LIMIT:
            raise _Invalid("body 最大 1MB")
    ndjson = path.endswith("/_msearch")
    if ndjson and not (isinstance(body, list) and body and all(isinstance(item, dict) for item in body)):
        raise _Invalid("_msearch 的 body 要写成数组：[{头}, {查询}, {头}, {查询}…]")
    if path in _ES_SQL:
        sql = body.get("query") if isinstance(body, dict) else None
        if not isinstance(sql, str) and not (isinstance(body, dict) and isinstance(body.get("cursor"), str)):
            raise _Invalid('SQL 接口的 body 要写成 {"query": "SELECT ..."}')
        problem = _sql_problem(sql) if isinstance(sql, str) else None
        if problem:
            raise _Invalid(f"SQL {problem}")
    username = _text(arguments, "username")
    password = _text(arguments, "password", limit=4096)
    api_key = _text(arguments, "api_key", limit=4096)
    if api_key and (username or password):
        raise _Invalid("api_key 和 username / password 只能用一种")
    if password and not username:
        raise _Invalid("用 password 时还要写 username")
    return {"url": url.rstrip("/"), "method": method, "path": path, "params": query, "body": body, "ndjson": ndjson,
            "username": username, "password": password, "api_key": api_key,
            "tls_verify": _flag(arguments, "tls_verify", True),
            "timeout": _number(arguments, "timeout", 15, 1, 60, integer=False)}


def _es_result(status: int, content_type: str, raw: bytes, method: str) -> dict:
    truncated = len(raw) > _ES_READ_LIMIT
    if truncated:
        body: Any = raw[:MAX_OUTPUT * 4].decode("utf-8", errors="replace")
    else:
        body = raw.decode("utf-8", errors="replace")
        if body.strip() and ("json" in content_type.lower() or body.lstrip()[:1] in ("{", "[")):
            try:
                body = _plain(json.loads(body))
            except (ValueError, RecursionError):
                pass
    if isinstance(body, str):
        if len(body) > MAX_OUTPUT:
            body, truncated = body[:MAX_OUTPUT] + TRUNCATED, True
    elif _json_len(body) > MAX_OUTPUT:
        body, truncated = json.dumps(body, ensure_ascii=False)[:MAX_OUTPUT] + TRUNCATED, True
    if method == "HEAD":
        body = None
    return {"status": status, "body": body, "truncated": truncated}


def _es_query(args: dict) -> dict:
    import http.client
    import urllib.error
    import urllib.request

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None   # 3xx 原样当响应返回,不跟随

    url = args["url"] + args["path"] + ("?" + urlencode(args["params"]) if args["params"] else "")
    headers = {"Accept": "application/json"}
    data = None
    if args["body"] is not None:
        if args["ndjson"]:
            data = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in args["body"]).encode("utf-8")
            headers["Content-Type"] = "application/x-ndjson"
        else:
            data = json.dumps(args["body"], ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
    if args["api_key"]:
        headers["Authorization"] = "ApiKey " + args["api_key"]
    elif args["username"]:
        token = base64.b64encode(f"{args['username']}:{args['password'] or ''}".encode("utf-8")).decode("ascii")
        headers["Authorization"] = "Basic " + token
    request = urllib.request.Request(url, data=data, method=args["method"], headers=headers)
    # 不走环境变量里的代理(业务 ES 在集群内网,也不能把账号发给代理)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
                                         urllib.request.HTTPSHandler(context=_tls_context(args["tls_verify"])))
    timeout = args["timeout"]
    try:
        try:
            with opener.open(request, timeout=timeout) as response:
                status, content_type = response.status, response.headers.get("Content-Type", "")
                raw = response.read(_ES_READ_LIMIT + 1)
        except urllib.error.HTTPError as exc:   # 4xx / 5xx / 没跟随的 3xx:也是查询结果
            try:
                status, content_type = exc.code, exc.headers.get("Content-Type", "") if exc.headers else ""
                raw = exc.read(_ES_READ_LIMIT + 1) if getattr(exc, "fp", None) else b""
            finally:
                try:
                    exc.close()
                except Exception:
                    pass
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            raise _QueryError("TIMEOUT", f"Elasticsearch 在 {timeout:g} 秒内没有响应") from None
        raise _QueryError("CONNECT_FAILED", f"连接 Elasticsearch 失败：{exc.reason}") from None
    except (TimeoutError, socket.timeout):
        raise _QueryError("TIMEOUT", f"Elasticsearch 在 {timeout:g} 秒内没有响应") from None
    except (http.client.HTTPException, OSError) as exc:
        raise _QueryError("CONNECT_FAILED", f"连接 Elasticsearch 失败：{type(exc).__name__}: {exc}") from None
    return _es_result(status, content_type, raw, args["method"])


# ---------------------------------------------------------------- 结果转换与上限

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _json_len(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str))


def _clip(text: str) -> str:
    return text if len(text) <= MAX_VALUE else text[:MAX_VALUE] + TRUNCATED


def _bytes_text(raw: bytes) -> str:
    """UTF-8 文本按文本返回;不是文本(解不开、有控制字符)就是 0x 开头的十六进制。"""
    prefix = raw[:MAX_VALUE * 4 + 4]
    try:
        text = codecs.getincrementaldecoder("utf-8")().decode(prefix, final=len(prefix) == len(raw))
    except UnicodeDecodeError:
        text = None
    if text is not None and not _CONTROL.search(text):
        return _clip(text)
    hexed = "0x" + raw[:MAX_VALUE // 2].hex()
    return hexed[:MAX_VALUE] + TRUNCATED if 2 + 2 * len(raw) > MAX_VALUE else hexed


def _duration(value: datetime.timedelta) -> str:
    """MySQL TIME(PyMySQL 返回 timedelta)写成 [-]HH:MM:SS[.ffffff]。"""
    micros = (value.days * 86400 + value.seconds) * 1_000_000 + value.microseconds
    sign, micros = ("-" if micros < 0 else ""), abs(micros)
    seconds, micros = divmod(micros, 1_000_000)
    hours, rest = divmod(seconds, 3600)
    text = f"{sign}{hours:02d}:{rest // 60:02d}:{rest % 60:02d}"
    return f"{text}.{micros:06d}" if micros else text


def _plain(value: Any, depth: int = 0) -> Any:
    """转成 JSON 能表示的值:Decimal → 字符串、日期时间 → ISO、bytes → 文本或十六进制;
    单个值最多 MAX_VALUE 字符;字典里键名像密码的(同 config._SECRET_KEY)值换成 [REDACTED]。"""
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)   # inf / nan 不是合法 JSON
    if isinstance(value, str):
        return _clip(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return _bytes_text(bytes(value))
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, datetime.timedelta):
        return _duration(value)
    if depth >= 64 and isinstance(value, (dict, list, tuple, set, frozenset)):
        return _clip(json.dumps(value, ensure_ascii=False, default=str))   # 太深的结构整体当文本
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            name = key if isinstance(key, str) else str(_plain(key, depth + 1))
            result[_clip(name)] = REDACTED if item is not None and _SECRET_KEY.match(name) else _plain(item, depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_plain(item, depth + 1) for item in value]
    if isinstance(value, (set, frozenset)):
        items = [_plain(item, depth + 1) for item in value]
        try:
            items.sort()
        except TypeError:
            pass
        return items
    return _clip(str(value))


def _first(value: Any, count: int) -> tuple[Any, bool]:
    """列表 / 字典只留前 count 项。"""
    if isinstance(value, list):
        return value[:count], len(value) > count
    if isinstance(value, dict):
        return dict(list(value.items())[:count]), len(value) > count
    return value, False


def _fit(value: Any, budget: int) -> tuple[Any, bool]:
    """序列化后超过 budget 字符时:集合按顺序放到放不下为止,其他值转成截断的文本。"""
    if _json_len(value) <= budget:
        return value, False
    if isinstance(value, (list, dict)):
        items = list(value.items()) if isinstance(value, dict) else value
        kept, used = [], 2
        for item in items:
            used += _json_len(item) + 2
            if used > budget:
                break
            kept.append(item)
        return (dict(kept) if isinstance(value, dict) else kept), True
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text[:budget] + TRUNCATED, True


# ---------------------------------------------------------------- 对外接口

_NORMALIZERS = {"mysql": _mysql_args, "redis": _redis_args, "elasticsearch": _es_args}
_RUNNERS = {"mysql": _mysql_query, "redis": _redis_query, "elasticsearch": _es_query}


def _normalize(operation: str, arguments: dict) -> dict:
    if operation not in _NORMALIZERS:
        raise _Invalid(f"不支持的数据查询类型 {str(operation)[:40]}（只支持 mysql、redis、elasticsearch）")
    if not isinstance(arguments, dict):
        raise _Invalid("arguments 必须是对象")
    return _NORMALIZERS[operation](arguments)


def _secret_values(arguments: Any) -> list[str]:
    """要从错误信息里去掉的值:password、sentinel_password、api_key(含 URL 编码形式)和 Basic 认证头。"""
    if not isinstance(arguments, dict):
        return []
    values = set()
    for key in ("password", "sentinel_password", "api_key"):
        value = arguments.get(key)
        if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value):
            values.update((str(value), quote(str(value), safe="")))
    user, password = arguments.get("username"), arguments.get("password")
    if isinstance(user, str) and user and isinstance(password, (str, int)) and not isinstance(password, bool):
        values.add(base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii"))
    return sorted((value for value in values if value), key=len, reverse=True)


def _failure(code: str, message: str, secrets: list[str]) -> dict:
    for secret in secrets:
        message = message.replace(secret, REDACTED)
    return {"success": False, "error": {"code": code, "message": message[:1000]}}


def check_data_query(operation: str, arguments: dict) -> str | None:
    """参数和只读检查:不合格返回给模型看的原因,合格返回 None。"""
    try:
        _normalize(operation, arguments)
    except _Invalid as exc:
        return str(exc)
    except Exception:   # 检查本身出错按不合格处理,不能放行
        return "参数检查失败，请核对参数格式"
    return None


async def run_data_query(operation: str, arguments: dict, timeout: float = 60) -> dict:
    """执行;返回 {"success": True, "data": {...}, "truncated": bool} 或 {"success": False, "error": {"code", "message"}}。

    data:mysql 为 {columns, rows, row_count, truncated, elapsed_ms};redis 为 {result, truncated, elapsed_ms},
    用了哨兵再加 master,集群跳转过再加 redirected_to(都是 "host:port");
    elasticsearch 为 {status, body, truncated}(HTTP 有响应就算成功,4xx / 5xx 看 status)。
    timeout 是整体上限(秒),和参数里的 timeout(+5 秒连接余量)取小的。
    """
    secrets = _secret_values(arguments)
    try:
        args = _normalize(operation, arguments)
    except _Invalid as exc:
        return _failure("INVALID_ARGUMENT", str(exc), secrets)
    except Exception:
        return _failure("INVALID_ARGUMENT", "参数检查失败，请核对参数格式", secrets)
    try:
        limit = float(timeout)
    except (TypeError, ValueError):
        limit = 60.0
    limit = max(0.5, min(limit if math.isfinite(limit) else 60.0, args["timeout"] + 5))
    try:
        data = await asyncio.wait_for(asyncio.to_thread(_RUNNERS[operation], args), limit)
    except _QueryError as exc:
        return _failure(exc.code, str(exc), secrets)
    except TimeoutError:
        return _failure("TIMEOUT", f"查询 {limit:g} 秒内没有完成，已停止等待（可以缩小查询范围后重试）", secrets)
    except Exception as exc:   # 驱动里没想到的异常:只给类型和去掉密码后的消息
        return _failure("QUERY_FAILED", f"{type(exc).__name__}: {exc}", secrets)
    return {"success": True, "data": data, "truncated": bool(data.get("truncated"))}
