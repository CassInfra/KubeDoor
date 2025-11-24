import { http } from "@/utils/http";

type ResultTable = {
  success: boolean;
  data?: Array<any>;
  meta?: Array<any>;
  pods?: Array<any>;
  count: any;
};

/**
 * 获取K8S环境列表
 */
export const getPromEnv = () => {
  return http.request<ResultTable>("get", "/api/prom_env");
};

/**
 * 获取命名空间列表
 * @param env K8S环境
 */
export const getPromNamespace = (env: string, flush?: boolean) => {
  return http.request<ResultTable>("get", "/api/agent/namespaces", {
    params: { env, flush }
  });
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
 */
export const getPodData = (
  env: string,
  namespace: string,
  deployment: string
) => {
  return http.request<ResultTable>("get", "/api/get_dpm_pods", {
    params: { env, namespace, deployment }
  });
};

export const updatePodCount = (data?: any) => {
  return http.request<any>("post", "/api/sql", {
    params: {
      add_http_cors_header: 1,
      default_format: "JSONCompact"
    },
    data: `ALTER TABLE __KUBEDOORDB__.k8s_res_control UPDATE pod_count_manual=${data.pod_count_manual} WHERE env = '${data.env}' AND namespace='${data.namespace}' AND deployment='${data.deployment_name}' `,
    headers: {
      "Content-Type": "text/plain;charset=UTF-8"
    }
  });
};
// 获取是否显示"已开启固定节点均衡模式"
export const showAddLabel = (env: string, namespace: string) => {
  return http.request<any>("post", "/api/sql", {
    params: {
      add_http_cors_header: 1,
      default_format: "JSONCompact"
    },
    data: `SELECT 1 FROM __KUBEDOORDB__.k8s_agent_status where env = '${env}' and admission = 1 and scheduler = 1 and admission_namespace like '%"${namespace}"%' `,
    headers: {
      "Content-Type": "text/plain;charset=UTF-8"
    }
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
 * 创建Pod日志流WebSocket连接URL
 * @param env K8S环境
 * @param namespace 命名空间
 * @param podName Pod名称
 * @returns WebSocket连接URL
 */
export const createPodLogStreamUrl = (
  env: string,
  namespace: string,
  podName: string
) => {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const host = window.location.host;
  return `${protocol}//${host}/ws/pod-logs?env=${env}&namespace=${namespace}&pod_name=${podName}`;
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
