// 历史日志配置页的纯函数：表单 ↔ 提交数据、校验（规则同 master logsearch/config.py，约定 §2）、
// 集群列表、扫描结果的展示、按规则生成草稿、草稿合并进表单、「AI 生成说明」的请求体。
// 单测 tests/log-sources.test.cjs 用 esbuild 单文件编译加载，这里只能 import type，不能 import 别的模块。
import type {
  CodeInfoHeader,
  LogAuthType,
  LogBinding,
  LogBindingCondition,
  LogCluster,
  LogConditionOp,
  LogConnection,
  LogConnectionTest,
  LogConnectionType,
  LogCorrelationKey,
  LogDurationUnit,
  LogKnownCluster,
  LogLevelKey,
  LogScanGroup,
  LogScanIdField,
  LogScanResult,
  LogScanTarget,
  LogSemanticField,
  LogSource,
  LogSourceCorrelation,
  LogSourceKind,
  LogSourcesConfigInput,
  LogSourcesSettings,
  LogVerifyResult
} from "@/api/system";
import type {
  LogDescribeRequest,
  LogDescribeResult,
  LogDraftBinding,
  LogDraftSource
} from "@/api/ai";

// ---------------------------------------------------------------------------
// 常量（和 master 的校验一致，改了要两边一起改）
// ---------------------------------------------------------------------------
export const ID_MAX_LENGTH = 32;
export const NAME_MAX_LENGTH = 50;
export const URL_MAX_LENGTH = 2048;
export const USERNAME_MAX_LENGTH = 128;
export const MAX_HEADERS = 10;
export const HEADER_VALUE_MAX_LENGTH = 4096;
export const MIN_TIMEOUT = 5;
export const MAX_TIMEOUT = 120;
export const DEFAULT_TIMEOUT = 30;
export const MIN_CONCURRENCY = 1;
export const MAX_CONCURRENCY = 20;
export const DEFAULT_CONCURRENCY = 4;
export const DEFAULT_MAX_ROWS = 1000000000;
/** 单次扫描行数上限的最大值（master MAX_ROWS_TO_READ） */
export const MAX_ROWS_LIMIT = 10 ** 13;
export const PASSWORD_MAX_LENGTH = 256;
export const API_KEY_MAX_LENGTH = 1024;
/** 数量上限（同 master logsearch/config.py） */
export const MAX_CONNECTIONS = 20;
export const MAX_KEYS = 50;
export const MAX_SOURCES = 200;
export const MAX_CLUSTERS = 500;
export const MAX_BINDINGS = 50;
export const KEY_LABEL_MAX_LENGTH = 30;
export const KEY_DESCRIPTION_MAX_LENGTH = 500;
export const PATTERN_MAX_LENGTH = 200;
export const SOURCE_DESCRIPTION_MAX_LENGTH = 8000;
export const FIELD_NAME_MAX_LENGTH = 200;
export const MAX_LEVEL_VALUES = 20;
export const LEVEL_VALUE_MAX_LENGTH = 50;
export const MAX_CORRELATIONS = 10;
export const CORRELATION_TEXT_MAX_LENGTH = 100;
export const MAX_COLUMNS = 300;
export const COLUMN_DESCRIPTION_MAX_LENGTH = 300;
export const MAX_DEFAULT_FIELDS = 50;
export const MAX_MASK_FIELDS = 50;
export const MAX_MASK_PATTERNS = 20;
export const ENV_MAX_LENGTH = 100;
export const CLUSTER_NOTE_MAX_LENGTH = 4000;
export const MAX_CONDITIONS = 5;
export const MAX_IN_VALUES = 50;
export const CONDITION_VALUE_MAX_LENGTH = 200;
export const BINDING_NOTE_MAX_LENGTH = 2000;
export const ARCHITECTURE_MAX_LENGTH = 20000;
export const ES_MAX_TARGETS = 20;
export const CH_DATABASE_MAX_LENGTH = 64;
/** 整份配置序列化后的上限（字符数；页面按提交的内容估算，master 按存库格式精确检查） */
export const CONFIG_MAX_CHARS = 1024 * 1024;
/** 「AI 生成说明」的补充说明 */
export const HINT_MAX_LENGTH = 4000;
/** 「AI 生成说明」的请求体上限：nginx 对 /api/ai/ 限制 2MB，留余量 */
export const DESCRIBE_MAX_BYTES = 1.5 * 1024 * 1024;
export const DESCRIBE_MAX_TARGETS = 20;
/** kubedoor-ai 一次最多给多少组生成说明 */
export const DESCRIBE_MAX_GROUPS = 100;
export const DESCRIBE_MAX_VALUES = 10;
export const DESCRIBE_MAX_SAMPLES = 2;

/** 帮助文档（只读账号、各种日志源的配置示例都在里面） */
export const HELP_URL = `https://github.com/CassInfra/KubeDoor/blob/main/help/${encodeURIComponent("历史日志配置说明.md")}`;
/** Agent 管理页（维护集群别名） */
export const AGENT_PAGE = "/workbench/index";
/** 链路追踪配置页（关联字段里的链路 ID 接着查链路） */
export const TRACING_PAGE = "/system/tracing";

export const TYPE_OPTIONS: { value: LogConnectionType; label: string }[] = [
  { value: "clickhouse", label: "ClickHouse" },
  { value: "elasticsearch", label: "Elasticsearch" }
];
const TYPE_VALUES = TYPE_OPTIONS.map(item => item.value);
export const typeLabel = (type: string) =>
  TYPE_OPTIONS.find(item => item.value === type)?.label ?? type;

/** Elasticsearch 的认证方式；ClickHouse 固定用户名密码 */
export const ES_AUTH_OPTIONS: { value: LogAuthType; label: string }[] = [
  { value: "none", label: "无" },
  { value: "basic", label: "用户名密码" },
  { value: "api_key", label: "API Key" }
];
const AUTH_VALUES = ES_AUTH_OPTIONS.map(item => item.value);

export const KIND_OPTIONS: {
  value: LogSourceKind;
  label: string;
  hint: string;
}[] = [
  {
    value: "access",
    label: "访问日志",
    hint: "每行是一次请求：状态码、路径、耗时（入口网关、nginx 等）"
  },
  {
    value: "app",
    label: "应用日志",
    hint: "应用输出的日志：正文、级别、Pod"
  },
  { value: "other", label: "其他", hint: "" }
];
const KIND_VALUES = KIND_OPTIONS.map(item => item.value);
export const kindLabel = (kind: string) =>
  KIND_OPTIONS.find(item => item.value === kind)?.label ?? kind;

export const DURATION_UNITS: { value: LogDurationUnit; label: string }[] = [
  { value: "ms", label: "毫秒（ms）" },
  { value: "s", label: "秒（s）" },
  { value: "us", label: "微秒（us）" },
  { value: "ns", label: "纳秒（ns）" }
];
const DURATION_VALUES = DURATION_UNITS.map(item => item.value);

export const LEVEL_KEYS: LogLevelKey[] = ["error", "warn", "info", "debug"];
export const LEVEL_LABELS: Record<LogLevelKey, string> = {
  error: "错误",
  warn: "警告",
  info: "信息",
  debug: "调试"
};
/** 级别取值输入框里的示例 */
export const LEVEL_EXAMPLES: Record<LogLevelKey, string> = {
  error: "ERROR、FATAL",
  warn: "WARN、WARNING",
  info: "INFO",
  debug: "DEBUG、TRACE"
};

export const CONDITION_OPS: { value: LogConditionOp; label: string }[] = [
  { value: "in", label: "属于（∈）" },
  { value: "eq", label: "等于（=）" },
  { value: "ne", label: "不等于（≠）" },
  { value: "not_in", label: "不属于（∉）" },
  { value: "prefix", label: "前缀是" }
];
const OP_VALUES = CONDITION_OPS.map(item => item.value);
/** in / not_in 可以有多个值（1~50），其余恰好一个 */
export const isMultiOp = (op: string) => op === "in" || op === "not_in";

/** 绑定建议的匹配方式 */
export const MATCH_LABELS: Record<string, string> = {
  exact: "取值和集群名相同",
  alias: "取值和集群别名相同",
  token: "名称相近，请确认",
  table: "按表名对应（一表一集群）"
};

export interface SemanticMeta {
  key: LogSemanticField;
  label: string;
  /** 页面上的说明 */
  hint: string;
}

/** 语义字段（约定 §2.4）：顺序、中文名以 GET 返回的为准，这里是兜底 */
export const SEMANTIC_KEYS: LogSemanticField[] = [
  "message",
  "level",
  "namespace",
  "pod",
  "service",
  "status",
  "duration",
  "method",
  "path",
  "host",
  "client_ip"
];

export const SEMANTIC_META: Record<
  LogSemanticField,
  { label: string; hint: string }
> = {
  message: {
    label: "正文",
    hint: "关键字搜索、报错归类、正文里的关联 ID、上下文都用它"
  },
  level: {
    label: "级别",
    hint: "按 error / warn 等过滤，取值见下方「级别取值」"
  },
  namespace: { label: "命名空间", hint: "按命名空间过滤" },
  pod: { label: "Pod", hint: "按 Pod、Deployment 过滤，看某条日志的上下文" },
  service: { label: "服务 / 应用", hint: "统计时常用的分组字段" },
  status: { label: "状态码", hint: "访问日志的 HTTP 状态码" },
  duration: { label: "耗时", hint: "访问日志的请求耗时，单位见下方" },
  method: { label: "请求方法", hint: "GET、POST 等" },
  path: { label: "路径", hint: "请求路径" },
  host: { label: "域名 / Host", hint: "请求的域名" },
  client_ip: { label: "客户端 IP", hint: "发起请求的客户端地址" }
};

/** GET 返回的语义字段（顺序即显示顺序）；服务端没返回的用内置的补 */
export function semanticFields(
  data?: Partial<LogSourcesSettings> | null
): SemanticMeta[] {
  const list: SemanticMeta[] = [];
  for (const item of data?.semantic_fields ?? []) {
    const key = item?.key;
    if (!SEMANTIC_META[key] || list.some(row => row.key === key)) continue;
    list.push({
      key,
      label: item.label || SEMANTIC_META[key].label,
      hint: SEMANTIC_META[key].hint
    });
  }
  for (const key of SEMANTIC_KEYS)
    if (!list.some(row => row.key === key))
      list.push({ key, ...SEMANTIC_META[key] });
  return list;
}

export const semanticLabel = (key: string) =>
  SEMANTIC_META[key as LogSemanticField]?.label ?? key;

/** 内置打码的名称（约定 §8），页面上展示用；以 GET 返回的为准 */
export const BUILTIN_MASK_NAMES = [
  "authorization",
  "proxy-authorization",
  "cookie",
  "set-cookie",
  "x-api-key",
  "api-key",
  "apikey",
  "api_key",
  "x-auth-token",
  "auth-token",
  "access_token",
  "refresh_token",
  "id_token",
  "token",
  "password",
  "passwd",
  "pwd",
  "secret",
  "client_secret",
  "private_key",
  "session",
  "sessionid",
  "jsessionid",
  "sign",
  "signature",
  "ticket",
  "credential",
  "credentials",
  "access_key",
  "accesskey",
  "secret_key",
  "secretkey",
  "passphrase"
];

export const builtinMaskNames = (data?: Partial<LogSourcesSettings> | null) =>
  data?.mask_builtin?.length ? data.mask_builtin : BUILTIN_MASK_NAMES;

// ---------------------------------------------------------------------------
// 表单
// ---------------------------------------------------------------------------
export interface HeaderRow {
  id: number;
  name: string;
  /** 只写：留空表示沿用已保存的同名请求头 */
  value: string;
}

export interface ConnectionForm {
  /** v-for 的 key；编辑对话框改的是副本，确定时按它写回 */
  rowId: number;
  /** 已保存过（GET 返回的）：ID 不能改，密码按 ID 沿用 */
  saved: boolean;
  id: string;
  name: string;
  type: LogConnectionType;
  url: string;
  auth_type: LogAuthType;
  username: string;
  /** 只写：留空表示沿用已保存的密码 */
  password: string;
  has_password: boolean;
  /** 点了「清除」：保存、测试时带 clear_password: true（填的密码不提交） */
  clear_password: boolean;
  /** 只写：留空表示沿用已保存的 API Key */
  api_key: string;
  has_api_key: boolean;
  clear_api_key: boolean;
  headers: HeaderRow[];
  /** 已保存过值的请求头名称（小写）：值留空时沿用 */
  savedHeaders: string[];
  timeout: number;
  tls_verify: boolean;
  max_concurrency: number;
  max_rows_to_read: number;
  allow_raw: boolean;
}

export interface KeyForm {
  rowId: number;
  key: string;
  label: string;
  description: string;
  pattern: string;
  trace: boolean;
}

/** 日志源里登记关联字段的方式：列（field）或正文写法（text） */
export type CorrelationMode = "field" | "text";

export interface SourceCorrelationForm {
  rowId: number;
  key: string;
  mode: CorrelationMode;
  field: string;
  text: string;
}

export interface ColumnForm {
  rowId: number;
  name: string;
  description: string;
}

export interface SourceForm {
  rowId: number;
  id: string;
  name: string;
  enabled: boolean;
  connection: string;
  kind: LogSourceKind;
  description: string;
  time_field: string;
  /** 语义字段 → 实际字段名；每个语义字段都有（空串 = 没配置），不认识的键原样保留 */
  fields: Record<string, string>;
  duration_unit: LogDurationUnit;
  level_values: Record<LogLevelKey, string[]>;
  correlations: SourceCorrelationForm[];
  columns: ColumnForm[];
  default_fields: string[];
  mask_fields: string[];
  mask_patterns: string[];
  default_target: string;
  cluster_field: string;
}

export interface ConditionForm {
  field: string;
  op: LogConditionOp;
  values: (string | number)[];
}

export interface BindingForm {
  rowId: number;
  source: string;
  connection: string;
  target: string;
  conditions: ConditionForm[];
  note: string;
}

export interface ClusterForm {
  env: string;
  note: string;
  bindings: BindingForm[];
}

export interface LogSourcesForm {
  enabled: boolean;
  architecture: string;
  connections: ConnectionForm[];
  correlation_keys: KeyForm[];
  sources: SourceForm[];
  clusters: ClusterForm[];
}

let rowSeq = 0;
/** 表格行、请求头的 key（v-for 用） */
export const nextRowId = () => ++rowSeq;

const text = (value: unknown): string =>
  typeof value === "string" ? value : value == null ? "" : String(value);

const numberOr = (value: unknown, fallback: number): number =>
  typeof value === "number" && Number.isFinite(value) ? value : fallback;

