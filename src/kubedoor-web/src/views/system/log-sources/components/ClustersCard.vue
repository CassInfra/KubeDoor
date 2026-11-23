<script setup lang="ts">
// 「集群」卡片：左边是集群列表（KubeDoor 已登记的 + 配置里有但已不在的），右边是选中集群的说明和日志源绑定
import { computed, reactive, ref, watch } from "vue";
import { ElMessageBox } from "element-plus";
import { message } from "@/utils/message";
import type { LogKnownCluster } from "@/api/system";
import BindingDialog from "./BindingDialog.vue";
import PreviewDialog from "./PreviewDialog.vue";
import {
  type BindingForm,
  type LogSourcesForm,
  type ScanMap,
  AGENT_PAGE,
  CLUSTER_NOTE_MAX_LENGTH,
  MAX_BINDINGS,
  bindingConnection,
  bindingForm,
  clusterRows,
  conditionsText,
  defaultBinding,
  emptyCluster,
  removeBinding,
  removeCluster,
  upsertBinding
} from "../log-sources-utils";

defineOptions({ name: "LogClustersCard" });

const form = defineModel<LogSourcesForm>("form", { required: true });
/** 选中的集群（保存前校验出错时页面会切到出错的集群） */
const selected = defineModel<string>("selected", { required: true });

const props = defineProps<{
  known: LogKnownCluster[];
  scans: ScanMap;
}>();

const dialog = reactive({
  visible: false,
  creating: false,
  binding: null as BindingForm | null
});
const preview = reactive({
  visible: false,
  form: null as LogSourcesForm | null,
  source: ""
});
const filter = ref("");

const rows = computed(() => clusterRows(form.value, props.known));
const shownRows = computed(() => {
  const text = filter.value.trim().toLowerCase();
  return text
    ? rows.value.filter(
        row =>
          row.env.toLowerCase().includes(text) ||
          row.aliases.some(alias => alias.toLowerCase().includes(text))
      )
    : rows.value;
});
const current = computed(
  () => rows.value.find(row => row.env === selected.value) ?? null
);
const cluster = computed(
  () => form.value.clusters.find(item => item.env === selected.value) ?? null
);
const bindings = computed(() => cluster.value?.bindings ?? []);
/** 还没绑定到这个集群、已启用的日志源 */
const unbound = computed(() =>
  form.value.sources.filter(
    item =>
      item.enabled &&
      item.id &&
      !bindings.value.some(binding => binding.source === item.id)
  )
);
/** 同类型的连接多于一个时，表格里才显示连接一列 */
const showConnection = computed(() => {
  const counts: Record<string, number> = {};
  for (const item of form.value.connections)
    counts[item.type] = (counts[item.type] ?? 0) + 1;
  return Object.values(counts).some(count => count > 1);
});
const note = computed({
  get: () => cluster.value?.note ?? "",
  set: (value: string) => {
    ensureCluster().note = value;
  }
});

// 没选中或选中的集群不在了：选第一个
watch(
  rows,
  list => {
    if (!list.some(row => row.env === selected.value))
      selected.value = list[0]?.env ?? "";
  },
  { immediate: true }
);

/** 选中的集群还没有配置时先建一个空的（没有说明也没有绑定的集群不会保存） */
function ensureCluster() {
  const clusters = form.value.clusters;
  if (!clusters.some(item => item.env === selected.value))
    clusters.push(emptyCluster(selected.value));
  // 从响应式数组里取回来再改，修改才会被追踪
  return clusters.find(item => item.env === selected.value);
}

const sourceName = (id: string) => {
  const item = form.value.sources.find(row => row.id === id);
  return item ? item.name || item.id : `${id}（不存在）`;
};

function connectionText(binding: BindingForm) {
  const conn = bindingConnection(form.value, binding);
  if (!conn) return "—";
  return binding.connection
    ? `${conn.name || conn.id}（${conn.id}）`
    : `同日志源（${conn.id}）`;
}

function addBinding(sourceId = "") {
  if (!form.value.sources.length) {
    message("先在「日志源」里添加日志源", { type: "warning" });
    return;
  }
  const source = form.value.sources.find(item => item.id === sourceId);
  dialog.binding = source
    ? defaultBinding(
        source,
        selected.value,
        props.scans[source.connection],
        current.value?.aliases ?? []
      )
    : bindingForm({});
  dialog.creating = true;
  dialog.visible = true;
}

function editBinding(binding: BindingForm) {
  dialog.binding = binding;
  dialog.creating = false;
  dialog.visible = true;
}

