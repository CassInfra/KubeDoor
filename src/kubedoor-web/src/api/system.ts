import { http } from "@/utils/http";
import type { ClusterPermission, UserRole } from "./auth";
import type { OpLevels, PolicyValue } from "@/utils/permission";

export interface SystemUser {
  id: number;
  username: string;
  source: "local" | "ldap";
  role: UserRole;
  display_name: string;
  email: string;
  enabled: boolean;
  builtin: boolean;
  super_admin: boolean;
  must_change_password: boolean;
  password_default: boolean;
  last_login_at: string | null;
  last_login_ip: string;
  comment: string;
  created_at: string;
  session_count: number;
  clusters: Record<string, ClusterPermission>;
}

export interface ClusterGrant {
  env: string;
  permission: ClusterPermission;
}

export interface KnownCluster {
  env: string;
  online: boolean;
}

export interface LdapConfig {
  enabled: boolean;
  url: string;
  start_tls: boolean;
  tls_verify: boolean;
  ca_cert: string;
  bind_dn: string;
  /** 只写:不填表示保持原值,空字符串表示清除 */
  bind_password?: string | null;
  has_bind_password?: boolean;
  base_dn: string;
  user_filter: string;
  user_dn_template: string;
  attr_username: string;
  attr_display_name: string;
  attr_email: string;
  auto_create: boolean;
  default_role: "read" | "none";
  timeout: number;
}

export interface LdapTestStep {
  name: string;
  ok: boolean;
  detail: string;
}

export interface LdapTestResult {
  success: boolean;
  steps: LdapTestStep[];
  user?: { dn: string; username: string; display_name: string; email: string };
}

export interface AuditRecord {
  id: number;
  ts: string;
  username: string;
  action: string;
  env: string;
  target: string;
  detail: Record<string, any> | null;
  ip: string;
  success: boolean;
}

/** 权限表的一行:一种操作类型的定义(顺序即页面上的行顺序) */
export interface PermissionOperation {
  key: string;
  group: string;
  name: string;
  desc: string;
  /** 适用范围:网页 / AI / 网页+AI */
  applies: string;
  defaults: OpLevels;
  /** 审批默认值 */
  approval: boolean;
  /** 审批固定不可改:approval 为 true 时是高危固定审批,为 false 时等同 approval_na */
  approval_fixed: boolean;
  /** 审批列显示「—」:不是 AI 操作(或者是查询),不能勾选 */
  approval_na: boolean;
  /** 页面上标红(权限很宽的操作) */
  highlight: boolean;
}

export interface PermissionPolicyData {
  definitions: PermissionOperation[];
  policy: PolicyValue;
  defaults: PolicyValue;
  /** 查看命令的内置参数检查说明(命令 → 说明) */
  builtin_checks?: Record<string, string>;
  updated_by: string | null;
  updated_at: string | null;
}

type Result<T> = { success: boolean; data: T; message?: string };

export const getUsers = () =>
  http.request<Result<SystemUser[]>>("get", "/api/admin/users");

export const createUser = (data: Partial<SystemUser> & { password?: string }) =>
  http.request<Result<SystemUser>>("post", "/api/admin/users", { data });

export const updateUser = (id: number, data: Partial<SystemUser>) =>
  http.request<Result<null>>("put", `/api/admin/users/${id}`, { data });

export const deleteUser = (id: number) =>
  http.request<Result<null>>("delete", `/api/admin/users/${id}`);

export const resetUserPassword = (id: number, password: string) =>
  http.request<Result<null>>("put", `/api/admin/users/${id}/password`, {
    data: { password }
  });

export const logoutUser = (id: number) =>
  http.request<Result<null>>("post", `/api/admin/users/${id}/logout`);

export const getUserClusters = (id: number) =>
  http.request<
    Result<{
      user: SystemUser;
      clusters: ClusterGrant[];
      known: KnownCluster[];
    }>
  >("get", `/api/admin/users/${id}/clusters`);

export const setUserClusters = (id: number, clusters: ClusterGrant[]) =>
  http.request<Result<null>>("put", `/api/admin/users/${id}/clusters`, {
    data: { clusters }
  });

