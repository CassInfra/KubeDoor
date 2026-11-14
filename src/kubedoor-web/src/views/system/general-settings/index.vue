<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from "vue";
import { onBeforeRouteLeave } from "vue-router";
import { ElMessageBox } from "element-plus";
import dayjs from "dayjs";
import { message } from "@/utils/message";
import {
  type GeneralSettingKey,
  type GeneralSettings,
  type MetricsTestRequest,
  getGeneralSettings,
  saveGeneralSettings,
  testMetricsSettings
} from "@/api/general-settings";
import {
  type CardId,
  type FormContext,
  type FormState,
  type TestView,
  CARDS,
  FIELDS,
  cardErrors,
  cardOf,
  cardPayload,
  changedKeys,
  defaultFormValue,
  defaultText,
  defaultsOf,
  emptyForm,
  errorText,
  fieldHint,
  formFromValues,
  fromHttp,
  hasMaskedPassword,
  keyChanged,
  mergeForm,
  metricsTestRequest,
  placeholderOf,
  problemList,
  restorePending,
  testErrorView,
  testResultView,
  updatedByLabel
} from "./general-settings-utils";

defineOptions({ name: "SystemGeneralSettings" });

const loading = ref(false);
// 正在保存的卡片；同一时间只保存一张，避免两次保存的返回先后颠倒
const saving = ref<CardId | "">("");
const testing = ref(false);
// 读取成功过才能保存：读取失败时不能用默认值覆盖已保存的配置
const loaded = ref(false);
const loadError = ref("");
const settings = ref<GeneralSettings | null>(null);
// 不同类型的输入框共用一个表单对象，模板里按配置项的类型绑定
const form = reactive<Record<GeneralSettingKey, any>>(emptyForm());
const initial = ref<FormState>(emptyForm());
// 点了「恢复默认」的项：保存时提交 null
const restores = reactive(new Set<GeneralSettingKey>());
// 点过保存但没通过校验的卡片：之后边改边显示校验结果
const attempted = reactive(new Set<CardId>());
// 保存失败的原因（以服务端返回的为准），显示在卡片里
const serverErrors = reactive<Record<CardId, string>>(
  Object.fromEntries(CARDS.map(card => [card.id, ""])) as Record<CardId, string>
);
const expanded = reactive<Record<CardId, boolean>>(
  Object.fromEntries(CARDS.map(card => [card.id, !card.collapsed])) as Record<
    CardId,
    boolean
  >
);
const testResult = ref<TestView | null>(null);

const defaults = computed(() => defaultsOf(settings.value));
const context = (): FormContext => ({
  form,
  initial: initial.value,
  restores,
  defaults: defaults.value
});
const cardDirty = computed(
  () =>
    Object.fromEntries(
      CARDS.map(card => [
        card.id,
        loaded.value && changedKeys(card.id, context()).length > 0
      ])
    ) as Record<CardId, boolean>
);
const dirty = computed(() => CARDS.some(card => cardDirty.value[card.id]));
const busy = computed(() => loading.value || !!saving.value);
const fieldErrors = computed(() => {
  const errors: Partial<Record<GeneralSettingKey, string>> = {};
  for (const id of attempted) Object.assign(errors, cardErrors(id, context()));
  return errors;
});
const problems = computed(() => settings.value?.problems ?? {});
const problemItems = computed(() => problemList(problems.value));
const stored = computed(() => new Set(settings.value?.stored ?? []));
const modifiedText = computed(() => {
  const data = settings.value;
  if (!data?.updated_at) return "尚未保存过，全部使用默认值";
  return `上次修改：${updatedByLabel(data.updated_by)} · ${dayjs(data.updated_at).format("YYYY-MM-DD HH:mm:ss")}`;
});

const isStored = (key: GeneralSettingKey) => stored.value.has(key);
const pending = (key: GeneralSettingKey) => restorePending(key, context());
const changed = (key: GeneralSettingKey) =>
  loaded.value && keyChanged(key, context());
