<template>
  <div class="load-balance-container">
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

        <el-form-item>
          <el-button
            type="primary"
            :icon="Refresh"
            :loading="analyzing"
            @click="handleAnalyze"
          >
            分析负载
          </el-button>
          <el-button :icon="Setting" @click="openConfigDialog">配置</el-button>
        </el-form-item>

        <el-form-item>
          <el-tag :type="statusTagType" size="large">{{ statusText }}</el-tag>
        </el-form-item>
      </el-form>
    </div>

    <!-- 主内容区 -->
    <el-row :gutter="16" class="mt-2">
      <!-- 左侧：迁移计划 -->
      <el-col :span="16">
        <el-card v-loading="analyzing">
          <template #header>
            <div class="card-header">
              <span>迁移计划</span>
              <div v-if="migrationPlan">
                <el-tag type="info" class="mr-2"
                  >极差: {{ migrationPlan.current_range }}%</el-tag
                >
                <el-tag
                  v-if="migrationPlan.expected_range"
                  type="success"
                  class="mr-2"
                >
                  预期: {{ migrationPlan.expected_range }}%
                </el-tag>
                <el-tag
                  v-if="migrationPlan.current_std"
                  type="info"
                  class="mr-2"
                >
                  σ: {{ migrationPlan.current_std }}
                </el-tag>
                <el-tag v-if="migrationPlan.expected_std" type="success">
                  预期σ: {{ migrationPlan.expected_std }}
                </el-tag>
              </div>
            </div>
          </template>

          <div
            v-if="!migrationPlan || migrationPlan.migrations.length === 0"
            class="empty-state"
          >
            <el-empty description="暂无迁移计划，请点击「分析负载」生成" />
          </div>

          <div v-else>
            <el-table
              ref="migrationTableRef"
              :data="migrationPlan.migrations"
              style="width: 100%"
              stripe
              border
              @selection-change="handleMigrationSelectionChange"
            >
              <el-table-column type="selection" width="40" />
              <el-table-column
                prop="namespace"
                label="命名空间"
                min-width="100"
                show-overflow-tooltip
              />
              <el-table-column
                prop="pod_name"
                label="Pod名称"
                min-width="180"
                show-overflow-tooltip
              />
              <el-table-column
                prop="source_node"
                label="源节点"
                min-width="180"
                show-overflow-tooltip
              >
                <template #default="scope">
                  {{ scope.row.source_node }}
                  <span v-if="scope.row.source_percent" class="node-percent">
                    ({{ scope.row.source_percent }}%→{{
                      scope.row.source_percent_after
                    }}%)
                  </span>
                  <span v-else class="node-percent">
                    ({{ getNodeCpuPercent(scope.row.source_node) }}%)
                  </span>
                </template>
              </el-table-column>
              <el-table-column
                prop="target_node"
                label="目标节点"
                min-width="180"
                show-overflow-tooltip
              >
                <template #default="scope">
                  {{ scope.row.target_node }}
                  <span v-if="scope.row.target_percent" class="node-percent">
                    ({{ scope.row.target_percent }}%→{{
                      scope.row.target_percent_after
                    }}%)
                  </span>
                  <span v-else class="node-percent">
                    ({{ getNodeCpuPercent(scope.row.target_node) }}%)
                  </span>
                </template>
              </el-table-column>
              <el-table-column
                prop="cpu_used"
                label="CPU(核)"
                min-width="80"
                align="center"
              >
                <template #default="scope">
                  {{ scope.row.cpu_used.toFixed(2) }}
                </template>
              </el-table-column>
              <el-table-column
                prop="expected_effect"
                label="预期效果"
                min-width="100"
                show-overflow-tooltip
              />
              <el-table-column
                prop="status"
                label="状态"
                width="80"
                align="center"
              >
                <template #default="scope">
                  <el-tag
                    v-if="scope.row.status"
                    :type="getStatusTagType(scope.row.status)"
                    size="small"
                  >
                    {{ getStatusText(scope.row.status) }}
                  </el-tag>
                  <el-tag v-else type="info" size="small">待执行</el-tag>
                </template>
              </el-table-column>
            </el-table>

            <div class="action-bar">
              <el-button
                type="primary"
                :disabled="selectedMigrations.length === 0 || executing"
                :loading="executing"
                @click="handleExecute"
              >
                执行选中 ({{ selectedMigrations.length }})
              </el-button>
              <el-button
                type="success"
                :disabled="
                  !migrationPlan ||
                  migrationPlan.migrations.length === 0 ||
                  executing
                "
                :loading="executing"
                @click="handleExecuteAll"
              >
                执行全部
              </el-button>
              <el-button v-if="executing" type="danger" @click="handleStop">
                停止
              </el-button>
            </div>

            <!-- 迭代日志折叠面板 -->
            <el-collapse
              v-if="migrationPlan?.iteration_logs?.length"
              class="mt-4"
            >
              <el-collapse-item>
                <template #title>
                  <span
                    >迭代日志 ({{
                      migrationPlan.iteration_logs.length
                    }}
                    次迭代)</span
                  >
                </template>
                <div class="iteration-logs">
                  <div
                    v-for="log in migrationPlan.iteration_logs"
                    :key="log.iteration"
                    class="iteration-log-item"
                  >
                    <div class="iteration-header">
                      <el-tag
                        :type="getIterationLogType(log.result)"
                        size="small"
                        class="mr-2"
                      >
                        迭代 {{ log.iteration }}
                      </el-tag>
                      <span class="iteration-range"
                        >极差: {{ log.sim_range }}%</span
                      >
                      <span v-if="log.source_node" class="iteration-node"
                        >节点: {{ log.source_node }}</span
                      >
                    </div>
                    <!-- 显示每个尝试的Pod -->
                    <div v-if="log.tried_pods?.length" class="tried-pods">
                      <div
                        v-for="(pod, idx) in log.tried_pods"
                        :key="idx"
                        class="tried-pod-item"
                      >
                        <span
                          :class="[
                            'pod-result',
                            pod.result === '成功' ? 'success' : 'failed'
                          ]"
                        >
                          {{ pod.result === "成功" ? "✅" : "❌" }}
                        </span>
                        <span class="pod-name">{{ pod.pod }}</span>
                        <span class="pod-cpu"
                          >({{ pod.cpu.toFixed(2) }}核)</span
                        >
                        <span v-if="pod.result !== '成功'" class="pod-reason">{{
                          pod.result
                        }}</span>
                        <span v-if="pod.target" class="pod-target"
                          >→ {{ pod.target }}</span
                        >
                        <span v-if="pod.effect" class="pod-effect">{{
                          pod.effect
                        }}</span>
                      </div>
                    </div>
                    <!-- 兼容旧格式 -->
                    <span v-else-if="log.details" class="iteration-details">{{
                      log.details
                    }}</span>
                  </div>
                </div>
              </el-collapse-item>
            </el-collapse>
          </div>
        </el-card>
      </el-col>

      <!-- 右侧：节点资源、隔离Pod和日志 -->
      <el-col :span="8">
        <!-- 节点资源使用率 -->
        <el-card class="mb-4">
          <template #header>
            <div class="card-header">
              <span>节点资源</span>
              <div>
                <el-tag
                  v-if="nodesCpuList.length > 0"
                  type="info"
                  size="small"
                  class="mr-2"
                >
                  {{ nodesCpuList.length }} 节点
                </el-tag>
                <el-button
                  type="primary"
                  size="small"
                  :loading="loadingNodesCpu"
                  @click="loadNodesCpu"
                >
                  刷新
                </el-button>
              </div>
            </div>
          </template>

          <div v-if="nodesCpuList.length === 0" class="empty-state-small">
            <el-empty description="点击「刷新」获取数据" :image-size="60" />
          </div>

          <el-table
            v-else
            :data="nodesCpuList"
            size="small"
            stripe
            max-height="300"
            :default-sort="{ prop: 'cpu', order: 'descending' }"
          >
            <el-table-column
              prop="name"
              label="节点"
              show-overflow-tooltip
              sortable
            />
            <el-table-column
              prop="schedulable"
              label="调度"
              width="70"
              sortable
            >
              <template #default="{ row }">
                <span
                  :style="{ color: row.schedulable ? '#67C23A' : '#F56C6C' }"
                >
                  {{ row.schedulable ? "是" : "否" }}
                </span>
              </template>
            </el-table-column>
            <el-table-column prop="cpu" label="CPU%" width="80" sortable>
              <template #default="{ row }">
                <span :style="{ color: getNodeCpuColor(row.cpu) }"
                  >{{ row.cpu }}%</span
                >
              </template>
            </el-table-column>
            <el-table-column prop="mem" label="MEM%" width="80" sortable>
              <template #default="{ row }">
                <span :style="{ color: getNodeCpuColor(row.mem) }"
                  >{{ row.mem }}%</span
                >
              </template>
            </el-table-column>
            <el-table-column prop="pods" label="Pod" width="70" sortable />
          </el-table>
        </el-card>

        <!-- 隔离Pod列表 -->
        <el-card class="mb-4">
          <template #header>
            <div class="card-header">
              <span>隔离Pod</span>
              <el-button
                type="primary"
                size="small"
                :loading="loadingIsolated"
                @click="loadIsolatedPods"
              >
                刷新
              </el-button>
            </div>
          </template>

          <div v-if="isolatedPods.length === 0" class="empty-state-small">
            <el-empty description="暂无隔离Pod" :image-size="60" />
          </div>

          <div v-else>
            <el-table
              ref="isolatedTableRef"
              :data="isolatedPods"
              style="width: 100%"
              size="small"
              stripe
              max-height="200"
              @selection-change="handleIsolatedSelectionChange"
            >
              <el-table-column type="selection" width="35" />
              <el-table-column
                prop="namespace"
                label="命名空间"
                min-width="60"
                show-overflow-tooltip
              />
              <el-table-column
                prop="name"
                label="Pod名称"
                min-width="120"
                show-overflow-tooltip
              />
              <el-table-column
                prop="selector_label"
                label="Selector标签"
                min-width="120"
                show-overflow-tooltip
              />
              <el-table-column
                prop="cpu_used"
                label="CPU(m)"
                width="80"
                align="center"
              >
                <template #default="{ row }">
                  {{ Math.round((row.cpu_used || 0) * 1000) }}
                </template>
              </el-table-column>
            </el-table>

            <div class="action-bar-small">
              <el-button
                type="danger"
                size="small"
                :disabled="selectedIsolatedPods.length === 0"
                :loading="cleaningUp"
                @click="handleCleanup"
              >
                清理选中 ({{ selectedIsolatedPods.length }})
              </el-button>
            </div>
          </div>
        </el-card>

        <!-- 操作日志 -->
        <el-card>
          <template #header>
            <div class="card-header">
              <span>操作日志</span>
              <el-button
                type="primary"
                size="small"
                :loading="loadingLogs"
                @click="loadLogs"
              >
                刷新
              </el-button>
            </div>
          </template>

          <div class="log-container">
            <div v-for="(log, index) in logs" :key="index" class="log-item">
              <span class="log-time">{{ formatTime(log.timestamp) }}</span>
              <el-tag :type="getLogTagType(log.action)" size="small">{{
                log.action
              }}</el-tag>
              <span class="log-message">{{ log.message }}</span>
            </div>
            <div v-if="logs.length === 0" class="empty-state-small">
              <el-empty description="暂无日志" :image-size="60" />
            </div>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <!-- 配置对话框 -->
    <el-dialog
      v-model="configDialogVisible"
      title="负载均衡配置"
      width="600px"
      :close-on-click-modal="false"
    >
      <el-form :model="configForm" label-width="140px">
        <el-form-item label="不均衡阈值(%)">
          <el-input-number
            v-model="configForm.imbalance_threshold"
            :min="1"
            :max="80"
          />
          <span class="form-tip">极差超过此值触发均衡</span>
        </el-form-item>
        <el-form-item label="均衡目标(%)">
          <el-input-number
            v-model="configForm.balance_target"
            :min="1"
            :max="50"
          />
          <span class="form-tip">优化到极差小于此值</span>
        </el-form-item>
        <el-form-item label="最小副本数">
          <el-input-number
            v-model="configForm.min_replicas"
            :min="1"
            :max="10"
          />
          <span class="form-tip">副本数小于此值的Deployment不参与</span>
        </el-form-item>
        <el-form-item label="黑名单">
          <el-select
            v-model="configForm.blacklist"
            multiple
            filterable
            allow-create
            default-first-option
            placeholder="输入namespace/deployment"
            style="width: 100%"
          />
        </el-form-item>
        <el-form-item label="排除命名空间">
          <el-select
            v-model="configForm.exclude_namespaces"
            multiple
            filterable
            placeholder="选择要排除的namespace"
            style="width: 100%"
          >
            <el-option
              v-for="ns in namespaceOptions"
              :key="ns"
              :label="ns"
              :value="ns"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="排除节点">
          <el-select
            v-model="configForm.exclude_nodes"
            multiple
            filterable
            placeholder="选择要排除的节点"
            style="width: 100%"
          >
            <el-option
              v-for="node in nodeOptions"
              :key="node"
              :label="node"
              :value="node"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="源节点选择">
          <el-radio-group v-model="configForm.source_strategy">
            <el-radio value="average">高于平均值</el-radio>
            <el-radio value="percentile">
              高于
              <el-input-number
                v-model="configForm.source_percentile"
                :min="50"
                :max="95"
                :step="5"
                size="small"
                style="width: 80px; margin: 0 4px"
                :disabled="configForm.source_strategy !== 'percentile'"
              />
              分位数
            </el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="目标节点选择">
          <el-radio-group v-model="configForm.target_strategy">
            <el-radio value="average">低于平均值</el-radio>
            <el-radio value="percentile">
              低于
              <el-input-number
                v-model="configForm.target_percentile"
                :min="5"
                :max="50"
                :step="5"
                size="small"
                style="width: 80px; margin: 0 4px"
                :disabled="configForm.target_strategy !== 'percentile'"
              />
              分位数
            </el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="最小Pod CPU(核)">
          <el-input-number
            v-model="configForm.min_pod_cpu"
            :min="0.1"
            :max="10"
            :step="0.1"
            :precision="1"
          />
          <span class="form-tip">低于此值的Pod不参与迁移</span>
        </el-form-item>
        <el-form-item>
          <template #label>
            <span>最大迭代次数</span>
            <el-tooltip placement="top">
              <template #content>
                <div style="max-width: 300px">
                  <p>迭代逻辑说明：</p>
                  <p>
                    •
                    同一次迭代内：从最高负载节点选择Pod，如果Pod1找不到目标节点
                    → 继续尝试Pod2 → Pod3...
                  </p>
                  <p>
                    •
                    只有当该节点所有Pod都找不到目标时，才会标记节点为skipped，下一次迭代选择其他高负载节点
                  </p>
                  <p>• 每次迭代最多成功迁移1个Pod</p>
                </div>
              </template>
              <el-icon style="margin-left: 4px; cursor: help"
                ><QuestionFilled
              /></el-icon>
            </el-tooltip>
          </template>
          <el-input-number
            v-model="configForm.max_iterations"
            :min="1"
            :max="100"
            :step="10"
          />
          <span class="form-tip">最多迁移的Pod数量上限</span>
        </el-form-item>
        <el-form-item label="每批执行数量">
          <el-input-number v-model="configForm.batch_size" :min="1" :max="20" />
          <span class="form-tip">分批执行时每批的Pod数量</span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="configDialogVisible = false">取消</el-button>
        <el-button
          type="primary"
          :loading="savingConfig"
          @click="handleSaveConfig"
          >保存</el-button
        >
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { Refresh, Setting, QuestionFilled } from "@element-plus/icons-vue";
import { getAgentNames } from "@/api/istio";
import { useSearchStoreHook } from "@/store/modules/search";
import {
  getLoadBalanceConfig,
  updateLoadBalanceConfig,
  getLoadBalanceStatus,
  getLoadBalanceLogs,
  analyzeLoadBalance,
  executeLoadBalance,
  getIsolatedPods,
  cleanupIsolatedPods,
  getNodesCpu,
  getNamespaces,
  checkPodsStatus,
  type LoadBalanceConfig,
  type MigrationPlan,
  type MigrationItem,
  type IsolatedPod,
  type LoadBalanceLog,
  type LoadBalanceStatus,
  type NodeCpuInfo,
  type PodStatusResult
} from "@/api/loadBalance";

