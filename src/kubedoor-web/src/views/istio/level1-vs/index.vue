<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessageBox } from "element-plus";
import { Plus, Refresh, Setting, Warning } from "@element-plus/icons-vue";
import { message } from "@/utils/message";
import { useClusterPermission } from "@/utils/permission";
import {
  type ApplyItem,
  type IstioGroup,
  type IstioGroupList,
  type IstioMatrix,
  type MatrixRow,
  collectIstio,
  getIstioGroups,
  getIstioMatrix,
  removeIstioVSCluster,
  resetIstioVS
} from "@/api/istio";
import GroupDialog from "./components/GroupDialog.vue";
import CompareDialog from "./components/CompareDialog.vue";
import ImportDialog from "./components/ImportDialog.vue";
import ApplyDialog from "./components/ApplyDialog.vue";
import HostsDialog from "./components/HostsDialog.vue";
import VSDialog from "./components/VSDialog.vue";
import {
  COVERAGE_LABEL,
  ENV_TYPE_LABEL,
  ENV_TYPE_TAG,
  STATUS_META,
  formatTime
} from "./utils";

defineOptions({ name: "Level1VirtualService" });

const route = useRoute();
const router = useRouter();
const { isAdmin } = useClusterPermission();

const GROUP_KEY = "kubedoor-istio-group";
const groupList = ref<IstioGroupList>({ groups: [] });
const groupId = ref<number | null>(null);
const matrix = ref<IstioMatrix | null>(null);
const loading = ref(false);
const collecting = ref(false);
const keyword = ref("");
const filter = ref("all");
const shownClusters = ref<string[]>([]);
/** 勾选的行只记行标识,刷新数据后从当前数据里取(表格保留勾选,但给出的是刷新前的旧行对象) */
const selectedKeys = ref<string[]>([]);

const FILTERS = [
  { value: "all", label: "全部" },
  { value: "problem", label: "需要处理" },
  { value: "unmanaged", label: "未纳管" },
  { value: "inconsistent", label: "各集群规则不一致" },
  { value: "lint", label: "规范问题" },
  { value: "test_only", label: "只在测试环境" },
  { value: "prod_only", label: "只在生产环境" }
];
const ACTION_STATUSES = ["pending", "diff", "missing", "incomplete", "unbound"];

const group = computed(() => matrix.value?.group ?? null);
const clusters = computed(() => group.value?.clusters ?? []);
const visibleClusters = computed(() =>
  clusters.value.filter(c => shownClusters.value.includes(c.k8s_name))
);
const rows = computed(() => matrix.value?.rows ?? []);
const gatewayOptions = computed(() =>
  Array.from(new Set(rows.value.flatMap(r => r.gateways))).sort()
);

function needsAction(row: MatrixRow) {
  if (row.vs_id === null) return row.versions.length > 1;
  return Object.values(row.cells).some(c => ACTION_STATUSES.includes(c.status));
}

const filteredRows = computed(() => {
  const kw = keyword.value.trim().toLowerCase();
  return rows.value.filter(row => {
    if (filter.value === "problem" && !needsAction(row)) return false;
    if (filter.value === "unmanaged" && row.vs_id !== null) return false;
    if (filter.value === "inconsistent" && row.versions.length <= 1)
      return false;
    if (filter.value === "lint" && !row.lint.length) return false;
    if (
      ["test_only", "prod_only"].includes(filter.value) &&
      row.coverage !== filter.value
    ) {
      return false;
    }
    if (!kw) return true;
    if (`${row.namespace}/${row.name}`.toLowerCase().includes(kw)) return true;
    return Object.values(row.cells).some(c =>
      c.hosts.some(h => h.toLowerCase().includes(kw))
    );
  });
});

const selectedRows = computed(() => {
  const keys = new Set(selectedKeys.value);
  return rows.value.filter(row => keys.has(rowKey(row)));
});

function onSelectionChange(selected: MatrixRow[]) {
  selectedKeys.value = selected.map(rowKey);
}

