<script setup lang="ts">
import { computed } from "vue";
import { useUserStoreHook } from "@/store/modules/user";

defineOptions({
  name: "ClusterPermTag"
});

/** 集群选择框旁的权限提示:对当前集群只读时提醒用户不能做修改(后端会拒绝写操作) */
const props = defineProps<{ env?: string }>();

const user = useUserStoreHook();
const readOnly = computed(
  () => !!props.env && user.permissionOf(props.env) === "read"
);
</script>

<template>
  <el-tooltip
    v-if="readOnly"
    :content="`当前账号对集群 ${env} 只有只读权限:可以查看,不能执行修改操作`"
    placement="top"
  >
    <el-tag type="info" effect="plain" class="ml-2">只读</el-tag>
  </el-tooltip>
</template>
