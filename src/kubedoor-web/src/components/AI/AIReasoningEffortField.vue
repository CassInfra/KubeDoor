<script setup lang="ts">
import { computed } from "vue";
import type { AIReasoningEffort } from "@/api/ai";

/** DeepSeek 思考强度；v-model 为 null 表示用模型默认（high），请求里不带参数 */
defineProps<{ disabled?: boolean }>();
const effort = defineModel<AIReasoningEffort | null>({ default: null });

const DEFAULT = "default";
const options = [
  { value: DEFAULT, label: "默认（high）" },
  { value: "low", label: "low" },
  { value: "max", label: "max" },
  { value: "off", label: "关闭" }
];
const selected = computed({
  get: () => effort.value ?? DEFAULT,
  set: (value: string) => {
    effort.value = value === DEFAULT ? null : (value as AIReasoningEffort);
  }
});
</script>

<template>
  <el-form-item label="思考强度">
    <el-select v-model="selected" :disabled="disabled">
      <el-option
        v-for="item in options"
        :key="item.value"
        :label="item.label"
        :value="item.value"
      />
    </el-select>
    <p class="field-hint">
      仅对 DeepSeek 模型生效。调低或关闭思考，回答更快、推理 token
      更少，上下文增长也更慢；复杂排障建议保持默认。会话标题、记忆总结和自动摘要始终不开思考。
    </p>
  </el-form-item>
</template>

<style scoped>
.field-hint {
  width: 100%;
  margin: 8px 0 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--el-text-color-secondary);
}
</style>
