<script setup lang="ts">
import {
  computed,
  nextTick,
  onBeforeUnmount,
  onMounted,
  reactive,
  ref
} from "vue";
import { useRouter } from "vue-router";
import { ElMessage } from "element-plus";
import {
  getSetupMarker,
  setupApi,
  SetupRequestError,
  type MetricsSettings,
  type NotificationInput,
  type PostgresSettings,
  type SetupAgentOptions,
  type SetupCheck,
  type SetupMarker,
  type SetupState
} from "@/api/setup";
import {
  agentAwaitingUser,
  agentFormFrom,
  agentOutcome,
  agentPayload,
  agentStepStatus,
  adminPasswordKey,
  autoPublicURL,
  checkTone,
  connectionPassed,
  deadlineText,
  hasInstallation,
  isInstalling,
  metricsEndpoints,
  metricsPayload,
  NOTICE_NOTE,
  NOTICE_ROWS,
  notificationBadge,
  notificationFormFrom,
  notificationPayload,
  notificationSummary,
  notificationTestRequest,
  notificationTokenPlaceholder,
  notifyErrorView,
  notifyHint,
  notifyResultView,
  NOTIFY_LABELS,
  NOTIFY_TYPES,
  postgresPayload,
  publicURL,
  serviceStatus,
  SETUP_STEPS,
  stateStep,
  toneMark,
  type AgentForm,
  type NotificationForm,
  type NotifyTestView
} from "./setup-utils";

defineOptions({ name: "Setup" });
const router = useRouter();
const task = ref<SetupState>();
const completedMarker = ref<SetupMarker>();
const authenticated = ref(false);
const initializing = ref(true);
const busy = ref(false);
const error = ref("");
const connectionError = ref("");
const key = ref("");
const step = ref(1);
const highestStep = ref(1);
const passwordConfirmation = ref("");
const hasAdminPassword = ref(false);
const repairing = ref(false);
const changedSteps = reactive(new Set<number>());
const successNotified = new Set<string>();
let timer: ReturnType<typeof setTimeout> | undefined;
let active = true;
let polling = false;
// 提交次数：提交前已发出的轮询结果作废
let submissions = 0;
// 已预填 Agent 表单的安装 id：每次安装只预填一次，轮询不覆盖正在填写的内容
let agentFilledFor = "";
/** 等待选择是否安装 Agent 时慢速轮询，到点自动收尾或其他页面操作后能及时更新 */
const AGENT_CHOICE_POLL = 15000;

const steps = [
  { title: "环境检查", note: "确认安装位置与环境" },
  { title: "PostgreSQL", note: "连接业务数据库" },
  { title: "时序数据库", note: "连接监控指标存储" },
  { title: "基础设置", note: "管理员、告警与通知" },
  { title: "自动安装", note: "逐项检查，确认就绪" },
  { title: "安装 Agent", note: "可选：本集群接入" }
];
const postgres = reactive<PostgresSettings>({
  host: "",
  port: 5432,
  database: "kubedoor",
  user: "kubedoor",
  password: "",
  sslmode: "disable",
  has_password: false
});
const metrics = reactive<MetricsSettings>({
  type: "Victoria-Metrics-Single",
  scheme: "http",
  host: "",
  tenant: "0",
  user: "",
  password: "",
  has_password: false,
  read_url: "",
  write_url: "",
  tag_key: "origin_prometheus"
});
const basics = reactive({
  admin: { username: "kubedoor", password: "" },
  public_url: "",
  /** 告警组件 vmalert、Alertmanager、kubedoor-alarm 一起装或一起不装 */
  alerting: true
});
/** 访问地址默认自动获取、只读，点「修改」后才能填写 */
const editingURL = ref(false);
/** 完成页的管理员密码默认显示成圆点，点眼睛图标才显示明文 */
const passwordVisible = ref(false);
const publicURLInput = ref<HTMLInputElement>();
/** 第 4 步的告警通知（可选）：机器人地址只在这次输入时有值，已保存的不回传 */
const notification = reactive<NotificationForm>(notificationFormFrom());
/** 「发送测试消息」的结果，改动通知设置后清掉 */
const notifyTest = ref<NotifyTestView>();
const notifyTesting = ref(false);
const notifyOptions = NOTIFY_TYPES.map(value => ({
  value,
  label: NOTIFY_LABELS[value]
}));
const agentForm = reactive<AgentForm>(agentFormFrom());
const phase = computed(() => completedMarker.value?.phase || task.value?.phase);
const completed = computed(() => phase.value === "succeeded");
const publicFinalizing = computed(
  () => completedMarker.value?.phase === "finalizing"
);
const executing = computed(
  () => hasInstallation(phase.value) && !repairing.value
);
const running = computed(() =>
  isInstalling(phase.value, task.value?.agent?.status)
);
const locked = computed(
  () => busy.value || initializing.value || running.value
);
// 第 6 步：控制端已就绪，选择是否在本集群安装 Agent
const agentPhase = computed(() => phase.value === "agent");
const agent = computed(() =>
  agentPhase.value ? task.value?.agent : undefined
);
const agentStatus = computed(() => agent.value?.status || "choice");
const agentChoosing = computed(
  () => agentPhase.value && agentAwaitingUser(agent.value?.status)
);
const agentSteps = computed(() => agent.value?.steps || []);
const agentChecks = computed(() => agent.value?.checks || []);
const agentDone = computed(
  () =>
    agentSteps.value.filter(item =>
      ["success", "succeeded", "skipped"].includes(item.status)
    ).length
);
const agentDeadline = computed(() =>
  agentChoosing.value ? deadlineText(agent.value?.deadline) : ""
);
const agentTitle = computed(
  () =>
    ({
      running: "正在本集群安装 Agent",
      succeeded: "Agent 已上线，正在完成安装",
      partial: "Agent 已上线，指标采集验证未通过",
      failed: "本集群 Agent 安装未完成",
      skipped: "正在完成安装"
    })[agentStatus.value] || "在本集群安装 Agent"
);
// 收尾和完成页显示本集群 Agent 的结果（公开完成标记里也带 status 和 env）
const finished = computed(() =>
  ["finalizing", "succeeded"].includes(phase.value)
);
const agentResult = computed(() =>
  finished.value
    ? agentOutcome(completedMarker.value?.agent || task.value?.agent)
    : undefined
);
// 完成页的登录信息：访问地址（默认是 Web 的 NodePort 地址）、管理员账号和第 4 步设置的密码
const accessURL = computed(
  () =>
    completedMarker.value?.public_url ||
    task.value?.public_url ||
    task.value?.config?.public_url ||
    basics.public_url
);
const adminPassword = computed(() => {
  const id =
    completedMarker.value?.installation_id ||
    completedMarker.value?.id ||
    task.value?.id;
  if (!completed.value || !id) return "";
  try {
    return sessionStorage.getItem(adminPasswordKey(id)) || "";
  } catch {
    return "";
  }
});
const services = computed(
  () => completedMarker.value?.services || task.value?.services || []
);
const checks = computed(
  () => completedMarker.value?.checks || task.value?.checks || []
);
const taskError = computed(
  () => completedMarker.value?.error || task.value?.error
);
const chosenServices = computed(() =>
  services.value.filter(service => service.status !== "skipped")
);
const readyServices = computed(
  () =>
    chosenServices.value.filter(
      service => checkTone(service.status) === "success"
    ).length
);
const progress = computed(() =>
  chosenServices.value.length
    ? (readyServices.value / chosenServices.value.length) * 100
    : 0
);
const environment = computed(() => task.value?.environment);
/** 自动获取的访问地址：当前打开安装页面的地址，端口转发打开时用 Web 的 NodePort 地址 */
const autoURL = computed(() =>
  autoPublicURL(window.location.origin, environment.value?.web_url)
);
const publicURLNote = computed(() => {
  const url = basics.public_url.trim().replace(/\/+$/, "");
  const usage =
    "其他集群的 Agent 通过这个地址连接 KubeDoor，告警通知里的链接也指向它。";
  if (!url) return `未能自动获取，请填写访问地址。${usage}`;
  if (editingURL.value)
    return `${usage}请确认各目标集群都能访问，不能填写本机地址或临时端口转发地址。`;
  const source =
    url === autoPublicURL(window.location.origin)
      ? "与当前打开安装页面的地址一致。"
      : url === environment.value?.web_url
        ? "自动使用 Web 的 NodePort 地址（节点 IP + NodePort）。"
        : "";
  return `${source}${usage}使用域名或负载均衡时点「修改」。`;
});
/** 已保存的告警通知（只有 IM 类型、默认 @ 人和是否已配置） */
const savedNotification = computed(() => task.value?.config?.notification);
const notifyHints = computed(() => notifyHint(notification.type));
const notifyPlaceholder = computed(() =>
  notificationTokenPlaceholder(notification, savedNotification.value)
);
const notifyBadge = computed(() =>
  notificationBadge(notification, savedNotification.value)
);
// 环境检查和本集群 Agent 安装权限都记在安装状态的 checks 里
const environmentNames = ["environment", "agent_installer"];
const environmentChecks = computed(
  () =>
    environment.value?.checks ||
    checks.value.filter(
      check =>
        environmentNames.includes(check.id) ||
        environmentNames.includes(check.name)
    )
);
// 引导时是否授予了临时集群权限：决定控制端就绪后能否在第 6 步安装本集群 Agent
const agentInstallerCheck = computed(() =>
  environmentChecks.value.find(
    check => (check.id || check.name) === "agent_installer"
  )
);
// 安装权限只是第 1 步的提示，安装结果在「本集群 Agent」单独展示，最终检查里不重复列出
const finalChecks = computed(() =>
  checks.value.filter(check => (check.id || check.name) !== "agent_installer")
);
const canRetry = computed(
  () =>
    authenticated.value &&
    !repairing.value &&
    !completed.value &&
    !running.value &&
    !agentPhase.value &&
    task.value?.retryable === true
);
const installationTitle = computed(() => {
  if (completed.value) return "全部服务安装与检查通过";
  if (connectionError.value) return "正在重新获取安装状态";
  if (phase.value === "finalizing") return "服务已部署，正在完成最终检查";
  if (["failed", "interrupted"].includes(phase.value)) return "安装尚未完成";
  return "正在安装各项服务";
});
// 第 2、3 步测试连接和继续分两步：测试通过（含「请留意」）且之后没有改动表单，才能继续
const connectionSection = computed(() =>
  step.value === 2 ? "postgres" : step.value === 3 ? "metrics" : ""
);
const connectionReady = computed(
  () =>
    !!connectionSection.value &&
    !changedSteps.has(step.value) &&
    connectionPassed(checks.value, connectionSection.value)
);
const actionText = computed(() => {
  if (busy.value)
    return step.value >= 5
      ? "正在提交…"
      : connectionSection.value
        ? "正在测试…"
        : "正在检查…";
  if (agentPhase.value) {
    if (!agentChoosing.value)
      return agentStatus.value === "running"
        ? "正在安装 Agent…"
        : "正在完成安装…";
    if (!agentForm.install) return "完成安装";
    return agentStatus.value === "failed" ? "重新安装 Agent" : "安装 Agent";
  }
  if (running.value)
    return phase.value === "finalizing" ? "正在完成安装…" : "等待全部服务就绪…";
  if (canRetry.value) return "重试失败服务并复查";
  if (connectionSection.value)
    return connectionReady.value ? "继续" : "测试连接";
  return ["", "开始配置", "", "", "检查并继续", "开始安装"][step.value];
});
const groupChecks = computed(() =>
  checks.value.filter(check => {
    if (changedSteps.has(step.value)) return false;
    const name = `${check.id || ""} ${check.name}`.toLowerCase();
    return step.value === 2
      ? /postgres|database|数据库|pg/.test(name)
      : step.value === 3
        ? /metrics|prom|时序|指标|vm|label/.test(name)
        : false;
  })
);

function stopPolling() {
  if (timer) clearTimeout(timer);
  timer = undefined;
}

function notifyCompletion(previous: string | undefined, id: string) {
  if (
    previous &&
    previous !== "succeeded" &&
    completed.value &&
    !successNotified.has(id)
  ) {
    successNotified.add(id);
    ElMessage.success("所有选定服务与最终检查均已通过，KubeDoor 安装成功。");
  }
}

