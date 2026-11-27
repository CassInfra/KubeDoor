// 公共默认模型与多平台 IM 机器人管理的纯函数与表单校验。
// 这个文件只能有 type import：tests/im-bots.test.cjs 用 esbuild 单文件编译后直接加载。
import type {
  AIReasoningEffort,
  FeishuDomainPreset,
  IMBindingState,
  IMBot,
  IMBotInput,
  IMGroupPolicy,
  IMPlatform,
  SharedModelConfig,
  SharedModelInput,
  WeComBinding
} from "@/api/ai";

export const DEFAULT_IDLE_MINUTES = 120;
export const NO_CLUSTER = "__none__";
export const NO_CLUSTER_LABEL = "无（每个新对话先选集群）";

export const PLATFORM_LABELS: Record<IMPlatform, string> = {
  wecom: "企业微信",
  feishu: "飞书 / Lark"
};

export const PLATFORM_OPTIONS: ReadonlyArray<{
  value: IMPlatform;
  label: string;
}> = [
  { value: "wecom", label: "企业微信" },
  { value: "feishu", label: "飞书 / Lark" }
];

export const GROUP_POLICY_OPTIONS: ReadonlyArray<{
  value: IMGroupPolicy;
  label: string;
  desc: string;
}> = [
  {
    value: "disabled",
    label: "仅私聊（默认）",
    desc: "忽略群聊消息，仅允许成员与机器人私聊"
  },
  {
    value: "mention",
    label: "群聊需 @机器人",
    desc: "允许群聊触发，但必须显式 @机器人"
  },
  {
    value: "allowlist",
    label: "群聊白名单且需 @机器人",
    desc: "仅响应白名单内的群聊，且必须 @机器人"
  }
];

export const FEISHU_DOMAIN_OPTIONS: ReadonlyArray<{
  value: FeishuDomainPreset;
  label: string;
  domain: string;
}> = [
  {
    value: "feishu",
    label: "飞书中国版 (open.feishu.cn)",
    domain: "https://open.feishu.cn"
  },
  {
    value: "lark",
    label: "国际版 Lark (open.larksuite.com)",
    domain: "https://open.larksuite.com"
  },
  {
    value: "custom",
    label: "私有化部署域名",
    domain: ""
  }
];

export interface SharedModelForm {
  base_url: string;
  api_key: string;
  model: string;
  max_input_tokens: number | null;
  max_output_tokens: number | null;
  compression_ratio: number | null;
  reasoning_effort: AIReasoningEffort | null;
}

export interface BotModelOverrideForm {
  enabled: boolean;
  base_url: string;
  api_key: string;
  api_key_set: boolean;
  model: string;
  max_input_tokens: number | null;
  max_output_tokens: number | null;
  compression_ratio: number | null;
  reasoning_effort: AIReasoningEffort | null;
}

export interface BotForm {
  id: string;
  platform: IMPlatform;
  name: string;
  enabled: boolean;
  bot_id: string;
  bot_secret: string;
  app_id: string;
  app_secret: string;
  domain_preset: FeishuDomainPreset;
  custom_domain: string;
  secret_set: boolean;
  session_idle_minutes: number;
  group_policy: IMGroupPolicy;
  allowed_chat_ids_text: string;
  unbound_username: string;
  unbound_default_env: string;
  model_override: BotModelOverrideForm;
}

export function platformLabel(platform: string): string {
  return PLATFORM_LABELS[platform as IMPlatform] || platform;
}

export function groupPolicyLabel(policy: string): string {
  return (
    GROUP_POLICY_OPTIONS.find(item => item.value === policy)?.label || policy
  );
}

export function feishuDomainLabel(
  preset?: FeishuDomainPreset | string,
  customDomain?: string
): string {
  if (preset === "lark") return "open.larksuite.com";
  if (preset === "custom") return customDomain?.trim() || "私有化域名";
  return "open.feishu.cn";
}

export function botStatusInfo(
  bot: Pick<
    IMBot,
    | "enabled"
    | "configured"
    | "connected"
    | "authenticated"
    | "reconnecting"
    | "error"
  >
): {
  label: string;
  type: "info" | "success" | "warning" | "danger";
} {
  if (!bot.enabled) return { label: "已停用", type: "info" };
  if (bot.authenticated) return { label: "已连接", type: "success" };
  if (bot.reconnecting) return { label: "正在重连", type: "warning" };
  if (bot.connected) return { label: "已连接，认证中", type: "warning" };
  if (bot.error) return { label: "连接异常", type: "danger" };
  return { label: "未连接", type: "info" };
}

export function validateHttpApiBaseUrl(
  value: string,
  label = "Base URL"
): string {
  const trimmed = value.trim();
  if (!trimmed) return `请填写 ${label}。`;
  try {
    const url = new URL(trimmed);
    if (
      !["http:", "https:"].includes(url.protocol) ||
      !url.hostname ||
      url.username ||
      url.password ||
      url.search ||
      url.hash
    ) {
      throw new Error();
    }
  } catch {
    return `${label} 必须是有效的 HTTP/HTTPS API 基址（不能包含账号密码、查询参数或片段）。`;
  }
  return "";
}

