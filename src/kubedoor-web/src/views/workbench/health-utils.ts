/**
 * 「修复」弹框里健康检查结果的展示：分组、汇总、标签颜色、修复说明。
 * 纯函数，不 import 别的模块（单测见 tests/agent-install.test.cjs）。
 */
import type {
  AgentHealth,
  AgentHealthCheck,
  HealthStatus
} from "@/api/agent-install";

/** 分组的显示顺序和标题；不认识的分组排在最后，标题「其他」 */
export const HEALTH_GROUPS = [
  { key: "agent", label: "Agent 与连接" },
  { key: "component", label: "组件" },
  { key: "data", label: "数据上报" },
  { key: "repair", label: "修复" }
] as const;

export interface HealthGroup {
  key: string;
  label: string;
  checks: AgentHealthCheck[];
}

/** 按分组列出检查项：分组按 HEALTH_GROUPS 的顺序，组内保持服务端给的顺序，空的分组不显示 */
export function groupChecks(checks: AgentHealthCheck[]): HealthGroup[] {
  const known = HEALTH_GROUPS.map(group => ({
    key: group.key as string,
    label: group.label as string,
    checks: checks.filter(check => check.group === group.key)
  }));
  const names = new Set<string>(HEALTH_GROUPS.map(group => group.key));
  const others = checks.filter(check => !names.has(check.group));
  return [...known, { key: "other", label: "其他", checks: others }].filter(
    group => group.checks.length > 0
  );
}

/** 检查项标签的颜色（Element Plus 的 tag type） */
export function healthTagType(
  status: HealthStatus | string
): "success" | "warning" | "danger" | "info" {
  if (status === "success") return "success";
  if (status === "warning") return "warning";
  if (status === "error") return "danger";
  return "info";
}

const names = (checks: AgentHealthCheck[]) =>
  checks.map(check => check.name).join("、");

/** 顶部的汇总：Agent 离线 / 有异常 / 只有需要注意的 / 全部正常 */
export function healthSummary(health: Pick<AgentHealth, "online" | "checks">): {
  type: "success" | "warning" | "error" | "info";
  title: string;
} {
  const errors = health.checks.filter(check => check.status === "error");
  const warnings = health.checks.filter(check => check.status === "warning");
  const skipped = health.checks.filter(check => check.status === "skipped");
  if (!health.online)
    return {
      type: "error",
      title:
        "Agent 离线，连不上控制端：要经 Agent 查询的几项已跳过。Agent 起不来时可以用「修复」覆盖安装"
    };
  if (errors.length)
    return {
      type: "error",
      title: `发现 ${errors.length} 项异常：${names(errors)}`
    };
  if (warnings.length)
    return {
      type: "warning",
      title: `有 ${warnings.length} 项需要注意：${names(warnings)}`
    };
  if (!health.checks.length) return { type: "info", title: "没有检查结果" };
  return {
    type: "success",
    title:
      "全部检查通过" + (skipped.length ? `（${skipped.length} 项跳过）` : "")
  };
}

/**
 * 「修复（覆盖安装）」按钮旁边的说明。kubeconfig 是健康检查里的 AI Kubeconfig 信息；
 * 健康检查没结果（还在查或接口失败）时传 undefined，说明两种情况
 */
export function repairHint(kubeconfig?: AgentHealth["kubeconfig"]): string {
  const head =
    "修复会用 kubeconfig 重新安装这个集群的 Agent（覆盖安装），集群唯一标识和命名空间不变。";
  if (!kubeconfig)
    return (
      head +
      "有 AI Kubeconfig 时直接用它进入「确认信息」，没有时要上传 kubeconfig。"
    );
  if (!kubeconfig.configured)
    return (
      head +
      "这个集群还没有 AI Kubeconfig，需要上传一份能在集群里创建 Agent 资源的 kubeconfig。"
    );
  const label =
    kubeconfig.origin === "agent-install"
      ? "自动配置"
      : kubeconfig.context || "手动上传";
  return head + `将直接用保存的 AI Kubeconfig（${label}）进入「确认信息」。`;
}

/** 检查时间显示成本地时间 YYYY-MM-DD HH:mm:ss；解析不了就原样显示 */
export function formatCheckedAt(value: string): string {
  const time = new Date(value);
  if (!value || Number.isNaN(time.getTime())) return value || "";
  const pad = (number: number) => String(number).padStart(2, "0");
  return (
    `${time.getFullYear()}-${pad(time.getMonth() + 1)}-${pad(time.getDate())} ` +
    `${pad(time.getHours())}:${pad(time.getMinutes())}:${pad(time.getSeconds())}`
  );
}
