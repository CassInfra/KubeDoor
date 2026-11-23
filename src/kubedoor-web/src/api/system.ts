import { http } from "@/utils/http";
import type { ClusterPermission, UserRole } from "./auth";
import type { OpLevels, PolicyValue } from "@/utils/permission";

export interface SystemUser {
  id: number;
  username: string;
  source: "local" | "ldap";
  role: UserRole;
  display_name: string;
  email: string;
  enabled: boolean;
  builtin: boolean;
  super_admin: boolean;
  must_change_password: boolean;
  password_default: boolean;
  last_login_at: string | null;
  last_login_ip: string;
  comment: string;
  created_at: string;
  session_count: number;
  clusters: Record<string, ClusterPermission>;
}

export interface ClusterGrant {
  env: string;
  permission: ClusterPermission;
}

export interface KnownCluster {
  env: string;
  online: boolean;
}

export interface LdapConfig {
  enabled: boolean;
  url: string;
  start_tls: boolean;
  tls_verify: boolean;
  ca_cert: string;
  bind_dn: string;
  /** 只写:不填表示保持原值,空字符串表示清除 */
  bind_password?: string | null;
  has_bind_password?: boolean;
  base_dn: string;
  user_filter: string;
  user_dn_template: string;
  attr_username: string;
  attr_display_name: string;
  attr_email: string;
  auto_create: boolean;
  default_role: "read" | "none";
  timeout: number;
}

export interface LdapTestStep {
  name: string;
  ok: boolean;
  detail: string;
}

export interface LdapTestResult {
  success: boolean;
  steps: LdapTestStep[];
  user?: { dn: string; username: string; display_name: string; email: string };
}

export interface AuditRecord {
  id: number;
  ts: string;
  username: string;
  action: string;
  env: string;
  target: string;
  detail: Record<string, any> | null;
  ip: string;
  success: boolean;
}

/** 权限表的一行:一种操作类型的定义(顺序即页面上的行顺序) */
export interface PermissionOperation {
  key: string;
  group: string;
  name: string;
  desc: string;
  /** 适用范围:网页 / AI / 网页+AI */
  applies: string;
  defaults: OpLevels;
  /** 审批默认值 */
  approval: boolean;
  /** 审批固定不可改:approval 为 true 时是高危固定审批,为 false 时等同 approval_na */
  approval_fixed: boolean;
  /** 审批列显示「—」:不是 AI 操作(或者是查询),不能勾选 */
  approval_na: boolean;
  /** 页面上标红(权限很宽的操作) */
  highlight: boolean;
}

export interface PermissionPolicyData {
  definitions: PermissionOperation[];
  policy: PolicyValue;
  defaults: PolicyValue;
  /** 查看命令的内置参数检查说明(命令 → 说明) */
  builtin_checks?: Record<string, string>;
  updated_by: string | null;
  updated_at: string | null;
}

type Result<T> = { success: boolean; data: T; message?: string };

export const getUsers = () =>
  http.request<Result<SystemUser[]>>("get", "/api/admin/users");

export const createUser = (data: Partial<SystemUser> & { password?: string }) =>
  http.request<Result<SystemUser>>("post", "/api/admin/users", { data });

export const updateUser = (id: number, data: Partial<SystemUser>) =>
  http.request<Result<null>>("put", `/api/admin/users/${id}`, { data });

export const deleteUser = (id: number) =>
  http.request<Result<null>>("delete", `/api/admin/users/${id}`);

export const resetUserPassword = (id: number, password: string) =>
  http.request<Result<null>>("put", `/api/admin/users/${id}/password`, {
    data: { password }
  });

export const logoutUser = (id: number) =>
  http.request<Result<null>>("post", `/api/admin/users/${id}/logout`);

export const getUserClusters = (id: number) =>
  http.request<
    Result<{
      user: SystemUser;
      clusters: ClusterGrant[];
      known: KnownCluster[];
    }>
  >("get", `/api/admin/users/${id}/clusters`);

export const setUserClusters = (id: number, clusters: ClusterGrant[]) =>
  http.request<Result<null>>("put", `/api/admin/users/${id}/clusters`, {
    data: { clusters }
  });

export const getKnownClusters = () =>
  http.request<Result<KnownCluster[]>>("get", "/api/admin/clusters");

