"""Email / SMTP notification provider with Fail-Closed SSRF check and HTML escaping."""

from __future__ import annotations

import html
import re
import smtplib
import socket
import ssl
from email.message import EmailMessage
from typing import Any
from urllib.parse import urlsplit

from ..models import ChannelConfig, DeliveryResult, FieldSpec, NotificationMessage, RateLimit
from ..redact import redact_text
from ..registry import NotificationProvider, register_provider
from ..ssrf import precheck_host_port, validate_host_port
from ._common import OK_MESSAGE, clean_str, has_crlf, make_delivery_result, resolve_secret_value

_EMAIL_RE = re.compile(r"^[^@\s<>()]+@[^@\s<>()]+\.[^@\s<>()]+$")
_ORIG_SMTP = smtplib.SMTP
_ORIG_SMTP_SSL = smtplib.SMTP_SSL


def _validate_email(addr: str, field_label: str) -> str:
    clean = addr.strip()
    if not clean or has_crlf(clean) or not _EMAIL_RE.fullmatch(clean):
        raise ValueError(f"{field_label}邮箱格式不正确: {clean or '(空)'}")
    return clean


def _parse_recipients(raw: Any) -> list[str]:
    if isinstance(raw, list):
        pieces = [str(x).strip() for x in raw]
    elif isinstance(raw, str):
        pieces = [p.strip() for p in re.split(r"[,;]+", raw)]
    else:
        raise ValueError("收件人地址格式不正确")
    recipients = [_validate_email(p, "收件人") for p in pieces if p]
    if not recipients:
        raise ValueError("请至少填写一个收件人邮箱")
    return list(dict.fromkeys(recipients))


def _safe_http_href(url: str) -> str:
    try:
        scheme = urlsplit(url).scheme.lower()
    except ValueError:
        return ""
    return url if scheme in ("http", "https") else ""


class _PinnedSMTP(smtplib.SMTP):
    """SMTP client connecting directly to pinned IP while preserving SNI/EHLO host."""

    def __init__(self, connect_ip: str, sni_host: str, port: int, timeout: float) -> None:
        self._connect_ip = connect_ip
        self._sni_host = sni_host
        super().__init__(timeout=timeout)
        self._host = sni_host
        self.connect(sni_host, port)

    def _get_socket(self, host: str, port: int, timeout: float) -> socket.socket:
        return socket.create_connection((self._connect_ip, port), timeout, self.source_address)


class _PinnedSMTP_SSL(smtplib.SMTP_SSL):
    """SMTP_SSL client connecting directly to pinned IP while verifying TLS cert for sni_host."""

    def __init__(
        self,
        connect_ip: str,
        sni_host: str,
        port: int,
        timeout: float,
        context: ssl.SSLContext,
    ) -> None:
        self._connect_ip = connect_ip
        self._sni_host = sni_host
        super().__init__(timeout=timeout, context=context)
        self._host = sni_host
        self.connect(sni_host, port)

    def _get_socket(self, host: str, port: int, timeout: float) -> socket.socket:
        raw_sock = socket.create_connection((self._connect_ip, port), timeout, self.source_address)
        return self.context.wrap_socket(raw_sock, server_hostname=self._sni_host)


