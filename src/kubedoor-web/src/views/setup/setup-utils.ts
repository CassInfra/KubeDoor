import type {
  MetricsSettings,
  PostgresSettings,
  SetupMarker,
  SetupPhase,
  SetupState
} from "@/api/setup";

const bootstrapPhases = new Set<SetupPhase>([
  "draft",
  "ready",
  "running",
  "failed",
  "interrupted",
  "finalizing"
]);

export function needsSetup(marker: SetupMarker | null): boolean {
  return !!marker && bootstrapPhases.has(marker.phase);
}

export function isInstalling(phase?: SetupPhase): boolean {
  return phase === "running" || phase === "finalizing";
}

export function hasInstallation(phase?: SetupPhase): boolean {
  return (
    isInstalling(phase) ||
    ["failed", "interrupted", "succeeded"].includes(phase)
  );
}

export function stateStep(state: SetupState): number {
  if (hasInstallation(state.phase) || state.phase === "ready") return 5;
  return Math.min(5, Math.max(1, Number(state.step) || 1));
}

export const serviceStatus: Record<string, string> = {
  pending: "等待中",
  waiting: "等待依赖",
  deploying: "部署中",
  pulling: "拉取镜像",
  scheduling: "等待调度",
  starting: "启动中",
  checking: "检查中",
  running: "检查中",
  ready: "已就绪",
  success: "检查通过",
  succeeded: "已就绪",
  failed: "失败",
  error: "失败",
  timeout: "超时",
  skipped: "未选择",
  warning: "请留意"
};

export function checkTone(status: string): string {
  if (["ready", "success", "succeeded"].includes(status)) return "success";
  if (["failed", "error", "timeout"].includes(status)) return "error";
  if (["pending", "waiting", "skipped"].includes(status)) return "muted";
  if (status === "warning") return "warning";
  return "active";
}

export function urlInput(value: string, label: string, base = false): string {
  let url: URL;
  try {
    url = new URL(value.trim());
  } catch {
    throw new Error(`请填写有效的${label}，包含 http:// 或 https://。`);
  }
  if (
    !["http:", "https:"].includes(url.protocol) ||
    !url.hostname ||
    url.username ||
    url.password ||
    url.hash ||
    url.search
  )
    throw new Error(
      `${label}请填写 HTTP / HTTPS 地址，账号密码在独立字段填写。`
    );
  if (base && url.pathname !== "/")
    throw new Error(`${label}只需填写协议、主机和端口，不包含路径。`);
  return value.trim().replace(/\/+$/, "");
}

export function isLoopback(host: string): boolean {
  return /^(localhost|127(?:\.\d{1,3}){3}|0\.0\.0\.0|\[?::1\]?)$/i.test(host);
}

export function suggestedPublicURL(origin: string): string {
  try {
    const url = new URL(origin);
    return isLoopback(url.hostname) ? "" : url.origin;
  } catch {
    return "";
  }
}

export function derivedWriteURL(type: string, url: string): string {
  if (type !== "Victoria-Metrics-Single" || !url.trim()) return "";
  try {
    return `${urlInput(url, "时序库地址")}/api/v1/write`;
  } catch {
    return "";
  }
}

export function postgresPayload(form: PostgresSettings): PostgresSettings {
  const host = form.host.trim();
  if (!host || /[\s/@?#]/.test(host) || isLoopback(host))
    throw new Error(
      "请填写 Kubernetes Pod 可以访问的数据库主机名或 IP，不含协议。 "
    );
  const port = Number(form.port);
  if (!Number.isInteger(port) || port < 1 || port > 65535)
    throw new Error("数据库端口需在 1 到 65535 之间。");
  if (!form.database.trim() || !form.user.trim())
    throw new Error("请填写数据库名和用户名。");
  if (!form.password && !form.has_password)
    throw new Error("请填写数据库密码。");
  return {
    host,
    port,
    database: form.database.trim(),
    user: form.user.trim(),
    sslmode: form.sslmode,
    ...(form.password ? { password: form.password } : {})
  };
}

export function metricsPayload(form: MetricsSettings): MetricsSettings {
  const url = urlInput(form.url, "时序库地址");
  if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(form.tag_key.trim()))
    throw new Error("集群标签名只能含字母、数字、下划线，且不能以数字开头。");
  const write_url =
    form.type === "Victoria-Metrics-Single"
      ? derivedWriteURL(form.type, url)
      : form.write_url.trim()
        ? urlInput(form.write_url, "指标写入地址")
        : "";
  return {
    type: form.type,
    url,
    user: form.user.trim(),
    write_url,
    tag_key: form.tag_key.trim(),
    ...(form.password ? { password: form.password } : {})
  };
}

export function publicURL(value: string): string {
  const url = urlInput(value, "KubeDoor 访问地址", true);
  if (isLoopback(new URL(url).hostname))
    throw new Error(
      "访问地址必须能从目标集群访问，不能填写 localhost 或回环地址。"
    );
  return url;
}