const searchStore = useSearchStoreHook();

// 响应式数据
const envOptions = ref<string[]>([]);
const searchForm = reactive({ env: searchStore.env || "" });

const analyzing = ref(false);
const executing = ref(false);
const loadingIsolated = ref(false);
const loadingLogs = ref(false);
const loadingNodesCpu = ref(false);
const cleaningUp = ref(false);
const savingConfig = ref(false);

const status = ref<LoadBalanceStatus | null>(null);
const migrationPlan = ref<MigrationPlan | null>(null);
const selectedMigrations = ref<MigrationItem[]>([]);
const isolatedPods = ref<IsolatedPod[]>([]);
const selectedIsolatedPods = ref<IsolatedPod[]>([]);
const logs = ref<LoadBalanceLog[]>([]);
const nodesCpuData = ref<Record<string, NodeCpuInfo>>({});
const namespaceOptions = ref<string[]>([]);
const nodeOptions = ref<string[]>([]);

const configDialogVisible = ref(false);
// 默认值由后端service.py定义，这里只是初始化结构
const configForm = reactive<LoadBalanceConfig>({
  enabled: false,
  imbalance_threshold: 30,
  balance_target: 20,
  check_interval: 5,
  min_replicas: 2,
  peak_hours: [],
  blacklist: [],
  exclude_namespaces: [],
  exclude_nodes: [],
  auto_execute: false,
  source_strategy: "average",
  source_percentile: 70,
  target_strategy: "average",
  target_percentile: 30,
  min_pod_cpu: 0.5,
  max_iterations: 50,
  batch_size: 4
});

