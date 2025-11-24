<template>
  <div class="node-manager-container">
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

        <el-form-item class="refresh-btn-item">
          <el-button
            type="primary"
            :icon="RefreshRight"
            :loading="loading"
            @click="handleRefresh"
          >
            刷新
          </el-button>
        </el-form-item>

        <!-- 批量操作按钮 -->
        <el-form-item>
          <el-button
            type="warning"
            :disabled="!searchForm.env || selectedNodes.length === 0"
            @click="handleBatchCordon"
          >
            批量禁止调度
          </el-button>
          <el-button
            type="success"
            :disabled="!searchForm.env || selectedNodes.length === 0"
            @click="handleBatchUncordon"
          >
            批量允许调度
          </el-button>
        </el-form-item>
      </el-form>
    </div>

    <!-- 节点表格 -->
    <div class="mt-2">
      <el-card v-loading="loading">
        <el-table
          ref="nodeTableRef"
          :data="nodeList"
          style="width: 100%"
          stripe
          border
          :default-sort="{ prop: 'creation_timestamp', order: 'descending' }"
          @selection-change="handleSelectionChange"
          @sort-change="handleSortChange"
        >
          <!-- 多选列 -->
          <el-table-column type="selection" width="40" />

          <!-- 节点名称 -->
          <el-table-column
            prop="name"
            label="节点名称"
            min-width="100"
            show-overflow-tooltip
            sortable
          />

          <!-- 调度状态 -->
          <el-table-column
            prop="schedulable"
            label="调度状态"
            min-width="100"
            align="center"
            sortable
          >
            <template #default="scope">
              <el-tag
                :type="scope.row.schedulable ? 'success' : 'warning'"
                size="large"
                style="font-size: 16px"
              >
                {{ scope.row.schedulable ? "可调度" : "禁止调度" }}
              </el-tag>
            </template>
          </el-table-column>

          <!-- CPU使用率 -->
          <el-table-column
            label="CPU(核)"
            min-width="140"
            align="center"
            sortable="custom"
            column-key="cpu_usage"
          >
            <template #default="scope">
              <div>
                <div>
                  <span style="font-weight: bold; color: #f56c6c">{{
                    formatCpuCores(scope.row.current_cpu_m)
                  }}</span>
                  /
                  <span style="font-weight: bold; color: #409eff">{{
                    formatCpuCores(scope.row.allocatable_cpu_m)
                  }}</span>
                </div>
                <div style="font-size: 12px; color: #909399">
                  使用率: {{ getCpuUsagePercent(scope.row) }}%
                </div>
              </div>
            </template>
          </el-table-column>

          <!-- 内存使用率 -->
          <el-table-column
            label="内存(GiB)"
            min-width="140"
            align="center"
            sortable="custom"
            column-key="memory_usage"
          >
            <template #default="scope">
              <div>
                <div style="white-space: nowrap">
                  <span style="font-weight: bold; color: #f56c6c">{{
                    formatNumber(scope.row.current_memory_gi)
                  }}</span>
                  /
                  <span style="font-weight: bold; color: #409eff">{{
                    formatNumber(scope.row.allocatable_memory_gi)
                  }}</span>
                </div>
                <div style="font-size: 12px; color: #909399">
                  使用率: {{ getMemoryUsagePercent(scope.row) }}%
                </div>
              </div>
            </template>
          </el-table-column>

          <!-- 磁盘使用率 -->
          <el-table-column
            label="磁盘(GiB)"
            min-width="140"
            align="center"
            sortable="custom"
            column-key="disk_usage"
          >
            <template #default="scope">
              <div>
                <div style="white-space: nowrap">
                  <span style="font-weight: bold; color: #f56c6c">{{
                    formatNumber(scope.row.nodefs_used_gib)
                  }}</span>
                  /
                  <span style="font-weight: bold; color: #409eff">{{
                    formatNumber(scope.row.nodefs_capacity_gib)
                  }}</span>
                </div>
                <el-tooltip placement="top">
                  <template #content>
                    <div style="line-height: 1.4">
                      <div>
                        capacity_ephemeral_storage_gi:
                        {{
                          formatNumber(scope.row.capacity_ephemeral_storage_gi)
                        }}
                        GIB
                      </div>
                      <div>
                        allocatable_ephemeral_storage_gi:
                        {{
                          formatNumber(
                            scope.row.allocatable_ephemeral_storage_gi
                          )
                        }}
                        GIB
                      </div>
                      <div>
                        imagefs_capacity_gib:
                        {{ formatNumber(scope.row.imagefs_capacity_gib) }} GIB
                      </div>
                      <div>
                        imagefs_used_gib:
                        {{ formatNumber(scope.row.imagefs_used_gib) }} GIB
                      </div>
                    </div>
                  </template>
                  <div style="font-size: 12px; color: #909399">
                    使用率: {{ getDiskUsagePercent(scope.row) }}%
                  </div>
                </el-tooltip>
              </div>
            </template>
          </el-table-column>

          <!-- Pod数量 -->
          <el-table-column
            label="Pod数量"
            min-width="120"
            align="center"
            sortable="custom"
            column-key="pod_usage"
          >
            <template #default="scope">
              <div>
                <div>
                  <span style="font-weight: bold; color: #f56c6c">{{
                    scope.row.current_pods
                  }}</span>
                  /
                  <span style="font-weight: bold; color: #409eff">{{
                    scope.row.allocatable_pods
                  }}</span>
                </div>
                <div style="font-size: 12px; color: #909399">
                  使用率: {{ getPodUsagePercent(scope.row) }}%
                </div>
              </div>
            </template>
          </el-table-column>

          <!-- 节点状态 -->
          <el-table-column
            prop="conditions"
            label="节点状态"
            min-width="100"
            align="center"
            sortable
          >
            <template #default="scope">
              <div v-if="Array.isArray(scope.row.conditions)">
                <el-tag
                  v-for="condition in scope.row.conditions"
                  :key="condition"
                  :type="condition === 'Ready' ? 'success' : 'warning'"
                  size="large"
                  style="margin: 2px; font-size: 16px"
                >
                  {{ getConditionText(condition) }}
                </el-tag>
              </div>
              <div v-else>
                <el-tag type="info" size="large" style="font-size: 16px"
                  >未知</el-tag
                >
              </div>
            </template>
          </el-table-column>

          <!-- 创建时间 -->
          <el-table-column
            prop="creation_timestamp"
            label="创建时间"
            min-width="100"
            align="center"
            sortable
          >
            <template #default="scope">
              {{ formatDateTime(scope.row.creation_timestamp) }}
            </template>
          </el-table-column>

          <!-- Kubelet版本 -->
          <el-table-column
            prop="kubelet_version"
            label="Kubelet"
            min-width="120"
            show-overflow-tooltip
            sortable
          />

          <!-- 操作系统 -->
          <el-table-column
            prop="os_image"
            label="操作系统"
            min-width="120"
            show-overflow-tooltip
            sortable
          />

          <!-- 内核版本 -->
          <el-table-column
            prop="kernel_version"
            label="内核版本"
            min-width="120"
            show-overflow-tooltip
            sortable
          />

          <!-- 容器运行时 -->
          <el-table-column
            prop="container_runtime"
            label="容器运行时"
            min-width="120"
            show-overflow-tooltip
            sortable
          />

          <!-- 操作列 -->
          <el-table-column
            label="操作"
            min-width="180"
            align="center"
            fixed="right"
          >
            <template #default="scope">
              <el-button
                v-if="scope.row.schedulable"
                type="warning"
                size="default"
                @click="handleCordonNode(scope.row)"
              >
                禁止调度
              </el-button>
              <el-button
                v-else
                type="success"
                size="default"
                @click="handleUncordonNode(scope.row)"
              >
                允许调度
              </el-button>
              <el-button
                class="action-edit-btn"
                type="primary"
                size="default"
                @click="handleEdit(scope.row)"
              >
                编辑
              </el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-card>
    </div>
    <!-- 编辑节点 YAML -->
    <el-dialog
      v-model="editDialogVisible"
      title="编辑节点"
      width="95%"
      top="2.5vh"
      :close-on-click-modal="false"
      destroy-on-close
    >
      <div class="edit-container">
        <div class="yaml-editor-container">
          <div class="editor-header">
            <span class="editor-title">Node YAML编辑</span>
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

    <!-- 更新方式选择 -->
    <el-dialog
      v-model="updateMethodDialogVisible"
      title="选择更新方式"
      width="500px"
      :close-on-click-modal="false"
    >
      <div class="update-method-container">
        <el-radio-group v-model="selectedUpdateMethod">
          <el-radio value="apply">Apply - 应用（推荐）</el-radio>
          <el-radio value="replace">Replace - 替换</el-radio>
        </el-radio-group>
        <div class="method-description">
          <p v-if="selectedUpdateMethod === 'apply'">
            Apply方式会合并配置，适合大部分更新场景。
          </p>
          <p v-if="selectedUpdateMethod === 'replace'">
            Replace方式会完全替换配置，请确认YAML内容完整。
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
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted, nextTick, watch } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { RefreshRight } from "@element-plus/icons-vue";
import { getNodesList, cordonNodes, uncordonNodes } from "@/api/node";
import { getAgentNames } from "@/api/istio";
import { useSearchStoreHook } from "@/store/modules/search";
import { getServiceContent, updateServiceContent } from "@/api/service";
import * as monaco from "monaco-editor";
import * as yaml from "js-yaml";
import { YAMLException } from "js-yaml";

