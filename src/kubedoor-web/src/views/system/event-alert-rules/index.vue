<script setup lang="ts">
import {
  computed,
  h,
  nextTick,
  onBeforeUnmount,
  onMounted,
  reactive,
  ref,
  shallowRef,
  watch
} from "vue";
import { onBeforeRouteLeave } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";
import { Plus } from "@element-plus/icons-vue";
import dayjs from "dayjs";
import {
  type EventRules,
  type EventRulesState,
  getDefaultEventAlertRules,
  getEventAlertRules,
  saveEventAlertRules
} from "@/api/event-alert-rules";
import { getKnownClusters } from "@/api/system";
import {
  type Issue,
  type RuleForm,
  ALERT_KEY,
  IGNORE_KEY,
  copyRule,
  formToRules,
  formatJson,
  issueOwner,
  issueRange,
  newRule,
  parseJson,
  parsedOf,
  rulesToForm,
  stableStringify,
  syntaxIssueOf,
  syntaxRange,
  validateRules
} from "./rules-utils";
import RuleCard from "./components/RuleCard.vue";
import RulesJsonEditor from "./components/RulesJsonEditor.vue";
import PreviewDialog from "./components/PreviewDialog.vue";

defineOptions({ name: "SystemEventAlertRules" });

/**
 * K8S 事件告警规则:界面配置 / JSON 两种模式随时切换,试运行,恢复默认,保存后马上生效。
 * 规则存 master 的 auth_settings(event_alert_rules),库里没有时 master 用内置默认规则
 */
type Mode = "ui" | "json";

const mode = ref<Mode>("ui");
const form = reactive<{ ignore: RuleForm[]; alert: RuleForm[] }>({
  ignore: [],
  alert: []
});
const jsonText = ref("");
const loading = ref(false);
const saving = ref(false);
const loadingDefault = ref(false);
// 读取成功过才能保存:读取失败时不能用空规则覆盖已保存的规则
const loaded = ref(false);
const loadError = ref("");
const state = ref<EventRulesState | null>(null);
/** 加载 / 保存后的规则(stableStringify),和它比判断有没有没保存的修改 */
const baseline = ref("");
/** 加载时界面表示不了的内容(这时用 JSON 模式打开) */
const openedLost = ref<Issue[]>([]);
/** 界面配置模式下:点过保存 / 试运行,或者加载的规则本来就有问题,才在卡片上显示问题 */
const showIssues = ref(false);
/** 保存时 master 返回的问题(前端校验和 master 一致,一般用不上) */
const serverIssues = ref<Issue[]>([]);
const clusters = ref<string[]>([]);
const previewVisible = ref(false);
const previewRules = shallowRef<EventRules | null>(null);
const editor = ref<InstanceType<typeof RulesJsonEditor>>();
const ignoreSection = ref<HTMLElement>();
const alertSection = ref<HTMLElement>();

