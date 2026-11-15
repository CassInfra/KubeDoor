<script setup lang="ts">
import { computed, h, onBeforeUnmount, reactive, ref, watch } from "vue";
import { ElMessageBox } from "element-plus";
import {
  agentInstallApi,
  type AgentInstallDefaults,
  type AgentInstallNotification,
  type AgentInstallOptions,
  type AgentInstallTask,
  type InstallCheck
} from "@/api/agent-install";
import {
  getNotificationSettings,
  testNotification,
  type NotificationSettings,
  type NotifyChannelInput,
  type NotifyTestRequest
} from "@/api/notification";
import { useUserStoreHook } from "@/store/modules/user";
import NotifyChannelFields from "@/views/system/notification/NotifyChannelFields.vue";
import {
  type ChannelForm,
  type TestView,
  TYPE_LABELS,
  channelInput,
  existingClusterText,
  testErrorView,
  testRequest,
  testResultView,
  tokenPlaceholder,
  typeHint
} from "@/views/system/notification/notification-utils";
import {
  AUTO_DETECT_HINT,
  COLLECTOR_CHOICES,
  GENERAL_SETTINGS_URL,
  INSTALL_STATUS_TEXT,
  MAX_KUBECONFIG_BYTES,
  clusterLabelPair,
  collectorPreset,
  connectionLabel,
  emptyInstallOptions,
  installError,
  installFingerprint,
  installValidation,
  missingSettings,
  pickInstallOptions,
  recoverableInstall,
  repairNotice,
  settingsReminder,
  suggestedClusterName,
  takeoverParts
} from "./install-utils";

const visible = defineModel<boolean>({ default: false });
const props = defineProps<{
  /** 修复这个已接入集群的 Agent（Agent 列表版本号菜单的「修复」）；不传是普通安装 */
  repairEnv?: string;
  /** 这个集群的 AI Kubeconfig（Agent 列表取到的）：修复说明里显示它是自动配置的还是哪个 context */
  repairConnection?: { origin?: string; context?: string } | null;
}>();
const emit = defineEmits<{ changed: [] }>();
const options = reactive<AgentInstallOptions>(emptyInstallOptions());
const defaults = ref<AgentInstallDefaults>();
const task = ref<AgentInstallTask>();
const recentTasks = ref<AgentInstallTask[]>([]);
const selectedContext = ref("");
const stage = ref(0);
const filename = ref("");
const working = ref(false);
const loading = ref(false);
const reloading = ref(false);
const error = ref("");
/** 展开的折叠项：只有「消息通知（可选）」 */
const expanded = ref<string[]>([]);
const fileInput = ref<HTMLInputElement>();
const checkedFingerprint = ref("");
const targetFixed = ref(false);
// 通知配置（可选）：Agent 上线后写进「消息通知 → 按集群单独配置」
const notify = reactive<ChannelForm>({ type: "wecom", token: "", at: "" });
/** 这次安装上次检测时已填了机器人地址（只在服务端加密保存）：留空重新检测就沿用 */
const notifyKept = ref<AgentInstallNotification | null>(null);
const notifyTest = reactive<{ testing: boolean; result: TestView | null }>({
  testing: false,
  result: null
});
/** 消息通知已保存的配置，用来看这个集群是否已有单独配置；读不到时为 null */
const notifySettings = ref<NotificationSettings | null>(null);
/** 修复时没有可用的 AI Kubeconfig 的原因（服务端给的），这时要上传 kubeconfig */
const repairMessage = ref("");
let generation = 0;
let pollTimer: ReturnType<typeof setTimeout> | undefined;
const storageKey = () =>
  `kubedoor-agent-install:${useUserStoreHook().username}`;
