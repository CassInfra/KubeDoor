<script setup lang="ts">
import {
  computed,
  onBeforeUnmount,
  onMounted,
  reactive,
  ref,
  watch
} from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import dayjs from "dayjs";
import {
  aiApi,
  type AIReasoningEffort,
  type IMBot,
  type IMPlatform,
  type IMUser,
  type SharedModelConfig
} from "@/api/ai";
import { getKnownClusters, getUsers, type SystemUser } from "@/api/system";
import AIModelLimitsFields from "@/components/AI/AIModelLimitsFields.vue";
import AIReasoningEffortField from "@/components/AI/AIReasoningEffortField.vue";
import {
  followModelLimits,
  modelLimitsFor,
  type AIModelLimits
} from "@/utils/ai";
import {
  PERM_LABELS,
  PERM_TAG,
  ROLE_LABELS,
  permissionOf
} from "@/utils/permission";
import {
  FEISHU_DOMAIN_OPTIONS,
  GROUP_POLICY_OPTIONS,
  NO_CLUSTER,
  NO_CLUSTER_LABEL,
  PLATFORM_OPTIONS,
  botFormFromSaved,
  botStatusInfo,
  buildBotPayload,
  buildSharedModelPayload,
  defaultBotForm,
  defaultSharedModelForm,
  feishuDomainLabel,
  groupPolicyLabel,
  platformLabel,
  validateBotForm,
  validateSharedModelForm,
  type BotForm,
  type SharedModelForm
} from "./im-bots-utils";

type TabName = "shared-model" | "bots" | "users";

const activeTab = ref<TabName>("shared-model");
const loading = ref(false);
const sharedSaving = ref(false);
const sharedTesting = ref(false);
const botsLoading = ref(false);
const botSaving = ref(false);
const togglingBotId = ref("");
const unboundSaving = ref(false);
const usersLoading = ref(false);
const mappingSaving = ref(false);

const sharedConfig = ref<SharedModelConfig>({
  available: false,
  configured: false,
  base_url: "",
  model: "",
  max_input_tokens: null,
  max_output_tokens: null,
  compression_ratio: null,
  reasoning_effort: null,
  api_key_set: false
});
const sharedForm = reactive<SharedModelForm>(defaultSharedModelForm());
const sharedLimits = ref<AIModelLimits>(modelLimitsFor(""));
const sharedEffort = ref<AIReasoningEffort | null>(null);

// 模型名变化时，没手动改过的上下文设置按新模型名重新预填。
// 同步触发：loadSharedModel 先设模型名、再载入已保存的值，保存的值不会被改写。
watch(
  () => sharedForm.model,
  (model, previous) => {
    sharedLimits.value = followModelLimits(sharedLimits.value, previous, model);
  },
  { flush: "sync" }
);

const bots = ref<IMBot[]>([]);
const botDrawerVisible = ref(false);
const botForm = reactive<BotForm>(defaultBotForm("wecom"));
const overrideLimits = ref<AIModelLimits>(modelLimitsFor(""));
const overrideEffort = ref<AIReasoningEffort | null>(null);

watch(
  () => botForm.model_override.model,
  (model, previous) => {
    overrideLimits.value = followModelLimits(
      overrideLimits.value,
      previous,
      model
    );
  },
  { flush: "sync" }
);

const users = ref<IMUser[]>([]);
const systemUsers = ref<SystemUser[]>([]);
const clusters = ref<string[]>([]);
const selectedBotFilter = ref("");
const userKeyword = ref("");

const envOrNull = (value: string) =>
  value && value !== NO_CLUSTER ? value : null;

const mapping = reactive({
  bot_id: "",
  external_user_id: "",
  username: "",
  default_env: NO_CLUSTER
});

const botsById = computed(() => {
  const map = new Map<string, IMBot>();
  for (const bot of bots.value) map.set(bot.id, bot);
  return map;
});

const unboundCandidates = computed(() =>
  systemUsers.value.filter(
    item =>
      item.enabled &&
      item.role !== "agent" &&
      item.role !== "admin" &&
      item.role !== "cluster_admin"
  )
);

const botUnboundAccess = computed(() => {
  const user = systemUsers.value.find(
    item => item.username === botForm.unbound_username
  );
  if (!user) return [];
  return clusters.value
    .map(env => ({
      env,
      permission: permissionOf(user, env)
    }))
    .filter(item => item.permission !== "none");
});

const botUnboundUnavailable = computed(() => {
  if (!botForm.unbound_username || usersLoading.value) return false;
  return !unboundCandidates.value.some(
    item => item.username === botForm.unbound_username
  );
});

const mappingMember = computed(() =>
  users.value.find(
    item =>
      item.bot_id === mapping.bot_id &&
      item.external_user_id === mapping.external_user_id.trim()
  )
);

const availableMappingClusters = computed(() => {
  const user = systemUsers.value.find(
    item => item.username === mapping.username
  );
  if (!user) return [];
  return clusters.value.filter(env => permissionOf(user, env) !== "none");
});

const filteredUsers = computed(() => {
  const kw = userKeyword.value.trim().toLowerCase();
  return users.value.filter(row => {
    if (selectedBotFilter.value && row.bot_id !== selectedBotFilter.value) {
      return false;
    }
    if (!kw) return true;
    return [
      row.remark,
      row.self_name,
      row.external_user_id,
      row.username,
      botNameOf(row)
    ]
      .filter(Boolean)
      .some(text => String(text).toLowerCase().includes(kw));
  });
});