export const getLdapConfig = () =>
  http.request<Result<LdapConfig>>("get", "/api/admin/ldap");

export const saveLdapConfig = (data: LdapConfig) =>
  http.request<Result<LdapConfig>>("put", "/api/admin/ldap", { data });

export const testLdapConfig = (data: {
  config: LdapConfig;
  username?: string;
  password?: string;
}) =>
  http.request<Result<LdapTestResult>>(
    "post",
    "/api/admin/ldap/test",
    { data },
    { timeout: 60000 }
  );

export const getAuditLogs = (params: Record<string, any>) =>
  http.request<Result<AuditRecord[]> & { total: number }>(
    "get",
    "/api/admin/audit",
    { params }
  );

export const getAuditActions = () =>
  http.request<Result<string[]>>("get", "/api/admin/audit/actions");

/** 权限与审批:定义、当前配置、出厂默认值 */
export const getPermissionPolicy = () =>
  http.request<Result<PermissionPolicyData>>(
    "get",
    "/api/admin/permission-policy"
  );

/** 保存权限与审批,成功时返回和 GET 一样的数据 */
export const savePermissionPolicy = (data: PolicyValue) =>
  http.request<Result<PermissionPolicyData>>(
    "put",
    "/api/admin/permission-policy",
    { data }
  );

/** 获取代码接口的请求头:值只写不读 */
export interface CodeInfoHeader {
  name: string;
  /** 只写:留空表示保持已保存的值 */
  value?: string | null;
  has_value?: boolean;
}

/** 获取代码接口的配置(Deployment 页「获取代码」由 master 调这个接口) */
export interface CodeInfoConfig {
  enabled: boolean;
  url: string;
  headers: CodeInfoHeader[];
  timeout: number;
  tls_verify: boolean;
  /** 允许 AI 助手下载源码分析(源码会发给模型服务商),默认关闭 */
  ai_download: boolean;
  updated_by?: string | null;
  updated_at?: string | null;
}

/** master POST 给接口的请求体,固定这 4 个字段 */
export interface CodeInfoRequest {
  k8s: string;
  namespace: string;
  deployment: string;
  image: string;
}

/** 接口返回的 3 个链接;没返回或者不是 http(s) 地址的是空字符串 */
export interface CodeInfoLinks {
  /** 代码页面:GitLab 上这个仓库或这个 commit 的代码页面 */
  web_url: string;
  /** 下载链接:这个 commit 的代码压缩包 */
  download_url: string;
  /** Commit 说明:这个仓库最近一些 commit 的提交说明 */
  commits_url: string;
}

/** 格式不对(不是 http(s) 地址)的字段和原值 */
export type CodeInfoInvalid = Partial<Record<keyof CodeInfoLinks, string>>;

export interface CodeInfoTestResult {
  ok: boolean;
  status: number | null;
  elapsed_ms: number;
  /** 原始响应(最多 4KB) */
  body: string;
  links: CodeInfoLinks;
  invalid: CodeInfoInvalid;
  error: string;
  /** 发出的请求体:测试时只有填了的字段 */
  request: Partial<CodeInfoRequest>;
}

export const getCodeInfoConfig = () =>
  http.request<Result<CodeInfoConfig>>("get", "/api/admin/code-info");

/** 保存配置;没传的字段沿用原值(右上角开关只传 enabled) */
export const saveCodeInfoConfig = (data: Partial<CodeInfoConfig>) =>
  http.request<Result<CodeInfoConfig>>("put", "/api/admin/code-info", {
    data
  });

/** 用页面上的配置(不用先保存)和手填的入参调一次接口;入参都可以不填,没填的不放进请求体 */
export const testCodeInfoConfig = (data: {
  config: Partial<CodeInfoConfig>;
  request: Partial<CodeInfoRequest>;
}) =>
  http.request<Result<CodeInfoTestResult>>(
    "post",
    "/api/admin/code-info/test",
    { data },
    // 接口超时最长 60 秒
    { timeout: 70000 }
  );

