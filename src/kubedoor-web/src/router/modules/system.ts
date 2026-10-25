// 系统管理:用户与集群授权、LDAP、企微 AI 机器人、AI 用量看板、操作审计,只对管理员开放(后端 /api/admin/* 同样只认管理员)
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