function onBotUnboundUserChange() {
  if (
    !botUnboundAccess.value.some(
      item => item.env === botForm.unbound_default_env
    )
  ) {
    botForm.unbound_default_env = NO_CLUSTER;
  }
}

function botNameOf(row: Pick<IMUser, "bot_id" | "bot_name">): string {
  return (
    row.bot_name ||
    botsById.value.get(row.bot_id)?.name ||
    row.bot_id.slice(0, 8)
  );
}

function defaultEnvOf(row: IMUser): string | null {
  if (row.username) return row.default_env;
  const bot = botsById.value.get(row.bot_id);
  return bot?.unbound_username ? bot.unbound_default_env || null : null;
}

function botUnboundName(row: IMUser): string {
  return botsById.value.get(row.bot_id)?.unbound_username || "";
}

function memberName(row: Pick<IMUser, "remark" | "self_name">): string {
  return row.remark || (row.self_name ? `${row.self_name}（自称）` : "");
}

function boundLabel(row: IMUser): string {
  if (!row.username || !row.bound_by) return "";
  return row.bound_by === row.username ? "自助绑定" : `${row.bound_by} 绑定`;
}

function formatTime(value?: string | null): string {
  return value ? dayjs(value).format("MM-DD HH:mm") : "";
}

/** ElMessageBox 被取消或关掉时 reject 的值，不算出错 */
const dismissed = (error: unknown) => error === "cancel" || error === "close";

let refreshTimer: ReturnType<typeof setInterval> | undefined;

function currentSharedFormSnapshot(): SharedModelForm {
  return {
    ...sharedForm,
    max_input_tokens: sharedLimits.value.max_input_tokens,
    max_output_tokens: sharedLimits.value.max_output_tokens,
    compression_ratio: sharedLimits.value.compression_ratio,
    reasoning_effort: sharedEffort.value
  };
}

function currentBotFormSnapshot(): BotForm {
  return {
    ...botForm,
    model_override: {
      ...botForm.model_override,
      max_input_tokens: overrideLimits.value.max_input_tokens,
      max_output_tokens: overrideLimits.value.max_output_tokens,
      compression_ratio: overrideLimits.value.compression_ratio,
      reasoning_effort: overrideEffort.value
    }
  };
}

async function loadSharedModel() {
  const value = await aiApi.sharedModelConfig();
  sharedConfig.value = value;
  Object.assign(sharedForm, defaultSharedModelForm(value));
  sharedLimits.value = modelLimitsFor(sharedForm.model, value);
  sharedEffort.value = value.reasoning_effort ?? null;
}

async function loadBots() {
  botsLoading.value = true;
  try {
    const result = await aiApi.imBots();
    bots.value = result.bots || [];
    if (!mapping.bot_id && bots.value.length === 1) {
      mapping.bot_id = bots.value[0].id;
    }
  } finally {
    botsLoading.value = false;
  }
}

async function loadUsers() {
  usersLoading.value = true;
  try {
    const [usersResult, systemResult, clusterResult] = await Promise.all([
      aiApi.imUsers(selectedBotFilter.value || undefined),
      getUsers(),
      getKnownClusters()
    ]);
    users.value = usersResult.users || [];
    systemUsers.value = systemResult.data || [];
    clusters.value = (clusterResult.data || []).map(item => item.env).sort();
  } finally {
    usersLoading.value = false;
  }
}

async function refresh() {
  loading.value = true;
  try {
    await Promise.all([loadSharedModel(), loadBots(), loadUsers()]);
  } catch (error) {
    ElMessage.error(
      error instanceof Error ? error.message : "读取 AI 机器人与模型配置失败"
    );
  } finally {
    loading.value = false;
  }
}

async function testSharedModel() {
  const snapshot = currentSharedFormSnapshot();
  const validationError = validateSharedModelForm(snapshot);
  if (validationError) {
    ElMessage.warning(validationError);
    return;
  }
  sharedTesting.value = true;
  try {
    const result = await aiApi.testSharedModel({
      base_url: snapshot.base_url.trim().replace(/\/+$/, ""),
      api_key: snapshot.api_key.trim(),
      model: snapshot.model.trim(),
      max_input_tokens: snapshot.max_input_tokens,
      max_output_tokens: snapshot.max_output_tokens,
      compression_ratio: snapshot.compression_ratio,
      reasoning_effort: snapshot.reasoning_effort
    });
    if (result.success === false) {
      throw new Error(result.message || "模型测试失败");
    }
    if (result.warning || result.tool_calling === false) {
      ElMessage.warning(
        result.warning || "连接成功，但模型未确认支持工具调用。"
      );
    } else {
      ElMessage.success("公共模型与工具调用测试通过。");
    }
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "模型测试失败");
  } finally {
    sharedTesting.value = false;
  }
}

