"""对象存储上传地址（jmap dump、jstack、JFR 文件上传用）从控制端带到 pod-mgr。

master 每次把 /api/pod/* 转给 Agent 时，在 WebSocket request 消息里带上通用配置里的地址（顶层字段 oss_url，
没配置时是空串）；Agent 主进程转给本机 pod-mgr 时放进请求头 X-Kubedoor-Oss-Url，pod-mgr 优先用它，
改了通用配置所有集群马上生效。旧 master 不带这个字段，pod-mgr 继续用安装时写进 Secret 的环境变量 OSS_URL。

地址里可能有账号密码：不能写进日志，日志里用 masked。只用标准库，主进程和 pod-mgr 共用。
"""

from urllib.parse import quote, urlsplit, urlunsplit

HEADER = "X-Kubedoor-Oss-Url"
POD_PREFIX = "/api/pod/"
POD_MGR = "http://127.0.0.1:81"  # 本机 pod-mgr（FastAPI）
AGENT = "https://127.0.0.1"  # 本进程的 HTTPS 服务
NOT_CONFIGURED = "控制端没有配置对象存储上传地址，请在系统管理 → 通用配置填写"
NO_ENV = "没有配置对象存储上传地址（控制端没有带上地址，Agent 的环境变量 OSS_URL 也是空的）"
# 请求头只能放可见 ASCII：其他字符（中文、空白、控制字符等）按 URL 的规则编码，已有的 %XX 和 URL 的保留字符不动
_SAFE = "!#$%&'()*+,/:;=?@[]~"


def local_request(message):
    """master 发来的 request 消息 → (本机地址, 要加的请求头)。

    /api/pod/* 交给 pod-mgr：消息里有 oss_url 键（新 master，即使是空串）就带上请求头；其余交给本进程的 HTTPS 服务。
    """
    path = message["path"]
    if not path.startswith(POD_PREFIX):
        return AGENT + path, {}
    headers = {}
    if "oss_url" in message:
        value = message["oss_url"]
        headers[HEADER] = quote(value.strip() if isinstance(value, str) else "", safe=_SAFE)
    return POD_MGR + path, headers


def resolve(headers, fallback):
    """pod-mgr 这次请求用的上传地址，返回 (地址, 原因)：地址为空时原因说明为什么（给页面看）。

    有请求头（新 master）用请求头，空串表示控制端没有配置；没有请求头（旧 master）用环境变量 OSS_URL。
    """
    value = headers.get(HEADER)
    if value is not None:
        value = value.strip().rstrip("/")
        return (value, "") if value else ("", NOT_CONFIGURED)
    value = (fallback or "").strip().rstrip("/")
    return (value, "") if value else ("", NO_ENV)


def masked(url):
    """日志里显示的地址：密码换成 ******"""
    try:
        parts = urlsplit(url)
        password = parts.password
    except ValueError:
        return "<对象存储地址>"
    if password is None:
        return url
    userinfo, host = parts.netloc.rsplit("@", 1)
    return urlunsplit((parts.scheme, f"{userinfo.split(':', 1)[0]}:******@{host}", parts.path, parts.query, parts.fragment))