function applyState(next: SetupState, syncForms = false, restoreStep = false) {
  if (!active) return;
  const previous = phase.value;
  const previousAgent =
    task.value?.phase === "agent" && task.value.id === next.id
      ? task.value.agent?.status
      : undefined;
  task.value = next;
  connectionError.value = "";
  if (syncForms && next.config) {
    Object.assign(postgres, next.config.postgres, { password: "" });
    Object.assign(metrics, next.config.metrics, { password: "" });
    Object.assign(basics.admin, {
      username: next.config.admin?.username || "kubedoor",
      password: ""
    });
    hasAdminPassword.value = !!next.config.admin?.has_password;
    passwordConfirmation.value = "";
    basics.public_url = next.config.public_url || autoURL.value;
    basics.alerting = next.config.components?.alerting !== false;
    editingURL.value = false;
    Object.assign(notification, notificationFormFrom(next.config.notification));
    notifyTest.value = undefined;
  }
  fillAgentForm(next, previousAgent);
  if (restoreStep || (hasInstallation(next.phase) && !repairing.value))
    step.value = stateStep(next);
  highestStep.value = Math.max(highestStep.value, stateStep(next));
  notifyCompletion(previous, next.id);
  if (isInstalling(next.phase, next.agent?.status)) schedulePoll();
  else if (next.phase === "agent") schedulePoll(AGENT_CHOICE_POLL);
  else stopPolling();
}

/**
 * 每次安装只预填一次 Agent 表单；安装中退回失败时按实际提交的选项回填
 * （安装中表单已锁定，回填不会丢掉用户输入）。
 */
function fillAgentForm(next: SetupState, previousStatus?: string) {
  if (next.phase !== "agent") return;
  const returned =
    !agentAwaitingUser(previousStatus) &&
    agentAwaitingUser(next.agent?.status) &&
    !!next.agent?.options;
  if (agentFilledFor === next.id && !returned) return;
  agentFilledFor = next.id;
  Object.assign(agentForm, agentFormFrom(next.agent));
}

function applyMarker(marker: SetupMarker | null) {
  if (!active || !["finalizing", "succeeded"].includes(marker?.phase))
    return false;
  const id = marker.installation_id || marker.id;
  if (task.value && id && id !== task.value.id) return false;
  const previous = phase.value;
  completedMarker.value = marker;
  step.value = SETUP_STEPS;
  connectionError.value = "";
  error.value = "";
  stopPolling();
  notifyCompletion(previous, id || "completed");
  if (marker.phase === "finalizing") schedulePoll();
  return true;
}

function schedulePoll(delay = 2500) {
  stopPolling();
  if (
    active &&
    (authenticated.value || publicFinalizing.value) &&
    !completed.value
  )
    timer = setTimeout(pollState, delay);
}

async function pollState() {
  if (polling || !active || busy.value) {
    if (active && running.value) schedulePoll();
    return;
  }
  polling = true;
  // 轮询期间开始了新的提交时丢弃这次结果，避免旧状态覆盖提交结果
  const current = submissions;
  try {
    if (publicFinalizing.value) {
      const marker = await getSetupMarker();
      const currentId =
        completedMarker.value?.installation_id || completedMarker.value?.id;
      if (
        marker &&
        ["failed", "interrupted"].includes(marker.phase) &&
        (marker.installation_id || marker.id) === currentId
      ) {
        // 收尾守护任务可将状态退回失败；重新连接安装服务以恢复修复入口。
        completedMarker.value = undefined;
        authenticated.value = false;
        task.value = undefined;
        await initialize();
        return;
      }
      if (!applyMarker(marker)) {
        connectionError.value = "暂时无法读取安装收尾状态，正在重新获取。";
        schedulePoll();
      }
      return;
    }
    const next = await setupApi.state();
    if (current === submissions) applyState(next);
  } catch (reason) {
    if (current !== submissions) return;
    if (applyMarker(await getSetupMarker())) return;
    if (!active) return;
    connectionError.value = errorText(reason);
    if (reason instanceof SetupRequestError && reason.status === 401) {
      authenticated.value = false;
      error.value = "安装会话已过期，请重新输入安装密钥。后台任务仍会继续。";
      stopPolling();
      return;
    }
    schedulePoll();
  } finally {
    polling = false;
  }
}

function errorText(reason: unknown) {
  return reason instanceof Error
    ? reason.message
    : "操作未完成，请刷新状态后重试。";
}

async function initialize() {
  if (busy.value) return;
  initializing.value = true;
  error.value = "";
  try {
    if (applyMarker(await getSetupMarker())) return;
    const status = await setupApi.status();
    if (["finalizing", "succeeded"].includes(status.phase)) {
      if (applyMarker(status)) return;
      if (applyMarker(await getSetupMarker())) return;
    }
    authenticated.value = status.authenticated === true;
    if (authenticated.value) applyState(await setupApi.state(), true, true);
  } catch (reason) {
    if (!applyMarker(await getSetupMarker())) error.value = errorText(reason);
  } finally {
    initializing.value = false;
  }
}

async function unlock() {
  if (busy.value || !key.value.trim()) return;
  busy.value = true;
  error.value = "";
  try {
    await setupApi.session(key.value.trim());
    key.value = "";
    authenticated.value = true;
    applyState(await setupApi.state(), true, true);
  } catch (reason) {
    if (!applyMarker(await getSetupMarker())) error.value = errorText(reason);
  } finally {
    busy.value = false;
  }
}

function edited() {
  highestStep.value = Math.min(highestStep.value, step.value);
  changedSteps.add(step.value);
  error.value = "";
}

/** 改了类型、协议、地址、租户或账号密码时重新生成远程读写地址（覆盖手动修改） */
function regenerateEndpoints() {
  Object.assign(metrics, metricsEndpoints(metrics));
}

function moveTo(nextStep: number) {
  if (executing.value) return;
  step.value = nextStep;
  highestStep.value = Math.max(highestStep.value, nextStep);
  if (nextStep === 5) repairing.value = false;
}

function goStep(value: number) {
  if (locked.value || executing.value || !authenticated.value) return;
  if (value < 1 || value > highestStep.value) return;
  step.value = value;
  error.value = "";
}

async function perform(action: () => Promise<SetupState>, nextStep?: number) {
  const currentStep = step.value;
  busy.value = true;
  error.value = "";
  submissions++;
  stopPolling();
  try {
    applyState(await action(), true);
    changedSteps.delete(currentStep);
    if (nextStep) moveTo(nextStep);
  } catch (reason) {
    const message = errorText(reason);
    try {
      // 失败检测也可能保存草稿并增加版本；获取最新版本后才能再次提交。
      applyState(await setupApi.state());
      changedSteps.delete(currentStep);
    } catch {
      if (applyMarker(await getSetupMarker())) return;
    }
    if (reason instanceof SetupRequestError && reason.status === 401)
      authenticated.value = false;
    error.value = message;
  } finally {
    busy.value = false;
    if (running.value) schedulePoll();
  }
}

async function advance() {
  if (locked.value || !task.value) return;
  if (agentPhase.value) {
    await submitAgent();
    return;
  }
  if (canRetry.value) {
    await perform(() => setupApi.retry(task.value.revision));
    return;
  }
  try {
    if (step.value === 1) {
      if (
        environmentChecks.value.some(
          check => checkTone(check.status) === "error"
        )
      )
        throw new Error("环境检查尚未通过，请修复上述问题后刷新状态。");
      step.value = 2;
      highestStep.value = Math.max(highestStep.value, 2);
    } else if (connectionSection.value) {
      // 测试通过后才继续；没测试、测试失败或之后改过表单时先测试
      if (connectionReady.value) moveTo(step.value + 1);
      else await testConnection();
    } else if (step.value === 4) {
      if (!basics.admin.password && !hasAdminPassword.value)
        throw new Error("请设置管理员密码。");
      if (basics.admin.password !== passwordConfirmation.value)
        throw new Error("两次输入的管理员密码不一致。");
      const data = {
        admin: {
          username: basics.admin.username,
          ...(basics.admin.password ? { password: basics.admin.password } : {})
        },
        public_url: publicURL(basics.public_url),
        components: { alerting: basics.alerting },
        notification: notificationPayload(notification, savedNotification.value)
      };
      // 完成页要显示管理员密码：保存成功后只记在当前标签页（sessionStorage），关掉标签页或进入控制台就清除
      const typed = basics.admin.password;
      await perform(() => setupApi.configure(task.value.revision, data), 5);
      if (typed && !error.value) rememberAdminPassword(task.value?.id, typed);
    } else if (!executing.value) {
      await perform(() => setupApi.start(task.value.revision));
    }
  } catch (reason) {
    error.value = errorText(reason);
  }
}

/** 第 2、3 步「测试连接」：只检测并在本页显示结果，通过后再点「继续」进入下一步 */
async function testConnection() {
  if (locked.value || !task.value || !connectionSection.value) return;
  try {
    if (step.value === 2) {
      const data = postgresPayload(postgres);
      await perform(() => setupApi.postgres(task.value.revision, data));
    } else {
      const data = metricsPayload(metrics);
      await perform(() => setupApi.metrics(task.value.revision, data));
    }
  } catch (reason) {
    error.value = errorText(reason);
  }
}

function editURL() {
  editingURL.value = true;
  nextTick(() => publicURLInput.value?.focus());
}

function resetURL() {
  basics.public_url = autoURL.value;
  editingURL.value = false;
  edited();
}

/** 改了告警通知：之前的测试结果作废 */
function notifyEdited() {
  notifyTest.value = undefined;
}

/** 填了新的机器人地址就不再是「清除」 */
function notifyTokenEdited() {
  if (notification.token.trim()) notification.clear = false;
  notifyEdited();
}

/** 清除已保存的告警通知：保存后不发通知 */
function clearNotification() {
  Object.assign(notification, { token: "", at: "", clear: true });
  notifyEdited();
  edited();
}

/** 「发送测试消息」：安装服务直接发给 IM，不保存配置，结果显示在按钮下方 */
async function testNotification() {
  if (locked.value || notifyTesting.value || !task.value) return;
  let data: NotificationInput;
  try {
    data = notificationTestRequest(notification, savedNotification.value);
  } catch (reason) {
    notifyTest.value = notifyErrorView(reason);
    return;
  }
  notifyTesting.value = true;
  notifyTest.value = undefined;
  try {
    notifyTest.value = notifyResultView(
      await setupApi.testNotification(data),
      data.type
    );
  } catch (reason) {
    if (reason instanceof SetupRequestError && reason.status === 401) {
      authenticated.value = false;
      error.value = errorText(reason);
      return;
    }
    notifyTest.value = notifyErrorView(reason);
  } finally {
    notifyTesting.value = false;
  }
}

/** 第 6 步：勾选时校验并在本集群安装 Agent；不勾选或选择跳过时直接完成安装 */
async function submitAgent(skip = false) {
  if (locked.value || !task.value || !agentChoosing.value) return;
  if (skip || !agentForm.install) {
    await perform(() => setupApi.skipAgent(task.value.revision));
    return;
  }
  let options: SetupAgentOptions;
  try {
    options = agentPayload(agentForm);
  } catch (reason) {
    error.value = errorText(reason);
    return;
  }
  await perform(() => setupApi.installAgent(task.value.revision, options));
}

function agentEdited() {
  error.value = "";
}

function stepLabel(value: number): string {
  return String(value).padStart(2, "0");
}

function details(check: SetupCheck): string {
  return Array.isArray(check.details)
    ? check.details.join("\n")
    : check.details || "";
}

function checkName(check: SetupCheck): string {
  return (
    {
      postgres: "PostgreSQL 连接与权限",
      metrics: "时序库连接与指标标签",
      environment: "集群环境与部署权限",
      agent_installer: "本集群 Agent 安装权限",
      resources: "资源归属与准入检查",
      write: "指标写入验证",
      endpoints: "对外入口与登录验证",
      cleanup: "收尾与临时权限清理"
    }[check.name] || check.name
  );
}

