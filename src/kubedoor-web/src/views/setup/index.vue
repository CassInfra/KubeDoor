<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from "vue";
import { useRouter } from "vue-router";
import { ElMessage } from "element-plus";
import {
  getSetupMarker,
  setupApi,
  SetupRequestError,
  type MetricsSettings,
  type PostgresSettings,
  type SetupCheck,
  type SetupMarker,
  type SetupState
} from "@/api/setup";
import {
  checkTone,
  derivedWriteURL,
  hasInstallation,
  isInstalling,
  metricsPayload,
  postgresPayload,
  publicURL,
  serviceStatus,
  stateStep,
  suggestedPublicURL
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

const steps = [
  { title: "环境检查", note: "确认安装位置与环境" },
  { title: "PostgreSQL", note: "连接业务数据库" },
  { title: "时序数据库", note: "连接监控指标存储" },
  { title: "基础设置", note: "管理员与访问地址" },
  { title: "自动安装", note: "逐项检查，确认就绪" }
];
const postgres = reactive<PostgresSettings>({
  host: "",
  port: 5432,
  database: "kubedoor",
  user: "kubedoor",
  password: "",
  sslmode: "prefer",
  has_password: false
});
const metrics = reactive<MetricsSettings>({
  type: "Victoria-Metrics-Single",
  url: "",
  user: "",
  password: "",
  has_password: false,
  write_url: "",
  tag_key: "origin_prometheus"
});
const basics = reactive({
  admin: { username: "kubedoor", password: "" },
  public_url: "",
  components: { grafana: true, vmalert: true, alertmanager: true }
});
const phase = computed(() => completedMarker.value?.phase || task.value?.phase);
const completed = computed(() => phase.value === "succeeded");
const publicFinalizing = computed(
  () => completedMarker.value?.phase === "finalizing"
);
const executing = computed(
  () => hasInstallation(phase.value) && !repairing.value
);
const running = computed(() => isInstalling(phase.value));
const locked = computed(
  () => busy.value || initializing.value || running.value
);
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
const writeURL = computed(() => derivedWriteURL(metrics.type, metrics.url));
const agentWriteURL = computed(() => {
  try {
    return `${publicURL(basics.public_url)}/agent/metrics/write`;
  } catch {
    return "确认访问地址后自动生成";
  }
});
const environment = computed(() => task.value?.environment);
const environmentChecks = computed(
  () =>
    environment.value?.checks ||
    checks.value.filter(
      check => check.id === "environment" || check.name === "environment"
    )
);
const canRetry = computed(
  () =>
    authenticated.value &&
    !repairing.value &&
    !completed.value &&
    !running.value &&
    task.value?.retryable === true
);
const installationTitle = computed(() => {
  if (completed.value) return "全部服务安装与检查通过";
  if (connectionError.value) return "正在重新获取安装状态";
  if (phase.value === "finalizing") return "服务已部署，正在完成最终检查";
  if (["failed", "interrupted"].includes(phase.value)) return "安装尚未完成";
  return "正在安装各项服务";
});
const actionText = computed(() => {
  if (busy.value) return step.value === 5 ? "正在提交…" : "正在检查…";
  if (running.value) return "等待全部服务就绪…";
  if (canRetry.value) return "重试失败服务并复查";
  return ["", "开始配置", "测试并继续", "测试并继续", "检查并继续", "开始安装"][
    step.value
  ];
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
    basics.public_url =
      next.config.public_url || suggestedPublicURL(window.location.origin);
    Object.assign(basics.components, next.config.components);
  }
  if (restoreStep || (hasInstallation(next.phase) && !repairing.value))
    step.value = stateStep(next);
  highestStep.value = Math.max(highestStep.value, stateStep(next));
  notifyCompletion(previous, next.id);
  if (isInstalling(next.phase)) schedulePoll();
  else stopPolling();
}

function applyMarker(marker: SetupMarker | null) {
  if (!active || !["finalizing", "succeeded"].includes(marker?.phase))
    return false;
  const id = marker.installation_id || marker.id;
  if (task.value && id && id !== task.value.id) return false;
  const previous = phase.value;
  completedMarker.value = marker;
  step.value = 5;
  connectionError.value = "";
  error.value = "";
  stopPolling();
  notifyCompletion(previous, id || "completed");
  if (marker.phase === "finalizing") schedulePoll();
  return true;
}

function schedulePoll() {
  stopPolling();
  if (
    active &&
    (authenticated.value || publicFinalizing.value) &&
    !completed.value
  )
    timer = setTimeout(pollState, 2500);
}

async function pollState() {
  if (polling || !active || busy.value) {
    if (active && running.value) schedulePoll();
    return;
  }
  polling = true;
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
    applyState(await setupApi.state());
  } catch (reason) {
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
  stopPolling();
  try {
    applyState(await action(), true);
    changedSteps.delete(currentStep);
    if (nextStep && !executing.value) {
      step.value = nextStep;
      highestStep.value = Math.max(highestStep.value, nextStep);
      if (nextStep === 5) repairing.value = false;
    }
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
    } else if (step.value === 2) {
      const data = postgresPayload(postgres);
      await perform(() => setupApi.postgres(task.value.revision, data), 3);
    } else if (step.value === 3) {
      const data = metricsPayload(metrics);
      await perform(() => setupApi.metrics(task.value.revision, data), 4);
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
        components: { ...basics.components }
      };
      await perform(() => setupApi.configure(task.value.revision, data), 5);
    } else if (!executing.value) {
      await perform(() => setupApi.start(task.value.revision));
    }
  } catch (reason) {
    error.value = errorText(reason);
  }
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
      environment: "集群环境与部署权限"
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
  router.push({
    path: "/login",
    query: agent ? { redirect: "/workbench/index" } : {}
  });
}

