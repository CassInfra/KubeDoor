// 链路追踪配置页的纯函数：字段映射元数据、表单 ↔ 提交数据、校验（规则同 master tracing/config.py）、
// 识别结果的套用、测试结果和 Jaeger 后端能力的说明文字、链路里的集群取值和 KubeDoor 集群的对照。
// 单测 tests/tracing.test.cjs 用 esbuild 单文件编译加载，这里只能 import type，不能 import 别的模块。
import type {
  CodeInfoHeader,
  TracingAnalyzeResult,
  TracingCapabilities,
  TracingClickHouse,
  TracingClickHouseTest,
  TracingCluster,
  TracingConfigInput,
  TracingFieldKey,
  TracingFieldMap,
  TracingJaegerTest,
  TracingKeyLevel,
  TracingObservedKey,
  TracingSettings
} from "@/api/system";

// ---------------------------------------------------------------------------
// 常量（和 master 的校验一致，改了要两边一起改）
// ---------------------------------------------------------------------------
export const MAX_HEADERS = 10;
export const MIN_TIMEOUT = 5;
export const MAX_TIMEOUT = 120;
export const DEFAULT_TIMEOUT = 30;
export const URL_MAX_LENGTH = 2048;
/** 每个语义字段最多几个候选属性名 */
export const MAX_FIELD_NAMES = 5;
export const ATTR_MAX_LENGTH = 200;
export const MAX_CUSTOM_FIELDS = 50;
export const LABEL_MAX_LENGTH = 50;
export const MAX_MASK_KEYS = 50;
export const MASK_KEY_MAX_LENGTH = 128;
export const CLUSTER_KEY_MAX_LENGTH = 128;
/** 粘贴的 Trace JSON 最大字节数 */
export const TRACE_JSON_MAX_BYTES = 5 * 1024 * 1024;

/** 帮助文档（建表语句、只读账号都在里面） */
export const HELP_URL = `https://github.com/CassInfra/KubeDoor/blob/main/help/${encodeURIComponent("链路追踪配置说明.md")}`;

/** Agent 管理页（维护集群别名） */
export const AGENT_PAGE = "/workbench/index";

export const CLICKHOUSE_DEFAULTS: Omit<
  TracingClickHouse,
  "password" | "has_password" | "clear_password"
> = {
  enabled: false,
  url: "",
  database: "jaeger",
  table: "trace_search",
  dim_table: "trace_search_dim",
  username: "",
  timeout: 20,
  tls_verify: true
};

/** 内置打码的属性名（§6.6），页面上展示用 */
export const BUILTIN_MASK_KEYS = [
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
  "jsessionid"
];

/** 识别字段「从 Jaeger 取样」的时间范围 */
export const LOOKBACK_OPTIONS = [
  { value: "15m", label: "最近 15 分钟" },
  { value: "1h", label: "最近 1 小时" },
  { value: "6h", label: "最近 6 小时" },
  { value: "1d", label: "最近 1 天" }
];

/** 常见的集群字段名：OTEL_RESOURCE_ATTRIBUTES 里自己加的、OTel Operator / k8sattributes 处理器加的 */
export const CLUSTER_KEY_EXAMPLES = ["k8sname", "k8s.cluster.name"];

// ---------------------------------------------------------------------------
// 字段映射（顺序、名称、默认候选同 master tracing/fields.py；页面以 GET 返回的为准，这里是兜底）
// ---------------------------------------------------------------------------
/** any：在 Span 属性和资源属性里都找；event：在 Span 的事件（exception 事件）里找 */
export type FieldLevel = "any" | "event";

export interface FieldMeta {
  label: string;
  level: FieldLevel;
  defaults: string[];
  /** 页面上的说明 */
  hint: string;
}

export const FIELD_KEYS: TracingFieldKey[] = [
  "status_code",
  "method",
  "route",
  "path",
  "url",
  "query",
  "host",
  "client_ip",
  "exception_type",
  "pod",
  "namespace",
  "deployment"
];

export const FIELD_META: Record<TracingFieldKey, FieldMeta> = {
  status_code: {
    label: "HTTP 状态码",
    level: "any",
    defaults: ["http.response.status_code", "http.status_code"],
    hint: "按状态码搜索（504、5xx 等），也用来判断请求最终是否失败"
  },
  method: {
    label: "请求方法",
    level: "any",
    defaults: ["http.request.method", "http.method"],
    hint: "GET、POST 等"
  },
  route: {
    label: "路由模板",
    level: "any",
    defaults: ["http.route"],
    hint: "带占位符的路由，例如 /cs/:ticketId/open；Spring Cloud Gateway 的是路由 ID"
  },
  path: {
    label: "请求路径",
    level: "any",
    defaults: ["url.path", "http.target"],
    hint: "实际请求的路径，例如 /purchase/selectPurchaseBagDetail"
  },
  url: {
    label: "完整 URL",
    level: "any",
    defaults: ["url.full", "http.url"],
    hint: "HTTP 客户端 Span 上调用的完整地址"
  },
  query: {
    label: "查询参数",
    level: "any",
    defaults: ["url.query"],
    hint: "URL 里 ? 后面的部分"
  },
  host: {
    label: "域名 / 被调方地址",
    level: "any",
    defaults: ["server.address", "http.host", "net.host.name", "net.peer.name"],
    hint: "服务端 Span 上是请求的域名，客户端 Span 上是被调方的地址"
  },
  client_ip: {
    label: "客户端 IP",
    level: "any",
    defaults: ["client.address", "http.client_ip", "net.sock.peer.addr"],
    hint: "发起请求的客户端地址"
  },
  exception_type: {
    label: "异常类型",
    level: "event",
    defaults: ["exception.type"],
    hint: "在 Span 的 exception 事件里，例如 java.net.SocketTimeoutException"
  },
  pod: {
    label: "Pod",
    level: "any",
    defaults: ["k8s.pod.name", "host.name"],
    hint: "一般在资源属性上；K8S 里 host.name 通常就是 Pod 名"
  },
  namespace: {
    label: "命名空间",
    level: "any",
    defaults: ["k8s.namespace.name"],
    hint: "一般在资源属性上"
  },
  deployment: {
    label: "Deployment",
    level: "any",
    defaults: ["k8s.deployment.name"],
    hint: "一般在资源属性上"
  }
};

