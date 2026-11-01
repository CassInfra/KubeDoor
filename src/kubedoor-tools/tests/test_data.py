"""kubedoor_tools.data:MySQL / Redis / Elasticsearch 只读查询的检查与执行。

不依赖真实服务:MySQL、Redis 用假客户端(替换 data._load_pymysql / data._load_redis),ES 用本地 http.server。
"""

import base64
import datetime
import decimal
import json
import socket
import ssl
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from kubedoor_tools import DATA_OPERATIONS, KubernetesExecutor, check_data_query, classify_operation, run_data_query
from kubedoor_tools import data
from kubedoor_tools import executor as executor_module

BS = chr(92)   # 反斜杠(直接写在字符串里容易被转义吃掉)
PASSWORD = "Pa55-w0rd!s3cret"
SENTINEL_PASSWORD = "S3ntinel-Pa55!x"
SENTINEL = "sentinel.app.svc:26379"


def mysql(sql, **extra):
    return {"host": "db.app.svc", "user": "app", "password": PASSWORD, "sql": sql, **extra}


def redis(*command, **extra):
    return {"host": "redis.app.svc", "password": PASSWORD, "command": list(command), **extra}


def sentinel(*command, **extra):
    """经哨兵查询:host 是哨兵(端口默认 26379),username / password 是主节点的"""
    return {"host": "sentinel.app.svc", "sentinel_master": "mymaster", "username": "app", "password": PASSWORD,
            "command": list(command), **extra}


def es(path, **extra):
    return {"url": "http://es.app.svc:9200", "path": path, **extra}


def test_data_operations_are_exported():
    assert DATA_OPERATIONS == ("mysql", "redis", "elasticsearch")


# ---------------------------------------------------------------- MySQL 只读检查

@pytest.mark.parametrize("sql", [
    "SELECT * FROM orders WHERE note = ';' AND memo = '-- x' AND tag = \"#\" AND status = 'UPDATE'",
    "select id from orders limit 10;",
    "  SELECT 1 ;  ",
    "SHOW TABLES", "SHOW CREATE TABLE orders", "DESC orders", "DESCRIBE orders", "EXPLAIN SELECT * FROM orders",
    "EXPLAIN FORMAT=JSON SELECT 1", "WITH recent AS (SELECT * FROM orders) SELECT * FROM recent", "TABLE orders",
    "SELECT REPLACE(name, 'a', 'b'), INSERT('abc', 1, 1, 'x') FROM users",   # 字符串函数,不是语句
    "SELECT t.update, t.delete, `insert`, x$update FROM t",                  # 限定名、反引号里、带 $ 的是列名
    "SELECT update_time, deleted, inserted_at FROM t",
    "(SELECT 1) UNION ALL (SELECT 2)",
    "SELECT 'it''s', \"say \"\"hi\"\"\", `a``b`",                           # 两个引号是转义
    "SELECT '/* not a comment */', '--', '#', 'a;b' FROM `x--y`",
    "SELECT * FROM t WHERE a LIKE '%" + BS + "_%'",                          # 反斜杠不在引号前,两种理解一样
    "SELECT 'C:" + BS + BS + "'",                                            # 两个反斜杠:两种理解都闭合
    "SELECT * FROM performance_schema.events_statements_summary_by_digest WHERE digest_text LIKE '%DELETE%'",
])
def test_mysql_read_only_queries_pass(sql):
    assert check_data_query("mysql", mysql(sql)) is None


@pytest.mark.parametrize("sql,reason", [
    ("SELECT 1; SELECT 2", "一条语句"), ("SELECT 1;;", "一条语句"), ("SELECT ';'; DELETE FROM t", "一条语句"),
    ("SELECT 1 /* c */", "注释"), ("SELECT /*!50000 1 */", "注释"), ("SELECT /*+ BKA(t) */ 1", "注释"),
    ("SELECT 1 -- c", "注释"), ("SELECT 1 # c", "注释"),
    ("UPDATE t SET a = 1", "不能执行 UPDATE"), ("INSERT INTO t VALUES (1)", "不能执行 INSERT"),
    ("DELETE FROM t", "不能执行 DELETE"), ("REPLACE INTO t VALUES (1)", "不能执行 REPLACE"),
    ("SET @a = 1", "不能执行 SET"), ("CALL cleanup()", "不能执行 CALL"), ("DROP TABLE t", "不能执行 DROP"),
    ("LOAD DATA INFILE 'x' INTO TABLE t", "不能执行 LOAD"), ("'SELECT'", "只能执行"),
    ("SELECT * FROM t INTO OUTFILE '/tmp/x'", "INTO OUTFILE"), ("SELECT a INTO DUMPFILE '/tmp/x' FROM t", "INTO OUTFILE"),
    ("SELECT * FROM t FOR UPDATE", "加锁读"), ("SELECT * FROM t FOR SHARE", "加锁读"),
    ("SELECT * FROM t LOCK IN SHARE MODE", "加锁读"),
    ("SELECT SLEEP(10)", "SLEEP"), ("SELECT benchmark (1000000, md5('x'))", "BENCHMARK"),
    ("SELECT GET_LOCK('a', 10)", "GET_LOCK"), ("SELECT RELEASE_LOCK('a')", "RELEASE_LOCK"),
    ("SELECT LOAD_FILE('/etc/passwd')", "LOAD_FILE"), ("SELECT * FROM t WHERE id = (SELECT SLEEP(5))", "SLEEP"),
    ("WITH x AS (SELECT 1) UPDATE t SET a = 1", "不能 UPDATE"), ("WITH x AS (SELECT 1) DELETE FROM t", "不能 DELETE"),
    ("WITH x AS (SELECT 1) INSERT INTO t SELECT * FROM x", "不能 INSERT"),
    ("EXPLAIN DELETE FROM t", "不能 DELETE"), ("DESC UPDATE t SET a = 1", "不能 UPDATE"),
    ("SELECT 'abc", "引号没有闭合"), ("SELECT `abc", "引号没有闭合"),
    # 服务器开了 NO_BACKSLASH_ESCAPES 时 \' 不是转义:后半段会变成注释和第二条语句,两种理解都要通过
    ("SELECT 'a" + BS + "', 1; DROP TABLE t; -- '", "注释"),
    ("SELECT 'O" + BS + "'Brien'", "引号没有闭合"),
    ("SELECT '" + chr(0) + "'", "NUL"),
    ("", "缺少 sql"), ("   ", "缺少 sql"),
])
def test_mysql_rejects_writes_and_ambiguous_sql(sql, reason):
    problem = check_data_query("mysql", mysql(sql))
    assert problem and reason in problem
    assert PASSWORD not in problem


def test_mysql_sql_length_limit():
    assert "20000" in check_data_query("mysql", mysql("SELECT " + "1," * 10000 + "1"))


@pytest.mark.parametrize("arguments,reason", [
    ({"user": "u", "sql": "SELECT 1"}, "缺少 host"),
    ({"host": "db:3306", "user": "u", "sql": "SELECT 1"}, "port"),
    ({"host": "mysql://db", "user": "u", "sql": "SELECT 1"}, "主机名"),
    ({"host": "db", "sql": "SELECT 1"}, "缺少 user"),
    ({"host": "db", "user": "u", "sql": "SELECT 1", "max_rows": 1001}, "max_rows"),
    ({"host": "db", "user": "u", "sql": "SELECT 1", "max_rows": 0}, "max_rows"),
    ({"host": "db", "user": "u", "sql": "SELECT 1", "timeout": 61}, "timeout"),
    ({"host": "db", "user": "u", "sql": "SELECT 1", "port": True}, "port"),
    ({"host": "db", "user": "u", "sql": "SELECT 1", "ssl": "yes"}, "ssl"),
    ({"host": "db", "user": "u", "sql": "SELECT 1", "query": "SELECT 2"}, "不支持参数 query"),
    ({"host": "db", "user": "u", "sql": "SELECT 1", "password": {"value": PASSWORD}}, "password"),
])
def test_mysql_argument_validation(arguments, reason):
    problem = check_data_query("mysql", arguments)
    assert problem and reason in problem
    assert PASSWORD not in problem


def test_mysql_arguments_are_normalized():
    args = data._normalize("mysql", {"host": "[::1]", "user": 1001, "password": 123456, "sql": " SELECT ';' ; ",
                                     "port": "3307", "max_rows": "10", "timeout": "2.5"})
    assert args["host"] == "::1" and args["user"] == "1001" and args["password"] == "123456"
    assert args["port"] == 3307 and args["max_rows"] == 10 and args["timeout"] == 2.5
    assert args["sql"] == "SELECT ';'"   # 末尾的分号去掉,引号里的不动
    assert args["database"] is None and args["ssl"] is False