const locked = computed(() => working.value || loading.value);
const isRunning = computed(() => task.value?.status === "running");
/** 修复模式：从版本号菜单的「修复」打开，或者在任务列表里选了修复任务 */
const isRepair = computed(() => !!props.repairEnv || !!task.value?.repair);
/** 修复的是哪个集群 */
const repairTarget = computed(
  () => props.repairEnv || task.value?.repair?.env || ""
);
/** 集群唯一标识和命名空间不能改：已开始安装，或者是修复（沿用现有 Agent 的） */
const identityLocked = computed(() => targetFixed.value || isRepair.value);
const dialogTitle = computed(() =>
  isRepair.value ? `修复 Agent：${repairTarget.value}` : "安装 Agent"
);
/** 修复任务用的是保存的 AI Kubeconfig（不是这次上传的） */
const usingSavedConnection = computed(
  () => task.value?.repair?.source === "ai-connection"
);
const savedConnectionLabel = computed(() =>
  connectionLabel(props.repairConnection, task.value?.context)
);
const repairText = computed(() =>
  repairNotice(
    { env: repairTarget.value, source: task.value?.repair?.source },
    savedConnectionLabel.value
  )
);
/** 修复时没有可用的 AI Kubeconfig：服务端给的原因 + 请上传 */
const uploadForRepair = computed(
  () =>
    `${repairMessage.value.replace(/[。.]+$/, "")}。请上传集群 ${repairTarget.value} 的 kubeconfig（要能在集群里创建、修改 Agent 的资源），用它覆盖安装。`
);
/** 失败后不能再重试（临时凭据过期、或用不了）：要重新上传 kubeconfig / 重新开始修复 */
const needsRestart = computed(
  () =>
    task.value?.status === "expired" ||
    (["failed", "interrupted"].includes(task.value?.status || "") &&
      task.value?.retryable === false)
);
/** 正在使用的通用配置（地址已脱敏） */
const settings = computed(() => defaults.value?.settings);
/** 通用配置里有远程写地址才能安装 vmagent */
const metricsAvailable = computed(() => !!defaults.value?.metrics_available);
const missing = computed(() => missingSettings(settings.value));
/** external_labels 里要有的键值对，例如 origin_prometheus: prod-k8s */
const labelPair = computed(() =>
  clusterLabelPair(settings.value?.prom_k8s_tag_key, options.env)
);
const collectorHint = computed(() =>
  options.vmagent === "install"
    ? "vmagent 把指标写入通用配置里的远程写地址，装好后验证能采到指标；kube-state-metrics、node-exporter 选了不安装时，vmagent 抓集群里已有的，检测时确认存在。"
    : "不安装 vmagent 时用已有的 Prometheus / vmagent 采集，检测时会查时序库里有没有这个集群的指标；这里安装的 kube-state-metrics、node-exporter 要由已有的采集去抓取。"
);
/** 有组件选了「自动检测」时，说明怎么判断 */
const autoDetecting = computed(
  () => options.ksm === "auto" || options.node_exporter === "auto"
);
/** 这个集群在消息通知里已有的单独配置 */
const existingChannel = computed(() => {
  const env = options.env.trim();
  return (
    notifySettings.value?.clusters?.find(
      item => item.env === env && item.configured
    ) ?? null
  );
});
const existingText = computed(() => existingClusterText(existingChannel.value));
/** 上次检测填的地址当作「已保存的」：IM 类型没变时机器人地址可以留空 */
const keptChannel = computed(() =>
  notifyKept.value ? { ...notifyKept.value, token_hint: "" } : null
);
const notifyPlaceholder = computed(() => {
  if (!keptChannel.value) return typeHint(notify.type).token;
  return keptChannel.value.type === notify.type
    ? "这次安装已填写，留空沿用"
    : tokenPlaceholder(notify.type, keptChannel.value);
});
const canInstall = computed(
  () =>
    task.value?.status === "ready" && checkedFingerprint.value === fingerprint()
);
const contexts = computed(() => {
  const choices = task.value?.contexts || [];
  return choices.length || !task.value?.context
    ? choices
    : [{ name: task.value.context }];
});
const selectedServer = computed(
  () =>
    contexts.value.find(item => item.name === selectedContext.value)?.server ||
    ""
);
const retryable = computed(() =>
  ["failed", "partial", "interrupted"].includes(task.value?.status || "")
);
const progressTitle = computed(() =>
  task.value ? INSTALL_STATUS_TEXT[task.value.status] : "安装进度"
);
const resultType = computed(() => {
  if (task.value?.status === "succeeded") return "success";
  if (task.value?.status === "partial") return "warning";
  if (["failed", "expired"].includes(task.value?.status || "")) return "error";
  return "info";
});
// AI Kubeconfig 自动配置的结果：已配置 / 保留手动上传的 / 没配上（不影响安装）
const aiConnectionType = computed(
  () =>
    (({ saved: "success", skipped: "info", failed: "warning" }) as const)[
      task.value?.ai_connection?.status || "skipped"
    ]
);

/** 通知配置里要重新检测的内容：什么都没填（也没有上次检测填的）时为 null，只改 IM 类型不算修改 */
function notificationPart() {
  const token = notify.token.trim();
  const at = notify.at.trim();
  if (!token && !at && !notifyKept.value) return null;
  return [notify.type, token, at, notifyKept.value?.type ?? null];
}

/** 已检测的计划对应的指纹：安装参数、通知配置、重新读取到的通用配置变了都要重新检测 */
function fingerprint() {
  return installFingerprint(selectedContext.value, options, [
    notificationPart(),
    settings.value ?? null
  ]);
}

/** 任务里的通知配置填进表单：机器人地址只在服务端，表单留空表示沿用 */
function applyNotification(saved?: AgentInstallNotification | null) {
  notifyKept.value = saved?.configured ? { ...saved } : null;
  Object.assign(notify, {
    type: saved?.type || notify.type,
    token: "",
    at: saved?.at || ""
  });
  notifyTest.result = null;
}

/** 「不配置通知」：清空表单，也不再沿用这次安装上次检测填的地址 */
function clearNotification() {
  applyNotification(null);
}

/** 检测请求里的通知配置：什么都没填为 null；不合格抛出中文说明 */
function notificationInput(): NotifyChannelInput | null {
  if (notificationPart() === null) return null;
  return channelInput("通知配置", notify, keptChannel.value);
}

function openSection(name: string) {
  if (!expanded.value.includes(name))
    expanded.value = [...expanded.value, name];
}

/** 切换安装方式：三个采集组件换成这种方式的默认值（之后每个还能单独改） */
function changeMode(value: unknown) {
  const mode = value === "agent" ? "agent" : "full";
  options.mode = mode;
  Object.assign(options, collectorPreset(mode));
}

