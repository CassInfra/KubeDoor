"""AI 会话的公网分享：把当前会话渲染成脱敏的静态快照，发布到 Cloudflare Workers。

和站内分享（Service.set_share / shared_session）不同：公网分享不需要登录 KubeDoor，
内容是发布时的快照（之后的新消息不同步），工具调用只保留名称和状态，不包含参数和结果。
快照 HTML 存在 Workers KV，由 AI 服务部署到 Cloudflare 的 Worker 按分享码读取；配置存
kubedoor_ai_public_share_config，API Token 用 AI_ENCRYPTION_KEY 加密。
"""
from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
from kubedoor_tools import redact_export
from markdown_it import MarkdownIt

from .domain import AIError, BEIJING_TZ, utc_now


PUBLIC_SHARE_CODE = re.compile(r"[A-Za-z0-9_-]{22}")
DEFAULT_WORKER_NAME = "kubedoor-ai-share"
KV_BINDING = "AI_SHARES"
CONFIG_AAD = "public-share-config"
CLOUDFLARE_API = "https://api.cloudflare.com/client/v4"
ACCOUNT_ID = re.compile(r"[0-9a-f]{32}")
NAMESPACE_ID = re.compile(r"[0-9a-f]{32}")
WORKER_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")


@dataclass
class PublicShareConfig:
    account_id: str = ""
    api_token: str = ""
    worker_name: str = DEFAULT_WORKER_NAME
    kv_namespace_id: str = ""
    public_base_url: str = ""

    @classmethod
    def parse(cls, raw: Any, current: "PublicShareConfig | None" = None) -> "PublicShareConfig":
        if not isinstance(raw, dict):
            raise AIError("配置格式无效")
        current = current or cls()
        account_id = _text(raw.get("account_id"), current.account_id).lower()
        if not ACCOUNT_ID.fullmatch(account_id):
            raise AIError("Account ID 必须是 32 位十六进制字符串")
        api_token = _text(raw.get("api_token"), current.api_token)
        if not api_token:
            raise AIError("请填写 API Token")
        if len(api_token) > 1000:
            raise AIError("API Token 过长")
        worker_name = _text(raw.get("worker_name"), current.worker_name or DEFAULT_WORKER_NAME).lower()
        if not WORKER_NAME.fullmatch(worker_name):
            raise AIError("Worker 名称只能包含小写字母、数字和连字符，且不超过 63 个字符")
        kv_namespace_id = _text(raw.get("kv_namespace_id"), current.kv_namespace_id).lower()
        if kv_namespace_id and not NAMESPACE_ID.fullmatch(kv_namespace_id):
            raise AIError("KV Namespace ID 必须是 32 位十六进制字符串")
        base_url = _text(raw.get("public_base_url"), current.public_base_url).rstrip("/")
        if not base_url:
            raise AIError("请填写公网访问地址")
        parsed = urlsplit(base_url)
        try:
            parsed.port
        except ValueError:
            raise AIError("公网访问地址的端口无效") from None
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or any(ord(char) < 33 for char in base_url)):
            raise AIError("公网访问地址必须是 http(s) 地址，不能带账号、查询参数或片段")
        if len(base_url) > 500:
            raise AIError("公网访问地址过长")
        return cls(account_id=account_id, api_token=api_token, worker_name=worker_name,
                   kv_namespace_id=kv_namespace_id, public_base_url=base_url)

    def public(self) -> dict:
        """给管理页看的配置：不返回 API Token，只返回是否已设置（字段名不能以 token / secret 结尾）。"""
        return {"account_id": self.account_id, "worker_name": self.worker_name,
                "kv_namespace_id": self.kv_namespace_id, "public_base_url": self.public_base_url,
                "api_token_set": bool(self.api_token)}

    def encrypted(self) -> dict:
        return {"account_id": self.account_id, "api_token": self.api_token, "worker_name": self.worker_name,
                "kv_namespace_id": self.kv_namespace_id, "public_base_url": self.public_base_url}


def _text(value: Any, fallback: Any = "") -> str:
    if value is None or value == "" or value == "******":
        return str(fallback or "").strip()
    return str(value).strip()


def public_share_url(base_url: str, code: str) -> str:
    return f"{base_url.rstrip('/')}/s/{code}"