export const LEVEL_LABELS: Record<TracingKeyLevel, string> = {
  span: "Span",
  resource: "资源",
  event: "事件",
  scope: "Scope"
};

export const levelLabel = (level: string) =>
  LEVEL_LABELS[level as TracingKeyLevel] ?? level;

/** GET 返回的默认候选；缺的项用内置的补 */
export function fieldDefaults(
  data?: Partial<TracingSettings> | null
): TracingFieldMap {
  const server = data?.field_defaults;
  return Object.fromEntries(
    FIELD_KEYS.map(key => [
      key,
      Array.isArray(server?.[key])
        ? [...server[key]]
        : [...FIELD_META[key].defaults]
    ])
  ) as TracingFieldMap;
}

/** GET 返回的中文名称；缺的项用内置的补 */
export function fieldLabels(
  data?: Partial<TracingSettings> | null
): Record<TracingFieldKey, string> {
  const server = data?.field_labels;
  return Object.fromEntries(
    FIELD_KEYS.map(key => [key, server?.[key] || FIELD_META[key].label])
  ) as Record<TracingFieldKey, string>;
}

/** GET 返回的层级（空 = 不限，event = 异常事件）；没返回的项用内置的 */
export function fieldLevels(
  data?: Partial<TracingSettings> | null
): Record<TracingFieldKey, FieldLevel> {
  const server = data?.field_levels;
  return Object.fromEntries(
    FIELD_KEYS.map(key => {
      const level = server?.[key];
      return [
        key,
        typeof level === "string"
          ? level === "event"
            ? "event"
            : "any"
          : FIELD_META[key].level
      ];
    })
  ) as Record<TracingFieldKey, FieldLevel>;
}

/** 内置打码的属性名：以 GET 返回的为准 */
export const builtinMaskKeys = (data?: Partial<TracingSettings> | null) =>
  data?.mask_builtin?.length ? data.mask_builtin : BUILTIN_MASK_KEYS;

// ---------------------------------------------------------------------------
// 表单
// ---------------------------------------------------------------------------
export interface HeaderRow {
  id: number;
  name: string;
  /** 只写：留空表示沿用已保存的同名请求头 */
  value: string;
}

export interface CustomFieldRow {
  id: number;
  key: string;
  label: string;
}

export interface ClickHouseForm {
  enabled: boolean;
  url: string;
  database: string;
  table: string;
  dim_table: string;
  username: string;
  /** 只写：留空表示沿用已保存的密码 */
  password: string;
  /** 已保存过密码 */
  has_password: boolean;
  /** 点了「清除」：保存或测试时带 clear_password: true，清空已保存的密码（填的密码不提交）；重新加载后复位 */
  clear_password: boolean;
  timeout: number;
  tls_verify: boolean;
}

export interface TracingForm {
  enabled: boolean;
  url: string;
  ui_url: string;
  headers: HeaderRow[];
  /** 已保存过值的请求头名称（小写）：值留空时沿用，同 master ext_api.normalize_headers */
  savedHeaders: string[];
  timeout: number;
  tls_verify: boolean;
  cluster_key: string;
  fields: TracingFieldMap;
  custom_fields: CustomFieldRow[];
  mask_keys: string[];
  clickhouse: ClickHouseForm;
}

let rowSeq = 0;
/** 请求头、业务字段每一行的 key（v-for 用） */
export const nextRowId = () => ++rowSeq;

export function emptyForm(): TracingForm {
  return formFromSettings(null);
}

