<template>
  <div class="k8s-overview">
    <section class="hero-panel">
      <div class="hero-main">
        <div class="hero-left">
          <div class="hero-meta">
            <div class="refresh-compact">
              <span class="refresh-label">自动刷新</span>
              <el-switch
                v-model="autoRefresh"
                size="small"
                inline-prompt
                active-text="开"
                inactive-text="关"
              />
              <div class="hero-updated">上次更新：{{ lastUpdatedText }}</div>
            </div>
            <p class="hero-eyebrow">
              资源管理 · 集群大屏 · 数据来源：VictoriaMetrics
            </p>
          </div>
          <div class="hero-title-row">
            <h1>K8S资源总览</h1>
            <el-select
              v-model="selectedEnv"
              placeholder="全部集群"
              clearable
              size="small"
              class="env-select"
              popper-class="env-select-dropdown"
              @change="handleEnvChange"
            >
              <el-option
                v-for="env in envList"
                :key="env"
                :label="env"
                :value="env"
              />
            </el-select>
          </div>
          <div class="hero-stat-list">
            <div
              v-for="stat in heroHighlightCards"
              :key="stat.label"
              class="hero-stat"
              :style="heroStatStyle(stat.theme)"
            >
              <span class="label">{{ stat.label }}</span>
              <strong>{{ formatNumber(stat.value) }}</strong>
              <p>{{ stat.desc }}</p>
            </div>
          </div>
          <div class="hero-focus">
            <div
              v-for="badge in heroRiskBadges"
              :key="badge.label"
              class="focus-chip"
              :class="badge.status"
            >
              <span>{{ badge.label }}</span>
              <strong>{{ formatNumber(badge.value) }}</strong>
            </div>
          </div>
        </div>

        <div class="hero-right">
          <div class="health-list">
            <div
              v-for="item in maxUsageList"
              :key="item.key"
              class="health-item"
            >
              <div class="health-header">
                <div class="health-title">
                  <span class="label">{{ item.label }}</span>
                  <span class="name">{{ item.name }}</span>
                </div>
                <span class="value">{{ formatNumber(item.value) }}%</span>
              </div>
              <el-progress
                :percentage="Math.min(item.value, 100)"
                :stroke-width="8"
                :color="progressColor(item.value)"
                :show-text="false"
              />
            </div>
          </div>
        </div>
      </div>

      <div class="summary-grid">
        <div
          v-for="card in summaryCards"
          :key="card.key"
          class="summary-card"
          :style="cardGradientStyle(card.gradient)"
        >
          <div class="summary-icon">
            <iconify-icon-online :icon="card.icon" width="26" height="26" />
          </div>
          <div class="summary-meta">
            <p>{{ card.title }}</p>
            <h3>
              {{ formatNumber(card.value) }}
              <span v-if="card.unit" class="unit">{{ card.unit }}</span>
              <span v-if="card.subValue" class="sub">{{ card.subValue }}</span>
            </h3>
            <div
              v-if="card.trend !== undefined && card.trendLabel"
              :class="['card-trend', getTrendClass(card.trend)]"
            >
              <span>{{ formatTrend(card.trend) }}</span>
              <small>{{ card.trendLabel }}</small>
            </div>
          </div>
        </div>
      </div>
    </section>

    <section class="ck-charts-row">
      <div class="ck-card pod-alert-card">
        <div class="ck-card-header">
          <div>
            <h3>今日累计 · Top10 异常 Pod 告警</h3>
          </div>
        </div>
        <div v-if="podAlerts.length" class="progress-list">
          <el-tooltip
            v-for="item in podAlerts"
            :key="`${item.env}-${item.pod}-${item.alert_name}`"
            effect="dark"
            :content="`命名空间：${item.namespace} | Pod：${item.pod} | 描述：${item.description}`"
            placement="top-start"
            :show-after="80"
          >
            <div class="progress-item">
              <div class="progress-meta">
                <div class="meta-left">
                  <span class="env-tag">{{ item.env }}</span>
                  <span class="alert-name">{{ item.alert_name }}</span>
                </div>
                <div class="meta-right">
                  <span class="count">{{
                    formatNumber(item.count_firing)
                  }}</span>
                  <span class="ratio">{{ item.ratio_percent }}%</span>
                </div>
              </div>
              <div class="progress-bar">
                <div
                  class="progress-fill"
                  :style="barStyle(item.ratio_percent)"
                />
              </div>
            </div>
          </el-tooltip>
        </div>
        <div v-else class="empty">暂无数据</div>
      </div>

      <div class="ck-card pie-card">
        <div class="ck-card-header">
          <div>
            <p class="eyebrow">今日累计</p>
            <h3>Top10 异常 K8S 事件</h3>
          </div>
        </div>
        <div ref="eventPieRef" class="chart-shell" />
      </div>

      <div class="ck-card bar-card">
        <div class="ck-card-header">
          <div>
            <p class="eyebrow">最近 10 日</p>
            <h3>累计 Pod 告警数统计</h3>
          </div>
        </div>
        <div ref="alertBarRef" class="chart-shell" />
      </div>
    </section>
  </div>
