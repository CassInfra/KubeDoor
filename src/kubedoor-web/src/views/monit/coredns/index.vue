<template>
  <div class="coredns-manager">
    <!-- 左边的筛选条件放不下时整体换行;右上角的按钮固定在第一行,不会被挤走 -->
    <div class="search-section">
      <el-form inline class="query-form" @submit.prevent>
        <div class="query-form__left">
          <el-form-item label="K8S">
            <el-select
              v-model="env"
              placeholder="请选择K8S环境"
              class="!w-[220px]"
              filterable
              @change="handleEnvChange"
            >
              <el-option
                v-for="item in envOptions"
                :key="item"
                :label="item"
                :value="item"
              />
            </el-select>
            <ClusterPermTag :env="env" />
          </el-form-item>
          <el-form-item>
            <el-button
              type="primary"
              :icon="Refresh"
              :loading="loading"
              @click="handleRefresh"
            >
              刷新
            </el-button>
          </el-form-item>
          <el-form-item>
            <el-input
              v-model="keyword"
              placeholder="搜索 IP / 域名 / 备注"
              clearable
              :prefix-icon="Search"
              style="width: 200px"
            />
          </el-form-item>
          <el-form-item>
            <el-radio-group v-model="statusFilter" class="filter-group">
              <el-radio-button value="all">全部</el-radio-button>
              <el-radio-button value="enabled">启用</el-radio-button>
              <el-radio-button value="disabled">停用</el-radio-button>
              <el-radio-button value="comment">注释</el-radio-button>
              <el-radio-button value="changed">
                未保存<span v-if="dirtyRows.length"
                  >({{ dirtyRows.length }})</span
                >
              </el-radio-button>
            </el-radio-group>
          </el-form-item>
        </div>
        <div class="query-form__right">
          <el-button :icon="Aim" :disabled="!env" @click="openNetTools('dns')"
            >解析测试</el-button
          >
          <el-tooltip
            :disabled="!env || canWrite(env)"
            content="端口测试会从集群内向任意地址发起连接,需要这个集群的读写权限"
            placement="bottom"
          >
            <span>
              <el-button
                :icon="Connection"
                :disabled="!env || !canWrite(env)"
                @click="openNetTools('tcp')"
                >端口测试</el-button
              >
            </span>
          </el-tooltip>
          <el-button
            type="primary"
            :icon="EditPen"
            :disabled="!configmap"
            @click="advancedVisible = true"
          >
            高级编辑
          </el-button>
        </div>
      </el-form>
    </div>

    <div class="mt-2">
      <el-card v-loading="loading" element-loading-text="加载中...">
        <el-result v-if="loadError" icon="warning" :title="loadError">
          <template #extra>
            <el-button type="primary" @click="load()">重试</el-button>
          </template>
        </el-result>

        <template v-else-if="model">
          <div class="info-bar">
            <div class="info-bar__group">
              <span class="info-item"
                >ConfigMap <b>kube-system/coredns</b></span
              >
              <span class="info-item info-muted"
                >resourceVersion {{ resourceVersion }}</span
              >
              <el-radio-group
                v-if="blocks.length > 1"
                v-model="blockIndex"
                size="small"
              >
                <el-radio-button
                  v-for="item in blocks"
                  :key="item.index"
                  :value="item.index"
                >
                  {{ item.server.label }}
                </el-radio-button>
              </el-radio-group>
              <span v-else-if="block" class="info-item"
                >server 块 <b>{{ block.server.label }}</b></span
              >
              <span v-if="block?.args.length" class="info-item info-muted">
                hosts 文件 {{ block.args[0] }}(表格只管理写在 Corefile 里的记录)
              </span>
              <span class="info-item">
                记录 <b>{{ stats.entries }}</b>
                <span class="info-muted"
                  >(启用 {{ stats.enabled }} · 停用 {{ stats.disabled }})</span
                >
                · 注释 <b>{{ stats.comments }}</b>
              </span>
            </div>
            <div class="info-bar__group">
              <template v-if="dirtyRows.length">
                <el-tag type="warning" effect="light"
                  >{{ dirtyRows.length }} 处修改未保存</el-tag
                >
                <el-button size="small" :disabled="busy" @click="discardAll"
                  >放弃修改</el-button
                >
                <el-button
                  size="small"
                  type="primary"
                  :disabled="!canSave || busy"
                  @click="saveAll"
                >
                  全部保存
                </el-button>
              </template>
              <el-button
                size="small"
                type="success"
                plain
                :icon="Plus"
                :disabled="!canEdit"
                @click="appendRow('entry')"
              >
                添加记录
              </el-button>
              <el-button
                size="small"
                plain
                :icon="ChatLineSquare"
                :disabled="!canEdit"
                @click="appendRow('comment')"
              >
                添加注释
              </el-button>
            </div>
          </div>

          <el-alert
            v-for="item in alerts"
            :key="item.title"
            :type="item.type"
            :title="item.title"
            :closable="false"
            show-icon
            class="block-alert"
          />

          <el-table
            :data="visibleRows"
            row-key="id"
            border
            size="small"
            class="coredns-table"
            :row-class-name="rowClass"
            :span-method="spanMethod"
            :empty-text="emptyText"
          >
            <el-table-column label="行号" width="72" align="center">
              <template #default="{ row }">
                <div class="line-cell">
                  <span v-if="row.origin" class="line-no">{{
                    row.origin.line + 1
                  }}</span>
                  <el-tag
                    v-if="!row.origin"
                    size="small"
                    type="success"
                    effect="dark"
                    >新增</el-tag
                  >
                  <el-tag
                    v-else-if="rowChanged(row)"
                    size="small"
                    type="warning"
                    >已修改</el-tag
                  >
                </div>
              </template>
            </el-table-column>

            <el-table-column width="76" align="center">
              <template #header>
                <el-tooltip
                  content="关闭 = 在行首加 # 把这条记录注释掉,不删除,随时可以再启用"
                  placement="top"
                >
                  <span class="header-tip">启用</span>
                </el-tooltip>
              </template>
              <template #default="{ row }">
                <el-switch
                  v-if="row.kind === 'entry'"
                  v-model="row.enabled"
                  size="small"
                  :disabled="!canEdit"
                />
                <el-tag v-else size="small" type="info" effect="plain"
                  >注释</el-tag
                >
              </template>
            </el-table-column>

            <!-- 注释行把 IP / 域名 / 备注三列合成一格(spanMethod) -->
            <el-table-column label="IP" width="190">
              <template #default="{ row }">
                <div :data-focus="row.id">
                  <el-input
                    v-if="row.kind === 'comment'"
                    v-model="row.text"
                    size="small"
                    :disabled="!canEdit"
                    placeholder="整行注释,例如 ── Redis ──(只能是注释)"
                    class="comment-input"
                    :class="{ 'is-invalid': errorOf(row, 'text') }"
                    @blur="touch(row, 'text')"
                  >
                    <template #prefix><span class="hash">#</span></template>
                    <template v-if="errorOf(row, 'text')" #suffix>
                      <el-tooltip
                        :content="errorOf(row, 'text')"
                        placement="top"
                      >
                        <el-icon class="issue-icon is-error"
                          ><WarningFilled
                        /></el-icon>
                      </el-tooltip>
                    </template>
                  </el-input>
                  <el-input
                    v-else
                    v-model="row.ip"
                    size="small"
                    :disabled="!canEdit"
                    placeholder="10.0.0.1"
                    class="mono"
                    :class="{ 'is-invalid': errorOf(row, 'ip') }"
                    @blur="touch(row, 'ip')"
                  >
                    <template v-if="errorOf(row, 'ip')" #suffix>
                      <el-tooltip :content="errorOf(row, 'ip')" placement="top">
                        <el-icon class="issue-icon is-error"
                          ><WarningFilled
                        /></el-icon>
                      </el-tooltip>
                    </template>
                  </el-input>
                </div>
              </template>
            </el-table-column>

            <el-table-column label="域名" min-width="300">
              <template #default="{ row }">
                <el-input
                  v-model="row.names"
                  size="small"
                  :disabled="!canEdit"
                  placeholder="a.example.com(多个用空格分隔)"
                  class="mono"
                  :class="{
                    'is-invalid': errorOf(row, 'names'),
                    'is-warning':
                      !errorOf(row, 'names') && issueOf(row).warnings.length
                  }"
                  @blur="touch(row, 'names')"
                >
                  <template
                    v-if="errorOf(row, 'names') || issueOf(row).warnings.length"
                    #suffix
                  >
                    <el-tooltip placement="top">
                      <template #content>
                        <div v-if="errorOf(row, 'names')">
                          {{ errorOf(row, "names") }}
                        </div>
                        <div v-for="text in issueOf(row).warnings" :key="text">
                          {{ text }}
                        </div>
                      </template>
                      <el-icon
                        :class="[
                          'issue-icon',
                          errorOf(row, 'names') ? 'is-error' : 'is-warning'
                        ]"
                      >
                        <WarningFilled />
                      </el-icon>
                    </el-tooltip>
                  </template>
                </el-input>
              </template>
            </el-table-column>

            <el-table-column label="备注" min-width="240">
              <template #default="{ row }">
                <el-input
                  v-model="row.remark"
                  size="small"
                  :disabled="!canEdit"
                  placeholder="可选,写在行尾的 # 后面"
                  :class="{ 'is-invalid': errorOf(row, 'remark') }"
                  @blur="touch(row, 'remark')"
                />
              </template>
            </el-table-column>

            <el-table-column label="操作" width="300" fixed="right">
              <template #default="{ row }">
                <div class="ops">
                  <el-button
                    link
                    type="primary"
                    size="small"
                    :disabled="!canEdit || busy || !rowChanged(row)"
                    :loading="savingId === row.id"
                    @click="saveRow(row)"
                  >
                    保存
                  </el-button>
                  <el-button
                    link
                    size="small"
                    :disabled="!row.origin || !rowChanged(row) || busy"
                    @click="revertRow(row)"
                  >
                    撤销
                  </el-button>
                  <el-button
                    link
                    size="small"
                    :disabled="!canEdit"
                    @click="copyRow(row)"
                    >复制</el-button
                  >
                  <el-dropdown
                    trigger="click"
                    :disabled="!canEdit"
                    @command="onInsert(row, $event)"
                  >
                    <el-button link size="small" :disabled="!canEdit">
                      插入<el-icon class="el-icon--right"
                        ><ArrowDown
                      /></el-icon>
                    </el-button>
                    <template #dropdown>
                      <el-dropdown-menu>
                        <el-dropdown-item command="entry-above"
                          >在上方插入记录</el-dropdown-item
                        >
                        <el-dropdown-item command="entry-below"
                          >在下方插入记录</el-dropdown-item
                        >
                        <el-dropdown-item command="comment-above" divided
                          >在上方插入注释</el-dropdown-item
                        >
                        <el-dropdown-item command="comment-below"
                          >在下方插入注释</el-dropdown-item
                        >
                      </el-dropdown-menu>
                    </template>
                  </el-dropdown>
                  <el-button
                    link
                    type="danger"
                    size="small"
                    :disabled="!canEdit || busy"
                    @click="deleteRow(row)"
                  >
                    删除
                  </el-button>
                  <el-tooltip
                    v-if="row.kind === 'entry'"
                    :disabled="checkable(row)"
                    :content="
                      row.origin
                        ? '这条记录的 IP 或域名不合法,无法检查'
                        : '保存后才能检查'
                    "
                    placement="top"
                  >
                    <span>
                      <el-button
                        link
                        type="success"
                        size="small"
                        :disabled="!checkable(row)"
                        @click="openCheck(row)"
                      >
                        检查
                      </el-button>
                    </span>
                  </el-tooltip>
                </div>
              </template>
            </el-table-column>
          </el-table>

          <div class="table-tip">
            <div>
              ·
              直接在表格里修改,改过的行左侧标黄、新增的标绿;点该行的「保存」或上方的「全部保存」才会写入集群,保存和删除都会先展示改动再确认。
            </div>
            <div>
              · 「启用」关闭等于在行首加 #
              注释掉这条记录;整行注释(如分组标题)只能是注释,不能去掉 #。
            </div>
            <div>
              ·
              用「插入」可以在任意位置加记录或注释,「复制」会在下一行复制出一条新记录;只有改动的行会被重写,其他内容原样保留。
            </div>
          </div>
        </template>
      </el-card>
    </div>

    <CheckDialog
      v-model="checkVisible"
      :env="env"
      :has-reload="hasReload"
      :target="checkTarget"
    />
    <NetToolsDialog
      v-model="netVisible"
      :env="env"
      :mode="netMode"
      :can-tcp="canWrite(env)"
    />
    <AdvancedEditDialog
      v-model="advancedVisible"
      :env="env"
      :configmap="configmap"
      :readonly="!canSave"
      :pending="dirtyRows.length"
      @saved="onAdvancedSaved"
      @loaded="onAdvancedLoaded"
    />
  </div>
