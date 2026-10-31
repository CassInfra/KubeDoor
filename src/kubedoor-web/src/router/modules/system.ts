// 系统管理:用户与集群授权、LDAP、企微 AI 机器人、获取代码接口、AI 用量看板、操作审计,只对管理员开放(后端 /api/admin/* 同样只认管理员)
export default {
  path: "/system",
  redirect: "/system/users",
  meta: {
    icon: "ep:user",
    title: "系统管理",
    rank: 65,
    roles: ["admin"]
  },
  children: [
    {
      path: "/system/users",
      name: "SystemUsers",
      component: () => import("@/views/system/users/index.vue"),
      meta: {
        title: "用户与权限",
        roles: ["admin"]
      }
    },
    {
      path: "/system/permissions",
      name: "SystemPermissions",
      component: () => import("@/views/system/permissions/index.vue"),
      meta: {
        title: "权限与审批",
        roles: ["admin"]
      }
    },
    {
      path: "/system/ldap",
      name: "SystemLdap",
      component: () => import("@/views/system/ldap/index.vue"),
      meta: {
        title: "LDAP 设置",
        roles: ["admin"]
      }
    },
    {
      path: "/system/wecom-ai",
      name: "SystemWeComAI",
      component: () => import("@/views/system/wecom-ai/index.vue"),
      meta: {
        title: "企微 AI 机器人",
        roles: ["admin"]
      }
    },
    {
      // Deployment 页「获取代码」调用的接口
      path: "/system/code-info",
      name: "SystemCodeInfo",
      component: () => import("@/views/system/code-info/index.vue"),
      meta: {
        title: "获取代码接口",
        roles: ["admin"]
      }
    },
    {
      path: "/system/ai-usage",
      name: "SystemAIUsage",
      component: () => import("@/views/system/ai-usage/index.vue"),
      meta: {
        title: "AI 用量看板📊",
        // Grafana 看板(KubeDoor-AI),管理员本来就能看到全部集群
        roles: ["admin"]
      }
    },
    {
      path: "/system/audit",
      name: "SystemAudit",
      component: () => import("@/views/system/audit/index.vue"),
      meta: {
        title: "操作审计",
        roles: ["admin"]
      }
    }
  ]
} satisfies RouteConfigsTable;