const busy = computed(
  () => loading.value || saving.value || loadingDefault.value
);
const parsed = computed(() => parseJson(jsonText.value));
/** JSON 解析成功时的结果(值、位置、重复的键),有语法错误时 null */
const parsedJson = computed(() => parsedOf(parsed.value));
const syntaxError = computed(() =>
  mode.value === "json" ? syntaxIssueOf(parsed.value) : null
);
/** 编辑区里的规则;JSON 有语法错误时为 undefined */
const draft = computed<unknown>(() => {
  if (mode.value === "ui") return formToRules(form);
  return parsedJson.value ? parsedJson.value.value : undefined;
});
const issues = computed<Issue[]>(() => {
  if (mode.value === "ui") return validateRules(draft.value);
  const result = parsedJson.value;
  return result ? validateRules(result.value, result.duplicates) : [];
});
const dirty = computed(() => {
  if (!loaded.value) return false;
  if (draft.value === undefined) return true;
  return stableStringify(draft.value) !== baseline.value;
});
/** 界面配置模式顶部列出的问题 */
const summaryIssues = computed(() => {
  if (mode.value !== "ui" || !showIssues.value) return [];
  return issues.value.length ? issues.value : serverIssues.value;
});
/** JSON 模式:问题和它在编辑框里的位置 */
const jsonIssues = computed(() => {
  const result = parsedJson.value;
  if (mode.value !== "json" || !result) return [];
  const list = issues.value.length ? issues.value : serverIssues.value;
  return list.map(item => issueRange(jsonText.value, result.ranges, item));
});
const jsonErrors = computed(() =>
  syntaxError.value ? [syntaxRange(jsonText.value, syntaxError.value)] : []
);
const metaText = computed(() => {
  const data = state.value;
  if (!data) return "";
  if (data.source === "default")
    return "还没有保存过，当前使用内置默认规则（随版本更新）；保存后改用你保存的规则。";
  const time = data.updated_at
    ? dayjs(data.updated_at).format("YYYY-MM-DD HH:mm:ss")
    : "";
  const who =
    data.updated_by === "legacy-import"
      ? "升级时从旧的规则文件导入"
      : data.updated_by || "系统";
  return `上次修改：${who}${time ? ` · ${time}` : ""}。保存后马上生效，不用重启。`;
});

// 改了规则,上次保存时 master 返回的问题就不准了
watch(draft, () => {
  if (serverIssues.value.length) serverIssues.value = [];
});

function errorText(reason: unknown, fallback: string): string {
  const failure = reason as any;
  const data = failure?.response?.data;
  return (
    data?.message ||
    data?.error ||
    (failure?.response?.status ? `HTTP ${failure.response.status}` : "") ||
    failure?.message ||
    fallback
  );
}

function setForm(next: { ignore: RuleForm[]; alert: RuleForm[] }) {
  form.ignore = next.ignore;
  form.alert = next.alert;
}

/** 用接口返回的规则刷新页面;keepMode:保存后留在原来的模式 */
function applyState(data: EventRulesState, keepMode = false) {
  state.value = data;
  const { form: next, lost } = rulesToForm(data.rules);
  setForm(next);
  openedLost.value = lost;
  // 界面能完整表示时用转换后的样子比较(补上了默认值、条件按固定顺序),否则用原样
  const value = lost.length ? data.rules : formToRules(next);
  baseline.value = stableStringify(value);
  jsonText.value = formatJson(value);
  if (lost.length) mode.value = "json";
  else if (!keepMode) mode.value = "ui";
  serverIssues.value = [];
  showIssues.value = validateRules(value).length > 0;
  loaded.value = true;
}

async function confirmDiscard(message: string): Promise<boolean> {
  try {
    await ElMessageBox.confirm(message, "未保存的修改", {
      type: "warning",
      confirmButtonText: "放弃修改",
      cancelButtonText: "继续编辑"
    });
    return true;
  } catch {
    return false;
  }
}

async function load(confirm = false) {
  if (busy.value) return;
  if (
    confirm &&
    dirty.value &&
    !(await confirmDiscard("重新加载会丢掉还没保存的修改，确定吗？"))
  )
    return;
  loading.value = true;
  loadError.value = "";
  try {
    const res = await getEventAlertRules();
    if (!res.success) throw new Error(res.message || "读取事件告警规则失败");
    applyState(res.data);
  } catch (reason) {
    loadError.value = errorText(reason, "读取事件告警规则失败");
  } finally {
    loading.value = false;
  }
}

async function loadClusters() {
  try {
    const res = await getKnownClusters();
    if (res.success) clusters.value = res.data.map(item => item.env);
  } catch {
    // 只用来给集群名做输入提示,读不到不影响编辑
  }
}

