<template>
  <div class="pod-manager-container">
    <div class="search-section">
      <el-form
        :model="searchForm"
        inline
        class="query-form"
        style="margin-bottom: -18px"
      >
        <el-form-item label="K8S">
          <el-select
            v-model="searchForm.env"
            placeholder="请选择K8S环境"
            class="!w-[180px]"
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
        </el-form-item>

        <el-form-item label="命名空间">
          <el-select
            v-model="searchForm.namespace"
            placeholder="请选择命名空间"
            class="!w-[180px]"
            filterable
            clearable
            :disabled="!envOptions.length"
            @change="handleNamespaceChange"
          >
            <el-option
              v-for="item in nsOptions"
              :key="item"
              :label="item"
              :value="item"
            />
          </el-select>
        </el-form-item>

        <el-form-item label="关键字">
          <el-input
            v-model="searchForm.keyword"
            placeholder="请输入关键字搜索"
            style="width: 200px"
            clearable
          />
        </el-form-item>

        <el-form-item>
          <el-button type="primary" :loading="loading" @click="handleQuery">
            查询
          </el-button>
        </el-form-item>

        <el-form-item>
          <el-button
            type="primary"
            plain
            :icon="Refresh"
            :loading="loading"
            @click="handleRefresh"
          >
            刷新
          </el-button>
        </el-form-item>

        <el-form-item class="right-auto">
          <el-button
            type="danger"
            plain
            :disabled="!selectedPods.length"
            :loading="batchDeleting"
            @click="handleBatchDelete"
          >
            批量删除
          </el-button>
          <el-button type="success" @click="handleCreate">新建</el-button>
        </el-form-item>
      </el-form>
    </div>

    <div class="mt-2">
      <el-card v-loading="loading" element-loading-text="加载中...">
        <el-table
          ref="tableRef"
          :data="paginatedTableData"
          style="width: 100%"
          stripe
          border
          :row-key="row => `${row.namespace}-${row.name}`"
          empty-text="请先选择查询条件并点击查询"
          @selection-change="handleSelectionChange"
          @sort-change="handleSortChange"
        >
          <el-table-column type="selection" width="48" align="center" />
          <el-table-column
            prop="namespace"
            label="命名空间"
            min-width="90"
            show-overflow-tooltip
            align="center"
            sortable="custom"
          />

          <el-table-column
            prop="name"
            label="Pod名称"
            min-width="250"
            show-overflow-tooltip
            align="center"
            sortable="custom"
          >
            <template #default="podScope">
              <div
                style="
                  overflow: hidden;
                  text-align: left;
                  text-overflow: ellipsis;
                  white-space: nowrap;
                  direction: rtl;
                "
              >
                {{ podScope.row.name }}
              </div>
            </template>
          </el-table-column>

          <el-table-column
            prop="status"
            label="状态"
            width="100"
            align="center"
            sortable="custom"
          >
            <template #default="scope">
              <el-tooltip
                v-if="
                  scope.row.status !== 'Pending' &&
                  scope.row.status !== 'Running' &&
                  scope.row.status !== 'Succeeded'
                "
                effect="dark"
                placement="top"
                :show-after="0"
              >
                <template #content>
                  <div class="status-tooltip">
                    <div>
                      status_reason: {{ scope.row.status_reason || "-" }}
                    </div>
                    <div>
                      status_message: {{ scope.row.status_message || "-" }}
                    </div>
                  </div>
                </template>
                <el-tag
                  size="small"
                  effect="plain"
                  :type="getStatusTagType(scope.row.status)"
                >
                  {{ scope.row.status }}
                </el-tag>
              </el-tooltip>
              <el-tag
                v-else-if="scope.row.status"
                size="small"
                effect="plain"
                :type="getStatusTagType(scope.row.status)"
              >
                {{ scope.row.status }}
              </el-tag>
              <span v-else>-</span>
            </template>
          </el-table-column>
          <el-table-column
            prop="pod_ip"
            label="Pod IP"
            min-width="120"
            align="center"
            show-overflow-tooltip
            sortable="custom"
          >
            <template #default="scope">
              {{ scope.row.pod_ip || "-" }}
            </template>
          </el-table-column>
          <el-table-column
            prop="restart_count"
            label="重启"
            width="80"
            align="center"
            sortable="custom"
          >
            <template #default="scope">
              <span
                :class="[
                  'numeric-cell',
                  getRestartClass(scope.row.restart_count)
                ]"
              >
                {{ scope.row.restart_count }}
              </span>
            </template>
          </el-table-column>
          <el-table-column
            prop="containers"
            label="容器状态"
            width="110"
            sortable="custom"
          >
            <template #default="scope">
              <div class="containers-cell">
                <template
                  v-if="scope.row.containers && scope.row.containers.length"
                >
                  <div class="container-emojis">
                    <el-tooltip
                      v-for="container in getSortedContainers(
                        scope.row.containers
                      )"
                      :key="`${scope.row.name}-${container.name}`"
                      effect="dark"
                      placement="top"
                      :show-after="0"
                    >
                      <template #content>
                        <pre class="container-tooltip">{{
                          formatContainerInfo(container)
                        }}</pre>
                      </template>
                      <span class="container-status-emoji">
                        {{ getContainerEmoji(container) }}
                      </span>
                    </el-tooltip>
                  </div>
                </template>
                <span v-else class="text-disabled">-</span>
              </div>
            </template>
          </el-table-column>
          <el-table-column
            prop="current_cpu_cores"
            label="CPU(核)"
            width="110"
            align="center"
            sortable="custom"
          >
            <template #default="scope">
              <span
                :class="[
                  'numeric-cell',
                  getCpuClass(scope.row.current_cpu_cores)
                ]"
              >
                {{ formatCpu(scope.row.current_cpu_cores) }}
              </span>
            </template>
          </el-table-column>
          <el-table-column
            prop="current_memory_mb"
            label="内存(MB)"
            width="110"
            align="center"
            sortable="custom"
          >
            <template #default="scope">
              <span
                :class="[
                  'numeric-cell',
                  getMemoryClass(scope.row.current_memory_mb)
                ]"
              >
                {{ formatMemory(scope.row.current_memory_mb) }}
              </span>
            </template>
          </el-table-column>

          <el-table-column
            prop="node_name"
            label="节点IP"
            min-width="100"
            show-overflow-tooltip
            align="center"
            sortable="custom"
          />
          <el-table-column
            prop="controlled_by"
            label="控制器"
            min-width="100"
            align="center"
            show-overflow-tooltip
            sortable="custom"
          >
            <template #default="scope">
              <el-tag
                v-if="scope.row.controlled_by"
                size="small"
                effect="plain"
              >
                {{ scope.row.controlled_by }}
              </el-tag>
              <span v-else>-</span>
            </template>
          </el-table-column>
          <el-table-column
            prop="creation_timestamp"
            label="创建时间"
            min-width="120"
            align="center"
            sortable="custom"
          >
            <template #default="scope">
              {{ formatCreationTime(scope.row.creation_timestamp) }}
            </template>
          </el-table-column>
          <el-table-column label="操作" width="80" align="center" fixed="right">
            <template #default="podScope">
              <el-dropdown>
                <el-button type="primary" size="small"> 操作 </el-button>
                <template #dropdown>
                  <el-dropdown-menu>
                    <el-dropdown-item
                      @click="
                        handleViewLogs(
                          searchForm.env,
                          podScope.row.namespace,
                          podScope.row.name,
                          extractDeploymentName(podScope.row.controlled_by)
                        )
                      "
                      >日志</el-dropdown-item
                    >
                    <el-dropdown-item
                      @click="
                        handleModifyPod(
                          searchForm.env,
                          podScope.row.namespace,
                          podScope.row.name,
                          extractDeploymentName(podScope.row.controlled_by),
                          podScope.row.controlled_by
                        )
                      "
                      >隔离</el-dropdown-item
                    >
                    <el-dropdown-item
                      @click="
                        handleDeletePod(
                          searchForm.env,
                          podScope.row.namespace,
                          podScope.row.name
                        )
                      "
                      >删除</el-dropdown-item
                    >
                    <el-dropdown-item
                      @click="
                        handleAutoDump(
                          searchForm.env,
                          podScope.row.namespace,
                          podScope.row.name
                        )
                      "
                      >Dump</el-dropdown-item
                    >
                    <el-dropdown-item
                      @click="
                        handleAutoJstack(
                          searchForm.env,
                          podScope.row.namespace,
                          podScope.row.name
                        )
                      "
                      >Jstack</el-dropdown-item
                    >
                    <el-dropdown-item
                      @click="
                        handleAutoJfr(
                          searchForm.env,
                          podScope.row.namespace,
                          podScope.row.name
                        )
                      "
                      >JFR</el-dropdown-item
                    >
                    <el-dropdown-item
                      @click="
                        handleAutoJvmMem(
                          searchForm.env,
                          podScope.row.namespace,
                          podScope.row.name
                        )
                      "
                      >JVM</el-dropdown-item
                    >
                  </el-dropdown-menu>
                </template>
              </el-dropdown>
            </template>
          </el-table-column>
        </el-table>
        <div class="table-pagination">
          <el-pagination
            background
            layout="total, sizes, prev, pager, next, jumper"
            :total="filteredTableData.length"
            :page-sizes="pageSizeOptions"
            :page-size="pageSize"
            :current-page="currentPage"
            @size-change="handlePageSizeChange"
            @current-change="handlePageChange"
          />
        </div>
      </el-card>
    </div>

    <el-dialog
      v-model="createDialogVisible"
      title="新建Pod"
      fullscreen
      :close-on-click-modal="false"
      destroy-on-close
    >
      <div class="edit-container">
        <div class="yaml-editor-container">
          <div class="editor-header">
            <span class="editor-title">Pod YAML配置</span>
            <el-button
              type="primary"
              :loading="createSaveLoading"
              @click="handleCreateSave"
            >
              提交
            </el-button>
          </div>
          <div
            ref="createYamlEditorRef"
            class="yaml-editor create-yaml-editor"
          />
        </div>
      </div>
    </el-dialog>

    <el-dialog
      v-model="createUpdateMethodDialogVisible"
      title="选择更新方式"
      width="600px"
      :close-on-click-modal="false"
    >
      <div class="update-method-container">
        <div class="method-radio-row">
          <el-radio-group v-model="selectedCreateUpdateMethod">
            <el-radio value="create">Create - 新建</el-radio>
            <el-radio value="apply">Apply - 应用（推荐）</el-radio>
            <el-radio value="replace">Replace - 替换</el-radio>
          </el-radio-group>
        </div>
        <div class="method-description">
          <p v-if="selectedCreateUpdateMethod === 'create'">
            Create方式会按照提供的YAML直接创建Pod。
          </p>
          <p v-if="selectedCreateUpdateMethod === 'apply'">
            Apply方式会智能合并已有字段，推荐用于大多数更新场景。
          </p>
          <p v-if="selectedCreateUpdateMethod === 'replace'">
            Replace方式会完全替换配置，请确保YAML内容完整准确。
          </p>
        </div>
      </div>
      <template #footer>
        <span class="dialog-footer">
          <el-button @click="createUpdateMethodDialogVisible = false">
            取消
          </el-button>
          <el-button
            type="primary"
            :loading="createSaveLoading"
            @click="confirmCreateUpdate"
          >
            确认提交
          </el-button>
        </span>
      </template>
    </el-dialog>

    <el-dialog
      v-model="logDialogVisible"
      width="99%"
      top="0.5vh"
      :style="{ 'padding-top': '7px' }"
      :close-on-click-modal="false"
      @close="stopLogStream"
    >
      <template #header>
        <div class="log-dialog-header">
          <div class="log-controls-header">
            <el-button
              v-if="!isLogConnected"
              type="primary"
              size="small"
              :loading="logConnecting"
              @click="startLogStream"
            >
              开始查看日志
            </el-button>
            <el-button v-else type="danger" size="small" @click="stopLogStream">
              停止查看
            </el-button>
            <el-button size="small" @click="clearLogs">清空日志</el-button>
            <el-button size="small" @click="scrollToBottom"
              >滚动到底部</el-button
            >
            <div class="log-status">
              <span
                :class="{
                  'status-connected': isLogConnected,
                  'status-disconnected': !isLogConnected
                }"
              >
                {{ isLogConnected ? "已连接" : "未连接" }}
              </span>
            </div>
            <div class="log-search-container">
              <el-input
                v-model="searchKeyword"
                placeholder="搜索日志内容"
                size="small"
                style="width: 200px; margin-right: 8px"
                @keyup.enter="() => performSearch(true)"
              >
                <template #append>
                  <el-button size="small" @click="() => performSearch(true)">
                    搜索
                  </el-button>
                </template>
              </el-input>
              <el-button
                size="small"
                :type="isFilterMode ? 'primary' : 'default'"
                :disabled="!searchKeyword.trim() || totalMatches === 0"
                @click="toggleFilterMode"
              >
                {{ isFilterMode ? "取消筛选" : "筛选" }}
              </el-button>
              <span v-if="totalMatches > 0" class="search-info">
                {{ currentMatchIndex + 1 }}/{{ totalMatches }}
              </span>
              <el-button
                size="small"
                :disabled="totalMatches === 0"
                @click="goToPreviousMatch"
              >
                上一个
              </el-button>
              <el-button
                size="small"
                :disabled="totalMatches === 0"
                @click="goToNextMatch"
              >
                下一个
              </el-button>
              <el-button size="small" type="warning" @click="getPreviousLogs">
                重启前日志
              </el-button>
            </div>
          </div>
          <span class="dialog-title"
            >Pod日志: {{ currentPodInfo.env }}【{{
              currentPodInfo.namespace
            }}】{{ currentPodInfo.name }}</span
          >
        </div>
      </template>
      <div class="log-container">
        <div
          ref="logContentRef"
          v-loading="logConnecting"
          class="log-content"
          element-loading-text="正在连接日志流..."
          @scroll="handleScroll"
        >
          <div v-if="logMessages.length === 0" class="no-logs">
            暂无日志数据
          </div>
          <div
            v-for="(message, index) in filteredLogMessages"
            :key="getLogKey(message, index)"
            class="log-line"
            :class="{
              'log-error':
                message.includes('ERROR') || message.includes('Exception'),
              'log-warn': message.includes('WARN'),
              'log-info': message.includes('INFO')
            }"
            v-html="
              highlightSearchKeyword(message, getOriginalIndex(message, index))
            "
          />
        </div>
      </div>
    </el-dialog>

    <el-dialog
      v-model="resultDialogVisible"
      class="result-dialog"
      :width="
        currentOperation === 'jstack' || currentOperation === 'dump'
          ? '95%'
          : '700px'
      "
      :top="
        currentOperation === 'jstack' || currentOperation === 'dump'
          ? '2.5vh'
          : '15vh'
      "
      destroy-on-close
    >
      <pre
        class="result-content"
        :style="
          currentOperation === 'jstack' || currentOperation === 'dump'
            ? { 'max-height': '82vh', 'overflow-y': 'auto' }
            : { 'max-height': '600px', 'overflow-y': 'auto' }
        "
        v-html="resultMessage"
      />
      <template #footer>
        <el-button type="primary" @click="handleCopyAndClose">关闭</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import {
  computed,
  h,
  nextTick,
  onBeforeUnmount,
  onMounted,
  reactive,
  ref,
  watch
} from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import type { TableInstance } from "element-plus";
import { Refresh } from "@element-plus/icons-vue";
import { AnsiUp } from "ansi_up";
import { getAgentNames } from "@/api/istio";
import {
  getPromNamespace,
  getPodPreviousLogs,
  showAddLabel,
  getNodeResourceRank,
  createPodLogStreamUrl
} from "@/api/monit";
import {
  getPodList,
  deletePodsBatch,
  type PodContainerStatus,
  type PodItem,
  type DeletePodsPayload
} from "@/api/pod";
import {
  modifyPod,
  deletePod,
  autoDump,
  autoJstack,
  autoJfr,
  autoJvmMem
} from "@/api/alarm";
import { updateServiceContent } from "@/api/service";
import { useSearchStoreHook } from "@/store/modules/search";
import * as monaco from "monaco-editor";
import * as yaml from "js-yaml";
import { YAMLException } from "js-yaml";