const migrationTableRef = ref();
const isolatedTableRef = ref();
const stopRequested = ref(false); // 停止执行标志

// 计算属性
const statusTagType = computed(() => {
  if (!status.value) return "info";
  if (status.value.status === "analyzing") return "warning";
  if (status.value.status === "executing") return "danger";
  return status.value.enabled ? "success" : "info";
});

const statusText = computed(() => {
  if (!status.value) return "未知";
  if (status.value.status === "analyzing") return "分析中...";
  if (status.value.status === "executing") return "执行中...";
  return status.value.enabled ? "已启用" : "已禁用";
});

// 节点资源列表（优先使用独立获取的数据，否则使用分析结果）
const nodesCpuList = computed(() => {
  // 优先使用独立获取的数据
  if (Object.keys(nodesCpuData.value).length > 0) {
    return Object.entries(nodesCpuData.value)
      .map(([name, info]) => ({
        name,
        cpu: info.cpu_percent,
        mem: info.mem_percent || 0,
        pods: info.pod_count || 0,
        schedulable: !info.unschedulable
      }))
      .sort((a, b) => b.cpu - a.cpu);
  }
  // 否则使用分析结果
  const nodeStatus = migrationPlan.value?.node_status?.before;
  if (!nodeStatus) return [];
  return Object.entries(nodeStatus)
    .map(([name, cpu]) => ({
      name,
      cpu: cpu as number,
      mem: 0,
      pods: 0,
      schedulable: true
    }))
    .sort((a, b) => b.cpu - a.cpu);
});