function remember(id?: string) {
  // 修复不读也不写：下次普通安装不应恢复修复任务
  if (props.repairEnv) return;
  try {
    if (id) localStorage.setItem(storageKey(), id);
    else localStorage.removeItem(storageKey());
  } catch {
    // 浏览器禁用本地存储时，仍可通过服务端任务列表恢复。
  }
}

function stopPolling() {
  if (pollTimer) clearTimeout(pollTimer);
  pollTimer = undefined;
}

function applyTask(next: AgentInstallTask) {
  const previous = task.value;
  targetFixed.value =
    next.target_locked ??
    ((next.id === previous?.id && targetFixed.value) ||
      ["running", "succeeded", "partial", "failed", "interrupted"].includes(
        next.status
      ));
  task.value = next;
  selectedContext.value = next.context || "";
  Object.assign(options, pickInstallOptions(next.options));
  applyNotification(next.options?.notification);
  if (notifyKept.value) openSection("notify");
  checkedFingerprint.value = next.status === "ready" ? fingerprint() : "";
  stage.value = ["draft", "ready"].includes(next.status) ? 1 : 2;
  repairMessage.value = "";
  // 修复任务不记住：普通安装打开时不恢复它
  if (!next.repair) remember(next.id);
  recentTasks.value = [
    next,
    ...recentTasks.value.filter(item => item.id !== next.id)
  ];
  if (
    ["succeeded", "partial"].includes(next.status) &&
    (previous?.id !== next.id || previous?.status !== next.status)
  )
    emit("changed");
}

function schedulePoll() {
  stopPolling();
  if (!visible.value || !isRunning.value) return;
  const current = generation;
  const id = task.value!.id;
  pollTimer = setTimeout(async () => {
    try {
      const next = await agentInstallApi.get(id);
      if (generation !== current || !visible.value || task.value?.id !== id)
        return;
      error.value = "";
      applyTask(next);
    } catch (reason) {
      if (generation !== current || !visible.value) return;
      error.value = `暂时无法获取安装进度：${installError(reason)}。正在重试。`;
    }
    if (generation === current) schedulePoll();
  }, 2000);
}

async function open() {
  const current = ++generation;
  stopPolling();
  error.value = "";
  repairMessage.value = "";
  loading.value = true;
  void loadNotifySettings(current);
  try {
    if (props.repairEnv) {
      await openRepair(current, props.repairEnv);
      return;
    }
    const [configuration, tasks] = await Promise.all([
      agentInstallApi.defaults(),
      agentInstallApi.list()
    ]);
    if (generation !== current || !visible.value) return;
    defaults.value = configuration;
    recentTasks.value = tasks;
    let preferred: string | null = null;
    try {
      preferred = localStorage.getItem(storageKey());
    } catch {
      // 本地存储可选，服务端记录仍然可用。
    }
    const restored = recoverableInstall(tasks, preferred);
    if (restored) applyTask(restored);
    else newInstall();
    schedulePoll();
  } catch (reason) {
    if (generation === current) error.value = installError(reason);
  } finally {
    if (generation === current) loading.value = false;
  }
}

/**
 * 修复：不列出、不恢复别的安装任务。服务端用这个集群保存的 AI Kubeconfig 建修复任务（参数已预填）
 * 直接到「确认信息」；这个集群正在安装 / 修复时给的是那个任务；没有可用的 AI Kubeconfig 时停在上传这一步
 */
async function openRepair(current: number, env: string) {
  // 取不到时显示「重新加载」
  defaults.value = undefined;
  newInstall();
  recentTasks.value = [];
  const [configuration, started] = await Promise.all([
    agentInstallApi.defaults(),
    agentInstallApi.repair(env)
  ]);
  if (generation !== current || !visible.value) return;
  defaults.value = configuration;
  if (started.task) {
    applyTask(started.task);
    schedulePoll();
  } else {
    newInstall();
    repairMessage.value = started.message || "这个集群没有可用的 AI Kubeconfig";
  }
}

function newInstall() {
  stopPolling();
  task.value = undefined;
  filename.value = "";
  selectedContext.value = "";
  checkedFingerprint.value = "";
  targetFixed.value = false;
  error.value = "";
  repairMessage.value = "";
  stage.value = 0;
  expanded.value = [];
  Object.assign(options, pickInstallOptions(defaults.value));
  if (defaults.value?.metrics_available === false) changeMode("agent");
  applyNotification(null);
  notify.type = notifySettings.value?.default?.type ?? notify.type;
  remember();
}

/** 读消息通知的已保存配置（看这个集群是否已有单独配置）；读不到不影响安装 */
async function loadNotifySettings(current: number) {
  try {
    const res = await getNotificationSettings(true);
    if (generation === current && res?.success) notifySettings.value = res.data;
  } catch {
    // 不显示已有的单独配置即可
  }
}