const summary = computed(() => ({
  total: rows.value.length,
  managed: rows.value.filter(r => r.vs_id !== null).length,
  problem: rows.value.filter(needsAction).length,
  lint: rows.value.filter(r => r.lint.length).length
}));

// ---------------------------------------------------------------- 加载
async function loadGroups(prefer?: number | null) {
  const res = await getIstioGroups();
  groupList.value = res.data;
  const ids = res.data.groups.map(g => g.id);
  const wanted = [
    prefer,
    Number(route.query.group),
    Number(localStorage.getItem(GROUP_KEY))
  ];
  groupId.value = wanted.find(id => id && ids.includes(id)) ?? ids[0] ?? null;
}

async function loadMatrix() {
  if (groupId.value === null) {
    matrix.value = null;
    return;
  }
  loading.value = true;
  try {
    const res = await getIstioMatrix(groupId.value);
    const previous = new Set(
      matrix.value?.group.clusters.map(c => c.k8s_name) ?? []
    );
    matrix.value = res.data;
    const names = res.data.group.clusters.map(c => c.k8s_name);
    // 新加入组的集群默认显示;已隐藏的保持隐藏
    shownClusters.value = names.filter(
      name => !previous.has(name) || shownClusters.value.includes(name)
    );
  } finally {
    loading.value = false;
  }
}

async function selectGroup(id: number | null) {
  groupId.value = id;
  if (id !== null) {
    localStorage.setItem(GROUP_KEY, String(id));
    router.replace({ query: { ...route.query, group: String(id) } });
  }
  matrix.value = null;
  await loadMatrix();
}

async function onGroupSaved(id: number | null) {
  await loadGroups(id);
  await selectGroup(groupId.value);
}

async function collect() {
  if (groupId.value === null) return;
  collecting.value = true;
  try {
    const res = await collectIstio(groupId.value);
    const failed = res.data.filter(r => !r.success);
    const ok = res.data.filter(r => r.success);
    if (failed.length) {
      message(
        `已采集 ${ok.length} 个集群;失败:${failed.map(f => `${f.k8s_name}(${f.error})`).join(";")}`,
        { type: "warning", duration: 8000 }
      );
    } else {
      message(
        `已采集 ${ok.map(r => `${r.k8s_name} ${r.count} 个`).join(",")}`,
        {
          type: "success"
        }
      );
    }
    await loadMatrix();
  } finally {
    collecting.value = false;
  }
}

// ---------------------------------------------------------------- 对话框
const groupDialog = ref(false);
const vsDialog = ref(false);

const compare = ref({
  visible: false,
  row: null as MatrixRow | null,
  left: "",
  right: ""
});
function openCompare(row: MatrixRow, k8s?: string) {
  const present = clusters.value
    .filter(c => row.cells[c.k8s_name]?.in_cluster)
    .map(c => c.k8s_name);
  let left: string;
  let right: string;
  if (row.vs_id !== null) {
    const target = k8s ?? present[0] ?? clusters.value[0]?.k8s_name;
    left = row.cells[target]?.bound ? `standard@${target}` : "standard";
    right = `cluster@${target}`;
  } else {
    const base = row.versions[0]?.clusters[0] ?? present[0];
    const other =
      k8s && k8s !== base
        ? k8s
        : (row.versions[1]?.clusters[0] ??
          present.find(p => p !== base) ??
          base);
    left = `cluster@${base}`;
    right = `cluster@${other}`;
  }
  compare.value = { visible: true, row, left, right };
}

function onCompareApply(k8s: string) {
  const vsId = compare.value.row?.vs_id;
  if (vsId != null) openApply([{ vs_id: vsId, k8s_name: k8s }]);
}

function onCompareImport(base: string) {
  const row = compare.value.row;
  compare.value.visible = false;
  if (row) openImport([row], base);
}

const importState = ref({ visible: false, rows: [] as MatrixRow[], base: "" });
function openImport(rowsToImport: MatrixRow[], base = "") {
  importState.value = { visible: true, rows: rowsToImport, base };
}

