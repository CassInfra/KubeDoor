// 系统管理:用户与集群授权、LDAP、操作审计,只对管理员开放(后端 /api/admin/* 同样只认管理员)
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