/** GET / PUT 返回的配置 → 页面表单（请求头的值、密码不回显） */
export function formFromSettings(
  data?: Partial<TracingSettings> | null
): TracingForm {
  const defaults = fieldDefaults(data);
  const ch = { ...CLICKHOUSE_DEFAULTS, ...(data?.clickhouse ?? {}) };
  const headers = (data?.headers ?? []).filter(item => item?.name);
  return {
    enabled: !!data?.enabled,
    url: data?.url ?? "",
    ui_url: data?.ui_url ?? "",
    headers: headers.map(item => ({
      id: nextRowId(),
      name: item.name,
      value: ""
    })),
    // 只回显名称的请求头都是存过值的（master 不保存没有值的请求头）
    savedHeaders: headers
      .filter(item => item.has_value !== false)
      .map(item => item.name.toLowerCase()),
    timeout: data?.timeout ?? DEFAULT_TIMEOUT,
    tls_verify: data?.tls_verify !== false,
    cluster_key: data?.cluster_key ?? "",
    fields: Object.fromEntries(
      FIELD_KEYS.map(key => {
        const saved = data?.fields?.[key];
        return [key, Array.isArray(saved) ? [...saved] : [...defaults[key]]];
      })
    ) as TracingFieldMap,
    custom_fields: (data?.custom_fields ?? []).map(item => ({
      id: nextRowId(),
      key: item?.key ?? "",
      label: item?.label ?? ""
    })),
    mask_keys: [...(data?.mask_keys ?? [])],
    clickhouse: {
      enabled: !!ch.enabled,
      url: ch.url ?? "",
      database: ch.database ?? "",
      table: ch.table ?? "",
      dim_table: ch.dim_table ?? "",
      username: ch.username ?? "",
      password: "",
      has_password: !!data?.clickhouse?.has_password,
      clear_password: false,
      timeout: ch.timeout ?? CLICKHOUSE_DEFAULTS.timeout,
      tls_verify: ch.tls_verify !== false
    }
  };
}

/** 去掉首尾空白和末尾的 /（同 master） */
export const normalizeUrl = (value: string) =>
  (value ?? "").trim().replace(/\/+$/, "");

/** 去空白、去空、去重（属性名区分大小写），保持顺序 */
export function cleanNames(list: string[] | null | undefined): string[] {
  const result: string[] = [];
  for (const item of list ?? []) {
    const name = String(item ?? "").trim();
    if (name && !result.includes(name)) result.push(name);
  }
  return result;
}

/** 去空白、去空、去重（不区分大小写，打码按小写比较），保持顺序 */
function cleanCaseless(list: string[] | null | undefined): string[] {
  const result: string[] = [];
  const seen = new Set<string>();
  for (const item of list ?? []) {
    const name = String(item ?? "").trim();
    if (name && !seen.has(name.toLowerCase())) {
      seen.add(name.toLowerCase());
      result.push(name);
    }
  }
  return result;
}

const usedHeaders = (rows: HeaderRow[]) =>
  rows.filter(row => row.name.trim() || row.value);

const usedCustomFields = (rows: CustomFieldRow[]) =>
  rows.filter(row => row.key.trim() || row.label.trim());

/**
 * 表单 → 提交的配置（不含 enabled：保存时另加 enabled: true，测试 / 识别时加 enabled: false）。
 * 请求头的值留空传 null、密码留空不传，都表示沿用已保存的；名称和值都空的请求头、业务字段忽略。
 * 点了「清除」密码时带 clear_password: true，不传密码（master 会忽略同时传来的密码）。
 */
export function formPayload(form: TracingForm): TracingConfigInput {
  const ch = form.clickhouse;
  const clickhouse: Partial<TracingClickHouse> = {
    enabled: ch.enabled,
    url: normalizeUrl(ch.url),
    database: ch.database.trim(),
    table: ch.table.trim(),
    dim_table: ch.dim_table.trim(),
    username: ch.username.trim(),
    timeout: ch.timeout,
    tls_verify: ch.tls_verify
  };
  if (ch.clear_password) clickhouse.clear_password = true;
  else if (ch.password) clickhouse.password = ch.password;
  const headers: CodeInfoHeader[] = usedHeaders(form.headers).map(row => ({
    name: row.name.trim(),
    value: row.value || null
  }));
  return {
    url: normalizeUrl(form.url),
    ui_url: normalizeUrl(form.ui_url),
    headers,
    timeout: form.timeout,
    tls_verify: form.tls_verify,
    cluster_key: form.cluster_key.trim(),
    fields: Object.fromEntries(
      FIELD_KEYS.map(key => [key, cleanNames(form.fields[key])])
    ) as TracingFieldMap,
    custom_fields: usedCustomFields(form.custom_fields).map(row => ({
      key: row.key.trim(),
      label: row.label.trim()
    })),
    mask_keys: cleanCaseless(form.mask_keys),
    clickhouse
  };
}

/** 测试连接 / 识别字段用的配置：页面上的完整配置，不看启用状态 */
export const testPayload = (form: TracingForm): TracingConfigInput => ({
  ...formPayload(form),
  enabled: false
});

/** 判断有没有未保存的修改：和加载 / 保存时的快照比较 */
export const payloadKey = (form: TracingForm) =>
  JSON.stringify(formPayload(form));

/** 请求头的值留空时沿用已保存的值（名称不区分大小写） */
export const keepsSavedHeader = (form: TracingForm, row: HeaderRow) =>
  form.savedHeaders.includes(row.name.trim().toLowerCase());

/** ClickHouse 卡片默认折叠；启用了或者填过地址时展开 */
export const clickhouseExpanded = (data?: Partial<TracingSettings> | null) =>
  !!(data?.clickhouse?.enabled || data?.clickhouse?.url);

/** ClickHouse 密码框的提示：将清除 / 已配置 / 没有密码 */
export function passwordPlaceholder(ch: ClickHouseForm): string {
  if (ch.clear_password) return "将清除，保存后生效";
  return ch.has_password ? "已配置，留空表示不修改" : "没有密码可以不填";
}

// ---------------------------------------------------------------------------
// 校验（规则同约定 §2.1；不合法时 master 也会返回 400 和原因）
// ---------------------------------------------------------------------------
export type SectionId =
  | "jaeger"
  | "cluster"
  | "clickhouse"
  | "fields"
  | "custom"
  | "mask";

