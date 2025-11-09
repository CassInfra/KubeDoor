<template>
  <div class="stateful-manager-container">
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
            @change="handleSearch"
            @clear="handleNamespaceClear"
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
            placeholder="请输入StatefulSet名称"
            style="width: 200px"
            clearable
            @keyup.enter="handleSearch"
          />
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
        <el-form-item class="right-auto">
          <el-button type="success" @click="openCreateDialog">新建</el-button>
        </el-form-item>
      </el-form>
    </div>

    <div class="mt-2">
      <el-card v-loading="loading">
        <el-table
          :data="filteredTableData"
          row-key="id"
          style="width: 100%"
          stripe
          border
          :expand-row-keys="expandedRowKeys"
        >
          <el-table-column type="expand" width="1">
            <template #default="scope">
              <div
                v-loading="scope.row.podsLoading"
                class="pod-detail-container"
              >
                <el-table
                  v-if="scope.row.pods && scope.row.pods.length > 0"
                  :data="scope.row.pods"
                  border
                  style="width: 100%"
                >
                  <el-table-column
                    prop="name"
                    label="名称"
                    min-width="200"
                    show-overflow-tooltip
                    align="center"
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
                    min-width="90"
                    align="center"
                  >
                    <template #default="podScope">
                      <el-tag
                        :type="
                          podScope.row.status === 'Running'
                            ? 'success'
                            : 'danger'
                        "
                      >
                        {{ podScope.row.status }}
                      </el-tag>
                    </template>
                  </el-table-column>
                  <el-table-column
                    prop="ready"
                    label="就绪"
                    min-width="70"
                    align="center"
                  >
                    <template #default="podScope">
                      <el-tag
                        :type="podScope.row.ready ? 'success' : 'warning'"
                      >
                        {{ podScope.row.ready ? "是" : "否" }}
                      </el-tag>
                    </template>
                  </el-table-column>
                  <el-table-column
                    prop="pod_ip"
                    label="Pod IP"
                    min-width="140"
                    align="center"
                  />
                  <el-table-column
                    prop="cpu"
                    label="CPU"
                    min-width="90"
                    align="center"
                  />
                  <el-table-column
                    prop="memory"
                    label="内存"
                    min-width="90"
                    align="center"
                  />
                  <el-table-column
                    prop="node_name"
                    label="节点"
                    min-width="160"
                    show-overflow-tooltip
                  />
                  <el-table-column
                    prop="app_label"
                    label="应用标签"
                    min-width="150"
                    align="center"
                    show-overflow-tooltip
                    sortable
                  >
                    <template #default="podScope">
                      <div
                        :style="{
                          overflow: 'hidden',
                          'text-align': 'left',
                          'text-overflow': 'ellipsis',
                          'white-space': 'nowrap',
                          direction: 'rtl',
                          color:
                            podScope.row.app_label &&
                            podScope.row.app_label.endsWith('-ALERT')
                              ? 'red'
                              : ''
                        }"
                      >
                        {{ podScope.row.app_label }}
                      </div>
                    </template>
                  </el-table-column>
                  <el-table-column
                    prop="image"
                    label="镜像标签"
                    min-width="300"
                    show-overflow-tooltip
                    align="center"
                    sortable
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
                        {{ podScope.row.image }}
                      </div>
                    </template>
                  </el-table-column>
                  <el-table-column
                    prop="created_at"
                    label="创建时间"
                    min-width="160"
                    align="center"
                  />
                  <el-table-column
                    prop="restart_count"
                    label="重启次数"
                    min-width="90"
                    align="center"
                  />
                  <el-table-column
                    prop="restart_reason"
                    label="重启原因"
                    min-width="160"
                    show-overflow-tooltip
                  />
                  <el-table-column
                    prop="exception_reason"
                    label="异常原因"
                    min-width="200"
                    show-overflow-tooltip
                  />
                  <el-table-column
                    label="操作"
                    width="120"
                    align="center"
                    fixed="right"
                  >
                    <template #default="podScope">
                      <el-dropdown>
                        <el-button type="primary" size="small">
                          操作
                        </el-button>
                        <template #dropdown>
                          <el-dropdown-menu>
                            <el-dropdown-item
                              @click="handleViewLogs(scope.row, podScope.row)"
                            >
                              日志
                            </el-dropdown-item>
                            <el-dropdown-item
                              @click="handleIsolatePod(scope.row, podScope.row)"
                            >
                              隔离
                            </el-dropdown-item>
                            <el-dropdown-item
                              @click="handleDeletePod(scope.row, podScope.row)"
                            >
                              删除
                            </el-dropdown-item>
                          </el-dropdown-menu>
                        </template>
                      </el-dropdown>
                    </template>
                  </el-table-column>
                </el-table>
                <div v-else class="no-data">暂无Pod数据</div>
              </div>
            </template>
          </el-table-column>

          <el-table-column
            prop="namespace"
            label="命名空间"
            min-width="140"
            show-overflow-tooltip
          />
          <el-table-column
            prop="name"
            label="StatefulSet"
            min-width="200"
            show-overflow-tooltip
          />
          <el-table-column
            prop="desired_pods"
            label="期望Pod"
            min-width="100"
            align="center"
          />
          <el-table-column
            prop="ready_pods"
            label="就绪Pod"
            min-width="100"
            align="center"
          />
          <el-table-column label="明细" width="100" align="center">
            <template #default="scope">
              <el-button
                type="primary"
                size="small"
                plain
                @click="openPods(scope.row)"
              >
                明细
              </el-button>
            </template>
          </el-table-column>
          <el-table-column
            prop="cpu_requests"
            label="CPU Requests"
            min-width="140"
            align="center"
          />
          <el-table-column
            prop="cpu_limits"
            label="CPU Limits"
            min-width="140"
            align="center"
          />
          <el-table-column
            prop="memory_requests"
            label="内存Requests"
            min-width="160"
            align="center"
          />
          <el-table-column
            prop="memory_limits"
            label="内存Limits"
            min-width="160"
            align="center"
          />
          <el-table-column
            prop="creation_timestamp"
            label="创建时间"
            min-width="170"
            align="center"
          >
            <template #default="scope">
              {{ formatCreationTime(scope.row.creation_timestamp) }}
            </template>
          </el-table-column>
          <el-table-column
            label="操作"
            width="180"
            align="center"
            fixed="right"
          >
            <template #default="scope">
              <el-dropdown>
                <el-button type="primary" size="small">
                  操作
                  <el-icon class="el-icon--right"><arrow-down /></el-icon>
                </el-button>
                <template #dropdown>
                  <el-dropdown-menu>
                    <el-dropdown-item @click="handleScale(scope.row)"
                      >扩缩容</el-dropdown-item
                    >
                    <el-dropdown-item @click="handleRestart(scope.row)"
                      >重启</el-dropdown-item
                    >
                    <el-dropdown-item @click="handleEdit(scope.row)"
                      >编辑</el-dropdown-item
                    >
                    <el-dropdown-item @click="handleDelete(scope.row)"
                      >删除</el-dropdown-item
                    >
                  </el-dropdown-menu>
                </template>
              </el-dropdown>
            </template>
          </el-table-column>
        </el-table>
      </el-card>
    </div>

    <el-dialog
      v-model="createDialogVisible"
      title="新建StatefulSet"
      fullscreen
      :close-on-click-modal="false"
      destroy-on-close
    >
      <div class="edit-container">
        <div class="yaml-editor-container">
          <div class="editor-header">
            <span class="editor-title">StatefulSet YAML配置</span>
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
            <el-radio value="apply">Apply - 应用配置（推荐）</el-radio>
            <el-radio value="replace">Replace - 替换配置</el-radio>
          </el-radio-group>
        </div>
        <div class="method-description">
          <p v-if="selectedCreateUpdateMethod === 'create'">
            Create方式用于直接新建StatefulSet，提交的YAML会被新建。
          </p>
          <p v-if="selectedCreateUpdateMethod === 'apply'">
            Apply方式会智能合并配置，适合大多数更新场景。
          </p>
          <p v-if="selectedCreateUpdateMethod === 'replace'">
            Replace方式会完全替换配置，请确保YAML内容完整准确。
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
            确认提交
          </el-button>
        </span>
      </template>
    </el-dialog>

    <el-dialog
      v-model="editDialogVisible"
      title="编辑StatefulSet"
      width="95%"
      top="2.5vh"
      :close-on-click-modal="false"
      destroy-on-close
    >
      <div class="edit-container">
        <div class="yaml-editor-container">
          <div class="editor-header">
            <span class="editor-title">StatefulSet YAML配置</span>
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
            Apply方式会智能合并配置，适合大部分场景。
          </p>
          <p v-if="selectedUpdateMethod === 'replace'">
            Replace方式会完全替换配置，请确保YAML完整准确。
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

    <!-- Pod日志查看弹窗（复用 monit/index.vue 实现） -->
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
            <!-- 日志搜索功能 -->
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
  </div>