def test_unknown_operation_and_non_object_arguments():
    assert "不支持的数据查询类型" in check_data_query("mongodb", {})
    assert "arguments 必须是对象" in check_data_query("mysql", "SELECT 1")


# ---------------------------------------------------------------- Redis 只读检查

@pytest.mark.parametrize("command", [
    ["GET", "user:1"], ["get", "user:1"], ["MGET", "a", "b"], ["HGETALL", "session:1"], ["HMGET", "h", "a", "b"],
    ["SCAN", "0", "MATCH", "user:*", "COUNT", "1000"], ["HSCAN", "h", "0", "COUNT", "100"],
    ["SSCAN", "s", "0", "MATCH", "COUNT", "COUNT", "10"],   # 匹配模式恰好是 COUNT
    ["HSCAN", "h", "0", "NOVALUES", "COUNT", "5"], ["SCAN", 0, "COUNT", 100],   # 数字也可以
    ["ZRANGE", "z", "0", "-1", "WITHSCORES"], ["XRANGE", "s", "-", "+", "COUNT", "10"], ["XINFO", "STREAM", "s"],
    ["CONFIG", "GET", "maxmemory"], ["config", "get", "maxmemory-policy", "timeout"], ["CLIENT", "LIST"],
    ["memory", "usage", "k", "SAMPLES", "5"], ["MEMORY", "STATS"], ["JSON.GET", "doc", "$"], ["INFO", "memory"],
    ["CLUSTER", "NODES"], ["SLOWLOG", "GET", "10"], ["LATENCY", "DOCTOR"], ["OBJECT", "ENCODING", "k"],
    ["GEORADIUS_RO", "g", "15", "37", "200", "km"], ["SORT_RO", "l", "ALPHA"], ["PING"], ["DBSIZE"],
])
def test_redis_read_only_commands_pass(command):
    assert check_data_query("redis", redis(*command)) is None


@pytest.mark.parametrize("command,reason", [
    (["KEYS", "*"], "SCAN"), (["keys", "user:*"], "改用 SCAN"),
    (["SET", "k", "v"], "白名单"), (["DEL", "k"], "白名单"), (["FLUSHALL"], "白名单"), (["EVAL", "return 1", "0"], "白名单"),
    (["SORT", "k", "STORE", "x"], "白名单"), (["GEORADIUS", "g", "0", "0", "1", "km"], "白名单"), (["MONITOR"], "白名单"),
    (["AUTH", "x"], "白名单"), (["SELECT", "1"], "白名单"), (["GETDEL", "k"], "白名单"), (["GETEX", "k"], "白名单"),
    (["CONFIG", "SET", "maxmemory", "1"], "只读子命令"), (["CONFIG", "REWRITE"], "只读子命令"),
    (["CONFIG", "GET", "requirepass"], "密码"), (["CONFIG", "GET", "masterauth"], "密码"),
    (["CONFIG", "GET", "masteruser"], "密码"), (["CONFIG", "GET", "aclfile"], "ACL"),
    (["CONFIG", "GET", "tls-key-file-pass"], "密码"), (["CONFIG", "GET", "maxmemory", "requirepass"], "密码"),
    (["CONFIG", "GET", "*"], "通配符"), (["CONFIG", "GET", "max*"], "通配符"), (["CONFIG", "GET", "maxmemor?"], "通配符"),
    (["CONFIG", "GET"], "参数名"),
    (["CLIENT", "KILL", "ID", "1"], "只读子命令"), (["CLIENT"], "只读子命令"), (["CLUSTER", "RESET"], "只读子命令"),
    (["XINFO", "HELP"], "只读子命令"), (["MEMORY", "PURGE"], "只读子命令"), (["SLOWLOG", "RESET"], "只读子命令"),
    (["SCAN", "0", "COUNT", "5000"], "COUNT"), (["HSCAN", "h", "0", "COUNT", "1001"], "COUNT"),
    (["SCAN", "0", "COUNT", "x"], "COUNT"), (["ZSCAN", "z", "0", "COUNT"], "COUNT"), (["SCAN", "0", "COUNT", "0"], "COUNT"),
    (["MEMORY", "USAGE", "k", "SAMPLES", "0"], "SAMPLES"),
])
def test_redis_rejects_writes_and_heavy_commands(command, reason):
    problem = check_data_query("redis", redis(*command))
    assert problem and reason in problem
    assert PASSWORD not in problem


@pytest.mark.parametrize("arguments,reason", [
    (redis(), "command"), ({"host": "r", "command": "GET k"}, "command"), ({"host": "r", "command": [{"k": 1}]}, "字符串"),
    ({"host": "r", "command": ["GET", None]}, "字符串"), ({"host": "r", "command": [""]}, "命令名"),
    (redis("GET", "k", db=-1), "db"), (redis("GET", "k", max_items=1001), "max_items"), (redis("GET", "k", tls="true"), "tls"),
    (redis("GET", "k", database=1), "不支持参数 database"),
    (redis("GET", "k", sentinel_master=""), "不能为空"), (redis("GET", "k", sentinel_master="   "), "不能为空"),
    (redis("GET", "k", sentinel_master="m" * 201), "200"),
    (redis("GET", "k", sentinel_master="my" + chr(10) + "master"), "控制字符"),
    (redis("GET", "k", sentinel_master="my" + chr(0)), "控制字符"),
    (redis("GET", "k", sentinel_master=["mymaster"]), "字符串"), (redis("GET", "k", sentinel_master=True), "字符串"),
    (redis("GET", "k", sentinel_password=SENTINEL_PASSWORD), "sentinel_master"),
    (redis("GET", "k", sentinel_username="sentinel-user"), "sentinel_master"),
    (sentinel("GET", "k", sentinel_username=1.5), "sentinel_username"),
    (sentinel("GET", "k", sentinel_password="x" * 4097), "sentinel_password"),
    (sentinel("GET", "k", sentinel_password={"v": 1}), "sentinel_password"),
])
def test_redis_argument_validation(arguments, reason):
    problem = check_data_query("redis", arguments)
    assert problem and reason in problem
    assert PASSWORD not in problem and SENTINEL_PASSWORD not in problem


def test_redis_sentinel_arguments():
    assert check_data_query("redis", sentinel("GET", "k")) is None
    assert check_data_query("redis", sentinel("HGETALL", "h", port=26380, sentinel_username="sentinel-user",
                                              sentinel_password=SENTINEL_PASSWORD, tls=True, db=2)) is None
    # null / 空串等于没给:不用哨兵
    assert check_data_query("redis", redis("GET", "k", sentinel_master=None, sentinel_password="")) is None
    args = data._normalize("redis", sentinel("GET", "k", sentinel_master="  mymaster "))
    assert args["sentinel_master"] == "mymaster" and args["port"] == 26379   # 哨兵的默认端口
    assert data._normalize("redis", redis("GET", "k"))["port"] == 6379
    assert data._normalize("redis", sentinel("GET", "k", sentinel_master=7))["sentinel_master"] == "7"
    assert "sentinel_master" in check_data_query("redis", redis("GET", "k", unknown=1))   # 列出可用参数
    # 哨兵模式下命令照样做只读检查
    assert "白名单" in check_data_query("redis", sentinel("SET", "k", "v"))


# ---------------------------------------------------------------- Elasticsearch 只读检查