const stringList = (list: unknown): string[] =>
  Array.isArray(list)
    ? list
        .filter(item => typeof item === "string" || typeof item === "number")
        .map(String)
    : [];

const conditionValues = (list: unknown): (string | number)[] =>
  Array.isArray(list)
    ? list.filter(
        item =>
          typeof item === "string" ||
          (typeof item === "number" && Number.isFinite(item))
      )
    : [];

/** GET 返回的连接 → 表单（密码、请求头的值不回显）；saved：已保存过 */
export function connectionForm(
  data: Partial<LogConnection> | null | undefined,
  saved: boolean
): ConnectionForm {
  const type: LogConnectionType =
    data?.type === "elasticsearch" ? "elasticsearch" : "clickhouse";
  const auth = AUTH_VALUES.includes(data?.auth_type)
    ? data.auth_type
    : type === "clickhouse"
      ? "basic"
      : "none";
  const headers = (data?.headers ?? []).filter(item => item?.name);
  return {
    rowId: nextRowId(),
    saved,
    id: text(data?.id),
    name: text(data?.name),
    type,
    url: text(data?.url),
    auth_type: type === "clickhouse" ? "basic" : auth,
    username: text(data?.username),
    password: "",
    has_password: !!data?.has_password,
    clear_password: false,
    api_key: "",
    has_api_key: !!data?.has_api_key,
    clear_api_key: false,
    headers: headers.map(item => ({
      id: nextRowId(),
      name: item.name,
      value: ""
    })),
    // 只回显名称的请求头都是存过值的（master 不保存没有值的请求头）
    savedHeaders: headers
      .filter(item => item.has_value !== false)
      .map(item => item.name.toLowerCase()),
    timeout: numberOr(data?.timeout, DEFAULT_TIMEOUT),
    tls_verify: data?.tls_verify !== false,
    max_concurrency: numberOr(data?.max_concurrency, DEFAULT_CONCURRENCY),
    max_rows_to_read: numberOr(data?.max_rows_to_read, DEFAULT_MAX_ROWS),
    allow_raw: data?.allow_raw !== false
  };
}

/** 新建连接：ID 取 ck / es 不重复的，名称给个默认值 */
export function emptyConnection(
  type: LogConnectionType,
  used: Iterable<string>
): ConnectionForm {
  const conn = connectionForm({ type }, false);
  conn.id = uniqueId(type === "clickhouse" ? "ck" : "es", used, "conn");
  conn.name = type === "clickhouse" ? "日志 ClickHouse" : "日志 Elasticsearch";
  return conn;
}

export const keyForm = (data?: Partial<LogCorrelationKey> | null): KeyForm => ({
  rowId: nextRowId(),
  key: text(data?.key),
  label: text(data?.label),
  description: text(data?.description),
  pattern: text(data?.pattern),
  trace: !!data?.trace
});

export const correlationForm = (
  data?: Partial<LogSourceCorrelation> | null
): SourceCorrelationForm => ({
  rowId: nextRowId(),
  key: text(data?.key),
  mode: data?.text ? "text" : "field",
  field: text(data?.field),
  text: text(data?.text)
});

export function sourceForm(data?: Partial<LogSource> | null): SourceForm {
  const fields: Record<string, string> = Object.fromEntries(
    SEMANTIC_KEYS.map(key => [key, ""])
  );
  for (const [key, value] of Object.entries(data?.fields ?? {}))
    if (typeof value === "string") fields[key] = value;
  const levels = Object.fromEntries(
    LEVEL_KEYS.map(key => [key, stringList(data?.level_values?.[key])])
  ) as Record<LogLevelKey, string[]>;
  return {
    rowId: nextRowId(),
    id: text(data?.id),
    name: text(data?.name),
    enabled: data?.enabled !== false,
    connection: text(data?.connection),
    kind: KIND_VALUES.includes(data?.kind) ? data.kind : "other",
    description: text(data?.description),
    time_field: text(data?.time_field),
    fields,
    duration_unit: DURATION_VALUES.includes(data?.duration_unit)
      ? data.duration_unit
      : "ms",
    level_values: levels,
    correlations: (data?.correlations ?? []).map(item => correlationForm(item)),
    columns: (data?.columns ?? []).map(item => ({
      rowId: nextRowId(),
      name: text(item?.name),
      description: text(item?.description)
    })),
    default_fields: stringList(data?.default_fields),
    mask_fields: stringList(data?.mask_fields),
    mask_patterns: stringList(data?.mask_patterns),
    default_target: text(data?.default_target),
    cluster_field: text(data?.cluster_field)
  };
}

/** 新建日志源：ID 不重复，连接默认第一个 */
export function emptySource(form: LogSourcesForm, connection = ""): SourceForm {
  const source = sourceForm(null);
  source.id = uniqueId(
    "logs",
    form.sources.map(item => item.id)
  );
  source.kind = "app";
  source.connection = connection || form.connections[0]?.id || "";
  return source;
}

const cloneConditions = (list: unknown): ConditionForm[] =>
  (Array.isArray(list) ? list : []).map(cond => ({
    field: text(cond?.field),
    op: OP_VALUES.includes(cond?.op) ? cond.op : "in",
    values: conditionValues(cond?.values)
  }));

export const bindingForm = (
  data?: Partial<LogBinding> | null
): BindingForm => ({
  rowId: nextRowId(),
  source: text(data?.source),
  connection: text(data?.connection),
  target: text(data?.target),
  conditions: cloneConditions(data?.conditions),
  note: text(data?.note)
});

export const clusterForm = (
  data?: Partial<LogCluster> | null
): ClusterForm => ({
  env: text(data?.env),
  note: text(data?.note),
  bindings: (data?.bindings ?? []).map(bindingForm)
});

export const emptyCluster = (env: string): ClusterForm => ({
  env,
  note: "",
  bindings: []
});

/** GET / PUT 返回的配置 → 页面表单（密码、请求头的值不回显） */
export function formFromSettings(
  data?: Partial<LogSourcesSettings> | null
): LogSourcesForm {
  return {
    enabled: !!data?.enabled,
    architecture: text(data?.architecture),
    connections: (data?.connections ?? []).map(item =>
      connectionForm(item, true)
    ),
    correlation_keys: (data?.correlation_keys ?? []).map(keyForm),
    sources: (data?.sources ?? []).map(sourceForm),
    clusters: (data?.clusters ?? []).map(clusterForm)
  };
}

export const emptyForm = () => formFromSettings(null);

/** 深拷贝（编辑对话框 / 抽屉改副本，确定时写回）；保留 rowId */
export const cloneDeep = <T>(value: T): T =>
  JSON.parse(JSON.stringify(value)) as T;

// ---------------------------------------------------------------------------
// 表单 → 提交数据
// ---------------------------------------------------------------------------
/** 去掉首尾空白和末尾的 /（同 master） */
export const normalizeUrl = (value: string) =>
  (value ?? "").trim().replace(/\/+$/, "");

/** 表 / 索引：去掉逗号两边的空白和空项 */
export const normalizeTarget = (value: string) =>
  (value ?? "")
    .split(",")
    .map(item => item.trim())
    .filter(Boolean)
    .join(",");

/** 去空白、去空、去重（区分大小写），保持顺序 */
export function cleanNames(list: unknown): string[] {
  const result: string[] = [];
  for (const item of stringList(list)) {
    const name = item.trim();
    if (name && !result.includes(name)) result.push(name);
  }
  return result;
}

/** 去空白、去空、去重（不区分大小写，打码字段按小写比较），保持顺序 */
function cleanCaseless(list: unknown): string[] {
  const result: string[] = [];
  const seen = new Set<string>();
  for (const item of stringList(list)) {
    const name = item.trim();
    if (name && !seen.has(name.toLowerCase())) {
      seen.add(name.toLowerCase());
      result.push(name);
    }
  }
  return result;
}

const usedHeaders = (rows: HeaderRow[]) =>
  rows.filter(row => row.name.trim() || row.value);

/** 名称、值都空的行不算；只填了 key 的也算（校验时提示缺什么） */
const usedCorrelations = (rows: SourceCorrelationForm[]) =>
  rows.filter(row => row.key || row.field.trim() || row.text.trim());

/** 只有写了说明的字段才提交（「从扫描结果带出」的字段没填说明时忽略） */
const usedColumns = (rows: ColumnForm[]) =>
  rows.filter(row => row.description.trim());

/** 实际使用的认证方式：ClickHouse 固定用户名密码 */
export const authOf = (conn: Pick<ConnectionForm, "type" | "auth_type">) =>
  conn.type === "clickhouse" ? "basic" : conn.auth_type;

/**
 * 连接 → 提交格式。密码、API Key 留空不传（沿用已保存的），点了「清除」带 clear_*；
 * 只提交当前认证方式用到的密钥，请求头的值留空传 null（沿用）。
 */
export function connectionPayload(conn: ConnectionForm): LogConnection {
  const auth = authOf(conn);
  const payload: LogConnection = {
    id: conn.id.trim(),
    name: conn.name.trim(),
    type: conn.type,
    url: normalizeUrl(conn.url),
    auth_type: auth,
    username: auth === "basic" ? conn.username.trim() : "",
    headers: usedHeaders(conn.headers).map(
      (row): CodeInfoHeader => ({
        name: row.name.trim(),
        value: row.value || null
      })
    ),
    timeout: conn.timeout,
    tls_verify: conn.tls_verify,
    max_concurrency: conn.max_concurrency,
    max_rows_to_read: conn.max_rows_to_read,
    allow_raw: conn.allow_raw
  };
  if (auth === "basic") {
    if (conn.clear_password) payload.clear_password = true;
    else if (conn.password) payload.password = conn.password;
  } else if (auth === "api_key") {
    if (conn.clear_api_key) payload.clear_api_key = true;
    else if (conn.api_key.trim()) payload.api_key = conn.api_key.trim();
  }
  return payload;
}

export const keyPayload = (row: KeyForm): LogCorrelationKey => ({
  key: row.key.trim(),
  label: row.label.trim(),
  description: row.description.trim(),
  pattern: row.pattern.trim(),
  trace: !!row.trace
});

export function sourcePayload(source: SourceForm): LogSource {
  const fields: Record<string, string> = {};
  for (const [key, value] of Object.entries(source.fields ?? {})) {
    const name = text(value).trim();
    if (name) fields[key] = name;
  }
  const levels: Partial<Record<LogLevelKey, string[]>> = {};
  for (const key of LEVEL_KEYS) {
    const list = cleanNames(source.level_values?.[key]);
    if (list.length) levels[key] = list;
  }
  return {
    id: source.id.trim(),
    name: source.name.trim(),
    enabled: !!source.enabled,
    connection: source.connection,
    kind: source.kind,
    description: source.description.trim(),
    time_field: source.time_field.trim(),
    fields,
    duration_unit: source.duration_unit,
    level_values: levels,
    correlations: usedCorrelations(source.correlations).map(row =>
      row.mode === "text"
        ? { key: row.key, text: row.text.trim() }
        : { key: row.key, field: row.field.trim() }
    ),
    columns: usedColumns(source.columns).map(row => ({
      name: row.name.trim(),
      description: row.description.trim()
    })),
    default_fields: cleanNames(source.default_fields),
    mask_fields: cleanCaseless(source.mask_fields),
    mask_patterns: cleanNames(source.mask_patterns),
    default_target: normalizeTarget(source.default_target),
    cluster_field: source.cluster_field.trim()
  };
}

/** 条件的值：字符串去空白、去空、去重；数字原样 */
export function conditionPayload(cond: ConditionForm): LogBindingCondition {
  const values: (string | number)[] = [];
  for (const raw of conditionValues(cond.values)) {
    const value = typeof raw === "string" ? raw.trim() : raw;
    if (value === "" || values.includes(value)) continue;
    values.push(value);
  }
  return { field: text(cond.field).trim(), op: cond.op, values };
}

export const bindingPayload = (binding: BindingForm): LogBinding => ({
  source: binding.source,
  connection: binding.connection,
  target: normalizeTarget(binding.target),
  conditions: binding.conditions.map(conditionPayload),
  note: binding.note.trim()
});

export const clusterPayload = (cluster: ClusterForm): LogCluster => ({
  env: cluster.env.trim(),
  note: cluster.note.trim(),
  bindings: cluster.bindings.map(bindingPayload)
});

/** 没有说明也没有绑定的集群不提交（点开一个集群看看不算修改） */
const usedCluster = (cluster: LogCluster) =>
  !!cluster.note || cluster.bindings.length > 0;

/** 表单 → 保存的配置（不含 enabled：右上角的开关单独保存） */
export function formPayload(form: LogSourcesForm): LogSourcesConfigInput {
  return {
    architecture: form.architecture.trim(),
    connections: form.connections.map(connectionPayload),
    correlation_keys: form.correlation_keys.map(keyPayload),
    sources: form.sources.map(sourcePayload),
    clusters: form.clusters.map(clusterPayload).filter(usedCluster)
  };
}

/** 判断有没有未保存的修改：和加载 / 保存时的快照比较 */
export const payloadKey = (form: LogSourcesForm) =>
  JSON.stringify(formPayload(form));

/**
 * 测试连接、扫描用的配置：只带这一个连接（表单其他部分没填完也能测），不看启用状态；
 * 密码留空时 master 按连接 ID 用已保存的
 */
export const connectionTestPayload = (
  conn: ConnectionForm | null | undefined
): LogSourcesConfigInput => ({
  enabled: false,
  architecture: "",
  connections: conn ? [connectionPayload(conn)] : [],
  correlation_keys: [],
  sources: [],
  clusters: []
});

/** 表单里这个 ID 的连接的测试 / 扫描配置 */
export const testPayload = (
  form: LogSourcesForm,
  connection: string
): LogSourcesConfigInput =>
  connectionTestPayload(form.connections.find(item => item.id === connection));

/** 一组日志源用到的连接、关联字段，拼成一份能单独通过校验的配置 */
function partialPayload(
  form: LogSourcesForm,
  sources: SourceForm[],
  clusters: LogCluster[]
): LogSourcesConfigInput {
  const connections = new Set<string>(sources.map(item => item.connection));
  for (const cluster of clusters)
    for (const binding of cluster.bindings)
      if (binding.connection) connections.add(binding.connection);
  const keys = new Set<string>(
    sources
      .flatMap(item => usedCorrelations(item.correlations))
      .map(row => row.key)
  );
  return {
    enabled: false,
    architecture: "",
    connections: form.connections
      .filter(item => connections.has(item.id))
      .map(connectionPayload),
    correlation_keys: form.correlation_keys
      .filter(item => keys.has(item.key.trim()))
      .map(keyPayload),
    sources: sources.map(sourcePayload),
    clusters
  };
}

