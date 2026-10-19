import { http } from "@/utils/http";

/** Istio 管理(设计见 kubedoor-master/istio_route/README.md) */

export type EnvType = "test" | "prod";

/** 集群格子的状态,和后端 istio_route/spec.py 一致 */
export type CellStatus =
  | "synced"
  | "pending"
  | "diff"
  | "missing"
  | "incomplete"
  | "unknown"
  | "unbound"
  | "unmanaged"
  | "none";

/** 组标准规则 match 模板里域名块的标记:这个条件的域名按集群设置 */
export const DOMAIN_KEY = "$domains";

/** 规则里的一个域名条件:写在 authority 或 headers.host(host)上 */
export interface DomainItem {
  field: "authority" | "host";
  op: "exact" | "prefix" | "regex";
  value: string;
}

/** 一条规则在某个集群的域名:按 match 模板里域名块的顺序,每块一组 */
export type DomainLists = DomainItem[][];

export interface IstioGroupCluster {
  k8s_name: string;
  env_type: EnvType;
  remark: string;
  sort_order: number;
  collected_at: string | null;
  collected_by: string;
  vs_count: number;
}

export interface IstioGroup {
  id: number;
  name: string;
  description: string;
  clusters: IstioGroupCluster[];
  created_at: string;
  updated_at: string;
}

export interface IstioGroupList {
  groups: IstioGroup[];
  /** 只有管理员有:所有已接入的集群及所属组(空串表示未分组) */
  clusters?: { k8s_name: string; group: string }[];
}

export interface GroupPayload {
  name: string;
  description: string;
  clusters: { k8s_name: string; env_type: EnvType; remark: string }[];
}

export interface VersionInfo {
  label: string;
  hash: string;
  clusters: string[];
}

export interface MatrixCell {
  in_cluster: boolean;
  version: string | null;
  bound: boolean;
  hosts: string[];
  status: CellStatus;
  applied_at?: string | null;
  applied_by?: string;
}

export interface MatrixRow {
  namespace: string;
  name: string;
  vs_id: number | null;
  versions: VersionInfo[];
  standard_version: string | null;
  gateways: string[];
  route_count: number;
  has_default: boolean;
  lint: string[];
  updated_at: string | null;
  updated_by: string;
  cells: Record<string, MatrixCell>;
  coverage: "" | "test_only" | "prod_only";
  status_count: Record<string, number>;
}

export interface IstioMatrix {
  group: IstioGroup;
  rows: MatrixRow[];
}

export interface IstioRoute {
  id: number;
  vs_id: number;
  priority: number;
  name: string | null;
  /** 匹配条件模板:{"$domains": true} 的条目是域名块,各集群的域名在 domains 里 */
  match_rules: any[] | null;
  rewrite_rules: Record<string, any> | null;
  forward_type: "route" | "delegate" | null;
  forward_detail: any;
  timeout: string | null;
  /** 没建模的字段(retries、corsPolicy 等),只读,下发时原样带上 */
  extra: Record<string, any> | null;
  /** 各集群在域名块里要匹配的域名 */
  domains: Record<string, DomainLists>;
  /** 名称规范问题(用了重写却没名称,或反过来) */
  issue: string | null;
  updated_at: string;
}

export interface VSClusterRow extends IstioGroupCluster {
  bound: boolean;
  in_cluster: boolean;
  hosts: string[];
  status: CellStatus;
  applied_at: string | null;
  applied_by: string;
  /** 还没设置该集群域名的规则(状态是「缺少域名」时不为空) */
  missing_domains: string[];
  /** 集群现状里对应规则的域名(规则 id → 域名),部署到该集群、补设置域名时做默认值 */
  live_domains: Record<string, DomainLists>;
}

export interface VSFields {
  gateways: string[];
  df_forward_type: "route" | "delegate" | null;
  df_forward_detail: any;
  df_forward_timeout: string | null;
}

