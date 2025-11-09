import { http } from "@/utils/http";

export interface StatefulSetItem {
  env: string;
  namespace: string;
  name: string;
  desired_pods: number;
  ready_pods: number;
  cpu_requests: string;
  cpu_limits: string;
  memory_requests: string;
  memory_limits: string;
  creation_timestamp?: string;
}

export interface DaemonSetItem {
  env: string;
  namespace: string;
  name: string;
  desired_pods: number;
  current_pods: number;
  ready_pods: number;
  cpu_requests: string;
  cpu_limits: string;
  memory_requests: string;
  memory_limits: string;
  creation_timestamp?: string;
}

interface ListResponse<T> {
  success: boolean;
  data?: T[];
  total?: number;
  message?: string;
}

interface PodListResponse {
  success: boolean;
  pods?: any[];
  message?: string;
}

export const getStatefulSetList = (env: string, namespace?: string) => {
  const params: Record<string, string> = { env };
  if (namespace) params.namespace = namespace;
  return http.request<ListResponse<StatefulSetItem>>(
    "get",
    "/api/agent/statefulsets",
    {
      params
    }
  );
};

export const getStatefulSetPods = (
  env: string,
  namespace: string,
  statefulset: string
) => {
  return http.request<PodListResponse>("get", "/api/agent/statefulset/pods", {
    params: { env, namespace, statefulset }
  });
};

export const restartStatefulSet = (
  env: string,
  namespace: string,
  statefulset: string
) => {
  return http.request<{ success: boolean; message?: string }>(
    "post",
    "/api/agent/statefulset/restart",
    {
      params: { env },
      data: { namespace, statefulset }
    }
  );
};

export const scaleStatefulSet = (
  env: string,
  namespace: string,
  statefulset: string,
  replicas: number
) => {
  return http.request<{ success: boolean; message?: string }>(
    "post",
    "/api/agent/statefulset/scale",
    {
      params: { env },
      data: { namespace, statefulset, replicas }
    }
  );
};

export const getDaemonSetList = (env: string, namespace?: string) => {
  const params: Record<string, string> = { env };
  if (namespace) params.namespace = namespace;
  return http.request<ListResponse<DaemonSetItem>>(
    "get",
    "/api/agent/daemonsets",
    {
      params
    }
  );
};

export const getDaemonSetPods = (
  env: string,
  namespace: string,
  daemonset: string
) => {
  return http.request<PodListResponse>("get", "/api/agent/daemonset/pods", {
    params: { env, namespace, daemonset }
  });
};

export const restartDaemonSet = (
  env: string,
  namespace: string,
  daemonset: string
) => {
  return http.request<{ success: boolean; message?: string }>(
    "post",
    "/api/agent/daemonset/restart",
    {
      params: { env },
      data: { namespace, daemonset }
    }
  );
};
