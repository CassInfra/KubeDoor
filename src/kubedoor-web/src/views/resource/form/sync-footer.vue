<script setup lang="ts">
import { computed, reactive } from "vue";
import { QuestionFilled } from "@element-plus/icons-vue";
import { transformI18n } from "@/plugins/i18n";
import type { FormItemProps } from "../utils/types";

const props = defineProps<{
  formInline: FormItemProps;
  syncEnvList: string[];
}>();

const formInline = reactive(props.formInline);
const syncEnvList = computed(() =>
  props.syncEnvList.filter(env => env && env !== formInline.env)
);

function onSyncToggle(enabled: boolean) {
  if (!enabled) formInline.sync_env = "";
}
</script>

<template>
  <div class="resource-sync-footer">
    <el-checkbox v-model="formInline.sync_enabled" @change="onSyncToggle">
      <strong>{{ transformI18n("resource.syncTo") }}</strong>
    </el-checkbox>
    <el-tooltip :content="transformI18n('resource.syncHint')" placement="top">
      <el-icon class="resource-sync-footer__hint"><QuestionFilled /></el-icon>
    </el-tooltip>
    <el-select
      v-model="formInline.sync_env"
      class="resource-sync-footer__select"
      :disabled="!formInline.sync_enabled"
      :placeholder="transformI18n('resource.placeholder')"
      clearable
      filterable
    >
      <el-option
        v-for="env in syncEnvList"
        :key="env"
        :label="env"
        :value="env"
      />
    </el-select>
  </div>
</template>

<style scoped>
.resource-sync-footer {
  display: flex;
  min-width: 0;
  align-items: center;
  gap: 8px;
}

.resource-sync-footer__hint {
  flex: 0 0 auto;
  color: var(--el-text-color-secondary);
  cursor: help;
}

.resource-sync-footer__select {
  width: 180px;
}
</style>
