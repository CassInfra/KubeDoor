import type { AIScope } from "@/api/ai";

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

/** Context share at which the UI suggests a new session (same as the WeCom hint). */
export const CONTEXT_HINT_RATIO = 0.25;

/** Latest prompt size of the main agent divided by the model window, or null. */
export function contextShare(
  usage?: { last_input_tokens?: number; max_input_tokens?: number } | null
) {
  const used = usage?.last_input_tokens;
  const window = usage?.max_input_tokens;
  return typeof used === "number" &&
    typeof window === "number" &&
    used > 0 &&
    window > 0
    ? used / window
    : null;
}

/**
 * Context window defaults by model name, same rules as the AI service
 * (models.resolve_model_limits): deepseek* 1024K / 384K, gpt* 1024K / 128K,
 * anything else is prefilled with 1024K / 128K. `verify` marks names whose
 * real window may be smaller (other models and older gpt-3/gpt-4 releases).
 */
export function inferModelLimits(model: string) {
  const id = model.trim().toLowerCase().split("/").pop() || "";
  const deepseek = id.startsWith("deepseek");
  const recentGpt = id.startsWith("gpt") && !/^gpt-[34]/.test(id);
  return {
    max_input_tokens: 1024 * 1024,
    max_output_tokens: (deepseek ? 384 : 128) * 1024,
    compression_ratio: 0.5,
    verify: !deepseek && !recentGpt
  };
}

export interface AIModelLimits {
  max_input_tokens: number | null;
  max_output_tokens: number | null;
  compression_ratio: number | null;
}

const LIMIT_KEYS = [
  "max_input_tokens",
  "max_output_tokens",
  "compression_ratio"
] as const;

const validNumber = (value: unknown) =>
  typeof value === "number" && Number.isFinite(value) ? value : null;

/** Saved values win; empty values (older settings) are prefilled by name. */
export function modelLimitsFor(
  model: string,
  saved?: Partial<Record<keyof AIModelLimits, unknown>> | null
): AIModelLimits {
  const inferred = inferModelLimits(model);
  const result = {} as AIModelLimits;
  for (const key of LIMIT_KEYS)
    result[key] = validNumber(saved?.[key]) ?? inferred[key];
  return result;
}

/**
 * A renamed model re-prefills the values that still equal the previous
 * model's defaults; values the user changed by hand are kept.
 */
export function followModelLimits(
  values: AIModelLimits,
  previous: string,
  model: string
): AIModelLimits {
  const before = inferModelLimits(previous);
  const after = inferModelLimits(model);
  const result = { ...values };
  for (const key of LIMIT_KEYS)
    if (result[key] === null || result[key] === before[key])
      result[key] = after[key];
  return result;
}

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

const isRecord = (value: unknown): value is Record<string, any> =>
  !!value && typeof value === "object" && !Array.isArray(value);
const textList = (value: unknown) =>
  Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : [];

export interface AIControlStep {
  name: string;
  target: string;
  request: unknown;
  diff?: string;
}

export interface AIControlChange {
  scope: string;
  label: string;
  current: string;
  target: string;
}

export interface AIControlPlan {
  state: string;
  controlled: boolean;
  blocked: boolean;
  message: string;
  steps: AIControlStep[];
  changes: AIControlChange[];
  effects: string[];
  warnings: string[];
}

/**
 * The plan the gateway builds from the KubeDoor resource-control (admission
 * webhook) state of a Deployment; null for previews without one.
 */
export function controlPlan(preview: unknown): AIControlPlan | null {
  const plan = isRecord(preview) ? preview.plan : null;
  if (!isRecord(plan) || !Array.isArray(plan.steps)) return null;
  const control = isRecord(plan.control) ? plan.control : {};
  return {
    state: String(control.state ?? ""),
    controlled: control.controlled === true,
    blocked: control.blocked === true,
    message: String(control.message ?? ""),
    steps: plan.steps.filter(isRecord).map(step => {
      const direct = step.kind === "k8s";
      return {
        name: String(step.name ?? ""),
        target: direct
          ? `直接修改 Deployment（经 ${step.source === "direct" ? "kubeconfig" : "Agent"}）`
          : `${step.method ?? ""} ${step.path ?? ""}`.trim(),
        request: direct
          ? step.arguments
          : { query: step.query, body: step.body },
        diff: typeof step.diff === "string" && step.diff ? step.diff : undefined
      };
    }),
    changes: (Array.isArray(plan.changes) ? plan.changes : [])
      .filter(isRecord)
      .map(change => ({
        scope: String(change.scope ?? ""),
        label: String(change.label ?? change.field ?? ""),
        current: String(change.current ?? ""),
        target: String(change.target ?? "")
      })),
    effects: textList(plan.effects),
    warnings: textList(plan.warnings)
  };
}