@pytest.mark.parametrize("method,path,extra", [
    ("GET", "/", {}), ("HEAD", "/", {}), ("get", "/_cluster/health", {}), ("GET", "/_cluster/health/logs-*", {}),
    ("GET", "/_cluster/stats", {}), ("GET", "/_cat/indices", {"params": {"format": "json", "v": True, "bytes": "mb"}}),
    ("GET", "/_cat/indices/", {}), ("GET", "/_nodes/stats/jvm", {}), ("GET", "/_alias/current", {}), ("GET", "/_aliases", {}),
    ("GET", "/logs-*,-logs-old/_search", {"params": {"q": "level:error", "size": 10}}), ("GET", "/_search", {}),
    ("POST", "/logs-*/_search", {"body": {"query": {"match_all": {}}, "size": 5}}), ("POST", "/_search", {"body": {}}),
    ("GET", "/orders/_doc/1", {}), ("GET", "/orders/_source/1", {}), ("HEAD", "/orders", {}), ("GET", "/orders", {}),
    ("GET", "/orders/_mapping", {}), ("GET", "/_mapping/field/name", {}), ("GET", "/orders/_settings/index.number_of_shards", {}),
    ("GET", "/orders/_stats/docs", {}), ("GET", "/orders/_count", {}),
    ("POST", "/orders/_count", {"body": {"query": {"term": {"a": 1}}}}), ("POST", "/orders/_mget", {"body": {"ids": ["1"]}}),
    ("POST", "/orders/_msearch", {"body": [{}, {"query": {"match_all": {}}}]}),
    ("POST", "/orders/_explain/1", {"body": {"query": {"match_all": {}}}}), ("POST", "/orders/_validate/query", {"body": {}}),
    ("POST", "/orders/_field_caps", {"params": {"fields": "*"}}),
    ("POST", "/_sql", {"body": {"query": "SELECT * FROM orders LIMIT 5"}, "params": {"format": "txt"}}),
    ("POST", "/_sql", {"body": {"cursor": "abc"}}), ("POST", "/_sql/translate", {"body": {"query": "SELECT 1"}}),
    ("POST", "/_plugins/_sql", {"body": {"query": "SHOW TABLES LIKE %"}}), ("POST", "/_opendistro/_sql", {"body": {"query": "DESCRIBE TABLES"}}),
    ("GET", "/_tasks", {"params": {"actions": "*search*", "detailed": True}}), ("GET", "/_resolve/index/logs-*", {}),
])
def test_es_read_only_requests_pass(method, path, extra):
    assert check_data_query("elasticsearch", es(path, method=method, **extra)) is None


@pytest.mark.parametrize("arguments,reason", [
    (es("/orders/_doc/1", method="PUT"), "method"), (es("/orders", method="DELETE"), "method"),
    (es("/orders/_doc/1", method="PATCH"), "method"),
    (es("/_bulk", method="POST", body={}), "白名单"), (es("/orders/_update/1", method="POST", body={}), "白名单"),
    (es("/orders/_doc", method="POST", body={}), "白名单"), (es("/orders/_delete_by_query", method="POST", body={}), "白名单"),
    (es("/orders/_update_by_query", method="POST", body={}), "白名单"), (es("/_reindex", method="POST", body={}), "白名单"),
    (es("/orders/_close", method="POST"), "白名单"), (es("/_tasks/_cancel", method="POST"), "白名单"),
    (es("/_cluster/settings"), "白名单"), (es("/_snapshot"), "白名单"), (es("/_security/user"), "白名单"),
    (es("/_search/scroll", method="POST", body={"scroll_id": "x"}), "scroll"), (es("/_search/scroll"), "scroll"),
    (es("/orders/_pit", method="POST"), "PIT"), (es("/orders/_search", params={"scroll": "1m"}), "scroll"),
    (es("/orders/_search", body={"size": 1}), "POST"),       # GET 带 body
    (es("/_sql"), "要用 POST"), (es("/", method="POST"), "要用 GET"),
    (es("/_sql", method="POST", body={"query": "DELETE FROM orders"}), "SQL"),
    (es("/_plugins/_sql", method="POST", body={"query": "SELECT 1; DELETE FROM orders"}), "SQL"),
    (es("/_sql", method="POST", body={"sql": "SELECT 1"}), "query"),
    (es("/orders/../_bulk"), "path"), (es("/orders//_search"), "path"), (es("/orders%2f_bulk"), "path"),
    (es("/orders%2e%2e"), "path"), (es("/_cat/indices?v"), "params"), (es("/_cat indices"), "path"),
    (es("_cat/indices"), "/ 开头"),
    ({"url": "http://elastic:pw@es:9200", "path": "/"}, "账号密码"), ({"url": "http://es:9200/?x=1", "path": "/"}, "查询参数"),
    ({"url": "ftp://es:9200", "path": "/"}, "http(s)"), ({"url": "http://es:99999", "path": "/"}, "端口"),
    ({"url": "http://es:9200/../x", "path": "/"}, "路径前缀"), ({"path": "/"}, "缺少 url"),
    (es("/", api_key="k", username="elastic", password="p"), "只能用一种"), (es("/", password="p"), "username"),
    (es("/orders/_msearch", method="POST", body={"query": {}}), "_msearch"), (es("/", params={"a": {"b": 1}}), "params"),
    (es("/", params=["format=json"]), "params"), (es("/", tls_verify="false"), "tls_verify"),
    (es("/orders/_search", method="POST", body="query"), "JSON"), (es("/", index="orders"), "不支持参数 index"),
])
def test_es_rejects_writes_and_unsafe_requests(arguments, reason):
    problem = check_data_query("elasticsearch", arguments)
    assert problem and reason in problem


# ---------------------------------------------------------------- classify_operation

def test_classify_data_operations():
    ok = classify_operation("mysql", mysql("SELECT 1"))
    assert ok == {"read_only": True, "reason": ok["reason"], "ops": ["data.query"], "category": None, "dangerous": False}
    for operation, arguments in (("redis", redis("GET", "k")), ("elasticsearch", es("/_cat/indices"))):
        assert classify_operation(operation, arguments, {"view": ["ls"], "danger": ["rm"]})["read_only"] is True
    bad = classify_operation("mysql", mysql("DELETE FROM t"))
    assert bad == {"read_only": False, "reason": check_data_query("mysql", mysql("DELETE FROM t")), "ops": ["data.query"],
                   "category": None, "dangerous": False}
    assert classify_operation("redis", redis("FLUSHALL"))["read_only"] is False
    assert classify_operation("elasticsearch", None)["ops"] == ["data.query"]


# ---------------------------------------------------------------- MySQL 执行(假 PyMySQL)

class FakeMySQLError(Exception):
    pass


class FakeMySQL:
    """假的 pymysql:记录连接参数和执行的语句;fail 是 {语句前缀: 异常}。"""

    def __init__(self, columns=("id",), rows=(), fail=None, connect_error=None):
        self.columns, self.rows, self.fail, self.connect_error = list(columns), list(rows), fail or {}, connect_error
        self.connections = []
        self.MySQLError = FakeMySQLError
        self.cursors = SimpleNamespace(SSCursor=object())

    def connect(self, **options):
        if self.connect_error:
            raise self.connect_error
        connection = FakeConnection(self, options)
        self.connections.append(connection)
        return connection


class FakeConnection:
    def __init__(self, lib, options):
        self.lib, self.options = lib, options
        self.statements, self.events, self.fetch_sizes = [], [], []

    def cursor(self):
        return FakeCursor(self)

    def rollback(self):
        self.events.append("rollback")

    def close(self):
        self.events.append("close")


class FakeCursor:
    def __init__(self, connection):
        self.connection, self.description, self.rows = connection, None, iter(())

    def execute(self, sql, args=None):
        assert args is None   # 不能让 PyMySQL 按 % 格式化 SQL
        self.connection.statements.append(sql)
        for prefix, error in self.connection.lib.fail.items():
            if sql.startswith(prefix):
                raise error
        if not sql.startswith(("SET ", "START ")):
            self.description = [(name, 253, None, None, None, None, True) for name in self.connection.lib.columns]
            self.rows = iter(self.connection.lib.rows)
        return 0

    def fetchmany(self, size):
        self.connection.fetch_sizes.append(size)
        rows = []
        for row in self.rows:
            rows.append(tuple(row))
            if len(rows) == size:
                break
        return rows


@pytest.fixture
def fake_mysql(monkeypatch):
    def install(**options):
        fake = FakeMySQL(**options)
        monkeypatch.setattr(data, "_load_pymysql", lambda: fake)
        return fake
    return install


async def test_mysql_runs_in_read_only_transaction_and_rolls_back(fake_mysql):
    fake = fake_mysql(columns=["id", "name"], rows=[(1, "a"), (2, "b")])
    result = await run_data_query("mysql", mysql("SELECT id, name FROM users;", database="app", timeout=5, max_rows=10))
    assert result["success"] and result["truncated"] is False
    assert result["data"]["columns"] == ["id", "name"] and result["data"]["rows"] == [[1, "a"], [2, "b"]]
    assert result["data"]["row_count"] == 2 and result["data"]["truncated"] is False
    assert isinstance(result["data"]["elapsed_ms"], int)
    connection = fake.connections[0]
    assert connection.statements == ["SET SESSION TRANSACTION READ ONLY", "SET SESSION MAX_EXECUTION_TIME=5000",
                                     "START TRANSACTION READ ONLY", "SELECT id, name FROM users"]
    assert connection.events == ["rollback", "close"]
    assert connection.fetch_sizes == [11]
    options = connection.options
    assert options["connect_timeout"] == 5 and options["read_timeout"] == 5 and options["write_timeout"] == 5
    assert options["charset"] == "utf8mb4" and options["local_infile"] is False and options["client_flag"] == 0
    assert options["cursorclass"] is fake.cursors.SSCursor
    assert options["password"] == PASSWORD.encode() and options["database"] == "app" and options["port"] == 3306
    assert "ssl" not in options