/** 「重新读取」：在通用配置页填好后，重新取默认值和正在使用的通用配置 */
async function reloadDefaults() {
  if (reloading.value) return;
  reloading.value = true;
  const current = generation;
  try {
    const next = await agentInstallApi.defaults();
    if (generation !== current || !visible.value) return;
    defaults.value = next;
    error.value = "";
    if (!options.master_url && next.master_url)
      options.master_url = next.master_url;
    // 远程写地址又没了：不能再装 vmagent，退回仅安装 kubedoor-agent
    if (
      !next.metrics_available &&
      (options.mode === "full" || options.vmagent === "install")
    )
      changeMode("agent");
  } catch (reason) {
    if (generation === current) error.value = installError(reason);
  } finally {
    reloading.value = false;
  }
}

/** 新标签页打开通用配置，填完回来点「重新读取」 */
function openGeneralSettings() {
  window.open(GENERAL_SETTINGS_URL, "_blank", "noopener");
}

/** 发送测试消息：填了机器人地址用填的；没填时测这个集群已有的单独配置 */
async function testNotify() {
  if (notifyTest.testing) return;
  const env = options.env.trim();
  let request: NotifyTestRequest;
  try {
    if (!/^[A-Za-z0-9_-]{1,64}$/.test(env))
      throw new Error("请先填写集群唯一标识");
    if (!notify.token.trim() && notifyKept.value)
      throw new Error(
        "已填写的机器人地址只保存在这次安装里，发送测试消息请重新填写"
      );
    request = testRequest(env, notify, existingChannel.value);
  } catch (reason) {
    notifyTest.result = { type: "warning", text: (reason as Error).message };
    return;
  }
  notifyTest.testing = true;
  try {
    const res = await testNotification(request);
    notifyTest.result = res.success
      ? testResultView(res.data)
      : { type: "error", text: res.message || "发送测试消息失败" };
  } catch (reason) {
    notifyTest.result = testErrorView(reason);
  } finally {
    notifyTest.testing = false;
  }
}

async function selectTask(id: string) {
  const current = ++generation;
  stopPolling();
  working.value = true;
  error.value = "";
  try {
    const next = await agentInstallApi.get(id);
    if (generation !== current || !visible.value) return;
    applyTask(next);
    schedulePoll();
  } catch (reason) {
    if (generation === current) error.value = installError(reason);
  } finally {
    if (generation === current) working.value = false;
  }
}

async function upload(event: Event) {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  input.value = "";
  if (!file) return;
  if (file.size > MAX_KUBECONFIG_BYTES) {
    error.value = "Kubeconfig 文件不能超过 1 MiB";
    return;
  }
  working.value = true;
  error.value = "";
  const current = generation;
  // 修复时上传的 kubeconfig 建的是这个集群的修复任务（参数由服务端预填）
  const repairEnv = repairTarget.value;
  try {
    const inspected = await agentInstallApi.inspect(
      await file.text(),
      repairEnv || undefined
    );
    if (generation !== current || !visible.value) return;
    applyTask(inspected);
    filename.value = file.name;
    if (!options.env)
      options.env = repairEnv || suggestedClusterName(selectedContext.value);
    stage.value = 0;
  } catch (reason) {
    if (generation === current) error.value = installError(reason);
  } finally {
    if (generation === current) working.value = false;
  }
}

function continueToOptions() {
  if (!selectedContext.value) {
    error.value = "请选择目标集群的 context";
    return;
  }
  if (!options.env)
    options.env =
      repairTarget.value || suggestedClusterName(selectedContext.value);
  error.value = "";
  stage.value = 1;
}

// 预检报「目标集群已有 Agent 资源」：确认要接管的资源后可以强制覆盖安装。
// 修复时目标集群里没有找到这个集群的 Agent 也走这里（takeover 里 kind 为 Agent 的一项），确认 kubeconfig 连对了集群再继续
const forceable = computed(
  () =>
    task.value?.status === "draft" &&
    (task.value.checks || []).some(
      check => check.status === "error" && check.forceable
    )
);
const takeover = computed(() => takeoverParts(task.value?.takeover));
const takeoverText = computed(() =>
  takeover.value.missing.length && !takeover.value.resources.length
    ? `目标集群里没有找到集群 ${repairTarget.value || options.env} 的 Agent。确认 kubeconfig 连接的就是这个集群后，可以继续覆盖安装。`
    : "这些资源是之前的安装留下的（例如重装控制端前装的 Agent）。确认可以接管后，可以强制覆盖安装。"
);

async function forcePreflight() {
  if (!task.value || locked.value || !forceable.value) return;
  const { missing, resources } = takeover.value;
  const env = repairTarget.value || options.env;
  try {
    await ElMessageBox.confirm(
      h("div", { class: "takeover-confirm" }, [
        // 没找到 Agent 的那一项不是要接管的资源：只显示服务端的说明（里面已经请用户确认集群）
        ...missing.map(item => h("p", item.reason)),
        ...(missing.length
          ? [
              h(
                "p",
                `确认后会在这个集群里重新安装 Agent，以 ${env} 的名义接入控制端。`
              )
            ]
          : []),
        ...(resources.length
          ? [
              h(
                "p",
                "将接管目标集群里已有的这些 Agent 资源，改成这次安装的配置："
              ),
              h(
                "ul",
                resources.map(item =>
                  h(
                    "li",
                    `${item.kind}/${item.name}${item.namespace ? `（${item.namespace}）` : ""}：${item.reason}`
                  )
                )
              ),
              h(
                "p",
                "原来的 Agent 会改连当前控制端；如果另一套 KubeDoor 还在使用它，那套会失去这个集群。"
              )
            ]
          : [])
      ]),
      "强制覆盖安装",
      {
        type: "warning",
        confirmButtonText: "强制覆盖并重新检测",
        cancelButtonText: "取消"
      }
    );
  } catch {
    return;
  }
  await preflight(true);
}

