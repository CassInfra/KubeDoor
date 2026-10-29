// 权限相关的选项、标签和纯函数。
// 这个文件只能有 type import:tests/permission.test.cjs 用 esbuild 单文件编译后直接加载。
// 页面里取当前用户的权限用 usePermission()(src/utils/usePermission.ts)。
import type { ClusterPermission, UserRole } from "@/api/auth";

/** 权限表里的集群权限级别,从低到高(「无」看不到集群,不进表) */
export const PERMISSION_LEVELS = ["read", "rw", "admin"] as const;
export type PermissionLevel = (typeof PERMISSION_LEVELS)[number];
/** /api/auth/me 的 operations:操作类型 → 允许执行的集群权限级别 */
export type Operations = Record<string, PermissionLevel[]>;

/** 全局角色说明(与后端 auth/principal.py 一致) */
export const ROLE_OPTIONS: Array<{
  value: UserRole;
  label: string;
  desc: string;
  tag: "danger" | "warning" | "info" | "primary" | "success";
}> = [
  {
    value: "admin",
    label: "系统管理员",
    desc: "所有集群都是「管理员」权限,并且能做系统管理(用户、LDAP、企微 AI、Agent、权限与审批);不能再单独指定集群权限",
    tag: "danger"
  },
  {
    value: "cluster_admin",
    label: "集群管理员",
    desc: "没有单独授权的集群是「管理员」权限(可以改 CoreDNS、Istio 等),可以按集群单独降低;不能做系统管理",
    tag: "warning"
  },
  {
    value: "rw",
    label: "读写",
    desc: "没有单独授权的集群是「读写」权限(包括 AI 助手执行修改);只有管理员能做的操作(如 CoreDNS、Istio)不行",
    tag: "success"
  },
  {
    value: "read",
    label: "只读",
    desc: "没有单独授权的集群是「只读」权限:能查看,修改类操作按「权限与审批」里只读列的配置",
    tag: "info"
  },
  {
    value: "none",
    label: "无",
    desc: "默认看不到任何集群,只能访问单独授权的集群;新接入的集群也不会自动可见",
    tag: "info"
  },
  {
    value: "agent",
    label: "Agent 账号",
    desc: "只能用于 kubedoor-agent 连接 master,不能登录 Web",
    tag: "primary"
  }
];

export const ROLE_LABELS: Record<string, string> = Object.fromEntries(
  ROLE_OPTIONS.map(item => [item.value, item.label])
);

/** 单独授权时可选的集群权限 */
export const PERM_OPTIONS: Array<{ value: ClusterPermission; label: string }> =
  [
    { value: "admin", label: "管理员" },
    { value: "rw", label: "读写" },
    { value: "read", label: "只读" },
    { value: "none", label: "无(不可见)" }
  ];

export const PERM_LABELS: Record<string, string> = {
  admin: "管理员",
  rw: "读写",
  read: "只读",
  none: "无"
};

export const PERM_TAG: Record<
  string,
  "success" | "info" | "danger" | "warning"
> = {
  admin: "warning",
  rw: "success",
  read: "info",
  none: "danger"
};

/** 角色对应的「没有单独授权时」的集群权限 */
export function roleDefaultPermission(role: string): ClusterPermission {
  if (role === "admin" || role === "cluster_admin") return "admin";
  if (role === "rw") return "rw";
  if (role === "read") return "read";
  return "none";
}

/** 判断权限用到的用户信息(user store 的 state 满足这个结构) */
export interface PermissionSubject {
  /** 全局角色 */
  role?: string;
  /** 没有单独授权的集群的权限;没有时按全局角色推算 */
  defaultPermission?: string;
  /** 单独授权的集群 */
  clusters?: Record<string, string>;
  /** 已知集群的有效权限(/api/auth/me 的 cluster_permissions) */
  clusterPermissions?: Record<string, string>;
  operations?: Operations;
}

/** 用户对某个集群的有效权限:系统管理员一律「管理员」,有单独授权按授权,否则按全局权限兜底 */
export function permissionOf(
  user: PermissionSubject,
  env?: string | null
): ClusterPermission {
  if (user.role === "admin") return "admin";
  const fallback = (user.defaultPermission ||
    roleDefaultPermission(user.role ?? "")) as ClusterPermission;
  if (!env) return fallback;
  return (user.clusters?.[env] ?? fallback) as ClusterPermission;
}

/** 集群权限为 perm 时能否执行操作 op:查 operations 表,「无」和表里没有的操作一律不能 */
export function levelAllows(
  op: string,
  perm: string | null | undefined,
  operations: Operations | null | undefined
): boolean {
  if (!perm || perm === "none") return false;
  return !!operations?.[op]?.includes(perm as PermissionLevel);
}

