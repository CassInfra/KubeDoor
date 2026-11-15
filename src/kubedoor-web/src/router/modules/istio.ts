import { $t } from "@/plugins/i18n";

// ISTIO管理:全局VirtualService管理,只有一个可见子菜单,侧边栏显示成一级菜单(标题、图标取下面这一项和父级)。
// 委托VirtualService管理在「实验功能」里
export default {
  path: "/istio",
  redirect: "/istio/level1-vs",
  meta: {
    icon: "ep:connection",
    title: $t("menus.istioManagement"),
    rank: 40
  },
  children: [
    {
      path: "/istio/level1-vs",
      name: "Level1VirtualService",
      component: () => import("@/views/istio/level1-vs/index.vue"),
      meta: {
        title: $t("menus.istioManagement")
      }
    },
    {
      path: "/istio/level1-vs/detail/:id",
      name: "Level1VirtualServiceDetail",
      component: () => import("@/views/istio/level1-vs/detail.vue"),
      meta: {
        title: "VirtualService详情",
        showLink: false,
        activePath: "/istio/level1-vs"
      }
    }
  ]
} satisfies RouteConfigsTable;
