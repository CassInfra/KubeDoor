// 通用配置页的纯函数：配置项元数据（分组、中文名称、校验规则、说明）、表单 ↔ 请求体、校验、未保存判断、默认值显示。
// 单测 tests/general-settings.test.cjs 用 esbuild 单文件编译加载，这里只能 import type，不能 import 别的模块。
// 配置项、校验规则和默认值与 master 的 general_settings 一致，改动要两边一起改。
import type {
  GeneralSettingKey,
  GeneralSettings,
  GeneralSettingsInput,
  GeneralSettingsValues,
  MetricsTestRequest,
  MetricsTestResult
} from "@/api/general-settings";

/** 地址里已保存的密码显示成它；保存时原样保留表示沿用已保存的密码 */
export const PASSWORD_MASK = "******";

/** 地址最长字符数 */
export const URL_MAX_LENGTH = 2048;

export type CardId =
  | "metrics"
  | "access"
  | "oss"
  | "agent"
  | "node_shell"
  | "retention"
  | "auth"
  | "advanced";

export interface CardMeta {
  id: CardId;
  title: string;
  keys: GeneralSettingKey[];
  /** 默认折叠 */
  collapsed?: boolean;
}

/** 页面上的卡片，顺序即显示顺序；每张卡片单独保存 */
export const CARDS: CardMeta[] = [
  {
    id: "metrics",
    title: "时序数据库",
    keys: ["prom_type", "prom_url", "metrics_write_url", "prom_k8s_tag_key"]
  },
  {
    id: "access",
    title: "访问地址",
    keys: ["kubedoor_exturl", "alertmanager_exturl"]
  },
  { id: "oss", title: "对象存储", keys: ["oss_url"] },
  {
    id: "agent",
    title: "Agent 安装",
    keys: [
      "image_repo",
      "image_vmagent",
      "image_kube_state_metrics",
      "image_node_exporter"
    ]
  },
  { id: "node_shell", title: "节点终端", keys: ["node_shell_image"] },
  {
    id: "retention",
    title: "数据保留",
    keys: [
      "retention_events_days",
      "retention_alerts_days",
      "retention_resources_days",
      "retention_audit_days",
      "retention_ai_checkpoint_days"
    ]
  },
  {
    id: "auth",
    title: "登录与安全",
    keys: [
      "auth_session_hours",
      "auth_idle_hours",
      "auth_remember_days",
      "auth_login_max_failures",
      "auth_lock_minutes",
      "auth_cookie_secure",
      "agent_auth_required"
    ]
  },
  {
    id: "advanced",
    title: "高级",
    keys: ["alert_dedup_window", "silence_cache_ttl", "admis_cache_ttl"],
    collapsed: true
  }
];

/** 全部配置项，按页面顺序 */
export const SETTING_KEYS: GeneralSettingKey[] = CARDS.flatMap(
  card => card.keys
);

/**
 * 校验规则：
 * - url：完整的 http(s) 地址，能否带账号密码、是否只有协议主机端口、能否带查询参数见 FieldMeta.url
 * - int：整数，min ~ max；number：数字，min（aboveMin 时不含 min）~ max
 */
export type FieldRule =
  | "prom_type"
  | "url"
  | "tag_key"
  | "image_repo"
  | "image"
  | "node_image"
  | "int"
  | "number"
  | "cookie_secure"
  | "bool";

export type FieldInput = "select" | "text" | "number" | "switch";

export interface FieldOption {
  value: string;
  label: string;
}

/** 地址的规则（和 master general_settings/fields.py 一致） */
export interface UrlRule {
  /** 可以带「账号:密码@」 */
  credentials?: boolean;
  /** 只有协议、主机和端口 */
  base?: boolean;
  /** 可以带查询参数 */
  query?: boolean;
}

export interface FieldMeta {
  /** 中文名称（校验提示里用） */
  label: string;
  rule: FieldRule;
  input: FieldInput;
  url?: UrlRule;
  /** 数字项的范围 */
  min?: number;
  max?: number;
  /** 必须大于 min（不能等于） */
  aboveMin?: boolean;
  /** 数字项的单位 */
  unit?: string;
  /** 输入框下面的说明 */
  hint: string;
  /** 需要特别留意的说明（醒目显示） */
  warning?: string;
  /** 输入框为空时的提示；不填时显示默认值 */
  placeholder?: string;
  /** 占满一行 */
  wide?: boolean;
  /** 地址里的密码显示为 ****** */
  secret?: boolean;
  options?: FieldOption[];
}

