<script setup lang="ts">
import { computed, h, onBeforeUnmount, reactive, ref, watch } from "vue";
import { ElInputTag, ElMessageBox } from "element-plus";
import {
  agentInstallApi,
  type AgentInstallDefaults,
  type AgentInstallOptions,
  type AgentInstallTask,
  type InstallCheck
} from "@/api/agent-install";
import { useUserStoreHook } from "@/store/modules/user";
import {
  INSTALL_STATUS_TEXT,
  MAX_KUBECONFIG_BYTES,
  installError,
  installFingerprint,
  installValidation,
  recoverableInstall,
  suggestedClusterName
} from "./install-utils";

const visible = defineModel<boolean>({ default: false });
const emit = defineEmits<{ changed: [] }>();
const emptyOptions = (): AgentInstallOptions => ({
  env: "",
  aliases: [],
  namespace: "kubedoor",
  master_url: "",
  mode: "full",
  image_repo: "",
  image_pull_secret: "",
  agent_tag: "",
  oss_url: "",
  ksm: "auto",
  node_exporter: "auto",
  remote_write_url: ""
});
const options = reactive<AgentInstallOptions>(emptyOptions());
const defaults = ref<AgentInstallDefaults>();
const task = ref<AgentInstallTask>();
const recentTasks = ref<AgentInstallTask[]>([]);
const selectedContext = ref("");
const stage = ref(0);
const filename = ref("");
const working = ref(false);
const loading = ref(false);
const error = ref("");
const advanced = ref<string[]>([]);
const fileInput = ref<HTMLInputElement>();
const checkedFingerprint = ref("");
const targetFixed = ref(false);
let generation = 0;
let pollTimer: ReturnType<typeof setTimeout> | undefined;
const storageKey = () =>
  `kubedoor-agent-install:${useUserStoreHook().username}`;
const locked = computed(() => working.value || loading.value);
const isRunning = computed(() => task.value?.status === "running");
const canInstall = computed(
  () =>
    task.value?.status === "ready" &&
    checkedFingerprint.value ===
      installFingerprint(selectedContext.value, options)
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
  Object.assign(options, emptyOptions(), next.options);
  options.aliases = [...(next.options?.aliases || [])];
  checkedFingerprint.value =
    next.status === "ready"
      ? installFingerprint(selectedContext.value, options)
      : "";
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
  const { metrics_available: metricsAvailable, ...configured } =
    defaults.value || ({} as AgentInstallDefaults);
  Object.assign(options, emptyOptions(), configured);
  if (metricsAvailable === false) options.mode = "agent";
  remember();
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
    if (!options.master_url.trim()) advanced.value = ["advanced"];
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
      { ...options, aliases: [...(options.aliases || [])] },
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
            <el-form-item label="集群别名（可选）">
              <ElInputTag
                v-model="options.aliases"
                placeholder="输入别名后按回车"
                :max="20"
              />
            </el-form-item>
          </div>
          <el-form-item label="安装方式">
            <el-radio-group v-model="options.mode">
              <el-radio value="full">完整接入</el-radio>
              <el-radio value="agent">仅安装 Agent</el-radio>
            </el-radio-group>
            <div class="field-hint">
              {{
                options.mode === "full"
                  ? "安装 Agent 与指标采集，检测并复用兼容的已有采集组件；vmagent 直接写入时序库。"
                  : "使用已有监控采集，现有指标仍需具有对应的集群标签。"
              }}
            </div>
          </el-form-item>
          <el-alert
            v-if="!defaults?.metrics_available && options.mode === 'full'"
            title="控制端没有配置时序库写入地址，请在高级设置中填写指标写入地址，或选择仅安装 Agent。"
            type="warning"
            :closable="false"
          />
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
              <el-form-item label="镜像仓库">
                <el-input
                  v-model="options.image_repo"
                  placeholder="使用系统配置"
                />
              </el-form-item>
              <el-form-item label="镜像拉取 Secret（可选）">
                <el-input
                  v-model="options.image_pull_secret"
                  placeholder="目标命名空间中已有的 Secret 名称"
                />
                <div class="field-hint">
                  私有镜像仓库可使用已有 Secret，安装前会检查它是否存在。
                </div>
              </el-form-item>
              <el-form-item label="对象存储上传地址（可选）">
                <el-input
                  v-model="options.oss_url"
                  placeholder="用于 Java 诊断文件上传"
                />
              </el-form-item>
              <template v-if="options.mode === 'full'">
                <div class="field-grid">
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
                <el-form-item label="指标写入地址覆盖（可选）">
                  <el-input
                    v-model="options.remote_write_url"
                    placeholder="留空时使用控制端配置的时序库写入地址"
                  />
                  <div class="field-hint">
                    vmagent
                    直接写入这个地址，需要能从目标集群访问；账号口令可写成
                    http://账号:口令@地址，安装时放进目标集群的 Secret。
                  </div>
                </el-form-item>
              </template>
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
