import { createSseParser, type SseFrame } from "@/utils/ai";

/** DeepSeek 思考强度；null 表示用模型默认（high），请求里不带参数 */
export type AIReasoningEffort = "off" | "low" | "high" | "max";

export interface AIProvider {
  base_url: string;
  api_key: string;
  model: string;
  /** 上下文窗口（token）；null 表示由服务端按模型推断 */
  max_input_tokens?: number | null;
  /** 最大输出（token）；null 表示由服务端按模型推断 */
  max_output_tokens?: number | null;
  /** 压缩阈值（0.2–0.8，上下文达到窗口的这个比例时自动摘要）；null 表示默认 0.5 */
  compression_ratio?: number | null;
  reasoning_effort?: AIReasoningEffort | null;
}

export interface AIResource {
  namespace?: string;
  name: string;
}

export interface AIScope {
  env: string;
  namespace: string | null;
  deployment: AIResource | null;
  pod: AIResource | null;
}

export interface AICluster {
  env: string;
  online: boolean;
  configured: boolean;
  /** 当前用户对该集群的有效权限:rw 读写 / read 只读(看不到的集群不会返回) */
  permission?: "rw" | "read";
  context?: string;
  test_status?: string;
}

export interface AISkill {
  id?: string;
  name: string;
  description?: string;
}

export interface AIBootstrap {
  enabled: boolean;
  username: string;
  /** 全局角色:admin / rw / read / none */
  permission: string;
  clusters: AICluster[];
  skills: AISkill[];
}

export interface AITool {
  id: string;
  action_id?: string;
  tool_call_id?: string;
  name: string;
  status: string;
  env?: string;
  namespace?: string;
  source?: string;
  observed_at?: string;
  freshness?: string;
  truncated?: boolean;
  arguments?: unknown;
  command?: unknown;
  diff?: unknown;
  preview?: unknown;
  result?: unknown;
  error?: string;
  pending?: boolean;
}

export interface AIMessage {
  id: string;
  role: string;
  content: string;
  scope?: AIScope;
  tools?: AITool[];
  created_at?: string;
  memories?: AIMemoryAttachment[];
}

export interface AIMemoryAttachment {
  id: string;
  title: string;
  content: string;
  version: number;
}

export interface AIMemorySummary {
  id: string;
  title: string;
  version: number;
  created_by: string;
  updated_by: string;
  created_at: string;
  updated_at: string;
}

export interface AIMemory extends AIMemorySummary {
  content: string;
}

export interface AIMemoryDraft {
  title: string;
  content: string;
}

/** 一轮的模型用量（kubedoor_ai_runs.usage，done 事件里也会带上） */
export interface AIUsage {
  model_calls?: number;
  input_tokens?: number;
  output_tokens?: number;
  cache_read_tokens?: number;
  reasoning_tokens?: number;
  /** 主 Agent 最近一次请求的输入，即当前上下文大小 */
  last_input_tokens?: number;
  max_input_tokens?: number;
}

export interface AISession {
  id: string;
  title: string;
  created_at?: string;
  updated_at?: string;
  messages?: AIMessage[];
  active_run?: AIRun | string | null;
  /** 最近一轮的用量，用来显示上下文占用 */
  usage?: AIUsage | null;
  draft?: boolean;
  /** 搜索结果里命中的那段消息 */
  snippet?: string;
}

export interface AIRun {
  id: string;
  status: string;
  event_seq?: number;
  scope?: AIScope;
  pending_actions?: Record<string, unknown>[];
}

export interface AIEvent {
  seq: number;
  type: string;
  data: Record<string, any> | string;
}

export interface AIConnection {
  env: string;
  configured?: boolean;
  context?: string;
  server?: string;
  test_status?: string;
  tested_at?: string;
  updated_at?: string;
  version?: number;
  error?: string;
}