</template>

<script setup lang="ts">
import {
  computed,
  nextTick,
  onBeforeUnmount,
  onMounted,
  ref,
  shallowRef
} from "vue";
import { onBeforeRouteLeave } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";
import {
  Aim,
  ArrowDown,
  ChatLineSquare,
  Connection,
  EditPen,
  Plus,
  Refresh,
  Search,
  WarningFilled
} from "@element-plus/icons-vue";
import { getAgentNames } from "@/api/istio";
import {
  getCoreDns,
  isOldAgent,
  OLD_AGENT_TIP,
  saveCoreDns,
  type CoreDnsConfigMap
} from "@/api/coredns";
import { useSearchStoreHook } from "@/store/modules/search";
import { useClusterPermission } from "@/utils/permission";
import {
  buildCorefile,
  COREFILE_KEY,
  duplicateWarnings,
  isIP,
  isRowLine,
  parseCorefile,
  rebaseRows,
  rowChanged,
  rowFromLine,
  rowsOf,
  splitNames,
  validateCorefile,
  validateRow,
  type BuildResult,
  type CheckTarget,
  type CorefileModel,
  type HostsLine,
  type HostsRow,
  type RowIssues
} from "./corefile";
import { alertErrors, confirmChange } from "./confirm";
import CheckDialog from "./components/CheckDialog.vue";
import NetToolsDialog from "./components/NetToolsDialog.vue";
import AdvancedEditDialog from "./components/AdvancedEditDialog.vue";