/** 界面表示不了的内容:确认后才切到界面配置 */
async function confirmLost(lost: Issue[]): Promise<boolean> {
  const shown = lost.slice(0, 12);
  const message = h("div", null, [
    h(
      "p",
      null,
      "界面配置表示不了下面这些内容，切换过去会丢掉或改掉它们（规则的效果可能跟着变）："
    ),
    h(
      "ul",
      {
        style:
          "padding-left: 18px; margin: 8px 0; max-height: 260px; overflow: auto"
      },
      shown.map(item => h("li", null, item.message))
    ),
    lost.length > shown.length
      ? h("p", null, `……还有 ${lost.length - shown.length} 处`)
      : null,
    h("p", null, "留在 JSON 模式可以先把它们改好。")
  ]);
  try {
    await ElMessageBox.confirm(message, "切换到界面配置", {
      type: "warning",
      confirmButtonText: "仍然切换",
      cancelButtonText: "留在 JSON"
    });
    return true;
  } catch {
    return false;
  }
}

/** 模式切换按钮:要先确认,所以不直接 v-model */
function onModeChange(value: unknown) {
  void switchMode(value === "json" ? "json" : "ui");
}

function revealSyntaxError() {
  const error = syntaxError.value;
  if (!error) return;
  editor.value?.reveal(error.line, error.column);
}

async function switchMode(target: Mode) {
  if (target === mode.value || busy.value) return;
  if (target === "json") {
    jsonText.value = formatJson(formToRules(form));
    mode.value = "json";
    return;
  }
  const result = parsedJson.value;
  if (!result) {
    ElMessage.warning(
      `JSON 第 ${syntaxError.value?.line} 行有语法错误，改好后才能切换到界面配置`
    );
    revealSyntaxError();
    return;
  }
  const { form: next, lost } = rulesToForm(result.value);
  const duplicates = result.duplicates.map(item => ({
    path: item.path,
    message: `第 ${item.line} 行的键 "${item.key}" 重复了，只保留最后一个`
  }));
  const all = [...duplicates, ...lost];
  if (all.length && !(await confirmLost(all))) return;
  setForm(next);
  openedLost.value = []; // 打开时的提示已经处理过了
  mode.value = "ui";
}

/** 跳到第一处问题:JSON 模式跳到那一行,界面配置模式滚到那条规则 */
async function revealFirstIssue() {
  await nextTick();
  if (mode.value === "json") {
    const first = jsonIssues.value[0];
    if (first) editor.value?.reveal(first.startLineNumber, first.startColumn);
    return;
  }
  const card =
    ignoreSection.value?.querySelector(".rule-card.has-issues") ??
    alertSection.value?.querySelector(".rule-card.has-issues");
  card?.scrollIntoView({ behavior: "smooth", block: "center" });
}

/** 保存 / 试运行前:语法错误和不合法的地方都要先改好。返回能用的规则 */
function checkedDraft(action: string): EventRules | null {
  if (!loaded.value || busy.value) return null;
  if (syntaxError.value) {
    const error = syntaxError.value;
    ElMessage.warning(`JSON 第 ${error.line} 行有语法错误：${error.message}`);
    revealSyntaxError();
    return null;
  }
  const problems = issues.value;
  if (problems.length) {
    showIssues.value = true;
    ElMessage.warning(
      `还有 ${problems.length} 处需要改正，改好后才能${action}`
    );
    void revealFirstIssue();
    return null;
  }
  return draft.value as EventRules;
}

async function save() {
  if (loaded.value && !busy.value && !dirty.value) {
    ElMessage.info("没有需要保存的修改");
    return;
  }
  const rules = checkedDraft("保存");
  if (!rules) return;
  saving.value = true;
  try {
    const res = await saveEventAlertRules(rules);
    if (!res.success) {
      ElMessage.error(res.message || "保存失败");
      return;
    }
    applyState(res.data, true);
    ElMessage.success("已保存，马上生效");
  } catch (reason) {
    // 原因已由 http 封装提示过;master 指出的每处问题列在页面上
    const errors = (reason as any)?.response?.data?.errors;
    serverIssues.value = Array.isArray(errors) ? errors : [];
    showIssues.value = true;
  } finally {
    saving.value = false;
  }
}

function openPreview() {
  const rules = checkedDraft("试运行");
  if (!rules) return;
  previewRules.value = JSON.parse(JSON.stringify(rules));
  previewVisible.value = true;
}

