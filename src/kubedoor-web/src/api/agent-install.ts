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

export interface AgentInstallOptions {
  env: string;
  aliases: string[];
  namespace: string;
  master_url: string;
  mode: "full" | "agent";
  image_repo: string;
  image_pull_secret?: string;
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
  preflight: (id: string, context: string, options: AgentInstallOptions) =>
    request<AgentInstallTask>("post", `/${encodeURIComponent(id)}/preflight`, {
      context,
      options
    }),
  start: (id: string, revision: number) =>
    request<AgentInstallTask>("post", `/${encodeURIComponent(id)}/start`, {
      revision
    }),
  retry: (id: string) =>
    request<AgentInstallTask>("post", `/${encodeURIComponent(id)}/retry`, {})
};