async function saveSharedModel() {
  const snapshot = currentSharedFormSnapshot();
  const validationError = validateSharedModelForm(snapshot);
  if (validationError) {
    ElMessage.warning(validationError);
    return;
  }
  sharedSaving.value = true;
  try {
    const saved = await aiApi.saveSharedModel(
      buildSharedModelPayload(snapshot)
    );
    sharedConfig.value = saved;
    sharedForm.api_key = "";
    ElMessage.success("公共默认模型配置已保存。");
  } catch (error) {
    ElMessage.error(
      error instanceof Error ? error.message : "保存公共模型配置失败"
    );
  } finally {
    sharedSaving.value = false;
  }
}

function applyBotForm(next: BotForm) {
  Object.assign(botForm, {
    ...next,
    model_override: { ...next.model_override }
  });
  overrideLimits.value = modelLimitsFor(
    next.model_override.model,
    next.model_override
  );
  overrideEffort.value = next.model_override.reasoning_effort ?? null;
}

function openCreateBot(platform: IMPlatform = "wecom") {
  applyBotForm(defaultBotForm(platform));
  botDrawerVisible.value = true;
}

function openEditBot(bot: IMBot) {
  applyBotForm(botFormFromSaved(bot));
  botDrawerVisible.value = true;
}

async function saveBot() {
  const snapshot = currentBotFormSnapshot();
  const validationError = validateBotForm(snapshot);
  if (validationError) {
    ElMessage.warning(validationError);
    return;
  }
  botSaving.value = true;
  try {
    const payload = buildBotPayload(snapshot);
    if (snapshot.id) {
      await aiApi.updateIMBot(snapshot.id, payload);
      ElMessage.success(
        snapshot.enabled
          ? "机器人配置已更新，正在启动或重连。"
          : "机器人配置已保存。"
      );
    } else {
      await aiApi.createIMBot(payload);
      ElMessage.success(
        snapshot.enabled ? "机器人已创建并正在连接。" : "机器人已创建。"
      );
    }
    botDrawerVisible.value = false;
    await loadBots();
  } catch (error) {
    ElMessage.error(
      error instanceof Error ? error.message : "保存机器人配置失败"
    );
  } finally {
    botSaving.value = false;
  }
}

async function saveBotUnboundOnly() {
  if (!botForm.id) return;
  unboundSaving.value = true;
  try {
    const username = botForm.unbound_username.trim() || null;
    const defaultEnv = username ? envOrNull(botForm.unbound_default_env) : null;
    const saved = await aiApi.saveIMBotUnbound(
      botForm.id,
      username,
      defaultEnv
    );
    botForm.unbound_username = saved.unbound_username || "";
    botForm.unbound_default_env = saved.unbound_default_env || NO_CLUSTER;
    await loadBots();
    ElMessage.success(
      saved.unbound_username
        ? `未绑定成员将按 ${saved.unbound_username} 的权限使用该机器人，默认集群：${saved.unbound_default_env || "无"}。`
        : "已关闭该机器人的未绑定默认权限。"
    );
  } catch (error) {
    ElMessage.error(
      error instanceof Error ? error.message : "保存未绑定默认权限失败"
    );
  } finally {
    unboundSaving.value = false;
  }
}

async function toggleBotEnabled(bot: IMBot, enabled: boolean) {
  const nextForm = { ...botFormFromSaved(bot), enabled };
  if (enabled) {
    const validationError = validateBotForm(nextForm);
    if (validationError) {
      ElMessage.warning(`请先完善机器人配置：${validationError}`);
      openEditBot(bot);
      return;
    }
  }
  togglingBotId.value = bot.id;
  try {
    const updated = await aiApi.updateIMBot(bot.id, buildBotPayload(nextForm));
    const idx = bots.value.findIndex(item => item.id === bot.id);
    if (idx >= 0) bots.value.splice(idx, 1, updated);
    else await loadBots();
    ElMessage.success(
      enabled ? "机器人已启用，正在建立长连接。" : "机器人已停用。"
    );
  } catch (error) {
    ElMessage.error(
      error instanceof Error ? error.message : "切换机器人启用状态失败"
    );
  } finally {
    togglingBotId.value = "";
  }
}

async function removeBot(bot: IMBot) {
  try {
    await ElMessageBox.confirm(
      `确认删除机器人「${bot.name}」（${platformLabel(bot.platform)}）？将断开连接并清理该机器人的成员绑定关系。`,
      "删除机器人",
      { type: "warning", confirmButtonText: "删除" }
    );
    await aiApi.deleteIMBot(bot.id);
    if (selectedBotFilter.value === bot.id) selectedBotFilter.value = "";
    if (mapping.bot_id === bot.id) mapping.bot_id = "";
    await Promise.all([loadBots(), loadUsers()]);
    ElMessage.success("机器人已删除。");
  } catch (error) {
    if (!dismissed(error)) {
      ElMessage.error(
        error instanceof Error ? error.message : "删除机器人失败"
      );
    }
  }
}

function inspectBotUsers(bot: IMBot) {
  selectedBotFilter.value = bot.id;
  mapping.bot_id = bot.id;
  activeTab.value = "users";
}

function editMapping(row: IMUser) {
  mapping.bot_id = row.bot_id;
  mapping.external_user_id = row.external_user_id;
  mapping.username = row.username || "";
  mapping.default_env = row.default_env || NO_CLUSTER;
}

function resetMapping() {
  mapping.external_user_id = "";
  mapping.username = "";
  mapping.default_env = NO_CLUSTER;
}