defineOptions({
  name: "PodManager"
});

interface SearchForm {
  env: string;
  namespace: string;
  keyword: string;
}

const searchStore = useSearchStoreHook();

const searchForm = reactive<SearchForm>({
  env: searchStore.env || "",
  namespace: searchStore.namespace || "",
  keyword: ""
});

const envOptions = ref<string[]>([]);
const nsOptions = ref<string[]>([]);
const tableData = ref<PodItem[]>([]);
const tableRef = ref<TableInstance>();
const selectedPods = ref<PodItem[]>([]);
const batchDeleting = ref(false);
const loading = ref(false);
const appliedKeyword = ref("");
const lastFetchedEnv = ref<string | null>(null);
const lastFetchedNamespace = ref<string | null>(null);
const pageSizeOptions = [100, 200, 500, 1000];
const pageSize = ref<number>(pageSizeOptions[0]);
const currentPage = ref(1);
const resetPagination = () => {
  currentPage.value = 1;
};
const createDialogVisible = ref(false);
const createSaveLoading = ref(false);
const createYamlEditorRef = ref<HTMLElement | null>(null);
let createYamlEditor: monaco.editor.IStandaloneCodeEditor | null = null;
const createUpdateMethodDialogVisible = ref(false);
const selectedCreateUpdateMethod = ref<"create" | "apply" | "replace">(
  "create"
);

