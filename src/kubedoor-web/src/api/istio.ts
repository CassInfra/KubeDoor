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
  /** 采集按集群,各组共用 */
  collected_at: string | null;
  collected_by: string;
  /** 集群里的 VS 总数(不分组) */
  vs_count: number;
}

/** 组的包含 / 忽略规则:glob 通配符(* 任意多个字符,? 一个字符)或 regex 正则(全匹配);
 *  规则里有 / 时匹配「命名空间/名称」,否则只匹配名称 */
export interface VSPattern {
  type: "glob" | "regex";
  pattern: string;
}

export interface IstioGroup {
  id: number;
  name: string;
  description: string;
  /** 包含的 VS,为空表示全部 */
  include_patterns: VSPattern[];
  /** 忽略的 VS,优先于包含 */
  ignore_patterns: VSPattern[];
  clusters: IstioGroupCluster[];
  created_at: string;
  updated_at: string;
}

export interface IstioGroupList {
  groups: IstioGroup[];
  /** 只有管理员有:所有已接入的集群,以及各自已在哪些组里(一个集群可以属于多个组) */
  clusters?: { k8s_name: string; groups: string[] }[];
}

export interface GroupPayload {
  name: string;
  description: string;
  clusters: { k8s_name: string; env_type: EnvType; remark: string }[];
  include_patterns: VSPattern[];
  ignore_patterns: VSPattern[];
}

/** VS 在组里的范围判断:rule 组规则纳入 / include 单独纳入 / manual 手动忽略 / ignore_rule 匹配忽略规则 / not_included 不在包含规则里 */
export type ScopeReason =
  | "rule"
  | "include"
  | "manual"
  | "ignore_rule"
  | "not_included";

export interface VSScopeResult {
  visible: boolean;
  reason: ScopeReason;
  /** 命中的规则 */
  pattern: string | null;
}

/** 同一个 VS 只能属于一个组:也在这个组里(与本组共用这些集群) */
export interface VSConflict {
  group_id: number;
  group: string;
  clusters: string[];
}

/** 本组忽略的 VS(手动忽略的,和被组规则排除的) */
export interface IgnoredVS {
  namespace: string;
  name: string;
  reason: "manual" | "ignore_rule" | "not_included";
  pattern: string | null;
  /** 本组有组标准(忽略期间保留) */
  vs_id: number | null;
  /** 在哪些集群的现状里 */
  clusters: string[];
  by: string;
  at: string | null;
  /** 手动忽略过、现状和组标准里都已经没有了 */
  stale: boolean;
}

export interface ScopeStats {
  /** 组内各集群现状 + 组标准里的 VS 总数 */
  total: number;
  visible: number;
  ignored: number;
  manual: number;
  by_rule: number;
  conflict: number;
}

export interface GroupPreview {
  total: number;
  visible: number;
  ignored: number;
  conflict: number;
  /** 还没采集过现状的集群 */
  uncollected: string[];
  items: (VSScopeResult & {
    namespace: string;
    name: string;
    clusters: string[];
    managed: boolean;
    conflicts: VSConflict[];
  })[];
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
  /** 也在共用集群的其他组里(同一个 VS 只能属于一个组),不为空时整行标红 */
  conflicts: VSConflict[];
  /** 被组规则排除、单独纳入本组的 */
  included: boolean;
}

export interface IstioMatrix {
  group: IstioGroup;
  rows: MatrixRow[];
  ignored: IgnoredVS[];
  stats: ScopeStats;
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
  /** 在本组的范围判断:被忽略时不显示在列表里,也不能下发 */
  scope: VSScopeResult;
  conflicts: VSConflict[];
}

export interface VSCreatePayload extends VSFields {
  group_id: number;
  namespace: string;
  name: string;
  clusters: { k8s_name: string; hosts: string[] }[];
  /** 名称被本组排除时,确认后仍然创建并单独纳入本组 */
  force_include?: boolean;
}

/** 新建 VS 前的检查 */
export interface VSCheck {
  scope: VSScopeResult;
  /** 本组已有这个 VS 的组标准(可能被忽略了) */
  standard: boolean;
  /** 集群里已有同名的(要用纳管) */
  in_clusters: string[];
  conflicts: VSConflict[];
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
  /** 集群里的 VS 总数 */
  count?: number;
  /** 按本组规则归本组的 / 被忽略的 */
  group_count?: number;
  ignored_count?: number;
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

/** 组管理里预览包含 / 忽略规则的效果(按所选集群已采集的现状,不保存) */
export const previewIstioGroup = (params: {
  group_id: number | null;
  clusters: string[];
  include_patterns: VSPattern[];
  ignore_patterns: VSPattern[];
}) =>
  http.request<Result<GroupPreview>>("get", "/api/istio/groups/preview", {
    params: {
      group_id: params.group_id ?? undefined,
      clusters: params.clusters.join(","),
      include: JSON.stringify(params.include_patterns),
      ignore: JSON.stringify(params.ignore_patterns)
    }
  });

/** 在组里忽略这些 VS:不再显示、不再处理;集群里的 VS 不受影响 */
export const ignoreIstioVS = (
  groupId: number,
  items: { namespace: string; name: string }[]
) =>
  http.request<Done>("post", "/api/istio/ignore", {
    data: { group_id: groupId, items }
  });

/** 取消忽略;被组规则排除的会单独纳入本组 */
export const unignoreIstioVS = (
  groupId: number,
  items: { namespace: string; name: string }[]
) =>
  http.request<Done>("post", "/api/istio/unignore", {
    data: { group_id: groupId, items }
  });

/** 删除集群里的 VirtualService(立即生效);本组部署到了该集群的,同时不再部署 */
export const deleteIstioLiveVS = (
  groupId: number,
  k8sName: string,
  namespace: string,
  name: string
) =>
  http.request<Done>("post", "/api/istio/live/delete", {
    data: { group_id: groupId, k8s_name: k8sName, namespace, name }
  });

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

/** 新建前检查:本组是否已有、集群里是否已有、其他组里是否已有、名称是否被本组排除 */
export const checkIstioVS = (
  groupId: number,
  namespace: string,
  name: string
) =>
  http.request<Result<VSCheck>>("get", "/api/istio/vs/check", {
    params: { group_id: groupId, namespace, name }
  });

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
