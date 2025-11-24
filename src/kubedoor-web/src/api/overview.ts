import dayjs from "dayjs";
import { http } from "@/utils/http";

export interface OverviewCard {
  key: string;
  title: string;
  value: number;
  unit?: string;
  subValue?: string;
  trend?: number;
  trendLabel?: string;
  icon: string;
  gradient: [string, string];
}

export interface HeroPulse {
  clustersOnline: number;
  clustersTotal: number;
  nodesOnline: number;
  nodesTotal: number;
  workloads: number;
  pods: number;
  controlPlaneLatency: number;
  energySaving: number;
  requestsPerMinute: number;
  focus: Array<{
    label: string;
    value: string;
    status: "success" | "warning" | "danger";
  }>;
}

export interface HealthIndicator {
  label: string;
  value: number;
  unit?: string;
  target?: number;
}

export interface SaturationBadge {
  label: string;
  value: number;
  unit?: string;
  desc: string;
  status: "success" | "warning" | "danger";
}

export interface TrendSeries {
  timestamps: string[];
  cpu: number[];
  memory: number[];
  pods: number[];
  network: number[];
}

export interface TopNamespaceUsage {
  name: string;
  owner: string;
  cpu: number;
  memory: number;
  pods: number;
  trend: number;
}

export interface TopPodHotspot {
  name: string;
  namespace: string;
  restarts: number;
  qos: string;
  latency: number;
  status: "healthy" | "warning" | "danger";
}

export interface TopNodePressure {
  name: string;
  location: string;
  saturation: number;
  gpuUsage: number;
  workloads: number;
}

export interface TimelineEvent {
  id: string;
  title: string;
  detail: string;
  time: string;
  level: "primary" | "success" | "warning" | "danger";
}

export type MaxMetricTuple = [string, number];

interface SnapshotPayload {
  hero: HeroPulse;
  cards: OverviewCard[];
  health: HealthIndicator[];
  saturation: SaturationBadge[];
}

interface TrendsPayload {
  usage: TrendSeries;
}

interface TopPayload {
  namespaces: TopNamespaceUsage[];
  pods: TopPodHotspot[];
  nodes: TopNodePressure[];
}

interface TimelinePayload {
  items: TimelineEvent[];
}

export interface MockResponse<T> {
  success: boolean;
  data: T;
  timestamp: string;
}

export interface ApiResponse<T> {
  success: boolean;
  data: T;
  message?: string;
  timestamp?: string;
}

export interface PromOverviewMetrics {
  clusters: number;
  nodes: number;
  workloads: number;
  pods: number;
  pvcs: number;
  total_cpu_cores: number;
  total_mem_bytes: number;
  used_cpu_cores: number;
  used_mem_bytes: number;
  net_in_bytes_per_sec: number;
  net_out_bytes_per_sec: number;
  nodes_cpu_gt_70: number;
  nodes_mem_gt_80: number;
  nodes_pod_gt_90: number;
  pvcs_usage_gt_80: number;
  cpu_usage_percent: number;
  mem_usage_percent: number;
  max_cpu_usage_percent: MaxMetricTuple;
  max_mem_usage_percent: MaxMetricTuple;
  max_cpu_request_percent: MaxMetricTuple;
  max_mem_request_percent: MaxMetricTuple;
  max_pod_usage_percent: MaxMetricTuple;
  max_pvc_usage_percent: MaxMetricTuple;
}

export interface PromOverviewResponse {
  success: boolean;
  data: PromOverviewMetrics;
  timestamp?: string;
}

export interface CkPodAlertItem {
  env: string;
  namespace: string;
  alert_name: string;
  pod: string;
  description: string;
  count_firing: number;
  ratio_percent: number;
}

export interface CkEventItem {
  env: string;
  namespace: string;
  reason: string;
  name: string;
  message: string;
  count: number;
  ratio_percent: number;
}

export interface CkAlertDailyItem {
  day: string;
  day_label: string;
  daily_alert_count: number;
}

export const getPromOverview = (env?: string) => {
  return http.request<PromOverviewResponse>("get", "/api/prom_overview", {
    params: env ? { env } : undefined
  });
};

export const getCkTopPodAlerts = (env?: string) => {
  return http.request<ApiResponse<CkPodAlertItem[]>>(
    "get",
    "/api/ck_top10_pod_alerts",
    { params: env ? { env } : undefined }
  );
};

export const getCkTopEvents = (env?: string) => {
  return http.request<ApiResponse<CkEventItem[]>>(
    "get",
    "/api/ck_top10_events",
    { params: env ? { env } : undefined }
  );
};