export const PROM_TYPE_OPTIONS: FieldOption[] = [
  { value: "Victoria-Metrics-Single", label: "VictoriaMetrics 单机版" },
  { value: "Victoria-Metrics-Cluster", label: "VictoriaMetrics 集群版" },
  { value: "Prometheus", label: "Prometheus" }
];

export const COOKIE_SECURE_OPTIONS: FieldOption[] = [
  { value: "auto", label: "auto（按访问协议判断）" },
  { value: "true", label: "true（总是加 Secure）" },
  { value: "false", label: "false（不加 Secure）" }
];

const URL_AUTH_HINT =
  "需要认证时写成 http://账号:密码@主机:端口，密码里的特殊字符要做 URL 编码。";

export const FIELDS: Record<GeneralSettingKey, FieldMeta> = {
  prom_type: {
    label: "时序库类型",
    rule: "prom_type",
    input: "select",
    options: PROM_TYPE_OPTIONS,
    hint: "按实际使用的时序库选择；AI 助手的自由指标查询只支持 VictoriaMetrics。"
  },
  prom_url: {
    label: "查询地址",
    rule: "url",
    // master 要在后面拼 /api/v1/query 等路径，不能带查询参数
    url: { credentials: true },
    input: "text",
    wide: true,
    secret: true,
    placeholder: "例如 http://192.0.2.11:8428",
    // 格式说明按时序库类型变化，见 fieldHint
    hint: "KubeDoor 查询指标用，由 master 在服务端访问，要能从 master 所在的 Pod 访问到。"
  },
  metrics_write_url: {
    label: "远程写地址",
    rule: "url",
    // vmagent 的远程写地址可以带查询参数（例如 extra_label）
    url: { credentials: true, query: true },
    input: "text",
    wide: true,
    secret: true,
    placeholder: "例如 http://192.0.2.11:8428/api/v1/write",
    hint: "以后网页安装 Agent 时，vmagent 把指标写到这里，要能从各 K8S 集群访问到；没有远程写地址时，安装 Agent 只能选「仅安装 Agent」。"
  },
  prom_k8s_tag_key: {
    label: "集群指标标签 key",
    rule: "tag_key",
    input: "text",
    wide: true,
    hint: '各集群 vmagent 的 external_labels 里区分集群的标签 key，指标里写成 <key>="<集群名>"。',
    warning:
      "要和时序库里已有数据的标签一致；修改不会改 vmalert 规则、Alertmanager 分组、Grafana 看板和已安装集群的采集配置，一般只在接入已有监控时才需要改。"
  },
  kubedoor_exturl: {
    label: "KubeDoor 访问地址",
    rule: "url",
    url: { base: true },
    input: "text",
    wide: true,
    placeholder: "例如 https://kubedoor.example.com",
    hint: "网页安装 Agent 时 Agent 连接控制端的默认地址，告警通知里的「屏蔽」链接也用它。只填协议、主机和端口。"
  },
  alertmanager_exturl: {
    label: "Alertmanager 外部地址",
    rule: "url",
    // kubedoor-alarm 拼成 <地址>/#/alerts?...，不能带账号、查询参数和锚点
    url: {},
    input: "text",
    wide: true,
    placeholder: "可选，例如 http://alertmanager.example.com:9093",
    hint: "可选。没有 KubeDoor 访问地址时，告警屏蔽链接退回到这个地址。"
  },
  oss_url: {
    label: "对象存储上传地址",
    rule: "url",
    url: { credentials: true },
    input: "text",
    wide: true,
    secret: true,
    placeholder: "例如 http://oss.example.com/kubedoor",
    hint: `Java 诊断（dump / jstack / JFR）文件在业务 Pod 里用 HTTP PUT 上传到「<地址>/<集群>/<类型>/<文件名>」，需要业务 Pod 能访问；修改后对新版 Agent 马上生效，旧版 Agent 继续用安装时的地址。${URL_AUTH_HINT}`
  },
  image_repo: {
    label: "镜像仓库",
    rule: "image_repo",
    input: "text",
    wide: true,
    hint: "KubeDoor Agent 镜像从这里拉取：<镜像仓库>/kubedoor-agent:<版本>。"
  },
  image_vmagent: {
    label: "vmagent 镜像",
    rule: "image",
    input: "text",
    wide: true,
    hint: "选「完整接入」时安装的 vmagent，填完整镜像地址（可带 tag）。"
  },
  image_kube_state_metrics: {
    label: "kube-state-metrics 镜像",
    rule: "image",
    input: "text",
    wide: true,
    hint: "需要安装 kube-state-metrics 时使用，填完整镜像地址（可带 tag）。"
  },
  image_node_exporter: {
    label: "node-exporter 镜像",
    rule: "image",
    input: "text",
    wide: true,
    hint: "需要安装 node-exporter 时使用，填完整镜像地址（可带 tag）。"
  },
  node_shell_image: {
    label: "节点终端镜像",
    rule: "node_image",
    input: "text",
    wide: true,
    placeholder: "留空使用 Agent 内置的默认镜像",
    hint: "需要包含 nsenter，留空使用 Agent 内置的默认镜像。内网环境拉不到默认镜像时换成内网镜像，打开节点终端时随请求下发给 Agent。"
  },
  retention_events_days: {
    label: "K8S 事件保留天数",
    rule: "int",
    input: "number",
    min: 0,
    max: 3650,
    unit: "天",
    hint: "K8S 事件记录。"
  },
  retention_alerts_days: {
    label: "指标告警保留天数",
    rule: "int",
    input: "number",
    min: 0,
    max: 3650,
    unit: "天",
    hint: "kubedoor-alarm 入库的指标告警记录。"
  },
  retention_resources_days: {
    label: "高峰期资源保留天数",
    rule: "int",
    input: "number",
    min: 0,
    max: 3650,
    unit: "天",
    hint: "每天采集的高峰期资源数据。"
  },
  retention_audit_days: {
    label: "审计日志保留天数",
    rule: "int",
    input: "number",
    min: 0,
    max: 3650,
    unit: "天",
    hint: "操作审计记录。"
  },
  retention_ai_checkpoint_days: {
    label: "AI 对话检查点保留天数",
    rule: "int",
    input: "number",
    min: 0,
    max: 3650,
    unit: "天",
    hint: "企业微信旧对话的 AI 检查点，由 kubedoor-ai 每 6 小时清理一次；会话和消息记录保留。"
  },
  auth_session_hours: {
    label: "登录会话有效期（小时）",
    rule: "number",
    input: "number",
    min: 0,
    max: 720,
    aboveMin: true,
    unit: "小时",
    hint: "没勾选「记住我」时，登录后最长多久需要重新登录。"
  },
  auth_idle_hours: {
    label: "空闲超时（小时）",
    rule: "number",
    input: "number",
    // 0 = 不限空闲时间（auth/store.py 原来就是这个语义）
    min: 0,
    max: 720,
    unit: "小时",
    hint: "没勾选「记住我」的会话，多久没有操作就需要重新登录；0 = 不限。"
  },
  auth_remember_days: {
    label: "「记住我」有效天数",
    rule: "number",
    input: "number",
    min: 0,
    max: 365,
    aboveMin: true,
    unit: "天",
    hint: "登录时勾选「记住我」的会话有效期。"
  },
  auth_login_max_failures: {
    label: "登录失败锁定次数",
    rule: "int",
    input: "number",
    min: 1,
    max: 100,
    unit: "次",
    hint: "同一账号在同一来源连续登录失败这么多次后锁定；同一账号不分来源失败 4 倍次数也会锁定。"
  },
  auth_lock_minutes: {
    label: "锁定时长（分钟）",
    rule: "int",
    input: "number",
    min: 1,
    max: 1440,
    unit: "分钟",
    hint: "锁定后多久可以再次登录。"
  },
  auth_cookie_secure: {
    label: "Cookie Secure",
    rule: "cookie_secure",
    input: "select",
    options: COOKIE_SECURE_OPTIONS,
    hint: "登录 Cookie 是否加 Secure 属性。auto：按访问协议判断，用 HTTPS 访问（含反向代理传来的 X-Forwarded-Proto: https）时加；true：总是加，只能用 HTTPS 访问，用 HTTP 访问会登录不上；false：不加。"
  },
  agent_auth_required: {
    label: "Agent 连接必须带账号",
    rule: "bool",
    input: "switch",
    // 关闭时下面有一段警告，占满一行
    wide: true,
    hint: "开启时，各集群的 Agent 连接控制端必须带 Agent 账号。"
  },
  alert_dedup_window: {
    label: "事件告警去重窗口（秒）",
    rule: "int",
    input: "number",
    min: 0,
    max: 86400,
    unit: "秒",
    hint: "同一条 K8S 事件在这段时间内只发一次告警；0 = 不去重。"
  },
  silence_cache_ttl: {
    label: "告警屏蔽规则缓存（秒）",
    rule: "int",
    input: "number",
    min: 1,
    max: 3600,
    unit: "秒",
    hint: "kubedoor-alarm 缓存告警屏蔽规则的时间，决定新建或解除屏蔽后最长多久生效。"
  },
  admis_cache_ttl: {
    label: "准入查询缓存（秒）",
    rule: "int",
    input: "number",
    min: 0,
    max: 3600,
    unit: "秒",
    hint: "准入控制查管控值的缓存时间，修改管控表时会主动清空；0 = 不缓存。"
  }
};