// 响应式数据
const loading = ref(false);
const envOptions = ref<string[]>([]);
const nodeList = ref<any[]>([]);
const selectedNodes = ref<any[]>([]);
const nodeTableRef = ref();
const searchStore = useSearchStoreHook();
const editDialogVisible = ref(false);
const updateMethodDialogVisible = ref(false);
const selectedUpdateMethod = ref<"apply" | "replace">("apply");
const saveLoading = ref(false);
const currentEditNode = ref<any>(null);
const yamlEditorRef = ref<HTMLElement | null>(null);
let yamlEditor: monaco.editor.IStandaloneCodeEditor | null = null;
const DEFAULT_NAMESPACE = "default";

// 搜索表单
const searchForm = reactive({
  env: searchStore.env || ""
});

// 获取环境列表
const getEnvList = async () => {
  try {
    const res = await getAgentNames();
    if (res.data && res.data.length > 0) {
      envOptions.value = res.data.map(item => item);

      // 如果store中有保存的环境且在选项中存在，则使用保存的环境
      if (searchStore.env && envOptions.value.includes(searchStore.env)) {
        searchForm.env = searchStore.env;
      } else if (envOptions.value.length > 0) {
        // 否则选择第一个环境并保存到store
        searchForm.env = envOptions.value[0];
        searchStore.setEnv(envOptions.value[0]);
      }

      if (searchForm.env) {
        await getNodesData();
      }
    }
  } catch (error) {
    console.error("获取环境列表失败:", error);
    ElMessage.error("获取环境列表失败");
  }
};

