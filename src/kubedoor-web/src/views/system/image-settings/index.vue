<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { onBeforeRouteLeave } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";
import dayjs from "dayjs";
import {
  getImageRegistries,
  getImageUpdateRules,
  saveImageRegistries,
  saveImageUpdateRules,
  type ImageRegistrySettings,
  type ImageUpdateRule,
  type ImageUpdateSettings
} from "@/api/image-settings";
import { getKnownClusters, getUsers } from "@/api/system";
import {
  registriesPayload,
  rulesPayload,
  timeSummary,
  type RegistryForm
} from "./settings-utils";

defineOptions({ name: "SystemImageSettings" });

type RuleRow = ImageUpdateRule & { key: number; fixedDefault: boolean };
type RegistryRow = RegistryForm & { key: number };
let sequence = 0;
const previousEndTimes = new Map<number, string>();
const tab = ref("rules");
const rules = ref<RuleRow[]>([]);
const registries = ref<RegistryRow[]>([]);
const clusters = ref<string[]>([]);
const users = ref<Array<{ value: string; label: string }>>([]);
const rulesLoading = ref(false);
const registriesLoading = ref(false);
const rulesSaving = ref(false);
const registriesSaving = ref(false);
const rulesLoaded = ref(false);
const registriesLoaded = ref(false);
const rulesError = ref("");
const registriesError = ref("");
const rulesSnapshot = ref("");
const registriesSnapshot = ref("");
const rulesMeta = ref({ updated_by: "", updated_at: "" });
const registriesMeta = ref({ updated_by: "", updated_at: "" });

const rulesState = () =>
  JSON.stringify(rules.value.map(({ key, fixedDefault, ...rule }) => rule));
const registryState = () =>
  JSON.stringify(registries.value.map(({ key, ...item }) => item));
const rulesDirty = computed(
  () => rulesLoaded.value && rulesState() !== rulesSnapshot.value
);
const registriesDirty = computed(
  () => registriesLoaded.value && registryState() !== registriesSnapshot.value
);
const dirty = computed(() => rulesDirty.value || registriesDirty.value);
const busy = computed(() => rulesSaving.value || registriesSaving.value);

function errorText(reason: unknown): string {
  const error = reason as any;
  return (
    error?.response?.data?.message ||
    error?.response?.data?.error ||
    error?.message ||
    "请求失败，请稍后重试"
  );
}

function modified(meta: { updated_by: string; updated_at: string }): string {
  if (!meta.updated_at) return "尚未保存";
  return `上次修改：${meta.updated_by || "系统"} · ${dayjs(meta.updated_at).format("YYYY-MM-DD HH:mm:ss")}`;
}

function applyRules(data: ImageUpdateSettings) {
  previousEndTimes.clear();
  rules.value = data.rules.map(rule => ({
    ...rule,
    users: [...rule.users],
    fixedDefault: rule.env === "default",
    key: ++sequence
  }));
  rulesMeta.value = {
    updated_by: data.updated_by || "",
    updated_at: data.updated_at || ""
  };
  rulesLoaded.value = true;
  rulesSnapshot.value = rulesState();
}

function applyRegistries(data: ImageRegistrySettings) {
  registries.value = data.registries.map(item => ({
    ...item,
    ak: "",
    sk: "",
    key: ++sequence
  }));
  registriesMeta.value = {
    updated_by: data.updated_by || "",
    updated_at: data.updated_at || ""
  };
  registriesLoaded.value = true;
  registriesSnapshot.value = registryState();
}

async function confirmDiscard(): Promise<boolean> {
  try {
    await ElMessageBox.confirm("尚有未保存的修改，是否放弃？", "未保存的修改", {
      confirmButtonText: "放弃修改",
      cancelButtonText: "继续编辑",
      type: "warning"
    });
    return true;
  } catch {
    return false;
  }
}

