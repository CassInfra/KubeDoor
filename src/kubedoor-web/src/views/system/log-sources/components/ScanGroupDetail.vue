<script setup lang="ts">
// 扫描结果里一组的详情（表格展开行）：表 / 索引、字段猜测、绑定建议、字段、取值、样本
import { computed } from "vue";
import type { LogScanGroup, LogScanResult } from "@/api/system";
import {
  MATCH_LABELS,
  conditionsText,
  formatBytes,
  formatCount,
  semanticLabel,
  shortTime,
  valueText
} from "../log-sources-utils";

defineOptions({ name: "LogScanGroupDetail" });

const props = defineProps<{
  group: LogScanGroup;
  scan: LogScanResult;
}>();

const guesses = computed(() =>
  Object.entries(props.group.fields_guess ?? {})
    .filter(([key, value]) => key !== "duration_unit" && value)
    .map(([key, value]) => ({
      label: key === "time" ? "时间" : semanticLabel(key),
      value: String(value)
    }))
);
const suggestions = computed(() =>
  (props.scan.suggested_bindings ?? []).filter(
    item => item.group === props.group.ref
  )
);
const first = computed(() => props.group.targets?.[0]);
const values = computed(() =>
  Object.entries(first.value?.values ?? {}).filter(
    ([, list]) => Array.isArray(list) && list.length
  )
);
const samples = computed(() => (first.value?.samples ?? []).slice(0, 2));
</script>

<template>
  <div class="scan-detail">
    <div class="detail-title">表 / 索引（{{ group.targets.length }}）</div>
    <el-table :data="group.targets" border size="small" max-height="260">
      <el-table-column label="名称" min-width="220" show-overflow-tooltip>
        <template #default="{ row }">
          <code>{{ row.target }}</code>
          <span v-if="row.local" class="sub">本地表 {{ row.local }}</span>
        </template>
      </el-table-column>
      <el-table-column label="行数" width="100">
        <template #default="{ row }">{{ formatCount(row.rows) }}</template>
      </el-table-column>
      <el-table-column label="大小" width="90">
        <template #default="{ row }">{{ formatBytes(row.bytes) }}</template>
      </el-table-column>
      <el-table-column label="时间范围" min-width="230">
        <template #default="{ row }">
          <span v-if="row.first || row.last">
            {{ shortTime(row.first) || "?" }} ~ {{ shortTime(row.last) || "?" }}
          </span>
          <span v-else class="sub">—</span>
        </template>
      </el-table-column>
      <el-table-column label="保留" width="80">
        <template #default="{ row }">
          {{
            typeof row.retention_days === "number"
              ? `${row.retention_days} 天`
              : "—"
          }}
        </template>
      </el-table-column>
      <el-table-column
        label="排序键"
        min-width="160"
        prop="sorting_key"
        show-overflow-tooltip
      />
    </el-table>

    <div class="detail-title">识别结果</div>
    <div class="detail-grid">
      <span class="detail-label">字段</span>
      <div class="tag-line">
        <el-tag
          v-for="item in guesses"
          :key="item.label"
          size="small"
          effect="plain"
        >
          {{ item.label }} = {{ item.value }}
        </el-tag>
        <el-tag
          v-if="group.fields_guess?.duration_unit"
          size="small"
          effect="plain"
        >
          耗时单位 {{ group.fields_guess.duration_unit }}
        </el-tag>
        <span v-if="!guesses.length" class="sub">没有识别出来，需要手填</span>
      </div>
      <span class="detail-label">集群字段</span>
      <div>
        <code v-if="group.cluster_field_guess">{{
          group.cluster_field_guess
        }}</code>
        <span v-else class="sub">没有（可能一表一集群，或者不区分集群）</span>
      </div>
      <template v-if="group.id_fields?.length">
        <span class="detail-label">ID 字段</span>
        <div class="tag-line">
          <el-tag
            v-for="item in group.id_fields"
            :key="item.field"
            size="small"
            type="info"
            effect="plain"
          >
            {{ item.field }} · {{ item.shape }}
          </el-tag>
        </div>
      </template>
      <template v-if="group.text_ids?.length">
        <span class="detail-label">正文里的 ID</span>
        <div class="tag-line">
          <el-tag
            v-for="item in group.text_ids"
            :key="item.text"
            size="small"
            type="info"
            effect="plain"
          >
            {{ item.text }} · {{ item.shape }} · {{ item.count }} 次
          </el-tag>
        </div>
      </template>
      <template v-if="group.meta_description">
        <span class="detail-label">模板说明</span>
        <div>{{ group.meta_description }}</div>
      </template>
    </div>

    <template v-if="suggestions.length">
      <div class="detail-title">绑定建议</div>
      <ul class="detail-list">
        <li v-for="item in suggestions" :key="`${item.env}-${item.target}`">
          <strong>{{ item.env }}</strong> → <code>{{ item.target }}</code>
          <span class="sub">
            {{ conditionsText(item.conditions) || "整张表 / 整个索引" }} ·
            {{ MATCH_LABELS[item.match] || item.match }}
          </span>
        </li>
      </ul>
    </template>

    <div class="detail-title">字段（{{ group.columns.length }}）</div>
    <div class="tag-line columns">
      <span v-for="item in group.columns" :key="item.name" class="column">
        <code>{{ item.name }}</code>
        <span class="sub">{{ item.type }}</span>
      </span>
    </div>

    <template v-if="values.length">
      <div class="detail-title">取值（{{ first?.target }}，最近 15 分钟）</div>
      <div class="detail-grid">
        <template v-for="[field, list] in values" :key="field">
          <code class="detail-label">{{ field }}</code>
          <div class="tag-line">
            <el-tag
              v-for="value in list"
              :key="String(value)"
              size="small"
              type="info"
              effect="plain"
            >
              {{ value }}
            </el-tag>
          </div>
        </template>
      </div>
    </template>

    <template v-if="samples.length">
      <div class="detail-title">样本（已打码，每个值截断 300 字）</div>
      <pre v-for="(item, index) in samples" :key="index" class="sample">{{
        valueText(item, 2)
      }}</pre>
    </template>
  </div>
</template>

<style scoped lang="scss">
.scan-detail {
  padding: 4px 16px 12px 48px;
  font-size: 13px;
}

.detail-title {
  margin: 12px 0 6px;
  font-weight: 600;
}

.detail-grid {
  display: grid;
  grid-template-columns: 110px minmax(0, 1fr);
  gap: 6px 12px;
  align-items: start;
}

.detail-label {
  color: var(--el-text-color-secondary);
}

.tag-line {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 6px;
}

.columns {
  max-height: 160px;
  overflow: auto;
}

.column {
  display: inline-flex;
  gap: 4px;
  align-items: center;
  margin-right: 8px;
}

.detail-list {
  padding-left: 18px;
  margin: 0;
  line-height: 1.8;
  list-style: disc;
}

.sub {
  margin-left: 6px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

code {
  padding: 0 4px;
  font-family: Menlo, Consolas, monospace;
  font-size: 12px;
  background: var(--el-fill-color-light);
  border-radius: 3px;
}

.sample {
  max-height: 240px;
  padding: 8px 12px;
  margin: 0 0 8px;
  overflow: auto;
  font-family: Menlo, Consolas, monospace;
  font-size: 12px;
  line-height: 1.5;
  word-break: break-all;
  white-space: pre-wrap;
  background: var(--el-fill-color-light);
  border-radius: 4px;
}
</style>
