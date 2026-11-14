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

/**
 * 安装对话框能修改的参数及默认值。集群别名、镜像仓库、镜像拉取 Secret 不在对话框里：
 * 别名装好后在 Agent 列表里编辑，镜像仓库用控制端的配置。
 */
export function emptyInstallOptions(): AgentInstallOptions {
  return {
    env: "",
    namespace: "kubedoor",
    master_url: "",
    mode: "full",
    agent_tag: "",
    oss_url: "",
    ksm: "auto",
    node_exporter: "auto",
    remote_write_url: ""
  };
}

/** 从安装任务或默认值里只取对话框能修改的参数：服务端返回的其他参数（别名、镜像仓库等）不再提交回去 */
export function pickInstallOptions(
  source?: Partial<AgentInstallOptions> | null
): AgentInstallOptions {
  const result = emptyInstallOptions();
  // 与 result 是同一个对象，用宽松的值类型逐项覆盖
  const target: Partial<Record<keyof AgentInstallOptions, unknown>> = result;
  for (const key of Object.keys(result) as Array<keyof AgentInstallOptions>) {
    const value = source?.[key];
    if (value !== undefined && value !== null) target[key] = value;
  }
  return result;
}

export function installFingerprint(
  context: string,
  options: AgentInstallOptions
): string {
  // 逐项固定字段顺序，切换 context 或编辑任意安装参数都要重新检测。
  return JSON.stringify([
    context,
    options.env,
    options.namespace,
    options.master_url,
    options.mode,
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
