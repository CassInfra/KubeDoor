"""data.py 配真实驱动(PyMySQL、redis-py)跑一遍:本地起最小的 MySQL / RESP 协议假服务,不需要真实数据库。

验证连接参数真实驱动能接受、握手里没有多余的命令(RESP3 的 HELLO、CLIENT SETINFO)、不开 MULTI_STATEMENTS /
LOCAL_FILES,无缓冲游标读完才回滚、没读完直接断开,以及 Redis 集群 MOVED / ASK 的跟随(ASKING 和命令在同一个
连接上)和哨兵查主节点。没装驱动时跳过。
"""

import gc
import socket
import socketserver
import struct
import threading
import time

import pytest

from kubedoor_tools import run_data_query

PASSWORD = "Pa55-w0rd!s3cret"
SENTINEL_PASSWORD = "S3ntinel-Pa55!x"


def _closed_port():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()
    return port


def _serve(server):
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# ---------------------------------------------------------------- MySQL 协议假服务

def _lenenc(data: bytes) -> bytes:
    assert len(data) < 251
    return bytes([len(data)]) + data


OK = b"\x00\x00\x00" + struct.pack("<HH", 0, 0)
EOF = b"\xfe" + struct.pack("<HH", 0, 0)
CLIENT_LOCAL_FILES, CLIENT_MULTI_STATEMENTS = 1 << 7, 1 << 16


class MySQLHandler(socketserver.BaseRequestHandler):
    def recv(self, size):
        data = b""
        while len(data) < size:
            chunk = self.request.recv(size - len(data))
            if not chunk:
                return None
            data += chunk
        return data

    def read_packet(self):
        header = self.recv(4)
        if header is None:
            return None, None
        return header[3], self.recv(int.from_bytes(header[:3], "little"))

    def send(self, seq, payload):
        self.request.sendall(len(payload).to_bytes(3, "little") + bytes([seq & 0xFF]) + payload)

    def handle(self):
        try:
            self.serve()
        except OSError:   # 客户端带着没读的数据关闭连接时会收到 RST
            self.server.queries.append("<reset>")

    def serve(self):
        capabilities = 1 | 8 | 512 | 8192 | 32768 | (1 << 17) | (1 << 19) | (1 << 20) | (1 << 21)
        salt = b"12345678abcdefghijkl"
        self.send(0, b"\x0a8.0.36-fake\x00" + struct.pack("<I", 7) + salt[:8] + b"\x00"
                  + struct.pack("<HBHHB", capabilities & 0xFFFF, 255, 2, capabilities >> 16, 21) + b"\x00" * 10
                  + salt[8:] + b"\x00mysql_native_password\x00")
        seq, response = self.read_packet()
        self.server.client_flags.append(struct.unpack("<I", response[:4])[0])
        self.server.users.append(response[32:response.index(b"\x00", 32)].decode())
        self.send(seq + 1, b"\x00\x00\x00" + struct.pack("<HH", 2, 0))   # 认证通过(autocommit 状态)
        while True:
            _, packet = self.read_packet()
            if packet is None or packet[:1] == b"\x01":   # COM_QUIT
                self.server.queries.append("<quit>" if packet else "<closed>")
                return
            query = packet[1:].decode()
            self.server.queries.append(query)
            if query.upper().startswith("SELECT"):
                self.result_set()
            else:
                self.send(1, OK)

    def result_set(self):
        columns, rows = self.server.columns, self.server.rows
        packets = [bytes([len(columns)])]
        for name in columns:
            packets.append(b"".join(_lenenc(part) for part in (b"def", b"app", b"t", b"t", name.encode(), name.encode()))
                           + b"\x0c" + struct.pack("<HIBHB", 255, 1024, 253, 0, 0) + b"\x00\x00")
        packets.append(EOF)
        for row in rows:
            packets.append(b"".join(b"\xfb" if value is None else _lenenc(str(value).encode()) for value in row))
        packets.append(EOF)
        for seq, payload in enumerate(packets, start=1):
            self.send(seq, payload)


@pytest.fixture
def mysql_server():
    pytest.importorskip("pymysql")
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), MySQLHandler)
    server.daemon_threads = True
    server.columns, server.rows, server.queries, server.client_flags, server.users = ["id", "password"], [], [], [], []
    _serve(server)
    yield server
    server.shutdown()
    server.server_close()


def _wait_for(predicate):
    deadline = time.monotonic() + 3
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.01)


def _arguments(server, sql, **extra):
    return {"host": "127.0.0.1", "port": server.server_address[1], "user": "app", "password": PASSWORD,
            "sql": sql, "timeout": 5, **extra}


