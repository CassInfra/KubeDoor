<script setup lang="ts">
import { computed, ref } from "vue";
import type { AITool } from "@/api/ai";
import { formatAIStatus, safeAIText } from "@/utils/ai";

const props = defineProps<{
  tool: AITool;
  writable: boolean;
  deciding?: string;
  secret?: string;
}>();
const emit = defineEmits<{
  decide: [actionId: string, decision: "approve" | "reject"];
}>();
const expanded = ref(false);
const text = (value: unknown) => {
  const result = safeAIText(value);
  return props.secret ? result.split(props.secret).join("[已隐藏]") : result;
};
const statusType = computed(() =>
  props.tool.pending || props.tool.status === "unknown"
    ? "warning"
    : ["failed", "error", "rejected"].includes(props.tool.status)
      ? "danger"
      : props.tool.status === "running"
        ? "primary"
        : "success"
);
const time = computed(() => {
  if (!props.tool.observed_at) return "";
  const parsed = new Date(props.tool.observed_at);
  return Number.isNaN(parsed.getTime())
    ? props.tool.observed_at
    : parsed.toLocaleString();
});
</script>

<template>
  <section :class="['tool-card', { 'needs-approval': tool.pending }]">
    <button
      class="tool-heading"
      type="button"
      :aria-expanded="expanded || tool.pending"
      @click="expanded = !expanded"
    >
      <span class="tool-title">{{ tool.name }}</span>
      <el-tag :type="statusType" size="small" effect="plain">{{
        formatAIStatus(tool.status)
      }}</el-tag>
      <span class="expand-indicator">{{
        expanded || tool.pending ? "收起" : "详情"
      }}</span>
    </button>
    <div class="tool-meta">
      <span v-if="tool.env">集群 {{ tool.env }}</span>
      <span v-if="tool.namespace">命名空间 {{ tool.namespace }}</span>
      <span>来源 {{ tool.source || "等待工具返回" }}</span>
      <span v-if="time">观测 {{ time }}</span>
      <span v-if="tool.freshness">{{ tool.freshness }}</span>
      <span v-if="tool.truncated" class="truncated">结果已截断</span>
    </div>
    <div v-if="expanded || tool.pending" class="tool-details">
      <template v-if="tool.arguments !== undefined">
        <div class="detail-label">准确参数</div>
        <pre>{{ text(tool.arguments) }}</pre>
      </template>
      <template v-if="tool.command">
        <div class="detail-label">执行命令</div>
        <pre>{{ text(tool.command) }}</pre>
      </template>
      <template v-if="tool.preview">
        <div class="detail-label">变更预览</div>
        <pre>{{ text(tool.preview) }}</pre>
      </template>
      <template v-if="tool.diff">
        <div class="detail-label">配置差异</div>
        <pre>{{ text(tool.diff) }}</pre>
      </template>
      <template v-if="tool.result !== undefined">
        <div class="detail-label">工具结果</div>
        <pre>{{ text(tool.result) }}</pre>
      </template>
      <p v-if="tool.error" class="tool-error">{{ text(tool.error) }}</p>
    </div>
    <div v-if="tool.pending" class="approval-actions">
      <span>{{
        writable ? "批准后执行以上动作" : "当前账号只读，不能批准修改"
      }}</span>
      <el-button
        size="small"
        :disabled="!!deciding || !tool.action_id"
        @click="emit('decide', tool.action_id!, 'reject')"
        >拒绝</el-button
      >
      <el-button
        size="small"
        type="primary"
        :loading="deciding === tool.action_id"
        :disabled="!writable || !!deciding || !tool.action_id"
        @click="emit('decide', tool.action_id!, 'approve')"
        >批准这次操作</el-button
      >
    </div>
  </section>
</template>

<style scoped lang="scss">
.tool-card {
  margin-top: 12px;
  overflow: hidden;
  background: var(--el-bg-color);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
}

.needs-approval {
  border-color: var(--el-color-warning);
}

.tool-heading {
  display: flex;
  gap: 10px;
  align-items: center;
  width: 100%;
  padding: 12px;
  color: var(--el-text-color-primary);
  text-align: left;
  cursor: pointer;
  background: transparent;
  border: 0;
}

.tool-title {
  font-weight: 600;
  overflow-wrap: anywhere;
}

.expand-indicator {
  margin-left: auto;
  font-size: 12px;
  color: var(--el-color-primary);
}

.tool-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 14px;
  padding: 0 12px 10px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.truncated {
  color: var(--el-color-warning);
}

.tool-details {
  padding: 0 12px 12px;
}

.detail-label {
  margin-top: 8px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

pre {
  max-height: 320px;
  padding: 10px;
  margin: 6px 0 0;
  overflow: auto;
  font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
  font-size: 12px;
  overflow-wrap: anywhere;
  white-space: pre-wrap;
  background: var(--el-fill-color-light);
  border-radius: 5px;
}

.tool-error {
  color: var(--el-color-danger);
}

.approval-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  padding: 12px;
  background: var(--el-color-warning-light-9);
  border-top: 1px solid var(--el-border-color-lighter);
}

.approval-actions span {
  flex: 1;
  font-size: 12px;
  color: var(--el-text-color-regular);
}
</style>
