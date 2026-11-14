/**
 * 「系统管理 → 事件告警规则」页面的纯函数:
 * - 事件字段、运算符的定义和中文说明
 * - 界面配置的表单 ↔ 规则 JSON 的转换:rulesToForm 同时列出界面表示不了、会丢掉或改掉的内容
 * - 保存前的严格校验:与 master k8s_event/rules_schema.py 逐字一致,
 *   两边共用 src/kubedoor-master/tests/fixtures/event_rules_cases.json 里的用例
 * - JSON 模式用的解析器:语法错误给出行列;记下每个键 / 值的位置(把问题标在出错的行上)和重复的键
 *
 * 单测 tests/event-alert-rules.test.cjs 用 esbuild 单文件编译这个文件:不能 import 别的模块的值(类型可以)
 */

export type Path = (string | number)[];

/** 一处问题:path 是在规则 JSON 里的位置,例如 ["alert_rules", 2, "conditions", "count", "greater_than"] */
export interface Issue {
  path: Path;
  message: string;
}

export const IGNORE_KEY = "global_ignore_rules";
export const ALERT_KEY = "alert_rules";
export type GroupKey = "global_ignore_rules" | "alert_rules";
export const GROUPS: ReadonlyArray<readonly [GroupKey, string]> = [
  [IGNORE_KEY, "忽略规则"],
  [ALERT_KEY, "告警规则"]
];
const RULE_KEYS: Record<GroupKey, string[]> = {
  global_ignore_rules: ["name", "enabled", "conditions"],
  alert_rules: ["name", "enabled", "severity", "conditions"]
};

/** 事件字段(与 master 交给匹配器的事件一致),顺序即下拉的顺序 */
export const FIELDS = [
  "k8s",
  "namespace",
  "kind",
  "name",
  "reason",
  "message",
  "level",
  "count",
  "eventStatus",
  "reportingComponent",
  "reportingInstance",
  "firstTimestamp",
  "lastTimestamp",
  "eventUid"
];
export const NUMERIC_FIELD = "count";
export const FIELD_LABELS: Record<string, string> = {
  k8s: "集群",
  namespace: "命名空间",
  kind: "对象类型",
  name: "对象名称",
  reason: "原因",
  message: "消息",
  level: "级别",
  count: "次数",
  eventStatus: "事件状态",
  reportingComponent: "上报组件",
  reportingInstance: "上报主机",
  firstTimestamp: "首次时间",
  lastTimestamp: "最后时间",
  eventUid: "事件 UID"
};
export const FIELD_HELP: Record<string, string> = {
  k8s: "集群名，也就是 Agent 管理页里的集群名",
  namespace: "事件对象所在的命名空间；集群级对象（如 Node）为空",
  kind: "事件对象的类型，如 Pod、Node、Deployment",
  name: "事件对象的名称，如 Pod 名",
  reason: "事件原因，如 Unhealthy、FailedScheduling、OOMKilling",
  message: "事件消息",
  level: "只有 Normal、Warning 两种",
  count: "累计发生次数，唯一能比较大小的字段；没有次数的事件为 0",
  eventStatus:
    "ADDED（新增）或 MODIFIED（更新）；DELETED 的事件在规则之前已经跳过",
  reportingComponent: "上报事件的组件，如 kubelet、default-scheduler",
  reportingInstance: "上报事件的主机，通常是节点名",
  firstTimestamp: "北京时间，形如 2026-10-07 09:12:03，一般不用于规则",
  lastTimestamp: "北京时间，形如 2026-10-07 09:12:03，一般不用于规则",
  eventUid: "事件的唯一 ID，一般不用于规则"
};

/** 运算符。一个字段写了多个运算符时,匹配器按这个顺序只认第一个(保存时只允许写一个) */
export const LIST_OPERATORS = [
  "contains",
  "not_contains",
  "starts_with",
  "not_starts_with",
  "ends_with",
  "not_ends_with"
];
export const SCALAR_OPERATORS = ["equals", "not_equals"];
export const NUMERIC_OPERATORS = [
  "greater_than",
  "less_than",
  "greater_equal",
  "less_equal"
];
export const OPERATORS = [
  ...LIST_OPERATORS,
  ...SCALAR_OPERATORS,
  ...NUMERIC_OPERATORS
];
export const OPERATOR_LABELS: Record<string, string> = {
  contains: "包含任一",
  not_contains: "不包含任何",
  starts_with: "开头是任一",
  not_starts_with: "开头不是任何",
  ends_with: "结尾是任一",
  not_ends_with: "结尾不是任何",
  equals: "等于",
  not_equals: "不等于",
  greater_than: "大于",
  less_than: "小于",
  greater_equal: "大于等于",
  less_equal: "小于等于"
};
export const OPERATOR_HELP: Record<string, string> = {
  contains: "包含其中任意一个值就成立（子串匹配，不区分大小写）",
  not_contains: "一个值都不包含才成立（子串匹配，不区分大小写）",
  starts_with: "以其中任意一个值开头就成立（不区分大小写）",
  not_starts_with: "不以其中任何一个值开头才成立（不区分大小写）",
  ends_with: "以其中任意一个值结尾就成立（不区分大小写）",
  not_ends_with: "不以其中任何一个值结尾才成立（不区分大小写）",
  equals: "整个值相等才成立（不区分大小写）",
  not_equals: "整个值不相等才成立（不区分大小写）",
  greater_than: "次数大于这个数",
  less_than: "次数小于这个数",
  greater_equal: "次数大于等于这个数",
  less_equal: "次数小于等于这个数"
};

