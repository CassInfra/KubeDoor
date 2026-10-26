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

export type AgentAliasesResult = {
  success: boolean;
  msg?: string;
  /** 实际保存的别名(去掉了重复的和与集群名相同的) */
  aliases: string[];
};

/**
 * 更新集群别名(整体替换),AI 助手按别名识别用户说的是哪个集群
 * @param env K8S环境
 * @param aliases 别名列表
 */
export const updateAgentAliases = (env: string, aliases: string[]) => {
  return http.request<AgentAliasesResult>("post", "/api/db/agent/aliases", {
    data: { env, aliases }
  });
};

/** Agent 自己的 Deployment:install.sh 固定装在 kubedoor 命名空间 */
const AGENT_NAMESPACE = "kubedoor";
const AGENT_DEPLOYMENT = "kubedoor-agent";

export type AgentOpResult = {
  success?: boolean;
  message?: string;
  /** master / agent 拒绝或失败时的原因 */
  error?: string;
  /** 重启接口:200 也可能带逐个 Deployment 的失败原因 */
  error_list?: Array<{
    namespace: string;
    deployment_name: string;
    reason: string;
  }>;
};

/**
 * 更新 Agent 镜像(修改 kubedoor-agent 的镜像 tag,Agent 随之滚动重启)
 * @param env K8S环境
 * @param imageTag 镜像标签(版本号)
 * @param options.silent 批量操作:出错不逐个弹提示,由调用方汇总
 */
export const updateAgentImage = (
  env: string,
  imageTag: string,
  options?: { silent?: boolean }
) => {
  return http.request<AgentOpResult>(
    "post",
    "/api/update-image",
    {
      params: { env },
      data: {
        image_tag: imageTag,
        deployment: AGENT_DEPLOYMENT,
        namespace: AGENT_NAMESPACE
      }
    },
    { silent: options?.silent }
  );
};

/**
 * 重启 Agent(滚动重启 kubedoor-agent)
 * @param env K8S环境
 * @param options.silent 批量操作:出错不逐个弹提示,由调用方汇总
 */
export const restartAgent = (env: string, options?: { silent?: boolean }) => {
  return http.request<AgentOpResult>(
    "post",
    "/api/restart",
    {
      params: { env },
      data: {
        deployment_list: [
          { namespace: AGENT_NAMESPACE, deployment_name: AGENT_DEPLOYMENT }
        ],
        node_scheduler: []
      }
    },
    { silent: options?.silent }
  );
};