defineOptions({ name: "CoreDnsManager" });

const searchStore = useSearchStoreHook();
const { canWrite } = useClusterPermission();

const envOptions = ref<string[]>([]);
const env = ref(searchStore.env || "");
let loadedEnv = "";
const loading = ref(false);
const loadError = ref("");
const configmap = shallowRef<CoreDnsConfigMap | null>(null);
const model = shallowRef<CorefileModel | null>(null);
const rows = ref<HostsRow[]>([]);
const blockIndex = ref(0);
const keyword = ref("");
const statusFilter = ref<
  "all" | "enabled" | "disabled" | "comment" | "changed"
>("all");
const busy = ref(false);
const savingId = ref<number | null>(null);
const checkVisible = ref(false);
const checkTarget = ref<CheckTarget | null>(null);
const netVisible = ref(false);
const netMode = ref<"dns" | "tcp">("dns");
const advancedVisible = ref(false);
let seq = 0;
const nextId = () => ++seq;

const resourceVersion = computed(
  () => configmap.value?.metadata?.resourceVersion || ""
);
const blocks = computed(() => model.value?.blocks ?? []);
const block = computed(
  () => blocks.value.find(item => item.index === blockIndex.value) ?? null
);
const canSave = computed(
  () => !!model.value && !model.value.error && canWrite(env.value)
);
const canEdit = computed(() => canSave.value && !!block.value?.editable);
const hasReload = computed(
  () => !!block.value?.server.directives.includes("reload")
);
const dirtyRows = computed(() => rows.value.filter(rowChanged));