/** 预览用的配置：只带这个集群的这条绑定和它用到的日志源、连接、关联字段（其他部分没填完也能预览） */
export function previewPayload(
  form: LogSourcesForm,
  env: string,
  source: string
): LogSourcesConfigInput {
  const item = form.sources.find(row => row.id === source);
  const binding = form.clusters
    .find(row => row.env === env)
    ?.bindings.find(row => row.source === source);
  if (!item || !binding) return partialPayload(form, [], []);
  return partialPayload(
    form,
    [item],
    [{ env, note: "", bindings: [bindingPayload(binding)] }]
  );
}

/** 验证关联字段用的配置：这个集群里登记了这个关联字段的日志源和它们的绑定 */
export function verifyPayload(
  form: LogSourcesForm,
  env: string,
  key: string
): LogSourcesConfigInput {
  const bindings = form.clusters.find(row => row.env === env)?.bindings ?? [];
  const sources = form.sources.filter(
    item =>
      bindings.some(binding => binding.source === item.id) &&
      usedCorrelations(item.correlations).some(row => row.key === key)
  );
  const ids = new Set(sources.map(item => item.id));
  return partialPayload(form, sources, [
    {
      env,
      note: "",
      bindings: bindings
        .filter(binding => ids.has(binding.source))
        .map(bindingPayload)
    }
  ]);
}

/** 请求头的值留空时沿用已保存的值（名称不区分大小写） */
export const keepsSavedHeader = (conn: ConnectionForm, row: HeaderRow) =>
  conn.savedHeaders.includes(row.name.trim().toLowerCase());

/** 密码框的提示：将清除 / 已保存 / 没有密码 */
export function passwordPlaceholder(conn: ConnectionForm): string {
  if (conn.clear_password) return "将清除，保存后生效";
  if (conn.has_password) return "已保存，留空表示不修改";
  return conn.type === "clickhouse" ? "没有密码可以不填" : "请输入密码";
}

export function apiKeyPlaceholder(conn: ConnectionForm): string {
  if (conn.clear_api_key) return "将清除，保存后生效";
  if (conn.has_api_key) return "已保存，留空表示不修改";
  return "base64 编码的 id:key，原样粘贴";
}

/** 连接表格里「认证」一列的文字 */
export function authText(conn: ConnectionForm): string {
  const auth = authOf(conn);
  if (auth === "none") return "无";
  if (auth === "api_key")
    return conn.has_api_key || conn.api_key.trim()
      ? "API Key"
      : "API Key（未填）";
  return conn.username.trim()
    ? `用户名密码（${conn.username.trim()}）`
    : conn.type === "clickhouse"
      ? "default 用户"
      : "用户名密码";
}

/** 绑定实际用的连接：绑定里填了用绑定的，否则用日志源的 */
export function bindingConnection(
  form: LogSourcesForm,
  binding: Pick<BindingForm, "source" | "connection">
): ConnectionForm | null {
  const id =
    binding.connection ||
    form.sources.find(item => item.id === binding.source)?.connection ||
    "";
  return form.connections.find(item => item.id === id) ?? null;
}

// ---------------------------------------------------------------------------
// 校验（规则同约定 §2；不合法时 master 也会返回 400 和原因）
// ---------------------------------------------------------------------------
export type SectionId =
  | "connections"
  | "smart"
  | "sources"
  | "clusters"
  | "correlations"
  | "architecture";

export interface FormError {
  section: SectionId;
  text: string;
  /** 出错的集群：页面上选中它，右侧才看得到出错的绑定 */
  env?: string;
  source?: string;
  connection?: string;
}

const ID_PATTERN = /^[a-z][a-z0-9_-]{0,31}$/;
const KEY_PATTERN = /^[a-z][a-z0-9_]{0,31}$/;
const FIELD_PATTERN = /^[A-Za-z_@][A-Za-z0-9_.@-]{0,199}$/;
const HEADER_NAME = /^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,100}$/;
const RESERVED_HEADERS = [
  "host",
  "content-type",
  "content-length",
  "transfer-encoding",
  "connection"
];
const CH_PART = /^[A-Za-z_][A-Za-z0-9_]{0,191}$/;
const ES_PART = /^-?[a-z0-9*][a-z0-9_.*+-]{0,254}$/;

/** 字数：按字符（code point）算，同 master 的 len() */
export const charCount = (value: string) => Array.from(value ?? "").length;

const hasControlChar = (value: string) =>
  Array.from(value ?? "").some(ch => {
    const code = ch.charCodeAt(0);
    return code < 32 || code === 127;
  });

export const byteLength = (value: string) =>
  new TextEncoder().encode(value).length;

/** 日志源、连接的 ID */
export function idError(value: string): string {
  const id = (value ?? "").trim();
  if (!id) return "请填写";
  return ID_PATTERN.test(id)
    ? ""
    : `只能用小写字母、数字、_ 和 -，以小写字母开头，最长 ${ID_MAX_LENGTH} 个字符`;
}

/** 名称类：必填、字数上限、不能换行 */
function nameError(value: string, max: number): string {
  const name = (value ?? "").trim();
  if (!name) return "请填写";
  if (charCount(name) > max) return `最多 ${max} 个字`;
  return hasControlChar(name) ? "不能换行" : "";
}

/** 字段名：^[A-Za-z_@][A-Za-z0-9_.@-]{0,199}$ */
export function fieldNameError(value: string): string {
  const name = (value ?? "").trim();
  if (!name) return "请填写字段名";
  if (name.length > FIELD_NAME_MAX_LENGTH)
    return `${name.slice(0, 40)}… 太长了（最多 ${FIELD_NAME_MAX_LENGTH} 个字符）`;
  return FIELD_PATTERN.test(name)
    ? ""
    : `${name} 不是合法的字段名（只能用字母、数字和 _ . @ -，以字母、_ 或 @ 开头）`;
}

/**
 * 表 / 索引：ClickHouse 写成「库.表」；Elasticsearch 是逗号分隔的索引、别名或 data stream
 * （最多 20 项，可以用 * 通配、- 开头表示排除，不能跨集群、不能是 . 开头的系统索引）
 */
export function targetError(type: LogConnectionType, raw: string): string {
  const target = normalizeTarget(raw);
  if (type === "clickhouse") {
    if (!target) return "请填写表名（库.表）";
    const parts = target.split(".");
    if (parts.length !== 2 || !parts.every(part => CH_PART.test(part)))
      return `${target} 要写成 库.表（字母、数字和下划线，不能以数字开头），例如 logs.ingress_access`;
    if (parts[0].length > CH_DATABASE_MAX_LENGTH)
      return `库名最长 ${CH_DATABASE_MAX_LENGTH} 个字符`;
    return "";
  }
  if (!target) return "请填写索引，例如 app-logs-*";
  const items = target.split(",");
  if (items.length > ES_MAX_TARGETS) return `最多 ${ES_MAX_TARGETS} 个索引`;
  for (const item of items) {
    if (item.includes(":")) return `${item} 是跨集群索引（带 :），不支持`;
    if (item.replace(/^-/, "").startsWith("."))
      return `${item} 是 . 开头的系统索引，不支持`;
    if (!ES_PART.test(item))
      return `${item} 不是合法的索引名（只能用小写字母、数字和 _ . * + -），例如 app-logs-*`;
  }
  return "";
}

