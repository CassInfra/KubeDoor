<template>
  <div class="service-manager-container">
    <!-- 搜索表单 -->
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
            v-model="searchForm.ns"
            placeholder="请选择命名空间"
            class="!w-[180px]"
            filterable
            clearable
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
          <el-button
            type="primary"
            :disabled="!searchForm.env"
            :loading="loading"
            @click="handleSearch"
          >
            查询
          </el-button>
          <el-button
            type="primary"
            :icon="Refresh"
            :loading="loading"
            @click="handleRefresh"
          >
            刷新
          </el-button>
        </el-form-item>
        <!-- 新建按钮放置在表单最右侧 -->
        <el-form-item class="right-auto">
          <el-button type="success" @click="openCreateDialog">新建</el-button>
        </el-form-item>
      </el-form>
    </div>

    <!-- 表格数据 -->
    <div class="mt-2">
      <el-card v-loading="loading">
        <el-table
          ref="tableRef"
          class="hide-expand"
          :data="paginatedTableData"
          style="width: 100%"
          stripe
          border
          element-loading-text="加载中..."
          row-key="id"
          :expand-row-keys="expandedRowKeys"
          @sort-change="handleSortChange"
        >
          <el-table-column
            prop="namespace"
            label="命名空间"
            min-width="120"
            show-overflow-tooltip
          />
          <el-table-column
            prop="name"
            label="Service名称"
            min-width="200"
            show-overflow-tooltip
          />
          <el-table-column
            prop="type"
            label="类型"
            min-width="110"
            align="center"
            sortable="custom"
          >
            <template #default="scope">
              <el-tag
                v-if="scope.row.type"
                :type="getServiceTypeTagType(scope.row.type)"
                size="small"
              >
                {{ scope.row.type }}
              </el-tag>
              <span v-else>-</span>
            </template>
          </el-table-column>
          <el-table-column
            prop="clusterip"
            label="Cluster IP"
            min-width="180"
            show-overflow-tooltip
            sortable="custom"
          />
          <el-table-column
            prop="ports"
            label="端口"
            min-width="110"
            show-overflow-tooltip
            sortable="custom"
          />

          <!-- Endpoint列 -->
          <el-table-column label="Endpoint" min-width="90" align="center">
            <template #default="scope">
              <el-button
                type="primary"
                size="small"
                plain
                @click.stop="handleEndpointsDetail(scope.row)"
              >
                明细
              </el-button>
            </template>
          </el-table-column>

          <el-table-column
            prop="external_traffic_policy"
            label="外部策略"
            min-width="110"
            align="center"
            sortable="custom"
          >
            <template #default="scope">
              <el-tag
                v-if="scope.row.external_traffic_policy"
                :type="
                  getTrafficPolicyTagType(scope.row.external_traffic_policy)
                "
                size="small"
              >
                {{ scope.row.external_traffic_policy }}
              </el-tag>
              <span v-else>-</span>
            </template>
          </el-table-column>
          <el-table-column
            prop="internal_traffic_policy"
            label="内部策略"
            min-width="110"
            align="center"
            sortable="custom"
          >
            <template #default="scope">
              <el-tag
                v-if="scope.row.internal_traffic_policy"
                :type="
                  getTrafficPolicyTagType(scope.row.internal_traffic_policy)
                "
                size="small"
              >
                {{ scope.row.internal_traffic_policy }}
              </el-tag>
              <span v-else>-</span>
            </template>
          </el-table-column>
          <el-table-column
            prop="selector"
            label="选择器"
            min-width="160"
            show-overflow-tooltip
            sortable="custom"
          />
          <el-table-column
            prop="external_ips"
            label="外部IP"
            min-width="130"
            show-overflow-tooltip
            sortable="custom"
          />
          <el-table-column
            prop="creation_timestamp"
            label="创建时间"
            min-width="140"
            align="center"
            sortable="custom"
          >
            <template #default="scope">
              {{ formatCreationTime(scope.row.creation_timestamp) }}
            </template>
          </el-table-column>

          <!-- 展开行 -->
          <el-table-column type="expand" width="1">
            <template #default="scope">
              <div
                v-loading="scope.row.endpointsLoading"
                class="endpoints-detail-container"
              >
                <div
                  v-if="
                    scope.row.endpoints &&
                    scope.row.endpoints.subsets &&
                    scope.row.endpoints.subsets.length > 0
                  "
                >
                  <div
                    v-for="(subset, subsetIndex) in scope.row.endpoints.subsets"
                    :key="subsetIndex"
                    class="subset-container"
                  >
                    <h4>Subset {{ subsetIndex + 1 }}</h4>

                    <!-- Addresses表格 -->
                    <div
                      v-if="subset.addresses && subset.addresses.length > 0"
                      class="addresses-section"
                    >
                      <h5>Addresses:</h5>
                      <el-table
                        :data="subset.addresses"
                        border
                        size="small"
                        style="width: 100%; margin-bottom: 16px"
                      >
                        <el-table-column
                          prop="ip"
                          label="IP地址"
                          min-width="120"
                          align="center"
                        />
                        <el-table-column
                          prop="nodeName"
                          label="节点名称"
                          min-width="150"
                          align="center"
                          show-overflow-tooltip
                        />
                        <el-table-column
                          prop="pod_name"
                          label="Pod名称"
                          min-width="200"
                          align="center"
                          show-overflow-tooltip
                        />
                        <el-table-column
                          prop="pod_namespace"
                          label="Pod命名空间"
                          min-width="120"
                          align="center"
                        />
                      </el-table>
                    </div>

                    <!-- Ports表格 -->
                    <div
                      v-if="subset.ports && subset.ports.length > 0"
                      class="ports-section"
                    >
                      <h5>Ports:</h5>
                      <el-table
                        :data="subset.ports"
                        border
                        size="small"
                        style="width: 100%"
                      >
                        <el-table-column
                          prop="name"
                          label="端口名称"
                          min-width="120"
                          align="center"
                        />
                        <el-table-column
                          prop="port"
                          label="端口号"
                          min-width="80"
                          align="center"
                        />
                        <el-table-column
                          prop="protocol"
                          label="协议"
                          min-width="80"
                          align="center"
                        />
                      </el-table>
                    </div>
                  </div>
                </div>
                <div v-else-if="!scope.row.endpointsLoading" class="no-data">
                  暂无Endpoints数据
                </div>
              </div>
            </template>
          </el-table-column>

          <!-- 操作列 -->
          <el-table-column
            label="操作"
            width="140"
            align="center"
            fixed="right"
          >
            <template #default="scope">
              <el-button
                type="primary"
                size="small"
                @click="handleEdit(scope.row)"
              >
                编辑
              </el-button>
              <el-button
                type="danger"
                size="small"
                class="ml-2"
                @click="handleDelete(scope.row)"
              >
                删除
              </el-button>
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

    <!-- Service编辑对话框 -->
    <el-dialog
      v-model="editDialogVisible"
      title="编辑Service"
      width="95%"
      top="2.5vh"
      :close-on-click-modal="false"
      destroy-on-close
    >
      <div class="edit-container">
        <div class="yaml-editor-container">
          <div class="editor-header">
            <span class="editor-title">Service YAML配置</span>
            <el-button
              type="primary"
              :loading="saveLoading"
              @click="handleSave"
            >
              提交
            </el-button>
          </div>
          <div ref="yamlEditorRef" class="yaml-editor" />
        </div>
      </div>
    </el-dialog>

    <!-- 更新方式选择弹框 -->
    <el-dialog
      v-model="updateMethodDialogVisible"
      title="选择更新方式"
      width="400px"
      :close-on-click-modal="false"
    >
      <div class="update-method-container">
        <el-radio-group v-model="selectedUpdateMethod">
          <el-radio value="apply">Apply - 应用配置（推荐）</el-radio>
          <el-radio value="replace">Replace - 替换配置</el-radio>
        </el-radio-group>
        <div class="method-description">
          <p v-if="selectedUpdateMethod === 'apply'">
            Apply方式会智能合并配置，保留现有的其他字段，适用于大部分场景。
          </p>
          <p v-if="selectedUpdateMethod === 'replace'">
            Replace方式会完全替换现有配置，请确保YAML包含所有必要字段。
          </p>
        </div>
      </div>
      <template #footer>
        <span class="dialog-footer">
          <el-button @click="updateMethodDialogVisible = false">取消</el-button>
          <el-button
            type="primary"
            :loading="saveLoading"
            @click="confirmUpdate"
          >
            确认更新
          </el-button>
        </span>
      </template>
    </el-dialog>

    <!-- 新建Service全屏编辑对话框 -->
    <el-dialog
      v-model="createDialogVisible"
      title="新建Service"
      fullscreen
      :close-on-click-modal="false"
      destroy-on-close
    >
      <div class="edit-container">
        <div class="yaml-editor-container">
          <div class="editor-header">
            <span class="editor-title">Service YAML配置</span>
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

    <!-- 新建更新方式选择弹框（含 create/apply/replace） -->
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
            <el-radio value="apply">Apply - 应用配置（推荐）</el-radio>
            <el-radio value="replace">Replace - 替换配置</el-radio>
          </el-radio-group>
        </div>
        <div class="method-description">
          <p v-if="selectedCreateUpdateMethod === 'create'">
            Create方式用于新建资源，提交的YAML将被直接创建。
          </p>
          <p v-if="selectedCreateUpdateMethod === 'apply'">
            Apply方式会智能合并配置，保留现有的其他字段，适用于大部分场景。
          </p>
          <p v-if="selectedCreateUpdateMethod === 'replace'">
            Replace方式会完全替换现有配置，请确保YAML包含所有必要字段。
          </p>
        </div>
      </div>
      <template #footer>
        <span class="dialog-footer">
          <el-button @click="createUpdateMethodDialogVisible = false"
            >取消</el-button
          >
          <el-button
            type="primary"
            :loading="createSaveLoading"
            @click="confirmCreateUpdate"
          >
            确认更新
          </el-button>
        </span>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted, nextTick, watch } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import type { TableInstance } from "element-plus";