/** 能否在集群 env 上执行操作 op(只用于按钮置灰和提示,后端仍会校验) */
export function can(
  op: string,
  env: string | null | undefined,
  user: PermissionSubject
): boolean {
  if (!env) return false;
  return levelAllows(op, permissionOf(user, env), user.operations);
}

/** 与具体集群无关的配置(没限定集群的告警屏蔽、节点均衡全局配置):用全局角色对应的默认权限查表 */
export function canGlobal(op: string, user: PermissionSubject): boolean {
  return levelAllows(
    op,
    roleDefaultPermission(user.role ?? ""),
    user.operations
  );
}

/**
 * 网页上改镜像:有「更新镜像」权限直接放行;没有的只读账号还可能按 UPDATE_IMAGE 白名单
 * (集群、时间窗口、用户)放行,白名单由后端判断,所以只读账号不置灰;「管理员」列也取消(全站关闭)时一律不能。
 */
export function canUpdateImage(
  env: string | null | undefined,
  user: PermissionSubject
): boolean {
  if (!env) return false;
  if (can("workload.image", env, user)) return true;
  return (
    permissionOf(user, env) === "read" &&
    levelAllows("workload.image", "admin", user.operations)
  );
}

/** 屏蔽规则里限定集群的条件(与后端 auth/policy.py 的 silence_envs 一致) */
const SILENCE_ENV_KEYS = ["env", "k8s", "cluster"];

type MatcherLike = { key?: string; op?: string; value?: string };

/** 屏蔽规则作用的集群:env / k8s / cluster 的「=」条件;没有限定集群时返回 null(全局规则) */
export function silenceEnvs(
  matchers: MatcherLike[] | null | undefined
): string[] | null {
  const envs: string[] = [];
  for (const matcher of matchers ?? []) {
    const key = (matcher?.key ?? "").trim();
    if (!SILENCE_ENV_KEYS.includes(key) || (matcher.op || "=") !== "=")
      continue;
    const value = (matcher.value ?? "").trim();
    if (value && !envs.includes(value)) envs.push(value);
  }
  return envs.length ? envs : null;
}

/** 能否新建 / 修改这条屏蔽规则:限定了集群的,这些集群都要「告警屏蔽」;全局规则按全局角色 */
export function canSilence(
  matchers: MatcherLike[] | null | undefined,
  user: PermissionSubject
): boolean {
  const envs = silenceEnvs(matchers);
  if (!envs) return canGlobal("alert.silence", user);
  return envs.every(env => can("alert.silence", env, user));
}

/** 有没有能建的屏蔽规则:全局规则,或者任何一个已知集群 */
export function canSilenceAny(user: PermissionSubject): boolean {
  return (
    canGlobal("alert.silence", user) ||
    Object.keys(user.clusterPermissions ?? {}).some(env =>
      can("alert.silence", env, user)
    )
  );
}

/** 权限表一行的三个权限列 */
export type OpLevels = Record<PermissionLevel, boolean>;

/** 勾选联动,保持「只读 ⊆ 读写 ⊆ 管理员」:勾上一列时更高的列跟着勾上,取消一列时更低的列跟着取消 */
export function linkLevels(
  levels: OpLevels,
  changed: PermissionLevel,
  checked: boolean
): OpLevels {
  const index = PERMISSION_LEVELS.indexOf(changed);
  const next = { ...levels };
  PERMISSION_LEVELS.forEach((level, i) => {
    if (checked ? i >= index : i <= index) next[level] = checked;
  });
  return next;
}

/** 三列是否满足「只读 ⊆ 读写 ⊆ 管理员」 */
export function isMonotonic(levels: OpLevels): boolean {
  return (!levels.read || levels.rw) && (!levels.rw || levels.admin);
}

/** 权限与审批配置(/api/admin/permission-policy 的 policy / defaults) */
export interface PolicyValue {
  ops: Record<string, OpLevels & { approval: boolean }>;
  commands: { view: string[]; danger: string[] };
}

/** 两份配置是否相同(操作类型按 key 比较,命令清单按顺序比较),用来判断有没有改动 */
export function samePolicy(a: PolicyValue, b: PolicyValue): boolean {
  const opKeys = new Set([...Object.keys(a.ops), ...Object.keys(b.ops)]);
  for (const key of opKeys) {
    const x = a.ops[key];
    const y = b.ops[key];
    if (!x || !y) return false;
    if (
      x.read !== y.read ||
      x.rw !== y.rw ||
      x.admin !== y.admin ||
      x.approval !== y.approval
    ) {
      return false;
    }
  }
  const sameList = (p: string[], q: string[]) =>
    p.length === q.length && p.every((item, i) => item === q[i]);
  return (
    sameList(a.commands.view, b.commands.view) &&
    sameList(a.commands.danger, b.commands.danger)
  );
}