</template>

<script setup lang="ts">
import {
  computed,
  nextTick,
  onBeforeUnmount,
  onMounted,
  ref,
  watch
} from "vue";
import { useIntervalFn } from "@vueuse/core";
import dayjs from "dayjs";
import { ElMessage } from "element-plus";
import * as echarts from "echarts/core";
import { PieChart, BarChart } from "echarts/charts";
import {
  TooltipComponent,
  LegendComponent,
  GridComponent,
  TitleComponent
} from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import {
  getPromOverview,
  getCkAlertDaily,
  getCkTopEvents,
  getCkTopPodAlerts,
  type CkAlertDailyItem,
  type CkEventItem,
  type CkPodAlertItem,
  type OverviewCard,
  type PromOverviewMetrics
} from "@/api/overview";
import { getPromEnv } from "@/api/monit";

echarts.use([
  PieChart,
  BarChart,
  TooltipComponent,
  LegendComponent,
  GridComponent,
  TitleComponent,
  CanvasRenderer
]);

const createPromOverviewMetrics = (): PromOverviewMetrics => ({
  clusters: 0,
  nodes: 0,
  workloads: 0,
  pods: 0,
  pvcs: 0,
  total_cpu_cores: 0,
  total_mem_bytes: 0,
  used_cpu_cores: 0,
  used_mem_bytes: 0,
  net_in_bytes_per_sec: 0,
  net_out_bytes_per_sec: 0,
  nodes_cpu_gt_70: 0,
  nodes_mem_gt_80: 0,
  nodes_pod_gt_90: 0,
  pvcs_usage_gt_80: 0,
  cpu_usage_percent: 0,
  mem_usage_percent: 0,
  max_cpu_usage_percent: ["", 0],
  max_mem_usage_percent: ["", 0],
  max_cpu_request_percent: ["", 0],
  max_mem_request_percent: ["", 0],
  max_pod_usage_percent: ["", 0],
  max_pvc_usage_percent: ["", 0]
});

type HeroHighlightTheme = {
  start: string;
  end: string;
  glow: string;
};

type HeroHighlightCard = {
  label: string;
  value: number;
  desc: string;
  theme: HeroHighlightTheme;
};

const promOverview = ref<PromOverviewMetrics>(createPromOverviewMetrics());
const envList = ref<string[]>([]);
const selectedEnv = ref<string>("");
const summaryCards = ref<OverviewCard[]>([]);
const lastUpdated = ref<string>("");
const podAlerts = ref<CkPodAlertItem[]>([]);
const eventTop10 = ref<CkEventItem[]>([]);
const alertDaily = ref<CkAlertDailyItem[]>([]);
const eventPieRef = ref<HTMLDivElement>();
const alertBarRef = ref<HTMLDivElement>();
let eventPieChart: echarts.ECharts | null = null;
let alertBarChart: echarts.ECharts | null = null;

const isFetching = ref(false);
const autoRefresh = ref(true);
const refreshSeconds = ref(30);

const lastUpdatedText = computed(() =>
  lastUpdated.value ? dayjs(lastUpdated.value).format("HH:mm:ss") : "--"
);

