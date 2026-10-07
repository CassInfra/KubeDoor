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
  /** "1" 只看已屏蔽，"0" 只看未屏蔽，不传则不过滤 */
  silenced?: string;
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
  silenced?: string;
}

export const getEnv = () => {
  return http.request<ResultTable>("get", "/api/db/alert/envs");
};

export const getAlertName = () => {
  return http.request<ResultTable>("get", "/api/db/alert/names");
};

export const getAlarmTotal = (env?: string, startTime?: string) => {
  return http.request<ResultTable>("post", "/api/db/alert/total", {
    data: { env, startTime }
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
  silenced,
  page,
  pageSize
}: AlarmDetailParams) => {
  return http.request<ResultTable>("post", "/api/db/alert/detail", {
    data: {
      alertName,
      env,
      operate,
      status,
      severity,
      startTime,
      namespace,
      pod,
      silenced,
      page,
      pageSize
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
  pod,
  silenced
}: AlarmDetailTotalParams) => {
  return http.request<ResultTable>("post", "/api/db/alert/detail_total", {
    data: {
      alertName,
      env,
      operate,
      status,
      severity,
      startTime,
      namespace,
      pod,
      silenced
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
  return http.request<ResultTable>("post", "/api/db/alert/operate", {
    data: {
      operate: params.operate,
      fingerprint: params.fingerprint,
      start_time: params.start_time
    }
  });
};

export interface EventMenuParams {
  k8s: string;
  start_time: string;
  end_time: string;
  limit?: number;
  namespace?: string; // 可选参数
  level?: string;
  count?: number;
  message?: string;
}

// K8S事件菜单返回的数据结构（各字段为可选字符串数组）
export interface EventMenuData {
  namespace?: string[];
  kind?: string[];
  name?: string[];
  reason?: string[];
  reportingComponent?: string[];
  reportingInstance?: string[];
}

type EventMenuResult = {
  success: boolean;
  data?: EventMenuData;
  count?: any;
};

// 获取K8S事件菜单
export const getEventsMenu = (params: EventMenuParams) => {
  return http.request<EventMenuResult>("get", "/api/events/menu", {
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