/** 恢复默认:把内置默认规则载入编辑区,保存后才生效(不删库里的规则) */
async function restoreDefault() {
  if (!loaded.value || busy.value) return;
  try {
    await ElMessageBox.confirm(
      "把内置默认规则载入编辑区，替换编辑区里现在的内容。检查、试运行后点「保存」才生效；保存前点「重新加载」可以放弃。",
      "恢复默认",
      {
        type: "warning",
        confirmButtonText: "载入默认规则",
        cancelButtonText: "取消"
      }
    );
  } catch {
    return;
  }
  loadingDefault.value = true;
  try {
    const res = await getDefaultEventAlertRules();
    if (!res.success) {
      ElMessage.error(res.message || "读取默认规则失败");
      return;
    }
    if (mode.value === "json") jsonText.value = formatJson(res.data.rules);
    else setForm(rulesToForm(res.data.rules).form);
    ElMessage.success("已载入默认规则，保存后生效");
  } catch {
    // 失败原因已由 http 封装提示
  } finally {
    loadingDefault.value = false;
  }
}

function cardIssues(group: string, index: number): Issue[] {
  if (!showIssues.value) return [];
  const owner = `${group}:${index}`;
  return issues.value.filter(item => issueOwner(item) === owner);
}

async function addRule(kind: "ignore" | "alert") {
  (kind === "ignore" ? form.ignore : form.alert).push(newRule());
  await nextTick();
  const box = kind === "ignore" ? ignoreSection.value : alertSection.value;
  const cards = box?.querySelectorAll(".rule-card");
  cards?.[cards.length - 1]?.scrollIntoView({
    behavior: "smooth",
    block: "center"
  });
}

function copyAt(list: RuleForm[], index: number) {
  list.splice(index + 1, 0, copyRule(list[index]));
}

function removeAt(list: RuleForm[], index: number) {
  list.splice(index, 1);
}

function moveAlert(index: number, delta: number) {
  const target = index + delta;
  if (target < 0 || target >= form.alert.length) return;
  const [item] = form.alert.splice(index, 1);
  form.alert.splice(target, 0, item);
}

function revealLine(line: number, column: number) {
  editor.value?.reveal(line, column);
}

function beforeUnload(event: BeforeUnloadEvent) {
  if (!dirty.value && !saving.value) return;
  event.preventDefault();
  event.returnValue = "";
}

onBeforeRouteLeave(async () => {
  if (saving.value) {
    ElMessage.info("正在保存，请稍候");
    return false;
  }
  return (
    !dirty.value ||
    (await confirmDiscard("还有修改没有保存，离开会丢掉这些修改，确定吗？"))
  );
});
onMounted(() => {
  void load();
  void loadClusters();
  window.addEventListener("beforeunload", beforeUnload);
});
onBeforeUnmount(() => window.removeEventListener("beforeunload", beforeUnload));
</script>

