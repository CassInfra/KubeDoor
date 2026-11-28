import { createSseParser, type SseFrame } from "@/utils/ai";
import type {
  LogBindingCondition,
  LogColumnNote,
  LogCorrelationKey,
  LogDurationUnit,
  LogLevelKey,
  LogScanGroup,
  LogScanResult,
  LogSemanticField,
  LogSourceCorrelation,
  LogSourceKind,
  LogSuggestedBinding
} from "./system";

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

/** 网页 AI 助手用哪个模型：wecom 企微专用模型（管理员配置，所有账号共用），custom 自己填写的 */
export type AIProviderMode = "wecom" | "custom";

/** 请求里的模型参数：自定义模型的完整配置，或者用企微专用模型（API Key 只在服务端） */
export type AIProviderRequest = AIProvider | { source: "wecom" };

/** 企微专用模型（bootstrap 返回，只读展示；不含 API Key） */
export interface AISharedModel {
  /** Base URL 和模型名都配好了，网页可以直接用 */
  available: boolean;
  base_url: string;
  model: string;
  max_input_tokens: number | null;
  max_output_tokens: number | null;
  compression_ratio: number | null;
  reasoning_effort: AIReasoningEffort | null;
  /** API Key 是否已配置（只有是否，不返回 Key 本身） */
  api_key_set: boolean;
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
  /** 当前用户对该集群的有效权限:admin 管理员 / rw 读写 / read 只读(看不到的集群不会返回) */
  permission?: "admin" | "rw" | "read";
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
  /** 全局角色:admin / cluster_admin / rw / read / none */
  permission: string;
  /** 是否系统管理员 */
  is_admin?: boolean;
  clusters: AICluster[];
  skills: AISkill[];
  /** 企微专用模型；旧版 AI 服务没有这个字段 */
  shared_model?: AISharedModel | null;
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
  /** 分享页：查看人对这条消息的集群没有只读权限，工具调用的参数和结果没有返回 */
  tools_hidden?: boolean;
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
  /** 会话列表：是否正在分享 */
  shared?: boolean;
  /** 会话详情：分享码（分享链接里的随机码），null 表示没有分享 */
  share_code?: string | null;
  shared_at?: string | null;
  /** 会话列表：是否有公网分享快照 */
  public_shared?: boolean;
  /** 会话详情：公网分享地址（Cloudflare Workers），null 表示没有发布 */
  public_share_url?: string | null;
  public_shared_at?: string | null;
}

/** 开关分享的结果 */
export interface AIShareState {
  id: string;
  shared: boolean;
  share_code: string | null;
  shared_at: string | null;
}

/** 分享页看到的会话：匿名，不含会话 id 和账号 */
export interface AIShareView {
  title: string;
  shared_at: string;
  updated_at: string;
  /** 进行中的一轮；回答要等这一轮结束才写进消息 */
  active_run: { status: string } | null;
  messages: AIMessage[];
}

/** 公网分享（Cloudflare Workers）的管理配置；API Token 只返回是否已设置 */
export interface AIPublicShareConfig {
  configured: boolean;
  account_id: string;
  worker_name: string;
  kv_namespace_id: string;
  public_base_url: string;
  api_token_set: boolean;
  updated_by: string | null;
  updated_at: string | null;
  /** 保存并部署后检查公网地址的结果；能打开为 null，否则是提醒文字 */
  warning?: string | null;
}

/** 管理页保存 / 测试时提交的 Cloudflare 配置；API Token 留空表示沿用已保存的 */
export interface AIPublicShareConfigInput {
  account_id: string;
  api_token?: string;
  worker_name: string;
  kv_namespace_id: string;
  public_base_url: string;
}

/** 发布 / 取消公网分享的结果；public_url 是 Cloudflare Worker 上的快照地址 */
export interface AIPublicShareState {
  id: string;
  public_shared: boolean;
  public_share_code: string | null;
  public_shared_at: string | null;
  public_url: string | null;
  message_count?: number;
}