# 公网快照的脱敏：密码 / token 这类键值对和 Bearer 用 kubedoor_tools 的规则；
# 另外打码公网分享里最常见的身份和网络标识。工具参数和结果根本不进快照。
_IPV4 = re.compile(r"(?<![\w.])(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)(?![\w.])")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_CN_ID = re.compile(r"(?<!\d)\d{17}[0-9Xx](?!\d)")
_CN_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_TOKEN = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{16,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{30,})\b"
    r"|\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"
)
_URL_CREDENTIALS = re.compile(r"(https?://)[^/\s:@]+(?::[^/\s@]+)?@", re.I)
_CN_SECRET = re.compile(r"(?i)(密码|口令|密钥|令牌|secret|token|api\s*key)\s*(?:是|为|[:：=])\s*([^\s，。；,;]{4,})")


def redact_public_text(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    value = redact_export(value)
    value = _CN_SECRET.sub(lambda match: match.group(1) + "[REDACTED]", value)
    value = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/-]+", "Bearer [REDACTED]", value)
    value = _TOKEN.sub("[已脱敏]", value)
    value = _URL_CREDENTIALS.sub(r"\1[已脱敏]@", value)
    value = _CN_ID.sub("[证件号已脱敏]", value)
    value = _CN_PHONE.sub("[手机号已脱敏]", value)
    value = _EMAIL.sub("[邮箱已脱敏]", value)
    value = _IPV4.sub("[IP已脱敏]", value)
    return value


def build_public_snapshot(session: dict, rows: list[dict], active_status: str | None = None,
                          shared_at: str | None = None) -> dict:
    """把会话消息整理成公网快照：工具只留名称和状态，MCP 消息的正文不公开。"""
    messages = []
    for row in rows or []:
        tools = []
        for tool in row.get("tools") or []:
            if not isinstance(tool, dict):
                continue
            name = tool.get("name") or tool.get("operation")
            if not isinstance(name, str) or not name:
                continue
            status = tool.get("status") or tool.get("state") or "unknown"
            tools.append({"name": name[:120], "status": str(status)[:40]})
        origin = row.get("origin")
        mcp = origin == "mcp"
        content = row.get("content") if isinstance(row.get("content"), str) else ""
        role = row.get("role") if row.get("role") in ("user", "assistant", "system") else "assistant"
        messages.append({"role": role, "content": "" if mcp else redact_public_text(content),
                         "mcp": mcp, "tools": tools, "created_at": row.get("created_at")})
    title = session.get("title") if isinstance(session.get("title"), str) else ""
    return {"title": redact_public_text(title) or "AI 会话分享",
            "shared_at": shared_at or utc_now(), "messages": messages,
            "active_run": active_status, "message_count": len(messages)}


_MARKDOWN = MarkdownIt("commonmark", {"html": False}).enable("table").enable("strikethrough")


def _link_open(tokens, idx, options, env):
    tokens[idx].attrSet("target", "_blank")
    tokens[idx].attrSet("rel", "noopener noreferrer nofollow")
    return _MARKDOWN.renderer.renderToken(tokens, idx, options, env)


def _image_alt(tokens, idx, options, env):
    # 公网页面不加载外部图片，避免访问者的地址泄露给图片所在站点
    return f'<span class="img-alt">[图片：{html.escape(tokens[idx].content or "")}]</span>'


_MARKDOWN.renderer.rules["link_open"] = _link_open
_MARKDOWN.renderer.rules["image"] = _image_alt


def _format_time(value: Any) -> str:
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(BEIJING_TZ)
    return parsed.strftime("%Y-%m-%d %H:%M")


def _tool_status(status: Any) -> str:
    labels = {"completed": "已完成", "complete": "已完成", "done": "已完成", "success": "成功",
              "failed": "失败", "error": "失败", "running": "执行中", "queued": "排队中",
              "pending": "等待批准", "waiting_approval": "等待批准", "awaiting_approval": "等待批准",
              "rejected": "已拒绝", "cancelled": "已停止", "canceled": "已停止", "unknown": "结果不确定"}
    return labels.get(str(status), str(status or "未知"))


def _render_tool(tool: dict) -> str:
    name = html.escape(str(tool.get("name") or "tool"))
    status = html.escape(_tool_status(tool.get("status")))
    return ('<details class="tool"><summary><span class="dot" aria-hidden="true"></span>'
            f'<span class="tool-name">{name}</span><span class="tool-state">{status}</span></summary>'
            '<div class="tool-notice">公网分享不支持查看工具调用明细</div></details>')