/** 接口没给默认值时用的兜底（镜像的默认值以 master 返回的为准） */
export const FALLBACK_DEFAULTS: GeneralSettingsValues = {
  prom_type: "Victoria-Metrics-Single",
  prom_url: "",
  metrics_write_url: "",
  prom_k8s_tag_key: "origin_prometheus",
  kubedoor_exturl: "",
  alertmanager_exturl: "",
  oss_url: "",
  image_repo: "",
  image_vmagent: "",
  image_kube_state_metrics: "",
  image_node_exporter: "",
  node_shell_image: "",
  retention_events_days: 90,
  retention_alerts_days: 365,
  retention_resources_days: 365,
  retention_audit_days: 180,
  retention_ai_checkpoint_days: 7,
  auth_session_hours: 12,
  auth_idle_hours: 4,
  auth_remember_days: 7,
  auth_login_max_failures: 5,
  auth_lock_minutes: 15,
  auth_cookie_secure: "auto",
  agent_auth_required: true,
  alert_dedup_window: 300,
  silence_cache_ttl: 15,
  admis_cache_ttl: 60
};

/** 表单里的值：文本和下拉是字符串，数字是数字（清空为 null），开关是布尔 */
export type FormValue = string | number | boolean | null;
export type FormState = Record<GeneralSettingKey, FormValue>;

