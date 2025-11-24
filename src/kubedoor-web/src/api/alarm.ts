import { http } from "@/utils/http";

type ResultTable = {
  success: boolean;
  data?: Array<any>;
  meta?: Array<any>;
  count: any;
};

type ResultData = {
  success: boolean;
  message?: any;
};

interface AlarmDetailParams {
  alertName?: string[];
  env?: string[];
  operate?: string | undefined;
  status?: string[];
  severity?: string[];
  startTime?: string;
  namespace?: string;
  pod?: string;
  page: number;
  pageSize: number;
}

interface AlarmDetailTotalParams {
  alertName?: string[];
  env?: string[];
  operate?: string | undefined;
  status?: string[];
  severity?: string[];
  startTime?: string;
  namespace?: string;
  pod?: string;
}

export const getEnv = () => {
  return http.request<ResultTable>("post", "/api/sql", {
    params: {
      add_http_cors_header: 1,
      default_format: "JSONCompact"
    },
    data: "SELECT DISTINCT env from __KUBEDOORDB__.k8s_pod_alert_days order by env",
    headers: {
      "Content-Type": "text/plain;charset=UTF-8"
    }
  });
};

export const getAlertName = () => {
  return http.request<ResultTable>("post", "/api/sql", {
    params: {
      add_http_cors_header: 1,
      default_format: "JSONCompact"
    },
    data: "SELECT DISTINCT alert_name from __KUBEDOORDB__.k8s_pod_alert_days order by alert_name",
    headers: {
      "Content-Type": "text/plain;charset=UTF-8"
    }
  });
};

export const getAlarmTotal = (env?: string, startTime?: string) => {
  const conditions = [];
  if (env) {
    conditions.push(`env = '${env}'`);
  }
  if (startTime) {
    conditions.push(`start_time >= '${startTime}'`);
  }

  const whereClause =
    conditions.length > 0 ? `WHERE ${conditions.join(" AND ")}` : "";

  return http.request<ResultTable>("post", "/api/sql", {
    params: {
      add_http_cors_header: 1,
      default_format: "JSONCompact"
    },
    data: `
      SELECT 
        alert_name,
        COUNT(*) as total,
        COUNT(CASE WHEN alert_status = 'firing' THEN 1 END) as firing_count,
        COUNT(CASE WHEN alert_status = 'resolved' THEN 1 END) as resolved_count,
        any(severity) as severity
      FROM __KUBEDOORDB__.k8s_pod_alert_days 
      ${whereClause} 
      GROUP BY alert_name
      ORDER BY LENGTH(severity) DESC, firing_count DESC`,
    headers: {
      "Content-Type": "text/plain;charset=UTF-8"
    }
  });
};

export const getAlarmDetail = ({
  alertName,
  env,
  operate,
  status,
  severity,
  startTime,
  namespace,
  pod,
  page,
  pageSize
}: AlarmDetailParams) => {
  const conditions = [];

  if (alertName && alertName.length > 0) {
    conditions.push(`alert_name IN ('${alertName.join("','")}')`);
  }
  if (env && env.length > 0) {
    conditions.push(`env IN ('${env.join("','")}')`);
  }
  if (operate) {
    conditions.push(`operate = '${operate}'`);
  }
  if (status && status.length > 0) {
    conditions.push(`alert_status IN ('${status.join("','")}')`);
  }
  if (severity && severity.length > 0) {
    conditions.push(`severity IN ('${severity.join("','")}')`);
  }
  if (startTime) {
    conditions.push(`start_time >= '${startTime}'`);
  }
  if (namespace) {
    conditions.push(`namespace = '${namespace}'`);
  }
  if (pod) {
    conditions.push(`pod = '${pod}'`);
  }

  const whereClause =
    conditions.length > 0 ? `WHERE ${conditions.join(" AND ")}` : "";
  const offset = (page - 1) * pageSize;

  return http.request<ResultTable>("post", "/api/sql", {
    params: {
      add_http_cors_header: 1,
      default_format: "JSONCompact"
    },
    data: `
      SELECT 
        *
      FROM __KUBEDOORDB__.k8s_pod_alert_days 
      ${whereClause} 
      ORDER BY start_time DESC
      LIMIT ${pageSize}
      OFFSET ${offset}`,
    headers: {
      "Content-Type": "text/plain;charset=UTF-8"
    }
  });
};

