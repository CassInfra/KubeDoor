const { VITE_HIDE_HOME } = import.meta.env;
// const Layout = () => import("@/layout/index.vue");
import { $t } from "@/plugins/i18n";

export default {
  path: "/workbench",
  redirect: "/workbench/index",
  meta: {
    icon: "ep:setting",
    title: $t("menus.resourceWorkbench"),
    rank: 60,
    // Agent 管理只对管理员开放
    roles: ["admin"]
  },
  children: [
    {
      path: "/workbench/index",
      name: "workbench",
      component: () => import("@/views/workbench/index.vue"),
      meta: {
        title: $t("menus.resourceWorkbench"),
        showLink: VITE_HIDE_HOME === "true" ? false : true,
        roles: ["admin"]
      }
    }
  ]
} satisfies RouteConfigsTable;
