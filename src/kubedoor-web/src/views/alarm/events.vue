<template>
  <div class="events-container events-page">
    <!-- 顶部筛选菜单 -->
    <div class="filter-menu mb-3">
      <el-card class="filter-card">
        <!-- 第一行：基本筛选项 -->
        <div class="filter-row">
          <!-- 时间范围选择器 -->
          <div class="filter-item">
            <label class="filter-label">时间</label>
            <el-date-picker
              v-model="dateRange"
              type="daterange"
              unlink-panels
              range-separator="至"
              start-placeholder="开始日期"
              end-placeholder="结束日期"
              :shortcuts="shortcuts"
              size="default"
              format="YYYY-MM-DD"
              value-format="YYYY-MM-DD"
              style="width: 220px"
            />
          </div>

          <!-- K8S字段选择 -->
          <div class="filter-item">
            <label class="filter-label">K8S</label>
            <el-select
              v-model="selectedK8s"
              placeholder="请选择K8S集群"
              filterable
              style="width: 180px"
            >
              <el-option
                v-for="item in k8sList"
                :key="item"
                :label="item"
                :value="item"
              />
            </el-select>
          </div>

          <!-- 命名空间字段选择 -->
          <div class="filter-item">
            <label class="filter-label">空间</label>
            <el-select
              v-model="selectedNamespace"
              placeholder="请选择命名空间"
              filterable
              clearable
              :disabled="!selectedK8s"
              style="width: 100px"
              @clear="() => (selectedNamespace = '[全部]')"
            >
              <el-option
                v-for="item in namespaceList"
                :key="item"
                :label="item"
                :value="item"
              />
            </el-select>
          </div>

          <!-- Kind字段选择 -->
          <div class="filter-item">
            <label class="filter-label">Kind</label>
            <el-select
              v-model="selectedKind"
              placeholder="请选择Kind"
              filterable
              clearable
              :disabled="!selectedK8s"
              style="width: 120px"
              @clear="() => (selectedKind = '[全部]')"
            >
              <el-option
                v-for="item in kindList"
                :key="item"
                :label="item"
                :value="item"
              />
            </el-select>
          </div>

          <!-- Name字段选择 -->
          <div class="filter-item">
            <label class="filter-label">名称</label>
            <el-select
              v-model="selectedName"
              filterable
              clearable
              :disabled="!selectedK8s"
              style="width: 200px"
              @clear="() => (selectedName = '[全部]')"
            >
              <el-option
                v-for="item in nameList"
                :key="item"
                :label="item"
                :value="item"
              />
            </el-select>
          </div>

          <!-- Reason字段选择 -->
          <div class="filter-item">
            <label class="filter-label">原因</label>
            <el-tooltip
              content="支持输入, 模糊匹配"
              placement="top"
              effect="dark"
            >
              <el-select
                v-model="selectedReason"
                placeholder="支持输入, 模糊匹配"
                filterable
                clearable
                allow-create
                :disabled="!selectedK8s"
                style="width: 180px"
                @clear="() => (selectedReason = '[全部]')"
              >
                <el-option
                  v-for="item in reasonList"
                  :key="item"
                  :label="item"
                  :value="item"
                />
              </el-select>
            </el-tooltip>
          </div>

          <!-- Level字段选择 -->
          <div class="filter-item">
            <label class="filter-label">级别</label>
            <el-select
              v-model="selectedLevel"
              placeholder="默认全部"
              clearable
              style="width: 100px"
            >
              <el-option label="已告警" value="已告警" />
              <el-option label="Warning" value="Warning" />
              <el-option label="Normal" value="Normal" />
            </el-select>
          </div>

          <!-- 展开/收起按钮 -->
          <div class="filter-item">
            <el-button
              type="text"
              class="toggle-btn"
              @click="toggleAdvancedFilters"
            >
              <el-icon
                class="toggle-icon"
                :class="{ 'is-expanded': showAdvancedFilters }"
              >
                <ArrowDown />
              </el-icon>
              {{ showAdvancedFilters ? "收起" : "更多" }}
            </el-button>
          </div>

          <!-- 刷新按钮 -->
          <div class="filter-item">
            <el-button type="primary" :loading="loading" @click="handleRefresh">
              刷新
            </el-button>
          </div>

          <!-- 重置按钮 -->
          <div class="filter-item">
            <el-button type="primary" plain @click="resetFilters">
              重置
            </el-button>
          </div>
        </div>

        <!-- 第二行：高级筛选项（可折叠） -->
        <el-collapse-transition>
          <div v-show="showAdvancedFilters" class="filter-row advanced-filters">
            <!-- ReportingComponent字段选择 -->
            <div class="filter-item">
              <label class="filter-label">来源</label>
              <el-select
                v-model="selectedReportingComponent"
                placeholder="请选择ReportingComponent"
                filterable
                clearable
                :disabled="!selectedK8s"
                style="width: 220px"
                @clear="() => (selectedReportingComponent = '[全部]')"
              >
                <el-option
                  v-for="item in reportingComponentList"
                  :key="item"
                  :label="item"
                  :value="item"
                />
              </el-select>
            </div>

            <!-- ReportingInstance字段选择 -->
            <div class="filter-item">
              <label class="filter-label">来源IP</label>
              <el-select
                v-model="selectedReportingInstance"
                placeholder="请选择ReportingInstance"
                filterable
                clearable
                :disabled="!selectedK8s"
                style="width: 140px"
                @clear="() => (selectedReportingInstance = '[全部]')"
              >
                <el-option
                  v-for="item in reportingInstanceList"
                  :key="item"
                  :label="item"
                  :value="item"
                />
              </el-select>
            </div>

            <!-- Count字段输入 -->
            <div class="filter-item">
              <label class="filter-label">次数</label>
              <el-input-number
                v-model="selectedCount"
                :min="0"
                :controls="false"
                style="width: 100px"
              >
                <template #prefix><span>≧</span></template>
              </el-input-number>
            </div>

            <!-- Message字段输入 -->
            <div class="filter-item">
              <label class="filter-label">消息</label>
              <el-input
                v-model="selectedMessage"
                placeholder="支持模糊匹配"
                style="width: 260px"
              />
            </div>

            <!-- Limit字段选择 -->
            <div class="filter-item">
              <label class="filter-label">Limit</label>
              <el-select
                v-model="selectedLimit"
                placeholder="请选择Limit"
                style="width: 100px"
              >
                <el-option label="50" :value="50" />
                <el-option label="100" :value="100" />
                <el-option label="200" :value="200" />
                <el-option label="300" :value="300" />
                <el-option label="500" :value="500" />
                <el-option label="1000" :value="1000" />
              </el-select>
            </div>
          </div>
        </el-collapse-transition>
      </el-card>
    </div>

    <!-- 事件数据表格展示区域 -->
    <div v-if="eventsData.length > 0" class="events-table">
      <el-table
        :data="eventsData"
        stripe
        border
        style="width: 100%"
        :loading="loading"
      >
        <el-table-column label="状态" align="center" width="80">
          <template #default="{ row }">
            <el-tag
              :type="
                row[0] === 'ADDED'
                  ? 'success'
                  : row[0] === 'DELETED'
                    ? 'danger'
                    : 'warning'
              "
              size="small"
            >
              {{
                row[0] === "ADDED"
                  ? "新增"
                  : row[0] === "DELETED"
                    ? "删除"
                    : "更新"
              }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="级别" align="center" width="90">
          <template #default="{ row }">
            <el-tag
              :type="
                row[1] === '已告警'
                  ? 'danger'
                  : row[1] === 'Warning'
                    ? 'warning'
                    : 'primary'
              "
              effect="dark"
            >
              {{ row[1] }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column
          label="次数"
          width="80"
          align="center"
          sortable
          prop="2"
        >
          <template #default="{ row }">
            <span :style="getCountStyle(row[2])">{{ row[2] }}</span>
          </template>
        </el-table-column>
        <el-table-column
          label="类型"
          width="100"
          align="center"
          show-overflow-tooltip
        >
          <template #default="{ row }">
            <el-tag size="small" :type="success">
              {{ row[3] || "-" }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="命名空间" width="100">
          <template #default="{ row }">
            {{ row[5] }}
          </template>
        </el-table-column>
        <el-table-column label="名称" width="150" show-overflow-tooltip>
          <template #default="{ row }">
            {{ row[6] }}
          </template>
        </el-table-column>
        <el-table-column label="原因" width="140" show-overflow-tooltip>
          <template #default="{ row }">
            <span style="color: #f55">{{ row[7] }}</span>
          </template>
        </el-table-column>
        <el-table-column label="消息" min-width="200" show-overflow-tooltip>
          <template #default="{ row }">
            {{ row[8] }}
          </template>
        </el-table-column>
        <el-table-column label="首次时间" width="160">
          <template #default="{ row }">
            <span style="font-weight: bold; color: #409eff">
              {{ formatEventDate(row[9]) }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="最后时间" width="160">
          <template #default="{ row }">
            <span style="font-weight: bold; color: #409eff">{{
              formatEventDate(row[10])
            }}</span>
          </template>
        </el-table-column>
        <el-table-column label="来源" width="160" show-overflow-tooltip>
          <template #default="{ row }">
            {{ row[11] }}
          </template>
        </el-table-column>
        <el-table-column label="来源IP" width="120" show-overflow-tooltip>
          <template #default="{ row }">
            {{ row[12] }}
          </template>
        </el-table-column>
      </el-table>
    </div>

    <!-- 无数据提示 -->
    <div v-else-if="!loading" class="no-data">
      <el-empty description="暂无事件数据" />
    </div>
  </div>
</template>

<script lang="ts" setup>
import { ref, onMounted, watch, nextTick } from "vue";
import { useRoute, useRouter } from "vue-router";
import type { LocationQueryRaw, LocationQueryValue } from "vue-router";
import { ElMessage } from "element-plus";
import dayjs from "dayjs";
import { ArrowDown } from "@element-plus/icons-vue";
import { getAgentNames } from "@/api/istio";
import { getEventsMenu, queryEvents } from "@/api/alarm";
import { useSearchStoreHook } from "@/store/modules/search";
const searchStore = useSearchStoreHook();
const route = useRoute();
const router = useRouter();

const getDefaultDateRange = (): [string, string] => {
  const now = new Date();
  const beijingTime = new Date(now.getTime() + 8 * 60 * 60 * 1000);
  const today = beijingTime.toISOString().split("T")[0];
  return [today, today];
};

// 响应式数据
const dateRange = ref<[string, string] | null>(getDefaultDateRange());
const selectedK8s = ref<string>(searchStore.env || "");
const selectedNamespace = ref<string>("");
const selectedKind = ref<string>("");
const selectedName = ref<string>("");
const selectedReason = ref<string>("");
const selectedReportingComponent = ref<string>("");
const selectedReportingInstance = ref<string>("");
const showAdvancedFilters = ref<boolean>(false);
const selectedLevel = ref<string>("");
const selectedCount = ref<number | null>(null);
const selectedMessage = ref<string>("");
const selectedLimit = ref<number>(50);
const k8sList = ref<string[]>([]);
const namespaceList = ref<string[]>([]);
const kindList = ref<string[]>([]);
const nameList = ref<string[]>([]);
const reasonList = ref<string[]>([]);
const reportingComponentList = ref<string[]>([]);
const reportingInstanceList = ref<string[]>([]);
const eventsMenuData = ref<Record<string, any[]> | null>(null);
const eventsData = ref<any[]>([]);
const loading = ref(false);

const getStringFromQuery = (
  value: LocationQueryValue | LocationQueryValue[] | undefined
): string | undefined => {
  if (value === undefined || value === null) {
    return undefined;
  }
  const list = Array.isArray(value) ? value : [value];
  for (const item of list) {
    const normalized = (item ?? "").toString().trim();
    if (normalized.length > 0) {
      return normalized;
    }
  }
  return undefined;
};

const getNumberFromQuery = (
  value: LocationQueryValue | LocationQueryValue[] | undefined
): number | undefined => {
  const str = getStringFromQuery(value);
  if (str === undefined) {
    return undefined;
  }
  const parsed = Number(str);
  return Number.isNaN(parsed) ? undefined : parsed;
};

const areDateRangesEqual = (
  a: [string, string] | null,
  b: [string, string] | null
) => {
  if (a === b) {
    return true;
  }
  if (!a || !b) {
    return false;
  }
  return a[0] === b[0] && a[1] === b[1];
};

const buildRouteQueryFromFilters = (): Record<string, string | undefined> => {
  const query: Record<string, string | undefined> = {};
  if (dateRange.value && dateRange.value.length === 2) {
    query.start_time = dateRange.value[0];
    query.end_time = dateRange.value[1];
  }
  if (selectedK8s.value) {
    query.k8s = selectedK8s.value;
  }
  const isPlaceholder = (value?: string) =>
    !value || value === "[全部]" || value === "[空值]";

  if (!isPlaceholder(selectedNamespace.value)) {
    query.namespace = selectedNamespace.value;
  }
  if (!isPlaceholder(selectedKind.value)) {
    query.kind = selectedKind.value;
  }
  if (!isPlaceholder(selectedName.value)) {
    query.name = selectedName.value;
  }
  if (!isPlaceholder(selectedReason.value)) {
    query.reason = selectedReason.value;
  }
  if (!isPlaceholder(selectedReportingComponent.value)) {
    query.reporting_component = selectedReportingComponent.value;
  }
  if (!isPlaceholder(selectedReportingInstance.value)) {
    query.reporting_instance = selectedReportingInstance.value;
  }
  if (selectedLevel.value) {
    query.level = selectedLevel.value;
  }
  if (selectedCount.value !== null && selectedCount.value !== undefined) {
    query.count = String(selectedCount.value);
  }
  if (selectedMessage.value) {
    query.message = selectedMessage.value;
  }
  if (selectedLimit.value) {
    query.limit = String(selectedLimit.value);
  }
  return query;
};

let pendingRouteSyncs = 0;
let applyingRouteToFilters = false;
let filtersInitialized = false;
let filterWatcherSuppressDepth = 0;
let allowWatcherDrivenReloads = false;
let routeWatcherReady = false;
let agentNamesCache: string[] | null = null;
let agentNamesPromise: Promise<string[]> | null = null;

const syncRouteQueryFromFilters = () => {
  const serializedQuery = buildRouteQueryFromFilters();
  const keys = new Set([
    ...Object.keys(route.query),
    ...Object.keys(serializedQuery)
  ]);
  const nextQuery: LocationQueryRaw = { ...route.query };
  let changed = false;

  const setOrDelete = (key: string, value: string | undefined) => {
    const current = route.query[key];
    const normalizedCurrent = Array.isArray(current)
      ? current[current.length - 1]
      : (current as string | undefined);
    if (value === undefined) {
      if (normalizedCurrent !== undefined) {
        delete nextQuery[key];
        changed = true;
      }
      return;
    }
    if (normalizedCurrent !== value) {
      nextQuery[key] = value;
      changed = true;
    }
  };

  keys.forEach(key => {
    setOrDelete(key, serializedQuery[key]);
  });

  if (changed) {
    pendingRouteSyncs += 1;
    router
      .replace({
        path: route.path,
        query: nextQuery,
        hash: route.hash
      })
      .finally(() => {
        pendingRouteSyncs = Math.max(0, pendingRouteSyncs - 1);
      });
  }
};

const applyRouteQueryToFilters = () => {
  let changed = false;

  runWithFilterWatcherSuppressed(() => {
    const start = getStringFromQuery(route.query.start_time);
    const end = getStringFromQuery(route.query.end_time);
    const resolvedRange = (
      start && end ? [start, end] : (dateRange.value ?? getDefaultDateRange())
    ) as [string, string];
    if (!areDateRangesEqual(dateRange.value, resolvedRange)) {
      dateRange.value = resolvedRange;
      changed = true;
    }

    const resolveString = (current: string, incoming?: string) =>
      incoming !== undefined ? incoming : current;

    const resolveNumber = (current: number, incoming?: number) =>
      incoming !== undefined ? incoming : current;

    const nextK8s = resolveString(
      selectedK8s.value || searchStore.env || k8sList.value[0] || "",
      getStringFromQuery(route.query.k8s)
    );
    if (selectedK8s.value !== nextK8s) {
      selectedK8s.value = nextK8s;
      changed = true;
    }

    const stringTargets: Array<
      [
        { value: string },
        string,
        LocationQueryValue | LocationQueryValue[] | undefined
      ]
    > = [
      [selectedNamespace, selectedNamespace.value || "", route.query.namespace],
      [selectedKind, selectedKind.value || "", route.query.kind],
      [selectedName, selectedName.value || "", route.query.name],
      [selectedReason, selectedReason.value || "", route.query.reason],
      [
        selectedReportingComponent,
        selectedReportingComponent.value || "",
        route.query.reporting_component
      ],
      [
        selectedReportingInstance,
        selectedReportingInstance.value || "",
        route.query.reporting_instance
      ],
      [selectedLevel, selectedLevel.value || "", route.query.level],
      [selectedMessage, selectedMessage.value || "", route.query.message]
    ];

    stringTargets.forEach(([target, fallback, source]) => {
      const nextValue = resolveString(fallback, getStringFromQuery(source));
      if (target.value !== nextValue) {
        target.value = nextValue;
        changed = true;
      }
    });

    const countValue = getNumberFromQuery(route.query.count);
    const resolvedCount = countValue ?? null;
    if (selectedCount.value !== resolvedCount) {
      selectedCount.value = resolvedCount;
      changed = true;
    }

    const limitValue = resolveNumber(
      selectedLimit.value || 100,
      getNumberFromQuery(route.query.limit)
    );
    if (selectedLimit.value !== limitValue) {
      selectedLimit.value = limitValue;
      changed = true;
    }
  });

  return changed;
};

// 格式化日期为本地时间字符串
const formatLocalDate = (date: Date): string => {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
};

const formatEventDate = (value: string | number | Date | null) => {
  if (!value) {
    return "-";
  }
  const parsed = dayjs(value);
  if (!parsed.isValid()) {
    return value as string;
  }
  return parsed.format("YY/MM/DD HH:mm:ss");
};

const runWithFilterWatcherSuppressed = (fn: () => void) => {
  filterWatcherSuppressDepth += 1;
  try {
    fn();
  } finally {
    nextTick(() => {
      filterWatcherSuppressDepth = Math.max(0, filterWatcherSuppressDepth - 1);
    });
  }
};

const areFilterWatchersSuppressed = () => filterWatcherSuppressDepth > 0;

const getCountStyle = (value: string | number) => {
  const count = Number(value) || 0;
  return {
    fontWeight: 600,
    color: count >= 10 ? "#f56c6c" : "#303133"
  };
};

const getEventTypeTagType = (value: string) => {
  if (!value) {
    return "info";
  }
  const lower = value.toLowerCase();
  if (lower.includes("warn")) {
    return "warning";
  }
  if (lower.includes("normal") || lower.includes("success")) {
    return "success";
  }
  if (
    lower.includes("error") ||
    lower.includes("fail") ||
    lower.includes("crit") ||
    lower.includes("alarm")
  ) {
    return "danger";
  }
  return "info";
};

// 时间快捷选项
const shortcuts = [
  {
    text: "今天",
    value: () => {
      const today = new Date();
      const todayStr = formatLocalDate(today);
      return [todayStr, todayStr];
    }
  },
  {
    text: "昨天",
    value: () => {
      const yesterday = new Date();
      yesterday.setTime(yesterday.getTime() - 3600 * 1000 * 24);
      const yesterdayStr = formatLocalDate(yesterday);
      return [yesterdayStr, yesterdayStr];
    }
  },
  {
    text: "最近3天",
    value: () => {
      const end = new Date();
      const start = new Date();
      start.setTime(start.getTime() - 3600 * 1000 * 24 * 3);
      return [formatLocalDate(start), formatLocalDate(end)];
    }
  },
  {
    text: "本周",
    value: () => {
      const today = new Date();
      const dayOfWeek = today.getDay(); // 0是周日，1是周一
      const mondayOffset = dayOfWeek === 0 ? -6 : 1 - dayOfWeek; // 计算到周一的偏移

      const monday = new Date(today);
      monday.setDate(today.getDate() + mondayOffset);

      const sunday = new Date(monday);
      sunday.setDate(monday.getDate() + 6);

      return [formatLocalDate(monday), formatLocalDate(sunday)];
    }
  },
  {
    text: "最近1周",
    value: () => {
      const end = new Date();
      const start = new Date();
      start.setTime(start.getTime() - 3600 * 1000 * 24 * 7);
      return [formatLocalDate(start), formatLocalDate(end)];
    }
  },
  {
    text: "最近1个月",
    value: () => {
      const end = new Date();
      const start = new Date();
      start.setTime(start.getTime() - 3600 * 1000 * 24 * 30);
      return [formatLocalDate(start), formatLocalDate(end)];
    }
  },
  {
    text: "最近3个月",
    value: () => {
      const end = new Date();
      const start = new Date();
      start.setTime(start.getTime() - 3600 * 1000 * 24 * 90);
      return [formatLocalDate(start), formatLocalDate(end)];
    }
  }
];

// 加载K8S列表
const loadK8sList = async () => {
  try {
    if (!agentNamesCache) {
      if (!agentNamesPromise) {
        agentNamesPromise = getAgentNames();
      }
      const response = await agentNamesPromise;
      agentNamesPromise = null;
      if (response.success && Array.isArray(response.data)) {
        if (response.data.length > 0 && typeof response.data[0] === "string") {
          agentNamesCache = response.data;
        } else if (
          response.data.length > 0 &&
          Array.isArray(response.data[0])
        ) {
          agentNamesCache = response.data.map(item => item[0]);
        } else if (
          response.data.length > 0 &&
          typeof response.data[0] === "object"
        ) {
          agentNamesCache = response.data.map(
            item => Object.values(item)[0] as string
          );
        } else {
          agentNamesCache = [];
        }
      } else {
        agentNamesCache = [];
      }
    }

    if (agentNamesCache) {
      runWithFilterWatcherSuppressed(() => {
        k8sList.value = [...agentNamesCache!];
        if (k8sList.value.length > 0) {
          if (searchStore.env && k8sList.value.includes(searchStore.env)) {
            selectedK8s.value = searchStore.env;
          } else {
            selectedK8s.value = k8sList.value[0];
            searchStore.setEnv(k8sList.value[0]);
          }
        }
      });
    }
  } catch (error) {
    console.error("加载K8S列表失败:", error);
    ElMessage.error("加载K8S列表失败");
  }
};

// 切换高级筛选显示状态
const toggleAdvancedFilters = () => {
  showAdvancedFilters.value = !showAdvancedFilters.value;
};

// 重置筛选条件
const resetFilters = () => {
  runWithFilterWatcherSuppressed(() => {
    dateRange.value = getDefaultDateRange();

    if (k8sList.value.length > 0) {
      if (searchStore.env && k8sList.value.includes(searchStore.env)) {
        selectedK8s.value = searchStore.env;
      } else {
        selectedK8s.value = k8sList.value[0];
      }
    } else {
      selectedK8s.value = searchStore.env || "";
    }

    selectedNamespace.value = "[全部]";
    selectedKind.value = "";
    selectedName.value = "[全部]";
    selectedReason.value = "";
    selectedLevel.value = "";
    selectedReportingComponent.value = "[全部]";
    selectedReportingInstance.value = "[全部]";
    selectedCount.value = null;
    selectedMessage.value = "";
    selectedLimit.value = 100;
  });

  eventsData.value = [];
  showAdvancedFilters.value = false;

  ElMessage.success("筛选条件已重置");
  handleLocalFilterChange({ reloadMenu: true, clearMenu: true, force: true });
};

const handleRefresh = () => {
  if (!selectedK8s.value || !dateRange.value) {
    ElMessage.warning("请选择K8S集群和时间范围");
    return;
  }
  syncRouteQueryFromFilters();
  requestDataReload();
};

// 查询事件数据
const queryEventsData = async () => {
  if (!selectedK8s.value || !dateRange.value) {
    ElMessage.warning("请选择K8S集群和时间范围");
    return;
  }

  loading.value = true;
  try {
    const params = {
      k8s: selectedK8s.value,
      start_time: dateRange.value[0],
      end_time: dateRange.value[1],
      limit: selectedLimit.value
    };

    // 如果选择了命名空间，添加namespace参数
    if (selectedNamespace.value) {
      params.namespace = selectedNamespace.value;
    }

    // 如果选择了level，添加level参数
    if (selectedLevel.value) {
      params.level = selectedLevel.value;
    }

    // 如果输入了count，添加count参数
    if (selectedCount.value !== null && selectedCount.value !== undefined) {
      params.count = selectedCount.value;
    }

    // 如果选择了kind，添加kind参数
    if (selectedKind.value) {
      params.kind = selectedKind.value;
    }

    // 如果选择了name，添加name参数
    if (selectedName.value) {
      params.name = selectedName.value;
    }

    // 如果选择了reason，添加reason参数
    if (selectedReason.value) {
      params.reason = selectedReason.value;
    }

    // 如果选择了reportingComponent，添加reporting_component参数
    if (selectedReportingComponent.value) {
      params.reporting_component = selectedReportingComponent.value;
    }

    // 如果选择了reportingInstance，添加reporting_instance参数
    if (selectedReportingInstance.value) {
      params.reporting_instance = selectedReportingInstance.value;
    }

    // 如果输入了message，添加message参数
    if (selectedMessage.value) {
      params.message = selectedMessage.value;
    }

    const response = await queryEvents(params);

    console.log("事件查询API响应:", response);
    if (response.success && response.data) {
      // 处理事件列表数据
      eventsData.value = response.data;
      console.log("查询到的事件数据:", eventsData.value);
    } else {
      // 清空事件数据
      eventsData.value = [];
      ElMessage.warning("未查询到相关事件数据");
    }
  } catch (error) {
    console.error("查询事件数据失败:", error);
    ElMessage.error("查询事件数据失败");
    // 清空事件数据
    eventsData.value = [];
  } finally {
    loading.value = false;
  }
};

// 加载事件菜单数据
const loadEventsMenu = async () => {
  if (!selectedK8s.value || !dateRange.value) {
    ElMessage.warning("请选择K8S集群和时间范围");
    return;
  }

  loading.value = true;
  try {
    const params = {
      k8s: selectedK8s.value,
      start_time: dateRange.value[0],
      end_time: dateRange.value[1],
      limit: selectedLimit.value
    };

    // 如果选择了命名空间，添加namespace参数
    if (selectedNamespace.value) {
      params.namespace = selectedNamespace.value;
    }

    // 如果选择了level，添加level参数
    if (selectedLevel.value) {
      params.level = selectedLevel.value;
    }

    // 如果输入了count，添加count参数
    if (selectedCount.value !== null && selectedCount.value !== undefined) {
      params.count = selectedCount.value;
    }

    // 如果输入了message，添加message参数
    if (selectedMessage.value) {
      params.message = selectedMessage.value;
    }

    const response = await getEventsMenu(params);

    console.log("事件菜单API响应:", response);
    if (response.success && response.data) {
      // 处理菜单字段的数据
      runWithFilterWatcherSuppressed(() => {
        namespaceList.value = response.data.namespace || [];
        kindList.value = response.data.kind || [];
        nameList.value = response.data.name || [];
        reasonList.value = response.data.reason || [];
        reportingComponentList.value = response.data.reportingComponent || [];
        reportingInstanceList.value = response.data.reportingInstance || [];

        if (namespaceList.value.length > 0 && !selectedNamespace.value) {
          selectedNamespace.value = namespaceList.value[0];
        }

        if (kindList.value.length > 0 && !selectedKind.value) {
          selectedKind.value = kindList.value[0];
        }
        if (nameList.value.length > 0 && !selectedName.value) {
          selectedName.value = nameList.value[0];
        }
        if (reasonList.value.length > 0 && !selectedReason.value) {
          selectedReason.value = reasonList.value[0];
        }
        if (
          reportingComponentList.value.length > 0 &&
          !selectedReportingComponent.value
        ) {
          selectedReportingComponent.value = reportingComponentList.value[0];
        }
        if (
          reportingInstanceList.value.length > 0 &&
          !selectedReportingInstance.value
        ) {
          selectedReportingInstance.value = reportingInstanceList.value[0];
        }
      });

      console.log("处理后的菜单数据:", {
        namespace: namespaceList.value,
        kind: kindList.value,
        name: nameList.value,
        reason: reasonList.value,
        reportingComponent: reportingComponentList.value,
        reportingInstance: reportingInstanceList.value
      });

      eventsMenuData.value = response.data;
    } else {
      // 清空菜单数据
      namespaceList.value = [];
      kindList.value = [];
      nameList.value = [];
      reasonList.value = [];
      reportingComponentList.value = [];
      reportingInstanceList.value = [];
      eventsMenuData.value = null;
      ElMessage.warning("未查询到相关事件数据");
    }
  } catch (error) {
    console.error("加载事件菜单失败:", error);
    ElMessage.error("加载事件菜单失败");
    // 清空菜单数据
    namespaceList.value = [];
    kindList.value = [];
    nameList.value = [];
    reasonList.value = [];
    reportingComponentList.value = [];
    reportingInstanceList.value = [];
    eventsMenuData.value = null;
  } finally {
    loading.value = false;
  }
};

const clearMenuLists = () => {
  namespaceList.value = [];
  kindList.value = [];
  nameList.value = [];
  reasonList.value = [];
  reportingComponentList.value = [];
  reportingInstanceList.value = [];
};

type RequestReloadOptions = {
  reloadMenu?: boolean;
  clearMenu?: boolean;
  force?: boolean;
};

let reloadScheduled = false;
let menuReloadNeeded = false;

const requestDataReload = (options: RequestReloadOptions = {}) => {
  const { reloadMenu = false, clearMenu = false } = options;
  if (clearMenu) {
    clearMenuLists();
  }
  if (reloadMenu) {
    menuReloadNeeded = true;
  }
  if (reloadScheduled) {
    return;
  }
  reloadScheduled = true;
  Promise.resolve().then(async () => {
    try {
      if (!filtersInitialized || applyingRouteToFilters) {
        return;
      }
      if (!selectedK8s.value || !dateRange.value) {
        return;
      }
      const shouldReloadMenu = menuReloadNeeded;
      menuReloadNeeded = false;
      if (shouldReloadMenu) {
        await loadEventsMenu();
      }
      await queryEventsData();
    } catch (error) {
      console.error("刷新事件数据失败:", error);
    } finally {
      allowWatcherDrivenReloads = true;
      routeWatcherReady = true;
      reloadScheduled = false;
    }
  });
};

const handleLocalFilterChange = (options?: RequestReloadOptions) => {
  if (
    !filtersInitialized ||
    applyingRouteToFilters ||
    areFilterWatchersSuppressed() ||
    (!allowWatcherDrivenReloads && !options?.force)
  ) {
    return;
  }
  syncRouteQueryFromFilters();
  requestDataReload(options);
};

const dataOnlyFilterSources = [
  selectedKind,
  selectedName,
  selectedReason,
  selectedReportingComponent,
  selectedReportingInstance,
  selectedLevel,
  selectedCount,
  selectedMessage,
  selectedLimit
];

watch(
  () => selectedK8s.value,
  newVal => {
    if (newVal) {
      searchStore.setEnv(newVal);
    }
    handleLocalFilterChange({ reloadMenu: true, clearMenu: true });
  }
);

watch(
  () => dateRange.value,
  () => {
    handleLocalFilterChange({ reloadMenu: true, clearMenu: true });
  },
  { deep: true }
);

watch(
  () => selectedNamespace.value,
  () => {
    handleLocalFilterChange({ reloadMenu: true });
  }
);

watch(
  dataOnlyFilterSources,
  () => {
    handleLocalFilterChange();
  },
  { deep: true }
);

watch(
  () => route.query,
  () => {
    if (!routeWatcherReady) {
      return;
    }
    if (pendingRouteSyncs > 0) {
      return;
    }
    if (!filtersInitialized) {
      return;
    }
    applyingRouteToFilters = true;
    const changed = applyRouteQueryToFilters();
    applyingRouteToFilters = false;
    if (changed && filtersInitialized) {
      requestDataReload({ reloadMenu: true, clearMenu: true });
    }
  },
  { deep: true }
);

// 组件挂载时加载数据
onMounted(async () => {
  await loadK8sList();
  applyingRouteToFilters = true;
  applyRouteQueryToFilters();
  applyingRouteToFilters = false;
  filtersInitialized = true;
  syncRouteQueryFromFilters();
  if (selectedK8s.value && dateRange.value) {
    requestDataReload({ reloadMenu: true });
  }
});
</script>

<style scoped>
/* 响应式设计 */
@media screen and (width <= 768px) {
  .filter-row {
    flex-direction: column;
    align-items: stretch;
  }

  .filter-item {
    flex-direction: column;
    gap: 4px;
    align-items: stretch;
  }

  .filter-label {
    font-size: 14px;
  }
}

.events-container {
  padding: 8px;
}

.filter-menu {
  margin-bottom: 20px;
}

.filter-card {
  border-radius: 8px;
}

.filter-row {
  display: flex;
  flex-wrap: wrap;
  gap: 20px;
  align-items: center;
}

.filter-item {
  display: flex;
  gap: 4px;
  align-items: center;
}

.filter-label {
  font-weight: 500;
  color: var(--el-text-color-regular);
  white-space: nowrap;
}

/* 高级筛选区域样式 */
.advanced-filters {
  padding-top: 16px;
  margin-top: 16px;
  border-top: 1px solid #e4e7ed;
}

/* 展开/收起按钮样式 */
.toggle-btn {
  display: flex;
  gap: 4px;
  align-items: center;
  padding: 4px;
  font-size: 14px;
  color: #409eff;
}

.toggle-btn:hover {
  color: #66b1ff;
}

.toggle-icon {
  transition: transform 0.3s ease;
}

.toggle-icon.is-expanded {
  transform: rotate(180deg);
}

.events-menu {
  margin-bottom: 20px;
}

.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.menu-content {
  max-height: 600px;
  overflow-y: auto;
}

.menu-section {
  padding-bottom: 16px;
  margin-bottom: 24px;
  border-bottom: 1px solid var(--el-border-color-lighter);
}

.menu-section:last-child {
  margin-bottom: 0;
  border-bottom: none;
}

.menu-title {
  margin: 0 0 12px;
  font-size: 16px;
  font-weight: 600;
  color: var(--el-text-color-primary);
}

.menu-items {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.menu-tag {
  margin: 2px;
}

.empty-state {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 300px;
}

/* 事件表格样式 */
.events-table {
  margin-top: 20px;
}

.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.event-count {
  font-size: 14px;
  color: #909399;
}

.no-data {
  padding: 40px 0;
  margin-top: 20px;
  text-align: center;
}

/* 覆盖全局main-content的margin设置，只影响当前页面 */
.events-page {
  /* 抵消父级的24px margin，设置为10px效果 */
  padding: 10px;
  margin: 0 !important;
}
</style>
