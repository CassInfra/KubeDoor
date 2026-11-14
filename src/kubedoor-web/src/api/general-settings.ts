import { http } from "@/utils/http";

/** 时序库类型 */
export type PromType =
  | "Victoria-Metrics-Single"
  | "Victoria-Metrics-Cluster"
  | "Prometheus";

/** Cookie Secure：auto 按访问协议判断 */
export type CookieSecure = "auto" | "true" | "false";

/**
 * 通用配置的全部配置项（和 master general_settings 的 key 一致）。
 * 三个地址（prom_url、metrics_write_url、oss_url）里的密码显示为 ******。
 */
export interface GeneralSettingsValues {
  prom_type: PromType;
  prom_url: string;
  metrics_write_url: string;
  prom_k8s_tag_key: string;
  kubedoor_exturl: string;
  alertmanager_exturl: string;
  oss_url: string;
  image_repo: string;
  image_vmagent: string;
  image_kube_state_metrics: string;
  image_node_exporter: string;
  node_shell_image: string;
  retention_events_days: number;
  retention_alerts_days: number;
  retention_resources_days: number;
  retention_audit_days: number;
  retention_ai_checkpoint_days: number;
  auth_session_hours: number;
  auth_idle_hours: number;
  auth_remember_days: number;
  auth_login_max_failures: number;
  auth_lock_minutes: number;
  auth_cookie_secure: CookieSecure;
  agent_auth_required: boolean;
  alert_dedup_window: number;
  silence_cache_ttl: number;
  admis_cache_ttl: number;
}

export type GeneralSettingKey = keyof GeneralSettingsValues;

/** GET / PUT /api/admin/general-settings 的返回 */
export interface GeneralSettings {
  /** 全部配置项的生效值（库里的或默认值） */
  values: GeneralSettingsValues;
  /** 默认值 */
  defaults: Partial<GeneralSettingsValues>;
  /** 库里显式保存过的项；不在这里的项用默认值 */
  stored: GeneralSettingKey[];
  /** 有问题的项（例如已保存的地址无法解密，此时 values 里该项为空串） */
  problems: Partial<Record<GeneralSettingKey, string>>;
  /** 推荐的 Agent 版本，跟随控制端 */
  recommended_agent_tag: string;
  updated_by: string | null;
  updated_at: string | null;
}

/**
 * PUT 的 values：只带要改的项。null（或空串）表示删掉这一项、恢复默认值；
 * 地址里的密码保留 ****** 表示沿用已保存的密码。
 */
export type GeneralSettingsInput = {
  [K in GeneralSettingKey]?: GeneralSettingsValues[K] | null;
};

/** 测试时序库连接：两项都可以不传，不传用当前生效值；地址里的 ****** 用已保存的密码 */
export interface MetricsTestRequest {
  prom_url?: string;
  prom_k8s_tag_key?: string;
}

export interface MetricsTestResult {
  ok: boolean;
  /** success：正常；warning：连接正常但最近一天查不到带这个标签的数据；error：连不上或查询出错 */
  level?: "success" | "warning" | "error";
  message: string;
}

type Result<T> = { success: boolean; data: T; message?: string };

export const getGeneralSettings = () =>
  http.request<Result<GeneralSettings>>("get", "/api/admin/general-settings");

/** 保存：只带这次要改的项；返回内容同 GET。校验不通过返回 422，错误由 http 封装统一提示 */
export const saveGeneralSettings = (values: GeneralSettingsInput) =>
  http.request<Result<GeneralSettings>>("put", "/api/admin/general-settings", {
    data: { values }
  });

/** 测试时序库查询地址和集群标签；同一个账号 3 秒内只能测一次，超了返回 429 */
export const testMetricsSettings = (data: MetricsTestRequest) =>
  http.request<Result<MetricsTestResult>>(
    "post",
    "/api/admin/general-settings/test-metrics",
    { data },
    // 错误由页面自己显示（429 要换成「操作太频繁」）；master 依次查两次，每次最多等 12 秒
    { silent: true, timeout: 40000 }
  );
