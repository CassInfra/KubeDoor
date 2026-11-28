import type {
  AgentInstallOptions,
  AgentInstallRepair,
  AgentInstallSettings,
  AgentInstallTask,
  InstallStatus,
  InstallTakeover
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
    ...collectorPreset("full")
  };
}

type Collectors = Pick<
  AgentInstallOptions,
  "vmagent" | "ksm" | "node_exporter"
>;

/** 安装方式对应的三个采集组件默认值：完整安装装 vmagent、另外两个自动检测；仅安装 kubedoor-agent 三个都不装 */
export function collectorPreset(mode: AgentInstallOptions["mode"]): Collectors {
  return mode === "full"
    ? { vmagent: "install", ksm: "auto", node_exporter: "auto" }
    : { vmagent: "reuse", ksm: "reuse", node_exporter: "reuse" };
}

/** 三个采集组件的选项；reuse 显示为「不安装」：装 vmagent 时它去抓集群里已有的，不装时由已有的采集去抓 */
export const COLLECTOR_CHOICES = {
  component: [
    { value: "auto", label: "自动检测" },
    { value: "install", label: "安装" },
    { value: "reuse", label: "不安装" }
  ],
  vmagent: [
    { value: "install", label: "安装" },
    { value: "reuse", label: "不安装" }
  ]
} as const;

/**
 * 「自动检测」怎么判断（master agent_install/service.py 的 components，每次检测都重新判断）。
 * 安装向导第 6 步（views/setup/index.vue）写着同样的说明，改规则时两处一起改
 */
export const AUTO_DETECT_HINT =
  "自动检测：kube-state-metrics 看集群里有没有带 app.kubernetes.io/name=kube-state-metrics 标签、端口 8080 的 Service；node-exporter 看是不是每个 Linux 节点上都有使用主机网络、声明 9100 端口的运行中 Pod。有就复用，没有就安装；node-exporter 只有部分节点有时检测不通过（再装会端口冲突）。检测结果的「采集组件」一行写明是复用还是安装。";

/** 集群唯一标识在 external_labels 里的键值对（key 是通用配置的集群指标标签 key），例如 origin_prometheus: prod-k8s */
export function clusterLabelPair(
  tagKey: string | null | undefined,
  env: string
): string {
  return `${tagKey?.trim() || "origin_prometheus"}: ${env.trim() || "<集群唯一标识>"}`;
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
  // 以前的安装任务没有 vmagent 这一项：和服务端一样按安装方式推，仅安装 kubedoor-agent 什么采集组件都不装
  if (source && !source.vmagent) {
    result.vmagent = result.mode === "full" ? "install" : "reuse";
    if (result.mode === "agent") result.ksm = result.node_exporter = "reuse";
  }
  // Agent 端组件固定装在 kubedoor，任务里带回的历史命名空间也不能改
  result.namespace = "kubedoor";
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
    options.vmagent,
    options.ksm,
    options.node_exporter,
    extra ?? null
  ]);
}

/** 安装要用、通用配置里还没有的项（中文名称与通用配置页一致）；required 为 true 的缺了不能安装 vmagent */
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
  if (options.namespace !== "kubedoor")
    return "Agent 只能安装到 kubedoor 命名空间，不能修改";
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

/** 打开「安装 Agent」时恢复的任务：没结束的普通安装，优先记住的那个。修复任务不恢复（在任务列表里仍可选） */
export function recoverableInstall(
  tasks: AgentInstallTask[],
  preferredId?: string | null
): AgentInstallTask | undefined {
  const unfinished = tasks.filter(
    task =>
      !task.repair &&
      [
        "draft",
        "ready",
        "running",
        "interrupted",
        "failed",
        "partial"
      ].includes(task.status)
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

/**
 * 「AI Kubeconfig」那一份怎么称呼，和 Agent 列表那一列一样：网页安装时自动配置的叫「自动配置」，
 * 手动上传的显示 context 名；不知道来源时用 fallback（任务里的 context）
 */
export function connectionLabel(
  connection?: { origin?: string; context?: string } | null,
  fallback = ""
): string {
  if (connection?.origin === "agent-install") return "自动配置";
  return connection?.context || fallback || "已保存";
}

/** 修复时「确认信息」顶部的说明：用哪份 kubeconfig 连接集群、覆盖安装哪个集群的 Agent */
export function repairNotice(
  repair: Pick<AgentInstallRepair, "env"> & {
    source?: AgentInstallRepair["source"];
  },
  connection: string
): string {
  const via =
    repair.source === "ai-connection"
      ? `用保存的 AI Kubeconfig（${connection}）连接集群`
      : "用上传的 kubeconfig 连接集群";
  return `${via}，覆盖安装集群 ${repair.env} 的 Agent；集群唯一标识沿用现有的，命名空间固定为 kubedoor，不能修改。`;
}

/**
 * 强制覆盖确认框的内容：kind 为 Agent 的一项是修复时目标集群里没有找到这个集群的 Agent（reason 是说明），
 * 其余是要接管的已有资源
 */
export function takeoverParts(items: InstallTakeover[] | undefined): {
  missing: InstallTakeover[];
  resources: InstallTakeover[];
} {
  const list = items || [];
  return {
    missing: list.filter(item => item.kind === "Agent"),
    resources: list.filter(item => item.kind !== "Agent")
  };
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