async function saveMapping() {
  if (
    !mapping.bot_id ||
    !mapping.external_user_id.trim() ||
    !mapping.username
  ) {
    ElMessage.warning("请选择所属机器人、填写外部成员 ID 并选择花折账号。");
    return;
  }
  mappingSaving.value = true;
  try {
    await aiApi.saveIMUser({
      bot_id: mapping.bot_id,
      external_user_id: mapping.external_user_id.trim(),
      username: mapping.username,
      default_env: envOrNull(mapping.default_env)
    });
    resetMapping();
    await loadUsers();
    ElMessage.success("成员绑定已保存。");
  } catch (error) {
    ElMessage.error(
      error instanceof Error ? error.message : "保存成员绑定失败"
    );
  } finally {
    mappingSaving.value = false;
  }
}

async function removeMapping(row: IMUser) {
  try {
    await ElMessageBox.confirm(
      `解除 ${platformLabel(row.platform)} 成员 ${memberName(row) || row.external_user_id} 的花折账号绑定？`,
      "解除绑定",
      { type: "warning" }
    );
    await aiApi.deleteIMUser(row.bot_id, row.external_user_id);
    await loadUsers();
    ElMessage.success("成员绑定已解除。");
  } catch (error) {
    if (!dismissed(error)) {
      ElMessage.error(error instanceof Error ? error.message : "解除绑定失败");
    }
  }
}

async function editRemark(row: IMUser) {
  try {
    const who =
      memberName(row) || `ID 尾号 ${row.external_user_id.slice(-6)} 的成员`;
    const { value } = await ElMessageBox.prompt(
      `给 ${who} 写备注（比如姓名、部门），只用来辨认，不影响权限；留空表示清除。`,
      "成员备注",
      {
        inputValue: row.remark || "",
        inputPlaceholder: "例如：张三（运维部）",
        inputValidator: (text: string) =>
          !text || text.trim().length <= 100 || "备注最多 100 个字",
        confirmButtonText: "保存"
      }
    );
    await aiApi.saveIMUserRemark(
      row.bot_id,
      row.external_user_id,
      value?.trim() || null
    );
    await loadUsers();
    ElMessage.success("备注已保存。");
  } catch (error) {
    if (!dismissed(error)) {
      ElMessage.error(error instanceof Error ? error.message : "保存备注失败");
    }
  }
}

async function purgeMapping(row: IMUser) {
  try {
    await ElMessageBox.confirm(
      `清理 ${platformLabel(row.platform)} 成员 ${memberName(row) || row.external_user_id}？绑定、默认集群和对话都会清掉，进行中的任务会停止，旧对话不能再继续（消息记录仍保留在审计里）；他下次发消息时按新成员接入。`,
      "清理成员",
      { type: "warning", confirmButtonText: "清理" }
    );
    const result = await aiApi.purgeIMUser(row.bot_id, row.external_user_id);
    if (
      mapping.bot_id === row.bot_id &&
      mapping.external_user_id.trim() === row.external_user_id
    ) {
      resetMapping();
    }
    await loadUsers();
    ElMessage.success(
      result.stopped
        ? `已清理，停止了 ${result.stopped} 个进行中的任务。`
        : "已清理，该成员下次发消息时按新成员接入。"
    );
  } catch (error) {
    if (!dismissed(error)) {
      ElMessage.error(error instanceof Error ? error.message : "清理成员失败");
    }
  }
}

onMounted(() => {
  void refresh();
  refreshTimer = setInterval(() => {
    void aiApi
      .imBots()
      .then(result => {
        bots.value = result.bots || [];
      })
      .catch(() => undefined);
  }, 5000);
});

onBeforeUnmount(() => {
  if (refreshTimer) clearInterval(refreshTimer);
});
</script>