export const SEVERITIES = ["critical", "warning", "info"];
export const DEFAULT_SEVERITY = "warning";
export const SEVERITY_LABELS: Record<string, string> = {
  critical: "严重",
  warning: "警告",
  info: "提示"
};

export const MAX_RULES = 200;
export const MAX_VALUES = 200;
export const MAX_TEXT = 1000;
export const MAX_NAME = 100;
const NAME_IN_MESSAGE = 30;

/** 存库格式的一条规则(严格校验通过后的样子) */
export interface RuleValue {
  name: string;
  enabled: boolean;
  severity?: string;
  conditions: Record<string, Record<string, unknown>>;
}

export interface RulesValue {
  global_ignore_rules: RuleValue[];
  alert_rules: RuleValue[];
}

// ---------------------------------------------------------------------------
// 小工具
// ---------------------------------------------------------------------------
export function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function has(value: Record<string, unknown>, key: string): boolean {
  return Object.prototype.hasOwnProperty.call(value, key);
}

/** 按字符(而不是 UTF-16 码元)数长度,和 Python 的 len 一致 */
function charLength(text: string): number {
  return Array.from(text).length;
}

function isNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

const join = (items: string[]) => items.join("、");

export function operatorKind(op: string): "list" | "scalar" | "number" | "" {
  if (LIST_OPERATORS.includes(op)) return "list";
  if (SCALAR_OPERATORS.includes(op)) return "scalar";
  if (NUMERIC_OPERATORS.includes(op)) return "number";
  return "";
}

/** 字段能用的运算符(界面下拉只列这些);count 把数值比较放前面 */
export function operatorsFor(field: string): string[] {
  if (field === NUMERIC_FIELD)
    return [
      "greater_than",
      "greater_equal",
      "less_than",
      "less_equal",
      ...SCALAR_OPERATORS,
      ...LIST_OPERATORS
    ];
  return [...LIST_OPERATORS, ...SCALAR_OPERATORS];
}

function allowedOperators(field: string): string[] {
  return field === NUMERIC_FIELD
    ? OPERATORS
    : [...LIST_OPERATORS, ...SCALAR_OPERATORS];
}

/** 「告警规则第 3 条「重启事件告警」」;没有名称时只有序号 */
export function ruleLabel(
  groupLabel: string,
  index: number,
  rule: unknown
): string {
  let label = `${groupLabel}第 ${index + 1} 条`;
  const name = isObject(rule) ? rule.name : undefined;
  if (typeof name === "string" && name.trim()) {
    const chars = Array.from(name.trim());
    const text =
      chars.length > NAME_IN_MESSAGE
        ? chars.slice(0, NAME_IN_MESSAGE).join("") + "…"
        : chars.join("");
    label += `「${text}」`;
  }
  return label;
}

function groupLabel(group: GroupKey): string {
  return group === IGNORE_KEY ? "忽略规则" : "告警规则";
}

// ---------------------------------------------------------------------------
// 严格校验(与 master rules_schema.check 逐字一致)
// ---------------------------------------------------------------------------
function fieldHint(field: string): string {
  const known = FIELDS.find(item => item.toLowerCase() === field.toLowerCase());
  return known
    ? `（字段名区分大小写，应为 ${known}）`
    : `（可用字段：${join(FIELDS)}）`;
}

function checkValues(
  head: string,
  op: string,
  value: unknown,
  fail: (message: string, sub?: Path) => void
): boolean {
  if (!Array.isArray(value)) {
    fail(`${head} 的值必须是字符串数组，例如 ["abc"]`, [op]);
    return false;
  }
  if (!value.length) {
    fail(`${head} 至少要填一个值`, [op]);
    return false;
  }
  if (value.length > MAX_VALUES) {
    fail(`${head} 最多 ${MAX_VALUES} 个值`, [op]);
    return false;
  }
  for (let index = 0; index < value.length; index++) {
    const item = value[index];
    let problem = "";
    if (typeof item !== "string") problem = "必须是字符串";
    else if (!item) problem = "不能为空";
    else if (!item.trim()) problem = "只有空格";
    else if (charLength(item) > MAX_TEXT) problem = `最长 ${MAX_TEXT} 个字符`;
    if (problem) {
      fail(`${head} 的第 ${index + 1} 个值${problem}`, [op, index]);
      return false;
    }
  }
  return true;
}

function checkCondition(
  where: string,
  field: string,
  spec: unknown,
  path: Path,
  issues: Issue[]
): Record<string, unknown> | null {
  const fail = (message: string, sub: Path = []) =>
    issues.push({ path: [...path, ...sub], message });
  if (!FIELDS.includes(field)) {
    fail(`${where}的条件字段 ${field} 不存在${fieldHint(field)}`);
    return null;
  }
  let head = `${where}的条件 ${field}`;
  if (!isObject(spec)) {
    fail(`${head} 必须写成 {"运算符": 值}，例如 {"contains": ["abc"]}`);
    return null;
  }
  const ops = Object.keys(spec);
  if (!ops.length) {
    fail(`${head} 缺少运算符`);
    return null;
  }
  const allowed = allowedOperators(field);
  for (const op of ops) {
    if (NUMERIC_OPERATORS.includes(op) && field !== NUMERIC_FIELD) {
      fail(`${head}：${op} 只能用于 count 字段`, [op]);
      return null;
    }
    if (!allowed.includes(op)) {
      fail(`${head}：不认识的运算符 ${op}（可用：${join(allowed)}）`, [op]);
      return null;
    }
  }
  if (ops.length > 1) {
    fail(`${head} 只能写一个运算符，现在写了 ${join(ops)}`);
    return null;
  }
  const op = ops[0];
  const value = spec[op];
  head = `${head}：${op}`;
  if (LIST_OPERATORS.includes(op))
    return checkValues(head, op, value, fail)
      ? { [op]: [...(value as unknown[])] }
      : null;
  if (SCALAR_OPERATORS.includes(op)) {
    if (typeof value === "string") {
      if (charLength(value) > MAX_TEXT) {
        fail(`${head} 的值最长 ${MAX_TEXT} 个字符`, [op]);
        return null;
      }
    } else if (!isNumber(value)) {
      fail(`${head} 的值必须是字符串或数字`, [op]);
      return null;
    }
    return { [op]: value };
  }
  if (!isNumber(value)) {
    fail(`${head} 的值必须是数字`, [op]);
    return null;
  }
  return { [op]: value };
}