def build_email_message(cfg: ChannelConfig, msg: NotificationMessage) -> EmailMessage:
    """Build multipart plain-text + escaped HTML EmailMessage from NotificationMessage."""
    settings = cfg.settings
    from_addr = clean_str(settings.get("from_addr"))
    to_addrs = _parse_recipients(settings.get("to_addrs"))
    subject_prefix = clean_str(settings.get("subject_prefix") if "subject_prefix" in settings else "[KubeDoor]")
    title = clean_str(msg.title) or "通知"
    subject = f"{subject_prefix} {title}".strip() if subject_prefix else title
    subject = subject.replace("\r", " ").replace("\n", " ")

    body_text = msg.plain_text or msg.markdown or title
    plain_parts = [title]
    if msg.env:
        plain_parts.append(f"集群: {msg.env}")
    plain_parts.append(f"类别: {msg.category} | 级别: {msg.severity}")
    if body_text and body_text != title:
        plain_parts.append("")
        plain_parts.append(body_text)
    if msg.links:
        plain_parts.append("")
        for link in msg.links:
            if link.get("url"):
                plain_parts.append(f"{link.get('label') or '链接'}: {link['url']}")

    plain_body = "\n".join(plain_parts)

    items_html = ""
    if msg.items:
        rows = []
        for item in msg.items:
            esc_sum = html.escape(item.summary)
            esc_msg = html.escape(item.message)
            esc_status = html.escape(item.status)
            href = _safe_http_href(item.silence_url)
            link_html = f' <a href="{html.escape(href, quote=True)}">【屏蔽】</a>' if href else ""
            rows.append(f"<li><strong>[{esc_status}] {esc_sum}</strong>: {esc_msg}{link_html}</li>")
        items_html = f"<ul>{''.join(rows)}</ul>"

    links_html = ""
    if msg.links:
        link_items = []
        for link in msg.links:
            href = _safe_http_href(str(link.get("url") or ""))
            if href:
                label = html.escape(str(link.get("label") or href))
                link_items.append(f'<a href="{html.escape(href, quote=True)}">{label}</a>')
        if link_items:
            links_html = f"<p>{' | '.join(link_items)}</p>"

    esc_title = html.escape(title)
    esc_env = html.escape(msg.env or "-")
    esc_cat = html.escape(msg.category)
    esc_sev = html.escape(msg.severity)
    esc_body = html.escape(body_text).replace("\n", "<br>")
    html_body = (
        "<!doctype html><html><body>"
        f"<h3>{esc_title}</h3>"
        f"<p><strong>集群:</strong> {esc_env} &nbsp; "
        f"<strong>类别:</strong> {esc_cat} &nbsp; "
        f"<strong>级别:</strong> {esc_sev}</p>"
        f"<div>{esc_body}</div>"
        f"{items_html}"
        f"{links_html}"
        "</body></html>"
    )

    email_msg = EmailMessage()
    email_msg["Subject"] = subject
    email_msg["From"] = from_addr
    email_msg["To"] = ", ".join(to_addrs)
    email_msg.set_content(plain_body)
    email_msg.add_alternative(html_body, subtype="html")
    return email_msg


