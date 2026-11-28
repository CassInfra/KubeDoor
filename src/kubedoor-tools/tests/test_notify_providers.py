"""Unit tests for stdlib isolation, models, redaction, all 6 M1 providers, render, and compat."""

from __future__ import annotations

import ast
import base64
import hashlib
import hmac
import json
import pathlib
import smtplib
import subprocess
import sys
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, unquote_plus, urlsplit

import pytest

import kubedoor_tools
from kubedoor_tools import notify
from kubedoor_tools.notify import (
    FIELD_KINDS,
    AlertItem,
    ChannelConfig,
    DeliveryResult,
    FieldSpec,
    HttpPreparedRequest,
    NotificationMessage,
    RateLimit,
    build_alert_message,
    build_event_message,
    build_legacy_request,
    detect_provider_from_url,
    get_provider,
    list_providers,
    mask_hint,
    normalize_at,
    normalize_legacy_token,
    parse_legacy_response,
    provider_schemas,
    redact_text,
    test_content,
)
from kubedoor_tools.notify.providers.dingding import compute_dingding_sign
from kubedoor_tools.notify.providers.feishu import compute_feishu_sign

PKG_ROOT = pathlib.Path(kubedoor_tools.__file__).resolve().parent
NOTIFY_ROOT = PKG_ROOT / "notify"

WECOM_KEY = "a1b2c3d4-0000-4bc4-97a0-0ec2c3f5a1b2"
DING_TOKEN = "0123456789abcdef" * 4
FEISHU_HOOK = "3f8c1a2b-1111-2222-3333-444455556666"
SLACK_TOKEN = "T00000000/B00000000/slacksecretpart0123456789"


# ---------------------------------------------------------------------------
# Stdlib isolation & PEP 562 lazy import checks
# ---------------------------------------------------------------------------
def test_notify_package_only_imports_stdlib():
    py_files = sorted(NOTIFY_ROOT.rglob("*.py")) + [PKG_ROOT / "__init__.py"]
    assert len(py_files) >= 14
    for path in py_files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        top_modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                top_modules.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module:
                    top_modules.add(node.module.split(".")[0])
        non_stdlib = top_modules - set(sys.stdlib_module_names)
        assert not non_stdlib, f"{path.name} imported non-stdlib modules: {non_stdlib}"


def test_import_kubedoor_tools_notify_never_imports_third_party_deps():
    repo_root = PKG_ROOT.parent
    code = """
import importlib.abc
import sys

BLOCKED = {"kubernetes", "yaml", "pymysql", "redis", "requests", "aiohttp"}

class Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname.split(".")[0].lower() in BLOCKED:
            raise ImportError(f"Blocked third-party import: {fullname}")
        return None

sys.meta_path.insert(0, Blocker())

import kubedoor_tools
import kubedoor_tools.notify as n

assert kubedoor_tools.__all__ == [
    "KubernetesExecutor", "parse_kubeconfig", "redact_export",
    "classify_operation", "classify_pod_command", "identify_targets",
    "default_commands", "OP_KEYS", "DATA_OPERATIONS",
    "check_data_query", "run_data_query",
]
assert [p.id for p in n.list_providers()] == ["wecom", "feishu", "dingding", "slack", "webhook", "smtp"]
assert not any(m.split(".")[0].lower() in BLOCKED for m in sys.modules)
print("ISOLATION_OK")
"""
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert "ISOLATION_OK" in proc.stdout


def test_lazy_exports_preserve_exact_all_and_work():
    expected_all = [
        "KubernetesExecutor",
        "parse_kubeconfig",
        "redact_export",
        "classify_operation",
        "classify_pod_command",
        "identify_targets",
        "default_commands",
        "OP_KEYS",
        "DATA_OPERATIONS",
        "check_data_query",
        "run_data_query",
    ]
    assert kubedoor_tools.__all__ == expected_all
    for name in expected_all:
        assert hasattr(kubedoor_tools, name)
    with pytest.raises(AttributeError):
        getattr(kubedoor_tools, "no_such_export_xyz")