export function validateHttpsUrl(
  value: string,
  label = "飞书私有化域名"
): string {
  const trimmed = value.trim();
  if (!trimmed) return `请填写${label}。`;
  try {
    const url = new URL(trimmed);
    if (
      url.protocol !== "https:" ||
      !url.hostname ||
      url.username ||
      url.password ||
      url.search ||
      url.hash
    ) {
      throw new Error();
    }
  } catch {
    return `${label} 必须是有效的 HTTPS 地址（例如 https://open.example.com）。`;
  }
  return "";
}

export function defaultSharedModelForm(
  saved?: Partial<SharedModelConfig> | null
): SharedModelForm {
  return {
    base_url: saved?.base_url || "https://api.openai.com/v1",
    api_key: "",
    model: saved?.model || "",
    max_input_tokens: saved?.max_input_tokens ?? null,
    max_output_tokens: saved?.max_output_tokens ?? null,
    compression_ratio: saved?.compression_ratio ?? null,
    reasoning_effort: saved?.reasoning_effort ?? null
  };
}

export function validateSharedModelForm(form: SharedModelForm): string {
  const baseUrlError = validateHttpApiBaseUrl(form.base_url, "Base URL");
  if (baseUrlError) return baseUrlError;
  if (!form.model.trim()) return "请填写模型名。";
  if (
    form.max_input_tokens !== null &&
    (!Number.isFinite(form.max_input_tokens) || form.max_input_tokens < 4096)
  ) {
    return "上下文窗口不能小于 4K token。";
  }
  if (
    form.max_output_tokens !== null &&
    (!Number.isFinite(form.max_output_tokens) ||
      form.max_output_tokens < 1024 ||
      (form.max_input_tokens !== null &&
        form.max_output_tokens > form.max_input_tokens))
  ) {
    return "最大输出须在 1K token 以上，且不能超过上下文窗口。";
  }
  if (
    form.compression_ratio !== null &&
    (!Number.isFinite(form.compression_ratio) ||
      form.compression_ratio < 0.2 ||
      form.compression_ratio > 0.8)
  ) {
    return "压缩阈值须在 20% 到 80% 之间。";
  }
  return "";
}

export function buildSharedModelPayload(
  form: SharedModelForm
): SharedModelInput {
  return {
    base_url: form.base_url.trim().replace(/\/+$/, ""),
    ...(form.api_key.trim() ? { api_key: form.api_key.trim() } : {}),
    model: form.model.trim(),
    max_input_tokens: form.max_input_tokens,
    max_output_tokens: form.max_output_tokens,
    compression_ratio: form.compression_ratio,
    reasoning_effort: form.reasoning_effort
  };
}

export function parseAllowedChatIds(text: string): string[] {
  const seen = new Set<string>();
  const result: string[] = [];
  for (const part of text.split(/[\r\n,，]+/)) {
    const id = part.trim();
    if (!id || seen.has(id)) continue;
    seen.add(id);
    result.push(id);
  }
  return result;
}

export function defaultBotForm(platform: IMPlatform = "wecom"): BotForm {
  return {
    id: "",
    platform,
    name: "",
    enabled: true,
    bot_id: "",
    bot_secret: "",
    app_id: "",
    app_secret: "",
    domain_preset: "feishu",
    custom_domain: "",
    secret_set: false,
    session_idle_minutes: DEFAULT_IDLE_MINUTES,
    group_policy: "disabled",
    allowed_chat_ids_text: "",
    unbound_username: "",
    unbound_default_env: NO_CLUSTER,
    model_override: {
      enabled: false,
      base_url: "https://api.openai.com/v1",
      api_key: "",
      api_key_set: false,
      model: "",
      max_input_tokens: null,
      max_output_tokens: null,
      compression_ratio: null,
      reasoning_effort: null
    }
  };
}

export function botFormFromSaved(bot: IMBot): BotForm {
  const override = bot.model_override;
  return {
    id: bot.id,
    platform: bot.platform,
    name: bot.name || "",
    enabled: !!bot.enabled,
    bot_id: bot.credentials?.bot_id || "",
    bot_secret: "",
    app_id: bot.credentials?.app_id || "",
    app_secret: "",
    domain_preset: bot.credentials?.domain_preset || "feishu",
    custom_domain: bot.credentials?.custom_domain || "",
    secret_set: !!bot.secret_set,
    session_idle_minutes: bot.session_idle_minutes ?? DEFAULT_IDLE_MINUTES,
    group_policy: bot.group_policy || "disabled",
    allowed_chat_ids_text: (bot.allowed_chat_ids || []).join("\n"),
    unbound_username: bot.unbound_username || "",
    unbound_default_env: bot.unbound_default_env || NO_CLUSTER,
    model_override: {
      enabled: !!override,
      base_url: override?.base_url || "https://api.openai.com/v1",
      api_key: "",
      api_key_set: !!override?.api_key_set,
      model: override?.model || "",
      max_input_tokens: override?.max_input_tokens ?? null,
      max_output_tokens: override?.max_output_tokens ?? null,
      compression_ratio: override?.compression_ratio ?? null,
      reasoning_effort: override?.reasoning_effort ?? null
    }
  };
}