export interface FormError {
  section: SectionId;
  text: string;
}

const HEADER_NAME = /^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,100}$/;
const RESERVED_HEADERS = [
  "host",
  "content-type",
  "content-length",
  "transfer-encoding",
  "connection"
];
const CLUSTER_KEY = /^[A-Za-z0-9_.\-/:@]+$/;
const IDENTIFIER = /^[A-Za-z_][A-Za-z0-9_]{0,63}$/;

const hasControlChar = (value: string) =>
  Array.from(value).some(ch => {
    const code = ch.charCodeAt(0);
    return code < 32 || code === 127;
  });

/** 属性名：1~200 个字符，不能有空白和控制字符 */
function attrNameError(name: string, maxLength = ATTR_MAX_LENGTH): string {
  if (name.length > maxLength)
    return `${name.slice(0, 40)}… 太长了（最多 ${maxLength} 个字符）`;
  if (/\s/.test(name) || hasControlChar(name))
    return `「${name}」不能有空格或换行`;
  return "";
}

/** 地址：http(s)、有主机名、不带查询参数和 #、≤2048；required 时不能为空（返回 EMPTY） */
const EMPTY = "必须填写";

export function urlError(raw: string, required: boolean): string {
  const value = normalizeUrl(raw);
  if (!value) return required ? EMPTY : "";
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

/** 地址的完整错误说明：没填是「请填写 xx」，格式不对是「xx：原因」（几个地址名都以英文开头，中间留空格） */
function urlProblem(label: string, raw: string, required: boolean): string {
  const error = urlError(raw, required);
  if (!error) return "";
  return error === EMPTY ? `请填写 ${label}` : `${label}：${error}`;
}

function timeoutError(value: number): string {
  return Number.isInteger(value) && value >= MIN_TIMEOUT && value <= MAX_TIMEOUT
    ? ""
    : `超时取 ${MIN_TIMEOUT}~${MAX_TIMEOUT} 秒的整数`;
}

export function headerErrors(form: TracingForm): string[] {
  const errors: string[] = [];
  const rows = usedHeaders(form.headers);
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
    if (!row.value && !keepsSavedHeader(form, row))
      errors.push(`请填写 ${name} 的值`);
  }
  return errors;
}

/** Jaeger 连接；requireUrl：启用（保存）或测试时查询地址必填 */
export function jaegerErrors(
  form: TracingForm,
  requireUrl: boolean
): FormError[] {
  const errors: FormError[] = [];
  const add = (text: string) =>
    text && errors.push({ section: "jaeger", text });
  add(urlProblem("Jaeger 查询地址", form.url, requireUrl));
  add(urlProblem("Jaeger UI 地址", form.ui_url, false));
  for (const text of headerErrors(form)) add(`请求头：${text}`);
  const timeout = timeoutError(form.timeout);
  if (timeout) add(`Jaeger 连接：${timeout}`);
  return errors;
}

/** ClickHouse 加速；requireUrl：启用或测试时 HTTP 地址必填 */
export function clickhouseErrors(
  form: TracingForm,
  requireUrl: boolean
): FormError[] {
  const ch = form.clickhouse;
  const errors: FormError[] = [];
  const add = (text: string) =>
    text &&
    errors.push({ section: "clickhouse", text: `ClickHouse 加速 · ${text}` });
  add(urlProblem("HTTP 地址", ch.url, requireUrl || ch.enabled));
  const ident = "只能用字母、数字和下划线，不能以数字开头，最长 64 个字符";
  if (!IDENTIFIER.test(ch.database.trim()))
    add(ch.database.trim() ? `库名${ident}` : "请填写库名");
  if (!IDENTIFIER.test(ch.table.trim()))
    add(ch.table.trim() ? `搜索表${ident}` : "请填写搜索表");
  if (ch.dim_table.trim() && !IDENTIFIER.test(ch.dim_table.trim()))
    add(`汇总表${ident}`);
  const username = ch.username.trim();
  if (username.length > 128 || /\s/.test(username) || hasControlChar(username))
    add("账号不能有空格，最长 128 个字符");
  add(timeoutError(ch.timeout));
  return errors;
}

