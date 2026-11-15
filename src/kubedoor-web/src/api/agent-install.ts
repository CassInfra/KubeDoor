import { http } from "@/utils/http";
import type { NotifyChannelInput, NotifyType } from "@/api/notification";

export type InstallStatus =
  | "draft"
  | "ready"
  | "running"
  | "succeeded"
  | "partial"
  | "failed"
  | "interrupted"
  | "expired";

/**
 * 网页「安装 Agent」对话框能修改的参数。镜像仓库、时序库远程写地址、对象存储上传地址用通用配置，
 * 集群别名装好后在 Agent 列表里编辑
 */
export interface AgentInstallOptions {
  env: string;
  namespace: string;
  master_url: string;
  /** 安装方式：完整安装 / 仅安装 kubedoor-agent，只决定下面三个采集组件的默认值 */
  mode: "full" | "agent";
  agent_tag: string;
  /** install 安装；reuse 不安装，用已有的 Prometheus / vmagent 采集 */
  vmagent: "install" | "reuse";
  /** auto 自动检测（集群里已有就复用，没有就安装）；install 安装；reuse 不安装 */
  ksm: "auto" | "install" | "reuse";
  node_exporter: "auto" | "install" | "reuse";
}

/** 检测时提交的参数：notification 为 null 表示不配置通知；机器人地址留空表示沿用这次安装上次检测时填的 */
export interface AgentInstallRequest extends AgentInstallOptions {
  notification: NotifyChannelInput | null;
}

/** 安装任务里的通知配置：机器人地址只在服务端加密保存，这里只有是否已填写 */
export interface AgentInstallNotification {
  type: NotifyType;
  at: string;
  configured: boolean;
}

/** 正在使用的通用配置：地址里的密码换成了 ******，没配置为空串 */
export interface AgentInstallSettings {
  metrics_write_url: string;
  oss_url: string;
  image_repo: string;
  kubedoor_exturl: string;
  /** 集群指标标签 key：external_labels 里要有「key: 集群唯一标识」 */
  prom_k8s_tag_key?: string;
}

export interface AgentInstallDefaults extends Partial<AgentInstallOptions> {
  /** 通用配置里有时序库远程写地址，可以安装 vmagent */
  metrics_available: boolean;
  settings?: AgentInstallSettings;
}

export interface InstallCheck {
  name: string;
  status: "success" | "warning" | "error" | "pending" | "running" | "skipped";
  message?: string;
  /** 预检失败是因为目标集群里已有 Agent 资源：确认后可以强制覆盖安装 */
  forceable?: boolean;
}

/** 预检列出的、强制覆盖安装时要接管的已有资源 */
export interface InstallTakeover {
  kind: string;
  name: string;
  namespace?: string;
  reason: string;
}

export interface AgentInstallTask {
  id: string;
  status: InstallStatus;
  revision: number;
  context: string;
  contexts?: Array<{ name: string; cluster?: string; server?: string }>;
  options: AgentInstallOptions & {
    notification?: AgentInstallNotification | null;
  };
  checks?: InstallCheck[];
  steps?: InstallCheck[];
  resources?: Array<{ kind: string; name: string; namespace?: string }>;
  summary?: string;
  error?: string;
  created_at?: string;
  updated_at?: string;
  expires_at?: string;
  retryable?: boolean;
  target_locked?: boolean;
  /** 目标集群里已有、需要强制覆盖才能接管的资源 */
  takeover?: InstallTakeover[];
  /** 预检时已确认强制覆盖安装 */
  force?: boolean;
  /** Agent 上线后自动配置 AI Kubeconfig 的结果（Agent 服务账号令牌）；失败不影响安装 */
  ai_connection?: {
    status: "saved" | "skipped" | "failed";
    message: string;
  };
  /** Agent 上线后把填写的通知配置写进「消息通知 → 按集群单独配置」的结果；没填通知时没有，失败不影响安装 */
  notification_result?: {
    status: "saved" | "failed";
    message: string;
  };
  /** 修复任务：覆盖安装哪个已接入集群的 Agent。集群唯一标识固定为 env，命名空间由服务端定为现有 Agent 所在的 */
  repair?: AgentInstallRepair;
}

