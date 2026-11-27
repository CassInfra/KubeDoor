// 消息通知页的纯函数:组装保存 / 测试的请求体、按 IM 类型给输入提示、表单校验、token_hint 显示。
// 单测 tests/notification.test.cjs 与 tests/notification-v2.test.cjs 用 esbuild 单文件编译加载,
// 这里只能 import type,不能 import 别的模块。
import type {
  NotificationConfigV2,
  NotificationConfigV2Input,
  NotificationInput,
  NotificationSettings,
  NotifyChannel,
  NotifyChannelInput,
  NotifyChannelV2,
  NotifyChannelV2Input,
  NotifyRouteV2,
  NotifyTestRequest,
  NotifyTestResult,
  NotifyTestV2Request,
  NotifyType,
  ProviderFieldSpec,
  ProviderSchema
} from "@/api/notification";

/** IM 类型,顺序即下拉里的顺序;和 master notify/channels.py 的 TYPES 一致 */
export const NOTIFY_TYPES: NotifyType[] = [
  "wecom",
  "dingding",
  "feishu",
  "slack"
];

export const TYPE_LABELS: Record<NotifyType, string> = {
  wecom: "企业微信",
  dingding: "钉钉",
  feishu: "飞书",
  slack: "Slack"
};

export const TYPE_OPTIONS = NOTIFY_TYPES.map(value => ({
  value,
  label: TYPE_LABELS[value]
}));

export interface TypeHint {
  /** 机器人地址输入框的 placeholder(没有已保存的地址时) */
  token: string;
  /** 机器人地址输入框下面的说明 */
  tokenHelp: string;
  /** 默认 @ 人输入框的 placeholder */
  at: string;
}

/** 按 IM 类型给的输入提示;地址格式和 master channels.normalize_token 认的一致 */
export const TYPE_HINTS: Record<NotifyType, TypeHint> = {
  wecom: {
    token: "粘贴群机器人 Webhook 地址，或 key= 后面那一段",
    tokenHelp:
      "群机器人的 Webhook 地址，形如 https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx",
    at: "企业微信成员的 userid（账号，不是姓名），可以不填"
  },
  dingding: {
    token: "粘贴机器人 Webhook 地址，或 access_token= 后面那一段",
    tokenHelp:
      "自定义机器人的 Webhook 地址，形如 https://oapi.dingtalk.com/robot/send?access_token=xxx；安全设置不支持「加签」",
    at: "钉钉成员的手机号，可以不填"
  },
  feishu: {
    token: "粘贴自定义机器人 Webhook 地址，或 hook/ 后面那一段",
    tokenHelp:
      "自定义机器人的 Webhook 地址，形如 https://open.feishu.cn/open-apis/bot/v2/hook/xxx；安全设置不支持「签名校验」",
    at: "飞书用户的 open_id（ou_ 开头），可以不填"
  },
  slack: {
    token: "粘贴 Incoming Webhook 地址，或 services/ 后面那一段",
    tokenHelp:
      "Incoming Webhook 地址，形如 https://hooks.slack.com/services/T000/B000/xxx",
    at: "Slack 成员 ID（例如 U012AB3CD），可以不填"
  }
};

const NO_HINT: TypeHint = { token: "", tokenHelp: "", at: "" };

/** 某个 IM 类型的输入提示;不认识的类型返回空提示,不让模板报错 */
export function typeHint(type: string): TypeHint {
  return TYPE_HINTS[type as NotifyType] ?? NO_HINT;
}

export interface NoticeRow {
  /** 通知 */
  name: string;
  /** 谁发送 */
  sender: string;
  /** 什么时候发 */
  when: string;
}

/** 说明表(安装向导第 4 步 setup-utils.ts 里有同样的一份,文字保持一致) */
export const NOTICE_ROWS: NoticeRow[] = [
  {
    name: "K8S 事件告警",
    sender: "控制服务 kubedoor-master",
    when: "K8S 事件命中事件告警规则（在系统管理 → 事件告警规则配置）时，例如重启、健康检查失败、挂载失败、调度失败、资源压力；不受告警屏蔽影响"
  },
  {
    name: "运维操作通知",
    sender: "各集群的 kubedoor-agent，经控制服务发送",
    when: "扩缩容、重启、更新镜像（开始、成功、超时）、准入控制改写副本数或资源、定时任务创建或删除失败、取消禁止调度失败、jstack / jmap 诊断结果"
  },
  {
    name: "指标告警",
    sender: "告警服务 kubedoor-alarm（安装了告警组件才有）",
    when: "vmalert 告警规则触发和恢复时；可以按规则屏蔽，消息里带【屏蔽】链接"
  },
  {
    name: "管控表通知",
    sender: "控制服务 kubedoor-master",
    when: "每天采集高峰期数据时，发现新服务并自动加入管控表"
  }
];

export const NOTICE_NOTE =
  "给某个集群单独配置后，这个集群的上述通知都发到它自己的群，其他集群用全局配置。";

/** 页面上一个通道的输入:token 是这次新填的,留空表示沿用已保存的 */
export interface ChannelForm {
  type: NotifyType;
  token: string;
  at: string;
}

/** 按集群配置表格里的一行 */
export interface ClusterForm extends ChannelForm {
  env: string;
  /** 这个集群已保存的配置;新加的行为 null */
  saved: NotifyChannel | null;
}

/** 测试结果在页面上的显示 */
export interface TestView {
  type: "success" | "warning" | "error";
  text: string;
}

const ENV = /^[A-Za-z0-9_-]{1,100}$/;
const MAX_AT = 128;

/** 含控制字符(0x00~0x1f、0x7f) */
const hasControl = (value: string) =>
  [...value].some(ch => {
    const code = ch.charCodeAt(0);
    return code < 0x20 || code === 0x7f;
  });

/** 含除常规换行/制表外的控制字符(用于 textarea) */
const hasUnsafeControl = (value: string) =>
  [...value].some(ch => {
    const code = ch.charCodeAt(0);
    if (code === 0x09 || code === 0x0a || code === 0x0d) return false;
    return code < 0x20 || code === 0x7f;
  });

const WEBHOOK_HOSTS: Array<[RegExp, NotifyType]> = [
  [/^qyapi\.weixin\.qq\.com$/, "wecom"],
  [/^oapi\.dingtalk\.com$/, "dingding"],
  // 发送走 open.feishu.cn;国际版 Lark(open.larksuite.com)的机器人发不了,不当作飞书地址
  [/^open\.feishu\.cn$/, "feishu"],
  [/^hooks\.slack\.com$/, "slack"]
];