export const getCkAlertDaily = (env?: string) => {
  return http.request<ApiResponse<CkAlertDailyItem[]>>(
    "get",
    "/api/ck_day10_alert_daily",
    { params: env ? { env } : undefined }
  );
};

const delay = (ms: number) => new Promise(resolve => setTimeout(resolve, ms));

const mockRequest = async <T>(data: T): Promise<MockResponse<T>> => {
  await delay(260 + Math.random() * 240);
  return {
    success: true,
    data,
    timestamp: dayjs().toISOString()
  };
};

const gradients: Array<[string, string]> = [
  ["#48c6ef", "#6f86d6"],
  ["#fccb90", "#d57eeb"],
  ["#1EAE98", "#38ef7d"],
  ["#fa709a", "#fee140"],
  ["#209cff", "#68e0cf"],
  ["#8e2de2", "#4a00e0"],
  ["#f83600", "#f9d423"],
  ["#3C8CE7", "#00EAFF"]
];

export async function getK8sOverviewSnapshot(): Promise<
  MockResponse<SnapshotPayload>
> {
  const hero: HeroPulse = {
    clustersOnline: 7,
    clustersTotal: 8,
    nodesOnline: 531,
    nodesTotal: 544,
    workloads: 1863,
    pods: 18234,
    controlPlaneLatency: 42,
    energySaving: 18,
    requestsPerMinute: 1.27,
    focus: [
      { label: "升级窗口", value: "今晚 23:00", status: "warning" },
      { label: "灰度分批", value: "3 / 5", status: "success" },
      { label: "GPU调度", value: "24 节点紧张", status: "danger" }
    ]
  };

  const cards: OverviewCard[] = [
    {
      key: "clusters",
      title: "集群运行中",
      value: hero.clustersOnline,
      subValue: `/${hero.clustersTotal}`,
      trend: 2.1,
      trendLabel: "较昨日",
      unit: "个",
      icon: "mdi:cloud-check",
      gradient: gradients[0]
    },
    {
      key: "nodes",
      title: "节点在线",
      value: hero.nodesOnline,
      subValue: `/${hero.nodesTotal}`,
      trend: 1.8,
      trendLabel: "vs 30min",
      unit: "台",
      icon: "mdi:wan",
      gradient: gradients[1]
    },
    {
      key: "pods",
      title: "运行Pod",
      value: hero.pods,
      unit: "个",
      trend: 3.4,
      trendLabel: "环比",
      icon: "mdi:cube-outline",
      gradient: gradients[2]
    },
    {
      key: "workloads",
      title: "活跃工作负载",
      value: hero.workloads,
      unit: "个",
      trend: -1.2,
      trendLabel: "vs 上一窗口",
      icon: "mdi:layers-triple",
      gradient: gradients[3]
    },
    {
      key: "rpm",
      title: "入站请求",
      value: hero.requestsPerMinute,
      unit: "M rpm",
      trend: 5.3,
      trendLabel: "15min",
      icon: "mdi:chart-line",
      gradient: gradients[4]
    },
    {
      key: "latency",
      title: "控制面延迟",
      value: hero.controlPlaneLatency,
      unit: "ms",
      trend: -6.2,
      trendLabel: "vs 5min",
      icon: "mdi:speedometer",
      gradient: gradients[5]
    }
  ];

  const health: HealthIndicator[] = [
    { label: "CPU使用率", value: 67, unit: "%", target: 80 },
    { label: "内存使用率", value: 59, unit: "%", target: 78 },
    { label: "存储占用", value: 71, unit: "%", target: 85 },
    { label: "网络吞吐", value: 35, unit: "Gbps", target: 56 }
  ];

  const saturation: SaturationBadge[] = [
    {
      label: "高压命名空间",
      value: 5,
      unit: "个",
      desc: "资源饱和 > 85%",
      status: "danger"
    },
    {
      label: "受限节点",
      value: 12,
      unit: "台",
      desc: "CPU超卖",
      status: "warning"
    },
    {
      label: "待处理告警",
      value: 18,
      unit: "条",
      desc: "近30分钟",
      status: "warning"
    },
    {
      label: "节能调度",
      value: 42,
      unit: "%",
      desc: "夜间窗口",
      status: "success"
    }
  ];

  return mockRequest({ hero, cards, health, saturation });
}

export async function getK8sOverviewTrends(): Promise<
  MockResponse<TrendsPayload>
