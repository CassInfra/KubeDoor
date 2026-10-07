<template>
  <div class="silence-container">
    <!-- 顶部：状态筛选 + 搜索 + 新建 -->
    <div class="top-section">
      <div class="status-tabs">
        <div
          v-for="tab in statusTabs"
          :key="tab.value"
          class="status-tab"
          :class="{ active: query.status === tab.value }"
          :style="
            query.status === tab.value
              ? { borderColor: tab.color, color: tab.color }
              : {}
          "
          @click="handleStatusChange(tab.value)"
        >
          <span class="tab-dot" :style="{ background: tab.color }" />
          <span class="tab-label">{{ tab.label }}</span>
          <span class="tab-count">{{ tab.count }}</span>
        </div>
      </div>

      <div class="top-actions">
        <el-input
          v-model="query.keyword"
          placeholder="搜索匹配条件 / 备注 / 创建人"
          clearable
          style="width: 260px"
          @keyup.enter="handleSearch"
          @clear="handleSearch"
        >
          <template #prefix>
            <el-icon><Search /></el-icon>
          </template>
        </el-input>

        <el-tooltip
          effect="dark"
          :content="
            autoRefreshInterval === 0 ? '自动刷新已关闭' : '设置自动刷新间隔'
          "
          placement="top"
        >
          <el-button-group>
            <el-button plain type="primary" @click="fetchList">
              <el-icon style="margin-right: 4px"><RefreshRight /></el-icon>
              刷新
            </el-button>
            <el-dropdown trigger="click" @command="handleRefreshIntervalChange">
              <el-button plain type="primary">
                {{ autoRefreshInterval === 0 ? "" : autoRefreshInterval + "s" }}
                <el-icon style="margin-left: 4px"><ArrowDown /></el-icon>
              </el-button>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item :command="0">关</el-dropdown-item>
                  <el-dropdown-item :command="15">15s</el-dropdown-item>
                  <el-dropdown-item :command="30">30s</el-dropdown-item>
                  <el-dropdown-item :command="60">1m</el-dropdown-item>
                  <el-dropdown-item :command="300">5m</el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
          </el-button-group>
        </el-tooltip>

        <el-button type="primary" @click="handleCreate">
          <el-icon style="margin-right: 4px"><Plus /></el-icon>
          新建屏蔽
        </el-button>
      </div>
    </div>

    <!-- 列表 -->
    <el-card v-loading="loading" class="content">
      <el-table
        :data="tableData"
        style="width: 100%"
        :row-class-name="rowClassName"
      >
        <!-- 状态 -->
        <el-table-column label="状态" width="104" align="center">
          <template #default="{ row }">
            <el-tooltip
              effect="dark"
              :content="statusTooltip(row)"
              placement="top"
            >
              <el-tag
                :type="STATUS_META[row.status].tagType"
                :effect="row.status === 'active' ? 'dark' : 'plain'"
                size="small"
              >
                {{ STATUS_META[row.status].label }}
              </el-tag>
            </el-tooltip>
          </template>
        </el-table-column>

        <!-- 匹配条件 -->
        <el-table-column label="匹配条件" min-width="320">
          <template #default="{ row }">
            <div class="matcher-tags">
              <el-tag
                v-for="(matcher, index) in row.matchers"
                :key="index"
                size="small"
                :type="isRegexOp(matcher.op) ? 'warning' : 'info'"
                effect="plain"
                class="matcher-tag"
              >
                <span class="mt-key">{{ matcher.key }}</span>
                <span class="mt-op">{{ matcher.op }}</span>
                <span class="mt-value">{{ matcher.value || "（空）" }}</span>
              </el-tag>
            </div>
          </template>
        </el-table-column>

        <!-- 备注 -->
        <el-table-column
          prop="comment"
          label="备注"
          min-width="150"
          show-overflow-tooltip
        >
          <template #default="{ row }">
            <span v-if="row.comment">{{ row.comment }}</span>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>

        <!-- 生效时间 -->
        <el-table-column label="屏蔽周期" min-width="190">
          <template #default="{ row }">
            <div class="period">
              <div>{{ formatTime(row.starts_at) }}</div>
              <div class="period-end">
                至
                <span v-if="row.ends_at">{{ formatTime(row.ends_at) }}</span>
                <el-tag v-else type="warning" size="small" effect="plain">
                  长期
                </el-tag>
              </div>
            </div>
          </template>
        </el-table-column>

        <!-- 剩余时间 -->
        <el-table-column label="剩余" width="150">
          <template #default="{ row }">
            <div v-if="row.status === 'active'">
              <el-progress
                :percentage="elapsedPercent(row)"
                :stroke-width="6"
                :show-text="false"
                :color="elapsedColor(row)"
              />
              <div class="remain-text">{{ remainText(row) }}</div>
            </div>
            <div v-else-if="row.status === 'pending'" class="remain-text">
              {{ pendingText(row) }}后生效
            </div>
            <div v-else class="remain-text muted">
              {{ endedText(row) }}
            </div>
          </template>
        </el-table-column>

        <!-- 命中次数 -->
        <el-table-column label="已屏蔽" width="106" align="center">
          <template #header>
            <el-tooltip
              effect="dark"
              content="该规则累计拦截的告警通知条数，点击查看被屏蔽的告警"
              placement="top"
            >
              <span>已屏蔽</span>
            </el-tooltip>
          </template>
          <template #default="{ row }">
            <el-tooltip
              v-if="row.match_count > 0"
              effect="dark"
              :content="'最近命中：' + formatTime(row.last_match_at)"
              placement="top"
            >
              <span class="hit-count" @click="goSilencedAlerts(row)">
                {{ row.match_count }}
              </span>
            </el-tooltip>
            <span v-else class="muted">0</span>
          </template>
        </el-table-column>

        <!-- 创建人 -->
        <el-table-column label="创建人" width="110" show-overflow-tooltip>
          <template #default="{ row }">
            <span v-if="row.created_by">{{ row.created_by }}</span>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>

        <!-- 操作 -->
        <el-table-column label="操作" width="150" align="center" fixed="right">
          <template #default="{ row }">
            <el-button
              v-if="row.status === 'active' || row.status === 'pending'"
              link
              type="warning"
              size="small"
              @click="handleRevoke(row)"
            >
              解除
            </el-button>
            <el-button
              v-else
              link
              type="success"
              size="small"
              @click="handleExtend(row, 120)"
            >
              重新启用
            </el-button>

            <el-dropdown
              trigger="click"
              @command="cmd => handleCommand(cmd, row)"
            >
              <el-button link type="primary" size="small">
                更多<el-icon class="el-icon--right"><ArrowDown /></el-icon>
              </el-button>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item command="edit">编辑</el-dropdown-item>
                  <el-dropdown-item command="clone">克隆</el-dropdown-item>
                  <el-dropdown-item
                    v-for="preset in EXTEND_PRESETS"
                    :key="preset.minutes"
                    :command="'extend:' + preset.minutes"
                    :divided="preset.minutes === 30"
                  >
                    续期 {{ preset.label }}
                  </el-dropdown-item>
                  <el-dropdown-item command="delete" divided>
                    <span style="color: var(--el-color-danger)">删除</span>
                  </el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
          </template>
        </el-table-column>

        <template #empty>
          <div class="empty-box">
            <div class="empty-title">
              {{
                query.status === "all"
                  ? "还没有配置任何屏蔽规则"
                  : `没有${currentStatusLabel}的屏蔽规则`
              }}
            </div>
            <div class="empty-tip">
              屏蔽规则命中后，告警仍会入库留痕，只是不再推送通知。
            </div>
            <el-button type="primary" plain @click="handleCreate">
              新建屏蔽规则
            </el-button>
          </div>
        </template>
      </el-table>

      <div class="pagination">
        <el-pagination
          v-model:current-page="query.page"
          v-model:page-size="query.pageSize"
          :page-sizes="[10, 20, 50, 100]"
          layout="total, sizes, prev, pager, next"
          :total="total"
          @size-change="fetchList"
          @current-change="fetchList"
        />
      </div>
    </el-card>

    <SilenceEditDialog
      v-model="dialogVisible"
      :rule="editingRule"
      :prefill="prefillMatchers"
      @saved="fetchList"
    />
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted, onUnmounted } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";
import { Search, Plus, ArrowDown, RefreshRight } from "@element-plus/icons-vue";
import dayjs from "dayjs";
import {
  getSilenceList,
  revokeSilence,
  extendSilence,
  deleteSilence,
  type SilenceRule,
  type SilenceStatus,
  type SilenceMatcher,
  type SilenceStats
} from "@/api/silence";
import { useUserStoreHook } from "@/store/modules/user";
import SilenceEditDialog from "./components/SilenceEditDialog.vue";

