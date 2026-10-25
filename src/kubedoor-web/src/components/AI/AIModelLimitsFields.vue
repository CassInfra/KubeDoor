<script setup lang="ts">
import { computed } from "vue";
import { inferModelLimits, type AIModelLimits } from "@/utils/ai";

/**
 * 模型设置里的上下文窗口、最大输出（K token）和压缩阈值（%）；v-model 的值按 token 和小数保存。
 * 模型名变化时的重新预填由父组件处理（followModelLimits），这里只负责显示和输入。
 */
const props = defineProps<{ model: string; disabled?: boolean }>();
const limits = defineModel<AIModelLimits>({ required: true });

function update(patch: Partial<AIModelLimits>) {
  const next = { ...limits.value, ...patch };
  if (
    next.max_input_tokens !== null &&
    next.max_output_tokens !== null &&
    next.max_output_tokens > next.max_input_tokens
  )
    next.max_output_tokens = next.max_input_tokens;
  limits.value = next;
}

const toK = (tokens: number | null) =>
  tokens === null ? undefined : Math.round(tokens / 1024);
const inputK = computed({
  get: () => toK(limits.value.max_input_tokens),
  set: (value?: number | null) =>
    update({ max_input_tokens: value == null ? null : value * 1024 })
});
const outputK = computed({
  get: () => toK(limits.value.max_output_tokens),
  set: (value?: number | null) =>
    update({ max_output_tokens: value == null ? null : value * 1024 })
});
const ratioPercent = computed({
  get: () =>
    limits.value.compression_ratio === null
      ? undefined
      : Math.round(limits.value.compression_ratio * 100),
  set: (value?: number | null) =>
    update({ compression_ratio: value == null ? null : value / 100 })
});
const verify = computed(
  () => !!props.model.trim() && inferModelLimits(props.model).verify
);
</script>

<template>
  <el-form-item label="上下文与压缩">
    <div class="limits">
      <div class="limit">
        <span>上下文窗口（K token）</span>
        <el-input-number
          v-model="inputK"
          :min="4"
          :max="9765"
          :precision="0"
          controls-position="right"
          :disabled="disabled"
        />
      </div>
      <div class="limit">
        <span>最大输出（K token）</span>
        <el-input-number
          v-model="outputK"
          :min="1"
          :max="inputK ?? 9765"
          :precision="0"
          controls-position="right"
          :disabled="disabled"
        />
      </div>
      <div class="limit">
        <span>压缩阈值（%，默认 50）</span>
        <el-input-number
          v-model="ratioPercent"
          :min="20"
          :max="80"
          :step="5"
          :precision="0"
          controls-position="right"
          :disabled="disabled"
        />
      </div>
    </div>
    <p class="field-hint">
      按模型名自动填写：deepseek 开头 1024K / 384K，gpt 开头 1024K /
      128K，可以手动修改。上下文达到「窗口 ×
      压缩阈值」时，自动摘要较早的对话后继续。
    </p>
    <p v-if="verify" class="field-warning">
      该模型不在自动识别范围内，或是 gpt-4o 等旧型号，请按模型实际情况填写。
    </p>
  </el-form-item>
</template>

<style scoped lang="scss">
.limits {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
  width: 100%;
}

.limit {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
  font-size: 12px;
  line-height: 1.5;
  color: var(--el-text-color-secondary);

  .el-input-number {
    width: 100%;
  }
}

.field-hint,
.field-warning {
  width: 100%;
  margin: 8px 0 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--el-text-color-secondary);
}

.field-warning {
  color: var(--el-color-warning);
}

@media (width <= 640px) {
  .limits {
    grid-template-columns: 1fr;
  }
}
</style>