/** 判断改动、组装请求体需要的全部状态 */
export interface FormContext {
  /** 当前表单 */
  form: FormState;
  /** 读取 / 保存后的表单，用来判断有没有改动 */
  initial: FormState;
  /** 点了「恢复默认」的项 */
  restores: ReadonlySet<GeneralSettingKey>;
  defaults: GeneralSettingsValues;
}

export function defaultsOf(
  settings: Pick<GeneralSettings, "defaults"> | null | undefined
): GeneralSettingsValues {
  return { ...FALLBACK_DEFAULTS, ...(settings?.defaults ?? {}) };
}

function toFormValue(key: GeneralSettingKey, value: unknown): FormValue {
  const { input } = FIELDS[key];
  if (input === "switch") return value === true;
  if (input === "number") {
    if (value === null || value === undefined || value === "") return null;
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  }
  return value === null || value === undefined ? "" : String(value);
}

/** 接口返回的生效值 → 表单；缺的项用默认值 */
export function formFromValues(
  values: Partial<GeneralSettingsValues> | null | undefined,
  defaults: GeneralSettingsValues
): FormState {
  const form = {} as FormState;
  for (const key of SETTING_KEYS)
    form[key] = toFormValue(key, values?.[key] ?? defaults[key]);
  return form;
}

/** 还没读取到配置时的空表单 */
export function emptyForm(): FormState {
  return formFromValues(FALLBACK_DEFAULTS, FALLBACK_DEFAULTS);
}

/** 某一项的默认值（表单格式） */
export function defaultFormValue(
  key: GeneralSettingKey,
  defaults: GeneralSettingsValues
): FormValue {
  return toFormValue(key, defaults[key]);
}