export function validateBotForm(form: BotForm): string {
  const name = form.name.trim();
  if (!name) return "请填写机器人名称。";
  if (name.length > 80) return "机器人名称不能超过 80 个字符。";

  if (form.platform === "wecom") {
    if (!form.bot_id.trim()) return "请填写企业微信 Bot ID。";
    if (!form.bot_secret.trim() && !form.secret_set) {
      return "请填写企业微信 Bot Secret。";
    }
  } else if (form.platform === "feishu") {
    if (!form.app_id.trim()) return "请填写飞书 App ID。";
    if (!form.app_secret.trim() && !form.secret_set) {
      return "请填写飞书 App Secret。";
    }
    if (form.domain_preset === "custom") {
      const domainError = validateHttpsUrl(
        form.custom_domain,
        "飞书私有化域名"
      );
      if (domainError) return domainError;
    }
  } else {
    return "不支持的 IM 平台类型。";
  }

  if (
    !Number.isFinite(form.session_idle_minutes) ||
    form.session_idle_minutes < 0 ||
    form.session_idle_minutes > 10080
  ) {
    return "对话空闲超时须在 0 到 10080 分钟之间。";
  }

  if (
    form.group_policy === "allowlist" &&
    parseAllowedChatIds(form.allowed_chat_ids_text).length === 0
  ) {
    return "群聊白名单模式下请至少填写一个群聊 Chat ID。";
  }

  if (form.model_override.enabled) {
    const baseUrlError = validateHttpApiBaseUrl(
      form.model_override.base_url,
      "独立模型 Base URL"
    );
    if (baseUrlError) return baseUrlError;
    if (!form.model_override.model.trim()) {
      return "已开启模型覆盖，请填写独立模型名。";
    }
  }

  return "";
}

export function buildBotPayload(form: BotForm): IMBotInput {
  const unboundUsername = form.unbound_username.trim() || null;
  const unboundDefaultEnv =
    unboundUsername &&
    form.unbound_default_env &&
    form.unbound_default_env !== NO_CLUSTER
      ? form.unbound_default_env
      : null;

  const credentials: IMBotInput["credentials"] =
    form.platform === "wecom"
      ? {
          bot_id: form.bot_id.trim(),
          ...(form.bot_secret.trim()
            ? { bot_secret: form.bot_secret.trim() }
            : {})
        }
      : {
          app_id: form.app_id.trim(),
          domain_preset: form.domain_preset,
          ...(form.domain_preset === "custom"
            ? {
                custom_domain: form.custom_domain.trim().replace(/\/+$/, "")
              }
            : {}),
          ...(form.app_secret.trim()
            ? { app_secret: form.app_secret.trim() }
            : {})
        };

  return {
    platform: form.platform,
    name: form.name.trim(),
    enabled: form.enabled,
    credentials,
    session_idle_minutes: Number.isFinite(form.session_idle_minutes)
      ? Math.trunc(form.session_idle_minutes)
      : DEFAULT_IDLE_MINUTES,
    group_policy: form.group_policy,
    allowed_chat_ids: parseAllowedChatIds(form.allowed_chat_ids_text),
    unbound_username: unboundUsername,
    unbound_default_env: unboundDefaultEnv,
    model_override: form.model_override.enabled
      ? {
          base_url: form.model_override.base_url.trim().replace(/\/+$/, ""),
          ...(form.model_override.api_key.trim()
            ? { api_key: form.model_override.api_key.trim() }
            : {}),
          model: form.model_override.model.trim(),
          max_input_tokens: form.model_override.max_input_tokens,
          max_output_tokens: form.model_override.max_output_tokens,
          compression_ratio: form.model_override.compression_ratio,
          reasoning_effort: form.model_override.reasoning_effort
        }
      : null
  };
}

export function normalizeLegacyWecomBinding(
  legacy: WeComBinding
): IMBindingState {
  const members = (legacy.members || []).map(item => ({
    bot_id: "legacy-wecom",
    bot_name: "企业微信机器人",
    platform: "wecom" as const,
    external_user_id: item.wecom_userid,
    self_name: item.self_name,
    bound_by: item.bound_by,
    bound_at: item.bound_at,
    last_seen_at: item.last_seen_at
  }));
  const platforms: IMPlatform[] =
    legacy.enabled || members.length ? ["wecom"] : [];
  return {
    enabled: legacy.enabled,
    connected: legacy.connected,
    platforms,
    bots: legacy.enabled
      ? [
          {
            id: "legacy-wecom",
            name: "企业微信机器人",
            platform: "wecom",
            enabled: legacy.enabled,
            connected: legacy.connected
          }
        ]
      : [],
    members,
    code_minutes: legacy.code_minutes
  };
}
