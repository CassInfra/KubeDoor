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
  type AIProvider,
  type AIReasoningEffort,
  type WeComConfig,
  type WeComUnbound,
  type WeComUser
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
  roleDefaultPermission
} from "@/utils/permission";

/** 企微对话空闲多少分钟后自动开新对话（0 表示关闭） */
const DEFAULT_IDLE_MINUTES = 120;
const loading = ref(false);
const saving = ref(false);
const testing = ref(false);
const usersLoading = ref(false);
const unboundSaving = ref(false);
const config = ref<WeComConfig>({
  enabled: false,
  configured: false,
  connected: false,
  authenticated: false,
  reconnecting: false,
  has_bot_secret: false,
  has_api_key: false
});
const form = reactive({
  enabled: false,
  bot_id: "",
  bot_secret: "",
  base_url: "https://api.openai.com/v1",
  api_key: "",
  model: "",
  session_idle_minutes: DEFAULT_IDLE_MINUTES as number | undefined
});
const limits = ref<AIModelLimits>(modelLimitsFor(""));
const effort = ref<AIReasoningEffort | null>(null);
// 模型名变化时，没手动改过的上下文设置按新模型名重新预填。
// 同步触发：loadConfig 先设模型名、再载入已保存的值，保存的值不会被改写。
watch(
  () => form.model,
  (model, previous) => {
    limits.value = followModelLimits(limits.value, previous, model);
  },
  { flush: "sync" }
);
const mappings = ref<WeComUser[]>([]);
const systemUsers = ref<SystemUser[]>([]);
const clusters = ref<string[]>([]);
/** 默认集群的「无」:每个新对话都要先选集群(卡片里选,或提问里说出集群名 / 别名) */
const NO_CLUSTER = "__none__";
const NO_CLUSTER_LABEL = "无（每个新对话先选集群）";
const envOrNull = (value: string) =>
  value && value !== NO_CLUSTER ? value : null;
const mapping = reactive({
  wecom_userid: "",
  username: "",
  default_env: NO_CLUSTER
});
/** 绑定表单里正在绑定的成员(从列表点「绑定」带入时能显示他是谁) */
const mappingMember = computed(() =>
  mappings.value.find(item => item.wecom_userid === mapping.wecom_userid.trim())
);
const availableClusters = computed(() => {
  const user = systemUsers.value.find(
    item => item.username === mapping.username
  );
  if (!user) return [];
  return clusters.value.filter(env => {
    const permission =
      user.role === "admin" ? "rw" : (user.clusters?.[env] ?? user.role);
    return permission === "rw" || permission === "read";
  });
});
/** 已保存的未绑定成员默认权限(账号 + 默认集群);unbound 是正在编辑的值 */
const unboundSaved = ref<WeComUnbound>({
  unbound_username: null,
  unbound_default_env: null
});
const unbound = reactive({ username: "", default_env: NO_CLUSTER });
const unboundDirty = computed(
  () =>
    unbound.username !== (unboundSaved.value.unbound_username || "") ||
    (unbound.username ? envOrNull(unbound.default_env) : null) !==
      unboundSaved.value.unbound_default_env
);
/** 管理员账号不能作为默认权限(后端同样会拒绝) */
const unboundCandidates = computed(() =>
  systemUsers.value.filter(
    item => item.enabled && item.role !== "agent" && item.role !== "admin"
  )
);
const unboundUnavailable = computed(
  () =>
    !!unboundSaved.value.unbound_username &&
    !usersLoading.value &&
    !unboundCandidates.value.some(
      item => item.username === unboundSaved.value.unbound_username
    )
);
/** 所选账号能访问的集群,让管理员确认默认权限的范围,也是默认集群的选项 */
const unboundAccess = computed(() => {
  const user = systemUsers.value.find(
    item => item.username === unbound.username
  );
  if (!user) return [];
  return clusters.value
    .map(env => ({
      env,
      permission: user.clusters?.[env] ?? roleDefaultPermission(user.role)
    }))
    .filter(item => item.permission !== "none");
});
/** 已保存的默认集群不在该账号可访问的范围里了:成员会被要求先切换集群 */
const unboundEnvUnavailable = computed(() => {
  const { unbound_username: username, unbound_default_env: env } =
    unboundSaved.value;
  return (
    !!env &&
    !usersLoading.value &&
    unbound.username === username &&
    !unboundAccess.value.some(item => item.env === env)
  );
});

