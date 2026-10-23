import { http } from "@/utils/http";

/** kube-system/coredns 这个 ConfigMap(agent 返回的原始对象,去掉了 managedFields) */
export interface CoreDnsConfigMap {
  apiVersion?: string;
  kind?: string;
  metadata?: {
    name?: string;
    namespace?: string;
    resourceVersion?: string;
    labels?: Record<string, string>;
    annotations?: Record<string, string>;
    [key: string]: unknown;
  };
  data?: Record<string, string>;
  binaryData?: Record<string, string>;
  [key: string]: unknown;
}

/** JSON merge patch:值为 null 表示删除这个键 */
export interface CoreDnsPatch {
  data?: Record<string, string | null>;
  binaryData?: Record<string, string | null>;
  metadata?: {
    labels?: Record<string, string | null>;
    annotations?: Record<string, string | null>;
  };
}

export interface CoreDnsResult<T> {
  success?: boolean;
  data?: T;
  error?: string;
  message?: string;
  /** 保存时 resourceVersion 对不上(加载之后被别人改过) */
  conflict?: boolean;
}

/** 旧版 agent 没有这个接口:本地服务回 404 纯文本,agent 转发时解析 JSON 失败 */
export const isOldAgent = (res?: { success?: boolean; error?: string }) =>
  !!res && !res.success && /unexpected mimetype/i.test(String(res.error || ""));

export const oldAgentTip = (feature: string) =>
  `当前集群的 kubedoor-agent 版本过旧,还不支持${feature},请先升级 agent`;

export const OLD_AGENT_TIP = oldAgentTip("CoreDNS 管理");

export const getCoreDns = (env: string) =>
  http.request<CoreDnsResult<CoreDnsConfigMap>>("get", "/api/agent/coredns", {
    params: { env }
  });

/** summary 只用于审计日志和 agent 日志,说明这次改了什么 */
export const saveCoreDns = (
  env: string,
  resourceVersion: string,
  patch: CoreDnsPatch,
  summary: string
) =>
  http.request<CoreDnsResult<CoreDnsConfigMap>>(
    "post",
    "/api/agent/coredns/save",
    {
      params: { env, summary: summary.slice(0, 180) },
      data: { resource_version: resourceVersion, patch }
    }
  );