// 根据CPU使用率返回颜色
const getNodeCpuColor = (cpu: number) => {
  if (cpu >= 80) return "#F56C6C"; // 红色
  if (cpu >= 60) return "#E6A23C"; // 橙色
  if (cpu >= 40) return "#409EFF"; // 蓝色
  return "#67C23A"; // 绿色
};

// 获取节点CPU百分比
const getNodeCpuPercent = (nodeName: string): number => {
  // 优先从独立获取的数据中查找
  if (nodesCpuData.value[nodeName]) {
    return nodesCpuData.value[nodeName].cpu_percent;
  }
  // 否则从分析结果中查找
  const nodeStatus = migrationPlan.value?.node_status?.before;
  if (nodeStatus && nodeStatus[nodeName] !== undefined) {
    return nodeStatus[nodeName];
  }
  return 0;
};

// 获取节点CPU绝对值（单位：m）
const getNodeCpuUsedM = (nodeName: string): number => {
  // 优先从独立获取的数据中查找
  if (nodesCpuData.value[nodeName]) {
    return Math.round(nodesCpuData.value[nodeName].cpu_used * 1000);
  }
  // 否则从分析结果中查找
  const nodeCpuUsed = migrationPlan.value?.node_cpu_used?.before;
  if (nodeCpuUsed && nodeCpuUsed[nodeName] !== undefined) {
    return nodeCpuUsed[nodeName];
  }
  return 0;
};