const EMPTY_ISSUES: RowIssues = {
  ip: "",
  names: "",
  remark: "",
  text: "",
  warnings: []
};
const issues = computed(() => {
  const map = new Map<number, RowIssues>();
  for (const item of blocks.value) {
    const own = rows.value.filter(row => row.block === item.index);
    const duplicates = duplicateWarnings(own);
    for (const row of own) {
      const result = validateRow(row, item.zones);
      result.warnings.push(...(duplicates.get(row.id) ?? []));
      map.set(row.id, result);
    }
  }
  return map;
});
const issueOf = (row: HostsRow) => issues.value.get(row.id) ?? EMPTY_ISSUES;
const hasError = (row: HostsRow) => {
  const item = issueOf(row);
  return !!(item.ip || item.names || item.remark || item.text);
};

// 正在改的字段等失焦或点过「保存」再标红,免得刚插入一行、还没开始填就满屏报错;
// 没改过的已保存行(集群里原本就有问题的行)直接标红
type Field = "ip" | "names" | "remark" | "text";
const touched = ref(new Set<string>());
const attempted = ref(new Set<number>());
const touch = (row: HostsRow, field: Field) =>
  touched.value.add(`${row.id}:${field}`);
function errorOf(row: HostsRow, field: Field): string {
  const message = issueOf(row)[field];
  if (!message) return "";
  const shown =
    !rowChanged(row) ||
    attempted.value.has(row.id) ||
    touched.value.has(`${row.id}:${field}`);
  return shown ? message : "";
}

const stats = computed(() => {
  const own = rows.value.filter(row => row.block === blockIndex.value);
  const entries = own.filter(row => row.kind === "entry");
  const enabled = entries.filter(row => row.enabled).length;
  return {
    entries: entries.length,
    enabled,
    disabled: entries.length - enabled,
    comments: own.length - entries.length
  };
});

const visibleRows = computed(() => {
  const word = keyword.value.trim().toLowerCase();
  return rows.value.filter(row => {
    if (row.block !== blockIndex.value) return false;
    const changed = rowChanged(row);
    if (statusFilter.value === "changed") return changed;
    if (changed) return true; // 正在改的行始终显示,免得改着改着被筛选掉
    if (
      statusFilter.value === "enabled" &&
      !(row.kind === "entry" && row.enabled)
    )
      return false;
    if (
      statusFilter.value === "disabled" &&
      !(row.kind === "entry" && !row.enabled)
    )
      return false;
    if (statusFilter.value === "comment" && row.kind !== "comment")
      return false;
    return (
      !word ||
      [row.ip, row.names, row.remark, row.text]
        .join(" ")
        .toLowerCase()
        .includes(word)
    );
  });
});

const emptyText = computed(() => {
  if (keyword.value.trim() || statusFilter.value !== "all")
    return "没有匹配的记录";
  return canEdit.value
    ? "还没有自定义解析记录,点击右上角「添加记录」开始"
    : "没有记录";
});