/** 地址：http(s)、有主机名、不带查询参数和 #、≤ 2048 */
export function urlError(raw: string, required: boolean): string {
  const value = normalizeUrl(raw);
  if (!value) return required ? "请填写" : "";
  if (value.length > URL_MAX_LENGTH) return `不能超过 ${URL_MAX_LENGTH} 个字符`;
  if (/\s/.test(value) || hasControlChar(value)) return "不能有空格或换行";
  if (!/^https?:\/\//i.test(value)) return "要以 http:// 或 https:// 开头";
  if (value.includes("?") || value.includes("#"))
    return "不能带查询参数（?）或 #";
  try {
    if (!new URL(value).hostname) return "缺少主机名";
  } catch {
    return "格式不正确";
  }
  return "";
}

/** Python 正则的几种写法换成 JavaScript 的再试编译（命名组、开头的内联标志） */
const toJsRegex = (pattern: string) =>
  pattern
    .replace(/^\(\?[aiLmsux]+\)/, "")
    .replace(/\(\?P</g, "(?<")
    .replace(/\(\?P=(\w+)\)/g, "\\k<$1>");

/** 正则：≤ 200 且能编译（按 JavaScript 的语法粗检，master 用 Python re 精确检查） */
export function regexError(pattern: string): string {
  const value = (pattern ?? "").trim();
  if (!value) return "";
  if (value.length > PATTERN_MAX_LENGTH)
    return `最长 ${PATTERN_MAX_LENGTH} 个字符`;
  try {
    new RegExp(toJsRegex(value));
  } catch {
    return "不是合法的正则表达式";
  }
  return "";
}

function rangeError(
  value: number,
  low: number,
  high: number,
  unit = ""
): string {
  return Number.isInteger(value) && value >= low && value <= high
    ? ""
    : `取 ${low}~${high}${unit ? ` ${unit}` : ""} 的整数`;
}

export function headerErrors(conn: ConnectionForm): string[] {
  const errors: string[] = [];
  const rows = usedHeaders(conn.headers);
  if (rows.length > MAX_HEADERS) errors.push(`最多 ${MAX_HEADERS} 个`);
  const seen = new Set<string>();
  for (const row of rows) {
    const name = row.name.trim();
    if (!HEADER_NAME.test(name)) {
      errors.push(
        `名称不合法：${name || "（空）"}（只能用字母、数字和 - _ . 等符号，不能有空格和冒号）`
      );
      continue;
    }
    const lower = name.toLowerCase();
    if (RESERVED_HEADERS.includes(lower))
      errors.push(`${name} 由系统设置，不能自定义`);
    else if (seen.has(lower)) errors.push(`${name} 重复了`);
    seen.add(lower);
    if (!row.value && !keepsSavedHeader(conn, row))
      errors.push(`请填写 ${name} 的值`);
    else if (
      row.value.trim().length > HEADER_VALUE_MAX_LENGTH ||
      hasControlChar(row.value)
    )
      errors.push(
        `${name} 的值不能换行，最长 ${HEADER_VALUE_MAX_LENGTH} 个字符`
      );
  }
  return errors;
}

/** 一个连接的问题，每条形如「地址：要以 http:// 开头」（编辑对话框和保存前的校验共用） */
export function connectionErrors(conn: ConnectionForm): string[] {
  const errors: string[] = [];
  const add = (label: string, problem: string) => {
    if (problem) errors.push(`${label}：${problem}`);
  };
  add("ID", idError(conn.id));
  add("名称", nameError(conn.name, NAME_MAX_LENGTH));
  if (!TYPE_VALUES.includes(conn.type))
    add("类型", "只能是 ClickHouse 或 Elasticsearch");
  add("地址", urlError(conn.url, true));
  const auth = authOf(conn);
  if (!AUTH_VALUES.includes(auth))
    add("认证方式", "只能是无、用户名密码或 API Key");
  if (auth === "basic") {
    const username = conn.username.trim();
    if (
      username.length > USERNAME_MAX_LENGTH ||
      /\s/.test(username) ||
      hasControlChar(username)
    )
      add("账号", `不能有空格，最长 ${USERNAME_MAX_LENGTH} 个字符`);
    else if (conn.type === "elasticsearch" && !username) add("账号", "请填写");
    if (
      !conn.clear_password &&
      (conn.password.length > PASSWORD_MAX_LENGTH ||
        hasControlChar(conn.password))
    )
      add("密码", `不能换行，最长 ${PASSWORD_MAX_LENGTH} 个字符`);
  }
  if (auth === "api_key") {
    const key = conn.api_key.trim();
    if (!key && (!conn.has_api_key || conn.clear_api_key))
      add("API Key", "请填写");
    else if (/\s/.test(key) || hasControlChar(key))
      add("API Key", "不能有空格或换行");
    else if (key.length > API_KEY_MAX_LENGTH)
      add("API Key", `最长 ${API_KEY_MAX_LENGTH} 个字符`);
  }
  for (const problem of headerErrors(conn)) errors.push(`请求头：${problem}`);
  add("超时", rangeError(conn.timeout, MIN_TIMEOUT, MAX_TIMEOUT, "秒"));
  add(
    "并发",
    rangeError(conn.max_concurrency, MIN_CONCURRENCY, MAX_CONCURRENCY)
  );
  if (
    conn.type === "clickhouse" &&
    !(
      Number.isSafeInteger(conn.max_rows_to_read) &&
      conn.max_rows_to_read >= 0 &&
      conn.max_rows_to_read <= MAX_ROWS_LIMIT
    )
  )
    add("单次扫描行数上限", "填 0（不限）或正整数，最大 10 的 13 次方");
  return errors;
}

/** 关联字段的 key：^[a-z][a-z0-9_]{0,31}$ */
export function keyNameError(value: string): string {
  const key = (value ?? "").trim();
  if (!key) return "请填写";
  return KEY_PATTERN.test(key)
    ? ""
    : "只能用小写字母、数字和 _，以小写字母开头，最长 32 个字符";
}

/** 关联字段的取值格式：可空；不能写 ^ $（会嵌进别的正则里用），能编译 */
export function patternError(value: string): string {
  const pattern = (value ?? "").trim();
  if (!pattern) return "";
  if (pattern.startsWith("^") || /(^|[^\\])\$$/.test(pattern))
    return "不要写 ^ 和 $（会嵌进别的正则里用）";
  const error = regexError(pattern);
  if (error) return error;
  try {
    if (new RegExp(`^(?:${toJsRegex(pattern)})$`).test(""))
      return "不能匹配空字符串（例如 .* 要改成 .+）";
  } catch {
    // 单独能编译、套上 ^(?: )$ 后编译不了的写法交给 master 检查
  }
  return "";
}

export function keyErrors(row: KeyForm): string[] {
  const errors: string[] = [];
  const add = (label: string, problem: string) => {
    if (problem) errors.push(`${label}：${problem}`);
  };
  add("Key", keyNameError(row.key));
  add("名称", nameError(row.label, KEY_LABEL_MAX_LENGTH));
  if (charCount(row.description.trim()) > KEY_DESCRIPTION_MAX_LENGTH)
    add("说明", `最多 ${KEY_DESCRIPTION_MAX_LENGTH} 个字`);
  add("取值格式", patternError(row.pattern));
  return errors;
}

/** 正文写法里 {value} 的个数 */
export const valueSlots = (value: string) =>
  (value ?? "").split("{value}").length - 1;

/** 一个日志源的问题，每条形如「时间字段：请填写」（编辑抽屉和保存前的校验共用） */
export function sourceErrors(
  form: LogSourcesForm,
  source: SourceForm
): string[] {
  const errors: string[] = [];
  const add = (label: string, problem: string) => {
    if (problem) errors.push(`${label}：${problem}`);
  };
  add("ID", idError(source.id));
  add("名称", nameError(source.name, NAME_MAX_LENGTH));
  const conn = form.connections.find(item => item.id === source.connection);
  if (!source.connection) add("连接", "请选择");
  else if (!conn) add("连接", `${source.connection} 不存在`);
  if (!KIND_VALUES.includes(source.kind))
    add("类型", "只能是访问日志、应用日志或其他");
  if (charCount(source.description.trim()) > SOURCE_DESCRIPTION_MAX_LENGTH)
    add("说明", `最多 ${SOURCE_DESCRIPTION_MAX_LENGTH} 个字`);
  add(
    "时间字段",
    source.time_field.trim() ? fieldNameError(source.time_field) : "请填写"
  );
  for (const [key, value] of Object.entries(source.fields ?? {}))
    if (text(value).trim())
      add(`${semanticLabel(key)}字段`, fieldNameError(value));
  if (!DURATION_VALUES.includes(source.duration_unit))
    add("耗时单位", "只能是 ms、s、us、ns");
  for (const key of LEVEL_KEYS) {
    const values = cleanNames(source.level_values?.[key]);
    const label = `级别取值 ${key}`;
    if (values.length > MAX_LEVEL_VALUES)
      add(label, `最多 ${MAX_LEVEL_VALUES} 个`);
    const bad = values.find(
      value =>
        charCount(value) > LEVEL_VALUE_MAX_LENGTH || hasControlChar(value)
    );
    if (bad)
      add(
        label,
        `${bad.slice(0, 20)} 太长了（每个最多 ${LEVEL_VALUE_MAX_LENGTH} 个字，不能换行）`
      );
  }

  const correlations = usedCorrelations(source.correlations);
  if (correlations.length > MAX_CORRELATIONS)
    add("关联字段", `最多 ${MAX_CORRELATIONS} 个`);
  const keys = new Set(form.correlation_keys.map(item => item.key.trim()));
  const registered = new Set<string>();
  correlations.forEach((row, index) => {
    const label = row.key
      ? `关联字段 ${row.key}`
      : `第 ${index + 1} 个关联字段`;
    if (row.key && registered.has(row.key)) add(label, "登记了两次");
    registered.add(row.key);
    if (!row.key) add(label, "请选择关联字段");
    else if (!keys.has(row.key))
      add(label, "在「关联字段」卡片里还没有，先在那里添加");
    if (row.mode === "text") {
      const value = row.text.trim();
      const slots = valueSlots(value);
      if (!value) add(label, "请填写正文写法，例如 TID:{value}");
      else if (slots === 0) add(label, "正文写法要包含一个 {value}");
      else if (slots > 1) add(label, "正文写法只能有一个 {value}");
      else if (!value.replace("{value}", "").trim())
        add(label, "正文写法除了 {value} 还要有固定的文字，例如 TID:{value}");
      else if (
        charCount(value) > CORRELATION_TEXT_MAX_LENGTH ||
        hasControlChar(value)
      )
        add(
          label,
          `正文写法最多 ${CORRELATION_TEXT_MAX_LENGTH} 个字，不能换行`
        );
      if (!text(source.fields?.message).trim())
        add(label, "用正文写法要先配置「正文」字段");
    } else {
      add(label, row.field.trim() ? fieldNameError(row.field) : "请填写字段名");
    }
  });

  const columns = usedColumns(source.columns);
  if (columns.length > MAX_COLUMNS) add("字段说明", `最多 ${MAX_COLUMNS} 项`);
  const seen = new Set<string>();
  for (const row of columns) {
    const name = row.name.trim();
    if (!name) {
      add("字段说明", "有一行写了说明但没有字段名");
      continue;
    }
    const error = fieldNameError(name);
    if (error) {
      add("字段说明", error);
      continue;
    }
    if (seen.has(name)) add("字段说明", `${name} 重复了`);
    seen.add(name);
    if (charCount(row.description.trim()) > COLUMN_DESCRIPTION_MAX_LENGTH)
      add(`字段说明 ${name}`, `最多 ${COLUMN_DESCRIPTION_MAX_LENGTH} 个字`);
  }

  const defaults = cleanNames(source.default_fields);
  if (defaults.length > MAX_DEFAULT_FIELDS)
    add("默认返回字段", `最多 ${MAX_DEFAULT_FIELDS} 个`);
  for (const name of defaults) add("默认返回字段", fieldNameError(name));
  const masks = cleanCaseless(source.mask_fields);
  if (masks.length > MAX_MASK_FIELDS)
    add("打码字段", `最多 ${MAX_MASK_FIELDS} 个`);
  for (const name of masks) add("打码字段", fieldNameError(name));
  const patterns = cleanNames(source.mask_patterns);
  if (patterns.length > MAX_MASK_PATTERNS)
    add("打码正则", `最多 ${MAX_MASK_PATTERNS} 个`);
  for (const pattern of patterns) {
    const error = regexError(pattern);
    if (error) add("打码正则", `${pattern.slice(0, 40)} ${error}`);
  }
  if (source.default_target.trim() && conn)
    add("默认表 / 索引", targetError(conn.type, source.default_target));
  if (source.cluster_field.trim())
    add("集群字段", fieldNameError(source.cluster_field));
  // 同一个问题只说一次（例如同一个关联字段登记了三次）
  return [...new Set(errors)];
}

/** 一条集群绑定的问题，每条形如「表 / 索引：请填写」 */
export function bindingErrors(
  form: LogSourcesForm,
  binding: BindingForm
): string[] {
  const errors: string[] = [];
  const add = (label: string, problem: string) => {
    if (problem) errors.push(`${label}：${problem}`);
  };
  const source = form.sources.find(item => item.id === binding.source);
  if (!binding.source) add("日志源", "请选择");
  else if (!source) add("日志源", `${binding.source} 不存在`);
  let type = form.connections.find(
    item => item.id === source?.connection
  )?.type;
  if (binding.connection) {
    const conn = form.connections.find(item => item.id === binding.connection);
    if (!conn) add("连接", `${binding.connection} 不存在`);
    else if (type && conn.type !== type)
      add("连接", `要和日志源的连接同类型（${typeLabel(type)}）`);
    else type = conn.type;
  }
  if (type) add("表 / 索引", targetError(type, binding.target));
  else if (!normalizeTarget(binding.target)) add("表 / 索引", "请填写");

  if (binding.conditions.length > MAX_CONDITIONS)
    add("过滤条件", `最多 ${MAX_CONDITIONS} 个`);
  binding.conditions.forEach((cond, index) => {
    const payload = conditionPayload(cond);
    const label = `过滤条件 ${payload.field || index + 1}`;
    add(label, payload.field ? fieldNameError(payload.field) : "请填写字段");
    if (!OP_VALUES.includes(cond.op)) add(label, "条件不合法");
    else if (isMultiOp(cond.op)) {
      if (!payload.values.length) add(label, "至少填一个值");
      else if (payload.values.length > MAX_IN_VALUES)
        add(label, `最多 ${MAX_IN_VALUES} 个值`);
    } else if (payload.values.length !== 1) add(label, "要填一个值");
    const long = payload.values.find(
      value =>
        typeof value === "string" &&
        charCount(value) > CONDITION_VALUE_MAX_LENGTH
    );
    if (long !== undefined)
      add(
        label,
        `${String(long).slice(0, 20)}… 太长了（每个值最多 ${CONDITION_VALUE_MAX_LENGTH} 个字）`
      );
    else if (
      payload.values.some(
        value => typeof value === "string" && hasControlChar(value)
      )
    )
      add(label, "取值不能换行");
  });
  if (charCount(binding.note.trim()) > BINDING_NOTE_MAX_LENGTH)
    add("说明", `最多 ${BINDING_NOTE_MAX_LENGTH} 个字`);
  return errors;
}

/** 集群名：1~100，不能有空白和控制字符 */
export function envError(value: string): string {
  const env = (value ?? "").trim();
  if (!env) return "集群名不能为空";
  if (charCount(env) > ENV_MAX_LENGTH)
    return `集群名最长 ${ENV_MAX_LENGTH} 个字符`;
  return /\s/.test(env) || hasControlChar(env) ? "集群名不能有空格" : "";
}

/** 一组日志源、绑定和它们用到的连接、关联字段的问题（预览、验证前只检查这些，其他部分没填完也能用） */
function partialErrors(
  form: LogSourcesForm,
  sources: SourceForm[],
  bindings: BindingForm[]
): string[] {
  const errors: string[] = [];
  const connections = new Set<string>();
  const keys = new Set<string>();
  for (const source of sources) {
    connections.add(source.connection);
    for (const row of usedCorrelations(source.correlations)) keys.add(row.key);
    for (const problem of sourceErrors(form, source))
      errors.push(`日志源 ${source.id} · ${problem}`);
  }
  for (const binding of bindings) {
    if (binding.connection) connections.add(binding.connection);
    for (const problem of bindingErrors(form, binding))
      errors.push(`绑定 ${binding.source} · ${problem}`);
  }
  for (const conn of form.connections.filter(item => connections.has(item.id)))
    for (const problem of connectionErrors(conn))
      errors.push(`连接 ${conn.id} · ${problem}`);
  for (const row of form.correlation_keys.filter(item =>
    keys.has(item.key.trim())
  ))
    for (const problem of keyErrors(row))
      errors.push(`关联字段 ${row.key} · ${problem}`);
  return errors;
}

/** 预览前检查：这条绑定、它的日志源、用到的连接和关联字段 */
export function previewErrors(
  form: LogSourcesForm,
  env: string,
  source: string
): string[] {
  const item = form.sources.find(row => row.id === source);
  const binding = form.clusters
    .find(row => row.env === env)
    ?.bindings.find(row => row.source === source);
  if (!item || !binding) return [`集群 ${env} 里没有日志源 ${source} 的绑定`];
  return partialErrors(form, [item], [binding]);
}

/** 验证关联字段前检查：这个集群里登记了它的日志源、它们的绑定和用到的连接 */
export function verifyErrors(
  form: LogSourcesForm,
  env: string,
  key: string
): string[] {
  const bindings = form.clusters.find(row => row.env === env)?.bindings ?? [];
  const sources = form.sources.filter(
    item =>
      bindings.some(binding => binding.source === item.id) &&
      usedCorrelations(item.correlations).some(row => row.key === key)
  );
  if (sources.length < 2)
    return [
      `集群 ${env} 里登记了关联字段 ${key} 的日志源不到两个（${sources.map(item => item.id).join("、") || "没有"}），没法互相验证`
    ];
  const ids = new Set(sources.map(item => item.id));
  return partialErrors(
    form,
    sources,
    bindings.filter(binding => ids.has(binding.source))
  );
}

/** 能验证这个关联字段的集群：绑定了至少两个登记了它的日志源 */
export function verifyEnvs(form: LogSourcesForm, key: string): string[] {
  const sources = new Set(
    form.sources
      .filter(item =>
        usedCorrelations(item.correlations).some(row => row.key === key)
      )
      .map(item => item.id)
  );
  return form.clusters
    .filter(
      cluster =>
        new Set(
          cluster.bindings
            .map(binding => binding.source)
            .filter(id => sources.has(id))
        ).size >= 2
    )
    .map(cluster => cluster.env);
}

/** 保存前的完整校验：错误带所在的卡片（滚动到那里）和可读的中文说明 */
export function validateForm(form: LogSourcesForm): FormError[] {
  const errors: FormError[] = [];
  const limit = (
    section: SectionId,
    count: number,
    max: number,
    what: string
  ) => {
    if (count > max)
      errors.push({
        section,
        text: `${what}最多 ${max} 个（现在 ${count} 个）`
      });
  };
  limit("connections", form.connections.length, MAX_CONNECTIONS, "连接");
  limit("correlations", form.correlation_keys.length, MAX_KEYS, "关联字段");
  limit("sources", form.sources.length, MAX_SOURCES, "日志源");
  limit(
    "clusters",
    form.clusters.filter(cluster => usedCluster(clusterPayload(cluster)))
      .length,
    MAX_CLUSTERS,
    "有配置的集群"
  );
  if (form.enabled && !form.connections.length)
    errors.push({
      section: "connections",
      text: "已经启用了，至少要有一个连接（不用了可以先关掉右上角的开关）"
    });

  const connectionIds = new Set<string>();
  form.connections.forEach((conn, index) => {
    const id = conn.id.trim();
    const label = `连接 ${id || `第 ${index + 1} 个`}`;
    for (const problem of connectionErrors(conn))
      errors.push({
        section: "connections",
        connection: id,
        text: `${label} · ${problem}`
      });
    if (id && connectionIds.has(id))
      errors.push({
        section: "connections",
        connection: id,
        text: `连接 ID ${id} 重复了`
      });
    connectionIds.add(id);
  });

  const keys = new Set<string>();
  let traces = 0;
  form.correlation_keys.forEach((row, index) => {
    const key = row.key.trim();
    const label = `关联字段 ${key || `第 ${index + 1} 个`}`;
    for (const problem of keyErrors(row))
      errors.push({ section: "correlations", text: `${label} · ${problem}` });
    if (key && keys.has(key))
      errors.push({ section: "correlations", text: `关联字段 ${key} 重复了` });
    keys.add(key);
    if (row.trace) traces++;
  });
  if (traces > 1)
    errors.push({
      section: "correlations",
      text: "只能有一个关联字段是链路 ID"
    });

  const sourceIds = new Set<string>();
  form.sources.forEach((source, index) => {
    const id = source.id.trim();
    const label = `日志源 ${id || `第 ${index + 1} 个`}`;
    for (const problem of sourceErrors(form, source))
      errors.push({
        section: "sources",
        source: id,
        text: `${label} · ${problem}`
      });
    if (id && sourceIds.has(id))
      errors.push({
        section: "sources",
        source: id,
        text: `日志源 ID ${id} 重复了`
      });
    sourceIds.add(id);
  });

  const envs = new Set<string>();
  for (const cluster of form.clusters) {
    if (!usedCluster(clusterPayload(cluster))) continue;
    const env = cluster.env.trim();
    const label = `集群 ${env || "（空）"}`;
    const add = (problem: string) =>
      errors.push({ section: "clusters", env, text: `${label} · ${problem}` });
    const problem = envError(env);
    if (problem) add(problem);
    if (env && envs.has(env)) add("重复了");
    envs.add(env);
    if (charCount(cluster.note.trim()) > CLUSTER_NOTE_MAX_LENGTH)
      add(`集群说明：最多 ${CLUSTER_NOTE_MAX_LENGTH} 个字`);
    if (cluster.bindings.length > MAX_BINDINGS)
      add(`绑定最多 ${MAX_BINDINGS} 个`);
    const bound = new Set<string>();
    cluster.bindings.forEach((binding, index) => {
      const name = `绑定 ${binding.source || `第 ${index + 1} 个`}`;
      for (const item of bindingErrors(form, binding)) add(`${name} · ${item}`);
      if (binding.source && bound.has(binding.source))
        add(
          `日志源 ${binding.source} 绑定了两次，一个集群里每个日志源只能绑定一次`
        );
      bound.add(binding.source);
    });
  }

  if (charCount(form.architecture.trim()) > ARCHITECTURE_MAX_LENGTH)
    errors.push({
      section: "architecture",
      text: `网络架构说明：最多 ${ARCHITECTURE_MAX_LENGTH} 个字`
    });

  if (!errors.length) {
    const size = JSON.stringify(formPayload(form)).length;
    if (size > CONFIG_MAX_CHARS)
      errors.push({
        section: "sources",
        text: `配置太大了（约 ${(size / 1024 / 1024).toFixed(2)} MB，上限 1 MB）：精简日志源说明和字段说明`
      });
  }
  return errors;
}

// ---------------------------------------------------------------------------
// 改表单的操作：都返回新的表单，不改传进来的
// ---------------------------------------------------------------------------
/** 小写短名：非法字符换成 -，不以字母开头时加前缀，最长 32 */
export function slugId(value: string, fallback = "source"): string {
  let id = text(value)
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, "-")
    .replace(/-{2,}/g, "-")
    .replace(/^[-_]+|[-_]+$/g, "");
  if (!id) id = fallback;
  if (!/^[a-z]/.test(id)) id = `${fallback}-${id}`;
  return id.slice(0, ID_MAX_LENGTH).replace(/[-_]+$/, "") || fallback;
}

/** 不和 used 重复的 ID：重复时加 -2、-3…（总长不超过 32） */
export function uniqueId(
  base: string,
  used: Iterable<string>,
  fallback = "source"
): string {
  const taken = new Set(used);
  const id = slugId(base, fallback);
  if (!taken.has(id)) return id;
  for (let n = 2; ; n++) {
    const suffix = `-${n}`;
    const candidate =
      id.slice(0, ID_MAX_LENGTH - suffix.length).replace(/[-_]+$/, "") + suffix;
    if (!taken.has(candidate)) return candidate;
  }
}

/** 关联字段的 key：驼峰转下划线、小写，只留字母数字和 _ */
export function slugKey(value: string, fallback = "id"): string {
  let key = text(value)
    .replace(/([a-z0-9])([A-Z])/g, "$1_$2")
    .toLowerCase()
    .replace(/[^a-z0-9_]+/g, "_")
    .replace(/_{2,}/g, "_")
    .replace(/^_+|_+$/g, "");
  if (!key) key = fallback;
  if (!/^[a-z]/.test(key)) key = `${fallback}_${key}`;
  return key.slice(0, ID_MAX_LENGTH).replace(/_+$/, "") || fallback;
}

/** 每个日志源绑定了几个集群 */
export function sourceUsage(form: LogSourcesForm): Record<string, number> {
  return Object.fromEntries(
    form.sources.map(item => [item.id, boundEnvs(form, item.id).length])
  );
}

/** 绑定了这个日志源的集群 */
export const boundEnvs = (form: LogSourcesForm, id: string): string[] =>
  form.clusters
    .filter(cluster => cluster.bindings.some(binding => binding.source === id))
    .map(cluster => cluster.env);

/** 删除日志源，连带删除各集群里它的绑定 */
export function removeSource(form: LogSourcesForm, id: string): LogSourcesForm {
  return {
    ...form,
    sources: form.sources.filter(item => item.id !== id),
    clusters: form.clusters.map(cluster => ({
      ...cluster,
      bindings: cluster.bindings.filter(binding => binding.source !== id)
    }))
  };
}

/** 保存编辑后的日志源（按 rowId 找到原来的那个，找不到就新增）；ID 改了时同步各集群的绑定 */
export function upsertSource(
  form: LogSourcesForm,
  source: SourceForm,
  originalId: string | null
): LogSourcesForm {
  const index = form.sources.findIndex(item => item.rowId === source.rowId);
  const sources = [...form.sources];
  if (index >= 0) sources[index] = source;
  else sources.push(source);
  const renamed = !!originalId && originalId !== source.id;
  return {
    ...form,
    sources,
    clusters: renamed
      ? form.clusters.map(cluster => ({
          ...cluster,
          bindings: cluster.bindings.map(binding =>
            binding.source === originalId
              ? { ...binding, source: source.id }
              : binding
          )
        }))
      : form.clusters
  };
}

/** 引用了这个连接的日志源和绑定（被引用的连接不能删） */
export function connectionUsage(
  form: LogSourcesForm,
  id: string
): { sources: string[]; bindings: string[] } {
  return {
    sources: form.sources
      .filter(item => item.connection === id)
      .map(item => item.id),
    bindings: form.clusters.flatMap(cluster =>
      cluster.bindings
        .filter(binding => binding.connection === id)
        .map(binding => `${cluster.env} / ${binding.source}`)
    )
  };
}

/** 保存编辑后的连接（按 rowId）；没保存过的连接改了 ID 时同步日志源和绑定的引用 */
export function upsertConnection(
  form: LogSourcesForm,
  conn: ConnectionForm,
  originalId: string | null
): LogSourcesForm {
  const index = form.connections.findIndex(item => item.rowId === conn.rowId);
  const connections = [...form.connections];
  if (index >= 0) connections[index] = conn;
  else connections.push(conn);
  if (!originalId || originalId === conn.id) return { ...form, connections };
  return {
    ...form,
    connections,
    sources: form.sources.map(item =>
      item.connection === originalId ? { ...item, connection: conn.id } : item
    ),
    clusters: form.clusters.map(cluster => ({
      ...cluster,
      bindings: cluster.bindings.map(binding =>
        binding.connection === originalId
          ? { ...binding, connection: conn.id }
          : binding
      )
    }))
  };
}

export const removeConnection = (
  form: LogSourcesForm,
  id: string
): LogSourcesForm => ({
  ...form,
  connections: form.connections.filter(item => item.id !== id)
});

/** 登记了这个关联字段的日志源 */
export const keyUsage = (form: LogSourcesForm, key: string): string[] =>
  form.sources
    .filter(item => item.correlations.some(row => row.key === key))
    .map(item => item.id);

/** 保存编辑后的关联字段（按 rowId）；key 改了时同步日志源里的登记；设为链路 ID 时取消其他的 */
export function upsertKey(
  form: LogSourcesForm,
  row: KeyForm,
  originalKey: string | null
): LogSourcesForm {
  const index = form.correlation_keys.findIndex(
    item => item.rowId === row.rowId
  );
  let keys = [...form.correlation_keys];
  if (index >= 0) keys[index] = row;
  else keys.push(row);
  if (row.trace)
    keys = keys.map(item =>
      item.rowId !== row.rowId && item.trace ? { ...item, trace: false } : item
    );
  const renamed = !!originalKey && originalKey !== row.key;
  return {
    ...form,
    correlation_keys: keys,
    sources: renamed
      ? form.sources.map(item => ({
          ...item,
          correlations: item.correlations.map(entry =>
            entry.key === originalKey ? { ...entry, key: row.key } : entry
          )
        }))
      : form.sources
  };
}

/** 删除关联字段，连带删除日志源里对它的登记 */
export function removeKey(form: LogSourcesForm, key: string): LogSourcesForm {
  return {
    ...form,
    correlation_keys: form.correlation_keys.filter(item => item.key !== key),
    sources: form.sources.map(item =>
      item.correlations.some(row => row.key === key)
        ? {
            ...item,
            correlations: item.correlations.filter(row => row.key !== key)
          }
        : item
    )
  };
}

/** 链路 ID 开关：最多一个，打开一个时关掉其他的 */
export function setTraceKey(
  form: LogSourcesForm,
  key: string,
  on: boolean
): LogSourcesForm {
  return {
    ...form,
    correlation_keys: form.correlation_keys.map(item => ({
      ...item,
      trace: item.key === key ? on : on ? false : item.trace
    }))
  };
}

/** 保存一条绑定（按 rowId；集群还没有时新建） */
export function upsertBinding(
  form: LogSourcesForm,
  env: string,
  binding: BindingForm
): LogSourcesForm {
  const exists = form.clusters.some(item => item.env === env);
  const clusters = exists
    ? form.clusters
    : [...form.clusters, emptyCluster(env)];
  return {
    ...form,
    clusters: clusters.map(cluster => {
      if (cluster.env !== env) return cluster;
      const index = cluster.bindings.findIndex(
        item => item.rowId === binding.rowId
      );
      const bindings = [...cluster.bindings];
      if (index >= 0) bindings[index] = binding;
      else bindings.push(binding);
      return { ...cluster, bindings };
    })
  };
}

export const removeBinding = (
  form: LogSourcesForm,
  env: string,
  rowId: number
): LogSourcesForm => ({
  ...form,
  clusters: form.clusters.map(cluster =>
    cluster.env === env
      ? {
          ...cluster,
          bindings: cluster.bindings.filter(item => item.rowId !== rowId)
        }
      : cluster
  )
});

/** 删除集群（只用于已不在 KubeDoor 里的集群）：它的说明和绑定一起删 */
export const removeCluster = (
  form: LogSourcesForm,
  env: string
): LogSourcesForm => ({
  ...form,
  clusters: form.clusters.filter(item => item.env !== env)
});

// ---------------------------------------------------------------------------
// 显示
// ---------------------------------------------------------------------------
/** 过滤条件的文字：env ∈ {prod-a}、env = prod-a、host 以 api. 开头 */
export function conditionText(cond: {
  field: string;
  op: LogConditionOp | string;
  values: (string | number)[];
}): string {
  const field = text(cond?.field).trim() || "?";
  const values = conditionValues(cond?.values).map(value => String(value));
  switch (cond?.op) {
    case "eq":
      return `${field} = ${values[0] ?? ""}`;
    case "ne":
      return `${field} ≠ ${values[0] ?? ""}`;
    case "in":
      return `${field} ∈ {${values.join(", ")}}`;
    case "not_in":
      return `${field} ∉ {${values.join(", ")}}`;
    case "prefix":
      return `${field} 以 ${values[0] ?? ""} 开头`;
    default:
      return `${field} ${text(cond?.op)} ${values.join(", ")}`;
  }
}

/** 多个条件（AND）；没有条件时返回空串（页面显示「整张表 / 整个索引」） */
export const conditionsText = (
  conditions: ConditionForm[] | LogBindingCondition[]
) => (conditions ?? []).map(conditionText).join(" 且 ");

export interface ClusterRow {
  env: string;
  aliases: string[];
  online: boolean;
  /** KubeDoor 里已登记（k8s_agent_status）；配置里有但已经不在的为 false */
  registered: boolean;
  bindings: number;
  hasNote: boolean;
}

/** 集群列表：KubeDoor 已登记的（按 env 排序）在前，配置里有但已不在的标「未登记」排在后面 */
export function clusterRows(
  form: LogSourcesForm,
  known: LogKnownCluster[] | null | undefined
): ClusterRow[] {
  const configured = new Map(form.clusters.map(item => [item.env, item]));
  const row = (env: string): Pick<ClusterRow, "bindings" | "hasNote"> => ({
    bindings: configured.get(env)?.bindings.length ?? 0,
    hasNote: !!configured.get(env)?.note.trim()
  });
  const rows: ClusterRow[] = [...(known ?? [])]
    .filter(item => item?.env)
    .sort((a, b) => a.env.localeCompare(b.env))
    .map(item => ({
      env: item.env,
      aliases: [...(item.aliases ?? [])],
      online: !!item.online,
      registered: true,
      ...row(item.env)
    }));
  const names = new Set(rows.map(item => item.env));
  const extra = form.clusters
    .map(item => item.env)
    .filter(env => env && !names.has(env))
    .sort((a, b) => a.localeCompare(b));
  for (const env of new Set(extra))
    rows.push({
      env,
      aliases: [],
      online: false,
      registered: false,
      ...row(env)
    });
  return rows;
}

export interface InfoRow {
  label: string;
  value: string;
  /** true 时标黄提示 */
  warn?: boolean;
}

const readonlyLevel = (value: unknown): number => {
  if (value === true) return 1;
  const level = Number(value);
  return Number.isFinite(level) ? level : 0;
};

/** 测试连接结果的说明表 */
export function testRows(result: LogConnectionTest): InfoRow[] {
  if (result?.type === "elasticsearch")
    return [
      {
        label: "版本",
        value:
          `${result.distribution || "elasticsearch"} ${result.version || ""}`.trim()
      },
      { label: "集群名", value: result.cluster_name || "—" },
      { label: "许可", value: result.license || "—" },
      {
        label: "ES|QL",
        value: result.esql
          ? "可用（AI 可以用 ES|QL 做统计）"
          : "不可用：需要 Elasticsearch 8.14 及以上，OpenSearch 不支持；DSL 查询照常可用",
        warn: !result.esql
      }
    ];
  const level = readonlyLevel(result?.readonly);
  return [
    { label: "版本", value: `ClickHouse ${result?.version || ""}`.trim() },
    { label: "时区", value: result?.timezone || "—" },
    { label: "账号", value: result?.user || "—" },
    {
      label: "只读",
      value: level
        ? `是（readonly = ${level}）`
        : "否：建议给 KubeDoor 建只读账号（见帮助文档）",
      warn: !level
    },
    {
      label: "集群",
      value:
        (result?.clusters ?? []).join("、") || "（没有配置 ClickHouse 集群）"
    }
  ];
}

/** 测试结果的一句话摘要 */
export function testSummary(result: LogConnectionTest): string {
  if (result?.type === "elasticsearch")
    return `连接正常：${result.distribution || "elasticsearch"} ${result.version || ""}`.trim();
  return `连接正常：ClickHouse ${result?.version || ""}`.trim();
}

const trimNumber = (value: number) =>
  value >= 100 ? value.toFixed(0) : value.toFixed(1).replace(/\.0$/, "");

/** 行数：1.2 万、3.4 亿 */
export function formatCount(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  if (value >= 1e8) return `${trimNumber(value / 1e8)} 亿`;
  if (value >= 1e4) return `${trimNumber(value / 1e4)} 万`;
  return String(value);
}

export function formatBytes(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0)
    return "—";
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  let size = value;
  let index = 0;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index++;
  }
  return `${index ? trimNumber(size) : size} ${units[index]}`;
}

