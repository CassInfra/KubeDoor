import { http } from "@/utils/http";

export type ResultTable = {
  success?: boolean;
  data?: Array<any> | Record<string, any>;
  total?: number;
  message?: string;
  error?: string;
};

/**
 * 获取Ingress列表
 */
export const getIngressList = (env: string, namespace?: string) => {
  const params: Record<string, string> = { env };
  if (namespace) {
    params.namespace = namespace;
  }

  return http.request<ResultTable>("get", "/api/agent/ingresses", {
    params
  });
};

/**
 * 获取指定Ingress的规则
 */
export const getIngressRules = (
  env: string,
  namespace: string,
  ingressName: string
) => {
  return http.request<ResultTable>("get", "/api/agent/ingress/rules", {
    params: { env, namespace, ingress_name: ingressName }
  });
};
