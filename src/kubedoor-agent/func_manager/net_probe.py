"""从 kubedoor-agent 所在 Pod 发起的网络测试:域名解析、TCP 端口连通性

- 解析:和在 Pod 里 ping / curl 一样走系统解析器(getaddrinfo,读 /etc/resolv.conf 的 nameserver、search 域、ndots),
  看到的就是业务 Pod 看到的结果。CoreDNS 管理页「检查」每秒调一次,直到解析到期望的 IP。
- TCP:相当于 nc -zv host port,对解析出的每个地址各连一次,报告是否连通、耗时、源地址。
"""

import asyncio
import errno
import ipaddress
import re
import socket
import time
from concurrent.futures import ThreadPoolExecutor

from aiohttp import web
from loguru import logger

RESOLV_CONF = "/etc/resolv.conf"
RESOLVE_TIMEOUT = 5
CONNECT_TIMEOUT = 5
MAX_NAMES = 10
MAX_ADDRESSES = 8
FAMILIES = {"4": socket.AF_INET, "6": socket.AF_INET6, "any": socket.AF_UNSPEC}
_LABEL = re.compile(r"^[A-Za-z0-9_]([A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?$")

# getaddrinfo 是阻塞调用,放线程里做。单独一个小线程池:DNS 不通时卡住的线程不会占满默认线程池
# (K8S 大响应的反序列化也用默认线程池),排不上队的解析由 wait_for 超时返回
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="net-probe")
_source_ip = None

_GAI_ERRORS = {}
for _name, _code, _text in (
    ("EAI_NONAME", "not_found", "域名不存在"),
    ("EAI_AGAIN", "temporary", "DNS 暂时无法解析(超时或 DNS 服务出错)"),
    ("EAI_FAIL", "error", "DNS 解析失败"),
    ("EAI_NODATA", "no_data", "域名没有这种类型的记录"),
):
    # 有的平台上几个错误码是同一个值(Windows 的 EAI_NONAME == EAI_NODATA),先登记的优先
    if hasattr(socket, _name):
        _GAI_ERRORS.setdefault(getattr(socket, _name), (_code, _text))
_UNREACHABLE = {errno.EHOSTUNREACH: "没有到目标主机的路由", errno.ENETUNREACH: "网络不可达"}


def _fail(message, status=400):
    return web.json_response({"success": False, "error": message}, status=status)


def valid_host(name):
    """域名(可以是单标签,和 ping 一样会补 search 域)或 IP"""
    if not isinstance(name, str) or not name:
        return False
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    bare = name[:-1] if name.endswith(".") else name
    return 0 < len(bare) <= 253 and all(_LABEL.match(label) for label in bare.split("."))


def resolv_conf():
    """/etc/resolv.conf 里的 nameserver 和 search 域,页面上说明是从哪里解析的"""
    nameservers, search = [], []
    try:
        with open(RESOLV_CONF, encoding="utf-8") as file:
            for line in file:
                parts = line.split()
                if len(parts) >= 2 and parts[0] == "nameserver":
                    nameservers.append(parts[1])
                elif parts and parts[0] in ("search", "domain"):
                    search = parts[1:]
    except OSError:
        pass
    return {"nameservers": nameservers, "search": search}


async def _getaddrinfo(host, family=socket.AF_UNSPEC):
    loop = asyncio.get_running_loop()
    infos = await asyncio.wait_for(
        loop.run_in_executor(_executor, socket.getaddrinfo, host, None, family, socket.SOCK_STREAM),
        RESOLVE_TIMEOUT,
    )
    ips = []
    for info in infos:
        ip = info[4][0]
        if ip not in ips:
            ips.append(ip)
    return ips