/** 保存前的完整校验（保存即启用，Jaeger 查询地址必填） */
export function validateForm(form: TracingForm): FormError[] {
  const errors = [...jaegerErrors(form, true)];
  const key = form.cluster_key.trim();
  if (key && (key.length > CLUSTER_KEY_MAX_LENGTH || !CLUSTER_KEY.test(key)))
    errors.push({
      section: "cluster",
      text: `集群字段：只能用字母、数字和 _ . - / : @，最长 ${CLUSTER_KEY_MAX_LENGTH} 个字符`
    });
  errors.push(...clickhouseErrors(form, false));

  for (const field of FIELD_KEYS) {
    const names = cleanNames(form.fields[field]);
    const add = (text: string) =>
      errors.push({
        section: "fields",
        text: `字段映射 · ${FIELD_META[field].label}：${text}`
      });
    if (names.length > MAX_FIELD_NAMES) add(`最多 ${MAX_FIELD_NAMES} 个属性名`);
    for (const name of names) {
      const error = attrNameError(name);
      if (error) add(error);
    }
  }

  const rows = usedCustomFields(form.custom_fields);
  if (rows.length > MAX_CUSTOM_FIELDS)
    errors.push({
      section: "custom",
      text: `业务字段：最多 ${MAX_CUSTOM_FIELDS} 个`
    });
  const seen = new Set<string>();
  rows.forEach((row, index) => {
    const key = row.key.trim();
    const label = row.label.trim();
    const add = (text: string) =>
      errors.push({ section: "custom", text: `业务字段：${text}` });
    if (!key) add(`第 ${index + 1} 行缺少属性名`);
    else {
      const error = attrNameError(key);
      if (error) add(error);
      else if (seen.has(key)) add(`${key} 重复了`);
      seen.add(key);
    }
    if (!label) add(`${key || `第 ${index + 1} 行`} 缺少中文说明`);
    else if (label.length > LABEL_MAX_LENGTH)
      add(`${key} 的说明最多 ${LABEL_MAX_LENGTH} 个字`);
  });

  const masks = cleanCaseless(form.mask_keys);
  if (masks.length > MAX_MASK_KEYS)
    errors.push({
      section: "mask",
      text: `打码字段：最多 ${MAX_MASK_KEYS} 个`
    });
  for (const name of masks) {
    const error = attrNameError(name, MASK_KEY_MAX_LENGTH);
    if (error) errors.push({ section: "mask", text: `打码字段：${error}` });
  }
  return errors;
}

/** 浏览器等多久（毫秒）：配置的超时 × 要发的请求数，再留 15 秒余量 */
export function requestTimeoutMs(seconds: number, calls: number): number {
  const timeout = Number.isFinite(seconds)
    ? Math.min(MAX_TIMEOUT, Math.max(MIN_TIMEOUT, seconds))
    : DEFAULT_TIMEOUT;
  return (timeout * calls + 15) * 1000;
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
  const text =
    typeof data === "string" ? data : data?.message || data?.error || "";
  const status = error?.response?.status;
  if (text) return status ? `HTTP ${status}：${text}` : text;
  if (status) return `HTTP ${status}`;
  return error?.message || fallback;
}

// ---------------------------------------------------------------------------
// 字段映射：候选项、识别结果的建议和套用
// ---------------------------------------------------------------------------
export const sameList = (a: string[], b: string[]) =>
  a.length === b.length && a.every((item, index) => item === b[index]);

export interface FieldOption {
  value: string;
  /** 下拉里的说明：默认 / 样本里出现的次数 */
  note: string;
}

/** 候选属性名的下拉选项：默认候选、当前已选的、识别样本里出现过的（同层级，按次数取前 100 个） */
export function fieldOptions(
  level: FieldLevel,
  defaults: string[],
  current: string[],
  observed?: TracingObservedKey[] | null
): FieldOption[] {
  const counts = new Map<string, number>();
  const wanted = (keyLevel: string) =>
    level === "event"
      ? keyLevel === "event"
      : keyLevel === "span" || keyLevel === "resource";
  for (const item of observed ?? [])
    if (item?.key && wanted(item.level))
      counts.set(item.key, (counts.get(item.key) ?? 0) + (item.count || 0));
  const notes = new Map<string, string[]>();
  const add = (name: string, note: string) => {
    if (!name) return;
    const list = notes.get(name) ?? [];
    if (note && !list.includes(note)) list.push(note);
    notes.set(name, list);
  };
  for (const name of defaults) add(name, "默认");
  for (const name of current) add(name, "");
  const popular = [...counts].sort((a, b) => b[1] - a[1]).slice(0, 100);
  for (const [name, count] of popular) add(name, `样本 ${count} 次`);
  // 默认候选和已选的在样本里出现过的，也标上次数
  for (const [name, count] of counts)
    if (notes.has(name)) add(name, `样本 ${count} 次`);
  return [...notes].map(([value, list]) => ({ value, note: list.join(" · ") }));
}

export type SuggestionState = "apply" | "same" | "none";

export interface SuggestionRow {
  key: TracingFieldKey;
  label: string;
  current: string[];
  suggested: string[];
  /** apply：可以套用；same：和当前一致；none：样本里没有出现已知的属性名 */
  state: SuggestionState;
}

export function suggestionRows(
  fields: TracingFieldMap,
  result: TracingAnalyzeResult | null | undefined,
  labels: Record<TracingFieldKey, string>
): SuggestionRow[] {
  const suggested = result?.suggestions?.fields ?? {};
  return FIELD_KEYS.map(key => {
    const list = cleanNames(suggested[key]).slice(0, MAX_FIELD_NAMES);
    const current = cleanNames(fields[key]);
    const state: SuggestionState = !list.length
      ? "none"
      : sameList(list, current)
        ? "same"
        : "apply";
    return { key, label: labels[key], current, suggested: list, state };
  });
}

/** 一键套用所有能套用的建议，返回新的字段映射和套用了哪些项 */
export function applyAllSuggestions(
  fields: TracingFieldMap,
  result: TracingAnalyzeResult | null | undefined
): { fields: TracingFieldMap; applied: TracingFieldKey[] } {
  const next = Object.fromEntries(
    FIELD_KEYS.map(key => [key, [...(fields[key] ?? [])]])
  ) as TracingFieldMap;
  const applied: TracingFieldKey[] = [];
  for (const row of suggestionRows(fields, result, fieldLabels(null))) {
    if (row.state !== "apply") continue;
    next[row.key] = [...row.suggested];
    applied.push(row.key);
  }
  return { fields: next, applied };
}

