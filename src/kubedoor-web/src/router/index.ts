import { getConfig } from "@/config";
import NProgress from "@/utils/progress";
import { transformI18n } from "@/plugins/i18n";
import { buildHierarchyTree } from "@/utils/tree";
import remainingRouter from "./modules/remaining";
import { useMultiTagsStoreHook } from "@/store/modules/multiTags";
import { usePermissionStoreHook } from "@/store/modules/permission";
import { useUserStoreHook } from "@/store/modules/user";
import { isUrl, isAllEmpty } from "@pureadmin/utils";
import {
  ascending,
  getTopMenu,
  initRouter,
  getHistoryMode,
  findRouteByPath,
  handleAliveRoute,
  formatTwoStageRoutes,
  formatFlatteningRoutes
} from "./utils";
import {
  type Router,
  createRouter,
  type RouteRecordRaw,
  type RouteComponent
} from "vue-router";

/** 自动导入全部静态路由，无需再手动引入！匹配 src/router/modules 目录（任何嵌套级别）中具有 .ts 扩展名的所有文件，除了 remaining.ts 文件
 * 如何匹配所有文件请看：https://github.com/mrmlnc/fast-glob#basic-syntax
 * 如何排除文件请看：https://cn.vitejs.dev/guide/features.html#negative-patterns
 */
const modules: Record<string, any> = import.meta.glob(
  [
    "./modules/**/**.ts",
    "!./modules/**/resource.ts",
    "!./modules/**/statistics.ts",
    "!./modules/**/collection.ts",
    "!./modules/**/monitnode.ts",
    "!./modules/**/monitk8s.ts",
    "!./modules/**/error.ts",
    "!./modules/**/remaining.ts"
  ],
  {
    eager: true
  }
);

/** 原始静态路由（未做任何处理） */
const routes = [];

Object.keys(modules).forEach(key => {
  routes.push(modules[key].default);
});

/** 导出处理后的静态路由（三级及以上的路由全部拍成二级） */
export const constantRoutes: Array<RouteRecordRaw> = formatTwoStageRoutes(
  formatFlatteningRoutes(buildHierarchyTree(ascending(routes.flat(Infinity))))
);

/** 用于渲染菜单，保持原始层级 */
export const constantMenus: Array<RouteComponent> = ascending(
  routes.flat(Infinity)
).concat(...remainingRouter);

/** 不参与菜单的路由 */
export const remainingPaths = Object.keys(remainingRouter).map(v => {
  return remainingRouter[v].path;
});

/** 创建路由实例 */
export const router: Router = createRouter({
  history: getHistoryMode(import.meta.env.VITE_ROUTER_HISTORY),
  routes: constantRoutes.concat(...(remainingRouter as any)),
  strict: true,
  scrollBehavior(to, from, savedPosition) {
    return new Promise(resolve => {
      if (savedPosition) {
        return savedPosition;
      } else {
        if (from.meta.saveSrollTop) {
          const top: number =
            document.documentElement.scrollTop || document.body.scrollTop;
          resolve({ left: 0, top });
        }
      }
    });
  }
});

/** 重置路由 */
export function resetRouter() {
  router.getRoutes().forEach(route => {
    const { name, meta } = route;
    if (name && router.hasRoute(name) && meta?.backstage) {
      router.removeRoute(name);
      router.options.routes = formatTwoStageRoutes(
        formatFlatteningRoutes(
          buildHierarchyTree(ascending(routes.flat(Infinity)))
        )
      );
    }
  });
  usePermissionStoreHook().clearAllCachePage();
}

/** 路由白名单(不需要登录) */
const whiteList = ["/login"];

/** 当前用户能否访问该路由:meta.roles 里写了角色的(如系统管理、Grafana 看板)只对这些角色开放 */
function routeAllowed(to: ToRouteType) {
  const owned = useUserStoreHook().roles ?? [];
  return to.matched.every(item => {
    const roles = (item.meta?.roles as string[]) ?? [];
    return roles.length === 0 || roles.some(role => owned.includes(role));
  });
}

router.beforeEach(async (to: ToRouteType, _from, next) => {
  if (to.meta?.keepAlive) {
    handleAliveRoute(to, "add");
    // 页面整体刷新和点击标签页刷新
    if (_from.name === undefined || _from.name === "Redirect") {
      handleAliveRoute(to);
    }
  }
  NProgress.start();
  const externalLink = isUrl(to?.name as string);
  if (!externalLink) {
    to.matched.some(item => {
      if (!item.meta.title) return "";
      const Title = getConfig().Title;
      if (Title)
        document.title = `${transformI18n(item.meta.title)} | ${Title}`;
      else document.title = transformI18n(item.meta.title);
    });
  }

  // 登录页不需要登录
  if (whiteList.includes(to.path)) {
    next();
    return;
  }

  // 登录态在后端(HttpOnly Cookie),每次打开页面先确认一次当前用户
  const userStore = useUserStoreHook();
  if (!userStore.loaded) {
    try {
      await userStore.fetchMe();
    } catch {
      userStore.clearUser();
      next({ path: "/login", query: { redirect: to.fullPath } });
      return;
    }
  }

  if (!routeAllowed(to)) {
    // 没有权限的页面(如系统管理)回到首页,保留查询参数(AI 会话链接等)
    next({ path: "/monit/overview", query: to.query });
    return;
  }

  if (
    usePermissionStoreHook().wholeMenus.length === 0 &&
    to.path !== "/login"
  ) {
    initRouter().then((router: Router) => {
      if (!useMultiTagsStoreHook().getMultiTagsCache) {
        const { path } = to;
        const route = findRouteByPath(path, router.options.routes[0].children);
        getTopMenu(true);
        // query、params模式路由传参数的标签页不在此处处理
        if (route && route.meta?.title) {
          if (isAllEmpty(route.parentId) && route.meta?.backstage) {
            // 此处为动态顶级路由（目录）
            const { path, name, meta } = route.children[0];
            useMultiTagsStoreHook().handleTags("push", {
              path,
              name,
              meta
            });
          } else {
            const { path, name, meta } = route;
            useMultiTagsStoreHook().handleTags("push", {
              path,
              name,
              meta
            });
          }
        }
      }
      // 确保动态路由完全加入路由列表并且不影响静态路由（注意：动态路由刷新时router.beforeEach可能会触发两次，第一次触发动态路由还未完全添加，第二次动态路由才完全添加到路由列表，如果需要在router.beforeEach做一些判断可以在to.name存在的条件下去判断，这样就只会触发一次）
      if (isAllEmpty(to.name)) router.push(to.fullPath);
    });
  }
  next();
});

router.afterEach(() => {
  NProgress.done();
});

export default router;