async function preflight(force = false) {
  if (!task.value || locked.value) return;
  const invalid = installValidation(options);
  if (invalid) {
    error.value = invalid;
    return;
  }
  let notification: NotifyChannelInput | null;
  try {
    notification = notificationInput();
  } catch (reason) {
    error.value = (reason as Error).message;
    openSection("notify");
    return;
  }
  working.value = true;
  error.value = "";
  checkedFingerprint.value = "";
  const current = generation;
  try {
    const next = await agentInstallApi.preflight(
      task.value.id,
      selectedContext.value,
      { ...options, notification },
      // 只认确认框传来的 true：按钮的点击事件等其他值都不算强制覆盖
      force === true
    );
    if (generation !== current || !visible.value) return;
    applyTask(next);
  } catch (reason) {
    if (generation === current) error.value = installError(reason);
  } finally {
    if (generation === current) working.value = false;
  }
}

async function start(retry = false) {
  if (!task.value || locked.value || (!retry && !canInstall.value)) return;
  if (retry && task.value.retryable === false) return;
  working.value = true;
  error.value = "";
  const current = generation;
  try {
    const next = retry
      ? await agentInstallApi.retry(task.value.id)
      : await agentInstallApi.start(task.value.id, task.value.revision);
    if (generation !== current || !visible.value) return;
    applyTask(next);
    schedulePoll();
  } catch (reason) {
    if (generation === current) error.value = installError(reason);
  } finally {
    if (generation === current) working.value = false;
  }
}

function adjustOptions() {
  stage.value = 1;
  error.value = "";
  checkedFingerprint.value = "";
}

function checkType(check: InstallCheck) {
  if (check.status === "success") return "success";
  if (check.status === "error") return "danger";
  if (check.status === "warning") return "warning";
  return "info";
}

watch(
  visible,
  value => {
    if (value) void open();
    else {
      generation++;
      stopPolling();
      working.value = false;
      loading.value = false;
    }
  },
  { immediate: true }
);
onBeforeUnmount(() => {
  generation++;
  stopPolling();
});
</script>

