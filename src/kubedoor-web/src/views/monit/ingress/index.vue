<template>
  <div class="ingress-manager-container">
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
          class="hide-expand"
          :data="filteredTableData"
          style="width: 100%"
          stripe
          border
          element-loading-text="加载中..."
          row-key="id"
          :expand-row-keys="expandedRowKeys"
        >
          <el-table-column
            prop="namespace"
            label="命名空间"
            min-width="120"
            show-overflow-tooltip
          />
          <el-table-column
            prop="name"
            label="Ingress名称"
            min-width="220"
            show-overflow-tooltip
            sortable
          />
          <el-table-column
            prop="ingress_class"
            label="IngressClass"
            min-width="120"
            align="center"
          >
            <template #default="scope">
              <el-tag
                v-if="scope.row.ingress_class"
                type="success"
                size="small"
              >
                {{ scope.row.ingress_class }}
              </el-tag>
              <span v-else>-</span>
            </template>
          </el-table-column>
          <el-table-column
            prop="tls_secret_names"
            label="TLS Secrets"
            min-width="130"
            align="center"
            show-overflow-tooltip
            sortable
          >
            <template #default="scope">
              <div
                v-if="
                  Array.isArray(scope.row.tls_secret_names) &&
                  scope.row.tls_secret_names.length
                "
                class="tag-list tls-secrets-tags"
              >
                <el-tag
                  v-for="item in scope.row.tls_secret_names"
                  :key="item"
                  size="small"
                  effect="plain"
                  type="danger"
                >
                  {{ item }}
                </el-tag>
              </div>
              <span v-else>{{
                formatListField(scope.row.tls_secret_names)
              }}</span>
            </template>
          </el-table-column>
          <el-table-column
            prop="rules_hosts"
            label="域名"
            min-width="320"
            show-overflow-tooltip
            sortable
          >
            <template #default="scope">
              <div
                v-if="
                  Array.isArray(scope.row.rules_hosts) &&
                  scope.row.rules_hosts.length
                "
                class="tag-list"
              >
                <el-tag
                  v-for="host in scope.row.rules_hosts"
                  :key="host"
                  size="small"
                  effect="plain"
                >
                  {{ host }}
                </el-tag>
              </div>
              <span v-else>{{ formatListField(scope.row.rules_hosts) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="规则" min-width="100" align="center">
            <template #default="scope">
              <el-button
                type="primary"
                size="small"
                plain
                @click.stop="handleRulesDetail(scope.row)"
              >
                明细
              </el-button>
            </template>
          </el-table-column>
          <el-table-column
            prop="creation_timestamp"
            label="创建时间"
            min-width="130"
            align="center"
            sortable
          >
            <template #default="scope">
              {{ formatCreationTime(scope.row.creation_timestamp) }}
            </template>
          </el-table-column>
          <el-table-column
            type="expand"
            class-name="rules-expand-column"
            label-class-name="rules-expand-column"
            width="0"
            min-width="0"
            :resizable="false"
          >
            <template #default="scope">
              <div
                v-loading="scope.row.rulesLoading"
                class="rules-detail-container"
              >
                <div
                  v-if="scope.row.rulesDetail && scope.row.rulesDetail.length"
                  class="rules-wrapper"
                >
                  <div
                    v-for="(rule, index) in scope.row.rulesDetail"
                    :key="rule.host || index"
                    class="rule-host-container"
                  >
                    <h4>
                      域名：{{ rule.host || "默认" }}
                      <span class="rule-host-count"
                        >（{{ rule.paths.length }} 条路径）</span
                      >
                    </h4>
                    <el-table
                      :data="rule.paths"
                      border
                      size="small"
                      style="width: 100%"
                    >
                      <el-table-column
                        prop="path"
                        label="路径"
                        min-width="120"
                        align="center"
                        show-overflow-tooltip
                      />
                      <el-table-column
                        prop="pathType"
                        label="PathType"
                        min-width="150"
                        align="center"
                      />
                      <el-table-column
                        prop="backend_name"
                        label="后端服务"
                        min-width="200"
                        show-overflow-tooltip
                      />
                      <el-table-column
                        prop="backend_port"
                        label="端口"
                        min-width="120"
                        align="center"
                      />
                      <el-table-column
                        label="Property"
                        min-width="300"
                        show-overflow-tooltip
                      >
                        <template #default="scope">
                          <div v-if="scope.row.property">
                            <div
                              v-for="(value, key) in scope.row.property"
                              :key="key"
                            >
                              {{ key }}: {{ value }}
                            </div>
                          </div>
                        </template>
                      </el-table-column>
                    </el-table>
                  </div>
                </div>
                <div v-else-if="!scope.row.rulesLoading" class="no-data">
                  暂无规则数据
                </div>
              </div>
            </template>
          </el-table-column>
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
      </el-card>
    </div>

    <el-dialog
      v-model="editDialogVisible"
      title="编辑Ingress"
      width="95%"
      top="2.5vh"
      :close-on-click-modal="false"
      destroy-on-close
    >
      <div class="edit-container">
        <div class="yaml-editor-container">
          <div class="editor-header">
            <span class="editor-title">Ingress YAML配置</span>
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

    <el-dialog
      v-model="createDialogVisible"
      title="新建Ingress"
      fullscreen
      :close-on-click-modal="false"
      destroy-on-close
    >
      <div class="edit-container">
        <div class="yaml-editor-container">
          <div class="editor-header">
            <span class="editor-title">Ingress YAML配置</span>
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
import { ref, reactive, computed, onMounted, nextTick } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { Refresh } from "@element-plus/icons-vue";
import { getAgentNames } from "@/api/istio";
import { getPromNamespace } from "@/api/monit";
import { getIngressList, getIngressRules } from "@/api/ingress";
import {
  getServiceContent as getResourceContent,
  updateServiceContent as updateResourceContent,
  deleteK8sResource
} from "@/api/service";
import { useSearchStoreHook } from "@/store/modules/search";
import * as monaco from "monaco-editor";
import * as yaml from "js-yaml";
import { YAMLException } from "js-yaml";

defineOptions({
  name: "IngressManager"
});

const RESOURCE_TYPE = "ingress";

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
const expandedRowKeys = ref<string[]>([]);

const editDialogVisible = ref(false);
const saveLoading = ref(false);
const currentEditIngress = ref<any>(null);
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
    const candidates = [
      item.name,
      item.namespace,
      item.ingress_class,
      Array.isArray(item.tls_secret_names)
        ? item.tls_secret_names.join(",")
        : item.tls_secret_names,
      Array.isArray(item.rules_hosts)
        ? item.rules_hosts.join(",")
        : item.rules_hosts
    ];
    return candidates.some(
      field =>
        typeof field === "string" && field.toLowerCase().includes(keyword)
    );
  });
});