// ---------------------------------------------------------------------------
// 集群：链路里的取值 ↔ KubeDoor 的集群名和别名
// ---------------------------------------------------------------------------
/** 取值等于集群名或某个别名的集群（区分大小写，和搜索时的精确匹配一致） */
export function matchEnvs(value: string, clusters: TracingCluster[]): string[] {
  if (!value) return [];
  return (clusters ?? [])
    .filter(item => item.env === value || (item.aliases ?? []).includes(value))
    .map(item => item.env);
}

/** matched：对上一个集群；ambiguous：同时是几个集群的名称或别名；unmatched：对不上；empty：Span 上没有集群字段 */
export type MatchStatus = "matched" | "ambiguous" | "unmatched" | "empty";

export interface ValueMatch {
  status: MatchStatus;
  envs: string[];
}

/**
 * 判断一个集群取值对上了哪个集群。env、ambiguous 是 master 给的结果（反查规则相同）：
 * 本地按页面上的集群列表算不出时以它为准（列表是打开页面时取的，可能旧了）
 */
export function classifyValue(
  value: string,
  env: string | null | undefined,
  clusters: TracingCluster[],
  ambiguous = false
): ValueMatch {
  if (!value) return { status: "empty", envs: [] };
  const envs = matchEnvs(value, clusters);
  if (envs.length > 1 || ambiguous) return { status: "ambiguous", envs };
  if (envs.length === 1) return { status: "matched", envs };
  if (env) return { status: "matched", envs: [env] };
  return { status: "unmatched", envs: [] };
}

/** 取值状态的标签（ClickHouse 测试结果、集群字段候选里用） */
export function matchLabel(match: ValueMatch): {
  type: "success" | "danger" | "warning" | "info";
  text: string;
} {
  switch (match.status) {
    case "matched":
      return { type: "success", text: match.envs[0] };
    case "ambiguous":
      return {
        type: "danger",
        text: match.envs.length
          ? `同时对应 ${match.envs.join("、")}`
          : "同时对应几个集群"
      };
    case "empty":
      return { type: "info", text: "没有集群字段" };
    default:
      return { type: "warning", text: "对不上，到 Agent 管理页加别名" };
  }
}

export type ObservedSource = "clickhouse" | "sample";

/** 测试 / 识别时在链路里看到的一个集群取值 */
export interface ObservedValue {
  value: string;
  env: string | null;
  /** master 判断同时是几个集群的名称或别名 */
  ambiguous?: boolean;
  /** ClickHouse 测试：最近 10 分钟的 Span 数 */
  spans?: number;
  sources: ObservedSource[];
}

export function observedFromClickHouse(
  result: TracingClickHouseTest | null | undefined
): ObservedValue[] {
  return (result?.cluster_values ?? []).map(item => ({
    value: item?.value ?? "",
    env: item?.env ?? null,
    ambiguous: !!item?.ambiguous,
    spans: typeof item?.spans === "number" ? item.spans : undefined,
    sources: ["clickhouse"]
  }));
}

/** 识别样本里「当前集群字段」的取值：集群字段候选里有就用它的全部取值，否则用全部属性里的示例 */
export function observedFromAnalyze(
  result: TracingAnalyzeResult | null | undefined,
  clusterKey: string
): ObservedValue[] {
  const key = (clusterKey ?? "").trim();
  if (!result || !key) return [];
  const candidate = (result.cluster_candidates ?? []).find(
    item => item.key === key
  );
  if (candidate)
    return (candidate.values ?? []).map(item => ({
      value: item.value ?? "",
      env: item.env ?? null,
      ambiguous: !!item.ambiguous,
      sources: ["sample"]
    }));
  const entry = (result.keys ?? []).find(
    item => item.key === key && item.level === "resource"
  );
  return (entry?.examples ?? []).map(value => ({
    value,
    env: null,
    sources: ["sample"]
  }));
}

/** 同一个取值合并成一条（Span 数取 ClickHouse 的，来源合并） */
export function mergeObserved(items: ObservedValue[]): ObservedValue[] {
  const merged = new Map<string, ObservedValue>();
  for (const item of items) {
    const saved = merged.get(item.value);
    if (!saved) {
      merged.set(item.value, { ...item, sources: [...item.sources] });
      continue;
    }
    saved.env = saved.env ?? item.env;
    saved.ambiguous = saved.ambiguous || item.ambiguous;
    if (saved.spans === undefined) saved.spans = item.spans;
    for (const source of item.sources)
      if (!saved.sources.includes(source)) saved.sources.push(source);
  }
  return [...merged.values()];
}

export interface ClusterRow {
  env: string;
  aliases: string[];
  /** 链路里看到的、对上这个集群的取值 */
  observed: ObservedValue[];
}

export interface ClusterOverview {
  rows: ClusterRow[];
  /** 对不上任何集群的取值：提示去 Agent 管理页加别名 */
  unmatched: ObservedValue[];
  /** 同时是几个集群的名称或别名 */
  ambiguous: (ObservedValue & { envs: string[] })[];
  /** 没有集群字段的 Span（ClickHouse 里 k8s_cluster 为空）；null 表示没看到 */
  emptySpans: number | null;
}