/** 获取运行配置接口的配置(Deployment / Pod 页「获取运行配置」由 master 调这个接口) */
export interface RuntimeConfigSettings {
  enabled: boolean;
  url: string;
  /** 请求头:同获取代码接口,值只写不读 */
  headers: CodeInfoHeader[];
  /** 一次获取的总时长(秒),包括接口返回 202 后的等待和重试;5~120,默认 60 */
  timeout: number;
  tls_verify: boolean;
  /** 允许 AI 助手获取运行配置(密码、密钥换成占位符),默认关闭 */
  ai_enabled: boolean;
  updated_by?: string | null;
  updated_at?: string | null;
}

/**
 * master POST 给接口的请求体(不含 master 每次都带的 wait):deployment 和 pod 二选一,
 * 可选字段只在有值时带;测试时只有填了的字段
 */
export interface RuntimeConfigRequest {
  k8s?: string;
  namespace?: string;
  deployment?: string;
  pod?: string;
  container?: string;
  /** 按配置名过滤的正则,不区分大小写 */
  keys?: string;
  /** 附带配置来源 */
  detail?: boolean;
  /** 不用缓存,重新采集 */
  refresh?: boolean;
}

export interface RuntimeConfigTestResult {
  ok: boolean;
  status: number | null;
  elapsed_ms: number;
  /** 请求了几次(接口返回 202 时会重试) */
  attempts: number;
  error: string;
  /** 发出的请求体 */
  request: RuntimeConfigRequest;
  /** 接口返回的 JSON 对象;不是 JSON 对象时为 null */
  config: Record<string, any> | null;
  /** 返回的不是 JSON 对象时的原始响应(最多 64KB) */
  body: string;
}

export const getRuntimeConfigSettings = () =>
  http.request<Result<RuntimeConfigSettings>>(
    "get",
    "/api/admin/runtime-config"
  );

/** 保存配置;没传的字段沿用原值(右上角的开关只传 enabled / ai_enabled) */
export const saveRuntimeConfigSettings = (
  data: Partial<RuntimeConfigSettings>
) =>
  http.request<Result<RuntimeConfigSettings>>(
    "put",
    "/api/admin/runtime-config",
    { data }
  );

/** 用页面上的配置(不用先保存)和手填的入参调一次接口;入参都可以不填,没填的不放进请求体 */
export const testRuntimeConfigSettings = (data: {
  config: Partial<RuntimeConfigSettings>;
  request: RuntimeConfigRequest;
}) =>
  http.request<Result<RuntimeConfigTestResult>>(
    "post",
    "/api/admin/runtime-config/test",
    { data },
    // 一次获取最长 120 秒(含 202 重试),再留些网络余量
    { timeout: 130000 }
  );

/** 链路追踪的语义字段,顺序即页面上的显示顺序(和 master tracing/fields.py 一致) */
export type TracingFieldKey =
  | "status_code"
  | "method"
  | "route"
  | "path"
  | "url"
  | "query"
  | "host"
  | "client_ip"
  | "exception_type"
  | "pod"
  | "namespace"
  | "deployment";

/** 语义字段 → 候选属性名(顺序即优先级,每项 0~5 个) */
export type TracingFieldMap = Record<TracingFieldKey, string[]>;

/** 业务字段:链路里记录的业务标识,告诉 AI 按哪个属性查 */
export interface TracingCustomField {
  key: string;
  label: string;
}

/** ClickHouse 加速(可选);GET 返回的没有 password,换成 has_password */
export interface TracingClickHouse {
  enabled: boolean;
  url: string;
  database: string;
  table: string;
  /** 汇总表,可空 = 没有汇总表 */
  dim_table: string;
  username: string;
  /** 只写:不传表示沿用已保存的密码 */
  password?: string;
  /** 只写:true 表示清空已保存的密码(同时忽略 password),换成没有密码的账号时用 */
  clear_password?: boolean;
  /** GET 返回:已保存过密码 */
  has_password?: boolean;
  /** 单条 SQL 的超时(秒),5~120 */
  timeout: number;
  tls_verify: boolean;
}

/** 链路追踪配置(系统管理 → 链路追踪) */
export interface TracingConfig {
  enabled: boolean;
  /** Jaeger 查询地址(Jaeger UI 的地址,含 base path) */
  url: string;
  /** 给人点的 Jaeger UI 地址;空 = url */
  ui_url: string;
  /** 请求头:同获取代码接口,值只写不读 */
  headers: CodeInfoHeader[];
  /** 单次请求超时(秒),5~120 */
  timeout: number;
  tls_verify: boolean;
  /** 链路里区分集群的资源属性名,例如 k8sname;空 = 不按集群过滤 */
  cluster_key: string;
  fields: TracingFieldMap;
  custom_fields: TracingCustomField[];
  /** 额外打码的属性名 */
  mask_keys: string[];
  clickhouse: TracingClickHouse;
}