<template>
  <div class="event-alert-rules">
    <div class="page-heading">
      <h2>事件告警规则</h2>
      <p>决定哪些 K8S 事件要发告警通知。保存后马上生效，不用重启。</p>
    </div>
    <el-alert type="info" :closable="false" show-icon class="block">
      <template #title>事件怎么匹配规则</template>
      <ol class="flow">
        <li>状态是 DELETED（K8S 清理了事件对象）的事件不告警。</li>
        <li>命中任意一条启用的<b>忽略规则</b>就不告警。</li>
        <li>
          按顺序找第一条命中的<b>告警规则</b>（顺序就是优先级），把事件标成「已告警」。
        </li>
        <li>
          同一个事件在去重窗口内只通知一次（系统管理 →
          通用配置的「事件告警去重窗口」），发到该集群的通知群（系统管理 →
          消息通知）。
        </li>
      </ol>
      <p class="flow-note">
        一条规则里的条件要全部满足；字符串比较不区分大小写，「包含」是子串匹配。事件告警不受「告警屏蔽」影响。
      </p>
    </el-alert>

    <div class="toolbar">
      <div class="toolbar-group">
        <el-radio-group
          :model-value="mode"
          :disabled="!loaded || busy"
          @update:model-value="onModeChange"
        >
          <el-radio-button value="ui">界面配置</el-radio-button>
          <el-radio-button value="json">JSON</el-radio-button>
        </el-radio-group>
        <el-tag
          v-if="loaded && state?.source === 'default'"
          type="info"
          effect="plain"
        >
          当前使用内置默认规则
        </el-tag>
        <el-tag v-if="dirty" type="warning" effect="plain">未保存</el-tag>
      </div>
      <div class="toolbar-group">
        <el-button :disabled="!loaded || busy" @click="openPreview">
          试运行
        </el-button>
        <el-button
          :loading="loadingDefault"
          :disabled="!loaded || busy"
          @click="restoreDefault"
        >
          恢复默认
        </el-button>
        <el-button
          :loading="loading"
          :disabled="saving || loadingDefault"
          @click="load(true)"
        >
          重新加载
        </el-button>
        <el-button
          type="primary"
          :loading="saving"
          :disabled="!loaded || !dirty || loading || loadingDefault"
          @click="save"
        >
          保存
        </el-button>
      </div>
    </div>

    <el-alert
      v-if="loadError"
      type="error"
      :closable="false"
      show-icon
      class="block"
      :title="`读取事件告警规则失败：${loadError}`"
    />
    <el-alert
      v-if="mode === 'json' && openedLost.length"
      type="warning"
      :closable="false"
      show-icon
      class="block"
      title="保存的规则里有界面配置表示不了的内容（多半是升级时从旧的规则文件原样导入的），已用 JSON 模式打开"
    >
      <ul class="issue-list">
        <li v-for="(item, position) in openedLost.slice(0, 8)" :key="position">
          {{ item.message }}
        </li>
      </ul>
      <p v-if="openedLost.length > 8">……还有 {{ openedLost.length - 8 }} 处</p>
    </el-alert>
    <el-alert
      v-if="summaryIssues.length"
      type="error"
      :closable="false"
      show-icon
      class="block"
      :title="`还有 ${summaryIssues.length} 处需要改正，改好后才能保存`"
    >
      <ul class="issue-list">
        <li
          v-for="(item, position) in summaryIssues.slice(0, 8)"
          :key="position"
        >
          {{ item.message }}
        </li>
      </ul>
      <p v-if="summaryIssues.length > 8">
        ……还有 {{ summaryIssues.length - 8 }} 处，见下面标红的规则
      </p>
    </el-alert>

    <div v-loading="loading && !loaded" class="editor-area">
      <el-empty
        v-if="!loaded && !loading"
        description="还没有读到规则，请点「重新加载」"
      />
      <template v-else-if="mode === 'ui'">
        <section ref="ignoreSection" class="group">
          <div class="group-header">
            <h3>忽略规则</h3>
            <span class="group-count">{{ form.ignore.length }} 条</span>
            <span class="hint">命中任意一条就不告警，顺序不影响结果</span>
            <el-button
              :icon="Plus"
              :disabled="!loaded || busy"
              @click="addRule('ignore')"
            >
              添加忽略规则
            </el-button>
          </div>
          <div v-if="!form.ignore.length" class="empty">没有忽略规则</div>
          <RuleCard
            v-for="(item, index) in form.ignore"
            :key="item.key"
            v-model:rule="form.ignore[index]"
            kind="ignore"
            :index="index"
            :total="form.ignore.length"
            :issues="cardIssues(IGNORE_KEY, index)"
            :clusters="clusters"
            :disabled="busy"
            @copy="copyAt(form.ignore, index)"
            @remove="removeAt(form.ignore, index)"
          />
        </section>
        <section ref="alertSection" class="group">
          <div class="group-header">
            <h3>告警规则</h3>
            <span class="group-count">{{ form.alert.length }} 条</span>
            <span class="hint">
              按顺序匹配，第一条命中的生效：严重的放前面，用右边的箭头调整顺序
            </span>
            <el-button
              :icon="Plus"
              :disabled="!loaded || busy"
              @click="addRule('alert')"
            >
              添加告警规则
            </el-button>
          </div>
          <div v-if="!form.alert.length" class="empty">
            没有告警规则（不会发任何事件告警）
          </div>
          <RuleCard
            v-for="(item, index) in form.alert"
            :key="item.key"
            v-model:rule="form.alert[index]"
            kind="alert"
            :index="index"
            :total="form.alert.length"
            :issues="cardIssues(ALERT_KEY, index)"
            :clusters="clusters"
            :disabled="busy"
            @move="delta => moveAlert(index, delta)"
            @copy="copyAt(form.alert, index)"
            @remove="removeAt(form.alert, index)"
          />
        </section>
      </template>
      <template v-else>
        <RulesJsonEditor
          ref="editor"
          v-model="jsonText"
          :errors="jsonErrors"
          :warnings="jsonIssues"
          :readonly="busy"
          @save="save"
        />
        <div class="json-status">
          <div
            v-if="syntaxError"
            class="json-line error"
            @click="revealLine(syntaxError.line, syntaxError.column)"
          >
            第 {{ syntaxError.line }} 行第 {{ syntaxError.column }} 列：{{
              syntaxError.message
            }}
          </div>
          <template v-else-if="jsonIssues.length">
            <div class="json-title">
              还有 {{ jsonIssues.length }} 处需要改正（点一下跳到那一行）：
            </div>
            <div
              v-for="(item, position) in jsonIssues.slice(0, 50)"
              :key="position"
              class="json-line warning"
              @click="revealLine(item.startLineNumber, item.startColumn)"
            >
              第 {{ item.startLineNumber }} 行：{{ item.message }}
            </div>
          </template>
          <div v-else class="json-line ok">格式和内容都正确（Ctrl+S 保存）</div>
        </div>
      </template>
    </div>
    <p v-if="loaded" class="settings-meta">{{ metaText }}</p>

    <PreviewDialog
      v-model="previewVisible"
      :rules="previewRules"
      :clusters="clusters"
    />
  </div>