function checkRule(
  group: GroupKey,
  index: number,
  rule: unknown,
  issues: Issue[]
): RuleValue | null {
  const path: Path = [group, index];
  const where = ruleLabel(groupLabel(group), index, rule);
  const keys = RULE_KEYS[group];
  if (!isObject(rule)) {
    issues.push({ path, message: `${where}必须是对象（包含 ${join(keys)}）` });
    return null;
  }
  const before = issues.length;
  for (const key of Object.keys(rule)) {
    if (keys.includes(key)) continue;
    issues.push({
      path: [...path, key],
      message:
        key === "severity"
          ? `${where}不能有 severity：级别只对告警规则有用`
          : `${where}不能有 ${key}：只能有 ${join(keys)}`
    });
  }
  const rawName = has(rule, "name") ? rule.name : undefined;
  const name = typeof rawName === "string" ? rawName : "";
  const namePath = has(rule, "name") ? [...path, "name"] : path;
  if (rawName != null && typeof rawName !== "string")
    issues.push({
      path: namePath,
      message: `${where}的名称 name 必须是字符串`
    });
  else if (!name.trim())
    issues.push({ path: namePath, message: `${where}缺少名称 name` });
  else if (charLength(name.trim()) > MAX_NAME)
    issues.push({
      path: namePath,
      message: `${where}的名称最长 ${MAX_NAME} 个字符`
    });
  const enabled = has(rule, "enabled") ? rule.enabled : true;
  if (typeof enabled !== "boolean")
    issues.push({
      path: [...path, "enabled"],
      message: `${where}的 enabled 只能是 true 或 false`
    });
  const severity = has(rule, "severity") ? rule.severity : DEFAULT_SEVERITY;
  if (
    group === ALERT_KEY &&
    !(typeof severity === "string" && SEVERITIES.includes(severity))
  )
    issues.push({
      path: [...path, "severity"],
      message: `${where}的级别 severity 只能是 critical、warning 或 info`
    });
  const conditions = has(rule, "conditions") ? rule.conditions : undefined;
  const conditionsPath = has(rule, "conditions")
    ? [...path, "conditions"]
    : path;
  const normalized: Record<string, Record<string, unknown>> = {};
  if (conditions == null) {
    issues.push({
      path: conditionsPath,
      message: `${where}缺少条件 conditions`
    });
  } else if (!isObject(conditions)) {
    issues.push({
      path: conditionsPath,
      message: `${where}的条件 conditions 必须是对象，例如 {"reason": {"contains": ["OOMKilling"]}}`
    });
  } else if (!Object.keys(conditions).length) {
    issues.push({
      path: conditionsPath,
      message: `${where}至少要有一个条件（没有条件会匹配所有事件）`
    });
  } else {
    for (const [field, spec] of Object.entries(conditions)) {
      const checked = checkCondition(
        where,
        field,
        spec,
        [...path, "conditions", field],
        issues
      );
      if (checked) normalized[field] = checked;
    }
  }
  if (issues.length > before) return null;
  const result: RuleValue = {
    name: name.trim(),
    enabled: enabled as boolean
  } as RuleValue;
  if (group === ALERT_KEY) result.severity = severity as string;
  result.conditions = normalized;
  return result;
}

/** 严格校验:返回规范化后的规则(有问题时为 null)和问题列表;与 master rules_schema.check 一致 */
export function checkRules(value: unknown): {
  normalized: RulesValue | null;
  issues: Issue[];
} {
  if (!isObject(value))
    return {
      normalized: null,
      issues: [
        {
          path: [],
          message:
            "规则必须是一个 JSON 对象，包含 global_ignore_rules 和 alert_rules 两个数组"
        }
      ]
    };
  const issues: Issue[] = [];
  for (const key of Object.keys(value)) {
    if (key !== IGNORE_KEY && key !== ALERT_KEY)
      issues.push({
        path: [key],
        message: `顶层不能有 ${key}：只能有 ${IGNORE_KEY} 和 ${ALERT_KEY}`
      });
  }
  const result: Partial<RulesValue> = {};
  for (const [key, label] of GROUPS) {
    if (!has(value, key)) {
      issues.push({
        path: [],
        message: `缺少 ${key}（${label}）：没有规则时写成空数组 []`
      });
      continue;
    }
    const rules = value[key];
    if (!Array.isArray(rules)) {
      issues.push({ path: [key], message: `${key}（${label}）必须是数组` });
      continue;
    }
    if (rules.length > MAX_RULES) {
      issues.push({
        path: [key],
        message: `${label}最多 ${MAX_RULES} 条，现在有 ${rules.length} 条`
      });
      continue;
    }
    result[key] = rules.map((rule, index) =>
      checkRule(key, index, rule, issues)
    );
  }
  if (issues.length) return { normalized: null, issues };
  return { normalized: result as RulesValue, issues: [] };
}