async def resolve(name, family=socket.AF_UNSPEC):
    """返回 {ips, code, error, ms}

    code:ok / not_found(域名不存在)/ no_data(没有这种类型的记录)/ temporary / timeout / error。
    「检查停用的记录是否生效」时,not_found、no_data 算不再解析到,temporary、timeout 不能下结论
    """
    started = time.monotonic()
    result = {"ips": [], "code": "ok", "error": ""}
    try:
        result["ips"] = await _getaddrinfo(name, family)
    except asyncio.TimeoutError:
        result.update(code="timeout", error=f"{RESOLVE_TIMEOUT} 秒内没有解析完成")
    except socket.gaierror as exc:
        code, text = _GAI_ERRORS.get(exc.errno) or ("error", str(exc))
        result.update(code=code, error=text)
    except (OSError, UnicodeError) as exc:
        result.update(code="error", error=str(exc) or type(exc).__name__)
    result["ms"] = round((time.monotonic() - started) * 1000)
    return result


async def source():
    """发起测试的 Pod:容器主机名就是 Pod 名,Pod IP 在 /etc/hosts 里"""
    global _source_ip
    pod = socket.gethostname()
    if not _source_ip:
        try:
            ips = await _getaddrinfo(pod, socket.AF_UNSPEC)
            _source_ip = ips[0] if ips else ""
        except Exception:
            pass
    return {"pod": pod, "ip": _source_ip or "", **resolv_conf()}


async def _connect(ip, port):
    started = time.monotonic()
    attempt = {"ip": ip, "ok": False, "error": "", "local": ""}
    writer = None
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), CONNECT_TIMEOUT)
        attempt["ok"] = True
        local = writer.get_extra_info("sockname")
        attempt["local"] = local[0] if local else ""
    except asyncio.TimeoutError:
        attempt["error"] = f"{CONNECT_TIMEOUT} 秒内连不上(网络不通,或被防火墙 / 安全组拦截)"
    except ConnectionRefusedError:
        attempt["error"] = "连接被拒绝(端口没有监听)"
    except ConnectionResetError:
        attempt["error"] = "连接被重置"
    except OSError as exc:
        attempt["error"] = _UNREACHABLE.get(exc.errno) or str(exc) or type(exc).__name__
    finally:
        if writer is not None:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), 1)
            except Exception:
                pass
    attempt["ms"] = round((time.monotonic() - started) * 1000)
    return attempt


async def tcp_check(host, port):
    """相当于 nc -zv host port:域名先解析,对每个地址各连一次(最多 MAX_ADDRESSES 个)"""
    try:
        addresses, dns = [str(ipaddress.ip_address(host))], None
    except ValueError:
        dns = await resolve(host)
        addresses = dns["ips"][:MAX_ADDRESSES]
    attempts = list(await asyncio.gather(*(_connect(ip, port) for ip in addresses)))
    return {
        "host": host,
        "port": port,
        "dns": dns,
        "attempts": attempts,
        "ok": any(attempt["ok"] for attempt in attempts),
    }


# ---------------------------------------------------------------------------------------------- 接口
async def handle_resolve(request):
    """GET ?names=a.example.com,b&family=4|6|any"""
    names = []
    for name in (request.query.get("names") or "").split(","):
        name = name.strip()
        if name and name not in names:
            names.append(name)
    if not names:
        return _fail("缺少要解析的域名 names")
    if len(names) > MAX_NAMES:
        return _fail(f"一次最多解析 {MAX_NAMES} 个域名")
    invalid = [name for name in names if not valid_host(name)]
    if invalid:
        return _fail(f"域名格式不正确: {', '.join(invalid)}")
    family = FAMILIES.get(request.query.get("family") or "any")
    if family is None:
        return _fail("family 只能是 4、6 或 any")
    answers = await asyncio.gather(*(resolve(name, family) for name in names))
    return web.json_response({"success": True, "data": {
        "source": await source(), "results": dict(zip(names, answers))}})


async def handle_tcp(request):
    """GET ?host=db.example.com&port=3306"""
    host = (request.query.get("host") or "").strip()
    if not valid_host(host):
        return _fail("主机格式不正确,填域名或 IP")
    try:
        port = int(request.query.get("port") or "")
    except ValueError:
        port = 0
    if not 0 < port < 65536:
        return _fail("端口要在 1 ~ 65535 之间")
    result = await tcp_check(host, port)
    logger.info(f"[net-probe] tcp {host}:{port} -> {'通' if result['ok'] else '不通'}")
    return web.json_response({"success": True, "data": {"source": await source(), **result}})
