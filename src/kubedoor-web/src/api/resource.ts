import { http } from "@/utils/http";

type ResultTable = {
  success: boolean;
  data?: Array<any>;
  meta?: Array<any>;
  count: any;
};

export const getMaxDay = (env: string) => {
  return http.request<ResultTable>("get", "/api/db/res/max_day", {
    params: { env }
  });
};

export const getEnv = () => {
  return http.request<ResultTable>("get", "/api/db/res/envs");
};

export const getNamespace = (env: string) => {
  return http.request<ResultTable>("get", "/api/db/res/namespaces", {
    params: { env }
  });
};

export const getDeployment = (env: string, namespace: string) => {
  return http.request<ResultTable>("get", "/api/db/res/deployments", {
    params: { env, namespace }
  });
};

/** 获取系统管理-用户管理列表 */
export const getResourceList = (query: any) => {
  return http.request<ResultTable>("get", "/api/db/res/list", {
    params: {
      env: query.env,
      namespace: query.namespace,
      deployment: query.deployment,
      keyword: query.keyword
    }
  });
};

export const addData = (data?: any) => {
  return http.request<ResultTable>("post", "/api/db/res/add", { data });
};

export const editData = (data?: any) => {
  return http.request<ResultTable>("post", "/api/db/res/edit", { data });
};

export const getCollection = (date: string, env: string) => {
  return http.request<ResultTable>("get", "/api/db/res/collection", {
    params: { date, env }
  });
};

// export const kunlunCapacity = (data: any[], interval?: number) => {
//   return http.request<ResultTable>("post", "/api/kunlun/scale", {
//     params: interval ? { interval } : undefined,
//     data
//   });
// };

// export const penglaiCapacity = (data: any[], interval?: number) => {
//   return http.request<ResultTable>("post", "/api/penglai/scale", {
//     params: interval ? { interval } : undefined,
//     data
//   });
// };

export const execCapacity = (
  env: string,
  addLabel: boolean,
  data: any,
  interval?: number,
  temp?: boolean,
  strategy?: string,
  scheduler?: boolean,
  cci?: boolean
) => {
  let url = `/api/scale?env=${env}${addLabel ? "&add_label=true" : ""}`;
  if (temp) {
    url += "&temp=true";
  }
  if (strategy && addLabel) {
    url += `&type=${strategy}`;
  }
  if (scheduler) {
    url += "&scheduler=true";
  }
  if (cci) {
    url += "&cci=true";
  }
  return http.request<ResultTable>("post", url, {
    params: interval ? { interval } : undefined,
    data
  });
};

export const execTimeCron = (
  env: string,
  addLabel: boolean,
  data: any,
  temp?: boolean,
  strategy?: string,
  scheduler?: boolean,
  cci?: boolean
) => {
  let url = `/api/cron?env=${env}${addLabel ? "&add_label=true" : ""}`;
  if (temp) {
    url += "&temp=true";
  }
  if (strategy && addLabel) {
    url += `&type=${strategy}`;
  }
  if (scheduler) {
    url += "&scheduler=true";
  }
  if (cci) {
    url += "&cci=true";
  }
  return http.request<ResultTable>("post", url, {
    data
  });
};

export const rebootResource = (
  env: string,
  data: any,
  interval?: number,
  scheduler?: boolean,
  selectedNodes?: string[]
) => {
  let url = `/api/restart?env=${env}`;
  if (scheduler) {
    url += "&scheduler=true";
  }

  // 当启用调度器时，修改数据格式
  let requestData;
  if (scheduler && selectedNodes && selectedNodes.length > 0) {
    requestData = {
      deployment_list: data,
      node_scheduler: selectedNodes
    };
  } else {
    requestData = {
      deployment_list: data,
      node_scheduler: []
    };
  }

  return http.request<ResultTable>("post", url, {
    params: interval ? { interval } : undefined,
    data: requestData
  });
};

// export const kunlunRebootResource = (data: any[], interval?: number) => {
//   return http.request<ResultTable>("post", "/api/kunlun/restart", {
//     params: interval ? { interval } : undefined,
//     data
//   });
// };

// export const penglaiRebootResource = (data: any[], interval?: number) => {
//   return http.request<ResultTable>("post", "/api/penglai/restart", {
//     params: interval ? { interval } : undefined,
//     data
//   });
// };

export const updateImage = (env: string, data: any) => {
  return http.request<ResultTable>("post", `/api/update-image?env=${env}`, {
    data
  });
};