# ---------------------------------------------------------------------------
# Models, Registry, URL Detection, Redaction
# ---------------------------------------------------------------------------
def test_field_spec_supports_all_kinds_and_to_dict():
    assert FIELD_KINDS == ("text", "secret", "select", "number", "boolean", "textarea", "headers")
    for kind in FIELD_KINDS:
        spec = FieldSpec(key=f"f_{kind}", label=f"Label {kind}", kind=kind)
        d = spec.to_dict()
        assert d["kind"] == kind
        assert d["secret"] is (kind == "secret")
    with pytest.raises(ValueError, match="Unsupported FieldSpec.kind"):
        FieldSpec(key="bad", label="Bad", kind="file")  # type: ignore[arg-type]


def test_channel_config_accepts_provider_or_type_and_tuple_rate_limit():
    c1 = ChannelConfig(id="c1", name="C1", provider="wecom", rate_limit=(20, 60.0))
    c2 = ChannelConfig(id="c2", name="C2", type="feishu", rate_limit=RateLimit(100, 60.0, burst_limit=5))
    assert c1.provider == "wecom" and c1.type == "wecom"
    assert c1.rate_limit == RateLimit(20, 60.0)
    assert c2.provider == "feishu" and c2.rate_limit is not None and c2.rate_limit.burst_limit == 5


def test_providers_auto_registered_and_schemas_valid():
    providers = list_providers()
    assert [p.id for p in providers] == ["wecom", "feishu", "dingding", "slack", "webhook", "smtp"]
    schemas = provider_schemas()
    assert len(schemas) == 6
    for schema in schemas:
        assert set(("id", "label", "description", "default_rate_limit", "supports_at", "supports_markdown", "fields")) <= set(schema)
        assert isinstance(schema["fields"], list) and len(schema["fields"]) >= 2