function resetUnbound() {
  unbound.username = unboundSaved.value.unbound_username || "";
  unbound.default_env = unboundSaved.value.unbound_default_env || NO_CLUSTER;
}

function onUnboundUserChange() {
  if (!unboundAccess.value.some(item => item.env === unbound.default_env))
    unbound.default_env = NO_CLUSTER;
}

/** 成员没切换过集群时用的集群:绑定的看自己的默认集群,未绑定的看默认权限里的 */
function defaultEnvOf(row: WeComUser) {
  if (row.username) return row.default_env;
  return unboundSaved.value.unbound_username
    ? unboundSaved.value.unbound_default_env
    : null;
}

/** 列表里怎么称呼这个成员:企微只给加密的 UserID,管理员备注优先,其次是成员自报的名字 */
function memberName(row: WeComUser) {
  return row.remark || (row.self_name ? `${row.self_name}（自称）` : "");
}

/** 绑定方式:成员用绑定码自助绑定时,绑定人就是他自己的账号 */
function boundLabel(row: WeComUser) {
  if (!row.username || !row.bound_by) return "";
  return row.bound_by === row.username ? "自助绑定" : `${row.bound_by} 绑定`;
}

function formatTime(value: string | null) {
  return value ? dayjs(value).format("MM-DD HH:mm") : "";
}

/** ElMessageBox 被取消或关掉时 reject 的值,不算出错 */
const dismissed = (error: unknown) => error === "cancel" || error === "close";
let refreshTimer: ReturnType<typeof setInterval> | undefined;

async function loadConfig() {
  const value = await aiApi.wecomConfig();
  config.value = value;
  form.enabled = value.enabled;
  form.bot_id = value.bot_id || "";
  form.base_url = value.base_url || "https://api.openai.com/v1";
  form.model = value.model || "";
  limits.value = modelLimitsFor(form.model, value);
  effort.value = value.reasoning_effort ?? null;
  form.session_idle_minutes =
    value.session_idle_minutes ?? DEFAULT_IDLE_MINUTES;
  form.bot_secret = "";
  form.api_key = "";
}

async function loadMappings() {
  usersLoading.value = true;
  try {
    const [mappingResult, userResult, clusterResult] = await Promise.all([
      aiApi.wecomUsers(),
      getUsers(),
      getKnownClusters()
    ]);
    mappings.value = mappingResult.users;
    // 正在编辑默认权限时(还没保存)不要被刷新冲掉
    const editing = unboundDirty.value;
    unboundSaved.value = {
      unbound_username: mappingResult.unbound_username ?? null,
      unbound_default_env: mappingResult.unbound_default_env ?? null
    };
    if (!editing) resetUnbound();
    systemUsers.value = userResult.data || [];
    clusters.value = (clusterResult.data || []).map(item => item.env).sort();
  } finally {
    usersLoading.value = false;
  }
}

async function refresh() {
  loading.value = true;
  try {
    await Promise.all([loadConfig(), loadMappings()]);
  } catch (error) {
    ElMessage.error(
      error instanceof Error ? error.message : "读取企微配置失败"
    );
  } finally {
    loading.value = false;
  }
}

