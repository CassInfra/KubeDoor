<script setup lang="ts">
import { computed, ref, watch } from "vue";
import {
  type EventRules,
  type EventRulesPreviewResult,
  type EventRulesPreviewRule,
  previewEventAlertRules
} from "@/api/event-alert-rules";
import { SEVERITY_LABELS, previewNotes } from "../rules-utils";

defineOptions({ name: "EventRulesPreviewDialog" });

/**
 * 试运行:把编辑区里还没保存的规则,对最近一段时间库里的真实事件跑一遍(master 限量、在线程里匹配),
 * 看被忽略多少、每条规则命中多少和例子。只统计,不发通知、不改事件
 */
const visible = defineModel<boolean>({ required: true });
const props = defineProps<{
  /** 已经通过校验的规则(打开前由页面校验) */
  rules: EventRules | null;
  clusters: string[];
}>();

const HOURS = [
  { value: 1, label: "最近 1 小时" },
  { value: 6, label: "最近 6 小时" },
  { value: 24, label: "最近 24 小时" },
  { value: 72, label: "最近 3 天" },
  { value: 168, label: "最近 7 天" }
];
const hours = ref(24);
const env = ref("");
const running = ref(false);
const error = ref("");
const result = ref<EventRulesPreviewResult | null>(null);

const notes = computed(() => (result.value ? previewNotes(result.value) : []));
const stats = computed(() => {
  const data = result.value;
  if (!data) return [];
  return [
    { label: "参与试运行的事件", value: data.total, type: "" },
    { label: "被忽略规则忽略", value: data.ignored, type: "info" },
    { label: "命中告警规则", value: data.matched, type: "danger" },
    { label: "没有命中", value: data.unmatched, type: "success" }
  ];
});

// 每次打开都清掉上次的结果(规则可能已经改过)
watch(visible, value => {
  if (!value) return;
  result.value = null;
  error.value = "";
});

function errorText(reason: unknown): string {
  const failure = reason as any;
  const data = failure?.response?.data;
  return (
    data?.message ||
    data?.error ||
    (failure?.code === "ECONNABORTED"
      ? "请求超时，请缩小时间范围后再试"
      : "") ||
    failure?.message ||
    "试运行失败，请稍后重试"
  );
}

async function run() {
  if (!props.rules || running.value) return;
  running.value = true;
  error.value = "";
  try {
    const res = await previewEventAlertRules({
      rules: props.rules,
      hours: hours.value,
      env: env.value || undefined
    });
    if (!res.success) throw new Error(res.message || "试运行失败");
    result.value = res.data;
  } catch (reason) {
    result.value = null;
    error.value = errorText(reason);
  } finally {
    running.value = false;
  }
}

function hitText(row: EventRulesPreviewRule) {
  return row.enabled ? String(row.hits) : "停用";
}
</script>