/** 去掉地址路径末尾的 /（查询参数里的不动；只剩 / 的保留原样，交给校验报错） */
function stripPathSlash(text: string): string {
  const index = text.search(/[?#]/);
  const head = index < 0 ? text : text.slice(0, index);
  const tail = index < 0 ? "" : text.slice(index);
  return (head.replace(/\/+$/, "") || head) + tail;
}

/**
 * 规范化后的值，用于比较和提交：文本去掉首尾空白，地址路径和镜像仓库去掉末尾的 /；
 * 空文本、清空的数字返回 null（提交 null 表示恢复默认值 / 不配置）。
 */
export function normalizeField(
  key: GeneralSettingKey,
  raw: unknown
): string | number | boolean | null {
  const { input, rule } = FIELDS[key];
  if (input === "switch") return raw === true;
  if (input === "number") {
    if (raw === null || raw === undefined || raw === "") return null;
    return typeof raw === "number" ? raw : Number(String(raw).trim());
  }
  const text = String(raw ?? "").trim();
  if (!text) return null;
  if (rule === "url") return stripPathSlash(text);
  if (rule === "image_repo") return text.replace(/\/+$/, "") || text;
  return text;
}

function sameValue(key: GeneralSettingKey, a: unknown, b: unknown): boolean {
  return normalizeField(key, a) === normalizeField(key, b);
}

/** 点了「恢复默认」、而且值还是默认值：保存时提交 null（删掉库里的这一项） */
export function restorePending(
  key: GeneralSettingKey,
  ctx: FormContext
): boolean {
  return (
    ctx.restores.has(key) && sameValue(key, ctx.form[key], ctx.defaults[key])
  );
}

/** 这一项有没有没保存的修改 */
export function keyChanged(key: GeneralSettingKey, ctx: FormContext): boolean {
  return (
    restorePending(key, ctx) || !sameValue(key, ctx.form[key], ctx.initial[key])
  );
}

export function cardOf(id: CardId): CardMeta {
  return CARDS.find(card => card.id === id)!;
}

/** 卡片里改过的项 */
export function changedKeys(id: CardId, ctx: FormContext): GeneralSettingKey[] {
  return cardOf(id).keys.filter(key => keyChanged(key, ctx));
}

function hasSpaceOrControl(text: string): boolean {
  for (const char of text) {
    const code = char.charCodeAt(0);
    if (code < 32 || code === 127 || /\s/.test(char)) return true;
  }
  return false;
}

/** 拆地址：协议（小写）、「账号:密码」（没有 @ 时为 null）、主机和端口；不是 http(s):// 开头返回 null */
function splitUrl(
  text: string
): { scheme: string; userinfo: string | null; address: string } | null {
  const match = /^(https?):\/\/([^/?#]*)/i.exec(text);
  if (!match) return null;
  const authority = match[2];
  const at = authority.lastIndexOf("@");
  return {
    scheme: match[1].toLowerCase(),
    userinfo: at < 0 ? null : authority.slice(0, at),
    address: authority.slice(at + 1)
  };
}

function decoded(text: string): string {
  try {
    return decodeURIComponent(text);
  } catch {
    return text;
  }
}

/** 地址里的账号和密码（已解码）；没有「账号@」时返回 null */
function credentialsOf(
  text: string
): { user: string; password: string } | null {
  const userinfo = splitUrl(text)?.userinfo;
  if (userinfo === null || userinfo === undefined) return null;
  const index = userinfo.indexOf(":");
  return index < 0
    ? { user: decoded(userinfo), password: "" }
    : {
        user: decoded(userinfo.slice(0, index)),
        password: decoded(userinfo.slice(index + 1))
      };
}

/** 主机和端口里的端口：没写端口返回 null */
function portOf(address: string): string | null {
  const index = address.startsWith("[")
    ? address.indexOf("]:") + 1
    : address.lastIndexOf(":");
  return index > 0 ? address.slice(index + 1) : null;
}

/** 地址里的密码是不是已保存密码的占位 ****** */
export function hasMaskedPassword(value: unknown): boolean {
  return credentialsOf(String(value ?? "").trim())?.password === PASSWORD_MASK;
}

/**
 * ****** 表示沿用已保存的密码：要求已保存的地址有密码，账号、协议、主机和端口都没变
 * （和 master 的规则一致，防止改了主机后把已保存的密码发到别的地方）
 */
function maskError(value: string, initial: string): string {
  const current = credentialsOf(value);
  if (current?.password !== PASSWORD_MASK) return "";
  const before = credentialsOf(initial);
  if (before?.password !== PASSWORD_MASK) return "请把 ****** 换成实际密码";
  const now = splitUrl(value)!;
  const saved = splitUrl(initial)!;
  if (
    before.user !== current.user ||
    now.scheme !== saved.scheme ||
    now.address.toLowerCase() !== saved.address.toLowerCase()
  )
    return "改了账号、协议、主机或端口就要重新填写密码：把 ****** 换成实际密码";
  return "";
}

const URL_FORMAT = "需为完整的 http:// 或 https:// 地址";

function urlError(meta: FieldMeta, text: string, initial: string): string {
  const rule = meta.url ?? {};
  if (text.length > URL_MAX_LENGTH) return `不能超过 ${URL_MAX_LENGTH} 个字符`;
  if (hasSpaceOrControl(text) || text.includes("\\"))
    return "不能包含空白、反斜杠或控制字符";
  const value = stripPathSlash(text);
  const parts = splitUrl(value);
  if (!parts?.address) return URL_FORMAT;
  if (value.includes("#")) return "不能包含锚点（#）";
  const port = portOf(parts.address);
  if (port && !(/^\d{1,5}$/.test(port) && +port >= 1 && +port <= 65535))
    return "端口无效";
  try {
    if (!new URL(value).hostname) return URL_FORMAT;
  } catch {
    return URL_FORMAT;
  }
  if (value.includes("?") && (rule.base || !rule.query))
    return "不能包含查询参数（?）";
  if (parts.userinfo !== null) {
    if (!rule.credentials) return "不需要填写账号或密码";
    if (!credentialsOf(value)!.user)
      return "@ 前面缺少账号；不需要认证时去掉「账号:密码@」";
  }
  if (rule.base && /^https?:\/\/[^/?#]*\//i.test(value))
    return "只填写协议、主机和端口，不包含路径";
  return rule.credentials ? maskError(value, initial.trim()) : "";
}

function numberError(meta: FieldMeta, raw: unknown): string {
  if (raw === null || raw === undefined || raw === "") return "";
  const value = typeof raw === "number" ? raw : Number(String(raw).trim());
  const min = meta.min ?? 0;
  const max = meta.max ?? Number.MAX_SAFE_INTEGER;
  if (meta.rule === "int")
    return Number.isInteger(value) && value >= min && value <= max
      ? ""
      : `需为 ${min} 到 ${max} 之间的整数`;
  if (meta.aboveMin)
    return Number.isFinite(value) && value > min && value <= max
      ? ""
      : `需大于 ${min} 且不超过 ${max}`;
  return Number.isFinite(value) && value >= min && value <= max
    ? ""
    : `需在 ${min} 到 ${max} 之间`;
}

/**
 * 校验一项，返回错误原因（不含项目名称），通过返回空串。
 * 文本留空、数字清空表示恢复默认值（地址类表示不配置），都算通过。
 * initial 是读取到的值：判断地址里的 ****** 能不能沿用已保存的密码。
 */
export function validateField(
  key: GeneralSettingKey,
  raw: unknown,
  initial: unknown = ""
): string {
  const meta = FIELDS[key];
  switch (meta.rule) {
    case "bool":
      return typeof raw === "boolean" ? "" : "取值无效";
    case "int":
    case "number":
      return numberError(meta, raw);
    case "prom_type":
    case "cookie_secure":
      return meta.options!.some(option => option.value === raw)
        ? ""
        : "请从下拉列表里选择";
  }
  const text = String(raw ?? "").trim();
  if (!text) return "";
  switch (meta.rule) {
    case "tag_key":
      if (text.length > 128) return "不能超过 128 个字符";
      return /^[A-Za-z_][A-Za-z0-9_]*$/.test(text)
        ? ""
        : "只能含字母、数字、下划线，且不能以数字开头";
    case "image_repo":
      if (!/^[A-Za-z0-9][A-Za-z0-9._:/-]*$/.test(text))
        return "以字母或数字开头，只能含字母、数字和 . _ : / -";
      return text.replace(/\/+$/, "").length > 256 ? "不能超过 256 个字符" : "";
    case "image":
    case "node_image": {
      if (hasSpaceOrControl(text) || /["'`\\]/.test(text))
        return "不能包含空白、引号、反斜杠或控制字符";
      const limit = meta.rule === "node_image" ? 512 : 256;
      return text.length > limit ? `不能超过 ${limit} 个字符` : "";
    }
    default:
      return urlError(meta, text, String(initial ?? ""));
  }
}

/** 卡片里改过的项的校验结果：{项: 原因}，全部通过返回空对象（「恢复默认」的项不用校验） */
export function cardErrors(
  id: CardId,
  ctx: FormContext
): Partial<Record<GeneralSettingKey, string>> {
  const errors: Partial<Record<GeneralSettingKey, string>> = {};
  for (const key of changedKeys(id, ctx)) {
    if (restorePending(key, ctx)) continue;
    const error = validateField(key, ctx.form[key], ctx.initial[key]);
    if (error) errors[key] = error;
  }
  return errors;
}

/**
 * 保存一张卡片的请求体：只带这张卡片里改过的项。「恢复默认」和清空的项提交 null；
 * 地址里没改的 ****** 原样提交（master 沿用已保存的密码）。校验不通过时抛出「名称：原因」。
 */
export function cardPayload(
  id: CardId,
  ctx: FormContext
): GeneralSettingsInput {
  const payload: Record<string, string | number | boolean | null> = {};
  for (const key of changedKeys(id, ctx)) {
    if (restorePending(key, ctx)) {
      payload[key] = null;
      continue;
    }
    const error = validateField(key, ctx.form[key], ctx.initial[key]);
    if (error) throw new Error(`${FIELDS[key].label}：${error}`);
    payload[key] = normalizeField(key, ctx.form[key]);
  }
  return payload as GeneralSettingsInput;
}

/**
 * 用接口返回的配置刷新表单。savedKeys 为 null 表示全部刷新（重新读取）；
 * 否则刚保存的项和没有改动的项换成返回的值，其他卡片里没保存的修改保留（只更新比较基准）。
 * keep：保留了修改的项（它们的「恢复默认」标记也保留）。
 */
export function mergeForm(
  ctx: FormContext,
  fresh: FormState,
  savedKeys: GeneralSettingKey[] | null
): { form: FormState; initial: FormState; keep: GeneralSettingKey[] } {
  const form = { ...fresh };
  const keep: GeneralSettingKey[] = [];
  if (savedKeys) {
    for (const key of SETTING_KEYS) {
      if (savedKeys.includes(key) || !keyChanged(key, ctx)) continue;
      form[key] = ctx.form[key];
      keep.push(key);
    }
  }
  return { form, initial: { ...fresh }, keep };
}

/** 默认值的显示文字 */
export function defaultText(
  key: GeneralSettingKey,
  defaults: GeneralSettingsValues
): string {
  const meta = FIELDS[key];
  const value = defaults[key];
  if (meta.input === "switch") return value ? "开启" : "关闭";
  if (value === null || value === undefined || value === "")
    return key === "node_shell_image" ? "Agent 内置的镜像" : "未配置";
  if (meta.options)
    return (
      meta.options
        .find(option => option.value === value)
        ?.label.split("（")[0] ?? String(value)
    );
  if (meta.input === "number") return `${value} ${meta.unit}`;
  return String(value);
}

/** 输入框为空时的提示 */
export function placeholderOf(
  key: GeneralSettingKey,
  defaults: GeneralSettingsValues
): string {
  const meta = FIELDS[key];
  if (meta.placeholder) return meta.placeholder;
  const value = defaults[key];
  if (value === null || value === undefined || value === "") return "";
  return meta.input === "number" ? `默认 ${value}` : `留空使用默认值 ${value}`;
}

/** 按时序库类型给出查询 / 远程写地址的格式 */
export function metricsExamples(type: unknown): {
  query: string;
  write: string;
} {
  if (type === "Victoria-Metrics-Cluster")
    return {
      query: "http://<vmselect 主机>:8481/select/<租户 ID>/prometheus",
      write: "http://<vminsert 主机>:8480/insert/<租户 ID>/prometheus"
    };
  if (type === "Prometheus")
    return {
      query: "http://<主机>:9090",
      write:
        "http://<主机>:9090/api/v1/write（Prometheus 要开启 --web.enable-remote-write-receiver）"
    };
  return {
    query: "http://<主机>:8428",
    write: "http://<主机>:8428/api/v1/write"
  };
}

/** 输入框下面的说明；两个时序库地址带上按类型变化的格式 */
export function fieldHint(key: GeneralSettingKey, promType: unknown): string {
  const { hint } = FIELDS[key];
  const examples = metricsExamples(promType);
  if (key === "prom_url")
    return `${hint}格式：${examples.query}；${URL_AUTH_HINT}`;
  if (key === "metrics_write_url")
    return `${hint}格式：${examples.write}；${URL_AUTH_HINT}`;
  return hint;
}

/** 测试连接的请求体：用表单里当前的查询地址和标签 key（标签 key 留空用默认值） */
export function metricsTestRequest(ctx: FormContext): MetricsTestRequest {
  const url = normalizeField("prom_url", ctx.form.prom_url);
  if (url === null) throw new Error("请先填写查询地址");
  const urlProblem = validateField(
    "prom_url",
    ctx.form.prom_url,
    ctx.initial.prom_url
  );
  if (urlProblem) throw new Error(`${FIELDS.prom_url.label}：${urlProblem}`);
  const tagProblem = validateField(
    "prom_k8s_tag_key",
    ctx.form.prom_k8s_tag_key
  );
  if (tagProblem)
    throw new Error(`${FIELDS.prom_k8s_tag_key.label}：${tagProblem}`);
  const tag =
    normalizeField("prom_k8s_tag_key", ctx.form.prom_k8s_tag_key) ??
    ctx.defaults.prom_k8s_tag_key;
  return { prom_url: String(url), prom_k8s_tag_key: String(tag) };
}

export interface TestView {
  type: "success" | "warning" | "error";
  text: string;
}

/** 测试请求成功时的结果：按 level 显示（标签查不到数据是 warning），没有 level 时按 ok */
export function testResultView(result: MetricsTestResult): TestView {
  const fallback = result.ok ? "连接正常" : "测试未通过";
  const type =
    result.level === "success" ||
    result.level === "warning" ||
    result.level === "error"
      ? result.level
      : result.ok
        ? "success"
        : "error";
  return { type, text: result.message || fallback };
}

/** 测试请求本身出错（429、校验不通过、超时等）；401 已跳转登录页，返回 null 不再提示 */
export function testErrorView(error: unknown): TestView | null {
  const err = error as any;
  const status: number | undefined = err?.response?.status;
  if (status === 401) return null;
  const data = err?.response?.data;
  const reason =
    data && typeof data === "object" ? data.message || data.error || "" : "";
  // 429 的说明里带着还要等几秒
  if (status === 429)
    return { type: "warning", text: reason || "操作太频繁，请稍后再试" };
  if (reason) return { type: "error", text: String(reason) };
  if (status) return { type: "error", text: `测试失败（HTTP ${status}）` };
  if (err?.code === "ECONNABORTED" || /timeout/i.test(err?.message || ""))
    return { type: "error", text: "测试超时，请稍后再试" };
  return { type: "error", text: "测试失败：连不上服务，请稍后再试" };
}

/** 请求出错的原因：优先用接口返回的 message */
export function errorText(error: unknown, fallback: string): string {
  const err = error as any;
  const data = err?.response?.data;
  return (
    (data && typeof data === "object" ? data.message || data.error : "") ||
    (err?.response?.status ? `HTTP ${err.response.status}` : "") ||
    err?.message ||
    fallback
  );
}

/** 是不是 http 请求的错误：这类错误 http 封装已经弹过提示，页面不要再弹 */
export function fromHttp(error: unknown): boolean {
  const err = error as any;
  return !!(err?.isAxiosError || err?.response || err?.request);
}

/** 上次修改人：旧配置导入显示成中文 */
export function updatedByLabel(by: string | null | undefined): string {
  if (!by) return "系统";
  return by === "legacy-import" ? "旧配置导入" : by;
}

/** 有问题的项，按页面顺序；不认识的项排在最后，名称用 key */
export function problemList(
  problems: Partial<Record<string, string>> | null | undefined
): { key: string; label: string; message: string }[] {
  const order = (key: string) => {
    const index = SETTING_KEYS.indexOf(key as GeneralSettingKey);
    return index < 0 ? SETTING_KEYS.length : index;
  };
  const fields = FIELDS as Record<string, FieldMeta | undefined>;
  return Object.entries(problems ?? {})
    .filter(([, text]) => !!text)
    .sort(([a], [b]) => order(a) - order(b))
    .map(([key, text]) => ({
      key,
      label: fields[key]?.label ?? key,
      message: String(text)
    }));
}