const canRestore = (key: GeneralSettingKey) => isStored(key) && !pending(key);

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

/**
 * 用接口返回的配置刷新页面。savedKeys 为 null 表示重新读取（全部刷新）；
 * 否则只刷新刚保存的项和没改动的项，其他卡片里没保存的修改保留。
 */
function apply(data: GeneralSettings, savedKeys: GeneralSettingKey[] | null) {
  const fresh = formFromValues(data.values, defaultsOf(data));
  const next = mergeForm(context(), fresh, savedKeys);
  settings.value = data;
  Object.assign(form, next.form);
  initial.value = next.initial;
  for (const key of [...restores])
    if (!next.keep.includes(key)) restores.delete(key);
  if (!savedKeys) {
    attempted.clear();
    for (const card of CARDS) serverErrors[card.id] = "";
  }
  loaded.value = true;
  // 默认折叠的卡片里有项出问题时展开
  for (const card of CARDS)
    if (card.keys.some(key => data.problems?.[key])) expanded[card.id] = true;
}

async function load(reset = false) {
  if (busy.value) return;
  if (reset && dirty.value && !(await confirmDiscard())) return;
  loading.value = true;
  loadError.value = "";
  try {
    const res = await getGeneralSettings();
    if (!res.success) throw new Error(res.message || "读取通用配置失败");
    apply(res.data, null);
    testResult.value = null;
  } catch (reason) {
    loadError.value = errorText(reason, "读取通用配置失败");
  } finally {
    loading.value = false;
  }
}

async function saveCard(id: CardId) {
  if (!loaded.value || busy.value) return;
  const ctx = context();
  if (!changedKeys(id, ctx).length) return;
  const errors = cardErrors(id, ctx);
  const first = cardOf(id).keys.find(key => errors[key]);
  if (first) {
    attempted.add(id);
    expanded[id] = true;
    message(`${FIELDS[first].label}：${errors[first]}`, { type: "warning" });
    return;
  }
  const payload = cardPayload(id, ctx);
  saving.value = id;
  serverErrors[id] = "";
  try {
    const res = await saveGeneralSettings(payload);
    if (!res.success) throw new Error(res.message || "保存失败");
    attempted.delete(id);
    apply(res.data, Object.keys(payload) as GeneralSettingKey[]);
    message(`已保存「${cardOf(id).title}」`, { type: "success" });
  } catch (reason) {
    // 接口返回的错误（例如 422 某一项不合法）http 封装已经弹过提示，这里只在卡片里留一份原因
    serverErrors[id] = errorText(reason, "保存失败");
    if (!fromHttp(reason)) message(serverErrors[id], { type: "error" });
  } finally {
    saving.value = "";
  }
}

/** 恢复默认：表单里先换成默认值，保存这张卡片时提交 null */
function restoreDefault(key: GeneralSettingKey) {
  form[key] = defaultFormValue(key, defaults.value);
  restores.add(key);
}

function undoRestore(key: GeneralSettingKey) {
  restores.delete(key);
  form[key] = initial.value[key];
}

function toggleCard(id: CardId) {
  expanded[id] = !expanded[id];
}

/** 测试连接：用表单里当前的查询地址和标签 key，不用先保存 */
async function runMetricsTest() {
  if (!loaded.value || testing.value) return;
  let request: MetricsTestRequest;
  try {
    request = metricsTestRequest(context());
  } catch (reason) {
    message((reason as Error).message, { type: "warning" });
    return;
  }
  testing.value = true;
  testResult.value = null;
  let view: TestView | null = null;
  // true：请求成功，结果是时序库的检测结果；false：请求本身出错（429、地址不合法、超时等）
  let fromServer = false;
  try {
    const res = await testMetricsSettings(request);
    if (res.success) {
      view = testResultView(res.data);
      fromServer = true;
    } else {
      view = { type: "error", text: res.message || "测试失败" };
    }
  } catch (reason) {
    view = testErrorView(reason);
  } finally {
    testing.value = false;
  }
  if (!view) return;
  testResult.value = view;
  if (!fromServer) message(view.text, { type: view.type });
  else if (view.type === "success") message("连接正常", { type: "success" });
  else if (view.type === "warning")
    message("连接正常，但有需要留意的地方，见页面上的测试结果", {
      type: "warning"
    });
  else message("测试未通过，原因见页面上的测试结果", { type: "warning" });
}