export const getKnownClusters = () =>
  http.request<Result<KnownCluster[]>>("get", "/api/admin/clusters");

export const getLdapConfig = () =>
  http.request<Result<LdapConfig>>("get", "/api/admin/ldap");

export const saveLdapConfig = (data: LdapConfig) =>
  http.request<Result<LdapConfig>>("put", "/api/admin/ldap", { data });

export const testLdapConfig = (data: {
  config: LdapConfig;
  username?: string;
  password?: string;
}) =>
  http.request<Result<LdapTestResult>>(
    "post",
    "/api/admin/ldap/test",
    { data },
    { timeout: 60000 }
  );

export const getAuditLogs = (params: Record<string, any>) =>
  http.request<Result<AuditRecord[]> & { total: number }>(
    "get",
    "/api/admin/audit",
    { params }
  );

export const getAuditActions = () =>
  http.request<Result<string[]>>("get", "/api/admin/audit/actions");

/** 权限与审批:定义、当前配置、出厂默认值 */
export const getPermissionPolicy = () =>
  http.request<Result<PermissionPolicyData>>(
    "get",
    "/api/admin/permission-policy"
  );

/** 保存权限与审批,成功时返回和 GET 一样的数据 */
export const savePermissionPolicy = (data: PolicyValue) =>
  http.request<Result<PermissionPolicyData>>(
    "put",
    "/api/admin/permission-policy",
    { data }
  );

/** 获取代码接口的请求头:值只写不读 */
export interface CodeInfoHeader {
  name: string;
  /** 只写:留空表示保持已保存的值 */
  value?: string | null;
  has_value?: boolean;
}

/** 获取代码接口的配置(Deployment 页「获取代码」由 master 调这个接口) */
export interface CodeInfoConfig {
  enabled: boolean;
  url: string;
  headers: CodeInfoHeader[];
  timeout: number;
  tls_verify: boolean;
  /** 允许 AI 助手下载源码分析(源码会发给模型服务商),默认关闭 */
  ai_download: boolean;
  updated_by?: string | null;
  updated_at?: string | null;
}

/** master POST 给接口的请求体,固定这 4 个字段 */
export interface CodeInfoRequest {
  k8s: string;
  namespace: string;
  deployment: string;
  image: string;
}

/** 接口返回的 3 个链接;没返回或者不是 http(s) 地址的是空字符串 */
export interface CodeInfoLinks {
  /** 代码页面:GitLab 上这个仓库或这个 commit 的代码页面 */
  web_url: string;
  /** 下载链接:这个 commit 的代码压缩包 */
  download_url: string;
  /** Commit 说明:这个仓库最近一些 commit 的提交说明 */
  commits_url: string;
}

/** 格式不对(不是 http(s) 地址)的字段和原值 */
export type CodeInfoInvalid = Partial<Record<keyof CodeInfoLinks, string>>;

export interface CodeInfoTestResult {
  ok: boolean;
  status: number | null;
  elapsed_ms: number;
  /** 原始响应(最多 4KB) */
  body: string;
  links: CodeInfoLinks;
  invalid: CodeInfoInvalid;
  error: string;
  /** 发出的请求体:测试时只有填了的字段 */
  request: Partial<CodeInfoRequest>;
}

export const getCodeInfoConfig = () =>
  http.request<Result<CodeInfoConfig>>("get", "/api/admin/code-info");

/** 保存配置;没传的字段沿用原值(右上角开关只传 enabled) */
export const saveCodeInfoConfig = (data: Partial<CodeInfoConfig>) =>
  http.request<Result<CodeInfoConfig>>("put", "/api/admin/code-info", {
    data
  });

/** 用页面上的配置(不用先保存)和手填的入参调一次接口;入参都可以不填,没填的不放进请求体 */
export const testCodeInfoConfig = (data: {
  config: Partial<CodeInfoConfig>;
  request: Partial<CodeInfoRequest>;
}) =>
  http.request<Result<CodeInfoTestResult>>(
    "post",
    "/api/admin/code-info/test",
    { data },
    // 接口超时最长 60 秒
    { timeout: 70000 }
  );
