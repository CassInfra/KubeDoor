/** 首次安装使用独立会话，不能经过控制台的登录拦截器。 */
export type SetupPhase =
  | "draft"
  | "ready"
  | "running"
  | "failed"
  | "interrupted"
  /** 控制端已全部就绪，等待选择是否在本集群安装 Agent */
  | "agent"
  | "finalizing"
  | "succeeded";

export interface SetupCheck {
  id?: string;
  name: string;
  status: string;
  message?: string;
  details?: string | string[];
  updated_at?: string;
}

export interface SetupService extends SetupCheck {
  id: string;
  /** 对应的 Deployment 名，如 kubedoor-alarm */
  resource?: string;
}

export interface PostgresSettings {
  host: string;
  port: number;
  database: string;
  user: string;
  sslmode: string;
  has_password?: boolean;
  password?: string;
}

export interface MetricsSettings {
  type: "Victoria-Metrics-Single" | "Victoria-Metrics-Cluster";
  /** 连接协议，连接地址只填主机名或 IP */
  scheme: "http" | "https";
  host: string;
  /** 集群版租户 ID，默认 0 */
  tenant: string;
  user: string;
  has_password?: boolean;
  password?: string;
  /** 远程读 / 写地址，可带“账号:密码@”；已保存的密码显示为 ****** */
  read_url: string;
  write_url: string;
  tag_key: string;
}

/** Grafana 看板必装；告警组件 kubedoor-alarm、vmalert、Alertmanager 一起装或一起不装 */
export interface SetupComponents {
  alerting: boolean;
}

/** 告警通知的 IM 类型，和 master notify/channels.py 的 TYPES 一致 */
export type NotifyType = "wecom" | "dingding" | "feishu" | "slack";

/** 第 4 步保存的告警通知：页面只拿到是否已配置，不返回机器人地址 */
export interface NotificationSettings {
  type: NotifyType;
  /** 默认 @ 的人：企微 userid、钉钉手机号、飞书 open_id、Slack 用户 ID */
  at: string;
  configured: boolean;
}

/**
 * 提交的告警通知：token 可以是整条 Webhook 地址或其中的 token，留空沿用已保存的；
 * clear 为 true 时清除已保存的配置（不发通知）
 */
export interface NotificationInput {
  type: NotifyType;
  token: string;
  at: string;
  clear?: boolean;
}

/** 发送测试消息的结果：IM 返回失败时 ok 为 false，message 是 IM 的错误码和说明 */
export interface NotificationTestResult {
  ok: boolean;
  message: string;
}

export interface SetupConfig {
  postgres: PostgresSettings;
  metrics: MetricsSettings;
  admin: { username: string; has_password?: boolean; password?: string };
  public_url: string;
  components: SetupComponents;
  /** 旧版安装服务没有这一项 */
  notification?: NotificationSettings;
}

/**
 * 本集群 Agent 安装状态：choice / failed 等待选择（到 deadline 自动按不安装收尾），
 * running 安装中，succeeded / partial 后自动收尾；unavailable 表示没有临时集群权限，不进入该阶段。
 */
export type SetupAgentStatus =
  | "choice"
  | "running"
  | "failed"
  | "succeeded"
  | "partial"
  | "skipped"
  | "unavailable";

/** kube-state-metrics / node-exporter：自动检测、安装或复用已有 */
export type AgentComponentChoice = "auto" | "install" | "reuse";

export interface SetupAgentOptions {
  /** 集群名称，同时是指标集群标签（external_labels）的值 */
  env: string;
  aliases: string[];
  namespace: string;
  /** full：安装 vmagent 采集指标；agent：只安装 Agent */
  mode: "full" | "agent";
  ksm: AgentComponentChoice;
  node_exporter: AgentComponentChoice;
}

export interface SetupAgentStep {
  name: string;
  /** pending / running / success / error / skipped */
  status: string;
  message?: string;
}

/** 公开完成标记只带 Agent 的结果 */
export interface SetupAgentSummary {
  status: SetupAgentStatus;
  env?: string;
  message?: string;
}

export interface SetupAgent extends SetupAgentSummary {
  /** ISO 时间，choice / failed 时到点自动按不安装收尾 */
  deadline?: string;
  /** 表单预填值 */
  defaults?: Partial<Omit<SetupAgentOptions, "aliases">>;
  /** 最近一次提交的选项，重试时优先用它预填 */
  options?: SetupAgentOptions;
  /** master 安装任务的步骤 */
  steps?: SetupAgentStep[];
  /** 安装前检查（预检失败项也可能放在 steps 里） */
  checks?: SetupCheck[];
  error?: string;
  task_id?: string;
}

export interface SetupMarker {
  phase: SetupPhase;
  installation_id?: string;
  id?: string;
  authenticated?: boolean;
  services?: SetupService[];
  checks?: SetupCheck[];
  public_url?: string;
  error?: string;
  agent?: SetupAgentSummary;
}