</template>

<script setup lang="ts">
import {
  ref,
  reactive,
  computed,
  onMounted,
  nextTick,
  watch,
  onBeforeUnmount
} from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { Refresh, ArrowDown } from "@element-plus/icons-vue";
import { getAgentNames } from "@/api/istio";
import {
  getPromNamespace,
  getPodPreviousLogs,
  createPodLogStreamUrl
} from "@/api/monit";
import { modifyPod, deletePod } from "@/api/alarm";
import {
  getStatefulSetList,
  getStatefulSetPods,
  restartStatefulSet,
  scaleStatefulSet
} from "@/api/stateful";
import {
  getServiceContent,
  updateServiceContent,
  deleteK8sResource
} from "@/api/service";
import { useSearchStoreHook } from "@/store/modules/search";
import * as monaco from "monaco-editor";
import * as yaml from "js-yaml";
import { YAMLException } from "js-yaml";
import { AnsiUp } from "ansi_up";

defineOptions({
  name: "StatefulSetManager"
});

const searchStore = useSearchStoreHook();

const searchForm = reactive({
  env: searchStore.env || "",
  ns: searchStore.namespace || "",
  keyword: ""
});

const envOptions = ref<string[]>([]);
const nsOptions = ref<string[]>([]);
const tableData = ref<any[]>([]);
const loading = ref(false);
const expandedRowKeys = ref<string[]>([]);

