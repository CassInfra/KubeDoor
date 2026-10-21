import * as yaml from "js-yaml";
import dayjs from "dayjs";
import {
  type CellStatus,
  type DomainItem,
  type DomainLists,
  type EnvType,
  type VSConflict,
  type VSScopeResult,
  DOMAIN_KEY
} from "@/api/istio";

type TagType = "success" | "warning" | "danger" | "info" | "primary";

/** 不按某个集群展开时,域名条件显示成这个值(和后端 DOMAIN_PLACEHOLDER 一致) */
export const DOMAIN_PLACEHOLDER = "<按集群>";

export const PATTERN_TYPE_LABEL = { glob: "通配符", regex: "正则" } as const;

/** VS 被本组排除 / 纳入的原因 */
export function scopeText(scope: Pick<VSScopeResult, "reason" | "pattern">) {
  switch (scope.reason) {
    case "manual":
      return "手动忽略";
    case "ignore_rule":
      return `匹配忽略规则「${scope.pattern}」`;
    case "not_included":
      return "不在包含规则里";
    case "include":
      return "单独纳入(不受组规则影响)";
    default:
      return scope.pattern ? `匹配包含规则「${scope.pattern}」` : "组规则纳入";
  }
}

/** 同一个 VS 也在这些组里 */
export function conflictText(conflicts: VSConflict[]) {
  return conflicts
    .map(c => `组 ${c.group}(共用集群 ${c.clusters.join("、")})`)
    .join("、");
}

/** 集群格子状态:文案、颜色、说明 */
export const STATUS_META: Record<
  CellStatus,
  { label: string; type: TagType; desc: string }
> = {
  synced: {
    label: "一致",
    type: "success",
    desc: "集群现状 = 组标准 + 该集群的 hosts 和域名"
  },
  pending: {
    label: "待下发",
    type: "warning",
    desc: "组标准或该集群 hosts 改过还没下发;集群里仍是上次下发(或纳管时)的内容"
  },
  diff: {
    label: "不一致",
    type: "danger",
    desc: "集群里不是花折上次下发的内容:有人直接改过集群,或纳管后从未下发且不同"
  },
  missing: {
    label: "集群中没有",
    type: "danger",
    desc: "已纳入该集群,但集群里还没有这个 VirtualService,下发即可创建"
  },
  incomplete: {
    label: "缺少域名",
    type: "danger",
    desc: "有规则的域名条件还没设置该集群的域名:在「编辑 hosts 和域名」里补上后才能下发"
  },
  unknown: {
    label: "未采集",
    type: "info",
    desc: "该集群还没有采集过现状,点「刷新现状」"
  },
  unbound: {
    label: "未纳入",
    type: "info",
    desc: "集群里有同名 VirtualService,但组标准没有纳入该集群"
  },
  unmanaged: {
    label: "未纳管",
    type: "info",
    desc: "还没有组标准,只是集群现状"
  },
  none: { label: "—", type: "info", desc: "该集群没有,也不部署" }
};

export const ENV_TYPE_LABEL: Record<EnvType, string> = {
  test: "测试",
  prod: "生产"
};

export const ENV_TYPE_TAG: Record<EnvType, TagType> = {
  test: "info",
  prod: "danger"
};

export const COVERAGE_LABEL: Record<string, string> = {
  test_only: "只在测试环境",
  prod_only: "只在生产环境"
};

export function formatTime(value?: string | null) {
  return value ? dayjs(value).format("YYYY-MM-DD HH:mm:ss") : "";
}

/** spec 转 YAML;对比时两边都按键名排序,避免字段顺序不同造成的假差异 */
export function toYaml(value: unknown, sortKeys = false) {
  if (value === null || value === undefined) return "";
  return yaml.dump(value, { sortKeys, lineWidth: -1, noRefs: true });
}

/** 匹配条件的简短描述,和后端 describe_match 一致 */
export function describeMatch(match: any[] | null | undefined) {
  if (!match || !match.length) return "无匹配条件";
  const conditions = (field: string, spec: Record<string, any>) =>
    Object.entries(spec || {}).map(([op, value]) =>
      value === DOMAIN_PLACEHOLDER ? "域名按集群" : `${field} ${op} ${value}`
    );
  return match
    .map(cond => {
      const items: string[] = [
        ...conditions("uri", cond?.uri),
        ...conditions("authority", cond?.authority)
      ];
      for (const [key, spec] of Object.entries(cond?.headers || {})) {
        items.push(...conditions(`header ${key}`, spec as any));
      }
      if (cond?.[DOMAIN_KEY]) items.push("域名按集群");
      return items.join(" & ") || "空条件";
    })
    .join(" | ");
}

