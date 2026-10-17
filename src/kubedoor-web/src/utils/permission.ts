import { computed } from "vue";
import { useUserStoreHook } from "@/store/modules/user";
import type { ClusterPermission, UserRole } from "@/api/auth";

/** 全局角色说明(与后端 auth/principal.py 一致) */
export const ROLE_OPTIONS: Array<{
  value: UserRole;
  label: string;
  desc: string;
  tag: "danger" | "warning" | "info" | "primary" | "success";
}> = [
  {
    value: "admin",
    label: "管理员",
    desc: "所有集群读写,并可管理用户、LDAP 和 Agent;不能再单独指定集群权限",
    tag: "danger"
  },
  {
    value: "rw",
    label: "读写",
    desc: "所有集群读写(包括 AI 助手执行修改),不能管理用户和 Agent",
    tag: "warning"
  },
  {
    value: "read",
    label: "只读",
    desc: "所有集群只读,不能修改 K8S 和数据库(AI 助手也只能查询)",
    tag: "info"
  },
  {
    value: "none",
    label: "无",
    desc: "默认看不到任何集群,只能访问单独授权的集群;新接入的集群也不会自动可见",
    tag: "info"
  },
  {
    value: "agent",
    label: "Agent 账号",
    desc: "只能用于 kubedoor-agent 连接 master,不能登录 Web",
    tag: "primary"
  }
];

export const ROLE_LABELS: Record<string, string> = Object.fromEntries(
  ROLE_OPTIONS.map(item => [item.value, item.label])
);

export const PERM_OPTIONS: Array<{ value: ClusterPermission; label: string }> =
  [
    { value: "rw", label: "读写" },
    { value: "read", label: "只读" },
    { value: "none", label: "无(不可见)" }
  ];

export const PERM_LABELS: Record<string, string> = {
  rw: "读写",
  read: "只读",
  none: "无"
};

export const PERM_TAG: Record<string, "success" | "info" | "danger"> = {
  rw: "success",
  read: "info",
  none: "danger"
};

/** 角色对应的「没有单独授权时」的集群权限 */
export function roleDefaultPermission(role: string): ClusterPermission {
  if (role === "admin" || role === "rw") return "rw";
  if (role === "read") return "read";
  return "none";
}

/** 当前登录用户对集群的权限,页面上用来提示只读、隐藏写操作入口(后端仍会校验) */
export function useClusterPermission() {
  const user = useUserStoreHook();
  return {
    isAdmin: computed(() => user.isAdmin),
    permissionOf: (env?: string) => user.permissionOf(env),
    canWrite: (env?: string) => !!env && user.permissionOf(env) === "rw",
    canRead: (env?: string) =>
      !!env && ["rw", "read"].includes(user.permissionOf(env))
  };
}