function editConnections() {
  if (!canRetry.value || locked.value) return;
  repairing.value = true;
  applyState(task.value, true);
  step.value = 2;
  highestStep.value = 4;
  error.value = "";
}

function enterConsole(agent = false) {
  if (!completed.value) return;
  forgetAdminPassword();
  router.push({
    path: "/login",
    query: agent ? { redirect: "/workbench/index" } : {}
  });
}

function rememberAdminPassword(id: string | undefined, password: string) {
  if (!id) return;
  try {
    sessionStorage.setItem(adminPasswordKey(id), password);
  } catch {
    // 浏览器禁用存储时完成页只提示「第 4 步设置的密码」
  }
}

function forgetAdminPassword() {
  try {
    const prefix = adminPasswordKey("");
    for (let index = sessionStorage.length - 1; index >= 0; index--) {
      const name = sessionStorage.key(index);
      if (name?.startsWith(prefix)) sessionStorage.removeItem(name);
    }
  } catch {
    // 同上
  }
}

onMounted(initialize);
onBeforeUnmount(() => {
  active = false;
  stopPolling();
  key.value = "";
  postgres.password = metrics.password = basics.admin.password = "";
  notification.token = "";
  passwordConfirmation.value = "";
  passwordVisible.value = false;
  if (completed.value) forgetAdminPassword();
});
</script>