export function controlBadge(
  plan: Pick<AIControlPlan, "controlled" | "blocked">
) {
  if (plan.blocked)
    return { label: "未登记管控，修改会被拒绝", type: "danger" as const };
  return plan.controlled
    ? { label: "受花折资源管控", type: "warning" as const }
    : { label: "不受花折资源管控", type: "info" as const };
}

export interface AIVerification {
  checked: boolean;
  matched: boolean | null;
  message: string;
}

/** Read-back the gateway attaches after changing a Deployment. */
export function verificationOf(result: unknown): AIVerification | null {
  const value = isRecord(result) ? result.verification : null;
  if (!isRecord(value) || typeof value.message !== "string") return null;
  return {
    checked: value.checked === true,
    matched: typeof value.matched === "boolean" ? value.matched : null,
    message: value.message
  };
}

/** 页面上「问 AI」：预选的资源范围和放进输入框的问题（用户确认后再发送） */
export interface AIAskRequest {
  scope: Partial<AIScope>;
  prompt: string;
}

const facts = (pairs: Array<[string, unknown]>) =>
  pairs
    .filter(
      ([, value]) => value !== undefined && value !== null && value !== ""
    )
    .map(([label, value]) => `${label}：${value}`)
    .join("\n");

const resource = (namespace: string | null, name: unknown) =>
  namespace && typeof name === "string" && name ? { namespace, name } : null;

// 资源选择是逐级的：不知道所属 Deployment 时 Pod 选不上（选项加载不了），只选到命名空间，Pod 名写进问题里

/** 告警详情的一行：选到告警所在的命名空间，带上告警原文 */
export function alertQuestion(row: Record<string, any>): AIAskRequest {
  const namespace = row.namespace || null;
  return {
    scope: { env: row.env, namespace },
    prompt:
      "帮我分析这条告警的原因，并给出处理建议：\n" +
      facts([
        ["告警", row.alert_name],
        ["级别", row.severity],
        ["状态", row.alert_status],
        ["Pod", row.pod],
        ["开始时间", row.start_time],
        ["触发次数", row.count_firing],
        ["描述", row.description]
      ])
  };
}

/** K8S 事件的一行（数组）：Deployment 的事件选中它，其他资源选到命名空间 */
export function eventQuestion(env: string, row: unknown[]): AIAskRequest {
  const [
    ,
    level,
    count,
    kind,
    ,
    namespace,
    name,
    reason,
    message,
    first,
    last
  ] = row as any[];
  const ns = namespace || null;
  return {
    scope: {
      env,
      namespace: ns,
      deployment: kind === "Deployment" ? resource(ns, name) : null
    },
    prompt:
      "帮我分析这条 K8S 事件，找出原因并给出处理建议：\n" +
      facts([
        [
          "资源",
          [kind, ns && name ? `${ns}/${name}` : name].filter(Boolean).join(" ")
        ],
        ["级别", level],
        ["原因", reason],
        ["次数", count],
        ["首次时间", first],
        ["最后时间", last],
        ["消息", message]
      ])
  };
}

export function podQuestion(
  env: string,
  namespace: string,
  pod: string,
  deployment?: string
): AIAskRequest {
  const owner = resource(namespace, deployment);
  return {
    scope: {
      env,
      namespace,
      deployment: owner,
      pod: owner ? resource(namespace, pod) : null
    },
    prompt: `排查 Pod ${namespace}/${pod}：看看状态、重启原因、日志、事件和资源使用有没有异常，找出原因并给出处理建议。`
  };
}

export function deploymentQuestion(
  env: string,
  namespace: string,
  deployment: string
): AIAskRequest {
  return {
    scope: { env, namespace, deployment: resource(namespace, deployment) },
    prompt: `检查 Deployment ${namespace}/${deployment}：副本是否都正常、资源使用和最近的事件有没有异常，有问题给出处理建议。`
  };
}