<template>
  <div v-loading="loading" class="im-admin">
    <div class="page-header">
      <div>
        <h2>AI 机器人与公共模型</h2>
        <p>
          统一管理全局公共默认模型与多平台 IM 双向机器人（企业微信 / 飞书
          Lark），支持零公网长连接、群聊 @
          门禁、单机器人模型覆盖与跨平台成员绑定。
        </p>
      </div>
      <el-button @click="refresh">刷新状态</el-button>
    </div>

    <el-tabs v-model="activeTab" class="im-tabs">
      <el-tab-pane label="公共默认模型" name="shared-model">
        <section class="tab-section">
          <el-alert
            type="info"
            :closable="false"
            show-icon
            title="独立全局公共默认模型"
            class="section-alert"
          >
            此配置同时服务于「网页 AI 助手」「历史日志 AI
            生成说明」以及未单独配置覆盖模型的「IM 机器人」。即使所有 IM
            机器人均停用，网页 AI 与日志说明生成仍可正常使用。
          </el-alert>

          <div class="status-meta">
            <el-tag
              :type="sharedConfig.available ? 'success' : 'warning'"
              size="small"
            >
              {{ sharedConfig.available ? "已配置可用" : "尚未配置" }}
            </el-tag>
            <span v-if="sharedConfig.updated_at" class="meta-text">
              最近更新：{{ formatTime(sharedConfig.updated_at) }}
              <template v-if="sharedConfig.updated_by">
                （{{ sharedConfig.updated_by }}）
              </template>
            </span>
          </div>

          <el-form label-width="170px" class="config-form">
            <el-form-item label="Base URL" required>
              <el-input
                v-model="sharedForm.base_url"
                placeholder="https://api.openai.com/v1"
              />
            </el-form-item>
            <el-form-item label="API Key">
              <el-input
                v-model="sharedForm.api_key"
                type="password"
                show-password
                autocomplete="new-password"
                :placeholder="
                  sharedConfig.api_key_set
                    ? '已保存，留空保持不变'
                    : '本地兼容模型允许留空'
                "
              />
            </el-form-item>
            <el-form-item label="模型名" required>
              <el-input
                v-model="sharedForm.model"
                placeholder="例如 deepseek-chat / gpt-4o-mini"
              />
            </el-form-item>
            <AIModelLimitsFields
              v-model="sharedLimits"
              :model="sharedForm.model"
            />
            <AIReasoningEffortField v-model="sharedEffort" />
            <div class="form-actions">
              <el-button :loading="sharedTesting" @click="testSharedModel"
                >测试连接</el-button
              >
              <el-button
                type="primary"
                :loading="sharedSaving"
                @click="saveSharedModel"
                >保存公共模型</el-button
              >
            </div>
          </el-form>
        </section>
      </el-tab-pane>

      <el-tab-pane label="IM 机器人实例（企业微信 / 飞书）" name="bots">
        <section class="tab-section">
          <div class="toolbar">
            <div class="toolbar-desc">
              每个机器人实例拥有独立凭证、长连接状态、群聊安全策略（默认仅私聊）、未绑定默认权限与可选模型覆盖。
            </div>
            <div class="toolbar-actions">
              <el-button type="primary" @click="openCreateBot('feishu')"
                >新增飞书机器人</el-button
              >
              <el-button @click="openCreateBot('wecom')"
                >新增企业微信机器人</el-button
              >
            </div>
          </div>

          <el-table
            v-loading="botsLoading"
            :data="bots"
            row-key="id"
            size="small"
          >
            <el-table-column label="名称" min-width="190">
              <template #default="{ row }">
                <div class="bot-name-cell">
                  <span class="bot-name">{{ row.name }}</span>
                  <el-tag
                    v-if="row.model_override"
                    size="small"
                    type="info"
                    effect="plain"
                    >独立模型：{{ row.model_override.model }}</el-tag
                  >
                </div>
                <div class="sub-text">
                  <template v-if="row.platform === 'feishu'">
                    App ID: {{ row.credentials?.app_id || "—" }} ·
                    {{
                      feishuDomainLabel(
                        row.credentials?.domain_preset,
                        row.credentials?.custom_domain
                      )
                    }}
                  </template>
                  <template v-else>
                    Bot ID: {{ row.credentials?.bot_id || "—" }}
                  </template>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="平台" width="120">
              <template #default="{ row }">
                <el-tag
                  size="small"
                  :type="row.platform === 'feishu' ? 'primary' : 'success'"
                  >{{ platformLabel(row.platform) }}</el-tag
                >
              </template>
            </el-table-column>
            <el-table-column label="连接状态" min-width="160">
              <template #default="{ row }">
                <el-tag :type="botStatusInfo(row).type" size="small">
                  {{ botStatusInfo(row).label }}
                </el-tag>
                <div v-if="row.error" class="error-text" :title="row.error">
                  {{ row.error }}
                </div>
              </template>
            </el-table-column>
            <el-table-column label="群聊策略" min-width="160">
              <template #default="{ row }">
                <span>{{ groupPolicyLabel(row.group_policy) }}</span>
                <span
                  v-if="row.group_policy === 'allowlist'"
                  class="muted-badge"
                >
                  ({{ row.allowed_chat_ids?.length || 0 }} 个群)
                </span>
              </template>
            </el-table-column>
            <el-table-column label="默认集群 / 未绑定权限" min-width="180">
              <template #default="{ row }">
                <template v-if="row.unbound_username">
                  <span>{{ row.unbound_username }}</span>
                  <div class="sub-text">
                    默认集群：{{ row.unbound_default_env || "无（需先选择）" }}
                  </div>
                </template>
                <span v-else class="muted-text">需先绑定花折账号</span>
              </template>
            </el-table-column>
            <el-table-column label="启用开关" width="100">
              <template #default="{ row }">
                <el-switch
                  :model-value="row.enabled"
                  :loading="togglingBotId === row.id"
                  @change="toggleBotEnabled(row, Boolean($event))"
                />
              </template>
            </el-table-column>
            <el-table-column label="操作" width="170" fixed="right">
              <template #default="{ row }">
                <el-button link type="primary" @click="openEditBot(row)"
                  >编辑</el-button
                >
                <el-button link type="primary" @click="inspectBotUsers(row)"
                  >成员</el-button
                >
                <el-button link type="danger" @click="removeBot(row)"
                  >删除</el-button
                >
              </template>
            </el-table-column>
          </el-table>
        </section>
      </el-tab-pane>

      <el-tab-pane label="IM 成员绑定管理" name="users">
        <section class="tab-section">
          <div class="toolbar">
            <div class="filter-bar">
              <el-select
                v-model="selectedBotFilter"
                clearable
                placeholder="筛选全部机器人"
                style="width: 240px"
                @change="loadUsers"
              >
                <el-option
                  v-for="bot in bots"
                  :key="bot.id"
                  :label="`[${platformLabel(bot.platform)}] ${bot.name}`"
                  :value="bot.id"
                />
              </el-select>
              <el-input
                v-model="userKeyword"
                clearable
                placeholder="搜索备注、成员 ID 或花折账号"
                style="width: 240px"
              />
            </div>
            <el-button @click="loadUsers">刷新成员</el-button>
          </div>

          <el-table
            v-loading="usersLoading"
            :data="filteredUsers"
            :row-key="row => `${row.bot_id}:${row.external_user_id}`"
            size="small"
          >
            <el-table-column label="平台 / 机器人" min-width="160">
              <template #default="{ row }">
                <div class="platform-bot">
                  <el-tag
                    size="small"
                    :type="row.platform === 'feishu' ? 'primary' : 'success'"
                    >{{ platformLabel(row.platform) }}</el-tag
                  >
                  <span>{{ botNameOf(row) }}</span>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="IM 成员" min-width="220">
              <template #default="{ row }">
                <div class="member-name">
                  <span v-if="memberName(row)">{{ memberName(row) }}</span>
                  <span v-else class="muted-text">未知成员</span>
                  <el-button
                    link
                    type="primary"
                    size="small"
                    @click="editRemark(row)"
                    >备注</el-button
                  >
                </div>
                <div class="member-id" :title="row.external_user_id">
                  {{ row.external_user_id }}
                </div>
              </template>
            </el-table-column>
            <el-table-column label="花折账号" min-width="150">
              <template #default="{ row }">
                <template v-if="row.username">
                  <span>{{ row.username }}</span>
                  <div v-if="boundLabel(row)" class="member-id">
                    {{ boundLabel(row) }}
                  </div>
                </template>
                <template v-else>
                  <el-tag type="warning" size="small">待绑定</el-tag>
                  <span v-if="botUnboundName(row)" class="muted">
                    默认权限 {{ botUnboundName(row) }}
                  </span>
                </template>
              </template>
            </el-table-column>
            <el-table-column label="默认集群" min-width="130">
              <template #default="{ row }">
                {{
                  row.username || botUnboundName(row)
                    ? defaultEnvOf(row) || "无"
                    : "—"
                }}
              </template>
            </el-table-column>
            <el-table-column label="当前集群" min-width="130">
              <template #default="{ row }">
                {{ row.current_env || defaultEnvOf(row) || "未选择" }}
              </template>
            </el-table-column>
            <el-table-column label="最后活跃" width="110">
              <template #default="{ row }">
                {{ formatTime(row.last_seen_at) }}
              </template>
            </el-table-column>
            <el-table-column
              prop="pending_approvals"
              label="待审批"
              width="80"
            />
            <el-table-column label="操作" width="160" fixed="right">
              <template #default="{ row }">
                <el-button link type="primary" @click="editMapping(row)">{{
                  row.username ? "修改" : "绑定"
                }}</el-button>
                <el-button
                  v-if="row.username"
                  link
                  type="danger"
                  @click="removeMapping(row)"
                  >解除</el-button
                >
                <el-tooltip
                  content="当作新成员：清掉绑定、集群和对话，停止进行中的任务"
                  placement="top"
                >
                  <el-button link type="danger" @click="purgeMapping(row)"
                    >清理</el-button
                  >
                </el-tooltip>
              </template>
            </el-table-column>
          </el-table>

          <el-form label-width="170px" class="mapping-form">
            <el-divider content-position="left">绑定 IM 成员</el-divider>
            <el-form-item label="所属机器人" required>
              <el-select
                v-model="mapping.bot_id"
                filterable
                placeholder="选择 IM 机器人实例"
              >
                <el-option
                  v-for="bot in bots"
                  :key="bot.id"
                  :label="`[${platformLabel(bot.platform)}] ${bot.name}`"
                  :value="bot.id"
                />
              </el-select>
            </el-form-item>
            <el-form-item label="外部成员 ID" required>
              <el-input
                v-model="mapping.external_user_id"
                placeholder="在上方列表点「绑定」带入，或输入企微 UserID / 飞书 open_id"
              />
              <div
                v-if="mappingMember && memberName(mappingMember)"
                class="form-tip"
              >
                成员：{{ memberName(mappingMember) }}
              </div>
            </el-form-item>
            <el-form-item label="花折账号" required>
              <el-select
                v-model="mapping.username"
                filterable
                clearable
                placeholder="选择已启用的花折账号"
                @change="mapping.default_env = NO_CLUSTER"
              >
                <el-option
                  v-for="user in systemUsers.filter(
                    item => item.enabled && item.role !== 'agent'
                  )"
                  :key="user.id"
                  :label="`${user.username} (${ROLE_LABELS[user.role] || user.role})`"
                  :value="user.username"
                />
              </el-select>
            </el-form-item>
            <el-form-item label="默认集群">
              <el-select v-model="mapping.default_env">
                <el-option :label="NO_CLUSTER_LABEL" :value="NO_CLUSTER" />
                <el-option
                  v-for="env in availableMappingClusters"
                  :key="env"
                  :label="env"
                  :value="env"
                />
              </el-select>
              <div class="form-tip">
                该成员每个新对话从这个集群开始；选「无」时每个新对话都要先选集群。
              </div>
            </el-form-item>
            <div class="form-actions">
              <el-button
                :loading="mappingSaving"
                type="primary"
                @click="saveMapping"
                >保存绑定</el-button
              >
            </div>
          </el-form>
        </section>
      </el-tab-pane>
    </el-tabs>

    <el-drawer
      v-model="botDrawerVisible"
      :title="botForm.id ? '编辑 IM 机器人' : '新增 IM 机器人'"
      size="640px"
      destroy-on-close
    >
      <el-form label-width="150px" class="drawer-form">
        <el-form-item label="IM 平台" required>
          <el-radio-group v-model="botForm.platform" :disabled="!!botForm.id">
            <el-radio-button
              v-for="item in PLATFORM_OPTIONS"
              :key="item.value"
              :value="item.value"
              >{{ item.label }}</el-radio-button
            >
          </el-radio-group>
        </el-form-item>
        <el-form-item label="机器人名称" required>
          <el-input
            v-model="botForm.name"
            maxlength="80"
            placeholder="例如：生产运维飞书机器人"
          />
        </el-form-item>
        <el-form-item label="启用机器人">
          <el-switch
            v-model="botForm.enabled"
            active-text="启用"
            inactive-text="停用"
          />
        </el-form-item>

        <el-divider content-position="left">平台接入凭证</el-divider>
        <template v-if="botForm.platform === 'wecom'">
          <el-form-item label="Bot ID" required>
            <el-input
              v-model="botForm.bot_id"
              autocomplete="off"
              placeholder="企业微信 API 模式机器人 Bot ID"
            />
          </el-form-item>
          <el-form-item label="Bot Secret" required>
            <el-input
              v-model="botForm.bot_secret"
              type="password"
              show-password
              autocomplete="new-password"
              :placeholder="
                botForm.secret_set
                  ? '已保存，留空保持不变'
                  : '企业微信 API 模式机器人 Secret'
              "
            />
          </el-form-item>
        </template>

        <template v-else>
          <el-form-item label="App ID" required>
            <el-input
              v-model="botForm.app_id"
              autocomplete="off"
              placeholder="飞书自建应用 App ID（cli_...）"
            />
          </el-form-item>
          <el-form-item label="App Secret" required>
            <el-input
              v-model="botForm.app_secret"
              type="password"
              show-password
              autocomplete="new-password"
              :placeholder="
                botForm.secret_set
                  ? '已保存，留空保持不变'
                  : '飞书应用 App Secret'
              "
            />
          </el-form-item>
          <el-form-item label="平台环境">
            <el-select v-model="botForm.domain_preset">
              <el-option
                v-for="item in FEISHU_DOMAIN_OPTIONS"
                :key="item.value"
                :label="item.label"
                :value="item.value"
              />
            </el-select>
          </el-form-item>
          <el-form-item
            v-if="botForm.domain_preset === 'custom'"
            label="私有化域名"
            required
          >
            <el-input
              v-model="botForm.custom_domain"
              placeholder="https://open.example.com"
            />
          </el-form-item>
          <p class="form-tip drawer-tip">
            飞书机器人通过官方长连接（WebSocket）接入，无需公网回调
            URL；请在飞书开放平台开启机器人能力并订阅 im.message.receive_v1 与
            card.action.trigger 事件。
          </p>
        </template>

        <el-divider content-position="left">会话与群聊安全策略</el-divider>
        <el-form-item label="对话空闲超时（分钟）">
          <el-input-number
            v-model="botForm.session_idle_minutes"
            :min="0"
            :max="10080"
            :step="30"
            :precision="0"
            controls-position="right"
          />
          <div class="form-tip">
            距上次对话超过该分钟数，新消息自动进入新对话（0 表示关闭，默认
            120）。
          </div>
        </el-form-item>
        <el-form-item label="群聊策略">
          <el-select v-model="botForm.group_policy">
            <el-option
              v-for="item in GROUP_POLICY_OPTIONS"
              :key="item.value"
              :label="item.label"
              :value="item.value"
            />
          </el-select>
          <div class="form-tip">
            {{
              GROUP_POLICY_OPTIONS.find(
                item => item.value === botForm.group_policy
              )?.desc
            }}
          </div>
        </el-form-item>
        <el-form-item
          v-if="botForm.group_policy === 'allowlist'"
          label="群聊白名单"
          required
        >
          <el-input
            v-model="botForm.allowed_chat_ids_text"
            type="textarea"
            :rows="3"
            placeholder="每行或逗号分隔一个群 Chat ID，例如：oc_123456..."
          />
        </el-form-item>

        <el-divider content-position="left">未绑定成员默认权限</el-divider>
        <el-alert
          v-if="botUnboundUnavailable"
          :title="`所选账号 ${botForm.unbound_username} 已停用、删除或为管理员账号，保存后请重新选择。`"
          type="warning"
          :closable="false"
          show-icon
          class="unbound-alert"
        />
        <el-form-item label="默认权限账号">
          <el-select
            v-model="botForm.unbound_username"
            filterable
            clearable
            placeholder="不开放，未绑定成员需先绑定"
            @change="onBotUnboundUserChange"
          >
            <el-option
              v-for="user in unboundCandidates"
              :key="user.id"
              :label="`${user.username} (${ROLE_LABELS[user.role] || user.role})`"
              :value="user.username"
            />
          </el-select>
          <div v-if="botForm.unbound_username" class="unbound-access">
            <span class="form-tip">可访问：</span>
            <el-tag
              v-for="item in botUnboundAccess"
              :key="item.env"
              size="small"
              :type="PERM_TAG[item.permission]"
              >{{ item.env }} · {{ PERM_LABELS[item.permission] }}</el-tag
            >
            <span v-if="!botUnboundAccess.length" class="form-tip"
              >没有可访问的集群</span
            >
          </div>
        </el-form-item>
        <el-form-item label="默认集群">
          <el-select
            v-model="botForm.unbound_default_env"
            :disabled="!botForm.unbound_username"
          >
            <el-option :label="NO_CLUSTER_LABEL" :value="NO_CLUSTER" />
            <el-option
              v-for="item in botUnboundAccess"
              :key="item.env"
              :label="item.env"
              :value="item.env"
            />
          </el-select>
          <div v-if="botForm.id" class="unbound-inline-action">
            <el-button
              link
              type="primary"
              :loading="unboundSaving"
              @click="saveBotUnboundOnly"
              >仅保存未绑定权限</el-button
            >
          </div>
        </el-form-item>

        <el-divider content-position="left">模型覆盖（可选）</el-divider>
        <el-form-item label="覆盖公共模型">
          <el-switch
            v-model="botForm.model_override.enabled"
            active-text="使用独立模型"
            inactive-text="继承公共默认模型"
          />
        </el-form-item>
        <template v-if="botForm.model_override.enabled">
          <el-form-item label="Base URL" required>
            <el-input
              v-model="botForm.model_override.base_url"
              placeholder="https://api.openai.com/v1"
            />
          </el-form-item>
          <el-form-item label="API Key">
            <el-input
              v-model="botForm.model_override.api_key"
              type="password"
              show-password
              autocomplete="new-password"
              :placeholder="
                botForm.model_override.api_key_set
                  ? '已保存，留空保持不变'
                  : '本地模型允许留空'
              "
            />
          </el-form-item>
          <el-form-item label="模型名" required>
            <el-input
              v-model="botForm.model_override.model"
              placeholder="例如 deepseek-chat"
            />
          </el-form-item>
          <AIModelLimitsFields
            v-model="overrideLimits"
            :model="botForm.model_override.model"
          />
          <AIReasoningEffortField v-model="overrideEffort" />
        </template>
      </el-form>

      <template #footer>
        <div class="drawer-footer">
          <el-button @click="botDrawerVisible = false">取消</el-button>
          <el-button type="primary" :loading="botSaving" @click="saveBot"
            >保存机器人</el-button
          >
        </div>
      </template>
    </el-drawer>
  </div>