async def test_real_pymysql_reads_in_a_read_only_transaction(mysql_server):
    mysql_server.rows = [(1, PASSWORD), (2, None)]
    result = await run_data_query("mysql", _arguments(mysql_server, "SELECT id, password FROM users;", max_rows=10))
    assert result["success"], result
    assert result["data"]["columns"] == ["id", "password"]
    assert result["data"]["rows"] == [["1", "[REDACTED]"], ["2", None]] and result["truncated"] is False
    _wait_for(lambda: "<quit>" in mysql_server.queries)
    queries = mysql_server.queries
    start = queries.index("SET SESSION TRANSACTION READ ONLY")
    assert queries[start:] == ["SET SESSION TRANSACTION READ ONLY", "SET SESSION MAX_EXECUTION_TIME=5000",
                               "START TRANSACTION READ ONLY", "SELECT id, password FROM users", "ROLLBACK", "<quit>"]
    flags = mysql_server.client_flags[0]
    assert not flags & CLIENT_MULTI_STATEMENTS and not flags & CLIENT_LOCAL_FILES
    assert mysql_server.users == ["app"]


async def test_real_pymysql_disconnects_without_draining_extra_rows(mysql_server, recwarn):
    mysql_server.rows = [(i, None) for i in range(50)]
    result = await run_data_query("mysql", _arguments(mysql_server, "SELECT id, password FROM t", max_rows=3))
    assert result["success"] and result["truncated"] is True and len(result["data"]["rows"]) == 3
    ended = ("<quit>", "<closed>", "<reset>")
    _wait_for(lambda: mysql_server.queries[-1:] and mysql_server.queries[-1] in ended)
    # 没读完不回滚(回滚会先读完剩下的行),直接断开;Windows / Linux 带着没读的数据关闭会发 RST,COM_QUIT 可能收不到
    assert "ROLLBACK" not in mysql_server.queries
    assert mysql_server.queries[-2] == "SELECT id, password FROM t" and mysql_server.queries[-1] in ended
    gc.collect()   # 结果对象回收时不能再去读已经关闭的连接
    assert not [warning for warning in recwarn if issubclass(warning.category, pytest.PytestUnraisableExceptionWarning)]


async def test_real_pymysql_connection_failure():
    pytest.importorskip("pymysql")
    started = time.monotonic()
    result = await run_data_query("mysql", {"host": "127.0.0.1", "port": _closed_port(), "user": "app",
                                            "password": PASSWORD, "sql": "SELECT 1", "timeout": 2})
    # 参数都被 PyMySQL 接受(不是 TypeError),错误码 2003 来自驱动
    assert result["error"]["code"] == "CONNECT_FAILED" and "2003" in result["error"]["message"]
    assert PASSWORD not in result["error"]["message"] and time.monotonic() - started < 6


# ---------------------------------------------------------------- RESP 协议假服务

class RespHandler(socketserver.StreamRequestHandler):
    REPLIES = {
        "AUTH": b"+OK\r\n", "SELECT": b"+OK\r\n", "HLEN": b":2\r\n", "GET": b"$5\r\nvalue\r\n",
        "HGETALL": b"*4\r\n$4\r\nname\r\n$5\r\nalice\r\n$8\r\npassword\r\n$3\r\nxyz\r\n",
        "SCAN": b"*2\r\n$2\r\n17\r\n*2\r\n$2\r\nk1\r\n$2\r\nk2\r\n",
        "MGET": b"*2\r\n$2\r\n\xe4\xb8\r\n$-1\r\n",   # 第一个值不是完整的 UTF-8
    }

    def handle(self):
        server, asking = self.server, False
        while True:
            line = self.rfile.readline()
            if not line:
                return
            args = []
            for _ in range(int(line[1:])):
                size = int(self.rfile.readline()[1:])
                args.append(self.rfile.read(size + 2)[:-2].decode())
            server.commands.append(args)
            name = args[0].upper()
            if name == "ASKING":
                reply = b"+OK\r\n"
            elif name == "GET" and server.redirect:   # 集群:这个 key 的槽在别的节点(-MOVED / -ASK)
                reply = server.redirect.encode() + b"\r\n"
            elif name == "GET" and server.importing:   # 正在导入槽:只认同一个连接上紧跟在 ASKING 后面的命令
                reply = b"$8\r\nimported\r\n" if asking else b"-ERR ASKING expected\r\n"
            elif name == "SENTINEL" and args[1:2] == ["GET-MASTER-ADDR-BY-NAME"]:
                port = server.masters.get(args[2])
                reply = b"*-1\r\n" if port is None else b"*2\r\n$9\r\n127.0.0.1\r\n$%d\r\n%d\r\n" % (len(str(port)), port)
            else:
                reply = self.REPLIES.get(name, b"-ERR unknown command\r\n")
            asking = name == "ASKING"
            self.wfile.write(reply)


@pytest.fixture
def resp_factory():
    pytest.importorskip("redis")
    servers = []

    def make():
        server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), RespHandler)
        server.daemon_threads = True
        server.commands, server.redirect, server.importing, server.masters = [], None, False, {}
        servers.append(_serve(server))
        return server
    yield make
    for server in servers:
        server.shutdown()
        server.server_close()


