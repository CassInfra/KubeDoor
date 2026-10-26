<template>
  <div>
    <!-- 警告提示 -->
    <div style="max-width: 600px; margin: 0 auto">
      <el-alert
        title="【固定节点均衡】模式还在内部开发测试阶段，请勿开启！"
        type="error"
        center
        show-icon
        effect="dark"
        style="margin-bottom: 16px"
      />
    </div>
    <div class="agent-management-container">
      <!-- 搜索表单 -->
      <div class="search-section">
        <el-form :model="searchForm" inline style="margin-bottom: -18px">
          <el-form-item label="K8S">
            <el-input
              v-model="searchForm.keyword"
              placeholder="请输入K8S"
              clearable
            />
          </el-form-item>

          <el-form-item>
            <el-button type="primary" @click="handleSearch">刷新</el-button>
            <el-button @click="handleReset">重置</el-button>
          </el-form-item>

          <!-- 勾选后出现:批量更新 / 批量重启选中的 agent -->
          <el-form-item v-if="selectedRows.length > 0">
            <el-button type="primary" plain @click="handleBatchUpdate">
              批量更新 ({{ selectedRows.length }})
            </el-button>
            <el-button type="warning" plain @click="handleBatchRestart">
              批量重启 ({{ selectedRows.length }})
            </el-button>
          </el-form-item>
        </el-form>
      </div>

      <!-- 表格数据 -->
      <div class="mt-2">
        <el-card v-loading="loading">
          <el-table
            ref="tableRef"
            :data="filteredTableData"
            style="width: 100%"
            stripe
            :default-sort="{ prop: 'key', order: 'ascending' }"
            @selection-change="handleSelectionChange"
          >
            <el-table-column
              type="selection"
              width="46"
              align="center"
              :selectable="isSelectable"
            />
            <el-table-column
              prop="key"
              label="K8S"
              min-width="120"
              show-overflow-tooltip
              sortable
            />
            <el-table-column label="别名" min-width="160">
              <template #header>
                <span class="ai-connection-header">
                  别名
                  <el-tooltip placement="top">
                    <template #content>
                      <div class="ai-connection-tip">{{ ALIAS_TIP }}</div>
                    </template>
                    <el-icon class="ai-connection-header__hint">
                      <QuestionFilled />
                    </el-icon>
                  </el-tooltip>
                </span>
              </template>
              <template #default="scope">
                <div class="alias-cell">
                  <el-tag
                    v-for="alias in scope.row.aliases"
                    :key="alias"
                    size="small"
                    effect="plain"
                  >
                    {{ alias }}
                  </el-tag>
                  <el-button
                    v-if="isAdmin"
                    size="small"
                    link
                    type="primary"
                    @click="handleEditAliases(scope.row)"
                  >
                    编辑
                  </el-button>
                </div>
              </template>
            </el-table-column>
            <el-table-column
              prop="online"
              label="状态"
              min-width="80"
              align="center"
              sortable
            >
              <template #default="scope">
                <el-tag :type="scope.row.online == true ? 'success' : 'danger'">
                  {{ scope.row.online == true ? "在线" : "离线" }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column
              prop="last_heartbeat"
              label="心跳"
              align="center"
              min-width="160"
              show-overflow-tooltip
              sortable
            />
            <el-table-column
              prop="ver"
              label="版本"
              min-width="100"
              align="center"
              show-overflow-tooltip
              sortable
            />
            <el-table-column label="操作" min-width="150" align="center">
              <template #default="scope">
                <el-button
                  type="primary"
                  size="small"
                  plain
                  :disabled="!scope.row.online"
                  @click="handleUpdate(scope.row)"
                >
                  更新
                </el-button>
                <el-button
                  type="warning"
                  size="small"
                  plain
                  :disabled="!scope.row.online"
                  @click="handleRestart(scope.row)"
                >
                  重启
                </el-button>
              </template>
            </el-table-column>
            <el-table-column
              label="AI Kubeconfig"
              min-width="230"
              align="center"
            >
              <template #header>
                <span class="ai-connection-header">
                  AI Kubeconfig
                  <el-tooltip placement="top">
                    <template #content>
                      <div class="ai-connection-tip">
                        这个 kubeconfig 会加密存储，仅供 AI
                        助手使用，可以提供更好的 AI 调用 K8S
                        的能力，不上传也不影响 AI 的功能。
                      </div>
                    </template>
                    <el-icon class="ai-connection-header__hint">
                      <QuestionFilled />
                    </el-icon>
                  </el-tooltip>
                </span>
              </template>
              <template #default="scope">
                <div class="ai-connection-cell">
                  <el-tooltip
                    :content="connectionSummary(scope.row.key)"
                    placement="top"
                  >
                    <el-tag
                      size="small"
                      :type="
                        connections[scope.row.key]?.configured
                          ? 'success'
                          : 'info'
                      "
                      effect="plain"
                    >
                      {{
                        connections[scope.row.key]?.configured
                          ? connections[scope.row.key].context || "已配置"
                          : aiAvailable
                            ? "未配置"
                            : "AI 未启用"
                      }}
                    </el-tag>
                  </el-tooltip>
                  <el-button
                    size="small"
                    link
                    type="primary"
                    :disabled="!aiAvailable || aiPermission !== 'admin'"
                    @click="manageConnection(scope.row.key)"
                  >
                    {{
                      connections[scope.row.key]?.configured ? "管理" : "上传"
                    }}
                  </el-button>
                </div>
              </template>
            </el-table-column>
            <el-table-column
              prop="collect"
              label="自动采集"
              min-width="85"
              align="center"
              sortable
            >
              <template #default="scope">
                <el-switch
                  v-model="scope.row.collect"
                  :active-value="true"
                  :inactive-value="false"
                  @change="val => handleCollectChange(val, scope.row)"
                />
              </template>
            </el-table-column>
            <el-table-column
              prop="peak_hours"
              label="高峰时段"
              min-width="120"
              align="center"
              show-overflow-tooltip
              sortable
            />
            <el-table-column
              label="采集历史数据"
              min-width="100"
              align="center"
            >
              <template #default="scope">
                <el-button
                  type="primary"
                  size="small"
                  plain
                  :disabled="!scope.row.collect"
                  @click="handleCollectHistory(scope.row)"
                >
                  采集
                </el-button>
              </template>
            </el-table-column>
            <el-table-column
              prop="admission"
              label="准入控制"
              min-width="85"
              align="center"
              sortable
            >
              <template #default="scope">
                <el-switch
                  v-model="scope.row.admission"
                  :active-value="true"
                  :inactive-value="false"
                  @change="val => handleAdmissionChange(!!val, scope.row)"
                />
              </template>
            </el-table-column>
            <el-table-column
              prop="admission_namespace"
              label="管控命名空间"
              min-width="100"
              align="center"
              show-overflow-tooltip
            />
            <el-table-column
              prop="nms_not_confirm"
              label="新服务免确认"
              min-width="110"
              align="center"
              sortable
            >
              <template #default="scope">
                <el-switch
                  v-model="scope.row.nms_not_confirm"
                  :disabled="!scope.row.admission"
                  :active-value="true"
                  :inactive-value="false"
                  @change="val => handleNmsNotConfirmChange(!!val, scope.row)"
                />
              </template>
            </el-table-column>
            <el-table-column
              prop="scheduler"
              label="固定节点均衡"
              min-width="110"
              align="center"
              sortable
            >
              <template #default="scope">
                <!-- :disabled="!scope.row.admission" -->
                <el-switch
                  v-model="scope.row.scheduler"
                  :disabled="true"
                  :active-value="true"
                  :inactive-value="false"
                  @change="val => handleSchedulerChange(!!val, scope.row)"
                />
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </div>

      <!-- 采集历史数据对话框 -->
      <el-dialog
        v-model="collectDialogVisible"
        title="采集历史数据"
        width="500px"
      >
        <el-form :model="collectForm" label-width="100px">
          <el-form-item label="K8S环境">
            <el-input v-model="collectForm.env" disabled />
          </el-form-item>
          <el-form-item label="采集天数">
            <el-input-number
              v-model="collectForm.days"
              :min="1"
              :max="90"
              controls-position="right"
            />
          </el-form-item>
          <div class="warning-box">
            <p>
              ⚠️默认会从时序数据库采集近10天(建议采集1个月)的每日高峰时段监控数据，并将10天内最大资源消耗日的数据写入到管控表，如果耗时较长，请等待采集完成或缩短采集时长。
            </p>
            <p>
              ⚠️重复执行采集并更新不会导致重复写入数据，请放心使用，每次采集并更新后都会自动将10天内最大资源消耗日的数据更新到管控表。
            </p>
          </div>
        </el-form>
        <template #footer>
          <span class="dialog-footer">
            <el-button @click="collectDialogVisible = false">取消</el-button>
            <el-button
              type="primary"
              :loading="collectLoading"
              @click="submitCollectHistory"
            >
              确认
            </el-button>
          </span>
        </template>
      </el-dialog>
      <KubeconfigDialog
        v-if="connectionDialogMounted"
        v-model="connectionDialogVisible"
        :env="connectionEnv"
        :connection="connections[connectionEnv]"
        @changed="refreshConnections"
      />

      <!-- 自动采集配置对话框 -->
      <el-dialog
        v-model="collectConfigDialogVisible"
        :title="collectConfigForm.collect ? '开启自动采集' : '关闭自动采集'"
        width="500px"
      >
        <el-form
          v-if="collectConfigForm.collect"
          :model="collectConfigForm"
          label-width="100px"
        >
          <el-form-item label="K8S环境">
            <el-input v-model="collectConfigForm.env" disabled />
          </el-form-item>
          <el-form-item label="高峰时段">
            <el-input
              v-model="collectConfigForm.peak_hours"
              placeholder="请输入高峰时段"
            />
          </el-form-item>
          <div class="warning-box">
            <p>请按格式填写高峰时段。</p>
            <p>
              开启后，每日凌晨1点会自动采集前一天的数据，并将10天内最大资源消耗日的数据更新到管控表，可在KubeDoor-master所在K8S的CronJobs中修改。
            </p>
          </div>
        </el-form>
        <div v-else>
          <p>
            确认关闭
            <strong>{{ collectConfigForm.env }}</strong> 的自动采集功能吗？
          </p>
        </div>
        <template #footer>
          <span class="dialog-footer">
            <el-button @click="handleCancelCollectConfig">取消</el-button>
            <el-button
              type="primary"
              :loading="collectConfigLoading"
              @click="submitCollectConfig"
            >
              确认
            </el-button>
          </span>
        </template>
      </el-dialog>

      <!-- 准入控制对话框 -->
      <el-dialog
        v-model="admissionDialogVisible"
        title="准入控制"
        width="500px"
      >
        <el-form :model="admissionForm" label-width="80px">
          <el-form-item label="K8S环境">
            <el-input v-model="admissionForm.env" disabled />
          </el-form-item>
          <el-form-item label="命名空间">
            <el-select
              v-model="admissionForm.namespace"
              multiple
              filterable
              collapse-tags-tooltip
              placeholder="请选择命名空间"
              style="width: 100%"
            >
              <el-option
                v-for="item in namespaceOptions"
                :key="item"
                :label="item"
                :value="item"
              />
            </el-select>
          </el-form-item>
        </el-form>
        <template #footer>
          <span class="dialog-footer">
            <el-button @click="handleCancelAdmission">取消</el-button>
            <el-button
              type="primary"
              :loading="admissionLoading"
              @click="submitAdmission"
            >
              确认
            </el-button>
          </span>
        </template>
      </el-dialog>

      <!-- 编辑别名对话框 -->
      <el-dialog v-model="aliasDialogVisible" title="编辑别名" width="500px">
        <el-form :model="aliasForm" label-width="80px" @submit.prevent>
          <el-form-item label="K8S环境">
            <el-input v-model="aliasForm.env" disabled />
          </el-form-item>
          <el-form-item label="别名">
            <el-input-tag
              v-model="aliasForm.aliases"
              :max="ALIAS_MAX_COUNT"
              :maxlength="ALIAS_MAX_LENGTH"
              clearable
              placeholder="输入别名后按回车添加"
            />
          </el-form-item>
          <div class="alias-tip">
            <p>{{ ALIAS_TIP }}</p>
            <p>{{ ALIAS_RULE_TIP }}</p>
          </div>
        </el-form>
        <template #footer>
          <span class="dialog-footer">
            <el-button @click="aliasDialogVisible = false">取消</el-button>
            <el-button
              type="primary"
              :loading="aliasLoading"
              @click="submitAliases"
            >
              确认
            </el-button>
          </span>
        </template>
      </el-dialog>

      <!-- 更新对话框 -->
      <el-dialog v-model="updateDialogVisible" title="更新镜像" width="500px">
        <el-form :model="updateForm" label-width="80px">
          <el-form-item label="镜像标签">
            <el-input
              v-model="updateForm.image_tag"
              placeholder="请输入镜像标签"
            />
          </el-form-item>
        </el-form>
        <template #footer>
          <span class="dialog-footer">
            <el-button @click="updateDialogVisible = false">取消</el-button>
            <el-button
              type="primary"
              :loading="updateLoading"
              @click="submitUpdate"
            >
              确认
            </el-button>
          </span>
        </template>
      </el-dialog>

      <!-- 批量更新对话框 -->
      <el-dialog
        v-model="batchUpdateDialogVisible"
        title="批量更新镜像"
        width="500px"
      >
        <el-form :model="batchUpdateForm" label-width="80px" @submit.prevent>
          <el-form-item :label="`K8S (${batchTargets.length})`">
            <div class="batch-targets">
              <el-tag v-for="row in batchTargets" :key="row.key" size="small">
                {{ row.key }}
              </el-tag>
            </div>
          </el-form-item>
          <el-form-item label="镜像标签">
            <el-input
              v-model="batchUpdateForm.image_tag"
              placeholder="请输入镜像标签（版本号），如 2.3.0"
              clearable
              @keyup.enter="submitBatchUpdate"
            />
          </el-form-item>
        </el-form>
        <template #footer>
          <span class="dialog-footer">
            <el-button @click="batchUpdateDialogVisible = false">
              取消
            </el-button>
            <el-button
              type="primary"
              :loading="batchUpdateLoading"
              @click="submitBatchUpdate"
            >
              确认
            </el-button>
          </span>
        </template>
      </el-dialog>
    </div>
  </div>
</template>

<script setup lang="ts">
import {
  ref,
  reactive,
  computed,
  onMounted,
  defineAsyncComponent,
  h
} from "vue";
import {
  ElInputTag,
  ElMessage,
  ElMessageBox,
  type TableInstance
} from "element-plus";
import { QuestionFilled } from "@element-plus/icons-vue";
import {
  getAgentStatus,
  initPeakData,
  updateAgentCollect,
  admisSwitch,
  updateAdmission,
  updateNmsNotConfirm,
  updateScheduler,
  updateAgentAliases,
  updateAgentImage,
  restartAgent,
  type AgentOpResult
} from "@/api/workbench";

import { getPromNamespace } from "@/api/monit";

import { message } from "@/utils/message";
import { useClusterPermission } from "@/utils/permission";
import { transformI18n } from "@/plugins/i18n";
import { aiApi, type AIConnection } from "@/api/ai";

const KubeconfigDialog = defineAsyncComponent(
  () => import("@/components/AI/KubeconfigDialog.vue")
);
const aiAvailable = ref(false);
const aiPermission = ref("");
const connections = ref<Record<string, AIConnection>>({});
const connectionDialogMounted = ref(false);
const connectionDialogVisible = ref(false);
const connectionEnv = ref("");

async function refreshConnections() {
  try {
    const bootstrap = await aiApi.bootstrap();
    aiAvailable.value = bootstrap.enabled;
    aiPermission.value = bootstrap.permission;
    if (!bootstrap.enabled) return;
    const response = await aiApi.connections();
    connections.value = Object.fromEntries(
      (response.connections || []).map(item => [item.env, item])
    );
  } catch {
    aiAvailable.value = false;
    aiPermission.value = "";
  }
}
function manageConnection(env: string) {
  // kubeconfig 直连属于集群接入配置,只有管理员能改(后端同样校验)
  if (aiPermission.value !== "admin") return;
  connectionEnv.value = env;
  connectionDialogMounted.value = true;
  connectionDialogVisible.value = true;
}
function connectionSummary(env: string) {
  const connection = connections.value[env];
  if (!aiAvailable.value) return "AI 服务未启用或无法连接";
  if (!connection?.configured)
    return aiPermission.value === "admin"
      ? "上传 YAML / JSON Kubeconfig 并测试连接"
      : "只有管理员可以管理共享配置";
  return `context: ${connection.context || ""}；测试: ${connection.test_status || "未测试"} ${connection.tested_at || ""}`;
}

// 定义Agent数据类型
interface AgentData {
  key: string;
  aliases: string[];
  online: boolean;
  last_heartbeat: string;
  ver: string;
  collect: boolean;
  peak_hours: string;
  admission: boolean;
  admission_namespace: string;
  nms_not_confirm: boolean;
  scheduler: boolean;
}

// 搜索表单
const searchForm = reactive({
  keyword: ""
});

// 表格数据
const tableData = ref<AgentData[]>([]);
const loading = ref(false);

const namespaceOptions = ref<string[]>([]);

// 根据关键字过滤表格数据
const filteredTableData = computed(() => {
  if (!searchForm.keyword) {
    return tableData.value;
  }
  const keyword = searchForm.keyword.toLowerCase();
  return tableData.value.filter(item => {
    return item.key.toLowerCase().includes(keyword);
  });
});

const loadNamespaceOptions = async (env: string) => {
  try {
    const { data } = await getPromNamespace(env);
    if (data && Array.isArray(data)) {
      namespaceOptions.value = data.filter(
        ns => ns !== "kube-system" && ns !== "kubedoor"
      );
    } else {
      namespaceOptions.value = [];
    }
  } catch (error) {
    console.error("获取命名空间列表失败:", error);
    ElMessage.error("获取命名空间列表失败");
    namespaceOptions.value = [];
  }
};

// 获取Agent状态数据
const getAgentData = async () => {
  loading.value = true;
  void refreshConnections();
  try {
    const { data } = await getAgentStatus();
    if (data) {
      tableData.value = Object.keys(data).map(key => ({
        key,
        aliases: Array.isArray(data[key].aliases) ? data[key].aliases : [],
        online: data[key].online || false,
        last_heartbeat: data[key].last_heartbeat || "",
        ver: data[key].ver || "",
        collect: !!data[key].collect || false,
        peak_hours: data[key].peak_hours || "",
        admission: !!data[key].admission || false,
        admission_namespace: data[key].admission_namespace || "",
        nms_not_confirm: !!data[key].nms_not_confirm || false,
        scheduler: !!data[key].scheduler || false
      }));
    } else {
      tableData.value = [];
    }
  } catch (error) {
    console.error("获取Agent状态数据失败:", error);
    ElMessage.error("获取Agent状态数据失败");
    tableData.value = [];
  } finally {
    loading.value = false;
  }
};

// 处理搜索
const handleSearch = () => {
  getAgentData();
};

// 处理重置
const handleReset = () => {
  searchForm.keyword = "";
  getAgentData();
};

// 采集历史数据对话框
const collectDialogVisible = ref(false);
const collectForm = reactive({
  env: "",
  days: 10,
  peak_hours: ""
});
const collectLoading = ref(false);

// 处理采集历史数据
const handleCollectHistory = (row: AgentData) => {
  collectDialogVisible.value = true;
  collectForm.env = row.key;
  collectForm.peak_hours = row.peak_hours || "";
};

// 提交采集历史数据
const submitCollectHistory = async () => {
  collectLoading.value = true;
  try {
    const result = await initPeakData(
      collectForm.env,
      collectForm.days,
      collectForm.peak_hours
    );
    ElMessage.success(
      `K8S: ${collectForm.env} 采集 ${collectForm.days} 天历史数据完成`
    );
    if (Array.isArray(result.warnings) && result.warnings.length > 0) {
      ElMessage.warning(result.warnings.join("；"));
    }
    collectDialogVisible.value = false;
  } catch (error) {
    console.error("采集历史数据失败:", error);
    ElMessage.error("采集历史数据失败");
  } finally {
    collectLoading.value = false;
  }
};

// 自动采集配置对话框
const collectConfigDialogVisible = ref(false);
const collectConfigForm = reactive({
  env: "",
  collect: false,
  peak_hours: ""
});
const collectConfigLoading = ref(false);

// 取消自动采集配置
const handleCancelCollectConfig = () => {
  collectConfigDialogVisible.value = false;
};

// 处理采集切换
const handleCollectChange = async (val: any, row: AgentData) => {
  // 恢复原始状态，等待用户确认
  row.collect = !val;

  // 打开配置对话框
  collectConfigForm.env = row.key;
  collectConfigForm.collect = val;
  collectConfigForm.peak_hours = val
    ? row.peak_hours || "10:00:00-11:30:00"
    : "";
  collectConfigDialogVisible.value = true;
};

// 提交自动采集配置
const submitCollectConfig = async () => {
  collectConfigLoading.value = true;
  try {
    await updateAgentCollect(
      collectConfigForm.env,
      collectConfigForm.collect,
      collectConfigForm.peak_hours
    );

    // 等待1秒后显示成功消息
    // await new Promise(resolve => setTimeout(resolve, 1000));

    // 更新表格中对应行的数据
    const rowIndex = tableData.value.findIndex(
      item => item.key === collectConfigForm.env
    );
    if (rowIndex !== -1) {
      tableData.value[rowIndex].collect = collectConfigForm.collect;
      tableData.value[rowIndex].peak_hours = collectConfigForm.peak_hours;
    }

    ElMessage.success(
      `已${collectConfigForm.collect ? "开启" : "关闭"} ${collectConfigForm.env} 的自动采集功能`
    );
    collectConfigDialogVisible.value = false;
  } catch (error) {
    console.error("配置自动采集失败:", error);
    ElMessage.error("配置自动采集失败");
  } finally {
    collectConfigLoading.value = false;
  }
};

// 准入控制对话框
const admissionDialogVisible = ref(false);
const admissionForm = reactive({
  env: "",
  admission: false,
  namespace: []
});
const admissionLoading = ref(false);

// 处理准入控制状态变更
const handleAdmissionChange = async (val: boolean, row: AgentData) => {
  row.admission = !val;

  if (val) {
    // 开启准入控制，弹出选择命名空间的对话框
    await loadNamespaceOptions(row.key);
    admissionForm.env = row.key;
    admissionForm.admission = true;
    admissionForm.namespace = row.admission_namespace
      ? JSON.parse(row.admission_namespace)
      : [];
    admissionDialogVisible.value = true;
  } else {
    // 关闭准入控制，弹出确认对话框
    ElMessageBox.confirm(`确认关闭 ${row.key} 的准入控制功能吗？`, "警告", {
      confirmButtonText: "确认",
      cancelButtonText: "取消",
      type: "warning"
    }).then(async () => {
      try {
        const res = await admisSwitch("off", row.key);
        if (res.success) {
          ElMessage.success(res.message);
          updateAdmission(row.key, false, "");
          row.admission = false;
          row.admission_namespace = "";
        } else {
          ElMessage.error(res.message);
        }
      } catch (error) {
        console.error("关闭准入控制失败:", error);
      }
    });
  }
};

// 提交准入控制配置
const submitAdmission = async () => {
  if (!admissionForm.namespace.length) {
    ElMessage.warning("请输入命名空间");
    return;
  }

  admissionLoading.value = true;
  try {
    const res = await admisSwitch("on", admissionForm.env);
    if (res.success) {
      await updateAdmission(
        admissionForm.env,
        true,
        JSON.stringify(admissionForm.namespace)
      );
      // 更新当前行的admission_namespace
      const currentRow = tableData.value.find(
        row => row.key === admissionForm.env
      );
      if (currentRow) {
        currentRow.admission = true;
        currentRow.admission_namespace = JSON.stringify(
          admissionForm.namespace
        );
      }
      ElMessage.success(res.message);
    } else {
      ElMessage.error(res.message);
    }
    admissionDialogVisible.value = false;
  } catch (error) {
    console.error("更新准入控制状态失败:", error);
  } finally {
    admissionLoading.value = false;
  }
};

// 取消准入控制配置
const handleCancelAdmission = () => {
  admissionDialogVisible.value = false;
  getAgentData(); // 刷新数据，恢复原状态
};

// 新服务免确认对话框
const handleNmsNotConfirmChange = async (val: boolean, row: AgentData) => {
  row.nms_not_confirm = !val;

  ElMessageBox.confirm(
    `确认${val ? "开启" : "关闭"} ${row.key} 的新服务免确认功能吗？`,
    "警告",
    {
      confirmButtonText: "确认",
      cancelButtonText: "取消",
      type: "warning"
    }
  ).then(async () => {
    try {
      await updateNmsNotConfirm(row.key, val ? 1 : 0);
      ElMessage.success("修改成功");
      row.nms_not_confirm = val;
    } catch (error) {
      console.error("关闭准入控制失败:", error);
    }
  });
};

// 固定节点均衡对话框
const handleSchedulerChange = async (val: boolean, row: AgentData) => {
  row.scheduler = !val;

  ElMessageBox.confirm(
    `确认${val ? "开启" : "关闭"} ${row.key} 的固定节点均衡功能吗？`,
    "警告",
    {
      confirmButtonText: "确认",
      cancelButtonText: "取消",
      type: "warning"
    }
  ).then(async () => {
    try {
      await updateScheduler(row.key, val ? 1 : 0);
      row.scheduler = val;
      ElMessage.success("修改成功");
    } catch (error) {
      console.error("关闭准入控制失败:", error);
    }
  });
};

// ---- 集群别名(AI 助手按别名识别集群)----
// 只有管理员能编辑(后端同样校验);按登录账号判断,不依赖 AI 服务是否启用
const { isAdmin } = useClusterPermission();
// 和 master func_manager/db_api.py 的校验保持一致
const ALIAS_MAX_COUNT = 20;
const ALIAS_MAX_LENGTH = 64;
const ALIAS_TIP =
  "企业微信 AI 助手按别名识别用户说的是哪个集群（例如「beta 昆仑」），一个集群可以有多个别名，不能和其它集群的名称或别名重复。";
const ALIAS_RULE_TIP = `比较时忽略大小写、空格和 - _ . 符号，例如「beta 昆仑」和「Beta-昆仑」算同一个别名；最多 ${ALIAS_MAX_COUNT} 个，每个不超过 ${ALIAS_MAX_LENGTH} 个字符。中文别名在提问里原样出现就算提到了这个集群，不要用「测试」「生产」这类日常用词。`;
const aliasDialogVisible = ref(false);
const aliasLoading = ref(false);
// el-input-tag 点清空后给的是 undefined
const aliasForm = reactive<{ env: string; aliases?: string[] }>({
  env: "",
  aliases: []
});

const handleEditAliases = (row: AgentData) => {
  aliasForm.env = row.key;
  aliasForm.aliases = [...row.aliases];
  aliasDialogVisible.value = true;
};

const submitAliases = async () => {
  const aliases = aliasForm.aliases ?? [];
  aliasLoading.value = true;
  try {
    const res = await updateAgentAliases(aliasForm.env, aliases);
    const dropped = aliases.length - res.aliases.length;
    ElMessage.success(
      dropped > 0
        ? `更新成功，已忽略 ${dropped} 个重复或与集群名相同的别名`
        : "更新成功"
    );
    aliasDialogVisible.value = false;
    await getAgentData();
  } catch (error) {
    // 失败原因(比如和其它集群重复)已由请求拦截器提示,弹窗保留方便修改
    console.error("更新别名失败:", error);
  } finally {
    aliasLoading.value = false;
  }
};

// 更新对话框
const updateDialogVisible = ref(false);
const updateForm = reactive({
  env: "",
  image_tag: ""
});
const updateLoading = ref(false);

// 处理更新操作
const handleUpdate = (row: AgentData) => {
  updateForm.env = row.key;
  updateForm.image_tag = "";
  updateDialogVisible.value = true;
};

// 提交更新
const submitUpdate = async () => {
  if (!updateForm.image_tag) {
    ElMessage.warning("请输入镜像标签");
    return;
  }

  updateLoading.value = true;
  try {
    const res = await updateAgentImage(updateForm.env, updateForm.image_tag);
    if (res.success) {
      message(transformI18n(res.message), {
        type: "success"
      });
      updateDialogVisible.value = false;
      await getAgentData();
    } else {
      message(res.message || res.error || "更新失败", {
        type: "error"
      });
    }
  } finally {
    updateLoading.value = false;
  }
};

// 重启失败的原因,没有则返回空串(重启接口 200 也可能带逐个 Deployment 的失败原因)
const restartFailure = (res: AgentOpResult) =>
  res.error_list?.[0]?.reason ||
  (res.success === false ? res.message || res.error || "重启失败" : "");

// 重启 agent
const handleRestart = async (row: AgentData) => {
  try {
    await ElMessageBox.confirm(
      `确认重启 ${row.key} 的 agent 吗？重启期间该集群的 agent 会短暂离线。`,
      "重启 agent",
      { confirmButtonText: "重启", cancelButtonText: "取消", type: "warning" }
    );
  } catch {
    return; // 取消
  }
  let res: AgentOpResult;
  try {
    res = await restartAgent(row.key);
  } catch {
    return; // 接口出错的提示已由请求拦截器弹出
  }
  const reason = restartFailure(res);
  if (reason) {
    message(reason, { type: "error" });
    return;
  }
  message(`已发起 ${row.key} 的 agent 重启`, { type: "success" });
  await getAgentData();
};

// ---- 勾选与批量操作 ----
const tableRef = ref<TableInstance>();
const selectedRows = ref<AgentData[]>([]);
// 离线的 agent 收不到指令(master 会直接拒绝),不能勾选
const isSelectable = (row: AgentData) => row.online;
const handleSelectionChange = (rows: AgentData[]) => {
  selectedRows.value = rows;
};

type BatchFailure = { env: string; reason: string };

// 接口出错时的原因:master 拒绝返回 { error },网络异常取 message
const errorReason = (error: any): string =>
  error?.response?.data?.error ||
  error?.response?.data?.message ||
  error?.message ||
  "未知错误";

// 并发执行并收集失败项:task 返回失败原因,返回空表示成功
const runBatch = async (
  rows: AgentData[],
  task: (row: AgentData) => Promise<string | undefined>
) => {
  const results = await Promise.all(
    rows.map(async row => {
      try {
        return { env: row.key, reason: (await task(row)) || "" };
      } catch (error) {
        return { env: row.key, reason: errorReason(error) };
      }
    })
  );
  const failures: BatchFailure[] = results.filter(item => item.reason);
  return { total: rows.length, failures };
};

// 汇总提示:全部成功弹一条消息,有失败的列出每个集群的原因
const reportBatch = (
  title: string,
  total: number,
  failures: BatchFailure[]
) => {
  if (!failures.length) {
    message(`${title}：${total} 个 agent 已全部提交`, { type: "success" });
    return;
  }
  ElMessageBox.alert(
    h("div", [
      h(
        "p",
        `${title}：成功 ${total - failures.length} 个，失败 ${failures.length} 个`
      ),
      ...failures.map(item =>
        h(
          "p",
          { style: "margin-top: 6px; color: var(--el-color-danger)" },
          `${item.env}：${item.reason}`
        )
      )
    ]),
    "部分 agent 操作失败",
    { type: "warning", confirmButtonText: "知道了" }
  ).catch(() => {});
};

// 批量更新对话框:目标在打开时固定下来,避免表格刷新后勾选变化
const batchTargets = ref<AgentData[]>([]);
const batchUpdateDialogVisible = ref(false);
const batchUpdateForm = reactive({ image_tag: "" });
const batchUpdateLoading = ref(false);

const handleBatchUpdate = () => {
  if (!selectedRows.value.length) return;
  batchTargets.value = [...selectedRows.value];
  batchUpdateForm.image_tag = "";
  batchUpdateDialogVisible.value = true;
};

const submitBatchUpdate = async () => {
  const imageTag = batchUpdateForm.image_tag.trim();
  if (!imageTag) {
    ElMessage.warning("请输入镜像标签");
    return;
  }

  batchUpdateLoading.value = true;
  try {
    const { total, failures } = await runBatch(
      batchTargets.value,
      async row => {
        const res = await updateAgentImage(row.key, imageTag, { silent: true });
        return res.success === false
          ? res.message || res.error || "更新失败"
          : undefined;
      }
    );
    batchUpdateDialogVisible.value = false;
    reportBatch(`批量更新镜像（${imageTag}）`, total, failures);
    tableRef.value?.clearSelection();
    await getAgentData();
  } finally {
    batchUpdateLoading.value = false;
  }
};

// 批量重启
const handleBatchRestart = async () => {
  const targets = [...selectedRows.value];
  if (!targets.length) return;
  try {
    await ElMessageBox.confirm(
      h("div", [
        h(
          "p",
          `确认重启以下 ${targets.length} 个 agent 吗？重启期间这些集群的 agent 会短暂离线。`
        ),
        h(
          "div",
          {
            style:
              "max-height: 160px; margin-top: 8px; overflow-y: auto; line-height: 1.8"
          },
          targets.map(row => row.key).join("、")
        )
      ]),
      "批量重启 agent",
      { confirmButtonText: "重启", cancelButtonText: "取消", type: "warning" }
    );
  } catch {
    return; // 取消
  }

  const { total, failures } = await runBatch(targets, async row => {
    const res = await restartAgent(row.key, { silent: true });
    return restartFailure(res) || undefined;
  });
  reportBatch("批量重启", total, failures);
  tableRef.value?.clearSelection();
  await getAgentData();
};

// 页面加载时获取数据
onMounted(() => {
  getAgentData();
});
</script>

<style scoped>
/* .search-section {
  margin-bottom: 16px;
} */

.search-section {
  padding: 16px;
  border-radius: 8px;
}

.warning-box {
  padding: 10px 15px;
  margin: 10px 0;
  font-size: 14px;
  color: #5f5f5f;
  background-color: #fff9ed;
  border-left: 4px solid #e6a23c;
  border-radius: 4px;
}

.warning-box p {
  margin: 8px 0;
  line-height: 1.5;
}

.ai-connection-cell {
  display: flex;
  gap: 10px;
  align-items: center;
  justify-content: center;
}

.ai-connection-header {
  display: inline-flex;
  gap: 4px;
  align-items: center;
}

.ai-connection-header__hint {
  cursor: help;
}

.ai-connection-tip {
  max-width: 280px;
  line-height: 1.6;
}

.alias-cell {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  align-items: center;
}

.alias-tip {
  font-size: 13px;
  line-height: 1.6;
  color: var(--el-text-color-secondary);
}

.alias-tip p {
  margin: 4px 0;
}

.batch-targets {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  max-height: 120px;
  overflow-y: auto;
}
</style>
