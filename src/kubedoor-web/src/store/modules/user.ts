import { defineStore } from "pinia";
import {
  type userType,
  store,
  router,
  resetRouter,
  routerArrays,
  storageLocal
} from "../utils";
import {
  type AuthUser,
  type ClusterPermission,
  fetchMeApi,
  loginApi,
  logoutApi
} from "@/api/auth";
import { useMultiTagsStoreHook } from "./multiTags";
import { clearCache } from "@/utils/cache";
import { type DataInfo, setToken, removeToken, userKey } from "@/utils/auth";
import { can, canGlobal, permissionOf } from "@/utils/permission";

const cached = () => storageLocal().getItem<DataInfo<number>>(userKey);

export const useUserStore = defineStore({
  id: "pure-user",
  state: (): userType => ({
    // 头像
    avatar: cached()?.avatar ?? "",
    // 用户名
    username: cached()?.username ?? "",
    // 显示名
    nickname: cached()?.nickname ?? "",
    // 全局角色(菜单按 meta.roles 过滤)
    roles: cached()?.roles ?? [],
    // 按钮级别权限(未使用)
    permissions: cached()?.permissions ?? [],
    // 是否勾选了登录页的「记住我」
    isRemembered: false,
    // 「记住我」的天数,以后端配置为准,这里只用于提示
    loginDay: 7,
    // 以下字段来自 /api/auth/me,每次打开页面都会重新拉取
    loaded: false,
    role: "",
    roleLabel: "",
    source: "local",
    isAdmin: false,
    builtin: false,
    defaultPermission: "none",
    clusters: {},
    clusterPermissions: {},
    mustChangePassword: false,
    canChangePassword: false,
    operations: {}
  }),
  getters: {
    /** 当前用户对某个集群的有效权限:系统管理员一律「管理员」,有单独授权按授权,否则按全局权限兜底 */
    permissionOf: state => {
      return (env?: string | null): ClusterPermission =>
        permissionOf(state, env);
    },
    /** 能否在集群 env 上执行操作 op(按 /api/auth/me 的 operations 判断,后端仍会校验) */
    can: state => {
      return (op: string, env?: string | null): boolean => can(op, env, state);
    },
    /** 与具体集群无关的配置(没限定集群的告警屏蔽、节点均衡全局配置),按全局角色判断 */
    canGlobal: state => {
      return (op: string): boolean => canGlobal(op, state);
    }
  },
  actions: {
    /** 存储头像 */
    SET_AVATAR(avatar: string) {
      this.avatar = avatar;
    },
    /** 存储用户名 */
    SET_USERNAME(username: string) {
      this.username = username;
    },
    /** 存储昵称 */
    SET_NICKNAME(nickname: string) {
      this.nickname = nickname;
    },
    /** 存储角色 */
    SET_ROLES(roles: Array<string>) {
      this.roles = roles;
    },
    /** 存储按钮级别权限 */
    SET_PERMS(permissions: Array<string>) {
      this.permissions = permissions;
    },
    /** 存储是否勾选了登录页的免登录 */
    SET_ISREMEMBERED(bool: boolean) {
      this.isRemembered = bool;
    },
    /** 设置登录页的免登录存储几天 */
    SET_LOGINDAY(value: number) {
      this.loginDay = Number(value);
    },
    /** 保存后端返回的当前用户 */
    setUser(user: AuthUser) {
      // 换了账号:丢掉上一个人的接口缓存(集群列表等按权限过滤过)
      if (this.username !== user.username) clearCache();
      this.username = user.username;
      this.nickname = user.display_name || user.username;
      // 菜单按 meta.roles 过滤:全局角色,外加「能看到全部集群」标记(Grafana 看板菜单用)
      this.roles = user.all_clusters
        ? [user.role, "all-clusters"]
        : [user.role];
      this.role = user.role;
      this.roleLabel = user.role_label;
      this.source = user.source;
      this.isAdmin = user.is_admin;
      this.builtin = user.builtin;
      this.defaultPermission = user.default_permission;
      this.clusters = user.clusters ?? {};
      this.clusterPermissions = user.cluster_permissions ?? {};
      this.mustChangePassword = user.must_change_password;
      this.canChangePassword = user.can_change_password;
      this.operations = user.operations ?? {};
      this.loaded = true;
      setToken({
        username: this.username,
        nickname: this.nickname,
        roles: this.roles,
        permissions: []
      });
    },
    /** 清空本地登录信息 */
    clearUser() {
      clearCache();
      this.username = "";
      this.nickname = "";
      this.roles = [];
      this.permissions = [];
      this.role = "";
      this.isAdmin = false;
      this.defaultPermission = "none";
      this.clusters = {};
      this.clusterPermissions = {};
      this.operations = {};
      this.mustChangePassword = false;
      this.loaded = false;
      removeToken();
    },
    /** 登入 */
    async loginByUsername(data: {
      username: string;
      password: string;
      remember?: boolean;
    }) {
      const res = await loginApi({
        username: data.username,
        password: data.password,
        remember: !!data.remember
      });
      if (res?.success) this.setUser(res.data);
      return res;
    },
    /** 拉取当前登录用户;未登录时抛出异常 */
    async fetchMe() {
      const res = await fetchMeApi();
      if (!res?.success) throw new Error("not logged in");
      this.setUser(res.data);
      return res.data;
    },
    /** 登出:注销后端会话并回到登录页 */
    async logOut(redirect?: string) {
      try {
        await logoutApi();
      } catch {
        // 会话已失效也继续清理本地状态
      }
      this.resetToLogin(redirect);
    },
    /** 清理本地状态并跳转登录页(会话失效、被管理员下线等) */
    resetToLogin(redirect?: string) {
      this.clearUser();
      useMultiTagsStoreHook().handleTags("equal", [...routerArrays]);
      resetRouter();
      const query =
        redirect && !redirect.startsWith("/login") ? { redirect } : undefined;
      router.push({ path: "/login", query });
    }
  }
});

export function useUserStoreHook() {
  return useUserStore(store);
}
