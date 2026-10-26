import type {
  AIMcpApproval,
  AIMcpServer,
  AIMcpServerInput,
  AIMcpTransport
} from "@/api/ai";

export const MCP_TRANSPORTS: {
  value: AIMcpTransport;
  label: string;
  hint: string;
}[] = [
  {
    value: "streamable_http",
    label: "Streamable HTTP",
    hint: "新版 MCP 的 HTTP 传输，地址通常以 /mcp 结尾"
  },
  {
    value: "sse",
    label: "SSE",
    hint: "旧版的 HTTP + SSE 传输，地址通常以 /sse 结尾"
  }
];

export const MCP_APPROVALS: {
  value: AIMcpApproval;
  label: string;
  hint: string;
}[] = [
  {
    value: "readonly",
    label: "只读工具自动执行",
    hint: "MCP 声明为只读的工具直接调用，其余工具调用前要你批准"
  },
  { value: "never", label: "全部自动执行", hint: "调用前都不需要批准" },
  { value: "always", label: "每次都需批准", hint: "每次调用前都要你批准" }
];

export const MCP_MAX_HEADERS = 20;
/** 和 AI 服务一致：由 MCP 客户端自己设置的请求头不能填写 */
const RESERVED_HEADERS = new Set([
  "host",
  "content-length",
  "content-type",
  "accept",
  "connection",
  "transfer-encoding",
  "upgrade",
  "te",
  "trailer",
  "keep-alive",
  "proxy-connection",
  "mcp-session-id",
  "mcp-protocol-version",
  "last-event-id"
]);
const HEADER_NAME = /^[!#$%&'*+\-.^_`|~0-9A-Za-z]{1,128}$/;
const SERVER_NAME = /^[\w.\-\u4e00-\u9fff]{1,40}$/;

export interface AIMcpHeaderRow {
  name: string;
  value: string;
  /** 已保存的请求头原名：值留空表示沿用；改了名称就要重新填值 */
  saved: string | null;
}

export interface AIMcpForm {
  name: string;
  transport: AIMcpTransport;
  url: string;
  headers: AIMcpHeaderRow[];
  approval: AIMcpApproval;
  enabled: boolean;
}

export function emptyMcpForm(): AIMcpForm {
  return {
    name: "",
    transport: "streamable_http",
    url: "",
    headers: [],
    approval: "readonly",
    enabled: true
  };
}

/** 编辑已保存的 MCP：请求头只有名称，值留空表示不改 */
export function mcpFormFrom(server: AIMcpServer): AIMcpForm {
  return {
    name: server.name,
    transport: server.transport,
    url: server.url,
    headers: server.headers.map(item => ({
      name: item.name,
      value: "",
      saved: item.name
    })),
    approval: server.approval,
    enabled: server.enabled
  };
}

const keepsSaved = (row: AIMcpHeaderRow) =>
  !!row.saved && row.saved.toLowerCase() === row.name.trim().toLowerCase();

/** 名称和值都没填的空行不算 */
const filledRows = (form: AIMcpForm) =>
  form.headers.filter(row => row.name.trim() || row.value.trim());

/**
 * 返回第一个问题，没有问题返回空字符串。规则同 AI 服务（服务端还会再校验）；
 * 只测试连接时不需要名称。
 */
export function validateMcpForm(form: AIMcpForm, checkName = true): string {
  if (checkName && !SERVER_NAME.test(form.name.trim()))
    return "名称只能用 1–40 个字母、数字、汉字或 _ . -，AI 调用时用它指定服务";
  const url = form.url.trim();
  let parsed: URL | null = null;
  try {
    parsed = new URL(url);
  } catch {
    parsed = null;
  }
  if (
    !parsed ||
    /\s/.test(url) ||
    !["http:", "https:"].includes(parsed.protocol) ||
    parsed.username ||
    parsed.password ||
    parsed.hash
  )
    return "URL 必须是 http:// 或 https:// 开头的 MCP 服务地址，不能包含账号密码或 # 片段";
  const rows = filledRows(form);
  if (rows.length > MCP_MAX_HEADERS) return `请求头最多 ${MCP_MAX_HEADERS} 个`;
  const seen = new Set<string>();
  for (const row of rows) {
    const header = row.name.trim();
    if (!HEADER_NAME.test(header))
      return `请求头名称「${header}」无效：只能用字母、数字和 - _ 等符号`;
    const lower = header.toLowerCase();
    if (RESERVED_HEADERS.has(lower))
      return `请求头 ${header} 由 MCP 客户端自己设置，不能填写`;
    if (seen.has(lower)) return `请求头 ${header} 重复`;
    seen.add(lower);
    if (/[\r\n\0]/.test(row.value)) return `请求头 ${header} 的值不能换行`;
    if (!row.value.trim() && !keepsSaved(row))
      return `请填写请求头 ${header} 的值`;
  }
  return "";
}

export function mcpPayload(form: AIMcpForm): AIMcpServerInput {
  return {
    name: form.name.trim(),
    transport: form.transport,
    url: form.url.trim(),
    headers: filledRows(form).map(row => ({
      name: row.name.trim(),
      value: row.value.trim()
    })),
    approval: form.approval,
    enabled: form.enabled
  };
}

export const transportLabel = (value: string) =>
  MCP_TRANSPORTS.find(item => item.value === value)?.label || value;

export const approvalLabel = (value: string) =>
  MCP_APPROVALS.find(item => item.value === value)?.label || value;

/** 列表里显示的地址：查询参数的值打码（常是 key），编辑时才显示完整地址 */
export function maskedUrl(url: string) {
  try {
    const parsed = new URL(url);
    if (!parsed.search) return url;
    const keys = [...parsed.searchParams.keys()];
    return `${parsed.origin}${parsed.pathname}?${keys.map(key => `${encodeURIComponent(key)}=***`).join("&")}`;
  } catch {
    return url;
  }
}

/** 调用这个工具前要不要批准（同 AI 服务的 needs_approval） */
export const mcpNeedsApproval = (approval: AIMcpApproval, readOnly: boolean) =>
  approval === "always" || (approval === "readonly" && !readOnly);

/** 列表里的连接状态：连不上但有上次拿到的工具列表时，照样可以用 */
export function mcpStatus(server: AIMcpServer) {
  const count = `${server.tools.length} 个工具`;
  if (server.last_error && !server.tools.length)
    return {
      type: "danger" as const,
      label: "连接失败",
      detail: server.last_error
    };
  if (server.last_error)
    return {
      type: "warning" as const,
      label: count,
      detail: `最近一次连接失败，沿用上次的工具列表：${server.last_error}`
    };
  if (!server.tools_at)
    return {
      type: "info" as const,
      label: "未连接",
      detail: "还没有拿到工具列表"
    };
  return { type: "success" as const, label: count, detail: "" };
}
