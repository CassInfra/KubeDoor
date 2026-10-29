import { computed } from "vue";
import { useUserStoreHook } from "@/store/modules/user";
import { canSilence, canSilenceAny, canUpdateImage } from "@/utils/permission";

type Matchers = Parameters<typeof canSilence>[0];

/** 当前登录用户的权限:页面上用来置灰按钮、给出提示(后端仍会校验) */
export function usePermission() {
  const user = useUserStoreHook();
  return {
    /** 系统管理员(能做系统管理) */
    isAdmin: computed(() => user.isAdmin),
    /** 对某个集群的有效权限 */
    permissionOf: (env?: string | null) => user.permissionOf(env),
    /** 能否在集群 env 上执行操作 op(「权限与审批」里的操作类型) */
    can: (op: string, env?: string | null) => user.can(op, env),
    /** 与具体集群无关的配置,按全局角色判断 */
    canGlobal: (op: string) => user.canGlobal(op),
    /** 网页上改镜像(只读账号可能按 UPDATE_IMAGE 白名单放行,不置灰) */
    canUpdateImage: (env?: string | null) => canUpdateImage(env, user),
    /** 能否新建 / 修改这条屏蔽规则(限定了集群的按集群,全局规则按全局角色) */
    canSilence: (matchers: Matchers) => canSilence(matchers, user),
    /** 有没有能建的屏蔽规则 */
    canSilenceAny: computed(() => canSilenceAny(user))
  };
}