const applyState = ref({ visible: false, items: [] as ApplyItem[] });
function openApply(items: ApplyItem[]) {
  if (!items.length) {
    message("所选 VirtualService 在各集群都已一致,没有需要下发的", {
      type: "info"
    });
    return;
  }
  applyState.value = { visible: true, items };
}

/** 所选行里纳入了、且不一致的集群 */
function applySelected() {
  const items: ApplyItem[] = [];
  for (const row of selectedRows.value) {
    if (row.vs_id === null) continue;
    for (const c of visibleClusters.value) {
      const cell = row.cells[c.k8s_name];
      if (cell?.bound && cell.status !== "synced")
        items.push({ vs_id: row.vs_id, k8s_name: c.k8s_name });
    }
  }
  openApply(items);
}

const hostsState = ref({
  visible: false,
  vsId: null as number | null,
  k8sName: ""
});
function openHosts(row: MatrixRow, k8s: string) {
  hostsState.value = { visible: true, vsId: row.vs_id, k8sName: k8s };
}

async function resetFrom(row: MatrixRow, k8s: string) {
  if (row.vs_id === null) return;
  await ElMessageBox.confirm(
    `用 ${k8s} 的现状替换 ${row.name} 组标准的规则、默认路由和 gateways?各集群 hosts 不变,其他集群规则里的域名也保持各自的;其他集群下次下发时会改成这个版本。`,
    "以集群现状更新组标准",
    { type: "warning" }
  );
  await resetIstioVS(row.vs_id, k8s);
  message("组标准已更新,下发后生效", { type: "success" });
  await loadMatrix();
}

async function unbind(row: MatrixRow, k8s: string) {
  if (row.vs_id === null) return;
  await ElMessageBox.confirm(
    `${row.name} 不再部署到 ${k8s}?只删除花折里的部署关系和该集群的 hosts,集群里的 VirtualService 不受影响(如需删除请到集群处理)。`,
    "不再部署到该集群",
    { type: "warning" }
  );
  await removeIstioVSCluster(row.vs_id, k8s);
  message("已移出", { type: "success" });
  await loadMatrix();
}

function onCellCommand(row: MatrixRow, k8s: string, command: string) {
  if (command === "compare") openCompare(row, k8s);
  else if (command === "hosts") openHosts(row, k8s);
  else if (command === "apply" && row.vs_id !== null)
    openApply([{ vs_id: row.vs_id, k8s_name: k8s }]);
  else if (command === "reset") resetFrom(row, k8s);
  else if (command === "unbind") unbind(row, k8s);
  else if (command === "import") openImport([row], k8s);
}

function goDetail(row: MatrixRow) {
  if (row.vs_id !== null) {
    router.push({
      path: `/istio/level1-vs/detail/${row.vs_id}`,
      query: { group: String(groupId.value) }
    });
  }
}

function onVSSaved(vsId: number) {
  router.push({
    path: `/istio/level1-vs/detail/${vsId}`,
    query: { group: String(groupId.value) }
  });
}

function rowKey(row: MatrixRow) {
  return `${row.namespace}/${row.name}`;
}

onMounted(async () => {
  await loadGroups();
  await selectGroup(groupId.value);
});
</script>