/** 集群字段卡片里的对照表：每个集群的名称、别名和链路里看到的取值 */
export function clusterOverview(
  clusters: TracingCluster[],
  observed: ObservedValue[]
): ClusterOverview {
  const rows: ClusterRow[] = (clusters ?? []).map(item => ({
    env: item.env,
    aliases: [...(item.aliases ?? [])],
    observed: []
  }));
  const byEnv = new Map(rows.map(row => [row.env, row]));
  const overview: ClusterOverview = {
    rows,
    unmatched: [],
    ambiguous: [],
    emptySpans: null
  };
  for (const item of observed) {
    const match = classifyValue(item.value, item.env, clusters, item.ambiguous);
    if (match.status === "empty") {
      overview.emptySpans = (overview.emptySpans ?? 0) + (item.spans ?? 0);
    } else if (match.status === "ambiguous") {
      overview.ambiguous.push({ ...item, envs: match.envs });
    } else if (match.status === "matched" && byEnv.has(match.envs[0])) {
      byEnv.get(match.envs[0]).observed.push(item);
    } else if (match.status === "matched") {
      // master 认得、但页面上的集群列表里没有（列表是打开页面时取的）：单独加一行
      const row = { env: match.envs[0], aliases: [], observed: [item] };
      rows.push(row);
      byEnv.set(row.env, row);
    } else {
      overview.unmatched.push(item);
    }
  }
  return overview;
}

/** 一个取值的显示文字：取值 · N 个 Span */
export const observedText = (item: ObservedValue) =>
  item.spans === undefined
    ? item.value
    : `${item.value} · ${item.spans} 个 Span`;

/** 取值的来源说明（鼠标悬停显示） */
export const sourceText = (item: ObservedValue) =>
  item.sources
    .map(source =>
      source === "clickhouse" ? "ClickHouse 最近 10 分钟" : "识别字段的样本"
    )
    .join("、");

export interface Notice {
  type: "success" | "warning" | "info";
  text: string;
}

/** 识别结果里，当前填的集群字段在样本中的情况 */
export function clusterKeyCheck(
  result: TracingAnalyzeResult | null | undefined,
  clusterKey: string
): Notice | null {
  const key = (clusterKey ?? "").trim();
  if (!result) return null;
  if (!key)
    return {
      type: "info",
      text: "还没有填集群字段：从下面的候选里选一个「设为集群字段」，或者按上面「集群字段」里的说明填写"
    };
  const candidate = (result.cluster_candidates ?? []).find(
    item => item.key === key
  );
  if (candidate?.matched_envs?.length)
    return {
      type: "success",
      text: `当前的集群字段 ${key} 在样本里能对上集群：${candidate.matched_envs.join("、")}`
    };
  const entries = (result.keys ?? []).filter(item => item.key === key);
  const resource = entries.find(item => item.level === "resource");
  if (resource) {
    const values = (resource.examples ?? []).join("、");
    return {
      type: "warning",
      text: `样本里有资源属性 ${key}，但取值${values ? `（${values}）` : ""}对不上 KubeDoor 的集群名或别名：到 Agent 管理页给对应集群加别名，别名填链路里的取值`
    };
  }
  if (entries.length)
    return {
      type: "warning",
      text: `${key} 在样本里是${entries.map(item => levelLabel(item.level)).join(" / ")}属性，不是资源属性：集群字段要用资源属性（OTEL_RESOURCE_ATTRIBUTES 里设置的就是资源属性）`
    };
  return {
    type: "warning",
    text: `样本里没有属性 ${key}：检查字段名有没有写错，或者这个服务还没有设置集群属性`
  };
}

/** 集群字段输入框的建议：识别出的候选（对上的多的在前）+ 常见字段名 */
export function clusterKeySuggestions(
  query: string,
  result: TracingAnalyzeResult | null | undefined
): { value: string; note: string }[] {
  const items: { value: string; note: string }[] = [];
  for (const item of result?.cluster_candidates ?? [])
    items.push({
      value: item.key,
      note: `样本里对上 ${(item.matched_envs ?? []).length} 个集群`
    });
  for (const name of CLUSTER_KEY_EXAMPLES)
    if (!items.some(item => item.value === name))
      items.push({ value: name, note: "常见" });
  const text = (query ?? "").trim().toLowerCase();
  return text
    ? items.filter(item => item.value.toLowerCase().includes(text))
    : items;
}

/** 教用户怎么在 OTel Agent 里设置集群属性的示例 */
export function otelEnvExample(clusterKey: string, env: string): string {
  const key = (clusterKey ?? "").trim() || CLUSTER_KEY_EXAMPLES[0];
  return [
    "env:",
    "  - name: OTEL_RESOURCE_ATTRIBUTES",
    `    value: ${key}=${env || "<集群名>"}`
  ].join("\n");
}

// ---------------------------------------------------------------------------
// 测试结果
// ---------------------------------------------------------------------------
export interface InfoRow {
  label: string;
  value: string;
  /** false 时标红 */
  ok?: boolean;
}

/** 后端能力里的枚举值统一成小写短名：OPERATOR_AND / and → and */
const enumName = (value: string) =>
  String(value ?? "")
    .toLowerCase()
    .replace(/^(operator|op|level|filter_level)_/, "");

