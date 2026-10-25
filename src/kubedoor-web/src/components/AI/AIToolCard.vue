<script setup lang="ts">
import { computed, ref } from "vue";
import type { AITool } from "@/api/ai";
import {
  controlBadge,
  controlPlan,
  formatAIStatus,
  safeAIText,
  verificationOf
} from "@/utils/ai";

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
// 副本数、资源、JVM 等修改：网关按花折资源管控状态生成的执行计划和执行后回读
const plan = computed(() => controlPlan(props.tool.preview));
const badge = computed(() => (plan.value ? controlBadge(plan.value) : null));
const diffSteps = computed(() =>
  (plan.value?.steps || []).filter(step => step.diff)
);
const verification = computed(() => verificationOf(props.tool.result));
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
      <el-tag v-if="badge" :type="badge.type" size="small" effect="light">{{
        badge.label
      }}</el-tag>
      <span v-if="tool.env">集群 {{ tool.env }}</span>
      <span v-if="tool.namespace">命名空间 {{ tool.namespace }}</span>
      <span>来源 {{ tool.source || "等待工具返回" }}</span>
      <span v-if="time">观测 {{ time }}</span>
      <span v-if="tool.freshness">{{ tool.freshness }}</span>
      <span v-if="tool.truncated" class="truncated">结果已截断</span>
    </div>
    <p
      v-if="verification"
      :class="[
        'verification',
        verification.matched === false
          ? 'mismatch'
          : verification.matched
            ? 'matched'
            : 'unchecked'
      ]"
    >
      {{ verification.message }}
    </p>
    <div v-if="expanded || tool.pending" class="tool-details">
      <div v-if="plan" class="control-plan">
        <p class="control-message">{{ plan.message }}</p>
        <div class="detail-label">执行步骤</div>
        <ol class="plan-steps">
          <li v-for="(step, index) in plan.steps" :key="index">
            {{ step.name }}<span class="step-target">{{ step.target }}</span>
          </li>
        </ol>
        <template v-if="plan.changes.length">
          <div class="detail-label">变化</div>
          <div class="plan-changes">
            <table>
              <thead>
                <tr>
                  <th>范围</th>
                  <th>项目</th>
                  <th>当前</th>
                  <th>目标</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(change, index) in plan.changes" :key="index">
                  <td>{{ change.scope }}</td>
                  <td>{{ change.label }}</td>
                  <td>{{ change.current }}</td>
                  <td class="target">{{ change.target }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </template>
        <ul v-if="plan.effects.length" class="plan-notes">
          <li v-for="(effect, index) in plan.effects" :key="index">
            {{ effect }}
          </li>
        </ul>
        <ul v-if="plan.warnings.length" class="plan-notes warnings">
          <li v-for="(warning, index) in plan.warnings" :key="index">
            {{ warning }}
          </li>
        </ul>
        <div v-for="(step, index) in diffSteps" :key="index">
          <div class="detail-label">配置差异（{{ step.name }}）</div>
          <pre>{{ text(step.diff) }}</pre>
        </div>
        <details class="plan-requests">
          <summary>执行请求明细</summary>
          <pre>{{ text(plan.steps.map(step => step.request)) }}</pre>
        </details>
      </div>
      <template v-if="tool.arguments !== undefined">
        <div class="detail-label">准确参数</div>
        <pre>{{ text(tool.arguments) }}</pre>
      </template>
      <template v-if="tool.command">
        <div class="detail-label">执行命令</div>
        <pre>{{ text(tool.command) }}</pre>
      </template>
      <template v-if="tool.preview && !plan">
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

.verification {
  padding: 8px 12px;
  margin: 0 12px 10px;
  font-size: 12px;
  line-height: 1.6;
  border-radius: 5px;

  &.matched {
    color: var(--el-color-success);
    background: var(--el-color-success-light-9);
  }

  &.mismatch {
    color: var(--el-color-danger);
    background: var(--el-color-danger-light-9);
  }

  &.unchecked {
    color: var(--el-text-color-secondary);
    background: var(--el-fill-color-light);
  }
}

.control-plan {
  font-size: 13px;
}

.control-message {
  margin: 4px 0 0;
  color: var(--el-text-color-regular);
}

.plan-steps {
  padding-left: 20px;
  margin: 6px 0 0;
  list-style: decimal;

  li {
    line-height: 1.8;
  }
}

.step-target {
  margin-left: 8px;
  font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.plan-changes {
  margin-top: 6px;
  overflow-x: auto;

  table {
    width: 100%;
    font-size: 12px;
    border-collapse: collapse;
  }

  th,
  td {
    padding: 4px 8px;
    text-align: left;
    white-space: nowrap;
    border-bottom: 1px solid var(--el-border-color-lighter);
  }

  th {
    font-weight: 500;
    color: var(--el-text-color-secondary);
  }

  .target {
    font-weight: 600;
    color: var(--el-color-primary);
  }
}

.plan-notes {
  padding-left: 18px;
  margin: 8px 0 0;
  line-height: 1.7;
  color: var(--el-text-color-regular);
  list-style: disc;

  &.warnings {
    color: var(--el-color-warning-dark-2);
  }
}

.plan-requests {
  margin-top: 8px;
  font-size: 12px;
  color: var(--el-text-color-secondary);

  summary {
    cursor: pointer;
  }
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