/** 时间显示到分钟：2026-11-23T15:18:49+08:00 → 2026-11-23 15:18 */
export const shortTime = (value: string | null | undefined) =>
  value ? text(value).replace("T", " ").slice(0, 16) : "";

export interface GroupSummary {
  targets: number;
  rows: number | null;
  bytes: number | null;
  retention: string;
  range: string;
  fields: number;
}

/** 扫描结果里一组的概况：目标数、总行数、保留天数、时间范围、字段数 */
export function groupSummary(group: LogScanGroup): GroupSummary {
  const targets = group?.targets ?? [];
  const sum = (key: "rows" | "bytes") => {
    const list = targets
      .map(item => item[key])
      .filter((value): value is number => typeof value === "number");
    return list.length ? list.reduce((a, b) => a + b, 0) : null;
  };
  const days = [
    ...new Set(
      targets
        .map(item => item.retention_days)
        .filter((value): value is number => typeof value === "number")
    )
  ].sort((a, b) => a - b);
  const firsts = targets
    .map(item => item.first)
    .filter(Boolean)
    .sort();
  const lasts = targets
    .map(item => item.last)
    .filter(Boolean)
    .sort();
  return {
    targets: targets.length,
    rows: sum("rows"),
    bytes: sum("bytes"),
    retention: !days.length
      ? "—"
      : days.length === 1
        ? `${days[0]} 天`
        : `${days[0]}~${days[days.length - 1]} 天`,
    range:
      firsts.length || lasts.length
        ? `${shortTime(firsts[0]) || "?"} ~ ${shortTime(lasts[lasts.length - 1]) || "?"}`
        : "—",
    fields: (group?.columns ?? []).length
  };
}