<template>
  <el-dialog
    v-model="visible"
    :title="dialogTitle"
    width="min(780px, 96vw)"
    append-to-body
    :close-on-click-modal="false"
  >
    <div v-loading="loading" class="agent-install">
      <!-- 修复时只处理这一个集群，不列出别的安装任务 -->
      <div v-if="recentTasks.length && !repairEnv" class="task-picker">
        <el-select
          :model-value="task?.id"
          placeholder="查看最近安装任务"
          :disabled="locked"
          @change="selectTask"
        >
          <el-option
            v-for="item in recentTasks"
            :key="item.id"
            :value="item.id"
            :label="`${item.repair ? '修复 ' : ''}${item.options?.env || item.context || '待配置'} · ${INSTALL_STATUS_TEXT[item.status]}`"
          />
        </el-select>
        <el-button :disabled="locked" @click="newInstall">安装新集群</el-button>
      </div>

      <el-steps :active="stage" finish-status="success" align-center>
        <el-step title="连接集群" />
        <el-step title="确认信息" />
        <el-step title="安装并上线" />
      </el-steps>

      <el-alert
        v-if="error"
        :title="error"
        type="error"
        show-icon
        :closable="false"
        class="section-space"
      />

      <template v-if="stage === 0">
        <el-alert
          v-if="repairMessage"
          :title="uploadForRepair"
          type="warning"
          show-icon
          :closable="false"
          class="section-space"
        />
        <el-alert
          v-else-if="usingSavedConnection"
          :title="`当前用保存的 AI Kubeconfig（${savedConnectionLabel}）连接集群 ${repairTarget}，也可以上传一份替换。`"
          type="info"
          show-icon
          :closable="false"
          class="section-space"
        />
        <div class="upload-area">
          <input
            ref="fileInput"
            type="file"
            hidden
            :disabled="locked || targetFixed"
            @change="upload"
          />
          <el-button
            type="primary"
            :loading="working"
            :disabled="loading || targetFixed"
            @click="fileInput?.click()"
          >
            {{
              usingSavedConnection
                ? "上传 kubeconfig 替换"
                : task
                  ? "重新上传 kubeconfig"
                  : "上传 kubeconfig"
            }}
          </el-button>
          <span class="muted">{{ filename || "YAML / JSON，最大 1 MiB" }}</span>
        </div>
        <p class="muted">
          kubeconfig 仅用于本次安装，完成后清理。请使用带内嵌证书或 Token
          的配置。
        </p>
        <el-form v-if="task" label-position="top">
          <el-form-item label="目标 context">
            <el-select
              v-model="selectedContext"
              :disabled="locked || targetFixed"
              filterable
            >
              <el-option
                v-for="item in contexts"
                :key="item.name"
                :value="item.name"
                :label="item.name"
              />
            </el-select>
          </el-form-item>
          <p v-if="selectedServer" class="server-address">
            {{ selectedServer }}
          </p>
        </el-form>
        <p class="muted">
          安装时 KubeDoor 服务端需要能访问目标 API Server；目标集群需要能访问
          KubeDoor。
        </p>
      </template>

      <template v-else-if="stage === 1">
        <div class="target-summary">
          <strong>{{ selectedContext }}</strong>
          <span v-if="selectedServer">{{ selectedServer }}</span>
        </div>
        <el-alert
          v-if="isRepair"
          :title="repairText"
          type="warning"
          :closable="false"
          class="repair-notice"
        />
        <el-alert
          v-if="targetFixed"
          title="已创建安装资源，目标集群、集群标识和命名空间已固定。可以调整连接和采集配置后重新检测。"
          type="info"
          :closable="false"
          class="section-space"
        />
        <el-form :model="options" label-position="top" :disabled="locked">
          <el-form-item label="集群唯一标识" required>
            <el-input
              v-model="options.env"
              class="env-input"
              :disabled="identityLocked"
              placeholder="例如 production-k8s"
              maxlength="64"
            />
            <div class="field-hint">
              用来区分各集群的指标，安装后请保持不变。会作为 vmagent 配置里
              external_labels 的值：<code class="label-pair">{{
                labelPair
              }}</code
              >（key 是通用配置的集群指标标签 key）；使用已有的 Prometheus /
              vmagent 时，它们配置的 external_labels 下也要有这个键值对。
            </div>
          </el-form-item>
          <el-form-item label="安装方式">
            <el-radio-group v-model="options.mode" @change="changeMode">
              <el-radio value="full" :disabled="!metricsAvailable"
                >完整安装</el-radio
              >
              <el-radio value="agent">仅安装 kubedoor-agent</el-radio>
            </el-radio-group>
            <div v-if="!metricsAvailable" class="field-hint">
              通用配置里还没有远程写地址，vmagent
              不知道往哪里写指标，暂时不能安装
              vmagent；填好后点下面的「重新读取」。
            </div>
          </el-form-item>
          <div class="collector-grid">
            <el-form-item label="kube-state-metrics">
              <el-select v-model="options.ksm">
                <el-option
                  v-for="item in COLLECTOR_CHOICES.component"
                  :key="item.value"
                  :label="item.label"
                  :value="item.value"
                />
              </el-select>
            </el-form-item>
            <el-form-item label="node-exporter">
              <el-select v-model="options.node_exporter">
                <el-option
                  v-for="item in COLLECTOR_CHOICES.component"
                  :key="item.value"
                  :label="item.label"
                  :value="item.value"
                />
              </el-select>
            </el-form-item>
            <el-form-item label="vmagent">
              <el-select v-model="options.vmagent">
                <el-option
                  v-for="item in COLLECTOR_CHOICES.vmagent"
                  :key="item.value"
                  :label="item.label"
                  :value="item.value"
                  :disabled="item.value === 'install' && !metricsAvailable"
                />
              </el-select>
            </el-form-item>
          </div>
          <div class="field-hint collector-hint">
            <div v-if="autoDetecting">{{ AUTO_DETECT_HINT }}</div>
            <div>{{ collectorHint }}</div>
          </div>
          <div class="settings-panel">
            <div class="settings-head">
              <span class="settings-title">正在使用的通用配置</span>
              <span>
                <el-button link type="primary" @click="openGeneralSettings"
                  >去通用配置填写</el-button
                >
                <el-button
                  link
                  type="primary"
                  :loading="reloading"
                  @click="reloadDefaults"
                  >重新读取</el-button
                >
              </span>
            </div>
            <div class="settings-row">
              <span class="settings-label">远程写地址</span>
              <span>{{ settings?.metrics_write_url || "未配置" }}</span>
            </div>
            <div class="settings-row">
              <span class="settings-label">对象存储上传地址</span>
              <span>{{ settings?.oss_url || "未配置" }}</span>
            </div>
            <div class="settings-row">
              <span class="settings-label">镜像仓库</span>
              <span>{{ settings?.image_repo || "未配置" }}</span>
            </div>
            <el-alert
              v-for="item in missing"
              :key="item.key"
              :title="settingsReminder(item.label)"
              :type="item.required ? 'warning' : 'info'"
              :closable="false"
              class="settings-alert"
            />
            <div class="field-hint">
              远程写地址给 vmagent 写指标用；对象存储上传地址给 Java
              诊断文件（dump、jstack、JFR）上传用，不保存在 Agent
              里，控制端每次请求时带过去，改了马上生效。
            </div>
          </div>
          <el-form-item label="目标集群可访问的 KubeDoor 地址" required>
            <el-input
              v-model="options.master_url"
              placeholder="https://kubedoor.example.com"
            />
            <div class="field-hint">
              Agent 通过这个地址连接控制端。请勿填写本机端口转发地址。
            </div>
          </el-form-item>
          <div class="field-grid">
            <el-form-item label="命名空间">
              <el-input
                v-model="options.namespace"
                :disabled="identityLocked"
              />
            </el-form-item>
            <el-form-item label="Agent 镜像版本">
              <el-input
                v-model="options.agent_tag"
                placeholder="使用推荐版本"
              />
            </el-form-item>
          </div>
          <el-collapse v-model="expanded" class="notify-section">
            <el-collapse-item name="notify">
              <template #title>
                <span class="notify-title">消息通知（可选）</span>
              </template>
              <el-alert
                v-if="existingText"
                :title="existingText"
                type="info"
                :closable="false"
                class="notify-alert"
              />
              <p v-else class="field-hint notify-intro">
                填写后，Agent 上线时在「系统管理 → 消息通知 →
                按集群单独配置」为这个集群加上配置；留空则按消息通知现有的配置发送。
              </p>
              <div v-if="notifyKept" class="notify-kept">
                <span
                  >这次安装已填写{{
                    TYPE_LABELS[notifyKept.type] || notifyKept.type
                  }}机器人地址，留空沿用。</span
                >
                <el-button link type="danger" @click="clearNotification"
                  >不配置通知</el-button
                >
              </div>
              <NotifyChannelFields
                v-model:form="notify"
                :placeholder="notifyPlaceholder"
                :testing="notifyTest.testing"
                :result="notifyTest.result"
                @test="testNotify"
              />
            </el-collapse-item>
          </el-collapse>
        </el-form>

        <template v-if="task?.checks?.length">
          <h4 class="section-title">检测结果</h4>
          <div
            v-for="(check, index) in task.checks"
            :key="index"
            class="check-row"
          >
            <el-tag :type="checkType(check)" size="small">{{
              check.name
            }}</el-tag>
            <span>{{ check.message }}</span>
          </div>
          <div v-if="forceable" class="takeover-row">
            <span>{{ takeoverText }}</span>
            <el-button
              type="danger"
              plain
              size="small"
              :disabled="locked"
              @click="forcePreflight"
              >强制覆盖安装…</el-button
            >
          </div>
          <el-alert
            v-if="task.status === 'ready' && !canInstall"
            title="安装参数已修改，请重新检测。"
            type="info"
            :closable="false"
            class="section-space"
          />
        </template>
        <template v-if="canInstall">
          <h4 class="section-title">将安装的资源</h4>
          <p v-if="task?.summary" class="muted">{{ task.summary }}</p>
          <div class="resource-list">
            <el-tag
              v-for="item in task?.resources"
              :key="`${item.kind}/${item.namespace}/${item.name}`"
              type="info"
              effect="plain"
            >
              {{ item.kind }} / {{ item.name }}
            </el-tag>
          </div>
          <p class="muted">
            Agent 将获得目标集群管理权限；安装 node-exporter
            时需要主机网络与特权容器权限。
          </p>
        </template>
      </template>

      <template v-else>
        <el-alert
          :title="progressTitle"
          :type="resultType"
          :closable="false"
          show-icon
          class="section-space"
        />
        <p class="target-summary">
          {{ task?.options?.env }} · {{ task?.context }}
        </p>
        <div
          v-for="(step, index) in task?.steps"
          :key="index"
          class="check-row"
        >
          <el-tag :type="checkType(step)" size="small">{{ step.name }}</el-tag>
          <span>{{
            step.message ||
            (step.status === "running"
              ? "正在进行…"
              : step.status === "pending"
                ? "等待中"
                : step.status === "skipped"
                  ? "已跳过"
                  : "")
          }}</span>
        </div>
        <div v-if="task?.ai_connection" class="check-row">
          <el-tag :type="aiConnectionType" size="small">AI Kubeconfig</el-tag>
          <span>{{ task.ai_connection.message }}</span>
        </div>
        <div v-if="task?.notification_result" class="check-row">
          <el-tag
            :type="
              task.notification_result.status === 'saved'
                ? 'success'
                : 'warning'
            "
            size="small"
            >消息通知</el-tag
          >
          <span>{{ task.notification_result.message }}</span>
        </div>
        <el-alert
          v-if="task?.error"
          :title="task.error"
          type="error"
          :closable="false"
          class="section-space"
        />
        <p v-if="isRunning" class="muted">
          可以关闭此窗口，后台安装会继续。再次打开可查看进度。
        </p>
        <p v-else-if="needsRestart" class="muted">
          {{
            repairEnv
              ? "请点「重新开始修复」，用保存的 AI Kubeconfig 或重新上传的 kubeconfig 继续。"
              : "请重新上传 kubeconfig 后继续安装。"
          }}
        </p>
      </template>
    </div>
    <template #footer>
      <div class="install-actions">
        <el-button @click="visible = false">{{
          isRunning ? "后台运行" : "关闭"
        }}</el-button>
        <div>
          <el-button v-if="stage === 1" :disabled="locked" @click="stage = 0"
            >上一步</el-button
          >
          <el-button
            v-if="stage === 0 && task"
            type="primary"
            :disabled="locked || !selectedContext"
            @click="continueToOptions"
            >下一步</el-button
          >
          <template v-if="stage === 1">
            <el-button
              :loading="working"
              :disabled="loading"
              @click="preflight()"
              >检测连接与安装权限</el-button
            >
            <el-button
              type="primary"
              :disabled="locked || !canInstall"
              @click="start(false)"
              >{{ isRepair ? "覆盖安装 Agent" : "安装 Agent" }}</el-button
            >
          </template>
          <el-button
            v-if="stage === 2 && retryable"
            type="primary"
            :loading="working"
            :disabled="loading || task?.retryable === false"
            @click="start(true)"
            >{{
              task?.status === "partial" ? "重新验证指标采集" : "核对状态并重试"
            }}</el-button
          >
          <el-button
            v-if="stage === 2 && retryable && task?.retryable !== false"
            :disabled="locked"
            @click="adjustOptions"
            >调整配置</el-button
          >
          <!-- 修复只针对这一个集群：凭据用不了时重新开始修复，不提供「安装新集群」 -->
          <el-button
            v-if="stage === 2 && repairEnv && needsRestart"
            type="primary"
            :disabled="locked"
            @click="open"
            >重新开始修复</el-button
          >
          <el-button
            v-if="stage === 2 && !isRunning && !repairEnv"
            :disabled="locked"
            @click="newInstall"
            >安装新集群</el-button
          >
          <el-button v-if="!defaults && !loading" type="primary" @click="open"
            >重新加载</el-button
          >
        </div>
      </div>
    </template>
  </el-dialog>