const alerts = computed(() => {
  const list: Array<{ type: "error" | "warning" | "info"; title: string }> = [];
  const current = model.value;
  const item = block.value;
  if (!current) return list;
  if (current.error) {
    list.push({
      type: "error",
      title: `Corefile 格式有误:${current.error}。表格不能编辑,请用「编辑 YAML」修复`
    });
  } else if (!item) {
    list.push({
      type: "warning",
      title: "Corefile 里没有可以放 hosts 的 server 块,请用「编辑 YAML」修改"
    });
  } else {
    if (!item.editable) {
      list.push({
        type: "warning",
        title: `这个 hosts 块的写法不常见(${item.problem}),为避免改坏,表格只读,请用「编辑 YAML」修改`
      });
    }
    if (item.virtual) {
      list.push({
        type: "info",
        title: `Corefile 里还没有 hosts 插件,保存第一条记录时会在 ${item.server.label} 里自动加上 hosts { … fallthrough }`
      });
    } else if (!item.hasFallthrough && item.zones.includes(".")) {
      list.push({
        type: "warning",
        title:
          "hosts 没有配置 fallthrough:hosts 里没有的域名会直接解析失败(SERVFAIL),通常都要加上"
      });
    }
    if (!hasReload.value) {
      list.push({
        type: "warning",
        title:
          "Corefile 没有启用 reload 插件:修改后不会自动生效,需要重启 CoreDNS"
      });
    }
  }
  if (env.value && !canWrite(env.value)) {
    list.push({
      type: "info",
      title: "当前账号对这个集群只读:可以查看和检查解析,不能修改"
    });
  }
  return list;
});

function rowClass({ row }: { row: HostsRow }) {
  return [
    row.kind === "comment" ? "row-comment" : row.enabled ? "" : "row-disabled",
    !row.origin ? "row-new" : rowChanged(row) ? "row-changed" : ""
  ]
    .filter(Boolean)
    .join(" ");
}

// 注释行:IP、域名、备注三列合成一格
function spanMethod({
  row,
  columnIndex
}: {
  row: HostsRow;
  columnIndex: number;
}) {
  if (row.kind !== "comment") return undefined;
  if (columnIndex === 2) return [1, 3];
  if (columnIndex === 3 || columnIndex === 4) return [0, 0];
  return undefined;
}

// ---------------------------------------------------------------------------------------- 加载
function applyConfigMap(cm: CoreDnsConfigMap, keepEdits: boolean): number {
  configmap.value = cm;
  const text = cm.data?.[COREFILE_KEY];
  if (typeof text !== "string") {
    model.value = null;
    rows.value = [];
    loadError.value = `ConfigMap coredns 里没有 ${COREFILE_KEY}`;
    return 0;
  }
  const next = parseCorefile(text);
  let lost = 0;
  if (keepEdits && dirtyRows.value.length) {
    const merged = rebaseRows(rows.value, next, nextId);
    rows.value = merged.rows;
    lost = merged.lost;
  } else {
    rows.value = rowsOf(next, nextId);
  }
  model.value = next;
  if (!next.blocks.some(item => item.index === blockIndex.value))
    blockIndex.value = 0;
  return lost;
}

function warnLost(lost: number) {
  if (lost) {
    ElMessage.warning(
      `有 ${lost} 处未保存的修改,对应的行已经被别人修改或删除,这些修改已丢弃`
    );
  }
}

function resetState(message: string) {
  loadError.value = message;
  configmap.value = null;
  model.value = null;
  rows.value = [];
}

async function load(keepEdits = false): Promise<boolean> {
  const target = env.value;
  if (!target) return false;
  loading.value = true;
  let ok = false;
  try {
    const res = await getCoreDns(target);
    if (env.value !== target) return false; // 加载期间切换了集群
    if (res.success && res.data) {
      loadError.value = "";
      warnLost(applyConfigMap(res.data, keepEdits && loadedEnv === target));
      ok = !loadError.value;
    } else {
      resetState(
        isOldAgent(res)
          ? OLD_AGENT_TIP
          : res.error || res.message || "读取 CoreDNS 配置失败"
      );
    }
  } catch (error: any) {
    if (env.value !== target) return false;
    resetState(error?.response?.data?.error || "读取 CoreDNS 配置失败");
  } finally {
    if (env.value === target) loading.value = false;
  }
  loadedEnv = target;
  return ok;
}

async function init() {
  try {
    const res = await getAgentNames();
    envOptions.value = (res.data || []).map((item: any) => String(item));
  } catch {
    envOptions.value = [];
  }
  if (!envOptions.value.length) return;
  if (!envOptions.value.includes(env.value)) env.value = envOptions.value[0];
  searchStore.setEnv(env.value);
  await load();
}