<template>
  <div class="istio-page">
    <div class="toolbar">
      <el-select
        :model-value="groupId ?? undefined"
        placeholder="选择 Istio 组"
        style="width: 200px"
        @change="selectGroup"
      >
        <el-option
          v-for="g in groupList.groups"
          :key="g.id"
          :label="g.name"
          :value="g.id"
        />
      </el-select>
      <el-button v-if="isAdmin" :icon="Setting" @click="groupDialog = true"
        >组管理</el-button
      >
      <el-input
        v-model="keyword"
        placeholder="VS 名称 / host"
        clearable
        style="width: 200px"
      />
      <el-select v-model="filter" style="width: 160px">
        <el-option
          v-for="f in FILTERS"
          :key="f.value"
          :label="f.label"
          :value="f.value"
        />
      </el-select>
      <el-select
        v-model="shownClusters"
        multiple
        collapse-tags
        collapse-tags-tooltip
        placeholder="显示的集群"
        style="width: 240px"
      >
        <el-option
          v-for="c in clusters"
          :key="c.k8s_name"
          :label="`${c.k8s_name}(${ENV_TYPE_LABEL[c.env_type]})`"
          :value="c.k8s_name"
        />
      </el-select>
      <el-button :icon="Refresh" :loading="loading" @click="loadMatrix"
        >刷新</el-button
      >
      <template v-if="isAdmin && group">
        <el-button type="success" :loading="collecting" @click="collect"
          >刷新现状</el-button
        >
        <el-button type="primary" :icon="Plus" @click="vsDialog = true"
          >新建 VS</el-button
        >
        <el-button
          :disabled="!selectedRows.some(r => r.vs_id === null)"
          @click="openImport(selectedRows.filter(r => r.vs_id === null))"
        >
          纳管所选
        </el-button>
        <el-button
          type="warning"
          plain
          :disabled="!selectedRows.some(r => r.vs_id !== null)"
          @click="applySelected"
        >
          下发所选
        </el-button>
      </template>
    </div>

    <el-empty
      v-if="!groupList.groups.length"
      :description="
        isAdmin
          ? '还没有 Istio 组:点「组管理」把一套业务的集群建成一个组'
          : '没有可以查看的 Istio 组'
      "
    >
      <el-button v-if="isAdmin" type="primary" @click="groupDialog = true"
        >创建 Istio 组</el-button
      >
    </el-empty>

    <template v-else-if="group">
      <div class="cluster-strip">
        <div v-for="c in clusters" :key="c.k8s_name" class="cluster-chip">
          <el-tag size="small" :type="ENV_TYPE_TAG[c.env_type]">{{
            ENV_TYPE_LABEL[c.env_type]
          }}</el-tag>
          <b>{{ c.k8s_name }}</b>
          <span v-if="c.remark" class="sub">{{ c.remark }}</span>
          <span class="sub">
            {{
              c.collected_at
                ? `现状 ${formatTime(c.collected_at)} · ${c.vs_count} 个`
                : "未采集现状"
            }}
          </span>
        </div>
      </div>
      <div class="summary">
        共 {{ summary.total }} 个 VirtualService,已纳管
        {{ summary.managed }} 个,需要处理 {{ summary.problem }} 个,规范问题
        {{ summary.lint }} 个
        <span class="legend">
          <el-tooltip
            v-for="(meta, status) in STATUS_META"
            :key="status"
            :content="meta.desc"
            placement="top"
          >
            <el-tag
              v-if="status !== 'none'"
              size="small"
              :type="meta.type"
              effect="plain"
              >{{ meta.label }}</el-tag
            >
          </el-tooltip>
        </span>
      </div>

      <el-table
        v-loading="loading"
        :data="filteredRows"
        :row-key="rowKey"
        border
        size="small"
        class="matrix"
        @selection-change="onSelectionChange"
      >
        <el-table-column
          v-if="isAdmin"
          type="selection"
          width="36"
          fixed
          reserve-selection
        />
        <el-table-column label="VirtualService" min-width="230" fixed>
          <template #default="{ row }">
            <div class="vs-name">
              <el-link
                v-if="row.vs_id !== null"
                type="primary"
                @click="goDetail(row)"
                >{{ row.name }}</el-link
              >
              <span v-else>{{ row.name }}</span>
              <el-tag v-if="row.vs_id === null" size="small" type="info"
                >未纳管</el-tag
              >
              <el-tooltip v-if="row.lint.length" placement="top">
                <template #content>
                  <div v-for="item in row.lint" :key="item">{{ item }}</div>
                </template>
                <el-icon class="lint"><Warning /></el-icon>
              </el-tooltip>
            </div>
            <div class="sub">
              {{ row.namespace }} ·
              {{ row.gateways.join(", ") || "无 gateway" }} ·
              {{ row.route_count }} 条规则{{
                row.has_default ? " + 默认路由" : ""
              }}
            </div>
            <el-tag
              v-if="row.coverage"
              size="small"
              type="warning"
              effect="plain"
              >{{ COVERAGE_LABEL[row.coverage] }}</el-tag
            >
          </template>
        </el-table-column>
        <el-table-column label="规则版本" width="120">
          <template #default="{ row }">
            <el-tooltip
              v-for="v in row.versions"
              :key="v.label"
              :content="`${v.clusters.join('、')}${row.standard_version === v.label ? '(= 组标准)' : ''}`"
              placement="top"
            >
              <el-tag
                size="small"
                :type="
                  row.standard_version === v.label || row.versions.length === 1
                    ? 'success'
                    : 'warning'
                "
                class="mr-1"
                >{{ v.label
                }}{{ row.standard_version === v.label ? "★" : "" }}</el-tag
              >
            </el-tooltip>
          </template>
        </el-table-column>
        <el-table-column
          v-for="c in visibleClusters"
          :key="c.k8s_name"
          min-width="230"
        >
          <template #header>
            <div class="col-head">
              <span>{{ c.k8s_name }}</span>
              <el-tag size="small" :type="ENV_TYPE_TAG[c.env_type]">{{
                ENV_TYPE_LABEL[c.env_type]
              }}</el-tag>
            </div>
          </template>
          <template #default="{ row }">
            <el-dropdown
              trigger="click"
              class="cell-dropdown"
              @command="cmd => onCellCommand(row, c.k8s_name, cmd)"
            >
              <div :class="['cell', `cell-${row.cells[c.k8s_name].status}`]">
                <div class="cell-head">
                  <el-tooltip
                    v-if="
                      !['none', 'unmanaged'].includes(
                        row.cells[c.k8s_name].status
                      )
                    "
                    :content="STATUS_META[row.cells[c.k8s_name].status].desc"
                    placement="top"
                  >
                    <el-tag
                      size="small"
                      :type="STATUS_META[row.cells[c.k8s_name].status].type"
                      >{{
                        STATUS_META[row.cells[c.k8s_name].status].label
                      }}</el-tag
                    >
                  </el-tooltip>
                  <span
                    v-else-if="row.cells[c.k8s_name].status === 'none'"
                    class="sub"
                    >—</span
                  >
                  <!-- 各集群规则有分歧时才标版本,一样就不打扰 -->
                  <el-tag
                    v-if="
                      row.versions.length > 1 && row.cells[c.k8s_name].version
                    "
                    size="small"
                    effect="plain"
                    :type="
                      row.cells[c.k8s_name].version ===
                      (row.standard_version ?? row.versions[0].label)
                        ? 'success'
                        : 'warning'
                    "
                    >版本 {{ row.cells[c.k8s_name].version }}</el-tag
                  >
                </div>
                <div
                  v-for="h in row.cells[c.k8s_name].hosts.slice(0, 4)"
                  :key="h"
                  class="host"
                  :title="h"
                >
                  {{ h }}
                </div>
                <el-tooltip
                  v-if="row.cells[c.k8s_name].hosts.length > 4"
                  placement="top"
                >
                  <template #content>
                    <div
                      v-for="h in row.cells[c.k8s_name].hosts.slice(4)"
                      :key="h"
                    >
                      {{ h }}
                    </div>
                  </template>
                  <div class="sub">
                    还有 {{ row.cells[c.k8s_name].hosts.length - 4 }} 个…
                  </div>
                </el-tooltip>
              </div>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item command="compare">对比</el-dropdown-item>
                  <template v-if="isAdmin && row.vs_id !== null">
                    <el-dropdown-item
                      v-if="row.cells[c.k8s_name].bound"
                      command="hosts"
                    >
                      编辑 hosts 和域名
                    </el-dropdown-item>
                    <el-dropdown-item v-else command="hosts"
                      >部署到此集群</el-dropdown-item
                    >
                    <el-dropdown-item
                      v-if="row.cells[c.k8s_name].bound"
                      command="apply"
                    >
                      下发到此集群
                    </el-dropdown-item>
                    <el-dropdown-item
                      v-if="row.cells[c.k8s_name].in_cluster"
                      command="reset"
                      divided
                    >
                      以此集群现状更新组标准
                    </el-dropdown-item>
                    <el-dropdown-item
                      v-if="row.cells[c.k8s_name].bound"
                      command="unbind"
                    >
                      不再部署到此集群
                    </el-dropdown-item>
                  </template>
                  <el-dropdown-item
                    v-if="
                      isAdmin &&
                      row.vs_id === null &&
                      row.cells[c.k8s_name].in_cluster
                    "
                    command="import"
                  >
                    以此集群为准纳管
                  </el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="150" fixed="right" align="center">
          <template #default="{ row }">
            <el-button
              v-if="row.vs_id !== null"
              link
              type="primary"
              @click="goDetail(row)"
              >详情</el-button
            >
            <el-button link type="primary" @click="openCompare(row)"
              >对比</el-button
            >
            <el-button
              v-if="isAdmin && row.vs_id === null"
              link
              type="success"
              @click="openImport([row])"
              >纳管</el-button
            >
          </template>
        </el-table-column>
      </el-table>
    </template>

    <GroupDialog
      v-model="groupDialog"
      :groups="groupList.groups"
      :clusters="groupList.clusters ?? []"
      :current="groupId"
      @saved="onGroupSaved"
    />
    <template v-if="group">
      <CompareDialog
        v-if="compare.row"
        v-model="compare.visible"
        :group-id="group.id"
        :namespace="compare.row.namespace"
        :name="compare.row.name"
        :clusters="clusters"
        :vs-id="compare.row.vs_id"
        :bound-clusters="
          clusters
            .filter(c => compare.row?.cells[c.k8s_name]?.bound)
            .map(c => c.k8s_name)
        "
        :is-admin="isAdmin"
        :left="compare.left"
        :right="compare.right"
        @changed="loadMatrix"
        @apply="onCompareApply"
        @import="onCompareImport"
      />
      <ImportDialog
        v-model="importState.visible"
        :group-id="group.id"
        :rows="importState.rows"
        :clusters="clusters"
        :preset-base="importState.base"
        @done="loadMatrix"
      />
      <ApplyDialog
        v-model="applyState.visible"
        :items="applyState.items"
        :clusters="clusters"
        @done="loadMatrix"
      />
      <HostsDialog
        v-model="hostsState.visible"
        :vs-id="hostsState.vsId"
        :k8s-name="hostsState.k8sName"
        @saved="loadMatrix"
      />
      <VSDialog
        v-model="vsDialog"
        :group-id="group.id"
        :clusters="clusters"
        :vs="null"
        :gateway-options="gatewayOptions"
        @saved="onVSSaved"
      />
    </template>
  </div>
