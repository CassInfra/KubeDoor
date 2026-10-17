import { useUserStoreHook } from "@/store/modules/user";
import { storageLocal, isString, isIncludeAllChildren } from "@pureadmin/utils";

/**
 * 登录状态由后端的 HttpOnly 会话 Cookie 维持(前端读不到,也不需要读),
 * 这里只在 localStorage 里缓存一份用户信息,供菜单过滤和页面展示使用。
 * 是否真的已登录以 /api/auth/me 的结果为准(见 router 的全局守卫)。
 */
export interface DataInfo<T> {
  /** 兼容 pure-admin 模板的旧字段,已不再使用 */
  accessToken?: string;
  expires?: T;
  refreshToken?: string;
  /** 头像 */
  avatar?: string;
  /** 用户名 */
  username?: string;
  /** 显示名 */
  nickname?: string;
  /** 当前登录用户的全局角色,菜单按 meta.roles 过滤 */
  roles?: Array<string>;
  /** 按钮级别权限(未使用) */
  permissions?: Array<string>;
}

export const userKey = "user-info";

/** 本地缓存的用户信息 */
export function getToken(): DataInfo<number> {
  return storageLocal().getItem<DataInfo<number>>(userKey);
}

/** 缓存用户信息(登录成功或拉取当前用户后调用) */
export function setToken(data: DataInfo<any>) {
  storageLocal().setItem(userKey, {
    avatar: data?.avatar ?? "",
    username: data?.username ?? "",
    nickname: data?.nickname ?? "",
    roles: data?.roles ?? [],
    permissions: data?.permissions ?? []
  });
}

/** 清除本地缓存的用户信息(会话本身由后端注销) */
export function removeToken() {
  storageLocal().removeItem(userKey);
}

/** 是否有按钮级别的权限（根据登录接口返回的`permissions`字段进行判断）*/
export const hasPerms = (value: string | Array<string>): boolean => {
  if (!value) return false;
  const allPerms = "*:*:*";
  const { permissions } = useUserStoreHook();
  if (!permissions) return false;
  if (permissions.length === 1 && permissions[0] === allPerms) return true;
  const isAuths = isString(value)
    ? permissions.includes(value)
    : isIncludeAllChildren(value, permissions);
  return isAuths ? true : false;
};