export type CommandKind = "view" | "danger";

/** 每个清单最多多少项、每项最长多少字符(与后端校验一致) */
export const COMMAND_LIMIT = 300;
export const COMMAND_MAX_LENGTH = 64;

/** 去掉首尾空白,中间连续的空白合成一个空格 */
export function normalizeCommand(value: string): string {
  return value.trim().split(/\s+/).filter(Boolean).join(" ");
}

/** 高危清单项的命令名(「命令 参数」取命令,「前缀*」取前缀) */
function dangerName(item: string): string {
  return item.split(" ")[0].replace(/\*$/, "");
}

/** 查看命令 view 会不会被高危清单项 item 命中 */
function dangerCovers(item: string, view: string): boolean {
  if (item.includes(" ")) return false; // 「命令 参数」是对某个命令的细化,不算同名
  if (item.endsWith("*")) return view.startsWith(item.slice(0, -1));
  return item === view;
}

/**
 * 校验要加入清单的命令(value 已经 normalizeCommand),合法时返回空字符串。
 * 规则与后端一致:不含引号和 shell 元字符;查看项是单个命令名;
 * 高危项是「命令」「前缀*」「命令 参数」;两个清单不能有同一个命令。
 */
export function commandError(
  kind: CommandKind,
  value: string,
  lists: { view: string[]; danger: string[] }
): string {
  if (!value) return "请输入命令";
  if (value.length > COMMAND_MAX_LENGTH) {
    return `不能超过 ${COMMAND_MAX_LENGTH} 个字符`;
  }
  if (/[;&|<>()$`"'\\]/.test(value)) {
    return "不能包含引号和 shell 元字符(; & | < > ( ) $ ` \\)";
  }
  if (value.split(" ")[0].includes("/")) {
    return "只写命令名,不带路径(带路径执行的也按命令名匹配)";
  }
  if (lists[kind].length >= COMMAND_LIMIT) {
    return `每个清单最多 ${COMMAND_LIMIT} 项`;
  }
  if (lists[kind].includes(value)) {
    return `${value} 已经在清单里了`;
  }
  if (kind === "view") {
    if (/[\s*]/.test(value)) return "查看命令只能是单个命令名(不能带参数或 *)";
    const hit = lists.danger.find(item => dangerCovers(item, value));
    if (hit) {
      return hit === value
        ? `${value} 已经在高危命令清单里了`
        : `${value} 会被高危命令 ${hit} 匹配`;
    }
    return "";
  }
  const parts = value.split(" ");
  if (parts.length > 2) return "高危命令只能是「命令」「前缀*」或「命令 参数」";
  if (parts.length === 2) {
    if (value.includes("*")) return "「命令 参数」里不能用 *";
    return "";
  }
  if (value.includes("*") && !/^[^*]+\*$/.test(value)) {
    return "* 只能放在命令名末尾,表示前缀匹配,例如 mkfs*";
  }
  const name = dangerName(value);
  const hit = lists.view.find(view =>
    value.endsWith("*") ? view.startsWith(name) : view === name
  );
  if (hit) {
    return value.endsWith("*")
      ? `${value} 会匹配查看命令 ${hit},先从查看命令清单里删掉`
      : `${value} 已经在查看命令清单里了`;
  }
  return "";
}

/** 查看命令的内置参数检查(与 kubedoor-tools 一致):参数不满足时这条命令不算查看命令,继续按高危 / 其他命令判断 */
export const VIEW_COMMAND_CHECKS: Record<string, string> = {
  curl: "只允许 GET / HEAD / OPTIONS;不发数据、不上传、不写文件、不读配置;只访问 http/https",
  wget: "只能 -O - 或 --spider",
  nc: "只能 -z,不能 -e、-c、-l",
  ncat: "只能 -z,不能 -e、-c、-l",
  netcat: "只能 -z,不能 -e、-c、-l",
  find: "不能 -exec、-execdir、-ok、-okdir、-delete、-fprint*、-fls",
  sort: "不能 -o / --output",
  date: "不能 -s / --set",
  ip: "不能 add / del / delete / set / change / replace / flush / append / prepend / exec,也不能修改 netns",
  mount: "只能不带参数或 -l",
  env: "只能不带参数",
  hostname: "不能带位置参数(改主机名)"
};

/** 高危清单项的匹配方式 */
export function dangerMatchText(item: string): string {
  if (item.endsWith("*")) return `命令名以 ${item.slice(0, -1)} 开头`;
  const [command, arg] = item.split(" ");
  if (arg) return `${command} 命令带 ${arg} 参数`;
  return `命令 ${command}`;
}
