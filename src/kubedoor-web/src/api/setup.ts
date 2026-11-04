/** 首次安装使用独立会话，不能经过控制台的登录拦截器。 */
export type SetupPhase =
  | "draft"
  | "ready"
  | "running"
  | "failed"
  | "interrupted"
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
  type: "Victoria-Metrics-Single" | "Victoria-Metrics-Cluster" | "Prometheus";
  url: string;
  user: string;
  has_password?: boolean;
  password?: string;
  write_url: string;
  tag_key: string;
}

export interface SetupComponents {
  grafana: boolean;
  vmalert: boolean;
  alertmanager: boolean;
}

export interface SetupConfig {
  postgres: PostgresSettings;
  metrics: MetricsSettings;
  admin: { username: string; has_password?: boolean; password?: string };
  public_url: string;
  components: SetupComponents;
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
    checks?: SetupCheck[];
  };
  services: SetupService[];
  checks: SetupCheck[];
  error?: string;
  retryable?: boolean;
  target_locked?: boolean;
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
    options: Pick<SetupConfig, "admin" | "public_url" | "components">
  ) =>
    request<SetupState>(
      "/configuration",
      "PUT",
      { revision, ...options },
      60000
    ),
  start: (revision: number) =>
    request<SetupState>("/start", "POST", { revision }),
  retry: (revision: number) =>
    request<SetupState>("/retry", "POST", { revision })
};
