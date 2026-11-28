<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { emitter } from "@/utils/mitt";
import { useNav } from "@/layout/hooks/useNav";
import { useUserStoreHook } from "@/store/modules/user";
import { AIRequestError, aiApi } from "@/api/ai";
import LogoutCircleRLine from "@iconify-icons/ri/logout-circle-r-line";
import LockPasswordLine from "@iconify-icons/ri/lock-password-line";
import Chat3Line from "@iconify-icons/ri/chat-3-line";

defineOptions({
  name: "LayUserMenu"
});

const user = useUserStoreHook();
const { userAvatar, logout } = useNav();

const ROLE_TAG = {
  admin: "danger",
  rw: "warning",
  read: "info",
  none: "info"
} as const;

const tagType = computed(() => ROLE_TAG[user.role] ?? "info");
const displayName = computed(() => user.nickname || user.username);

/** 启用了 IM 机器人（或已经绑过，方便解除）才显示「绑定 IM 账号」；AI 服务没部署时请求失败，不显示 */
const imBindable = ref(false);
const imBoundCount = ref(0);
const imMenuLabel = computed(() =>
  imBoundCount.value > 0
    ? `绑定 IM 账号（${imBoundCount.value}）`
    : "绑定 IM 账号"
);

const isNotFound = (err: unknown) =>
  (err instanceof AIRequestError && err.status === 404) ||
  (typeof err === "object" &&
    err !== null &&
    "status" in err &&
    (err as { status?: number }).status === 404);

onMounted(() => {
  aiApi
    .imBinding()
    .then(value => {
      imBoundCount.value = value.members.length;
      imBindable.value = value.enabled || !!value.members.length;
    })
    .catch(err => {
      if (!isNotFound(err)) return;
      return aiApi
        .wecomBinding()
        .then(value => {
          imBoundCount.value = value.members.length;
          imBindable.value = value.enabled || !!value.members.length;
        })
        .catch(() => undefined);
    });
});
</script>

<template>
  <el-dropdown trigger="click">
    <span class="kd-user-link navbar-bg-hover select-none">
      <img :src="userAvatar" />
      <p v-if="displayName" class="dark:text-white">{{ displayName }}</p>
      <el-tag
        v-if="user.roleLabel"
        size="small"
        :type="tagType"
        effect="plain"
        class="ml-1"
      >
        {{ user.roleLabel }}
      </el-tag>
    </span>
    <template #dropdown>
      <el-dropdown-menu class="kd-user-menu">
        <el-dropdown-item disabled>
          {{ user.username }}({{
            user.source === "ldap" ? "LDAP" : "本地"
          }}账号)
        </el-dropdown-item>
        <el-dropdown-item
          v-if="user.canChangePassword"
          divided
          @click="emitter.emit('openChangePassword')"
        >
          <IconifyIconOffline :icon="LockPasswordLine" style="margin: 5px" />
          修改口令
        </el-dropdown-item>
        <el-dropdown-item
          v-if="imBindable"
          :divided="!user.canChangePassword"
          @click="emitter.emit('openWecomBind')"
        >
          <IconifyIconOffline :icon="Chat3Line" style="margin: 5px" />
          {{ imMenuLabel }}
        </el-dropdown-item>
        <el-dropdown-item
          :divided="!user.canChangePassword && !imBindable"
          @click="logout"
        >
          <IconifyIconOffline :icon="LogoutCircleRLine" style="margin: 5px" />
          退出登录
        </el-dropdown-item>
      </el-dropdown-menu>
    </template>
  </el-dropdown>
</template>

<style lang="scss" scoped>
.kd-user-link {
  display: flex;
  align-items: center;
  justify-content: space-around;
  height: 48px;
  padding: 10px;
  color: #000000d9;
  cursor: pointer;

  p {
    margin-left: 8px;
    font-size: 14px;
  }

  img {
    width: 22px;
    height: 22px;
    border-radius: 50%;
  }
}

.kd-user-menu {
  min-width: 140px;
}
</style>