const logDialogVisible = ref(false);
const logMessages = ref<string[]>([]);
const isLogConnected = ref(false);
const logConnecting = ref(false);
const logSocket = ref<WebSocket | null>(null);
const logContentRef = ref<HTMLElement | null>(null);
const isUserScrolling = ref(false);

const currentPodInfo = ref({
  name: "",
  env: "",
  namespace: "",
  deployment: "",
  originalDeployment: "",
  controlledBy: ""
});

const searchKeyword = ref("");
const searchMatches = ref<number[]>([]);
const currentMatchIndex = ref(-1);
const totalMatches = ref(0);
const isFilterMode = ref(false);

const filteredLogMessages = computed(() => {
  if (!isFilterMode.value || !searchKeyword.value.trim()) {
    return logMessages.value;
  }
  const keywordLower = searchKeyword.value.toLowerCase();
  return logMessages.value.filter(message =>
    message.toLowerCase().includes(keywordLower)
  );
});

const resultDialogVisible = ref(false);
const resultMessage = ref("");
const currentOperation = ref("");

const filteredTableData = computed(() => {
  const keyword = appliedKeyword.value.trim().toLowerCase();
  if (!keyword) {
    return tableData.value;
  }
  return tableData.value.filter(item => {
    const includesKeyword = (value?: string | null) =>
      value ? value.toLowerCase().includes(keyword) : false;

    if (
      includesKeyword(item.name) ||
      includesKeyword(item.namespace) ||
      includesKeyword(item.controlled_by) ||
      includesKeyword(item.status) ||
      includesKeyword(item.node_name) ||
      includesKeyword(item.pod_ip)
    ) {
      return true;
    }

    return item.containers?.some(
      container =>
        includesKeyword(container.name) ||
        includesKeyword(container.state) ||
        includesKeyword(container.reason) ||
        includesKeyword(container.message)
    );
  });
});

const clearTableSelection = () => {
  selectedPods.value = [];
  nextTick(() => {
    tableRef.value?.clearSelection();
  });
};

const handleSelectionChange = (selection: PodItem[]) => {
  selectedPods.value = selection;
};

const formatCreationTime = (timestamp: string | null) => {
  if (!timestamp) return "-";
  try {
    const date = new Date(timestamp);
    if (Number.isNaN(date.getTime())) return timestamp;

    const formatPart = (value: number) => value.toString().padStart(2, "0");
    const year = formatPart(date.getFullYear() % 100);
    const month = formatPart(date.getMonth() + 1);
    const day = formatPart(date.getDate());
    const hours = formatPart(date.getHours());
    const minutes = formatPart(date.getMinutes());

    return `${year}/${month}/${day} ${hours}:${minutes}`;
  } catch (error) {
    return timestamp;
  }
};

const formatCpu = (value: number | null) => {
  if (value === null || value === undefined) {
    return "-";
  }
  const num = Number(value);
  return Number.isFinite(num) ? num.toFixed(3) : "-";
};

const formatMemory = (value: number | null) => {
  if (value === null || value === undefined) {
    return "-";
  }
  const num = Number(value);
  if (!Number.isFinite(num)) return "-";
  if (Number.isInteger(num)) return String(num);
  return Math.round(num).toString();
};

const getContainerEmoji = (container: PodContainerStatus) => {
  if (container.state === "Running" && container.ready) {
    return "🟢";
  }
  if (container.state === "Terminated") {
    return "⚫";
  }
  return "🟠";
};

const formatContainerInfo = (container: PodContainerStatus) => {
  try {
    return JSON.stringify(container, null, 2);
  } catch (error) {
    return String(container);
  }
};

const getSortedContainers = (containers: PodContainerStatus[] = []) => {
  return [...containers].sort((a, b) => {
    const aInit = a?.is_init ? 1 : 0;
    const bInit = b?.is_init ? 1 : 0;
    if (aInit !== bInit) {
      return aInit - bInit;
    }
    return (a?.name || "").localeCompare(b?.name || "");
  });
};

const getContainerCount = (containers: PodContainerStatus[] | undefined) => {
  return containers ? containers.length : 0;
};

const sortByCpu = (a: PodItem, b: PodItem) => {
  const cpuA = a.current_cpu_cores ?? 0;
  const cpuB = b.current_cpu_cores ?? 0;
  return cpuA - cpuB;
};

const sortByMemory = (a: PodItem, b: PodItem) => {
  const memA = a.current_memory_mb ?? 0;
  const memB = b.current_memory_mb ?? 0;
  return memA - memB;
};

const sortByRestart = (a: PodItem, b: PodItem) => {
  const restartA = a.restart_count ?? 0;
  const restartB = b.restart_count ?? 0;
  return restartA - restartB;
};

const sortByContainerCount = (a: PodItem, b: PodItem) => {
  return getContainerCount(a.containers) - getContainerCount(b.containers);
};

const sortByCreation = (a: PodItem, b: PodItem) => {
  const timeA = a.creation_timestamp
    ? new Date(a.creation_timestamp).getTime()
    : 0;
  const timeB = b.creation_timestamp
    ? new Date(b.creation_timestamp).getTime()
    : 0;
  return timeA - timeB;
};

type SortOrder = "ascending" | "descending" | null;

const sortField = ref<string>("");
const sortOrder = ref<SortOrder>(null);

const columnComparators: Record<string, (a: PodItem, b: PodItem) => number> = {
  current_cpu_cores: sortByCpu,
  current_memory_mb: sortByMemory,
  restart_count: sortByRestart,
  containers: sortByContainerCount,
  creation_timestamp: sortByCreation
};

const defaultComparator = (aValue: unknown, bValue: unknown) => {
  if (aValue === bValue) return 0;
  if (aValue === undefined || aValue === null) return -1;
  if (bValue === undefined || bValue === null) return 1;

  if (typeof aValue === "number" && typeof bValue === "number") {
    return aValue - bValue;
  }

  const numA = Number(aValue);
  const numB = Number(bValue);
  if (!Number.isNaN(numA) && !Number.isNaN(numB)) {
    return numA - numB;
  }

  return String(aValue).localeCompare(String(bValue));
};

