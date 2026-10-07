import { http } from "@/utils/http";
import { getCached, setCache } from "@/utils/cache";

type ResultTable = {
  success: boolean;
  data?: Array<any>;
  meta?: Array<any>;
  pods?: Array<any>;
  message?: string;
  count: any;
};

/**
 * 获取K8S环境列表（缓存5分钟）
 */
export const getPromEnv = async () => {
  const cacheKey = "prom_env";
  const cached = getCached<ResultTable>(cacheKey);
  if (cached) return cached;

  const result = await http.request<ResultTable>("get", "/api/prom_env");
  if (result.success) setCache(cacheKey, result);
  return result;
};

/**
 * 获取命名空间列表（缓存5分钟，flush=true时跳过缓存）
 * @param env K8S环境
 */
export const getPromNamespace = async (env: string, flush?: boolean) => {
  const cacheKey = `namespaces_${env}`;
  if (!flush) {
    const cached = getCached<ResultTable>(cacheKey);
    if (cached) return cached;
  }

  const result = await http.request<ResultTable>(
    "get",
    "/api/agent/namespaces",
    {
      params: { env, flush }
    }
  );
  if (result.success) setCache(cacheKey, result);
  return result;
};

/**
 * 获取服务列表
 * @param env K8S环境
 * @param namespace 命名空间
 */
export const getPromServices = (env: string, namespace: string) => {
  return http.request<ResultTable>("get", "/api/prom_services", {
    params: { env, namespace }
  });
};

/**
 * 获取监控数据
 * @param env K8S环境
 * @param ns 命名空间（可选）
 */
export const getPromQueryData = (env: string, ns?: string) => {
  const params: Record<string, string> = { env };
  if (ns) {
    params.ns = ns;
  }
  return http.request<ResultTable>("get", "/api/prom_query", {
    params
  });
};

/**
 * 获取Pod数据
 * @param env K8S环境
 * @param namespace 命名空间
 * @param deployment 部署名称
 * @param options.silent 后台刷新:不显示进度条、出错不弹提示
 */
export const getPodData = (
  env: string,
  namespace: string,
  deployment: string,
  options?: { silent?: boolean }
) => {
  return http.request<ResultTable>(
    "get",
    "/api/get_dpm_pods",
    { params: { env, namespace, deployment } },
    { silent: options?.silent }
  );
};

export const updatePodCount = (data?: any) => {
  return http.request<any>("post", "/api/db/res/pod_count", { data });
};
// 获取是否显示"已开启固定节点均衡模式"
export const showAddLabel = (env: string, namespace: string) => {
  return http.request<any>("get", "/api/db/agent/show_add_label", {
    params: { env, namespace }
  });
};

/**
 * 获取Pod重启前日志
 * @param env K8S环境
 * @param namespace 命名空间
 * @param podName Pod名称
 * @param lines 日志行数（可选，默认100）
 */
export const getPodPreviousLogs = (
  env: string,
  namespace: string,
  podName: string,
  lines: number = 100
) => {
  return http.request<any>("get", "/api/pod/get_previous_logs", {
    params: { env, ns: namespace, pod: podName, lines }
  });
};

/**
 * 下载Pod完整日志（返回gzip压缩文件，超时5分钟）
 * @param env K8S环境
 * @param namespace 命名空间
 * @param podName Pod名称
 * @param container 容器名称（可选）
 */
export const downloadPodLogs = (
  env: string,
  namespace: string,
  podName: string,
  container?: string
) => {
  const params: Record<string, any> = { env, ns: namespace, pod: podName };
  if (container) {
    params.container = container;
  }
  return http.request<Blob>("get", "/api/pod/download_logs", {
    params,
    timeout: 300000,
    responseType: "blob"
  });
};

/**
 * 创建Pod日志流WebSocket连接URL
 * @param env K8S环境
 * @param namespace 命名空间
 * @param podName Pod名称
 * @param container 容器名称（可选）
 * @returns WebSocket连接URL
 */
export const createPodLogStreamUrl = (
  env: string,
  namespace: string,
  podName: string,
  container?: string
) => {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const host = window.location.host;
  let url = `${protocol}//${host}/ws/pod-logs?env=${env}&namespace=${namespace}&pod_name=${podName}`;
  if (container) {
    url += `&container=${container}`;
  }
  return url;
};

/**
 * Deployment/Pod 实时状态推送的 WebSocket 地址
 * @param env K8S环境
 * @param namespace 命名空间,空表示全部
 */
export const createWorkloadStreamUrl = (env: string, namespace: string) => {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const query = `env=${encodeURIComponent(env)}&namespace=${encodeURIComponent(namespace)}`;
  return `${protocol}//${window.location.host}/ws/workload-status?${query}`;
};

/**
 * 获取镜像标签列表
 * @param k8s K8S环境
 * @param namespace 命名空间
 * @param deployment 部署名称
 */
export const getImageTags = (
  k8s: string,
  namespace: string,
  deployment: string
) => {
  return http.request<any>("post", "/api/image/tags", {
    data: { k8s, namespace, deployment }
  });
};

/**
 * 获取节点资源排名
 * @param env K8S环境
 * @param type 资源类型
 * @param namespace 命名空间
 * @param deployment 微服务名称
 */
export const getNodeResourceRank = (
  env: string,
  type: string,
  namespace?: string,
  deployment?: string
) => {
  const params: any = { env, type };
  if (namespace) params.namespace = namespace;
  if (deployment) params.deployment = deployment;

  return http.request<ResultTable>("get", "/api/prom_node_rank", {
    params
  });
};

/**
 * 获取CCI ScheduleProfile信息
 * @param env K8S环境
 * @param namespace 命名空间
 * @param deployment 微服务名称
 */
export const getCciScheduleProfile = (
  env: string,
  namespace: string,
  deployment: string
) => {
  return http.request<{ exists: boolean; maxNum: number }>(
    "get",
    "/api/cci/schedule-profile",
    {
      params: { env, namespace, deployment }
    }
  );
};