def _render_message(message: dict) -> str:
    role = message.get("role")
    role_label = {"user": "提问人", "assistant": "花折 AI", "system": "系统"}.get(role, "花折 AI")
    role_class = "user" if role == "user" else "assistant"
    created = html.escape(_format_time(message.get("created_at")))
    if message.get("mcp"):
        body = '<div class="mcp">这条消息来自 MCP 工具调用，公网分享不展示其内容。</div>'
    else:
        content = message.get("content") or ""
        if role == "assistant" and content:
            body = f'<div class="markdown">{_MARKDOWN.render(content)}</div>'
        elif content:
            body = f'<div class="text">{html.escape(content).replace(chr(10), "<br>")}</div>'
        else:
            body = '<div class="text muted">（没有正文）</div>'
    tools = message.get("tools") or []
    tools_html = ""
    if tools:
        items = "".join(_render_tool(tool) for tool in tools)
        tools_html = (f'<div class="tools"><span class="tools-label">工具调用 {len(tools)}</span>'
                      f'<div class="tool-list">{items}</div></div>')
    return (f'<article class="message {role_class}"><div class="message-head">'
            f'<span class="role">{role_label}</span><span class="time">{created}</span></div>'
            f'{body}{tools_html}</article>')


_PUBLIC_PAGE_CSS = """
:root {
  color-scheme: light;
  --page: #f4f6fb;
  --card: #ffffff;
  --text: #1f2430;
  --muted: #6b7280;
  --line: #e5e7eb;
  --brand: #6d5bd0;
  --brand-strong: #2f6fed;
  --notice-bg: #eef4ff;
  --notice-line: #d8e6ff;
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0;
  color: var(--text);
  background:
    radial-gradient(900px 420px at 8% -8%, #e7f0ff 0%, transparent 60%),
    radial-gradient(700px 380px at 100% 0%, #f1e9ff 0%, transparent 55%),
    var(--page);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB",
    "Microsoft YaHei", "Noto Sans CJK SC", sans-serif;
  line-height: 1.6;
}
.page { width: min(860px, calc(100% - 32px)); margin: 0 auto; padding: 30px 0 48px; }
.header { display: flex; gap: 14px; align-items: center; margin-bottom: 18px; }
.avatar {
  display: grid;
  flex: 0 0 auto;
  width: 50px;
  height: 50px;
  color: #fff;
  font-size: 20px;
  font-weight: 700;
  place-items: center;
  background: linear-gradient(135deg, #2f8ded, #7a5bd0 58%, #c249a8);
  border-radius: 15px;
  box-shadow: 0 8px 20px rgb(76 63 190 / 24%);
}
.heading { min-width: 0; }
h1 { margin: 0; font-size: 22px; line-height: 1.35; overflow-wrap: anywhere; }
.heading p { margin: 4px 0 0; color: var(--muted); font-size: 12px; }
.notice {
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding: 12px 14px;
  color: #33415c;
  font-size: 13px;
  background: var(--notice-bg);
  border: 1px solid var(--notice-line);
  border-radius: 12px;
}
.notice strong { color: #1f4f9e; }
.messages { display: flex; flex-direction: column; gap: 14px; margin-top: 18px; }
.message {
  padding: 16px 18px;
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: 14px;
  box-shadow: 0 1px 2px rgb(16 24 40 / 4%);
}
.message.user { background: linear-gradient(135deg, #f2f7ff, #f8f5ff); border-color: #dfe8ff; }
.message-head { display: flex; gap: 8px; align-items: center; margin-bottom: 8px; color: var(--muted); font-size: 12px; }
.role { font-weight: 600; color: var(--brand); }
.message.user .role { color: var(--brand-strong); }
.time { margin-left: auto; font-size: 11px; }
.text { font-size: 14px; line-height: 1.75; white-space: pre-wrap; overflow-wrap: anywhere; }
.muted { color: var(--muted); font-style: italic; }
.mcp {
  padding: 8px 10px;
  color: var(--muted);
  font-size: 12px;
  background: #f8fafc;
  border: 1px dashed var(--line);
  border-radius: 8px;
}
.markdown { font-size: 14px; line-height: 1.75; overflow-wrap: anywhere; }
.markdown > :first-child { margin-top: 0; }
.markdown > :last-child { margin-bottom: 0; }
.markdown h1, .markdown h2, .markdown h3, .markdown h4 { margin: 16px 0 8px; line-height: 1.4; }
.markdown h1 { font-size: 19px; }
.markdown h2 { font-size: 17px; }
.markdown h3 { font-size: 15px; }
.markdown p { margin: 8px 0; }
.markdown ul, .markdown ol { margin: 8px 0; padding-left: 22px; }
.markdown a { color: var(--brand-strong); text-underline-offset: 2px; }
.markdown code {
  padding: 1px 5px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
  font-size: 0.9em;
  background: #f1f5f9;
  border-radius: 5px;
}
.markdown pre {
  margin: 10px 0;
  padding: 12px 14px;
  overflow: auto;
  background: #0f172a;
  border-radius: 10px;
  font-size: 12.5px;
  line-height: 1.6;
}
.markdown pre code { padding: 0; color: #e2e8f0; background: transparent; border-radius: 0; }
.markdown blockquote {
  margin: 10px 0;
  padding: 8px 12px;
  color: #4b5563;
  background: #f8fafc;
  border-left: 3px solid #c7d2fe;
}
.markdown table { display: block; width: 100%; margin: 10px 0; overflow: auto; border-collapse: collapse; font-size: 13px; }
.markdown th, .markdown td { padding: 6px 10px; text-align: left; border: 1px solid var(--line); }
.markdown th { background: #f8fafc; }
.img-alt { color: var(--muted); font-size: 12px; }
.tools { margin-top: 10px; padding-top: 10px; border-top: 1px dashed var(--line); }
.tools-label { color: var(--muted); font-size: 12px; }
.tool-list { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 8px; }
.tool { border: 1px solid var(--line); border-radius: 999px; background: #f8fafc; font-size: 12px; }
.tool[open] { border-radius: 10px; }
.tool summary { display: flex; gap: 6px; align-items: center; padding: 5px 10px; cursor: pointer; list-style: none; }
.tool summary::-webkit-details-marker { display: none; }
.dot { flex: 0 0 auto; width: 6px; height: 6px; background: #22c55e; border-radius: 50%; }
.tool-name { max-width: 260px; overflow: hidden; font-weight: 500; text-overflow: ellipsis; white-space: nowrap; }
.tool-state { color: var(--muted); font-size: 11px; }
.tool-notice { padding: 6px 10px 8px; color: #b45309; font-size: 11px; }
.active {
  margin: 0;
  padding: 10px 12px;
  color: #92400e;
  font-size: 12px;
  background: #fffbeb;
  border: 1px solid #fde68a;
  border-radius: 10px;
}
.empty { padding: 30px 0; color: var(--muted); font-size: 13px; text-align: center; }
.footer { margin-top: 26px; color: var(--muted); font-size: 11px; text-align: center; }
@media (max-width: 640px) {
  .page { width: min(100% - 20px, 860px); padding-top: 18px; }
  .header { gap: 11px; }
  .avatar { width: 42px; height: 42px; font-size: 17px; border-radius: 12px; }
  h1 { font-size: 19px; }
  .message { padding: 13px; border-radius: 12px; }
  .tool-name { max-width: 190px; }
}
@media (prefers-color-scheme: dark) {
  :root {
    color-scheme: dark;
    --page: #0f1420;
    --card: #151c2c;
    --text: #e5e7eb;
    --muted: #9ca3af;
    --line: #283246;
    --brand: #a5b4fc;
    --brand-strong: #93c5fd;
    --notice-bg: #17233d;
    --notice-line: #26375a;
  }
  body { background: radial-gradient(900px 420px at 8% -8%, #17233d 0%, transparent 60%), var(--page); }
  .message.user { background: linear-gradient(135deg, #17233d, #211a3a); border-color: #2b3a5b; }
  .notice { color: #cbd5e1; }
  .notice strong { color: #bfdbfe; }
  .mcp, .markdown th, .markdown blockquote { background: #101827; }
  .markdown code { background: #1e293b; }
  .tool { background: #101827; }
}
"""