const sortedTableData = computed(() => {
  const data = [...filteredTableData.value];
  if (!sortField.value || !sortOrder.value) {
    return data;
  }

  const comparator = columnComparators[sortField.value]
    ? columnComparators[sortField.value]
    : (a: PodItem, b: PodItem) =>
        defaultComparator(
          (a as Record<string, unknown>)[sortField.value],
          (b as Record<string, unknown>)[sortField.value]
        );

  const direction = sortOrder.value === "ascending" ? 1 : -1;
  return data.sort((a, b) => comparator(a, b) * direction);
});

const paginatedTableData = computed(() => {
  const start = (currentPage.value - 1) * pageSize.value;
  const end = start + pageSize.value;
  return sortedTableData.value.slice(start, end);
});

watch(pageSize, () => {
  resetPagination();
  clearTableSelection();
});

watch(
  () => filteredTableData.value.length,
  total => {
    const maxPage = total > 0 ? Math.ceil(total / pageSize.value) : 1;
    if (currentPage.value > maxPage) {
      currentPage.value = maxPage;
    }
  }
);

const handlePageSizeChange = (value: number) => {
  pageSize.value = value;
};

const handlePageChange = (value: number) => {
  currentPage.value = value;
  clearTableSelection();
};

const handleSortChange = ({
  prop,
  order
}: {
  prop?: string;
  order: SortOrder;
}) => {
  sortOrder.value = order;
  sortField.value = order ? prop || "" : "";
  resetPagination();
};

const clearSortState = () => {
  sortField.value = "";
  sortOrder.value = null;
  nextTick(() => {
    tableRef.value?.clearSort?.();
  });
};

const getCpuClass = (value: number | null) => {
  if (value === null || value === undefined) return "";
  if (value >= 1.6) return "cpu-high";
  if (value >= 0.8) return "cpu-mid";
  return "cpu-low";
};

const getMemoryClass = (value: number | null) => {
  if (value === null || value === undefined) return "";
  if (value >= 2500) return "memory-high";
  if (value >= 1500) return "memory-mid";
  return "memory-low";
};

const getRestartClass = (value: number | null) => {
  if (!value) return "";
  return "restart-warning";
};

const extractDeploymentName = (controlledBy: string | undefined | null) => {
  if (!controlledBy) return "";
  const parts = controlledBy.split("/");
  return parts.length > 1 ? parts[1] : parts[0];
};

const deriveSchedulerDeploymentName = (
  podName: string,
  controlledBy: string | undefined | null
) => {
  if (!podName) return "";
  const segments = podName.split("-");
  const controllerType = controlledBy
    ? controlledBy.split("/")[0]?.toLowerCase()
    : "";

  if (controllerType === "replicaset") {
    return segments.length >= 3 ? segments.slice(0, -2).join("-") : podName;
  }

  return segments.length >= 2 ? segments.slice(0, -1).join("-") : podName;
};

const refreshPods = async () => {
  lastFetchedEnv.value = null;
  lastFetchedNamespace.value = null;
  await fetchPods();
};

const showResultDialog = (message: string, operation: string = "") => {
  resultMessage.value = `<div style="white-space: pre-wrap; word-break: break-all;">${message}</div>`;
  currentOperation.value = operation;
  resultDialogVisible.value = true;
};

const handleCopyAndClose = () => {
  resultDialogVisible.value = false;
};

