<template>
  <div class="level1-vs-container">
    <!-- 警告提示 -->
    <div style="max-width: 600px; margin: 0 auto">
      <el-alert
        title="内部试用阶段，使用MySQL数据源，如需使用联系作者。"
        type="warning"
        center
        show-icon
        effect="dark"
        style="margin-bottom: 16px"
      />
    </div>
    <!-- 搜索表单 -->
    <div class="search-section">
      <el-form :model="searchForm" inline style="margin-bottom: -18px">
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
          <el-button
            type="primary"
            :icon="Plus"
            style="margin-left: 8px"
            @click="showCreateDialog = true"
          >
            新增
          </el-button>
          <el-button
            type="success"
            style="margin-left: 8px"
            @click="showCollectDialog = true"
          >
            采集VirtualService
          </el-button>
        </el-form-item>
      </el-form>
    </div>

    <!-- 表格数据 -->
    <div class="mt-2">
      <el-card v-loading="loading">
        <el-table
          v-loading="loading"
          :data="filteredTableData"
          style="width: 100%"
          stripe
          border
          element-loading-text="加载中..."
          :default-sort="{ prop: 'route_count', order: 'descending' }"
          @sort-change="handleSortChange"
        >
          <el-table-column
            prop="id"
            label="ID"
            width="60"
            show-overflow-tooltip
          />
          <el-table-column
            prop="name"
            label="名称"
            min-width="200"
            show-overflow-tooltip
            sortable
          />
          <el-table-column
            prop="namespace"
            label="命名空间"
            min-width="150"
            show-overflow-tooltip
            sortable
          />
          <el-table-column
            prop="hosts"
            label="Hosts"
            min-width="250"
            show-overflow-tooltip
            sortable
          />
          <el-table-column
            prop="df_forward_detail"
            label="默认路由"
            min-width="300"
            show-overflow-tooltip
            sortable
          />
          <el-table-column
            prop="df_forward_timeout"
            label="超时"
            min-width="80"
            show-overflow-tooltip
            sortable
          />
          <el-table-column
            prop="route_count"
            label="规则数"
            min-width="100"
            show-overflow-tooltip
            sortable
          />
          <el-table-column
            prop="updated_at"
            label="更新时间"
            min-width="180"
            show-overflow-tooltip
            sortable
          />
          <el-table-column label="操作" width="120" align="center">
            <template #default="scope">
              <el-button
                type="primary"
                size="small"
                :icon="View"
                @click="handleView(scope.row)"
              >
                查看
              </el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-card>
    </div>

    <!-- 创建VirtualService对话框 -->
    <el-dialog
      v-model="showCreateDialog"
      title="创建VirtualService"
      width="600px"
      :close-on-click-modal="false"
    >
      <el-form
        ref="createFormRef"
        :model="createForm"
        :rules="createFormRules"
        label-width="140px"
      >
        <el-form-item label="关联集群" prop="k8s_clusters">
          <el-select
            v-model="createForm.k8s_clusters"
            multiple
            placeholder="请选择K8S集群"
            style="width: 100%"
          >
            <el-option
              v-for="cluster in clusterOptions"
              :key="cluster"
              :label="cluster"
              :value="cluster"
            />
          </el-select>
        </el-form-item>

        <el-form-item label="VS名称" prop="name">
          <el-input
            v-model="createForm.name"
            placeholder="请输入VirtualService名称（只能包含小写字母、数字和中横线）"
            @input="handleNameInput"
          />
        </el-form-item>

        <el-form-item label="命名空间" prop="namespace">
          <el-input
            v-model="createForm.namespace"
            placeholder="请输入命名空间"
          />
        </el-form-item>

        <el-form-item label="关联网关" prop="gateways">
          <el-input
            v-model="createForm.gateways"
            placeholder="请输入网关，多个用逗号分隔"
          />
        </el-form-item>

        <el-form-item label="Host列表" prop="hosts">
          <div class="hosts-input">
            <el-tag
              v-for="(host, index) in createForm.hosts"
              :key="index"
              closable
              style="margin-right: 8px; margin-bottom: 4px"
              @close="removeHost(index)"
            >
              {{ host }}
            </el-tag>
            <el-input
              v-if="hostInputVisible"
              ref="hostInputRef"
              v-model="hostInputValue"
              size="small"
              style="width: 200px"
              @keyup.enter="handleHostInputConfirm"
              @blur="handleHostInputConfirm"
            />
            <el-button v-else size="small" @click="showHostInput">
              + 添加Host
            </el-button>
          </div>
        </el-form-item>

        <el-form-item label="默认规则">
          <el-select
            v-model="createForm.df_forward_type"
            placeholder="请选择默认规则类型"
            clearable
            @change="handleForwardTypeChange"
          >
            <el-option label="Route" value="route" />
            <el-option label="Delegate" value="delegate" />
          </el-select>
        </el-form-item>

        <!-- Route类型的字段 -->
        <template v-if="createForm.df_forward_type === 'route'">
          <el-form-item label="Service" prop="route_service">
            <div style="display: flex; gap: 8px; align-items: center">
              <el-select
                v-model="createForm.route_service"
                placeholder="请先选择命名空间"
                style="flex: 1; min-width: 200px"
                filterable
                allow-create
                @focus="
                  () =>
                    fetchServiceOptions(
                      createForm.k8s_clusters[0],
                      createForm.route_namespace
                    )
                "
                @change="
                  () =>
                    fetchServiceOptions(
                      createForm.k8s_clusters[0],
                      createForm.route_namespace
                    )
                "
              >
                <el-option
                  v-for="service in serviceOptions"
                  :key="service"
                  :label="service"
                  :value="service"
                />
              </el-select>
              <span>.</span>
              <el-select
                v-model="createForm.route_namespace"
                placeholder="命名空间"
                style="flex: 1; min-width: 100px"
                filterable
                allow-create
                @focus="() => fetchNamespaceOptions(createForm.k8s_clusters[0])"
                @change="
                  () =>
                    fetchServiceOptions(
                      createForm.k8s_clusters[0],
                      createForm.route_namespace
                    )
                "
              >
                <el-option
                  v-for="namespace in namespaceOptions"
                  :key="namespace"
                  :label="namespace"
                  :value="namespace"
                />
              </el-select>
              <span>.svc.cluster.local</span>
            </div>
          </el-form-item>

          <!-- 端口、刷新按钮和默认超时在同一行 -->
          <el-row :gutter="20">
            <el-col :span="12">
              <el-form-item label="端口" prop="route_port">
                <div style="display: flex; gap: 8px; align-items: center">
                  <el-input-number
                    v-model="createForm.route_port"
                    :min="0"
                    :max="65535"
                    placeholder="请输入端口号"
                    style="flex: 1"
                    controls-position="right"
                  />
                  <el-button
                    :icon="Refresh"
                    type="primary"
                    plain
                    size="small"
                    title="获取服务第一个端口号"
                    @click="refreshPortForCreate"
                  />
                </div>
              </el-form-item>
            </el-col>
            <el-col :span="12">
              <el-form-item label="默认超时" prop="df_forward_timeout">
                <el-input
                  v-model="createForm.df_forward_timeout"
                  placeholder="例如: 30s"
                />
              </el-form-item>
            </el-col>
          </el-row>

          <!-- Headers -->
          <el-form-item label="Headers">
            <div style="display: flex; gap: 8px; align-items: flex-start">
              <el-checkbox
                v-model="createForm.route_headers.enabled"
                label="启用"
                style="margin-top: 1px"
              />
              <el-input
                v-model="createForm.route_headers.value"
                :disabled="!createForm.route_headers.enabled"
                type="textarea"
                :rows="1"
                placeholder='默认：{"request": {"set": {"X-Forwarded-Proto": "https"}}}'
                style="flex: 1; min-width: 370px"
              />
            </div>
          </el-form-item>
        </template>

        <!-- Delegate类型的字段 -->
        <template v-if="createForm.df_forward_type === 'delegate'">
          <el-form-item label="VS名称" prop="delegate_name">
            <el-input
              v-model="createForm.delegate_name"
              placeholder="请输入委托名称"
            />
          </el-form-item>

          <el-form-item label="命名空间" prop="delegate_namespace">
            <el-input
              v-model="createForm.delegate_namespace"
              placeholder="请输入委托命名空间"
            />
          </el-form-item>
        </template>
      </el-form>

      <template #footer>
        <div class="dialog-footer">
          <el-button @click="showCreateDialog = false">取消</el-button>
          <el-button
            type="primary"
            :loading="createLoading"
            @click="handleCreate"
          >
            确认
          </el-button>
        </div>
      </template>
    </el-dialog>

    <!-- 采集路由弹框 -->
    <el-dialog
      v-model="showCollectDialog"
      title="采集VirtualService"
      width="500px"
      :close-on-click-modal="false"
    >
      <el-form
        ref="collectFormRef"
        :model="collectForm"
        :rules="collectFormRules"
        label-width="120px"
      >
        <el-form-item label="K8S集群" prop="env">
          <el-select
            v-model="collectForm.env"
            placeholder="请选择K8S集群"
            style="width: 100%"
            filterable
          >
            <el-option
              v-for="item in envOptions"
              :key="item"
              :label="item"
              :value="item"
            />
          </el-select>
        </el-form-item>

        <el-form-item label="确认操作" prop="confirmText">
          <el-input
            v-model="collectForm.confirmText"
            placeholder="请输入 yes 确认操作"
            style="width: 100%"
          />
          <div style="margin-top: 4px; font-size: 10px; color: #f56c6c">
            注意：如果该K8S已关联的VS也关联了别的K8S，则该VS关联的所有K8S也会解除该VS的关联，如果您没有一条VS关联多K8S则无影响。建议仅在首次初始化时候使用，请谨慎操作!
          </div>
        </el-form-item>
      </el-form>

      <template #footer>
        <div class="dialog-footer">
          <el-button @click="showCollectDialog = false">取消</el-button>
          <el-button
            type="primary"
            :loading="collectLoading"
            :disabled="collectForm.confirmText !== 'yes'"
            @click="handleCollectRoutes"
          >
            确认采集
          </el-button>
        </div>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted, nextTick } from "vue";