def render_public_share(snapshot: dict) -> str:
    title = html.escape(str(snapshot.get("title") or "AI 会话分享"))
    shared_at = _format_time(snapshot.get("shared_at"))
    messages = [_render_message(message) for message in snapshot.get("messages") or []]
    if not messages:
        messages.append('<p class="empty">这个会话还没有内容。</p>')
    active = snapshot.get("active_run")
    if active:
        messages.append('<p class="active">分享时有一轮还在进行中，它的回答不在快照里。</p>')
    count = int(snapshot.get("message_count") or 0)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<meta name="referrer" content="no-referrer">
<title>{title} · 花折 AI</title>
<style>{_PUBLIC_PAGE_CSS}</style>
</head>
<body>
<div class="page">
  <header class="header">
    <div class="avatar" aria-hidden="true">花</div>
    <div class="heading">
      <h1>{title}</h1>
      <p>公网快照 · {html.escape(shared_at)} · {count} 条消息</p>
    </div>
  </header>
  <div class="notice">
    <strong>这是一份公网快照</strong>
    <span>只包含分享时的内容，之后的新消息不会同步；工具调用只显示名称，不提供参数和结果。</span>
  </div>
  <main class="messages">
    {''.join(messages)}
  </main>
  <footer class="footer">由 KubeDoor 花折 AI 生成 · 内容已脱敏 · 快照时间 {html.escape(shared_at)}</footer>