function beforeUnload(event: BeforeUnloadEvent) {
  if (!dirty.value && !busy.value) return;
  event.preventDefault();
  event.returnValue = "";
}

onBeforeRouteLeave(async () => {
  if (saving.value) {
    message("正在保存，请稍候", { type: "info" });
    return false;
  }
  return !dirty.value || (await confirmDiscard());
});
onMounted(() => {
  void load();
  window.addEventListener("beforeunload", beforeUnload);
});
onBeforeUnmount(() => window.removeEventListener("beforeunload", beforeUnload));
</script>

<template>
  <div class="general-settings">
    <div class="page-heading">
      <div>
        <h2>通用配置</h2>
        <p>
          KubeDoor
          运行时使用的配置，保存在数据库里，保存后马上生效，不用重启服务。每张卡片单独保存，只提交这张卡片里改过的项；标着「默认」的项没有单独设置过，跟随默认值。
        </p>
      </div>
      <div class="heading-actions">
        <span v-if="loaded" class="settings-meta">{{ modifiedText }}</span>
        <el-button :disabled="busy" @click="load(true)">重新加载</el-button>
      </div>
    </div>

    <el-alert
      v-if="loadError"
      type="error"
      :closable="false"
      show-icon
      class="page-alert"
      :title="`读取通用配置失败：${loadError}`"
    />
    <el-alert
      v-if="problemItems.length"
      type="warning"
      :closable="false"
      show-icon
      class="page-alert"
      title="下面这些配置项需要处理"
    >
      <ul class="problem-list">
        <li v-for="item in problemItems" :key="item.key">
          {{ item.label }}：{{ item.message }}
        </li>
      </ul>
    </el-alert>

    <div v-loading="loading" class="cards">
      <el-empty
        v-if="!loaded && !loading"
        description="还没有读取到配置，请点「重新加载」"
      />
      <template v-if="loaded">
        <el-card
          v-for="card in CARDS"
          :key="card.id"
          shadow="never"
          class="settings-card"
          :body-style="expanded[card.id] ? undefined : { padding: '0' }"
        >
          <template #header>
            <div class="card-header">
              <div
                :class="['card-title', { clickable: card.collapsed }]"
                @click="card.collapsed && toggleCard(card.id)"
              >
                <strong>{{ card.title }}</strong>
                <el-tag
                  v-if="cardDirty[card.id]"
                  type="warning"
                  effect="plain"
                  size="small"
                  >未保存</el-tag
                >
                <span v-if="card.collapsed && !expanded[card.id]" class="hint">
                  一般不需要修改，点击展开
                </span>
              </div>
              <div class="card-actions">
                <el-button
                  v-if="card.collapsed"
                  link
                  type="primary"
                  @click="toggleCard(card.id)"
                  >{{ expanded[card.id] ? "收起" : "展开" }}</el-button
                >
                <el-button
                  type="primary"
                  :loading="saving === card.id"
                  :disabled="!cardDirty[card.id] || busy"
                  @click="saveCard(card.id)"
                  >保存</el-button
                >
              </div>
            </div>
          </template>

          <div v-show="expanded[card.id]">
            <el-alert
              v-if="serverErrors[card.id]"
              type="error"
              :closable="false"
              show-icon
              class="card-alert"
              :title="`保存失败：${serverErrors[card.id]}`"
            />
            <el-alert
              v-if="card.id === 'metrics'"
              type="warning"
              :closable="false"
              show-icon
              class="card-alert"
              title="只给 KubeDoor 自己用，修改不会同步到 Grafana、vmalert 和已安装集群"
            >
              这里的配置给 KubeDoor 自己用（查询指标、以后网页安装的 Agent
              写入指标）。Grafana 数据源、vmalert 和已安装集群的 vmagent
              用的是安装时写入的地址，修改这里不会跟着变，需要按部署文档手动同步。
            </el-alert>
            <p v-else-if="card.id === 'agent'" class="card-note">
              网页安装 Agent 时使用这些镜像，修改不影响已经安装的集群。推荐的
              Agent 版本跟随控制端：<b>{{
                settings?.recommended_agent_tag || "-"
              }}</b>
            </p>
            <p v-else-if="card.id === 'retention'" class="card-note">
              0 = 不清理；每天清理一轮。
            </p>
            <p v-else-if="card.id === 'auth'" class="card-note">
              修改后对之后的登录生效。
            </p>

            <el-form
              label-position="top"
              class="settings-form"
              :disabled="busy"
              @submit.prevent
            >
              <el-form-item
                v-for="key in card.keys"
                :key="key"
                :class="['setting-item', { wide: FIELDS[key].wide }]"
              >
                <template #label>
                  <span class="item-label">
                    <span>{{ FIELDS[key].label }}</span>
                    <el-tag
                      v-if="pending(key)"
                      size="small"
                      type="warning"
                      effect="plain"
                      >保存后恢复默认</el-tag
                    >
                    <el-tag
                      v-else-if="changed(key)"
                      size="small"
                      type="warning"
                      effect="plain"
                      >已修改</el-tag
                    >
                    <el-tag
                      v-else-if="!isStored(key)"
                      size="small"
                      type="info"
                      effect="plain"
                      >默认</el-tag
                    >
                  </span>
                </template>
                <div class="item-body">
                  <div class="item-control">
                    <el-select
                      v-if="FIELDS[key].input === 'select'"
                      v-model="form[key]"
                      class="select-input"
                    >
                      <el-option
                        v-for="option in FIELDS[key].options"
                        :key="option.value"
                        :label="option.label"
                        :value="option.value"
                      />
                    </el-select>
                    <el-input-number
                      v-else-if="FIELDS[key].input === 'number'"
                      v-model="form[key]"
                      :min="FIELDS[key].min"
                      :max="FIELDS[key].max"
                      :precision="FIELDS[key].rule === 'int' ? 0 : undefined"
                      controls-position="right"
                      :placeholder="placeholderOf(key, defaults)"
                      class="number-input"
                    >
                      <template #suffix>{{ FIELDS[key].unit }}</template>
                    </el-input-number>
                    <el-switch
                      v-else-if="FIELDS[key].input === 'switch'"
                      v-model="form[key]"
                      active-text="开启"
                      inactive-text="关闭"
                    />
                    <el-input
                      v-else
                      v-model="form[key]"
                      clearable
                      autocomplete="off"
                      :placeholder="placeholderOf(key, defaults)"
                    />
                    <el-button
                      v-if="canRestore(key)"
                      link
                      type="primary"
                      @click="restoreDefault(key)"
                      >恢复默认</el-button
                    >
                    <el-button
                      v-if="pending(key)"
                      link
                      @click="undoRestore(key)"
                      >撤销</el-button
                    >
                  </div>
                  <div class="hint">
                    {{ fieldHint(key, form.prom_type) }}
                    <span class="default-value"
                      >默认：{{ defaultText(key, defaults) }}</span
                    >
                  </div>
                  <div
                    v-if="FIELDS[key].secret && hasMaskedPassword(initial[key])"
                    class="hint"
                  >
                    地址里的密码显示为 ******：改密码就把 ******
                    换成新密码，不改就保留；改了账号、协议、主机或端口时也要重新填写密码。
                  </div>
                  <div v-if="FIELDS[key].warning" class="hint warning">
                    {{ FIELDS[key].warning }}
                  </div>
                  <el-alert
                    v-if="key === 'agent_auth_required' && form[key] === false"
                    type="error"
                    :closable="false"
                    show-icon
                    class="item-alert"
                    title="关闭后，不带账号的 Agent 连接也会被接受，任何能访问控制端的人都可以冒充 Agent 注册集群。只建议在升级旧 Agent 的过渡期临时关闭，旧 Agent 都换成带账号的连接后请马上打开。"
                  />
                  <div v-if="problems[key]" class="item-problem">
                    {{ problems[key] }}
                  </div>
                  <div v-if="fieldErrors[key]" class="item-error">
                    {{ fieldErrors[key] }}
                  </div>
                </div>
              </el-form-item>
            </el-form>

            <div v-if="card.id === 'metrics'" class="test-row">
              <el-button
                :loading="testing"
                :disabled="busy"
                @click="runMetricsTest"
                >测试连接</el-button
              >
              <span class="hint"
                >用表单里当前的查询地址和标签 key
                查询一次，不用先保存；不测远程写地址（写入测试数据有副作用）</span
              >
              <div v-if="testResult" :class="['test-result', testResult.type]">
                {{ testResult.text }}
              </div>
            </div>
          </div>
        </el-card>
      </template>
    </div>
  </div>
