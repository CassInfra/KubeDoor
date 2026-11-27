import { http } from "@/utils/http";

/** IM 类型:和 master notify/channels.py 的 TYPES 一致 */
export type NotifyType = "wecom" | "dingding" | "feishu" | "slack";

/** 已保存的一个通道:token 不回显,只给是否已配置和最后 4 位 */
export interface NotifyChannel {
  type: NotifyType;
  /** 默认 @ 人:企微 userid、钉钉手机号、飞书 open_id、Slack 用户 ID;空串表示不 @ 人 */
  at: string;
  configured: boolean;
  /** token 的最后 4 位,例如 ****a1b2 */
  token_hint: string;
}

export interface NotifyClusterChannel extends NotifyChannel {
  env: string;
}

/** GET / PUT /api/admin/notification 的返回 */
export interface NotificationSettings {
  /** 全局配置;没有配置时为 null */
  default: NotifyChannel | null;
  /** 按集群单独配置的集群 */
  clusters: NotifyClusterChannel[];
  /** 已注册的集群名(按名称排序),给「新增一行」的下拉用 */
  envs: string[];
  updated_by?: string | null;
  updated_at?: string | null;
}

/** 保存时的一个通道:token 留空表示沿用已保存的(没保存过或者换了 IM 类型时必须填写) */
export interface NotifyChannelInput {
  type: NotifyType;
  token: string;
  at: string;
}

export interface NotifyClusterInput extends NotifyChannelInput {
  env: string;
}

/** PUT /api/admin/notification 的请求体 */
export interface NotificationInput {
  /** null 表示删除全局配置 */
  default: NotifyChannelInput | null;
  /** 完整列表,整体替换 */
  clusters: NotifyClusterInput[];
}

/** 发送测试消息:env 为空测全局配置;token 为空用已保存的(type / at 以请求里的为准) */
export interface NotifyTestRequest extends NotifyChannelInput {
  env: string;
}

export interface NotifyTestResult {
  ok: boolean;
  /** 成功时是「发送成功」,失败时是 IM 返回的错误 */
  message: string;
}

/* ==================== v2 Schema-driven Multi-IM Types ==================== */

export type FieldKind =
  | "text"
  | "secret"
  | "select"
  | "number"
  | "boolean"
  | "textarea"
  | "headers";

export interface ProviderFieldOption {
  value: string;
  label: string;
}

export interface ProviderFieldSpec {
  key: string;
  label: string;
  kind: FieldKind;
  required?: boolean;
  secret?: boolean;
  default?: string | number | boolean | Record<string, string>;
  placeholder?: string;
  help?: string;
  options?: ProviderFieldOption[];
  min?: number;
  max?: number;
  visible_when?: Record<string, string | boolean | Array<string | boolean>>;
}

export interface ProviderRateLimit {
  max_requests?: number;
  limit?: number;
  window_seconds?: number;
  window?: number;
  burst?: number;
  burst_limit?: number;
  label?: string;
}

export interface ProviderSchema {
  id: string;
  type?: string;
  label: string;
  description: string;
  default_rate_limit: ProviderRateLimit | [number, number] | string;
  supports_at: boolean;
  supports_markdown: boolean;
  at_help?: string;
  fields: ProviderFieldSpec[];
}

export interface NotifyChannelV2 {
  id: string;
  name: string;
  provider: string;
  enabled: boolean;
  settings: Record<string, any>;
  secret_hints?: Record<string, string>;
  configured_secrets?: string[];
  configured?: boolean;
  updated_at?: string | null;
}

export interface NotifyChannelV2Input {
  id: string;
  name: string;
  provider: string;
  enabled: boolean;
  settings: Record<string, any>;
  secrets: Record<string, string>;
}

export interface NotifyRouteMatch {
  envs?: string[];
  categories?: string[];
  severities?: string[];
}