// 获取环境列表
const getEnvList = async () => {
  try {
    const res = await getAgentNames();
    if (res.data && res.data.length > 0) {
      envOptions.value = res.data;
      if (searchStore.env && envOptions.value.includes(searchStore.env)) {
        searchForm.env = searchStore.env;
      } else if (envOptions.value.length > 0) {
        searchForm.env = envOptions.value[0];
        searchStore.setEnv(envOptions.value[0]);
      }
      if (searchForm.env) {
        await loadInitialData();
      }
    }
  } catch (error) {
    console.error("获取环境列表失败:", error);
    ElMessage.error("获取环境列表失败");
  }
};

// 加载初始数据
const loadInitialData = async () => {
  await Promise.all([loadConfig(), loadStatus(), loadLogs()]);
};

// 环境变化处理
const handleEnvChange = (val: string) => {
  searchForm.env = val;
  searchStore.setEnv(val);
  migrationPlan.value = null;
  selectedMigrations.value = [];
  loadInitialData();
};

// 加载配置
const loadConfig = async () => {
  try {
    const res = await getLoadBalanceConfig();
    if (res.success && res.data) {
      Object.assign(configForm, res.data);
    }
  } catch (error) {
    console.error("加载配置失败:", error);
  }
};

// 加载状态
const loadStatus = async () => {
  try {
    const res = await getLoadBalanceStatus();
    if (res.success && res.data) {
      status.value = res.data;
    }
  } catch (error) {
    console.error("加载状态失败:", error);
  }
};

// 加载隔离Pod
const loadIsolatedPods = async () => {
  if (!searchForm.env) return;
  loadingIsolated.value = true;
  try {
    const res = await getIsolatedPods(searchForm.env);
    if (res.success && res.data) {
      isolatedPods.value = res.data;
    }
  } catch (error) {
    console.error("加载隔离Pod失败:", error);
  } finally {
    loadingIsolated.value = false;
  }
};

// 加载节点CPU
const loadNodesCpu = async () => {
  if (!searchForm.env) return;
  loadingNodesCpu.value = true;
  try {
    const res = await getNodesCpu(searchForm.env);
    if (res.success && res.data) {
      nodesCpuData.value = res.data;
    }
  } catch (error) {
    console.error("加载节点CPU失败:", error);
    ElMessage.error("加载节点CPU失败");
  } finally {
    loadingNodesCpu.value = false;
  }
};

// 加载日志
const loadLogs = async () => {
  loadingLogs.value = true;
  try {
    const res = await getLoadBalanceLogs(20);
    if (res.success && res.data) {
      logs.value = res.data;
    }
  } catch (error) {
    console.error("加载日志失败:", error);
  } finally {
    loadingLogs.value = false;
  }
};

// 保存配置
const handleSaveConfig = async () => {
  savingConfig.value = true;
  try {
    const res = await updateLoadBalanceConfig(configForm);
    if (res.success) {
      ElMessage.success("配置保存成功");
      configDialogVisible.value = false;
      await loadStatus();
    } else {
      ElMessage.error(res.error || "保存失败");
    }
  } catch (error) {
    console.error("保存配置失败:", error);
    ElMessage.error("保存配置失败");
  } finally {
    savingConfig.value = false;
  }
};

// 打开配置对话框
const openConfigDialog = async () => {
  configDialogVisible.value = true;
  if (!searchForm.env) return;
  // 加载namespace和节点列表
  try {
    const [nsRes, nodesRes] = await Promise.all([
      getNamespaces(searchForm.env),
      getNodesCpu(searchForm.env)
    ]);
    if (nsRes.success && nsRes.data) {
      namespaceOptions.value = nsRes.data;
    }
    if (nodesRes.success && nodesRes.data) {
      nodeOptions.value = Object.keys(nodesRes.data).sort();
    }
  } catch (error) {
    console.error("加载配置选项失败:", error);
  }
};

// 分析负载
const handleAnalyze = async () => {
  if (!searchForm.env) {
    ElMessage.warning("请先选择K8S环境");
    return;
  }

  analyzing.value = true;
  try {
    const res = await analyzeLoadBalance(searchForm.env);
    if (res.success && res.data) {
      migrationPlan.value = res.data;
      if (res.data.migrations && res.data.migrations.length > 0) {
        ElMessage.success(
          `分析完成，生成 ${res.data.migrations.length} 个迁移计划`
        );
      } else {
        ElMessage.info(res.data.message || "当前负载均衡，无需迁移");
      }
      await loadLogs();
    } else {
      ElMessage.error(res.error || "分析失败");
    }
  } catch (error) {
    console.error("分析失败:", error);
    ElMessage.error("分析失败");
  } finally {
    analyzing.value = false;
  }
};

// 迁移选择变化
const handleMigrationSelectionChange = (selection: MigrationItem[]) => {
  selectedMigrations.value = selection;
};