/** match 模板里有几个域名块 */
export function domainBlockCount(match: any[] | null | undefined) {
  return (match ?? []).filter(cond => cond?.[DOMAIN_KEY]).length;
}

/** 域名条件的显示:不是 authority 精确匹配的标出写法 */
export function formatDomain(item: DomainItem) {
  const notes: string[] = [];
  if (item.field === "host") notes.push("host 头");
  if (item.op !== "exact") notes.push(item.op);
  return notes.length ? `${item.value}(${notes.join(",")})` : item.value;
}

/** 复制域名时把一个集群的标识换成另一个,如 penglai → kunlun */
export function replaceDomains(
  lists: DomainLists,
  from: string,
  to: string
): DomainLists {
  return lists.map(items =>
    items.map(item => ({
      ...item,
      value: from ? item.value.split(from).join(to) : item.value
    }))
  );
}

/** 两个集群名只有一段不同时(cassmall-penglai / cassmall-kunlun),把这一段作为复制时的替换建议 */
export function suggestReplace(from: string, to: string) {
  const a = from.split("-");
  const b = to.split("-");
  if (a.length !== b.length) return { find: "", replace: "" };
  const diff = a.map((part, i) => [part, b[i]]).filter(([x, y]) => x !== y);
  return diff.length === 1
    ? { find: diff[0][0], replace: diff[0][1] }
    : { find: "", replace: "" };
}

/** 启用重写时的默认名称:第一条匹配条件的 uri 前缀去掉末尾的 /(用于访问日志展示) */
export function suggestRouteName(match: any[] | null | undefined) {
  const prefix = match?.[0]?.uri?.prefix;
  if (typeof prefix !== "string" || !prefix) return "";
  return prefix.replace(/\/+$/, "") || prefix;
}

/** 组标准的规则 → 展示用的条目(和后端渲染一致):域名块显示成占位,各集群的域名另外列出 */
export function displayRoute(route: {
  name: string | null;
  match_rules: any[] | null;
  rewrite_rules: Record<string, any> | null;
  forward_type: string | null;
  forward_detail: any;
  timeout: string | null;
  extra: Record<string, any> | null;
}) {
  const entry: Record<string, any> = {};
  if (route.name) entry.name = route.name;
  if (route.match_rules?.length) {
    entry.match = route.match_rules.map(cond => {
      if (!cond?.[DOMAIN_KEY]) return cond;
      const rest = Object.fromEntries(
        Object.entries(cond).filter(([key]) => key !== DOMAIN_KEY)
      );
      return { ...rest, authority: { exact: DOMAIN_PLACEHOLDER } };
    });
  }
  if (route.rewrite_rules) entry.rewrite = route.rewrite_rules;
  if (route.forward_type && route.forward_detail)
    entry[route.forward_type] = route.forward_detail;
  if (route.timeout) entry.timeout = route.timeout;
  return { ...entry, ...(route.extra ?? {}) };
}

/** 转发目标的简短描述:服务.命名空间:端口(权重);委托写下级 VS */
export function forwardSummary(route: {
  forward_type: string | null;
  forward_detail: any;
  extra?: Record<string, any> | null;
}) {
  if (route.forward_type === "delegate") {
    return `委托 → ${route.forward_detail?.namespace}/${route.forward_detail?.name}`;
  }
  if (Array.isArray(route.forward_detail)) {
    return route.forward_detail
      .map((item: any) => {
        const host = String(item?.destination?.host ?? "").replace(
          /\.svc\.cluster\.local$/,
          ""
        );
        const port = item?.destination?.port?.number;
        const weight = item?.weight !== undefined ? ` (${item.weight}%)` : "";
        return `${host}${port ? `:${port}` : ""}${weight}`;
      })
      .join(", ");
  }
  return route.extra ? "见其他字段" : "-";
}

/** 规范化 host 列表:去空格、去空、去重 */
export function cleanHosts(hosts: string[]) {
  const result: string[] = [];
  for (const host of hosts) {
    const value = (host || "").trim();
    if (value && !result.includes(value)) result.push(value);
  }
  return result;
}

/** 文本(换行、逗号、空格分隔)→ host 列表 */
export function parseHostsText(text: string) {
  return cleanHosts(text.split(/[\s,;，；]+/));
}
