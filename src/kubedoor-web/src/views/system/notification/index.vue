<template>
  <div class="system-notification">
    <el-card>
      <template #header>各类通知的用途</template>
      <el-table :data="NOTICE_ROWS" border size="small">
        <el-table-column prop="name" label="通知" width="130" />
        <el-table-column prop="sender" label="谁发送" width="300" />
        <el-table-column prop="when" label="什么时候发" min-width="360" />
      </el-table>
      <div class="note">{{ NOTICE_NOTE }}</div>
    </el-card>

    <el-card v-loading="loading" class="mt-3">
      <template #header>
        <div class="card-header">
          <span>通知配置</span>
          <span v-if="updatedBy" class="hint inline">
            上次修改：{{ updatedBy }} · {{ formatTime(updatedAt) }}
          </span>
        </div>
      </template>
      <el-alert
        type="info"
        :closable="false"
        class="page-alert"
        title="保存后马上生效，不用重启服务，也不用改各个集群；token 加密保存，页面只显示「已配置」和最后 4 位。"
      />
      <el-alert
        v-if="loadError"
        type="error"
        :closable="false"
        show-icon
        class="page-alert"
        :title="`读取通知配置失败：${loadError}`"
      >
        <el-button size="small" :disabled="busy" @click="load">
          重新加载
        </el-button>
      </el-alert>

      <!-- 全局配置 -->
      <div class="section-header">
        <span class="section-title">全局配置</span>
        <el-tag v-if="globalSaved" type="success" size="small" effect="plain">
          已配置 · {{ TYPE_LABELS[globalSaved.type] }}
        </el-tag>
        <el-tag v-else type="info" size="small" effect="plain">未配置</el-tag>
        <span class="hint inline">
          没有单独配置的集群都用全局配置；没有全局配置时，这些集群不发通知
        </span>
        <el-button
          v-if="globalSaved"
          link
          type="danger"
          class="remove-default"
          :loading="removing"
          :disabled="busy"
          @click="removeDefault"
        >
          删除全局配置
        </el-button>
      </div>
      <el-form
        label-width="120px"
        class="channel-form"
        :disabled="!loaded || busy"
      >
        <el-form-item label="IM 类型">
          <el-select v-model="globalForm.type" class="type-select">
            <el-option
              v-for="item in TYPE_OPTIONS"
              :key="item.value"
              :label="item.label"
              :value="item.value"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="机器人地址">
          <el-input
            v-model="globalForm.token"
            type="password"
            show-password
            autocomplete="new-password"
            :placeholder="tokenPlaceholder(globalForm.type, globalSaved)"
          />
          <div class="hint">{{ typeHint(globalForm.type).tokenHelp }}</div>
        </el-form-item>
        <el-form-item label="默认 @ 人">
          <el-input
            v-model="globalForm.at"
            clearable
            :placeholder="typeHint(globalForm.type).at"
          />
          <div class="hint">
            通知里 @ 这个人，只能填一个；指标告警规则的 annotations 里写了 at
            的，以规则为准
          </div>
        </el-form-item>
        <el-form-item>
          <el-button :loading="globalTest.testing" @click="testGlobal">
            发送测试消息
          </el-button>
          <span
            v-if="globalTest.result"
            :class="['test-result', globalTest.result.type]"
          >
            {{ globalTest.result.text }}
          </span>
        </el-form-item>
      </el-form>

      <!-- 按集群单独配置 -->
      <div class="section-header section-gap">
        <span class="section-title">按集群单独配置</span>
        <span class="hint inline">
          表格里没有的集群用全局配置；每个集群的配置完整独立，不沿用全局的 @ 人
        </span>
      </div>
      <div class="toolbar">
        <el-select
          v-model="newEnv"
          filterable
          clearable
          placeholder="选择要单独配置的集群"
          no-data-text="还没有已注册的集群"
          no-match-text="没有匹配的集群"
          class="env-select"
          :disabled="!loaded || busy"
        >
          <el-option
            v-for="item in envChoices"
            :key="item.value"
            :label="item.label"
            :value="item.value"
            :disabled="item.disabled"
          />
        </el-select>
        <el-button :disabled="!loaded || busy || !newEnv" @click="addRow">
          添加
        </el-button>
      </div>
      <el-table
        :data="rows"
        row-key="key"
        border
        empty-text="没有单独配置的集群，全部用全局配置"
      >
        <el-table-column label="集群" min-width="150">
          <template #default="{ row }">
            <span class="env-name">{{ row.env }}</span>
            <el-tag
              v-if="!row.saved"
              size="small"
              type="warning"
              effect="plain"
              class="new-tag"
            >
              新增
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="IM 类型" width="140">
          <template #default="{ row }">
            <el-select v-model="row.type" :disabled="busy">
              <el-option
                v-for="item in TYPE_OPTIONS"
                :key="item.value"
                :label="item.label"
                :value="item.value"
              />
            </el-select>
          </template>
        </el-table-column>
        <el-table-column label="机器人地址" min-width="320">
          <template #default="{ row }">
            <el-input
              v-model="row.token"
              type="password"
              show-password
              autocomplete="new-password"
              :disabled="busy"
              :placeholder="tokenPlaceholder(row.type, row.saved)"
            />
            <div
              v-if="row.result"
              :class="['test-result', 'block', row.result.type]"
            >
              {{ row.result.text }}
            </div>
          </template>
        </el-table-column>
        <el-table-column label="默认 @ 人" min-width="220">
          <template #default="{ row }">
            <el-input
              v-model="row.at"
              clearable
              :disabled="busy"
              :placeholder="typeHint(row.type).at"
            />
          </template>
        </el-table-column>
        <el-table-column label="操作" width="120">
          <template #default="{ row, $index }">
            <el-button
              link
              type="primary"
              :loading="row.testing"
              :disabled="busy"
              @click="testRow(row)"
            >
              测试
            </el-button>
            <el-button
              link
              type="danger"
              :disabled="busy"
              @click="removeRow($index)"
            >
              删除
            </el-button>
          </template>
        </el-table-column>
      </el-table>

      <div class="actions">
        <el-button
          type="primary"
          :loading="saving"
          :disabled="!loaded || loading || removing"
          @click="save"
        >
          保存
        </el-button>
        <el-button :disabled="busy" @click="load">重置</el-button>
        <el-tag v-if="dirty" type="warning" effect="plain"
          >有未保存的修改</el-tag
        >
      </div>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import dayjs from "dayjs";