// 获取节点数据
const getNodesData = async () => {
  if (!searchForm.env) {
    ElMessage.warning("请先选择K8S环境");
    return;
  }

  loading.value = true;
  try {
    const response = await getNodesList(searchForm.env);
    console.log("节点API响应:", response);

    if (response.success && response.data) {
      // 根据实际API响应结构处理数据
      if (Array.isArray(response.data)) {
        nodeList.value = response.data;
      } else if (response.data.nodes && Array.isArray(response.data.nodes)) {
        nodeList.value = response.data.nodes;
      } else {
        nodeList.value = [];
        ElMessage.warning("API返回的数据格式不正确");
      }
      console.log("处理后的节点数据:", nodeList.value);
    } else {
      ElMessage.error(response.message || "获取节点列表失败");
      nodeList.value = [];
    }
  } catch (error) {
    console.error("获取节点列表失败:", error);
    ElMessage.error("获取节点列表失败");
    nodeList.value = [];
  } finally {
    loading.value = false;
  }
};

// 环境变化处理
const handleEnvChange = (val: string) => {
  searchForm.env = val;
  searchStore.setEnv(val);
  selectedNodes.value = [];
  if (nodeTableRef.value) {
    nodeTableRef.value.clearSelection();
  }
  getNodesData();
};