const handleModifyPod = async (
  env: string,
  namespace: string,
  pod: string,
  deployment: string,
  controlledBy?: string | null
) => {
  try {
    const derivedDeployment = deriveSchedulerDeploymentName(pod, controlledBy);
    const effectiveDeployment = derivedDeployment || deployment || "";

    currentPodInfo.value = {
      name: pod,
      env: env,
      namespace: namespace,
      deployment: effectiveDeployment,
      originalDeployment: deployment || "",
      controlledBy: controlledBy || ""
    };

    const scalePodRef = ref(false);
    const addLabelRef = ref(false);
    const shouldShowAddLabel = ref(false);
    const scaleStrategyRef = ref("cpu");
    const schedulerRef = ref(false);
    const resourceTypeRef = ref("cpu");
    const nodeListRef = ref([]);
    const selectedNodesRef = ref([]);

    try {
      const labelResult = await showAddLabel(env, namespace);
      shouldShowAddLabel.value =
        labelResult.data && labelResult.data.length > 0;
      if (shouldShowAddLabel.value) {
        addLabelRef.value = true;
      }
    } catch (error) {
      console.error("检查固定节点均衡模式失败:", error);
      shouldShowAddLabel.value = false;
    }

    const fetchNodeResources = async (
      resourceType: string,
      namespace: string,
      deployment: string
    ) => {
      try {
        const result = await getNodeResourceRank(
          env,
          resourceType,
          namespace,
          deployment
        );
        if (result.success && result.data) {
          nodeListRef.value = result.data;
          selectedNodesRef.value = [];
          const nodeListContainer =
            document.getElementById("nodeListContainer");
          if (nodeListContainer) {
            nodeListContainer.style.display = "block";
            renderNodeList();
          }
        }
      } catch (error) {
        console.error("获取节点资源信息失败:", error);
        ElMessage.error("获取节点资源信息失败");
      }
    };

    const renderNodeList = () => {
      const nodeListContainer = document.getElementById("nodeListContainer");
      if (!nodeListContainer) return;

      nodeListContainer.innerHTML = "";

      nodeListRef.value.forEach((node: any) => {
        const nodeItem = document.createElement("div");
        nodeItem.style.cssText =
          "display: flex; align-items: center; margin-bottom: 8px; padding: 8px; border: 1px solid #e4e7ed; border-radius: 4px;";

        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.style.marginRight = "8px";
        checkbox.addEventListener("change", e => {
          const target = e.target as HTMLInputElement;
          if (target.checked) {
            if (!selectedNodesRef.value.includes(node.name)) {
              selectedNodesRef.value.push(node.name);
            }
          } else {
            const index = selectedNodesRef.value.indexOf(node.name);
            if (index > -1) {
              selectedNodesRef.value.splice(index, 1);
            }
          }
        });

        const label = document.createElement("span");
        const percentText =
          resourceTypeRef.value === "pod" ? node.percent : `${node.percent}%`;
        const cpodNumColor = node.cpod_num !== 0 ? "color: red;" : "";
        label.innerHTML = `${node.name} (<span style="color: blue;">${percentText}</span>，<span style="${cpodNumColor}">${node.cpod_num}Pod</span>)`;

        nodeItem.appendChild(checkbox);
        nodeItem.appendChild(label);
        nodeListContainer.appendChild(nodeItem);
      });
    };

    const schedulerContainer = h(
      "div",
      {
        id: "schedulerContainer",
        style:
          "display: none; margin-left: 24px; flex-direction: column; margin-bottom: 12px;"
      },
      [
        h(
          "div",
          { style: "margin-bottom: 8px; font-size: 14px; color: #606266;" },
          "资源类型:"
        ),
        h(
          "div",
          {
            style:
              "display: flex; gap: 12px; margin-bottom: 12px; flex-wrap: wrap;"
          },
          [
            h(
              "label",
              {
                style:
                  "display: flex; align-items: center; cursor: pointer; font-size: 12px;"
              },
              [
                h("input", {
                  type: "radio",
                  name: "resourceType",
                  value: "cpu",
                  style: "margin-right: 4px;",
                  onChange: () => {
                    resourceTypeRef.value = "cpu";
                    fetchNodeResources("cpu", namespace, effectiveDeployment);
                  }
                }),
                h("span", "当前CPU")
              ]
            ),
            h(
              "label",
              {
                style:
                  "display: flex; align-items: center; cursor: pointer; font-size: 12px;"
              },
              [
                h("input", {
                  type: "radio",
                  name: "resourceType",
                  value: "mem",
                  style: "margin-right: 4px;",
                  onChange: () => {
                    resourceTypeRef.value = "mem";
                    fetchNodeResources("mem", namespace, effectiveDeployment);
                  }
                }),
                h("span", "当前内存")
              ]
            ),
            h(
              "label",
              {
                style:
                  "display: flex; align-items: center; cursor: pointer; font-size: 12px;"
              },
              [
                h("input", {
                  type: "radio",
                  name: "resourceType",
                  value: "peak_cpu",
                  style: "margin-right: 4px;",
                  onChange: () => {
                    resourceTypeRef.value = "peak_cpu";
                    fetchNodeResources(
                      "peak_cpu",
                      namespace,
                      effectiveDeployment
                    );
                  }
                }),
                h("span", "峰值CPU")
              ]
            ),
            h(
              "label",
              {
                style:
                  "display: flex; align-items: center; cursor: pointer; font-size: 12px;"
              },
              [
                h("input", {
                  type: "radio",
                  name: "resourceType",
                  value: "peak_mem",
                  style: "margin-right: 4px;",
                  onChange: () => {
                    resourceTypeRef.value = "peak_mem";
                    fetchNodeResources(
                      "peak_mem",
                      namespace,
                      effectiveDeployment
                    );
                  }
                }),
                h("span", "峰值内存")
              ]
            ),
            h(
              "label",
              {
                style:
                  "display: flex; align-items: center; cursor: pointer; font-size: 12px;"
              },
              [
                h("input", {
                  type: "radio",
                  name: "resourceType",
                  value: "pod",
                  style: "margin-right: 4px;",
                  onChange: () => {
                    resourceTypeRef.value = "pod";
                    fetchNodeResources("pod", namespace, effectiveDeployment);
                  }
                }),
                h("span", "Pod数")
              ]
            )
          ]
        ),
        h("div", {
          id: "nodeListContainer",
          style:
            "display: none; max-height: 200px; overflow-y: auto; border: 1px solid #e4e7ed; border-radius: 4px; padding: 8px;"
        })
      ]
    );

    const addLabelContainer = h(
      "div",
      {
        id: "addLabelContainer",
        style: "display: none; margin-left: 24px; flex-direction: column;"
      },
      [
        h(
          "div",
          { style: "margin-bottom: 8px; display: flex; align-items: center;" },
          [
            h("input", {
              type: "checkbox",
              id: "addLabelCheckbox",
              checked: true,
              style: "margin-right: 8px;",
              onChange: (e: Event) => {
                addLabelRef.value = (e.target as HTMLInputElement).checked;
                const strategyContainer =
                  document.getElementById("strategyContainer");
                if (strategyContainer) {
                  strategyContainer.style.display = addLabelRef.value
                    ? "block"
                    : "none";
                }
              }
            }),
            h(
              "label",
              { for: "addLabelCheckbox", style: "color: #f56c6c;" },
              "已开启固定节点均衡模式"
            )
          ]
        ),
        h(
          "div",
          {
            id: "strategyContainer",
            style: "margin-left: 0px; margin-top: 8px; display: block;"
          },
          [
            h(
              "div",
              { style: "margin-bottom: 4px; font-size: 14px; color: #606266;" },
              "扩缩容策略:"
            ),
            h("div", { style: "display: flex; gap: 16px;" }, [
              h(
                "label",
                {
                  style: "display: flex; align-items: center; cursor: pointer;"
                },
                [
                  h("input", {
                    type: "radio",
                    name: "scaleStrategy",
                    value: "cpu",
                    checked: true,
                    style: "margin-right: 4px;",
                    onChange: () => {
                      scaleStrategyRef.value = "cpu";
                    }
                  }),
                  h("span", "节点CPU")
                ]
              ),
              h(
                "label",
                {
                  style: "display: flex; align-items: center; cursor: pointer;"
                },
                [
                  h("input", {
                    type: "radio",
                    name: "scaleStrategy",
                    value: "mem",
                    style: "margin-right: 4px;",
                    onChange: () => {
                      scaleStrategyRef.value = "mem";
                    }
                  }),
                  h("span", "节点内存")
                ]
              )
            ])
          ]
        )
      ]
    );

    const messageElements = [
      h(
        "p",
        { style: "margin-bottom: 16px; margin-left: 24px;" },
        "确认要隔离该Pod吗？"
      ),
      h(
        "div",
        {
          style:
            "display: flex; align-items: center; margin-bottom: 12px; gap: 20px; margin-left: 24px;"
        },
        [
          h("div", { style: "display: flex; align-items: center;" }, [
            h("input", {
              type: "checkbox",
              id: "schedulerCheckbox",
              style: "margin-right: 8px;",
              onChange: (e: Event) => {
                schedulerRef.value = (e.target as HTMLInputElement).checked;
                resourceTypeRef.value = "";
                nodeListRef.value = [];
                selectedNodesRef.value = [];
                const radioButtons = document.querySelectorAll(
                  'input[name="resourceType"]'
                );
                radioButtons.forEach((radio: any) => {
                  radio.checked = false;
                });
                const nodeListContainer =
                  document.getElementById("nodeListContainer");
                if (nodeListContainer) {
                  nodeListContainer.style.display = "none";
                }
                const container = document.getElementById("schedulerContainer");
                if (container) {
                  container.style.display = schedulerRef.value
                    ? "flex"
                    : "none";
                }
              }
            }),
            h("label", { for: "schedulerCheckbox" }, "调度到指定节点")
          ])
        ]
      ),
      schedulerContainer,
      addLabelContainer
    ];

    await ElMessageBox({
      title: "提示",
      message: h("div", messageElements),
      confirmButtonText: "确定",
      cancelButtonText: "取消",
      showCancelButton: true,
      customStyle: {
        width: "600px"
      }
    });

    const params: any = {
      env: env,
      ns: namespace,
      pod_name: pod
    };

    if (schedulerRef.value) {
      params.scheduler = true;
    }

    if (scalePodRef.value) {
      params.scale_pod = true;
      if (shouldShowAddLabel.value && addLabelRef.value) {
        params.add_label = true;
        params.type = scaleStrategyRef.value;
      }
    }

    const bodyData: any = {};
    if (schedulerRef.value && selectedNodesRef.value.length > 0) {
      bodyData.node_scheduler = selectedNodesRef.value;
    }

    const res = await modifyPod(params, bodyData);
    if (res.success) {
      ElMessage.success("操作成功");
      showResultDialog(res.message, "modify");
      await refreshPods();
    } else {
      ElMessage.error("操作失败");
    }
  } catch (error) {
    console.error(error);
  }
};

const handleBatchDelete = async () => {
  if (!searchForm.env) {
    ElMessage.warning("请选择K8S环境");
    return;
  }

  if (!selectedPods.value.length) {
    ElMessage.warning("请先选择需要删除的Pod");
    return;
  }

  const podsToDelete = selectedPods.value.map(item => ({
    pod_name: item.name,
    ns: item.namespace
  }));

  try {
    await ElMessageBox.confirm(
      `确认要删除选中的 ${podsToDelete.length} 个Pod吗？`,
      "提示",
      {
        confirmButtonText: "确定",
        cancelButtonText: "取消",
        type: "warning"
      }
    );
  } catch {
    return;
  }

  batchDeleting.value = true;
  try {
    const payload: DeletePodsPayload = {
      pods: podsToDelete
    };
    const res = await deletePodsBatch(searchForm.env, payload);
    if (res.success) {
      ElMessage.success(res.message || "批量删除Pod成功");
      clearTableSelection();
      await refreshPods();
    } else {
      ElMessage.error(res.message || "批量删除Pod失败");
    }
  } catch (error) {
    console.error("批量删除Pod失败:", error);
    ElMessage.error("批量删除Pod失败");
  } finally {
    batchDeleting.value = false;
  }
};

const handleDeletePod = async (env: string, namespace: string, pod: string) => {
  try {
    await ElMessageBox.confirm("确认要删除该Pod吗？", "提示", {
      confirmButtonText: "确定",
      cancelButtonText: "取消",
      type: "warning"
    });
    const res = await deletePod({
      env: env,
      ns: namespace,
      pod_name: pod
    });
    if (res.success) {
      ElMessage.success("操作成功");
      await refreshPods();
    } else {
      ElMessage.error("操作失败");
    }
  } catch (error) {
    console.error(error);
  }
};

