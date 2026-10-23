import { http } from "@/utils/http";

/** 发起测试的 kubedoor-agent Pod(解析走它的 /etc/resolv.conf,和业务 Pod 一样) */
export interface ProbeSource {
  pod: string;
  ip: string;
  nameservers: string[];
  search: string[];
}

export interface ResolveAnswer {
  ips: string[];
  /** not_found:域名不存在;no_data:没有这种类型的记录;temporary / timeout / error:没能解析 */
  code: "ok" | "not_found" | "no_data" | "temporary" | "timeout" | "error";
  error: string;
  ms: number;
}

export interface ResolveResult {
  source: ProbeSource;
  results: Record<string, ResolveAnswer>;
}

export interface TcpAttempt {
  ip: string;
  ok: boolean;
  error: string;
  /** 连接用的源地址(agent 的 Pod IP) */
  local: string;
  ms: number;
}

export interface TcpResult {
  source: ProbeSource;
  host: string;
  port: number;
  /** host 是域名时的解析结果;是 IP 时为 null */
  dns: ResolveAnswer | null;
  attempts: TcpAttempt[];
  ok: boolean;
}

export interface ProbeResponse<T> {
  success?: boolean;
  data?: T;
  error?: string;
  message?: string;
}

/**
 * 在 kubedoor-agent 所在 Pod 里解析域名(和进 Pod 执行 ping 一样走系统解析器)
 * @param family 4 只要 IPv4、6 只要 IPv6、any 都要
 * @param silent 检查弹框每秒调一次:静默请求,不显示进度条、出错不弹全局提示
 */
export const resolveFromAgent = (
  env: string,
  names: string[],
  family: "4" | "6" | "any" = "any",
  silent = false
) =>
  http.request<ProbeResponse<ResolveResult>>(
    "get",
    "/api/agent/net/resolve",
    { params: { env, names: names.join(","), family } },
    { silent }
  );

/** 从 kubedoor-agent 所在 Pod 测试 TCP 端口(相当于 nc -zv host port) */
export const tcpCheckFromAgent = (env: string, host: string, port: number) =>
  http.request<ProbeResponse<TcpResult>>(
    "get",
    "/api/agent/net/tcp",
    { params: { env, host, port } },
    { silent: true }
  );