export interface VSDetail extends VSFields {
  id: number;
  group_id: number;
  namespace: string;
  name: string;
  df_extra: Record<string, any> | null;
  extra_spec: Record<string, any> | null;
  updated_by: string;
  created_at: string;
  updated_at: string;
  group: { id: number; name: string };
  clusters: VSClusterRow[];
  routes: IstioRoute[];
  lint: string[];
}

export interface VSCreatePayload extends VSFields {
  group_id: number;
  namespace: string;
  name: string;
  clusters: { k8s_name: string; hosts: string[] }[];
}

export interface RoutePayload {
  name: string | null;
  priority?: number | null;
  match_rules: any[];
  rewrite_rules: Record<string, any> | null;
  forward_type: "route" | "delegate";
  forward_detail: any;
  timeout: string | null;
  /** 有域名块时,已纳入的集群都要给:集群 → 各域名块的域名 */
  domains: Record<string, DomainLists>;
}

export interface CompareSide {
  side: string;
  label: string;
  exists: boolean;
  spec: Record<string, any>;
  vs_id?: number;
  /** 组标准 · 集群:还没设置该集群域名的规则 */
  missing_domains?: string[];
}

export interface RouteDiffRow {
  /** domains:只是规则里的域名不同 */
  op: "same" | "domains" | "changed" | "left_only" | "right_only";
  moved: boolean;
  label: string;
  left_index: number | null;
  right_index: number | null;
  left: Record<string, any> | null;
  right: Record<string, any> | null;
}

export interface SpecDiff {
  same: boolean;
  shared_same: boolean;
  /** 只有 hosts 和规则里的域名不同 */
  rules_same: boolean;
  hosts: { same: boolean; left_only: string[]; right_only: string[] };
  gateways: { same: boolean; left: string[]; right: string[] };
  default: { same: boolean; left: any; right: any };
  extra: { same: boolean; left: any; right: any };
  routes: RouteDiffRow[];
  summary: {
    left_only: number;
    right_only: number;
    changed: number;
    domains: number;
    moved: number;
  };
}

export interface CompareResult {
  left: CompareSide;
  right: CompareSide;
  diff: SpecDiff;
}

export interface ImportItem {
  namespace: string;
  name: string;
  base: string;
  clusters: string[];
}

export interface ItemResult {
  namespace: string;
  name: string;
  success: boolean;
  error?: string | null;
  vs_id?: number;
}

export interface CollectResult {
  k8s_name: string;
  success: boolean;
  count?: number;
  error?: string;
}

export interface ApplyItem {
  vs_id: number;
  k8s_name: string;
}

export interface ApplyPreviewItem extends ApplyItem {
  namespace: string;
  name: string;
  error: string | null;
  exists: boolean;
  live: Record<string, any> | null;
  rendered: Record<string, any>;
  same: boolean;
  /** 集群现状(左)→ 将要下发(右);集群里还没有时为空 */
  diff: SpecDiff | null;
}

export interface ApplyResult extends ApplyItem {
  namespace: string;
  name: string;
  success: boolean;
  error: string | null;
}

type Result<T> = { success: boolean; data: T; msg?: string };
type Done = { success: boolean; msg?: string };

// ---------------------------------------------------------------- 组
export const getIstioGroups = () =>
  http.request<Result<IstioGroupList>>("get", "/api/istio/groups");

export const createIstioGroup = (data: GroupPayload) =>
  http.request<Result<{ id: number }>>("post", "/api/istio/groups", { data });

export const updateIstioGroup = (id: number, data: GroupPayload) =>
  http.request<Done>("put", "/api/istio/groups", { params: { id }, data });

export const deleteIstioGroup = (id: number) =>
  http.request<Done>("delete", "/api/istio/groups", { params: { id } });

// ---------------------------------------------------------------- 矩阵 / 现状 / 对比 / 纳管
export const getIstioMatrix = (groupId: number) =>
  http.request<Result<IstioMatrix>>("get", "/api/istio/matrix", {
    params: { group_id: groupId }
  });

