import { createSseParser, type SseFrame } from "@/utils/ai";

export interface AIProvider {
  base_url: string;
  api_key: string;
  model: string;
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

export interface AISession {
  id: string;
  title: string;
  created_at?: string;
  updated_at?: string;
  messages?: AIMessage[];
  active_run?: AIRun | string | null;
  draft?: boolean;
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
  sessions: () => request<{ sessions: AISession[] }>("/sessions"),
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