export type VerifyLevel = "good" | "partial" | "none" | "empty" | "error";

export interface VerifyCell {
  to: string;
  sampled: number;
  matched: number;
  examples: string[];
  /** 去查的时候出错（例如字段不存在） */
  error: string;
  /** good：一半以上能对上；partial：部分对上；none：都没对上；empty：没抽到取值；error：查询出错 */
  level: VerifyLevel;
}

export interface VerifyMatrix {
  sources: string[];
  rows: { from: string; cells: (VerifyCell | null)[] }[];
}

/** 验证关联字段的命中矩阵：行是取值来源，列是去查的日志源 */
export function verifyMatrix(
  result: LogVerifyResult | null | undefined
): VerifyMatrix {
  const pairs = (result?.pairs ?? []).filter(item => item?.from && item?.to);
  const sources: string[] = [];
  for (const pair of pairs)
    for (const id of [pair.from, pair.to])
      if (!sources.includes(id)) sources.push(id);
  const rows = sources
    .filter(from => pairs.some(pair => pair.from === from))
    .map(from => ({
      from,
      cells: sources.map(to => {
        const pair = pairs.find(item => item.from === from && item.to === to);
        if (!pair) return null;
        const sampled = numberOr(pair.sampled, 0);
        const matched = numberOr(pair.matched, 0);
        const error = text(pair.error);
        const level: VerifyLevel = error
          ? "error"
          : !sampled
            ? "empty"
            : !matched
              ? "none"
              : matched * 2 >= sampled
                ? "good"
                : "partial";
        return {
          to,
          sampled,
          matched,
          examples: stringList(pair.examples),
          error,
          level
        };
      })
    }));
  return { sources, rows };
}

/** 预览结果的列：所有行出现过的字段，@time 排第一，最多 limit 列 */
export function previewColumns(
  rows: Record<string, unknown>[] | null | undefined,
  limit = 30
): string[] {
  const columns: string[] = [];
  for (const row of rows ?? [])
    for (const key of Object.keys(row ?? {}))
      if (!columns.includes(key)) columns.push(key);
  const time = columns.indexOf("@time");
  if (time > 0) columns.unshift(...columns.splice(time, 1));
  return columns.slice(0, limit);
}

/** 单元格 / 查询 / 问题等任意值的显示文字 */
export function valueText(value: unknown, indent = 0): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean")
    return String(value);
  try {
    return JSON.stringify(value, null, indent || undefined);
  } catch {
    return String(value);
  }
}

/** 预览 / 接口返回的错误：字符串，或者 {code, message} */
export function problemText(value: unknown): string {
  if (!value) return "";
  if (typeof value === "string") return value;
  const item = value as { message?: string; field?: string; where?: string };
  if (item.message) return item.message;
  if (item.field)
    return item.where
      ? `${item.where}：${item.field} 不存在`
      : `${item.field} 不存在`;
  return valueText(value);
}

/** 请求失败的原因：接口返回的 message / error，或者网络错误本身的说明 */
export function errorText(reason: unknown, fallback: string): string {
  const error = reason as {
    response?: { status?: number; data?: unknown };
    message?: string;
  };
  const data = error?.response?.data as
    | { message?: string; error?: string }
    | string
    | undefined;
  const message =
    typeof data === "string" ? data : data?.message || data?.error || "";
  const status = error?.response?.status;
  if (message) return status ? `HTTP ${status}：${message}` : message;
  if (status) return `HTTP ${status}`;
  return error?.message || fallback;
}

/** 「AI 生成说明」失败的原因（kubedoor-ai 的错误码见约定 §11.4） */
export function describeErrorText(reason: unknown): string {
  const error = reason as { status?: number; code?: string; message?: string };
  const code = error?.code ?? "";
  if (code === "shared_model_unavailable")
    return "企微专用模型还没有配置好：到「系统管理 → 企微 AI 机器人」配置模型后再试";
  if (code === "describe_timeout" || error?.status === 504)
    return "模型 3 分钟内没有返回：少勾选几个组，或稍后再试";
  if (code === "describe_invalid")
    return "模型返回的内容不是合法的 JSON：再试一次，或少勾选几个组";
  if (code === "describe_failed")
    return "调用模型失败：稍后再试，或检查企微专用模型的配置";
  if (error?.status === 403) return "只有系统管理员可以使用";
  if (error?.status === 413) return "请求太大：少勾选几个组再试";
  return error?.message || "AI 生成说明失败";
}

/** 浏览器等多久（毫秒）：配置的超时 × 要发的请求数，再留 15 秒余量 */
export function requestTimeoutMs(seconds: number, calls: number): number {
  const timeout = Number.isFinite(seconds)
    ? Math.min(MAX_TIMEOUT, Math.max(MIN_TIMEOUT, seconds))
    : DEFAULT_TIMEOUT;
  return (timeout * calls + 15) * 1000;
}

/** 网络架构说明的示例模板：入口 → 网关 → 服务，每层写日志源 ID（有日志源时用实际的 ID） */
export function architectureTemplate(form: LogSourcesForm): string {
  const access = form.sources
    .filter(item => item.kind === "access")
    .map(item => item.id);
  const app = form.sources
    .filter(item => item.kind === "app")
    .map(item => item.id);
  const entry = access[0] || "ingress";
  const gateway = access[1] || "gateway";
  const service = app[0] || "app";
  const trace =
    form.correlation_keys.find(item => item.trace)?.key || "trace_id";
  const request =
    form.correlation_keys.find(item => !item.trace)?.key || "request_id";
  return [
    "## 请求链路",
    "",
    `1. 入口：客户端 → 入口网关（日志源 \`${entry}\`），按域名和路径路由`,
    `2. 网关：入口网关 → 内部网关（日志源 \`${gateway}\`），转发到各服务的 Service`,
    `3. 服务：各服务的 Pod（日志源 \`${service}\`）处理请求，再调用下游服务和数据库`,
    "",
    "## 串联",
    "",
    `- 入口网关生成 \`${request}\`，一路透传到服务日志`,
    `- 服务日志里有链路 ID（关联字段 \`${trace}\`），可以接着查链路`,
    "",
    "## 例外",
    "",
    "- 测试环境没有内部网关，入口直接到服务"
  ].join("\n");
}

// ---------------------------------------------------------------------------
// 扫描结果：字段、表 / 索引、取值的候选
// ---------------------------------------------------------------------------
/** 各连接最近一次的扫描结果（只在页面内存里，刷新页面后要重新扫描） */
export type ScanMap = Record<string, LogScanResult>;

/** 表 / 索引所在的组（也认 ClickHouse 分布式表对应的本地表） */
export function groupOfTarget(
  scan: LogScanResult | null | undefined,
  target: string
): LogScanGroup | null {
  const value = normalizeTarget(target);
  if (!scan || !value) return null;
  return (
    (scan.groups ?? []).find(group =>
      (group.targets ?? []).some(
        item => item.target === value || item.local === value
      )
    ) ?? null
  );
}

/** 日志源对应的扫描组：按它的默认表 / 索引，或者各集群绑定的表 / 索引找 */
export function scanGroupFor(
  scans: ScanMap,
  form: LogSourcesForm,
  source: Pick<SourceForm, "id" | "connection" | "default_target">
): LogScanGroup | null {
  const scan = scans?.[source.connection];
  if (!scan) return null;
  const targets = [
    source.default_target,
    ...form.clusters.flatMap(cluster =>
      cluster.bindings
        .filter(
          binding =>
            binding.source === source.id &&
            (!binding.connection || binding.connection === source.connection)
        )
        .map(binding => binding.target)
    )
  ];
  for (const target of targets) {
    const group = groupOfTarget(scan, target);
    if (group) return group;
  }
  return null;
}

export interface Option {
  value: string;
  /** 下拉里的说明（字段类型、行数等） */
  note: string;
}

/** 时间类型（ClickHouse 的 Date / DateTime*，Elasticsearch 的 date / date_nanos） */
export const isTimeType = (type: string) => /date/i.test(type ?? "");

/** 扫描组里的表 / 索引已经被哪些日志源用了（同一个连接下的默认表 / 索引，或者集群绑定的表 / 索引） */
export function groupUsedBy(
  form: LogSourcesForm,
  connection: string,
  group: LogScanGroup | null | undefined
): string[] {
  const targets = new Set(
    (group?.targets ?? []).flatMap(item =>
      [item.target, item.local].filter(Boolean)
    )
  );
  const ids: string[] = [];
  const add = (id: string) => {
    if (!ids.includes(id)) ids.push(id);
  };
  for (const source of form.sources)
    if (
      source.connection === connection &&
      targets.has(normalizeTarget(source.default_target))
    )
      add(source.id);
  for (const cluster of form.clusters)
    for (const binding of cluster.bindings) {
      const conn =
        binding.connection ||
        form.sources.find(item => item.id === binding.source)?.connection;
      if (conn === connection && targets.has(normalizeTarget(binding.target)))
        add(binding.source);
    }
  return ids;
}

/** 字段候选：扫描到的字段名 + 类型；time 为 true 时时间类型的排前面 */
export function columnOptions(
  group: LogScanGroup | null | undefined,
  time = false
): Option[] {
  const options = (group?.columns ?? [])
    .filter(item => item?.name)
    .map(item => ({ value: item.name, note: text(item.type) }));
  return time
    ? [
        ...options.filter(item => isTimeType(item.note)),
        ...options.filter(item => !isTimeType(item.note))
      ]
    : options;
}

/** 表 / 索引的候选：同组的目标，附行数和保留天数 */
export function targetOptions(
  group: LogScanGroup | null | undefined
): Option[] {
  return (group?.targets ?? [])
    .filter(item => item?.target)
    .map(item => ({
      value: item.target,
      note: [
        typeof item.rows === "number" ? `${formatCount(item.rows)} 行` : "",
        typeof item.retention_days === "number"
          ? `保留 ${item.retention_days} 天`
          : ""
      ]
        .filter(Boolean)
        .join(" · ")
    }));
}

/** 条件取值的候选：这个表 / 索引里该字段的 topK；它没有时用同组其他目标的 */
export function valueOptions(
  scan: LogScanResult | null | undefined,
  target: string,
  field: string
): (string | number)[] {
  const group = groupOfTarget(scan, target);
  const name = (field ?? "").trim();
  if (!group || !name) return [];
  const value = normalizeTarget(target);
  const own = group.targets.find(
    item => item.target === value || item.local === value
  );
  const lists = own?.values?.[name]?.length
    ? [own.values[name]]
    : group.targets.map(item => item.values?.[name] ?? []);
  const result: (string | number)[] = [];
  for (const list of lists)
    for (const item of conditionValues(list))
      if (!result.includes(item)) result.push(item);
  return result;
}

/** 取值里和集群名（优先）或别名相同的那个（不区分大小写） */
export function matchClusterValue(
  values: (string | number)[],
  env: string,
  aliases: string[] = []
): string | number | null {
  for (const name of [env, ...aliases]) {
    const lower = text(name).toLowerCase();
    if (!lower) continue;
    const hit = values.find(value => String(value).toLowerCase() === lower);
    if (hit !== undefined) return hit;
  }
  return null;
}

/**
 * 新建绑定的默认值：表 / 索引用日志源的默认值；扫描有这个集群的绑定建议时用建议的表和条件，
 * 否则按日志源的集群字段出条件「集群字段 ∈ {集群名}」（取值能对上集群名或别名时用表里的写法）
 */
export function defaultBinding(
  source: Pick<
    SourceForm,
    "id" | "connection" | "default_target" | "cluster_field"
  >,
  env: string,
  scan?: LogScanResult | null,
  aliases: string[] = []
): BindingForm {
  const binding = bindingForm({
    source: source.id,
    target: normalizeTarget(source.default_target)
  });
  const usable =
    scan && scan.connection?.id === source.connection ? scan : null;
  const group = groupOfTarget(usable, source.default_target);
  const suggested = group
    ? (usable.suggested_bindings ?? []).find(
        item => item.env === env && item.group === group.ref
      )
    : null;
  if (suggested) {
    binding.target = suggested.target;
    binding.conditions = cloneConditions(suggested.conditions);
    return binding;
  }
  const field = (source.cluster_field ?? "").trim();
  if (field) {
    const known = usable?.clusters?.find(item => item.env === env)?.aliases;
    const value = matchClusterValue(
      valueOptions(usable, binding.target, field),
      env,
      known?.length ? known : aliases
    );
    binding.conditions = [{ field, op: "in", values: [value ?? env] }];
  }
  return binding;
}

// ---------------------------------------------------------------------------
// 草稿：按规则生成 / AI 生成说明 → 勾选 → 应用到表单
// ---------------------------------------------------------------------------
export interface DraftBinding extends LogDraftBinding {
  /** 按规则生成时：绑定建议的匹配方式（MATCH_LABELS） */
  match?: string;
}

/** 页面里的草稿：草稿日志源都用 connection 这个连接 */
export interface DraftState {
  origin: "rules" | "ai";
  connection: string;
  sources: LogDraftSource[];
  correlation_keys: LogCorrelationKey[];
  bindings: DraftBinding[];
  cluster_notes: { env: string; note: string }[];
  architecture: string;
  notes: string[];
  /** AI 生成时用的模型 */
  model: string;
}

