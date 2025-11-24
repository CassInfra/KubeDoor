<template>
  <div class="configmap-manager-container">
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

        <el-form-item label="命名空间">
          <div class="namespace-select-wrapper">
            <el-select
              v-model="searchForm.ns"
              placeholder="请选择命名空间"
              class="!w-[180px]"
              filterable
              clearable
              @change="handleSearch"
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
          style="width: 100%"
          stripe
          border
          row-key="id"
          element-loading-text="加载中..."
        >
          <el-table-column
            prop="namespace"
            label="命名空间"
            min-width="160"
            show-overflow-tooltip
          />
          <el-table-column
            prop="name"
            label="ConfigMap名称"
            min-width="220"
            show-overflow-tooltip
            sortable
          />
          <el-table-column
            prop="data_keys"
            label="数据键"
            min-width="260"
            sortable
          >
            <template #default="scope">
              <div class="data-keys-container">
                <el-tag
                  v-for="(key, index) in scope.row.data_keys || []"
                  :key="`${scope.row.name}-${key}-${index}`"
                  size="small"
                  effect="plain"
                >
                  {{ key }}
                </el-tag>
                <span
                  v-if="
                    !scope.row.data_keys || scope.row.data_keys.length === 0
                  "
                  >-</span
                >
              </div>
            </template>
          </el-table-column>
          <el-table-column
            prop="creation_timestamp"
            label="创建时间"
            min-width="160"
            align="center"
            sortable
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
      </el-card>
    </div>

    <el-dialog
      v-model="editDialogVisible"
      title="编辑ConfigMap"
      width="95%"
      top="2.5vh"
      :close-on-click-modal="false"
      destroy-on-close
    >
      <div class="edit-container">
        <div class="yaml-editor-container">
          <div class="editor-header">
            <span class="editor-title">ConfigMap YAML配置</span>
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
          <el-radio value="apply">Apply - 应用（推荐）</el-radio>
          <el-radio value="replace">Replace - 替换</el-radio>
        </el-radio-group>
        <div class="method-description">
          <p v-if="selectedUpdateMethod === 'apply'">
            Apply方式会合并配置，适合大多数场景。
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

    <el-dialog
      v-model="createDialogVisible"
      title="新建ConfigMap"
      fullscreen
      :close-on-click-modal="false"
      destroy-on-close
    >
      <div class="edit-container">
        <div class="yaml-editor-container">
          <div class="editor-header">
            <span class="editor-title">ConfigMap YAML配置</span>
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
            Create方式会按照提供的YAML直接创建ConfigMap。
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
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted, nextTick, watch } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { Refresh } from "@element-plus/icons-vue";
import { getAgentNames } from "@/api/istio";
import { getPromNamespace } from "@/api/monit";
import {
  getConfigMapList,
  getServiceContent,
  updateServiceContent,
  deleteK8sResource
} from "@/api/service";
import { useSearchStoreHook } from "@/store/modules/search";
import * as monaco from "monaco-editor";
import * as yaml from "js-yaml";
import { YAMLException } from "js-yaml";

defineOptions({
  name: "ConfigMapManager"
});

const searchStore = useSearchStoreHook();

const searchForm = reactive({
  env: searchStore.env || "",
  ns: searchStore.namespace || "",
  keyword: ""
});

const envOptions = ref<string[]>([]);
const nsOptions = ref<string[]>([]);
const nsRefreshing = ref(false);
const tableData = ref<any[]>([]);
const loading = ref(false);

const editDialogVisible = ref(false);
const saveLoading = ref(false);
const currentEditConfigMap = ref<any>(null);
const yamlEditorRef = ref<HTMLElement | null>(null);
let yamlEditor: monaco.editor.IStandaloneCodeEditor | null = null;

const updateMethodDialogVisible = ref(false);
const selectedUpdateMethod = ref<"apply" | "replace">("apply");

const createDialogVisible = ref(false);
const createSaveLoading = ref(false);
const createYamlEditorRef = ref<HTMLElement | null>(null);
let createYamlEditor: monaco.editor.IStandaloneCodeEditor | null = null;

const createUpdateMethodDialogVisible = ref(false);
const selectedCreateUpdateMethod = ref<"create" | "apply" | "replace">(
  "create"
);

const filteredTableData = computed(() => {
  if (!searchForm.keyword) {
    return tableData.value;
  }
  const keyword = searchForm.keyword.toLowerCase();
  return tableData.value.filter(item => {
    return (
      item.env.toLowerCase().includes(keyword) ||
      (item.namespace && item.namespace.toLowerCase().includes(keyword)) ||
      (item.name && item.name.toLowerCase().includes(keyword))
    );
  });
});

const formatCreationTime = (timestamp: string) => {
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
    console.error("获取K8S环境列表失败:", error);
    ElMessage.error("获取K8S环境列表失败");
  }
};