const handleAutoDump = async (env: string, namespace: string, pod: string) => {
  try {
    await ElMessageBox.confirm("确认要对该Pod执行Dump吗？", "提示", {
      confirmButtonText: "确定",
      cancelButtonText: "取消",
      type: "warning"
    });
    const res = await autoDump({
      env: env,
      ns: namespace,
      pod_name: pod
    });
    if (res.success) {
      showResultDialog(res.message, "dump");
      ElMessage.success("操作成功");
    } else {
      ElMessage.error("操作失败");
    }
  } catch (error) {
    console.error(error);
  }
};

const handleAutoJstack = async (
  env: string,
  namespace: string,
  pod: string
) => {
  try {
    await ElMessageBox.confirm("确认要对该Pod执行Jstack吗？", "提示", {
      confirmButtonText: "确定",
      cancelButtonText: "取消",
      type: "warning"
    });
    const res = await autoJstack({
      env: env,
      ns: namespace,
      pod_name: pod
    });
    if (res.success) {
      showResultDialog(res.message, "jstack");
      ElMessage.success("操作成功");
    } else {
      ElMessage.error("操作失败");
    }
  } catch (error) {
    console.error(error);
  }
};

const handleAutoJfr = async (env: string, namespace: string, pod: string) => {
  try {
    await ElMessageBox.confirm("确认要对该Pod执行JFR吗？", "提示", {
      confirmButtonText: "确定",
      cancelButtonText: "取消",
      type: "warning"
    });
    const res = await autoJfr({
      env: env,
      ns: namespace,
      pod_name: pod
    });
    if (res.success) {
      showResultDialog(res.message, "jfr");
      ElMessage.success("操作成功");
    } else {
      ElMessage.error("操作失败");
    }
  } catch (error) {
    console.error(error);
  }
};

const handleAutoJvmMem = async (
  env: string,
  namespace: string,
  pod: string
) => {
  try {
    await ElMessageBox.confirm("确认要对该Pod执行JVM吗？", "提示", {
      confirmButtonText: "确定",
      cancelButtonText: "取消",
      type: "warning"
    });
    const res = await autoJvmMem({
      env: env,
      ns: namespace,
      pod_name: pod
    });
    if (res.success) {
      showResultDialog(res.message, "jvm");
      ElMessage.success("操作成功");
    } else {
      ElMessage.error("操作失败");
    }
  } catch (error) {
    console.error(error);
  }
};

const handleViewLogs = async (
  env: string,
  namespace: string,
  pod: string,
  deployment: string
) => {
  currentPodInfo.value = {
    name: pod,
    env,
    namespace,
    deployment,
    originalDeployment: deployment,
    controlledBy: ""
  };

  logMessages.value = [];
  searchMatches.value = [];
  currentMatchIndex.value = -1;
  totalMatches.value = 0;
  searchKeyword.value = "";
  isFilterMode.value = false;

  logDialogVisible.value = true;
  document.body.style.overflow = "hidden";
};

const ansiUp = new AnsiUp();
ansiUp.escape_html = true;
ansiUp.use_classes = false;

const convertAnsiToHtml = (message: string) => {
  return ansiUp.ansi_to_html(message);
};

const getLogKey = (message: string, index: number) => {
  if (isFilterMode.value) {
    return `${message.slice(0, 50)}-${index}`;
  }
  return index;
};

const getOriginalIndex = (message: string, filteredIndex: number) => {
  if (!isFilterMode.value) {
    return filteredIndex;
  }
  return logMessages.value.findIndex(item => item === message);
};

const getFilteredIndex = (originalIndex: number) => {
  if (!isFilterMode.value) {
    return originalIndex;
  }
  const targetMessage = logMessages.value[originalIndex];
  return filteredLogMessages.value.findIndex(msg => msg === targetMessage);
};

const highlightSearchKeyword = (message: string, originalIndex: number) => {
  let processedMessage = convertAnsiToHtml(message);

  if (!searchKeyword.value.trim()) {
    return processedMessage;
  }

  const keyword = searchKeyword.value;
  const keywordRegex = new RegExp(
    `(${keyword.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")})`,
    "gi"
  );

  const currentMatch =
    currentMatchIndex.value >= 0 &&
    searchMatches.value[currentMatchIndex.value] === originalIndex;

  const highlightClass = currentMatch
    ? "search-highlight-current"
    : "search-highlight";

  if (!message.toLowerCase().includes(keyword.toLowerCase())) {
    return processedMessage;
  }

  return processedMessage.replace(
    keywordRegex,
    `<span class="${highlightClass}">$1</span>`
  );
};

const clearLogs = () => {
  logMessages.value = [];
  searchMatches.value = [];
  currentMatchIndex.value = -1;
  totalMatches.value = 0;
};

const startLogStream = () => {
  if (!currentPodInfo.value.env || !currentPodInfo.value.namespace) {
    ElMessage.warning("缺少Pod信息，无法建立日志连接");
    return;
  }

  if (logSocket.value) {
    logSocket.value.close();
    logSocket.value = null;
  }

  logConnecting.value = true;
  isLogConnected.value = false;
  logMessages.value = [];

  const wsUrl = createPodLogStreamUrl(
    currentPodInfo.value.env,
    currentPodInfo.value.namespace,
    currentPodInfo.value.name
  );

  try {
    logSocket.value = new WebSocket(wsUrl);
  } catch (error) {
    console.error("创建WebSocket失败:", error);
    logConnecting.value = false;
    ElMessage.error("日志连接失败");
    return;
  }

  logSocket.value.onopen = () => {
    logConnecting.value = false;
    isLogConnected.value = true;
    ElMessage.success("日志连接成功");
  };

  logSocket.value.onmessage = event => {
    if (event.data && event.data.trim()) {
      logMessages.value.push(event.data);
    }

    if (logMessages.value.length > 1500) {
      logMessages.value = logMessages.value.slice(-1200);
    }

    nextTick(() => {
      if (!isUserScrolling.value || isAtBottom()) {
        scrollToBottom();
      }
    });
  };

  logSocket.value.onerror = error => {
    console.error("WebSocket错误:", error);
    logConnecting.value = false;
    isLogConnected.value = false;
    ElMessage.error("日志连接失败");
  };

  logSocket.value.onclose = () => {
    logConnecting.value = false;
    isLogConnected.value = false;
  };
};

const stopLogStream = () => {
  if (logSocket.value) {
    logSocket.value.close();
    logSocket.value = null;
  }
  isLogConnected.value = false;
};

const scrollToBottom = () => {
  if (logContentRef.value) {
    logContentRef.value.scrollTop = logContentRef.value.scrollHeight;
    isUserScrolling.value = false;
  }
};

const isAtBottom = () => {
  if (!logContentRef.value) return false;
  const { scrollTop, scrollHeight, clientHeight } = logContentRef.value;
  return scrollTop + clientHeight >= scrollHeight - 10;
};

const handleScroll = () => {
  if (!logContentRef.value) return;
  isUserScrolling.value = !isAtBottom();
};

const scrollToMatch = (originalIndex: number) => {
  if (!logContentRef.value || originalIndex < 0) return;

  const domIndex = isFilterMode.value
    ? getFilteredIndex(originalIndex)
    : originalIndex;

  if (domIndex < 0) return;

  const logLines = logContentRef.value.querySelectorAll(".log-line");
  const targetElement = logLines[domIndex] as HTMLElement | undefined;
  if (!targetElement) return;

  const containerHeight = logContentRef.value.clientHeight;
  const elementTop = targetElement.offsetTop;
  const elementHeight = targetElement.offsetHeight;
  const scrollTop = elementTop - containerHeight / 2 + elementHeight / 2;

  logContentRef.value.scrollTo({
    top: Math.max(0, scrollTop),
    behavior: "smooth"
  });
};