// 日志查看相关（复用 monit/index.vue）
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
  deployment: ""
});

const createDialogVisible = ref(false);
const createSaveLoading = ref(false);
const createYamlEditorRef = ref<HTMLElement | null>(null);
let createYamlEditor: monaco.editor.IStandaloneCodeEditor | null = null;
const createUpdateMethodDialogVisible = ref(false);
const selectedCreateUpdateMethod = ref<"create" | "apply" | "replace">(
  "create"
);

const editDialogVisible = ref(false);
const updateMethodDialogVisible = ref(false);
const selectedUpdateMethod = ref<"apply" | "replace">("apply");
const saveLoading = ref(false);
const yamlEditorRef = ref<HTMLElement | null>(null);
let yamlEditor: monaco.editor.IStandaloneCodeEditor | null = null;
const currentEditResource = ref<any>(null);

const filteredTableData = computed(() => {
  if (!searchForm.keyword) {
    return tableData.value;
  }
  const keyword = searchForm.keyword.toLowerCase();
  return tableData.value.filter(item => {
    return (
      item.name.toLowerCase().includes(keyword) ||
      (item.namespace && item.namespace.toLowerCase().includes(keyword))
    );
  });
});

const formatCreationTime = (timestamp?: string) => {
  if (!timestamp) return "-";
  try {
    const date = new Date(timestamp);
    return date.toLocaleString();
  } catch (error) {
    return timestamp;
  }
};

const getEnvOptions = async () => {
  try {
    const res = await getAgentNames();
    if (res.data && res.data.length > 0) {
      envOptions.value = res.data.map((item: string) => item);
      if (searchStore.env && envOptions.value.includes(searchStore.env)) {
        searchForm.env = searchStore.env;
        await getNsOptions(searchStore.env);
      } else {
        searchForm.env = res.data[0];
        searchStore.setEnv(res.data[0]);
        await getNsOptions(res.data[0]);
      }
    }
  } catch (error) {
    console.error("获取K8S环境失败:", error);
    ElMessage.error("获取K8S环境失败");
  }
};