/** 修复任务的目标集群，以及连接集群用的 kubeconfig 从哪来 */
export interface AgentInstallRepair {
  env: string;
  /** ai-connection：Agent 管理「AI Kubeconfig」列保存的那份；upload：修复时上传的 */
  source: "ai-connection" | "upload";
}

/** 开始修复：有正在执行的安装 / 修复任务就是它；没有可用的 AI Kubeconfig 时 task 为 null，message 说明原因 */
export interface AgentRepairStart {
  task: AgentInstallTask | null;
  message: string;
}

export type HealthStatus = "success" | "warning" | "error" | "skipped";

/** 一项健康检查的结果 */
export interface AgentHealthCheck {
  /** connection / version / k8s_api / pod_mgr / admission / component:<组件> / events / metrics / kubeconfig */
  key: string;
  /** 分组：agent Agent 与连接 / component 组件 / data 数据上报 / repair 修复 */
  group: "agent" | "component" | "data" | "repair";
  name: string;
  status: HealthStatus;
  message: string;
}

/** 「修复」弹框里的健康检查结果 */
export interface AgentHealth {
  env: string;
  online: boolean;
  /** Agent 版本；空串表示不知道 */
  version: string;
  recommended_version: string;
  /** 检查组件用的 Agent 命名空间 */
  namespace: string;
  /** 检查时间（ISO） */
  checked_at: string;
  /** 已按显示顺序排好；Agent 离线时要经 Agent 查的几项是 skipped */
  checks: AgentHealthCheck[];
  /** 这个集群的 AI Kubeconfig：有就能直接用它修复 */
  kubeconfig: {
    configured: boolean;
    origin?: "agent-install" | "upload";
    context?: string;
  };
}

interface Result<T> {
  success: boolean;
  data: T;
  message?: string;
  error?: string;
}

const base = "/api/admin/agent-installations";

async function request<T>(
  method: "get" | "post",
  path: string,
  data?: unknown,
  timeout = 120000
): Promise<T> {
  const result = await http.request<Result<T>>(
    method,
    base + path,
    { data },
    { silent: true, timeout }
  );
  if (!result.success) {
    throw new Error(result.message || result.error || "安装请求失败，请重试");
  }
  return result.data;
}

/** 健康检查：几项并行，最慢的要等 Agent 回包（各限 20 秒）、时序库、kubedoor-ai，最坏 20~30 秒 */
async function health(env: string): Promise<AgentHealth> {
  const result = await http.request<Result<AgentHealth>>(
    "get",
    "/api/admin/agent-health",
    { params: { env } },
    { silent: true, timeout: 60000 }
  );
  if (!result.success) {
    throw new Error(result.message || result.error || "健康检查失败，请重试");
  }
  return result.data;
}

export const agentInstallApi = {
  defaults: () => request<AgentInstallDefaults>("get", "/defaults"),
  list: () => request<AgentInstallTask[]>("get", ""),
  get: (id: string) =>
    request<AgentInstallTask>("get", `/${encodeURIComponent(id)}`),
  /** repairEnv：修复这个已接入集群的 Agent（新建的是修复任务，集群唯一标识固定为它） */
  inspect: (kubeconfig: string, repairEnv?: string) =>
    request<AgentInstallTask>(
      "post",
      "/inspect",
      repairEnv ? { kubeconfig, repair_env: repairEnv } : { kubeconfig }
    ),
  /** 修复：用这个集群保存的 AI Kubeconfig 新建修复任务（有正在执行的任务就返回它；没有可用的 kubeconfig 时 task 为 null） */
  repair: (env: string) =>
    request<AgentRepairStart>("post", "/repair", { env }),
  /** 「修复」弹框里的健康检查 */
  health,
  /** force：确认接管目标集群里已有的 Agent 资源（预检报「已有 Agent 资源」后的强制覆盖安装） */
  preflight: (
    id: string,
    context: string,
    options: AgentInstallRequest,
    force = false
  ) =>
    request<AgentInstallTask>("post", `/${encodeURIComponent(id)}/preflight`, {
      context,
      options,
      ...(force ? { force: true } : {})
    }),
  start: (id: string, revision: number) =>
    request<AgentInstallTask>("post", `/${encodeURIComponent(id)}/start`, {
      revision
    }),
  retry: (id: string) =>
    request<AgentInstallTask>("post", `/${encodeURIComponent(id)}/retry`, {})
};