/** 常见关联字段的名称 */
const KEY_LABELS: Record<string, string> = {
  trace_id: "链路 ID",
  request_id: "请求 ID",
  span_id: "Span ID",
  access_id: "访问 ID",
  correlation_id: "关联 ID"
};

/** 字段名 / 正文写法里的名字 → 关联字段的 key（X-Request-Id → request_id，traceId、TID → trace_id） */
export function correlationKeyOf(name: string): string {
  const key = slugKey(name, "id").replace(/^x_/, "");
  if (/trace|^tid$/.test(key)) return "trace_id";
  if (/request|^req(_?id)?$|^rid$/.test(key)) return "request_id";
  if (/span/.test(key)) return "span_id";
  if (/access/.test(key)) return "access_id";
  if (/correlation/.test(key)) return "correlation_id";
  return key;
}

/** 正文写法里 {value} 前面的名字：TID:{value} → TID，"traceId":"{value}" → traceId */
export function textIdName(value: string): string {
  const before = text(value).split("{value}")[0];
  const words = before.match(/[A-Za-z_][A-Za-z0-9_-]*/g);
  return words?.[words.length - 1] ?? "id";
}

/** 扫描识别出的 ID 形状 → 取值格式的正则 */
export function patternForShape(shape: string): string {
  const value = text(shape).toLowerCase();
  if (value === "uuid")
    return "[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}";
  const hex = /^hex(\d{1,3})$/.exec(value);
  return hex ? `[0-9a-fA-F]{${hex[1]}}` : "";
}