const getNsOptions = async (env: string) => {
  if (!env) {
    nsOptions.value = [];
    searchForm.ns = "";
    searchStore.setNamespace("");
    tableData.value = [];
    return;
  }
  try {
    const res = await getPromNamespace(env);
    if (res.data && res.data.length > 0) {
      nsOptions.value = res.data.map((item: string) => item);
      if (
        searchStore.namespace &&
        nsOptions.value.includes(searchStore.namespace)
      ) {
        searchForm.ns = searchStore.namespace;
      } else {
        searchForm.ns = res.data[0];
        searchStore.setNamespace(searchForm.ns);
      }
      await handleSearch();
    } else {
      nsOptions.value = [];
      searchForm.ns = "";
      searchStore.setNamespace("");
      tableData.value = [];
    }
  } catch (error) {
    console.error("获取命名空间失败:", error);
    ElMessage.error("获取命名空间失败");
  }
};

const handleEnvChange = async (val: string) => {
  searchForm.ns = "";
  searchStore.setEnv(val);
  searchStore.setNamespace("");
  if (val) {
    await getNsOptions(val);
  } else {
    tableData.value = [];
  }
};

const handleSearch = async () => {
  if (!searchForm.env) {
    tableData.value = [];
    return;
  }
  loading.value = true;
  try {
    searchStore.setEnv(searchForm.env);
    searchStore.setNamespace(searchForm.ns || "");
    const res = await getStatefulSetList(
      searchForm.env,
      searchForm.ns || undefined
    );
    if (res.success && res.data) {
      tableData.value = res.data.map(item => ({
        ...item,
        id: `${item.env}-${item.namespace}-${item.name}`,
        pods: [],
        podsLoading: false
      }));
    } else {
      tableData.value = [];
      if (res.message) {
        ElMessage.error(res.message);
      }
    }
  } catch (error) {
    console.error("获取StatefulSet列表失败:", error);
    ElMessage.error("获取StatefulSet列表失败");
    tableData.value = [];
  } finally {
    loading.value = false;
  }
};

const handleNamespaceClear = () => {
  searchForm.ns = "";
  handleSearch();
};

const handleRefresh = () => {
  handleSearch();
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

const openCreateDialog = async () => {
  if (!searchForm.env) {
    ElMessage.warning("请选择K8S环境");
    return;
  }
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
      ElMessage.success(
        `StatefulSet ${selectedCreateUpdateMethod.value} 更新成功`
      );
      createUpdateMethodDialogVisible.value = false;
      createDialogVisible.value = false;
      handleSearch();
    } else {
      ElMessage.error(
        res.message ||
          `StatefulSet ${selectedCreateUpdateMethod.value} 更新失败`
      );
    }
  } catch (error) {
    console.error("提交StatefulSet失败:", error);
    ElMessage.error("提交StatefulSet失败");
  } finally {
    createSaveLoading.value = false;
  }
};

const openPods = async (row: any) => {
  const keyIndex = expandedRowKeys.value.indexOf(row.id);
  if (keyIndex >= 0) {
    expandedRowKeys.value.splice(keyIndex, 1);
    return;
  }
  expandedRowKeys.value.push(row.id);
  await loadPods(row);
};

const loadPods = async (row: any) => {
  row.podsLoading = true;
  try {
    const res = await getStatefulSetPods(row.env, row.namespace, row.name);
    if (res.success && res.pods) {
      row.pods = res.pods;
    } else {
      row.pods = [];
      if (res.message) {
        ElMessage.warning(res.message);
      }
    }
  } catch (error) {
    console.error("获取StatefulSet Pod信息失败:", error);
    ElMessage.error("获取StatefulSet Pod信息失败");
    row.pods = [];
  } finally {
    row.podsLoading = false;
  }
};

const handleRestart = async (row: any) => {
  try {
    await ElMessageBox.confirm(
      `确定要重启 StatefulSet "${row.name}" 吗？`,
      "确认重启",
      {
        confirmButtonText: "重启",
        cancelButtonText: "取消",
        type: "warning"
      }
    );
    const res = await restartStatefulSet(row.env, row.namespace, row.name);
    if (res.success) {
      ElMessage.success(res.message || "重启任务已触发");
    } else {
      ElMessage.error(res.message || "重启StatefulSet失败");
    }
  } catch (error) {
    if (error !== "cancel" && error !== "close") {
      console.error("重启StatefulSet失败:", error);
      ElMessage.error("重启StatefulSet失败");
    }
  }
};