const heroHighlightThemes: HeroHighlightTheme[] = [
  { start: "#3df5a6", end: "#24c6dc", glow: "rgb(45 212 191 / 35%)" },
  { start: "#c084fc", end: "#60a5fa", glow: "rgb(129 140 248 / 35%)" },
  { start: "#3b82f6", end: "#06b6d4", glow: "rgb(59 130 246 / 35%)" },
  { start: "#f97316", end: "#fb7185", glow: "rgb(249 115 22 / 35%)" },
  { start: "#fbbf24", end: "#fcd34d", glow: "rgb(251 191 36 / 35%)" }
];

const heroHighlights = computed(() => [
  {
    label: "集群数量",
    value: promOverview.value.clusters,
    desc: "Kubernetes Clusters"
  },
  {
    label: "节点总数",
    value: promOverview.value.nodes,
    desc: "Nodes"
  },
  {
    label: "工作负载",
    value: promOverview.value.workloads,
    desc: "Workloads"
  },
  {
    label: "容器组",
    value: promOverview.value.pods,
    desc: "Pods"
  },
  {
    label: "持久卷",
    value: promOverview.value.pvcs,
    desc: "PVCs"
  }
]);

const heroHighlightCards = computed<HeroHighlightCard[]>(() =>
  heroHighlights.value.map((item, idx) => ({
    ...item,
    theme: heroHighlightThemes[idx % heroHighlightThemes.length]
  }))
);

const getStressBadgeStatus = (value: number) => {
  if (value >= 20) return "danger";
  if (value > 0) return "warning";
  return "success";
};

const heroRiskBadges = computed(() => [
  {
    label: "CPU% > 70% 节点",
    value: promOverview.value.nodes_cpu_gt_70,
    status: getStressBadgeStatus(promOverview.value.nodes_cpu_gt_70)
  },
  {
    label: "内存% > 80% 节点",
    value: promOverview.value.nodes_mem_gt_80,
    status: getStressBadgeStatus(promOverview.value.nodes_mem_gt_80)
  },
  {
    label: "Pod数 > 90% 节点",
    value: promOverview.value.nodes_pod_gt_90,
    status: getStressBadgeStatus(promOverview.value.nodes_pod_gt_90)
  },
  {
    label: "持久卷% > 80% 节点",
    value: promOverview.value.pvcs_usage_gt_80,
    status: getStressBadgeStatus(promOverview.value.pvcs_usage_gt_80)
  }
]);

const normalizeMaxMetric = (
  metric: PromOverviewMetrics["max_cpu_usage_percent"]
) => {
  const [name, rawValue] = metric ?? ["", 0];
  const numericValue = Number(rawValue);
  const value = Number.isFinite(numericValue)
    ? Number(numericValue.toFixed(2))
    : 0;
  return { name: name || "--", value };
};

const maxUsageList = computed(() => {
  const metrics = promOverview.value;
  return [
    {
      key: "max_cpu_usage_percent",
      label: "最高CPU使用率：",
      ...normalizeMaxMetric(metrics.max_cpu_usage_percent)
    },
    {
      key: "max_mem_usage_percent",
      label: "最高内存使用率：",
      ...normalizeMaxMetric(metrics.max_mem_usage_percent)
    },
    {
      key: "max_cpu_request_percent",
      label: "最高CPU已分配率：",
      ...normalizeMaxMetric(metrics.max_cpu_request_percent)
    },
    {
      key: "max_mem_request_percent",
      label: "最高内存已分配率：",
      ...normalizeMaxMetric(metrics.max_mem_request_percent)
    },
    {
      key: "max_pod_usage_percent",
      label: "最高Pod数使用率：",
      ...normalizeMaxMetric(metrics.max_pod_usage_percent)
    },
    {
      key: "max_pvc_usage_percent",
      label: "最高PVC使用率：",
      ...normalizeMaxMetric(metrics.max_pvc_usage_percent)
    }
  ];
});

const formatNumber = (value: number) =>
  Number.isInteger(value) ? value.toLocaleString("zh-CN") : value.toFixed(2);