function validProvider() {
  if (!form.base_url.trim() || !form.model.trim()) {
    ElMessage.warning("请填写模型 Base URL 和模型名。");
    return false;
  }
  try {
    const url = new URL(form.base_url.trim());
    if (
      !["http:", "https:"].includes(url.protocol) ||
      url.username ||
      url.password ||
      url.search ||
      url.hash
    )
      throw new Error();
  } catch {
    ElMessage.warning("Base URL 必须是有效的 HTTP/HTTPS API 基址。");
    return false;
  }
  return true;
}

function provider(): AIProvider {
  return {
    base_url: form.base_url.trim(),
    api_key: form.api_key,
    model: form.model.trim(),
    ...limits.value,
    reasoning_effort: effort.value
  };
}

async function testModel() {
  if (!validProvider()) return;
  testing.value = true;
  try {
    const result = await aiApi.testWecomProvider(provider());
    if (result.success === false)
      throw new Error(result.message || "模型测试失败");
    if (result.warning || result.tool_calling === false) {
      ElMessage.warning(
        result.warning || "连接成功，但模型未确认支持工具调用。"
      );
    } else {
      ElMessage.success("模型与工具调用测试通过。");
    }
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "模型测试失败");
  } finally {
    testing.value = false;
  }
}

async function saveConfig() {
  if (
    form.enabled &&
    (!form.bot_id.trim() || (!form.bot_secret && !config.value.has_bot_secret))
  ) {
    ElMessage.warning("启用机器人需要 Bot ID 和 Secret。");
    return;
  }
  if (form.enabled && !validProvider()) return;
  saving.value = true;
  try {
    config.value = await aiApi.saveWecomConfig({
      enabled: form.enabled,
      bot_id: form.bot_id.trim(),
      bot_secret: form.bot_secret || undefined,
      base_url: form.base_url.trim(),
      api_key: form.api_key || undefined,
      model: form.model.trim(),
      ...limits.value,
      reasoning_effort: effort.value,
      session_idle_minutes: form.session_idle_minutes ?? DEFAULT_IDLE_MINUTES
    });
    form.bot_secret = "";
    form.api_key = "";
    ElMessage.success(
      form.enabled
        ? "配置已保存，机器人长连接正在启动或重连。"
        : "企微机器人已停用。"
    );
  } catch (error) {
    ElMessage.error(
      error instanceof Error ? error.message : "保存企微配置失败"
    );
  } finally {
    saving.value = false;
  }
}

async function saveUnbound() {
  unboundSaving.value = true;
  try {
    const result = await aiApi.saveWecomUnbound(
      unbound.username || null,
      unbound.username ? envOrNull(unbound.default_env) : null
    );
    unboundSaved.value = result;
    resetUnbound();
    ElMessage.success(
      result.unbound_username
        ? `未绑定成员将按 ${result.unbound_username} 的权限使用机器人，默认集群：${result.unbound_default_env || "无"}。`
        : "已关闭默认权限，未绑定成员需要先绑定。"
    );
  } catch (error) {
    ElMessage.error(
      error instanceof Error ? error.message : "保存默认权限失败"
    );
  } finally {
    unboundSaving.value = false;
  }
}

function editMapping(row: WeComUser) {
  mapping.wecom_userid = row.wecom_userid;
  mapping.username = row.username || "";
  mapping.default_env = row.default_env || NO_CLUSTER;
}

function resetMapping() {
  mapping.wecom_userid = "";
  mapping.username = "";
  mapping.default_env = NO_CLUSTER;
}

async function saveMapping() {
  if (!mapping.wecom_userid.trim() || !mapping.username) {
    ElMessage.warning("请填写企微 UserID 并选择花折账号。");
    return;
  }
  try {
    await aiApi.saveWecomUser({
      wecom_userid: mapping.wecom_userid.trim(),
      username: mapping.username,
      default_env: envOrNull(mapping.default_env)
    });
    resetMapping();
    await loadMappings();
    ElMessage.success("成员映射已保存。");
  } catch (error) {
    ElMessage.error(
      error instanceof Error ? error.message : "保存成员映射失败"
    );
  }
}

