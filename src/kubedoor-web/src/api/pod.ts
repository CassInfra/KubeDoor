import { http } from "@/utils/http";

export interface PodContainerStatus {
  name: string;
  state: string;
  ready: boolean;
  restart_count: number;
  reason: string;
  message: string;
  is_init?: boolean;
  image?: string;
  image_id?: string;
  started?: boolean | null;
  state_detail?: Record<string, any> | null;
  last_state_detail?: Record<string, any> | null;
}

export interface PodItem {
  namespace: string;
  name: string;
  containers: PodContainerStatus[];
  restart_count: number;
  controlled_by: string;
  pod_ip: string | null;
  creation_timestamp: string | null;
  node_name: string;
  status: string;
  status_reason?: string;
  status_message?: string;
  current_cpu_cores: number | null;
  current_memory_mb: number | null;
}

export interface PodListResponse {
  success?: boolean;
  data?: PodItem[];
  total?: number;
  message?: string;
  error?: string;
}

export interface PodDeleteItem {
  pod_name: string;
  ns: string;
}

export interface DeletePodsPayload {
  pods: PodDeleteItem[];
}

export interface DeletePodsResponse {
  success?: boolean;
  message?: string;
  error?: string;
}

export const getPodList = (env: string, namespace?: string) => {
  const params: Record<string, string> = {};
  if (env) {
    params.env = env;
  }
  if (namespace) {
    params.namespace = namespace;
  }
  return http.request<PodListResponse>("get", "/api/agent/pods", {
    params
  });
};

export const deletePodsBatch = (env: string, payload: DeletePodsPayload) => {
  const params: Record<string, string> = {};
  if (env) {
    params.env = env;
  }
  return http.request<DeletePodsResponse>("delete", "/api/pod/delete_pods", {
    params,
    data: payload
  });
};
