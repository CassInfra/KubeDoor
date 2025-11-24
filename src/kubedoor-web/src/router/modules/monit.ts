const { VITE_HIDE_HOME } = import.meta.env;
const Layout = () => import("@/layout/index.vue");
import { $t } from "@/plugins/i18n";

export default {
  path: "/",
  redirect: "/monit/overview",
  component: Layout,
  meta: {
    icon: "ep:monitor",
    title: $t("menus.resourceMonit"),
    rank: 1
  },
  children: [
    {
      path: "/monit/overview",
      name: "K8sOverview",
      component: () => import("@/views/monit/overview/index.vue"),
      meta: {
        title: $t("menus.k8sOverview"),
        icon: "ep:odometer",
        showLink: VITE_HIDE_HOME === "true" ? false : true
      }
    },
    {
      path: "/monit/pod",
      name: "PodManager",
      component: () => import("@/views/monit/pod/index.vue"),
      meta: {
        title: "Pod",
        icon: "ep:box",
        showLink: VITE_HIDE_HOME === "true" ? false : true
      }
    },
    {
      path: "/monit/index",
      name: "Monit",
      component: () => import("@/views/monit/index.vue"),
      meta: {
        title: $t("menus.RealtimeResource"),
        icon: "ep:data-analysis",
        showLink: VITE_HIDE_HOME === "true" ? false : true
      }
    },
    {
      path: "/monit/service",
      name: "ServiceManager",
      component: () => import("@/views/monit/service/index.vue"),
      meta: {
        title: "Service",
        icon: "ep:connection",
        showLink: VITE_HIDE_HOME === "true" ? false : true
      }
    },
    {
      path: "/monit/ingress",
      name: "IngressManager",
      component: () => import("@/views/monit/ingress/index.vue"),
      meta: {
        title: "Ingress",
        icon: "ep:guide",
        showLink: VITE_HIDE_HOME === "true" ? false : true
      }
    },
    {
      path: "/monit/configmap",
      name: "ConfigMapManager",
      component: () => import("@/views/monit/configmap/index.vue"),
      meta: {
        title: "ConfigMap",
        icon: "ep:document",
        showLink: VITE_HIDE_HOME === "true" ? false : true
      }
    },

    {
      path: "/monit/statefulset",
      name: "StatefulSetManager",
      component: () => import("@/views/monit/stateful/index.vue"),
      meta: {
        title: "StatefulSet",
        icon: "ep:grid",
        showLink: VITE_HIDE_HOME === "true" ? false : true
      }
    },

    {
      path: "/monit/daemonset",
      name: "DaemonSetManager",
      component: () => import("@/views/monit/daemon/index.vue"),
      meta: {
        title: "DaemonSet",
        icon: "ep:collection",
        showLink: VITE_HIDE_HOME === "true" ? false : true
      }
    },
    {
      path: "/monit/nodemanager",
      name: "NodeManager",
      component: () => import("@/views/monit/nodemanager/index.vue"),
      meta: {
        title: "节点管理",
        icon: "ep:cpu",
        showLink: VITE_HIDE_HOME === "true" ? false : true
      }
    }
  ]
} satisfies RouteConfigsTable;