async function loadRules(reset = false) {
  if (rulesSaving.value || rulesLoading.value) return;
  if (reset && rulesDirty.value && !(await confirmDiscard())) return;
  rulesLoading.value = true;
  rulesError.value = "";
  try {
    const response = await getImageUpdateRules();
    if (!response.success)
      throw new Error(response.message || "读取镜像更新规则失败");
    applyRules(response.data);
  } catch (reason) {
    rulesError.value = errorText(reason);
  } finally {
    rulesLoading.value = false;
  }
}

async function loadRegistries(reset = false) {
  if (registriesSaving.value || registriesLoading.value) return;
  if (reset && registriesDirty.value && !(await confirmDiscard())) return;
  registriesLoading.value = true;
  registriesError.value = "";
  try {
    const response = await getImageRegistries();
    if (!response.success)
      throw new Error(response.message || "读取仓库凭据失败");
    applyRegistries(response.data);
  } catch (reason) {
    registriesError.value = errorText(reason);
  } finally {
    registriesLoading.value = false;
  }
}

async function saveRules() {
  if (!rulesLoaded.value || rulesSaving.value || rulesLoading.value) return;
  let payload: ImageUpdateRule[];
  try {
    payload = rulesPayload(rules.value);
  } catch (reason) {
    ElMessage.warning(errorText(reason));
    return;
  }
  rulesSaving.value = true;
  try {
    const response = await saveImageUpdateRules(payload);
    if (!response.success)
      throw new Error(response.message || "保存镜像更新规则失败");
    applyRules(response.data);
    ElMessage.success("镜像更新规则已保存并生效");
  } catch (reason) {
    ElMessage.error(errorText(reason));
  } finally {
    rulesSaving.value = false;
  }
}

async function saveRegistries() {
  if (
    !registriesLoaded.value ||
    registriesSaving.value ||
    registriesLoading.value
  )
    return;
  let payload;
  try {
    payload = registriesPayload(registries.value);
  } catch (reason) {
    ElMessage.warning(errorText(reason));
    return;
  }
  registriesSaving.value = true;
  try {
    const response = await saveImageRegistries(payload);
    if (!response.success)
      throw new Error(response.message || "保存仓库凭据失败");
    applyRegistries(response.data);
    ElMessage.success("镜像仓库凭据已保存并生效");
  } catch (reason) {
    ElMessage.error(errorText(reason));
  } finally {
    registriesSaving.value = false;
  }
}

function addRule() {
  rules.value.push({
    key: ++sequence,
    fixedDefault: false,
    env: "",
    enabled: false,
    start_time: "20:00",
    end_time: "08:00",
    users: []
  });
}

function toggleEndOfDay(rule: RuleRow, checked: string | number | boolean) {
  if (checked === true) {
    if (rule.end_time !== "24:00")
      previousEndTimes.set(rule.key, rule.end_time);
    rule.end_time = "24:00";
  } else {
    rule.end_time = previousEndTimes.get(rule.key) || "08:00";
  }
}

function setAllDay(rule: RuleRow) {
  rule.start_time = "00:00";
  toggleEndOfDay(rule, true);
}

function addRegistry() {
  registries.value.push({
    key: ++sequence,
    id: "",
    domain: "",
    env: "default",
    ak: "",
    sk: "",
    has_ak: false,
    has_sk: false
  });
}

async function loadChoices() {
  const results = await Promise.allSettled([getKnownClusters(), getUsers()]);
  const [clusterResult, userResult] = results;
  if (clusterResult.status === "fulfilled" && clusterResult.value.success)
    clusters.value = clusterResult.value.data.map(item => item.env);
  if (userResult.status === "fulfilled" && userResult.value.success)
    users.value = userResult.value.data
      .filter(user => user.enabled && user.role !== "agent")
      .map(user => ({
        value: user.username,
        label: user.display_name
          ? `${user.display_name}（${user.username}）`
          : user.username
      }));
}

function beforeUnload(event: BeforeUnloadEvent) {
  if (!dirty.value && !busy.value) return;
  event.preventDefault();
  event.returnValue = "";
}