// 轮询检查Pod Ready状态
const pollPodsReady = async (
  podMapping: Map<string, { namespace: string; pod_name: string }>,
  maxAttempts: number = 120 // 5秒间隔，120次 = 10分钟
): Promise<boolean> => {
  // 跟踪已Ready的Pod，避免重复检查
  const readyPods = new Set<string>();
  const allPodNames = new Set(podMapping.keys());

  for (let attempt = 0; attempt < maxAttempts; attempt++) {
    if (stopRequested.value) return false;

    // 只检查尚未Ready的Pod
    const pendingPods = Array.from(podMapping.entries())
      .filter(([oldPod]) => !readyPods.has(oldPod))
      .map(([, info]) => info);

    if (pendingPods.length === 0) {
      return true; // 全部Ready
    }

    try {
      const res = await checkPodsStatus(searchForm.env, pendingPods);
      if (res.success && res.data) {
        const results = res.data as PodStatusResult[];

        // 更新迁移计划中的状态（逐个更新已Ready的Pod）
        if (migrationPlan.value) {
          results.forEach(r => {
            if (r.ready) {
              // 找到对应的old_pod
              for (const [oldPod, info] of podMapping.entries()) {
                if (info.pod_name === r.pod_name && !readyPods.has(oldPod)) {
                  readyPods.add(oldPod);
                  // 立即更新该Pod的状态
                  migrationPlan.value!.migrations =
                    migrationPlan.value!.migrations.map(m => {
                      if (m.pod_name === oldPod) {
                        return { ...m, status: "ready" as const };
                      }
                      return m;
                    });
                  break;
                }
              }
            }
          });
        }

        // 检查是否全部Ready
        if (readyPods.size === allPodNames.size) {
          return true;
        }
      }
    } catch (error) {
      console.error("检查Pod状态失败:", error);
    }

    // 等待5秒后重试（缩短间隔，更快响应）
    await new Promise(resolve => setTimeout(resolve, 5000));
  }
  return readyPods.size === allPodNames.size;
};

// 分批执行迁移的核心逻辑
const executeBatches = async (migrations: MigrationItem[]) => {
  const batchSize = configForm.batch_size || 4;
  const batches: MigrationItem[][] = [];

  // 分批
  for (let i = 0; i < migrations.length; i += batchSize) {
    batches.push(migrations.slice(i, i + batchSize));
  }

  let totalSuccess = 0;
  let totalReady = 0;
  let totalFailed = 0;

  // 初始化所有为pending状态
  if (migrationPlan.value) {
    const podNames = new Set(migrations.map(m => m.pod_name));
    migrationPlan.value.migrations = migrationPlan.value.migrations.map(m => {
      if (podNames.has(m.pod_name)) {
        return { ...m, status: "pending" as const };
      }
      return m;
    });
  }

  // 记录每批的新Pod信息，用于轮询检查
  const batchNewPods: Map<string, { namespace: string; pod_name: string }> =
    new Map();

  for (let batchIndex = 0; batchIndex < batches.length; batchIndex++) {
    // 检查是否请求停止
    if (stopRequested.value) {
      ElMessage.warning("已停止执行");
      break;
    }

    const batch = batches[batchIndex];
    const batchPodNames = new Set(batch.map(m => m.pod_name));
    batchNewPods.clear();

    // 标记当前批次为执行中
    if (migrationPlan.value) {
      migrationPlan.value.migrations = migrationPlan.value.migrations.map(m => {
        if (batchPodNames.has(m.pod_name)) {
          return { ...m, status: "executing" as const };
        }
        return m;
      });
    }

    ElMessage.info(
      `正在执行第 ${batchIndex + 1}/${batches.length} 批 (${batch.length} 个Pod)`
    );

    try {
      const res = await executeLoadBalance(searchForm.env, batch);

      if (res.success && res.data) {
        totalSuccess += res.data.success || 0;
        totalFailed += res.data.failed || 0;

        // 根据执行结果更新状态，收集新Pod信息
        if (migrationPlan.value && res.data.results) {
          const resultMap = new Map<
            string,
            { status: string; newPod?: string; namespace?: string }
          >();
          res.data.results.forEach((r: any) => {
            const key = r.old_pod || r.migration?.pod_name;
            if (key) {
              const status = r.status || (r.success ? "scheduled" : "failed");
              resultMap.set(key, {
                status,
                newPod: r.new_pod,
                namespace: r.namespace || r.migration?.namespace
              });
              // 收集调度成功的新Pod
              if (r.success && r.new_pod && r.namespace) {
                batchNewPods.set(key, {
                  namespace: r.namespace,
                  pod_name: r.new_pod
                });
              }
            }
          });

          migrationPlan.value.migrations = migrationPlan.value.migrations.map(
            m => {
              if (resultMap.has(m.pod_name)) {
                return {
                  ...m,
                  status: resultMap.get(m.pod_name)!.status as any
                };
              }
              return m;
            }
          );
        }

        // 轮询检查Pod Ready状态（即使有失败的也要检查成功的Pod）
        if (batchNewPods.size > 0) {
          ElMessage.info(`等待第 ${batchIndex + 1} 批 Pod Ready...`);
          const allReady = await pollPodsReady(batchNewPods);

          if (stopRequested.value) {
            break;
          }

          if (allReady) {
            totalReady += batchNewPods.size;
            ElMessage.success(`第 ${batchIndex + 1} 批全部 Ready`);
          } else {
            ElMessage.warning(`第 ${batchIndex + 1} 批部分Pod未Ready`);
          }
        }

        // 如果本批有失败的，停止执行
        if (res.data.failed > 0) {
          ElMessage.error(
            `第 ${batchIndex + 1} 批有 ${res.data.failed} 个失败，停止执行`
          );
          break;
        }
      } else {
        // 批次执行失败，标记为失败并停止
        totalFailed += batch.length;
        if (migrationPlan.value) {
          migrationPlan.value.migrations = migrationPlan.value.migrations.map(
            m => {
              if (batchPodNames.has(m.pod_name) && m.status === "executing") {
                return { ...m, status: "failed" as const };
              }
              return m;
            }
          );
        }
        ElMessage.error(
          `第 ${batchIndex + 1} 批执行失败: ${res.error || "未知错误"}，停止执行`
        );
        break;
      }
    } catch (error: any) {
      // 捕获异常（如504超时），标记为失败并停止
      totalFailed += batch.length;
      if (migrationPlan.value) {
        migrationPlan.value.migrations = migrationPlan.value.migrations.map(
          m => {
            if (batchPodNames.has(m.pod_name) && m.status === "executing") {
              return { ...m, status: "failed" as const };
            }
            return m;
          }
        );
      }
      const errMsg =
        error?.response?.status === 504
          ? "请求超时(504)"
          : error?.message || "网络异常";
      ElMessage.error(`第 ${batchIndex + 1} 批执行异常: ${errMsg}，停止执行`);
      console.error(`第 ${batchIndex + 1} 批执行异常:`, error);
      break;
    }
  }

  return { totalSuccess, totalReady, totalFailed, total: migrations.length };
};

