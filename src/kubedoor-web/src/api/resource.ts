import { http } from "@/utils/http";

type ResultTable = {
  success: boolean;
  data?: Array<any>;
  meta?: Array<any>;
  count: any;
};

export const getMaxDay = (env: string) => {
  return http.request<ResultTable>("post", "/api/sql", {
    params: {
      add_http_cors_header: 1,
      default_format: "JSONCompact"
    },
    data: `select formatDateTime(toDateTime(update), '%Y-%m-%d')  from __KUBEDOORDB__.k8s_res_control where env='${env}' group by update order by count() desc limit 1`,
    headers: {
      "Content-Type": "text/plain;charset=UTF-8"
    }
  });
};

export const getEnv = () => {
  return http.request<ResultTable>("post", "/api/sql", {
    params: {
      add_http_cors_header: 1,
      default_format: "JSONCompact"
    },
    data: "SELECT DISTINCT env from __KUBEDOORDB__.k8s_res_control order by env",
    headers: {
      "Content-Type": "text/plain;charset=UTF-8"
    }
  });
};

export const getNamespace = (env: string) => {
  return http.request<ResultTable>("post", "/api/sql", {
    params: {
      add_http_cors_header: 1,
      default_format: "JSONCompact"
    },
    data: `SELECT DISTINCT namespace from __KUBEDOORDB__.k8s_res_control where env='${env}' order by namespace`,
    headers: {
      "Content-Type": "text/plain;charset=UTF-8"
    }
  });
};

export const getDeployment = (env: string, namespace: string) => {
  return http.request<ResultTable>("post", "/api/sql", {
    params: {
      add_http_cors_header: 1,
      default_format: "JSONCompact"
    },
    data: `SELECT DISTINCT deployment from __KUBEDOORDB__.k8s_res_control where env = '${env}' and namespace = '${namespace}' order by deployment`,
    headers: {
      "Content-Type": "text/plain;charset=UTF-8"
    }
  });
};

/** 获取系统管理-用户管理列表 */
export const getResourceList = (query: any) => {
  let sql = "select * from __KUBEDOORDB__.k8s_res_control";
  const conditions = [];

  if (query.namespace) {
    conditions.push(`namespace = '${query.namespace}'`);
  }
  if (query.deployment) {
    conditions.push(`deployment = '${query.deployment}'`);
  }
  if (query.env) {
    conditions.push(`env = '${query.env}'`);
  }
  if (query.keyword) {
    conditions.push(
      `(deployment LIKE '%${query.keyword}%' or namespace LIKE '%${query.keyword}%')`
    );
  }
  if (conditions.length > 0) {
    sql += " where " + conditions.join(" and ");
  }
  return http.request<ResultTable>("post", "/api/sql", {
    params: {
      add_http_cors_header: 1,
      default_format: "JSONCompact"
    },
    data: sql,
    headers: {
      "Content-Type": "text/plain;charset=UTF-8"
    }
  });
};

export const addData = (data?: any) => {
  let dataStr = `'${data.env}','${data.namespace}', '${data.deployment}', ${data.pod_count_manual}, ${data.limit_cpu_m}, ${data.limit_mem_mb}, ${data.request_cpu_m}, ${data.request_mem_mb}`;
  return http.request<ResultTable>("post", "/api/sql", {
    params: {
      add_http_cors_header: 1,
      default_format: "JSONCompact"
    },
    data: `INSERT INTO __KUBEDOORDB__.k8s_res_control (env,namespace,deployment,pod_count_manual,limit_cpu_m,limit_mem_mb,request_cpu_m,request_mem_mb) VALUES (${dataStr})`,
    headers: {
      "Content-Type": "text/plain;charset=UTF-8"
    }
  });
};

export const editData = (data?: any) => {
  return http.request<ResultTable>("post", "/api/sql", {
    params: {
      add_http_cors_header: 1,
      default_format: "JSONCompact"
    },
    data: `ALTER TABLE __KUBEDOORDB__.k8s_res_control UPDATE limit_mem_mb=${data.limit_mem_mb},limit_cpu_m=${data.limit_cpu_m},pod_count_manual=${data.pod_count_manual} WHERE env = '${data.env}' AND namespace='${data.namespace}' AND deployment='${data.deployment}' `,
    headers: {
      "Content-Type": "text/plain;charset=UTF-8"
    }
  });
};

export const getCollection = (date: string, env: string) => {
  return http.request<ResultTable>("post", "/api/sql", {
    params: {
      add_http_cors_header: 1,
      default_format: "JSONCompact"
    },
    data: `SELECT * FROM __KUBEDOORDB__.k8s_resources WHERE date >= '${date} 00:00:00' and date <= '${date} 23:59:59' and env='${env}'`,
    headers: {
      "Content-Type": "text/plain;charset=UTF-8"
    }
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
  scheduler?: boolean
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
  scheduler?: boolean
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
  return http.request<ResultTable>("post", url, {
    data
  });
};

export const rebootResource = (
  env: string,
  data: any[],
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
    requestData = data;
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