onBeforeRouteLeave(async () => {
  if (busy.value) {
    ElMessage.info("正在保存，请稍候");
    return false;
  }
  return !dirty.value || (await confirmDiscard());
});
onMounted(() => {
  void loadRules();
  void loadRegistries();
  void loadChoices();
  window.addEventListener("beforeunload", beforeUnload);
});
onBeforeUnmount(() => window.removeEventListener("beforeunload", beforeUnload));
</script>

<template>
  <div class="image-settings">
    <div class="page-heading">
      <div>
        <h2>镜像管理</h2>
        <p>管理镜像更新的额外放行规则，以及查询镜像版本使用的仓库凭据。</p>
      </div>
    </div>
    <el-tabs v-model="tab" class="settings-tabs">
      <el-tab-pane label="镜像更新规则" name="rules">
        <el-alert type="info" :closable="false" show-icon class="intro">
          <template #title
            >为只读用户按集群、时段和账号额外放行镜像更新</template
          >
          <p>
            已有「更新镜像」权限的用户按「权限与审批」执行；关闭该操作的管理员权限后，额外规则也不放行。
            这些规则仅用于网页操作，AI 不使用此白名单。
          </p>
          <p>
            集群专属规则优先，未配置的集群使用默认规则。时间按北京时间计算，包含开始时刻、不含结束时刻，支持跨天。
          </p>
        </el-alert>
        <div class="toolbar">
          <el-button
            :disabled="!rulesLoaded || rulesSaving || rulesLoading"
            @click="addRule"
            >添加集群规则</el-button
          >
          <div class="toolbar-actions">
            <el-tag v-if="rulesDirty" type="warning" effect="plain"
              >未保存</el-tag
            >
            <el-button
              :disabled="rulesSaving || rulesLoading"
              @click="loadRules(true)"
              >重新加载</el-button
            >
            <el-button
              type="primary"
              :loading="rulesSaving"
              :disabled="!rulesDirty || rulesLoading"
              @click="saveRules"
              >保存规则</el-button
            >
          </div>
        </div>
        <el-alert
          v-if="rulesError"
          :title="rulesError"
          type="error"
          :closable="false"
          show-icon
          class="load-error"
        />
        <div v-loading="rulesLoading" class="records">
          <el-empty
            v-if="!rulesLoaded && !rulesLoading"
            description="尚未加载规则，请重新加载"
          />
          <el-card
            v-for="(rule, index) in rules"
            :key="rule.key"
            shadow="never"
            class="record-card"
          >
            <template #header>
              <div class="record-header">
                <div class="record-title">
                  <strong>{{
                    rule.env === "default"
                      ? "默认规则"
                      : rule.env || "新集群规则"
                  }}</strong>
                  <el-tag v-if="rule.env === 'default'" size="small" type="info"
                    >未单独配置的集群</el-tag
                  >
                  <span class="time-summary">{{ timeSummary(rule) }}</span>
                </div>
                <el-button
                  v-if="!rule.fixedDefault"
                  link
                  type="danger"
                  :disabled="rulesSaving || rulesLoading"
                  @click="rules.splice(index, 1)"
                  >删除</el-button
                >
              </div>
            </template>
            <el-form
              label-position="top"
              :disabled="rulesSaving || rulesLoading"
              class="rule-form"
            >
              <el-form-item label="适用集群" required>
                <el-input
                  v-if="rule.fixedDefault"
                  model-value="默认规则"
                  disabled
                />
                <el-select
                  v-else
                  v-model="rule.env"
                  filterable
                  allow-create
                  default-first-option
                  placeholder="选择或输入集群标识"
                >
                  <el-option
                    v-for="env in clusters"
                    :key="env"
                    :label="env"
                    :value="env"
                  />
                </el-select>
              </el-form-item>
              <el-form-item label="额外放行">
                <el-switch
                  v-model="rule.enabled"
                  active-text="启用"
                  inactive-text="停用"
                />
              </el-form-item>
              <el-form-item label="允许开始时间" :required="rule.enabled">
                <el-time-picker
                  v-model="rule.start_time"
                  value-format="HH:mm"
                  format="HH:mm"
                  placeholder="开始时间"
                  :disabled="!rule.enabled"
                  :clearable="false"
                />
              </el-form-item>
              <el-form-item label="允许结束时间" :required="rule.enabled">
                <el-input
                  v-if="rule.end_time === '24:00'"
                  model-value="24:00"
                  disabled
                />
                <el-time-picker
                  v-else
                  v-model="rule.end_time"
                  value-format="HH:mm"
                  format="HH:mm"
                  placeholder="结束时间"
                  :disabled="!rule.enabled"
                  :clearable="false"
                />
                <div class="time-options">
                  <el-checkbox
                    :model-value="rule.end_time === '24:00'"
                    :disabled="!rule.enabled"
                    @change="toggleEndOfDay(rule, $event)"
                    >至当天 24:00</el-checkbox
                  >
                  <el-button
                    link
                    type="primary"
                    :disabled="!rule.enabled"
                    @click="setAllDay(rule)"
                    >设为全天</el-button
                  >
                </div>
              </el-form-item>
              <el-form-item
                class="users-field"
                label="允许用户"
                :required="rule.enabled"
              >
                <el-select
                  v-model="rule.users"
                  multiple
                  filterable
                  allow-create
                  default-first-option
                  :reserve-keyword="false"
                  :disabled="!rule.enabled"
                  placeholder="选择用户，也可输入用户名后按回车"
                >
                  <el-option
                    v-for="user in users"
                    :key="user.value"
                    :label="user.label"
                    :value="user.value"
                  />
                </el-select>
              </el-form-item>
            </el-form>
          </el-card>
        </div>
        <p v-if="rulesLoaded" class="settings-meta">
          {{ modified(rulesMeta) }}。新增、修改和删除均在保存后生效。
        </p>
      </el-tab-pane>

      <el-tab-pane label="镜像仓库凭据" name="registries">
        <el-alert type="info" :closable="false" show-icon class="intro">
          <template #title>用于查询 Deployment 镜像的可用版本</template>
          <p>
            华为 SWR、阿里 ACR 填 Access Key / Secret Key，Harbor 填用户名 /
            密码。集群专属凭据优先，未配置的集群使用该仓库的默认凭据。
          </p>
          <p>
            已保存的凭据不会回显；编辑时留空即可保留原值。删除记录并保存后，会移除该组凭据。
          </p>
        </el-alert>
        <div class="toolbar">
          <el-button
            :disabled="
              !registriesLoaded || registriesSaving || registriesLoading
            "
            @click="addRegistry"
            >添加仓库凭据</el-button
          >
          <div class="toolbar-actions">
            <el-tag v-if="registriesDirty" type="warning" effect="plain"
              >未保存</el-tag
            >
            <el-button
              :disabled="registriesSaving || registriesLoading"
              @click="loadRegistries(true)"
              >重新加载</el-button
            >
            <el-button
              type="primary"
              :loading="registriesSaving"
              :disabled="!registriesDirty || registriesLoading"
              @click="saveRegistries"
              >保存凭据</el-button
            >
          </div>
        </div>
        <el-alert
          v-if="registriesError"
          :title="registriesError"
          type="error"
          :closable="false"
          show-icon
          class="load-error"
        />
        <div v-loading="registriesLoading" class="records">
          <el-empty
            v-if="!registriesLoading && !registries.length"
            :description="
              registriesLoaded
                ? '尚未配置镜像仓库凭据'
                : '尚未加载凭据，请重新加载'
            "
          />
          <el-card
            v-for="(registry, index) in registries"
            :key="registry.key"
            shadow="never"
            class="record-card"
          >
            <template #header>
              <div class="record-header">
                <div class="record-title">
                  <strong>{{ registry.domain || "新仓库凭据" }}</strong>
                  <el-tag
                    size="small"
                    :type="registry.env === 'default' ? 'info' : 'primary'"
                    >{{
                      registry.env === "default"
                        ? "默认凭据"
                        : registry.env || "未选集群"
                    }}</el-tag
                  >
                </div>
                <el-button
                  link
                  type="danger"
                  :disabled="registriesSaving || registriesLoading"
                  @click="registries.splice(index, 1)"
                  >删除</el-button
                >
              </div>
            </template>
            <el-form
              label-position="top"
              :disabled="registriesSaving || registriesLoading"
              class="registry-form"
            >
              <el-form-item label="仓库域名" required>
                <el-input
                  v-model="registry.domain"
                  placeholder="例如 reg.example.com 或 reg.example.com:5000"
                  autocomplete="off"
                />
                <p class="field-hint">
                  只填写域名和端口，不含 https://、项目或镜像路径。
                </p>
              </el-form-item>
              <el-form-item label="适用集群" required>
                <el-select
                  v-model="registry.env"
                  filterable
                  allow-create
                  default-first-option
                  placeholder="选择或输入集群标识"
                >
                  <el-option
                    label="默认凭据（未单独配置的集群）"
                    value="default"
                  />
                  <el-option
                    v-for="env in clusters"
                    :key="env"
                    :label="env"
                    :value="env"
                  />
                </el-select>
              </el-form-item>
              <el-form-item label="用户名 / Access Key" required>
                <el-input
                  v-model="registry.ak"
                  type="password"
                  show-password
                  autocomplete="new-password"
                  :placeholder="
                    registry.has_ak
                      ? '已设置，留空保持不变'
                      : '填写用户名或 Access Key'
                  "
                />
              </el-form-item>
              <el-form-item label="密码 / Secret Key" required>
                <el-input
                  v-model="registry.sk"
                  type="password"
                  show-password
                  autocomplete="new-password"
                  :placeholder="
                    registry.has_sk
                      ? '已设置，留空保持不变'
                      : '填写密码或 Secret Key'
                  "
                />
              </el-form-item>
            </el-form>
          </el-card>
        </div>
        <p v-if="registriesLoaded" class="settings-meta">
          {{ modified(registriesMeta) }}。保存后下次查询镜像版本即使用新凭据。
        </p>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<style scoped lang="scss">