const formatTrend = (value: number) =>
  `${value > 0 ? "+" : ""}${value.toFixed(1)}%`;

const getTrendClass = (value: number) =>
  value > 0 ? "trend-up" : value < 0 ? "trend-down" : "trend-flat";

const progressColor = (value: number) => {
  if (value >= 80) return "#f97316";
  if (value >= 60) return "#38bdf8";
  return "#34d399";
};

const barStyle = (value: number) => {
  const clamped = Math.min(Math.max(value, 1), 100);
  return {
    width: `${clamped}%`,
    background: "linear-gradient(90deg, #60a5fa, #a78bfa)",
    boxShadow: "0 6px 16px rgb(96 165 250 / 35%)"
  };
};

const heroStatStyle = (theme: HeroHighlightTheme) => ({
  "--hero-start": theme.start,
  "--hero-end": theme.end,
  "--hero-glow": theme.glow
});

const cardGradientStyle = (gradient: [string, string]) => ({
  backgroundImage: `linear-gradient(135deg, ${gradient[0]}, ${gradient[1]})`
});

const bytesToGiB = (bytes: number) =>
  Number((bytes / 1024 / 1024 / 1024).toFixed(2));

const formatThroughput = (bytesPerSecond: number) => {
  if (!bytesPerSecond) {
    return { value: 0, unit: "B/s" };
  }
  const units = ["B/s", "KiB/s", "MiB/s", "GiB/s", "TiB/s"];
  let value = bytesPerSecond;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  const precision = unitIndex === 0 ? 0 : 2;
  return {
    value: Number(value.toFixed(precision)),
    unit: units[unitIndex]
  };
};

const piePalette = [
  "#60a5fa",
  "#34d399",
  "#fbbf24",
  "#a78bfa",
  "#f472b6",
  "#22d3ee",
  "#fb7185",
  "#f59e0b",
  "#2dd4bf",
  "#7dd3fc"
];

const legendDotStyle = (idx: number) => {
  const color = piePalette[idx % piePalette.length];
  return {
    background: color,
    boxShadow: `0 0 0 4px ${color}33`
  };
};

const renderEventPie = () => {
  if (!eventPieRef.value) return;
  if (!eventPieChart) {
    eventPieChart = echarts.init(eventPieRef.value);
  }

  const data = eventTop10.value.map((item, idx) => ({
    name: item.reason || item.name || `事件${idx + 1}`,
    value: item.count,
    ratio: item.ratio_percent,
    env: item.env,
    namespace: item.namespace,
    kind: item.kind,
    resourceName: item.name,
    message: item.message,
    itemStyle: { color: piePalette[idx % piePalette.length] }
  }));

  eventPieChart.setOption({
    tooltip: {
      trigger: "item",
      backgroundColor: "rgba(15,23,42,0.9)",
      borderColor: "rgba(255,255,255,0.08)",
      textStyle: { color: "#e2e8f0" },
      formatter: (params: any) => {
        const d = params.data || {};
        return `
          <div style="line-height:1.6">
            <div><strong>${params.name}</strong></div>
            <div>集群：${d.env || "--"}</div>
            <div>命名空间：${d.namespace || "--"}</div>
            <div>类型：${d.kind || "--"}</div>
            <div>对象：${d.resourceName || "--"}</div>
            <div>次数：${formatNumber(params.value)}</div>
            <div>占比：${d.ratio || params.percent}%</div>
            <div style="max-width:240px;white-space:normal;">信息：${d.message || "--"}</div>
          </div>
        `;
      }
    },
    series: [
      {
        name: "异常事件",
        type: "pie",
        radius: ["38%", "72%"],
        center: ["50%", "50%"],
        roseType: "area",
        minAngle: 4,
        data,
        label: {
          color: "#cbd5e1",
          formatter: (p: any) => {
            const ratio = Number(p.data?.ratio ?? p.percent ?? 0);
            return `${p.name}\n${ratio.toFixed(2)}%`;
          }
        },
        labelLine: { length: 18, length2: 10, smooth: true }
      }
    ]
  });
};