/** 同一集群同一日志源只留第一条 */
function dedupeBindings<T extends { env: string; source: string }>(
  list: T[]
): T[] {
  const seen = new Set<string>();
  return list.filter(item => {
    const key = bindingKey(item.env, item.source);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

/** 草稿绑定的勾选 key（集群名和日志源 ID 里都不会有换行） */
export const bindingKey = (env: string, source: string) => `${env}\n${source}`;

/**
 * 按规则生成草稿（不调模型）：每个勾选的组一个日志源，字段用扫描的猜测（fields_guess、cluster_field_guess），
 * 绑定用扫描的建议（suggested_bindings），ID 字段 / 正文里的 ID 写法生成关联字段。
 * 组里的表 / 索引已经是某个日志源的默认表 / 索引时，草稿用那个日志源的 ID（应用时只补说明，不重复新增）。
 */
export function draftFromScan(
  scan: LogScanResult,
  refs: string[],
  form: LogSourcesForm
): DraftState {
  const connection = scan?.connection?.id ?? "";
  const used = new Set(form.sources.map(item => item.id));
  const existingKeys = new Set(form.correlation_keys.map(item => item.key));
  let hasTrace = form.correlation_keys.some(item => item.trace);
  const newKeys = new Map<string, LogCorrelationKey>();
  const addKey = (key: string, shape: string) => {
    if (existingKeys.has(key) || newKeys.has(key)) return;
    const trace = key === "trace_id" && !hasTrace;
    if (trace) hasTrace = true;
    newKeys.set(key, {
      key,
      label: KEY_LABELS[key] ?? key,
      description: "",
      pattern: patternForShape(shape),
      trace
    });
  };
  const groupIds = new Map<string, string>();
  const sources: LogDraftSource[] = [];
  for (const group of (scan?.groups ?? []).filter(item =>
    refs.includes(item.ref)
  )) {
    const targets = (group.targets ?? []).map(item => item.target);
    const existing = form.sources.find(
      item =>
        item.connection === connection &&
        targets.includes(normalizeTarget(item.default_target))
    );
    const id = existing
      ? existing.id
      : uniqueId(group.name_guess || targets[0] || "logs", used);
    used.add(id);
    groupIds.set(group.ref, id);
    const guess = group.fields_guess ?? {};
    const fields: Partial<Record<LogSemanticField, string>> = {};
    for (const key of SEMANTIC_KEYS) if (guess[key]) fields[key] = guess[key];
    const correlations: LogSourceCorrelation[] = [];
    // 「按规则生成」只自动加请求追踪类的 ID（链路、请求、Span、访问、关联 ID）；订单号这类业务 ID、sign 之类的
    // 写法留给 AI 生成说明或管理员自己加。顺序：有索引的列 → 正文写法 → ES 运行时字段（没有索引，关联很慢）
    const addField = (item: LogScanIdField) => {
      if (!item?.field || fieldNameError(item.field)) return;
      const key = correlationKeyOf(item.field);
      if (!KEY_LABELS[key] || correlations.some(row => row.key === key)) return;
      correlations.push({ key, field: item.field });
      addKey(key, item.shape);
    };
    for (const item of group.id_fields ?? [])
      if (!item?.runtime) addField(item);
    if (fields.message)
      for (const item of group.text_ids ?? []) {
        if (valueSlots(item?.text) !== 1) continue;
        const key = correlationKeyOf(textIdName(item.text));
        if (!KEY_LABELS[key] || correlations.some(row => row.key === key))
          continue;
        correlations.push({ key, text: item.text });
        addKey(key, item.shape);
      }
    for (const item of group.id_fields ?? []) if (item?.runtime) addField(item);
    sources.push({
      group: group.ref,
      id,
      name: (group.name_guess || targets[0] || id).slice(0, NAME_MAX_LENGTH),
      kind: KIND_VALUES.includes(group.kind_guess) ? group.kind_guess : "other",
      description: text(group.meta_description).slice(
        0,
        SOURCE_DESCRIPTION_MAX_LENGTH
      ),
      time_field: text(guess.time),
      fields,
      duration_unit: DURATION_VALUES.includes(guess.duration_unit)
        ? guess.duration_unit
        : "ms",
      level_values: {},
      cluster_field: text(group.cluster_field_guess),
      default_target: targets[0] ?? "",
      columns: [],
      correlations: correlations.slice(0, MAX_CORRELATIONS)
    });
  }
  const bindings = dedupeBindings(
    (scan?.suggested_bindings ?? [])
      .filter(item => groupIds.has(item.group))
      .map(item => ({
        env: item.env,
        source: groupIds.get(item.group),
        target: item.target,
        conditions: cloneConditions(item.conditions),
        note: "",
        match: item.match
      }))
  );
  return {
    origin: "rules",
    connection,
    sources,
    correlation_keys: [...newKeys.values()],
    bindings,
    cluster_notes: [],
    architecture: "",
    notes: [],
    model: ""
  };
}

/** AI 草稿里的一个日志源 → 规范化（类型、长度）；缺时间字段、默认表时用扫描的猜测补上 */
function normalizeDraftSource(
  raw: Partial<LogDraftSource>,
  group: LogScanGroup | undefined
): LogDraftSource {
  const id = ID_PATTERN.test(text(raw?.id))
    ? text(raw.id)
    : slugId(text(raw?.id) || group?.name_guess || "logs", "logs");
  const fields: Partial<Record<LogSemanticField, string>> = {};
  for (const key of SEMANTIC_KEYS) {
    const value = raw?.fields?.[key];
    if (typeof value === "string" && value.trim()) fields[key] = value.trim();
  }
  const levels: Partial<Record<LogLevelKey, string[]>> = {};
  for (const key of LEVEL_KEYS) {
    const values = cleanNames(raw?.level_values?.[key]).slice(
      0,
      MAX_LEVEL_VALUES
    );
    if (values.length) levels[key] = values;
  }
  const columns = (Array.isArray(raw?.columns) ? raw.columns : [])
    .filter(item => text(item?.name).trim() && text(item?.description).trim())
    .slice(0, MAX_COLUMNS)
    .map(item => ({
      name: text(item.name).trim(),
      description: text(item.description)
        .trim()
        .slice(0, COLUMN_DESCRIPTION_MAX_LENGTH)
    }));
  const correlations = (
    Array.isArray(raw?.correlations) ? raw.correlations : []
  )
    .filter(item => text(item?.key) && (text(item?.field) || text(item?.text)))
    .slice(0, MAX_CORRELATIONS)
    .map(item =>
      item.text
        ? { key: text(item.key), text: text(item.text) }
        : { key: text(item.key), field: text(item.field) }
    );
  return {
    group: text(raw?.group),
    id,
    name: (text(raw?.name).trim() || id).slice(0, NAME_MAX_LENGTH),
    kind: KIND_VALUES.includes(raw?.kind)
      ? raw.kind
      : KIND_VALUES.includes(group?.kind_guess)
        ? group.kind_guess
        : "other",
    description: text(raw?.description)
      .trim()
      .slice(0, SOURCE_DESCRIPTION_MAX_LENGTH),
    time_field: text(raw?.time_field).trim() || text(group?.fields_guess?.time),
    fields,
    duration_unit: DURATION_VALUES.includes(raw?.duration_unit)
      ? raw.duration_unit
      : DURATION_VALUES.includes(group?.fields_guess?.duration_unit)
        ? group.fields_guess.duration_unit
        : "ms",
    level_values: levels,
    cluster_field: text(raw?.cluster_field).trim(),
    default_target:
      text(raw?.default_target).trim() || group?.targets?.[0]?.target || "",
    columns,
    correlations
  };
}

/**
 * 「AI 生成说明」的返回 → 页面草稿：日志源用扫描的这个连接；组不在这次勾选里的新日志源、
 * key 不合法的关联字段、引用了不存在的日志源的绑定丢掉，并写进 notes。
 */
export function draftFromDescribe(
  result: LogDescribeResult,
  scan: LogScanResult,
  refs: string[],
  form: LogSourcesForm
): DraftState {
  const draft = result?.draft;
  const notes = stringList(result?.notes);
  const groups = new Map(
    (scan?.groups ?? [])
      .filter(item => refs.includes(item.ref))
      .map(item => [item.ref, item])
  );
  const sources: LogDraftSource[] = [];
  for (const raw of Array.isArray(draft?.sources) ? draft.sources : []) {
    const group = groups.get(text(raw?.group));
    const existing = form.sources.some(item => item.id === text(raw?.id));
    if (!group && !existing) {
      notes.push(
        `丢掉了日志源 ${text(raw?.id) || "（无 ID）"}：对应的组不在这次勾选里`
      );
      continue;
    }
    const source = normalizeDraftSource(raw, group);
    if (sources.some(item => item.id === source.id)) {
      notes.push(`丢掉了重复的日志源 ${source.id}`);
      continue;
    }
    sources.push(source);
  }
  const keys: LogCorrelationKey[] = [];
  for (const raw of Array.isArray(draft?.correlation_keys)
    ? draft.correlation_keys
    : []) {
    const key = text(raw?.key).trim();
    if (!KEY_PATTERN.test(key) || keys.some(item => item.key === key)) {
      notes.push(`丢掉了关联字段 ${key || "（空）"}：key 不合法或重复`);
      continue;
    }
    keys.push({
      key,
      label: (text(raw.label).trim() || KEY_LABELS[key] || key).slice(
        0,
        KEY_LABEL_MAX_LENGTH
      ),
      description: text(raw.description)
        .trim()
        .slice(0, KEY_DESCRIPTION_MAX_LENGTH),
      pattern: patternError(text(raw.pattern)) ? "" : text(raw.pattern).trim(),
      trace: !!raw.trace
    });
  }
  const known = new Set([
    ...sources.map(item => item.id),
    ...form.sources.map(item => item.id)
  ]);
  const bindings: DraftBinding[] = [];
  for (const raw of Array.isArray(draft?.bindings) ? draft.bindings : []) {
    const env = text(raw?.env).trim();
    const source = text(raw?.source).trim();
    if (!env || !known.has(source)) {
      notes.push(`丢掉了绑定 ${env || "?"} / ${source || "?"}：日志源不存在`);
      continue;
    }
    bindings.push({
      env,
      source,
      target: text(raw.target).trim(),
      conditions: cloneConditions(raw.conditions).slice(0, MAX_CONDITIONS),
      note: text(raw.note).trim().slice(0, BINDING_NOTE_MAX_LENGTH)
    });
  }
  const clusterNotes = (
    Array.isArray(draft?.cluster_notes) ? draft.cluster_notes : []
  )
    .map(item => ({
      env: text(item?.env).trim(),
      note: text(item?.note).trim().slice(0, CLUSTER_NOTE_MAX_LENGTH)
    }))
    .filter(item => item.env && item.note);
  return {
    origin: "ai",
    connection: scan?.connection?.id ?? "",
    sources,
    correlation_keys: keys,
    bindings: dedupeBindings(bindings),
    cluster_notes: clusterNotes.filter(
      (item, index) =>
        clusterNotes.findIndex(other => other.env === item.env) === index
    ),
    architecture: text(draft?.architecture)
      .trim()
      .slice(0, ARCHITECTURE_MAX_LENGTH),
    notes,
    model: text(result?.model)
  };
}

export interface DescribePayload {
  payload: LogDescribeRequest;
  /** 为了控制大小去掉了什么（页面上提示） */
  dropped: string[];
  bytes: number;
  /** 只剩一个组也超过上限：不能发送 */
  tooLarge: boolean;
}

/** 每个字段的取值最多 limit 个 */
function limitValues(
  values: LogScanTarget["values"],
  limit: number
): LogScanTarget["values"] {
  if (!values) return undefined;
  return Object.fromEntries(
    Object.entries(values).map(([key, list]) => [
      key,
      Array.isArray(list) ? list.slice(0, limit) : []
    ])
  );
}

function withoutKey<T extends object, K extends keyof T>(
  value: T,
  key: K
): Omit<T, K> {
  const { [key]: _omitted, ...rest } = value;
  return rest;
}

/**
 * 「AI 生成说明」的请求体：只放勾选的组（最多 100 组）和这些组的绑定建议，每组最多 20 个表 / 索引，
 * 样本只留第一个目标的（最多 2 条），每个字段的取值最多 10 个；
 * existing 只放日志源的 id / name、关联字段的 key / label 和网络架构说明。
 * 序列化后超过 1.5MB（nginx 限制 2MB）时依次去掉取值、去掉样本、减少组数，dropped 说明去掉了什么。
 */
export function describePayload(
  scan: LogScanResult,
  refs: string[],
  form: LogSourcesForm,
  hint: string
): DescribePayload {
  const dropped: string[] = [];
  const picked = (scan?.groups ?? []).filter(group => refs.includes(group.ref));
  if (picked.length > DESCRIBE_MAX_GROUPS)
    dropped.push(
      `一次最多给 ${DESCRIBE_MAX_GROUPS} 组生成说明，只发送了前 ${DESCRIBE_MAX_GROUPS} 组（共勾选 ${picked.length} 组），其余的分批再生成`
    );
  let groups: LogScanGroup[] = picked
    .slice(0, DESCRIBE_MAX_GROUPS)
    .map(group => ({
      ...group,
      targets: (group.targets ?? [])
        .slice(0, DESCRIBE_MAX_TARGETS)
        .map((target, index) => {
          const item: LogScanTarget = {
            ...target,
            values: limitValues(target.values, DESCRIBE_MAX_VALUES)
          };
          if (!item.values) delete item.values;
          if (index === 0 && target.samples?.length)
            item.samples = target.samples.slice(0, DESCRIBE_MAX_SAMPLES);
          else delete item.samples;
          return item;
        })
    }));
  const base = {
    clusters: (scan?.clusters ?? []).map(item => ({
      env: item.env,
      aliases: [...(item.aliases ?? [])]
    })),
    existing: {
      sources: form.sources.map(item => ({ id: item.id, name: item.name })),
      correlation_keys: form.correlation_keys.map(item => ({
        key: item.key,
        label: item.label
      })),
      architecture: form.architecture
    },
    hint: text(hint).trim().slice(0, HINT_MAX_LENGTH)
  };
  // 绑定建议只给发送的组；扫描的说明（例如没有权限读某些表）也给模型参考
  const build = (): LogDescribeRequest => {
    const sent = new Set(groups.map(group => group.ref));
    return {
      scan: {
        groups,
        connection: scan?.connection,
        partial: !!scan?.partial,
        notes: stringList(scan?.notes).slice(0, 10),
        suggested_bindings: (scan?.suggested_bindings ?? []).filter(item =>
          sent.has(item.group)
        )
      },
      ...base
    };
  };
  let payload = build();
  let bytes = byteLength(JSON.stringify(payload));
  if (bytes > DESCRIBE_MAX_BYTES) {
    groups = groups.map(group => ({
      ...group,
      targets: group.targets.map(target => withoutKey(target, "values"))
    }));
    dropped.push("去掉了各表 / 索引的字段取值");
    payload = build();
    bytes = byteLength(JSON.stringify(payload));
  }
  if (bytes > DESCRIBE_MAX_BYTES) {
    groups = groups.map(group => ({
      ...group,
      targets: group.targets.map(target => withoutKey(target, "samples"))
    }));
    dropped.push("去掉了样本数据");
    payload = build();
    bytes = byteLength(JSON.stringify(payload));
  }
  if (bytes > DESCRIBE_MAX_BYTES && groups.length > 1) {
    // 按每组序列化后的大小从后往前去掉，最后再整体算一次
    const total = groups.length;
    const sizes = groups.map(group => byteLength(JSON.stringify(group)) + 1);
    let size = bytes;
    let keep = groups.length;
    while (size > DESCRIBE_MAX_BYTES && keep > 1) {
      keep--;
      size -= sizes[keep];
    }
    const omitted = groups
      .slice(keep)
      .map(group => group.name_guess || group.ref);
    groups = groups.slice(0, keep);
    payload = build();
    bytes = byteLength(JSON.stringify(payload));
    dropped.push(
      `请求太大，只发送了前 ${keep} 个组（共勾选 ${total} 个），没发送的：${omitted.join("、")}；可以分批再生成`
    );
  }
  return { payload, dropped, bytes, tooLarge: bytes > DESCRIBE_MAX_BYTES };
}

export interface DraftSourceRow {
  /** 草稿里的 ID */
  id: string;
  /** 应用后的 ID：新增的和已有 ID 重复时加 -2 等 */
  finalId: string;
  name: string;
  kind: LogSourceKind;
  group: string;
  target: string;
  time_field: string;
  /** 配置了几个语义字段 */
  fields: number;
  columns: number;
  description: string;
  /** 已有日志源：能补充的内容（空 = 没有能补充的） */
  adds: string[];
}

export interface DraftBindingRow {
  key: string;
  env: string;
  source: string;
  target: string;
  conditions: string;
  note: string;
  match: string;
  /** 这个集群里已经绑定了这个日志源：不覆盖 */
  exists: boolean;
  /** 日志源在表单里已有（不依赖草稿里的新日志源是否勾选） */
  existingSource: boolean;
  /** KubeDoor 里已登记的集群 */
  registered: boolean;
  /** 表 / 索引格式不对（按日志源的连接类型检查）：应用后要改 */
  problem: string;
}

export interface DraftView {
  newSources: DraftSourceRow[];
  updateSources: DraftSourceRow[];
  /** 按集群分组 */
  bindings: { env: string; registered: boolean; rows: DraftBindingRow[] }[];
  keys: (LogCorrelationKey & { exists: boolean })[];
  notes: { env: string; note: string; overwrite: boolean }[];
  architecture: { text: string; overwrite: boolean } | null;
}

/** 已有日志源能从草稿里补充的内容：空的说明、还没有说明的字段 */
function sourceAdditions(current: SourceForm, item: LogDraftSource): string[] {
  const adds: string[] = [];
  if (!current.description.trim() && text(item.description).trim())
    adds.push("说明");
  const described = new Set(
    current.columns
      .filter(row => row.description.trim())
      .map(row => row.name.trim())
  );
  const columns = new Set(
    (item.columns ?? [])
      .filter(row => text(row?.name).trim() && text(row?.description).trim())
      .map(row => row.name.trim())
      .filter(name => !described.has(name))
  );
  if (columns.size) adds.push(`${columns.size} 个字段说明`);
  return adds;
}

/** 草稿对话框里的分类：将新增的日志源、将补充说明的已有日志源、绑定（按集群）、关联字段、集群说明、网络架构 */
export function draftView(
  form: LogSourcesForm,
  draft: DraftState,
  known: LogKnownCluster[] = []
): DraftView {
  const used = new Set(form.sources.map(item => item.id));
  const row = (
    item: LogDraftSource,
    finalId: string,
    adds: string[],
    name = item.name
  ) => ({
    id: item.id,
    finalId,
    name,
    kind: item.kind,
    group: item.group,
    target: item.default_target ?? "",
    time_field: item.time_field,
    fields: Object.values(item.fields ?? {}).filter(Boolean).length,
    columns: (item.columns ?? []).length,
    description: item.description ?? "",
    adds
  });
  const newSources: DraftSourceRow[] = [];
  const updateSources: DraftSourceRow[] = [];
  for (const item of draft.sources) {
    const current = form.sources.find(source => source.id === item.id);
    if (current) {
      // 已有日志源显示表单里的名称（草稿里的名称不会写进去）
      updateSources.push(
        row(
          item,
          item.id,
          sourceAdditions(current, item),
          current.name || item.name
        )
      );
      continue;
    }
    const finalId = uniqueId(item.id, used);
    used.add(finalId);
    newSources.push(row(item, finalId, []));
  }
  const registered = new Set((known ?? []).map(item => item.env));
  const bound = (env: string, source: string) =>
    form.clusters.some(
      cluster =>
        cluster.env === env &&
        cluster.bindings.some(binding => binding.source === source)
    );
  /** 绑定用的连接类型：已有日志源按它的连接，草稿里的新日志源按草稿的连接 */
  const typeOf = (source: string) => {
    const existing = form.sources.find(item => item.id === source);
    const id = existing ? existing.connection : draft.connection;
    return form.connections.find(item => item.id === id)?.type;
  };
  const groups: DraftView["bindings"] = [];
  for (const item of draft.bindings) {
    let group = groups.find(entry => entry.env === item.env);
    if (!group) {
      group = { env: item.env, registered: registered.has(item.env), rows: [] };
      groups.push(group);
    }
    const existingSource = form.sources.some(
      source => source.id === item.source
    );
    group.rows.push({
      key: bindingKey(item.env, item.source),
      env: item.env,
      source: item.source,
      target: item.target,
      conditions: conditionsText(item.conditions),
      note: item.note,
      match: item.match ? (MATCH_LABELS[item.match] ?? item.match) : "",
      exists: existingSource && bound(item.env, item.source),
      existingSource,
      registered: registered.has(item.env),
      problem: typeOf(item.source)
        ? targetError(typeOf(item.source), item.target)
        : ""
    });
  }
  const keys = new Set(form.correlation_keys.map(item => item.key));
  return {
    newSources,
    updateSources,
    bindings: groups,
    keys: draft.correlation_keys.map(item => ({
      ...item,
      exists: keys.has(item.key)
    })),
    notes: draft.cluster_notes.map(item => ({
      ...item,
      overwrite: !!form.clusters
        .find(cluster => cluster.env === item.env)
        ?.note.trim()
    })),
    architecture: draft.architecture.trim()
      ? {
          text: draft.architecture.trim(),
          overwrite: !!form.architecture.trim()
        }
      : null
  };
}

/** 草稿对话框里每项是否勾选 */
export interface DraftPicks {
  /** 草稿日志源的 ID */
  sources: string[];
  /** bindingKey(env, source) */
  bindings: string[];
  keys: string[];
  /** 集群说明：集群名 */
  notes: string[];
  architecture: boolean;
}

/** 默认勾选：新日志源、有可补充内容的已有日志源、不冲突的绑定、新的关联字段；集群说明、网络架构原来为空的才勾选 */
export function defaultPicks(
  form: LogSourcesForm,
  draft: DraftState,
  known: LogKnownCluster[] = []
): DraftPicks {
  const view = draftView(form, draft, known);
  return {
    sources: [
      ...view.newSources.map(item => item.id),
      ...view.updateSources
        .filter(item => item.adds.length)
        .map(item => item.id)
    ],
    bindings: view.bindings
      .flatMap(group => group.rows)
      .filter(item => !item.exists)
      .map(item => item.key),
    keys: view.keys.filter(item => !item.exists).map(item => item.key),
    notes: view.notes.filter(item => !item.overwrite).map(item => item.env),
    architecture: !!view.architecture && !view.architecture.overwrite
  };
}

export interface MergeResult {
  form: LogSourcesForm;
  /** 做了什么（页面上提示） */
  summary: string[];
}

/**
 * 把勾选的草稿合并进表单（不保存）：
 * - 新日志源：ID 和已有的重复时加 -2 等；关联字段只留存在的 key，正文写法要有正文字段
 * - 已有日志源（ID 相同）：只补空的说明和还没有说明的字段说明，不改其他配置
 * - 绑定：这个集群已经绑定了这个日志源的不覆盖；引用的新日志源没勾选时跳过
 * - 关联字段：按 key 去重，已有的不覆盖；链路 ID 最多一个
 * - 集群说明、网络架构：勾选了就写入（覆盖原来的）
 */
export function mergeDraft(
  form: LogSourcesForm,
  draft: DraftState,
  picks: DraftPicks
): MergeResult {
  const next: LogSourcesForm = {
    ...form,
    correlation_keys: [...form.correlation_keys],
    sources: [...form.sources],
    clusters: form.clusters.map(cluster => ({
      ...cluster,
      bindings: [...cluster.bindings]
    }))
  };
  const count = {
    added: 0,
    updated: 0,
    bindings: 0,
    keys: 0,
    notes: 0,
    architecture: false
  };

  const keys = new Set(next.correlation_keys.map(item => item.key));
  let hasTrace = next.correlation_keys.some(item => item.trace);
  for (const item of draft.correlation_keys) {
    if (!picks.keys.includes(item.key) || keys.has(item.key)) continue;
    const row = keyForm(item);
    if (row.trace && hasTrace) row.trace = false;
    if (row.trace) hasTrace = true;
    next.correlation_keys.push(row);
    keys.add(item.key);
    count.keys++;
  }

  const ids = new Map<string, string>();
  const used = new Set(next.sources.map(item => item.id));
  for (const item of draft.sources) {
    if (!picks.sources.includes(item.id)) continue;
    // 按原表单判断是不是已有的：这次刚新增的同 ID 草稿（AI 给两个组起了同一个 ID）照样新增
    const index = form.sources.findIndex(source => source.id === item.id);
    if (index >= 0) {
      const current = next.sources[index];
      if (!ids.has(item.id)) ids.set(item.id, current.id);
      let description = current.description;
      if (!description.trim() && text(item.description).trim())
        description = text(item.description).trim();
      const columns = [...current.columns];
      for (const column of item.columns ?? []) {
        const name = text(column?.name).trim();
        const note = text(column?.description).trim();
        if (!name || !note) continue;
        const at = columns.findIndex(row => row.name.trim() === name);
        if (at < 0)
          columns.push({ rowId: nextRowId(), name, description: note });
        else if (!columns[at].description.trim())
          columns[at] = { ...columns[at], description: note };
      }
      const changed =
        description !== current.description ||
        columns.length !== current.columns.length ||
        columns.some((row, at) => row !== current.columns[at]);
      if (changed) {
        next.sources[index] = { ...current, description, columns };
        count.updated++;
      }
      continue;
    }
    const id = uniqueId(item.id, used);
    used.add(id);
    // 草稿里 ID 相同的几个日志源：绑定指向第一个
    if (!ids.has(item.id)) ids.set(item.id, id);
    const source = sourceForm({
      ...item,
      id,
      enabled: true,
      connection: draft.connection,
      mask_fields: [],
      mask_patterns: [],
      default_fields: []
    });
    source.correlations = source.correlations.filter(
      row =>
        keys.has(row.key) &&
        (row.mode === "field" || !!source.fields.message?.trim())
    );
    next.sources.push(source);
    count.added++;
  }

  const clusterOf = (env: string) => {
    let cluster = next.clusters.find(item => item.env === env);
    if (!cluster) {
      cluster = emptyCluster(env);
      next.clusters.push(cluster);
    }
    return cluster;
  };
  for (const item of draft.bindings) {
    if (!picks.bindings.includes(bindingKey(item.env, item.source))) continue;
    let source = ids.get(item.source);
    // 草稿里没有（或没勾选）但表单里已有的日志源，照样可以绑定
    if (!source && form.sources.some(row => row.id === item.source))
      source = item.source;
    if (!source) continue;
    const cluster = clusterOf(item.env);
    if (cluster.bindings.some(binding => binding.source === source)) continue;
    cluster.bindings.push(
      bindingForm({
        source,
        connection: "",
        target: item.target,
        conditions: item.conditions,
        note: item.note
      })
    );
    count.bindings++;
  }

  for (const item of draft.cluster_notes) {
    if (!picks.notes.includes(item.env) || !item.note.trim()) continue;
    clusterOf(item.env).note = item.note.trim();
    count.notes++;
  }

  if (picks.architecture && draft.architecture.trim()) {
    next.architecture = draft.architecture.trim();
    count.architecture = true;
  }

  const summary = [
    count.added ? `新增日志源 ${count.added} 个` : "",
    count.updated ? `补充已有日志源 ${count.updated} 个` : "",
    count.bindings ? `新增绑定 ${count.bindings} 个` : "",
    count.keys ? `新增关联字段 ${count.keys} 个` : "",
    count.notes ? `填写集群说明 ${count.notes} 个` : "",
    count.architecture ? "填写网络架构说明" : ""
  ].filter(Boolean);
  return { form: next, summary };
}