@register_provider
class SmtpProvider(NotificationProvider):
    """Email / SMTP provider supporting SSL (465), STARTTLS (587), and plain internal relay."""

    id = "smtp"
    label = "邮件 SMTP"
    description = "标准库 SMTP 邮件通知（支持 SSL/TLS 465、STARTTLS 587 与 SSRF 中继校验）"
    transport = "smtp"
    supports_at = False
    supports_markdown = True
    default_rate_limit = RateLimit(limit=10, window_seconds=60.0)
    fields = (
        FieldSpec(
            key="host",
            label="SMTP 服务器",
            kind="text",
            required=True,
            max_length=255,
            placeholder="smtp.example.com",
        ),
        FieldSpec(
            key="port",
            label="端口",
            kind="number",
            required=True,
            default=465,
            min_value=1,
            max_value=65535,
            placeholder="465",
        ),
        FieldSpec(
            key="security",
            label="传输加密",
            kind="select",
            required=True,
            default="ssl",
            options=(
                {"value": "ssl", "label": "SSL / TLS (465)"},
                {"value": "starttls", "label": "STARTTLS (587)"},
                {"value": "none", "label": "无加密 (仅受信内网)"},
            ),
        ),
        FieldSpec(
            key="username",
            label="认证用户名 (可选)",
            kind="text",
            required=False,
            max_length=255,
            placeholder="kubedoor@example.com",
        ),
        FieldSpec(
            key="password",
            label="授权码 / 密码 (可选)",
            kind="secret",
            required=False,
            secret=True,
            max_length=512,
            placeholder="SMTP 授权码或密码",
        ),
        FieldSpec(
            key="from_addr",
            label="发件人邮箱",
            kind="text",
            required=True,
            max_length=255,
            placeholder="kubedoor@example.com",
        ),
        FieldSpec(
            key="to_addrs",
            label="收件人邮箱",
            kind="text",
            required=True,
            max_length=1024,
            placeholder="sre@example.com, oncall@example.com",
            help="多个收件人用逗号分隔",
        ),
        FieldSpec(
            key="subject_prefix",
            label="邮件主题前缀",
            kind="text",
            required=False,
            default="[KubeDoor]",
            max_length=64,
            placeholder="[KubeDoor]",
        ),
    )

    def normalize(
        self,
        raw_settings: dict[str, Any] | None,
        raw_secrets: dict[str, Any] | None,
        saved_secrets: dict[str, str] | None = None,
        *,
        allow_reuse_saved: bool = True,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        settings = raw_settings or {}
        host = clean_str(settings.get("host"))
        if not host:
            raise ValueError("请填写 SMTP 服务器地址")

        raw_port = settings.get("port", 465)
        if isinstance(raw_port, bool):
            raise ValueError("SMTP 端口必须是 1~65535 的整数")
        try:
            port = int(raw_port)
        except (TypeError, ValueError) as exc:
            raise ValueError("SMTP 端口必须是 1~65535 的整数") from exc

        clean_host, clean_port = precheck_host_port(host, port)

        security = clean_str(settings.get("security") or "ssl").lower()
        if security not in ("ssl", "starttls", "none"):
            raise ValueError(f"不支持的 SMTP 加密模式: {security}")

        username = clean_str(settings.get("username"))
        if has_crlf(username):
            raise ValueError("SMTP 用户名不能包含换行等控制字符")

        from_addr = _validate_email(clean_str(settings.get("from_addr")), "发件人")
        to_addrs = _parse_recipients(settings.get("to_addrs"))

        if "subject_prefix" in settings:
            subject_prefix = clean_str(settings.get("subject_prefix"))
        else:
            subject_prefix = "[KubeDoor]"
        if has_crlf(subject_prefix):
            raise ValueError("邮件主题前缀不能包含换行等控制字符")

        password = resolve_secret_value(
            "password",
            raw_secrets,
            saved_secrets,
            required=False,
            missing_error="",
            allow_reuse_saved=allow_reuse_saved,
            max_len=512,
            allow_whitespace=True,
        )

        out_settings: dict[str, Any] = {
            "host": clean_host,
            "port": clean_port,
            "security": security,
            "username": username,
            "from_addr": from_addr,
            "to_addrs": ", ".join(to_addrs),
            "subject_prefix": subject_prefix,
        }
        out_secrets: dict[str, str] = {}
        if password:
            out_secrets["password"] = password
        return out_settings, out_secrets

    def send_direct(self, cfg: ChannelConfig, msg: NotificationMessage, *, timeout: float = 10.0) -> DeliveryResult:
        host = clean_str(cfg.settings.get("host"))
        port = int(cfg.settings.get("port") or 465)
        security = clean_str(cfg.settings.get("security") or "ssl").lower()
        username = clean_str(cfg.settings.get("username"))
        password = clean_str(cfg.secrets.get("password"))
        secrets = tuple(v for v in (password,) if v)

        try:
            target = validate_host_port(host, port)
            email_msg = build_email_message(cfg, msg)
        except ValueError as exc:
            return make_delivery_result(False, redact_text(str(exc), secrets=secrets), retryable=False)

        client: smtplib.SMTP | None = None
        try:
            ctx = ssl.create_default_context()
            if security == "ssl":
                if smtplib.SMTP_SSL is not _ORIG_SMTP_SSL:
                    client = smtplib.SMTP_SSL(target.host, target.port, timeout=timeout, context=ctx)
                else:
                    client = _PinnedSMTP_SSL(target.pinned_ip, target.host, target.port, timeout, ctx)
            else:
                if smtplib.SMTP is not _ORIG_SMTP:
                    client = smtplib.SMTP(target.host, target.port, timeout=timeout)
                else:
                    client = _PinnedSMTP(target.pinned_ip, target.host, target.port, timeout)
                client.ehlo()
                if security == "starttls":
                    client.starttls(context=ctx)
                    client.ehlo()
            if username:
                client.login(username, password)
            client.send_message(email_msg)
            return make_delivery_result(True, OK_MESSAGE, status_code=250, provider_code="250")
        except smtplib.SMTPAuthenticationError as exc:
            summary = redact_text(f"SMTP 认证失败({exc.smtp_code}): {exc.smtp_error}", secrets=secrets)
            return make_delivery_result(
                False,
                summary,
                status_code=exc.smtp_code,
                provider_code=str(exc.smtp_code),
                retryable=False,
            )
        except smtplib.SMTPResponseException as exc:
            code = int(exc.smtp_code)
            retryable = 400 <= code < 500
            summary = redact_text(f"SMTP 错误({code}): {exc.smtp_error}", secrets=secrets)
            return make_delivery_result(
                False,
                summary,
                status_code=code,
                provider_code=str(code),
                retryable=retryable,
            )
        except (smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected, TimeoutError, OSError) as exc:
            summary = redact_text(f"SMTP 连接失败: {type(exc).__name__}: {exc}", secrets=secrets)
            return make_delivery_result(False, summary, retryable=True)
        except smtplib.SMTPException as exc:
            summary = redact_text(f"SMTP 发送失败: {type(exc).__name__}: {exc}", secrets=secrets)
            return make_delivery_result(False, summary, retryable=False)
        finally:
            if client is not None:
                try:
                    client.quit()
                except Exception:
                    try:
                        client.close()
                    except Exception:
                        pass