</div>
</body>
</html>
"""


# 随包部署到 Cloudflare Workers 的脚本：只读 KV 里的 /s/<code>，不依赖任何外部资源。
WORKER_SCRIPT = r"""const SHARE_PATH = /\/s\/([A-Za-z0-9_-]{22})$/;
const HTML_TYPE = "text/html; charset=utf-8";
const HEADERS = {
  "content-type": HTML_TYPE,
  "cache-control": "no-store",
  "x-content-type-options": "nosniff",
  "x-frame-options": "DENY",
  "referrer-policy": "no-referrer",
  "x-robots-tag": "noindex, nofollow",
  "permissions-policy": "camera=(), microphone=(), geolocation=()",
  "content-security-policy": "default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
};
const MISSING = `<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow"><title>分享不存在 · 花折 AI</title>
<style>
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#f4f6fb;color:#1f2430;
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
.card{max-width:420px;padding:28px 30px;background:#fff;border:1px solid #e5e7eb;border-radius:16px;text-align:center}
h1{margin:0 0 8px;font-size:18px}p{margin:0;color:#6b7280;font-size:13px}
@media (prefers-color-scheme:dark){body{background:#0f1420}.card{background:#151c2c;border-color:#283246;color:#e5e7eb}p{color:#9ca3af}}
</style></head><body><div class="card"><h1>分享不存在或已取消</h1><p>请向分享人确认链接是否正确，或者分享是否已经取消。</p></div></body></html>`;

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (/\/health$/.test(url.pathname)) {
      return new Response("ok", { headers: { "content-type": "text/plain; charset=utf-8", "cache-control": "no-store" } });
    }
    if (request.method !== "GET" && request.method !== "HEAD") {
      return new Response("Method Not Allowed", { status: 405, headers: {
        "content-type": "text/plain; charset=utf-8", "cache-control": "no-store"
      } });
    }
    const match = SHARE_PATH.exec(url.pathname);
    if (!match) return new Response(MISSING, { status: 404, headers: HEADERS });
    const page = await env.AI_SHARES.get(match[1]);
    if (!page) return new Response(MISSING, { status: 404, headers: HEADERS });
    return new Response(request.method === "HEAD" ? null : page, { headers: HEADERS });
  }
};
"""


class CloudflareShare:
    """Cloudflare Workers + KV 的最小客户端：验证配置、创建 / 绑定命名空间、部署 Worker、读写快照。"""

    def __init__(self, config: PublicShareConfig, client: httpx.AsyncClient | None = None):
        self.config = config
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))
        self._owned = client is None

    async def close(self):
        if self._owned:
            await self._client.aclose()

    @staticmethod
    def _error(response: httpx.Response, data: Any) -> str:
        errors = data.get("errors") if isinstance(data, dict) else None
        if isinstance(errors, list) and errors:
            first = errors[0]
            if isinstance(first, dict):
                message = str(first.get("message") or "").strip()
                code = first.get("code")
                if message:
                    return f"Cloudflare：{message}" + (f"（代码 {code}）" if code else "")
        if response.status_code == 401:
            return "Cloudflare API Token 无效或已过期"
        if response.status_code == 403:
            return "Cloudflare API Token 没有所需权限"
        if response.status_code == 404:
            return "Cloudflare 账号或资源不存在，请检查 Account ID"
        return f"Cloudflare 请求失败（HTTP {response.status_code}）"

    async def _request(self, method: str, path: str, *, error_status: int = 502,
                       allow_404: bool = False, **kwargs):
        headers = {"Authorization": f"Bearer {self.config.api_token}", **(kwargs.pop("headers", None) or {})}
        try:
            response = await self._client.request(method, CLOUDFLARE_API + path, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise AIError(f"连接 Cloudflare 失败：{type(exc).__name__}", 502, "cloudflare_unreachable") from exc
        try:
            data = response.json()
        except ValueError:
            data = {}
        if allow_404 and response.status_code == 404:
            return None
        if response.status_code >= 400 or (isinstance(data, dict) and data.get("success") is False):
            raise AIError(self._error(response, data), error_status, "cloudflare_error",
                          {"status": response.status_code})
        return data.get("result") if isinstance(data, dict) else None

    async def verify(self) -> dict:
        token = await self._request("GET", "/user/tokens/verify", error_status=422)
        status = (token or {}).get("status") if isinstance(token, dict) else None
        if status and status != "active":
            raise AIError(f"Cloudflare API Token 状态异常：{status}", 422, "cloudflare_token_invalid")
        await self._request("GET", f"/accounts/{self.config.account_id}/workers/scripts?per_page=1", error_status=422)
        if self.config.kv_namespace_id:
            await self._request(
                "GET",
                f"/accounts/{self.config.account_id}/storage/kv/namespaces/{self.config.kv_namespace_id}",
                error_status=422,
            )
        return {"success": True, "account_id": self.config.account_id,
                "worker_name": self.config.worker_name, "kv_namespace_id": self.config.kv_namespace_id}

    async def ensure_namespace(self) -> str:
        if self.config.kv_namespace_id:
            await self._request(
                "GET",
                f"/accounts/{self.config.account_id}/storage/kv/namespaces/{self.config.kv_namespace_id}",
            )
            return self.config.kv_namespace_id
        title = f"{self.config.worker_name}-kv"
        listed = await self._request(
            "GET", f"/accounts/{self.config.account_id}/storage/kv/namespaces?per_page=100"
        )
        for item in listed or []:
            if isinstance(item, dict) and item.get("title") == title and item.get("id"):
                return str(item["id"])
        created = await self._request(
            "POST", f"/accounts/{self.config.account_id}/storage/kv/namespaces", json={"title": title}
        )
        namespace = (created or {}).get("id") if isinstance(created, dict) else None
        if not namespace:
            raise AIError("创建 KV Namespace 失败：Cloudflare 没有返回 ID", 502, "cloudflare_error")
        return str(namespace)

    async def deploy(self):
        if not self.config.kv_namespace_id:
            raise AIError("KV Namespace 还没有准备好", 500, "cloudflare_error")
        metadata = {
            "main_module": "worker.js",
            "compatibility_date": "2024-09-23",
            "bindings": [{"type": "kv_namespace", "name": KV_BINDING, "namespace_id": self.config.kv_namespace_id}],
        }
        files = {
            "metadata": (None, json.dumps(metadata), "application/json"),
            "worker.js": ("worker.js", WORKER_SCRIPT, "application/javascript+module"),
        }
        await self._request(
            "PUT",
            f"/accounts/{self.config.account_id}/workers/scripts/{self.config.worker_name}",
            files=files,
        )

    def _value_path(self, code: str) -> str:
        return (f"/accounts/{self.config.account_id}/storage/kv/namespaces/"
                f"{self.config.kv_namespace_id}/values/{quote(code, safe='')}")

    async def check_public_url(self) -> str | None:
        """部署后访问一次 <公网地址>/health，返回提示文字；能打开返回 None。不影响保存，只提醒用户检查绑定。"""
        try:
            response = await self._client.get(f"{self.config.public_base_url}/health", timeout=10.0)
        except httpx.HTTPError as exc:
            return f"Worker 已部署，但公网地址暂时无法访问（{type(exc).__name__}），请检查 Worker 的域名绑定"
        if response.status_code != 200 or response.text.strip() != "ok":
            return f"Worker 已部署，但公网地址返回 HTTP {response.status_code}，请检查 Worker 的域名绑定"
        return None

    async def put_value(self, code: str, page: str):
        await self._request(
            "PUT", self._value_path(code), content=page.encode("utf-8"),
            headers={"Content-Type": "text/html; charset=utf-8"},
        )

    async def delete_value(self, code: str):
        await self._request("DELETE", self._value_path(code), allow_404=True)