/** 保存 / 试运行前的校验;duplicates 是 JSON 模式里重复的键(只有前端看得到,一律要求改掉) */
export function validateRules(
  value: unknown,
  duplicates: DuplicateKey[] = []
): Issue[] {
  const dupes = duplicates.map(item => ({
    path: item.path,
    message: `第 ${item.line} 行：键 "${item.key}" 重复了（只有最后一个生效），请合并或删掉一个`
  }));
  return [...dupes, ...checkRules(value).issues];
}

// ---------------------------------------------------------------------------
// 界面配置的表单
// ---------------------------------------------------------------------------
export interface ConditionForm {
  key: number;
  field: string;
  op: string;
  /** 列表型运算符的值 */
  values: string[];
  /** equals / not_equals 的值 */
  text: string;
  /** 数值运算符的值 */
  num: number | null;
}

export interface RuleForm {
  key: number;
  name: string;
  enabled: boolean;
  /** 只有告警规则用 */
  severity: string;
  conditions: ConditionForm[];
}

export interface RulesForm {
  ignore: RuleForm[];
  alert: RuleForm[];
}

let sequence = 0;
/** 给 v-for 用的唯一 key */
export const nextKey = () => ++sequence;

const NEW_FIELD_ORDER = [
  "reason",
  "message",
  "k8s",
  "namespace",
  "kind",
  "name",
  "level",
  "count",
  "eventStatus",
  "reportingComponent",
  "reportingInstance",
  "firstTimestamp",
  "lastTimestamp",
  "eventUid"
];

export function defaultOperator(field: string): string {
  return field === NUMERIC_FIELD ? "greater_than" : "contains";
}

/** 新的一行条件:字段取还没用过的(原因、消息、集群……依次往后) */
export function newCondition(used: string[] = []): ConditionForm {
  const field =
    NEW_FIELD_ORDER.find(item => !used.includes(item)) ?? NEW_FIELD_ORDER[0];
  return {
    key: nextKey(),
    field,
    op: defaultOperator(field),
    values: [],
    text: "",
    num: null
  };
}

export function newRule(): RuleForm {
  return {
    key: nextKey(),
    name: "",
    enabled: true,
    severity: DEFAULT_SEVERITY,
    conditions: [newCondition()]
  };
}

export function copyRule(rule: RuleForm): RuleForm {
  return {
    ...rule,
    key: nextKey(),
    name: rule.name.trim() ? `${rule.name.trim()}（副本）` : "",
    conditions: rule.conditions.map(item => ({
      ...item,
      key: nextKey(),
      values: [...item.values]
    }))
  };
}

/** 换字段:新字段不能用原来的运算符时换成默认运算符,填过的值留着 */
export function changeField(condition: ConditionForm, field: string): void {
  condition.field = field;
  if (!operatorsFor(field).includes(condition.op))
    condition.op = defaultOperator(field);
}

/** 换运算符:值的类型变了时尽量把已经填的值带过去 */
export function changeOperator(condition: ConditionForm, op: string): void {
  const from = operatorKind(condition.op);
  const to = operatorKind(op);
  condition.op = op;
  if (from === to) return;
  const first =
    from === "list"
      ? (condition.values[0] ?? "")
      : from === "number"
        ? condition.num == null
          ? ""
          : String(condition.num)
        : condition.text;
  if (to === "list" && !condition.values.length && first.trim())
    condition.values = [first.trim()];
  else if (to === "scalar" && !condition.text) condition.text = first;
  else if (to === "number" && condition.num == null && first.trim()) {
    const num = Number(first.trim());
    if (Number.isFinite(num)) condition.num = num;
  }
}

/** 多值输入框的值:去掉首尾空白、空值和重复的 */
export function cleanValues(values: unknown[]): string[] {
  const result: string[] = [];
  for (const item of values) {
    const text = String(item ?? "").trim();
    if (text && !result.includes(text)) result.push(text);
  }
  return result;
}

/** Python 的真假判断(以前的匹配器按它判断 enabled) */
function pythonTruthy(value: unknown): boolean {
  if (Array.isArray(value)) return value.length > 0;
  if (isObject(value)) return Object.keys(value).length > 0;
  return Boolean(value);
}

/** 把条件值转成界面里的字符串,和以前匹配时 Python 的 str() 一样;转不了返回 null */
function scalarText(value: unknown): string | null {
  if (typeof value === "string") return value;
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  if (typeof value === "boolean") return value ? "True" : "False";
  return null;
}