<template>
  <div class="setup-page">
    <header class="setup-topbar">
      <div class="setup-brand">
        <span class="brand-mark">K</span>KubeDoor<span class="brand-caption"
          >安装向导</span
        >
      </div>
      <button
        class="quiet-button"
        type="button"
        :disabled="busy || initializing"
        @click="executing && authenticated ? pollState() : initialize()"
      >
        刷新状态
      </button>
    </header>
    <main class="setup-layout">
      <aside class="setup-rail" aria-label="安装步骤">
        <div class="eyebrow">LET’S GET STARTED</div>
        <h1>让集群管理，<br />从简单开始。</h1>
        <p class="rail-intro">
          连接已有数据库，完成基础设置。<br />其余的，交给 KubeDoor。
        </p>
        <ol class="setup-steps">
          <li
            v-for="(item, index) in steps"
            :key="item.title"
            :class="{
              current: index + 1 === step,
              done: index + 1 < step || completed
            }"
            :aria-current="index + 1 === step ? 'step' : undefined"
          >
            <button
              type="button"
              :disabled="
                locked || executing || !authenticated || index + 1 > highestStep
              "
              @click="goStep(index + 1)"
            >
              <span class="step-number">{{
                index + 1 < step || completed ? "✓" : index + 1
              }}</span>
              <span
                ><b>{{ item.title }}</b
                ><small>{{ item.note }}</small></span
              >
            </button>
          </li>
        </ol>
        <div class="rail-note">
          <b>只需准备两项连接信息</b>一个 PostgreSQL
          数据库，以及一个可访问的时序数据库。安装完成后，即可接入集群。
        </div>
      </aside>
      <section class="setup-card" aria-label="KubeDoor 安装设置">
        <div class="card-heading">
          <span>STEP {{ stepLabel(step) }} / {{ stepLabel(steps.length) }}</span
          ><span v-if="task" class="saved-note">安装进度自动保存</span>
        </div>
        <div
          v-if="initializing"
          class="setup-panel initial-panel"
          role="status"
        >
          <h2>正在获取安装状态</h2>
          <p>确认当前环境和已有安装进度…</p>
        </div>
        <form
          v-else-if="!authenticated && !completed && !publicFinalizing"
          class="setup-panel"
          @submit.prevent="unlock"
        >
          <h2>欢迎使用 KubeDoor</h2>
          <p class="subtitle">验证安装密钥后，开始配置或继续已有安装。</p>
          <div class="setup-callout">
            安装密钥由 Web
            引导命令生成并输出。它用于保护首次安装入口，完成后自动失效。
          </div>
          <label for="setup-key">安装密钥</label
          ><input
            id="setup-key"
            v-model="key"
            type="password"
            autocomplete="off"
            :disabled="busy"
            required
            placeholder="输入启动时显示的安装密钥"
          />
          <p v-if="error" class="error-message" role="alert">{{ error }}</p>
          <button
            type="submit"
            class="action-button"
            :disabled="busy || !key.trim()"
          >
            {{ busy ? "正在验证…" : "验证并继续" }}
          </button>
          <p class="field-hint">
            如安装服务尚未启动，请等待就绪后点击右上角“刷新状态”。
          </p>
        </form>
        <template v-else>
          <form class="setup-panel" @submit.prevent="advance">
            <template v-if="step === 1">
              <h2>确认安装环境</h2>
              <p class="subtitle">
                控制端将安装到 Web 所在集群，无需再上传 kubeconfig。
              </p>
              <div
                class="arch"
                role="img"
                aria-label="KubeDoor 架构：用户通过浏览器访问 kubedoor-master 控制端，各 K8S 集群的 kubedoor-agent 主动连接控制端"
              >
                <div class="arch-user">
                  <span class="arch-avatar" aria-hidden="true">
                    <svg viewBox="0 0 24 24">
                      <circle cx="12" cy="8" r="4" />
                      <path d="M4 21c0-4.4 3.6-8 8-8s8 3.6 8 8" />
                    </svg>
                  </span>
                  <b>用户</b>
                  <small>浏览器访问</small>
                </div>
                <div class="arch-link" aria-hidden="true">
                  <span>HTTP(S)</span>
                </div>
                <div class="arch-group arch-master">
                  <div class="arch-title">
                    <b>kubedoor-master</b><em>本次安装</em>
                  </div>
                  <ul>
                    <li>kubedoor-web</li>
                    <li>kubedoor-master</li>
                    <li>kubedoor-ai</li>
                    <li
                      class="arch-more"
                      title="Grafana 看板，以及可选的告警组件 kubedoor-alarm、vmalert、Alertmanager"
                    >
                      ···
                    </li>
                  </ul>
                </div>
                <div class="arch-link back" aria-hidden="true">
                  <span>Agent 主动连接</span>
                </div>
                <div class="arch-group arch-agents">
                  <div class="arch-title">
                    <b>各 K8S 集群</b><em>第 6 步起接入</em>
                  </div>
                  <ul>
                    <li>kubedoor-agent</li>
                    <li>kubedoor-agent</li>
                    <li>kubedoor-agent</li>
                    <li class="arch-more" title="更多 K8S 集群">···</li>
                  </ul>
                </div>
              </div>
              <p class="arch-note">
                本次安装 <b>kubedoor-master 控制端</b>：Web 入口、控制服务、AI
                助手、Grafana 监控看板，以及第 4
                步选择的告警组件（vmalert、Alertmanager、kubedoor-alarm）。<b
                  >kubedoor-agent</b
                >
                每个 K8S 集群装一个，主动连接控制端：本集群可以在第 6
                步直接安装，其他集群在安装完成后从控制台接入。
              </p>
              <div class="environment-card">
                <span class="cluster-mark">⬡</span>
                <div>
                  <b>当前 Kubernetes 集群</b>
                  <p>{{ environment?.server || "使用集群内安装身份" }}</p>
                  <p>
                    命名空间 {{ environment?.namespace || "kubedoor"
                    }}<template v-if="environment?.version">
                      · {{ environment.version }}</template
                    >
                  </p>
                </div>
              </div>
              <ul v-if="environmentChecks.length" class="checks-list">
                <li
                  v-for="check in environmentChecks"
                  :key="check.id || check.name"
                  :class="checkTone(check.status)"
                >
                  <span class="check-dot">{{
                    toneMark(checkTone(check.status))
                  }}</span
                  ><span
                    >{{ checkName(check)
                    }}<small v-if="check.message">{{
                      check.message
                    }}</small></span
                  ><b>{{ serviceStatus[check.status] || check.status }}</b>
                </li>
              </ul>
              <div class="setup-callout teal">
                <b>已有进度会继续保留</b>
                <p>安装任务在集群内运行。刷新或关闭浏览器不会取消后台安装。</p>
              </div>
            </template>
            <template v-else-if="step === 2">
              <h2>连接 PostgreSQL</h2>
              <p class="subtitle">用于保存账号、系统配置、告警和操作记录。</p>
              <fieldset :disabled="locked" @input="edited">
                <div class="field-grid">
                  <div>
                    <label for="pg-host">数据库地址</label
                    ><input
                      id="pg-host"
                      v-model="postgres.host"
                      :readonly="task?.target_locked"
                      autocomplete="off"
                      placeholder="例如 192.0.2.10"
                    />
                    <p class="field-hint">
                      填写集群内 Pod 可访问的主机名或 IP，不含 http://，不能用
                      localhost。
                    </p>
                  </div>
                  <div>
                    <label for="pg-port">端口</label
                    ><input
                      id="pg-port"
                      v-model.number="postgres.port"
                      :readonly="task?.target_locked"
                      type="number"
                      min="1"
                      max="65535"
                    />
                  </div>
                  <div>
                    <label for="pg-database">数据库名</label
                    ><input
                      id="pg-database"
                      v-model="postgres.database"
                      :readonly="task?.target_locked"
                      autocomplete="off"
                    />
                  </div>
                  <div>
                    <label for="pg-user">数据库用户名</label
                    ><input
                      id="pg-user"
                      v-model="postgres.user"
                      :readonly="task?.target_locked"
                      autocomplete="off"
                    />
                  </div>
                  <div>
                    <label for="pg-password">数据库密码</label
                    ><input
                      id="pg-password"
                      v-model="postgres.password"
                      type="password"
                      autocomplete="off"
                      :placeholder="
                        postgres.has_password
                          ? '已保存，留空保留原密码'
                          : '输入数据库密码'
                      "
                    />
                    <p class="field-hint">
                      密码只用于连接验证和运行配置，不会在页面回显。
                    </p>
                  </div>
                  <div>
                    <label for="pg-ssl">SSL 模式</label
                    ><select id="pg-ssl" v-model="postgres.sslmode">
                      <option value="disable">禁用 SSL（可信内网）</option>
                      <option value="prefer">优先使用 SSL</option>
                      <option value="require">必须使用 SSL</option>
                      <option value="verify-full">校验证书与主机名</option>
                    </select>
                  </div>
                </div>
              </fieldset>
              <div class="setup-callout">
                <b>点击“测试连接”验证</b>
                <p>
                  网络连接、账号认证、数据库访问和建表权限；数据库不存在时用该账号自动创建。测试通过后点“继续”，正式初始化在最后点击“开始安装”时执行。
                </p>
              </div>
            </template>
            <template v-else-if="step === 3">
              <h2>连接时序数据库</h2>
              <p class="subtitle">
                读取集群监控数据，并接收 Agent 上报的指标。
              </p>
              <fieldset :disabled="locked" @input="edited" @change="edited">
                <div class="field-grid three">
                  <div>
                    <label for="metrics-type">时序库类型</label
                    ><select
                      id="metrics-type"
                      v-model="metrics.type"
                      @change="regenerateEndpoints"
                    >
                      <option value="Victoria-Metrics-Single">
                        VictoriaMetrics 单机版（推荐）
                      </option>
                      <option value="Victoria-Metrics-Cluster">
                        VictoriaMetrics 集群版
                      </option>
                    </select>
                  </div>
                  <div>
                    <label for="metrics-user"
                      >账号 <span class="optional">可选</span></label
                    ><input
                      id="metrics-user"
                      v-model="metrics.user"
                      autocomplete="off"
                      @input="regenerateEndpoints"
                    />
                  </div>
                  <div>
                    <label for="metrics-password"
                      >密码 <span class="optional">可选</span></label
                    ><input
                      id="metrics-password"
                      v-model="metrics.password"
                      type="password"
                      autocomplete="off"
                      :placeholder="
                        metrics.has_password
                          ? '已保存，留空保留原密码'
                          : '无认证时留空'
                      "
                      @input="regenerateEndpoints"
                    />
                  </div>
                </div>
                <div
                  class="field-grid address spaced"
                  :class="{
                    tenant: metrics.type === 'Victoria-Metrics-Cluster'
                  }"
                >
                  <div>
                    <label for="metrics-host">连接地址</label>
                    <div class="input-group">
                      <select
                        v-model="metrics.scheme"
                        aria-label="连接协议"
                        @change="regenerateEndpoints"
                      >
                        <option value="http">http://</option>
                        <option value="https">https://</option></select
                      ><input
                        id="metrics-host"
                        v-model="metrics.host"
                        autocomplete="off"
                        placeholder="例如 192.0.2.11"
                        @input="regenerateEndpoints"
                      />
                    </div>
                    <p class="field-hint">
                      只填主机名或 IP，不需要填写端口：单机版用 8428，集群版读
                      8481、写 8480，会自动带上。
                    </p>
                  </div>
                  <div v-if="metrics.type === 'Victoria-Metrics-Cluster'">
                    <label for="metrics-tenant">租户 ID</label
                    ><input
                      id="metrics-tenant"
                      v-model="metrics.tenant"
                      autocomplete="off"
                      placeholder="0"
                      @input="regenerateEndpoints"
                    />
                  </div>
                </div>
                <div class="spaced">
                  <label for="metrics-read-url">远程读地址</label
                  ><input
                    id="metrics-read-url"
                    v-model="metrics.read_url"
                    type="url"
                    autocomplete="off"
                  />
                </div>
                <div class="spaced">
                  <label for="metrics-write-url">远程写地址</label
                  ><input
                    id="metrics-write-url"
                    v-model="metrics.write_url"
                    type="url"
                    autocomplete="off"
                  />
                  <p class="field-hint">
                    按上面的类型、地址和账号自动生成；端口或路径不同（如经过
                    vmauth、反向代理）时可直接修改，再改上面的内容会重新生成。各集群的
                    vmagent 直接写入这个地址，需要能从各 K8S 集群访问。
                  </p>
                </div>
                <div class="label-card">
                  <h3>集群指标标签 <span class="important">重要</span></h3>
                  <label for="cluster-label"
                    >标签 key（master 端仅填写 key 即可）</label
                  ><input
                    id="cluster-label"
                    v-model="metrics.tag_key"
                    placeholder="origin_prometheus"
                  />
                  <p>
                    这是用于多 K8S 监控数据，vmagent
                    通过远程写方式，写入到同一个时序数据库的场景。使用远程存储时，这个
                    key 和 value 会作为标签增加到每一个指标中（在 Prometheus /
                    vmagent 配置中的 <strong>external_labels</strong>
                    参数），这样通过这个标签就可以区分出指标属于哪个 K8S 了。
                  </p>
                  <p>
                    注意：<strong>external_labels</strong> 的 key 要和每一个 K8S
                    安装的 vmagent 的 <strong>external_labels</strong> 的 key
                    保持一致。如果是新安装 VictoriaMetrics，可以使用默认配置的
                    <strong>origin_prometheus</strong> 作为
                    <strong>external_labels</strong> 的 key；如果已有完整的
                    VictoriaMetrics，请填写实际的值。
                  </p>
                  <p>
                    例如
                    <code>origin_prometheus="prod-k8s"</code>：这里只填
                    key，value（集群名称 <code>prod-k8s</code>）在第 6
                    步安装本集群 Agent 或之后接入其他集群时填写。
                  </p>
                </div>
              </fieldset>
              <div class="setup-callout teal">
                <b>测试连接时会查找带这个标签的数据</b>
                <p>
                  已有完整监控应能查到带该标签的数据；新部署的时序库查不到是正常的，结果显示“请留意”，可以继续。写入链路在安装时验证。
                </p>
              </div>
            </template>
            <template v-else-if="step === 4">
              <h2>完成基础设置</h2>
              <p class="subtitle">
                设置管理员密码，确认访问地址，选择是否安装告警组件，按需填写告警通知。
              </p>
              <fieldset :disabled="locked" @input="edited" @change="edited">
                <div class="field-grid">
                  <div>
                    <label for="admin-user">管理员账号</label
                    ><input
                      id="admin-user"
                      :value="basics.admin.username"
                      readonly
                    />
                  </div>
                  <div>
                    <label for="admin-password">管理员密码</label
                    ><input
                      id="admin-password"
                      v-model="basics.admin.password"
                      :disabled="task?.target_locked"
                      type="password"
                      autocomplete="new-password"
                      :placeholder="
                        hasAdminPassword
                          ? '已保存，留空保留原密码'
                          : '设置管理员密码'
                      "
                    />
                  </div>
                  <div class="full">
                    <label for="admin-confirm">确认管理员密码</label
                    ><input
                      id="admin-confirm"
                      v-model="passwordConfirmation"
                      :disabled="task?.target_locked"
                      type="password"
                      autocomplete="new-password"
                      :placeholder="
                        hasAdminPassword
                          ? '修改密码时再次输入'
                          : '再次输入管理员密码'
                      "
                    />
                  </div>
                  <div class="full">
                    <label for="public-url">KubeDoor 访问地址</label>
                    <div class="url-field">
                      <input
                        id="public-url"
                        ref="publicURLInput"
                        v-model="basics.public_url"
                        type="url"
                        :readonly="!editingURL && !!basics.public_url"
                        placeholder="http://192.0.2.10:30080"
                      />
                      <button
                        v-if="!editingURL && basics.public_url"
                        class="secondary-button"
                        type="button"
                        @click="editURL"
                      >
                        修改</button
                      ><button
                        v-else-if="autoURL && basics.public_url !== autoURL"
                        class="secondary-button"
                        type="button"
                        @click="resetURL"
                      >
                        恢复默认
                      </button>
                    </div>
                    <p class="field-hint">{{ publicURLNote }}</p>
                  </div>
                </div>
                <details class="advanced alerting">
                  <summary>
                    安装告警组件<span
                      class="summary-state"
                      :class="{ off: !basics.alerting }"
                      >{{ basics.alerting ? "安装" : "不安装" }}</span
                    >
                  </summary>
                  <div
                    class="alert-flow"
                    :class="{ off: !basics.alerting }"
                    role="img"
                    aria-label="告警链路：vmalert 从 VictoriaMetrics 读取指标并计算告警规则，告警交给 alertmanager 分组路由，再由 kubedoor-alarm 记录并发送通知"
                  >
                    <div class="flow-node external">
                      <b>VictoriaMetrics</b><small>数据源</small>
                    </div>
                    <div class="arch-link back" aria-hidden="true" />
                    <div class="flow-node">
                      <b>vmalert</b><small>告警规则</small>
                    </div>
                    <div class="arch-link" aria-hidden="true" />
                    <div class="flow-node">
                      <b>alertmanager</b><small>告警路由</small>
                    </div>
                    <div class="arch-link" aria-hidden="true" />
                    <div class="flow-node">
                      <b>kubedoor-alarm</b><small>告警通知</small>
                    </div>
                  </div>
                  <label class="switch-row"
                    ><b>是否安装</b
                    ><input
                      v-model="basics.alerting"
                      type="checkbox"
                      role="switch"
                      :disabled="task?.target_locked"
                    /><span class="switch-track" aria-hidden="true" /><span
                      class="switch-state"
                      >{{ basics.alerting ? "开启" : "关闭" }}</span
                    ></label
                  >
                  <p class="field-hint">
                    {{
                      basics.alerting
                        ? "开启：安装 vmalert、Alertmanager 和 kubedoor-alarm，按告警规则检查指标并发送告警通知。"
                        : "关闭：三个组件都不安装，不会产生指标告警。"
                    }}K8S 事件告警由控制服务直接发送，不受这里影响。
                  </p>
                </details>
                <details class="advanced notification">
                  <summary>
                    告警通知<span class="optional">可选</span
                    ><span
                      class="summary-state"
                      :class="{ off: !notifyBadge.on }"
                      >{{ notifyBadge.text }}</span
                    >
                  </summary>
                  <p class="field-hint">
                    把下表的通知发到 IM
                    群机器人。留空表示不发通知，装完可以在「系统管理 →
                    消息通知」配置，还能给各集群单独配置群。
                  </p>
                  <div class="field-grid">
                    <div>
                      <label for="notify-type">IM 类型</label
                      ><select
                        id="notify-type"
                        v-model="notification.type"
                        @change="notifyEdited"
                      >
                        <option
                          v-for="item in notifyOptions"
                          :key="item.value"
                          :value="item.value"
                        >
                          {{ item.label }}
                        </option>
                      </select>
                    </div>
                    <div>
                      <label for="notify-at"
                        >默认 @ 人 <span class="optional">可选</span></label
                      ><input
                        id="notify-at"
                        v-model="notification.at"
                        autocomplete="off"
                        maxlength="128"
                        :placeholder="notifyHints.at"
                        @input="notifyEdited"
                      />
                    </div>
                    <div class="full">
                      <label for="notify-token">机器人地址</label>
                      <div class="url-field notify-field">
                        <input
                          id="notify-token"
                          v-model="notification.token"
                          type="password"
                          autocomplete="off"
                          :placeholder="notifyPlaceholder"
                          @input="notifyTokenEdited"
                        /><button
                          v-if="
                            savedNotification?.configured && !notification.clear
                          "
                          class="secondary-button"
                          type="button"
                          @click="clearNotification"
                        >
                          清除</button
                        ><button
                          class="secondary-button"
                          type="button"
                          data-notify-test-button
                          :disabled="notifyTesting"
                          @click="testNotification"
                        >
                          {{ notifyTesting ? "正在发送…" : "发送测试消息" }}
                        </button>
                      </div>
                      <p class="field-hint">
                        {{
                          notifyHints.tokenHelp
                        }}。可以粘贴整条地址，只保存其中的 token，页面不回显。
                      </p>
                    </div>
                  </div>
                  <ul
                    v-if="notifyTest"
                    class="checks-list"
                    aria-label="测试消息结果"
                    aria-live="polite"
                  >
                    <li
                      :class="notifyTest.tone"
                      :data-notify-test="notifyTest.tone"
                    >
                      <span class="check-dot">{{
                        toneMark(notifyTest.tone)
                      }}</span
                      ><span
                        >测试消息<small>{{ notifyTest.message }}</small></span
                      ><b>{{ notifyTest.label }}</b>
                    </li>
                  </ul>
                  <table class="notice-table">
                    <thead>
                      <tr>
                        <th scope="col">通知</th>
                        <th scope="col">谁发送</th>
                        <th scope="col">什么时候发</th>
                      </tr>
                    </thead>
                    <tbody>
                      <tr
                        v-for="row in NOTICE_ROWS"
                        :key="row.name"
                        data-notice-row
                      >
                        <th scope="row">{{ row.name }}</th>
                        <td>{{ row.sender }}</td>
                        <td>{{ row.when }}</td>
                      </tr>
                    </tbody>
                  </table>
                  <p class="field-hint">{{ NOTICE_NOTE }}</p>
                </details>
              </fieldset>
              <div class="setup-callout">
                AI
                模型、按集群单独配置的通知群、镜像更新规则和仓库凭据，可进入控制台后配置。
              </div>
            </template>
            <template v-else-if="!executing">
              <h2>准备好了，开始安装</h2>
              <p class="subtitle">
                确认下面的信息，KubeDoor 将自动部署并逐项检查。
              </p>
              <dl class="summary-list">
                <div>
                  <dt>安装位置</dt>
                  <dd>当前集群 / {{ environment?.namespace || "kubedoor" }}</dd>
                </div>
                <div>
                  <dt>PostgreSQL</dt>
                  <dd>
                    {{ postgres.host }}:{{ postgres.port }} /
                    {{ postgres.database }}
                  </dd>
                </div>
                <div>
                  <dt>时序数据库</dt>
                  <dd>{{ metrics.read_url }}</dd>
                </div>
                <div>
                  <dt>集群指标标签</dt>
                  <dd>{{ metrics.tag_key }}</dd>
                </div>
                <div>
                  <dt>访问地址</dt>
                  <dd>{{ basics.public_url }}</dd>
                </div>
                <div>
                  <dt>管理员</dt>
                  <dd>{{ basics.admin.username }}</dd>
                </div>
                <div>
                  <dt>告警组件</dt>
                  <dd>
                    {{
                      basics.alerting
                        ? "安装 vmalert、Alertmanager、kubedoor-alarm"
                        : "不安装"
                    }}
                  </dd>
                </div>
                <div data-summary="notification">
                  <dt>告警通知</dt>
                  <dd>{{ notificationSummary(savedNotification) }}</dd>
                </div>
                <div v-if="agentInstallerCheck">
                  <dt>本集群 Agent</dt>
                  <dd>
                    {{
                      checkTone(agentInstallerCheck.status) === "success"
                        ? "控制端就绪后，在第 6 步选择是否安装"
                        : "未授予临时集群权限，安装后从控制台接入"
                    }}
                  </dd>
                </div>
              </dl>
              <div class="setup-callout teal">
                <b>全部检查通过，才会通知安装成功</b>
                <p>
                  将保存连接配置、初始化数据表并部署服务；启用指标写入时会写入少量安装检测指标。过程中可关闭页面，稍后回来继续查看。
                </p>
              </div>
            </template>
            <template v-else-if="agentPhase">
              <div class="control-ready" role="status">
                <span class="control-ready-mark" aria-hidden="true">✓</span>
                <div>
                  <b>KubeDoor 控制端组件安装完成</b>
                  <p>全部服务与最终检查均已通过。</p>
                </div>
              </div>
              <h2>{{ agentTitle }}</h2>
              <p class="subtitle">
                {{
                  agentChoosing
                    ? "可以顺便把控制端所在的集群接入 KubeDoor，也可以跳过，稍后在控制台接入。"
                    : "安装在后台进行，可以关闭页面，稍后回来查看结果。"
                }}
              </p>
              <div v-if="connectionError" class="warning-message" role="status">
                {{ connectionError }} 当前显示上次收到的状态，正在重新检查。
              </div>
              <div class="environment-card cluster-info">
                <div>
                  <b>本集群（控制端所在集群）</b>
                  <p>{{ environment?.server || "使用集群内安装身份" }}</p>
                  <p v-if="!agentChoosing && agent?.options">
                    集群名称 {{ agent.options.env }} · 命名空间
                    {{ agent.options.namespace }} ·
                    {{
                      agent.options.mode === "full"
                        ? "安装指标采集"
                        : "只安装 Agent"
                    }}
                  </p>
                  <p v-else>
                    控制端命名空间 {{ environment?.namespace || "kubedoor"
                    }}<template v-if="environment?.version">
                      · {{ environment.version }}</template
                    >
                  </p>
                </div>
              </div>
              <template v-if="agentSteps.length">
                <div class="service-progress">
                  <div>
                    <span
                      :style="{
                        width: `${(agentDone / agentSteps.length) * 100}%`
                      }"
                    />
                  </div>
                  <b>{{ agentDone }} / {{ agentSteps.length }} 步完成</b>
                </div>
                <ul class="checks-list" aria-label="本集群 Agent 安装进度">
                  <li
                    v-for="(item, index) in agentSteps"
                    :key="`${index}-${item.name}`"
                    :class="checkTone(item.status)"
                    :data-agent-step="item.status"
                  >
                    <span class="check-dot">{{
                      toneMark(checkTone(item.status))
                    }}</span
                    ><span
                      >{{ item.name
                      }}<small v-if="item.message">{{
                        item.message
                      }}</small></span
                    ><b>{{ agentStepStatus[item.status] || item.status }}</b>
                  </li>
                </ul>
              </template>
              <template v-if="agentChecks.length">
                <h3 class="checks-heading">安装前检查</h3>
                <ul class="checks-list">
                  <li
                    v-for="(check, index) in agentChecks"
                    :key="`${index}-${check.id || check.name}`"
                    :class="checkTone(check.status)"
                  >
                    <span class="check-dot">{{
                      toneMark(checkTone(check.status))
                    }}</span
                    ><span
                      >{{ checkName(check)
                      }}<small v-if="check.message">{{
                        check.message
                      }}</small></span
                    ><b>{{ serviceStatus[check.status] || check.status }}</b>
                  </li>
                </ul>
              </template>
              <div
                v-if="agentStatus === 'partial' && agent?.message"
                class="warning-message"
                role="status"
              >
                {{ agent.message }}
              </div>
              <div
                v-if="
                  agentStatus === 'failed' && (agent?.error || agent?.message)
                "
                class="error-message"
                role="alert"
              >
                {{ agent.error || agent.message }}
              </div>
              <template v-if="agentChoosing">
                <div class="agent-card" :class="{ checked: agentForm.install }">
                  <label class="agent-choice"
                    ><input
                      v-model="agentForm.install"
                      type="checkbox"
                      :disabled="locked"
                      @change="agentEdited"
                    /><span
                      ><b>在本集群安装 Agent</b
                      ><small
                        >接入后即可在 KubeDoor
                        中管理本集群的资源、日志、终端和监控。</small
                      ></span
                    ></label
                  >
                  <fieldset
                    v-if="agentForm.install"
                    class="agent-options"
                    :disabled="locked"
                    @input="agentEdited"
                    @change="agentEdited"
                  >
                    <div class="field-grid">
                      <div>
                        <label for="agent-env">集群名称</label
                        ><input
                          id="agent-env"
                          v-model="agentForm.env"
                          autocomplete="off"
                          maxlength="100"
                          placeholder="例如 prod-k8s"
                        />
                        <p class="field-hint">
                          也是集群标签的值：每个指标都会带上
                          <code
                            >{{ metrics.tag_key || "origin_prometheus" }}="{{
                              agentForm.env.trim() || "prod-k8s"
                            }}"</code
                          >，用来区分集群，安装后请保持不变。
                        </p>
                      </div>
                      <div>
                        <label for="agent-aliases"
                          >集群别名 <span class="optional">可选</span></label
                        ><input
                          id="agent-aliases"
                          v-model="agentForm.aliases"
                          autocomplete="off"
                          placeholder="例如 生产, prod"
                        />
                        <p class="field-hint">
                          多个用逗号分隔，最多 20 个；AI
                          助手可以按别名识别集群。
                        </p>
                      </div>
                      <div>
                        <label for="agent-namespace">命名空间</label
                        ><input
                          id="agent-namespace"
                          v-model="agentForm.namespace"
                          autocomplete="off"
                        />
                        <p class="field-hint">
                          Agent 安装到这个命名空间，不存在时自动创建。
                        </p>
                      </div>
                      <div>
                        <label for="agent-mode">指标采集</label
                        ><select id="agent-mode" v-model="agentForm.mode">
                          <option value="full">
                            安装 vmagent 采集指标（推荐）
                          </option>
                          <option value="agent">只安装 Agent</option>
                        </select>
                        <p class="field-hint">
                          {{
                            agentForm.mode === "full"
                              ? "指标由 vmagent 直接写入第 3 步的远程写地址。"
                              : "已有指标采集时选择，现有指标需要带上同样的集群标签。"
                          }}
                        </p>
                      </div>
                      <template v-if="agentForm.mode === 'full'">
                        <div>
                          <label for="agent-ksm">kube-state-metrics</label
                          ><select id="agent-ksm" v-model="agentForm.ksm">
                            <option value="auto">自动检测（推荐）</option>
                            <option value="install">安装</option>
                            <option value="reuse">复用已有</option>
                          </select>
                        </div>
                        <div>
                          <label for="agent-node-exporter">node-exporter</label
                          ><select
                            id="agent-node-exporter"
                            v-model="agentForm.node_exporter"
                          >
                            <option value="auto">自动检测（推荐）</option>
                            <option value="install">安装</option>
                            <option value="reuse">复用已有</option>
                          </select>
                        </div>
                        <p class="field-hint full">
                          自动检测：集群里已有可用的组件就复用，没有就安装。安装
                          node-exporter 需要主机网络和特权容器权限。
                        </p>
                      </template>
                    </div>
                  </fieldset>
                </div>
                <div class="setup-callout">
                  <b>使用引导时授予的临时集群权限</b>
                  <p>
                    选择安装时，安装服务会用引导阶段授予的临时集群权限在本集群创建
                    Agent{{
                      agentForm.install && agentForm.mode === "full"
                        ? " 和指标采集组件"
                        : ""
                    }}，安装完成后撤销这些权限。{{
                      agentDeadline
                        ? `到 ${agentDeadline} 仍未选择`
                        : "超过 2 小时不做选择"
                    }}，会自动按「不安装」完成。
                  </p>
                </div>
              </template>
            </template>
            <template v-else>
              <h2>{{ installationTitle }}</h2>
              <p class="subtitle">
                每项服务都需就绪并通过健康检查。页面始终保留各项结果。
              </p>
              <div v-if="connectionError" class="warning-message" role="status">
                {{ connectionError }} 当前显示上次收到的状态，正在重新检查。
              </div>
              <div class="service-progress">
                <div><span :style="{ width: `${progress}%` }" /></div>
                <b
                  >{{ readyServices }} / {{ chosenServices.length }} 服务就绪</b
                >
              </div>
              <ul class="service-list" aria-label="各服务安装状态">
                <li
                  v-for="service in services"
                  :key="service.id"
                  :class="checkTone(service.status)"
                  :data-service="service.id"
                  :data-status="service.status"
                >
                  <strong
                    >{{ service.name
                    }}<code
                      v-if="service.resource"
                      class="service-resource"
                      :title="`Deployment ${service.resource}`"
                      >{{ service.resource }}</code
                    ></strong
                  ><span class="status-badge">{{
                    serviceStatus[service.status] || service.status
                  }}</span>
                  <p>
                    {{
                      service.message ||
                      (checkTone(service.status) === "success"
                        ? "部署与健康检查通过"
                        : service.status === "skipped"
                          ? "此次未选择此组件"
                          : "等待安装服务更新状态")
                    }}
                  </p>
                  <details v-if="details(service)">
                    <summary>查看详情</summary>
                    <pre>{{ details(service) }}</pre>
                  </details>
                </li>
              </ul>
              <template v-if="agentResult">
                <h3 class="checks-heading">本集群 Agent</h3>
                <ul class="checks-list">
                  <li class="agent-result" :class="agentResult.tone">
                    <span class="check-dot">{{
                      toneMark(agentResult.tone)
                    }}</span
                    ><span
                      >{{ agentResult.title
                      }}<small>{{ agentResult.note }}</small></span
                    ><b>{{ agentResult.label }}</b>
                  </li>
                </ul>
              </template>
              <h3 class="checks-heading">外部依赖与最终检查</h3>
              <ul class="checks-list">
                <li
                  v-for="check in finalChecks"
                  :key="check.id || check.name"
                  :class="checkTone(check.status)"
                >
                  <span class="check-dot">{{
                    toneMark(checkTone(check.status))
                  }}</span
                  ><span
                    >{{ checkName(check)
                    }}<small v-if="check.message">{{
                      check.message
                    }}</small></span
                  ><b>{{ serviceStatus[check.status] || check.status }}</b>
                </li>
              </ul>
              <div
                v-if="taskError && !completed"
                class="error-message"
                role="alert"
              >
                {{ taskError }}
              </div>
              <div v-if="publicFinalizing" class="setup-callout">
                正在同步安装结果并完成权限清理。如长时间无进展，可重新执行原
                install.sh web 命令恢复收尾。
              </div>
              <div
                v-if="!completed && !running && !connectionError"
                class="setup-callout"
              >
                已有部署结果会保留。请根据失败项修复问题后重试；其他服务也会重新检查健康状态。
              </div>
              <div v-if="completed" class="completion" role="status">
                <div class="success-mark">✓</div>
                <h2>安装成功，可以开始使用了</h2>
                <p>
                  {{
                    agentResult?.installed
                      ? "所有选定服务与最终检查均已通过，本集群已经接入。登录后即可查看资源和监控，其他集群可在控制台接入。"
                      : "所有选定服务与最终检查均已通过。登录后接入第一个集群，即可查看资源和监控。"
                  }}
                </p>
                <dl class="access-info" aria-label="登录信息">
                  <div>
                    <dt>访问地址</dt>
                    <dd>
                      <a
                        v-if="accessURL"
                        :href="accessURL"
                        target="_blank"
                        rel="noopener"
                        >{{ accessURL }}</a
                      ><span v-else class="access-muted">未记录</span>
                    </dd>
                  </div>
                  <div>
                    <dt>管理员账号</dt>
                    <dd>
                      <code>{{ basics.admin.username || "kubedoor" }}</code>
                    </dd>
                  </div>
                  <div>
                    <dt>管理员密码</dt>
                    <dd>
                      <template v-if="adminPassword">
                        <code class="access-password">{{
                          passwordVisible ? adminPassword : "••••••••"
                        }}</code
                        ><button
                          class="eye-button"
                          type="button"
                          :aria-label="
                            passwordVisible ? '隐藏密码' : '查看密码'
                          "
                          :title="passwordVisible ? '隐藏密码' : '查看密码'"
                          :aria-pressed="passwordVisible"
                          @click="passwordVisible = !passwordVisible"
                        >
                          <svg viewBox="0 0 24 24" aria-hidden="true">
                            <path
                              d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"
                            />
                            <circle cx="12" cy="12" r="3" />
                            <path v-if="passwordVisible" d="M4 4l16 16" />
                          </svg>
                        </button>
                      </template>
                      <span v-else class="access-muted"
                        >第 4 步设置的管理员密码</span
                      >
                    </dd>
                  </div>
                </dl>
                <div v-if="agentResult?.installed" class="completion-actions">
                  <button
                    class="action-button"
                    type="button"
                    @click="enterConsole()"
                  >
                    进入控制台</button
                  ><button
                    class="secondary-button"
                    type="button"
                    @click="enterConsole(true)"
                  >
                    接入其他集群
                  </button>
                </div>
                <div v-else class="completion-actions">
                  <button
                    class="action-button"
                    type="button"
                    @click="enterConsole(true)"
                  >
                    接入第一个集群</button
                  ><button
                    class="secondary-button"
                    type="button"
                    @click="enterConsole()"
                  >
                    进入控制台
                  </button>
                </div>
              </div>
            </template>
            <ul v-if="!executing && groupChecks.length" class="checks-list">
              <li
                v-for="check in groupChecks"
                :key="check.id || check.name"
                :class="checkTone(check.status)"
              >
                <span class="check-dot">{{
                  toneMark(checkTone(check.status))
                }}</span
                ><span
                  >{{ checkName(check)
                  }}<small>{{ check.message }}</small></span
                ><b>{{ serviceStatus[check.status] || check.status }}</b>
              </li>
            </ul>
            <div v-if="error" class="error-message" role="alert">
              {{ error }}
            </div>
            <div class="setup-actions">
              <p>
                {{
                  completed
                    ? "首次安装入口已关闭。"
                    : agentChoosing
                      ? "不安装也可以稍后在控制台接入本集群。"
                      : agentPhase
                        ? "可以关闭页面，后台安装会继续。"
                        : executing
                          ? "服务就绪数量不代表最终检查已通过。"
                          : connectionSection
                            ? connectionReady
                              ? "测试已通过，点“继续”进入下一步。"
                              : "先测试连接，通过后再继续。"
                            : "设置通过检查后会自动保存。"
                }}
              </p>
              <div>
                <button
                  v-if="canRetry"
                  class="secondary-button"
                  type="button"
                  :disabled="locked"
                  @click="editConnections"
                >
                  修改连接设置
                </button>
                <button
                  v-if="
                    agentChoosing &&
                    agentStatus === 'failed' &&
                    agentForm.install
                  "
                  class="secondary-button"
                  type="button"
                  :disabled="locked"
                  @click="submitAgent(true)"
                >
                  跳过，直接完成
                </button>
                <button
                  v-if="step > 1 && !executing"
                  class="secondary-button"
                  type="button"
                  :disabled="locked"
                  @click="goStep(step - 1)"
                >
                  ← 上一步</button
                ><button
                  v-if="connectionReady && !executing"
                  class="secondary-button"
                  type="button"
                  :disabled="locked"
                  @click="testConnection"
                >
                  重新测试</button
                ><button
                  v-if="
                    !completed &&
                    (!executing || running || canRetry || agentPhase)
                  "
                  class="action-button"
                  type="submit"
                  :disabled="locked || !task"
                >
                  {{ actionText }}<span v-if="!locked"> →</span></button
                ><button
                  v-else-if="!completed"
                  class="secondary-button"
                  type="button"
                  :disabled="busy"
                  @click="pollState"
                >
                  刷新检查结果
                </button>
              </div>
            </div>
          </form>
        </template>
      </section>
    </main>
    <footer class="page-footnote">
      KubeDoor · 数据留在你的环境，管理回归简单
    </footer>
  </div>