<template>
  <el-dialog
    v-model="visible"
    title="试运行"
    width="min(1180px, 96vw)"
    top="5vh"
    append-to-body
  >
    <el-alert
      type="info"
      :closable="false"
      show-icon
      class="intro"
      title="用编辑区里的规则（还没保存的也算）对库里最近的真实事件跑一遍：只统计，不发通知，也不改事件。"
    />
    <div class="controls">
      <el-select v-model="hours" class="hours">
        <el-option
          v-for="item in HOURS"
          :key="item.value"
          :label="item.label"
          :value="item.value"
        />
      </el-select>
      <el-select
        v-model="env"
        class="env"
        clearable
        filterable
        placeholder="全部集群"
      >
        <el-option
          v-for="item in clusters"
          :key="item"
          :label="item"
          :value="item"
        />
      </el-select>
      <el-button
        type="primary"
        :loading="running"
        :disabled="!rules"
        @click="run"
      >
        开始试运行
      </el-button>
    </div>
    <el-alert
      v-if="error"
      type="error"
      :title="error"
      :closable="false"
      show-icon
      class="intro"
    />

    <div v-if="result" v-loading="running" class="result">
      <div class="stats">
        <div v-for="item in stats" :key="item.label" class="stat">
          <div :class="['stat-value', item.type]">{{ item.value }}</div>
          <div class="stat-label">{{ item.label }}</div>
        </div>
      </div>
      <ul class="notes">
        <li v-for="note in notes" :key="note">{{ note }}</li>
      </ul>

      <h4>忽略规则</h4>
      <el-table
        :data="result.ignore_rules"
        size="small"
        border
        empty-text="没有忽略规则"
      >
        <el-table-column type="expand" width="40">
          <template #default="{ row }">
            <el-table
              :data="row.examples"
              size="small"
              class="examples"
              empty-text="没有例子"
            >
              <el-table-column
                label="最后时间"
                prop="lastTimestamp"
                width="160"
              />
              <el-table-column label="集群 / 命名空间" min-width="170">
                <template #default="{ row: item }">
                  {{ item.k8s }} / {{ item.namespace || "-" }}
                </template>
              </el-table-column>
              <el-table-column label="对象" min-width="200">
                <template #default="{ row: item }">
                  {{ item.kind }}/{{ item.name }}
                </template>
              </el-table-column>
              <el-table-column label="原因" prop="reason" width="150" />
              <el-table-column label="次数" prop="count" width="70" />
              <el-table-column
                label="消息"
                prop="message"
                min-width="260"
                show-overflow-tooltip
              />
            </el-table>
          </template>
        </el-table-column>
        <el-table-column label="名称" prop="name" min-width="260" />
        <el-table-column label="忽略的事件" width="120" align="right">
          <template #default="{ row }">
            <span :class="{ muted: !row.enabled }">{{ hitText(row) }}</span>
          </template>
        </el-table-column>
      </el-table>

      <h4>告警规则（按顺序匹配）</h4>
      <el-table
        :data="result.alert_rules"
        size="small"
        border
        empty-text="没有告警规则"
      >
        <el-table-column type="expand" width="40">
          <template #default="{ row }">
            <el-table
              :data="row.examples"
              size="small"
              class="examples"
              empty-text="没有例子"
            >
              <el-table-column
                label="最后时间"
                prop="lastTimestamp"
                width="160"
              />
              <el-table-column label="集群 / 命名空间" min-width="170">
                <template #default="{ row: item }">
                  {{ item.k8s }} / {{ item.namespace || "-" }}
                </template>
              </el-table-column>
              <el-table-column label="对象" min-width="200">
                <template #default="{ row: item }">
                  {{ item.kind }}/{{ item.name }}
                </template>
              </el-table-column>
              <el-table-column label="原因" prop="reason" width="150" />
              <el-table-column label="次数" prop="count" width="70" />
              <el-table-column
                label="消息"
                prop="message"
                min-width="260"
                show-overflow-tooltip
              />
            </el-table>
          </template>
        </el-table-column>
        <el-table-column label="#" width="50">
          <template #default="{ row }">{{ row.index + 1 }}</template>
        </el-table-column>
        <el-table-column label="名称" prop="name" min-width="220" />
        <el-table-column label="级别" width="90">
          <template #default="{ row }">
            {{ SEVERITY_LABELS[row.severity] || row.severity }}
          </template>
        </el-table-column>
        <el-table-column label="命中" width="90" align="right">
          <template #default="{ row }">
            <span
              :class="{ muted: !row.enabled, hit: row.enabled && row.hits }"
            >
              {{ hitText(row) }}
            </span>
          </template>
        </el-table-column>
        <el-table-column width="180" align="right">
          <template #header>
            <el-tooltip
              content="这些事件这条规则也能命中，但排在前面的规则先命中了（一个事件只发一条通知）"
              placement="top"
            >
              <span class="with-help">被前面的规则先命中</span>
            </el-tooltip>
          </template>
          <template #default="{ row }">
            {{ row.enabled ? row.shadowed : "-" }}
          </template>
        </el-table-column>
      </el-table>
    </div>
    <el-empty
      v-else-if="!running && !error"
      :image-size="60"
      description="选好时间范围后点「开始试运行」"
    />
    <div v-else-if="running" v-loading="true" class="placeholder" />
  </el-dialog>
</template>

<style scoped lang="scss">
.intro {
  margin-bottom: 12px;
}

.controls {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  margin-bottom: 12px;
}

.hours {
  width: 150px;
}

.env {
  width: 220px;
}

.stats {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
  margin-bottom: 8px;
}

.stat {
  padding: 10px 14px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
}

.stat-value {
  font-size: 22px;
  font-weight: 600;

  &.info {
    color: var(--el-text-color-secondary);
  }

  &.danger {
    color: var(--el-color-danger);
  }

  &.success {
    color: var(--el-color-success);
  }
}

.stat-label {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.notes {
  padding-left: 18px;
  margin: 8px 0 4px;
  font-size: 12px;
  line-height: 1.8;
  color: var(--el-text-color-secondary);
}

h4 {
  margin: 16px 0 8px;
  font-size: 14px;
}

.examples {
  margin: 0 12px 0 40px;
}

.muted {
  color: var(--el-text-color-placeholder);
}

.hit {
  font-weight: 600;
  color: var(--el-color-danger);
}

.with-help {
  cursor: help;
  border-bottom: 1px dashed var(--el-text-color-secondary);
}

.placeholder {
  height: 160px;
}
</style>
