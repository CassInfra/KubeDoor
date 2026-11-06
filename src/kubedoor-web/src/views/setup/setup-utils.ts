import type {
  AgentComponentChoice,
  MetricsSettings,
  PostgresSettings,
  SetupAgent,
  SetupAgentOptions,
  SetupAgentSummary,
  SetupMarker,
  SetupPhase,
  SetupState
} from "@/api/setup";

/** 向导总步数：第 6 步是可选的本集群 Agent 安装，收尾和完成结果也显示在这一步 */
export const SETUP_STEPS = 6;

const bootstrapPhases = new Set<SetupPhase>([
  "draft",
  "ready",
  "running",
  "failed",
  "interrupted",
  "agent",
  "finalizing"
]);

export function needsSetup(marker: SetupMarker | null): boolean {
  return !!marker && bootstrapPhases.has(marker.phase);
}

/** Agent 阶段只有 choice / failed（或缺少状态）在等用户选择，其余状态由后台推进 */
export function agentAwaitingUser(status?: string): boolean {
  return !status || status === "choice" || status === "failed";
}

/** 后台任务进行中（需要轮询、锁定表单）：控制端安装、收尾，或本集群 Agent 安装 */
export function isInstalling(
  phase?: SetupPhase,
  agentStatus?: string
): boolean {
  if (phase === "agent") return !agentAwaitingUser(agentStatus);
  return phase === "running" || phase === "finalizing";
}

/** 已开始安装：不能再回到前面的步骤修改配置 */
export function hasInstallation(phase?: SetupPhase): boolean {
  return (
    isInstalling(phase) ||
    ["failed", "interrupted", "agent", "succeeded"].includes(phase)
  );
}

/** 控制端安装中、失败停在第 5 步；Agent 阶段、收尾和完成结果在最后一步 */
export function stateStep(state: SetupState): number {
  if (["agent", "finalizing", "succeeded"].includes(state.phase))
    return SETUP_STEPS;
  if (hasInstallation(state.phase) || state.phase === "ready") return 5;
  return Math.min(5, Math.max(1, Number(state.step) || 1));
}

export const serviceStatus: Record<string, string> = {
  pending: "等待中",
  waiting: "等待依赖",
  deploying: "部署中",
  pulling: "拉取镜像",
  scheduling: "等待调度",
  starting: "启动中",
  checking: "检查中",
  running: "检查中",
  ready: "已就绪",
  success: "检查通过",
  succeeded: "已就绪",
  failed: "失败",
  error: "失败",
  timeout: "超时",
  skipped: "未选择",
  warning: "请留意"
};

export function checkTone(status: string): string {
  if (["ready", "success", "succeeded"].includes(status)) return "success";
  if (["failed", "error", "timeout"].includes(status)) return "error";
  if (["pending", "waiting", "skipped"].includes(status)) return "muted";
  if (status === "warning") return "warning";
  return "active";
}

/** 检查项前的圆点：通过打勾，警告和失败用感叹号 */
export function toneMark(tone: string): string {
  if (tone === "success") return "✓";
  return tone === "warning" || tone === "error" ? "!" : "·";
}

/** master Agent 安装任务的步骤状态 */
export const agentStepStatus: Record<string, string> = {
  pending: "等待中",
  running: "进行中",
  success: "完成",
  succeeded: "完成",
  error: "失败",
  failed: "失败",
  skipped: "已跳过",
  warning: "请留意"
};

const componentChoices: AgentComponentChoice[] = ["auto", "install", "reuse"];

/** 第 6 步表单；别名用文本填写，提交时再拆分 */
export interface AgentForm {
  /** 是否在本集群安装 Agent */
  install: boolean;
  env: string;
  /** 逗号或换行分隔 */
  aliases: string;
  namespace: string;
  mode: SetupAgentOptions["mode"];
  ksm: AgentComponentChoice;
  node_exporter: AgentComponentChoice;
}

function componentChoice(value?: string): AgentComponentChoice {
  return componentChoices.find(choice => choice === value) || "auto";
}

/** 预填 Agent 表单：优先用最近一次提交的选项（重试），否则用默认值；提交过才默认勾选安装 */
export function agentFormFrom(agent?: SetupAgent): AgentForm {
  const options = agent?.options;
  const source: Partial<SetupAgentOptions> = options || agent?.defaults || {};
  return {
    install: !!options,
    env: source.env || "",
    aliases: (options?.aliases || []).join(", "),
    namespace: source.namespace || "kubedoor",
    mode: source.mode === "agent" ? "agent" : "full",
    ksm: componentChoice(source.ksm),
    node_exporter: componentChoice(source.node_exporter)
  };
}

/** 别名按逗号（含中文逗号）或换行分隔，去掉空白和重复项 */
export function agentAliases(text: string): string[] {
  return Array.from(
    new Set(
      text
        .split(/[,，\r\n]+/)
        .map(alias => alias.trim())
        .filter(Boolean)
    )
  );
}