/** /api/v3/capabilities 的说明（测试连接的结果里显示） */
export function capabilityRows(
  capabilities: TracingCapabilities | null | undefined
): InfoRow[] {
  if (!capabilities)
    return [
      {
        label: "后端能力",
        value: "取不到（Jaeger 2.23 之前没有 /api/v3/capabilities 接口）",
        ok: false
      }
    ];
  const search = capabilities.search ?? {};
  const rows: InfoRow[] = [
    {
      label: "不填服务名搜索",
      value: search.withoutServiceName
        ? "支持"
        : "不支持（搜索时必须指定服务）",
      ok: !!search.withoutServiceName
    }
  ];
  const filter = search.filter;
  if (!filter) {
    rows.push({
      label: "结构化过滤",
      value: "不支持（搜索要用 query.filter，需要 Jaeger 2.23 及以上）",
      ok: false
    });
    return rows;
  }
  const operators = (filter.operators ?? []).map(enumName);
  const missing = ["and", "or", "eq"].filter(op => !operators.includes(op));
  rows.push({
    label: "过滤运算符",
    value:
      (operators.join("、") || "（无）") +
      (missing.length ? `（缺少 ${missing.join("、")}，不能搜索）` : ""),
    ok: !missing.length
  });
  const levels = (filter.levels ?? []).map(enumName);
  rows.push({ label: "过滤层级", value: levels.join("、") || "（无）" });
  rows.push({
    label: "多个条件",
    value: search.sameSpanConjunction
      ? "要落在同一个 Span 上（含它的资源属性）"
      : "未声明"
  });
  return rows;
}

export interface TestView {
  type: "success" | "warning" | "error";
  text: string;
}

export function jaegerTestView(result: TracingJaegerTest): TestView {
  const elapsed = `耗时 ${result.elapsed_ms ?? 0} ms`;
  if (!result.ok)
    return {
      type: "error",
      text: `连接失败：${result.error || "未知原因"}（${elapsed}）`
    };
  const services = `${result.services_count ?? result.services?.length ?? 0} 个服务`;
  if (!result.search_ok)
    return {
      type: "warning",
      text: `连接正常（${services}，${elapsed}），但不能搜索：搜索需要 Jaeger 2.23 及以上；按链路 ID 查看、列服务和接口照常可用`
    };
  return {
    type: "success",
    text: `连接正常（${services}，${elapsed}），可以搜索`
  };
}

/** 缺少的列：每张表一行 */
export function missingColumnLines(
  result: TracingClickHouseTest | null | undefined
): string[] {
  return Object.entries(result?.missing_columns ?? {})
    .filter(([, columns]) => Array.isArray(columns) && columns.length)
    .map(([table, columns]) => `${table} 缺少列：${columns.join("、")}`);
}

export function clickhouseTestView(result: TracingClickHouseTest): TestView {
  const meta = [
    result.version ? `ClickHouse ${result.version}` : "",
    `耗时 ${result.elapsed_ms ?? 0} ms`
  ]
    .filter(Boolean)
    .join("，");
  if (!result.ok)
    return {
      type: "error",
      text: `测试未通过：${result.error || missingColumnLines(result).join("；") || "未知原因"}（${meta}）`
    };
  if (missingColumnLines(result).length)
    return { type: "warning", text: `连接正常，但表里缺少列（${meta}）` };
  return { type: "success", text: `连接正常，表结构符合要求（${meta}）` };
}

// ---------------------------------------------------------------------------
// 识别字段
// ---------------------------------------------------------------------------
export const byteLength = (text: string) =>
  new TextEncoder().encode(text).length;

/** 粘贴的 Trace JSON 先在页面上做个粗检查（详细解析由 master 做） */
export function traceJsonError(text: string): string {
  const value = (text ?? "").trim();
  if (!value) return "请先粘贴 Trace JSON";
  if (!value.startsWith("{"))
    return "要粘贴 JSON 对象（以 { 开头），例如 Jaeger UI 链路页的「Trace JSON」";
  const bytes = byteLength(value);
  if (bytes > TRACE_JSON_MAX_BYTES)
    return `内容有 ${(bytes / 1024 / 1024).toFixed(1)} MB，超过了 5 MB：换一条 Span 少一些的链路`;
  return "";
}

export function analysisSummary(result: TracingAnalyzeResult): string {
  return `分析了 ${result.traces ?? 0} 条链路、${result.spans ?? 0} 个 Span，出现 ${(result.keys ?? []).length} 个属性`;
}

/** 全部属性表的筛选：属性名包含关键字（不区分大小写）+ 层级 */
export function filterKeys(
  keys: TracingObservedKey[],
  text: string,
  level: string
): TracingObservedKey[] {
  const keyword = (text ?? "").trim().toLowerCase();
  return (keys ?? []).filter(
    item =>
      (!level || item.level === level) &&
      (!keyword || item.key.toLowerCase().includes(keyword))
  );
}

/** 业务字段属性名的输入建议：识别样本里的 Span / 资源属性（按次数，最多 50 个） */
export function observedKeySuggestions(
  query: string,
  result: TracingAnalyzeResult | null | undefined
): { value: string }[] {
  const counts = new Map<string, number>();
  for (const item of result?.keys ?? [])
    if (item.level === "span" || item.level === "resource")
      counts.set(item.key, (counts.get(item.key) ?? 0) + (item.count || 0));
  const keyword = (query ?? "").trim().toLowerCase();
  return [...counts]
    .filter(([key]) => !keyword || key.toLowerCase().includes(keyword))
    .sort((a, b) => b[1] - a[1])
    .slice(0, 50)
    .map(([value]) => ({ value }));
}
