"""Unit tests for Fail-Closed SSRF guard, DNS pinning, and redirect blocking."""

from __future__ import annotations

import http.server
import socket
import ssl
import threading

import pytest

from kubedoor_tools.notify.models import HttpPreparedRequest
from kubedoor_tools.notify.ssrf import (
    ResolvedTarget,
    SSRFError,
    send_pinned_http,
    validate_host_port,
    validate_target_url,
)


def _fake_resolver(mapping: dict[str, list[str]]):
    def _resolve(host, port, type=socket.SOCK_STREAM):
        ips = mapping[host]
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, type, 0, "", (ip, port)) for ip in ips]

    return _resolve


@pytest.mark.parametrize(
    "bad_url",
    [
        "",
        "http://hooks.example.com/hook",  # http blocked unless allow_http=True
        "file:///etc/passwd",
        "gopher://127.0.0.1:6379/_INFO",
        "ftp://hooks.example.com/a",
        "https://user:pass@hooks.example.com/hook",
        "https://hooks.example.com@evil.com/hook",
        "https://hooks.example.com/a b",
        "https://hooks.example.com/a\r\nb",
        "https://hooks.example.com/a\x00b",
        "https://127.0.0.1/hook",
        "https://[::1]/hook",
        "https://localhost/hook",
        "https://sub.localhost/hook",
        "https://10.0.0.1/hook",
        "https://172.16.5.4/hook",
        "https://192.168.1.10/hook",
        "https://169.254.169.254/latest/meta-data/",
        "https://[fe80::1]/hook",
        "https://[fc00::1]/hook",
        "https://100.64.0.1/hook",
        "https://100.127.255.254/hook",
        "https://[::ffff:127.0.0.1]/hook",
        "https://[::ffff:169.254.169.254]/hook",
        "https://[::ffff:10.0.0.5]/hook",
        "https://[64:ff9b::127.0.0.1]/hook",
        "https://0.0.0.0/hook",
        "https://224.0.0.1/hook",
    ],
)
def test_validate_target_url_blocks_unsafe_schemes_userinfo_and_ips(bad_url):
    with pytest.raises(SSRFError):
        validate_target_url(bad_url)


@pytest.mark.parametrize(
    "dns_ips",
    [
        ["127.0.0.1"],
        ["10.1.2.3"],
        ["169.254.169.254"],
        ["100.64.10.10"],
        ["::1"],
        ["fe80::1%eth0"],
        ["::ffff:127.0.0.1"],
        ["93.184.216.34", "10.0.0.1"],  # multi-A split answer attack
        ["2606:4700:4700::1111", "::ffff:192.168.1.1"],
    ],
)
def test_validate_target_url_blocks_unsafe_dns_answers_including_split_answers(dns_ips):
    resolver = _fake_resolver({"evil.example.com": dns_ips})
    with pytest.raises(SSRFError):
        validate_target_url("https://evil.example.com/hook", resolver=resolver)


def test_validate_target_url_allows_public_ips_and_allow_http():
    resolver = _fake_resolver({"hooks.example.com": ["93.184.216.34", "2606:4700:4700::1111"]})
    target = validate_target_url("https://hooks.example.com/events", resolver=resolver)
    assert target == ResolvedTarget(
        url="https://hooks.example.com/events",
        scheme="https",
        host="hooks.example.com",
        port=443,
        pinned_ip="93.184.216.34",
        ips=("93.184.216.34", "2606:4700:4700::1111"),
    )

    http_target = validate_target_url("http://hooks.example.com:8080/events", allow_http=True, resolver=resolver)
    assert http_target.scheme == "http" and http_target.port == 8080

    # allowed_hosts enforcement
    with pytest.raises(SSRFError):
        validate_target_url("https://hooks.example.com/events", allowed_hosts=["other.example.com"], resolver=resolver)
    assert validate_target_url("https://hooks.example.com/events", allowed_hosts=["*.example.com"], resolver=resolver).host == "hooks.example.com"


