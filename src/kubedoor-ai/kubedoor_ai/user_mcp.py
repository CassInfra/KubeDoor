"""用户在网页 AI 助手「MCP 管理」里接入的外部 MCP 服务（SSE / Streamable HTTP）。

配置按 KubeDoor 账号隔离：地址和请求头用 AI_ENCRYPTION_KEY 加密存进 kubedoor_ai_mcp_servers，接口只回显
请求头的名称。对话时模型通过固定的 mcp_tool 调用（工具定义不随用户变化，见缓存不变量 I2），本轮可用的
服务和工具名放在本轮上下文块里，参数说明由模型按需 describe。工具列表缓存在库里，过期后在后台刷新，
不拖慢提问。调用和 K8S 操作共用网关的动作记录、审批与审计（gateway.invoke_mcp）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import socket
import ssl
import uuid
from urllib.parse import parse_qsl, urlsplit, urlunsplit

import asyncpg

from .domain import AIError, redact_values

LOGGER = logging.getLogger(__name__)

TRANSPORTS = {"streamable_http": "Streamable HTTP", "sse": "SSE"}
# 调用前要不要用户批准：readonly 声明了只读（readOnlyHint）的工具自动执行、其余要批准；never 全部自动执行；always 每次都要批准
APPROVALS = ("readonly", "never", "always")
MAX_SERVERS = 20
MAX_HEADERS = 20
MAX_TOOLS = 300
NAME = re.compile(r"[\w.\-]{1,40}")
HEADER_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]{1,128}")
# 工具名来自外部服务：不能带空白、尖括号和控制字符，否则可能冒充本轮上下文块的边界
TOOL_NAME = re.compile(r"[^\s<>\x00-\x1f\x7f]{1,128}")
# 由 MCP 客户端自己设置的请求头，用户填了会破坏协议
RESERVED_HEADERS = frozenset({"host", "content-length", "content-type", "accept", "connection", "transfer-encoding",
                              "upgrade", "te", "trailer", "keep-alive", "proxy-connection", "mcp-session-id",
                              "mcp-protocol-version", "last-event-id"})
# 名称像密钥的请求头和 URL 参数：它们的值不能出现在结果和日志里
SECRET_NAME = re.compile(r"auth|token|key|secret|passw|cookie|session|credential|sign", re.IGNORECASE)
CONNECT_TIMEOUT = 15     # 建立连接和握手
LIST_TIMEOUT = 30        # 测试连接、刷新工具列表的整体上限
CALL_TIMEOUT = 120       # 单次工具调用
REFRESH_SECONDS = 600    # 工具列表缓存多久之后在后台刷新
RESULT_LIMIT = 60000     # 返回给模型的工具结果最多多少字符
TURN_TOOL_NAMES = 100    # 本轮上下文块里每个服务最多列出的工具名
# 服务端明确拒绝、肯定没有执行的 JSON-RPC 错误；其余（超时、连接断开、内部错误）都可能已经执行
REJECTED_CODES = frozenset({-32700, -32600, -32601, -32602})
REQUEST_TIMEOUT = -32001


def short(text, limit):
    text = " ".join(text.split()) if isinstance(text, str) else ""
    return text if len(text) <= limit else text[:limit - 1] + "…"


def safe_url(url: str) -> str:
    """去掉查询参数（常带 key）后的地址，用于日志"""
    try:
        parsed = urlsplit(url)
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    except ValueError:
        return "[URL]"


def strip_url_queries(text: str) -> str:
    return re.sub(r"https?://[^\s<>\"']+", lambda match: safe_url(match.group()), text)


def secret_values(config: dict) -> tuple[str, ...]:
    """请求头和 URL 查询参数里的密钥：不能出现在工具结果、错误提示和日志里。

    名称像密钥的（Authorization、X-Api-Key、token 等）按值替换；其他的（X-Env: production、city=beijing）
    只替换像随机串的长值，免得结果里的普通词被打码。
    """
    values = []

    def add(name, value):
        if SECRET_NAME.search(name):
            values.extend([value] if len(value) >= 4 else [])
            # 「Bearer xxx」这类值，服务端回显的常常只有后面的 token
            values.extend(part for part in value.split() if len(part) >= 8)
        elif len(value) >= 16:
            values.append(value)

    for name, value in config.get("headers", {}).items():
        add(name, value)
    try:
        for name, value in parse_qsl(urlsplit(config.get("url", "")).query):
            add(name, value)
    except ValueError:
        pass
    # 先替换长的，免得短值把长值拆开
    return tuple(sorted(set(values), key=len, reverse=True))


def normalize_url(raw) -> str:
    message = "URL 必须是 http:// 或 https:// 开头的 MCP 服务地址，不能包含账号密码或 # 片段"
    if not isinstance(raw, str):
        raise AIError(message)
    value = raw.strip()
    if not value or len(value) > 2048 or any(c.isspace() or ord(c) < 32 for c in value) or "\\" in value:
        raise AIError(message)
    try:
        parsed = urlsplit(value)
        parsed.port  # 校验端口和 IPv6 地址格式
    except ValueError:
        raise AIError(message) from None
    if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.fragment):
        raise AIError(message)
    return value


def parse_headers(raw, saved=None) -> dict:
    """[{name, value}] → {name: value}；编辑时值留空表示沿用已保存的同名请求头（名称不区分大小写）"""
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise AIError("请求头格式无效")
    if len(raw) > MAX_HEADERS:
        raise AIError(f"请求头最多 {MAX_HEADERS} 个")
    kept = {name.lower(): value for name, value in (saved or {}).items()}
    result, seen = {}, set()
    for item in raw:
        if not isinstance(item, dict):
            raise AIError("请求头格式无效")
        name, value = item.get("name"), item.get("value")
        name = name.strip() if isinstance(name, str) else ""
        if not HEADER_NAME.fullmatch(name):
            raise AIError(f"请求头名称「{short(name, 40)}」无效：只能用字母、数字和 - _ 等符号")
        if name.lower() in RESERVED_HEADERS:
            raise AIError(f"请求头 {name} 由 MCP 客户端自己设置，不能填写")
        if name.lower() in seen:
            raise AIError(f"请求头 {name} 重复")
        seen.add(name.lower())
        value = value.strip() if isinstance(value, str) else value
        if value in (None, ""):
            if name.lower() not in kept:
                raise AIError(f"请填写请求头 {name} 的值")
            value = kept[name.lower()]
        if not isinstance(value, str) or len(value) > 8192 or any(c in value for c in "\r\n\0"):
            raise AIError(f"请求头 {name} 的值无效：不能换行，最多 8192 字符")
        result[name] = value
    return result


def parse_connection(body: dict, saved_headers=None) -> dict:
    transport = body.get("transport")
    if transport not in TRANSPORTS:
        raise AIError("连接方式只能是 SSE 或 Streamable HTTP")
    return {"transport": transport, "url": normalize_url(body.get("url")), "headers": parse_headers(body.get("headers"), saved_headers)}


def parse_name(raw) -> str:
    name = raw.strip() if isinstance(raw, str) else ""
    if not NAME.fullmatch(name):
        raise AIError("名称只能用 1–40 个字母、数字、汉字或 _ . -，AI 调用时用它指定服务")
    return name


def parse_approval(raw) -> str:
    if raw not in APPROVALS:
        raise AIError("调用审批只能是 readonly、never、always")
    return raw


def parse_enabled(raw) -> bool:
    if type(raw) is not bool:
        raise AIError("enabled 必须为布尔值")
    return raw


def needs_approval(approval: str, read_only: bool) -> bool:
    return approval == "always" or (approval == "readonly" and not read_only)


def tool_entry(tool) -> dict | None:
    """mcp_types.Tool → 缓存进库的工具说明；名字不合规的工具丢弃"""
    data = tool.model_dump(mode="json", by_alias=True, exclude_none=True) if hasattr(tool, "model_dump") else dict(tool)
    name = data.get("name")
    if not isinstance(name, str) or not TOOL_NAME.fullmatch(name):
        return None
    annotations = data.get("annotations") if isinstance(data.get("annotations"), dict) else {}
    schema = data.get("inputSchema") if isinstance(data.get("inputSchema"), dict) else {"type": "object"}
    title = data.get("title") or annotations.get("title")
    return {"name": name, "title": short(title, 200) or None, "description": short(data.get("description"), 8000),
            "input_schema": schema, "read_only": annotations.get("readOnlyHint") is True}


def server_summary(client) -> dict:
    info = getattr(client, "server_info", None)
    data = info.model_dump(mode="json", exclude_none=True) if hasattr(info, "model_dump") else {}
    summary = {"name": short(data.get("name"), 200), "title": short(data.get("title"), 200),
               "version": short(data.get("version"), 100), "instructions": short(getattr(client, "instructions", None), 4000),
               "protocol_version": getattr(client, "protocol_version", None)}
    return {key: value for key, value in summary.items() if value}


def call_content(result) -> tuple[str, bool, bool]:
    """CallToolResult → (给模型的文本, 是否截断, 是否工具报错)；图片、音频、二进制资源只写占位说明"""
    data = result.model_dump(mode="json", by_alias=True, exclude_none=True) if hasattr(result, "model_dump") else dict(result)
    parts = []
    for block in data.get("content") or []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            parts.append(str(block.get("text", "")))
        elif kind in ("image", "audio"):
            size = len(block.get("data") or "") * 3 // 4
            parts.append(f"[{'图片' if kind == 'image' else '音频'} {block.get('mimeType', '')}，约 {size // 1024 + 1} KB，没有提供给模型]")
        elif kind == "resource":
            resource = block.get("resource") if isinstance(block.get("resource"), dict) else {}
            if isinstance(resource.get("text"), str):
                parts.append(f"[资源 {resource.get('uri', '')}]\n{resource['text']}")
            else:
                parts.append(f"[二进制资源 {resource.get('uri', '')} {resource.get('mimeType', '')}，没有提供给模型]")
        elif kind == "resource_link":
            parts.append(f"[资源链接 {block.get('name', '')} {block.get('uri', '')}]")
        else:
            parts.append(json.dumps(block, ensure_ascii=False)[:2000])
    text = "\n".join(part for part in parts if part)
    if not text and data.get("structuredContent") is not None:
        text = json.dumps(data["structuredContent"], ensure_ascii=False)
    truncated = len(text) > RESULT_LIMIT
    return (text[:RESULT_LIMIT] if truncated else text), truncated, bool(data.get("isError"))


def turn_catalogue(servers: list[dict]) -> str:
    """本轮上下文块里的「用户接入的 MCP」：服务名和工具名，参数说明由模型按需 describe"""
    items = []
    for server in servers:
        item = {"server": server["name"]}
        names = [tool["name"] for tool in server["tools"]]
        if names:
            item["tools"] = names[:TURN_TOOL_NAMES]
            if len(names) > TURN_TOOL_NAMES:
                item["more"] = len(names) - TURN_TOOL_NAMES
        else:
            item["unavailable"] = "暂时连不上，没有工具列表"
        items.append(item)
    return json.dumps(items, ensure_ascii=False, separators=(",", ":"))


def frozen_fingerprint(server_id, revision) -> str:
    # 审批冻结的连接配置版本；沿用 credential_fingerprint 这个键，界面和历史里统一不显示
    return f"mcp:{server_id}:{revision}"


def changed_problem(row, preview) -> str | None:
    if not row:
        return "这个 MCP 已被删除，不能执行"
    if not row["enabled"]:
        return "这个 MCP 已停用，重新启用后请重新提问"
    if frozen_fingerprint(row["id"], row["revision"]) != preview.get("credential_fingerprint"):
        return "这个 MCP 的连接配置在审批期间改过，请重新提问"
    return None


def _walk(exc):
    """异常本身、ExceptionGroup 里的子异常和 cause / context 链"""
    seen, stack = set(), [exc]
    while stack and len(seen) < 64:
        current = stack.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        if isinstance(current, BaseExceptionGroup):
            stack.extend(current.exceptions)
        stack.extend((current.__cause__, current.__context__))


def classify_failure(exc, transport="streamable_http", status=None, secrets=()) -> tuple[str, str]:
    """连接或调用失败的 (类别, 给用户看的原因)；原因里不带 URL 查询参数和请求头的值。

    类别：rejected 服务端明确拒绝（JSON-RPC 参数错误、没有这个工具等，肯定没执行）、timeout、http、dns、tls、connect、other
    """
    import httpx2
    from mcp.shared.exceptions import MCPError

    errors = list(_walk(exc))

    def find(kind):
        return next((error for error in errors if isinstance(error, kind)), None)

    http_error = find(httpx2.HTTPStatusError)
    if http_error is not None:
        status = http_error.response.status_code
    other = TRANSPORTS["sse" if transport == "streamable_http" else "streamable_http"]
    if status in (401, 403):
        return "http", f"MCP 服务拒绝访问（HTTP {status}），请检查请求头里的鉴权信息（如 Authorization）"
    if status == 404:
        return "http", f"MCP 服务返回 HTTP 404，请检查 URL 路径；也可能是连接方式不对，可以换成 {other} 试试"
    if status in (400, 405, 406, 415):
        return "http", f"MCP 服务返回 HTTP {status}，请确认连接方式选对了（当前是 {TRANSPORTS.get(transport, transport)}，可以换成 {other} 试试）"
    if status == 429:
        return "http", "MCP 服务限流（HTTP 429），请稍后重试"
    if status is not None and status >= 500:
        return "http", f"MCP 服务暂时不可用（HTTP {status}）"
    if status is not None and status >= 400:
        return "http", f"MCP 服务拒绝了请求（HTTP {status}）"
    if find(socket.gaierror):
        return "dns", "无法解析 MCP 服务的域名，请检查 URL 里的主机名和 AI 服务所在环境的 DNS"
    if find(ssl.SSLError):
        return "tls", "TLS 握手失败或证书校验没通过（不支持自签证书），请检查 HTTPS 证书"
    mcp_error = find(MCPError)
    if find(httpx2.TimeoutException) or find(TimeoutError) or (mcp_error is not None and mcp_error.code == REQUEST_TIMEOUT):
        return "timeout", "MCP 服务响应超时，请检查网络连通性和服务的处理速度"
    if find(httpx2.ConnectError) or find(ConnectionError):
        return "connect", "无法连接 MCP 服务（连接被拒绝或网络不通），请检查 URL 和 AI 服务到该地址的网络"
    if mcp_error is not None:
        detail = short(redact_values(strip_url_queries(mcp_error.message or ""), secrets), 300)
        return ("rejected" if mcp_error.code in REJECTED_CODES else "other"), f"MCP 服务返回错误：{detail or mcp_error.code}"
    return "other", f"连接 MCP 服务失败（{type(exc).__name__}）"


def failure_message(exc, transport="streamable_http", status=None, secrets=()) -> str:
    return classify_failure(exc, transport, status, secrets)[1]


class HttpRecorder:
    """记下最后一个 HTTP 响应的状态码：Streamable HTTP 传输会把 401/404 等都报成笼统的错误"""
    def __init__(self):
        self.status = None

    async def response(self, response):
        self.status = response.status_code

    @property
    def error_status(self):
        return self.status if isinstance(self.status, int) and self.status >= 400 else None


def open_client(transport: str, url: str, headers: dict, recorder: HttpRecorder | None = None):
    """按连接方式建 fastmcp 客户端；HTTP 客户端由这里创建，以便记下响应状态码"""
    import httpx2
    from fastmcp import Client
    from fastmcp.client.transports import SSETransport, StreamableHttpTransport

    def http_client(headers=None, timeout=None, auth=None, **_):
        hooks = {"response": [recorder.response]} if recorder else {}
        # 和 MCP SDK 的默认客户端一致：连接 30 秒，读事件流 300 秒。不让 httpx 自动跳转（fastmcp 会要求打开）：
        # 跨域跳转时 httpx 只去掉 Authorization，X-Api-Key 这类请求头会被带到别的域名；同源跳转由 SDK 自己处理
        return httpx2.AsyncClient(headers=headers, timeout=timeout or httpx2.Timeout(30, read=300), auth=auth,
                                  event_hooks=hooks, follow_redirects=False)

    kind = SSETransport if transport == "sse" else StreamableHttpTransport
    return Client(kind(url, headers=dict(headers), httpx_client_factory=http_client),
                  timeout=CALL_TIMEOUT, init_timeout=CONNECT_TIMEOUT)


async def disconnect(client):
    """断开时出的错不影响已经拿到的结果"""
    try:
        await client.__aexit__(None, None, None)
    except Exception as exc:
        LOGGER.debug("关闭 MCP 连接时出错: %s", type(exc).__name__)


class McpServers:
    def __init__(self, store, cipher, client_factory=None):
        self.store, self.cipher = store, cipher
        self.client_factory = client_factory or open_client
        self.refreshing: dict[str, asyncio.Task] = {}

    # ---- 加密存储 ----

    def encrypt(self, server_id, url, headers) -> bytes:
        # 关联数据绑定记录 id：一条记录的密文不能挪到另一条上用
        return self.cipher.encrypt(f"mcp:{server_id}", {"url": url, "headers": headers})

    def decrypt(self, row) -> dict:
        return self.cipher.decrypt(f"mcp:{row['id']}", bytes(row["encrypted_config"]))

    def public(self, row) -> dict:
        try:
            config = self.decrypt(row)
            url, headers, broken = config["url"], [{"name": name} for name in config["headers"]], None
        except Exception:
            url, headers, broken = "", [], "保存的地址和请求头无法解密（AI_ENCRYPTION_KEY 变了？），请重新填写后保存"
        info = row.get("server_info") or {}
        return {"id": row["id"], "name": row["name"], "transport": row["transport"], "url": url, "headers": headers,
                "enabled": row["enabled"], "approval": row["approval"],
                "tools": [{"name": tool["name"], "title": tool.get("title"), "description": short(tool.get("description"), 300),
                           "read_only": bool(tool.get("read_only"))} for tool in row.get("tools") or []],
                "server_info": {**info, "instructions": short(info.get("instructions"), 500)} if info else None,
                "tools_at": row.get("tools_at"), "checked_at": row.get("checked_at"),
                "last_error": broken or row.get("last_error"),
                "created_at": row.get("created_at"), "updated_at": row.get("updated_at")}

    async def row(self, identity, server_id):
        row = await self.store.one("SELECT * FROM kubedoor_ai_mcp_servers WHERE id=$1::uuid AND username=$2", server_id, identity.username)
        if not row:
            raise AIError("MCP 不存在", 404, "not_found")
        return row

    async def check_name(self, identity, name, server_id=None):
        clash = await self.store.one(
            "SELECT id FROM kubedoor_ai_mcp_servers WHERE username=$1 AND lower(name)=lower($2) AND id IS DISTINCT FROM $3::uuid",
            identity.username, name, server_id)
        if clash:
            raise AIError(f"已经有名为 {name} 的 MCP（名称不区分大小写）", 409, "name_conflict")

    # ---- 管理接口（只管自己账号的）----

    async def list(self, identity):
        rows = await self.store.fetch("SELECT * FROM kubedoor_ai_mcp_servers WHERE username=$1 ORDER BY created_at,id", identity.username)
        return {"servers": [self.public(row) for row in rows]}

    async def create(self, identity, body):
        name, connection = parse_name(body.get("name")), parse_connection(body)
        enabled = parse_enabled(body.get("enabled", True))
        approval = parse_approval(body.get("approval", "readonly"))
        count = await self.store.one("SELECT count(*) AS total FROM kubedoor_ai_mcp_servers WHERE username=$1", identity.username)
        if count and count["total"] >= MAX_SERVERS:
            raise AIError(f"每个账号最多接入 {MAX_SERVERS} 个 MCP", 409, "too_many")
        await self.check_name(identity, name)
        server_id = str(uuid.uuid4())
        # 保存时顺带连一次拿工具列表；连不上也保存，界面上提示原因
        probe = await self.probe(**connection)
        try:
            row = await self.store.one(
                "INSERT INTO kubedoor_ai_mcp_servers(id,username,name,transport,encrypted_config,enabled,approval,tools,server_info,"
                "tools_at,checked_at,last_error) VALUES($1::uuid,$2,$3,$4,$5,$6,$7,$8,$9,CASE WHEN $10 THEN now() END,now(),$11) RETURNING *",
                server_id, identity.username, name, connection["transport"],
                self.encrypt(server_id, connection["url"], connection["headers"]), enabled, approval,
                probe["tools"], probe["server_info"], probe["error"] is None, probe["error"])
        except asyncpg.UniqueViolationError:
            raise AIError(f"已经有名为 {name} 的 MCP（名称不区分大小写）", 409, "name_conflict") from None
        return self.public(row)

    async def update(self, identity, server_id, body):
        row = await self.row(identity, server_id)
        try:
            saved = self.decrypt(row)
        except Exception:
            saved = {"url": "", "headers": {}}
        name, connection = parse_name(body.get("name")), parse_connection(body, saved["headers"])
        enabled = parse_enabled(body.get("enabled", row["enabled"]))
        approval = parse_approval(body.get("approval", row["approval"]))
        await self.check_name(identity, name, server_id)
        changed = (connection["transport"], connection["url"], connection["headers"]) != (row["transport"], saved["url"], saved["headers"])
        try:
            if not changed:
                row = await self.store.one(
                    "UPDATE kubedoor_ai_mcp_servers SET name=$3,enabled=$4,approval=$5,updated_at=now() "
                    "WHERE id=$1::uuid AND username=$2 RETURNING *", server_id, identity.username, name, enabled, approval)
            else:
                # 连接配置变了：版本号加一，审批中的调用随之失效；旧的工具列表不再可信
                probe = await self.probe(**connection)
                row = await self.store.one(
                    "UPDATE kubedoor_ai_mcp_servers SET name=$3,transport=$4,encrypted_config=$5,enabled=$6,approval=$7,"
                    "revision=revision+1,tools=$8,server_info=$9,tools_at=CASE WHEN $10 THEN now() END,checked_at=now(),"
                    "last_error=$11,updated_at=now() WHERE id=$1::uuid AND username=$2 RETURNING *",
                    server_id, identity.username, name, connection["transport"],
                    self.encrypt(server_id, connection["url"], connection["headers"]), enabled, approval,
                    probe["tools"], probe["server_info"], probe["error"] is None, probe["error"])
        except asyncpg.UniqueViolationError:
            raise AIError(f"已经有名为 {name} 的 MCP（名称不区分大小写）", 409, "name_conflict") from None
        if not row:
            raise AIError("MCP 不存在", 404, "not_found")
        return self.public(row)

    async def patch(self, identity, server_id, body):
        """列表上的开关和审批方式：不改连接配置，不重新连接"""
        enabled = parse_enabled(body["enabled"]) if "enabled" in body else None
        approval = parse_approval(body["approval"]) if "approval" in body else None
        if enabled is None and approval is None:
            raise AIError("没有要修改的内容")
        row = await self.store.one(
            "UPDATE kubedoor_ai_mcp_servers SET enabled=COALESCE($3,enabled),approval=COALESCE($4,approval),updated_at=now() "
            "WHERE id=$1::uuid AND username=$2 RETURNING *", server_id, identity.username, enabled, approval)
        if not row:
            raise AIError("MCP 不存在", 404, "not_found")
        return self.public(row)

    async def delete(self, identity, server_id):
        row = await self.store.one("DELETE FROM kubedoor_ai_mcp_servers WHERE id=$1::uuid AND username=$2 RETURNING id", server_id, identity.username)
        if not row:
            raise AIError("MCP 不存在", 404, "not_found")
        return {"success": True}

    async def test(self, identity, body):
        """测试表单里的配置，不保存；编辑时留空的请求头用已保存的值"""
        saved = {}
        if body.get("id"):
            row = await self.row(identity, body["id"])
            try:
                saved = self.decrypt(row)["headers"]
            except Exception:
                saved = {}
        probe = await self.probe(**parse_connection(body, saved))
        tools = [{"name": tool["name"], "title": tool.get("title"), "description": short(tool.get("description"), 300),
                  "read_only": tool["read_only"]} for tool in probe["tools"]]
        return {"success": probe["error"] is None, "error": probe["error"], "tools": tools, "server_info": probe["server_info"]}

    async def refresh(self, identity, server_id):
        await self.update_tools(await self.row(identity, server_id))
        return self.public(await self.row(identity, server_id))

    # ---- 连接 ----

    async def probe(self, transport, url, headers) -> dict:
        """连上去拿工具列表和服务信息；失败时 error 是给用户看的原因"""
        recorder = HttpRecorder()
        try:
            async with asyncio.timeout(LIST_TIMEOUT):
                client = await self.client_factory(transport, url, headers, recorder).__aenter__()
                try:
                    listed = await client.list_tools()
                    info = server_summary(client)
                finally:
                    await disconnect(client)
            tools = [entry for entry in (tool_entry(tool) for tool in listed) if entry][:MAX_TOOLS]
            return {"tools": tools, "server_info": info or None, "error": None}
        except Exception as exc:
            message = failure_message(exc, transport, recorder.error_status, secret_values({"url": url, "headers": headers}))
            # 不记 URL 查询参数、请求头和异常原文（可能带着它们）
            LOGGER.warning("MCP 连接失败 %s", json.dumps({"url": safe_url(url), "transport": transport, "error": type(exc).__name__,
                                                           "status": recorder.error_status, "message": message}, ensure_ascii=False))
            return {"tools": [], "server_info": None, "error": message}

    async def update_tools(self, row):
        """重新拿工具列表写回库；连接配置在这期间被改过（版本号变了）就不写"""
        try:
            config = self.decrypt(row)
        except Exception:
            await self.store.execute("UPDATE kubedoor_ai_mcp_servers SET checked_at=now(),last_error=$3 WHERE id=$1::uuid AND revision=$2",
                                     row["id"], row["revision"], "保存的地址和请求头无法解密（AI_ENCRYPTION_KEY 变了？），请重新填写后保存")
            return
        probe = await self.probe(row["transport"], config["url"], config["headers"])
        if probe["error"] is None:
            await self.store.execute(
                "UPDATE kubedoor_ai_mcp_servers SET tools=$3,server_info=$4,tools_at=now(),checked_at=now(),last_error=NULL "
                "WHERE id=$1::uuid AND revision=$2", row["id"], row["revision"], probe["tools"], probe["server_info"])
        else:
            # 保留上次成功拿到的工具列表：服务临时连不上时照样列出，调用时再报错
            await self.store.execute("UPDATE kubedoor_ai_mcp_servers SET checked_at=now(),last_error=$3 WHERE id=$1::uuid AND revision=$2",
                                     row["id"], row["revision"], probe["error"])

    def refresh_later(self, server_id):
        if server_id in self.refreshing:
            return
        task = asyncio.create_task(self._refresh(server_id), name=f"mcp-refresh-{server_id}")
        self.refreshing[server_id] = task
        task.add_done_callback(lambda _: self.refreshing.pop(server_id, None))

    async def _refresh(self, server_id):
        try:
            row = await self.store.one("SELECT * FROM kubedoor_ai_mcp_servers WHERE id=$1::uuid AND enabled", server_id)
            if row:
                await self.update_tools(row)
        except Exception as exc:
            LOGGER.warning("刷新 MCP 工具列表失败: %s", type(exc).__name__)

    async def catalogue(self, identity) -> list[dict]:
        """本轮可用的 MCP：只读缓存，不等网络；过期的在后台刷新，下一轮生效"""
        rows = await self.store.fetch(
            "SELECT id,name,approval,revision,tools,server_info,last_error,"
            "(checked_at IS NULL OR checked_at < now() - make_interval(secs => $2)) AS stale "
            "FROM kubedoor_ai_mcp_servers WHERE username=$1 AND enabled ORDER BY created_at,id", identity.username, float(REFRESH_SECONDS))
        servers = []
        for row in rows:
            if row["stale"]:
                self.refresh_later(row["id"])
            servers.append({"id": row["id"], "name": row["name"], "approval": row["approval"], "revision": row["revision"],
                            "tools": row["tools"] or [], "instructions": (row["server_info"] or {}).get("instructions"),
                            "error": row["last_error"]})
        return servers

    async def frozen_problem(self, identity, preview) -> str | None:
        row = await self.store.one("SELECT id,enabled,revision FROM kubedoor_ai_mcp_servers WHERE id=$1::uuid AND username=$2",
                                   preview.get("server_id"), identity.username)
        return changed_problem(row, preview)

    # ---- 对话里的调用（gateway.invoke_mcp → dispatch → execute）----

    @staticmethod
    def describe(server: dict, tool: str = "") -> dict:
        tools = server["tools"]
        if not tools:
            raise AIError(f"MCP {server['name']} 暂时连不上，没有工具列表：{server.get('error') or '请稍后再试'}", 409, "mcp_unavailable")
        if tool:
            spec = next((item for item in tools if item["name"] == tool), None)
            if spec is None:
                raise AIError(f"MCP {server['name']} 没有工具 {tool}，可用工具：{', '.join(item['name'] for item in tools[:50])}", 404, "mcp_tool_not_found")
            return {"success": True, "server": server["name"], "tool": spec["name"], "title": spec.get("title"),
                    "description": spec.get("description"), "input_schema": spec.get("input_schema"),
                    "read_only": spec.get("read_only", False),
                    "needs_approval": needs_approval(server["approval"], spec.get("read_only", False))}
        listed, size = [], 0
        for item in tools:
            entry = {"name": item["name"], "description": short(item.get("description"), 300), "read_only": item.get("read_only", False)}
            size += len(json.dumps(entry, ensure_ascii=False))
            if size > 40000:
                break
            listed.append(entry)
        result = {"success": True, "server": server["name"], "tools": listed}
        if server.get("instructions"):
            result["instructions"] = server["instructions"]
        if len(listed) < len(tools):
            result["truncated"] = True
            result["note"] = f"工具太多，只列出前 {len(listed)} 个；其他工具传 tool 名称查看"
        return result

    async def execute(self, identity, action) -> dict:
        """执行已经记账（需要时已批准）的一次调用；返回网关的统一结果，不抛异常"""
        preview, tool = action["preview"], action["preview"]["tool"]
        row = await self.store.one("SELECT * FROM kubedoor_ai_mcp_servers WHERE id=$1::uuid AND username=$2", preview.get("server_id"), identity.username)
        problem = changed_problem(row, preview)
        if problem:
            return {"success": False, "error": {"code": "mcp_changed", "message": problem}}
        try:
            config = self.decrypt(row)
        except Exception:
            return {"success": False, "error": {"code": "mcp_changed", "message": "保存的地址和请求头无法解密，请在 MCP 管理里重新填写"}}
        secrets, recorder = secret_values(config), HttpRecorder()
        base = {"server": row["name"], "tool": tool}
        try:
            async with asyncio.timeout(CONNECT_TIMEOUT + 10):
                client = await self.client_factory(row["transport"], config["url"], config["headers"], recorder).__aenter__()
        except Exception as exc:
            # 还没连上，调用没有发出：已知失败
            return {**base, "success": False, "error": {"code": "mcp_unavailable", "message": failure_message(exc, row["transport"], recorder.error_status, secrets)}}
        try:
            async with asyncio.timeout(CALL_TIMEOUT + 10):
                result = await client.call_tool_mcp(tool, action["arguments"] or {}, timeout=CALL_TIMEOUT)
        except Exception as exc:
            kind, message = classify_failure(exc, row["transport"], recorder.error_status, secrets)
            # 服务端明确拒绝（参数不对、没有这个工具等）是已知失败；超时、连接断开可能已经执行，写操作按结果未知处理
            code = {"rejected": "mcp_error", "timeout": "timeout"}.get(kind, "transport_error")
            return {**base, "success": False, "error": {"code": code, "message": message}}
        finally:
            await disconnect(client)
        text, truncated, is_error = call_content(result)
        text = redact_values(text, secrets)
        if is_error:
            return {**base, "success": False, "content": text, "truncated": truncated,
                    "error": {"code": "mcp_tool_error", "message": short(text, 2000) or "MCP 工具返回了错误"}}
        return {**base, "success": True, "content": text, "truncated": truncated}

    async def close(self):
        tasks = list(self.refreshing.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