const performSearch = (forceFirstMatch = false) => {
  if (!searchKeyword.value.trim()) {
    searchMatches.value = [];
    currentMatchIndex.value = -1;
    totalMatches.value = 0;
    return;
  }

  const keyword = searchKeyword.value.toLowerCase();

  let currentMatchContent = "";
  if (currentMatchIndex.value >= 0 && searchMatches.value.length > 0) {
    const currentLineIndex = searchMatches.value[currentMatchIndex.value];
    if (currentLineIndex < logMessages.value.length) {
      currentMatchContent = logMessages.value[currentLineIndex];
    }
  }

  const matches: number[] = [];
  logMessages.value.forEach((message, index) => {
    if (message.toLowerCase().includes(keyword)) {
      matches.push(index);
    }
  });

  searchMatches.value = matches;
  totalMatches.value = matches.length;

  if (matches.length === 0) {
    currentMatchIndex.value = -1;
    return;
  }

  let newMatchIndex = 0;
  if (forceFirstMatch) {
    newMatchIndex = 0;
  } else if (currentMatchContent) {
    const sameContentIndex = matches.findIndex(
      index => logMessages.value[index] === currentMatchContent
    );
    if (sameContentIndex >= 0) {
      newMatchIndex = sameContentIndex;
    } else {
      const prevIndex =
        searchMatches.value[currentMatchIndex.value] ?? matches[0];
      let closestIdx = 0;
      let minDistance = Math.abs(matches[0] - prevIndex);
      for (let i = 1; i < matches.length; i++) {
        const distance = Math.abs(matches[i] - prevIndex);
        if (distance < minDistance) {
          minDistance = distance;
          closestIdx = i;
        }
      }
      newMatchIndex = closestIdx;
    }
  }

  currentMatchIndex.value = newMatchIndex;
  scrollToMatch(matches[newMatchIndex]);
};

const goToPreviousMatch = () => {
  if (searchMatches.value.length === 0) return;
  currentMatchIndex.value =
    (currentMatchIndex.value - 1 + searchMatches.value.length) %
    searchMatches.value.length;
  scrollToMatch(searchMatches.value[currentMatchIndex.value]);
};

const goToNextMatch = () => {
  if (searchMatches.value.length === 0) return;
  currentMatchIndex.value =
    (currentMatchIndex.value + 1) % searchMatches.value.length;
  scrollToMatch(searchMatches.value[currentMatchIndex.value]);
};

const toggleFilterMode = () => {
  if (!searchKeyword.value.trim()) return;
  isFilterMode.value = !isFilterMode.value;
  performSearch(true);
};

const getPreviousLogs = async () => {
  if (
    !currentPodInfo.value.name ||
    !currentPodInfo.value.env ||
    !currentPodInfo.value.namespace
  ) {
    ElMessage.warning("缺少Pod信息，无法获取重启前日志");
    return;
  }

  try {
    stopLogStream();
    clearLogs();
    logConnecting.value = true;

    const data = await getPodPreviousLogs(
      currentPodInfo.value.env,
      currentPodInfo.value.namespace,
      currentPodInfo.value.name,
      400
    );

    if (data.success && data.message) {
      logMessages.value = data.message
        .split("\n")
        .filter(line => line.trim() !== "");
      ElMessage.success("重启前日志获取成功");
      nextTick(() => scrollToBottom());
    } else {
      ElMessage.warning(data.message || "获取重启前日志失败");
    }
  } catch (error: any) {
    console.error("获取重启前日志失败:", error);
    ElMessage.error(`获取重启前日志失败: ${error?.message || error}`);
  } finally {
    logConnecting.value = false;
  }
};

const closeLogDialog = () => {
  stopLogStream();
  clearLogs();
  searchKeyword.value = "";
  isFilterMode.value = false;
  const container = logContentRef.value;
  if (container) {
    container.removeEventListener("scroll", handleScroll);
  }
  document.body.style.overflow = "";
};

const getStatusTagType = (status: string) => {
  const normalized = status?.toLowerCase();
  if (normalized === "running" || normalized === "succeeded") return "success";
  if (normalized === "pending" || normalized === "unknown") return "warning";
  return "danger";
};

const initCreateYamlEditor = async () => {
  if (!createYamlEditorRef.value) return;
  if (createYamlEditor) {
    createYamlEditor.dispose();
  }
  createYamlEditor = monaco.editor.create(createYamlEditorRef.value, {
    value: "",
    language: "yaml",
    theme: "vs-dark",
    automaticLayout: true,
    minimap: { enabled: false },
    scrollBeyondLastLine: false,
    wordWrap: "on",
    fontSize: 14,
    lineNumbers: "on",
    folding: true,
    selectOnLineNumbers: true,
    roundedSelection: false,
    readOnly: false,
    cursorStyle: "line"
  });
};

const handleCreate = async () => {
  if (!searchForm.env) {
    ElMessage.warning("请选择K8S环境");
    return;
  }
  createSaveLoading.value = false;
  createUpdateMethodDialogVisible.value = false;
  selectedCreateUpdateMethod.value = "create";
  createDialogVisible.value = true;
  await nextTick();
  await initCreateYamlEditor();
  if (createYamlEditor) {
    createYamlEditor.setValue("");
  }
};

const handleCreateSave = async () => {
  if (!createYamlEditor) {
    ElMessage.error("编辑器未初始化");
    return;
  }

  const yamlContent = createYamlEditor.getValue();
  if (!yamlContent.trim()) {
    ElMessage.error("YAML内容不能为空");
    return;
  }

  try {
    yaml.load(yamlContent);
    selectedCreateUpdateMethod.value = "create";
    createUpdateMethodDialogVisible.value = true;
  } catch (error) {
    console.error("YAML格式验证失败:", error);
    if (error instanceof YAMLException) {
      ElMessage.error(`YAML格式错误: ${error.message}`);
    } else {
      ElMessage.error("YAML格式验证失败");
    }
  }
};

const confirmCreateUpdate = async () => {
  if (!createYamlEditor) {
    ElMessage.error("编辑器未初始化");
    return;
  }
  if (!searchForm.env) {
    ElMessage.warning("请选择K8S环境");
    return;
  }

  const yamlContent = createYamlEditor.getValue();

  try {
    createSaveLoading.value = true;
    const res = await updateServiceContent(
      searchForm.env,
      selectedCreateUpdateMethod.value,
      yamlContent
    );

    if (res.success) {
      ElMessage.success(`Pod ${selectedCreateUpdateMethod.value} 提交成功`);
      createUpdateMethodDialogVisible.value = false;
      createDialogVisible.value = false;
      await fetchPods();
    } else {
      ElMessage.error(
        res.message || `Pod ${selectedCreateUpdateMethod.value} 提交失败`
      );
    }
  } catch (error) {
    console.error("提交Pod失败:", error);
    ElMessage.error("提交Pod失败");
  } finally {
    createSaveLoading.value = false;
  }
};

const handleEnvChange = async (value: string) => {
  searchForm.env = value;
  searchStore.setEnv(value || "");
  searchForm.namespace = "";
  searchStore.setNamespace("");
  searchForm.keyword = "";
  appliedKeyword.value = "";
  tableData.value = [];
  lastFetchedEnv.value = null;
  lastFetchedNamespace.value = null;
  resetPagination();
  clearSortState();
  clearTableSelection();
  await fetchNamespaceOptions(value);
};

const handleNamespaceChange = (value: string) => {
  searchForm.namespace = value || "";
  searchStore.setNamespace(searchForm.namespace);
  tableData.value = [];
  lastFetchedNamespace.value = null;
  resetPagination();
  clearSortState();
  clearTableSelection();
};

const handleQuery = async () => {
  appliedKeyword.value = searchForm.keyword.trim();
  resetPagination();
  const normalizedNamespace = searchForm.namespace || "";
  const shouldFetch =
    lastFetchedEnv.value !== searchForm.env ||
    lastFetchedNamespace.value !== normalizedNamespace ||
    tableData.value.length === 0;

  if (shouldFetch) {
    await fetchPods();
  }
};

