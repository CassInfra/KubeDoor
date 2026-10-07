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
        </el-form-item>

        <el-form-item label="节点">
          <el-select
            v-model="searchForm.nodeName"
            placeholder="全部节点"
            class="!w-[180px]"
            filterable
            clearable
            :disabled="!searchForm.env"
            @change="handleNodeChange"
          >
            <el-option
              v-for="item in nodeOptions"
              :key="item"
              :label="item"
              :value="item"
            />
          </el-select>
        </el-form-item>

        <el-form-item label="命名空间">
          <div class="namespace-select-wrapper">
            <el-select
              v-model="searchForm.namespaces"
              placeholder="全部命名空间"
              class="!w-[220px]"
              filterable
              clearable
              multiple
              collapse-tags
              collapse-tags-tooltip
              :max-collapse-tags="2"
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
            <el-icon
              :class="[
                'namespace-refresh-icon',
                {
                  disabled: !searchForm.env || nsRefreshing,
                  'is-loading': nsRefreshing
                }
              ]"
              title="刷新命名空间"
              @click="handleNamespaceRefresh"
            >
              <Refresh />
            </el-icon>
          </div>
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
          :default-sort="tableDefaultSort"
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
          >
            <template #default="scope">
              <el-tag
                v-if="scope.row.namespace"
                type="primary"
                effect="plain"
                size="small"
              >
                {{ scope.row.namespace }}
              </el-tag>
              <span v-else>-</span>
            </template>
          </el-table-column>

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
            min-width="100"
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
            min-width="100"
            align="center"
            sortable="custom"
          >
            <template #default="scope">
              {{ formatCreationTime(scope.row.creation_timestamp) }}
            </template>
          </el-table-column>
          <el-table-column
            label="操作"
            min-width="130"
            align="center"
            fixed="right"
          >
            <template #default="podScope">
              <div class="operation-cell">
                <el-button
                  type="warning"
                  size="small"
                  :disabled="!searchForm.env"
                  plain
                  @click="handleViewEvents(podScope.row)"
                >
                  事件
                </el-button>
                <el-button
                  type="danger"
                  size="small"
                  :disabled="!searchForm.env"
                  plain
                  @click="handleViewAlarmDetail(podScope.row)"
                >
                  告警
                </el-button>
                <el-dropdown class="operation-dropdown">
                  <el-button type="primary" size="small"> 操作 </el-button>
                  <template #dropdown>
                    <el-dropdown-menu>
                      <el-dropdown-item
                        @click="
                          handleEditPod(
                            searchForm.env,
                            podScope.row.namespace,
                            podScope.row.name
                          )
                        "
                        >编辑</el-dropdown-item
                      >
                      <el-dropdown-item
                        @click="
                          handleViewLogs(
                            searchForm.env,
                            podScope.row.namespace,
                            podScope.row.name,
                            extractDeploymentName(podScope.row.controlled_by),
                            podScope.row.containers
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
              </div>
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

    <!-- Pod编辑对话框 -->
    <el-dialog
      v-model="editDialogVisible"
      title="编辑Pod"
      width="95%"
      top="2.5vh"
      :close-on-click-modal="false"
      destroy-on-close
    >
      <div class="edit-container">
        <div class="yaml-editor-container">
          <div class="editor-header">
            <span class="editor-title">Pod YAML配置</span>
            <el-button
              type="primary"
              :loading="editSaveLoading"
              @click="handleEditSave"
            >
              提交
            </el-button>
          </div>
          <div ref="editYamlEditorRef" class="yaml-editor" />
        </div>
      </div>
    </el-dialog>

    <!-- 编辑更新方式选择弹框 -->
    <el-dialog
      v-model="editUpdateMethodDialogVisible"
      title="选择更新方式"
      width="400px"
      :close-on-click-modal="false"
    >
      <div class="update-method-container">
        <el-radio-group v-model="selectedEditUpdateMethod">
          <el-radio value="apply">Apply - 应用配置（推荐）</el-radio>
          <el-radio value="replace">Replace - 替换配置</el-radio>
        </el-radio-group>
        <div class="method-description">
          <p v-if="selectedEditUpdateMethod === 'apply'">
            Apply方式会智能合并配置，保留现有的其他字段，适用于大部分场景。
          </p>
          <p v-if="selectedEditUpdateMethod === 'replace'">
            Replace方式会完全替换现有配置，请确保YAML包含所有必要字段。
          </p>
        </div>
      </div>
      <template #footer>
        <span class="dialog-footer">
          <el-button @click="editUpdateMethodDialogVisible = false"
            >取消</el-button
          >
          <el-button
            type="primary"
            :loading="editSaveLoading"
            @click="confirmEditUpdate"
          >
            确认更新
          </el-button>
        </span>
      </template>
    </el-dialog>

    <PodLogViewer ref="logViewerRef" />

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
import dayjs from "dayjs";
import { getAgentNames } from "@/api/istio";
import {
  getPromNamespace,
  showAddLabel,
  getNodeResourceRank
} from "@/api/monit";
import { getNodesList } from "@/api/node";
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
import { updateServiceContent, getServiceContent } from "@/api/service";
import { useSearchStoreHook } from "@/store/modules/search";
import PodLogViewer from "@/views/monit/components/PodLogViewer.vue";
import * as monaco from "monaco-editor";
import * as yaml from "js-yaml";
import { YAMLException } from "js-yaml";

defineOptions({
  name: "PodManager"
});

interface SearchForm {
  env: string;
  nodeName: string;
  namespaces: string[];
  keyword: string;
}

const searchStore = useSearchStoreHook();

const searchForm = reactive<SearchForm>({
  env: searchStore.env || "",
  nodeName: "",
  namespaces: searchStore.namespace ? [searchStore.namespace] : [],
  keyword: ""
});

const envOptions = ref<string[]>([]);
const nodeOptions = ref<string[]>([]);
const nsOptions = ref<string[]>([]);
const nsRefreshing = ref(false);
const tableData = ref<PodItem[]>([]);
const tableRef = ref<TableInstance>();
const selectedPods = ref<PodItem[]>([]);
const batchDeleting = ref(false);
const loading = ref(false);
const appliedKeyword = ref("");
const lastFetchedEnv = ref<string | null>(null);
const lastFetchedNamespace = ref<string | null>(null);
const pageSizeOptions = [50, 100, 200, 500, 1000];
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

// 编辑对话框相关
const editDialogVisible = ref(false);
const editSaveLoading = ref(false);
const editYamlEditorRef = ref<HTMLElement | null>(null);
let editYamlEditor: monaco.editor.IStandaloneCodeEditor | null = null;
const editUpdateMethodDialogVisible = ref(false);
const selectedEditUpdateMethod = ref<"apply" | "replace">("apply");
const currentEditPod = ref<{
  env: string;
  namespace: string;
  name: string;
} | null>(null);

// 日志查看：统一使用 PodLogViewer 组件
const logViewerRef = ref<InstanceType<typeof PodLogViewer> | null>(null);

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

const handleViewEvents = (pod: PodItem) => {
  if (!searchForm.env) {
    ElMessage.warning("请先选择K8S环境");
    return;
  }
  const endDate = dayjs();
  const startDate = endDate.subtract(7, "day");
  const params = new URLSearchParams({
    start_time: startDate.format("YYYY-MM-DD"),
    end_time: endDate.format("YYYY-MM-DD"),
    k8s: searchForm.env,
    limit: "50",
    namespace: pod.namespace,
    kind: "Pod",
    name: pod.name
  });
  window.open(`/#/alarm/events?${params.toString()}`, "_blank");
};

const handleViewAlarmDetail = (pod: PodItem) => {
  if (!searchForm.env) {
    ElMessage.warning("请先选择K8S环境");
    return;
  }
  const params = new URLSearchParams({
    timeRange: "7",
    env: searchForm.env,
    status: "firing",
    namespace: pod.namespace,
    pod: pod.name
  });
  window.open(`/#/alarm/detail?${params.toString()}`, "_blank");
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

const DEFAULT_SORT_FIELD = "creation_timestamp";
const DEFAULT_SORT_ORDER: Exclude<SortOrder, null> = "descending";

const tableDefaultSort = {
  prop: DEFAULT_SORT_FIELD,
  order: DEFAULT_SORT_ORDER
};

const sortField = ref<string>(DEFAULT_SORT_FIELD);
const sortOrder = ref<SortOrder>(DEFAULT_SORT_ORDER);

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
          (a as unknown as Record<string, unknown>)[sortField.value],
          (b as unknown as Record<string, unknown>)[sortField.value]
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
  sortField.value = DEFAULT_SORT_FIELD;
  sortOrder.value = DEFAULT_SORT_ORDER;
  nextTick(() => {
    tableRef.value?.sort?.(DEFAULT_SORT_FIELD, DEFAULT_SORT_ORDER);
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

const refreshPods = async (targetNamespace?: string) => {
  // 如果指定了目标命名空间，且当前是"全部命名空间"模式，则切换到目标命名空间
  if (targetNamespace && searchForm.namespaces.length === 0) {
    searchForm.namespaces = [targetNamespace];
    searchStore.setNamespace(targetNamespace);
  }
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
      await refreshPods(namespace);
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
      // 取第一个被删除Pod的命名空间作为目标命名空间
      const targetNs = podsToDelete.length > 0 ? podsToDelete[0].ns : undefined;
      clearTableSelection();
      await refreshPods(targetNs);
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
      await refreshPods(namespace);
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

const handleViewLogs = (
  env: string,
  namespace: string,
  pod: string,
  deployment: string,
  containers?: PodContainerStatus[]
) => {
  logViewerRef.value?.openForPod({
    env,
    namespace,
    deployment,
    pod,
    containers
  });
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

// 初始化编辑YAML编辑器
const initEditYamlEditor = async () => {
  if (!editYamlEditorRef.value) return;
  if (editYamlEditor) {
    editYamlEditor.dispose();
  }
  editYamlEditor = monaco.editor.create(editYamlEditorRef.value, {
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

// 处理编辑Pod
const handleEditPod = async (env: string, namespace: string, name: string) => {
  if (!env || !namespace || !name) {
    ElMessage.error("缺少编辑Pod所需信息");
    return;
  }
  currentEditPod.value = { env, namespace, name };
  editDialogVisible.value = true;

  await nextTick();
  await initEditYamlEditor();

  try {
    const res = await getServiceContent(env, namespace, name, "pod");
    if (res.success && res.data) {
      if (editYamlEditor) {
        editYamlEditor.setValue(res.data);
      }
    }
  } catch (error) {
    console.error("获取Pod内容失败:", error);
    ElMessage.error("获取Pod内容失败");
  }
};

// 处理编辑保存 - 显示更新方式选择弹框
const handleEditSave = async () => {
  if (!editYamlEditor || !currentEditPod.value) {
    ElMessage.error("编辑器未初始化或未选择Pod");
    return;
  }

  const yamlContent = editYamlEditor.getValue();
  if (!yamlContent.trim()) {
    ElMessage.error("YAML内容不能为空");
    return;
  }

  try {
    yaml.load(yamlContent);
    selectedEditUpdateMethod.value = "apply";
    editUpdateMethodDialogVisible.value = true;
  } catch (error) {
    console.error("YAML格式验证失败:", error);
    if (error instanceof YAMLException) {
      ElMessage.error(`YAML格式错误: ${error.message}`);
    } else {
      ElMessage.error("YAML格式验证失败");
    }
  }
};

// 确认编辑更新
const confirmEditUpdate = async () => {
  if (!editYamlEditor || !currentEditPod.value) {
    ElMessage.error("编辑器未初始化或未选择Pod");
    return;
  }

  const yamlContent = editYamlEditor.getValue();

  try {
    editSaveLoading.value = true;
    const res = await updateServiceContent(
      currentEditPod.value.env,
      selectedEditUpdateMethod.value,
      yamlContent
    );

    if (res.success) {
      ElMessage.success(`Pod ${selectedEditUpdateMethod.value} 更新成功`);
      editUpdateMethodDialogVisible.value = false;
      editDialogVisible.value = false;
      await fetchPods();
    } else {
      ElMessage.error(
        res.message || `Pod ${selectedEditUpdateMethod.value} 更新失败`
      );
    }
  } catch (error) {
    console.error("更新Pod失败:", error);
    ElMessage.error("更新Pod失败");
  } finally {
    editSaveLoading.value = false;
  }
};

const handleEnvChange = async (value: string) => {
  searchForm.env = value;
  searchStore.setEnv(value || "");
  searchForm.nodeName = "";
  tableData.value = [];
  lastFetchedEnv.value = null;
  lastFetchedNamespace.value = null;
  resetPagination();
  clearSortState();
  clearTableSelection();
  await Promise.all([fetchNodeOptions(value), fetchNamespaceOptions(value)]);
};

const handleNodeChange = () => {
  tableData.value = [];
  resetPagination();
  clearSortState();
  clearTableSelection();
};

const handleNamespaceChange = (value: string[]) => {
  searchForm.namespaces = value || [];
  // 存储第一个选中的命名空间到 store（兼容其他页面）
  searchStore.setNamespace(value.length > 0 ? value[0] : "");
  tableData.value = [];
  lastFetchedNamespace.value = null;
  resetPagination();
  clearSortState();
  clearTableSelection();
};

const handleNamespaceRefresh = async () => {
  if (!searchForm.env || nsRefreshing.value) {
    return;
  }

  nsRefreshing.value = true;
  try {
    const refreshed = await fetchNamespaceOptions(searchForm.env, true);
    if (refreshed) {
      ElMessage.success("命名空间已刷新");
    }
  } catch (error) {
    console.error("刷新命名空间列表失败:", error);
  } finally {
    nsRefreshing.value = false;
  }
};

const handleQuery = async () => {
  appliedKeyword.value = searchForm.keyword.trim();
  resetPagination();
  const normalizedNamespaces = searchForm.namespaces.join(",");
  const shouldFetch =
    lastFetchedEnv.value !== searchForm.env ||
    lastFetchedNamespace.value !== normalizedNamespaces ||
    tableData.value.length === 0;

  if (shouldFetch) {
    await fetchPods();
  }
};

const handleRefresh = async () => {
  appliedKeyword.value = searchForm.keyword.trim();
  await fetchPods();
};

const fetchNodeOptions = async (env: string) => {
  if (!env) {
    nodeOptions.value = [];
    searchForm.nodeName = "";
    return;
  }
  try {
    const res = await getNodesList(env, true);
    nodeOptions.value = res.data || [];
  } catch (error) {
    console.error("获取节点列表失败:", error);
    nodeOptions.value = [];
  }
};

const fetchEnvOptions = async () => {
  try {
    const res = await getAgentNames();
    const options = res.data || [];
    envOptions.value = options;
    if (!options.length) {
      searchForm.env = "";
      searchStore.setEnv("");
      nodeOptions.value = [];
      nsOptions.value = [];
      searchForm.namespaces = [];
      searchStore.setNamespace("");
      return;
    }

    if (!options.includes(searchForm.env)) {
      searchForm.env = options[0];
      searchStore.setEnv(searchForm.env);
    }

    await Promise.all([
      fetchNodeOptions(searchForm.env),
      fetchNamespaceOptions(searchForm.env)
    ]);
  } catch (error) {
    console.error("获取K8S环境列表失败:", error);
    ElMessage.error("获取K8S环境列表失败");
  }
};

const fetchNamespaceOptions = async (
  env: string,
  flush = false
): Promise<boolean> => {
  if (!env) {
    nsOptions.value = [];
    searchForm.namespaces = [];
    searchStore.setNamespace("");
    return false;
  }
  try {
    const res = await getPromNamespace(env, flush);
    const options = res.data || [];
    nsOptions.value = options;

    if (!options.length) {
      searchForm.namespaces = [];
      searchStore.setNamespace("");
      return true;
    }

    // 过滤掉不存在的命名空间
    const validNamespaces = searchForm.namespaces.filter(ns =>
      options.includes(ns)
    );
    if (
      validNamespaces.length === 0 &&
      searchStore.namespace &&
      options.includes(searchStore.namespace)
    ) {
      searchForm.namespaces = [searchStore.namespace];
    } else {
      searchForm.namespaces = validNamespaces;
    }

    searchStore.setNamespace(
      searchForm.namespaces.length > 0 ? searchForm.namespaces[0] : ""
    );
    return true;
  } catch (error) {
    console.error("获取命名空间列表失败:", error);
    ElMessage.error("获取命名空间列表失败");
    nsOptions.value = [];
    searchForm.namespaces = [];
    searchStore.setNamespace("");
    return false;
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
      searchForm.namespaces.length > 0 ? searchForm.namespaces : undefined,
      searchForm.nodeName || undefined
    );
    tableData.value = res.data || [];
    lastFetchedEnv.value = searchForm.env;
    lastFetchedNamespace.value = searchForm.namespaces.join(",");
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

watch(editDialogVisible, visible => {
  if (!visible && editYamlEditor) {
    editYamlEditor.dispose();
    editYamlEditor = null;
  }
  if (!visible) {
    editSaveLoading.value = false;
    editUpdateMethodDialogVisible.value = false;
    currentEditPod.value = null;
  }
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
  flex-wrap: nowrap;
  align-items: center;
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
  display: inline-flex;
  align-items: center;
  font-size: 14px;
  line-height: 1;
  cursor: default;
}

.container-tooltip {
  max-width: 360px;
  margin: 0;
  font-size: 12px;
  line-height: 1.4;
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
  display: flex;
  flex-direction: column;
  height: 82vh;
}

.yaml-editor-container {
  display: flex;
  flex: 1;
  flex-direction: column;
  overflow: hidden;
  border: 1px solid #dcdfe6;
  border-radius: 4px;
}

.editor-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
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
  padding: 12px;
  margin-top: 20px;
  background-color: #f5f7fa;
  border-left: 4px solid #409eff;
  border-radius: 4px;
}

.method-description p {
  margin: 0;
  font-size: 14px;
  line-height: 1.5;
  color: #606266;
}

.result-content {
  font-size: 13px;
  word-break: break-all;
  white-space: pre-wrap;
}

.table-pagination {
  display: flex;
  justify-content: flex-end;
  margin-top: 12px;
}

.operation-cell {
  display: flex;
  flex-wrap: nowrap;
  gap: 8px;
  align-items: center;
  justify-content: center;
}

.operation-cell > .el-button + .el-button {
  margin-left: 0;
}

.operation-dropdown {
  flex-shrink: 0;
}
</style>
