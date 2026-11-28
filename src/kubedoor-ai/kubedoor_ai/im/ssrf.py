"""Fail-closed SSRF validation for custom Feishu/Lark deployment domains."""
from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlsplit, urlunsplit

from ..domain import AIError

CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")
LOCAL_HOST_SUFFIXES = (".localhost", ".local", ".internal")
LOCAL_EXACT_HOSTS = frozenset({"localhost", "localhost.localdomain", "metadata.google.internal"})


def _split_env_list(*names: str) -> list[str]:
    items: list[str] = []
    for name in names:
        raw = os.getenv(name, "")
        for part in raw.split(","):
            cleaned = part.strip()
            if cleaned:
                items.append(cleaned)
    return items


def _allowed_hosts() -> list[str]:
    return [item.lower() for item in _split_env_list("IM_ALLOWED_HOSTS", "NOTIFY_ALLOWED_HOSTS")]


def _allowed_cidrs() -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    nets: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for item in _split_env_list("IM_ALLOWED_PRIVATE_CIDRS", "NOTIFY_ALLOWED_PRIVATE_CIDRS"):
        try:
            nets.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            continue
    return nets


def _host_matches_allowlist(hostname: str, port: int | None, allowlist: list[str]) -> bool:
    host = hostname.lower().rstrip(".")
    host_port = f"{host}:{port}" if port is not None else host
    for entry in allowlist:
        pattern = entry.rstrip(".")
        if pattern.startswith("*."):
            suffix = pattern[1:]
            if host.endswith(suffix) and len(host) > len(suffix):
                return True
        elif pattern in (host, host_port):
            return True
    return False


def _unwrap_ip(raw_ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    if isinstance(raw_ip, ipaddress.IPv6Address) and raw_ip.ipv4_mapped is not None:
        return raw_ip.ipv4_mapped
    return raw_ip


def _ip_in_cidrs(ip: ipaddress.IPv4Address | ipaddress.IPv6Address, cidrs: list[ipaddress.IPv4Network | ipaddress.IPv6Network]) -> bool:
    unwrapped = _unwrap_ip(ip)
    return any(unwrapped.version == net.version and unwrapped in net for net in cidrs)


def _is_blocked_ip(raw_ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    ip = _unwrap_ip(raw_ip)
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return True
    if isinstance(ip, ipaddress.IPv4Address) and ip in CGNAT_NETWORK:
        return True
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.sixtofour is not None and _is_blocked_ip(ip.sixtofour):
            return True
        if ip.teredo is not None and (_is_blocked_ip(ip.teredo[0]) or _is_blocked_ip(ip.teredo[1])):
            return True
    return False


def _parse_ip_literal(hostname: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(hostname.strip("[]"))
    except ValueError:
        return None


def validate_custom_domain(url: str) -> str:
    """Validate and normalize a custom Feishu/Lark domain with fail-closed SSRF guards.

    Blocks RFC1918, loopback, link-local (169.254.169.254), CGNAT (100.64.0.0/10),
    multicast/reserved/unspecified, IPv4-mapped IPv6, userinfo, query/fragment, and
    non-HTTPS unless explicitly allowlisted via IM_ALLOWED_* or NOTIFY_ALLOWED_* env vars.
    """
    if not isinstance(url, str):
        raise AIError("自定义飞书域名必须为有效的 HTTPS URL", 400, "invalid_custom_domain")
    value = url.strip()
    if not value or len(value) > 2048 or any(c.isspace() or ord(c) < 32 for c in value) or "\\" in value:
        raise AIError("自定义飞书域名包含非法空白或控制字符", 400, "invalid_custom_domain")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise AIError("自定义飞书域名 URL 格式无效", 400, "invalid_custom_domain") from None

    if parsed.scheme not in ("https", "http"):
        raise AIError("自定义飞书域名仅支持 https://（显式白名单内网地址除外）", 400, "invalid_custom_domain")
    if parsed.username is not None or parsed.password is not None:
        raise AIError("自定义飞书域名不能包含账号密码信息", 400, "invalid_custom_domain")
    if parsed.query or parsed.fragment:
        raise AIError("自定义飞书域名不能包含查询参数或片段", 400, "invalid_custom_domain")
    hostname = (parsed.hostname or "").strip()
    if not hostname:
        raise AIError("自定义飞书域名缺少主机名", 400, "invalid_custom_domain")

    allowed_hosts = _allowed_hosts()
    allowed_cidrs = _allowed_cidrs()
    host_allowlisted = _host_matches_allowlist(hostname, port, allowed_hosts)

    ip_literal = _parse_ip_literal(hostname)
    resolved_ips: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []

    if ip_literal is not None:
        resolved_ips = [ip_literal]
    else:
        lowered = hostname.lower().rstrip(".")
        if (lowered in LOCAL_EXACT_HOSTS or any(lowered.endswith(suffix) for suffix in LOCAL_HOST_SUFFIXES)) and not host_allowlisted:
            raise AIError("自定义飞书域名禁止指向本地或内网保留域名", 400, "invalid_custom_domain")
        lookup_port = port or (443 if parsed.scheme == "https" else 80)
        try:
            infos = socket.getaddrinfo(hostname, lookup_port, type=socket.SOCK_STREAM)
        except OSError:
            raise AIError("无法解析自定义飞书域名，请检查域名与 DNS", 400, "invalid_custom_domain") from None
        for info in infos:
            addr = info[4][0]
            try:
                resolved_ips.append(ipaddress.ip_address(addr.split("%", 1)[0]))
            except ValueError:
                continue
        if not resolved_ips:
            raise AIError("自定义飞书域名未解析到有效 IP 地址", 400, "invalid_custom_domain")

    all_in_allowed_cidrs = bool(resolved_ips) and all(_ip_in_cidrs(ip, allowed_cidrs) for ip in resolved_ips)
    for ip in resolved_ips:
        if _is_blocked_ip(ip) and not (host_allowlisted or _ip_in_cidrs(ip, allowed_cidrs)):
            raise AIError(
                "自定义飞书域名解析到受限内网或保留地址，已拦截（如需私有化放行请配置 IM_ALLOWED_PRIVATE_CIDRS / IM_ALLOWED_HOSTS）",
                400,
                "invalid_custom_domain",
            )

    if parsed.scheme != "https" and not (host_allowlisted or all_in_allowed_cidrs):
        raise AIError("自定义飞书域名必须使用 https://", 400, "invalid_custom_domain")

    clean_path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, clean_path, "", ""))
