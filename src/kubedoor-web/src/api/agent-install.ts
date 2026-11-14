import { http } from "@/utils/http";

export type InstallStatus =
  | "draft"
  | "ready"
  | "running"
  | "succeeded"
  | "partial"
  | "failed"
  | "interrupted"
  | "expired";

/** 网页「安装 Agent」提交的参数；镜像仓库用控制端配置，集群别名装好后在 Agent 列表里编辑 */
export interface AgentInstallOptions {
  env: string;
  namespace: string;
  master_url: string;
  mode: "full" | "agent";
  agent_tag: string;
  oss_url: string;
  ksm: "auto" | "install" | "reuse";
  node_exporter: "auto" | "install" | "reuse";
  remote_write_url: string;
}

export interface AgentInstallDefaults extends Partial<AgentInstallOptions> {
  metrics_available: boolean;
}

export interface InstallCheck {
  name: string;
  status: "success" | "warning" | "error" | "pending" | "running" | "skipped";
  message?: string;
  /** 预检失败是因为目标集群里已有 Agent 资源：确认后可以强制覆盖安装 */
  forceable?: boolean;
}

/** 预检列出的、强制覆盖安装时要接管的已有资源 */
export interface InstallTakeover {
  kind: string;
  name: string;
  namespace?: string;
  reason: string;
}

export interface AgentInstallTask {
  id: string;
  status: InstallStatus;
  revision: number;
  context: string;
  contexts?: Array<{ name: string; cluster?: string; server?: string }>;
  options: AgentInstallOptions;
  checks?: InstallCheck[];
  steps?: InstallCheck[];
  resources?: Array<{ kind: string; name: string; namespace?: string }>;
  summary?: string;
  error?: string;
  created_at?: string;
  updated_at?: string;
  expires_at?: string;
  retryable?: boolean;
  target_locked?: boolean;
  /** 目标集群里已有、需要强制覆盖才能接管的资源 */
  takeover?: InstallTakeover[];
  /** 预检时已确认强制覆盖安装 */
  force?: boolean;
  /** Agent 上线后自动配置 AI Kubeconfig 的结果（Agent 服务账号令牌）；失败不影响安装 */
  ai_connection?: {
    status: "saved" | "skipped" | "failed";
    message: string;
  };
}

interface Result<T> {
  success: boolean;
  data: T;
  message?: string;
  error?: string;
}

const base = "/api/admin/agent-installations";

async function request<T>(
  method: "get" | "post",
  path: string,
  data?: unknown,
  timeout = 120000
): Promise<T> {
  const result = await http.request<Result<T>>(
    method,
    base + path,
    { data },
    { silent: true, timeout }
  );
  if (!result.success) {
    throw new Error(result.message || result.error || "安装请求失败，请重试");
  }
  return result.data;
}

export const agentInstallApi = {
  defaults: () => request<AgentInstallDefaults>("get", "/defaults"),
  list: () => request<AgentInstallTask[]>("get", ""),
  get: (id: string) =>
    request<AgentInstallTask>("get", `/${encodeURIComponent(id)}`),
  inspect: (kubeconfig: string) =>
    request<AgentInstallTask>("post", "/inspect", { kubeconfig }),
  /** force：确认接管目标集群里已有的 Agent 资源（预检报「已有 Agent 资源」后的强制覆盖安装） */
  preflight: (
    id: string,
    context: string,
    options: AgentInstallOptions,
    force = false
  ) =>
    request<AgentInstallTask>("post", `/${encodeURIComponent(id)}/preflight`, {
      context,
      options,
      ...(force ? { force: true } : {})
    }),
  start: (id: string, revision: number) =>
    request<AgentInstallTask>("post", `/${encodeURIComponent(id)}/start`, {
      revision
    }),
  retry: (id: string) =>
    request<AgentInstallTask>("post", `/${encodeURIComponent(id)}/retry`, {})
};