// 表格选择变化处理
const handleSelectionChange = (selection: any[]) => {
  selectedNodes.value = selection;
};

// 处理表格排序变化
const handleSortChange = ({ column, prop, order }: any) => {
  console.log("排序变化:", {
    column,
    prop,
    order,
    columnKey: column?.columnKey
  });

  if (!order) {
    // 取消排序，恢复原始顺序
    getNodesData();
    return;
  }

  const sortedData = [...nodeList.value];
  const sortKey = column?.columnKey || prop;

  if (sortKey === "cpu_usage") {
    sortedData.sort((a, b) => {
      const aUsage = getCpuUsagePercent(a);
      const bUsage = getCpuUsagePercent(b);
      return order === "ascending" ? aUsage - bUsage : bUsage - aUsage;
    });
  } else if (sortKey === "memory_usage") {
    sortedData.sort((a, b) => {
      const aUsage = getMemoryUsagePercent(a);
      const bUsage = getMemoryUsagePercent(b);
      return order === "ascending" ? aUsage - bUsage : bUsage - aUsage;
    });
  } else if (sortKey === "disk_usage") {
    sortedData.sort((a, b) => {
      const aUsage = getDiskUsagePercent(a);
      const bUsage = getDiskUsagePercent(b);
      return order === "ascending" ? aUsage - bUsage : bUsage - aUsage;
    });
  } else if (sortKey === "pod_usage") {
    sortedData.sort((a, b) => {
      const aUsage = getPodUsagePercent(a);
      const bUsage = getPodUsagePercent(b);
      return order === "ascending" ? aUsage - bUsage : bUsage - aUsage;
    });
  }

  nodeList.value = sortedData;
};

// 计算CPU使用率百分比
const getCpuUsagePercent = (row: any) => {
  if (!row.current_cpu_m || !row.allocatable_cpu_m) return 0;
  return Math.round((row.current_cpu_m / row.allocatable_cpu_m) * 100);
};

// 计算内存使用率百分比
const getMemoryUsagePercent = (row: any) => {
  if (!row.current_memory_gi || !row.allocatable_memory_gi) return 0;
  return Math.round((row.current_memory_gi / row.allocatable_memory_gi) * 100);
};

// 计算磁盘使用率百分比
const getDiskUsagePercent = (row: any) => {
  if (!row.nodefs_used_gib || !row.nodefs_capacity_gib) return 0;
  return Math.round((row.nodefs_used_gib / row.nodefs_capacity_gib) * 100);
};

// 计算Pod使用率百分比
const getPodUsagePercent = (row: any) => {
  if (!row.current_pods || !row.allocatable_pods) return 0;
  return Math.round((row.current_pods / row.allocatable_pods) * 100);
};

// 格式化数字，保留两位小数
const formatNumber = (value: number) => {
  if (value === null || value === undefined) return "0";
  return Number(value).toFixed(2);
};