import { ElMessageBox } from "element-plus";
import { message } from "@/utils/message";
import {
  type NotificationInput,
  type NotificationSettings,
  type NotifyChannel,
  type NotifyTestRequest,
  getNotificationSettings,
  saveNotificationSettings,
  testNotification
} from "@/api/notification";
import {
  type ChannelForm,
  type ClusterForm,
  type TestView,
  NOTICE_NOTE,
  NOTICE_ROWS,
  TYPE_LABELS,
  TYPE_OPTIONS,
  envOptions,
  formKey,
  formsFromSettings,
  newClusterRow,
  savedDefault,
  settingsPayload,
  testErrorView,
  testRequest,
  testResultView,
  tokenPlaceholder,
  typeHint,
  withoutDefault
} from "./notification-utils";

defineOptions({ name: "SystemNotification" });

/** 测试按钮的状态:全局配置一份,表格每行一份 */
interface TestState {
  testing: boolean;
  result: TestView | null;
}

type ClusterRow = ClusterForm & TestState & { key: number };

const loading = ref(false);
const saving = ref(false);
const removing = ref(false);
// 读取成功过才能保存:读取失败时不能用空表单覆盖已保存的配置
const loaded = ref(false);
const loadError = ref("");
const settings = ref<NotificationSettings | null>(null);
const globalForm = reactive<ChannelForm>({ type: "wecom", token: "", at: "" });
const globalTest = reactive<TestState>({ testing: false, result: null });
const rows = ref<ClusterRow[]>([]);
const newEnv = ref("");
let rowSeq = 0;