/** KubeDoor 的集群和别名(Agent 管理页维护) */
export interface TracingCluster {
  env: string;
  aliases: string[];
}

/** GET / PUT 的返回 */
export interface TracingSettings extends TracingConfig {
  /** 字段映射的默认候选 */
  field_defaults: TracingFieldMap;
  /** 字段映射的中文名称 */
  field_labels: Record<TracingFieldKey, string>;
  /** 字段映射的层级:空 = 不限(Span + 资源属性),event = 异常事件 */
  field_levels?: Partial<Record<TracingFieldKey, string>>;
  /** 内置打码的属性名 */
  mask_builtin?: string[];
  clusters: TracingCluster[];
  updated_by?: string | null;
  updated_at?: string | null;
}

/** 提交的配置:没传的字段沿用已保存的(右上角开关只传 enabled) */
export type TracingConfigInput = Partial<Omit<TracingConfig, "clickhouse">> & {
  clickhouse?: Partial<TracingClickHouse>;
};

export type TracingTestTarget = "jaeger" | "clickhouse";

/** Jaeger /api/v3/capabilities 的返回;false 的字段省略 */
export interface TracingCapabilities {
  search?: {
    withoutServiceName?: boolean;
    sameSpanConjunction?: boolean;
    filter?: { levels?: string[]; operators?: string[] };
  };
}

export interface TracingJaegerTest {
  ok: boolean;
  elapsed_ms: number;
  services_count?: number;
  /** 服务列表(前 1000 个) */
  services?: string[];
  /** 2.23 之前没有这个接口:null */
  capabilities?: TracingCapabilities | null;
  /** Jaeger ≥ 2.23 且支持结构化过滤,能搜索 */
  search_ok?: boolean;
  messages?: string[];
  error?: string;
}

/** 链路里的集群取值和它对上的 KubeDoor 集群(对不上或有歧义为 null) */
export interface TracingClusterValue {
  value: string;
  env: string | null;
  /** 同时是几个集群的名称或别名 */
  ambiguous?: boolean;
  /** ClickHouse 测试:最近 10 分钟的 Span 数 */
  spans?: number;
}

export interface TracingClickHouseTest {
  ok: boolean;
  elapsed_ms: number;
  version?: string;
  /** 表名 → 缺少的列 */
  missing_columns?: Record<string, string[]>;
  cluster_values?: TracingClusterValue[];
  messages?: string[];
  error?: string;
}

export interface TracingTestResults {
  jaeger: TracingJaegerTest;
  clickhouse: TracingClickHouseTest;
}

export type TracingKeyLevel = "span" | "resource" | "event" | "scope";

/** 识别字段:链路里出现过的一个属性 */
export interface TracingObservedKey {
  key: string;
  level: TracingKeyLevel;
  types: string[];
  count: number;
  /** 最多 3 个示例值(每个 ≤80 字,敏感值已打码) */
  examples: string[];
}

/** 识别字段:取值能对上 KubeDoor 集群名或别名的资源属性 */
export interface TracingClusterCandidate {
  key: string;
  values: TracingClusterValue[];
  matched_envs: string[];
}

export interface TracingAnalyzeResult {
  traces: number;
  spans: number;
  keys: TracingObservedKey[];
  cluster_candidates: TracingClusterCandidate[];
  suggestions: {
    cluster_key?: string;
    fields?: Partial<TracingFieldMap>;
  };
  /** 说明,例如取样时没找到链路 */
  notes?: string[];
}

/** 识别字段:从 Jaeger 按服务取样,或者解析粘贴的 Trace JSON(≤5MB) */
export type TracingAnalyzeRequest = { config?: TracingConfigInput } & (
  | { mode: "sample"; service: string; lookback?: string }
  | { mode: "json"; trace_json: string }
);

export const getTracingConfig = () =>
  http.request<Result<TracingSettings>>("get", "/api/admin/tracing");

