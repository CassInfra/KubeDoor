<script setup lang="ts">
import { Delete, Plus } from "@element-plus/icons-vue";
import type { VSPattern } from "@/api/istio";
import { PATTERN_TYPE_LABEL } from "../utils";

defineOptions({ name: "IstioPatternList" });

/** 组的包含 / 忽略规则列表:每条选通配符或正则 */
defineProps<{ emptyText: string }>();
const items = defineModel<VSPattern[]>({ required: true });

const PLACEHOLDER = {
  glob: "如 b2c-*、*-int、istio-system/cis-*",
  regex: "如 b2c[-.].*(全匹配)"
};
</script>

<template>
  <div class="pattern-list">
    <div v-for="(item, index) in items" :key="index" class="pattern-row">
      <el-select v-model="item.type" size="small" class="pattern-type">
        <el-option
          v-for="(label, value) in PATTERN_TYPE_LABEL"
          :key="value"
          :label="label"
          :value="value"
        />
      </el-select>
      <el-input
        v-model.trim="item.pattern"
        size="small"
        :placeholder="PLACEHOLDER[item.type]"
        class="pattern-input"
      />
      <el-button
        link
        type="danger"
        :icon="Delete"
        @click="items.splice(index, 1)"
      />
    </div>
    <div v-if="!items.length" class="empty">{{ emptyText }}</div>
    <el-button
      size="small"
      :icon="Plus"
      @click="items.push({ type: 'glob', pattern: '' })"
    >
      添加
    </el-button>
  </div>
</template>

<style scoped>
.pattern-list {
  width: 100%;
}

.pattern-row {
  display: flex;
  gap: 6px;
  align-items: center;
  margin-bottom: 6px;
}

.pattern-type {
  flex: none;
  width: 88px;
}

.pattern-input {
  flex: 1;
  min-width: 0;
}

.pattern-input :deep(input) {
  font-family: Consolas, Monaco, monospace;
}

.empty {
  margin-bottom: 6px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
</style>
