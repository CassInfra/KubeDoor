// 消息通知页的纯函数:组装保存 / 测试的请求体、按 IM 类型给输入提示、表单校验、token_hint 显示。
// 单测 tests/notification.test.cjs 用 esbuild 单文件编译加载,这里只能 import type,不能 import 别的模块。
import type {
  NotificationInput,
  NotificationSettings,
  NotifyChannel,
  NotifyChannelInput,
  NotifyTestRequest,
  NotifyTestResult,
  NotifyType
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

/** 说明表(安装向导第 4 步和控制台消息通知页共用,文字保持一致) */
export const NOTICE_ROWS: NoticeRow[] = [
  {
    name: "K8S 事件告警",
    sender: "控制服务 kubedoor-master",
    when: "K8S 事件命中「事件告警规则」时，例如重启、健康检查失败、挂载失败、调度失败、资源压力；不受告警屏蔽影响"
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