/** 校验第 6 步的安装选项，规则与安装服务一致 */
export function agentPayload(form: AgentForm): SetupAgentOptions {
  const env = form.env.trim();
  if (!env) throw new Error("请填写集群名称。");
  if (!/^[A-Za-z0-9_-]{1,100}$/.test(env))
    throw new Error(
      "集群名称只能含字母、数字、下划线和中划线，最长 100 个字符。"
    );
  const aliases = agentAliases(form.aliases);
  if (aliases.length > 20) throw new Error("集群别名最多 20 个。");
  // 按字符计数，和安装服务（Python len）一致
  if (aliases.some(alias => Array.from(alias).length > 100))
    throw new Error("每个集群别名最长 100 个字符。");
  const namespace = form.namespace.trim();
  if (!/^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$/.test(namespace))
    throw new Error(
      "命名空间只能含小写字母、数字和中划线，以字母或数字开头和结尾，最长 63 个字符。"
    );
  if (form.mode !== "full" && form.mode !== "agent")
    throw new Error("请选择是否安装指标采集。");
  if (!componentChoices.includes(form.ksm))
    throw new Error("请选择 kube-state-metrics 的处理方式。");
  if (!componentChoices.includes(form.node_exporter))
    throw new Error("请选择 node-exporter 的处理方式。");
  return {
    env,
    aliases,
    namespace,
    mode: form.mode,
    ksm: form.ksm,
    node_exporter: form.node_exporter
  };
}

/** 截止时间显示为本地时间：当天只显示时分，跨天带上日期 */
export function deadlineText(deadline?: string, now = new Date()): string {
  const time = new Date(deadline || "");
  if (!deadline || Number.isNaN(time.getTime())) return "";
  const pad = (value: number) => String(value).padStart(2, "0");
  const clock = `${pad(time.getHours())}:${pad(time.getMinutes())}`;
  return time.toDateString() === now.toDateString()
    ? clock
    : `${pad(time.getMonth() + 1)}-${pad(time.getDate())} ${clock}`;
}

export interface AgentOutcome {
  tone: "success" | "warning" | "muted";
  installed: boolean;
  title: string;
  note: string;
  label: string;
}

/** 完成页的本集群 Agent 结果；安装服务没有给出 Agent 信息时不显示 */
export function agentOutcome(
  agent?: SetupAgentSummary & { options?: Partial<SetupAgentOptions> }
): AgentOutcome | undefined {
  if (!agent?.status) return undefined;
  const env = agent.env || agent.options?.env || "";
  if (agent.status === "succeeded" || agent.status === "partial") {
    const partial = agent.status === "partial";
    return {
      tone: partial ? "warning" : "success",
      installed: true,
      title: env ? `已在本集群安装 Agent：${env}` : "已在本集群安装 Agent",
      note:
        agent.message ||
        (partial
          ? "Agent 已上线，但指标采集验证没有通过，可登录后在控制台检查。"
          : "Agent 已上线，登录后即可查看本集群的资源和监控。"),
      label: partial ? "指标待检查" : "已接入"
    };
  }
  return {
    tone: "muted",
    installed: false,
    title: "未在本集群安装 Agent",
    note:
      agent.status === "unavailable"
        ? "本集群未授予临时集群权限，可在控制台上传 kubeconfig 接入。"
        : agent.message ||
          "可登录后在控制台上传 kubeconfig，接入本集群和其他集群。",
    label: "未安装"
  };
}

export function urlInput(value: string, label: string, base = false): string {
  let url: URL;
  try {
    url = new URL(value.trim());
  } catch {
    throw new Error(`请填写有效的${label}，包含 http:// 或 https://。`);
  }
  if (
    !["http:", "https:"].includes(url.protocol) ||
    !url.hostname ||
    url.username ||
    url.password ||
    url.hash ||
    url.search
  )
    throw new Error(
      `${label}请填写 HTTP / HTTPS 地址，账号密码在独立字段填写。`
    );
  if (base && url.pathname !== "/")
    throw new Error(`${label}只需填写协议、主机和端口，不包含路径。`);
  return value.trim().replace(/\/+$/, "");
}

export function isLoopback(host: string): boolean {
  return /^(localhost|127(?:\.\d{1,3}){3}|0\.0\.0\.0|\[?::1\]?)$/i.test(host);
}

export function suggestedPublicURL(origin: string): string {
  try {
    const url = new URL(origin);
    return isLoopback(url.hostname) ? "" : url.origin;
  } catch {
    return "";
  }
}

/** 远程读写地址里已保存的密码显示成它，提交后由安装服务换成实际密码 */
export const PASSWORD_MASK = "******";

type EndpointSource = Pick<
  MetricsSettings,
  "type" | "scheme" | "host" | "tenant" | "user" | "password" | "has_password"
>;

/**
 * 按时序库类型生成远程读 / 写地址：单机版 8428，集群版读 vmselect 8481、写 vminsert 8480。
 * 有账号时带“账号:密码@”（URL 编码），密码已保存且没重新输入时用 ****** 占位。
 */