const renderAlertDailyBar = () => {
  if (!alertBarRef.value) return;
  if (!alertBarChart) {
    alertBarChart = echarts.init(alertBarRef.value);
  }
  const categories = alertDaily.value.map(item => item.day_label);
  const days = alertDaily.value.map(item => item.day);
  const values = alertDaily.value.map(
    item => Number(item.daily_alert_count) || 0
  );

  alertBarChart.setOption({
    grid: { left: 56, right: 12, top: 26, bottom: 32 },
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "shadow" },
      backgroundColor: "rgba(15,23,42,0.9)",
      textStyle: { color: "#e2e8f0" },
      formatter: (params: any) => {
        const p = params?.[0];
        const idx = p?.dataIndex ?? 0;
        const day = days[idx] || p?.name || "";
        return `${day}<br/>累计告警：${formatNumber(p?.value ?? 0)}`;
      }
    },
    xAxis: {
      type: "category",
      data: categories,
      axisTick: { show: false },
      axisLine: { lineStyle: { color: "rgb(148 163 184 / 60%)" } },
      axisLabel: { color: "#cbd5e1" }
    },
    yAxis: {
      type: "value",
      axisLine: { show: false },
      splitLine: { lineStyle: { color: "rgb(148 163 184 / 20%)" } },
      axisLabel: {
        color: "#cbd5e1",
        formatter: (val: number) => formatNumber(Number(val))
      }
    },
    series: [
      {
        type: "bar",
        data: values,
        barWidth: 18,
        itemStyle: {
          borderRadius: [6, 6, 0, 0],
          color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: "#38bdf8" },
            { offset: 1, color: "#6366f1" }
          ])
        },
        emphasis: {
          itemStyle: {
            color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
              { offset: 0, color: "#a78bfa" },
              { offset: 1, color: "#6366f1" }
            ])
          }
        }
      }
    ]
  });
};

const resizeCharts = () => {
  eventPieChart?.resize();
  alertBarChart?.resize();
};

const destroyCharts = () => {
  eventPieChart?.dispose();
  alertBarChart?.dispose();
  eventPieChart = null;
  alertBarChart = null;
};

const buildPromSummaryCards = (
  metrics: PromOverviewMetrics
): OverviewCard[] => {
  const inbound = formatThroughput(metrics.net_in_bytes_per_sec);
  const outbound = formatThroughput(metrics.net_out_bytes_per_sec);
  const totalMemGiB = Math.round(bytesToGiB(metrics.total_mem_bytes));
  const usedMemGiB = Math.round(bytesToGiB(metrics.used_mem_bytes));
  const totalCpu = Math.round(metrics.total_cpu_cores);
  const usedCpu = Math.round(metrics.used_cpu_cores);
  const inboundVal = Math.round(inbound.value);
  const outboundVal = Math.round(outbound.value);
  return [
    {
      key: "total_cpu",
      title: "CPU总核数",
      value: totalCpu,
      unit: "核",
      icon: "mdi:cpu-64-bit",
      gradient: ["#48c6ef", "#6f86d6"]
    },
    {
      key: "used_cpu",
      title: "CPU使用量",
      value: usedCpu,
      unit: "核",
      icon: "mdi:chip",
      gradient: ["#fa709a", "#fee140"]
    },
    {
      key: "total_mem",
      title: "内存总量",
      value: totalMemGiB,
      unit: "GiB",
      icon: "mdi:memory",
      gradient: ["#1EAE98", "#38ef7d"]
    },
    {
      key: "used_mem",
      title: "内存使用量",
      value: usedMemGiB,
      unit: "GiB",
      icon: "mdi:server",
      gradient: ["#209cff", "#68e0cf"]
    },
    {
      key: "net_in",
      title: "入站流量",
      value: inboundVal,
      unit: inbound.unit,
      icon: "mdi:download-network-outline",
      gradient: ["#8e2de2", "#4a00e0"]
    },
    {
      key: "net_out",
      title: "出站流量",
      value: outboundVal,
      unit: outbound.unit,
      icon: "mdi:upload-network-outline",
      gradient: ["#3C8CE7", "#00EAFF"]
    }
  ];
};

const handleEnvChange = () => {
  fetchAllData();
};