import { Refresh } from "@element-plus/icons-vue";
import { getAgentNames } from "@/api/istio";
import { getPromNamespace } from "@/api/monit";
import {
  getServiceList,
  getServiceContent,
  updateServiceContent,
  getServiceEndpoints,
  deleteK8sResource
} from "@/api/service";
import { useSearchStoreHook } from "@/store/modules/search";
import * as monaco from "monaco-editor";
import * as yaml from "js-yaml";
import { YAMLException } from "js-yaml";

defineOptions({
  name: "ServiceManager"
});

const searchStore = useSearchStoreHook();

// 定义表单数据
const searchForm = reactive({
  env: searchStore.env || "",
  ns: searchStore.namespace || "",
  keyword: ""
});

// 定义选项数据
const envOptions = ref<string[]>([]);
const nsOptions = ref<string[]>([]);

// 表格数据
const tableData = ref<any[]>([]);
const appliedKeyword = ref("");
const lastFetchedEnv = ref<string | null>(null);
const lastFetchedNamespace = ref<string | null>(null);
const tableRef = ref<TableInstance>();
const pageSizeOptions = [100, 200, 500, 1000];
const pageSize = ref<number>(pageSizeOptions[0]);
const currentPage = ref(1);
const loading = ref(false);

// 展开行相关
const expandedRowKeys = ref<string[]>([]);

