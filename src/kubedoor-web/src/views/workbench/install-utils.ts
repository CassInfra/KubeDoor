import type {
  AgentInstallOptions,
  AgentInstallSettings,
  AgentInstallTask,
  InstallStatus
} from "@/api/agent-install";

export const MAX_KUBECONFIG_BYTES = 1024 * 1024;

/** 通用配置页（新标签页打开），填完回到对话框点「重新读取」 */
export const GENERAL_SETTINGS_URL = "#/system/general-settings";

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
 * 安装对话框能修改的参数及默认值。集群别名、镜像仓库、镜像拉取 Secret、远程写地址、对象存储地址都不在对话框里：
 * 别名装好后在 Agent 列表里编辑，其余用通用配置。
 */
export function emptyInstallOptions(): AgentInstallOptions {
  return {
    env: "",
    namespace: "kubedoor",
    master_url: "",
    mode: "full",
    agent_tag: "",
    ksm: "auto",
    node_exporter: "auto"
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

/**
 * 已检测的计划对应的参数指纹：切换 context、编辑任意安装参数都要重新检测。
 * extra 是参数以外也要重新检测的内容（通知配置、重新读取到的通用配置）
 */
export function installFingerprint(
  context: string,
  options: AgentInstallOptions,
  extra: unknown = null
): string {
  // 逐项固定字段顺序
  return JSON.stringify([
    context,
    options.env,
    options.namespace,
    options.master_url,
    options.mode,
    options.agent_tag,
    options.ksm,
    options.node_exporter,
    extra ?? null
  ]);
}

/** 安装要用、通用配置里还没有的项（中文名称与通用配置页一致）；required 为 true 的缺了不能完整接入 */
export function missingSettings(
  settings: AgentInstallSettings | null | undefined
): Array<{
  key: "metrics_write_url" | "oss_url";
  label: string;
  required: boolean;
}> {
  const items = [
    { key: "metrics_write_url" as const, label: "远程写地址", required: true },
    { key: "oss_url" as const, label: "对象存储上传地址", required: false }
  ];
  return items.filter(item => !(settings?.[item.key] || "").trim());
}

/** 通用配置里没有某一项时的提醒 */
export function settingsReminder(label: string): string {
  return `通用配置里还没有${label}。在系统管理 → 通用配置填写一次，以后安装 Agent 自动使用`;
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
