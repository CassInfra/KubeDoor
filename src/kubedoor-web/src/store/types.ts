import type { RouteRecordName } from "vue-router";
import type { Operations } from "@/utils/permission";

export type cacheType = {
  mode: string;
  name?: RouteRecordName;
};

export type positionType = {
  startIndex?: number;
  length?: number;
};

export type appType = {
  sidebar: {
    opened: boolean;
    withoutAnimation: boolean;
    // 判断是否手动点击Collapse
    isClickCollapse: boolean;
  };
  layout: string;
  device: string;
  viewportSize: { width: number; height: number };
};

export type multiType = {
  path: string;
  name: string;
  meta: any;
  query?: object;
  params?: object;
};

export type setType = {
  title: string;
  fixedHeader: boolean;
  hiddenSideBar: boolean;
};

export type userType = {
  avatar?: string;
  username?: string;
  nickname?: string;
  roles?: Array<string>;
  permissions?: Array<string>;
  isRemembered?: boolean;
  loginDay?: number;
  /** 是否已从后端拉取过当前用户 */
  loaded?: boolean;
  /** 全局角色:admin / cluster_admin / rw / read / none */
  role?: string;
  roleLabel?: string;
  /** 账号来源:local / ldap */
  source?: string;
  isAdmin?: boolean;
  builtin?: boolean;
  /** 没有单独授权的集群的权限 */
  defaultPermission?: string;
  /** 单独授权的集群 */
  clusters?: Record<string, string>;
  /** 已知集群的有效权限 */
  clusterPermissions?: Record<string, string>;
  /** 各操作类型允许的集群权限级别(「权限与审批」页的配置) */
  operations?: Operations;
  mustChangePassword?: boolean;
  canChangePassword?: boolean;
};