const getEnvOptions = async (): Promise<void> => {
  try {
    const res = await getAgentNames();
    if (res.data && res.data.length > 0) {
      envOptions.value = res.data.map((item: string) => item);
      if (searchStore.env && envOptions.value.includes(searchStore.env)) {
        searchForm.env = searchStore.env;
        await getNsOptions(searchStore.env);
      } else {
        searchForm.env = res.data[0];
        await getNsOptions(res.data[0]);
      }
    }
  } catch (error) {
    console.error("获取K8S环境列表失败:", error);
    ElMessage.error("获取K8S环境列表失败");
  }
};

const handleEnvChange = async (val: string) => {
  searchForm.ns = "";
  searchStore.setEnv(val);
  if (val) {
    await getNsOptions(val);
    handleSearch();
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

const getNsOptions = async (env: string, flush = false): Promise<boolean> => {
  if (!env) {
    nsOptions.value = [];
    searchForm.ns = "";
    searchStore.setNamespace("");
    return false;
  }

  try {
    const res = await getPromNamespace(env, flush);
    if (res.data) {
      nsOptions.value = res.data.map((item: string) => item);
      if (
        searchStore.namespace &&
        nsOptions.value.includes(searchStore.namespace)
      ) {
        searchForm.ns = searchStore.namespace;
      } else {
        searchForm.ns = res.data[0] || "";
        searchStore.setNamespace(res.data[0] || "");
      }
    } else {
      nsOptions.value = [];
      searchForm.ns = "";
      searchStore.setNamespace("");
    }
    return true;
  } catch (error) {
    console.error("获取命名空间列表失败:", error);
    ElMessage.error("获取命名空间列表失败");
    nsOptions.value = [];
    searchForm.ns = "";
    searchStore.setNamespace("");
    return false;
  }
};

const handleSearch = async () => {
  if (!searchForm.env) {
    ElMessage.warning("请选择K8S环境");
    return;
  }

  const namespace: string | undefined = searchForm.ns
    ? searchForm.ns
    : undefined;

  searchStore.setNamespace(searchForm.ns || "");

  loading.value = true;
  try {
    const res = await getIngressList(searchForm.env, namespace);
    if (res.success && res.data) {
      tableData.value = res.data.map(item => ({
        ...item,
        env: item.env || searchForm.env,
        namespace: item.namespace || searchForm.ns,
        id: `${item.env || searchForm.env}-${item.namespace || searchForm.ns}-${item.name}`,
        rulesDetail: [],
        rulesLoading: false
      }));
    } else {
      tableData.value = [];
    }
  } catch (error) {
    console.error("获取Ingress列表失败:", error);
    ElMessage.error("获取Ingress列表失败");
    tableData.value = [];
  } finally {
    loading.value = false;
  }
};

const handleRefresh = () => {
  handleSearch();
};

const buildRulesDetail = (data: Record<string, any>) => {
  if (!data || typeof data !== "object") return [];
  return Object.entries(data).map(([host, paths]) => ({
    host,
    paths: Array.isArray(paths) ? paths : []
  }));
};

const handleRulesDetail = async (row: any) => {
  if (expandedRowKeys.value.includes(row.id)) {
    expandedRowKeys.value = expandedRowKeys.value.filter(id => id !== row.id);
    return;
  }

  row.rulesLoading = true;
  try {
    const res = await getIngressRules(row.env, row.namespace, row.name);
    if (res.success && res.data) {
      row.rulesDetail = buildRulesDetail(res.data);
    } else {
      row.rulesDetail = [];
    }
    expandedRowKeys.value.push(row.id);
  } catch (error) {
    console.error("获取Ingress规则失败:", error);
    ElMessage.error("获取Ingress规则失败");
    row.rulesDetail = [];
  } finally {
    row.rulesLoading = false;
  }
};

const handleEdit = async (row: any) => {
  currentEditIngress.value = row;
  editDialogVisible.value = true;

  await nextTick();
  await initYamlEditor();

  try {
    const res = await getResourceContent(
      row.env,
      row.namespace,
      row.name,
      RESOURCE_TYPE
    );
    if (res.success && res.data && yamlEditor) {
      yamlEditor.setValue(res.data);
    }
  } catch (error) {
    console.error("获取Ingress内容失败:", error);
    ElMessage.error("获取Ingress内容失败");
  }
};

const handleDelete = async (row: any) => {
  if (!row?.env || !row?.namespace || !row?.name) {
    ElMessage.error("缺少删除Ingress所需信息");
    return;
  }

  try {
    await ElMessageBox.confirm(
      `确定要删除 Ingress "${row.name}" 吗？`,
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
      RESOURCE_TYPE,
      row.name
    );

    if (res.success) {
      ElMessage.success("Ingress 删除成功");
      handleSearch();
    } else {
      ElMessage.error(res.message || "Ingress 删除失败");
    }
  } catch (error) {
    if (error !== "cancel" && error !== "close") {
      console.error("删除Ingress失败:", error);
      ElMessage.error("删除Ingress失败");
    }
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
    const res = await updateResourceContent(
      searchForm.env,
      selectedCreateUpdateMethod.value,
      yamlContent
    );

    if (res.success) {
      ElMessage.success(`Ingress ${selectedCreateUpdateMethod.value} 更新成功`);
      createUpdateMethodDialogVisible.value = false;
      createDialogVisible.value = false;
      handleSearch();
    } else {
      ElMessage.error(
        res.message || `Ingress ${selectedCreateUpdateMethod.value} 更新失败`
      );
    }
  } catch (error) {
    console.error("更新Ingress失败:", error);
    ElMessage.error("更新Ingress失败");
  } finally {
    createSaveLoading.value = false;
  }
};

const handleSave = async () => {
  if (!yamlEditor || !currentEditIngress.value) {
    ElMessage.error("编辑器未初始化或未选择Ingress");
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
  if (!yamlEditor || !currentEditIngress.value) {
    ElMessage.error("编辑器未初始化或未选择Ingress");
    return;
  }

  const yamlContent = yamlEditor.getValue();

  try {
    saveLoading.value = true;
    const res = await updateResourceContent(
      currentEditIngress.value.env,
      selectedUpdateMethod.value,
      yamlContent
    );

    if (res.success) {
      ElMessage.success(`Ingress ${selectedUpdateMethod.value} 更新成功`);
      updateMethodDialogVisible.value = false;
      editDialogVisible.value = false;
      handleSearch();
    } else {
      ElMessage.error(
        res.message || `Ingress ${selectedUpdateMethod.value} 更新失败`
      );
    }
  } catch (error) {
    console.error("更新Ingress失败:", error);
    ElMessage.error("更新Ingress失败");
  } finally {
    saveLoading.value = false;
  }
};

const formatListField = (value: string | string[] | null | undefined) => {
  if (!value) return "-";
  if (Array.isArray(value)) {
    const filtered = value.filter(item => !!item);
    return filtered.length ? filtered.join(", ") : "-";
  }
  return value;
};

const formatCreationTime = (timestamp: string) => {
  if (!timestamp) return "-";

  try {
    const date = new Date(timestamp);
    const beijingTime = new Date(date.getTime() + 8 * 60 * 60 * 1000);

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

onMounted(async () => {
  await getEnvOptions();
  if (searchForm.env) {
    await handleSearch();
  }
});
</script>

<style scoped>
.ingress-manager-container {
  padding: 1px;
}

.create-yaml-editor {
  height: calc(100vh - 120px);
}

.method-radio-row {
  display: flex;
  flex-wrap: nowrap;
  gap: 12px;
  align-items: center;
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

.tag-list {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}

.tls-secrets-tags {
  justify-content: center;
}

.rules-detail-container {
  padding: 16px;
  background-color: #f8f9fa;
  border-radius: 4px;
}

.rules-wrapper {
  display: flex;
  flex-direction: column;
  gap: 24px;
}

.rule-host-container h4 {
  margin: 0 0 12px;
  font-size: 16px;
  font-weight: bold;
  color: #409eff;
}

.rule-host-count {
  margin-left: 8px;
  font-size: 12px;
  color: #909399;
}

.no-data {
  padding: 20px 0;
  font-size: 14px;
  color: #909399;
  text-align: center;
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

.update-method-container .el-radio {
  margin: 0;
  font-size: 16px;
  font-weight: 500;
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
</style>

<style>
.hide-expand .el-table__expand-icon {
  display: none;
}

.hide-expand th.rules-expand-column,
.hide-expand td.rules-expand-column {
  width: 0 !important;
  min-width: 0 !important;
  padding: 0 !important;
  border: none !important;
}

.hide-expand th.rules-expand-column .cell,
.hide-expand td.rules-expand-column .cell {
  display: none !important;
}
</style>
