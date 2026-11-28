"""Fail-closed SSRF protection, DNS pre-resolution, IP pinning, and stdlib HTTP transport."""

from __future__ import annotations

import http.client
import inspect
import ipaddress
import os
import socket
import ssl
import threading
from dataclasses import dataclass
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

from .models import HttpPreparedRequest
from .redact import redact_text

_CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")
_THIS_NET_V4 = ipaddress.ip_network("0.0.0.0/8")
_NAT64_WELL_KNOWN = ipaddress.ip_network("64:ff9b::/96")
DEFAULT_DNS_TIMEOUT = 5.0


class SSRFError(ValueError):
    """Raised when a target URL or host violates SSRF / egress safety rules."""


@dataclass(frozen=True)
class ResolvedTarget:
    """Validated outbound target with pre-resolved IP addresses for pinning."""

    url: str
    scheme: str
    host: str
    port: int
    pinned_ip: str
    ips: tuple[str, ...]


def _parse_csv_env(name: str) -> tuple[str, ...]:
    raw = os.environ.get(name, "")
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _normalize_hosts(items: Iterable[str] | None) -> tuple[str, ...]:
    if items is None:
        return ()
    out: list[str] = []
    for item in items:
        host = str(item).strip().lower().rstrip(".")
        if host.startswith("[") and host.endswith("]"):
            host = host[1:-1]
        if host:
            out.append(host)
    return tuple(dict.fromkeys(out))


def _host_matches(host: str, patterns: Iterable[str]) -> bool:
    candidate = host.lower().rstrip(".")
    for pattern in patterns:
        if pattern.startswith("*."):
            suffix = pattern[1:]  # ".example.com"
            if candidate.endswith(suffix) and len(candidate) > len(suffix):
                return True
        elif candidate == pattern:
            return True
    return False


def _parse_cidrs(items: Iterable[str] | None) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    nets: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    if items is None:
        return ()
    for item in items:
        raw = str(item).strip()
        if not raw:
            continue
        try:
            nets.append(ipaddress.ip_network(raw, strict=False))
        except ValueError as exc:
            raise SSRFError(f"Invalid CIDR entry: {raw}") from exc
    return tuple(nets)


def _effective_private_hosts(allow_private_hosts: Iterable[str] | None) -> tuple[str, ...]:
    combined = list(_parse_csv_env("NOTIFY_ALLOWED_HOSTS"))
    if allow_private_hosts is not None:
        combined.extend(allow_private_hosts)
    return _normalize_hosts(combined)