const busy = computed(() => loading.value || saving.value || removing.value);
const globalSaved = computed(() => savedDefault(settings.value));
const updatedBy = computed(() => settings.value?.updated_by ?? "");
const updatedAt = computed(() => settings.value?.updated_at ?? "");
const envChoices = computed(() =>
  envOptions(settings.value?.envs ?? [], rows.value)
);

/** 和已保存的配置比,有没有没保存的修改 */
const dirty = computed(() => {
  if (!loaded.value || !settings.value) return false;
  const saved = formsFromSettings(settings.value, globalForm.type);
  return (
    formKey(globalSaved.value, globalForm, rows.value) !==
    formKey(globalSaved.value, saved.global, saved.rows)
  );
});

function formatTime(value: string) {
  return value ? dayjs(value).format("YYYY-MM-DD HH:mm:ss") : "";
}

function errorText(error: unknown, fallback: string) {
  const err = error as any;
  const data = err?.response?.data;
  return (
    data?.message ||
    data?.error ||
    (err?.response?.status ? `HTTP ${err.response.status}` : "") ||
    err?.message ||
    fallback
  );
}

function toRow(form: ClusterForm): ClusterRow {
  return { ...form, key: ++rowSeq, testing: false, result: null };
}

/**
 * 用接口返回的已保存配置刷新页面,机器人地址一律清空。
 * keepRows:只删除了全局配置时保留表格里没保存的修改,只更新每行对应的已保存配置
 */
function applySaved(data: NotificationSettings, keepRows = false) {
  settings.value = data;
  const forms = formsFromSettings(data, globalForm.type);
  Object.assign(globalForm, forms.global);
  globalTest.result = null;
  if (keepRows) {
    const byEnv = new Map((data.clusters ?? []).map(item => [item.env, item]));
    for (const row of rows.value) row.saved = byEnv.get(row.env) ?? null;
  } else {
    rows.value = forms.rows.map(toRow);
    newEnv.value = "";
  }
  loaded.value = true;
}

async function load() {
  if (saving.value || removing.value) return;
  loading.value = true;
  loadError.value = "";
  try {
    const res = await getNotificationSettings();
    if (!res.success) throw new Error(res.message || "读取通知配置失败");
    applySaved(res.data);
  } catch (error) {
    loadError.value = errorText(error, "读取通知配置失败");
  } finally {
    loading.value = false;
  }
}

async function save() {
  if (!loaded.value || busy.value) return;
  let payload: NotificationInput;
  try {
    payload = settingsPayload(globalForm, globalSaved.value, rows.value);
  } catch (error) {
    message((error as Error).message, { type: "warning" });
    return;
  }
  saving.value = true;
  try {
    // 接口返回的错误(比如 422 要求重新填写机器人地址)由 http 封装统一提示
    const res = await saveNotificationSettings(payload);
    if (!res.success) {
      message(res.message || "保存失败", { type: "error" });
      return;
    }
    applySaved(res.data);
    message("已保存，马上生效", { type: "success" });
  } finally {
    saving.value = false;
  }
}