async function handleEnvChange(value: string) {
  if (dirtyRows.value.length && loadedEnv && value !== loadedEnv) {
    try {
      await ElMessageBox.confirm(
        `集群 ${loadedEnv} 还有 ${dirtyRows.value.length} 处修改没有保存,切换集群会丢弃这些修改,确定切换吗?`,
        "提示",
        { type: "warning", confirmButtonText: "切换", cancelButtonText: "取消" }
      );
    } catch {
      env.value = loadedEnv;
      return;
    }
  }
  searchStore.setEnv(value);
  resetState("");
  blockIndex.value = 0;
  await load();
}

async function handleRefresh() {
  const kept = dirtyRows.value.length;
  if (await load(true)) {
    ElMessage.success(
      kept ? "已加载集群里的最新配置,未保存的修改已保留" : "已刷新"
    );
  }
}

async function discardAll() {
  try {
    await ElMessageBox.confirm(
      `确定放弃 ${dirtyRows.value.length} 处未保存的修改吗?`,
      "放弃修改",
      {
        type: "warning",
        confirmButtonText: "放弃",
        cancelButtonText: "取消"
      }
    );
  } catch {
    return;
  }
  if (model.value) rows.value = rowsOf(model.value, nextId);
}

// ---------------------------------------------------------------------------------------- 编辑
function blankRow(kind: HostsRow["kind"], source?: HostsRow): HostsRow {
  return {
    id: nextId(),
    block: blockIndex.value,
    kind,
    enabled: source ? source.enabled : true,
    ip: source?.ip ?? "",
    names: source?.names ?? "",
    remark: source?.remark ?? "",
    text: source?.text ?? "",
    origin: null
  };
}

async function insertAt(row: HostsRow, index: number) {
  rows.value.splice(index, 0, row);
  await nextTick();
  const input = document.querySelector<HTMLInputElement>(
    `.coredns-table [data-focus="${row.id}"] input`
  );
  input?.scrollIntoView({ block: "center", behavior: "smooth" });
  input?.focus({ preventScroll: true });
}

const indexOfRow = (row: HostsRow) =>
  rows.value.findIndex(item => item.id === row.id);

function onInsert(anchor: HostsRow, command: string | number | object) {
  const [kind, where] = String(command).split("-") as [
    HostsRow["kind"],
    "above" | "below"
  ];
  const index = indexOfRow(anchor);
  insertAt(blankRow(kind), where === "above" ? index : index + 1);
}

function copyRow(row: HostsRow) {
  insertAt(blankRow(row.kind, row), indexOfRow(row) + 1);
}

function appendRow(kind: HostsRow["kind"]) {
  let index = -1;
  rows.value.forEach((row, i) => {
    if (row.block === blockIndex.value) index = i;
  });
  insertAt(blankRow(kind), index >= 0 ? index + 1 : rows.value.length);
}

function revertRow(row: HostsRow) {
  const origin = row.origin;
  if (!origin) return;
  Object.assign(row, {
    enabled: origin.enabled,
    ip: origin.ip,
    names: origin.names.join(" "),
    remark: origin.remark,
    text: origin.text
  });
}

const describe = (row: HostsRow) =>
  row.kind === "comment"
    ? `「${row.text.trim()}」`
    : `${row.ip.trim()} ${splitNames(row.names).join(" ")}`;

function actionOf(row: HostsRow): string {
  const what = row.kind === "comment" ? "注释" : "记录";
  const origin = row.origin;
  if (!origin) return `新增${what}`;
  if (row.kind === "entry" && row.enabled !== origin.enabled) {
    const edited =
      row.ip.trim() !== origin.ip ||
      splitNames(row.names).join(" ") !== origin.names.join(" ") ||
      row.remark.trim() !== origin.remark;
    return `${edited ? "修改并" : ""}${row.enabled ? "启用" : "停用"}记录`;
  }
  return `修改${what}`;
}

// ---------------------------------------------------------------------------------------- 保存
/** 保存后把表格行对到新的 Corefile:写入的行变成已保存,没写入的修改原样保留 */
function afterSaved(
  cm: CoreDnsConfigMap,
  nextRows: HostsRow[],
  apply: Set<number>,
  built: BuildResult
) {
  const reloadClean = () => {
    applyConfigMap(cm, false);
    ElMessage.warning(
      "已保存,但页面没能对上保存后的内容,已重新加载(未保存的修改已丢弃)"
    );
  };
  const text = cm.data?.[COREFILE_KEY];
  if (text !== built.text) return reloadClean();
  const next = parseCorefile(text);
  const lines = new Map<number, { line: HostsLine; block: number }>();
  next.blocks.forEach(item =>
    item.lines
      .filter(isRowLine)
      .forEach(line => lines.set(line.line, { line, block: item.index }))
  );
  const result: HostsRow[] = [];
  for (const row of nextRows) {
    const at = built.lineOf.get(row.id);
    if (at === undefined) {
      if (row.origin) return reloadClean();
      result.push(row); // 没写入的新行
      continue;
    }
    const found = lines.get(at);
    if (!found || found.line.kind !== row.kind) return reloadClean();
    if (apply.has(row.id)) {
      result.push(rowFromLine(found.line, found.block, row.id));
    } else {
      row.origin = found.line;
      row.block = found.block;
      result.push(row);
    }
  }
  configmap.value = cm;
  model.value = next;
  rows.value = result;
}

