<script setup lang="ts">
import { computed } from "vue";
import { useUserStoreHook } from "@/store/modules/user";

defineOptions({
  name: "ClusterPermTag"
});

/** 集群选择框旁的权限提示:当前集群是「只读」或「管理员」权限时显示(后端仍按权限校验每个操作) */
const props = defineProps<{ env?: string }>();

const user = useUserStoreHook();
const perm = computed(() =>
  props.env ? user.permissionOf(props.env) : "none"
);
</script>

<template>
  <el-tooltip
    v-if="perm === 'admin'"
    :content="`当前账号在集群 ${env} 是「管理员」权限,包括只有管理员能做的操作(如 CoreDNS、Istio 修改)`"
    placement="top"
  >
    <el-tag type="warning" effect="plain" class="ml-2">管理员</el-tag>
  </el-tooltip>
  <el-tooltip
    v-else-if="perm === 'read'"
    :content="`当前账号在集群 ${env} 是「只读」权限:可以查看,没有权限的修改操作会置灰`"
    placement="top"
  >
    <el-tag type="info" effect="plain" class="ml-2">只读</el-tag>
  </el-tooltip>
</template>