async function removeMapping(row: WeComUser) {
  try {
    await ElMessageBox.confirm(
      `解除企微成员 ${memberName(row) || row.wecom_userid} 的花折账号绑定？`,
      "解除绑定",
      { type: "warning" }
    );
    await aiApi.deleteWecomUser(row.wecom_userid);
    await loadMappings();
    ElMessage.success("成员绑定已解除。");
  } catch (error) {
    if (!dismissed(error))
      ElMessage.error(error instanceof Error ? error.message : "解除绑定失败");
  }
}

async function editRemark(row: WeComUser) {
  try {
    // 加密 UserID 有 32 位,放标题里太长:没有名字时用尾号指代
    const who =
      memberName(row) || `UserID 尾号 ${row.wecom_userid.slice(-6)} 的成员`;
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
    await aiApi.saveWecomRemark(row.wecom_userid, value?.trim() || null);
    await loadMappings();
    ElMessage.success("备注已保存。");
  } catch (error) {
    if (!dismissed(error))
      ElMessage.error(error instanceof Error ? error.message : "保存备注失败");
  }
}

async function purgeMapping(row: WeComUser) {
  try {
    await ElMessageBox.confirm(
      `清理企微成员 ${memberName(row) || row.wecom_userid}？绑定、默认集群和对话都会清掉，进行中的任务会停止，旧对话不能再继续（消息记录仍保留在审计里）；他下次发消息时按新成员接入。`,
      "清理成员",
      { type: "warning", confirmButtonText: "清理" }
    );
    const result = await aiApi.purgeWecomUser(row.wecom_userid);
    if (mapping.wecom_userid.trim() === row.wecom_userid) resetMapping();
    await loadMappings();
    ElMessage.success(
      result.stopped
        ? `已清理，停止了 ${result.stopped} 个进行中的任务。`
        : "已清理，该成员下次发消息时按新成员接入。"
    );
  } catch (error) {
    if (!dismissed(error))
      ElMessage.error(error instanceof Error ? error.message : "清理成员失败");
  }
}

function statusLabel() {
  if (!config.value.enabled) return "已停用";
  if (config.value.authenticated) return "已连接";
  if (config.value.reconnecting) return "正在重连";
  if (config.value.connected) return "已连接，认证中";
  return "未连接";
}

onMounted(() => {
  void refresh();
  refreshTimer = setInterval(() => {
    void aiApi
      .wecomConfig()
      .then(value => (config.value = value))
      .catch(() => undefined);
  }, 5000);
});
onBeforeUnmount(() => {
  if (refreshTimer) clearInterval(refreshTimer);
});
</script>