@pytest.fixture
def resp_server(resp_factory):
    return resp_factory()


def _redis(server, *command, **extra):
    return {"host": "127.0.0.1", "port": server.server_address[1], "password": PASSWORD, "command": list(command), **extra}


def _address(server):
    return f"127.0.0.1:{server.server_address[1]}"


async def test_real_redis_py_uses_resp2_without_extra_handshake(resp_server):
    result = await run_data_query("redis", _redis(resp_server, "HGETALL", "h", username="app", db=2))
    assert result["success"], result
    assert result["data"]["result"] == {"name": "alice", "password": "[REDACTED]"}
    # 只有 AUTH 和 SELECT:没有 RESP3 的 HELLO(Redis 6 之前没有),也没有 CLIENT SETINFO
    assert resp_server.commands == [["AUTH", "app", PASSWORD], ["SELECT", "2"], ["HLEN", "h"], ["HGETALL", "h"]]


async def test_real_redis_py_scan_and_invalid_utf8(resp_server):
    result = await run_data_query("redis", _redis(resp_server, "SCAN", "0", "COUNT", "100"))
    assert result["data"]["result"] == ["17", ["k1", "k2"]]
    result = await run_data_query("redis", _redis(resp_server, "MGET", "a", "b"))
    assert result["data"]["result"] == ["�", None]


async def test_real_redis_py_follows_moved(resp_factory):
    source, target = resp_factory(), resp_factory()
    source.redirect = f"-MOVED 3999 {_address(target)}"
    result = await run_data_query("redis", _redis(source, "GET", "k", username="app"))
    assert result["success"], result
    assert result["data"]["result"] == "value" and result["data"]["redirected_to"] == _address(target)
    assert set(result["data"]) == {"result", "truncated", "elapsed_ms", "redirected_to"}
    # 目标节点用同样的账号重发同一条命令
    assert source.commands == target.commands == [["AUTH", "app", PASSWORD], ["GET", "k"]]


async def test_real_redis_py_sends_asking_on_the_same_connection(resp_factory):
    source, target = resp_factory(), resp_factory()
    source.redirect, target.importing = f"-ASK 3999 {_address(target)}", True
    result = await run_data_query("redis", _redis(source, "GET", "k"))
    assert result["success"], result   # 不在同一个连接上,目标节点会回 ERR ASKING expected
    assert result["data"]["result"] == "imported" and result["data"]["redirected_to"] == _address(target)
    assert target.commands == [["AUTH", PASSWORD], ["ASKING"], ["GET", "k"]]


async def test_real_redis_py_stops_after_three_redirects(resp_server):
    resp_server.redirect = f"-MOVED 1 {_address(resp_server)}"   # 一直指向自己
    result = await run_data_query("redis", _redis(resp_server, "GET", "k"))
    assert result["error"]["code"] == "REDIS_MOVED" and _address(resp_server) in result["error"]["message"]
    assert [args for args in resp_server.commands if args[0] == "GET"] == [["GET", "k"]] * 4   # 第一次 + 跳 3 次


async def test_real_redis_py_sentinel(resp_factory):
    probe, master = resp_factory(), resp_factory()
    probe.masters = {"mymaster": master.server_address[1]}
    arguments = {"host": "127.0.0.1", "port": probe.server_address[1], "sentinel_master": "mymaster",
                 "sentinel_password": SENTINEL_PASSWORD, "password": PASSWORD, "db": 1, "command": ["GET", "k"]}
    result = await run_data_query("redis", arguments)
    assert result["success"], result
    assert result["data"]["result"] == "value" and result["data"]["master"] == _address(master)
    # 哨兵:用哨兵自己的口令认证、不选库;主节点:主节点的口令 + 选库
    assert probe.commands == [["AUTH", SENTINEL_PASSWORD], ["SENTINEL", "GET-MASTER-ADDR-BY-NAME", "mymaster"]]
    assert master.commands == [["AUTH", PASSWORD], ["SELECT", "1"], ["GET", "k"]]
    result = await run_data_query("redis", {**arguments, "sentinel_master": "other"})
    assert result["error"]["code"] == "QUERY_FAILED" and "哨兵上没有这个主节点名" in result["error"]["message"]
    assert len(master.commands) == 3   # 查不到主节点就不去连


async def test_real_redis_py_does_not_retry_connection_failures():
    pytest.importorskip("redis")
    started = time.monotonic()
    result = await run_data_query("redis", {"host": "127.0.0.1", "port": _closed_port(), "password": PASSWORD,
                                            "command": ["GET", "k"], "timeout": 1})
    # redis-py 新版默认重试 10 次(指数退避),关掉后几秒内就返回
    assert result["error"]["code"] in ("CONNECT_FAILED", "TIMEOUT") and time.monotonic() - started < 5
    assert PASSWORD not in result["error"]["message"]
