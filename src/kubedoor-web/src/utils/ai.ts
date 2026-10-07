export interface SseFrame {
  event?: string;
  id?: string;
  data: string;
}

/** Incremental SSE parsing, including UTF-8 chunks, CRLF and multiline data. */
export function createSseParser(onFrame: (frame: SseFrame) => void) {
  let buffer = "";
  let first = true;
  let data: string[] = [];
  let event = "";
  let id = "";
  const flush = () => {
    if (data.length)
      onFrame({ event: event || undefined, id, data: data.join("\n") });
    data = [];
    event = "";
  };
  const line = (value: string) => {
    if (first) {
      value = value.replace(/^\uFEFF/, "");
      first = false;
    }
    if (!value) return flush();
    if (value.startsWith(":")) return;
    const colon = value.indexOf(":");
    const field = colon < 0 ? value : value.slice(0, colon);
    const content = colon < 0 ? "" : value.slice(colon + 1).replace(/^ /, "");
    if (field === "data") data.push(content);
    if (field === "event") event = content;
    if (field === "id" && !content.includes("\0")) id = content;
  };
  const drain = (final: boolean) => {
    while (buffer) {
      const match = /\r\n|\r|\n/.exec(buffer);
      if (!match) break;
      if (!final && match[0] === "\r" && match.index === buffer.length - 1)
        break;
      line(buffer.slice(0, match.index));
      buffer = buffer.slice(match.index + match[0].length);
    }
    if (final && buffer) {
      line(buffer);
      buffer = "";
    }
  };
  return {
    push(chunk: string) {
      buffer += chunk;
      drain(false);
    },
    finish() {
      drain(true);
      flush();
    }
  };
}

export function newAIId() {
  // The console may run over HTTP, where randomUUID is unavailable.
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, item =>
    item.toString(16).padStart(2, "0")
  ).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

export const isTerminalRun = (status: string) =>
  [
    "completed",
    "complete",
    "done",
    "failed",
    "error",
    "cancelled",
    "canceled",
    "rejected"
  ].includes(status);

export const resourceKey = (
  resource: { namespace?: string; name: string } | null
) =>
  resource ? JSON.stringify([resource.namespace || "", resource.name]) : "";

export function parseResourceKey(
  value: string
): { namespace?: string; name: string } | null {
  if (!value) return null;
  try {
    const [namespace, name] = JSON.parse(value);
    if (typeof namespace !== "string" || typeof name !== "string" || !name)
      return null;
    return { namespace, name };
  } catch {
    return null;
  }
}

/** Render untrusted model/tool data as text, never HTML; redact credential fields. */
export function safeAIText(value: unknown): string {
  if (value == null) return "";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(
      value,
      (key, item) =>
        /^(api[_-]?key|authorization|token|password|client[_-]?key[_-]?data)$/i.test(
          key
        )
          ? "[已隐藏]"
          : item,
      2
    );
  } catch {
    return String(value);
  }
}

export const providerStorageKey = (username: string) =>
  `kubedoor-ai:provider:${encodeURIComponent(username)}`;

/** Normalizes live events and persisted action-ledger records consistently. */
export function normalizeAITool(value: Record<string, any>, env = "") {
  const result = value.result ?? value.output;
  const meta = result && typeof result === "object" ? result : {};
  const preview = value.preview || {};
  const state =
    value.status ||
    value.state ||
    (meta.success === false ? "failed" : "completed");
  const statuses: Record<string, string> = {
    pending: "waiting_approval",
    approved: "running",
    ready: "running",
    dispatching: "running",
    succeeded: "completed",
    unknown: "unknown"
  };
  return {
    ...value,
    id: String(value.id || value.action_id || value.tool_call_id || newAIId()),
    name: String(value.name || value.operation || value.tool || "工具调用"),
    status: statuses[state] || state,
    pending: value.pending ?? state === "pending",
    env: value.env || value.scope?.env || env,
    source: value.source || meta.source,
    observed_at: value.observed_at || meta.observed_at || meta.collected_at,
    freshness: value.freshness || meta.freshness,
    truncated: value.truncated ?? meta.truncated,
    arguments: value.arguments ?? value.args,
    command: value.command ?? preview.command,
    diff: value.diff ?? preview.diff,
    preview: value.preview,
    result,
    error:
      typeof meta.error === "string"
        ? meta.error
        : meta.error?.message || value.error
  };
}

export function formatAIStatus(status: string) {
  const labels: Record<string, string> = {
    queued: "排队中",
    running: "执行中",
    cancelling: "正在停止",
    waiting_approval: "等待批准",
    awaiting_approval: "等待批准",
    interrupted: "等待批准",
    pending: "等待批准",
    completed: "已完成",
    complete: "已完成",
    done: "已完成",
    success: "成功",
    failed: "失败",
    error: "失败",
    cancelled: "已停止",
    canceled: "已停止",
    rejected: "已拒绝",
    unknown: "执行结果不确定"
  };
  return labels[status] || status;
}