/** 保存配置;没传的字段沿用原值(右上角开关只传 enabled) */
export const saveTracingConfig = (data: TracingConfigInput) =>
  http.request<Result<TracingSettings>>("put", "/api/admin/tracing", {
    data
  });

/**
 * 用页面上的配置(不用先保存,请求头的值和密码留空用已保存的)测试 Jaeger 或 ClickHouse;
 * timeout 是浏览器等待的毫秒数,按配置的超时和要发的请求数算
 */
export const testTracingConfig = <T extends TracingTestTarget>(
  target: T,
  config: TracingConfigInput,
  timeout = 130000
) =>
  http.request<Result<TracingTestResults[T]>>(
    "post",
    "/api/admin/tracing/test",
    { data: { target, config } },
    { timeout }
  );

/** 识别字段:样例只用于识别,不保存 */
export const analyzeTracingFields = (
  data: TracingAnalyzeRequest,
  timeout = 130000
) =>
  http.request<Result<TracingAnalyzeResult>>(
    "post",
    "/api/admin/tracing/analyze",
    { data },
    { timeout }
  );

// ---------------------------------------------------------------------------
// 历史日志(系统管理 → 历史日志,AI 助手的 kubedoor-logs Skill 查 ClickHouse / Elasticsearch 用)
// ---------------------------------------------------------------------------
export type LogConnectionType = "clickhouse" | "elasticsearch";
/** clickhouse 固定 basic;elasticsearch: none / basic / api_key */
export type LogAuthType = "none" | "basic" | "api_key";
/** access 请求 / 访问日志;app 应用日志;other 其他 */
export type LogSourceKind = "access" | "app" | "other";
export type LogDurationUnit = "ms" | "s" | "us" | "ns";
/** 语义字段,顺序即页面上的显示顺序(以 GET 返回的 semantic_fields 为准) */
export type LogSemanticField =
  | "message"
  | "level"
  | "namespace"
  | "pod"
  | "service"
  | "status"
  | "duration"
  | "method"
  | "path"
  | "host"
  | "client_ip";
export type LogLevelKey = "error" | "warn" | "info" | "debug";
/** 集群绑定的过滤条件:eq / ne / prefix 恰好 1 个值,in / not_in 1~50 个 */
export type LogConditionOp = "eq" | "ne" | "in" | "not_in" | "prefix";

/** 一个 ClickHouse(HTTP 接口)或 Elasticsearch 的访问方式;GET 返回的没有 password / api_key */
export interface LogConnection {
  /** ^[a-z][a-z0-9_-]{0,31}$;已保存的连接不能改(密码按 id 沿用) */
  id: string;
  name: string;
  type: LogConnectionType;
  url: string;
  auth_type: LogAuthType;
  username: string;
  /** 只写:不传表示沿用已保存的密码 */
  password?: string;
  /** 只写:ES 的 API Key(base64 编码的 id:key 原样填),不传表示沿用 */
  api_key?: string;
  /** 只写:true 表示清除已保存的密码 / API Key */
  clear_password?: boolean;
  clear_api_key?: boolean;
  /** GET 返回:已保存过密码 / API Key */
  has_password?: boolean;
  has_api_key?: boolean;
  /** 请求头:同获取代码接口,值只写不读 */
  headers: CodeInfoHeader[];
  /** 单次查询超时(秒),5~120 */
  timeout: number;
  tls_verify: boolean;
  /** 同一连接同时进行的查询数,1~20 */
  max_concurrency: number;
  /** ClickHouse:单次查询每个分片最多读取行数,0 = 不限;ES 忽略 */
  max_rows_to_read: number;
  /** 允许 AI 用 log_query 写原生查询 */
  allow_raw: boolean;
}

/** 关联字段:不同日志源之间串起同一个请求的 ID */
export interface LogCorrelationKey {
  key: string;
  label: string;
  description: string;
  /** 取值的正则(可空,不含 ^ $) */
  pattern: string;
  /** 链路 ID(最多一个),可以接着用链路追踪的 trace_get */
  trace: boolean;
}

/** 日志源里登记的关联字段:field(列)和 text(正文写法,恰好一个 {value})二选一 */
export interface LogSourceCorrelation {
  key: string;
  field?: string;
  text?: string;
}

/** 字段说明 */
export interface LogColumnNote {
  name: string;
  description: string;
}

