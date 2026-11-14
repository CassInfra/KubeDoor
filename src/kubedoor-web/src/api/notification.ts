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