export interface AIRun {
  id: string;
  status: string;
  event_seq?: number;
  scope?: AIScope;
  pending_actions?: Record<string, unknown>[];
  /** chat 网页对话；mcp 外部 MCP 客户端发起（在浏览器链接里等待批准） */
  origin?: string;
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
  /** upload：管理员手动上传；agent-install：网页安装 Agent 时用 Agent 服务账号自动配置 */
  origin?: "upload" | "agent-install";
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
  /** Bot Secret / 模型 API Key 是否保存过（不返回值本身；不叫 has_* 是因为 *_secret、*_api_key 会被服务端打码） */
  bot_secret_set: boolean;
  api_key_set: boolean;
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

export type AIMcpTransport = "streamable_http" | "sse";
/** 调用审批：readonly 声明只读的工具自动执行、其余需批准；never 全部自动执行；always 每次都需批准 */
export type AIMcpApproval = "readonly" | "never" | "always";

export interface AIMcpTool {
  name: string;
  title?: string | null;
  description?: string;
  /** MCP 服务声明了只读（readOnlyHint） */
  read_only: boolean;
}

export interface AIMcpServerInfo {
  name?: string;
  title?: string;
  version?: string;
  instructions?: string;
  protocol_version?: string;
}

/** 当前账号自己接入的外部 MCP（AI 助手的「MCP 管理」）；请求头只回显名称 */
export interface AIMcpServer {
  id: string;
  name: string;
  transport: AIMcpTransport;
  url: string;
  headers: { name: string }[];
  enabled: boolean;
  approval: AIMcpApproval;
  tools: AIMcpTool[];
  server_info?: AIMcpServerInfo | null;
  tools_at?: string | null;
  checked_at?: string | null;
  last_error?: string | null;
  created_at?: string;
  updated_at?: string;
}

export interface AIMcpServerInput {
  name: string;
  transport: AIMcpTransport;
  url: string;
  /** 编辑时值留空表示沿用已保存的同名请求头 */
  headers: { name: string; value: string }[];
  approval: AIMcpApproval;
  enabled: boolean;
}

export interface AIMcpTestResult {
  success: boolean;
  error?: string | null;
  tools: AIMcpTool[];
  server_info?: AIMcpServerInfo | null;
}

/** 历史日志「AI 生成说明」的请求：扫描结果（只放勾选的组）、KubeDoor 集群、已有配置的名字、管理员补充说明 */
export interface LogDescribeRequest {
  scan: {
    groups: LogScanGroup[];
    connection: LogScanResult["connection"];
    /** 扫描超时只返回了一部分 */
    partial?: boolean;
    notes?: string[];
    /** 扫描按规则给的绑定建议（只有勾选的组） */
    suggested_bindings?: LogSuggestedBinding[];
  };
  clusters: { env: string; aliases: string[] }[];
  existing: {
    sources: { id: string; name: string }[];
    correlation_keys: { key: string; label: string }[];
    architecture: string;
  };
  /** 管理员补充说明（≤ 4000） */
  hint: string;
}

/** 草稿里的日志源：group 是扫描结果里组的 ref */
export interface LogDraftSource {
  group: string;
  id: string;
  name: string;
  kind: LogSourceKind;
  description: string;
  time_field: string;
  fields: Partial<Record<LogSemanticField, string>>;
  duration_unit?: LogDurationUnit;
  level_values?: Partial<Record<LogLevelKey, string[]>>;
  cluster_field?: string;
  default_target?: string;
  columns?: LogColumnNote[];
  correlations?: LogSourceCorrelation[];
}

export interface LogDraftBinding {
  env: string;
  /** 草稿里的日志源 id，或者已有日志源的 id */
  source: string;
  target: string;
  conditions: LogBindingCondition[];
  note: string;
}

export interface LogDraft {
  sources: LogDraftSource[];
  correlation_keys: LogCorrelationKey[];
  bindings: LogDraftBinding[];
  cluster_notes: { env: string; note: string }[];
  architecture: string;
}

export interface LogDescribeResult {
  draft: LogDraft;
  /** 被丢掉的项等说明 */
  notes: string[];
  model: string;
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
  /** 开启 / 关闭会话分享（只有会话本人）；已分享时再开启返回原来的分享码 */
  setShare: (id: string, enabled: boolean) =>
    request<AIShareState>(`/sessions/${itemPath(id)}/share`, "PUT", {
      enabled
    }),
  /** 分享页：任何登录用户凭分享码查看（只读、匿名） */
  sharedSession: (code: string, signal?: AbortSignal) =>
    request<AIShareView>(
      `/shares/${itemPath(code)}`,
      "GET",
      undefined,
      undefined,
      signal
    ),
  /** 发布 / 取消公网分享（Cloudflare Workers 上的脱敏静态快照，只有会话本人） */
  publishPublicShare: (id: string) =>
    request<AIPublicShareState>(
      `/sessions/${itemPath(id)}/public-share`,
      "POST"
    ),
  unpublishPublicShare: (id: string) =>
    request<AIPublicShareState>(
      `/sessions/${itemPath(id)}/public-share`,
      "DELETE"
    ),
  /** 管理员：Cloudflare 公网分享配置（保存时会自动创建 / 绑定 KV 并部署 Worker） */
  publicShareConfig: () => request<AIPublicShareConfig>("/public-share-config"),
  savePublicShareConfig: (config: AIPublicShareConfigInput) =>
    request<AIPublicShareConfig>("/public-share-config", "PUT", config),
  testPublicShareConfig: (config: AIPublicShareConfigInput) =>
    request<{
      success: boolean;
      account_id: string;
      worker_name: string;
      kv_namespace_id: string;
    }>("/public-share-config/test", "POST", config),
  run: (
    id: string,
    body: {
      message: string;
      scope: AIScope;
      provider: AIProviderRequest;
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
  summarizeMemory: (
    id: string,
    provider: AIProviderRequest,
    signal?: AbortSignal
  ) =>
    request<AIMemoryDraft>(
      `/sessions/${itemPath(id)}/memory-summary`,
      "POST",
      { provider },
      undefined,
      signal,
      120000
    ),
  generateSessionTitle: (
    id: string,
    message: string,
    provider: AIProviderRequest
  ) =>
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
    provider: AIProviderRequest
  ) =>
    request<{ status?: string }>(`/runs/${itemPath(id)}/decisions`, "POST", {
      action_id: actionId,
      decision,
      provider
    }),
  cancel: (id: string) =>
    request<{ status?: string }>(`/runs/${itemPath(id)}/cancel`, "POST", {}),
  testProvider: (provider: AIProviderRequest) =>
    request<Record<string, any>>("/provider/test", "POST", { provider }),
  /** 以下是当前账号自己接入的外部 MCP；保存、测试、刷新时服务端会连一次 MCP 拿工具列表 */
  mcpServers: () => request<{ servers: AIMcpServer[] }>("/mcp-servers"),
  createMcpServer: (data: AIMcpServerInput) =>
    request<AIMcpServer>("/mcp-servers", "POST", data),
  updateMcpServer: (id: string, data: AIMcpServerInput) =>
    request<AIMcpServer>(`/mcp-servers/${itemPath(id)}`, "PUT", data),
  /** 列表上的启用开关和调用审批：不改连接配置，不重新连接 */
  patchMcpServer: (
    id: string,
    data: { enabled?: boolean; approval?: AIMcpApproval }
  ) => request<AIMcpServer>(`/mcp-servers/${itemPath(id)}`, "PATCH", data),
  deleteMcpServer: (id: string) =>
    request<{ success: boolean }>(`/mcp-servers/${itemPath(id)}`, "DELETE"),
  refreshMcpServer: (id: string) =>
    request<AIMcpServer>(`/mcp-servers/${itemPath(id)}/refresh`, "POST", {}),
  /** 只测试不保存；id 用于沿用已保存的请求头 */
  testMcpServer: (data: AIMcpServerInput & { id?: string }) =>
    request<AIMcpTestResult>("/mcp-servers/test", "POST", data),
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

/**
 * 历史日志「AI 生成说明」（仅系统管理员）：用企微专用模型按扫描结果生成配置草稿，不保存。
 * 服务端最长等模型 180 秒，浏览器多等 20 秒；请求体由页面控制在 1.5MB 以内（nginx 限制 2MB）。
 */
export const describeLogSources = (body: LogDescribeRequest) =>
  request<LogDescribeResult>(
    "/log-sources/describe",
    "POST",
    body,
    undefined,
    undefined,
    200000
  );

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