async function onConflict() {
  try {
    await ElMessageBox.confirm(
      "CoreDNS 配置在你加载之后被修改过(别人或别的页面保存过)。点「加载最新」会重新读取集群里的配置,并尽量保留你还没保存的修改,确认后再保存。",
      "保存冲突",
      {
        type: "warning",
        confirmButtonText: "加载最新",
        cancelButtonText: "取消"
      }
    );
  } catch {
    return;
  }
  await load(true);
}

async function commit(
  nextRows: HostsRow[],
  apply: Set<number>,
  options: {
    title: string;
    summary: string;
    confirmText: string;
    danger?: boolean;
  }
): Promise<boolean> {
  const current = model.value;
  if (!current || !configmap.value) return false;
  const built = buildCorefile(current, nextRows, apply);
  if (built.text === current.text) {
    ElMessage.info("内容没有变化");
    return false;
  }
  const after = validateCorefile(built.text);
  const before = validateCorefile(current.text);
  if (after.errors.length && !before.errors.length) {
    alertErrors("保存后 Corefile 会出错,已取消", after.errors);
    return false;
  }
  const confirmed = await confirmChange({
    env: env.value,
    title: options.title,
    diffs: [{ before: current.text, after: built.text }],
    notes: [...after.errors, ...after.warnings],
    confirmText: options.confirmText,
    danger: options.danger
  });
  if (!confirmed) return false;
  busy.value = true;
  try {
    const res = await saveCoreDns(
      env.value,
      resourceVersion.value,
      { data: { [COREFILE_KEY]: built.text } },
      options.summary
    );
    if (res.success && res.data) {
      afterSaved(res.data, nextRows, apply, built);
      return true;
    }
    if (res.conflict) {
      await onConflict();
    } else {
      ElMessage.error(
        isOldAgent(res) ? OLD_AGENT_TIP : res.error || res.message || "保存失败"
      );
    }
  } catch {
    // 请求失败时 http 工具已经提示过
  } finally {
    busy.value = false;
  }
  return false;
}

async function saveRow(row: HostsRow) {
  if (hasError(row)) {
    attempted.value.add(row.id);
    ElMessage.error("请先修正标红的内容");
    return;
  }
  const action = actionOf(row);
  savingId.value = row.id;
  try {
    if (
      await commit(rows.value, new Set([row.id]), {
        title: `确认${action}`,
        summary: `${action} ${describe(row)}`,
        confirmText: "保存"
      })
    ) {
      ElMessage.success(
        row.kind === "entry"
          ? "已保存,可以点这一行的「检查」确认是否生效"
          : "已保存"
      );
    }
  } finally {
    savingId.value = null;
  }
}

async function deleteRow(row: HostsRow) {
  const what = row.kind === "comment" ? "注释" : "记录";
  if (!row.origin) {
    try {
      await ElMessageBox.confirm(`这条${what}还没有保存,确定删除吗?`, "删除", {
        type: "warning",
        confirmButtonText: "删除",
        cancelButtonText: "取消",
        confirmButtonClass: "el-button--danger"
      });
    } catch {
      return;
    }
    rows.value = rows.value.filter(item => item.id !== row.id);
    return;
  }
  savingId.value = row.id;
  try {
    if (
      await commit(
        rows.value.filter(item => item.id !== row.id),
        new Set(),
        {
          title: `确认删除这条${what}`,
          summary: `删除${what} ${row.origin.raw.trim()}`,
          confirmText: "删除",
          danger: true
        }
      )
    ) {
      ElMessage.success("已删除");
    }
  } finally {
    savingId.value = null;
  }
}

async function saveAll() {
  const dirty = dirtyRows.value;
  if (!dirty.length) return;
  const bad = dirty.filter(hasError);
  if (bad.length) {
    bad.forEach(row => attempted.value.add(row.id));
    statusFilter.value = "changed";
    ElMessage.error(`有 ${bad.length} 条修改没有通过校验,请先修正标红的内容`);
    return;
  }
  if (
    await commit(rows.value, new Set(dirty.map(row => row.id)), {
      title: `确认保存全部修改(${dirty.length} 处)`,
      summary: `批量保存 ${dirty.length} 处修改`,
      confirmText: "全部保存"
    })
  ) {
    ElMessage.success("已全部保存");
  }
}

// 高级编辑里保存 / 重新加载后,表格换成集群里最新的内容,没保存的修改尽量保留
function onAdvancedSaved(cm: CoreDnsConfigMap) {
  warnLost(applyConfigMap(cm, true));
}

