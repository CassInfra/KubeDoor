<script setup lang="ts">
import { computed, getCurrentInstance } from "vue";
import type { AITool } from "@/api/ai";
import { formatAIStatus } from "@/utils/ai";
import AIToolCard from "./AIToolCard.vue";

const props = withDefaults(
  defineProps<{
    tools: AITool[];
    writable: boolean;
    deciding?: string;
    secret?: string;
    active?: boolean;
  }>(),
  { active: true }
);
const expanded = defineModel<boolean>("expanded", { default: false });
const emit = defineEmits<{
  decide: [actionId: string, decision: "approve" | "reject"];
}>();
const detailsId = `ai-tool-details-${getCurrentInstance()!.uid}`;
const pendingCount = computed(
  () => props.tools.filter(tool => tool.pending).length
);
const label = (tool: AITool) =>
  props.secret ? tool.name.split(props.secret).join("[已隐藏]") : tool.name;
const running = (tool: AITool) =>
  props.active &&
  !tool.pending &&
  ["running", "queued", "cancelling"].includes(tool.status);
const failed = (tool: AITool) =>
  ["failed", "error", "unknown"].includes(tool.status);
</script>

<template>
  <section :class="['tool-group', { 'has-approval': pendingCount }]">
    <button
      type="button"
      class="tool-group-toggle"
      :aria-expanded="expanded"
      :aria-controls="detailsId"
      @click="expanded = !expanded"
    >
      <span class="tool-group-title"
        >工具调用 <span>{{ tools.length }}</span></span
      >
      <span v-if="pendingCount" class="tool-group-pending"
        >{{ pendingCount }} 项待批准</span
      >
      <span class="tool-group-hint">{{ expanded ? "收起" : "展开" }}</span>
      <svg
        :class="['tool-group-chevron', { expanded }]"
        width="14"
        height="14"
        viewBox="0 0 16 16"
        aria-hidden="true"
      >
        <path d="m4 6 4 4 4-4" />
      </svg>
      <span class="tool-group-progress">
        <span
          v-for="tool in tools"
          :key="tool.id"
          :class="[
            'tool-progress-item',
            {
              running: running(tool),
              pending: tool.pending,
              failed: failed(tool)
            }
          ]"
          :title="`${label(tool)}：${formatAIStatus(tool.status)}`"
        >
          <span v-if="running(tool)" class="tool-spinner" aria-hidden="true" />
          <svg
            v-else-if="['completed', 'success', 'done'].includes(tool.status)"
            class="tool-check"
            width="12"
            height="12"
            viewBox="0 0 16 16"
            aria-hidden="true"
          >
            <path d="m3 8 3 3 7-7" />
          </svg>
          <span v-else class="tool-state-dot" aria-hidden="true" />
          <span class="tool-progress-name">{{ label(tool) }}</span>
          <span class="sr-only">{{ formatAIStatus(tool.status) }}</span>
        </span>
      </span>
    </button>
    <div v-show="expanded" :id="detailsId" class="tool-group-details">
      <AIToolCard
        v-for="tool in tools"
        :key="tool.id"
        :tool="tool"
        :writable="writable"
        :deciding="deciding"
        :secret="secret"
        @decide="(id, decision) => emit('decide', id, decision)"
      />
    </div>
  </section>
</template>

<style scoped lang="scss">
.tool-group {
  margin-bottom: 14px;
  overflow: hidden;
  background: var(--el-bg-color);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
}

.has-approval {
  border-color: var(--el-color-warning);
}

.tool-group-toggle {
  display: flex;
  flex-wrap: wrap;
  gap: 9px;
  align-items: center;
  width: 100%;
  padding: 10px 12px;
  color: var(--el-text-color-primary);
  text-align: left;
  cursor: pointer;
  background: transparent;
  border: 0;

  &:hover {
    background: var(--el-fill-color-light);
  }

  &:focus-visible {
    outline: 2px solid var(--el-color-primary);
    outline-offset: -2px;
  }
}

.tool-group-title {
  display: flex;
  gap: 6px;
  align-items: center;
  font-size: 12px;
  font-weight: 600;

  span {
    min-width: 18px;
    padding: 1px 5px;
    font-size: 11px;
    text-align: center;
    background: var(--el-fill-color);
    border-radius: 5px;
  }
}

.tool-group-pending {
  font-size: 11px;
  color: var(--el-color-warning);
}

.tool-group-hint {
  margin-left: auto;
  font-size: 11px;
  color: var(--el-text-color-secondary);
}

.tool-group-chevron {
  color: var(--el-text-color-secondary);
  transition: transform 150ms ease;

  &.expanded {
    transform: rotate(180deg);
  }
}

.tool-group-chevron path,
.tool-check path {
  fill: none;
  stroke: currentcolor;
  stroke-linecap: round;
  stroke-linejoin: round;
  stroke-width: 1.8;
}

.tool-group-progress {
  display: flex;
  flex-basis: 100%;
  flex-wrap: wrap;
  gap: 6px;
  min-width: 0;
}

.tool-progress-item {
  display: inline-flex;
  gap: 5px;
  align-items: center;
  min-width: 0;
  max-width: 100%;
  padding: 3px 7px;
  font-size: 11px;
  color: var(--el-text-color-secondary);
  background: var(--el-fill-color-light);
  border-radius: 5px;

  &.running {
    color: var(--el-color-primary);
  }

  &.pending {
    color: var(--el-color-warning);
  }

  &.failed {
    color: var(--el-color-danger);
  }
}

.tool-progress-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tool-spinner {
  flex-shrink: 0;
  width: 11px;
  height: 11px;
  border: 1.5px solid var(--el-color-primary-light-7);
  border-top-color: currentcolor;
  border-radius: 50%;
  animation: ai-tool-spin 800ms linear infinite;
}

.tool-check {
  flex-shrink: 0;
  color: var(--el-color-success);
}

.tool-state-dot {
  flex-shrink: 0;
  width: 5px;
  height: 5px;
  background: currentcolor;
  border-radius: 50%;
}

.tool-group-details {
  padding: 0 10px 10px;
  border-top: 1px solid var(--el-border-color-lighter);
}

@keyframes ai-tool-spin {
  to {
    transform: rotate(360deg);
  }
}

@media (prefers-reduced-motion: reduce) {
  .tool-group-chevron {
    transition: none;
  }

  .tool-spinner {
    animation: none;
  }
}
</style>