export const getAlarmDetailTotal = ({
  alertName,
  env,
  operate,
  status,
  severity,
  startTime,
  namespace,
  pod
}: AlarmDetailTotalParams) => {
  const conditions = [];

  if (alertName && alertName.length > 0) {
    conditions.push(`alert_name IN ('${alertName.join("','")}')`);
  }
  if (env && env.length > 0) {
    conditions.push(`env IN ('${env.join("','")}')`);
  }
  if (operate) {
    conditions.push(`operate = '${operate}'`);
  }
  if (status && status.length > 0) {
    conditions.push(`alert_status IN ('${status.join("','")}')`);
  }
  if (severity && severity.length > 0) {
    conditions.push(`severity IN ('${severity.join("','")}')`);
  }
  if (startTime) {
    conditions.push(`start_time >= '${startTime}'`);
  }
  if (namespace) {
    conditions.push(`namespace = '${namespace}'`);
  }
  if (pod) {
    conditions.push(`pod = '${pod}'`);
  }

  const whereClause =
    conditions.length > 0 ? `WHERE ${conditions.join(" AND ")}` : "";

  return http.request<ResultTable>("post", "/api/sql", {
    params: {
      add_http_cors_header: 1,
      default_format: "JSONCompact"
    },
    data: `
      SELECT 
        COUNT(*) as total
      FROM __KUBEDOORDB__.k8s_pod_alert_days 
      ${whereClause}`,
    headers: {
      "Content-Type": "text/plain;charset=UTF-8"
    }
  });
};

interface PodParams {
  env: string;
  ns: string;
  pod_name: string;
  scale_pod?: boolean;
}

// Pod隔离接口
export const modifyPod = (params: PodParams, data?: any) => {
  return http.request<ResultData>("post", "/api/pod/modify_pod", {
    params,
    data
  });
};

// Pod删除接口
export const deletePod = (params: PodParams) => {
  return http.request<ResultData>("get", "/api/pod/delete_pod", { params });
};

// Pod自动dump接口
export const autoDump = (params: PodParams) => {
  return http.request<ResultData>(
    "get",
    "/api/pod/auto_dump",
    { params },
    { timeout: 120000 }
  );
};

// Pod jstack接口
export const autoJstack = (params: PodParams) => {
  return http.request<ResultData>(
    "get",
    "/api/pod/auto_jstack",
    { params },
    { timeout: 120000 }
  );
};

// Pod jfr接口
export const autoJfr = (params: PodParams) => {
  return http.request<ResultData>(
    "get",
    "/api/pod/auto_jfr",
    { params },
    { timeout: 120000 }
  );
};

// Pod JVM内存信息接口
export const autoJvmMem = (params: PodParams) => {
  return http.request<ResultData>(
    "get",
    "/api/pod/auto_jvm_mem",
    { params },
    { timeout: 120000 }
  );
};

interface UpdateOperateParams {
  operate: string;
  fingerprint: string;
  start_time: string;
}

// 修改operate状态
export const updateOperate = (params: UpdateOperateParams) => {
  return http.request<ResultTable>("post", "/api/sql", {
    params: {
      add_http_cors_header: 1,
      default_format: "JSONCompact"
    },
    data: `ALTER TABLE __KUBEDOORDB__.k8s_pod_alert_days 
           UPDATE operate = '${params.operate}'
           WHERE start_time = '${params.start_time}'
           AND fingerprint = '${params.fingerprint}'`,
    headers: {
      "Content-Type": "text/plain;charset=UTF-8"
    }
  });
};

export interface EventMenuParams {
  k8s: string;
  start_time: string;
  end_time: string;
  namespace?: string; // 可选参数
}

// 获取K8S事件菜单
export const getEventsMenu = (params: EventMenuParams) => {
  return http.request<ResultTable>("get", "/api/events/menu", {
    params
  });
};

export interface EventQueryParams {
  k8s: string;
  start_time: string;
  end_time: string;
  limit: number;
  namespace?: string;
  count?: number;
  level?: string;
  kind?: string;
  name?: string;
  reason?: string;
  reporting_component?: string;
  reporting_instance?: string;
  message?: string;
}

// 查询K8S事件
export const queryEvents = (params: EventQueryParams) => {
  // 过滤掉空值参数
  const filteredParams = Object.fromEntries(
    Object.entries(params).filter(
      ([_, value]) => value !== undefined && value !== null && value !== ""
    )
  );

  return http.request<ResultTable>("post", "/api/events/query", {
    data: filteredParams
  });
};