// 格式化CPU核数，将毫核转换为核，保留3位小数且去掉结尾的0
const formatCpuCores = (milliCores: number) => {
  if (milliCores === null || milliCores === undefined) return "0";
  const cores = milliCores / 1000;
  return parseFloat(cores.toFixed(3)).toString();
};

// 格式化日期时间
const formatDateTime = (timestamp: string) => {
  if (!timestamp) return "-";
  try {
    const date = new Date(timestamp);
    return date.toLocaleDateString("zh-CN", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit"
    });
  } catch (error) {
    return timestamp;
  }
};

// 获取条件状态的中文文本
const getConditionText = (condition: string) => {
  const conditionMap: { [key: string]: string } = {
    Ready: "就绪",
    MemoryPressure: "内存压力",
    DiskPressure: "磁盘压力",
    PIDPressure: "PID压力",
    NetworkUnavailable: "网络不可用"
  };
  return conditionMap[condition] || condition;
};

// 单个节点禁止调度
const handleCordonNode = async (node: any) => {
  try {
    await ElMessageBox.confirm(
      `确定要禁止节点 "${node.name}" 的调度吗？`,
      "确认操作",
      {
        confirmButtonText: "确定",
        cancelButtonText: "取消",
        type: "warning"
      }
    );

    await executeCordon([node.name]);
  } catch (error) {
    // 用户取消操作
  }
};

// 单个节点允许调度
const handleUncordonNode = async (node: any) => {
  try {
    await ElMessageBox.confirm(
      `确定要允许节点 "${node.name}" 的调度吗？`,
      "确认操作",
      {
        confirmButtonText: "确定",
        cancelButtonText: "取消",
        type: "info"
      }
    );

    await executeUncordon([node.name]);
  } catch (error) {
    // 用户取消操作
  }
};

const handleEdit = async (row: any) => {
  if (!searchForm.env) {
    ElMessage.warning("请先选择K8S环境");
    return;
  }

  currentEditNode.value = row;
  editDialogVisible.value = true;
  selectedUpdateMethod.value = "apply";

  await nextTick();
  await initYamlEditor();

  try {
    const namespace = row?.namespace || DEFAULT_NAMESPACE;
    const res = await getServiceContent(
      searchForm.env,
      namespace,
      row.name,
      "node"
    );
    if (res.success && res.data && yamlEditor) {
      yamlEditor.setValue(res.data);
    }
  } catch (error) {
    console.error("获取节点配置失败:", error);
    ElMessage.error("获取节点配置失败");
  }
};