export interface WeComConfig {
  enabled: boolean;
  configured: boolean;
  connected: boolean;
  authenticated: boolean;
  reconnecting: boolean;
  error?: string | null;
  bot_id?: string;
  base_url?: string;
  model?: string;
  max_input_tokens?: number | null;
  max_output_tokens?: number | null;
  compression_ratio?: number | null;
  reasoning_effort?: AIReasoningEffort | null;
  /** 对话空闲多少分钟后，新消息自动进入新对话；0 表示关闭 */
  session_idle_minutes?: number;
  has_bot_secret: boolean;
  has_api_key: boolean;
  updated_by?: string;
  updated_at?: string;
}

export interface WeComUser {
  wecom_userid: string;
  username: string | null;
  default_env: string | null;
  current_env: string | null;
  /** 管理员写的备注(比如姓名) */
  remark: string | null;
  /** 成员在企微里发「我是 张三」自报的名字,只用于辨认 */
  self_name: string | null;
  /** 绑定人:管理员绑定是管理员账号,成员用绑定码自助绑定是他本人的账号 */
  bound_by: string | null;
  bound_at: string | null;
  first_seen_at: string;
  last_seen_at: string;
  pending_approvals: number;
}

/** 当前花折账号绑定的企微成员(右上角用户菜单「绑定企业微信」) */
export interface WeComBinding {
  /** 管理员是否启用了企微机器人 */
  enabled: boolean;
  connected: boolean;
  members: {
    wecom_userid: string;
    self_name: string | null;
    bound_by: string | null;
    bound_at: string | null;
    last_seen_at: string;
  }[];
  /** 绑定码有效分钟数 */
  code_minutes: number;
}

/** 未绑定成员的默认权限账号和默认集群 */
export interface WeComUnbound {
  unbound_username: string | null;
  unbound_default_env: string | null;
}

export class AIRequestError extends Error {
  constructor(
    message: string,
    public status: number,
    public code?: string
  ) {
    super(message);
    this.name = "AIRequestError";
  }
}

const prefix = "/api/ai";
const itemPath = (id: string) => encodeURIComponent(id);

async function assertResponse(response: Response): Promise<void> {
  if (response.ok) return;
  // nginx 用 master 校验登录会话,会话失效时这里收到 401:通知全局回登录页(见 utils/http)
  if (response.status === 401) {
    window.dispatchEvent?.(new CustomEvent("kubedoor:unauthorized"));
  }
  let detail = "";
  let code: string | undefined;
  try {
    const body = await response.json();
    const value = body.detail ?? body.error ?? body.message;
    if (typeof value?.code === "string") code = value.code;
    detail =
      typeof value === "string"
        ? value
        : typeof value?.message === "string"
          ? value.message
          : JSON.stringify(value ?? "");
  } catch {
    // Reverse proxies may return HTML. Never insert it into the chat.
  }
  throw new AIRequestError(
    detail || `请求失败（HTTP ${response.status}）`,
    response.status,
    code
  );
}

async function request<T>(
  path: string,
  method = "GET",
  body?: unknown,
  idempotencyKey?: string,
  signal?: AbortSignal,
  timeout = 60000
): Promise<T> {
  const controller = new AbortController();
  const abort = () => controller.abort();
  if (signal?.aborted) controller.abort();
  else signal?.addEventListener("abort", abort, { once: true });
  const timer = window.setTimeout(() => controller.abort(), timeout);
  try {
    const headers: Record<string, string> = { Accept: "application/json" };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;
    const response = await fetch(`${prefix}${path}`, {
      method,
      headers,
      credentials: "same-origin",
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal
    });
    await assertResponse(response);
    if (response.status === 204) return {} as T;
    return (await response.json()) as T;
  } catch (error) {
    if (error instanceof Error && error.name === "AbortError") {
      if (signal?.aborted) throw error;
      throw new Error("请求超时，请刷新状态后重试；已提交的操作可能仍在运行。");
    }
    throw error;
  } finally {
    window.clearTimeout(timer);
    signal?.removeEventListener("abort", abort);
  }
}