@pytest.mark.parametrize(
    "url,expected",
    [
        (f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={WECOM_KEY}", "wecom"),
        (f"https://open.feishu.cn/open-apis/bot/v2/hook/{FEISHU_HOOK}", "feishu"),
        (f"https://open.larksuite.com/open-apis/bot/v2/hook/{FEISHU_HOOK}", "feishu"),
        (f"OPEN.LARKSUITE.COM/open-apis/bot/v2/hook/{FEISHU_HOOK}", "feishu"),
        (f"https://oapi.dingtalk.com/robot/send?access_token={DING_TOKEN}", "dingding"),
        (f"https://hooks.slack.com/services/{SLACK_TOKEN}", "slack"),
        ("https://hooks.example.com/events", None),
        ("ftp://open.feishu.cn/x", None),
        ("", None),
    ],
)
def test_detect_provider_from_url(url, expected):
    assert detect_provider_from_url(url) == expected


def test_redact_text_and_mask_hint():
    assert mask_hint("") == ""
    assert mask_hint("short") == "****"
    assert mask_hint("12345678abcd") == "****abcd"

    text = (
        f"POST https://oapi.dingtalk.com/robot/send?access_token={DING_TOKEN}&sign=abc%2B123 "
        f"Authorization: Bearer topsecret12345 "
        f"X-Api-Key: my-key-val "
        f'{{"X-Secret": "supersecret99"}} '
        f"slack part slacksecretpart0123456789 "
        f"custom={WECOM_KEY}"
    )
    redacted = redact_text(text, secrets=(WECOM_KEY, SLACK_TOKEN, "supersecret99"), max_len=500)
    for sensitive in (DING_TOKEN, "abc%2B123", "topsecret12345", "my-key-val", "supersecret99", "slacksecretpart0123456789", WECOM_KEY):
        assert sensitive not in redacted
    assert "Authorization: ***" in redacted
    assert "X-Api-Key: ***" in redacted
    assert "access_token=***" in redacted
    assert "sign=***" in redacted
    assert redact_text(redacted, max_len=500) == redacted

    clipped = redact_text("x" * 400, max_len=300)
    assert len(clipped) == 300 and clipped.endswith("…")


# ---------------------------------------------------------------------------
# Provider 1: WeCom
# ---------------------------------------------------------------------------
def test_wecom_provider_normalize_truncate_and_parse():
    p = get_provider("wecom")
    assert p.default_rate_limit == RateLimit(20, 60.0)

    settings, secrets = p.normalize(
        {"domain_preset": "public", "at": " zhangsan "},
        {"token": f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?debug=1&key={WECOM_KEY}"},
    )
    assert settings == {"domain_preset": "public", "at": "zhangsan"}
    assert secrets == {"token": WECOM_KEY}

    # Custom domain SSRF blocks loopback
    with pytest.raises(ValueError):
        p.normalize({"domain_preset": "custom", "custom_domain": "https://127.0.0.1"}, {"token": WECOM_KEY})

    # Custom domain valid URL
    c_settings, c_secrets = p.normalize(
        {"domain_preset": "custom", "custom_domain": "https://wecom.corp.example/"},
        {"token": WECOM_KEY},
    )
    assert c_settings["custom_domain"] == "https://wecom.corp.example"

    cfg = ChannelConfig(id="wc1", name="WeCom", provider="wecom", settings=settings, secrets=secrets)
    long_msg = NotificationMessage(title="测试告警", markdown="告警" * 3000, at="zhangsan")
    req = p.build_http(cfg, long_msg)
    assert isinstance(req, HttpPreparedRequest)
    assert req.url == f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={WECOM_KEY}"
    body = json.loads(req.body.decode("utf-8"))
    content = body["markdown"]["content"]
    assert len(content.encode("utf-8")) <= 4096
    assert content.endswith("…<@zhangsan>")

    assert p.parse_http_response(200, {}, '{"errcode":0,"errmsg":"ok"}').ok is True
    r429 = p.parse_http_response(429, {"Retry-After": "12"}, '{"errcode":45009,"errmsg":"limit"}')
    assert r429.ok is False and r429.retryable is True and r429.retry_after == 12.0
    r400 = p.parse_http_response(200, {}, '{"errcode":93000,"errmsg":"invalid key"}')
    assert r400.ok is False and r400.retryable is False


# ---------------------------------------------------------------------------
# Provider 2: Feishu / Lark
# ---------------------------------------------------------------------------
def test_feishu_provider_lark_signing_colors_and_at(monkeypatch):
    p = get_provider("feishu")
    assert p.default_rate_limit == RateLimit(100, 60.0, burst_limit=5, burst_window_seconds=1.0)

    settings, secrets = p.normalize(
        {"domain_preset": "feishu", "at": "ou_123,all"},
        {"token": f"https://open.larksuite.com/open-apis/bot/v2/hook/{FEISHU_HOOK}", "sign_secret": "sec-feishu"},
    )
    assert settings["domain_preset"] == "lark"
    assert secrets == {"token": FEISHU_HOOK, "sign_secret": "sec-feishu"}

    monkeypatch.setattr(time, "time", lambda: 1700000000.9)
    cfg = ChannelConfig(id="fs1", name="Lark", provider="feishu", settings=settings, secrets=secrets)
    msg = NotificationMessage(title="Pod CrashLoop", markdown="详情内容", severity="warning")
    req = p.build_http(cfg, msg)
    assert req.url == f"https://open.larksuite.com/open-apis/bot/v2/hook/{FEISHU_HOOK}"
    payload = json.loads(req.body.decode("utf-8"))

    # Golden signature check
    expected_key = b"1700000000\nsec-feishu"
    expected_sign = base64.b64encode(hmac.new(expected_key, b"", hashlib.sha256).digest()).decode("utf-8")
    assert payload["timestamp"] == "1700000000"
    assert payload["sign"] == expected_sign
    assert compute_feishu_sign("1700000000", "sec-feishu") == expected_sign

    assert payload["card"]["header"]["title"]["content"] == "Pod CrashLoop"
    assert payload["card"]["header"]["template"] == "orange"
    md = payload["card"]["elements"][0]["content"]
    assert "<at id=all></at>" in md and "<at id=ou_123></at>" in md

    # Severity color mapping
    for sev, color in [("critical", "red"), ("firing", "red"), ("resolved", "green"), ("ok", "green"), ("info", "blue"), ("test", "blue")]:
        c_payload = json.loads(p.build_http(cfg, NotificationMessage(title="T", markdown="M", severity=sev)).body.decode("utf-8"))
        assert c_payload["card"]["header"]["template"] == color

    assert p.parse_http_response(200, {}, '{"StatusCode":0,"StatusMessage":"success"}').ok is True
    assert p.parse_http_response(200, {}, '{"code":0}').ok is True
    r_rl = p.parse_http_response(200, {}, '{"code":11232,"msg":"rate limited"}')
    assert r_rl.ok is False and r_rl.retryable is True


def test_feishu_parse_http_response_rejects_mixed_and_missing_status_codes():
    p = get_provider("feishu")

    # Both zero -> ok
    assert p.parse_http_response(200, {}, '{"code":0,"StatusCode":0,"StatusMessage":"success"}').ok is True
    assert p.parse_http_response(200, {}, '{"code":null,"StatusCode":0}').ok is True

    # Non-zero code alongside StatusCode: 0 -> MUST fail and report 19024
    mixed1 = p.parse_http_response(200, {}, '{"code":19024,"msg":"Key Words Not Found","StatusCode":0}')
    assert mixed1.ok is False
    assert mixed1.provider_code == "19024"
    assert "19024" in mixed1.message and "Key Words Not Found" in mixed1.message

    # Non-zero StatusCode alongside code: 0 -> MUST fail and report 19024
    mixed2 = p.parse_http_response(200, {}, '{"code":0,"msg":"ok","StatusCode":19024,"StatusMessage":"Key Words Not Found"}')
    assert mixed2.ok is False
    assert mixed2.provider_code == "19024"
    assert "19024" in mixed2.message and "Key Words Not Found" in mixed2.message

    # Retryable error code with StatusCode: 0 -> fail + retryable
    mixed_rl = p.parse_http_response(200, {}, '{"code":11232,"msg":"rate limit","StatusCode":0}')
    assert mixed_rl.ok is False and mixed_rl.retryable is True and mixed_rl.provider_code == "11232"

    # Missing or null status fields -> fail
    assert p.parse_http_response(200, {}, "{}").ok is False
    assert p.parse_http_response(200, {}, '{"code":null,"msg":"unknown"}').ok is False


# ---------------------------------------------------------------------------
# Provider 3: DingTalk
# ---------------------------------------------------------------------------
def test_dingding_provider_signing_title_and_at(monkeypatch):
    p = get_provider("dingding")
    assert p.default_rate_limit == RateLimit(20, 60.0)

    settings, secrets = p.normalize(
        {"domain_preset": "public", "at": "13800000000, all"},
        {"token": f"https://oapi.dingtalk.com/robot/send?access_token={DING_TOKEN}&timestamp=1&sign=old", "sign_secret": "SECsecret"},
    )
    assert secrets == {"token": DING_TOKEN, "sign_secret": "SECsecret"}

    monkeypatch.setattr(time, "time", lambda: 1700000000.123)
    cfg = ChannelConfig(id="dd1", name="Ding", provider="dingding", settings=settings, secrets=secrets)
    req = p.build_http(cfg, NotificationMessage(title="节点磁盘告警", markdown="磁盘使用率 92%"))

    parsed = urlsplit(req.url)
    qs = parse_qs(parsed.query)
    assert qs["access_token"] == [DING_TOKEN]
    assert qs["timestamp"] == ["1700000000123"]
    expected_msg = b"1700000000123\nSECsecret"
    expected_b64 = base64.b64encode(hmac.new(b"SECsecret", expected_msg, hashlib.sha256).digest()).decode("utf-8")
    assert unquote_plus(qs["sign"][0]) == expected_b64
    assert compute_dingding_sign("1700000000123", "SECsecret")

    payload = json.loads(req.body.decode("utf-8"))
    assert payload["markdown"]["title"] == "节点磁盘告警"
    assert payload["at"] == {"atMobiles": ["13800000000"], "isAtAll": True}
    assert "@13800000000" in payload["markdown"]["text"]

    # Fallback title
    fallback_payload = json.loads(p.build_http(cfg, NotificationMessage(title="", markdown="Hi")).body.decode("utf-8"))
    assert fallback_payload["markdown"]["title"] == "通知"
    assert p.parse_http_response(200, {}, '{"errcode":0,"errmsg":"ok"}').ok is True


# ---------------------------------------------------------------------------
# Provider 4: Slack
# ---------------------------------------------------------------------------
def test_slack_provider_block_kit_and_retry_after():
    p = get_provider("slack")
    assert p.default_rate_limit == RateLimit(60, 60.0, burst_limit=1, burst_window_seconds=1.0)

    settings, secrets = p.normalize(
        {"at": "U12345"},
        {"token": f"https://hooks.slack.com/services/{SLACK_TOKEN}"},
    )
    assert secrets == {"token": SLACK_TOKEN}

    cfg = ChannelConfig(id="sl1", name="Slack", provider="slack", settings=settings, secrets=secrets)
    msg = NotificationMessage(
        title="prod 指标告警",
        items=(
            AlertItem(summary="HighCPU,已持续5分钟", message="CPU 95%", status="firing", silence_url="https://kd.example/#/alarm/silence?id=1"),
            AlertItem(summary="DiskOK,小于1分钟", message="恢复正常", status="resolved"),
        ),
    )
    req = p.build_http(cfg, msg)
    assert req.url == f"https://hooks.slack.com/services/{SLACK_TOKEN}"
    payload = json.loads(req.body.decode("utf-8"))
    assert payload["blocks"][0]["type"] == "header"
    assert payload["blocks"][0]["text"]["text"] == "prod 指标告警"
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "【屏蔽】" in serialized
    assert "https://kd.example/#/alarm/silence?id=1" in serialized
    assert "<@U12345>" in serialized
    assert any(b["type"] == "divider" for b in payload["blocks"])

    assert p.parse_http_response(200, {}, "ok\n").ok is True
    r429 = p.parse_http_response(429, {"Retry-After": "7"}, "rate_limited")
    assert r429.ok is False and r429.retryable is True and r429.retry_after == 7.0
    r403 = p.parse_http_response(403, {}, "invalid_token")
    assert r403.ok is False and r403.retryable is False
    assert "slacksecretpart0123456789" in p.redact_values(cfg)


# ---------------------------------------------------------------------------
# Provider 5: Custom Webhook
# ---------------------------------------------------------------------------
def test_webhook_provider_ssrf_headers_hmac_and_envelope():
    p = get_provider("webhook")
    assert p.default_rate_limit == RateLimit(30, 60.0)

    # Disallow http when allow_http=False, allow when True
    with pytest.raises(ValueError):
        p.normalize({"url": "http://hooks.example.com/events", "allow_http": False}, {})
    with pytest.raises(ValueError):
        p.normalize({"url": "https://127.0.0.1/events"}, {})
    with pytest.raises(ValueError):
        p.normalize({"url": "https://hooks.example.com/events", "headers_json": '{"Host":"evil.com"}'}, {})
    with pytest.raises(ValueError):
        p.normalize({"url": "https://hooks.example.com/events", "headers_json": '{"X-Bad":"a\\r\\nb"}'}, {})

    settings, secrets = p.normalize(
        {
            "url": "http://hooks.example.com/events",
            "allow_http": True,
            "method": "PUT",
            "headers_json": '{"X-Env":"prod"}',
            "auth_header_name": "X-Api-Key",
            "hmac_header": "X-KubeDoor-Signature-256",
        },
        {"auth_header_value": "secret-api-key", "hmac_secret": "my-hmac-secret"},
    )
    cfg = ChannelConfig(id="wh1", name="WH", provider="webhook", settings=settings, secrets=secrets)
    msg = NotificationMessage(
        title="Deployment Updated",
        markdown="Updated image",
        env="prod-k8s",
        category="ops",
        severity="info",
        event_id="evt-1",
        timestamp="2026-11-27T10:00:00+00:00",
        links=({"label": "详情", "url": "https://kd.example/1"},),
    )
    req = p.build_http(cfg, msg)
    assert req.method == "PUT"
    assert req.allow_http is True
    assert req.allow_redirects is False
    assert req.headers["X-Env"] == "prod"
    assert req.headers["X-Api-Key"] == "secret-api-key"

    expected_sig = "sha256=" + hmac.new(b"my-hmac-secret", req.body, hashlib.sha256).hexdigest()
    assert req.headers["X-KubeDoor-Signature-256"] == expected_sig

    envelope = json.loads(req.body.decode("utf-8"))
    assert set(envelope) == {"schema_version", "event_id", "timestamp", "env", "category", "severity", "title", "body", "at", "items", "links"}
    assert envelope["schema_version"] == 1
    assert envelope["event_id"] == "evt-1"
    assert envelope["env"] == "prod-k8s"

    assert p.parse_http_response(204, {}, "").ok is True
    assert p.parse_http_response(503, {}, "unavailable").retryable is True
    assert p.parse_http_response(400, {}, "bad").retryable is False


# ---------------------------------------------------------------------------
# Provider 6: SMTP
# ---------------------------------------------------------------------------
def test_smtp_provider_normalize_escape_ssrf_and_send(monkeypatch):
    p = get_provider("smtp")
    assert p.default_rate_limit == RateLimit(10, 60.0)
    assert p.transport == "smtp"

    with pytest.raises(ValueError):
        p.normalize({"host": "127.0.0.1", "port": 465, "from_addr": "a@b.com", "to_addrs": "c@d.com"}, {})
    with pytest.raises(ValueError):
        p.normalize({"host": "smtp.example.com", "port": 465, "from_addr": "bad\r\n@b.com", "to_addrs": "c@d.com"}, {})

    settings, secrets = p.normalize(
        {
            "host": "smtp.example.com",
            "port": 465,
            "security": "ssl",
            "username": "bot@example.com",
            "from_addr": "bot@example.com",
            "to_addrs": "a@example.com, b@example.com",
        },
        {"password": "smtp-super-password"},
    )
    # Blank password reuses saved password
    _, reused_secrets = p.normalize(settings, {"password": ""}, saved_secrets=secrets)
    assert reused_secrets == {"password": "smtp-super-password"}

    sent_messages = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None, context=None):
            self.host, self.port, self.timeout, self.context = host, port, timeout, context
            self.logged_in = None

        def ehlo(self):
            pass

        def starttls(self, context=None):
            pass

        def login(self, user, pwd):
            self.logged_in = (user, pwd)

        def send_message(self, email_msg):
            sent_messages.append(email_msg)

        def quit(self):
            pass

    monkeypatch.setattr("kubedoor_tools.notify.providers.smtp.validate_host_port", lambda h, port: SimpleNamespace(host=h, port=port, pinned_ip="93.184.216.34"))
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)

    cfg = ChannelConfig(id="mail1", name="Mail", provider="smtp", settings=settings, secrets=secrets)
    xss_msg = NotificationMessage(
        title='<script>alert(1)</script>',
        markdown='Body <img src=x onerror=alert(1)>',
        env="prod",
        items=(AlertItem(summary="<b onclick=x>Alert</b>", message="desc", silence_url="javascript:alert(1)"),),
    )
    res = p.send_direct(cfg, xss_msg)
    assert res.ok is True
    assert len(sent_messages) == 1
    raw_email = sent_messages[0].as_string()
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in raw_email
    assert "javascript:alert(1)" not in raw_email

    # Auth error: not retryable and password redacted
    class AuthFailSMTP(FakeSMTP):
        def login(self, user, pwd):
            raise smtplib.SMTPAuthenticationError(535, f"auth failed for {pwd}".encode("utf-8"))

    monkeypatch.setattr(smtplib, "SMTP_SSL", AuthFailSMTP)
    auth_res = p.send_direct(cfg, xss_msg)
    assert auth_res.ok is False and auth_res.retryable is False
    assert "smtp-super-password" not in auth_res.message

    # Transient 4xx SMTP error: retryable=True
    class TempFailSMTP(FakeSMTP):
        def send_message(self, email_msg):
            raise smtplib.SMTPDataError(421, b"4.3.2 try again later")

    monkeypatch.setattr(smtplib, "SMTP_SSL", TempFailSMTP)
    temp_res = p.send_direct(cfg, xss_msg)
    assert temp_res.ok is False and temp_res.retryable is True

    # STARTTLS & none modes use smtplib.SMTP
    starttls_calls = []

    class FakePlainSMTP:
        def __init__(self, host, port, timeout=None):
            starttls_calls.append(("init", host, port))

        def ehlo(self):
            starttls_calls.append(("ehlo",))

        def starttls(self, context=None):
            starttls_calls.append(("starttls", context is not None))

        def send_message(self, email_msg):
            starttls_calls.append(("send", email_msg["Subject"]))

        def quit(self):
            starttls_calls.append(("quit",))

    monkeypatch.setattr(smtplib, "SMTP", FakePlainSMTP)
    tls_settings, _ = p.normalize(
        {"host": "smtp.example.com", "port": 587, "security": "starttls", "from_addr": "bot@example.com", "to_addrs": "a@example.com"},
        {},
    )
    tls_cfg = ChannelConfig(id="mail2", name="Mail2", provider="smtp", settings=tls_settings, secrets={})
    assert p.send_direct(tls_cfg, NotificationMessage(title="Ping", markdown="Pong")).ok is True
    assert ("starttls", True) in starttls_calls
    assert starttls_calls.count(("ehlo",)) == 2


