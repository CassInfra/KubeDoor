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
        </el-form>
      </div>

      <!-- 表格数据 -->
      <div class="mt-2">
        <el-card v-loading="loading">
          <el-table
            :data="filteredTableData"
            style="width: 100%"
            stripe
            :default-sort="{ prop: 'key', order: 'ascending' }"
          >
            <el-table-column
              prop="key"
              label="K8S"
              min-width="120"
              show-overflow-tooltip
              sortable
            />
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
            <el-table-column label="更新" min-width="80" align="center">
              <template #default="scope">
                <el-button
                  type="primary"
                  size="small"
                  plain
                  @click="handleUpdate(scope.row)"
                >
                  更新
                </el-button>
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
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import {
  getAgentStatus,
  initPeakData,
  updateAgentCollect,
  admisSwitch,
  updateAdmission,
  updateNmsNotConfirm,
  updateScheduler
} from "@/api/workbench";

import { updateImage } from "@/api/resource";

import { getPromNamespace } from "@/api/monit";

import { message } from "@/utils/message";
import { transformI18n } from "@/plugins/i18n";

// 定义Agent数据类型
interface AgentData {
  key: string;
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
  try {
    const { data } = await getAgentStatus();
    if (data) {
      tableData.value = Object.keys(data).map(key => ({
        key,
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
    await initPeakData(
      collectForm.env,
      collectForm.days,
      collectForm.peak_hours
    );
    ElMessage.success(
      `K8S: ${collectForm.env} 采集 ${collectForm.days} 天历史数据完成`
    );
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
    const res = await updateImage(updateForm.env, {
      image_tag: updateForm.image_tag,
      deployment: "kubedoor-agent",
      namespace: "kubedoor"
    });
    if ((res as any).success) {
      message(transformI18n((res as any).message), {
        type: "success"
      });
      updateDialogVisible.value = false;
      await getAgentData();
    } else {
      message((res as any).message, {
        type: "error"
      });
    }
  } finally {
    updateLoading.value = false;
  }
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
</style>