def test_env_and_param_private_allowlists(monkeypatch):
    resolver = _fake_resolver(
        {
            "feishu.corp.local": ["10.20.1.5"],
            "other.corp.local": ["10.99.1.5"],
            "meta.corp.local": ["169.254.169.254"],
        }
    )

    monkeypatch.setenv("NOTIFY_ALLOWED_PRIVATE_CIDRS", "10.20.0.0/16")
    assert validate_target_url("https://feishu.corp.local/hook", resolver=resolver).pinned_ip == "10.20.1.5"
    with pytest.raises(SSRFError):
        validate_target_url("https://other.corp.local/hook", resolver=resolver)
    with pytest.raises(SSRFError):
        validate_target_url("https://meta.corp.local/hook", resolver=resolver)

    monkeypatch.delenv("NOTIFY_ALLOWED_PRIVATE_CIDRS")
    monkeypatch.setenv("NOTIFY_ALLOWED_HOSTS", "feishu.corp.local")
    assert validate_target_url("https://feishu.corp.local/hook", resolver=resolver).pinned_ip == "10.20.1.5"
    with pytest.raises(SSRFError):
        validate_target_url("https://other.corp.local/hook", resolver=resolver)
    # Cloud metadata link-local remains blocked even if host is listed in NOTIFY_ALLOWED_HOSTS
    monkeypatch.setenv("NOTIFY_ALLOWED_HOSTS", "meta.corp.local")
    with pytest.raises(SSRFError):
        validate_target_url("https://meta.corp.local/hook", resolver=resolver)


def test_validate_host_port_for_smtp_relays():
    with pytest.raises(SSRFError):
        validate_host_port("127.0.0.1", 465)
    with pytest.raises(SSRFError):
        validate_host_port("localhost", 587)
    with pytest.raises(SSRFError):
        validate_host_port("100.64.0.5", 465)
    with pytest.raises(SSRFError):
        validate_host_port("https://smtp.example.com", 465)

    resolver = _fake_resolver({"smtp.example.com": ["93.184.216.34"]})
    target = validate_host_port("smtp.example.com", 465, resolver=resolver)
    assert target.pinned_ip == "93.184.216.34" and target.port == 465


def test_send_pinned_http_pins_ip_preserves_host_and_blocks_redirects():
    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or "0")
            body = self.rfile.read(length).decode("utf-8")
            seen.append({"path": self.path, "host": self.headers.get("Host"), "body": body})
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')

        def log_message(self, format, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        resolver = _fake_resolver({"webhook.test.local": ["127.0.0.1"]})
        req = HttpPreparedRequest(
            method="POST",
            url=f"http://webhook.test.local:{port}/events",
            headers={"Content-Type": "application/json"},
            body=b'{"hello":"world"}',
            allow_http=True,
        )
        # Fail-closed without private host allowlist
        with pytest.raises(SSRFError):
            send_pinned_http(req, resolver=resolver)

        status, body, _ = send_pinned_http(
            req,
            allow_private_hosts=["webhook.test.local"],
            resolver=resolver,
        )
        assert status == 200 and body == '{"ok":true}'
        assert seen[-1]["host"] == f"webhook.test.local:{port}"
        assert seen[-1]["body"] == '{"hello":"world"}'

        # Redirect is NEVER followed
        redir_req = HttpPreparedRequest(
            method="POST",
            url=f"http://webhook.test.local:{port}/redirect",
            body=b"{}",
            allow_http=True,
        )
        r_status, _, r_headers = send_pinned_http(
            redir_req,
            allow_private_hosts=["webhook.test.local"],
            resolver=resolver,
        )
        assert r_status == 302
        assert r_headers["Location"] == "http://169.254.169.254/latest/meta-data/"
    finally:
        server.shutdown()
        server.server_close()


def test_send_pinned_https_passes_sni_host_while_connecting_to_pinned_ip(monkeypatch):
    calls = []

    class FakeWrappedSocket:
        def sendall(self, data):
            pass

        def makefile(self, mode, *args, **kwargs):
            import io

            return io.BytesIO(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")

        def close(self):
            pass

    class FakeContext:
        def wrap_socket(self, sock, server_hostname=None):
            calls.append(("wrap_socket", server_hostname))
            return FakeWrappedSocket()

    def fake_create_connection(addr, timeout=None, source_address=None):
        calls.append(("create_connection", addr))
        return object()

    monkeypatch.setattr(socket, "create_connection", fake_create_connection)
    resolver = _fake_resolver({"api.example.com": ["93.184.216.34"]})
    req = HttpPreparedRequest(method="POST", url="https://api.example.com/hook", body=b"{}")
    status, body, _ = send_pinned_http(req, resolver=resolver, ssl_context=FakeContext())  # type: ignore[arg-type]
    assert status == 200 and body == "ok"
    assert calls == [
        ("create_connection", ("93.184.216.34", 443)),
        ("wrap_socket", "api.example.com"),
    ]