def _effective_private_cidrs(
    allow_private_cidrs: Iterable[str] | None,
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    combined = list(_parse_csv_env("NOTIFY_ALLOWED_PRIVATE_CIDRS"))
    if allow_private_cidrs is not None:
        combined.extend(allow_private_cidrs)
    return _parse_cidrs(combined)


def _unwrap_ip(ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    while isinstance(ip_obj, ipaddress.IPv6Address) and ip_obj.ipv4_mapped is not None:
        ip_obj = ip_obj.ipv4_mapped
    if isinstance(ip_obj, ipaddress.IPv6Address) and ip_obj.sixtofour is not None:
        return _unwrap_ip(ip_obj.sixtofour)
    return ip_obj


def _ip_in_cidrs(
    raw_ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
    unwrapped_ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
    cidrs: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    for net in cidrs:
        if raw_ip.version == net.version and raw_ip in net:
            return True
        if unwrapped_ip.version == net.version and unwrapped_ip in net:
            return True
    return False


def _validate_ip_address(
    ip_str: str,
    host: str,
    *,
    private_cidrs: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
    host_allowed_private: bool,
) -> str:
    clean_ip = ip_str.strip()
    if "%" in clean_ip:
        # Scoped link-local IPv6 addresses are inherently non-public local interfaces.
        clean_ip_no_zone = clean_ip.split("%", 1)[0]
        try:
            scoped_obj = ipaddress.ip_address(clean_ip_no_zone)
        except ValueError as exc:
            raise SSRFError(f"Invalid IP address for host {host}") from exc
        if not _ip_in_cidrs(scoped_obj, _unwrap_ip(scoped_obj), private_cidrs):
            raise SSRFError(f"Blocked scoped link-local IP for host {host}")
        return str(scoped_obj)

    try:
        raw_ip = ipaddress.ip_address(clean_ip)
    except ValueError as exc:
        raise SSRFError(f"Invalid resolved IP address for host {host}") from exc

    unwrapped = _unwrap_ip(raw_ip)

    if _ip_in_cidrs(raw_ip, unwrapped, private_cidrs):
        return str(raw_ip)

    # Check NAT64 embedded IPv4 addresses (64:ff9b::/96).
    if isinstance(unwrapped, ipaddress.IPv6Address) and unwrapped in _NAT64_WELL_KNOWN:
        embedded_v4 = ipaddress.IPv4Address(int(unwrapped) & 0xFFFFFFFF)
        _validate_ip_address(
            str(embedded_v4),
            host,
            private_cidrs=private_cidrs,
            host_allowed_private=host_allowed_private,
        )
        return str(raw_ip)

    if unwrapped.is_link_local or unwrapped.is_multicast or unwrapped.is_unspecified:
        raise SSRFError(f"Blocked unsafe IP {unwrapped} for host {host}")

    if isinstance(unwrapped, ipaddress.IPv4Address) and unwrapped in _THIS_NET_V4:
        raise SSRFError(f"Blocked unsafe IP {unwrapped} for host {host}")

    is_cgnat = isinstance(unwrapped, ipaddress.IPv4Address) and unwrapped in _CGNAT_NET
    is_private_or_loopback = unwrapped.is_private or unwrapped.is_loopback or is_cgnat

    if unwrapped.is_reserved and not is_cgnat and not host_allowed_private:
        raise SSRFError(f"Blocked reserved IP {unwrapped} for host {host}")

    if is_private_or_loopback and not host_allowed_private:
        raise SSRFError(f"Blocked private/loopback/CGNAT IP {unwrapped} for host {host}")

    return str(raw_ip)


def _has_control_or_space(value: str) -> bool:
    return any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in value)


def _parse_ip_literal(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return None


def _parse_url_parts(
    url: str,
    *,
    allowed_hosts: Iterable[str] | None = None,
    allow_http: bool = False,
) -> tuple[str, str, int]:
    if not isinstance(url, str) or not url:
        raise SSRFError("Target URL must be a non-empty string")
    if _has_control_or_space(url):
        raise SSRFError("Target URL cannot contain whitespace or control characters")
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise SSRFError("Malformed target URL") from exc

    scheme = parts.scheme.lower()
    allowed_schemes = ("https", "http") if allow_http else ("https",)
    if scheme not in allowed_schemes:
        raise SSRFError(f"Disallowed URL scheme: {scheme or '(empty)'}")

    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise SSRFError("Target URL cannot contain userinfo credentials")

    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise SSRFError("Target URL is missing hostname")

    try:
        explicit_port = parts.port
    except ValueError as exc:
        raise SSRFError("Invalid port in target URL") from exc
    port = explicit_port if explicit_port is not None else (443 if scheme == "https" else 80)
    if port < 1 or port > 65535:
        raise SSRFError("Target port out of range")

    if allowed_hosts is not None:
        normalized = _normalize_hosts(allowed_hosts)
        if not _host_matches(host, normalized):
            raise SSRFError(f"Target host {host} is not in allowed_hosts")

    return scheme, host, port


def precheck_target_url(
    url: str,
    *,
    allowed_hosts: Iterable[str] | None = None,
    allow_http: bool = False,
    allow_private_cidrs: Iterable[str] | None = None,
    allow_private_hosts: Iterable[str] | None = None,
) -> tuple[str, str, int]:
    """Static URL & IP-literal SSRF precheck without performing network DNS lookups."""
    scheme, host, port = _parse_url_parts(url, allowed_hosts=allowed_hosts, allow_http=allow_http)
    private_hosts = _effective_private_hosts(allow_private_hosts)
    private_cidrs = _effective_private_cidrs(allow_private_cidrs)
    host_allowed_private = _host_matches(host, private_hosts)

    if (host == "localhost" or host.endswith(".localhost")) and not host_allowed_private:
        raise SSRFError(f"Blocked loopback hostname: {host}")

    literal = _parse_ip_literal(host)
    if literal is not None:
        _validate_ip_address(
            str(literal),
            host,
            private_cidrs=private_cidrs,
            host_allowed_private=host_allowed_private,
        )
    return scheme, host, port


def _effective_dns_timeout(dns_timeout: float | None = None) -> float:
    if dns_timeout is not None:
        return max(float(dns_timeout), 0.01)
    raw = os.environ.get("NOTIFY_DNS_TIMEOUT", "").strip()
    if raw:
        try:
            val = float(raw)
            if val > 0:
                return val
        except ValueError:
            pass
    return DEFAULT_DNS_TIMEOUT


def _resolve_default_getaddrinfo(host: str, port: int, timeout: float) -> Any:
    done = threading.Event()
    box: list[Any] = [None, None]

    def _worker() -> None:
        try:
            box[0] = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except BaseException as exc:  # noqa: BLE001
            box[1] = exc
        finally:
            done.set()

    thread = threading.Thread(target=_worker, name="notify-dns-resolve", daemon=True)
    thread.start()
    if not done.wait(timeout):
        raise TimeoutError(f"DNS lookup timed out after {timeout:.2f}s")
    if box[1] is not None:
        raise box[1]
    return box[0]


def _resolve_ips(
    host: str,
    port: int,
    resolver: Callable[..., Any] | None,
    *,
    dns_timeout: float | None = None,
) -> list[str]:
    literal = _parse_ip_literal(host)
    if literal is not None and resolver is None:
        return [str(literal)]

    try:
        if resolver is None:
            infos = _resolve_default_getaddrinfo(host, port, _effective_dns_timeout(dns_timeout))
        else:
            try:
                sig = inspect.signature(resolver)
                params = sig.parameters
                accepts_varargs = any(
                    p.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
                    for p in params.values()
                )
                if accepts_varargs or "type" in params:
                    infos = resolver(host, port, type=socket.SOCK_STREAM)
                else:
                    infos = resolver(host, port)
            except (TypeError, ValueError):
                infos = resolver(host, port)
    except OSError as exc:
        raise SSRFError(f"Failed to resolve host {host}: {type(exc).__name__}") from exc

    ips: list[str] = []
    for entry in infos or ():
        if isinstance(entry, str):
            ips.append(entry)
        elif isinstance(entry, (tuple, list)) and len(entry) >= 5 and isinstance(entry[4], (tuple, list)):
            ips.append(str(entry[4][0]))
        else:
            raise SSRFError(f"Unexpected DNS resolver entry for host {host}")

    if not ips:
        raise SSRFError(f"No IP addresses resolved for host {host}")
    return ips


def validate_target_url(
    url: str,
    *,
    allowed_hosts: Iterable[str] | None = None,
    allow_http: bool = False,
    allow_private_cidrs: Iterable[str] | None = None,
    allow_private_hosts: Iterable[str] | None = None,
    resolver: Callable[..., Any] | None = None,
    dns_timeout: float | None = None,
) -> ResolvedTarget:
    """Validate URL scheme/host and resolve+check EVERY A/AAAA record against SSRF rules."""
    scheme, host, port = precheck_target_url(
        url,
        allowed_hosts=allowed_hosts,
        allow_http=allow_http,
        allow_private_cidrs=allow_private_cidrs,
        allow_private_hosts=allow_private_hosts,
    )
    private_hosts = _effective_private_hosts(allow_private_hosts)
    private_cidrs = _effective_private_cidrs(allow_private_cidrs)
    host_allowed_private = _host_matches(host, private_hosts)

    raw_ips = _resolve_ips(host, port, resolver, dns_timeout=dns_timeout)
    validated: list[str] = []
    for ip_str in raw_ips:
        validated.append(
            _validate_ip_address(
                ip_str,
                host,
                private_cidrs=private_cidrs,
                host_allowed_private=host_allowed_private,
            )
        )
    unique_ips = tuple(dict.fromkeys(validated))
    return ResolvedTarget(
        url=url,
        scheme=scheme,
        host=host,
        port=port,
        pinned_ip=unique_ips[0],
        ips=unique_ips,
    )


def precheck_host_port(
    host: str,
    port: int,
    *,
    allow_private_cidrs: Iterable[str] | None = None,
    allow_private_hosts: Iterable[str] | None = None,
) -> tuple[str, int]:
    """Static SSRF precheck for bare host:port targets (e.g. SMTP relays)."""
    if not isinstance(host, str) or not host:
        raise SSRFError("Target host must be a non-empty string")
    if _has_control_or_space(host):
        raise SSRFError("Target host cannot contain whitespace or control characters")
    if "://" in host or any(ch in host for ch in ("/", "@", "?", "#")):
        raise SSRFError("Target host must be a plain hostname or IP without scheme or path")
    clean = host.lower().rstrip(".")
    if clean.startswith("[") and clean.endswith("]"):
        clean = clean[1:-1]
    if not clean:
        raise SSRFError("Target host cannot be empty")
    if isinstance(port, bool) or not isinstance(port, int) or port < 1 or port > 65535:
        raise SSRFError("Target port must be an integer between 1 and 65535")

    private_hosts = _effective_private_hosts(allow_private_hosts)
    private_cidrs = _effective_private_cidrs(allow_private_cidrs)
    host_allowed_private = _host_matches(clean, private_hosts)

    if (clean == "localhost" or clean.endswith(".localhost")) and not host_allowed_private:
        raise SSRFError(f"Blocked loopback hostname: {clean}")

    literal = _parse_ip_literal(clean)
    if literal is not None:
        _validate_ip_address(
            str(literal),
            clean,
            private_cidrs=private_cidrs,
            host_allowed_private=host_allowed_private,
        )
    return clean, port


def validate_host_port(
    host: str,
    port: int,
    *,
    allow_private_cidrs: Iterable[str] | None = None,
    allow_private_hosts: Iterable[str] | None = None,
    resolver: Callable[..., Any] | None = None,
    dns_timeout: float | None = None,
) -> ResolvedTarget:
    """Validate and resolve a bare host:port target (e.g. SMTP relay) with SSRF rules."""
    clean_host, clean_port = precheck_host_port(
        host,
        port,
        allow_private_cidrs=allow_private_cidrs,
        allow_private_hosts=allow_private_hosts,
    )
    private_hosts = _effective_private_hosts(allow_private_hosts)
    private_cidrs = _effective_private_cidrs(allow_private_cidrs)
    host_allowed_private = _host_matches(clean_host, private_hosts)

    raw_ips = _resolve_ips(clean_host, clean_port, resolver, dns_timeout=dns_timeout)
    validated = [
        _validate_ip_address(
            ip_str,
            clean_host,
            private_cidrs=private_cidrs,
            host_allowed_private=host_allowed_private,
        )
        for ip_str in raw_ips
    ]
    unique_ips = tuple(dict.fromkeys(validated))
    return ResolvedTarget(
        url=f"tcp://{clean_host}:{clean_port}",
        scheme="tcp",
        host=clean_host,
        port=clean_port,
        pinned_ip=unique_ips[0],
        ips=unique_ips,
    )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPSConnection that connects directly to pinned_ip while verifying TLS SNI for sni_host."""

    def __init__(
        self,
        pinned_ip: str,
        sni_host: str,
        port: int,
        *,
        timeout: float,
        context: ssl.SSLContext,
    ) -> None:
        super().__init__(sni_host, port=port, timeout=timeout, context=context)
        self._pinned_ip = pinned_ip
        self._sni_host = sni_host

    def connect(self) -> None:
        sock = socket.create_connection((self._pinned_ip, self.port), self.timeout, self.source_address)
        if self._tunnel_host:
            self.sock = sock
            self._tunnel()
        self.sock = self._context.wrap_socket(sock, server_hostname=self._sni_host)


def send_pinned_http(
    req: HttpPreparedRequest,
    *,
    ssrf_check: bool = True,
    allowed_hosts: Iterable[str] | None = None,
    allow_http: bool = False,
    allow_private_cidrs: Iterable[str] | None = None,
    allow_private_hosts: Iterable[str] | None = None,
    resolver: Callable[..., Any] | None = None,
    ssl_context: ssl.SSLContext | None = None,
    dns_timeout: float | None = None,
) -> tuple[int, str, dict[str, str]]:
    """Execute HttpPreparedRequest pinned to validated IP, preserving Host + SNI, zero redirects."""
    effective_allow_http = bool(allow_http or req.allow_http)
    if ssrf_check:
        effective_dns_timeout = (
            min(_effective_dns_timeout(), max(float(req.timeout), 0.1))
            if dns_timeout is None
            else dns_timeout
        )
        target = validate_target_url(
            req.url,
            allowed_hosts=allowed_hosts,
            allow_http=effective_allow_http,
            allow_private_cidrs=allow_private_cidrs,
            allow_private_hosts=allow_private_hosts,
            resolver=resolver,
            dns_timeout=effective_dns_timeout,
        )
    else:
        scheme, host, port = _parse_url_parts(req.url, allowed_hosts=allowed_hosts, allow_http=effective_allow_http)
        target = ResolvedTarget(url=req.url, scheme=scheme, host=host, port=port, pinned_ip=host, ips=(host,))

    parts = urlsplit(req.url)
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"

    host_base = f"[{target.host}]" if ":" in target.host else target.host
    default_port = 443 if target.scheme == "https" else 80
    host_header = host_base if target.port == default_port else f"{host_base}:{target.port}"

    headers = {k: v for k, v in req.headers.items() if k.lower() != "host"}
    headers["Host"] = host_header

    if target.scheme == "https":
        ctx = ssl_context or ssl.create_default_context()
        conn: http.client.HTTPConnection = _PinnedHTTPSConnection(
            target.pinned_ip,
            target.host,
            target.port,
            timeout=req.timeout,
            context=ctx,
        )
    else:
        conn = http.client.HTTPConnection(target.pinned_ip, port=target.port, timeout=req.timeout)

    try:
        conn.request(req.method.upper(), path, body=req.body, headers=headers)
        resp = conn.getresponse()
        raw_body = resp.read()
        resp_headers = {k: v for k, v in resp.getheaders()}
        return resp.status, raw_body.decode("utf-8", errors="replace"), resp_headers
    except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
        raise OSError(redact_text(f"{type(exc).__name__}: {exc}", secrets=(req.url,))) from exc
    finally:
        conn.close()
