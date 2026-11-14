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
  GENERAL_SETTINGS_URL,
  INSTALL_STATUS_TEXT,
  MAX_KUBECONFIG_BYTES,
  emptyInstallOptions,
  installError,
  installFingerprint,
  installValidation,
  missingSettings,
  pickInstallOptions,
  recoverableInstall,
  settingsReminder,
  suggestedClusterName
} from "./install-utils";

const visible = defineModel<boolean>({ default: false });
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
const advanced = ref<string[]>([]);
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
let generation = 0;
let pollTimer: ReturnType<typeof setTimeout> | undefined;
const storageKey = () =>
  `kubedoor-agent-install:${useUserStoreHook().username}`;
const locked = computed(() => working.value || loading.value);
const isRunning = computed(() => task.value?.status === "running");
/** 正在使用的通用配置（地址已脱敏） */
const settings = computed(() => defaults.value?.settings);
/** 通用配置里有远程写地址才能完整接入 */
const metricsAvailable = computed(() => !!defaults.value?.metrics_available);
const missing = computed(() => missingSettings(settings.value));
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
  if (!advanced.value.includes(name))
    advanced.value = [...advanced.value, name];
}

function remember(id?: string) {
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
  remember(next.id);
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
  loading.value = true;
  void loadNotifySettings(current);
  try {
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

function newInstall() {
  stopPolling();
  task.value = undefined;
  filename.value = "";
  selectedContext.value = "";
  checkedFingerprint.value = "";
  targetFixed.value = false;
  error.value = "";
  stage.value = 0;
  advanced.value = [];
  Object.assign(options, pickInstallOptions(defaults.value));
  if (defaults.value?.metrics_available === false) options.mode = "agent";
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
    if (!next.metrics_available && options.mode === "full")
      options.mode = "agent";
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
  try {
    const inspected = await agentInstallApi.inspect(await file.text());
    if (generation !== current || !visible.value) return;
    applyTask(inspected);
    filename.value = file.name;
    if (!options.env) options.env = suggestedClusterName(selectedContext.value);
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
  if (!options.env) options.env = suggestedClusterName(selectedContext.value);
  error.value = "";
  stage.value = 1;
}

// 预检报「目标集群已有 Agent 资源」：确认要接管的资源后可以强制覆盖安装
const forceable = computed(
  () =>
    task.value?.status === "draft" &&
    (task.value.checks || []).some(
      check => check.status === "error" && check.forceable
    )
);

async function forcePreflight() {
  if (!task.value || locked.value || !forceable.value) return;
  const items = task.value.takeover || [];
  try {
    await ElMessageBox.confirm(
      h("div", { class: "takeover-confirm" }, [
        h("p", "将接管目标集群里已有的这些 Agent 资源，改成这次安装的配置："),
        h(
          "ul",
          items.map(item =>
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
    if (!options.master_url.trim()) openSection("advanced");
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
    title="安装 Agent"
    width="min(780px, 96vw)"
    append-to-body
    :close-on-click-modal="false"
  >
    <div v-loading="loading" class="agent-install">
      <div v-if="recentTasks.length" class="task-picker">
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
            :label="`${item.options?.env || item.context || '待配置'} · ${INSTALL_STATUS_TEXT[item.status]}`"
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
            {{ task ? "重新上传 kubeconfig" : "上传 kubeconfig" }}
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
          v-if="targetFixed"
          title="已创建安装资源，目标集群、集群标识和命名空间已固定。可以调整连接和采集配置后重新检测。"
          type="info"
          :closable="false"
          class="section-space"
        />
        <el-form :model="options" label-position="top" :disabled="locked">
          <div class="field-grid">
            <el-form-item label="集群唯一标识" required>
              <el-input
                v-model="options.env"
                :disabled="targetFixed"
                placeholder="例如 production-k8s"
                maxlength="64"
              />
              <div class="field-hint">
                用于区分集群及指标，安装后请保持一致。
              </div>
            </el-form-item>
          </div>
          <el-form-item label="安装方式">
            <el-radio-group v-model="options.mode">
              <el-radio value="full" :disabled="!metricsAvailable"
                >完整接入</el-radio
              >
              <el-radio value="agent">仅安装 Agent</el-radio>
            </el-radio-group>
            <div class="field-hint">
              {{
                !metricsAvailable
                  ? "通用配置里还没有远程写地址，vmagent 不知道往哪里写指标，暂时只能仅安装 Agent；填好后点下面的「重新读取」。"
                  : options.mode === "full"
                    ? "安装 Agent 与指标采集，检测并复用兼容的已有采集组件；vmagent 直接写入通用配置里的远程写地址。"
                    : "使用已有监控采集，现有指标仍需具有对应的集群标签。"
              }}
            </div>
          </el-form-item>
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
          <el-collapse v-model="advanced" class="advanced-options">
            <el-collapse-item name="advanced" title="高级设置">
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
                    :disabled="targetFixed"
                  />
                </el-form-item>
                <el-form-item label="Agent 镜像版本">
                  <el-input
                    v-model="options.agent_tag"
                    placeholder="使用推荐版本"
                  />
                </el-form-item>
              </div>
              <div v-if="options.mode === 'full'" class="field-grid">
                <el-form-item label="kube-state-metrics">
                  <el-select v-model="options.ksm">
                    <el-option label="自动检测" value="auto" />
                    <el-option label="安装" value="install" />
                    <el-option label="复用已有" value="reuse" />
                  </el-select>
                </el-form-item>
                <el-form-item label="node-exporter">
                  <el-select v-model="options.node_exporter">
                    <el-option label="自动检测" value="auto" />
                    <el-option label="安装" value="install" />
                    <el-option label="复用已有" value="reuse" />
                  </el-select>
                </el-form-item>
              </div>
            </el-collapse-item>
            <el-collapse-item name="notify" title="消息通知（可选）">
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
            <span
              >这些资源是之前的安装留下的（例如重装控制端前装的
              Agent）。确认可以接管后，可以强制覆盖安装。</span
            >
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
        <p
          v-else-if="
            task?.status === 'expired' ||
            (['failed', 'interrupted'].includes(task?.status || '') &&
              task?.retryable === false)
          "
          class="muted"
        >
          请重新上传 kubeconfig 后继续安装。
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
              >安装 Agent</el-button
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
          <el-button
            v-if="stage === 2 && !isRunning"
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

.advanced-options {
  margin-top: 8px;
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
  .field-grid {
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