async def test_mysql_ssl_encrypts_without_verifying_certificates(fake_mysql):
    fake = fake_mysql()
    assert (await run_data_query("mysql", mysql("SELECT 1", ssl=True)))["success"]
    context = fake.connections[0].options["ssl"]
    assert isinstance(context, ssl.SSLContext) and context.verify_mode == ssl.CERT_NONE and not context.check_hostname


async def test_mysql_truncates_at_max_rows_and_disconnects_without_draining(fake_mysql):
    fake = fake_mysql(columns=["n"], rows=[(i,) for i in range(50)])
    result = await run_data_query("mysql", mysql("SELECT n FROM t", max_rows=3))
    assert result["success"] and result["truncated"] is True and result["data"]["truncated"] is True
    assert result["data"]["rows"] == [[0], [1], [2]] and result["data"]["row_count"] == 3
    connection = fake.connections[0]
    assert connection.fetch_sizes == [4]
    # 没读完时回滚会先把剩下的行全部读完:直接断开,服务端自动回滚只读事务
    assert connection.events == ["close"]


async def test_mysql_exactly_max_rows_is_not_truncated(fake_mysql):
    fake_mysql(columns=["n"], rows=[(1,), (2,)])
    result = await run_data_query("mysql", mysql("SELECT n FROM t", max_rows=2))
    assert result["truncated"] is False and result["data"]["row_count"] == 2


async def test_mysql_converts_values_and_redacts_secret_columns(fake_mysql):
    columns = ["amount", "created", "day", "clock", "late", "text", "blob", "bit", "long", "big_blob", "empty",
               "password", "api_key", "access_token", "secret_note", "ratio"]
    row = (decimal.Decimal("12.50"), datetime.datetime(2024, 1, 2, 3, 4, 5), datetime.date(2024, 1, 2),
           datetime.timedelta(hours=26, minutes=3, seconds=4, microseconds=5), datetime.timedelta(seconds=-3661),
           "中文".encode(), b"\xff\x00", b"\x01", "x" * 1500, b"\xff" * 2000, None,
           PASSWORD, "key-123", "tok", "visible", 1.5)
    fake_mysql(columns=columns, rows=[row, (None,) * len(columns)])
    result = await run_data_query("mysql", mysql("SELECT * FROM t"))
    first, second = (dict(zip(columns, values)) for values in result["data"]["rows"])
    assert first["amount"] == "12.50" and first["created"] == "2024-01-02T03:04:05" and first["day"] == "2024-01-02"
    assert first["clock"] == "26:03:04.000005" and first["late"] == "-01:01:01"
    assert first["text"] == "中文" and first["blob"] == "0xff00" and first["bit"] == "0x01"
    assert first["long"] == "x" * 1000 + data.TRUNCATED
    assert first["big_blob"] == "0x" + "ff" * 499 + data.TRUNCATED and first["empty"] is None
    assert first["password"] == first["api_key"] == first["access_token"] == "[REDACTED]"
    assert first["secret_note"] == "visible" and first["ratio"] == 1.5
    assert second["password"] is None   # 空值没有可打码的
    assert PASSWORD not in json.dumps(result, ensure_ascii=False)


async def test_mysql_limits_total_output(fake_mysql):
    fake_mysql(columns=["a", "b"], rows=[("x" * 1000, "y" * 1000)] * 100)
    result = await run_data_query("mysql", mysql("SELECT a, b FROM t", max_rows=100))
    assert result["truncated"] is True and 0 < result["data"]["row_count"] < 100
    assert len(json.dumps(result["data"], ensure_ascii=False)) <= data.MAX_OUTPUT + 200


async def test_mysql_rejects_before_connecting(fake_mysql):
    fake = fake_mysql()
    result = await run_data_query("mysql", mysql("DELETE FROM t"))
    assert result == {"success": False, "error": {"code": "INVALID_ARGUMENT", "message": check_data_query("mysql", mysql("DELETE FROM t"))}}
    assert fake.connections == []


async def test_mysql_errors_never_include_the_password(fake_mysql):
    denied = FakeMySQLError(1045, f"Access denied for user 'app'@'10.0.0.1' (using password: YES) {PASSWORD}")
    fake_mysql(connect_error=denied)
    result = await run_data_query("mysql", mysql("SELECT 1"))
    error = result["error"]
    assert error["code"] == "CONNECT_FAILED" and "1045" in error["message"] and "[REDACTED]" in error["message"]
    assert PASSWORD not in json.dumps(result)

    fake = fake_mysql(fail={"SELECT": FakeMySQLError(1064, f"syntax error near '{PASSWORD}'")})
    result = await run_data_query("mysql", mysql("SELECT 1"))
    assert result["error"]["code"] == "QUERY_FAILED" and PASSWORD not in result["error"]["message"]
    assert fake.connections[0].events == ["close"]


async def test_mysql_read_only_session_is_required(fake_mysql):
    fake = fake_mysql(fail={"SET SESSION TRANSACTION": FakeMySQLError(1193, "Unknown system variable")})
    result = await run_data_query("mysql", mysql("SELECT 1"))
    assert result["error"]["code"] == "QUERY_FAILED" and "只读事务" in result["error"]["message"]
    assert fake.connections[0].statements == ["SET SESSION TRANSACTION READ ONLY"]
    assert fake.connections[0].events == ["close"]


async def test_mysql_execution_time_limit_falls_back_for_mariadb(fake_mysql):
    fake = fake_mysql(fail={"SET SESSION MAX_EXECUTION_TIME": FakeMySQLError(1193, "Unknown system variable")})
    assert (await run_data_query("mysql", mysql("SELECT 1", timeout=2.5)))["success"]
    assert fake.connections[0].statements[1:4] == ["SET SESSION MAX_EXECUTION_TIME=2500", "SET SESSION max_statement_time=2.5",
                                                  "START TRANSACTION READ ONLY"]


@pytest.mark.parametrize("error", [FakeMySQLError(3024, "maximum statement execution time exceeded"),
                                   FakeMySQLError(2013, "Lost connection to MySQL server during query (timed out)")])
async def test_mysql_query_timeouts(fake_mysql, error):
    fake_mysql(fail={"SELECT": error})
    assert (await run_data_query("mysql", mysql("SELECT 1")))["error"]["code"] == "TIMEOUT"


async def test_mysql_cryptography_hint(fake_mysql):
    fake_mysql(connect_error=RuntimeError("'cryptography' package is required for sha256_password or caching_sha2_password auth methods"))
    error = (await run_data_query("mysql", mysql("SELECT 1")))["error"]
    assert error["code"] == "CONNECT_FAILED" and "ssl" in error["message"]


async def test_missing_drivers_are_reported(monkeypatch):
    monkeypatch.setitem(sys.modules, "pymysql", None)
    monkeypatch.setitem(sys.modules, "redis", None)
    assert (await run_data_query("mysql", mysql("SELECT 1")))["error"]["code"] == "PROGRAM_UNAVAILABLE"
    assert (await run_data_query("redis", redis("GET", "k")))["error"]["code"] == "PROGRAM_UNAVAILABLE"


async def test_overall_timeout_does_not_wait_for_a_stuck_driver(monkeypatch):
    release = threading.Event()

    def stuck(args):
        release.wait(10)
        return {"truncated": False}

    monkeypatch.setitem(data._RUNNERS, "mysql", stuck)
    started = time.monotonic()
    try:
        result = await run_data_query("mysql", mysql("SELECT 1"), timeout=0.6)
    finally:
        release.set()
    assert result["error"]["code"] == "TIMEOUT" and time.monotonic() - started < 3


# ---------------------------------------------------------------- Redis 执行(假 redis-py)

class RedisError(Exception):
    pass


class ResponseError(RedisError):
    pass


class AskError(ResponseError):
    def __init__(self, resp):
        super().__init__(resp)
        slot, node = resp.split(" ")
        host, port = node.rsplit(":", 1)
        self.slot_id, self.host, self.port = int(slot), host, int(port)


class MovedError(AskError):
    pass


class RedisConnectionError(RedisError):
    pass


