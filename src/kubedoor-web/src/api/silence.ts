import { http } from "@/utils/http";

/** 匹配条件操作符，语义与 Alertmanager Silence 一致 */
export type SilenceOp = "=" | "!=" | "=~" | "!~";

export interface SilenceMatcher {
  key: string;
  op: SilenceOp;
  value: string;
}

/** 规则状态，由后端按当前时间实时推导，不落库 */
export type SilenceStatus = "active" | "pending" | "expired" | "revoked";

export interface SilenceRule {
  id: number;
  matchers: SilenceMatcher[];
  /** ISO 8601 带时区 */
  starts_at: string;
  /** null 表示长期有效 */
  ends_at: string | null;
  comment: string;
  created_by: string;
  revoked_at: string | null;
  revoked_by: string;
  match_count: number;
  last_match_at: string | null;
  created_at: string;
  updated_at: string;
  status: SilenceStatus;
  /** 剩余秒数；null = 长期有效，0 = 已结束 */
  remaining_seconds: number | null;
}

export interface SilenceStats {
  total: number;
  active: number;
  pending: number;
  expired: number;
  revoked: number;
}

interface SilenceListResult {
  success: boolean;
  data: SilenceRule[];
  total: number;
  stats: SilenceStats;
  msg?: string;
}

interface SilenceOpResult {
  success: boolean;
  msg?: string;
  id?: number;
}

export interface SilencePreviewSample {
  alert_name: string;
  env: string;
  namespace: string;
  pod: string;
  severity: string;
  firings: number;
}

export interface SilencePreviewResult {
  success: boolean;
  days: number;
  /** 命中的告警记录条数 */
  matched: number;
  /** 命中记录的告警触发总次数 */
  firings: number;
  /** 无法在历史告警表中预览的自定义标签，此时预览结果偏大 */
  unsupported_keys: string[];
  samples: SilencePreviewSample[];
  msg?: string;
}

export interface SilenceListParams {
  status?: SilenceStatus | "all";
  keyword?: string;
  page?: number;
  pageSize?: number;
}

/** 获取屏蔽规则列表（响应内附带各状态计数） */
export const getSilenceList = (params: SilenceListParams) => {
  return http.request<SilenceListResult>("get", "/api/db/silence/list", {
    params
  });
};

/** 获取某个标签在近 30 天告警中出现过的候选值 */
export const getSilenceLabelValues = (key: string) => {
  return http.request<{ success: boolean; data: string[] }>(
    "get",
    "/api/db/silence/label_values",
    { params: { key } }
  );
};

/** 命中预览：用当前匹配条件回溯最近 N 天的历史告警 */
export const previewSilence = (matchers: SilenceMatcher[], days = 7) => {
  return http.request<SilencePreviewResult>("post", "/api/db/silence/preview", {
    data: { matchers, days }
  });
};

export interface SilenceSavePayload {
  id?: number;
  matchers: SilenceMatcher[];
  starts_at: string;
  /** 空字符串表示长期有效 */
  ends_at: string | null;
  comment: string;
  created_by: string;
}

export const addSilence = (data: SilenceSavePayload) => {
  return http.request<SilenceOpResult>("post", "/api/db/silence/add", { data });
};

export const editSilence = (data: SilenceSavePayload) => {
  return http.request<SilenceOpResult>("post", "/api/db/silence/edit", {
    data
  });
};

/** 解除屏蔽：规则立即失效，记录保留可追溯 */
export const revokeSilence = (id: number, revoked_by: string) => {
  return http.request<SilenceOpResult>("post", "/api/db/silence/revoke", {
    data: { id, revoked_by }
  });
};

/** 续期：延长结束时间，并让已解除/已过期的规则重新生效 */
export const extendSilence = (id: number, ends_at: string | null) => {
  return http.request<SilenceOpResult>("post", "/api/db/silence/extend", {
    data: { id, ends_at }
  });
};

export const deleteSilence = (id: number) => {
  return http.request<SilenceOpResult>("post", "/api/db/silence/delete", {
    data: { id }
  });
};

/** 匹配条件里可直接选用的常用标签（也支持自由输入任意 Prometheus 标签） */
export const COMMON_MATCHER_KEYS = [
  { label: "K8S集群", value: "env" },
  { label: "命名空间", value: "namespace" },
  { label: "Pod", value: "pod" },
  { label: "容器", value: "container" },
  { label: "告警名称", value: "alertname" },
  { label: "告警分组", value: "alertgroup" },
  { label: "严重级别", value: "severity" },
  { label: "告警描述", value: "description" }
];

export const MATCHER_OPS: Array<{
  label: string;
  value: SilenceOp;
  tip: string;
}> = [
  { label: "=", value: "=", tip: "等于" },
  { label: "≠", value: "!=", tip: "不等于" },
  { label: "=~", value: "=~", tip: "正则匹配（需完全匹配整个值）" },
  { label: "!~", value: "!~", tip: "正则不匹配" }
];