// 执行选中的迁移
const handleExecute = async () => {
  if (selectedMigrations.value.length === 0) {
    ElMessage.warning("请先选择要执行的迁移");
    return;
  }

  const batchSize = configForm.batch_size || 4;
  const batchCount = Math.ceil(selectedMigrations.value.length / batchSize);

  try {
    await ElMessageBox.confirm(
      `确定要执行选中的 ${selectedMigrations.value.length} 个迁移吗？（分 ${batchCount} 批执行，每批 ${batchSize} 个）`,
      "确认执行",
      { confirmButtonText: "确定", cancelButtonText: "取消", type: "warning" }
    );

    executing.value = true;
    stopRequested.value = false;

    const result = await executeBatches([...selectedMigrations.value]);

    if (stopRequested.value) {
      ElMessage.warning(
        `已停止，共执行: ${result.totalSuccess}/${result.total} 调度成功, ${result.totalReady} 个Ready`
      );
    } else {
      ElMessage.success(
        `执行完成: ${result.totalSuccess}/${result.total} 调度成功, ${result.totalReady} 个Ready`
      );
    }

    selectedMigrations.value = [];
    await Promise.all([loadIsolatedPods(), loadLogs()]);
  } catch (error) {
    if (error !== "cancel") {
      console.error("执行失败:", error);
      ElMessage.error("执行失败");
    }
  } finally {
    executing.value = false;
    stopRequested.value = false;
  }
};

// 执行全部迁移
const handleExecuteAll = async () => {
  if (!migrationPlan.value || migrationPlan.value.migrations.length === 0) {
    return;
  }

  // 只执行未完成的（pending或failed状态，或无状态）
  const pendingMigrations = migrationPlan.value.migrations.filter(
    m => !m.status || m.status === "pending" || m.status === "failed"
  );
  if (pendingMigrations.length === 0) {
    ElMessage.info("没有待执行的迁移");
    return;
  }

  const batchSize = configForm.batch_size || 4;
  const batchCount = Math.ceil(pendingMigrations.length / batchSize);

  try {
    await ElMessageBox.confirm(
      `确定要执行全部 ${pendingMigrations.length} 个迁移吗？（分 ${batchCount} 批执行，每批 ${batchSize} 个）`,
      "确认执行",
      { confirmButtonText: "确定", cancelButtonText: "取消", type: "warning" }
    );

    executing.value = true;
    stopRequested.value = false;

    const result = await executeBatches([...pendingMigrations]);

    if (stopRequested.value) {
      ElMessage.warning(
        `已停止，共执行: ${result.totalSuccess}/${result.total} 调度成功, ${result.totalReady} 个Ready`
      );
    } else {
      ElMessage.success(
        `执行完成: ${result.totalSuccess}/${result.total} 调度成功, ${result.totalReady} 个Ready`
      );
    }

    await Promise.all([loadIsolatedPods(), loadLogs()]);
  } catch (error) {
    if (error !== "cancel") {
      console.error("执行失败:", error);
      ElMessage.error("执行失败");
    }
  } finally {
    executing.value = false;
    stopRequested.value = false;
  }
};

// 隔离Pod选择变化
const handleIsolatedSelectionChange = (selection: IsolatedPod[]) => {
  selectedIsolatedPods.value = selection;
};