class AuthenticationError(RedisConnectionError):
    pass


class RedisTimeoutError(RedisError):
    pass


REDIS_EXCEPTIONS = SimpleNamespace(RedisError=RedisError, ResponseError=ResponseError, AskError=AskError,
                                   MovedError=MovedError, ConnectionError=RedisConnectionError,
                                   AuthenticationError=AuthenticationError, TimeoutError=RedisTimeoutError)


class FakeRedisClient:
    def __init__(self, replies, options):
        self.replies, self.options, self.commands, self.closed = replies, options, [], False

    def execute_command(self, *args):
        self.commands.append(list(args))
        reply = self.replies[args[0].upper()]
        if isinstance(reply, Exception):
            raise reply
        return reply(list(args)) if callable(reply) else reply

    def close(self):
        self.closed = True


@pytest.fixture
def fake_redis(monkeypatch):
    clients = []

    def install(replies, nodes=None):
        """replies:所有节点共用的回复;nodes:按 "host:port" 给某些节点单独的回复(集群跳转、哨兵)"""
        def client(**options):
            node = (nodes or {}).get(f"{options['host']}:{options['port']}", replies)
            clients.append(FakeRedisClient(node, options))
            return clients[-1]

        lib = SimpleNamespace(Redis=client, Retry=lambda backoff, retries: ("retry", backoff, retries),
                              NoBackoff=lambda: "no-backoff", exceptions=REDIS_EXCEPTIONS)
        monkeypatch.setattr(data, "_load_redis", lambda: lib)
        return clients
    return install


async def test_redis_get_with_safe_connection_options(fake_redis):
    clients = fake_redis({"GET": "value"})
    result = await run_data_query("redis", redis("get", "user:1", db=2, username="app"))
    assert result["success"] and result["data"]["result"] == "value" and result["truncated"] is False
    assert "redirected_to" not in result["data"] and "master" not in result["data"]
    client = clients[0]
    assert client.commands == [["get", "user:1"]] and client.closed
    options = client.options
    assert options["protocol"] == 2 and options["retry"] == ("retry", "no-backoff", 0)
    assert options["lib_name"] is None and options["lib_version"] is None and options["health_check_interval"] == 0
    assert options["decode_responses"] is True and options["encoding_errors"] == "replace"
    # 每次连接的超时是总时限里剩下的时间(第一次连接时几乎是完整的 timeout)
    assert 9.5 < options["socket_timeout"] <= 10 and 9.5 < options["socket_connect_timeout"] <= 10
    assert options["password"] == PASSWORD and options["username"] == "app" and options["db"] == 2
    assert "ssl" not in options
    fake_redis({"GET": None})
    await run_data_query("redis", redis("GET", "k", tls=True))
    assert clients[-1].options["ssl"] is True and clients[-1].options["ssl_cert_reqs"] == "none"


async def test_redis_new_clients_disable_setinfo_with_driver_info(monkeypatch):
    seen = []

    class Client:   # 新版 redis-py 的构造函数有 driver_info 参数
        def __init__(self, driver_info="default", **options):
            seen.append((driver_info, options))

        def execute_command(self, *args):
            return "v"

        def close(self):
            pass

    lib = SimpleNamespace(Redis=Client, Retry=lambda backoff, retries: None, NoBackoff=lambda: None, exceptions=REDIS_EXCEPTIONS)
    monkeypatch.setattr(data, "_load_redis", lambda: lib)
    assert (await run_data_query("redis", redis("GET", "k")))["success"]
    assert seen[0][0] is None and "lib_name" not in seen[0][1] and "lib_version" not in seen[0][1]


async def test_redis_scan_keeps_cursor_and_truncates_items(fake_redis):
    fake_redis({"SCAN": (123456789012345678901, [f"k{i}" for i in range(150)])})
    result = await run_data_query("redis", redis("SCAN", "0", "MATCH", "k*", "COUNT", "1000", max_items=100))
    cursor, keys = result["data"]["result"]
    assert cursor == "123456789012345678901" and keys == [f"k{i}" for i in range(100)]   # 游标转成字符串,不丢精度
    assert result["truncated"] is True and result["data"]["truncated"] is True


async def test_redis_hgetall_redacts_secret_fields(fake_redis):
    clients = fake_redis({"HLEN": 4, "HGETALL": {"name": "alice", "password": PASSWORD, "api_key": "k", "note": "y" * 1500}})
    result = await run_data_query("redis", redis("HGETALL", "session:1"))
    value = result["data"]["result"]
    assert value["name"] == "alice" and value["password"] == value["api_key"] == "[REDACTED]"
    assert value["note"] == "y" * 1000 + data.TRUNCATED
    assert clients[0].commands == [["HLEN", "session:1"], ["HGETALL", "session:1"]]
    assert PASSWORD not in json.dumps(result)


async def test_redis_hget_and_hmget_redact_requested_secret_fields(fake_redis):
    fake_redis({"HMGET": ["alice", PASSWORD, None], "HGET": "tok-123"})
    result = await run_data_query("redis", redis("HMGET", "user:1", "name", "password", "token"))
    assert result["data"]["result"] == ["alice", "[REDACTED]", None]
    assert (await run_data_query("redis", redis("HGET", "user:1", "access_token")))["data"]["result"] == "[REDACTED]"


async def test_redis_json_get_is_parsed_and_redacted(fake_redis):
    fake_redis({"JSON.GET": json.dumps({"user": "a", "password": PASSWORD, "items": [1, 2]})})
    result = await run_data_query("redis", redis("JSON.GET", "doc"))
    assert result["data"]["result"] == {"user": "a", "password": "[REDACTED]", "items": [1, 2]}


async def test_redis_refuses_whole_collection_of_huge_keys(fake_redis):
    clients = fake_redis({"HLEN": 50_000, "SCARD": 20_000})
    result = await run_data_query("redis", redis("HGETALL", "big"))
    assert result["error"]["code"] == "TOO_LARGE" and "HSCAN" in result["error"]["message"]
    assert clients[0].commands == [["HLEN", "big"]] and clients[0].closed
    assert "SSCAN" in (await run_data_query("redis", redis("SMEMBERS", "big")))["error"]["message"]


class Lengths:
    """_redis_bound 用到的长度查询"""

    def __init__(self, **lengths):
        self.lengths, self.commands = lengths, []

    def execute_command(self, *args):
        self.commands.append(args[0])
        return self.lengths[args[0]]


@pytest.mark.parametrize("command,lengths,expected", [
    (["LRANGE", "l", "0", "-1"], {"LLEN": 5000}, ["LRANGE", "l", "0", "100"]),
    (["LRANGE", "l", "0", "-1"], {"LLEN": 5}, ["LRANGE", "l", "0", "-1"]),
    (["LRANGE", "l", "10", "20"], {}, ["LRANGE", "l", "10", "20"]),
    (["LRANGE", "l", "-200", "-1"], {"LLEN": 1000}, ["LRANGE", "l", "800", "900"]),
    (["ZRANGE", "z", "0", "-1", "WITHSCORES"], {"ZCARD": 500}, ["ZRANGE", "z", "0", "100", "WITHSCORES"]),
    (["ZRANGE", "z", "0", "+inf", "BYSCORE"], {}, ["ZRANGE", "z", "0", "+inf", "BYSCORE", "LIMIT", "0", "101"]),
    (["ZRANGEBYSCORE", "z", "-inf", "+inf", "WITHSCORES", "LIMIT", "0", "5000"], {},
     ["ZRANGEBYSCORE", "z", "-inf", "+inf", "WITHSCORES", "LIMIT", "0", "101"]),
    (["ZREVRANGEBYLEX", "z", "+", "-"], {}, ["ZREVRANGEBYLEX", "z", "+", "-", "LIMIT", "0", "101"]),
    (["XRANGE", "s", "-", "+"], {}, ["XRANGE", "s", "-", "+", "COUNT", "101"]),
    (["XREVRANGE", "s", "+", "-", "COUNT", "10"], {}, ["XREVRANGE", "s", "+", "-", "COUNT", "10"]),
    (["SRANDMEMBER", "s", "-100000000"], {}, ["SRANDMEMBER", "s", "-101"]),
    (["HRANDFIELD", "h", "5000", "WITHVALUES"], {}, ["HRANDFIELD", "h", "101", "WITHVALUES"]),
    (["LPOS", "l", "x", "COUNT", "0"], {}, ["LPOS", "l", "x", "COUNT", "101"]),
    (["LPOS", "l", "x"], {}, ["LPOS", "l", "x"]),
    (["SORT_RO", "l", "BY", "LIMIT", "GET", "o_*"], {}, ["SORT_RO", "l", "BY", "LIMIT", "GET", "o_*", "LIMIT", "0", "101"]),
    (["GEOSEARCH", "g", "FROMLONLAT", "15", "37", "BYRADIUS", "200", "km", "ASC"], {},
     ["GEOSEARCH", "g", "FROMLONLAT", "15", "37", "BYRADIUS", "200", "km", "ASC", "COUNT", "101"]),
    (["XPENDING", "s", "g", "IDLE", "1000", "-", "+", "5000"], {}, ["XPENDING", "s", "g", "IDLE", "1000", "-", "+", "101"]),
    (["GET", "k"], {}, ["GET", "k"]),
])
def test_redis_collections_are_bounded_on_the_server(command, lengths, expected):
    client = Lengths(**lengths)
    assert data._redis_bound(client, command, 100) == expected
    assert client.commands == list(lengths)


