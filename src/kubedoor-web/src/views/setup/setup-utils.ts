import type {
  AgentComponentChoice,
  MetricsSettings,
  NotificationInput,
  NotificationSettings,
  NotificationTestResult,
  NotifyType,
  PostgresSettings,
  SetupAgent,
  SetupAgentOptions,
  SetupAgentSummary,
  SetupCheck,
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

/**
 * 第 2、3 步的连接测试是否已通过：「检查通过」和「请留意」都算通过，可以点「继续」。
 * section 是检查项名称（postgres / metrics）。
 */
export function connectionPassed(
  checks: SetupCheck[] | undefined,
  section: string
): boolean {
  const check = (checks || []).find(item => (item.id || item.name) === section);
  return !!check && ["success", "warning"].includes(check.status);
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

/** 完成页显示管理员密码用：第 4 步保存成功后记在当前标签页的 sessionStorage，按安装 id 区分 */
export function adminPasswordKey(installationId: string): string {
  return `kubedoor-setup-admin:${installationId}`;
}

export function suggestedPublicURL(origin: string): string {
  try {
    const url = new URL(origin);
    return isLoopback(url.hostname) ? "" : url.origin;
  } catch {
    return "";
  }
}

/**
 * 自动获取的 KubeDoor 访问地址：优先用当前打开安装页面的地址；
 * 用本机回环（端口转发）打开时，改用安装服务给出的 Web NodePort 地址（节点 IP + NodePort）。
 */
export function autoPublicURL(origin: string, nodePortURL?: string): string {
  return suggestedPublicURL(origin) || suggestedPublicURL(nodePortURL || "");
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

/**
 * 第 4 步的对象存储上传地址（可选），规则同安装服务：完整的 HTTP / HTTPS 地址，可以带“账号:密码@”，
 * 不含查询参数和锚点，去掉末尾的 /。留空表示不配置；已保存的密码显示为 ******，原样提交表示沿用。
 */
export function ossURLInput(value: string): string {
  const text = (value || "").trim();
  if (!text) return "";
  let url: URL;
  try {
    url = new URL(text);
  } catch {
    throw new Error(
      "请填写有效的对象存储上传地址，包含 http:// 或 https://；不配置时留空。"
    );
  }
  if (
    !["http:", "https:"].includes(url.protocol) ||
    !url.hostname ||
    url.hash ||
    url.search ||
    /[\s\\#?]/.test(text)
  )
    throw new Error(
      "对象存储上传地址需为 HTTP / HTTPS 地址，不含空格、查询参数或锚点。"
    );
  if (url.password && !url.username)
    throw new Error(
      "对象存储上传地址的 @ 前面缺少账号；不需要认证时去掉“账号:密码@”。"
    );
  if (isLoopback(url.hostname))
    throw new Error(
      "对象存储上传地址需使用各业务集群都能访问的地址，不能填写本机地址。"
    );
  return text.replace(/\/+$/, "");
}

/** 第 5 步摘要：已保存的对象存储上传地址（密码已打码），没填写时「未配置」 */
export function ossSummary(saved?: string): string {
  return (saved || "").trim() || "未配置";
}

/* ---------- 第 4 步：告警通知（可选） ---------- */

/** IM 类型，顺序即下拉里的顺序；和 master notify/channels.py 的 TYPES 一致 */
export const NOTIFY_TYPES: NotifyType[] = [
  "wecom",
  "dingding",
  "feishu",
  "slack"
];

export const NOTIFY_LABELS: Record<NotifyType, string> = {
  wecom: "企业微信",
  dingding: "钉钉",
  feishu: "飞书",
  slack: "Slack"
};

export interface NotifyHint {
  /** 机器人地址输入框的 placeholder（没有已保存的地址时） */
  token: string;
  /** 机器人地址输入框下面的说明 */
  tokenHelp: string;
  /** 默认 @ 人输入框的 placeholder */
  at: string;
}

/** 按 IM 类型给的输入提示，和控制台「系统管理 → 消息通知」页一致 */
export const NOTIFY_HINTS: Record<NotifyType, NotifyHint> = {
  wecom: {
    token: "粘贴群机器人 Webhook 地址，或 key= 后面那一段",
    tokenHelp:
      "群机器人的 Webhook 地址，形如 https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx",
    at: "企业微信成员的 userid（账号，不是姓名），可以不填"
  },
  dingding: {
    token: "粘贴机器人 Webhook 地址，或 access_token= 后面那一段",
    tokenHelp:
      "自定义机器人的 Webhook 地址，形如 https://oapi.dingtalk.com/robot/send?access_token=xxx；安全设置不支持「加签」",
    at: "钉钉成员的手机号，可以不填"
  },
  feishu: {
    token: "粘贴自定义机器人 Webhook 地址，或 hook/ 后面那一段",
    tokenHelp:
      "自定义机器人的 Webhook 地址，形如 https://open.feishu.cn/open-apis/bot/v2/hook/xxx；安全设置不支持「签名校验」",
    at: "飞书用户的 open_id（ou_ 开头），可以不填"
  },
  slack: {
    token: "粘贴 Incoming Webhook 地址，或 services/ 后面那一段",
    tokenHelp:
      "Incoming Webhook 地址，形如 https://hooks.slack.com/services/T000/B000/xxx",
    at: "Slack 成员 ID（例如 U012AB3CD），可以不填"
  }
};

/** 某个 IM 类型的输入提示；不认识的类型按企业微信提示 */
export function notifyHint(type: string): NotifyHint {
  return NOTIFY_HINTS[type as NotifyType] || NOTIFY_HINTS.wecom;
}

export interface NoticeRow {
  /** 通知 */
  name: string;
  /** 谁发送 */
  sender: string;
  /** 什么时候发 */
  when: string;
}

/** 说明表（和控制台「系统管理 → 消息通知」页共用的文字，保持一致） */
export const NOTICE_ROWS: NoticeRow[] = [
  {
    name: "K8S 事件告警",
    sender: "控制服务 kubedoor-master",
    when: "K8S 事件命中事件告警规则（在系统管理 → 事件告警规则配置）时，例如重启、健康检查失败、挂载失败、调度失败、资源压力；不受告警屏蔽影响"
  },
  {
    name: "运维操作通知",
    sender: "各集群的 kubedoor-agent，经控制服务发送",
    when: "扩缩容、重启、更新镜像（开始、成功、超时）、准入控制改写副本数或资源、定时任务创建或删除失败、取消禁止调度失败、jstack / jmap 诊断结果"
  },
  {
    name: "指标告警",
    sender: "告警服务 kubedoor-alarm（安装了告警组件才有）",
    when: "vmalert 告警规则触发和恢复时；可以按规则屏蔽，消息里带【屏蔽】链接"
  },
  {
    name: "管控表通知",
    sender: "控制服务 kubedoor-master",
    when: "每天采集高峰期数据时，发现新服务并自动加入管控表"
  }
];

export const NOTICE_NOTE =
  "给某个集群单独配置后，这个集群的上述通知都发到它自己的群，其他集群用全局配置。";

/** 第 4 步的告警通知表单：token 是这次新填的（留空沿用已保存的），clear 表示清除已保存的配置 */
export interface NotificationForm {
  type: NotifyType;
  token: string;
  at: string;
  clear: boolean;
}

/** 按已保存的配置预填表单，机器人地址一律留空（安装服务不回传） */
export function notificationFormFrom(
  saved?: NotificationSettings
): NotificationForm {
  return {
    type: saved && NOTIFY_TYPES.includes(saved.type) ? saved.type : "wecom",
    token: "",
    at: saved?.at || "",
    clear: false
  };
}

/** 有已保存的地址、没点清除、IM 类型也没改：机器人地址留空就沿用已保存的 */
export function keepsSavedNotification(
  form: NotificationForm,
  saved?: NotificationSettings
): boolean {
  return !form.clear && !!saved?.configured && saved.type === form.type;
}

/** 机器人地址输入框的 placeholder：已保存时提示留空保留，否则按 IM 类型提示怎么填 */
export function notificationTokenPlaceholder(
  form: NotificationForm,
  saved?: NotificationSettings
): string {
  if (keepsSavedNotification(form, saved)) return "已保存，留空保留";
  if (form.clear) return "已清除，留空表示不发通知";
  if (saved?.configured)
    return `换了 IM 类型，请重新填写${NOTIFY_LABELS[form.type]}机器人地址`;
  return notifyHint(form.type).token;
}

/** 折叠栏标题上的状态：填了地址或沿用已保存的显示 IM 类型，否则「未配置」 */
export function notificationBadge(
  form: NotificationForm,
  saved?: NotificationSettings
): { on: boolean; text: string } {
  return (form.token || "").trim() || keepsSavedNotification(form, saved)
    ? { on: true, text: NOTIFY_LABELS[form.type] || form.type }
    : { on: false, text: "未配置" };
}

/** 第 5 步摘要：企业微信 / 未配置 */
export function notificationSummary(saved?: NotificationSettings): string {
  return saved?.configured ? NOTIFY_LABELS[saved.type] || saved.type : "未配置";
}

const WEBHOOK_HOSTS: Record<string, NotifyType> = {
  "qyapi.weixin.qq.com": "wecom",
  "oapi.dingtalk.com": "dingding",
  "open.feishu.cn": "feishu",
  "hooks.slack.com": "slack"
};

/** 粘贴的是整条 Webhook 地址（可以没写 https://）时按主机认出是哪家的；只填了 token 或认不出时返回 "" */
export function webhookType(value: string): NotifyType | "" {
  const match = /^(?:https?:\/\/)?([^/?#:@\s]+)[/?#:]/i.exec(value.trim());
  return (match && WEBHOOK_HOSTS[match[1].toLowerCase()]) || "";
}

const MAX_NOTIFY_TOKEN = 512;
const MAX_NOTIFY_AT = 128;

/** 含控制字符（0x00~0x1f、0x7f） */
function hasControl(value: string): boolean {
  return Array.from(value).some(char => {
    const code = char.charCodeAt(0);
    return code < 0x20 || code === 0x7f;
  });
}

/** 校验表单里填的内容（规则同安装服务），返回去掉首尾空白的 {type, token, at} */
function checkNotification(form: NotificationForm): NotificationInput {
  const type = form.type;
  if (!NOTIFY_TYPES.includes(type))
    throw new Error("请选择告警通知的 IM 类型。");
  const token = (form.token || "").trim();
  const at = (form.at || "").trim();
  if (token) {
    if (/\s/.test(token) || hasControl(token))
      throw new Error("告警通知的机器人地址不能包含空格、换行或控制字符。");
    if (token.length > MAX_NOTIFY_TOKEN)
      throw new Error(
        `告警通知的机器人地址太长（最多 ${MAX_NOTIFY_TOKEN} 个字符）。`
      );
    const detected = webhookType(token);
    if (detected && detected !== type)
      throw new Error(
        `告警通知的机器人地址是${NOTIFY_LABELS[detected]}的，IM 类型却选了${NOTIFY_LABELS[type]}。`
      );
  }
  if (hasControl(at)) throw new Error("默认 @ 人不能包含换行等控制字符。");
  if (Array.from(at).length > MAX_NOTIFY_AT)
    throw new Error(`默认 @ 人最多 ${MAX_NOTIFY_AT} 个字符。`);
  return { type, token, at };
}

/**
 * 第 4 步提交的告警通知：点了清除（没再填地址）时只带 clear；
 * 已保存的地址换了 IM 类型又没填新地址时提示重新填写。都没填表示不发通知。
 */
export function notificationPayload(
  form: NotificationForm,
  saved?: NotificationSettings
): NotificationInput {
  if (form.clear && !(form.token || "").trim())
    return { type: form.type, token: "", at: "", clear: true };
  const input = checkNotification(form);
  if (!input.token && saved?.configured && saved.type !== input.type)
    throw new Error(
      `告警通知的 IM 类型从${NOTIFY_LABELS[saved.type] || saved.type}改成了${NOTIFY_LABELS[input.type]}，请重新填写机器人地址；不发通知时点「清除」。`
    );
  return input;
}

/** 「发送测试消息」的请求：填了地址用填的，没填用已保存的（IM 类型要和保存时一致） */
export function notificationTestRequest(
  form: NotificationForm,
  saved?: NotificationSettings
): NotificationInput {
  const input = checkNotification(form);
  if (!input.token && !keepsSavedNotification(form, saved))
    throw new Error(
      saved?.configured && !form.clear
        ? "换了 IM 类型，请先填写新的机器人地址。"
        : `请先填写${NOTIFY_LABELS[input.type]}机器人地址。`
    );
  return input;
}

/** 测试结果的一行，样式同检查结果：通过绿色、失败红色、请稍后橙色 */
export interface NotifyTestView {
  tone: "success" | "warning" | "error";
  /** 右侧的结果文字 */
  label: string;
  message: string;
}

/** 测试接口正常返回时的显示：IM 接口失败时带上它返回的错误码和说明 */
export function notifyResultView(
  result: NotificationTestResult | undefined,
  type: NotifyType
): NotifyTestView {
  if (result?.ok)
    return {
      tone: "success",
      label: "检查通过",
      message: `测试消息已发送，请到${NOTIFY_LABELS[type] || ""}群里确认收到。`
    };
  const reason = (result?.message || "").trim();
  return {
    tone: "error",
    label: "失败",
    message: reason ? `发送失败：${reason}` : "发送失败，IM 接口没有返回原因。"
  };
}

/** 没能发送（填写有误、测试太频繁、安装服务连不上）时的显示；429 用「请稍后」 */
export function notifyErrorView(reason: unknown): NotifyTestView {
  const message =
    reason instanceof Error && reason.message
      ? reason.message
      : "发送测试消息失败，请稍后重试。";
  if ((reason as { status?: number } | undefined)?.status === 429)
    return { tone: "warning", label: "请稍后", message };
  return { tone: "error", label: "未发送", message };
}
