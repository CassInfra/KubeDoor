import type {
  AgentInstallOptions,
  AgentInstallTask,
  InstallStatus
} from "@/api/agent-install";

export const MAX_KUBECONFIG_BYTES = 1024 * 1024;

export const INSTALL_STATUS_TEXT: Record<InstallStatus, string> = {
  draft: "等待检测",
  ready: "可以安装",
  running: "正在安装",
  succeeded: "Agent 已上线",
  partial: "Agent 已连接，指标采集异常",
  failed: "安装失败",
  interrupted: "安装中断，等待核对",
  expired: "临时凭据已过期"
};

export function suggestedClusterName(context: string): string {
  return context
    .replace(/[^A-Za-z0-9_-]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 64);
}

export function installFingerprint(
  context: string,
  options: AgentInstallOptions
): string {
  // 逐项固定字段顺序，切换 context 或编辑任意安装参数都要重新检测。
  return JSON.stringify([
    context,
    options.env,
    options.aliases || [],
    options.namespace,
    options.master_url,
    options.mode,
    options.image_repo,
    options.image_pull_secret || "",
    options.agent_tag,
    options.oss_url,
    options.ksm,
    options.node_exporter,
    options.remote_write_url
  ]);
}

export function installValidation(options: AgentInstallOptions): string {
  if (!/^[A-Za-z0-9_-]{1,64}$/.test(options.env))
    return "集群标识只能包含字母、数字、下划线和中划线，最长 64 位";
  if (
    !/^[a-z0-9]([-a-z0-9]*[a-z0-9])?$/.test(options.namespace) ||
    options.namespace.length > 63
  )
    return "命名空间应为最多 63 位的小写字母、数字和中划线";
  if (!options.master_url.trim()) return "请填写目标集群可访问的 KubeDoor 地址";
  try {
    const address = new URL(options.master_url);
    if (
      !["http:", "https:"].includes(address.protocol) ||
      !address.hostname ||
      address.username ||
      address.password
    )
      return "KubeDoor 地址应为不含账号口令的 HTTP 或 HTTPS 地址";
  } catch {
    return "请输入有效的 KubeDoor 地址，例如 https://kubedoor.example.com";
  }
  return "";
}

export function recoverableInstall(
  tasks: AgentInstallTask[],
  preferredId?: string | null
): AgentInstallTask | undefined {
  const unfinished = tasks.filter(task =>
    ["draft", "ready", "running", "interrupted", "failed", "partial"].includes(
      task.status
    )
  );
  return (
    unfinished.find(task => task.id === preferredId) ||
    [...unfinished].sort((a, b) =>
      (b.updated_at || b.created_at || "").localeCompare(
        a.updated_at || a.created_at || ""
      )
    )[0]
  );
}

export function installError(reason: unknown): string {
  const error = reason as {
    message?: string;
    response?: { data?: { message?: string; error?: string } };
  };
  return (
    error?.response?.data?.message ||
    error?.response?.data?.error ||
    error?.message ||
    "请求失败，请检查连接后重试"
  );
}