/** 删除全局配置:二次确认后马上生效;按集群的配置原样保留,表格里没保存的修改也保留 */
async function removeDefault() {
  if (!loaded.value || busy.value || !globalSaved.value || !settings.value)
    return;
  try {
    await ElMessageBox.confirm(
      "删除后马上生效：没有单独配置的集群不再发送任何通知（K8S 事件告警、运维操作通知、指标告警、管控表通知）；单独配置了的集群不受影响。",
      "删除全局配置",
      { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" }
    );
  } catch {
    return;
  }
  removing.value = true;
  try {
    const res = await saveNotificationSettings(withoutDefault(settings.value));
    if (!res.success) {
      message(res.message || "删除失败", { type: "error" });
      return;
    }
    applySaved(res.data, true);
    message("已删除全局配置", { type: "success" });
  } finally {
    removing.value = false;
  }
}

function addRow() {
  const env = newEnv.value;
  if (!env || rows.value.some(row => row.env === env)) return;
  rows.value.push(toRow(newClusterRow(env, settings.value, globalForm)));
  newEnv.value = "";
}

function removeRow(index: number) {
  rows.value.splice(index, 1);
}

/** 发测试消息:已填的机器人地址用填的值,没填用已保存的 */
async function runTest(
  env: string,
  form: ChannelForm,
  saved: NotifyChannel | null,
  state: TestState
) {
  if (state.testing) return;
  let request: NotifyTestRequest;
  try {
    request = testRequest(env, form, saved);
  } catch (error) {
    message((error as Error).message, { type: "warning" });
    return;
  }
  state.testing = true;
  let view: TestView | null = null;
  // true:请求成功,结果是 IM 接口的返回;false:请求本身出错(429、校验不通过、超时等)
  let fromIm = false;
  try {
    const res = await testNotification(request);
    if (res.success) {
      view = testResultView(res.data);
      fromIm = true;
    } else {
      view = { type: "error", text: res.message || "发送测试消息失败" };
    }
  } catch (error) {
    view = testErrorView(error);
  } finally {
    state.testing = false;
  }
  if (!view) return;
  state.result = view;
  if (!fromIm) message(view.text, { type: view.type });
  else if (view.type === "success")
    message("发送成功，请到群里查看", { type: "success" });
  else message("发送失败，原因见页面上的测试结果", { type: "warning" });
}

function testGlobal() {
  return runTest("", globalForm, globalSaved.value, globalTest);
}

function testRow(row: ClusterRow) {
  return runTest(row.env, row, row.saved, row);
}

onMounted(load);
</script>

<style lang="scss" scoped>
.system-notification {
  max-width: 1280px;
  padding: 4px;
}

.card-header {
  display: flex;
  gap: 12px;
  align-items: center;
  justify-content: space-between;
}

.note {
  margin-top: 8px;
  font-size: 13px;
  line-height: 1.8;
  color: var(--el-text-color-regular);
}

// el-alert 自带 margin: 0,工具类 mb-4 压不过,用带作用域的类
.page-alert {
  margin-bottom: 16px;
}

.section-header {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  margin-bottom: 12px;
}

.section-gap {
  padding-top: 16px;
  margin-top: 8px;
  border-top: 1px solid var(--el-border-color-lighter);
}

.section-title {
  font-size: 15px;
  font-weight: 600;
}

.remove-default {
  margin-left: auto;
}

// 表单项里的提示独占一行(在输入框下面);inline 的跟在标题后面
.hint {
  display: block;
  width: 100%;
  font-size: 12px;
  line-height: 1.6;
  color: var(--el-text-color-secondary);

  &.inline {
    display: inline;
    width: auto;
  }
}

.channel-form {
  max-width: 760px;
}

.type-select {
  width: 200px;
}

.toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  margin-bottom: 12px;
}

.env-select {
  width: 280px;
}

.env-name {
  word-break: break-all;
}

.new-tag {
  margin-left: 6px;
}

.test-result {
  flex: 1;
  min-width: 0;
  margin-left: 12px;
  font-size: 13px;
  line-height: 1.6;
  word-break: break-all;

  // 表格里的结果放在机器人地址下面
  &.block {
    display: block;
    margin: 4px 0 0;
    font-size: 12px;
  }

  &.success {
    color: var(--el-color-success);
  }

  &.warning {
    color: var(--el-color-warning);
  }

  &.error {
    color: var(--el-color-danger);
  }
}

// 按钮之间沿用 el-button 自带的间距,这里不用 gap
.actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  margin-top: 16px;

  .el-tag {
    margin-left: 12px;
  }
}
</style>
