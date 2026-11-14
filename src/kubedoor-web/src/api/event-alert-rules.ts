import { http } from "@/utils/http";

/** 一条规则:conditions 是 {字段: {运算符: 值}};severity 只有告警规则有 */
export interface EventRule {
  name: string;
  enabled: boolean;
  severity?: string;
  conditions: Record<string, Record<string, unknown>>;
}

/** 存库格式的规则(与以前的 alert_rules.json 结构相同) */
export interface EventRules {
  global_ignore_rules: EventRule[];
  alert_rules: EventRule[];
}

/** GET / PUT /api/admin/event-alert-rules 的返回 */
export interface EventRulesState {
  /**
   * 生效的规则。source=database 时是库里保存的(从旧规则文件原样导入的可能不符合严格校验,
   * 页面要能显示和修改);source=default 时是内置默认规则(库里还没有,随版本更新)
   */
  rules: unknown;
  source: "database" | "default";
  updated_by: string | null;
  updated_at: string | null;
}

/** 422 invalid_rules 的 errors:path 是在规则 JSON 里的位置 */
export interface EventRuleIssue {
  path: (string | number)[];
  message: string;
}

/** POST /api/admin/event-alert-rules/preview 的请求 */
export interface EventRulesPreviewRequest {
  rules: EventRules;
  /** 最近多少小时,1~168,默认 24 */
  hours: number;
  /** 只看某个集群;空表示全部集群 */
  env?: string;
}

/** 试运行举的例子(级别、状态是库里的原值) */
export interface EventRulesPreviewExample {
  k8s: string;
  namespace: string;
  kind: string;
  name: string;
  reason: string;
  /** 最长 300 字符,超出的截断加「…」 */
  message: string;
  count: number;
  level: string;
  eventStatus: string;
  firstTimestamp: string;
  lastTimestamp: string;
}

export interface EventRulesPreviewRule {
  /** 在这一组里的下标(从 0 开始) */
  index: number;
  name: string;
  enabled: boolean;
  /** 只有告警规则有 */
  severity?: string;
  /** 忽略规则:按顺序第一条命中、由它忽略的事件数;告警规则:第一条命中的事件数 */
  hits: number;
  /** 只有告警规则有:也能命中、但被前面的规则先命中的事件数 */
  shadowed?: number;
  examples: EventRulesPreviewExample[];
}

export interface EventRulesPreviewResult {
  hours: number;
  env: string;
  /** 最多取多少条事件 */
  limit: number;
  /** 参与试运行的事件数 */
  total: number;
  /** 事件超过 limit,只取了最近的 limit 条 */
  truncated: boolean;
  ignored: number;
  matched: number;
  unmatched: number;
  /** 库里状态是 DELETED、按删除前的状态参与匹配的事件数 */
  deleted: number;
  /** 库里级别已改成「已告警」、按 Warning 参与匹配的事件数 */
  alerted_level: number;
  ignore_rules: EventRulesPreviewRule[];
  alert_rules: EventRulesPreviewRule[];
}

type Result<T> = { success: boolean; data: T; message?: string };

/** 读取失败由页面自己显示(页面上有「重新加载」),不再弹提示 */
export const getEventAlertRules = () =>
  http.request<Result<EventRulesState>>(
    "get",
    "/api/admin/event-alert-rules",
    {},
    { silent: true }
  );

/** 严格校验后保存,马上生效;不合法时 422,errors 里是每处问题的位置和原因(message 由 http 封装统一提示) */
export const saveEventAlertRules = (rules: EventRules) =>
  http.request<Result<EventRulesState>>("put", "/api/admin/event-alert-rules", {
    data: { rules }
  });

/** 内置默认规则(「恢复默认」载入编辑区,保存后才生效) */
export const getDefaultEventAlertRules = () =>
  http.request<Result<{ rules: EventRules }>>(
    "get",
    "/api/admin/event-alert-rules/default"
  );

/** 试运行:错误由试运行窗口自己显示;查最近 7 天的事件可能要几十秒 */
export const previewEventAlertRules = (data: EventRulesPreviewRequest) =>
  http.request<Result<EventRulesPreviewResult>>(
    "post",
    "/api/admin/event-alert-rules/preview",
    { data },
    { silent: true, timeout: 90000 }
  );
