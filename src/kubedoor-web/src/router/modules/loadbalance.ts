const { VITE_HIDE_HOME } = import.meta.env;
const Layout = () => import("@/layout/index.vue");

export default {
  path: "/load-balance",
  redirect: "/load-balance/index",
  component: Layout,
  meta: {
    icon: "ep:scale-to-original",
    title: "节点均衡",
    rank: 50
  },
  children: [
    {
      path: "/load-balance/index",
      name: "LoadBalance",
      component: () => import("@/views/monit/load-balance/index.vue"),
      meta: {
        title: "节点均衡",
        showLink: VITE_HIDE_HOME === "true" ? false : true
      }
    }
  ]
} satisfies RouteConfigsTable;