function conditionFromJson(
  where: string,
  field: string,
  spec: unknown,
  path: Path,
  lost: Issue[]
): ConditionForm | null {
  const head = `${where}的条件 ${field}`;
  if (!FIELDS.includes(field)) {
    lost.push({
      path,
      message: `${where}的条件字段 ${field} 不认识，会被丢掉`
    });
    return null;
  }
  if (!isObject(spec)) {
    lost.push({
      path,
      message: `${head} 不是 {"运算符": 值} 的形式，会被丢掉`
    });
    return null;
  }
  // 以前的匹配器:按 OPERATORS 的顺序只认第一个;数值运算符只对 count 有效
  const usable = allowedOperators(field);
  const op = usable.find(item => has(spec, item));
  if (!op) {
    lost.push({
      path,
      message: `${head} 没有认识的运算符（原来视为成立），会被丢掉`
    });
    return null;
  }
  const others = Object.keys(spec).filter(item => item !== op);
  if (others.length)
    lost.push({
      path,
      message: `${head} 只保留生效的 ${op}，丢掉 ${join(others)}`
    });
  const condition: ConditionForm = {
    key: nextKey(),
    field,
    op,
    values: [],
    text: "",
    num: null
  };
  const value = spec[op];
  const at = [...path, op];
  const kind = operatorKind(op);
  if (kind === "list") {
    const raw = Array.isArray(value) ? value : [value];
    if (!Array.isArray(value) && scalarText(value) == null) {
      lost.push({
        path: at,
        message: `${head}：${op} 的值不能在界面里编辑，会被丢掉`
      });
      return null;
    }
    const values: string[] = [];
    raw.forEach((item, index) => {
      const text = scalarText(item);
      if (text == null)
        lost.push({
          path: [...at, index],
          message: `${head}：${op} 的第 ${index + 1} 个值不是字符串，会被丢掉`
        });
      else if (!values.includes(text)) values.push(text);
    });
    condition.values = values;
    return condition;
  }
  if (kind === "scalar") {
    const text = scalarText(value);
    if (text == null) {
      lost.push({
        path: at,
        message: `${head}：${op} 的值不是字符串或数字，会被丢掉`
      });
      return null;
    }
    condition.text = text;
    return condition;
  }
  if (isNumber(value)) {
    condition.num = value;
  } else if (typeof value === "boolean") {
    condition.num = value ? 1 : 0;
    lost.push({
      path: at,
      message: `${head}：${op} 的值 ${value} 改成了 ${condition.num}`
    });
  } else if (
    typeof value === "string" &&
    value.trim() &&
    Number.isFinite(Number(value))
  ) {
    condition.num = Number(value);
    lost.push({
      path: at,
      message: `${head}：${op} 的值「${value}」是字符串（原来这个条件永远不成立），改成了数字 ${condition.num}`
    });
  } else {
    lost.push({
      path: at,
      message: `${head}：${op} 的值不是数字（原来这个条件永远不成立），需要重新填写`
    });
  }
  return condition;
}

function ruleFromJson(
  group: GroupKey,
  index: number,
  rule: unknown,
  lost: Issue[]
): RuleForm | null {
  const path: Path = [group, index];
  const where = ruleLabel(groupLabel(group), index, rule);
  if (!isObject(rule)) {
    lost.push({ path, message: `${where}不是对象，会被丢掉` });
    return null;
  }
  for (const key of Object.keys(rule)) {
    if (RULE_KEYS[group].includes(key)) continue;
    lost.push({
      path: [...path, key],
      message:
        key === "severity"
          ? `${where}的 severity 会被丢掉（忽略规则不用级别）`
          : `${where}的 ${key} 会被丢掉`
    });
  }
  const form = newRule();
  form.conditions = [];
  const name = rule.name;
  if (typeof name === "string") form.name = name;
  else if (name != null) {
    form.name = String(name);
    lost.push({
      path: [...path, "name"],
      message: `${where}的名称不是字符串，改成了「${form.name}」`
    });
  }
  if (has(rule, "enabled") && typeof rule.enabled !== "boolean") {
    form.enabled = pythonTruthy(rule.enabled);
    lost.push({
      path: [...path, "enabled"],
      message: `${where}的 enabled 不是 true / false，按原来的效果改成了 ${form.enabled}`
    });
  } else if (has(rule, "enabled")) form.enabled = rule.enabled as boolean;
  if (group === ALERT_KEY && has(rule, "severity")) {
    const severity = rule.severity;
    if (typeof severity === "string" && SEVERITIES.includes(severity))
      form.severity = severity;
    else
      lost.push({
        path: [...path, "severity"],
        message: `${where}的级别 ${JSON.stringify(severity)} 不认识，改成了 warning`
      });
  }
  const conditions = rule.conditions;
  if (isObject(conditions)) {
    const rows: ConditionForm[] = [];
    for (const [field, spec] of Object.entries(conditions)) {
      const row = conditionFromJson(
        where,
        field,
        spec,
        [...path, "conditions", field],
        lost
      );
      if (row) rows.push(row);
    }
    // 条件之间是 AND,顺序不影响结果:按字段的固定顺序显示(数据库也不保留键的顺序)
    rows.sort((a, b) => FIELDS.indexOf(a.field) - FIELDS.indexOf(b.field));
    form.conditions = rows;
  } else if (conditions != null) {
    lost.push({
      path: [...path, "conditions"],
      message: `${where}的条件 conditions 不是对象，会被丢掉`
    });
  }
  return form;
}

/** 规则 JSON → 界面表单;lost 列出界面表示不了、会丢掉或改掉的内容(为空表示转换不改变规则的效果) */
export function rulesToForm(value: unknown): {
  form: RulesForm;
  lost: Issue[];
} {
  const lost: Issue[] = [];
  const form: RulesForm = { ignore: [], alert: [] };
  if (!isObject(value)) {
    lost.push({ path: [], message: "规则不是 JSON 对象，界面里会是空的" });
    return { form, lost };
  }
  for (const key of Object.keys(value)) {
    if (key !== IGNORE_KEY && key !== ALERT_KEY)
      lost.push({ path: [key], message: `顶层的 ${key} 会被丢掉` });
  }
  for (const [key, label] of GROUPS) {
    if (!has(value, key)) continue;
    const rules = value[key];
    if (!Array.isArray(rules)) {
      lost.push({
        path: [key],
        message: `${key}（${label}）不是数组，会被当成没有规则`
      });
      continue;
    }
    const target = key === IGNORE_KEY ? form.ignore : form.alert;
    rules.forEach((rule, index) => {
      const item = ruleFromJson(key, index, rule, lost);
      if (item) target.push(item);
    });
  }
  return { form, lost };
}