const getNsOptions = async (env: string, flush = false): Promise<boolean> => {
  if (!env) {
    nsOptions.value = [];
    return false;
  }
  try {
    const res = await getPromNamespace(env, flush);
    if (res.data && res.data.length > 0) {
      nsOptions.value = res.data.map((item: string) => item);
      if (
        searchStore.namespace &&
        nsOptions.value.includes(searchStore.namespace)
      ) {
        searchForm.ns = searchStore.namespace;
      } else {
        searchForm.ns = res.data[0];
      }
      searchStore.setNamespace(searchForm.ns || "");
      await handleSearch();
    } else {
      nsOptions.value = [];
      searchForm.ns = "";
      searchStore.setNamespace("");
      tableData.value = [];
    }
    return true;
  } catch (error) {
    console.error("获取命名空间失败:", error);
    ElMessage.error("获取命名空间失败");
    return false;
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

const handleNamespaceRefresh = async () => {
  if (!searchForm.env || nsRefreshing.value) {
    return;
  }

  nsRefreshing.value = true;
  try {
    const refreshed = await getNsOptions(searchForm.env, true);
    if (refreshed) {
      ElMessage.success("命名空间已刷新");
    }
  } catch (error) {
    console.error("刷新命名空间列表失败:", error);
  } finally {
    nsRefreshing.value = false;
  }
};

const handleSearch = async () => {
  if (!searchForm.env) {
    tableData.value = [];
    return;
  }

  const namespace: string | undefined = searchForm.ns
    ? searchForm.ns
    : undefined;

  loading.value = true;
  try {
    searchStore.setEnv(searchForm.env);
    searchStore.setNamespace(searchForm.ns || "");
    const res = await getConfigMapList(searchForm.env, namespace);
    if (res.success && res.data) {
      tableData.value = res.data.map((item: any) => ({
        ...item,
        id: `${item.env}-${item.namespace}-${item.name}`
      }));
    } else {
      tableData.value = [];
      ElMessage.error(res.message || "获取ConfigMap列表失败");
    }
  } catch (error) {
    console.error("获取ConfigMap列表失败:", error);
    ElMessage.error("获取ConfigMap列表失败");
    tableData.value = [];
  } finally {
    loading.value = false;
  }
};

const handleRefresh = () => {
  handleSearch();
};

const handleEdit = async (row: any) => {
  currentEditConfigMap.value = row;
  editDialogVisible.value = true;

  await nextTick();
  await initYamlEditor();

  try {
    const res = await getServiceContent(
      row.env,
      row.namespace,
      row.name,
      "configmap"
    );
    if (res.success && res.data && yamlEditor) {
      yamlEditor.setValue(res.data);
    }
  } catch (error) {
    console.error("获取ConfigMap内容失败:", error);
    ElMessage.error("获取ConfigMap内容失败");
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

const handleSave = async () => {
  if (!yamlEditor || !currentEditConfigMap.value) {
    ElMessage.error("编辑器未初始化或未选择ConfigMap");
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
    console.error("YAML格式验证失败:", error);
    if (error instanceof YAMLException) {
      ElMessage.error(`YAML格式错误: ${error.message}`);
    } else {
      ElMessage.error("YAML格式验证失败");
    }
  }
};

const confirmUpdate = async () => {
  if (!yamlEditor || !currentEditConfigMap.value) {
    ElMessage.error("编辑器未初始化或未选择ConfigMap");
    return;
  }

  const yamlContent = yamlEditor.getValue();

  try {
    saveLoading.value = true;
    const res = await updateServiceContent(
      currentEditConfigMap.value.env,
      selectedUpdateMethod.value,
      yamlContent
    );

    if (res.success) {
      ElMessage.success(`ConfigMap ${selectedUpdateMethod.value} 更新成功`);
      updateMethodDialogVisible.value = false;
      editDialogVisible.value = false;
      handleSearch();
    } else {
      ElMessage.error(
        res.message || `ConfigMap ${selectedUpdateMethod.value} 更新失败`
      );
    }
  } catch (error) {
    console.error("更新ConfigMap失败:", error);
    ElMessage.error("更新ConfigMap失败");
  } finally {
    saveLoading.value = false;
  }
};

const handleDelete = async (row: any) => {
  if (!row?.env || !row?.namespace || !row?.name) {
    ElMessage.error("缺少删除ConfigMap所需信息");
    return;
  }

  try {
    await ElMessageBox.confirm(
      `确定要删除 ConfigMap \"${row.name}\" 吗？`,
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
      "configmap",
      row.name
    );

    if (res.success) {
      ElMessage.success("ConfigMap 删除成功");
      handleSearch();
    } else {
      ElMessage.error(res.message || "ConfigMap 删除失败");
    }
  } catch (error) {
    if (error !== "cancel" && error !== "close") {
      console.error("删除ConfigMap失败:", error);
      ElMessage.error("删除ConfigMap失败");
    }
  }
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
      ElMessage.success(
        `ConfigMap ${selectedCreateUpdateMethod.value} 更新成功`
      );
      createUpdateMethodDialogVisible.value = false;
      createDialogVisible.value = false;
      handleSearch();
    } else {
      ElMessage.error(
        res.message || `ConfigMap ${selectedCreateUpdateMethod.value} 更新失败`
      );
    }
  } catch (error) {
    console.error("提交ConfigMap失败:", error);
    ElMessage.error("提交ConfigMap失败");
  } finally {
    createSaveLoading.value = false;
  }
};

watch(editDialogVisible, visible => {
  if (!visible) {
    if (yamlEditor) {
      yamlEditor.dispose();
      yamlEditor = null;
    }
    currentEditConfigMap.value = null;
  }
});

watch(createDialogVisible, visible => {
  if (!visible) {
    if (createYamlEditor) {
      createYamlEditor.dispose();
      createYamlEditor = null;
    }
  }
});

onMounted(async () => {
  await getEnvOptions();
  if (searchForm.env) {
    await handleSearch();
  }
});
</script>

<style scoped>
.configmap-manager-container {
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

.data-keys-container {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.ml-2 {
  margin-left: 8px;
}
</style>