defineOptions({ name: "AlarmSilence" });

const route = useRoute();
const router = useRouter();

const STATUS_META: Record<
  SilenceStatus,
  {
    label: string;
    tagType: "success" | "primary" | "info" | "warning";
    color: string;
  }
> = {
  active: { label: "生效中", tagType: "success", color: "#67C23A" },
  pending: { label: "待生效", tagType: "primary", color: "#409EFF" },
  expired: { label: "已过期", tagType: "info", color: "#909399" },
  revoked: { label: "已解除", tagType: "warning", color: "#E6A23C" }
};

const EXTEND_PRESETS = [
  { label: "30分钟", minutes: 30 },
  { label: "2小时", minutes: 120 },
  { label: "1天", minutes: 1440 },
  { label: "7天", minutes: 10080 }
];

const loading = ref(false);
const tableData = ref<SilenceRule[]>([]);
const total = ref(0);
const stats = ref<SilenceStats>({
  total: 0,
  active: 0,
  pending: 0,
  expired: 0,
  revoked: 0
});

const query = reactive({
  status: "all" as SilenceStatus | "all",
  keyword: "",
  page: 1,
  pageSize: 20
});

const dialogVisible = ref(false);
const editingRule = ref<SilenceRule | null>(null);
const prefillMatchers = ref<SilenceMatcher[] | null>(null);