function conditionValue(condition: ConditionForm): unknown {
  const kind = operatorKind(condition.op);
  if (kind === "list") return [...condition.values];
  if (kind === "number")
    return typeof condition.num === "number" ? condition.num : null;
  const text = condition.text ?? "";
  // count 的等于 / 不等于写成数字(和写成字符串的效果一样,JSON 里更直观)
  if (condition.field === NUMERIC_FIELD && /^-?\d+$/.test(text.trim()))
    return Number(text.trim());
  return text;
}

function ruleToJson(rule: RuleForm, alert: boolean): RuleValue {
  const conditions: Record<string, Record<string, unknown>> = {};
  for (const condition of rule.conditions)
    conditions[condition.field] = {
      [condition.op]: conditionValue(condition)
    };
  const result: RuleValue = {
    name: rule.name.trim(),
    enabled: rule.enabled
  } as RuleValue;
  if (alert) result.severity = rule.severity;
  result.conditions = conditions;
  return result;
}

/** 界面表单 → 规则 JSON(不校验;没填好的值原样带上,交给 validateRules 指出来) */
export function formToRules(form: RulesForm): RulesValue {
  return {
    global_ignore_rules: form.ignore.map(rule => ruleToJson(rule, false)),
    alert_rules: form.alert.map(rule => ruleToJson(rule, true))
  };
}

/** 页面上显示的问题:按规则分组的 key 是 "alert_rules:2",其余(顶层的)是 "" */
export function issueOwner(issue: Issue): string {
  const [group, index] = issue.path;
  return (group === IGNORE_KEY || group === ALERT_KEY) &&
    typeof index === "number"
    ? `${group}:${index}`
    : "";
}

/** 问题属于某条规则的哪个条件字段;不是条件上的问题返回 "" */
export function issueField(issue: Issue): string {
  return issue.path[2] === "conditions" && typeof issue.path[3] === "string"
    ? issue.path[3]
    : "";
}

// ---------------------------------------------------------------------------
// JSON:带位置的解析、格式化、比较
// ---------------------------------------------------------------------------
export interface DuplicateKey {
  path: Path;
  key: string;
  line: number;
}

export interface NodeRange {
  /** 键名的范围(数组元素没有) */
  key?: [number, number];
  /** 值的范围 */
  value: [number, number];
}

/** 解析成功:值、每个键 / 值的位置、重复的键 */
export interface JsonParsed {
  ok: true;
  value: unknown;
  ranges: Map<string, NodeRange>;
  duplicates: DuplicateKey[];
}

/** 语法错误:中文原因和位置(行列从 1 开始) */
export interface JsonSyntaxIssue {
  ok: false;
  message: string;
  offset: number;
  line: number;
  column: number;
}

export type JsonParse = JsonParsed | JsonSyntaxIssue;

/** 解析成功时的结果,否则 null(项目没开 strict,联合类型在页面里收窄不了,用这两个函数取) */
export function parsedOf(result: JsonParse): JsonParsed | null {
  return result.ok ? (result as JsonParsed) : null;
}

/** 语法错误,没有时 null */
export function syntaxIssueOf(result: JsonParse): JsonSyntaxIssue | null {
  return result.ok ? null : (result as JsonSyntaxIssue);
}

class JsonSyntaxError extends Error {
  constructor(
    message: string,
    public offset: number
  ) {
    super(message);
  }
}

export const pathKey = (path: Path) => JSON.stringify(path);

/** 偏移量 → 行列(都从 1 开始;列按 UTF-16 码元数,和 Monaco 一样) */
export function positionOf(
  text: string,
  offset: number
): { line: number; column: number } {
  let line = 1;
  let start = 0;
  for (let i = 0; i < offset && i < text.length; i++) {
    if (text[i] === "\n") {
      line++;
      start = i + 1;
    }
  }
  return { line, column: offset - start + 1 };
}

const MAX_DEPTH = 100;