async def test_redis_list_results_are_cut_to_max_items_and_total_size(fake_redis):
    fake_redis({"LRANGE": [f"v{i}" for i in range(101)], "LLEN": 1000, "MGET": ["v" * 900] * 100})
    result = await run_data_query("redis", redis("LRANGE", "l", "0", "-1"))
    assert len(result["data"]["result"]) == 100 and result["truncated"] is True
    result = await run_data_query("redis", redis("MGET", *[f"k{i}" for i in range(100)]))
    assert result["truncated"] is True and 0 < len(result["data"]["result"]) < 100
    assert len(json.dumps(result["data"], ensure_ascii=False)) <= data.MAX_OUTPUT + 200


async def test_redis_report_text_is_not_cut_at_single_value_limit(fake_redis):
    fake_redis({"LATENCY": "line\n" * 1000})
    result = await run_data_query("redis", redis("LATENCY", "DOCTOR"))
    assert result["data"]["result"] == "line\n" * 1000 and result["truncated"] is False


async def test_redis_values_are_converted(fake_redis):
    fake_redis({"TIME": (1700000000, 12), "ZSCORE": float("inf"), "SMEMBERS": {"b", "a"}, "SCARD": 2,
                "LASTSAVE": datetime.datetime(2024, 1, 2, 3, 4, 5)})
    assert (await run_data_query("redis", redis("TIME")))["data"]["result"] == [1700000000, 12]
    assert (await run_data_query("redis", redis("ZSCORE", "z", "m")))["data"]["result"] == "inf"
    assert (await run_data_query("redis", redis("SMEMBERS", "s")))["data"]["result"] == ["a", "b"]
    assert (await run_data_query("redis", redis("LASTSAVE")))["data"]["result"] == "2024-01-02T03:04:05"


def nodes_of(clients):
    return [f"{client.options['host']}:{client.options['port']}" for client in clients]


async def test_redis_follows_moved_with_the_same_credentials(fake_redis):
    clients = fake_redis({"GET": MovedError("3999 10.0.0.5:6381")}, nodes={"10.0.0.5:6381": {"GET": "v"}})
    result = await run_data_query("redis", redis("GET", "user:1", username="app", tls=True, timeout=8))
    assert result["success"], result
    assert result["data"]["result"] == "v" and result["data"]["redirected_to"] == "10.0.0.5:6381"
    assert "master" not in result["data"]
    assert nodes_of(clients) == ["redis.app.svc:6379", "10.0.0.5:6381"] and all(client.closed for client in clients)
    assert clients[1].commands == [["GET", "user:1"]]   # 重发同一条命令;MOVED 不发 ASKING
    first, target = clients[0].options, clients[1].options
    for key in ("username", "password", "db", "ssl", "ssl_cert_reqs", "protocol", "retry"):
        assert target[key] == first[key]
    assert target["password"] == PASSWORD and target["ssl"] is True and target["socket_timeout"] <= 8


async def test_redis_ask_sends_asking_before_each_command(fake_redis):
    clients = fake_redis({"HLEN": AskError("12 10.0.0.6:6380")}, nodes={
        "10.0.0.6:6380": {"ASKING": "OK", "HLEN": 2, "HGETALL": {"a": "1", "b": "2"}}})
    result = await run_data_query("redis", redis("HGETALL", "h"))
    assert result["data"]["result"] == {"a": "1", "b": "2"} and result["data"]["redirected_to"] == "10.0.0.6:6380"
    # ASKING 只对紧跟的一条命令有效:限量用的预查询和正式命令前面都要发
    assert clients[1].commands == [["ASKING"], ["HLEN", "h"], ["ASKING"], ["HGETALL", "h"]]
    # ASK 的目标又回 MOVED(迁移刚好完成):再跳一次,新节点上不再发 ASKING
    clients = fake_redis({"GET": AskError("12 10.0.0.6:6380")}, nodes={
        "10.0.0.6:6380": {"ASKING": "OK", "GET": MovedError("12 10.0.0.7:6380")}, "10.0.0.7:6380": {"GET": "v"}})
    result = await run_data_query("redis", redis("GET", "k"))
    assert result["data"]["result"] == "v" and result["data"]["redirected_to"] == "10.0.0.7:6380"
    assert [client.commands for client in clients[-3:]] == [[["GET", "k"]], [["ASKING"], ["GET", "k"]], [["GET", "k"]]]


async def test_redis_gives_up_after_three_redirects(fake_redis):
    clients = fake_redis({}, nodes={
        "redis.app.svc:6379": {"GET": MovedError("7 10.0.0.1:7001")},
        "10.0.0.1:7001": {"GET": MovedError("7 10.0.0.2:7002")},
        "10.0.0.2:7002": {"GET": AskError("7 10.0.0.3:7003")},
        "10.0.0.3:7003": {"ASKING": "OK", "GET": MovedError("7 10.0.0.4:7004")},
        "10.0.0.4:7004": {"GET": "never"}})
    result = await run_data_query("redis", redis("GET", "k"))
    assert not result["success"] and result["error"]["code"] == "REDIS_MOVED"
    message = result["error"]["message"]
    assert "10.0.0.4:7004" in message and "3 次" in message and "槽 7" in message   # 写明最后的目标节点
    assert nodes_of(clients) == ["redis.app.svc:6379", "10.0.0.1:7001", "10.0.0.2:7002", "10.0.0.3:7003"]
    assert all(client.closed for client in clients) and PASSWORD not in message


async def test_redis_redirect_target_formats(fake_redis):
    # 主机名为空 = 当前节点的主机、只换端口
    fake_redis({"GET": MovedError("1 :6390")}, nodes={"redis.app.svc:6390": {"GET": "v"}})
    assert (await run_data_query("redis", redis("GET", "k")))["data"]["redirected_to"] == "redis.app.svc:6390"
    # IPv6:连接用原地址,返回的 host:port 加方括号
    clients = fake_redis({"GET": MovedError("1 fd00::5:6381")}, nodes={"fd00::5:6381": {"GET": "v"}})
    result = await run_data_query("redis", redis("GET", "k"))
    assert result["data"]["redirected_to"] == "[fd00::5]:6381" and clients[-1].options["host"] == "fd00::5"
    # 认不出的地址(cluster-preferred-endpoint-type hostname 但没配主机名时是 ?):报错,不去连
    clients = fake_redis({"GET": MovedError("1 ?:6381")})
    result = await run_data_query("redis", redis("GET", "k"))
    assert result["error"]["code"] == "REDIS_MOVED" and "无法识别" in result["error"]["message"]
    assert nodes_of(clients)[-1] == "redis.app.svc:6379"