const initYamlEditor = async () => {
  if (!yamlEditorRef.value) return;

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

const handleSave = async () => {
  if (!yamlEditor || !currentEditNode.value) {
    ElMessage.error("编辑器未初始化或未选择节点");
    return;
  }

  const yamlContent = yamlEditor.getValue();
  if (!yamlContent.trim()) {
    ElMessage.error("YAML内容不能为空");
    return;
  }

  try {
    yaml.load(yamlContent);
    selectedUpdateMethod.value = "apply";
    updateMethodDialogVisible.value = true;
  } catch (error) {
    console.error("YAML格式校验失败:", error);
    if (error instanceof YAMLException) {
      ElMessage.error(`YAML格式错误: ${error.message}`);
    } else {
      ElMessage.error("YAML格式校验失败");
    }
  }
};

const confirmUpdate = async () => {
  if (!yamlEditor || !currentEditNode.value || !searchForm.env) {
    ElMessage.error("编辑器未初始化或未选择节点");
    return;
  }

  const yamlContent = yamlEditor.getValue();

  try {
    saveLoading.value = true;
    const res = await updateServiceContent(
      searchForm.env,
      selectedUpdateMethod.value,
      yamlContent
    );

    if (res.success) {
      ElMessage.success("节点更新成功");
      updateMethodDialogVisible.value = false;
      editDialogVisible.value = false;
      await getNodesData();
    } else {
      ElMessage.error(res.message || "节点更新失败");
    }
  } catch (error) {
    console.error("更新节点失败:", error);
    ElMessage.error("更新节点失败");
  } finally {
    saveLoading.value = false;
  }
};

// 批量禁止调度
const handleBatchCordon = async () => {
  if (selectedNodes.value.length === 0) {
    ElMessage.warning("请先选择要操作的节点");
    return;
  }

  try {
    await ElMessageBox.confirm(
      `确定要禁止选中的 ${selectedNodes.value.length} 个节点的调度吗？`,
      "确认批量操作",
      {
        confirmButtonText: "确定",
        cancelButtonText: "取消",
        type: "warning"
      }
    );

    const nodeNames = selectedNodes.value.map(node => node.name);
    await executeCordon(nodeNames);
  } catch (error) {
    // 用户取消操作
  }
};

// 批量允许调度
const handleBatchUncordon = async () => {
  if (selectedNodes.value.length === 0) {
    ElMessage.warning("请先选择要操作的节点");
    return;
  }

  try {
    await ElMessageBox.confirm(
      `确定要允许选中的 ${selectedNodes.value.length} 个节点的调度吗？`,
      "确认批量操作",
      {
        confirmButtonText: "确定",
        cancelButtonText: "取消",
        type: "info"
      }
    );

    const nodeNames = selectedNodes.value.map(node => node.name);
    await executeUncordon(nodeNames);
  } catch (error) {
    // 用户取消操作
  }
};

// 执行禁止调度操作
const executeCordon = async (nodeNames: string[]) => {
  loading.value = true;
  try {
    const response = await cordonNodes(searchForm.env, nodeNames);

    if (response.success) {
      ElMessage.success(response.data?.message || "禁止调度操作成功");
      // 清空选择并刷新数据
      selectedNodes.value = [];
      if (nodeTableRef.value) {
        nodeTableRef.value.clearSelection();
      }
      await getNodesData();
    } else {
      ElMessage.error(response.data?.error || "禁止调度操作失败");
    }
  } catch (error) {
    console.error("禁止调度操作失败:", error);
    ElMessage.error("禁止调度操作失败");
  } finally {
    loading.value = false;
  }
};

// 执行允许调度操作
const executeUncordon = async (nodeNames: string[]) => {
  loading.value = true;
  try {
    const response = await uncordonNodes(searchForm.env, nodeNames);
    if (response.success) {
      ElMessage.success(response.data?.message || "允许调度操作成功");
      // 清空选择并刷新数据
      selectedNodes.value = [];
      if (nodeTableRef.value) {
        nodeTableRef.value.clearSelection();
      }
      await getNodesData();
    } else {
      ElMessage.error(response.data?.error || "允许调度操作失败");
    }
  } catch (error) {
    console.error("允许调度操作失败:", error);
    ElMessage.error("允许调度操作失败");
  } finally {
    loading.value = false;
  }
};

watch(editDialogVisible, visible => {
  if (!visible) {
    if (yamlEditor) {
      yamlEditor.dispose();
      yamlEditor = null;
    }
    currentEditNode.value = null;
    selectedUpdateMethod.value = "apply";
    updateMethodDialogVisible.value = false;
    saveLoading.value = false;
  }
});

// 刷新数据
const handleRefresh = async () => {
  if (!searchForm.env) {
    ElMessage.warning("请先选择K8S环境");
    return;
  }
  await getNodesData();
  ElMessage.success("刷新成功");
};

// 组件挂载时获取数据
onMounted(() => {
  getEnvList();
});
</script>

<style scoped>
.node-manager-container {
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

.refresh-btn-item {
  margin-left: -12px !important;
}

.action-edit-btn {
  margin-left: 8px;
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

.update-method-container {
  padding: 20px 0;
}

.update-method-container .el-radio-group {
  display: flex;
  flex-direction: row;
  gap: 24px;
}

.method-description {
  padding: 12px;
  margin-top: 16px;
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
</style>