/** 解析 JSON:语法错误给出中文原因和行列;成功时记下每个键 / 值的位置和重复的键 */
export function parseJson(text: string): JsonParse {
  const ranges = new Map<string, NodeRange>();
  const duplicateOffsets: { path: Path; key: string; offset: number }[] = [];
  const n = text.length;
  let i = 0;

  const fail = (message: string, at = i): never => {
    throw new JsonSyntaxError(message, at);
  };

  function skipSpace() {
    while (i < n) {
      const c = text[i];
      if (c === " " || c === "\t" || c === "\n" || c === "\r") i++;
      else if (c === "/" && (text[i + 1] === "/" || text[i + 1] === "*"))
        fail("JSON 不能写注释（// 或 /* */）");
      else break;
    }
  }

  function unexpected(): never {
    const c = text[i];
    if (c === "'") fail('字符串和键名要用英文双引号 "，不能用单引号');
    if ("“”‘’".includes(c)) fail('这里用了中文引号，要用英文双引号 "');
    if (/^(?:NaN|-?Infinity|undefined)\b/.test(text.slice(i, i + 10)))
      fail("JSON 不支持 NaN、Infinity、undefined");
    if (c === "}" || c === "]" || c === ",") fail("这里缺少一个值");
    if (c === "，" || c === "：") fail("这里用了中文标点，要用英文的 , 或 :");
    return fail(`这里不应该出现「${c}」`);
  }

  function parseString(): string {
    const start = i;
    i++;
    while (i < n) {
      const c = text[i];
      if (c === '"') {
        i++;
        return JSON.parse(text.slice(start, i));
      }
      if (c === "\\") {
        const next = text[i + 1];
        if (next === "u") {
          if (!/^[0-9a-fA-F]{4}$/.test(text.slice(i + 2, i + 6)))
            fail("字符串里的 \\u 后面要跟 4 位十六进制数");
          i += 6;
        } else if (next !== undefined && '"\\/bfnrt'.includes(next)) i += 2;
        else fail(`字符串里有不合法的转义 \\${next ?? ""}`);
        continue;
      }
      if (c === "\n" || c === "\r")
        fail('字符串没有结束（缺少右边的双引号 "）', start);
      if (c < " ") fail("字符串里不能直接写控制字符（换行请写成 \\n）");
      i++;
    }
    return fail('字符串没有结束（缺少右边的双引号 "）', start);
  }

  function parseNumber(): number {
    const match = /-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?/y;
    match.lastIndex = i;
    const found = match.exec(text);
    if (!found || !found[0] || found[0] === "-") return fail("不是合法的数字");
    if (found[0] === "0" || found[0] === "-0") {
      if (/[0-9]/.test(text[i + found[0].length] ?? ""))
        fail("数字不能以 0 开头");
    }
    i += found[0].length;
    return Number(found[0]);
  }

  function record(path: Path, start: number, end: number) {
    const key = pathKey(path);
    const node = ranges.get(key);
    if (node) node.value = [start, end];
    else ranges.set(key, { value: [start, end] });
  }

  function parseValue(path: Path, depth: number): unknown {
    skipSpace();
    if (i >= n) fail("JSON 不完整：这里缺少一个值");
    if (depth > MAX_DEPTH) fail("嵌套太深");
    const start = i;
    const c = text[i];
    let value: unknown;
    if (c === "{") value = parseObject(path, depth);
    else if (c === "[") value = parseArray(path, depth);
    else if (c === '"') value = parseString();
    else if (c === "-" || (c >= "0" && c <= "9")) value = parseNumber();
    else if (text.startsWith("true", i)) {
      i += 4;
      value = true;
    } else if (text.startsWith("false", i)) {
      i += 5;
      value = false;
    } else if (text.startsWith("null", i)) {
      i += 4;
      value = null;
    } else unexpected();
    record(path, start, i);
    return value;
  }

  /** 一项后面:逗号继续、右括号结束;否则指出缺什么 */
  function afterItem(close: string, previousEnd: number): boolean {
    skipSpace();
    if (i >= n) fail(`JSON 不完整：缺少 ${close}`, previousEnd);
    const c = text[i];
    if (c === ",") {
      const comma = i;
      i++;
      skipSpace();
      if (text[i] === close)
        fail("最后一项后面多了一个逗号（JSON 不允许）", comma);
      return true;
    }
    if (c === close) {
      i++;
      return false;
    }
    if (c === '"' || c === "{" || c === "[" || /[-0-9tfn]/.test(c))
      fail(`上一项后面缺少逗号 ,`, previousEnd);
    if (c === "，") fail("这里用了中文逗号，要用英文的 ,");
    return fail(`这里应该是逗号 , 或 ${close}`);
  }

  function parseObject(path: Path, depth: number) {
    const result: Record<string, unknown> = {};
    const seen = new Set<string>();
    i++;
    skipSpace();
    if (text[i] === "}") {
      i++;
      return result;
    }
    for (;;) {
      skipSpace();
      if (i >= n) fail("JSON 不完整：缺少 }");
      if (text[i] !== '"') {
        if (text[i] === "'" || "“”‘’".includes(text[i])) unexpected();
        fail('键名要用英文双引号括起来，例如 "name"');
      }
      const keyStart = i;
      const key = parseString();
      const keyEnd = i;
      skipSpace();
      if (text[i] !== ":") {
        if (text[i] === "：") fail("这里用了中文冒号，要用英文的 :");
        fail(`键 "${key}" 后面缺少冒号 :`, keyEnd);
      }
      i++;
      const childPath = [...path, key];
      if (seen.has(key))
        duplicateOffsets.push({ path: childPath, key, offset: keyStart });
      seen.add(key);
      const value = parseValue(childPath, depth + 1);
      ranges.get(pathKey(childPath))!.key = [keyStart, keyEnd];
      // 用 defineProperty:键名是 __proto__ 时也只是普通的键
      Object.defineProperty(result, key, {
        value,
        enumerable: true,
        writable: true,
        configurable: true
      });
      if (!afterItem("}", i)) return result;
    }
  }

  function parseArray(path: Path, depth: number) {
    const result: unknown[] = [];
    i++;
    skipSpace();
    if (text[i] === "]") {
      i++;
      return result;
    }
    for (;;) {
      result.push(parseValue([...path, result.length], depth + 1));
      if (!afterItem("]", i)) return result;
    }
  }

  try {
    skipSpace();
    if (i >= n) fail("内容是空的");
    const value = parseValue([], 0);
    skipSpace();
    if (i < n) fail("JSON 结束后还有多余的内容（是不是多了 } 或 ]？）");
    const duplicates = duplicateOffsets.map(item => ({
      path: item.path,
      key: item.key,
      line: positionOf(text, item.offset).line
    }));
    return { ok: true, value, ranges, duplicates };
  } catch (error) {
    if (!(error instanceof JsonSyntaxError)) throw error;
    const { line, column } = positionOf(text, error.offset);
    return {
      ok: false,
      message: error.message,
      offset: error.offset,
      line,
      column
    };
  }
}

