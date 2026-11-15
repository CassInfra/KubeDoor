const { VITE_HIDE_HOME } = import.meta.env;
import { $t } from "@/plugins/i18n";

// 实验功能:还在完善中的功能。页面路径保持原样(书签、页面之间的跳转不受影响)
export default {
  path: "/experimental",
  redirect: "/istio/level2-vs",
  meta: {
    icon: "ep:magic-stick",
    title: "实验功能",
    rank: 70
  },
  children: [
    {
      path: "/istio/level2-vs",
      name: "Level2VirtualService",
      component: () => import("@/views/istio/level2-vs/index.vue"),
      meta: {
        title: $t("menus.level2VirtualService"),
        icon: "ep:place"
      }
    },
    {
      path: "/load-balance/index",
      name: "LoadBalance",
      component: () => import("@/views/monit/load-balance/index.vue"),
      meta: {
        title: "节点均衡",
        icon: "ep:scale-to-original",
        showLink: VITE_HIDE_HOME === "true" ? false : true
      }
    }
  ]
} satisfies RouteConfigsTable;