/** 日志源:一类日志(结构和含义相同) */
export interface LogSource {
  id: string;
  name: string;
  enabled: boolean;
  connection: string;
  kind: LogSourceKind;
  /** Markdown,≤ 8000 */
  description: string;
  time_field: string;
  /** 语义字段 → 实际字段名,只有非空项 */
  fields: Partial<Record<LogSemanticField, string>>;
  duration_unit: LogDurationUnit;
  /** 只有非空项 */
  level_values: Partial<Record<LogLevelKey, string[]>>;
  correlations: LogSourceCorrelation[];
  columns: LogColumnNote[];
  default_fields: string[];
  mask_fields: string[];
  mask_patterns: string[];
  /** 页面便利:新建绑定时默认的表 / 索引 */
  default_target: string;
  /** 页面便利:区分集群的字段,新建绑定时默认按它出条件 */
  cluster_field: string;
}

export interface LogBindingCondition {
  field: string;
  op: LogConditionOp;
  values: (string | number)[];
}

/** 集群绑定:这类日志在这个集群里的表 / 索引和过滤条件 */
export interface LogBinding {
  source: string;
  /** 空 = 用日志源的连接 */
  connection: string;
  /** CH:库.表;ES:逗号分隔的索引 / 别名 / data stream */
  target: string;
  /** 全部 AND;空 = 整张表 / 整个索引都属于这个集群 */
  conditions: LogBindingCondition[];
  note: string;
}

export interface LogCluster {
  env: string;
  note: string;
  bindings: LogBinding[];
}

export interface LogSourcesConfig {
  enabled: boolean;
  /** 网络架构说明(Markdown) */
  architecture: string;
  connections: LogConnection[];
  correlation_keys: LogCorrelationKey[];
  sources: LogSource[];
  clusters: LogCluster[];
}

/** KubeDoor 已登记的集群(k8s_agent_status),按 env 排序 */
export interface LogKnownCluster {
  env: string;
  aliases: string[];
  online: boolean;
}

export interface LogSemanticFieldMeta {
  key: LogSemanticField;
  label: string;
}

/** GET / PUT 的返回 */
export interface LogSourcesSettings extends LogSourcesConfig {
  clusters_known: LogKnownCluster[];
  semantic_fields: LogSemanticFieldMeta[];
  /** 内置打码名 */
  mask_builtin: string[];
  updated_by?: string | null;
  updated_at?: string | null;
}

/** 提交的配置:传了的顶层键整体替换,没传的沿用(右上角开关只传 enabled) */
export type LogSourcesConfigInput = Partial<LogSourcesConfig>;

/** 测试连接的结果:ClickHouse 和 Elasticsearch 各自的字段 */
export interface LogConnectionTest {
  type: LogConnectionType;
  version?: string;
  /** ClickHouse */
  timezone?: string;
  user?: string;
  /** ClickHouse 的 readonly 设置:0 = 不是只读账号 */
  readonly?: number | string | boolean | null;
  clusters?: string[];
  /** Elasticsearch */
  distribution?: string;
  cluster_name?: string;
  license?: string;
  /** 版本 ≥ 8.14 且不是 OpenSearch */
  esql?: boolean;
}

export interface LogScanColumn {
  name: string;
  type: string;
  /** Elasticsearch 运行时字段（没有索引，按它过滤、关联很慢） */
  runtime?: boolean;
}

/** 扫描识别出的 ID 列 */
export interface LogScanIdField {
  field: string;
  shape: string;
  /** Elasticsearch 运行时字段 */
  runtime?: boolean;
}

/** 扫描到的一个表 / 索引 */
export interface LogScanTarget {
  target: string;
  /** ClickHouse Distributed 表对应的本地表 */
  local?: string;
  cluster?: string;
  rows?: number | null;
  bytes?: number | null;
  first?: string | null;
  last?: string | null;
  retention_days?: number | null;
  sorting_key?: string;
  partition_key?: string;
  /** 低基数字段的 topK:字段名 → 取值 */
  values?: Record<string, (string | number)[]>;
  /** 样本(打码、每个值截断 300),只有组里第一个目标有 */
  samples?: Record<string, unknown>[];
}