.image-settings {
  padding: 16px;
}

.page-heading {
  margin-bottom: 20px;

  h2 {
    margin: 0 0 6px;
    font-size: 22px;
    font-weight: 600;
  }

  p {
    margin: 0;
    color: var(--el-text-color-secondary);
  }
}

.intro p {
  margin: 6px 0 0;
  line-height: 1.7;
}

.toolbar,
.toolbar-actions,
.record-header,
.record-title {
  display: flex;
  gap: 12px;
  align-items: center;
}

.toolbar,
.record-header {
  justify-content: space-between;
}

.toolbar {
  flex-wrap: wrap;
  margin: 18px 0;
}

.toolbar-actions,
.record-title {
  flex-wrap: wrap;
}

.records {
  min-height: 100px;
}

.load-error,
.record-card {
  margin-bottom: 14px;
}

.time-summary,
.field-hint,
.settings-meta {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.field-hint {
  margin: 4px 0 0;
  line-height: 1.5;
}

.time-options {
  display: flex;
  flex-wrap: wrap;
  gap: 0 10px;
  align-items: center;
}

.rule-form {
  display: grid;
  grid-template-columns: minmax(200px, 2fr) minmax(140px, 1fr) repeat(
      2,
      minmax(140px, 1fr)
    );
  gap: 0 20px;

  .users-field {
    grid-column: 1 / -1;
  }
}

.registry-form {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0 20px;
}

:deep(.el-select),
:deep(.el-date-editor.el-input) {
  width: 100%;
}

@media (max-width: 1000px) {
  .rule-form {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 640px) {
  .image-settings {
    padding: 8px;
  }

  .toolbar-actions {
    width: 100%;
    justify-content: flex-end;
  }

  .rule-form,
  .registry-form {
    grid-template-columns: minmax(0, 1fr);
  }

  .record-title {
    gap: 6px;
  }
}
</style>