import { ElMessage } from "element-plus";

import { useRouter, useRoute } from "vue-router";
import { Refresh, View, Plus } from "@element-plus/icons-vue";
import { getPromNamespace, getPromServices } from "@/api/monit";
import {
  getVirtualServices,
  getAgentNames,
  collectVirtualServiceRoutes,
  getServiceFirstPort
} from "@/api/istio";

defineOptions({
  name: "Level1VirtualService"
});

// 定义表单数据
const searchForm = reactive({
  env: "",
  ns: "",
  keyword: ""
});

// 定义选项数据
const envOptions = ref<string[]>([]);
const nsOptions = ref<string[]>([]);
const nsRefreshing = ref(false);

// 表格数据
const tableData = ref<any[]>([]);
const loading = ref(false);
const router = useRouter();
const route = useRoute();

// 创建表单相关
const showCreateDialog = ref(false);
const createLoading = ref(false);
const createFormRef = ref();
const clusterOptions = ref<string[]>([]);
const hostInputVisible = ref(false);
const hostInputValue = ref("");
const hostInputRef = ref();

// Service和Namespace选项数据
const serviceOptions = ref<string[]>([]);
const namespaceOptions = ref<string[]>([]);

// 创建表单数据
const createForm = reactive({
  k8s_clusters: [] as string[],
  name: "",
  namespace: "istio-system",
  gateways: "istio-system/internal",
  hosts: [] as string[],
  df_forward_type: "",
  route_service: "",
  route_namespace: "",
  route_port: 0,
  df_forward_timeout: "",
  route_headers: {
    enabled: false,
    value: '{"request": {"set": {"X-Forwarded-Proto": "https"}}}'
  },
  delegate_name: "",
  delegate_namespace: ""
});

