import { http } from "@/utils/http";

export type ResultTable = {
  success?: boolean;
  data?: Array<any>;
  total?: number;
  message?: string;
  error?: string;
};

/**
 * 获取Service列表
 * @param env K8S环境
 * @param namespace 命名空间
 */
export const getServiceList = (env: string, namespace?: string) => {
  const params: Record<string, string> = { env };
  if (namespace) {
    params.namespace = namespace;
  }

  return http.request<ResultTable>("get", "/api/agent/services", {
    params
  });
};

/**
 * 获取ConfigMap列表
 * @param env K8S环境
 * @param namespace 命名空间
 */
export const getConfigMapList = (env: string, namespace?: string) => {
  const params: Record<string, string> = { env };
  if (namespace) {
    params.namespace = namespace;
  }

  return http.request<ResultTable>("get", "/api/agent/configmaps", {
    params
  });
};

/**
 * 获取指定Service内容
 * @param env K8S环境
 * @param namespace 命名空间
 * @param serviceName Service名称
 */
export const getServiceContent = (
  env: string,
  namespace: string,
  serviceName: string,
  resourceType: string
) => {
  return http.request<ResultTable>("get", "/api/agent/res/content", {
    params: {
      env,
      namespace,
      resource_name: serviceName,
      resource_type: resourceType
    }
  });
};

/**
 * 更新Service内容
 * @param env K8S环境
 * @param method 更新方式 (apply|replace)
 * @param yamlContent YAML内容
 */
export const updateServiceContent = (
  env: string,
  method: "apply" | "replace",
  yamlContent: string
) => {
  return http.request<ResultTable>("post", "/api/agent/res/ops", {
    params: {
      env,
      method
    },
    data: {
      yaml_content: yamlContent
    }
  });
};

/**
 * 获取Service的Endpoints信息
 * @param env K8S环境
 * @param namespace 命名空间
 * @param serviceName Service名称
 */
export const getServiceEndpoints = (
  env: string,
  namespace: string,
  serviceName: string
) => {
  return http.request<ResultTable>("get", "/api/agent/service/endpoints", {
    params: { env, namespace, service_name: serviceName }
  });
};

/**
 * 获取Service的第一个端口号
 * @param env K8S环境
 * @param namespace 命名空间
 * @param serviceName Service名称
 */
export const getServiceFirstPort = (
  env: string,
  namespace: string,
  serviceName: string
) => {
  return http.request<ResultTable>("get", "/api/agent/service/first-port", {
    params: { env, namespace, service_name: serviceName }
  });
};

/**
 * 删除K8S资源
 * @param env K8S环境
 * @param namespace 命名空间
 * @param resourceType 资源类型
 * @param resourceName 资源名称
 */
export const deleteK8sResource = (
  env: string,
  namespace: string,
  resourceType: string,
  resourceName: string
) => {
  return http.request<ResultTable>("delete", "/api/agent/res/delete", {
    params: {
      env,
      namespace,
      resource_type: resourceType,
      resource_name: resourceName
    }
  });
};