export const aiApi = {
  bootstrap: () => request<AIBootstrap>("/bootstrap"),
  resources: (
    kind: "namespaces" | "deployments" | "pods",
    scope: AIScope,
    signal?: AbortSignal
  ) => {
    const params = new URLSearchParams({ env: scope.env, kind });
    if (kind !== "namespaces" && scope.namespace)
      params.set("namespace", scope.namespace);
    if (kind === "pods" && scope.deployment) {
      params.set("deployment", scope.deployment.name);
      if (scope.deployment.namespace) {
        params.set("deployment_namespace", scope.deployment.namespace);
      }
    }
    return request<{ items: AIResource[] }>(
      `/resources?${params}`,
      "GET",
      undefined,
      undefined,
      signal
    );
  },
  /** query 非空时按标题和消息内容搜索本人的会话 */
  sessions: (query = "") =>
    request<{ sessions: AISession[] }>(
      query ? `/sessions?q=${encodeURIComponent(query)}` : "/sessions"
    ),
  createSession: (key: string, title = "新会话") =>
    request<AISession>("/sessions", "POST", { title }, key),
  session: (id: string, signal?: AbortSignal) =>
    request<AISession>(
      `/sessions/${itemPath(id)}`,
      "GET",
      undefined,
      undefined,
      signal
    ),
  renameSession: (id: string, title: string) =>
    request<AISession>(`/sessions/${itemPath(id)}`, "PATCH", { title }),
  deleteSession: (id: string) => request(`/sessions/${itemPath(id)}`, "DELETE"),
  run: (
    id: string,
    body: {
      message: string;
      scope: AIScope;
      provider: AIProvider;
      skill_ids?: string[];
      memory_ids?: string[];
      /** 重新生成 / 编辑后重发：替换最后一轮 */
      replace_last?: boolean;
    },
    key: string
  ) =>
    request<{ run_id: string; status: string; message?: AIMessage }>(
      `/sessions/${itemPath(id)}/runs`,
      "POST",
      body,
      key
    ),
  listMemories: (
    params: { search?: string; page?: number; page_size?: number } = {},
    signal?: AbortSignal
  ) => {
    const query = new URLSearchParams({
      search: params.search || "",
      page: String(params.page || 1),
      page_size: String(params.page_size || 20)
    });
    return request<{
      memories: AIMemorySummary[];
      total: number;
      page: number;
      page_size: number;
    }>(`/memories?${query}`, "GET", undefined, undefined, signal);
  },
  memory: (id: string, signal?: AbortSignal) =>
    request<AIMemory>(
      `/memories/${itemPath(id)}`,
      "GET",
      undefined,
      undefined,
      signal
    ),
  createMemory: (draft: AIMemoryDraft) =>
    request<AIMemory>("/memories", "POST", draft),
  updateMemory: (id: string, draft: AIMemoryDraft & { version: number }) =>
    request<AIMemory>(`/memories/${itemPath(id)}`, "PATCH", draft),
  deleteMemory: (id: string, version: number) =>
    request<{ success: boolean }>(
      `/memories/${itemPath(id)}?version=${encodeURIComponent(version)}`,
      "DELETE"
    ),
  summarizeMemory: (id: string, provider: AIProvider, signal?: AbortSignal) =>
    request<AIMemoryDraft>(
      `/sessions/${itemPath(id)}/memory-summary`,
      "POST",
      { provider },
      undefined,
      signal,
      120000
    ),
  generateSessionTitle: (id: string, message: string, provider: AIProvider) =>
    request<{ id: string; title: string; generated: boolean }>(
      `/sessions/${itemPath(id)}/title`,
      "POST",
      { message, provider },
      undefined,
      undefined,
      25000
    ),
  decision: (
    id: string,
    actionId: string,
    decision: "approve" | "reject",
    provider: AIProvider
  ) =>
    request<{ status?: string }>(`/runs/${itemPath(id)}/decisions`, "POST", {
      action_id: actionId,
      decision,
      provider
    }),
  cancel: (id: string) =>
    request<{ status?: string }>(`/runs/${itemPath(id)}/cancel`, "POST", {}),
  testProvider: (provider: AIProvider) =>
    request<Record<string, any>>("/provider/test", "POST", { provider }),
  wecomConfig: () => request<WeComConfig>("/wecom"),
  saveWecomConfig: (data: {
    enabled: boolean;
    bot_id: string;
    bot_secret?: string;
    base_url: string;
    api_key?: string;
    model: string;
    max_input_tokens: number | null;
    max_output_tokens: number | null;
    compression_ratio: number | null;
    reasoning_effort: AIReasoningEffort | null;
    session_idle_minutes: number;
  }) => request<WeComConfig>("/wecom", "PUT", data),
  testWecomProvider: (provider: AIProvider) =>
    request<Record<string, any>>("/wecom/test", "POST", { provider }),
  wecomUsers: () =>
    request<{ users: WeComUser[] } & WeComUnbound>("/wecom/users"),
  saveWecomUser: (data: {
    wecom_userid: string;
    username: string;
    default_env?: string | null;
  }) => request<{ success: boolean }>("/wecom/users", "POST", data),
  /** 未绑定成员按 username 的权限使用机器人(null = 必须先绑定);defaultEnv 为 null 时要先切换集群才能提问 */
  saveWecomUnbound: (username: string | null, defaultEnv: string | null) =>
    request<WeComUnbound>("/wecom/unbound", "PUT", {
      username,
      default_env: defaultEnv
    }),
  deleteWecomUser: (userid: string) =>
    request<{ success: boolean }>(`/wecom/users/${itemPath(userid)}`, "DELETE"),
  /** 清理企微成员:下次发消息时按新成员接入(停掉进行中的任务,对话不能再继续) */
  purgeWecomUser: (userid: string) =>
    request<{ success: boolean; sessions: number; stopped: number }>(
      `/wecom/users/${itemPath(userid)}/purge`,
      "POST",
      {}
    ),
  saveWecomRemark: (userid: string, remark: string | null) =>
    request<{ remark: string | null }>(
      `/wecom/users/${itemPath(userid)}/remark`,
      "PUT",
      { remark }
    ),
  /** 以下三个任何登录用户都能用:管理自己账号绑定的企微成员 */
  wecomBinding: () => request<WeComBinding>("/wecom/binding"),
  createWecomBindCode: () =>
    request<{ code: string; expires_at: string; minutes?: number }>(
      "/wecom/binding/code",
      "POST",
      {}
    ),
  unbindWecom: (userid: string) =>
    request<{ success: boolean }>(
      `/wecom/binding/${itemPath(userid)}`,
      "DELETE"
    ),
  connections: () => request<{ connections: AIConnection[] }>("/connections"),
  parseConnection: (content: string) =>
    request<{
      contexts: { name: string; namespace?: string; server?: string }[];
      current_context?: string;
    }>("/connections/parse", "POST", { content }),
  testConnection: (env: string, content?: string, context?: string) =>
    request<Record<string, any>>(
      `/connections/${itemPath(env)}/test`,
      "POST",
      content ? { content, context } : {}
    ),
  saveConnection: (env: string, content: string, context: string) =>
    request<AIConnection>(`/connections/${itemPath(env)}`, "PUT", {
      content,
      context
    }),
  deleteConnection: (env: string) =>
    request(`/connections/${itemPath(env)}`, "DELETE")
};

export async function streamAIEvents(
  runId: string,
  after: number,
  signal: AbortSignal,
  onEvent: (event: AIEvent) => void
): Promise<void> {
  const response = await fetch(
    `${prefix}/runs/${itemPath(runId)}/events?after=${after}`,
    {
      headers: {
        Accept: "text/event-stream",
        "Last-Event-ID": String(after)
      },
      credentials: "same-origin",
      signal
    }
  );
  await assertResponse(response);
  if (!response.body)
    throw new Error("浏览器不支持流式响应，请使用现代浏览器。");
  const parser = createSseParser((frame: SseFrame) => {
    if (!frame.data || frame.data === "[DONE]") return;
    const parsed = JSON.parse(frame.data);
    const event = {
      seq: Number(parsed.seq ?? frame.id ?? 0),
      type: parsed.type ?? frame.event ?? "message",
      data: parsed.data ?? parsed
    } as AIEvent;
    onEvent(event);
  });
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  try {
    while (!signal.aborted) {
      const { done, value } = await reader.read();
      if (done) break;
      parser.push(decoder.decode(value, { stream: true }));
    }
    parser.push(decoder.decode());
    parser.finish();
  } finally {
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}