export interface MarkerRange {
  startLineNumber: number;
  startColumn: number;
  endLineNumber: number;
  endColumn: number;
  message: string;
}

function lineEnd(text: string, offset: number): number {
  const end = text.indexOf("\n", offset);
  return end < 0 ? text.length : end;
}

/** 问题 → 编辑器里的标记位置:有键名标键名;没有标值所在的那一行;路径找不到时往上找最近的 */
export function issueRange(
  text: string,
  ranges: Map<string, NodeRange>,
  issue: Issue
): MarkerRange {
  let path = issue.path;
  let node = ranges.get(pathKey(path));
  while (!node && path.length) {
    path = path.slice(0, -1);
    node = ranges.get(pathKey(path));
  }
  let [start, end] = node?.key ?? node?.value ?? [0, 0];
  end = Math.min(Math.max(end, start + 1), lineEnd(text, start));
  if (end <= start) end = start + 1;
  const from = positionOf(text, start);
  const to = positionOf(text, end);
  return {
    startLineNumber: from.line,
    startColumn: from.column,
    endLineNumber: to.line,
    endColumn: to.column,
    message: issue.message
  };
}

/** 语法错误的标记:从出错的位置标到这一行末尾(至少一个字符) */
export function syntaxRange(
  text: string,
  parsed: { message: string; offset: number }
): MarkerRange {
  const start = Math.min(parsed.offset, text.length);
  const end = Math.max(lineEnd(text, start), start + 1);
  const from = positionOf(text, start);
  const to = positionOf(text, end);
  return {
    startLineNumber: from.line,
    startColumn: from.column,
    endLineNumber: from.line,
    endColumn: to.line === from.line ? to.column : from.column + 1,
    message: parsed.message
  };
}

const isPrimitive = (value: unknown) =>
  value === null || typeof value !== "object";

/** 格式化:两格缩进;只有简单值的短数组写在一行(和出厂规则文件的写法一样) */
export function formatJson(value: unknown, indent = 0): string {
  const pad = "  ".repeat(indent);
  if (Array.isArray(value)) {
    if (!value.length) return "[]";
    if (value.every(isPrimitive)) {
      const inline = `[${value.map(item => JSON.stringify(item) ?? "null").join(", ")}]`;
      if (inline.length + indent * 2 <= 100) return inline;
    }
    const items = value.map(item => `${pad}  ${formatJson(item, indent + 1)}`);
    return `[\n${items.join(",\n")}\n${pad}]`;
  }
  if (isObject(value)) {
    const keys = Object.keys(value);
    if (!keys.length) return "{}";
    const items = keys.map(
      key =>
        `${pad}  ${JSON.stringify(key)}: ${formatJson(value[key], indent + 1)}`
    );
    return `{\n${items.join(",\n")}\n${pad}}`;
  }
  return JSON.stringify(value) ?? "null";
}

/** 比较用的序列化:对象的键排序(键的顺序不影响规则的效果),数组保持顺序 */
export function stableStringify(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableStringify).join(",")}]`;
  if (isObject(value))
    return `{${Object.keys(value)
      .sort()
      .map(key => `${JSON.stringify(key)}:${stableStringify(value[key])}`)
      .join(",")}}`;
  return JSON.stringify(value) ?? "null";
}

// ---------------------------------------------------------------------------
// 输入提示与试运行
// ---------------------------------------------------------------------------
const SUGGESTIONS: Record<string, string[]> = {
  level: ["Warning", "Normal"],
  eventStatus: ["ADDED", "MODIFIED"],
  kind: [
    "Pod",
    "Node",
    "Deployment",
    "ReplicaSet",
    "StatefulSet",
    "DaemonSet",
    "Job",
    "CronJob",
    "Service",
    "PersistentVolumeClaim",
    "HorizontalPodAutoscaler"
  ],
  reason: [
    "OOMKilling",
    "SystemOOM",
    "NodeNotReady",
    "Unhealthy",
    "BackOff",
    "Failed",
    "FailedScheduling",
    "FailedMount",
    "FailedAttachVolume",
    "FailedCreatePodSandBox",
    "Evicted",
    "Killing",
    "Pressure"
  ],
  reportingComponent: ["kubelet", "default-scheduler"]
};

/** 值输入框的候选项(还能自己输入别的);集群名来自已注册的集群 */
export function valueSuggestions(field: string, clusters: string[]): string[] {
  if (field === "k8s") return clusters;
  return SUGGESTIONS[field] ?? [];
}

export interface PreviewNumbers {
  limit: number;
  truncated: boolean;
  deleted: number;
  alerted_level: number;
}

/** 试运行结果下面的说明 */
export function previewNotes(result: PreviewNumbers): string[] {
  const notes: string[] = [];
  if (result.truncated)
    notes.push(`这段时间的事件太多，只取了最近的 ${result.limit} 条`);
  if (result.deleted)
    notes.push(
      `其中 ${result.deleted} 条在库里已是「删除」状态（K8S 清理了事件对象），按删除前的状态参与匹配`
    );
  if (result.alerted_level)
    notes.push(
      `其中 ${result.alerted_level} 条的级别在库里已改成「已告警」（原来的级别没有记录），按 Warning 参与匹配`
    );
  notes.push(
    "每个事件只算一次；实际发不发、发几次还要看去重窗口和事件更新的次数。试运行不会发通知，也不会改事件"
  );
  return notes;
}