/** 粘贴的是整条 Webhook 地址时按域名认出是哪家的;只填了 token 或认不出时返回 "" */
export function webhookType(value: string): NotifyType | "" {
  const match = /^https?:\/\/([^/?#:@]+)(?:[/?#:]|$)/i.exec(value.trim());
  if (!match) return "";
  const host = match[1].toLowerCase();
  for (const [pattern, type] of WEBHOOK_HOSTS)
    if (pattern.test(host)) return type;
  return "";
}

/** 有已保存的地址而且 IM 类型没改:机器人地址留空就沿用已保存的 */
export function keepsSavedToken(
  type: NotifyType,
  saved: NotifyChannel | null | undefined
): boolean {
  return !!saved?.configured && saved.type === type;
}

/** 已保存地址的显示:「已配置（****a1b2）」;没配置时为「未配置」 */
export function tokenHintText(saved: NotifyChannel | null | undefined): string {
  if (!saved?.configured) return "未配置";
  const hint = (saved.token_hint || "").trim();
  if (!hint) return "已配置";
  return `已配置（${hint.startsWith("*") ? hint : `****${hint}`}）`;
}

/** 机器人地址输入框的 placeholder:已配置时提示留空保留,否则按 IM 类型提示怎么填 */
export function tokenPlaceholder(
  type: NotifyType,
  saved: NotifyChannel | null | undefined
): string {
  if (keepsSavedToken(type, saved)) return `${tokenHintText(saved)}，留空保留`;
  if (saved?.configured)
    return `换了 IM 类型，请重新填写${TYPE_LABELS[type]}机器人地址`;
  return typeHint(type).token;
}

/** 安装 Agent 对话框:这个集群在消息通知里已有单独配置时的说明;没有时为空串 */
export function existingClusterText(
  saved: NotifyChannel | null | undefined
): string {
  if (!saved?.configured) return "";
  const label = TYPE_LABELS[saved.type] ?? saved.type;
  const hint = (saved.token_hint || "").trim();
  return `该集群已有单独配置：${label}${hint ? `（${hint}）` : ""}，留空保持不变，填写则覆盖`;
}

/**
 * 检查一个通道的输入,返回要提交的内容;不合格时抛出中文说明。
 * label 是说明里的范围:「全局配置」或「集群 prod-k8s 」(后面带空格,中文前留空)
 */
export function channelInput(
  label: string,
  form: ChannelForm,
  saved: NotifyChannel | null | undefined
): NotifyChannelInput {
  const type = form.type;
  if (!NOTIFY_TYPES.includes(type)) throw new Error(`${label}请选择 IM 类型`);
  const token = (form.token || "").trim();
  const at = (form.at || "").trim();
  if (token) {
    if (/\s/.test(token) || hasControl(token))
      throw new Error(`${label}的机器人地址不能包含空格、换行或控制字符`);
    const detected = webhookType(token);
    if (detected && detected !== type)
      throw new Error(
        `${label}的机器人地址是${TYPE_LABELS[detected]}的，IM 类型却选了${TYPE_LABELS[type]}`
      );
  } else if (!keepsSavedToken(type, saved)) {
    throw new Error(
      saved?.configured
        ? `${label}换了 IM 类型，请重新填写机器人地址`
        : `${label}请填写机器人地址`
    );
  }
  if (hasControl(at)) throw new Error(`${label}的默认 @ 人不能包含控制字符`);
  if ([...at].length > MAX_AT)
    throw new Error(`${label}的默认 @ 人最多 ${MAX_AT} 个字符`);
  return { type, token, at };
}

const clusterLabel = (env: string) => `集群 ${env} `;

/** 已保存的全局配置;没有时为 null */
export function savedDefault(
  settings: NotificationSettings | null | undefined
): NotifyChannel | null {
  return settings?.default?.configured ? settings.default : null;
}

/**
 * 组装 PUT 请求体:全局配置 + 完整的按集群列表(整体替换)。
 * 没保存过全局配置、这次也没填,就不配置全局(删除已保存的全局配置走 withoutDefault)
 */
export function settingsPayload(
  globalForm: ChannelForm,
  savedGlobal: NotifyChannel | null,
  rows: ClusterForm[]
): NotificationInput {
  const filled =
    !!(globalForm.token || "").trim() || !!(globalForm.at || "").trim();
  const defaultInput =
    savedGlobal?.configured || filled
      ? channelInput("全局配置", globalForm, savedGlobal)
      : null;
  const seen = new Set<string>();
  const clusters = rows.map(row => {
    const env = (row.env || "").trim();
    if (!ENV.test(env))
      throw new Error(
        `集群名「${env}」不合规：只能包含字母、数字、下划线和连字符，最长 100 个字符`
      );
    if (seen.has(env)) throw new Error(`集群 ${env} 重复了`);
    seen.add(env);
    return { env, ...channelInput(clusterLabel(env), row, row.saved) };
  });
  return { default: defaultInput, clusters };
}

/** 删除全局配置的请求体:按集群的配置原样保留(token 留空即沿用已保存的) */
export function withoutDefault(
  settings: NotificationSettings
): NotificationInput {
  return {
    default: null,
    clusters: (settings.clusters ?? []).map(item => ({
      env: item.env,
      type: item.type,
      token: "",
      at: item.at ?? ""
    }))
  };
}

/** 测试请求:已填的机器人地址用填的值,没填用已保存的;校验同保存 */
export function testRequest(
  env: string,
  form: ChannelForm,
  saved: NotifyChannel | null | undefined
): NotifyTestRequest {
  const label = env ? clusterLabel(env) : "全局配置";
  return { env, ...channelInput(label, form, saved) };
}

/** test 接口正常返回时的显示:成功是「发送成功」,失败是 IM 返回的错误 */
export function testResultView(
  data: NotifyTestResult | null | undefined
): TestView {
  if (data?.ok) return { type: "success", text: "发送成功" };
  const reason = (data?.message || "").trim();
  return { type: "error", text: reason ? `发送失败：${reason}` : "发送失败" };
}

/**
 * test 接口本身出错时的显示(请求被拒、超时、连不上);
 * 401 返回 null:http 封装已经提示登录过期并跳转登录页
 */
export function testErrorView(error: unknown): TestView | null {
  const err = error as any;
  const status: number | undefined = err?.response?.status;
  if (status === 401) return null;
  if (status === 429)
    return { type: "warning", text: "操作太频繁，请稍后再试" };
  const data = err?.response?.data;
  const reason =
    data && typeof data === "object" ? data.message || data.error || "" : "";
  if (reason) return { type: "error", text: String(reason) };
  if (status)
    return { type: "error", text: `发送测试消息失败（HTTP ${status}）` };
  if (err?.code === "ECONNABORTED" || /timeout/i.test(err?.message || ""))
    return { type: "error", text: "发送测试消息超时，请稍后再试" };
  return { type: "error", text: "发送测试消息失败：连不上服务，请稍后再试" };
}

/** 按已保存的配置生成表单(机器人地址一律留空);没有全局配置时 IM 类型用 fallbackType */
export function formsFromSettings(
  settings: NotificationSettings,
  fallbackType: NotifyType = "wecom"
): { global: ChannelForm; rows: ClusterForm[] } {
  const saved = savedDefault(settings);
  return {
    global: {
      type: saved?.type ?? fallbackType,
      token: "",
      at: saved?.at ?? ""
    },
    rows: (settings.clusters ?? []).map(item => ({
      env: item.env,
      type: item.type,
      token: "",
      at: item.at ?? "",
      saved: item
    }))
  };
}

/** 新增一行:这个集群原来就保存过(删掉又加回来)时沿用已保存的,否则 IM 类型跟全局配置一样 */
export function newClusterRow(
  env: string,
  settings: NotificationSettings | null | undefined,
  globalForm: ChannelForm
): ClusterForm {
  const saved = settings?.clusters?.find(item => item.env === env) ?? null;
  if (saved)
    return { env, type: saved.type, token: "", at: saved.at ?? "", saved };
  return { env, type: globalForm.type, token: "", at: "", saved: null };
}

/** 「新增一行」的下拉:已经单独配置的集群不能重复添加 */
export function envOptions(
  envs: string[],
  rows: Array<{ env: string }>
): Array<{ value: string; label: string; disabled: boolean }> {
  const used = new Set(rows.map(row => row.env));
  return envs.map(env => ({
    value: env,
    label: used.has(env) ? `${env}（已单独配置）` : env,
    disabled: used.has(env)
  }));
}

/**
 * 表单内容的比较键,判断有没有未保存的修改:
 * 没保存过全局配置、也没填地址和 @ 人时只改 IM 类型不算修改;集群按名称排序后比较
 */
export function formKey(
  savedGlobal: NotifyChannel | null,
  globalForm: ChannelForm,
  rows: ClusterForm[]
): string {
  const token = (globalForm.token || "").trim();
  const at = (globalForm.at || "").trim();
  const globalPart =
    !savedGlobal?.configured && !token && !at
      ? null
      : [globalForm.type, token, at];
  const clusterPart = rows
    .map(row => [
      (row.env || "").trim(),
      row.type,
      (row.token || "").trim(),
      (row.at || "").trim()
    ])
    .sort((a, b) => (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0));
  return JSON.stringify([globalPart, clusterPart]);
}

/* ==================== v2 Schema-driven Multi-IM Helpers ==================== */

export const FALLBACK_PROVIDERS: ProviderSchema[] = [
  {
    id: "wecom",
    label: "企业微信",
    description:
      "企业微信群机器人 Webhook，支持公有云与私有化部署域名，UTF-8 4096 字节安全裁剪",
    default_rate_limit: { max_requests: 20, window_seconds: 60 },
    supports_at: true,
    supports_markdown: true,
    fields: [
      {
        key: "domain_preset",
        label: "服务域名",
        kind: "select",
        default: "public",
        options: [
          { value: "public", label: "企业微信公有云 (qyapi.weixin.qq.com)" },
          { value: "custom", label: "私有化部署域名" }
        ]
      },
      {
        key: "custom_domain",
        label: "自定义域名",
        kind: "text",
        placeholder: "https://wecom.internal.example.com",
        help: "仅在选择私有化部署域名时生效，需包含 https://",
        visible_when: { domain_preset: "custom" }
      },
      {
        key: "token",
        label: "机器人地址 / Key",
        kind: "secret",
        required: true,
        secret: true,
        placeholder: "粘贴群机器人 Webhook 地址，或 key= 后面那一段",
        help: "形如 https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx"
      },
      {
        key: "at",
        label: "默认 @ 人",
        kind: "text",
        placeholder: "企业微信成员 userid（多个用逗号分隔，或填 all）",
        help: "支持 userid 列表，或填 all @所有人"
      }
    ]
  },
  {
    id: "feishu",
    label: "飞书 / Lark",
    description:
      "飞书国内版与 Lark 国际版群机器人，支持 HMAC-SHA256 签名校验与分级着色交互卡片",
    default_rate_limit: { max_requests: 100, window_seconds: 60, burst: 5 },
    supports_at: true,
    supports_markdown: true,
    fields: [
      {
        key: "domain_preset",
        label: "平台版本",
        kind: "select",
        default: "feishu",
        options: [
          { value: "feishu", label: "飞书国内版 (open.feishu.cn)" },
          { value: "lark", label: "Lark 国际版 (open.larksuite.com)" },
          { value: "custom", label: "私有化部署域名" }
        ]
      },
      {
        key: "custom_domain",
        label: "自定义域名",
        kind: "text",
        placeholder: "https://open.example.com",
        help: "仅在选择私有化部署域名时生效，需包含 https://",
        visible_when: { domain_preset: "custom" }
      },
      {
        key: "token",
        label: "机器人地址 / Token",
        kind: "secret",
        required: true,
        secret: true,
        placeholder: "粘贴自定义机器人 Webhook 地址，或 hook/ 后面那一段",
        help: "形如 https://open.feishu.cn/open-apis/bot/v2/hook/xxx 或 open.larksuite.com"
      },
      {
        key: "sign_secret",
        label: "签名校验密钥",
        kind: "secret",
        required: false,
        secret: true,
        placeholder: "可选：开启「签名校验」时填写密钥",
        help: "在飞书机器人安全设置开启「签名校验」时填写对应密钥"
      },
      {
        key: "at",
        label: "默认 @ 人",
        kind: "text",
        placeholder: "飞书用户 open_id（ou_ 开头，多个用逗号分隔，或填 all）"
      }
    ]
  },
  {
    id: "dingding",
    label: "钉钉",
    description: "钉钉自定义机器人，支持 HMAC-SHA256 加签与多手机号 / @所有人",
    default_rate_limit: { max_requests: 20, window_seconds: 60 },
    supports_at: true,
    supports_markdown: true,
    fields: [
      {
        key: "domain_preset",
        label: "服务域名",
        kind: "select",
        default: "public",
        options: [
          { value: "public", label: "钉钉公有云 (oapi.dingtalk.com)" },
          { value: "custom", label: "专有钉钉域名" }
        ]
      },
      {
        key: "custom_domain",
        label: "自定义域名",
        kind: "text",
        placeholder: "https://oapi.dingtalk.internal",
        help: "仅在选择专有钉钉域名时生效，需包含 https://",
        visible_when: { domain_preset: "custom" }
      },
      {
        key: "token",
        label: "机器人地址 / Token",
        kind: "secret",
        required: true,
        secret: true,
        placeholder: "粘贴机器人 Webhook 地址，或 access_token= 后面那一段",
        help: "形如 https://oapi.dingtalk.com/robot/send?access_token=xxx"
      },
      {
        key: "sign_secret",
        label: "加签密钥",
        kind: "secret",
        required: false,
        secret: true,
        placeholder: "可选：开启「加签」时填写 SEC... 密钥",
        help: "在钉钉机器人安全设置开启「加签」时填写对应密钥"
      },
      {
        key: "at",
        label: "默认 @ 人",
        kind: "text",
        placeholder: "钉钉成员手机号（多个用逗号分隔，或填 all）"
      }
    ]
  },
  {
    id: "slack",
    label: "Slack",
    description:
      "Slack Incoming Webhook，使用 Block Kit 结构化卡片渲染告警详情与操作链接",
    default_rate_limit: { max_requests: 60, window_seconds: 60, burst: 1 },
    supports_at: true,
    supports_markdown: true,
    fields: [
      {
        key: "token",
        label: "Incoming Webhook / Token",
        kind: "secret",
        required: true,
        secret: true,
        placeholder: "粘贴 Incoming Webhook 地址，或 services/ 后面那一段",
        help: "形如 https://hooks.slack.com/services/T000/B000/xxx"
      },
      {
        key: "at",
        label: "默认 @ 人",
        kind: "text",
        placeholder: "Slack 成员 ID（如 U012AB3CD）或 here / channel"
      }
    ]
  },
  {
    id: "webhook",
    label: "自定义 Webhook",
    description:
      "标准结构化 JSON 事件回调，支持自定义请求头、鉴权 Header、HMAC-SHA256 签名与 Fail-Closed SSRF 防护",
    default_rate_limit: { max_requests: 30, window_seconds: 60 },
    supports_at: false,
    supports_markdown: false,
    fields: [
      {
        key: "url",
        label: "回调地址",
        kind: "text",
        required: true,
        placeholder: "https://hooks.example.com/kubedoor/notify",
        help: "默认仅允许 HTTPS 端点；内网地址需在后端白名单显式放行"
      },
      {
        key: "method",
        label: "请求方法",
        kind: "select",
        default: "POST",
        options: [
          { value: "POST", label: "POST" },
          { value: "PUT", label: "PUT" }
        ]
      },
      {
        key: "allow_http",
        label: "允许明文 HTTP",
        kind: "boolean",
        default: false,
        help: "默认仅允许 HTTPS，仅在受信内网场景下开启"
      },
      {
        key: "headers_json",
        label: "自定义请求头",
        kind: "headers",
        default: {},
        placeholder: "例如 X-Source: kubedoor",
        help: "附加到请求上的非敏感 HTTP Header（密钥请填在下方鉴权 Header 值）"
      },
      {
        key: "auth_header_name",
        label: "鉴权 Header 名",
        kind: "text",
        placeholder: "例如 Authorization 或 X-Api-Key"
      },
      {
        key: "auth_header_value",
        label: "鉴权 Header 值",
        kind: "secret",
        required: false,
        secret: true,
        placeholder: "例如 Bearer xxx（加密保存，留空保留）"
      },
      {
        key: "hmac_header",
        label: "签名 Header 名",
        kind: "text",
        default: "X-KubeDoor-Signature-256",
        placeholder: "X-KubeDoor-Signature-256"
      },
      {
        key: "hmac_secret",
        label: "HMAC 签名密钥",
        kind: "secret",
        required: false,
        secret: true,
        placeholder: "可选：使用 sha256=<hex> 对 JSON 请求体签名"
      }
    ]
  },
  {
    id: "smtp",
    label: "邮件 SMTP",
    description:
      "标准 SMTP 邮件通知（TLS 465 / STARTTLS 587），支持纯文本与安全转义 HTML 正文",
    default_rate_limit: { max_requests: 10, window_seconds: 60 },
    supports_at: false,
    supports_markdown: false,
    fields: [
      {
        key: "host",
        label: "SMTP 服务器",
        kind: "text",
        required: true,
        placeholder: "smtp.example.com"
      },
      {
        key: "port",
        label: "端口",
        kind: "number",
        required: true,
        default: 465,
        min: 1,
        max: 65535
      },
      {
        key: "security",
        label: "加密方式",
        kind: "select",
        default: "ssl",
        options: [
          { value: "ssl", label: "SSL / TLS (465)" },
          { value: "starttls", label: "STARTTLS (587)" },
          { value: "none", label: "无加密 (25)" }
        ]
      },
      {
        key: "username",
        label: "认证账号",
        kind: "text",
        placeholder: "alerts@example.com"
      },
      {
        key: "password",
        label: "认证密码 / 授权码",
        kind: "secret",
        required: false,
        secret: true,
        placeholder: "加密保存，留空保留"
      },
      {
        key: "from_addr",
        label: "发件人地址",
        kind: "text",
        required: true,
        placeholder: "kubedoor@example.com"
      },
      {
        key: "to_addrs",
        label: "收件人地址",
        kind: "textarea",
        required: true,
        placeholder:
          "多个邮箱用逗号或换行分隔，如 sre@example.com, ops@example.com"
      },
      {
        key: "subject_prefix",
        label: "主题前缀",
        kind: "text",
        default: "[KubeDoor]",
        placeholder: "[KubeDoor]"
      }
    ]
  }
];

export const CATEGORY_OPTIONS: Array<{ value: string; label: string }> = [
  { value: "k8s_event", label: "K8S 事件告警" },
  { value: "ops", label: "运维操作通知" },
  { value: "metric_alert", label: "指标告警" },
  { value: "control_table", label: "管控表通知" },
  { value: "generic", label: "通用通知" }
];

export const SEVERITY_OPTIONS: Array<{ value: string; label: string }> = [
  { value: "critical", label: "严重 (critical)" },
  { value: "warning", label: "警告 (warning)" },
  { value: "info", label: "提示 (info)" },
  { value: "resolved", label: "恢复 (resolved)" }
];

export function categoryLabel(category?: string): string {
  if (!category) return "-";
  return (
    CATEGORY_OPTIONS.find(item => item.value === category)?.label ?? category
  );
}

export function severityLabel(severity?: string): string {
  if (!severity) return "-";
  return (
    SEVERITY_OPTIONS.find(item => item.value === severity)?.label ?? severity
  );
}

export interface ChannelFormV2 {
  id: string;
  name: string;
  provider: string;
  enabled: boolean;
  settings: Record<string, any>;
  secrets: Record<string, string>;
  savedProvider: string;
  secretHints: Record<string, string>;
  configuredSecrets: string[];
}

export interface RouteFormV2 {
  id: string;
  name: string;
  enabled: boolean;
  envs: string[];
  categories: string[];
  severities: string[];
  channel_ids: string[];
  continue: boolean;
}

const CHANNEL_ID_RE = /^[A-Za-z0-9_.:-]{1,64}$/;
const IM_WEBHOOK_PROVIDERS = new Set(["wecom", "feishu", "dingding", "slack"]);

const V2_WEBHOOK_HOSTS: Array<[RegExp, string]> = [
  [/^qyapi\.weixin\.qq\.com$/, "wecom"],
  [/^oapi\.dingtalk\.com$/, "dingding"],
  [/^open\.feishu\.cn$/, "feishu"],
  [/^open\.larksuite\.com$/, "feishu"],
  [/^hooks\.slack\.com$/, "slack"]
];

/**
 * 识别常见 IM Webhook URL 所属提供方（含国际版 Lark open.larksuite.com）；
 * 仅填写 token、非内置域名或格式不合法（含 userinfo / 空白）返回 ""。
 */
export function detectProviderFromUrl(value: string): string {
  const trimmed = (value || "").trim();
  if (!trimmed || /\s/.test(trimmed)) return "";
  const match = /^https?:\/\/([^/?#:@]+)(?:[/?#:]|$)/i.exec(trimmed);
  if (!match) return "";
  const host = match[1].toLowerCase();
  for (const [pattern, provider] of V2_WEBHOOK_HOSTS) {
    if (pattern.test(host)) return provider;
  }
  return "";
}

/** 识别飞书国内版 / 国际版 Lark 域名预设；非飞书/Lark URL 返回 "" */
export function detectFeishuPresetFromUrl(
  value: string
): "feishu" | "lark" | "" {
  const match = /^https?:\/\/([^/?#:@]+)(?:[/?#:]|$)/i.exec(
    (value || "").trim()
  );
  if (!match) return "";
  const host = match[1].toLowerCase();
  if (host === "open.larksuite.com") return "lark";
  if (host === "open.feishu.cn") return "feishu";
  return "";
}

function effectiveProviders(providers?: ProviderSchema[]): ProviderSchema[] {
  return providers && providers.length ? providers : FALLBACK_PROVIDERS;
}

export function findProviderSchema(
  providerId: string,
  providers?: ProviderSchema[]
): ProviderSchema | undefined {
  return effectiveProviders(providers).find(
    item => item.id === providerId || item.type === providerId
  );
}

export function providerLabel(
  providerId: string,
  providers?: ProviderSchema[]
): string {
  return (
    findProviderSchema(providerId, providers)?.label ||
    TYPE_LABELS[providerId as NotifyType] ||
    providerId
  );
}

export function formatRateLimit(
  rateLimit: ProviderSchema["default_rate_limit"] | undefined
): string {
  if (!rateLimit) return "";
  if (typeof rateLimit === "string") return rateLimit;
  if (Array.isArray(rateLimit)) {
    return rateLimit.length >= 2 ? `${rateLimit[0]} 次 / ${rateLimit[1]}s` : "";
  }
  if (typeof rateLimit === "object") {
    if (rateLimit.label) return rateLimit.label;
    const maxReq = rateLimit.max_requests ?? rateLimit.limit;
    const windowSec = rateLimit.window_seconds ?? rateLimit.window ?? 60;
    const burst = rateLimit.burst ?? rateLimit.burst_limit;
    if (maxReq) {
      return burst
        ? `${maxReq} 次 / ${windowSec}s（突发 ${burst}/s）`
        : `${maxReq} 次 / ${windowSec}s`;
    }
  }
  return "";
}

export function isFieldVisible(
  field: ProviderFieldSpec,
  settings: Record<string, any>,
  allFields?: ProviderFieldSpec[]
): boolean {
  if (field.visible_when) {
    for (const [depKey, expected] of Object.entries(field.visible_when)) {
      const actual = settings?.[depKey];
      if (Array.isArray(expected)) {
        if (!expected.includes(actual)) return false;
      } else if (actual !== expected) {
        return false;
      }
    }
    return true;
  }
  if (
    (field.key === "custom_domain" || field.key === "custom_base_url") &&
    allFields?.some(item => item.key === "domain_preset")
  ) {
    return settings?.domain_preset === "custom";
  }
  return true;
}

function defaultValueForField(field: ProviderFieldSpec): any {
  if (field.kind === "secret" || field.secret) return "";
  if (field.default !== undefined) {
    if (typeof field.default === "object" && field.default !== null) {
      return { ...field.default };
    }
    return field.default;
  }
  if (field.kind === "boolean") return false;
  if (field.kind === "number") return field.min ?? 0;
  if (field.kind === "headers") return {};
  if (field.kind === "select") return field.options?.[0]?.value ?? "";
  return "";
}

function buildDefaultsFromSchema(schema?: ProviderSchema): {
  settings: Record<string, any>;
  secrets: Record<string, string>;
} {
  const settings: Record<string, any> = {};
  const secrets: Record<string, string> = {};
  for (const field of schema?.fields ?? []) {
    if (field.kind === "secret" || field.secret) {
      secrets[field.key] = "";
    } else {
      settings[field.key] = defaultValueForField(field);
    }
  }
  return { settings, secrets };
}

export function defaultChannelFormV2(
  providerId = "wecom",
  providers?: ProviderSchema[],
  overrides: Partial<ChannelFormV2> = {}
): ChannelFormV2 {
  const schema =
    findProviderSchema(providerId, providers) ??
    effectiveProviders(providers)[0];
  const resolvedProvider = schema?.id || providerId;
  const { settings, secrets } = buildDefaultsFromSchema(schema);
  return {
    id: overrides.id ?? "",
    name: overrides.name ?? "",
    provider: resolvedProvider,
    enabled: overrides.enabled ?? true,
    settings: { ...settings, ...(overrides.settings ?? {}) },
    secrets: { ...secrets, ...(overrides.secrets ?? {}) },
    savedProvider: overrides.savedProvider ?? "",
    secretHints: { ...(overrides.secretHints ?? {}) },
    configuredSecrets: [...(overrides.configuredSecrets ?? [])]
  };
}

export function switchChannelProviderV2(
  form: ChannelFormV2,
  nextProvider: string,
  providers?: ProviderSchema[]
): ChannelFormV2 {
  const schema = findProviderSchema(nextProvider, providers);
  const { settings, secrets } = buildDefaultsFromSchema(schema);
  return {
    ...form,
    provider: schema?.id || nextProvider,
    settings,
    secrets
  };
}

export function channelFormFromSavedV2(
  saved: NotifyChannelV2,
  providers?: ProviderSchema[]
): ChannelFormV2 {
  const schema = findProviderSchema(saved.provider, providers);
  const { settings, secrets } = buildDefaultsFromSchema(schema);
  const savedSettings =
    saved.settings && typeof saved.settings === "object" ? saved.settings : {};
  for (const [key, val] of Object.entries(savedSettings)) {
    if (val !== undefined) settings[key] = val;
  }
  // Ensure headers fields stay easily editable
  for (const field of schema?.fields ?? []) {
    if (field.kind === "headers") {
      try {
        settings[field.key] = parseHeadersValue(settings[field.key]);
      } catch {
        settings[field.key] = {};
      }
    }
  }
  const secretHints: Record<string, string> = { ...(saved.secret_hints ?? {}) };
  const configuredSet = new Set<string>(saved.configured_secrets ?? []);
  for (const key of Object.keys(secretHints)) {
    configuredSet.add(key);
  }
  if (saved.configured && configuredSet.size === 0) {
    for (const field of schema?.fields ?? []) {
      if ((field.kind === "secret" || field.secret) && field.required) {
        configuredSet.add(field.key);
      }
    }
  }
  return {
    id: saved.id,
    name: saved.name,
    provider: saved.provider,
    enabled: saved.enabled !== false,
    settings,
    secrets,
    savedProvider: saved.provider,
    secretHints,
    configuredSecrets: [...configuredSet]
  };
}

export function keepsSavedSecretV2(
  form: Pick<
    ChannelFormV2,
    "provider" | "savedProvider" | "secretHints" | "configuredSecrets"
  >,
  fieldKey: string
): boolean {
  if (!form.savedProvider || form.provider !== form.savedProvider) return false;
  return (
    Object.hasOwn(form.secretHints ?? {}, fieldKey) ||
    (form.configuredSecrets ?? []).includes(fieldKey)
  );
}

export function secretPlaceholderV2(
  field: ProviderFieldSpec,
  form: ChannelFormV2
): string {
  if (keepsSavedSecretV2(form, field.key)) {
    const hint = (form.secretHints?.[field.key] || "").trim();
    if (!hint) return "已配置，留空保留";
    const masked = hint.startsWith("*") ? hint : `****${hint}`;
    return `已配置（${masked}），留空保留`;
  }
  if (
    form.savedProvider &&
    form.savedProvider !== form.provider &&
    (Object.keys(form.secretHints ?? {}).length > 0 ||
      (form.configuredSecrets ?? []).length > 0)
  ) {
    return `换了通道类型，请重新填写${field.label}`;
  }
  return field.placeholder || "";
}

export function channelHintSummaryV2(
  channel:
    | Pick<
        ChannelFormV2,
        | "provider"
        | "savedProvider"
        | "secretHints"
        | "configuredSecrets"
        | "secrets"
        | "settings"
      >
    | NotifyChannelV2,
  providers?: ProviderSchema[]
): string {
  const isForm = "secretHints" in channel;
  const hints: Record<string, string> = isForm
    ? (channel.secretHints ?? {})
    : (channel.secret_hints ?? {});
  const configuredList: string[] = isForm
    ? (channel.configuredSecrets ?? [])
    : (channel.configured_secrets ?? []);
  const entries = Object.entries(hints)
    .filter(([, val]) => Boolean((val || "").trim()))
    .map(([key, val]) => `${key}: ${val}`);
  if (entries.length) return `已配置（${entries.join(" / ")}）`;
  if (configuredList.length || (!isForm && channel.configured)) return "已配置";
  const settings = channel.settings ?? {};
  if (channel.provider === "webhook" && settings.url) {
    return String(settings.url);
  }
  if (channel.provider === "smtp" && settings.host) {
    const port = settings.port ? `:${settings.port}` : "";
    const to = settings.to_addrs ? ` → ${settings.to_addrs}` : "";
    return `${settings.host}${port}${to}`;
  }
  const schema = findProviderSchema(channel.provider, providers);
  const hasRequiredSecret = (schema?.fields ?? []).some(
    f => (f.kind === "secret" || f.secret) && f.required
  );
  if (isForm) {
    const entered = Object.values(channel.secrets ?? {}).some(v =>
      Boolean((v || "").trim())
    );
    if (entered) return "待保存（已填写新凭据）";
  }
  return hasRequiredSecret ? "未配置凭据" : "已就绪";
}

export function parseHeadersValue(raw: unknown): Record<string, string> {
  if (raw === undefined || raw === null || raw === "") return {};
  let obj: unknown = raw;
  if (typeof raw === "string") {
    const trimmed = raw.trim();
    if (!trimmed) return {};
    try {
      obj = JSON.parse(trimmed);
    } catch {
      throw new Error("自定义请求头格式不正确，请输入合法的 JSON 对象");
    }
  }
  if (!obj || typeof obj !== "object" || Array.isArray(obj)) {
    throw new Error("自定义请求头必须是键值对对象");
  }
  const result: Record<string, string> = {};
  for (const [k, v] of Object.entries(obj)) {
    const key = String(k).trim();
    if (!key) continue;
    if (hasControl(key) || /\s/.test(key)) {
      throw new Error(`自定义请求头名「${key}」不能包含空白或控制字符`);
    }
    const val = v === undefined || v === null ? "" : String(v);
    if (hasControl(val)) {
      throw new Error(`自定义请求头「${key}」的值不能包含控制字符`);
    }
    result[key] = val;
  }
  return result;
}

export function formatHeadersJson(raw: unknown): string {
  try {
    return JSON.stringify(parseHeadersValue(raw), null, 2);
  } catch {
    return typeof raw === "string" ? raw : "{}";
  }
}

export function buildChannelPayloadV2(
  form: ChannelFormV2,
  providers?: ProviderSchema[]
): NotifyChannelV2Input {
  const schema = findProviderSchema(form.provider, providers);
  if (!schema) {
    throw new Error(`不支持的通知通道类型：${form.provider || "(空)"}`);
  }
  const id = (form.id || "").trim();
  if (!CHANNEL_ID_RE.test(id)) {
    throw new Error(
      `通道 ID「${id || "(空)"}」不合规：只能包含字母、数字、下划线、点、冒号或连字符，最长 64 个字符`
    );
  }
  const name = (form.name || "").trim();
  if (!name) throw new Error(`通道 ${id} 请填写名称`);
  if (hasControl(name)) throw new Error(`通道「${name}」名称不能包含控制字符`);
  if ([...name].length > 100)
    throw new Error(`通道「${name}」名称最多 100 个字符`);

  const settings: Record<string, any> = {};
  const secrets: Record<string, string> = {};
  const channelLabel = `通道「${name}」`;

  for (const field of schema.fields) {
    if (!isFieldVisible(field, form.settings, schema.fields)) {
      continue;
    }
    const isSecret = field.kind === "secret" || field.secret;
    if (isSecret) {
      const rawSecret = form.secrets?.[field.key];
      const val = typeof rawSecret === "string" ? rawSecret.trim() : "";
      if (val) {
        if (hasControl(val)) {
          throw new Error(`${channelLabel}的${field.label}不能包含控制字符`);
        }
        if (field.key === "token" && IM_WEBHOOK_PROVIDERS.has(schema.id)) {
          if (/\s/.test(val)) {
            throw new Error(
              `${channelLabel}的${field.label}不能包含空格或换行`
            );
          }
          const detected = detectProviderFromUrl(val);
          if (detected && detected !== schema.id) {
            throw new Error(
              `${channelLabel}填写的地址属于${providerLabel(detected, providers)}，但当前类型选择了${schema.label}`
            );
          }
        }
        secrets[field.key] = val;
      } else {
        if (field.required && !keepsSavedSecretV2(form, field.key)) {
          const switched =
            form.savedProvider &&
            form.savedProvider !== form.provider &&
            (Object.keys(form.secretHints ?? {}).length > 0 ||
              (form.configuredSecrets ?? []).length > 0);
          throw new Error(
            switched
              ? `${channelLabel}切换了类型，请重新填写${field.label}`
              : `${channelLabel}请填写${field.label}`
          );
        }
        secrets[field.key] = "";
      }
      continue;
    }

    const rawValue = form.settings?.[field.key];
    if (field.kind === "boolean") {
      settings[field.key] = Boolean(rawValue);
    } else if (field.kind === "number") {
      if (
        rawValue === undefined ||
        rawValue === null ||
        String(rawValue).trim() === ""
      ) {
        if (field.required) {
          throw new Error(`${channelLabel}请填写${field.label}`);
        }
        continue;
      }
      const num = Number(rawValue);
      if (!Number.isFinite(num) || !Number.isInteger(num)) {
        throw new Error(`${channelLabel}的${field.label}必须是整数`);
      }
      if (field.min !== undefined && num < field.min) {
        throw new Error(`${channelLabel}的${field.label}不能小于 ${field.min}`);
      }
      if (field.max !== undefined && num > field.max) {
        throw new Error(`${channelLabel}的${field.label}不能大于 ${field.max}`);
      }
      settings[field.key] = num;
    } else if (field.kind === "headers") {
      const parsed = parseHeadersValue(rawValue);
      settings[field.key] =
        typeof field.default === "string" ? JSON.stringify(parsed) : parsed;
    } else {
      const text =
        rawValue === undefined || rawValue === null
          ? ""
          : String(rawValue).trim();
      const controlFail =
        field.kind === "textarea" ? hasUnsafeControl(text) : hasControl(text);
      if (controlFail) {
        throw new Error(`${channelLabel}的${field.label}不能包含控制字符`);
      }
      if (field.required && !text) {
        throw new Error(`${channelLabel}请填写${field.label}`);
      }
      if (
        text &&
        (field.key === "url" ||
          field.key === "custom_domain" ||
          field.key === "custom_base_url")
      ) {
        if (/\s/.test(text)) {
          throw new Error(`${channelLabel}的${field.label}不能包含空格`);
        }
        let parsedUrl: URL;
        try {
          parsedUrl = new URL(text);
        } catch {
          throw new Error(
            `${channelLabel}请填写有效的${field.label}（包含 http:// 或 https://）`
          );
        }
        if (
          !["http:", "https:"].includes(parsedUrl.protocol) ||
          !parsedUrl.hostname
        ) {
          throw new Error(
            `${channelLabel}的${field.label}必须是 HTTP 或 HTTPS 地址`
          );
        }
        if (parsedUrl.username || parsedUrl.password) {
          throw new Error(
            `${channelLabel}的${field.label}不能在 URL 中内嵌账号密码`
          );
        }
        if (
          field.key === "url" &&
          parsedUrl.protocol === "http:" &&
          !form.settings?.allow_http
        ) {
          throw new Error(
            `${channelLabel}的回调地址必须使用 https://（或在受信内网下勾选「允许明文 HTTP」）`
          );
        }
      }
      if (field.key === "at" && [...text].length > MAX_AT) {
        throw new Error(`${channelLabel}的默认 @ 人最多 ${MAX_AT} 个字符`);
      }
      settings[field.key] = text;
    }
  }

  return {
    id,
    name,
    provider: schema.id,
    enabled: form.enabled !== false,
    settings,
    secrets
  };
}

export function defaultRouteFormV2(seq = 1): RouteFormV2 {
  return {
    id: `rt_${seq}`,
    name: `路由规则 ${seq}`,
    enabled: true,
    envs: [],
    categories: [],
    severities: [],
    channel_ids: [],
    continue: false
  };
}

export function routeFormFromSavedV2(route: NotifyRouteV2): RouteFormV2 {
  return {
    id: route.id,
    name: route.name,
    enabled: route.enabled !== false,
    envs: [...(route.match?.envs ?? [])],
    categories: [...(route.match?.categories ?? [])],
    severities: [...(route.match?.severities ?? [])],
    channel_ids: [...(route.channel_ids ?? [])],
    continue: Boolean(route.continue)
  };
}

const cleanStringList = (items: string[] | undefined): string[] => {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const item of items ?? []) {
    const val = String(item || "").trim();
    if (!val || seen.has(val)) continue;
    seen.add(val);
    out.push(val);
  }
  return out;
};

export function buildConfigV2Payload(
  channels: ChannelFormV2[],
  routes: RouteFormV2[],
  defaultChannelIds: string[],
  providers?: ProviderSchema[]
): NotificationConfigV2Input {
  const builtChannels: NotifyChannelV2Input[] = [];
  const channelIds = new Set<string>();

  for (const channel of channels) {
    const built = buildChannelPayloadV2(channel, providers);
    if (channelIds.has(built.id)) {
      throw new Error(`通道 ID「${built.id}」重复了`);
    }
    channelIds.add(built.id);
    builtChannels.push(built);
  }

  const dedupedDefaults = cleanStringList(defaultChannelIds);
  for (const cid of dedupedDefaults) {
    if (!channelIds.has(cid)) {
      throw new Error(`默认兜底通道引用了不存在的通道：${cid}`);
    }
  }

  const builtRoutes: NotifyRouteV2[] = [];
  const routeIds = new Set<string>();
  for (const route of routes) {
    const id = (route.id || "").trim();
    if (!CHANNEL_ID_RE.test(id)) {
      throw new Error(`路由规则 ID「${id || "(空)"}」格式不合规`);
    }
    if (routeIds.has(id)) {
      throw new Error(`路由规则 ID「${id}」重复了`);
    }
    routeIds.add(id);
    const name = (route.name || "").trim();
    if (!name) {
      throw new Error(`路由规则 ${id} 请填写名称`);
    }
    const targetIds = cleanStringList(route.channel_ids);
    if (targetIds.length === 0) {
      throw new Error(`路由规则「${name}」请至少选择一个目标通道`);
    }
    for (const cid of targetIds) {
      if (!channelIds.has(cid)) {
        throw new Error(`路由规则「${name}」引用了不存在的通道：${cid}`);
      }
    }
    builtRoutes.push({
      id,
      name,
      enabled: route.enabled !== false,
      match: {
        envs: cleanStringList(route.envs),
        categories: cleanStringList(route.categories),
        severities: cleanStringList(route.severities)
      },
      channel_ids: targetIds,
      continue: Boolean(route.continue)
    });
  }

  return {
    version: 2,
    channels: builtChannels,
    routes: builtRoutes,
    default_channel_ids: dedupedDefaults
  };
}

export function buildTestPayloadV2(
  form: ChannelFormV2,
  options: {
    env?: string;
    category?: string;
    severity?: string;
    providers?: ProviderSchema[];
  } = {}
): NotifyTestV2Request {
  const effectiveForm: ChannelFormV2 = form.id?.trim()
    ? form
    : { ...form, id: "draft_test", name: form.name?.trim() || "测试通道" };
  const built = buildChannelPayloadV2(effectiveForm, options.providers);
  const request: NotifyTestV2Request = {
    provider: built.provider,
    name: built.name,
    settings: built.settings,
    secrets: built.secrets
  };
  if (
    form.id?.trim() &&
    form.savedProvider &&
    form.savedProvider === form.provider
  ) {
    request.channel_id = form.id.trim();
  }
  if (options.env?.trim()) request.env = options.env.trim();
  if (options.category?.trim()) request.category = options.category.trim();
  if (options.severity?.trim()) request.severity = options.severity.trim();
  return request;
}

export function routeSummary(
  route: NotifyRouteV2 | RouteFormV2,
  channelNames?: Record<string, string> | Map<string, string>
): string {
  const envs = cleanStringList(
    "match" in route ? route.match?.envs : route.envs
  );
  const categories = cleanStringList(
    "match" in route ? route.match?.categories : route.categories
  );
  const severities = cleanStringList(
    "match" in route ? route.match?.severities : route.severities
  );
  const targetIds = cleanStringList(route.channel_ids);
  const resolveName = (id: string) =>
    channelNames instanceof Map
      ? channelNames.get(id) || id
      : channelNames?.[id] || id;
  const matchParts = [
    envs.length ? `集群: ${envs.join(", ")}` : "全部集群",
    categories.length
      ? `类别: ${categories.map(categoryLabel).join(", ")}`
      : "全部类别",
    severities.length
      ? `级别: ${severities.map(severityLabel).join(", ")}`
      : "全部级别"
  ];
  const targets = targetIds.length
    ? targetIds.map(resolveName).join("、")
    : "未选择通道";
  const tail = route.continue ? "继续后续规则" : "命中即停止";
  const prefix = route.enabled === false ? "[已停用] " : "";
  return `${prefix}${matchParts.join(" · ")} → ${targets}（${tail}）`;
}

export function normalizeProvidersList(raw: unknown): ProviderSchema[] {
  const list = Array.isArray(raw)
    ? raw
    : raw && typeof raw === "object" && Array.isArray((raw as any).providers)
      ? (raw as any).providers
      : [];
  if (!list.length) return FALLBACK_PROVIDERS;
  return list.map((item: any) => ({
    ...item,
    id: item.id || item.type,
    fields: Array.isArray(item.fields) ? item.fields : []
  }));
}

export function normalizeConfigV2Response(raw: unknown): NotificationConfigV2 {
  const src = (raw && typeof raw === "object" ? raw : {}) as any;
  let channels: NotifyChannelV2[] = [];
  if (Array.isArray(src.channels)) {
    channels = src.channels;
  } else if (src.channels && typeof src.channels === "object") {
    channels = Object.entries(src.channels).map(
      ([id, entry]: [string, any]) => ({
        id,
        name: entry?.name || id,
        provider: entry?.provider || entry?.type || "wecom",
        enabled: entry?.enabled !== false,
        settings: entry?.settings ?? {},
        secret_hints: entry?.secret_hints ?? {},
        configured_secrets: entry?.configured_secrets,
        configured: entry?.configured
      })
    );
  }
  const routes: NotifyRouteV2[] = Array.isArray(src.routes) ? src.routes : [];
  const default_channel_ids: string[] = Array.isArray(src.default_channel_ids)
    ? src.default_channel_ids
    : [];
  const envs: string[] = Array.isArray(src.envs) ? src.envs : [];
  return {
    version: 2,
    channels,
    routes,
    default_channel_ids,
    envs,
    updated_by: src.updated_by ?? null,
    updated_at: src.updated_at ?? null
  };
}

export function configV2Key(
  channels: ChannelFormV2[],
  routes: RouteFormV2[],
  defaultChannelIds: string[]
): string {
  const normalizedChannels = channels.map(ch => ({
    id: (ch.id || "").trim(),
    name: (ch.name || "").trim(),
    provider: ch.provider,
    enabled: ch.enabled !== false,
    settings: Object.keys(ch.settings ?? {})
      .sort()
      .reduce<Record<string, any>>((acc, k) => {
        acc[k] = ch.settings[k];
        return acc;
      }, {}),
    secrets: Object.keys(ch.secrets ?? {})
      .sort()
      .reduce<Record<string, string>>((acc, k) => {
        acc[k] = (ch.secrets[k] || "").trim();
        return acc;
      }, {})
  }));
  const normalizedRoutes = routes.map(rt => ({
    id: (rt.id || "").trim(),
    name: (rt.name || "").trim(),
    enabled: rt.enabled !== false,
    envs: cleanStringList(rt.envs),
    categories: cleanStringList(rt.categories),
    severities: cleanStringList(rt.severities),
    channel_ids: cleanStringList(rt.channel_ids),
    continue: Boolean(rt.continue)
  }));
  return JSON.stringify([
    normalizedChannels,
    normalizedRoutes,
    cleanStringList(defaultChannelIds)
  ]);
}

/** 将 v1 响应平滑投影为 v2 结构（用于旧版后端降级只读/兼容展示） */
export function v1SettingsToConfigV2(
  settings: NotificationSettings
): NotificationConfigV2 {
  const channels: NotifyChannelV2[] = [];
  const routes: NotifyRouteV2[] = [];
  const default_channel_ids: string[] = [];

  if (settings.default?.configured) {
    channels.push({
      id: "ch_default",
      name: `全局默认（${TYPE_LABELS[settings.default.type] || settings.default.type}）`,
      provider: settings.default.type,
      enabled: true,
      settings: { at: settings.default.at || "" },
      secret_hints: settings.default.token_hint
        ? { token: settings.default.token_hint }
        : { token: "" },
      configured: true
    });
    default_channel_ids.push("ch_default");
  }

  for (const cluster of settings.clusters ?? []) {
    const channelId = `ch_cluster_${cluster.env}`;
    channels.push({
      id: channelId,
      name: `集群 ${cluster.env}`,
      provider: cluster.type,
      enabled: true,
      settings: { at: cluster.at || "" },
      secret_hints: cluster.token_hint
        ? { token: cluster.token_hint }
        : { token: "" },
      configured: cluster.configured
    });
    routes.push({
      id: `rt_cluster_${cluster.env}`,
      name: `集群 ${cluster.env} 专属规则`,
      enabled: true,
      match: { envs: [cluster.env] },
      channel_ids: [channelId],
      continue: false
    });
  }

  return {
    version: 2,
    channels,
    routes,
    default_channel_ids,
    envs: settings.envs ?? [],
    updated_by: settings.updated_by ?? null,
    updated_at: settings.updated_at ?? null
  };
}
