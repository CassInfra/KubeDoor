<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import {
  aiApi,
  type AIProvider,
  type WeComConfig,
  type WeComUser
} from "@/api/ai";
import { getKnownClusters, getUsers, type SystemUser } from "@/api/system";
import {
  PERM_LABELS,
  PERM_TAG,
  ROLE_LABELS,
  roleDefaultPermission
} from "@/utils/permission";

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
  model: ""
});
const mappings = ref<WeComUser[]>([]);
const systemUsers = ref<SystemUser[]>([]);
const clusters = ref<string[]>([]);
const mapping = reactive({ wecom_userid: "", username: "", default_env: "" });
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
/** 已保存的未绑定成员默认权限账号;unbound.username 是正在编辑的值 */
const unboundSaved = ref<string | null>(null);
const unbound = reactive({ username: "" });
/** 管理员账号不能作为默认权限(后端同样会拒绝) */
const unboundCandidates = computed(() =>
  systemUsers.value.filter(
    item => item.enabled && item.role !== "agent" && item.role !== "admin"
  )
);
const unboundUnavailable = computed(
  () =>
    !!unboundSaved.value &&
    !usersLoading.value &&
    !unboundCandidates.value.some(item => item.username === unboundSaved.value)
);
/** 所选账号能访问的集群,让管理员确认默认权限的范围 */
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
let refreshTimer: ReturnType<typeof setInterval> | undefined;

async function loadConfig() {
  const value = await aiApi.wecomConfig();
  config.value = value;
  form.enabled = value.enabled;
  form.bot_id = value.bot_id || "";
  form.base_url = value.base_url || "https://api.openai.com/v1";
  form.model = value.model || "";
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
    const editing = unbound.username !== (unboundSaved.value || "");
    unboundSaved.value = mappingResult.unbound_username ?? null;
    if (!editing) unbound.username = unboundSaved.value || "";
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
    model: form.model.trim()
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
      model: form.model.trim()
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
    const result = await aiApi.saveWecomUnbound(unbound.username || null);
    unboundSaved.value = result.unbound_username;
    unbound.username = result.unbound_username || "";
    ElMessage.success(
      result.unbound_username
        ? `未绑定成员将按 ${result.unbound_username} 的权限使用机器人。`
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
  mapping.default_env = row.default_env || "";
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
      default_env: mapping.default_env || null
    });
    mapping.wecom_userid = "";
    mapping.username = "";
    mapping.default_env = "";
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
      `解除企微成员 ${row.wecom_userid} 的花折账号绑定？`,
      "解除绑定",
      { type: "warning" }
    );
    await aiApi.deleteWecomUser(row.wecom_userid);
    await loadMappings();
    ElMessage.success("成员绑定已解除。");
  } catch (error) {
    if (error !== "cancel")
      ElMessage.error(error instanceof Error ? error.message : "解除绑定失败");
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
  <div class="wecom-admin" v-loading="loading">
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
        </div>
      </div>
      <el-form label-width="170px" class="unbound-form">
        <el-form-item label="未绑定成员默认权限">
          <div class="unbound-row">
            <el-select
              v-model="unbound.username"
              class="unbound-select"
              filterable
              clearable
              placeholder="不开放，未绑定的成员需要先绑定"
            >
              <el-option
                v-for="user in unboundCandidates"
                :key="user.id"
                :label="`${user.username} (${ROLE_LABELS[user.role] || user.role})`"
                :value="user.username"
              />
            </el-select>
            <el-button
              type="primary"
              :loading="unboundSaving"
              :disabled="unbound.username === (unboundSaved || '')"
              @click="saveUnbound"
              >保存</el-button
            >
          </div>
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
      </el-form>
      <el-alert
        v-if="unboundUnavailable"
        :title="`默认权限账号 ${unboundSaved} 已停用、已删除或已改为管理员，未绑定的成员现在无法使用机器人，请重新选择。`"
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
        <el-table-column
          prop="wecom_userid"
          label="企业微信 UserID"
          min-width="220"
          show-overflow-tooltip
        />
        <el-table-column label="花折账号" min-width="150">
          <template #default="{ row }">
            <span v-if="row.username">{{ row.username }}</span>
            <template v-else>
              <el-tag type="warning" size="small">待绑定</el-tag>
              <span v-if="unboundSaved" class="muted"
                >默认权限 {{ unboundSaved }}</span
              >
            </template>
          </template>
        </el-table-column>
        <el-table-column prop="default_env" label="默认集群" min-width="140">
          <template #default="{ row }">{{
            row.default_env || "未设置"
          }}</template>
        </el-table-column>
        <el-table-column prop="current_env" label="当前集群" min-width="140">
          <template #default="{ row }">{{
            row.current_env || row.default_env || "未选择"
          }}</template>
        </el-table-column>
        <el-table-column prop="pending_approvals" label="待审批" width="90" />
        <el-table-column label="操作" width="150" fixed="right">
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
          </template>
        </el-table-column>
      </el-table>
      <el-form label-width="170px" class="mapping-form">
        <el-divider content-position="left">绑定企微成员</el-divider>
        <el-form-item label="企业微信 UserID" required>
          <el-input
            v-model="mapping.wecom_userid"
            placeholder="从上方待绑定成员选择，或输入成员 UserID"
          />
        </el-form-item>
        <el-form-item label="花折账号" required>
          <el-select
            v-model="mapping.username"
            filterable
            clearable
            placeholder="选择已启用的花折账号"
            @change="mapping.default_env = ''"
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
          <el-select
            v-model="mapping.default_env"
            clearable
            placeholder="首次对话使用的集群"
          >
            <el-option
              v-for="env in availableClusters"
              :key="env"
              :label="env"
              :value="env"
            />
          </el-select>
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
}

.unbound-row {
  display: flex;
  gap: 8px;
  width: 100%;
}

.unbound-select {
  flex: 1;
  min-width: 0;
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