</template>

<style scoped>
.istio-page {
  padding: 4px;
}

.toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  margin-bottom: 10px;
}

.toolbar .el-button + .el-button {
  margin-left: 0;
}

.cluster-strip {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 8px;
}

.cluster-chip {
  display: flex;
  gap: 6px;
  align-items: center;
  padding: 4px 10px;
  font-size: 13px;
  background: var(--el-fill-color-light);
  border-radius: 4px;
}

.summary {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  margin-bottom: 8px;
  font-size: 13px;
  color: var(--el-text-color-regular);
}

.legend {
  display: inline-flex;
  gap: 4px;
  margin-left: auto;
}

.sub {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.vs-name {
  display: flex;
  gap: 6px;
  align-items: center;
  font-weight: 600;
}

.lint {
  color: var(--el-color-warning);
  cursor: help;
}

.col-head {
  display: flex;
  gap: 6px;
  align-items: center;
}

.cell-dropdown {
  display: block;
  width: 100%;
}

.cell {
  min-height: 36px;
  padding: 2px 4px;
  cursor: pointer;
  border-radius: 4px;
}

.cell:hover {
  background: var(--el-fill-color);
}

.cell-head {
  display: flex;
  gap: 6px;
  align-items: center;
  margin-bottom: 2px;
}

.host {
  overflow: hidden;
  font-family: Consolas, Monaco, monospace;
  font-size: 12px;
  line-height: 18px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.cell-none .host,
.cell-unbound .host,
.cell-unmanaged .host {
  color: var(--el-text-color-secondary);
}
</style>