/** 字段相同的一组表 / 索引 */
export interface LogScanGroup {
  ref: string;
  name_guess: string;
  kind_guess: LogSourceKind;
  columns: LogScanColumn[];
  /** 语义字段的猜测(含 time 和 duration_unit) */
  fields_guess: Partial<Record<LogSemanticField | "time", string>> & {
    duration_unit?: LogDurationUnit;
  };
  cluster_field_guess?: string;
  id_fields?: LogScanIdField[];
  text_ids?: { text: string; shape: string; count: number }[];
  meta_description?: string;
  targets: LogScanTarget[];
}

/** 绑定建议:exact 取值和集群名相同、alias 和别名相同、token 名称相近、table 一表一集群 */
export interface LogSuggestedBinding {
  env: string;
  group: string;
  target: string;
  conditions: LogBindingCondition[];
  match: "exact" | "alias" | "token" | "table";
}

export interface LogScanResult {
  connection: {
    id: string;
    type: LogConnectionType;
    version?: string;
    timezone?: string;
  };
  /** 到点(90 秒)返回已扫到的 */
  partial: boolean;
  elapsed_ms: number;
  notes: string[];
  groups: LogScanGroup[];
  suggested_bindings: LogSuggestedBinding[];
  clusters: { env: string; aliases: string[] }[];
}

/** 预览:按集群绑定查最近 15 分钟最新 5 条 */
export interface LogPreviewResult {
  ok: boolean;
  rows: Record<string, unknown>[];
  total?: number | null;
  elapsed_ms?: number;
  /** 实际执行的 SQL / DSL(参数已代入) */
  query?: string | Record<string, unknown> | null;
  /** 配置里引用了但实际不存在的字段 */
  schema_problems?: unknown[];
  error?: string | { code?: string; message?: string } | null;
}

/** 验证关联字段:from 日志源的取值在 to 日志源里查到了几个 */
export interface LogVerifyPair {
  from: string;
  to: string;
  sampled: number;
  matched: number;
  examples: string[];
  /** 到 to 里查的时候出错(例如字段不存在) */
  error?: string;
}

export interface LogVerifyResult {
  pairs: LogVerifyPair[];
  notes: string[];
}

export const getLogSourcesConfig = () =>
  http.request<Result<LogSourcesSettings>>("get", "/api/admin/log-sources");

/** 保存配置;没传的顶层键沿用原值(右上角开关只传 enabled) */
export const saveLogSourcesConfig = (data: LogSourcesConfigInput) =>
  http.request<Result<LogSourcesSettings>>("put", "/api/admin/log-sources", {
    data
  });

/** 测试连接:用页面上还没保存的配置(密码留空用已保存的);timeout 是浏览器等待的毫秒数 */
export const testLogConnection = (
  config: LogSourcesConfigInput,
  connection: string,
  timeout = 130000
) =>
  http.request<Result<LogConnectionTest>>(
    "post",
    "/api/admin/log-sources/test",
    { data: { config, connection } },
    { timeout }
  );

/** 扫描连接里的表 / 索引,给出日志源和绑定建议;include / exclude 是 glob,空时不传(用后端默认的排除) */
export const scanLogConnection = (
  config: LogSourcesConfigInput,
  connection: string,
  include: string[] = [],
  exclude: string[] = []
) =>
  http.request<Result<LogScanResult>>(
    "post",
    "/api/admin/log-sources/scan",
    {
      data: {
        config,
        connection,
        ...(include.length ? { include } : {}),
        ...(exclude.length ? { exclude } : {})
      }
    },
    // 后端整体限时 90 秒
    { timeout: 120000 }
  );

/** 按这个集群的绑定查最近 15 分钟最新 5 条 */
export const previewLogBinding = (
  config: LogSourcesConfigInput,
  env: string,
  source: string,
  timeout = 130000
) =>
  http.request<Result<LogPreviewResult>>(
    "post",
    "/api/admin/log-sources/preview",
    { data: { config, env, source } },
    { timeout }
  );

/** 验证关联字段:各日志源抽取值到其他日志源里查 */
export const verifyLogCorrelation = (
  config: LogSourcesConfigInput,
  env: string,
  key: string
) =>
  http.request<Result<LogVerifyResult>>(
    "post",
    "/api/admin/log-sources/verify",
    { data: { config, env, key } },
    { timeout: 120000 }
  );