</template>

<style scoped>
.agent-install {
  min-height: 220px;
}

.task-picker,
.upload-area,
.install-actions {
  display: flex;
  gap: 12px;
  align-items: center;
}

.task-picker {
  margin-bottom: 24px;
}

.task-picker .el-select {
  flex: 1;
}

.upload-area {
  flex-wrap: wrap;
  margin-top: 28px;
}

.target-summary {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 16px;
  margin: 24px 0 18px;
  overflow-wrap: anywhere;
}

.field-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0 20px;
}

.field-hint {
  width: 100%;
  margin-top: 4px;
  font-size: 12px;
  line-height: 1.7;
  color: var(--el-text-color-secondary);
}

.muted {
  margin: 12px 0;
  line-height: 1.8;
  color: var(--el-text-color-secondary);
}

.server-address {
  overflow-wrap: anywhere;
}

.env-input {
  width: min(100%, 360px);
}

/* external_labels 里要有的「集群指标标签 key: 集群唯一标识」 */
.label-pair {
  padding: 1px 6px;
  font-family: var(--el-font-family-mono, Menlo, Consolas, monospace);
  color: var(--el-color-primary);
  white-space: nowrap;
  background: var(--el-fill-color-light);
  border-radius: 4px;
}

/* 三个采集组件一行 */
.collector-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 0 16px;
}