// 清理隔离Pod
const handleCleanup = async () => {
  if (selectedIsolatedPods.value.length === 0) {
    ElMessage.warning("请先选择要清理的Pod");
    return;
  }

  try {
    await ElMessageBox.confirm(
      `确定要清理选中的 ${selectedIsolatedPods.value.length} 个隔离Pod吗？`,
      "确认清理",
      { confirmButtonText: "确定", cancelButtonText: "取消", type: "warning" }
    );

    cleaningUp.value = true;
    const pods = selectedIsolatedPods.value.map(p => ({
      name: p.name,
      namespace: p.namespace
    }));
    const res = await cleanupIsolatedPods(searchForm.env, pods);
    if (res.success && res.data) {
      ElMessage.success(`清理完成: ${res.data.success.length} 个成功`);
      selectedIsolatedPods.value = [];
      await Promise.all([loadIsolatedPods(), loadLogs()]);
    } else {
      ElMessage.error(res.error || "清理失败");
    }
  } catch (error) {
    if (error !== "cancel") {
      console.error("清理失败:", error);
      ElMessage.error("清理失败");
    }
  } finally {
    cleaningUp.value = false;
  }
};

// 格式化时间
const formatTime = (timestamp: string) => {
  if (!timestamp) return "";
  const date = new Date(timestamp);
  return date.toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit"
  });
};

// 获取日志标签类型
const getLogTagType = (action: string) => {
  if (action.includes("start")) return "warning";
  if (action.includes("done") || action.includes("cleanup")) return "success";
  if (action.includes("error") || action.includes("fail")) return "danger";
  return "info";
};

// 获取迭代日志标签类型
const getIterationLogType = (result: string | null) => {
  if (!result) return "info";
  if (result.includes("成功") || result.includes("迁移")) return "success";
  if (result.includes("跳过") || result.includes("skipped")) return "warning";
  if (result.includes("达到目标") || result.includes("完成")) return "success";
  return "info";
};

// 获取状态标签类型
const getStatusTagType = (status: string) => {
  switch (status) {
    case "ready":
      return "success";
    case "scheduled":
      return "primary";
    case "executing":
      return "warning";
    case "failed":
      return "danger";
    default:
      return "info";
  }
};

// 获取状态文本
const getStatusText = (status: string) => {
  switch (status) {
    case "ready":
      return "成功";
    case "scheduled":
      return "已调度";
    case "executing":
      return "执行中";
    case "failed":
      return "失败";
    case "pending":
      return "待执行";
    default:
      return status;
  }
};

// 停止执行
const handleStop = () => {
  stopRequested.value = true;
  ElMessage.warning("已请求停止，将在当前批次完成后停止");
};

onMounted(() => {
  getEnvList();
});
</script>

<style scoped>
.load-balance-container {
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

.mt-2 {
  margin-top: 8px;
}

.mb-4 {
  margin-bottom: 16px;
}

.mr-2 {
  margin-right: 8px;
}

.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.empty-state {
  padding: 40px 0;
}

.empty-state-small {
  padding: 20px 0;
}

.action-bar {
  display: flex;
  gap: 12px;
  justify-content: flex-end;
  padding-top: 16px;
  margin-top: 16px;
  border-top: 1px solid #ebeef5;
}

.action-bar-small {
  display: flex;
  justify-content: flex-end;
  padding-top: 8px;
  margin-top: 8px;
}

.log-container {
  max-height: 300px;
  overflow-y: auto;
}

.log-item {
  display: flex;
  gap: 8px;
  align-items: center;
  padding: 6px 0;
  font-size: 12px;
  border-bottom: 1px solid #f0f0f0;
}

.log-item:last-child {
  border-bottom: none;
}

.log-time {
  flex-shrink: 0;
  color: #909399;
}

.log-message {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.form-tip {
  margin-left: 12px;
  font-size: 12px;
  color: #909399;
}

.nodes-cpu-container {
  max-height: 280px;
  overflow-y: auto;
}

.node-cpu-item {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
  font-size: 12px;
}

.node-cpu-item:last-child {
  margin-bottom: 0;
}

.node-name {
  flex-shrink: 0;
  width: 90px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: #606266;
}

.node-progress {
  flex: 1;
}

.node-cpu-value {
  flex-shrink: 0;
  width: 50px;
  text-align: right;
  font-weight: 500;
}

.mt-4 {
  margin-top: 16px;
}

.iteration-logs {
  max-height: 300px;
  overflow-y: auto;
}

.iteration-log-item {
  padding: 8px 0;
  font-size: 12px;
  border-bottom: 1px solid #f0f0f0;
}

.iteration-log-item:last-child {
  border-bottom: none;
}

.iteration-header {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 6px;
}

.iteration-range {
  color: #409eff;
  font-weight: 500;
}

.iteration-node {
  color: #606266;
}

.tried-pods {
  padding-left: 16px;
}

.tried-pod-item {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 3px 0;
  font-size: 11px;
  color: #606266;
}

.pod-result.success {
  color: #67c23a;
}

.pod-result.failed {
  color: #f56c6c;
}

.pod-name {
  color: #303133;
  font-family: monospace;
}

.pod-cpu {
  color: #909399;
}

.pod-reason {
  color: #e6a23c;
  font-size: 10px;
}

.pod-target {
  color: #67c23a;
}

.pod-effect {
  color: #409eff;
  font-size: 10px;
}

.iteration-details {
  color: #909399;
  font-size: 11px;
  padding-left: 16px;
}

.node-percent {
  color: #909399;
  font-size: 12px;
}
</style>