const handleScale = async (row: any) => {
  try {
    const { value } = await ElMessageBox.prompt(
      `请输入StatefulSet "${row.name}" 的目标副本数`,
      "扩缩容",
      {
        confirmButtonText: "提交",
        cancelButtonText: "取消",
        inputPattern: /^\d+$/,
        inputErrorMessage: "请输入非负整数",
        inputValue: String(row.desired_pods ?? 0)
      }
    );
    const replicas = Number(value);
    const res = await scaleStatefulSet(
      row.env,
      row.namespace,
      row.name,
      replicas
    );
    if (res.success) {
      ElMessage.success(res.message || "扩缩容操作已提交");
      handleSearch();
    } else {
      ElMessage.error(res.message || "扩缩容失败");
    }
  } catch (error) {
    if (error !== "cancel" && error !== "close") {
      console.error("扩缩容失败:", error);
      ElMessage.error("扩缩容失败");
    }
  }
};

const handleEdit = async (row: any) => {
  currentEditResource.value = row;
  editDialogVisible.value = true;
  await nextTick();
  initYamlEditor();
  try {
    const res = await getServiceContent(
      row.env,
      row.namespace,
      row.name,
      "statefulset"
    );
    if (res.success && res.data && yamlEditor) {
      yamlEditor.setValue(res.data as string);
    } else {
      ElMessage.error(res.message || "获取YAML内容失败");
    }
  } catch (error) {
    console.error("获取StatefulSet内容失败:", error);
    ElMessage.error("获取StatefulSet内容失败");
  }
};

const initYamlEditor = () => {
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
  if (!yamlEditor || !currentEditResource.value) {
    ElMessage.error("编辑器未初始化或未选择StatefulSet");
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
    if (error instanceof YAMLException) {
      ElMessage.error(`YAML格式错误: ${error.message}`);
    } else {
      ElMessage.error("YAML格式验证失败");
    }
  }
};

const confirmUpdate = async () => {
  if (!yamlEditor || !currentEditResource.value) {
    ElMessage.error("编辑器未初始化或未选择StatefulSet");
    return;
  }
  const yamlContent = yamlEditor.getValue();
  try {
    saveLoading.value = true;
    const res = await updateServiceContent(
      currentEditResource.value.env,
      selectedUpdateMethod.value,
      yamlContent
    );
    if (res.success) {
      ElMessage.success(`StatefulSet ${selectedUpdateMethod.value} 更新成功`);
      updateMethodDialogVisible.value = false;
      editDialogVisible.value = false;
      handleSearch();
    } else {
      ElMessage.error(
        res.message || `StatefulSet ${selectedUpdateMethod.value} 更新失败`
      );
    }
  } catch (error) {
    console.error("更新StatefulSet失败:", error);
    ElMessage.error("更新StatefulSet失败");
  } finally {
    saveLoading.value = false;
  }
};

const handleDelete = async (row: any) => {
  try {
    await ElMessageBox.confirm(
      `确定要删除 StatefulSet "${row.name}" 吗？`,
      "确认删除",
      {
        confirmButtonText: "删除",
        cancelButtonText: "取消",
        type: "warning"
      }
    );
    const res = await deleteK8sResource(
      row.env,
      row.namespace,
      "statefulset",
      row.name
    );
    if (res.success) {
      ElMessage.success(res.message || "StatefulSet 删除成功");
      handleSearch();
    } else {
      ElMessage.error(res.message || "删除StatefulSet失败");
    }
  } catch (error) {
    if (error !== "cancel" && error !== "close") {
      console.error("删除StatefulSet失败:", error);
      ElMessage.error("删除StatefulSet失败");
    }
  }
};

// 日志查看入口（适配本页参数形式）
const handleViewLogs = (row: any, pod: any) => {
  currentPodInfo.value = {
    name: pod.name,
    env: row.env,
    namespace: row.namespace,
    deployment: row.name
  };
  logMessages.value = [];
  logDialogVisible.value = true;
  document.body.style.overflow = "hidden";
};