// 每秒推进一次，驱动剩余时间与进度条实时刷新
const nowTs = ref(Date.now());
let tickTimer: ReturnType<typeof setInterval> | null = null;

const autoRefreshInterval = ref(0);
let refreshTimer: ReturnType<typeof setInterval> | null = null;

const statusTabs = computed(() => [
  {
    value: "active" as const,
    label: "生效中",
    count: stats.value.active,
    color: STATUS_META.active.color
  },
  {
    value: "pending" as const,
    label: "待生效",
    count: stats.value.pending,
    color: STATUS_META.pending.color
  },
  {
    value: "expired" as const,
    label: "已过期",
    count: stats.value.expired,
    color: STATUS_META.expired.color
  },
  {
    value: "revoked" as const,
    label: "已解除",
    count: stats.value.revoked,
    color: STATUS_META.revoked.color
  },
  {
    value: "all" as const,
    label: "全部",
    count: stats.value.total,
    color: "#606266"
  }
]);

const currentStatusLabel = computed(() =>
  query.status === "all" ? "" : STATUS_META[query.status as SilenceStatus].label
);

const isRegexOp = (op: string) => op === "=~" || op === "!~";

const formatTime = (value: string | null) =>
  value ? dayjs(value).format("YYYY-MM-DD HH:mm:ss") : "—";

const humanize = (seconds: number) => {
  const abs = Math.max(0, Math.floor(seconds));
  const d = Math.floor(abs / 86400);
  const h = Math.floor((abs % 86400) / 3600);
  const m = Math.floor((abs % 3600) / 60);
  const s = abs % 60;
  if (d > 0) return `${d}天${h}小时`;
  if (h > 0) return `${h}小时${m}分`;
  if (m > 0) return `${m}分${s}秒`;
  return `${s}秒`;
};

const remainSeconds = (row: SilenceRule) => {
  if (!row.ends_at) return null;
  return (dayjs(row.ends_at).valueOf() - nowTs.value) / 1000;
};

const remainText = (row: SilenceRule) => {
  const remain = remainSeconds(row);
  if (remain === null) return "长期有效";
  return remain <= 0 ? "即将过期" : `还剩 ${humanize(remain)}`;
};

const pendingText = (row: SilenceRule) =>
  humanize((dayjs(row.starts_at).valueOf() - nowTs.value) / 1000);

const endedText = (row: SilenceRule) => {
  if (row.status === "revoked") {
    const by = row.revoked_by ? `由 ${row.revoked_by} ` : "";
    return `${by}已解除`;
  }
  if (!row.ends_at) return "已结束";
  return `已过期 ${humanize((nowTs.value - dayjs(row.ends_at).valueOf()) / 1000)}`;
};

const elapsedPercent = (row: SilenceRule) => {
  if (!row.ends_at) return 0;
  const start = dayjs(row.starts_at).valueOf();
  const end = dayjs(row.ends_at).valueOf();
  if (end <= start) return 100;
  const pct = ((nowTs.value - start) / (end - start)) * 100;
  return Math.min(100, Math.max(0, Math.round(pct)));
};

