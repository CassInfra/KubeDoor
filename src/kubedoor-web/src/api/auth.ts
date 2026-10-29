import { http } from "@/utils/http";

/** 集群权限:管理员 / 读写 / 只读 / 无(看不到) */
export type ClusterPermission = "admin" | "rw" | "read" | "none";
/** 全局角色:系统管理员 / 集群管理员 / 读写 / 只读 / 无 / Agent 账号 */
export type UserRole =
  | "admin"
  | "cluster_admin"
  | "rw"
  | "read"
  | "none"
  | "agent";

export interface AuthUser {
  username: string;
  display_name: string;
  role: UserRole;
  role_label: string;
  source: "local" | "ldap";
  /** 没有单独授权的集群按这个兜底 */
  default_permission: ClusterPermission;
  /** 单独授权的集群 */
  clusters: Record<string, ClusterPermission>;
  /** 当前已知集群的有效权限 */
  cluster_permissions: Record<string, ClusterPermission>;
  /** 能看到全部集群(没有任何「无」权限的集群);Grafana 看板只对这类账号开放 */
  all_clusters: boolean;
  is_admin: boolean;
  builtin: boolean;
  must_change_password: boolean;
  can_change_password: boolean;
  /** 用户在各操作类型下有哪些权限(read / rw / admin),前端按这个判断能否执行 */
  operations?: Record<string, Array<"read" | "rw" | "admin">>;
}

type AuthResult = { success: boolean; data: AuthUser; message?: string };

/** 认证接口自己处理 401,不触发全局的「跳转登录页」 */
const quiet = { silent: true, skipAuthRedirect: true };

export const loginApi = (data: {
  username: string;
  password: string;
  remember: boolean;
}) => http.request<AuthResult>("post", "/api/auth/login", { data }, quiet);

export const fetchMeApi = () =>
  http.request<AuthResult>("get", "/api/auth/me", {}, quiet);

export const logoutApi = () =>
  http.request<{ success: boolean }>("post", "/api/auth/logout", {}, quiet);

export const changePasswordApi = (data: {
  old_password: string;
  new_password: string;
}) =>
  http.request<{ success: boolean; message?: string }>(
    "post",
    "/api/auth/password",
    { data },
    { skipAuthRedirect: true }
  );
