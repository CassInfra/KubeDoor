import { http } from "@/utils/http";

type ResultTable = {
  success: boolean;
  data?: any;
  message?: string;
  error?: string;
};

// 负载均衡配置类型
export interface LoadBalanceConfig {
  enabled: boolean;
  imbalance_threshold: number;
  balance_target: number;
  check_interval: number;
  min_replicas: number;
  peak_hours: { start: string; end: string }[];
  blacklist: string[];
  exclude_namespaces: string[];
  exclude_nodes: string[];
  auto_execute: boolean;
  // 节点选择策略
  source_strategy: "average" | "percentile";
  source_percentile: number;
  target_strategy: "average" | "percentile";
  target_percentile: number;
  // 迁移限制
  min_pod_cpu: number;
  max_iterations: number;
  batch_size: number; // 每批执行数量
}

// 迁移计划项类型
export interface MigrationItem {
  pod_name: string;
  namespace: string;
  deployment: string;
  source_node: string;
  target_node: string;
  cpu_used: number;
  expected_effect?: string;
  status?: "pending" | "executing" | "scheduled" | "ready" | "failed"; // 前端状态跟踪
  // 迁移时的模拟百分比
  source_percent?: number;
  target_percent?: number;
  source_percent_after?: number;
  target_percent_after?: number;
}

// 尝试的Pod详情
export interface TriedPod {
  pod: string;
  cpu: number;
  result: string;
  target?: string;
  effect?: string;
}

// 迭代日志类型
export interface IterationLog {
  iteration: number;
  sim_range: number;
  sim_avg: number;
  result: string | null;
  details: string | null;
  source_node?: string;
  tried_pods?: TriedPod[];
}

// 迁移计划类型
export interface MigrationPlan {
  migrations: MigrationItem[];
  current_range: number;
  expected_range?: number;
  current_std?: number;
  expected_std?: number;
  message?: string;
  timestamp?: string;
  iteration_logs?: IterationLog[];
  node_status?: {
    before: Record<string, number>;
    after: Record<string, number>;
  };
  // 节点CPU绝对值（单位：m）
  node_cpu_used?: {
    before: Record<string, number>;
    after: Record<string, number>;
  };
  config?: {
    imbalance_threshold: number;
    balance_target: number;
    min_replicas: number;
  };
}

// 隔离Pod类型
export interface IsolatedPod {
  name: string;
  namespace: string;
  node_name: string;
  selector_label: string;
  created_at: string;
  status: string;
  cpu_used?: number;
}

// 操作日志类型
export interface LoadBalanceLog {
  timestamp: string;
  action: string;
  message: string;
  details?: any;
}

// 状态类型
export interface LoadBalanceStatus {
  status: "idle" | "analyzing" | "executing";
  enabled: boolean;
  last_check_time: string | null;
  has_pending_plan: boolean;
  pending_migrations_count: number;
}

// 获取负载均衡配置
export const getLoadBalanceConfig = () => {
  return http.request<ResultTable>("get", "/api/load-balance/config");
};

// 更新负载均衡配置
export const updateLoadBalanceConfig = (config: Partial<LoadBalanceConfig>) => {
  return http.request<ResultTable>("put", "/api/load-balance/config", {
    data: config
  });
};

// 获取负载均衡状态
export const getLoadBalanceStatus = () => {
  return http.request<ResultTable>("get", "/api/load-balance/status");
};

// 获取待确认的迁移计划
export const getLoadBalancePlan = () => {
  return http.request<ResultTable>("get", "/api/load-balance/plan");
};

// 获取操作日志
export const getLoadBalanceLogs = (limit: number = 50) => {
  return http.request<ResultTable>("get", "/api/load-balance/logs", {
    params: { limit }
  });
};

// 触发负载分析
export const analyzeLoadBalance = (env: string) => {
  return http.request<ResultTable>(
    "post",
    "/api/load-balance/analyze",
    { params: { env } },
    { timeout: 120000 }
  );
};

// 执行迁移计划
export const executeLoadBalance = (
  env: string,
  migrations: MigrationItem[]
) => {
  return http.request<ResultTable>(
    "post",
    "/api/load-balance/execute",
    { params: { env }, data: { migrations } },
    { timeout: 300000 }
  );
};

// 获取隔离Pod列表
export const getIsolatedPods = (env: string) => {
  return http.request<ResultTable>(
    "get",
    "/api/load-balance/isolated",
    { params: { env } },
    { timeout: 120000 }
  );
};

// 清理隔离Pod
export const cleanupIsolatedPods = (
  env: string,
  pods: { name: string; namespace: string }[]
) => {
  return http.request<ResultTable>(
    "post",
    "/api/load-balance/cleanup",
    { params: { env }, data: { pods } },
    { timeout: 120000 }
  );
};

// 节点资源数据类型
export interface NodeCpuInfo {
  cpu_percent: number;
  cpu_used: number;
  cpu_cores: number;
  mem_percent: number;
  pod_count: number;
  unschedulable: boolean;
}

// 获取节点CPU使用率
export const getNodesCpu = (env: string) => {
  return http.request<ResultTable>(
    "get",
    "/api/load-balance/nodes-cpu",
    { params: { env } },
    { timeout: 120000 }
  );
};

// 获取namespace列表
export const getNamespaces = (env: string) => {
  return http.request<ResultTable>(
    "get",
    "/api/load-balance/namespaces",
    { params: { env } },
    { timeout: 120000 }
  );
};

// Pod状态检查结果类型
export interface PodStatusResult {
  namespace: string;
  pod_name: string;
  status: string;
  ready: boolean;
  error?: string;
}

// 检查Pod Ready状态
export const checkPodsStatus = (
  env: string,
  pods: { namespace: string; pod_name: string }[]
) => {
  return http.request<ResultTable>(
    "post",
    "/api/load-balance/check-pods",
    { params: { env }, data: { pods } },
    { timeout: 30000 }
  );
};