const fetchAllData = async () => {
  if (isFetching.value) return;
  isFetching.value = true;
  try {
    const envParam = selectedEnv.value || undefined;
    const [promRes, podRes, eventRes, dailyRes] = await Promise.all([
      getPromOverview(envParam),
      getCkTopPodAlerts(envParam),
      getCkTopEvents(envParam),
      getCkAlertDaily(envParam)
    ]);
    const metrics = promRes.data ?? createPromOverviewMetrics();
    promOverview.value = metrics;
    summaryCards.value = buildPromSummaryCards(metrics);
    lastUpdated.value = promRes.timestamp || dayjs().toISOString();
    podAlerts.value = podRes.data ?? [];
    eventTop10.value = (eventRes.data ?? []).map(item => ({
      ...item,
      env: (item as any).env ?? (item as any).k8s ?? ""
    }));
    alertDaily.value = dailyRes.data ?? [];
    await nextTick();
    renderEventPie();
    renderAlertDailyBar();
  } catch (error) {
    console.error(error);
    ElMessage.error("获取资源总览数据失败");
  } finally {
    isFetching.value = false;
  }
};

const intervalMs = computed(() => refreshSeconds.value * 1000);

const { pause, resume } = useIntervalFn(
  () => {
    if (autoRefresh.value) {
      fetchAllData();
    }
  },
  intervalMs,
  { immediate: false }
);

watch([autoRefresh, intervalMs], () => {
  pause();
  if (autoRefresh.value) {
    resume();
  }
});

onMounted(async () => {
  window.addEventListener("resize", resizeCharts);
  try {
    const envRes = await getPromEnv();
    if (envRes.data) {
      envList.value = envRes.data;
    }
  } catch (error) {
    console.error("Failed to fetch env list:", error);
  }
  await fetchAllData();
  if (autoRefresh.value) {
    resume();
  }
});

onBeforeUnmount(() => {
  pause();
  window.removeEventListener("resize", resizeCharts);
  destroyCharts();
});
</script>

<style scoped lang="scss">
.k8s-overview {
  display: flex;
  flex-direction: column;
  gap: 10px;
  background: transparent;
}