# ---------------------------------------------------------------------------
# Render & Compat
# ---------------------------------------------------------------------------
def test_render_strips_font_html_and_compat_matches_legacy():
    msg = build_alert_message(
        [
            {
                "status": "故障",
                "summary": '<font color="#ff0000">HighMem,已持续10分钟</font>',
                "message": "Pod memory 95%<br>check",
                "severity": "warning",
                "silence_url": "https://kd.example/#/alarm/silence",
            }
        ],
        env="prod-k8s",
    )
    assert "<font" not in msg.title and "<font" not in msg.markdown
    assert msg.severity == "warning"
    assert "【屏蔽】" in msg.markdown

    evt = build_event_message("重启通知", '<font color="#00ff00">成功</font>', env="prod")
    assert evt.markdown == "成功"

    # Legacy compat checks
    assert normalize_legacy_token("wecom", f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={WECOM_KEY}") == WECOM_KEY
    assert normalize_at(13800000000) == "13800000000"
    u, payload = build_legacy_request("feishu", FEISHU_HOOK, "hello", "ou_1")
    assert u.endswith(FEISHU_HOOK)
    assert payload["card"]["header"]["title"]["content"] == "告警通知"
    assert parse_legacy_response("slack", 200, "ok") == (True, "发送成功")
    assert test_content.__test__ is False