// 编辑对话框相关
const editDialogVisible = ref(false);
const saveLoading = ref(false);
const currentEditService = ref<any>(null);
const yamlEditorRef = ref<HTMLElement | null>(null);
let yamlEditor: monaco.editor.IStandaloneCodeEditor | null = null;

// 更新方式选择弹框相关
const updateMethodDialogVisible = ref(false);
const selectedUpdateMethod = ref<"apply" | "replace">("apply");

// 新建对话框相关
const createDialogVisible = ref(false);
const createSaveLoading = ref(false);
const createYamlEditorRef = ref<HTMLElement | null>(null);
let createYamlEditor: monaco.editor.IStandaloneCodeEditor | null = null;

// 新建更新方式选择弹框相关
const createUpdateMethodDialogVisible = ref(false);
const selectedCreateUpdateMethod = ref<"create" | "apply" | "replace">(
  "create"
);

const searchFields = [
  "namespace",
  "name",
  "type",
  "clusterip",
  "ports",
  "external_traffic_policy",
  "internal_traffic_policy",
  "selector",
  "external_ips",
  "creation_timestamp"
] as const;

const normalizeFieldValue = (value: unknown): string => {
  if (value === undefined || value === null) {
    return "";
  }
  if (Array.isArray(value)) {
    return value.map(normalizeFieldValue).join(" ");
  }
  if (typeof value === "object") {
    return Object.values(value as Record<string, unknown>)
      .map(normalizeFieldValue)
      .join(" ");
  }
  return String(value);
};