</template>

<style scoped lang="scss">
.event-alert-rules {
  max-width: 1280px;
  padding: 16px;

  @media (width <= 640px) {
    padding: 8px;
  }
}

.page-heading {
  margin-bottom: 16px;

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

// el-alert 自带 margin: 0,用带作用域的类
.block {
  margin-bottom: 14px;
}

.flow {
  padding-left: 18px;
  margin: 6px 0 0;
  line-height: 1.8;
  list-style: decimal;
}

.flow-note {
  margin: 4px 0 0;
  line-height: 1.7;
}

.issue-list {
  padding-left: 18px;
  margin: 6px 0 0;
  line-height: 1.7;
}

.toolbar,
.toolbar-group,
.group-header {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  align-items: center;
}

.toolbar {
  justify-content: space-between;
  margin-bottom: 14px;
}

.editor-area {
  min-height: 120px;
}

.group {
  margin-bottom: 22px;
}

.group-header {
  margin-bottom: 10px;

  h3 {
    margin: 0;
    font-size: 16px;
    font-weight: 600;
  }

  .el-button {
    margin-left: auto;
  }
}

.group-count,
.hint,
.empty,
.settings-meta {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.empty {
  padding: 14px;
  margin-bottom: 12px;
  text-align: center;
  border: 1px dashed var(--el-border-color);
  border-radius: 6px;
}

.json-status {
  max-height: 220px;
  padding: 8px 0;
  overflow: auto;
  font-size: 13px;
  line-height: 1.7;
}

.json-title {
  color: var(--el-text-color-regular);
}

.json-line {
  cursor: pointer;

  &.error {
    color: var(--el-color-danger);
  }

  &.warning {
    color: var(--el-color-warning-dark-2);
  }

  &.ok {
    color: var(--el-color-success);
    cursor: default;
  }
}
</style>