</template>

<style scoped lang="scss">
.general-settings {
  max-width: 1280px;
  padding: 16px;
}

.page-heading {
  display: flex;
  flex-wrap: wrap;
  gap: 12px 24px;
  align-items: flex-start;
  justify-content: space-between;
  margin-bottom: 16px;

  h2 {
    margin: 0 0 6px;
    font-size: 22px;
    font-weight: 600;
  }

  p {
    max-width: 860px;
    margin: 0;
    line-height: 1.7;
    color: var(--el-text-color-secondary);
  }
}

.heading-actions,
.card-header,
.card-title,
.card-actions,
.item-label,
.item-control {
  display: flex;
  gap: 8px;
  align-items: center;
}

.heading-actions,
.card-title {
  flex-wrap: wrap;
}

.card-header {
  justify-content: space-between;
}

.card-title.clickable {
  cursor: pointer;
}

// el-alert 自带 margin: 0，工具类压不过，用带作用域的类
.page-alert,
.card-alert {
  margin-bottom: 16px;
}

.problem-list {
  padding-left: 18px;
  margin: 4px 0 0;
  line-height: 1.7;
  list-style: disc;
}

.cards {
  min-height: 120px;
}

.settings-card {
  margin-bottom: 16px;
}

.card-note {
  margin: 0 0 16px;
  font-size: 13px;
  line-height: 1.7;
  color: var(--el-text-color-regular);
}

.settings-form {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: 0 24px;

  .wide {
    grid-column: 1 / -1;
  }
}

.item-body {
  width: 100%;
}

.item-control {
  .el-input {
    max-width: 760px;
  }
}

.select-input {
  width: 280px;
}

.number-input {
  width: 180px;
}

.hint,
.settings-meta {
  font-size: 12px;
  line-height: 1.6;
  color: var(--el-text-color-secondary);
}

.hint {
  margin-top: 4px;

  &.warning {
    color: var(--el-color-warning);
  }
}

.default-value {
  margin-left: 6px;
  white-space: nowrap;
}

.item-alert {
  margin-top: 6px;
}

.item-problem,
.item-error {
  margin-top: 4px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--el-color-danger);
}

.test-row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 12px;
  align-items: center;
  padding-top: 12px;
  border-top: 1px solid var(--el-border-color-lighter);

  .hint {
    margin-top: 0;
  }
}

.test-result {
  flex-basis: 100%;
  font-size: 13px;
  line-height: 1.6;
  word-break: break-all;

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

@media (width <= 640px) {
  .general-settings {
    padding: 8px;
  }

  .select-input {
    width: 100%;
  }
}
</style>