// 过滤后的表格数据
const filteredTableData = computed(() => {
  const keyword = appliedKeyword.value.trim().toLowerCase();
  if (!keyword) {
    return tableData.value;
  }

  return tableData.value.filter(item =>
    searchFields.some(field => {
      const fieldValue = normalizeFieldValue(item[field]);
      return fieldValue.toLowerCase().includes(keyword);
    })
  );
});

type SortOrder = "ascending" | "descending" | null;

const sortField = ref<string>("");
const sortOrder = ref<SortOrder>(null);

const resetPagination = () => {
  currentPage.value = 1;
};

const clearSortState = () => {
  sortField.value = "";
  sortOrder.value = null;
  nextTick(() => {
    tableRef.value?.clearSort?.();
  });
};

const getComparableValue = (row: any, prop?: string) => {
  if (!prop) return undefined;
  if (prop === "creation_timestamp") {
    return row?.creation_timestamp
      ? new Date(row.creation_timestamp).getTime()
      : 0;
  }
  if (prop === "ports") {
    return normalizeFieldValue(row?.ports);
  }
  if (prop === "selector") {
    return normalizeFieldValue(row?.selector);
  }
  if (prop === "external_ips") {
    return normalizeFieldValue(row?.external_ips);
  }
  return row?.[prop];
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

  const direction = sortOrder.value === "ascending" ? 1 : -1;
  return data.sort((a, b) => {
    return (
      defaultComparator(
        getComparableValue(a, sortField.value),
        getComparableValue(b, sortField.value)
      ) * direction
    );
  });
});

const paginatedTableData = computed(() => {
  const start = (currentPage.value - 1) * pageSize.value;
  const end = start + pageSize.value;
  return sortedTableData.value.slice(start, end);
});