onMounted(initialize);
onBeforeUnmount(() => {
  active = false;
  stopPolling();
  key.value = "";
  postgres.password = metrics.password = basics.admin.password = "";
  passwordConfirmation.value = "";
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
          <span>STEP 0{{ step }} / 05</span
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
                    checkTone(check.status) === "success" ? "✓" : "·"
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
                  <div class="full">
                    <label for="pg-host">数据库地址</label
                    ><input
                      id="pg-host"
                      v-model="postgres.host"
                      :readonly="task?.target_locked"
                      autocomplete="off"
                      placeholder="例如 192.0.2.10"
                    />
                    <p class="field-hint">
                      填写 Kubernetes Pod 可以访问的主机名或 IP，不含
                      http://，不能使用 localhost。
                    </p>
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
                  <div class="full">
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
                </div>
                <details class="advanced">
                  <summary>高级连接设置</summary>
                  <div class="field-grid">
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
                      <label for="pg-ssl">SSL 模式</label
                      ><select id="pg-ssl" v-model="postgres.sslmode">
                        <option value="prefer">优先使用 SSL</option>
                        <option value="require">必须使用 SSL</option>
                        <option value="verify-full">校验证书与主机名</option>
                        <option value="disable">禁用 SSL（可信内网）</option>
                      </select>
                    </div>
                  </div>
                </details>
              </fieldset>
              <div class="setup-callout">
                <b>下一步会自动验证</b>
                <p>
                  网络连接、账号认证、数据库访问和建表权限。正式初始化在最后点击“开始安装”时执行。
                </p>
              </div>
            </template>
            <template v-else-if="step === 3">
              <h2>连接时序数据库</h2>
              <p class="subtitle">
                读取集群监控数据，并接收 Agent 上报的指标。
              </p>
              <fieldset :disabled="locked" @input="edited" @change="edited">
                <label for="metrics-type">时序库类型</label
                ><select id="metrics-type" v-model="metrics.type">
                  <option value="Victoria-Metrics-Single">
                    VictoriaMetrics 单机版（推荐）
                  </option>
                  <option value="Victoria-Metrics-Cluster">
                    VictoriaMetrics 集群版
                  </option>
                  <option value="Prometheus">Prometheus</option>
                </select>
                <label for="metrics-url">{{
                  metrics.type === "Victoria-Metrics-Single"
                    ? "服务地址"
                    : "查询入口地址"
                }}</label
                ><input
                  id="metrics-url"
                  v-model="metrics.url"
                  type="url"
                  placeholder="http://192.0.2.11:8428"
                />
                <p
                  v-if="metrics.type === 'Victoria-Metrics-Single'"
                  class="derived-address"
                >
                  自动生成写入地址
                  <code>{{ writeURL || "填写有效服务地址后自动生成" }}</code>
                </p>
                <template v-else
                  ><label for="metrics-write-url"
                    >完整写入地址
                    <span class="optional">已有采集链路可留空</span></label
                  ><input
                    id="metrics-write-url"
                    v-model="metrics.write_url"
                    type="url"
                    placeholder="填写实际写入接口，含租户或接口路径"
                  />
                  <p class="field-hint">
                    查询和写入可能使用不同入口。留空表示复用已有采集，后续仅安装
                    Agent。
                  </p></template
                >
                <div class="field-grid spaced">
                  <div>
                    <label for="metrics-user"
                      >认证用户名 <span class="optional">可选</span></label
                    ><input
                      id="metrics-user"
                      v-model="metrics.user"
                      autocomplete="off"
                    />
                  </div>
                  <div>
                    <label for="metrics-password"
                      >认证密码 <span class="optional">可选</span></label
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
                    />
                  </div>
                </div>
                <div class="label-card">
                  <h3>集群指标标签 <span class="important">重要</span></h3>
                  <label for="cluster-label">用哪个标签区分不同集群？</label
                  ><input
                    id="cluster-label"
                    v-model="metrics.tag_key"
                    placeholder="origin_prometheus"
                  />
                  <p>
                    全新安装保持默认；已有监控填写实际标签，例如
                    <code>cluster</code>。
                  </p>
                  <p>
                    例如
                    <code>origin_prometheus="prod-k8s"</code
                    >：这里填标签名，接入 Agent 时填写集群标识
                    <code>prod-k8s</code>。新 Agent 自动继承该标签名。
                  </p>
                </div>
              </fieldset>
              <div class="setup-callout teal">
                <b>时序库里暂时没有数据，也可以继续</b>
                <p>
                  先验证查询连接，安装时再检查写入链路。接入 Agent
                  后，才会出现集群指标。
                </p>
              </div>
            </template>
            <template v-else-if="step === 4">
              <h2>完成基础设置</h2>
              <p class="subtitle">
                设置管理员密码，以及各集群访问 KubeDoor 的统一入口。
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
                    <label for="public-url">KubeDoor 访问地址</label
                    ><input
                      id="public-url"
                      v-model="basics.public_url"
                      type="url"
                      placeholder="https://kubedoor.example.com"
                    />
                    <p class="field-hint">
                      请确认各目标集群都能访问此地址。不能填写本机地址或临时端口转发地址。
                    </p>
                    <p class="derived-address">
                      Agent 指标入口 <code>{{ agentWriteURL }}</code>
                    </p>
                  </div>
                </div>
                <details class="advanced">
                  <summary>安装组件</summary>
                  <p class="field-hint">
                    Web、控制服务、AI
                    助手和告警服务自动安装。以下组件按需选择，选中后也必须通过检查才会完成安装。
                  </p>
                  <div class="component-options">
                    <label
                      ><input
                        v-model="basics.components.grafana"
                        :disabled="task?.target_locked"
                        type="checkbox"
                      />Grafana 监控看板</label
                    ><label
                      ><input
                        v-model="basics.components.vmalert"
                        :disabled="task?.target_locked"
                        type="checkbox"
                      />vmalert 告警规则计算</label
                    ><label
                      ><input
                        v-model="basics.components.alertmanager"
                        :disabled="task?.target_locked"
                        type="checkbox"
                      />Alertmanager 告警路由</label
                    >
                  </div>
                </details>
              </fieldset>
              <div class="setup-callout">
                AI 模型、通知渠道、镜像更新规则和仓库凭据，可进入控制台后配置。
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
                  <dd>{{ metrics.url }}</dd>
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
                  <dt>可选组件</dt>
                  <dd>
                    {{
                      [
                        basics.components.grafana && "Grafana",
                        basics.components.vmalert && "vmalert",
                        basics.components.alertmanager && "Alertmanager"
                      ]
                        .filter(Boolean)
                        .join("、") || "未选择"
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
                  <strong>{{ service.name }}</strong
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
              <h3 class="checks-heading">外部依赖与最终检查</h3>
              <ul class="checks-list">
                <li
                  v-for="check in checks"
                  :key="check.id || check.name"
                  :class="checkTone(check.status)"
                >
                  <span class="check-dot">{{
                    checkTone(check.status) === "success" ? "✓" : "·"
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
                  所有选定服务与最终检查均已通过。登录后接入第一个集群，即可查看资源和监控。
                </p>
                <div class="completion-actions">
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
                  checkTone(check.status) === "success" ? "✓" : "·"
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
                    : executing
                      ? "服务就绪数量不代表最终检查已通过。"
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
                  v-if="step > 1 && !executing"
                  class="secondary-button"
                  type="button"
                  :disabled="locked"
                  @click="goStep(step - 1)"
                >
                  ← 上一步</button
                ><button
                  v-if="!completed && (!executing || running || canRetry)"
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
.advanced .field-grid,
.component-options {
  margin-top: 16px;
}
.component-options {
  display: flex;
  flex-wrap: wrap;
  gap: 10px 18px;
}
.component-options label {
  display: flex;
  gap: 7px;
  align-items: center;
  font-weight: 400;
}
.component-options input {
  accent-color: var(--blue);
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
.derived-address {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  font-size: 11px;
  color: #8291a6;
  margin: 10px 0 0;
}
.derived-address code {
  flex: 1;
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
  .field-grid {
    grid-template-columns: 1fr;
    gap: 16px;
  }
  .derived-address {
    display: block;
  }
  .derived-address code {
    display: block;
    margin-top: 5px;
  }
  .label-card {
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
@media (prefers-reduced-motion: reduce) {
  * {
    transition: none !important;
  }
}
</style>