// 越接近结束越醒目，提醒运维该规则快要失效
const elapsedColor = (row: SilenceRule) => {
  const pct = elapsedPercent(row);
  if (pct >= 90) return "#F56C6C";
  if (pct >= 70) return "#E6A23C";
  return "#67C23A";
};

const statusTooltip = (row: SilenceRule) => {
  switch (row.status) {
    case "active":
      return `${remainText(row)}，命中的告警不会推送通知`;
    case "pending":
      return `${pendingText(row)}后开始屏蔽`;
    case "expired":
      return `${endedText(row)}，规则已不再生效`;
    default:
      return `${formatTime(row.revoked_at)} ${endedText(row)}`;
  }
};

// 过期/解除的行整体淡化，与生效中的规则形成明显区分
const rowClassName = ({ row }: { row: SilenceRule }) =>
  row.status === "expired" || row.status === "revoked" ? "row-inactive" : "";

// -- 数据 -------------------------------------------------------------------
const fetchList = async () => {
  loading.value = true;
  try {
    const res = await getSilenceList({
      status: query.status,
      keyword: query.keyword,
      page: query.page,
      pageSize: query.pageSize
    });
    tableData.value = res.data || [];
    total.value = res.total || 0;
    stats.value = res.stats || stats.value;
  } catch (error: any) {
    ElMessage.error(error?.response?.data?.msg || "获取屏蔽规则失败");
  } finally {
    loading.value = false;
  }
};

const handleSearch = () => {
  query.page = 1;
  fetchList();
};

const handleStatusChange = (status: SilenceStatus | "all") => {
  query.status = status;
  query.page = 1;
  fetchList();
};

const handleRefreshIntervalChange = (interval: number) => {
  autoRefreshInterval.value = interval;
  if (refreshTimer) {
    clearInterval(refreshTimer);
    refreshTimer = null;
  }
  if (interval > 0) {
    refreshTimer = setInterval(fetchList, interval * 1000);
  }
};

// -- 操作 -------------------------------------------------------------------
const currentUser = () =>
  useUserStoreHook()?.nickname || useUserStoreHook()?.username || "";

const handleCreate = () => {
  editingRule.value = null;
  prefillMatchers.value = null;
  dialogVisible.value = true;
};

const handleRevoke = async (row: SilenceRule) => {
  try {
    await ElMessageBox.confirm(
      "解除后该规则立即失效，命中的告警将恢复通知。规则记录会保留，可随时重新启用。",
      "解除屏蔽",
      {
        type: "warning",
        confirmButtonText: "确定解除",
        cancelButtonText: "取消"
      }
    );
  } catch {
    return;
  }
  try {
    await revokeSilence(row.id, currentUser());
    ElMessage.success("已解除屏蔽");
    fetchList();
  } catch (error: any) {
    ElMessage.error(error?.response?.data?.msg || "解除失败");
  }
};

const handleExtend = async (row: SilenceRule, minutes: number) => {
  try {
    await extendSilence(row.id, dayjs().add(minutes, "minute").format());
    ElMessage.success(
      `已续期，屏蔽至 ${dayjs().add(minutes, "minute").format("MM-DD HH:mm")}`
    );
    fetchList();
  } catch (error: any) {
    ElMessage.error(error?.response?.data?.msg || "续期失败");
  }
};

const handleDelete = async (row: SilenceRule) => {
  try {
    await ElMessageBox.confirm(
      "删除后规则记录不可恢复，历史命中统计也会一并丢失。若只是想停止屏蔽，建议改用「解除」。",
      "删除屏蔽规则",
      {
        type: "warning",
        confirmButtonText: "确定删除",
        cancelButtonText: "取消"
      }
    );
  } catch {
    return;
  }
  try {
    await deleteSilence(row.id);
    ElMessage.success("已删除");
    fetchList();
  } catch (error: any) {
    ElMessage.error(error?.response?.data?.msg || "删除失败");
  }
};