export interface SetupState extends SetupMarker {
  id: string;
  revision: number;
  step: number;
  config: SetupConfig;
  environment: {
    namespace: string;
    server?: string;
    version?: string;
    /** Web 的 NodePort 地址（安装服务所在节点 IP + NodePort），取不到时为空 */
    web_url?: string;
    checks?: SetupCheck[];
  };
  services: SetupService[];
  checks: SetupCheck[];
  error?: string;
  retryable?: boolean;
  target_locked?: boolean;
  agent?: SetupAgent;
}

interface Envelope<T> {
  success: boolean;
  data: T;
  message?: string;
  error?: string | { message?: string };
}

export class SetupRequestError extends Error {
  constructor(
    message: string,
    public status: number
  ) {
    super(message);
    this.name = "SetupRequestError";
  }
}

async function fetchJSON(path: string, init: RequestInit, timeout: number) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  try {
    const response = await fetch(path, {
      ...init,
      credentials: "same-origin",
      cache: "no-store",
      redirect: "error",
      headers: { Accept: "application/json", ...init.headers },
      signal: controller.signal
    });
    let body: unknown;
    try {
      body = await response.json();
    } catch {
      throw new SetupRequestError(
        "安装服务暂未返回有效结果，请稍后刷新状态。",
        response.status
      );
    }
    return { response, body };
  } catch (error) {
    if (error instanceof SetupRequestError) throw error;
    throw new SetupRequestError(
      error instanceof Error && error.name === "AbortError"
        ? "等待安装服务超时，请刷新状态后再试。后台安装不会因此取消。"
        : "暂时无法连接安装服务，请检查网络后刷新状态。",
      0
    );
  } finally {
    clearTimeout(timer);
  }
}

async function request<T>(
  path: string,
  method = "GET",
  data?: unknown,
  timeout = 15000
): Promise<T> {
  const { response, body } = await fetchJSON(
    `/setup-api${path}`,
    {
      method,
      ...(data === undefined
        ? {}
        : {
            body: JSON.stringify(data),
            headers: { "Content-Type": "application/json" }
          })
    },
    timeout
  );
  const result = body as Envelope<T>;
  if (!response.ok || result?.success !== true) {
    const detail =
      typeof result?.error === "string" ? result.error : result?.error?.message;
    throw new SetupRequestError(
      result?.message || detail || "安装请求未完成，请刷新状态后重试。",
      response.status
    );
  }
  return result.data;
}

/** 没有独立安装标记的旧部署保持正常登录流程，不能因后端故障进入初始化。 */
export async function getSetupMarker(): Promise<SetupMarker | null> {
  try {
    const { response, body } = await fetchJSON("/setup-status.json", {}, 4000);
    if (!response.ok || !body || typeof body !== "object") return null;
    const value = body as SetupMarker;
    return typeof value.phase === "string" ? value : null;
  } catch {
    return null;
  }
}

export const setupApi = {
  status: () => request<SetupMarker>("/status"),
  session: (key: string) => request<SetupMarker>("/session", "POST", { key }),
  state: () => request<SetupState>("/state"),
  postgres: (revision: number, postgres: PostgresSettings) =>
    request<SetupState>(
      "/check/postgres",
      "POST",
      { revision, postgres },
      60000
    ),
  metrics: (revision: number, metrics: MetricsSettings) =>
    request<SetupState>("/check/metrics", "POST", { revision, metrics }, 60000),
  configure: (
    revision: number,
    options: Pick<SetupConfig, "admin" | "public_url" | "components"> & {
      notification?: NotificationInput;
    }
  ) =>
    request<SetupState>(
      "/configuration",
      "PUT",
      { revision, ...options },
      60000
    ),
  /** 第 4 步「发送测试消息」：安装服务直接发给 IM，不保存配置；同一会话 3 秒内只能测一次（429） */
  testNotification: (notification: NotificationInput) =>
    request<NotificationTestResult>(
      "/notification/test",
      "POST",
      { notification },
      30000
    ),
  start: (revision: number) =>
    request<SetupState>("/start", "POST", { revision }),
  retry: (revision: number) =>
    request<SetupState>("/retry", "POST", { revision }),
  /** 用临时集群权限在本集群安装 Agent，受理后返回 202（agent.status 为 running） */
  installAgent: (revision: number, options: SetupAgentOptions) =>
    request<SetupState>("/agent/install", "POST", { revision, options }, 60000),
  /** 不在本集群安装 Agent，直接进入收尾；安装服务会等收尾任务启动（最多十几秒）再返回 */
  skipAgent: (revision: number) =>
    request<SetupState>("/agent/skip", "POST", { revision }, 60000)
};