watch(pageSize, () => {
  resetPagination();
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
};

const handleSortChange = ({
  prop,
  order
}: {
  prop?: string;
  order: SortOrder;
}) => {
  if (!order) {
    sortField.value = "";
    sortOrder.value = null;
  } else {
    sortField.value = prop || "";
    sortOrder.value = order;
  }
  resetPagination();
};

// 获取K8S环境列表
const getEnvOptions = async (): Promise<void> => {
  try {
    const res = await getAgentNames();
    if (res.data && res.data.length > 0) {
      envOptions.value = res.data.map(item => item);
      // 如果 store 中有值且存在于选项中，则使用 store 中的值
      if (searchStore.env && envOptions.value.includes(searchStore.env)) {
        searchForm.env = searchStore.env;
        await getNsOptions(searchStore.env);
      } else {
        searchForm.env = res.data[0];
        await getNsOptions(res.data[0]);
      }
    }
    return Promise.resolve();
  } catch (error) {
    console.error("获取K8S环境列表失败:", error);
    ElMessage.error("获取K8S环境列表失败");
    return Promise.reject(error);
  }
};

// 处理环境变化
const handleEnvChange = async (val: string) => {
  searchForm.env = val;
  searchForm.ns = "";
  searchForm.keyword = "";
  appliedKeyword.value = "";
  searchStore.setEnv(val);
  searchStore.setNamespace("");
  expandedRowKeys.value = [];
  tableData.value = [];
  lastFetchedEnv.value = null;
  lastFetchedNamespace.value = null;
  resetPagination();
  clearSortState();
  if (val) {
    await getNsOptions(val);
  } else {
    nsOptions.value = [];
  }
};

const handleNamespaceChange = (val: string) => {
  searchForm.ns = val || "";
  searchStore.setNamespace(searchForm.ns);
  appliedKeyword.value = "";
  expandedRowKeys.value = [];
  tableData.value = [];
  lastFetchedNamespace.value = null;
  resetPagination();
};

// 获取命名空间列表
const getNsOptions = async (env: string): Promise<void> => {
  if (!env) {
    nsOptions.value = [];
    return Promise.resolve();
  }

  try {
    const res = await getPromNamespace(env);
    if (res.data) {
      nsOptions.value = res.data.map(item => item);
      if (
        searchStore.namespace &&
        nsOptions.value.includes(searchStore.namespace)
      ) {
        searchForm.ns = searchStore.namespace;
      } else {
        searchForm.ns = res.data[0] || "";
        searchStore.setNamespace(res.data[0] || "");
      }
    }
    return Promise.resolve();
  } catch (error) {
    console.error("获取命名空间列表失败:", error);
    ElMessage.error("获取命名空间列表失败");
    return Promise.reject(error);
  }
};

const fetchServiceList = async () => {
  const namespace: string | undefined = searchForm.ns
    ? searchForm.ns
    : undefined;
  loading.value = true;
  try {
    const res = await getServiceList(searchForm.env, namespace);
    if (res.success) {
      if (res.data) {
        tableData.value = res.data.map(item => {
          const rowEnv = item.env || searchForm.env || "";
          const rowNamespace = item.namespace || searchForm.ns || "";
          const rowName = item.name || "";
          return {
            ...item,
            env: rowEnv,
            namespace: rowNamespace,
            name: rowName,
            id: `${rowEnv || "-"}-${rowNamespace || "-"}-${rowName || "-"}`
              .replace(/\s+/g, "-")
              .toLowerCase(),
            endpoints: null,
            endpointsLoading: false
          };
        });
      } else {
        tableData.value = [];
      }
      lastFetchedEnv.value = searchForm.env || null;
      lastFetchedNamespace.value = searchForm.ns || "";
    } else {
      tableData.value = [];
    }
  } catch (error) {
    console.error("获取Service列表失败:", error);
    ElMessage.error("获取Service列表失败");
    tableData.value = [];
  } finally {
    loading.value = false;
  }
};

const resolveRowContext = (row: any) => {
  return {
    env: row?.env || searchForm.env || "",
    namespace: row?.namespace || searchForm.ns || "",
    name: row?.name || ""
  };
};

// 处理搜索
const handleSearch = async (forceFetchOrEvent: boolean | Event = false) => {
  if (forceFetchOrEvent instanceof Event) {
    forceFetchOrEvent.preventDefault();
  }
  const forceFetch =
    typeof forceFetchOrEvent === "boolean" ? forceFetchOrEvent : false;

  if (!searchForm.env) {
    ElMessage.warning("请选择K8S环境");
    return;
  }

  appliedKeyword.value = searchForm.keyword.trim();
  resetPagination();
  expandedRowKeys.value = [];

  const normalizedNamespace = searchForm.ns || "";
  searchStore.setNamespace(normalizedNamespace);

  const shouldFetch =
    forceFetch ||
    lastFetchedEnv.value !== searchForm.env ||
    lastFetchedNamespace.value !== normalizedNamespace ||
    tableData.value.length === 0;

  if (shouldFetch) {
    await fetchServiceList();
  }
};

// 刷新数据
const handleRefresh = () => {
  handleSearch(true);
};

// 处理Endpoints详情
const handleEndpointsDetail = async (row: any) => {
  // 如果已经加载过Endpoints数据，直接展开/收起行
  if (expandedRowKeys.value.includes(row.id)) {
    expandedRowKeys.value = expandedRowKeys.value.filter(id => id !== row.id);
    return;
  }

  // 设置加载状态
  row.endpointsLoading = true;

  try {
    const { env, namespace, name } = resolveRowContext(row);
    if (!env || !namespace || !name) {
      throw new Error("缺少查询Endpoints所需的环境、命名空间或名称");
    }
    const res = await getServiceEndpoints(env, namespace, name);
    if (res.success && res.data) {
      row.endpoints = res.data;
    } else {
      row.endpoints = { subsets: [] };
    }
    // 展开行
    expandedRowKeys.value.push(row.id);
  } catch (error) {
    console.error("获取Endpoints数据失败:", error);
    ElMessage.error("获取Endpoints数据失败");
    row.endpoints = { subsets: [] };
  } finally {
    row.endpointsLoading = false;
  }
};

// 处理编辑
const handleEdit = async (row: any) => {
  const { env, namespace, name } = resolveRowContext(row);
  if (!env || !namespace || !name) {
    ElMessage.error("缺少编辑Service所需信息");
    return;
  }
  currentEditService.value = {
    ...row,
    env,
    namespace,
    name
  };
  editDialogVisible.value = true;

  // 等待对话框渲染完成后初始化编辑器
  await nextTick();
  await initYamlEditor();

  // 获取Service详细内容
  try {
    const res = await getServiceContent(env, namespace, name, "service");
    if (res.success && res.data) {
      // 后端已返回完整的YAML字符串，直接使用
      if (yamlEditor) {
        yamlEditor.setValue(res.data);
      }
    }
  } catch (error) {
    console.error("获取Service内容失败:", error);
    ElMessage.error("获取Service内容失败");
  }
};

const handleDelete = async (row: any) => {
  const { env, namespace, name } = resolveRowContext(row);
  if (!env || !namespace || !name) {
    ElMessage.error("缺少删除Service所需信息");
    return;
  }

  try {
    await ElMessageBox.confirm(
      `确定要删除 Service \"${name}\" 吗？`,
      "确认删除",
      {
        confirmButtonText: "删除",
        cancelButtonText: "取消",
        type: "warning"
      }
    );

    const res = await deleteK8sResource(env, namespace, "service", name);

    if (res.success) {
      ElMessage.success("Service 删除成功");
      await handleSearch(true);
    } else {
      ElMessage.error(res.message || "Service 删除失败");
    }
  } catch (error) {
    if (error !== "cancel" && error !== "close") {
      console.error("删除Service失败:", error);
      ElMessage.error("删除Service失败");
    }
  }
};

// 初始化YAML编辑器
const initYamlEditor = async () => {
  if (!yamlEditorRef.value) return;

  // 如果编辑器已存在，先销毁
  if (yamlEditor) {
    yamlEditor.dispose();
  }

  yamlEditor = monaco.editor.create(yamlEditorRef.value, {
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

// 初始化新建的YAML编辑器
const initCreateYamlEditor = async () => {
  if (!createYamlEditorRef.value) return;

  // 如果编辑器已存在，先销毁
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

// 打开新建对话框
const openCreateDialog = async () => {
  if (!searchForm.env) {
    ElMessage.warning("请选择K8S环境");
    return;
  }

  createDialogVisible.value = true;
  await nextTick();
  await initCreateYamlEditor();
  // 默认空内容
  if (createYamlEditor) {
    createYamlEditor.setValue("");
  }
};

// 新建保存 - 显示更新方式选择弹框
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
    // 验证YAML格式
    yaml.load(yamlContent);

    // 显示更新方式选择弹框，默认选择create
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

// 确认新建/更新
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
      ElMessage.success(`Service ${selectedCreateUpdateMethod.value} 更新成功`);
      createUpdateMethodDialogVisible.value = false;
      createDialogVisible.value = false;
      await handleSearch(true); // 刷新列表
    } else {
      ElMessage.error(
        res.message || `Service ${selectedCreateUpdateMethod.value} 更新失败`
      );
    }
  } catch (error) {
    console.error("更新Service失败:", error);
    ElMessage.error("更新Service失败");
  } finally {
    createSaveLoading.value = false;
  }
};

// 处理保存 - 显示更新方式选择弹框
const handleSave = async () => {
  if (!yamlEditor || !currentEditService.value) {
    ElMessage.error("编辑器未初始化或未选择Service");
    return;
  }

  const yamlContent = yamlEditor.getValue();
  if (!yamlContent.trim()) {
    ElMessage.error("YAML内容不能为空");
    return;
  }

  try {
    // 验证YAML格式
    yaml.load(yamlContent);

    // 显示更新方式选择弹框
    selectedUpdateMethod.value = "apply"; // 默认选择apply
    updateMethodDialogVisible.value = true;
  } catch (error) {
    console.error("YAML格式验证失败:", error);
    if (error instanceof YAMLException) {
      ElMessage.error(`YAML格式错误: ${error.message}`);
    } else {
      ElMessage.error("YAML格式验证失败");
    }
  }
};

// 确认更新
const confirmUpdate = async () => {
  if (!yamlEditor || !currentEditService.value) {
    ElMessage.error("编辑器未初始化或未选择Service");
    return;
  }

  const yamlContent = yamlEditor.getValue();

  try {
    saveLoading.value = true;
    const res = await updateServiceContent(
      currentEditService.value.env,
      selectedUpdateMethod.value,
      yamlContent
    );

    if (res.success) {
      ElMessage.success(`Service ${selectedUpdateMethod.value} 更新成功`);
      updateMethodDialogVisible.value = false;
      editDialogVisible.value = false;
      await handleSearch(true); // 刷新列表
    } else {
      ElMessage.error(
        res.message || `Service ${selectedUpdateMethod.value} 更新失败`
      );
    }
  } catch (error) {
    console.error("更新Service失败:", error);
    ElMessage.error("更新Service失败");
  } finally {
    saveLoading.value = false;
  }
};

// 获取Service类型的tag类型
const getServiceTypeTagType = (type: string) => {
  switch (type) {
    case "ClusterIP":
      return "primary";
    case "NodePort":
      return "success";
    case "LoadBalancer":
      return "warning";
    case "ExternalName":
      return "info";
    default:
      return "";
  }
};

// 获取流量策略的tag类型
const getTrafficPolicyTagType = (policy: string) => {
  switch (policy) {
    case "Cluster":
      return "primary";
    case "Local":
      return "success";
    default:
      return "info";
  }
};

// 格式化创建时间为北京时间
const formatCreationTime = (timestamp: string) => {
  if (!timestamp) return "-";

  try {
    const date = new Date(timestamp);
    // 转换为北京时间 (UTC+8)
    const beijingTime = new Date(date.getTime() + 8 * 60 * 60 * 1000);

    // 格式化为 25/07/21 14:22
    const year = beijingTime.getUTCFullYear().toString().slice(-2);
    const month = String(beijingTime.getUTCMonth() + 1).padStart(2, "0");
    const day = String(beijingTime.getUTCDate()).padStart(2, "0");
    const hours = String(beijingTime.getUTCHours()).padStart(2, "0");
    const minutes = String(beijingTime.getUTCMinutes()).padStart(2, "0");

    return `${day}/${month}/${year} ${hours}:${minutes}`;
  } catch (error) {
    console.error("时间格式化失败:", error);
    return "-";
  }
};

// 页面初始化
onMounted(async () => {
  await getEnvOptions();
});
</script>

<style scoped>
.create-yaml-editor {
  height: calc(100vh - 120px);
}

.method-radio-row {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: nowrap;
}
</style>

<style scoped>
.service-manager-container {
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

.endpoints-detail-container {
  padding: 16px;
  border-radius: 4px;
  background-color: #f8f9fa;
}

.subset-container {
  margin-bottom: 24px;
}

.subset-container h4 {
  margin: 0 0 12px 0;
  color: #409eff;
  font-size: 16px;
  font-weight: bold;
}

.subset-container h5 {
  margin: 0 0 8px 0;
  color: #606266;
  font-size: 14px;
  font-weight: bold;
}

.addresses-section,
.ports-section {
  margin-bottom: 16px;
}

.no-data {
  padding: 20px 0;
  font-size: 14px;
  color: #909399;
  text-align: center;
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

.update-method-container {
  padding: 20px 0;
}

.update-method-container .el-radio-group {
  display: flex;
  flex-direction: row;
  gap: 24px;
}

.update-method-container .el-radio {
  margin: 0;
  font-size: 16px;
  font-weight: 500;
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

.table-pagination {
  display: flex;
  justify-content: flex-end;
  margin-top: 12px;
}
</style>

<style>
.hide-expand .el-table__expand-icon {
  visibility: hidden;
}
</style>