</template>

<style scoped lang="scss">
.setup-page {
  --ink: #19304c;
  --muted: #718198;
  --blue: #4263ed;
  --line: #e6edf5;
  min-height: 100vh;
  color: var(--ink);
  background:
    radial-gradient(ellipse at 8% 6%, #dfeaff 0, transparent 45%),
    radial-gradient(ellipse at 97% 88%, #e2f4ee 0, transparent 40%), #f4f7fc;
  font-size: 14px;
  line-height: 1.65;
  padding-bottom: 16px;
  * {
    box-sizing: border-box;
  }
  button,
  input,
  select {
    font: inherit;
  }
  button {
    cursor: pointer;
  }
  button:disabled {
    cursor: default;
    opacity: 0.6;
  }
  button:focus-visible,
  input:focus-visible,
  select:focus-visible,
  summary:focus-visible {
    outline: 3px solid #95b7f3;
    outline-offset: 3px;
  }
}
.setup-topbar {
  max-width: 1320px;
  margin: auto;
  padding: 28px 40px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 15px;
}
.setup-brand {
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: 23px;
  font-weight: 750;
}
.brand-mark {
  display: grid;
  place-items: center;
  width: 38px;
  height: 38px;
  border-radius: 12px;
  background: var(--blue);
  color: white;
}
.brand-caption {
  font-size: 12px;
  font-weight: 400;
  color: var(--muted);
  border-left: 1px solid #ccd7e6;
  padding-left: 14px;
}
.quiet-button {
  border: 0;
  background: transparent;
  color: #627990;
  font-size: 12px !important;
}
.setup-layout {
  max-width: 1240px;
  display: grid;
  grid-template-columns: 265px minmax(0, 1fr);
  gap: 38px;
  padding: 0 28px;
  margin: 18px auto 0;
}
.setup-rail {
  padding-top: 27px;
}
.eyebrow {
  font-size: 11px;
  letter-spacing: 2px;
  font-weight: 750;
  color: var(--blue);
}
.setup-rail h1 {
  font-size: 30px;
  line-height: 1.4;
  font-weight: 750;
  margin: 14px 0 12px;
}
.rail-intro {
  color: var(--muted);
  font-size: 13px;
  margin-bottom: 30px;
}
.setup-steps {
  list-style: none;
  padding: 0;
  margin: 0;
}
.setup-steps li {
  min-height: 79px;
  position: relative;
}
.setup-steps li:not(:last-child)::after {
  position: absolute;
  content: "";
  left: 17px;
  top: 40px;
  height: 31px;
  width: 1px;
  background: #d6deec;
}
.setup-steps button {
  display: flex;
  align-items: flex-start;
  gap: 14px;
  padding: 0;
  background: none;
  border: 0;
  text-align: left;
  color: #8796aa;
  opacity: 1 !important;
}
.setup-steps b {
  display: block;
  font-size: 13px;
  font-weight: 650;
}
.setup-steps small {
  display: block;
  font-size: 11px;
  color: #8595aa;
  margin-top: 3px;
}
.step-number {
  display: grid;
  place-items: center;
  border: 1px solid #d5ddeb;
  background: #f5f8fc;
  color: #8292a6;
  border-radius: 50%;
  width: 35px;
  height: 35px;
  flex-shrink: 0;
  font-size: 13px;
}
.current .step-number {
  background: var(--blue);
  color: white;
  border-color: var(--blue);
  box-shadow: 0 0 0 5px #4263ed0d;
}
.current b {
  color: var(--ink);
}
.done .step-number {
  background: #e2f3ef;
  color: #159988;
  border-color: #c6e7df;
}
.rail-note {
  padding: 17px;
  background: #ffffff80;
  border: 1px solid #e1e8f2;
  border-radius: 12px;
  font-size: 12px;
  color: var(--muted);
  margin-top: 17px;
}
.rail-note b {
  display: block;
  color: #536c83;
  margin-bottom: 5px;
}
.setup-card {
  background: #fff;
  border: 1px solid #e5ebf5;
  border-radius: 20px;
  box-shadow: 0 20px 60px #3657800a;
  overflow: hidden;
  min-width: 0;
  min-height: 650px;
}
.card-heading {
  padding: 30px 38px 0;
  display: flex;
  justify-content: space-between;
  gap: 10px;
  color: #8a9aae;
  font-size: 11px;
  letter-spacing: 1px;
}
.saved-note {
  font-size: 10px;
  letter-spacing: 0;
}
.setup-panel {
  padding: 15px 38px 0;
}
.setup-panel h2 {
  font-size: 25px;
  line-height: 1.4;
  font-weight: 750;
  margin: 5px 0 8px;
}
.subtitle,
.initial-panel p {
  color: var(--muted);
  font-size: 13px;
  margin: 0 0 27px;
}
.initial-panel {
  padding-bottom: 40px;
}
.setup-panel > label,
fieldset > label {
  margin-top: 18px;
}
fieldset {
  border: 0;
  padding: 0;
  margin: 0;
  min-width: 0;
}
.field-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 18px 20px;
}
.field-grid > div {
  min-width: 0;
}
.field-grid .full {
  grid-column: 1 / -1;
}
.field-grid.three {
  grid-template-columns: minmax(0, 1.4fr) minmax(0, 1fr) minmax(0, 1fr);
}
.field-grid.address {
  grid-template-columns: minmax(0, 1fr);
}
.field-grid.address.tenant {
  grid-template-columns: minmax(0, 1fr) 150px;
}
.input-group {
  display: flex;
}
.input-group select {
  flex: 0 0 96px;
  width: 96px;
  border-right: 0;
  border-radius: 8px 0 0 8px;
  background: #f6f8fc;
}
.input-group input {
  flex: 1;
  border-radius: 0 8px 8px 0;
}
.spaced {
  margin-top: 18px;
}
label {
  display: block;
  font-size: 12px;
  font-weight: 650;
  color: #4a607a;
  margin-bottom: 7px;
}
input:not([type="checkbox"]),
select {
  width: 100%;
  padding: 10px 13px;
  height: 44px;
  border: 1px solid #dfe5ef;
  border-radius: 8px;
  background: #fff;
  color: #354c68;
  font-size: 13px !important;
  min-width: 0;
}
input::placeholder {
  color: #96a4b7;
}
input[readonly] {
  background: #f6f8fc;
  color: #718198;
}
.field-hint {
  font-size: 11px;
  color: #8291a6;
  margin: 7px 0 0;
}
.optional {
  color: #8291a6;
  font-size: 11px;
  font-weight: 400;
  margin-left: 5px;
}
.advanced {
  margin-top: 22px;
  border-top: 1px solid var(--line);
  padding-top: 15px;
}
.advanced summary {
  color: #72839a;
  font-size: 12px;
  cursor: pointer;
}
.advanced .field-grid {
  margin-top: 16px;
}
.summary-state {
  margin-left: 8px;
  padding: 1px 8px;
  border-radius: 10px;
  font-size: 10px;
  background: #edf0ff;
  color: var(--blue);
}
.summary-state.off {
  background: #f0f3f8;
  color: #7b899c;
}
.url-field {
  display: flex;
  gap: 10px;
  align-items: center;
}
.url-field input {
  flex: 1;
}
.url-field .secondary-button {
  flex-shrink: 0;
}
/* 告警通知的说明表：每类通知由谁发送、什么时候发 */
.notice-table {
  width: 100%;
  margin-top: 16px;
  overflow: hidden;
  font-size: 11px;
  line-height: 1.6;
  color: #5d7390;
  border: 1px solid var(--line);
  border-spacing: 0;
  border-collapse: separate;
  border-radius: 12px;
}
.notice-table th,
.notice-table td {
  padding: 9px 12px;
  text-align: left;
  vertical-align: top;
}
.notice-table thead th {
  font-weight: 650;
  color: #4a607a;
  white-space: nowrap;
  background: #f6f8fc;
}
.notice-table tbody th,
.notice-table tbody td {
  border-top: 1px solid var(--line);
}
.notice-table tbody th {
  font-weight: 650;
  color: var(--ink);
  white-space: nowrap;
}
.notice-table tbody td:nth-child(2) {
  width: 30%;
}
.alert-flow {
  display: flex;
  align-items: center;
  margin-top: 16px;
  padding: 18px 16px;
  border: 1px solid var(--line);
  border-radius: 12px;
  background: linear-gradient(135deg, #f6f8ff 0%, #f3faf7 100%);
}
.alert-flow .arch-link {
  min-width: 24px;
  margin: 0 8px;
}
.flow-node {
  display: grid;
  flex: 0 1 auto;
  gap: 3px;
  justify-items: center;
  min-width: 0;
  padding: 9px 11px;
  text-align: center;
  background: #fff;
  border: 1px solid #c7d2fb;
  border-radius: 10px;
  box-shadow: 0 6px 16px rgb(66 99 237 / 10%);
  transition: opacity 0.2s;
}
.flow-node b {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 11px;
  font-weight: 600;
  color: #3e5574;
  overflow-wrap: anywhere;
}
.flow-node small {
  font-size: 10px;
  color: var(--muted);
}
/* 时序库是第 3 步连接的已有服务，不在本次安装范围 */
.flow-node.external {
  background: #fbfcfe;
  border-style: dashed;
  border-color: #cfd8e6;
  box-shadow: none;
}
.alert-flow.off .flow-node:not(.external),
.alert-flow.off .arch-link {
  opacity: 0.35;
}
.switch-row {
  position: relative;
  display: flex;
  gap: 10px;
  align-items: center;
  margin: 16px 0 0;
  font-weight: 400;
  cursor: pointer;
}
.switch-row > b {
  font-size: 13px;
  font-weight: 700;
  color: var(--ink);
}
/* 原生复选框保留键盘操作和读屏，只把外观换成滑块 */
.switch-row input {
  position: absolute;
  width: 1px;
  height: 1px;
  margin: 0;
  opacity: 0;
}
.switch-track {
  position: relative;
  flex-shrink: 0;
  width: 40px;
  height: 22px;
  background: #cdd5e2;
  border-radius: 11px;
  transition: background 0.2s;
}
.switch-track::after {
  position: absolute;
  top: 3px;
  left: 3px;
  width: 16px;
  height: 16px;
  content: "";
  background: #fff;
  border-radius: 50%;
  box-shadow: 0 1px 3px rgb(25 48 76 / 25%);
  transition: transform 0.2s;
}
.switch-row input:checked + .switch-track {
  background: var(--blue);
}
.switch-row input:checked + .switch-track::after {
  transform: translateX(18px);
}
.switch-row input:focus-visible + .switch-track {
  outline: 3px solid #95b7f3;
  outline-offset: 3px;
}
.switch-row input:disabled + .switch-track {
  opacity: 0.6;
}
.switch-state {
  font-size: 12px;
  color: var(--muted);
}
.setup-callout {
  background: #f1f5ff;
  border: 1px solid #e5ecff;
  border-radius: 10px;
  padding: 13px 16px;
  font-size: 12px;
  color: #61789e;
  margin: 22px 0;
}
.setup-callout p {
  margin: 4px 0 0;
}
.setup-callout.teal {
  background: #f0f9f5;
  border-color: #e0f0e9;
  color: #568271;
}
.label-card {
  padding: 17px 18px;
  border: 1px solid var(--line);
  background: #fbfcfe;
  border-radius: 11px;
  margin-top: 23px;
}
.label-card h3 {
  font-size: 12px;
  margin: 0 0 11px;
}
.label-card input {
  max-width: 320px;
}
.label-card p {
  color: #71839b;
  font-size: 11px;
  margin: 9px 0 0;
}
.important {
  color: #5770da;
  background: #edf0ff;
  padding: 3px 9px;
  font-size: 10px;
  border-radius: 15px;
  margin-left: 8px;
  font-weight: 400;
}
code {
  font-size: 11px;
  word-break: break-word;
  background: #f2f5fa;
  color: #5d7390;
  border-radius: 4px;
  padding: 2px 4px;
}
.arch {
  display: flex;
  align-items: center;
  padding: 22px 20px;
  margin-bottom: 12px;
  border: 1px solid var(--line);
  border-radius: 14px;
  background: linear-gradient(135deg, #f6f8ff 0%, #f3faf7 100%);
}
.arch-user {
  display: grid;
  justify-items: center;
  gap: 5px;
  width: 78px;
  flex-shrink: 0;
  text-align: center;
}
.arch-user b {
  font-size: 13px;
  color: var(--ink);
}
.arch-user small {
  font-size: 10px;
  color: var(--muted);
}
.arch-avatar {
  display: grid;
  place-items: center;
  width: 46px;
  height: 46px;
  border-radius: 50%;
  background: #fff;
  border: 1px solid #dbe3f6;
  box-shadow: 0 6px 16px rgb(66 99 237 / 14%);
}
.arch-avatar svg {
  width: 22px;
  height: 22px;
  fill: none;
  stroke: var(--blue);
  stroke-width: 1.8;
  stroke-linecap: round;
}
.arch-link {
  position: relative;
  flex: 1;
  min-width: 52px;
  height: 2px;
  margin: 0 10px;
  background: repeating-linear-gradient(
    90deg,
    #9fb0f4 0 6px,
    transparent 6px 11px
  );
  background-size: 11px 2px;
  animation: arch-flow 1.1s linear infinite;
}
.arch-link::after {
  content: "";
  position: absolute;
  top: -4px;
  right: -3px;
  border: 5px solid transparent;
  border-right: 0;
  border-left: 7px solid #8197f0;
}
.arch-link.back {
  background-image: repeating-linear-gradient(
    90deg,
    #92cfba 0 6px,
    transparent 6px 11px
  );
  animation-direction: reverse;
}
.arch-link.back::after {
  right: auto;
  left: -3px;
  border-left: 0;
  border-right: 7px solid #4fab8c;
}
.arch-link span {
  position: absolute;
  bottom: 8px;
  left: 50%;
  transform: translateX(-50%);
  font-size: 10px;
  color: var(--muted);
  white-space: nowrap;
}

@keyframes arch-flow {
  to {
    background-position: 11px 0;
  }
}
.arch-group {
  flex-shrink: 0;
  width: 186px;
  padding: 12px;
  border-radius: 12px;
  background: #fff;
  border: 1px solid #dfe6f4;
  box-shadow: 0 8px 22px rgb(25 48 76 / 6%);
}
.arch-master {
  border-color: #c7d2fb;
  box-shadow: 0 10px 26px rgb(66 99 237 / 16%);
}
.arch-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
  margin-bottom: 9px;
}
.arch-title b {
  font-size: 12px;
  color: var(--ink);
  white-space: nowrap;
}
.arch-title em {
  font-style: normal;
  font-size: 10px;
  padding: 2px 7px;
  border-radius: 10px;
  white-space: nowrap;
  background: #edf0ff;
  color: var(--blue);
}
.arch-agents .arch-title em {
  background: #e9f6f0;
  color: #278571;
}
.arch-group ul {
  display: grid;
  gap: 6px;
  margin: 0;
  padding: 0;
  list-style: none;
}
.arch-group li {
  padding: 6px 9px;
  border-radius: 7px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 11px;
  color: #3e5574;
  background: #f4f7ff;
  border: 1px solid #e2e8fb;
}
.arch-agents li {
  color: #2d6a56;
  background: #f1faf6;
  border-color: #d9eee4;
}
.arch-group li.arch-more {
  padding: 1px 0;
  text-align: center;
  letter-spacing: 3px;
  color: #96a4b8;
  background: none;
  border: 1px dashed #d4dceb;
}
.arch-note {
  margin: 0 0 18px;
  font-size: 11px;
  line-height: 1.7;
  color: var(--muted);
}
.arch-note b {
  font-weight: 600;
  color: var(--ink);
  white-space: nowrap;
}
.environment-card {
  border: 1px solid var(--line);
  background: #f9fbff;
  border-radius: 12px;
  padding: 20px;
  display: flex;
  align-items: center;
  gap: 16px;
}
.environment-card p {
  margin: 4px 0 0;
  font-size: 12px;
  color: var(--muted);
  overflow-wrap: anywhere;
}
.cluster-mark {
  font-size: 30px;
  color: var(--blue);
}
.summary-list {
  border: 1px solid var(--line);
  border-radius: 12px;
  padding: 6px 20px;
  margin: 0;
}
.summary-list > div {
  display: flex;
  justify-content: space-between;
  gap: 15px;
  padding: 12px 0;
  font-size: 12px;
}
.summary-list > div + div {
  border-top: 1px solid #f0f3f7;
}
.summary-list dt {
  color: #8291a6;
  flex-shrink: 0;
}
.summary-list dd {
  margin: 0;
  text-align: right;
  overflow-wrap: anywhere;
}
.setup-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 15px;
  border-top: 1px solid var(--line);
  margin: 28px -38px 0;
  padding: 19px 38px;
}
.setup-actions p {
  font-size: 10px;
  color: #8291a6;
  margin: 0;
  max-width: 260px;
}
.setup-actions > div {
  display: flex;
  gap: 12px;
  align-items: center;
}
.action-button,
.secondary-button {
  border-radius: 8px;
  padding: 10px 17px;
  font-size: 12px !important;
  white-space: nowrap;
  border: 1px solid #dfe5ef;
}
.action-button {
  background: var(--blue);
  border-color: var(--blue);
  color: #fff;
  box-shadow: 0 5px 10px #4263ed18;
}
.action-button:hover:not(:disabled) {
  background: #3655dc;
}
.secondary-button {
  background: white;
  color: #6e8097;
}
.setup-panel > .action-button {
  margin: 24px 0 12px;
}
.error-message,
.warning-message {
  padding: 13px 15px;
  border-radius: 9px;
  margin: 18px 0;
  font-size: 12px;
  overflow-wrap: anywhere;
  white-space: pre-wrap;
}
.error-message {
  background: #fff1ef;
  color: #b44533;
}
.warning-message {
  background: #fff9ec;
  color: #927641;
}
.service-progress {
  display: flex;
  align-items: center;
  gap: 12px;
  margin: 20px 0;
}
.service-progress > div {
  flex: 1;
  height: 5px;
  border-radius: 8px;
  overflow: hidden;
  background: #edf1f7;
}
.service-progress > div > span {
  display: block;
  height: 100%;
  background: linear-gradient(90deg, #4263ed, #35b9a6);
  transition: width 0.3s;
}
.service-progress b {
  color: #718198;
  font-size: 11px;
  font-weight: 400;
  white-space: nowrap;
}
.service-list,
.checks-list {
  list-style: none;
  padding: 0;
  margin: 16px 0;
  border: 1px solid var(--line);
  border-radius: 12px;
  overflow: hidden;
}
.service-list li {
  padding: 14px 17px;
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 5px 12px;
}
.service-list li + li,
.checks-list li + li {
  border-top: 1px solid var(--line);
}
.service-list strong {
  font-size: 13px;
  font-weight: 650;
  overflow-wrap: anywhere;
}
/* 服务对应的 Deployment 名，方便在集群里对照（kubectl get deploy） */
.service-resource {
  margin-left: 8px;
  padding: 1px 6px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 11px;
  font-weight: 400;
  color: #3e5574;
  vertical-align: 1px;
  background: #f4f7ff;
  border: 1px solid #e2e8fb;
  border-radius: 5px;
}
.status-badge {
  font-size: 11px;
  padding: 2px 9px;
  align-self: start;
  border-radius: 15px;
  background: #f0f3f8;
  color: #7b899c;
}
.service-list p,
.service-list details {
  grid-column: 1 / -1;
  font-size: 11px;
  color: var(--muted);
  margin: 0;
  overflow-wrap: anywhere;
}
.service-list summary {
  cursor: pointer;
  color: #7a8ba1;
}
.service-list pre {
  font: inherit;
  white-space: pre-wrap;
  background: #f6f8fc;
  padding: 10px;
  border-radius: 8px;
  margin-top: 8px;
}
.success .status-badge {
  background: #e9f6f0;
  color: #278571;
}
.error .status-badge {
  background: #fff1ef;
  color: #bb4534;
}
.active .status-badge {
  background: #edf1ff;
  color: #4263ed;
}
/* 部署中、拉取镜像、启动中、检查中的服务：状态前加一个转圈 */
.service-list li.active .status-badge::before {
  display: inline-block;
  width: 9px;
  height: 9px;
  margin-right: 5px;
  vertical-align: -1px;
  content: "";
  border: 1.5px solid currentColor;
  border-right-color: transparent;
  border-radius: 50%;
  animation: setup-spin 0.8s linear infinite;
}
.error p {
  color: #bb4534;
}
.checks-heading {
  font-size: 12px;
  color: #5a708c;
  margin: 22px 0 0;
}
.checks-list li {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 13px 16px;
  font-size: 12px;
}
.checks-list li > span:nth-child(2) {
  flex: 1;
  min-width: 0;
}
.checks-list small {
  display: block;
  font-size: 10px;
  color: #7e8fa4;
  overflow-wrap: anywhere;
  margin-top: 3px;
}
.checks-list b {
  font-weight: 400;
  font-size: 10px;
  white-space: nowrap;
  color: #7e8fa4;
}
/* 检查结果：检查通过绿色、请留意橙色、失败红色，都加粗 */
.checks-list li.success > b,
.checks-list li.warning > b,
.checks-list li.error > b {
  font-size: 11px;
  font-weight: 700;
}
.checks-list li.success > b {
  color: #18a058;
}
.checks-list li.warning > b {
  color: #e67e22;
}
.checks-list li.error > b {
  color: #d03050;
}
.check-dot {
  display: grid;
  place-items: center;
  width: 20px;
  height: 20px;
  border-radius: 50%;
  background: #f0f3f8;
  color: #91a0b2;
  flex-shrink: 0;
  font-size: 11px;
}
.success .check-dot {
  background: #e9f6f0;
  color: #278571;
}
.error .check-dot {
  background: #fff1ef;
  color: #bb4534;
}
.warning .check-dot {
  background: #fff6e5;
  color: #b7862b;
}
.active .check-dot {
  color: var(--blue);
  background: #edf1ff;
}
/* 进行中的检查项和安装步骤：圆点换成转圈 */
.checks-list li.active > .check-dot {
  color: transparent;
  background: none;
  border: 2px solid #d5defb;
  border-top-color: var(--blue);
  animation: setup-spin 0.8s linear infinite;
}
@keyframes setup-spin {
  to {
    transform: rotate(360deg);
  }
}
.agent-result.warning small {
  color: #927641;
}
.agent-card {
  margin-top: 18px;
  background: #fbfcfe;
  border: 1px solid var(--line);
  border-radius: 12px;
}
.agent-card.checked {
  background: #fff;
  border-color: #c7d2fb;
  box-shadow: 0 8px 22px rgb(66 99 237 / 8%);
}
.agent-choice {
  display: flex;
  gap: 12px;
  align-items: flex-start;
  padding: 16px 18px;
  margin: 0;
  font-weight: 400;
  cursor: pointer;
}
.agent-choice input {
  flex-shrink: 0;
  width: 17px;
  height: 17px;
  margin: 2px 0 0;
  accent-color: var(--blue);
}
.agent-choice b {
  display: block;
  font-size: 13px;
  font-weight: 650;
  color: var(--ink);
}
.agent-choice small {
  display: block;
  margin-top: 3px;
  font-size: 11px;
  color: var(--muted);
}
.agent-options {
  padding: 18px;
  border-top: 1px solid var(--line);
}
.agent-options .field-hint.full {
  margin-top: -8px;
}
.completion {
  margin-top: 26px;
  padding: 28px 0 6px;
  border-top: 1px solid var(--line);
  text-align: center;
}
.success-mark {
  display: grid;
  place-items: center;
  width: 64px;
  height: 64px;
  margin: 0 auto 18px;
  background: #edf8f1;
  border-radius: 20px;
  color: #2b9e7d;
  font-size: 30px;
}
.completion p {
  color: var(--muted);
  font-size: 12px;
  margin: 12px auto 22px;
  max-width: 430px;
}
/* 完成页的登录信息 */
.access-info {
  max-width: 460px;
  margin: 0 auto 22px;
  padding: 6px 18px;
  text-align: left;
  border: 1px solid var(--line);
  border-radius: 12px;
}
.access-info > div {
  display: flex;
  gap: 14px;
  align-items: center;
  justify-content: space-between;
  min-height: 40px;
  font-size: 12px;
}
.access-info > div + div {
  border-top: 1px solid #f0f3f7;
}
.access-info dt {
  flex-shrink: 0;
  color: #8291a6;
}
.access-info dd {
  display: flex;
  gap: 8px;
  align-items: center;
  min-width: 0;
  margin: 0;
  overflow-wrap: anywhere;
}
.access-info a {
  color: var(--blue);
}
.access-password {
  font-size: 12px;
  letter-spacing: 1px;
}
.access-muted {
  color: #8291a6;
}
.eye-button {
  display: grid;
  place-items: center;
  width: 26px;
  height: 26px;
  padding: 0;
  color: #6e8097;
  background: #fff;
  border: 1px solid #dfe5ef;
  border-radius: 6px;
}
.eye-button svg {
  width: 15px;
  height: 15px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.8;
  stroke-linecap: round;
  stroke-linejoin: round;
}
/* 第 6 步顶部：控制端已经装好 */
.control-ready {
  display: flex;
  gap: 12px;
  align-items: center;
  padding: 12px 16px;
  margin: 0 0 18px;
  color: #2d6a56;
  background: #f0f9f5;
  border: 1px solid #d9eee4;
  border-radius: 10px;
}
.control-ready b {
  font-size: 13px;
  color: #1f7a5c;
}
.control-ready p {
  margin: 2px 0 0;
  font-size: 11px;
  color: #568271;
}
.control-ready-mark {
  display: grid;
  flex-shrink: 0;
  place-items: center;
  width: 26px;
  height: 26px;
  font-size: 13px;
  color: #fff;
  background: #2b9e7d;
  border-radius: 50%;
}
.completion-actions {
  display: flex;
  justify-content: center;
  flex-wrap: wrap;
  gap: 12px;
}
.page-footnote {
  max-width: 1240px;
  margin: 22px auto;
  padding: 0 28px;
  text-align: right;
  color: #99a7b9;
  font-size: 11px;
}
@media (max-width: 1000px) {
  .setup-layout {
    grid-template-columns: 220px minmax(0, 1fr);
    gap: 22px;
  }
  .card-heading {
    padding-left: 27px;
    padding-right: 27px;
  }
  .setup-panel {
    padding-left: 27px;
    padding-right: 27px;
  }
  .setup-actions {
    margin-left: -27px;
    margin-right: -27px;
    padding-left: 27px;
    padding-right: 27px;
    flex-wrap: wrap;
  }
  .setup-actions > div {
    margin-left: auto;
  }
}
@media (max-width: 760px) {
  .arch {
    flex-direction: column;
    align-items: stretch;
    padding: 18px;
  }
  .arch-user,
  .arch-group {
    width: auto;
  }
  .arch-link {
    flex: none;
    width: 2px;
    height: 34px;
    min-width: 0;
    margin: 10px auto;
    background: repeating-linear-gradient(
      180deg,
      #9fb0f4 0 6px,
      transparent 6px 11px
    );
    background-size: 2px 11px;
    animation: none;
  }
  .arch-link.back {
    background-image: repeating-linear-gradient(
      180deg,
      #92cfba 0 6px,
      transparent 6px 11px
    );
  }
  .arch-link::after {
    inset: auto auto -3px -4px;
    border: 5px solid transparent;
    border-bottom: 0;
    border-top: 7px solid #8197f0;
  }
  .arch-link.back::after {
    top: -3px;
    bottom: auto;
    left: -4px;
    border: 5px solid transparent;
    border-top: 0;
    border-bottom: 7px solid #4fab8c;
  }
  .arch-link span {
    bottom: 50%;
    left: 14px;
    transform: translateY(50%);
  }
  .setup-topbar {
    padding: 21px 20px;
  }
  .setup-brand {
    font-size: 21px;
    gap: 8px;
  }
  .brand-caption {
    font-size: 10px;
    padding-left: 8px;
  }
  .setup-layout {
    display: block;
    padding: 0 16px;
    margin-top: 0;
  }
  .setup-rail {
    padding: 12px 4px 20px;
  }
  .eyebrow,
  .setup-rail h1,
  .rail-intro,
  .rail-note {
    display: none;
  }
  .setup-steps {
    display: flex;
    gap: 4px;
    justify-content: space-between;
  }
  .setup-steps li {
    flex: 1;
    min-width: 0;
    min-height: 0;
  }
  .setup-steps button {
    flex-direction: column;
    align-items: center;
    width: 100%;
    gap: 8px;
  }
  .setup-steps b {
    font-size: 10px;
    text-align: center;
    white-space: nowrap;
  }
  .setup-steps small {
    display: none;
  }
  .step-number {
    width: 30px;
    height: 30px;
    font-size: 11px;
  }
  .setup-steps li:not(:last-child)::after {
    left: calc(50% + 21px);
    right: calc(-50% + 21px);
    top: 15px;
    width: auto;
    height: 1px;
  }
  .setup-card {
    min-height: 0;
    border-radius: 14px;
  }
  .card-heading {
    padding: 23px 21px 0;
  }
  .setup-panel {
    padding: 12px 21px 0;
  }
  .initial-panel {
    padding-bottom: 40px;
  }
  .setup-panel h2 {
    font-size: 22px;
  }
  .subtitle {
    font-size: 12px;
    margin-bottom: 22px;
  }
  .field-grid,
  .field-grid.three,
  .field-grid.address.tenant {
    grid-template-columns: 1fr;
    gap: 16px;
  }
  .alert-flow {
    flex-direction: column;
    align-items: stretch;
  }
  .alert-flow .arch-link {
    min-width: 0;
    margin: 8px auto;
  }
  .label-card {
    padding: 14px;
  }
  /* 窄屏：机器人地址的两个按钮换到输入框下面，说明表的第一列允许换行 */
  .notify-field {
    flex-wrap: wrap;
  }
  .notify-field input {
    flex-basis: 100%;
  }
  .notice-table th,
  .notice-table td {
    padding: 8px 9px;
  }
  .notice-table tbody th {
    white-space: normal;
  }
  .agent-choice,
  .agent-options {
    padding: 14px;
  }
  .setup-actions {
    margin-left: -21px;
    margin-right: -21px;
    padding: 17px 21px;
  }
  .setup-actions p {
    order: 2;
    flex-basis: 100%;
    max-width: none;
  }
  .setup-actions > div {
    flex-wrap: wrap;
    justify-content: flex-end;
    gap: 8px;
  }
  .action-button,
  .secondary-button {
    padding: 10px 12px;
  }
  .service-list li {
    padding: 12px;
  }
  .checks-list li {
    padding: 12px;
    gap: 7px;
    font-size: 11px;
  }
  .summary-list {
    padding: 4px 15px;
  }
  .summary-list > div {
    font-size: 11px;
    gap: 10px;
  }
  .page-footnote {
    padding: 0 15px;
    text-align: center;
    font-size: 10px;
  }
}

@media (width <= 520px) {
  /* 窄屏放不下 6 个步骤标题：只保留编号，当前步骤见卡片顶部和标题 */
  .setup-steps button > span:last-child {
    display: none;
  }
  .step-number {
    width: 26px;
    height: 26px;
  }
  .setup-steps li:not(:last-child)::after {
    top: 13px;
    right: calc(-50% + 17px);
    left: calc(50% + 17px);
  }
}
@media (prefers-reduced-motion: reduce) {
  * {
    transition: none !important;
  }
  .arch-link,
  .checks-list li.active > .check-dot,
  .service-list li.active .status-badge::before {
    animation: none;
  }
}
</style>