.collector-grid .el-select {
  width: 100%;
}

.collector-hint {
  margin: -10px 0 14px;
}

.notify-section {
  margin-top: 4px;
}

/* 消息通知（可选）：比折叠栏默认的 13px 大一号并加粗，像一节的标题 */
.notify-title {
  font-size: 15px;
  font-weight: 600;
  color: var(--el-text-color-primary);
}

/* 正在使用的通用配置：远程写地址、对象存储上传地址（脱敏）、镜像仓库 */
.settings-panel {
  padding: 10px 12px;
  margin-bottom: 8px;
  font-size: 13px;
  line-height: 1.7;
  background: var(--el-fill-color-light);
  border-radius: 6px;
}

.settings-head {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  justify-content: space-between;
}

.settings-title {
  font-weight: 600;
}

.settings-row {
  display: flex;
  gap: 12px;
  overflow-wrap: anywhere;
}

.settings-label {
  flex-shrink: 0;
  width: 112px;
  color: var(--el-text-color-secondary);
}

.settings-alert {
  margin-top: 8px;
}

.notify-alert,
.notify-intro {
  margin: 0 0 12px;
}

.notify-kept {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  margin-bottom: 12px;
  font-size: 13px;
  color: var(--el-text-color-regular);
}

.section-space {
  margin-top: 20px;
}

/* 修复：用哪份 kubeconfig、覆盖安装哪个集群（紧跟在目标集群下面） */
.repair-notice {
  margin-bottom: 18px;
}

.section-title {
  margin: 20px 0 12px;
}

.check-row {
  display: flex;
  gap: 12px;
  align-items: flex-start;
  padding: 7px 0;
  line-height: 1.6;
  overflow-wrap: anywhere;
}

.check-row .el-tag {
  flex-shrink: 0;
}

/* 预检报目标集群已有 Agent 资源：强制覆盖安装的入口 */
.takeover-row {
  display: flex;
  gap: 12px;
  align-items: center;
  justify-content: space-between;
  padding: 10px 12px;
  margin-top: 8px;
  font-size: 12px;
  line-height: 1.6;
  color: #8a3b2c;
  background: #fff8f6;
  border: 1px dashed #efc2b8;
  border-radius: 6px;
}

.takeover-row .el-button {
  flex-shrink: 0;
}

.resource-list {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.install-actions {
  flex-wrap: wrap;
  justify-content: space-between;
}

@media (width <= 640px) {
  .field-grid,
  .collector-grid {
    grid-template-columns: minmax(0, 1fr);
  }

  .install-actions > div {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }

  .install-actions .el-button + .el-button {
    margin-left: 0;
  }
}
</style>
