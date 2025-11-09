// const { VITE_HIDE_HOME } = import.meta.env;
// const Layout = () => import("@/layout/index.vue");
import { $t } from "@/plugins/i18n";

export default {
  path: "/resource",
  redirect: "/resource/index",
  meta: {
    icon: "ep:histogram",
    title: $t("menus.peakResources"),
    rank: 10
  },
  children: [
    {
      path: "/resource/index",
      name: "resource",
      component: () => import("@/views/resource/index.vue"),
      meta: {
        title: $t("menus.resourceManagement"),
        icon: "ep:set-up"
      }
    },
    {
      path: "/collection/index",
      name: "collection",
      component: () => import("@/views/collection/index.vue"),
      meta: {
        title: $t("menus.resourceCollection"),
        icon: "ep:calendar"
      }
    },
    {
      path: "/statistics/index",
      name: "Statistics",
      component: () => import("@/views/statistics/index.vue"),
      meta: {
        title: $t("menus.resourceStatistics"),
        icon: "ep:pie-chart"
      }
    }
  ]
} satisfies RouteConfigsTable;