<template>
  <div v-loading="loading" class="wecom-admin">
    <section class="settings-section">
      <div class="section-heading">
        <div>
          <h2>企业微信智能机器人</h2>
          <p>API 模式长连接 · {{ statusLabel() }}</p>
        </div>
        <el-switch
          v-model="form.enabled"
          active-text="启用"
          inactive-text="停用"
        />
      </div>
      <el-alert
        v-if="config.error"
        :title="config.error"
        type="error"
        :closable="false"
        show-icon
      />
      <el-form label-width="170px" class="config-form">
        <el-divider content-position="left">机器人凭证</el-divider>
        <el-form-item label="Bot ID" required>
          <el-input
            v-model="form.bot_id"
            autocomplete="off"
            placeholder="企业微信 API 模式机器人 Bot ID"
          />
        </el-form-item>
        <el-form-item label="Bot Secret" required>
          <el-input
            v-model="form.bot_secret"
            type="password"
            show-password
            autocomplete="new-password"
            :placeholder="
              config.has_bot_secret
                ? '已保存，留空保持不变'
                : '企业微信 API 模式机器人 Secret'
            "
          />
        </el-form-item>
        <el-divider content-position="left">企微专用模型</el-divider>
        <el-form-item label="Base URL" required>
          <el-input
            v-model="form.base_url"
            placeholder="https://api.openai.com/v1"
          />
        </el-form-item>
        <el-form-item label="API Key" required>
          <el-input
            v-model="form.api_key"
            type="password"
            show-password
            autocomplete="new-password"
            :placeholder="
              config.has_api_key
                ? '已保存，留空保持不变'
                : '仅用于企业微信机器人'
            "
          />
        </el-form-item>
        <el-form-item label="模型名" required>
          <el-input v-model="form.model" placeholder="例如 gpt-4o-mini" />
        </el-form-item>
        <AIModelLimitsFields v-model="limits" :model="form.model" />
        <AIReasoningEffortField v-model="effort" />
        <el-form-item label="对话空闲超时（分钟）">
          <el-input-number
            v-model="form.session_idle_minutes"
            :min="0"
            :max="10080"
            :step="30"
            :precision="0"
            controls-position="right"
          />
          <div class="form-tip">
            距上次对话超过这个时间，成员的新消息自动进入新对话（0 表示关闭，默认
            120）。成员也可以发送「新对话」「继续上次」手动切换。
          </div>
        </el-form-item>
        <div class="form-actions">
          <el-button :loading="testing" @click="testModel">测试模型</el-button>
          <el-button type="primary" :loading="saving" @click="saveConfig"
            >保存配置</el-button
          >
        </div>
      </el-form>
    </section>

    <section class="settings-section members-section">
      <div class="section-heading">
        <div>
          <h2>成员与默认集群</h2>
          <p>
            企微成员权限始终按花折账号实时校验：已绑定的成员用绑定的账号，未绑定的成员用下面的默认权限；未绑定成员首次发消息后会出现在列表中。
          </p>
          <p>
            企微只提供加密的
            UserID，认不出是谁时：请成员在花折右上角用户菜单「绑定企业微信」获取绑定码、在企微里发给机器人完成自助绑定；或者请他在企微里发「我是
            张三」，名字会显示在列表里，也可以给成员写备注。
          </p>
        </div>
      </div>
      <el-form label-width="170px" class="unbound-form">
        <el-form-item label="未绑定成员默认权限">
          <el-select
            v-model="unbound.username"
            filterable
            clearable
            placeholder="不开放，未绑定的成员需要先绑定"
            @change="onUnboundUserChange"
          >
            <el-option
              v-for="user in unboundCandidates"
              :key="user.id"
              :label="`${user.username} (${ROLE_LABELS[user.role] || user.role})`"
              :value="user.username"
            />
          </el-select>
          <div class="form-tip">
            未绑定的成员按所选花折账号的权限使用机器人（包括按集群的单独授权），可以在「用户与权限」里新建一个专用账号；不能选管理员账号。
          </div>
          <div v-if="unbound.username" class="unbound-access">
            <span class="form-tip">可访问：</span>
            <el-tag
              v-for="item in unboundAccess"
              :key="item.env"
              size="small"
              :type="PERM_TAG[item.permission]"
              >{{ item.env }} · {{ PERM_LABELS[item.permission] }}</el-tag
            >
            <span v-if="!unboundAccess.length" class="form-tip"
              >没有可访问的集群</span
            >
          </div>
        </el-form-item>
        <el-form-item label="默认集群">
          <el-select
            v-model="unbound.default_env"
            :disabled="!unbound.username"
          >
            <el-option :label="NO_CLUSTER_LABEL" :value="NO_CLUSTER" />
            <el-option
              v-for="item in unboundAccess"
              :key="item.env"
              :label="item.env"
              :value="item.env"
            />
          </el-select>
          <div class="form-tip">
            未绑定成员每个新对话从这个集群开始；选「无」时，每个新对话都要先在卡片里选集群（也可以发「切换集群
            名称」，或在提问里说出集群名或别名）。
          </div>
        </el-form-item>
        <div class="form-actions">
          <el-button
            type="primary"
            :loading="unboundSaving"
            :disabled="!unboundDirty"
            @click="saveUnbound"
            >保存默认权限</el-button
          >
        </div>
      </el-form>
      <el-alert
        v-if="unboundUnavailable"
        :title="`默认权限账号 ${unboundSaved.unbound_username} 已停用、已删除或已改为管理员，未绑定的成员现在无法使用机器人，请重新选择。`"
        type="warning"
        :closable="false"
        show-icon
        class="unbound-alert"
      />
      <el-alert
        v-else-if="unboundEnvUnavailable"
        :title="`默认集群 ${unboundSaved.unbound_default_env} 已不在该账号可访问的范围内，未绑定的成员要先切换集群才能提问，请重新选择。`"
        type="warning"
        :closable="false"
        show-icon
        class="unbound-alert"
      />
      <el-table
        v-loading="usersLoading"
        :data="mappings"
        row-key="wecom_userid"
        size="small"
      >
        <el-table-column label="企微成员" min-width="230">
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
            <div class="member-id" :title="row.wecom_userid">
              {{ row.wecom_userid }}
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
              <span v-if="unboundSaved.unbound_username" class="muted"
                >默认权限 {{ unboundSaved.unbound_username }}</span
              >
            </template>
          </template>
        </el-table-column>
        <el-table-column label="默认集群" min-width="140">
          <template #default="{ row }">{{
            row.username || unboundSaved.unbound_username
              ? defaultEnvOf(row) || "无"
              : "—"
          }}</template>
        </el-table-column>
        <el-table-column label="当前集群" min-width="140">
          <template #default="{ row }">{{
            row.current_env || defaultEnvOf(row) || "未选择"
          }}</template>
        </el-table-column>
        <el-table-column label="最后活跃" width="110">
          <template #default="{ row }">{{
            formatTime(row.last_seen_at)
          }}</template>
        </el-table-column>
        <el-table-column prop="pending_approvals" label="待审批" width="80" />
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
        <el-divider content-position="left">绑定企微成员</el-divider>
        <el-form-item label="企业微信 UserID" required>
          <el-input
            v-model="mapping.wecom_userid"
            placeholder="在上方列表点「绑定」带入，或输入成员 UserID"
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
              :label="`${user.username} (${user.role})`"
              :value="user.username"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="默认集群">
          <el-select v-model="mapping.default_env">
            <el-option :label="NO_CLUSTER_LABEL" :value="NO_CLUSTER" />
            <el-option
              v-for="env in availableClusters"
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
          <el-button type="primary" @click="saveMapping">保存绑定</el-button>
        </div>
      </el-form>
    </section>
  </div>
</template>

<style scoped>
.wecom-admin {
  display: grid;
  gap: 20px;
  padding: 20px;
}

.settings-section {
  padding: 4px 0 20px;
  border-bottom: 1px solid var(--el-border-color-lighter);
}

.section-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 18px;
}

.section-heading h2 {
  margin: 0;
  font-size: 18px;
  font-weight: 600;
}

.section-heading p {
  margin: 7px 0 0;
  color: var(--el-text-color-secondary);
  line-height: 1.5;
}

.config-form,
.mapping-form {
  max-width: 820px;
  margin-top: 18px;
}

.unbound-form {
  max-width: 820px;
  margin-bottom: 18px;
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

.form-tip {
  margin-top: 4px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--el-text-color-secondary);
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

/* 企微的加密 UserID 很长:放在名字下面用小字,单行省略,悬停看全文 */
.member-id {
  overflow: hidden;
  font-size: 12px;
  color: var(--el-text-color-secondary);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.form-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}

@media (max-width: 640px) {
  .wecom-admin {
    padding: 12px;
  }

  .section-heading {
    flex-direction: column;
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