const startLogStream = () => {
  if (logSocket.value) {
    logSocket.value.close();
  }
  logConnecting.value = true;
  logMessages.value = [];
  const wsUrl = createPodLogStreamUrl(
    currentPodInfo.value.env,
    currentPodInfo.value.namespace,
    currentPodInfo.value.name
  );
  logSocket.value = new WebSocket(wsUrl);
  logSocket.value.onopen = () => {
    logConnecting.value = false;
    isLogConnected.value = true;
    ElMessage.success("日志连接成功");
  };
  logSocket.value.onmessage = event => {
    if (event.data && event.data.trim()) {
      logMessages.value.push(event.data);
    }
    if (logMessages.value.length > 1000) {
      logMessages.value = logMessages.value.slice(-800);
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

const clearLogs = () => {
  logMessages.value = [];
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
  if (!isAtBottom()) {
    isUserScrolling.value = true;
  } else {
    isUserScrolling.value = false;
  }
};

// 搜索/筛选相关
const searchKeyword = ref("");
const searchMatches = ref<number[]>([]);
const currentMatchIndex = ref(-1);
const totalMatches = ref(0);
const isFilterMode = ref(false);

const filteredLogMessages = computed(() => {
  if (!isFilterMode.value || !searchKeyword.value.trim()) {
    return logMessages.value;
  }
  const keyword = searchKeyword.value.toLowerCase();
  return logMessages.value.filter(m => m.toLowerCase().includes(keyword));
});

const getLogKey = (message: string, index: number) => {
  if (isFilterMode.value) {
    return `${message.slice(0, 50)}-${index}`;
  }
  return index;
};

const getOriginalIndex = (message: string, filteredIndex: number) => {
  if (!isFilterMode.value) return filteredIndex;
  return logMessages.value.findIndex(msg => msg === message);
};

const getFilteredIndex = (originalIndex: number) => {
  if (!isFilterMode.value) return originalIndex;
  const targetMessage = logMessages.value[originalIndex];
  return filteredLogMessages.value.findIndex(msg => msg === targetMessage);
};

const toggleFilterMode = () => {
  isFilterMode.value = !isFilterMode.value;
  if (isFilterMode.value && searchKeyword.value.trim()) {
    performSearch(true);
  }
};

// ANSI 转 HTML
const ansiUp = new AnsiUp();
ansiUp.escape_html = true;
ansiUp.use_classes = false;
const convertAnsiToHtml = (message: string) => ansiUp.ansi_to_html(message);

const highlightSearchKeyword = (message: string, index: number) => {
  let processedMessage = convertAnsiToHtml(message);
  if (!searchKeyword.value.trim()) return processedMessage;
  const keyword = searchKeyword.value;
  let isCurrentMatch = false;
  if (isFilterMode.value) {
    const filteredIndex = getFilteredIndex(index);
    isCurrentMatch =
      searchMatches.value[currentMatchIndex.value] === filteredIndex;
  } else {
    isCurrentMatch = searchMatches.value[currentMatchIndex.value] === index;
  }
  if (!message.toLowerCase().includes(keyword.toLowerCase())) {
    return processedMessage;
  }
  const regex = new RegExp(
    `(${keyword.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")})`,
    "gi"
  );
  const highlightClass = isCurrentMatch
    ? "search-highlight-current"
    : "search-highlight";
  return processedMessage.replace(
    regex,
    `<span class="${highlightClass}">$1</span>`
  );
};

const performSearch = (forceFirstMatch = false) => {
  if (!searchKeyword.value.trim()) {
    searchMatches.value = [];
    currentMatchIndex.value = -1;
    totalMatches.value = 0;
    return;
  }
  let currentMatchContent = "";
  if (currentMatchIndex.value >= 0 && searchMatches.value.length > 0) {
    const currentLineIndex = searchMatches.value[currentMatchIndex.value];
    const targetMessages = isFilterMode.value
      ? filteredLogMessages.value
      : logMessages.value;
    if (currentLineIndex < targetMessages.length) {
      currentMatchContent = targetMessages[currentLineIndex];
    }
  }
  const matches: number[] = [];
  const keyword = searchKeyword.value.toLowerCase();
  const targetMessages = isFilterMode.value
    ? filteredLogMessages.value
    : logMessages.value;
  targetMessages.forEach((message, index) => {
    if (message.toLowerCase().includes(keyword)) matches.push(index);
  });
  searchMatches.value = matches;
  totalMatches.value = matches.length;
  let newMatchIndex = 0;
  if (matches.length > 0) {
    if (forceFirstMatch) {
      newMatchIndex = 0;
    } else if (currentMatchContent) {
      const sameContentIndex = matches.findIndex(
        matchIndex => targetMessages[matchIndex] === currentMatchContent
      );
      if (sameContentIndex >= 0) {
        newMatchIndex = sameContentIndex;
      } else {
        const oldLineIndex = searchMatches.value[currentMatchIndex.value] || 0;
        let closestIndex = 0;
        let minDiff = Infinity;
        matches.forEach((idx, i) => {
          const diff = Math.abs(idx - oldLineIndex);
          if (diff < minDiff) {
            minDiff = diff;
            closestIndex = i;
          }
        });
        newMatchIndex = closestIndex;
      }
    }
    currentMatchIndex.value = newMatchIndex;
    if (forceFirstMatch || !currentMatchContent) {
      scrollToMatch(matches[newMatchIndex]);
    }
  } else {
    currentMatchIndex.value = -1;
  }
};

const goToPreviousMatch = () => {
  if (searchMatches.value.length === 0) return;
  currentMatchIndex.value =
    currentMatchIndex.value > 0
      ? currentMatchIndex.value - 1
      : searchMatches.value.length - 1;
  scrollToMatch(searchMatches.value[currentMatchIndex.value]);
};

const goToNextMatch = () => {
  if (searchMatches.value.length === 0) return;
  currentMatchIndex.value =
    currentMatchIndex.value < searchMatches.value.length - 1
      ? currentMatchIndex.value + 1
      : 0;
  scrollToMatch(searchMatches.value[currentMatchIndex.value]);
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
      const logLines = data.message
        .split("\n")
        .filter(line => line.trim() !== "");
      logMessages.value = logLines;
      ElMessage.success("重启前日志获取成功");
      nextTick(() => scrollToBottom());
    } else {
      ElMessage.warning(data.message || "获取重启前日志失败");
    }
  } catch (error: any) {
    console.error("获取重启前日志失败:", error);
    ElMessage.error(`获取重启前日志失败: ${error.message}`);
  } finally {
    logConnecting.value = false;
  }
};

const scrollToMatch = (lineIndex: number) => {
  if (!logContentRef.value) return;
  const logLines = logContentRef.value.querySelectorAll(".log-line");
  if (logLines[lineIndex]) {
    const targetElement = logLines[lineIndex] as HTMLElement;
    const containerHeight = logContentRef.value.clientHeight;
    const elementTop = targetElement.offsetTop;
    const elementHeight = targetElement.offsetHeight;
    const scrollTop = elementTop - containerHeight / 2 + elementHeight / 2;
    logContentRef.value.scrollTo({
      top: Math.max(0, scrollTop),
      behavior: "smooth"
    });
  }
};

watch(
  logContentRef,
  newRef => {
    if (newRef) newRef.addEventListener("scroll", handleScroll);
  },
  { immediate: true }
);

watch(
  logMessages,
  () => {
    if (searchKeyword.value.trim()) performSearch();
  },
  { deep: true }
);

onBeforeUnmount(() => {
  if (logContentRef.value) {
    logContentRef.value.removeEventListener("scroll", handleScroll);
  }
  document.body.style.overflow = "";
});

const handleIsolatePod = async (row: any, pod: any) => {
  try {
    await ElMessageBox.confirm(
      `确定要隔离 Pod "${pod.name}" 吗？`,
      "隔离确认",
      {
        confirmButtonText: "隔离",
        cancelButtonText: "取消",
        type: "warning"
      }
    );
    const res = await modifyPod({
      env: row.env,
      ns: row.namespace,
      pod_name: pod.name
    });
    if ((res as any).success) {
      ElMessage.success("隔离操作已提交");
      await loadPods(row);
    } else {
      ElMessage.error((res as any).message || "隔离失败");
    }
  } catch (error) {
    if (error !== "cancel" && error !== "close") {
      console.error("隔离Pod失败:", error);
      ElMessage.error("隔离失败");
    }
  }
};

const handleDeletePod = async (row: any, pod: any) => {
  try {
    await ElMessageBox.confirm(
      `确定要删除 Pod "${pod.name}" 吗？`,
      "删除确认",
      {
        confirmButtonText: "删除",
        cancelButtonText: "取消",
        type: "warning"
      }
    );
    const res = await deletePod({
      env: row.env,
      ns: row.namespace,
      pod_name: pod.name
    });
    if ((res as any).success) {
      ElMessage.success("删除Pod成功");
      await loadPods(row);
    } else {
      ElMessage.error((res as any).message || "删除Pod失败");
    }
  } catch (error) {
    if (error !== "cancel" && error !== "close") {
      console.error("删除Pod失败:", error);
      ElMessage.error("删除Pod失败");
    }
  }
};

watch(createDialogVisible, visible => {
  if (!visible && createYamlEditor) {
    createYamlEditor.dispose();
    createYamlEditor = null;
  }
});

watch(editDialogVisible, visible => {
  if (!visible && yamlEditor) {
    yamlEditor.dispose();
    yamlEditor = null;
  }
});

onMounted(async () => {
  await getEnvOptions();
  if (searchForm.env && searchForm.ns) {
    await handleSearch();
  }
});
</script>

<style scoped>
.stateful-manager-container {
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

.query-form .el-form-item {
  margin-right: 16px;
}

.query-form .el-form-item.right-auto {
  margin-left: auto;
  margin-right: 0;
}

.mt-2 {
  margin-top: 8px;
}

.pod-detail-container {
  padding: 16px;
  border-radius: 4px;
  background-color: #f8f9fa;
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

.create-yaml-editor {
  height: calc(100vh - 120px);
}

.update-method-container {
  padding: 20px 0;
}

.method-description {
  margin-top: 16px;
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

.log-toolbar {
  display: none;
}

.ellipsis-text {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  text-align: left;
}
</style>

<style scoped>
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
}

.log-controls-header .el-button {
  height: 24px !important;
  padding: 4px 8px !important;
  font-size: 11px !important;
}

.log-status {
  margin-left: 12px;
}

.status-connected {
  font-weight: bold;
  color: #67c23a;
}

.status-disconnected {
  font-weight: bold;
  color: #f56c6c;
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
  padding: 2px 0;
  margin-bottom: 2px;
}

.log-error {
  padding: 2px 4px;
  color: #f56c6c;
  background-color: rgb(245 108 108 / 10%);
  border-radius: 2px;
}

.log-warn {
  padding: 2px 4px;
  color: #e6a23c;
  background-color: rgb(230 162 60 / 10%);
  border-radius: 2px;
}

.log-info {
  color: #409eff;
}

.log-search-container {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-left: 16px;
}

.log-search-container .el-button {
  height: 24px !important;
  padding: 4px 8px !important;
  font-size: 11px !important;
}

.search-info {
  font-size: 12px;
  color: #606266;
  white-space: nowrap;
}
</style>

<style>
/* 搜索高亮样式（非 scoped） */
.search-highlight {
  padding: 1px 3px;
  color: #000 !important;
  background-color: #ffff00 !important;
  border-radius: 3px;
  font-weight: bold;
  box-shadow: 0 0 2px rgba(255, 255, 0, 0.5);
}

.search-highlight-current {
  padding: 1px 3px;
  font-weight: bold;
  color: #fff !important;
  background-color: #ff6600 !important;
  border-radius: 3px;
  box-shadow: 0 0 4px rgba(255, 102, 0, 0.8);
  animation: pulse 1s infinite;
}

@keyframes pulse {
  0% {
    box-shadow: 0 0 4px rgba(255, 102, 0, 0.8);
  }
  50% {
    box-shadow: 0 0 8px rgba(255, 102, 0, 1);
  }
  100% {
    box-shadow: 0 0 4px rgba(255, 102, 0, 0.8);
  }
}

/* 优化日志弹窗的标题栏样式 */
.el-dialog__header {
  position: relative !important;
  display: flex !important;
  align-items: center !important;
  min-height: 28px !important;
  padding: 0 40px 1px 10px !important;
  margin: 0 !important;
}

.el-overlay-dialog {
  overflow: hidden !important;
}

.el-dialog__headerbtn {
  position: absolute !important;
  top: 50% !important;
  right: 5px !important;
  z-index: 10 !important;
  width: 24px !important;
  height: 24px !important;
  transform: translateY(-50%) !important;
}

.el-dialog__title {
  flex: 1 !important;
  padding: 0 !important;
  margin: 0 !important;
  font-size: 12px !important;
  line-height: 1.2 !important;
}
</style>