function onSave(binding: BindingForm) {
  Object.assign(form.value, upsertBinding(form.value, selected.value, binding));
  message(
    `已保存 ${selected.value} 的绑定 ${binding.source}，点底部的「保存」后生效`,
    {
      type: "success"
    }
  );
}

async function deleteBinding(binding: BindingForm) {
  try {
    await ElMessageBox.confirm(
      `删除 ${selected.value} 的绑定「${sourceName(binding.source)}」？点底部的「保存」后生效`,
      "删除绑定",
      { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" }
    );
  } catch {
    return;
  }
  Object.assign(
    form.value,
    removeBinding(form.value, selected.value, binding.rowId)
  );
}

async function deleteCluster() {
  const env = selected.value;
  try {
    await ElMessageBox.confirm(
      `删除集群 ${env} 的说明和 ${bindings.value.length} 个绑定？点底部的「保存」后生效`,
      "删除集群配置",
      { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" }
    );
  } catch {
    return;
  }
  Object.assign(form.value, removeCluster(form.value, env));
}

function openPreview(binding: BindingForm, target: LogSourcesForm) {
  preview.form = target;
  preview.source = binding.source;
  preview.visible = true;
}

/** 绑定对话框里点「预览」：用对话框里还没确定的内容 */
function previewDraft(binding: BindingForm) {
  openPreview(binding, upsertBinding(form.value, selected.value, binding));
}
</script>

<template>
  <el-card shadow="never" class="log-card">
    <template #header><strong>集群</strong></template>
    <p class="card-note">
      每个集群的日志在哪：给集群绑定日志源，填这个集群的表 /
      索引和区分集群的过滤条件。AI
      在某个集群里提问时只能查这个集群绑定了的日志源，查询里一定带上这里的表 /
      索引和过滤条件。集群列表来自 KubeDoor 已登记的集群（<router-link
        :to="AGENT_PAGE"
        class="link"
        >Agent 管理</router-link
      >）。
    </p>
    <el-empty
      v-if="!rows.length"
      :image-size="60"
      description="还没有集群：在 Agent 管理页安装 Agent 后会出现在这里"
    />
    <div v-else class="cluster-layout">
      <div class="cluster-side">
        <el-input
          v-if="rows.length > 8"
          v-model="filter"
          size="small"
          clearable
          placeholder="按集群名、别名筛选"
          class="cluster-filter"
        />
        <div class="cluster-list">
          <div
            v-for="row in shownRows"
            :key="row.env"
            :class="['cluster-item', { active: row.env === selected }]"
            @click="selected = row.env"
          >
            <span
              :class="['dot', row.online ? 'online' : 'offline']"
              :title="
                row.registered ? (row.online ? '在线' : '离线') : '未登记'
              "
            />
            <span class="cluster-name" :title="row.env">{{ row.env }}</span>
            <el-tag
              v-if="!row.registered"
              size="small"
              type="warning"
              effect="plain"
            >
              未登记
            </el-tag>
            <span
              :class="['cluster-count', { empty: !row.bindings }]"
              :title="`${row.bindings} 个绑定`"
            >
              {{ row.bindings }}
            </span>
          </div>
        </div>
      </div>

      <div v-if="current" class="cluster-detail">
        <div class="detail-head">
          <strong class="detail-name">{{ current.env }}</strong>
          <el-tag
            size="small"
            effect="plain"
            :type="
              !current.registered
                ? 'warning'
                : current.online
                  ? 'success'
                  : 'info'
            "
          >
            {{
              !current.registered ? "未登记" : current.online ? "在线" : "离线"
            }}
          </el-tag>
          <span v-if="current.aliases.length" class="hint inline">
            别名：{{ current.aliases.join("、") }}
          </span>
          <el-button
            v-if="!current.registered"
            link
            type="danger"
            class="head-action"
            @click="deleteCluster"
          >
            删除这个集群的配置
          </el-button>
        </div>
        <el-alert
          v-if="!current.registered"
          type="warning"
          :closable="false"
          show-icon
          title="KubeDoor 里已经没有这个集群（可能改名或删除了）：AI 不会用到这里的配置，确认不要了可以删除"
          class="block-alert"
        />

        <div class="section-title">集群说明</div>
        <el-input
          v-model="note"
          type="textarea"
          :rows="3"
          :maxlength="CLUSTER_NOTE_MAX_LENGTH"
          show-word-limit
          placeholder="给 AI 看：这个集群的日志有什么特别的，例如「测试环境没有 nginx 日志」「入口流量先经过外部 nginx」"
        />

        <div class="section-title section-actions">
          <span>日志源绑定（{{ bindings.length }}）</span>
          <el-button
            size="small"
            type="primary"
            plain
            :disabled="bindings.length >= MAX_BINDINGS"
            @click="addBinding()"
          >
            添加绑定
          </el-button>
        </div>
        <el-table
          :data="bindings"
          row-key="rowId"
          border
          size="small"
          empty-text="还没有绑定：AI 在这个集群里查不到任何历史日志"
        >
          <el-table-column label="日志源" min-width="170">
            <template #default="{ row }">
              <div>{{ sourceName(row.source) }}</div>
              <code>{{ row.source }}</code>
            </template>
          </el-table-column>
          <el-table-column
            v-if="showConnection"
            label="连接"
            min-width="150"
            show-overflow-tooltip
          >
            <template #default="{ row }">{{ connectionText(row) }}</template>
          </el-table-column>
          <el-table-column
            label="表 / 索引"
            min-width="190"
            show-overflow-tooltip
          >
            <template #default="{ row }">
              <code>{{ row.target }}</code>
            </template>
          </el-table-column>
          <el-table-column
            label="过滤条件"
            min-width="200"
            show-overflow-tooltip
          >
            <template #default="{ row }">
              <span v-if="row.conditions.length" class="mono">
                {{ conditionsText(row.conditions) }}
              </span>
              <span v-else class="muted">整张表 / 整个索引</span>
            </template>
          </el-table-column>
          <el-table-column
            prop="note"
            label="说明"
            min-width="160"
            show-overflow-tooltip
          />
          <el-table-column label="操作" width="150" fixed="right">
            <template #default="{ row }">
              <el-button link type="primary" @click="editBinding(row)">
                编辑
              </el-button>
              <el-button link type="primary" @click="openPreview(row, form)">
                预览
              </el-button>
              <el-button link type="danger" @click="deleteBinding(row)">
                删除
              </el-button>
            </template>
          </el-table-column>
        </el-table>
        <div v-if="unbound.length" class="unbound">
          <span class="hint inline">还没绑定到这个集群的日志源：</span>
          <el-button
            v-for="item in unbound"
            :key="item.rowId"
            size="small"
            link
            type="primary"
            @click="addBinding(item.id)"
          >
            + {{ item.name || item.id }}
          </el-button>
        </div>
      </div>
    </div>

    <BindingDialog
      v-model:visible="dialog.visible"
      :binding="dialog.binding"
      :creating="dialog.creating"
      :env="selected"
      :aliases="current?.aliases ?? []"
      :form="form"
      :scans="scans"
      @save="onSave"
      @preview="previewDraft"
    />
    <PreviewDialog
      v-model:visible="preview.visible"
      :form="preview.form"
      :env="selected"
      :source="preview.source"
    />
  </el-card>
</template>

<style scoped lang="scss">
.cluster-layout {
  display: grid;
  grid-template-columns: 240px minmax(0, 1fr);
  gap: 16px;

  @media (width <= 900px) {
    grid-template-columns: minmax(0, 1fr);
  }
}

.cluster-side {
  min-width: 0;
}

.cluster-filter {
  margin-bottom: 8px;
}

.cluster-list {
  max-height: 560px;
  overflow: auto;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 4px;
}

.cluster-item {
  display: flex;
  gap: 8px;
  align-items: center;
  padding: 8px 10px;
  font-size: 13px;
  cursor: pointer;
  border-bottom: 1px solid var(--el-border-color-lighter);

  &:last-child {
    border-bottom: 0;
  }

  &:hover {
    background: var(--el-fill-color-light);
  }

  &.active {
    color: var(--el-color-primary);
    background: var(--el-color-primary-light-9);
  }
}

.dot {
  flex: 0 0 8px;
  width: 8px;
  height: 8px;
  border-radius: 50%;

  &.online {
    background: var(--el-color-success);
  }

  &.offline {
    background: var(--el-border-color);
  }
}

.cluster-name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.cluster-count {
  min-width: 20px;
  padding: 0 6px;
  font-size: 12px;
  line-height: 18px;
  color: var(--el-color-primary);
  text-align: center;
  background: var(--el-color-primary-light-9);
  border-radius: 9px;

  &.empty {
    color: var(--el-text-color-secondary);
    background: var(--el-fill-color);
  }
}

.cluster-detail {
  min-width: 0;
}

.detail-head {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}

.detail-name {
  font-size: 16px;
}

.head-action {
  margin-left: auto;
}

.unbound {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 8px;
  align-items: center;
  margin-top: 8px;

  .el-button + .el-button {
    margin-left: 0;
  }
}
</style>