export interface NotifyRouteV2 {
  id: string;
  name: string;
  enabled: boolean;
  match: NotifyRouteMatch;
  channel_ids: string[];
  continue: boolean;
}

export interface NotificationConfigV2 {
  version: 2;
  channels: NotifyChannelV2[];
  routes: NotifyRouteV2[];
  default_channel_ids: string[];
  envs: string[];
  updated_by?: string | null;
  updated_at?: string | null;
}

export interface NotificationConfigV2Input {
  version: 2;
  channels: NotifyChannelV2Input[];
  routes: NotifyRouteV2[];
  default_channel_ids: string[];
}

export interface NotifyTestV2Request {
  channel_id?: string;
  provider: string;
  name?: string;
  settings: Record<string, any>;
  secrets: Record<string, string>;
  env?: string;
  category?: string;
  severity?: string;
}

export interface DeliveryRecord {
  id: string | number;
  trace_id?: string;
  env?: string | null;
  category?: string;
  severity?: string;
  channel_id?: string;
  channel_name?: string;
  provider: string;
  ok: boolean;
  attempts?: number;
  http_status?: number | null;
  error_summary?: string | null;
  message?: string | null;
  latency_ms?: number | null;
  created_at: string;
}

export interface DeliveryQueryParams {
  env?: string;
  provider?: string;
  ok?: boolean | string;
  limit?: number;
  offset?: number;
}

export interface DeliveryListResponse {
  items: DeliveryRecord[];
  total?: number;
}

type Result<T> = { success: boolean; data: T; message?: string };

/** silent:出错不弹提示,交给调用方处理(安装 Agent 对话框读不到就不显示已有的单独配置) */
export const getNotificationSettings = (silent = false) =>
  http.request<Result<NotificationSettings>>(
    "get",
    "/api/admin/notification",
    undefined,
    silent ? { silent: true } : undefined
  );

/** 保存全局配置和按集群的配置(整体替换);返回内容同 GET */
export const saveNotificationSettings = (data: NotificationInput) =>
  http.request<Result<NotificationSettings>>("put", "/api/admin/notification", {
    data
  });

/** 发一条测试消息;同一个账号 3 秒内只能测一次,超了返回 429 */
export const testNotification = (data: NotifyTestRequest) =>
  http.request<Result<NotifyTestResult>>(
    "post",
    "/api/admin/notification/test",
    { data },
    // 错误由页面自己提示(429 要换成「操作太频繁」);master 调 IM 接口最多等 10 秒
    { silent: true, timeout: 30000 }
  );

/** 获取 Schema 驱动的通知通道提供方元数据 */
export const getNotificationProviders = (silent = false) =>
  http.request<Result<ProviderSchema[] | { providers: ProviderSchema[] }>>(
    "get",
    "/api/admin/notification/providers",
    undefined,
    silent ? { silent: true } : undefined
  );

/** 获取 v2 统一多通道通知配置（通道池 + 路由规则 + 集群列表） */
export const getNotificationConfigV2 = (silent = false) =>
  http.request<Result<NotificationConfigV2>>(
    "get",
    "/api/admin/notification/v2",
    undefined,
    silent ? { silent: true } : undefined
  );

/** 保存 v2 统一多通道通知配置 */
export const saveNotificationConfigV2 = (data: NotificationConfigV2Input) =>
  http.request<Result<NotificationConfigV2>>(
    "put",
    "/api/admin/notification/v2",
    { data }
  );

/** 发送 v2 测试消息 */
export const testNotificationV2 = (data: NotifyTestV2Request) =>
  http.request<Result<NotifyTestResult>>(
    "post",
    "/api/admin/notification/test",
    { data },
    { silent: true, timeout: 30000 }
  );

/** 查询通知投递审计日志 */
export const getNotificationDeliveries = (params?: DeliveryQueryParams) =>
  http.request<Result<DeliveryRecord[] | DeliveryListResponse>>(
    "get",
    "/api/admin/notification/deliveries",
    { params },
    { silent: true }
  );