const handleCommand = (command: string, row: SilenceRule) => {
  if (command === "edit") {
    editingRule.value = row;
    prefillMatchers.value = null;
    dialogVisible.value = true;
  } else if (command === "clone") {
    editingRule.value = null;
    prefillMatchers.value = row.matchers.map(item => ({ ...item }));
    dialogVisible.value = true;
  } else if (command.startsWith("extend:")) {
    handleExtend(row, Number(command.split(":")[1]));
  } else if (command === "delete") {
    handleDelete(row);
  }
};

// 跳到告警详情页，只看被这条规则屏蔽掉的告警
const goSilencedAlerts = (row: SilenceRule) => {
  const envMatcher = row.matchers.find(
    item => item.key === "env" && item.op === "="
  );
  router.push({
    name: "alarm-detail",
    query: {
      silenced: "1",
      timeRange: 7,
      status: ["firing", "resolved"],
      ...(envMatcher ? { env: envMatcher.value } : {})
    }
  });
};

// -- 生命周期 ---------------------------------------------------------------
onMounted(() => {
  // 从告警详情页的「已屏蔽」标记跳过来时，按规则 ID 直接定位
  const keyword = route.query.keyword as string | undefined;
  if (keyword) {
    query.keyword = keyword;
  }

  // IM 通知里的【屏蔽】链接会带 prefill 参数进来，直接打开预填好的新建弹窗
  const prefill = route.query.prefill as string | undefined;
  if (prefill) {
    try {
      const parsed = JSON.parse(decodeURIComponent(prefill));
      const matchers = Object.entries(parsed)
        .filter(([, value]) => value)
        .map(([key, value]) => ({
          key,
          op: "=" as const,
          value: String(value)
        }));
      if (matchers.length) {
        prefillMatchers.value = matchers;
        editingRule.value = null;
        dialogVisible.value = true;
      }
    } catch {
      ElMessage.warning("屏蔽链接参数解析失败，请手动填写匹配条件");
    }
    router.replace({ path: route.path, query: {} });
  }

  fetchList();
  tickTimer = setInterval(() => (nowTs.value = Date.now()), 1000);
});

onUnmounted(() => {
  if (tickTimer) clearInterval(tickTimer);
  if (refreshTimer) clearInterval(refreshTimer);
});
</script>

<style scoped>
.top-section {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  align-items: center;
  justify-content: space-between;
  padding: 12px 16px;
  margin-bottom: 16px;
  background-color: var(--el-bg-color);
  border-radius: 8px;
}

/* 状态筛选 */
.status-tabs {
  display: flex;
  gap: 8px;
}

.status-tab {
  display: flex;
  gap: 6px;
  align-items: center;
  padding: 6px 14px;
  font-size: 13px;
  color: var(--el-text-color-regular);
  cursor: pointer;
  border: 1px solid var(--el-border-color);
  border-radius: 18px;
  transition: all 0.2s ease;
}

.status-tab:hover {
  border-color: var(--el-color-primary-light-5);
}

.status-tab.active {
  font-weight: 600;
  background: var(--el-fill-color-light);
}

.tab-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
}

.tab-count {
  font-weight: 700;
}

.top-actions {
  display: flex;
  gap: 10px;
  align-items: center;
}

/* 匹配条件 */
.matcher-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.matcher-tag {
  font-family: var(--el-font-family-mono, monospace);
}

.mt-key {
  font-weight: 600;
}

.mt-op {
  margin: 0 4px;
  color: var(--el-color-primary);
}

.mt-value {
  color: var(--el-text-color-primary);
}

/* 周期与剩余 */
.period {
  font-size: 12px;
  line-height: 1.7;
}

.period-end {
  color: var(--el-text-color-secondary);
}

.remain-text {
  margin-top: 3px;
  font-size: 12px;
  color: var(--el-text-color-regular);
}

.muted {
  color: var(--el-text-color-secondary);
}

.hit-count {
  font-size: 15px;
  font-weight: 700;
  color: var(--el-color-primary);
  cursor: pointer;
}

.hit-count:hover {
  text-decoration: underline;
}

/* 失效规则整行淡化，一眼区分于生效中的规则 */
:deep(.row-inactive) {
  opacity: 0.55;
}

:deep(.row-inactive:hover) {
  opacity: 1;
}

.empty-box {
  padding: 28px 0;
}

.empty-title {
  margin-bottom: 6px;
  font-size: 14px;
  color: var(--el-text-color-regular);
}

.empty-tip {
  margin-bottom: 16px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.pagination {
  display: flex;
  justify-content: flex-end;
  margin-top: 16px;
}
</style>
