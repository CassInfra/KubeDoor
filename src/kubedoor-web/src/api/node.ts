import { http } from "@/utils/http";

type ResultTable = {
  success: boolean;
  data?: any;
  message?: string;
};

// 获取节点列表
export const getNodesList = (env: string) => {
  return http.request<ResultTable>("get", "/api/nodes/list", {
    params: {
      env: env
    }
  });
};

// 禁止节点调度（cordon）
export const cordonNodes = (env: string, nodeNames: string[]) => {
  return http.request<ResultTable>("post", "/api/nodes/cordon", {
    params: {
      env: env
    },
    data: {
      node_names: nodeNames
    }
  });
};

// 允许节点调度（uncordon）
export const uncordonNodes = (env: string, nodeNames: string[]) => {
  return http.request<ResultTable>("post", "/api/nodes/uncordon", {
    params: {
      env: env
    },
    data: {
      node_names: nodeNames
    }
  });
};