async def test_redis_redirects_share_one_deadline(fake_redis, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(data, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    def moved(target):
        def reply(command):
            clock[0] += 4   # 每个节点花 4 秒
            raise MovedError(f"7 {target}")
        return reply

    clients = fake_redis({}, nodes={"redis.app.svc:6379": {"GET": moved("10.0.0.1:7001")},
                                    "10.0.0.1:7001": {"GET": moved("10.0.0.2:7002")},
                                    "10.0.0.2:7002": {"GET": moved("10.0.0.3:7003")}})
    result = await run_data_query("redis", redis("GET", "k", timeout=10))
    assert result["error"]["code"] == "TIMEOUT" and "10 秒" in result["error"]["message"]
    # 总时限 10 秒:第二个节点只剩 6 秒,第三个只剩 2 秒,再跳就超时,不再连第四个
    assert [client.options["socket_timeout"] for client in clients] == [10, 6, 2]
    assert len(clients) == 3 and all(client.closed for client in clients)


async def test_redis_redirect_target_failure_names_the_node(fake_redis):
    fake_redis({"GET": MovedError("3999 10.0.0.5:6381")}, nodes={"10.0.0.5:6381": {
        "GET": RedisConnectionError(f"Error 113 connecting to 10.0.0.5:6381. No route to host. {PASSWORD}")}})
    result = await run_data_query("redis", redis("GET", "k"))
    message = result["error"]["message"]
    assert result["error"]["code"] == "CONNECT_FAILED" and PASSWORD not in message
    assert message.startswith("集群跳转的目标节点 10.0.0.5:6381 连接失败")


# ---------------------------------------------------------------- Redis 哨兵

async def test_redis_sentinel_finds_the_master(fake_redis):
    clients = fake_redis({}, nodes={SENTINEL: {"SENTINEL": ["10.0.0.9", "6379"]}, "10.0.0.9:6379": {"GET": "v"}})
    result = await run_data_query("redis", sentinel("GET", "k", db=3, tls=True))
    assert result["success"], result
    assert result["data"]["result"] == "v" and result["data"]["master"] == "10.0.0.9:6379"
    assert "redirected_to" not in result["data"]
    probe, master = clients
    assert probe.commands == [["SENTINEL", "GET-MASTER-ADDR-BY-NAME", "mymaster"]] and probe.closed and master.closed
    # 哨兵:默认端口 26379,不选库;没给 sentinel_username / sentinel_password 就不认证
    assert probe.options["port"] == 26379 and probe.options["db"] == 0
    assert probe.options["username"] is None and probe.options["password"] is None
    # 主节点:username / password / db / tls
    assert master.options["username"] == "app" and master.options["password"] == PASSWORD and master.options["db"] == 3
    assert probe.options["ssl"] is True and master.options["ssl"] is True and probe.options["protocol"] == 2
    assert master.commands == [["GET", "k"]]


async def test_redis_sentinel_authenticates_with_its_own_credentials(fake_redis):
    clients = fake_redis({}, nodes={"sentinel.app.svc:5000": {"SENTINEL": ("10.0.0.9", 6380)},
                                    "10.0.0.9:6380": {"GET": "v"}})
    result = await run_data_query("redis", sentinel("GET", "k", port=5000, sentinel_username="sentinel-user",
                                                    sentinel_password=SENTINEL_PASSWORD))
    assert result["data"]["master"] == "10.0.0.9:6380"
    probe, master = clients
    assert probe.options["username"] == "sentinel-user" and probe.options["password"] == SENTINEL_PASSWORD
    assert master.options["username"] == "app" and master.options["password"] == PASSWORD
    assert SENTINEL_PASSWORD not in master.options.values()


async def test_redis_sentinel_without_the_master_name(fake_redis):
    clients = fake_redis({}, nodes={SENTINEL: {"SENTINEL": None}})
    result = await run_data_query("redis", sentinel("GET", "k", sentinel_master="other"))
    assert result["error"]["code"] == "QUERY_FAILED"
    assert "哨兵上没有这个主节点名" in result["error"]["message"] and "other" in result["error"]["message"]
    assert len(clients) == 1 and clients[0].closed   # 没有去连别的地址


@pytest.mark.parametrize("reply", [["10.0.0.9"], ["bad host!", "6379"], ["10.0.0.9", "0"], ["10.0.0.9", "x"],
                                   ["10.0.0.9", True], "10.0.0.9:6379"])
async def test_redis_sentinel_unreadable_reply(fake_redis, reply):
    clients = fake_redis({}, nodes={SENTINEL: {"SENTINEL": reply}})
    result = await run_data_query("redis", sentinel("GET", "k"))
    assert result["error"]["code"] == "QUERY_FAILED" and "无法识别" in result["error"]["message"]
    assert len(clients) == 1


@pytest.mark.parametrize("node,error,code,expected", [
    (SENTINEL, AuthenticationError(f"WRONGPASS invalid username-password pair {SENTINEL_PASSWORD}"), "CONNECT_FAILED",
     "哨兵 sentinel.app.svc:26379 认证失败"),
    (SENTINEL, RedisConnectionError(f"Error 111 connecting to sentinel {SENTINEL_PASSWORD}"), "CONNECT_FAILED",
     "哨兵 sentinel.app.svc:26379 连接失败"),
    (SENTINEL, RedisTimeoutError(f"Timeout reading from socket {SENTINEL_PASSWORD}"), "TIMEOUT", "哨兵"),
    (SENTINEL, ResponseError(f"unknown command 'SENTINEL' {SENTINEL_PASSWORD}"), "QUERY_FAILED", "哨兵端口通常是 26379"),
    ("10.0.0.9:6379", AuthenticationError(f"WRONGPASS {PASSWORD} {SENTINEL_PASSWORD}"), "CONNECT_FAILED",
     "哨兵返回的主节点 10.0.0.9:6379 认证失败"),
    ("10.0.0.9:6379", ResponseError(f"ERR {SENTINEL_PASSWORD}"), "QUERY_FAILED", "哨兵返回的主节点 10.0.0.9:6379 返回错误"),
])
async def test_redis_sentinel_errors_never_include_the_sentinel_password(fake_redis, node, error, code, expected):
    replies = {SENTINEL: {"SENTINEL": ["10.0.0.9", "6379"]}, "10.0.0.9:6379": {"GET": "v"}}
    replies[node] = {"SENTINEL": error} if node == SENTINEL else {"GET": error}
    clients = fake_redis({}, nodes=replies)
    result = await run_data_query("redis", sentinel("GET", "k", sentinel_password=SENTINEL_PASSWORD))
    message = result["error"]["message"]
    assert result["error"]["code"] == code and expected in message
    assert SENTINEL_PASSWORD not in message and PASSWORD not in message and "[REDACTED]" in message
    assert all(client.closed for client in clients)


async def test_redis_sentinel_master_also_follows_moved(fake_redis):
    clients = fake_redis({}, nodes={SENTINEL: {"SENTINEL": ["10.0.0.9", "6379"]},
                                    "10.0.0.9:6379": {"GET": MovedError("5 10.0.0.10:6379")},
                                    "10.0.0.10:6379": {"GET": "v"}})
    result = await run_data_query("redis", sentinel("GET", "k", sentinel_password=SENTINEL_PASSWORD))
    assert result["data"]["master"] == "10.0.0.9:6379" and result["data"]["redirected_to"] == "10.0.0.10:6379"
    assert clients[2].options["password"] == PASSWORD   # 跳转用主节点的账号,不是哨兵的


@pytest.mark.parametrize("error,code", [
    (AuthenticationError(f"invalid username-password pair {PASSWORD}"), "CONNECT_FAILED"),
    (RedisConnectionError(f"Error 111 connecting to redis:6379 {PASSWORD}"), "CONNECT_FAILED"),
    (RedisTimeoutError("Timeout reading from socket"), "TIMEOUT"),
    (ResponseError("WRONGTYPE Operation against a key holding the wrong kind of value"), "QUERY_FAILED"),
])
async def test_redis_errors_never_include_the_password(fake_redis, error, code):
    clients = fake_redis({"GET": error})
    result = await run_data_query("redis", redis("GET", "k"))
    assert result["error"]["code"] == code and PASSWORD not in result["error"]["message"]
    assert clients[0].closed


# ---------------------------------------------------------------- Elasticsearch 执行(本地 http.server)

class EsHandler(BaseHTTPRequestHandler):
    def handle_request(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self.server.seen.append({"method": self.command, "path": self.path, "headers": self.headers, "body": body})
        route = self.path.split("?")[0]
        if route.endswith("/redirect/_search"):
            return self.reply(302, b"", "text/plain", {"Location": "/prefix/_cluster/health"})
        if route.endswith("/slow/_search"):
            time.sleep(2.5)
            return self.reply(200, b"{}")
        if route.endswith("/big/_search"):
            hits = [{"_id": str(i), "_source": {"message": "m" * 500}} for i in range(300)]
            return self.reply(200, json.dumps({"hits": {"hits": hits}}).encode())
        if route.endswith("/secret/_search"):
            source = {"user": "a", "password": "p", "nested": {"api_key": "k", "token": None}}
            return self.reply(200, json.dumps({"hits": {"hits": [{"_source": source}]}}).encode())
        if route.endswith("/_cat/indices"):
            return self.reply(200, b"health status index\ngreen  open   orders\n", "text/plain; charset=UTF-8")
        if route.endswith("/missing"):
            return self.reply(404, b'{"error":{"type":"index_not_found_exception"},"status":404}')
        return self.reply(200, json.dumps({"ok": True}).encode())

    def reply(self, status, body, content_type="application/json", headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    do_GET = do_POST = do_HEAD = handle_request

    def log_message(self, *args):
        pass


@pytest.fixture
def es_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), EsHandler)
    server.seen = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_address[1]}/prefix/"
    finally:
        server.shutdown()
        server.server_close()


async def test_es_basic_auth_params_and_url_prefix(es_server):
    server, url = es_server
    result = await run_data_query("elasticsearch", {"url": url, "username": "elastic", "password": PASSWORD,
                                                    "path": "/_cat/indices", "params": {"format": "txt", "v": True, "size": 5}})
    assert result["success"] and result["data"]["status"] == 200
    assert result["data"]["body"].startswith("health status index")
    request = server.seen[0]
    assert request["method"] == "GET" and request["path"] == "/prefix/_cat/indices?format=txt&v=true&size=5"
    assert request["headers"]["Authorization"] == "Basic " + base64.b64encode(f"elastic:{PASSWORD}".encode()).decode()


async def test_es_api_key_and_json_body(es_server):
    server, url = es_server
    body = {"query": {"match": {"name": "中文"}}, "size": 1}
    result = await run_data_query("elasticsearch", {"url": url, "api_key": "a2V5OnNlY3JldA==", "method": "POST",
                                                    "path": "/logs-*/_search", "body": body})
    assert result["data"] == {"status": 200, "body": {"ok": True}, "truncated": False}
    request = server.seen[0]
    assert request["headers"]["Authorization"] == "ApiKey a2V5OnNlY3JldA=="
    assert request["headers"]["Content-Type"] == "application/json" and json.loads(request["body"]) == body


async def test_es_msearch_sends_ndjson(es_server):
    server, url = es_server
    lines = [{"index": "a"}, {"query": {"match_all": {}}}, {}, {"size": 0}]
    await run_data_query("elasticsearch", {"url": url, "method": "POST", "path": "/_msearch", "body": lines})
    request = server.seen[0]
    assert request["headers"]["Content-Type"] == "application/x-ndjson"
    assert request["body"].decode().splitlines() == [json.dumps(line) for line in lines]
    assert request["body"].endswith(b"\n")


async def test_es_does_not_follow_redirects(es_server):
    server, url = es_server
    result = await run_data_query("elasticsearch", {"url": url, "path": "/redirect/_search"})
    assert result["success"] and result["data"]["status"] == 302
    assert len(server.seen) == 1


async def test_es_http_errors_are_returned_as_results(es_server):
    _, url = es_server
    result = await run_data_query("elasticsearch", {"url": url, "path": "/missing"})
    assert result["success"] and result["data"]["status"] == 404
    assert result["data"]["body"]["error"]["type"] == "index_not_found_exception"


async def test_es_head_has_no_body(es_server):
    _, url = es_server
    result = await run_data_query("elasticsearch", {"url": url, "path": "/orders", "method": "HEAD"})
    assert result["data"] == {"status": 200, "body": None, "truncated": False}


async def test_es_redacts_secret_fields(es_server):
    _, url = es_server
    result = await run_data_query("elasticsearch", {"url": url, "path": "/secret/_search"})
    source = result["data"]["body"]["hits"]["hits"][0]["_source"]
    assert source == {"user": "a", "password": "[REDACTED]", "nested": {"api_key": "[REDACTED]", "token": None}}


async def test_es_truncates_large_bodies(es_server):
    _, url = es_server
    result = await run_data_query("elasticsearch", {"url": url, "path": "/big/_search"})
    body = result["data"]["body"]
    assert result["truncated"] is True and result["data"]["truncated"] is True
    assert isinstance(body, str) and body.endswith(data.TRUNCATED) and len(body) == data.MAX_OUTPUT + len(data.TRUNCATED)


async def test_es_timeout(es_server):
    _, url = es_server
    started = time.monotonic()
    result = await run_data_query("elasticsearch", {"url": url, "path": "/slow/_search", "timeout": 1})
    assert result["error"]["code"] == "TIMEOUT" and time.monotonic() - started < 2.4


async def test_es_ignores_proxy_environment(es_server, monkeypatch):
    _, url = es_server
    for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    assert (await run_data_query("elasticsearch", {"url": url, "path": "/"}))["data"]["status"] == 200


async def test_es_connection_failure_hides_credentials():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()
    result = await run_data_query("elasticsearch", {"url": f"http://127.0.0.1:{port}", "path": "/", "username": "elastic",
                                                    "password": PASSWORD, "timeout": 2})
    assert result["error"]["code"] in ("CONNECT_FAILED", "TIMEOUT")
    text = json.dumps(result)
    assert PASSWORD not in text and base64.b64encode(f"elastic:{PASSWORD}".encode()).decode() not in text


# ---------------------------------------------------------------- 经 KubernetesExecutor(agent 走的路径)

async def test_executor_routes_data_queries_without_kubernetes(monkeypatch):
    calls = []

    async def fake_run(operation, arguments, timeout=60):
        calls.append((operation, arguments, timeout))
        return {"success": True, "data": {"rows": [["x"]], "password": "p"}, "truncated": True}

    monkeypatch.setattr(executor_module, "run_data_query", fake_run)
    executor = KubernetesExecutor()
    try:
        # 带 _preconditions 也不会去查 Kubernetes(没有集群,查了就会失败)
        result = await executor.execute("mysql", mysql("SELECT 1", _preconditions=[{"path": "/api/v1/x"}]), timeout=30)
        assert result["success"] and result["source"] == "mysql" and result["truncated"] is True
        assert result["data"] == {"rows": [["x"]], "password": "[REDACTED]"}
        assert calls[0][0] == "mysql" and calls[0][2] == 29

        async def failing(operation, arguments, timeout=60):
            return {"success": False, "error": {"code": "CONNECT_FAILED", "message": "连接 MySQL 失败"}}

        monkeypatch.setattr(executor_module, "run_data_query", failing)
        result = await executor.execute("redis", redis("GET", "k"))
        assert not result["success"] and result["error"] == {"code": "CONNECT_FAILED", "message": "连接 MySQL 失败"}
    finally:
        await executor.close()


async def test_executor_runs_a_real_es_query(es_server):
    server, url = es_server
    executor = KubernetesExecutor()
    try:
        result = await executor.execute("elasticsearch", {"url": url, "path": "/_cluster/health"}, call_id="es-1")
    finally:
        await executor.close()
    assert result["success"] and result["source"] == "elasticsearch" and result["data"]["status"] == 200
    assert server.seen[0]["path"] == "/prefix/_cluster/health"


async def test_executor_prepare_checks_and_redacts_preview():
    executor = KubernetesExecutor()
    try:
        prepared = await executor.prepare("redis", redis("GET", "k"))
        assert prepared["success"] and prepared["read_only"] is True and prepared["ops"] == ["data.query"]
        assert prepared["preview"]["arguments"]["password"] == "[REDACTED]"
        assert prepared["arguments"]["password"] == PASSWORD   # 完整参数留给网关
        rejected = await executor.prepare("redis", redis("SET", "k", "v"))
        assert not rejected["success"] and rejected["read_only"] is False and rejected["ops"] == ["data.query"]
        assert rejected["error"]["code"] == "INVALID_ARGUMENT" and "白名单" in rejected["error"]["message"]
        prepared = await executor.prepare("redis", sentinel("GET", "k", sentinel_password=SENTINEL_PASSWORD))
        preview = prepared["preview"]["arguments"]
        assert prepared["success"] and preview["sentinel_password"] == preview["password"] == "[REDACTED]"
        assert preview["sentinel_master"] == "mymaster" and SENTINEL_PASSWORD not in json.dumps(prepared["preview"])
        assert prepared["arguments"]["sentinel_password"] == SENTINEL_PASSWORD
    finally:
        await executor.close()


async def test_check_never_raises_on_odd_input():
    for operation in DATA_OPERATIONS:
        for arguments in ({}, {"host": object()}, {"command": [object()]}, {"url": 1, "path": None}, {"sql": 1},
                          {"host": "r", "command": ["GET", "k"], "sentinel_master": object()}):
            assert isinstance(check_data_query(operation, arguments), str)
    assert (await run_data_query("redis", {"host": "r"}))["error"]["code"] == "INVALID_ARGUMENT"
    assert (await run_data_query("mysql", None))["error"]["code"] == "INVALID_ARGUMENT"