// 采集路由相关
const showCollectDialog = ref(false);
const collectLoading = ref(false);
const collectFormRef = ref();
const collectForm = reactive({
  env: "",
  confirmText: ""
});

// 采集路由表单验证规则
const collectFormRules = {
  env: [{ required: true, message: "请选择K8S集群", trigger: "change" }],
  confirmText: [
    { required: true, message: "请输入确认文本", trigger: "blur" },
    {
      validator: (rule: any, value: string, callback: any) => {
        if (value !== "yes") {
          callback(new Error("请输入 yes 确认操作"));
        } else {
          callback();
        }
      },
      trigger: "blur"
    }
  ]
};

// 表单验证规则
const createFormRules = {
  k8s_clusters: [
    { required: true, message: "请选择关联集群", trigger: "change" }
  ],
  name: [
    { required: true, message: "请输入VS名称", trigger: "blur" },
    {
      pattern: /^[a-z0-9-]+$/,
      message: "VS名称只能包含小写字母、数字和中横线",
      trigger: "blur"
    }
  ],
  namespace: [{ required: true, message: "请输入命名空间", trigger: "blur" }],
  gateways: [{ required: true, message: "请输入关联网关", trigger: "blur" }],
  hosts: [{ required: true, message: "请添加至少一个Host", trigger: "change" }],
  route_service: [
    { required: true, message: "请输入Service名", trigger: "blur" }
  ],
  route_namespace: [
    { required: true, message: "请输入命名空间", trigger: "blur" }
  ],
  route_port: [{ required: true, message: "请输入端口号", trigger: "blur" }],
  df_forward_timeout: [
    { required: true, message: "请输入默认超时", trigger: "blur" }
  ],
  delegate_name: [{ required: true, message: "请输入VS名称", trigger: "blur" }],
  delegate_namespace: [
    { required: true, message: "请输入命名空间", trigger: "blur" }
  ]
};

