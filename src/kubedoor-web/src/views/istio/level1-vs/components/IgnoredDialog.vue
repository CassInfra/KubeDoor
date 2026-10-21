<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { ElMessageBox } from "element-plus";
import { message } from "@/utils/message";
import {
  type IgnoredVS,
  type ScopeStats,
  type VSPattern,
  unignoreIstioVS
} from "@/api/istio";
import { PATTERN_TYPE_LABEL, formatTime, scopeText } from "../utils";

defineOptions({ name: "IstioIgnoredDialog" });

/** 本组忽略的 VS:手动忽略的,和被组的包含 / 忽略规则排除的(刷新现状时就按规则排除了);可以取消忽略 */
const props = defineProps<{
  groupId: number;
  groupName: string;
  items: IgnoredVS[];
  stats: ScopeStats;
  includePatterns: VSPattern[];
  ignorePatterns: VSPattern[];
  isAdmin: boolean;
}>();
const emit = defineEmits<{
  (e: "changed"): void;
  (e: "edit-rules"): void;
}>();
const visible = defineModel<boolean>({ default: false });

const keyword = ref("");
const kind = ref<"all" | "manual" | "rule">("all");
const selection = ref<IgnoredVS[]>([]);
const saving = ref(false);

watch(visible, open => {
  if (open) {
    keyword.value = "";
    kind.value = "all";
  }
});

const rows = computed(() => {
  const kw = keyword.value.trim().toLowerCase();
  return props.items.filter(item => {
    if (kind.value === "manual" && item.reason !== "manual") return false;
    if (kind.value === "rule" && item.reason === "manual") return false;
    return !kw || `${item.namespace}/${item.name}`.toLowerCase().includes(kw);
  });
});
const staleCount = computed(() => props.items.filter(i => i.stale).length);

function ruleText(items: VSPattern[]) {
  return items
    .map(i =>
      i.type === "regex"
        ? `${PATTERN_TYPE_LABEL.regex} ${i.pattern}`
        : i.pattern
    )
    .join("、");
}

const key = (item: IgnoredVS) => `${item.namespace}/${item.name}`;

async function unignore(items: IgnoredVS[]) {
  if (!items.length) return;
  const byRule = items.filter(i => i.reason !== "manual").length;
  const label =
    items.length === 1 ? key(items[0]) : `所选的 ${items.length} 个 VS`;
  await ElMessageBox.confirm(
    `在组「${props.groupName}」取消忽略 ${label}?` +
      (byRule
        ? `其中 ${byRule} 个是被组规则排除的,取消后会单独纳入本组,不受包含 / 忽略规则影响(之后可以再忽略)。`
        : "取消后重新显示在本组列表里。"),
    "取消忽略",
    { type: "info" }
  );
  saving.value = true;
  try {
    const res = await unignoreIstioVS(
      props.groupId,
      items.map(i => ({ namespace: i.namespace, name: i.name }))
    );
    message(res.msg || "已取消忽略", { type: "success" });
    emit("changed");
  } finally {
    saving.value = false;
  }
}
</script>

<template>
  <el-dialog
    v-model="visible"
    :title="`查看忽略 · ${groupName}`"
    width="1000px"
    top="6vh"
  >
    <div class="stats">
      共 <b>{{ stats.total }}</b> 个 VirtualService:本组
      <b>{{ stats.visible }}</b> 个,忽略 <b>{{ stats.ignored }}</b> 个(手动忽略
      {{ stats.manual }} 个,规则排除 {{ stats.by_rule }} 个)
      <span v-if="staleCount" class="sub"
        >;另有 {{ staleCount }} 条手动忽略的 VS 现状里已经没有了</span
      >
    </div>
    <div class="rules">
      <span>包含:{{ ruleText(includePatterns) || "全部" }}</span>
      <span>忽略:{{ ruleText(ignorePatterns) || "无" }}</span>
      <el-button v-if="isAdmin" link type="primary" @click="emit('edit-rules')"
        >修改规则</el-button
      >
    </div>
    <div class="toolbar">
      <el-input
        v-model="keyword"
        placeholder="VS 名称"
        clearable
        size="small"
        style="width: 220px"
      />
      <el-radio-group v-model="kind" size="small">
        <el-radio-button value="all">全部</el-radio-button>
        <el-radio-button value="manual">手动忽略</el-radio-button>
        <el-radio-button value="rule">规则排除</el-radio-button>
      </el-radio-group>
    </div>
    <el-table
      :data="rows"
      :row-key="key"
      border
      size="small"
      max-height="520"
      empty-text="没有忽略的 VirtualService"
      @selection-change="selection = $event"
    >
      <el-table-column v-if="isAdmin" type="selection" width="36" />
      <el-table-column label="VirtualService" min-width="220">
        <template #default="{ row }">
          <span class="sub">{{ row.namespace }}/</span><b>{{ row.name }}</b>
          <el-tag v-if="row.vs_id" size="small" type="info" class="ml-1"
            >已纳管</el-tag
          >
        </template>
      </el-table-column>
      <el-table-column label="原因" min-width="200">
        <template #default="{ row }">
          <el-tag
            size="small"
            :type="row.reason === 'manual' ? 'warning' : 'info'"
            effect="plain"
            >{{ row.reason === "manual" ? "手动忽略" : "规则排除" }}</el-tag
          >
          <span v-if="row.reason !== 'manual'" class="sub ml-1">{{
            scopeText(row)
          }}</span>
        </template>
      </el-table-column>
      <el-table-column label="所在集群" min-width="220">
        <template #default="{ row }">
          <span v-if="row.clusters.length">{{ row.clusters.join("、") }}</span>
          <span v-else class="sub">现状里已经没有</span>
        </template>
      </el-table-column>
      <el-table-column label="忽略人 / 时间" width="170">
        <template #default="{ row }">
          <template v-if="row.reason === 'manual'">
            <div>{{ row.by || "-" }}</div>
            <div class="sub">{{ formatTime(row.at) }}</div>
          </template>
          <span v-else class="sub">按组规则</span>
        </template>
      </el-table-column>
      <el-table-column v-if="isAdmin" label="操作" width="100" align="center">
        <template #default="{ row }">
          <el-button link type="primary" @click="unignore([row])"
            >取消忽略</el-button
          >
        </template>
      </el-table-column>
    </el-table>
    <template #footer>
      <el-button
        v-if="isAdmin"
        type="primary"
        plain
        :disabled="!selection.length"
        :loading="saving"
        style="float: left"
        @click="unignore(selection)"
      >
        取消忽略所选({{ selection.length }})
      </el-button>
      <el-button @click="visible = false">关闭</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.stats {
  margin-bottom: 6px;
  font-size: 14px;
}

.rules {
  display: flex;
  flex-wrap: wrap;
  gap: 16px;
  align-items: center;
  margin-bottom: 8px;
  font-size: 12px;
  color: var(--el-text-color-regular);
}

.toolbar {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-bottom: 8px;
}

.sub {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
</style>
