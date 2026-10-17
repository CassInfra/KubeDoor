import { http } from "@/utils/http";
import type { ClusterPermission, UserRole } from "./auth";

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