// 过滤后的表格数据
const filteredTableData = computed(() => {
  if (!searchForm.keyword) {
    return tableData.value;
  }

  const keyword = searchForm.keyword.toLowerCase();
  return tableData.value.filter(
    item =>
      item.name.toLowerCase().includes(keyword) ||
      (item.namespace && item.namespace.toLowerCase().includes(keyword)) ||
      (item.hosts && item.hosts.toLowerCase().includes(keyword))
  );
});

// 获取K8S环境列表
const getEnvOptions = async (): Promise<void> => {
  try {
    const res = await getAgentNames();
    if (res.data && res.data.length > 0) {
      envOptions.value = res.data.map(item => item);
      searchForm.env = res.data[0];
      await getNsOptions(res.data[0]);
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
  searchForm.ns = "";
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

// 获取命名空间列表
const getNsOptions = async (env: string, flush = false): Promise<boolean> => {
  if (!env) {
    nsOptions.value = [];
    searchForm.ns = "";
    return false;
  }

  try {
    const res = await getPromNamespace(env, flush);
    if (res.data) {
      nsOptions.value = res.data.map(item => item);
      if (res.data.includes("istio-system")) {
        searchForm.ns = "istio-system";
      } else {
        searchForm.ns = res.data[0] || "";
      }
    } else {
      nsOptions.value = [];
      searchForm.ns = "";
    }
    return true;
  } catch (error) {
    console.error("获取命名空间列表失败:", error);
    ElMessage.error("获取命名空间列表失败");
    nsOptions.value = [];
    searchForm.ns = "";
    return false;
  }
};

// 获取命名空间选项（用于创建表单）
const fetchNamespaceOptions = async (env: string) => {
  if (!env) {
    namespaceOptions.value = [];
    return;
  }
  try {
    const response = await getPromNamespace(env);
    namespaceOptions.value = response.data;
  } catch (error) {
    console.error("获取命名空间选项失败:", error);
    namespaceOptions.value = [];
  }
};

// 获取服务选项（用于创建表单）
const fetchServiceOptions = async (env: string, namespace: string) => {
  if (!env || !namespace) {
    serviceOptions.value = [];
    return;
  }
  try {
    const response = await getPromServices(env, namespace);
    serviceOptions.value = response.data;
  } catch (error) {
    console.error("获取服务选项失败:", error);
    serviceOptions.value = [];
  }
};

// 创建VirtualService时刷新端口号
const refreshPortForCreate = async () => {
  if (!createForm.route_service || !createForm.route_namespace) {
    ElMessage.warning("请先填写服务名和命名空间");
    return;
  }

  if (!createForm.k8s_clusters || createForm.k8s_clusters.length === 0) {
    ElMessage.warning("请先选择关联集群");
    return;
  }

  try {
    const result = await getServiceFirstPort(
      createForm.k8s_clusters[0],
      createForm.route_namespace,
      createForm.route_service
    );

    if (result && result.data && result.data.first_port) {
      createForm.route_port = result.data.first_port;
      ElMessage.success(`已获取到端口号: ${result.data.first_port}`);
    } else {
      ElMessage.error("未能获取到端口号");
    }
  } catch (error) {
    console.error("获取端口号失败:", error);
    ElMessage.error("获取端口号失败");
  }
};

// 处理搜索
const handleSearch = async () => {
  if (!searchForm.env) {
    ElMessage.warning("请选择K8S环境");
    return;
  }

  loading.value = true;
  try {
    const response = await getVirtualServices(
      searchForm.env,
      searchForm.ns || undefined
    );

    if (response.data) {
      tableData.value = response.data;
    } else {
      tableData.value = [];
    }
  } catch (error) {
    console.error("获取VirtualService数据失败:", error);
    ElMessage.error("获取VirtualService数据失败");
    tableData.value = [];
  } finally {
    loading.value = false;
  }
};

// 查看VirtualService详情
const handleView = (row: any) => {
  router.push({
    path: `/istio/level1-vs/detail/${row.id}`,
    query: {
      id: row.id,
      env: searchForm.env,
      ns: searchForm.ns
    }
  });
};

// 刷新数据
const handleRefresh = () => {
  handleSearch();
};

// 处理排序变化
const handleSortChange = ({
  prop,
  order
}: {
  prop: string;
  order: string | null;
}) => {
  if (prop === "df_forward_timeout") {
    tableData.value.sort((a, b) => {
      const aTimeout = a.df_forward_timeout || "";
      const bTimeout = b.df_forward_timeout || "";

      // 处理空值情况 - 空值始终排在末尾
      if (!aTimeout && !bTimeout) return 0;
      if (!aTimeout) return 1; // 空值排在末尾
      if (!bTimeout) return -1; // 空值排在末尾

      // 提取数值部分进行比较
      const aValue = parseFloat(aTimeout.replace(/[^0-9.]/g, "")) || 0;
      const bValue = parseFloat(bTimeout.replace(/[^0-9.]/g, "")) || 0;

      if (order === "ascending") {
        return aValue - bValue;
      } else if (order === "descending") {
        return bValue - aValue;
      }
      return 0;
    });
  } else if (prop === "df_forward_detail") {
    tableData.value.sort((a, b) => {
      const aDetail = a.df_forward_detail || "";
      const bDetail = b.df_forward_detail || "";

      // 处理空值情况 - 空值始终排在末尾
      if (!aDetail && !bDetail) return 0;
      if (!aDetail) return 1; // 空值排在末尾
      if (!bDetail) return -1; // 空值排在末尾

      // 字符串比较
      if (order === "ascending") {
        return aDetail.localeCompare(bDetail);
      } else if (order === "descending") {
        return bDetail.localeCompare(aDetail);
      }
      return 0;
    });
  }
};

// 显示主机输入框
const showHostInput = () => {
  hostInputVisible.value = true;
  nextTick(() => {
    hostInputRef.value?.focus();
  });
};

// 确认主机输入
const handleHostInputConfirm = () => {
  if (
    hostInputValue.value &&
    !createForm.hosts.includes(hostInputValue.value)
  ) {
    createForm.hosts.push(hostInputValue.value);
  }
  hostInputVisible.value = false;
  hostInputValue.value = "";
};

// 移除主机
const removeHost = (index: number) => {
  createForm.hosts.splice(index, 1);
};

// 处理VS名称输入，只允许小写字母、数字和中横线
const handleNameInput = (value: string) => {
  // 过滤掉不符合规则的字符
  const filteredValue = value.replace(/[^a-z0-9-]/g, "");
  if (filteredValue !== value) {
    createForm.name = filteredValue;
  }
};

// 处理转发类型变化
const handleForwardTypeChange = (value: string) => {
  // 清空相关字段
  if (value === "route") {
    createForm.delegate_name = "";
    createForm.delegate_namespace = "";
  } else if (value === "delegate") {
    createForm.route_service = "";
    createForm.route_namespace = "";
    createForm.route_port = 0;
    createForm.df_forward_timeout = "";
    createForm.route_headers.enabled = false;
    createForm.route_headers.value =
      '{"request": {"set": {"X-Forwarded-Proto": "https"}}}';
  }
};

// 重置创建表单
const resetCreateForm = () => {
  Object.assign(createForm, {
    k8s_clusters: [],
    name: "",
    namespace: "istio-system",
    gateways: "istio-system/internal",
    hosts: [],
    df_forward_type: "",
    route_service: "",
    route_namespace: "",
    route_port: 0,
    df_forward_timeout: "",
    route_headers: {
      enabled: false,
      value: '{"request": {"set": {"X-Forwarded-Proto": "https"}}}'
    },
    delegate_name: "",
    delegate_namespace: ""
  });
  createFormRef.value?.clearValidate();
};

// 创建VirtualService
const handleCreate = async () => {
  if (!createFormRef.value) return;

  try {
    const valid = await createFormRef.value.validate();
    if (!valid) return;

    createLoading.value = true;

    // 构建请求数据
    const requestData: any = {
      k8s_clusters: createForm.k8s_clusters,
      name: createForm.name,
      namespace: createForm.namespace,
      gateways: createForm.gateways
        .split(",")
        .map(g => g.trim())
        .filter(g => g),
      hosts: createForm.hosts,
      protocol: "http"
    };

    // 只有选择了默认规则类型才添加相关字段
    if (createForm.df_forward_type) {
      requestData.df_forward_type = createForm.df_forward_type;

      if (createForm.df_forward_type === "route") {
        const routeItem: any = {
          destination: {
            host: `${createForm.route_service}.${createForm.route_namespace}.svc.cluster.local`,
            port: { number: createForm.route_port }
          }
        };

        // 处理headers字段
        if (
          createForm.route_headers.enabled &&
          createForm.route_headers.value
        ) {
          try {
            routeItem.headers = JSON.parse(createForm.route_headers.value);
          } catch (e) {
            console.warn("Headers JSON格式错误:", e);
          }
        }

        requestData.df_forward_detail = [routeItem];
        requestData.df_forward_timeout = createForm.df_forward_timeout;
      } else if (createForm.df_forward_type === "delegate") {
        requestData.df_forward_detail = {
          name: createForm.delegate_name,
          namespace: createForm.delegate_namespace
        };
      }
    }

    // 调用API
    const response = await fetch("/api/istio/vs", {
      method: "POST",
      headers: {
        "Content-Type": "application/json"
      },
      body: JSON.stringify(requestData)
    });

    const result = await response.json();

    // 检查响应是否成功
    if (response.ok && result && result.success === true) {
      ElMessage.success("创建VirtualService成功");
      showCreateDialog.value = false;
      resetCreateForm();
      handleSearch(); // 刷新列表
    } else {
      // 显示失败信息，包含完整的响应内容
      const errorMsg = result ? JSON.stringify(result, null, 2) : "未知错误";
      console.error("创建VirtualService失败 - 响应:", result);
      ElMessage.error({
        message: `创建VirtualService失败:\n${errorMsg}`,
        duration: 10000,
        showClose: true
      });
    }
  } catch (error) {
    console.error("创建VirtualService失败:", error);
    const errorMsg = error.message || error;
    ElMessage.error({
      message: `创建VirtualService失败:\n${errorMsg}`,
      duration: 10000,
      showClose: true
    });
  } finally {
    createLoading.value = false;
  }
};

// 获取集群选项
const getClusterOptions = async () => {
  try {
    // 这里可以调用获取集群列表的API
    // 暂时使用环境选项作为集群选项
    clusterOptions.value = envOptions.value;
  } catch (error) {
    console.error("获取集群选项失败:", error);
  }
};

// 采集路由处理方法
const handleCollectRoutes = async () => {
  if (!collectFormRef.value) return;

  try {
    const valid = await collectFormRef.value.validate();
    if (!valid) return;

    collectLoading.value = true;

    // 调用采集路由API
    const response = await collectVirtualServiceRoutes(collectForm.env);

    // 检查响应中的 success 字段
    if (response.success === true) {
      ElMessage.success({
        message: response.message || "路由采集成功！",
        duration: 3000,
        showClose: true
      });

      // 关闭弹框并重置表单
      showCollectDialog.value = false;
      resetCollectForm();

      // 刷新数据
      if (searchForm.env === collectForm.env) {
        await handleSearch();
      }
    } else {
      // 失败情况，直接显示服务器返回的内容
      console.error("采集路由失败，完整响应:", response);
      ElMessage.error({
        message: response.error || "采集路由失败",
        duration: 5000,
        showClose: true
      });
    }
  } catch (error) {
    console.error("采集路由失败:", error);
    // HTTP拦截器已经显示了错误消息，这里不再重复显示
    // 只有在特殊情况下才显示额外的错误信息
  } finally {
    collectLoading.value = false;
  }
};

// 重置采集表单
const resetCollectForm = () => {
  Object.assign(collectForm, {
    env: "",
    confirmText: ""
  });
  collectFormRef.value?.clearValidate();
};

// 页面初始化
onMounted(async () => {
  await getEnvOptions();
  await getClusterOptions();

  // 从URL参数中恢复环境和命名空间选择
  const envFromQuery = route.query.env as string;
  const nsFromQuery = route.query.ns as string;

  if (envFromQuery && envOptions.value.includes(envFromQuery)) {
    searchForm.env = envFromQuery;
    await getNsOptions(envFromQuery);

    if (nsFromQuery && nsOptions.value.includes(nsFromQuery)) {
      searchForm.ns = nsFromQuery;
    }
  }

  if (searchForm.env) {
    await handleSearch();
  }
});
</script>

<style scoped>
.level1-vs-container {
  padding: 1px;
}

.search-section {
  margin-bottom: 2px;
}

.mt-2 {
  margin-top: 8px;
}
</style>