/** 刷新集群现状:不传集群就是组里全部集群 */
export const collectIstio = (groupId: number, k8sNames?: string[]) =>
  http.request<Result<CollectResult[]>>("post", "/api/istio/collect", {
    data: { group_id: groupId, k8s_names: k8sNames }
  });

/** side:standard、standard@集群、cluster@集群 */
export const compareIstio = (
  groupId: number,
  namespace: string,
  name: string,
  left: string,
  right: string
) =>
  http.request<Result<CompareResult>>("get", "/api/istio/compare", {
    params: { group_id: groupId, namespace, name, left, right }
  });

export const importIstio = (groupId: number, items: ImportItem[]) =>
  http.request<Result<ItemResult[]>>("post", "/api/istio/import", {
    data: { group_id: groupId, items }
  });

/** 以某个集群的现状更新组标准(规则、默认路由、gateways),各集群 hosts 不变 */
export const resetIstioVS = (vsId: number, k8sName: string) =>
  http.request<Done>("post", "/api/istio/vs/reset", {
    data: { vs_id: vsId, k8s_name: k8sName }
  });

// ---------------------------------------------------------------- 组标准 VS
export const getIstioVS = (vsId: number) =>
  http.request<Result<VSDetail>>("get", "/api/istio/vs", {
    params: { vs_id: vsId }
  });

export const createIstioVS = (data: VSCreatePayload) =>
  http.request<Result<{ id: number }>>("post", "/api/istio/vs", { data });

export const updateIstioVS = (vsId: number, data: VSFields) =>
  http.request<Done>("put", "/api/istio/vs", { params: { vs_id: vsId }, data });

export const deleteIstioVS = (vsId: number) =>
  http.request<Done>("delete", "/api/istio/vs", { params: { vs_id: vsId } });

/** 部署到某个集群 / 修改该集群的 hosts;domains(规则 id → 各域名块的域名)给了就一起保存该集群的域名 */
export const saveIstioVSCluster = (
  vsId: number,
  k8sName: string,
  hosts: string[],
  domains?: Record<number, DomainLists>
) =>
  http.request<Done>("put", "/api/istio/vs/cluster", {
    data: { vs_id: vsId, k8s_name: k8sName, hosts, domains }
  });

export const removeIstioVSCluster = (vsId: number, k8sName: string) =>
  http.request<Done>("delete", "/api/istio/vs/cluster", {
    params: { vs_id: vsId, k8s_name: k8sName }
  });

// ---------------------------------------------------------------- 规则
export const createIstioRoute = (vsId: number, data: RoutePayload) =>
  http.request<Result<{ id: number }>>("post", "/api/istio/httproute", {
    params: { vs_id: vsId },
    data
  });

export const updateIstioRoute = (routeId: number, data: RoutePayload) =>
  http.request<Done>("put", "/api/istio/httproute", {
    params: { route_id: routeId },
    data
  });

export const deleteIstioRoute = (routeId: number) =>
  http.request<Done>("delete", "/api/istio/httproute", {
    params: { route_id: routeId }
  });

export const reorderIstioRoutes = (vsId: number) =>
  http.request<Done>("post", "/api/istio/httproute/reorder", {
    params: { vs_id: vsId }
  });

// ---------------------------------------------------------------- 下发
export const previewIstioApply = (items: ApplyItem[]) =>
  http.request<Result<ApplyPreviewItem[]>>("post", "/api/istio/apply/preview", {
    data: { items }
  });

export const applyIstio = (items: ApplyItem[]) =>
  http.request<Result<ApplyResult[]>>("post", "/api/istio/apply", {
    data: { items }
  });

// ---------------------------------------------------------------- 规则编辑辅助
/** 已接入的集群列表(监控、资源等多个页面也从这里引用) */
export const getAgentNames = () =>
  http.request<{ success: boolean; data?: Array<any>; count?: any }>(
    "get",
    "/api/agent_names"
  );

/** 获取服务的第一个端口号 */
export const getServiceFirstPort = (
  env: string,
  namespace: string,
  service_name: string
) =>
  http.request<any>("get", "/api/agent/service/first-port", {
    params: { env, namespace, service_name }
  });