const handleRefresh = async () => {
  await fetchPods();
};

const fetchEnvOptions = async () => {
  try {
    const res = await getAgentNames();
    const options = res.data || [];
    envOptions.value = options;
    if (!options.length) {
      searchForm.env = "";
      searchStore.setEnv("");
      nsOptions.value = [];
      searchForm.namespace = "";
      searchStore.setNamespace("");
      return;
    }

    if (!options.includes(searchForm.env)) {
      searchForm.env = options[0];
      searchStore.setEnv(searchForm.env);
    }

    await fetchNamespaceOptions(searchForm.env);
  } catch (error) {
    console.error("获取K8S环境列表失败:", error);
    ElMessage.error("获取K8S环境列表失败");
  }
};

const fetchNamespaceOptions = async (env: string) => {
  if (!env) {
    nsOptions.value = [];
    searchForm.namespace = "";
    searchStore.setNamespace("");
    return;
  }
  try {
    const res = await getPromNamespace(env);
    const options = res.data || [];
    nsOptions.value = options;

    if (!options.length) {
      searchForm.namespace = "";
      searchStore.setNamespace("");
      return;
    }

    if (!options.includes(searchForm.namespace)) {
      if (searchStore.namespace && options.includes(searchStore.namespace)) {
        searchForm.namespace = searchStore.namespace;
      } else {
        searchForm.namespace = options[0];
      }
    }

    searchStore.setNamespace(searchForm.namespace || "");
  } catch (error) {
    console.error("获取命名空间列表失败:", error);
    ElMessage.error("获取命名空间列表失败");
    nsOptions.value = [];
    searchForm.namespace = "";
    searchStore.setNamespace("");
  }
};

const fetchPods = async () => {
  if (!searchForm.env) {
    ElMessage.warning("请选择K8S环境");
    return;
  }
  loading.value = true;
  try {
    const res = await getPodList(
      searchForm.env,
      searchForm.namespace || undefined
    );
    tableData.value = res.data || [];
    lastFetchedEnv.value = searchForm.env;
    lastFetchedNamespace.value = searchForm.namespace || "";
    clearTableSelection();
  } catch (error) {
    console.error("获取Pod列表失败:", error);
    ElMessage.error("获取Pod列表失败");
  } finally {
    loading.value = false;
  }
};

onMounted(async () => {
  await fetchEnvOptions();
});

watch(createDialogVisible, visible => {
  if (!visible && createYamlEditor) {
    createYamlEditor.dispose();
    createYamlEditor = null;
  }
  if (!visible) {
    createSaveLoading.value = false;
    createUpdateMethodDialogVisible.value = false;
  }
});

watch(logDialogVisible, visible => {
  if (visible) {
    nextTick(() => {
      if (logContentRef.value) {
        logContentRef.value.addEventListener("scroll", handleScroll);
      }
    });
  } else {
    closeLogDialog();
  }
});

watch(
  logMessages,
  () => {
    if (searchKeyword.value.trim()) {
      performSearch();
    }
  },
  { deep: true }
);

onBeforeUnmount(() => {
  closeLogDialog();
});
</script>

<style scoped>
.pod-manager-container {
  padding: 1px;
}

.search-section {
  margin-bottom: 2px;
}

.query-form {
  display: flex;
  align-items: center;
  flex-wrap: nowrap;
  width: 100%;
}

.query-form .el-form-item.right-auto {
  margin-left: auto;
}

.mt-2 {
  margin-top: 8px;
}

.containers-cell {
  display: flex;
  flex-direction: column;
}

.container-emojis {
  flex-wrap: wrap;
  gap: 8px;
}

.text-disabled {
  color: #c0c4cc;
}

.container-status-emoji {
  font-size: 14px;
  line-height: 1;
  cursor: default;
  display: inline-flex;
  align-items: center;
}

.container-tooltip {
  margin: 0;
  font-size: 12px;
  line-height: 1.4;
  max-width: 360px;
  white-space: pre-wrap;
}

.numeric-cell {
  font-weight: 600;
  color: #303133;
}

.cpu-low {
  color: #409eff;
}

.cpu-mid {
  color: #e6a23c;
}

.cpu-high {
  color: #f56c6c;
}

.memory-low {
  color: #409eff;
}

.memory-mid {
  color: #e6a23c;
}

.memory-high {
  color: #f56c6c;
}

.restart-warning {
  color: #f56c6c;
}

.status-tooltip {
  font-size: 12px;
  line-height: 1.4;
}

.edit-container {
  height: 82vh;
  display: flex;
  flex-direction: column;
}

.yaml-editor-container {
  flex: 1;
  display: flex;
  flex-direction: column;
  border: 1px solid #dcdfe6;
  border-radius: 4px;
  overflow: hidden;
}

.editor-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px 16px;
  background-color: #f5f7fa;
  border-bottom: 1px solid #dcdfe6;
}

.editor-title {
  font-size: 16px;
  font-weight: bold;
  color: #303133;
}

.yaml-editor {
  flex: 1;
  min-height: 0;
}

.create-yaml-editor {
  height: calc(100vh - 120px);
}

.update-method-container {
  padding: 20px 0;
}

.method-radio-row {
  display: flex;
  flex-direction: row;
  gap: 24px;
}

.method-description {
  margin-top: 20px;
  padding: 12px;
  background-color: #f5f7fa;
  border-radius: 4px;
  border-left: 4px solid #409eff;
}

.method-description p {
  margin: 0;
  color: #606266;
  font-size: 14px;
  line-height: 1.5;
}

.log-container {
  display: flex;
  flex-direction: column;
  height: 92vh;
}

.log-dialog-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  min-height: 28px;
  padding: 4px 0;
  margin: 0;
}

.dialog-title {
  font-size: 12px;
  font-weight: 600;
  color: #303133;
}

.log-controls-header {
  display: flex;
  gap: 4px;
  align-items: center;
  flex-wrap: wrap;
}

.log-controls-header .el-button {
  height: 24px !important;
  padding: 4px 8px !important;
  font-size: 11px !important;
}

.log-status {
  display: flex;
  align-items: center;
  margin-left: 12px;
  gap: 6px;
}

.status-connected {
  color: #67c23a;
  font-weight: 600;
}

.status-disconnected {
  color: #f56c6c;
  font-weight: 600;
}

.log-search-container {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.search-info {
  font-size: 12px;
  color: #909399;
}

.log-content {
  flex: 1;
  padding: 16px;
  overflow-y: auto;
  font-family: Consolas, Monaco, "Courier New", monospace;
  font-size: 13px;
  line-height: 1.4;
  color: #d4d4d4;
  word-break: break-all;
  white-space: pre-wrap;
  background-color: #1e1e1e;
  border-radius: 4px;
}

.no-logs {
  padding: 40px 0;
  font-size: 14px;
  color: #909399;
  text-align: center;
}

.log-line {
  padding: 2px 4px;
  margin-bottom: 2px;
}

.log-error {
  color: #f56c6c;
  background-color: rgb(245 108 108 / 10%);
  border-radius: 2px;
}

.log-warn {
  color: #e6a23c;
  background-color: rgb(230 162 60 / 10%);
  border-radius: 2px;
}

.log-info {
  color: #409eff;
}

.search-highlight {
  background: rgba(247, 186, 29, 0.4);
  color: #fff;
  padding: 0 2px;
  border-radius: 2px;
}

.search-highlight-current {
  background: rgba(64, 158, 255, 0.6);
  color: #fff;
  padding: 0 2px;
  border-radius: 2px;
  box-shadow: 0 0 0 1px rgba(64, 158, 255, 0.8);
}

.result-content {
  font-size: 13px;
  white-space: pre-wrap;
  word-break: break-all;
}

.table-pagination {
  display: flex;
  justify-content: flex-end;
  margin-top: 12px;
}
</style>