export function metricsEndpoints(form: EndpointSource): {
  read_url: string;
  write_url: string;
} {
  let host = form.host.trim();
  if (!host) return { read_url: "", write_url: "" };
  if (host.includes(":") && !host.startsWith("[")) host = `[${host}]`;
  const user = form.user.trim();
  const secret = form.password
    ? encodeURIComponent(form.password)
    : form.has_password
      ? PASSWORD_MASK
      : "";
  const auth = user ? `${encodeURIComponent(user)}:${secret}@` : "";
  const origin = `${form.scheme}://${auth}${host}`;
  if (form.type === "Victoria-Metrics-Cluster") {
    const tenant = form.tenant.trim() || "0";
    return {
      read_url: `${origin}:8481/select/${tenant}/prometheus`,
      write_url: `${origin}:8480/insert/${tenant}/prometheus`
    };
  }
  return {
    read_url: `${origin}:8428`,
    write_url: `${origin}:8428/api/v1/write`
  };
}

function isIPv6(host: string): boolean {
  try {
    new URL(`http://[${host}]`);
    return true;
  } catch {
    return false;
  }
}

/** 连接地址只要主机名或 IP：协议单独选，端口按类型自动带上 */
export function metricsHost(value: string): string {
  let host = value.trim();
  if (host.startsWith("[") && host.endsWith("]")) host = host.slice(1, -1);
  if (!host || /:\/\/|[\s/\\@?#[\]]/.test(host))
    throw new Error("连接地址只填主机名或 IP，不含 http://、路径或账号。");
  if (host.includes(":") && !isIPv6(host))
    throw new Error("连接地址不需要填写端口，端口会按时序库类型自动带上。");
  if (isLoopback(host))
    throw new Error("连接地址需使用集群可访问的地址，不能使用本机地址。");
  return host;
}

/** 远程读 / 写地址允许带“账号:密码@” */
export function endpointInput(value: string, label: string): string {
  const text = value.trim();
  let url: URL;
  try {
    url = new URL(text);
  } catch {
    throw new Error(`请填写有效的${label}，包含 http:// 或 https://。`);
  }
  if (
    !["http:", "https:"].includes(url.protocol) ||
    !url.hostname ||
    url.hash ||
    url.search ||
    /\s/.test(text)
  )
    throw new Error(`${label}需为 HTTP / HTTPS 地址，不含查询参数或锚点。`);
  if (isLoopback(url.hostname))
    throw new Error(`${label}需使用集群可访问的地址，不能使用本机地址。`);
  return text.replace(/\/+$/, "");
}

export function postgresPayload(form: PostgresSettings): PostgresSettings {
  const host = form.host.trim();
  if (!host || /[\s/@?#]/.test(host) || isLoopback(host))
    throw new Error(
      "请填写 Kubernetes Pod 可以访问的数据库主机名或 IP，不含协议。 "
    );
  const port = Number(form.port);
  if (!Number.isInteger(port) || port < 1 || port > 65535)
    throw new Error("数据库端口需在 1 到 65535 之间。");
  if (!form.database.trim() || !form.user.trim())
    throw new Error("请填写数据库名和用户名。");
  if (!form.password && !form.has_password)
    throw new Error("请填写数据库密码。");
  return {
    host,
    port,
    database: form.database.trim(),
    user: form.user.trim(),
    sslmode: form.sslmode,
    ...(form.password ? { password: form.password } : {})
  };
}

export function metricsPayload(form: MetricsSettings): MetricsSettings {
  const host = metricsHost(form.host);
  const cluster = form.type === "Victoria-Metrics-Cluster";
  const tenant = form.tenant.trim() || "0";
  if (cluster && !/^\d{1,10}(:\d{1,10})?$/.test(tenant))
    throw new Error("租户 ID 需为整数（默认 0），或“账号ID:项目ID”格式。");
  if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(form.tag_key.trim()))
    throw new Error("集群标签名只能含字母、数字、下划线，且不能以数字开头。");
  const user = form.user.trim();
  if (user && !form.password && !form.has_password)
    throw new Error("填写了时序库账号时请填写密码。");
  if (!user && form.password) throw new Error("填写了时序库密码时请填写账号。");
  return {
    type: form.type,
    scheme: form.scheme,
    host,
    tenant: cluster ? tenant : "0",
    user,
    read_url: endpointInput(form.read_url, "远程读地址"),
    write_url: endpointInput(form.write_url, "远程写地址"),
    tag_key: form.tag_key.trim(),
    ...(user && form.password ? { password: form.password } : {})
  };
}

export function publicURL(value: string): string {
  const url = urlInput(value, "KubeDoor 访问地址", true);
  if (isLoopback(new URL(url).hostname))
    throw new Error(
      "访问地址必须能从目标集群访问，不能填写 localhost 或回环地址。"
    );
  return url;
}