function onAdvancedLoaded(cm: CoreDnsConfigMap) {
  if (cm.metadata?.resourceVersion !== resourceVersion.value) {
    warnLost(applyConfigMap(cm, true));
  }
}

// ---------------------------------------------------------------------------------------- 检查
const checkable = (row: HostsRow) =>
  row.kind === "entry" &&
  !!row.origin &&
  isIP(row.origin.ip) &&
  row.origin.names.length > 0 &&
  row.origin.names.length <= 10;

function openCheck(row: HostsRow) {
  const origin = row.origin;
  if (!origin || !checkable(row)) return;
  checkTarget.value = {
    names: [...origin.names],
    ip: origin.ip,
    enabled: origin.enabled,
    line: origin.line + 1,
    dirty: rowChanged(row)
  };
  checkVisible.value = true;
}

function openNetTools(mode: "dns" | "tcp") {
  netMode.value = mode;
  netVisible.value = true;
}

// ---------------------------------------------------------------------------------------- 离开页面
onBeforeRouteLeave(async () => {
  if (!dirtyRows.value.length) return true;
  try {
    await ElMessageBox.confirm(
      `还有 ${dirtyRows.value.length} 处修改没有保存,离开页面会丢弃这些修改,确定离开吗?`,
      "提示",
      { type: "warning", confirmButtonText: "离开", cancelButtonText: "留下" }
    );
    return true;
  } catch {
    return false;
  }
});

function onBeforeUnload(event: BeforeUnloadEvent) {
  if (!dirtyRows.value.length) return;
  event.preventDefault();
  event.returnValue = "";
}

onMounted(() => {
  window.addEventListener("beforeunload", onBeforeUnload);
  init();
});
onBeforeUnmount(() =>
  window.removeEventListener("beforeunload", onBeforeUnload)
);
</script>

<style scoped>
.coredns-manager {
  padding: 1px;
}

.search-section {
  margin-bottom: 2px;
}

.query-form {
  display: flex;
  gap: 12px;
  align-items: flex-start;
  width: 100%;
}

.query-form__left {
  display: flex;
  flex: 1;
  flex-wrap: wrap;
  gap: 8px 0;
  align-items: center;
  min-width: 0;
}

.query-form__left .el-form-item {
  margin-right: 16px;
  margin-bottom: 0;
}

.query-form__right {
  display: flex;
  flex: none;
  gap: 8px;
  align-items: center;
}

.query-form__right .el-button + .el-button {
  margin-left: 0;
}

.filter-group {
  flex-wrap: nowrap;
}

.mt-2 {
  margin-top: 8px;
}

.info-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 16px;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 10px;
}

.info-bar__group {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 14px;
  align-items: center;
}

.info-bar__group .el-button + .el-button {
  margin-left: 0;
}

.info-item {
  font-size: 13px;
  color: var(--el-text-color-regular);
}

.info-muted {
  color: var(--el-text-color-secondary);
}

.block-alert {
  margin-bottom: 10px;
}

.line-cell {
  display: flex;
  flex-direction: column;
  gap: 2px;
  align-items: center;
}

.line-no {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  color: var(--el-text-color-secondary);
}

.header-tip {
  cursor: help;
  border-bottom: 1px dashed var(--el-text-color-placeholder);
}

.mono :deep(.el-input__inner) {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}

.hash {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-weight: 600;
  color: var(--el-color-success);
}

.comment-input :deep(.el-input__inner) {
  font-style: italic;
  color: var(--el-color-success-dark-2);
}

.issue-icon {
  cursor: help;
}

.issue-icon.is-error {
  color: var(--el-color-danger);
}

.issue-icon.is-warning {
  color: var(--el-color-warning);
}

.is-invalid :deep(.el-input__wrapper) {
  box-shadow: 0 0 0 1px var(--el-color-danger) inset;
}

.is-warning :deep(.el-input__wrapper) {
  box-shadow: 0 0 0 1px var(--el-color-warning) inset;
}

.coredns-table :deep(.row-comment) {
  --el-table-tr-bg-color: var(--el-fill-color-lighter);
}

.coredns-table :deep(.row-disabled .el-input__inner) {
  color: var(--el-text-color-placeholder);
}

.coredns-table :deep(.row-new > td:first-child) {
  box-shadow: inset 3px 0 0 var(--el-color-success);
}

.coredns-table :deep(.row-changed > td:first-child) {
  box-shadow: inset 3px 0 0 var(--el-color-warning);
}

.ops {
  display: flex;
  flex-wrap: nowrap;
  gap: 2px;
  align-items: center;
}

.ops .el-button {
  padding: 0 4px;
}

.ops .el-button + .el-button {
  margin-left: 0;
}

.table-tip {
  margin-top: 10px;
  font-size: 12px;
  line-height: 1.8;
  color: var(--el-text-color-secondary);
}
</style>