.hero-panel {
  position: relative;
  padding: 28px;
  overflow: hidden;
  color: #e2e8f0;
  user-select: none;
  background:
    radial-gradient(circle at 20% 30%, rgb(139 92 246 / 35%), transparent 50%),
    radial-gradient(circle at 80% 10%, rgb(236 72 153 / 30%), transparent 55%),
    radial-gradient(circle at 50% 80%, rgb(59 130 246 / 25%), transparent 60%),
    linear-gradient(135deg, #0a0e1a, #1a1f35 40%, #0f172a 70%, #050a15), #0f172a;

  border-radius: 28px;
  box-shadow: 0 20px 60px rgb(2 6 23 / 40%);
  display: flex;
  flex-direction: column;
  gap: 18px;
}

.hero-main {
  position: relative;
  z-index: 1;
  display: flex;
  gap: 32px;
}

.hero-left {
  display: flex;
  flex: 1.4;
  flex-direction: column;
  gap: 20px;
}

.hero-eyebrow {
  display: inline-flex;
  align-items: center;
  margin: 0;
  white-space: nowrap;
  font-size: 13px;
  color: rgb(248 250 252 / 65%);
  letter-spacing: 0.1em;
}

.hero-meta {
  display: flex;
  align-items: center;
  gap: 18px;
  flex-wrap: nowrap;
}

.hero-meta .hero-updated {
  margin-left: auto;
}

.hero-title-row {
  display: flex;
  gap: 16px;
  align-items: center;
}

.hero-title-row h1 {
  margin: 0;
}

.env-select {
  width: 180px;

  :deep(.el-input__wrapper) {
    background: rgb(15 23 42 / 60%);
    border: 1px solid rgb(148 163 184 / 40%);
    box-shadow: none;
  }

  :deep(.el-select__placeholder) {
    color: var(--el-input-text-color, #ffffffba);
  }

  :deep(.el-select__wrapper) {
    background: #3a2f6d;
    box-shadow: 0 0 0 1px rgba(220, 223, 230, 0.18) inset;
  }

  :deep(.el-input__inner) {
    color: #e2e8f0;
  }

  :deep(.el-input__inner::placeholder) {
    color: rgb(226 232 240 / 60%);
  }

  :deep(.el-input__wrapper:hover) {
    border-color: rgb(139 92 246 / 60%);
  }

  :deep(.el-input__wrapper.is-focus) {
    border-color: rgb(139 92 246 / 80%);
    box-shadow: 0 0 0 1px rgb(139 92 246 / 30%);
  }
}

.hero-stat-list {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 14px;
}

.hero-stat {
  position: relative;
  padding: 16px;
  background:
    linear-gradient(145deg, rgb(45 68 129 / 90%), rgb(6 10 22 / 96%))
      padding-box,
    linear-gradient(135deg, var(--hero-start), var(--hero-end)) border-box;
  border: 1px solid transparent;
  border-bottom-width: 4px;
  border-radius: 18px;
  box-shadow:
    0 10px 24px rgb(14 165 233 / 20%),
    0 0 0 1px rgb(255 255 255 / 4%),
    0 14px 30px var(--hero-glow);
}

.hero-stat strong {
  display: block;
  margin: 6px 0;
  font-size: 28px;
  text-align: center;
}

.hero-stat p {
  text-align: center;
  font-size: 12px;
  color: rgb(226 232 240 / 70%);
}

.hero-focus {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.focus-chip {
  display: flex;
  gap: 10px;
  align-items: center;
  padding: 12px 18px;
  background: rgb(15 23 42 / 55%);
  border: 2px solid rgb(148 163 184 / 30%);
  border-radius: 999px;
}

.focus-chip.success {
  color: #4ade80;
  border-color: rgb(16 185 129 / 50%);
}

.focus-chip.warning {
  color: #facc15;
  border-color: rgb(251 191 36 / 50%);
}

.focus-chip.danger {
  color: #f87171;
  border-color: rgb(248 113 113 / 60%);
}

.hero-right {
  display: flex;
  flex: 1;
  flex-direction: column;
  gap: 14px;
  padding: 20px;
  background: rgb(15 23 42 / 45%);
  border: 1px solid rgba(107, 169, 255, 0.9);
  border-radius: 20px;
}

.hero-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

.refresh-compact {
  display: flex;
  align-items: center;
  gap: 10px;
}

.refresh-label {
  font-weight: 500;
  color: rgb(226 232 240 / 90%);
  font-size: 13px;
}

.refresh-hint {
  color: rgb(226 232 240 / 70%);
  font-size: 12px;
}

.hero-updated {
  font-size: 13px;
  color: rgb(248 250 252 / 75%);
}

.health-list {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.health-item {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.health-header {
  display: flex;
  justify-content: space-between;
  font-size: 13px;
  color: rgb(226 232 240 / 90%);
}

.health-title {
  display: inline-flex;
  align-items: baseline;
  gap: 6px;
  max-width: 100%;
}

.health-title .label {
  font-weight: 700;
}

.health-title .name {
  color: rgb(226 232 240 / 80%);
  font-size: 13px;
  line-height: 1.3;
  word-break: break-all;
}

.health-header .value {
  font-weight: 600;
}

.summary-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 18px;
}

.summary-card {
  display: flex;
  gap: 16px;
  padding: 18px;
  color: #fff;
  border-radius: 22px;
  box-shadow: 0 18px 35px rgb(15 23 42 / 25%);
}

.summary-card p {
  font-size: 15px;
  font-weight: 700;
}

.summary-card h3 {
  margin: 4px 0;
  font-size: 30px;
}

.summary-card .unit {
  margin-left: 6px;
  font-size: 14px;
}

.summary-card .sub {
  margin-left: 6px;
  font-size: 16px;
  opacity: 0.8;
}

.summary-icon {
  display: grid;
  place-items: center;
  width: 48px;
  height: 48px;
  background: rgb(255 255 255 / 20%);
  border-radius: 16px;
}

.card-trend {
  display: flex;
  gap: 6px;
  align-items: baseline;
}

.card-trend small {
  font-size: 12px;
  opacity: 0.7;
}

.trend-up {
  color: #4ade80;
}

.trend-down {
  color: #f87171;
}

.trend-flat {
  color: #e2e8f0;
}

.ck-charts-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: 10px;
}

.ck-card {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 18px;
  background:
    radial-gradient(circle at 20% 30%, rgb(139 92 246 / 35%), transparent 50%),
    radial-gradient(circle at 80% 10%, rgb(236 72 153 / 30%), transparent 55%),
    radial-gradient(circle at 50% 80%, rgb(59 130 246 / 25%), transparent 60%),
    linear-gradient(135deg, #0a0e1a, #1a1f35 40%, #0f172a 70%, #050a15), #0f172a;
  border: 1px solid rgb(148 163 184 / 20%);
  border-radius: 18px;
  box-shadow: 0 20px 40px rgb(15 23 42 / 25%);
}

.ck-card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
}

.ck-card-header h3 {
  margin: 4px 0 0;
  color: #e2e8f0;
}

.ck-card-header .eyebrow {
  margin: 0;
  color: rgb(148 163 184 / 85%);
  letter-spacing: 0.08em;
  font-size: 13px;
}

.chip {
  display: inline-flex;
  align-items: center;
  padding: 6px 10px;
  border-radius: 999px;
  background: rgb(99 102 241 / 15%);
  color: #a5b4fc;
  font-size: 12px;
  border: 1px solid rgb(99 102 241 / 35%);
}

.progress-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.progress-item {
  padding: 0;
}

.progress-meta {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
  color: #e2e8f0;
}

.meta-left {
  display: flex;
  gap: 10px;
  align-items: center;
  min-width: 0;
}

.env-tag {
  padding: 2px 8px;
  background: rgb(59 130 246 / 15%);
  color: #bfdbfe;
  border-radius: 999px;
  font-size: 11px;
  white-space: nowrap;
  max-width: 240px;
  overflow: hidden;
  text-overflow: ellipsis;
  font-weight: 600;
}

.alert-name {
  font-weight: 600;
  color: #e2e8f0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 300px;
  font-size: 13px;
}

.meta-right {
  display: flex;
  gap: 10px;
  align-items: center;
  color: #cbd5e1;
}

.meta-right .count {
  font-weight: 700;
}

.meta-right .ratio {
  color: #a78bfa;
  font-weight: 600;
}

.progress-bar {
  width: 100%;
  height: 10px;
  background: rgb(15 23 42 / 55%);
  border-radius: 12px;
  overflow: hidden;
  border: 1px solid rgb(148 163 184 / 18%);
}

.progress-fill {
  height: 100%;
  border-radius: 999px;
  transition: width 0.35s ease;
}

.chart-shell {
  width: 100%;
  height: clamp(320px, 32vw, 420px);
}

.legend {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.legend-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 0;
  border-bottom: 1px dashed rgb(148 163 184 / 15%);
}

.legend-item:last-child {
  border-bottom: none;
}

.legend .dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: linear-gradient(135deg, #60a5fa, #a78bfa);
  box-shadow: 0 0 0 4px rgb(96 165 250 / 20%);
}

.legend-text {
  flex: 1;
  min-width: 0;
  color: #e2e8f0;
}

.legend-title {
  margin: 0;
  font-weight: 600;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.legend-text small {
  color: #94a3b8;
}

.legend-value {
  color: #e2e8f0;
  font-weight: 700;
}

.empty {
  padding: 20px 0;
  text-align: center;
  color: #94a3b8;
}

@media (width <= 1280px) {
  .hero-main {
    flex-direction: column;
  }
}
</style>

<style lang="scss">
.env-select-dropdown {
  background: rgb(15 23 42 / 95%) !important;
  border: 1px solid rgb(148 163 184 / 40%) !important;

  .el-select-dropdown__item {
    color: #e2e8f0;

    &:hover {
      background: rgb(139 92 246 / 25%);
    }

    &.is-selected {
      color: #a78bfa;
      background: rgb(139 92 246 / 20%);
    }
  }
}
</style>