> {
  const points = 12;
  const timestamps = Array.from({ length: points }).map((_, idx) =>
    dayjs()
      .subtract(points - idx, "minute")
      .format("HH:mm")
  );

  const wave = (base: number, amplitude: number) =>
    timestamps.map((_, idx) =>
      Math.round(
        base + Math.sin(idx / 2) * amplitude + (Math.random() - 0.5) * amplitude
      )
    );

  const usage: TrendSeries = {
    timestamps,
    cpu: wave(65, 8),
    memory: wave(58, 6),
    pods: wave(72, 5),
    network: wave(32, 6)
  };

  return mockRequest({ usage });
}

export async function getK8sTopResources(): Promise<MockResponse<TopPayload>> {
  const namespaces: TopNamespaceUsage[] = [
    {
      name: "payment-core",
      owner: "财务域",
      cpu: 78,
      memory: 64,
      pods: 462,
      trend: 4.2
    },
    {
      name: "recommendation",
      owner: "体验域",
      cpu: 72,
      memory: 59,
      pods: 381,
      trend: 1.8
    },
    {
      name: "livestream",
      owner: "互动域",
      cpu: 68,
      memory: 74,
      pods: 256,
      trend: -0.6
    },
    {
      name: "edge-gateway",
      owner: "边缘域",
      cpu: 63,
      memory: 57,
      pods: 198,
      trend: 2.7
    },
    {
      name: "risk-control",
      owner: "风控域",
      cpu: 55,
      memory: 48,
      pods: 166,
      trend: -1.2
    }
  ];

  const pods: TopPodHotspot[] = [
    {
      name: "checkout-57cd8c",
      namespace: "payment-core",
      restarts: 3,
      qos: "Burstable",
      latency: 185,
      status: "warning"
    },
    {
      name: "media-svc-1078",
      namespace: "livestream",
      restarts: 0,
      qos: "Guaranteed",
      latency: 92,
      status: "healthy"
    },
    {
      name: "risk-eval-33ff",
      namespace: "risk-control",
      restarts: 6,
      qos: "Burstable",
      latency: 241,
      status: "danger"
    },
    {
      name: "edge-proxy-8a11",
      namespace: "edge-gateway",
      restarts: 1,
      qos: "Guaranteed",
      latency: 123,
      status: "healthy"
    },
    {
      name: "recommend-9bd21",
      namespace: "recommendation",
      restarts: 2,
      qos: "Burstable",
      latency: 141,
      status: "warning"
    }
  ];

  const nodes: TopNodePressure[] = [
    {
      name: "cn-prod-gpu-17",
      location: "上海可用区A",
      saturation: 86,
      gpuUsage: 92,
      workloads: 63
    },
    {
      name: "cn-prod-gpu-23",
      location: "上海可用区B",
      saturation: 82,
      gpuUsage: 88,
      workloads: 58
    },
    {
      name: "bj-prod-std-11",
      location: "北京可用区C",
      saturation: 79,
      gpuUsage: 54,
      workloads: 71
    },
    {
      name: "hz-prod-std-05",
      location: "杭州可用区A",
      saturation: 77,
      gpuUsage: 43,
      workloads: 65
    },
    {
      name: "sz-prod-std-14",
      location: "深圳可用区B",
      saturation: 74,
      gpuUsage: 35,
      workloads: 59
    }
  ];

  return mockRequest({ namespaces, pods, nodes });
}

export async function getK8sEventTimeline(): Promise<
  MockResponse<TimelinePayload>
> {
  const now = dayjs();
  const items: TimelineEvent[] = [
    {
      id: "evt-1",
      title: "risk-control 命名空间扩容完成",
      detail: "新增 24 个 pod，命中自动调度策略",
      time: now.subtract(4, "minute").format("HH:mm:ss"),
      level: "success"
    },
    {
      id: "evt-2",
      title: "checkout 服务 QPS 激增",
      detail: "支付链路触发金丝雀发布第 2 批次",
      time: now.subtract(7, "minute").format("HH:mm:ss"),
      level: "warning"
    },
    {
      id: "evt-3",
      title: "GPU 调度窗口开启",
      detail: "可执行离线训练作业 1.2 小时",
      time: now.subtract(12, "minute").format("HH:mm:ss"),
      level: "primary"
    },
    {
      id: "evt-4",
      title: "edge-gateway 热点节点降温",
      detail: "启用跨可用区流量打散策略",
      time: now.subtract(18, "minute").format("HH:mm:ss"),
      level: "success"
    },
    {
      id: "evt-5",
      title: "livestream 质量告警解除",
      detail: "链路延迟恢复至 95 ms",
      time: now.subtract(26, "minute").format("HH:mm:ss"),
      level: "primary"
    }
  ];

  return mockRequest({ items });
}
