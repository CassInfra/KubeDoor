"""net_probe:从 agent 所在 Pod 解析域名(系统解析器)、测试 TCP 端口。"""

import asyncio
import json
import socket
import time
from types import SimpleNamespace

import pytest


def body(resp):
    return json.loads(resp.body)


def request(**query):
    return SimpleNamespace(query=query)


@pytest.fixture
def net(net_probe_module, monkeypatch, tmp_path):
    resolv = tmp_path / "resolv.conf"
    resolv.write_text("nameserver 10.96.0.10\nsearch kubedoor.svc.cluster.local svc.cluster.local cluster.local\n"
                      "options ndots:5\n")
    monkeypatch.setattr(net_probe_module, "RESOLV_CONF", str(resolv))
    monkeypatch.setattr(net_probe_module, "_source_ip", None)
    monkeypatch.setattr(net_probe_module.socket, "gethostname", lambda: "kubedoor-agent-7c9f-abcde")
    return net_probe_module


def fake_dns(monkeypatch, table, calls=None):
    """按名字返回地址;没登记的名字当成不存在。只接管这几个测试用的名字,其余走真实解析"""
    real = socket.getaddrinfo

    def getaddrinfo(host, port, family=0, type=0, *args):
        if calls is not None:
            calls.append((host, family))
        if host == "kubedoor-agent-7c9f-abcde":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.2.3.4", 0))]
        if host in table:
            value = table[host]
            if isinstance(value, BaseException):
                raise value
            if callable(value):
                return value()
            infos = []
            for ip in value:
                fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
                if family in (0, fam):
                    infos.append((fam, socket.SOCK_STREAM, 6, "", (ip, 0)))
            return infos
        if host.endswith(".test"):
            raise socket.gaierror(socket.EAI_NONAME, "Name does not resolve")
        return real(host, port, family, type, *args)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


@pytest.mark.asyncio
async def test_resolve_like_a_pod(net, monkeypatch):
    calls = []
    fake_dns(monkeypatch, {"es.example.test": ["10.114.1.4", "10.114.1.4", "10.114.1.5", "fd00::4"]}, calls)
    assert (await net.resolve("es.example.test"))["ips"] == ["10.114.1.4", "10.114.1.5", "fd00::4"]
    only4 = await net.resolve("es.example.test", socket.AF_INET)
    assert only4["ips"] == ["10.114.1.4", "10.114.1.5"] and only4["code"] == "ok" and only4["ms"] >= 0
    assert calls[-1] == ("es.example.test", socket.AF_INET)
    missing = await net.resolve("gone.example.test")
    assert missing["ips"] == [] and missing["code"] == "not_found" and missing["error"] == "域名不存在"


@pytest.mark.asyncio
async def test_resolve_errors_and_timeout(net, monkeypatch):
    fake_dns(monkeypatch, {
        "flaky.example.test": socket.gaierror(socket.EAI_AGAIN, "Try again"),
        "slow.example.test": lambda: time.sleep(1) or [],
    })
    flaky = await net.resolve("flaky.example.test")
    assert flaky["code"] == "temporary" and "暂时无法解析" in flaky["error"]
    monkeypatch.setattr(net, "RESOLVE_TIMEOUT", 0.2)
    result = await net.resolve("slow.example.test")
    assert result["ips"] == [] and result["code"] == "timeout" and "秒内没有解析完成" in result["error"]


@pytest.mark.asyncio
async def test_handle_resolve(net, monkeypatch):
    fake_dns(monkeypatch, {"a.example.test": ["10.0.0.1"], "kubedoor-master.kubedoor": ["10.96.1.2"]})
    resp = await net.handle_resolve(request(names="a.example.test, kubedoor-master.kubedoor,a.example.test",
                                            family="4"))
    data = body(resp)["data"]
    assert resp.status == 200
    assert data["source"] == {"pod": "kubedoor-agent-7c9f-abcde", "ip": "10.2.3.4", "nameservers": ["10.96.0.10"],
                              "search": ["kubedoor.svc.cluster.local", "svc.cluster.local", "cluster.local"]}
    assert list(data["results"]) == ["a.example.test", "kubedoor-master.kubedoor"]
    assert data["results"]["kubedoor-master.kubedoor"]["ips"] == ["10.96.1.2"]


@pytest.mark.asyncio
@pytest.mark.parametrize("query,error", [
    ({}, "缺少要解析的域名"),
    ({"names": "a.com,*.b.com"}, "域名格式不正确"),
    ({"names": "a.com,中文.com"}, "域名格式不正确"),
    ({"names": ",".join(f"h{i}.com" for i in range(11))}, "最多解析"),
    ({"names": "a.com", "family": "5"}, "family"),
])
async def test_handle_resolve_validation(net, query, error):
    resp = await net.handle_resolve(request(**query))
    assert resp.status == 400 and error in body(resp)["error"]


async def _closed_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@pytest.mark.asyncio
async def test_tcp_open_refused_and_by_name(net, monkeypatch):
    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        ok = await net.tcp_check("127.0.0.1", port)
        assert ok["ok"] is True and ok["dns"] is None
        assert ok["attempts"][0]["ip"] == "127.0.0.1" and ok["attempts"][0]["local"] == "127.0.0.1"

        closed = await _closed_port()
        refused = await net.tcp_check("127.0.0.1", closed)
        assert refused["ok"] is False and "连接被拒绝" in refused["attempts"][0]["error"], refused

        # 域名:先按 Pod 的解析结果拿到地址,每个地址各连一次,有一个通就算通
        fake_dns(monkeypatch, {"db.example.test": ["127.0.0.1"], "gone.example.test": socket.gaierror(
            socket.EAI_NONAME, "Name does not resolve")})
        by_name = await net.tcp_check("db.example.test", port)
        assert by_name["ok"] is True and by_name["dns"]["ips"] == ["127.0.0.1"]
        missing = await net.tcp_check("gone.example.test", port)
        assert missing["ok"] is False and missing["attempts"] == [] and missing["dns"]["error"] == "域名不存在"
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_tcp_timeout_and_handler(net, monkeypatch):
    async def hang(*args, **kwargs):
        await asyncio.sleep(5)

    monkeypatch.setattr(net, "CONNECT_TIMEOUT", 0.2)
    monkeypatch.setattr(net.asyncio, "open_connection", hang)
    fake_dns(monkeypatch, {})
    result = await net.tcp_check("10.255.255.1", 3306)
    assert result["ok"] is False and "秒内连不上" in result["attempts"][0]["error"]

    resp = await net.handle_tcp(request(host="10.255.255.1", port="3306"))
    data = body(resp)["data"]
    assert resp.status == 200 and data["host"] == "10.255.255.1" and data["port"] == 3306 and data["ok"] is False
    assert data["source"]["pod"] == "kubedoor-agent-7c9f-abcde"


@pytest.mark.asyncio
@pytest.mark.parametrize("query,error", [
    ({"port": "80"}, "主机格式不正确"),
    ({"host": "a b", "port": "80"}, "主机格式不正确"),
    ({"host": "db.example.com"}, "端口"),
    ({"host": "db.example.com", "port": "0"}, "端口"),
    ({"host": "db.example.com", "port": "65536"}, "端口"),
    ({"host": "db.example.com", "port": "x"}, "端口"),
])
async def test_handle_tcp_validation(net, query, error):
    resp = await net.handle_tcp(request(**query))
    assert resp.status == 400 and error in body(resp)["error"]