</template>

<style scoped>
.im-admin {
  display: grid;
  gap: 16px;
  padding: 20px;
}

.page-header {
  display: flex;
  gap: 16px;
  align-items: flex-start;
  justify-content: space-between;
}

.page-header h2 {
  margin: 0;
  font-size: 18px;
  font-weight: 600;
}

.page-header p {
  margin: 6px 0 0;
  line-height: 1.5;
  color: var(--el-text-color-secondary);
}

.tab-section {
  padding-top: 4px;
}

.section-alert {
  margin-bottom: 16px;
}

.status-meta {
  display: flex;
  gap: 10px;
  align-items: center;
  margin-bottom: 12px;
}

.meta-text {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.config-form,
.mapping-form {
  max-width: 820px;
  margin-top: 12px;
}

.drawer-form {
  padding-right: 8px;
}

.toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 14px;
}

.toolbar-desc {
  font-size: 13px;
  color: var(--el-text-color-secondary);
}

.toolbar-actions,
.filter-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}

.bot-name-cell {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
}

.bot-name {
  font-weight: 500;
}

.sub-text {
  margin-top: 2px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.error-text {
  margin-top: 4px;
  overflow: hidden;
  font-size: 12px;
  color: var(--el-color-danger);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.muted-badge {
  margin-left: 4px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.platform-bot {
  display: flex;
  gap: 6px;
  align-items: center;
}

.unbound-access {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
  margin-top: 6px;
}

.unbound-alert {
  margin-bottom: 12px;
}

.unbound-inline-action {
  margin-top: 4px;
}

.form-tip {
  margin-top: 4px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--el-text-color-secondary);
}

.drawer-tip {
  margin: 0 0 12px 150px;
}

.unbound-access .form-tip {
  margin-top: 0;
}

.muted {
  margin-left: 6px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.muted-text {
  color: var(--el-text-color-secondary);
}

.member-name {
  display: flex;
  gap: 6px;
  align-items: center;
}

.member-id {
  overflow: hidden;
  font-size: 12px;
  color: var(--el-text-color-secondary);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.form-actions,
.drawer-footer {
  display: flex;
  gap: 8px;
  justify-content: flex-end;
}

@media (width <= 640px) {
  .im-admin {
    padding: 12px;
  }

  .page-header {
    flex-direction: column;
  }

  .drawer-tip {
    margin-left: 0;
  }

  :deep(.el-form-item) {
    display: block;
  }

  :deep(.el-form-item__label) {
    width: 100% !important;
    text-align: left;
  }
}
</style>
