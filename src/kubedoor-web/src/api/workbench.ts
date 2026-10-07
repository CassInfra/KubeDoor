import { http } from "@/utils/http";

type Result = {
  success: boolean;
  data: Array<any>;
  message?: string;
};

export const initByDays = (days: number, env: string) => {
  return http.request<Result>(
    "post",
    `/api/table?env=${env}`,
    { params: { days } },
    { timeout: 120000 }
  );
};

export const whSwitch = (action: string, env: string) => {
  return http.request<Result>("get", `/api/webhook_switch?env=${env}`, {
    params: { action }
  });
};

export const admisSwitch = (action: string, env: string) => {
  return http.request<Result>("get", `/api/admis_switch?env=${env}`, {
    params: { action }
  });
};

export const updateAdmission = (
  env: string,
  admission: boolean,
  admission_namespace: string
) => {
  return http.request<any>("post", "/api/db/agent/admission", {
    data: { env, admission, admission_namespace }
  });
};

type ResultTable = {
  success: boolean;
  data?: Array<any>;
  meta?: Array<any>;
  count: any;
};

/**
 * 获取Agent状态数据
 */
export const getAgentStatus = () => {
  return http.request<ResultTable>("get", "/api/agent_status");
};

/**
 * 初始化高峰期数据
 * @param env K8S环境
 * @param days 天数
 * @param peak_hours 高峰时段
 */
export const initPeakData = (
  env: string,
  days: number,
  peak_hours?: string
) => {
  const params: Record<string, any> = { env, days };
  if (peak_hours) {
    params.peak_hours = peak_hours;
  }
  return http.request<any>(
    "get",
    "/api/init_peak_data",
    { params },
    { timeout: 120000 }
  );
};

/**
 * 更新Agent采集状态
 * @param env K8S环境
 * @param collect 是否采集
 * @param peak_hours 高峰时段
 */
export const updateAgentCollect = (
  env: string,
  collect: boolean,
  peak_hours?: string
) => {
  return http.request<any>("post", "/api/db/agent/collect", {
    data: { env, collect, peak_hours: peak_hours ?? "" }
  });
};

export const updateNmsNotConfirm = (env: string, nmsNotConfirm: number) => {
  return http.request<any>("post", "/api/db/agent/nms_not_confirm", {
    data: { env, nms_not_confirm: nmsNotConfirm }
  });
};

export const updateScheduler = (env: string, scheduler: number) => {
  return http.request<any>("post", "/api/db/agent/scheduler", {
    data: { env, scheduler }
  });
};
