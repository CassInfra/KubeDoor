<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from "vue";
import { onBeforeRouteLeave } from "vue-router";
import dayjs from "dayjs";
// ElInputTag 没有在 plugins/elementPlus.ts 全局注册，这里单独引入（同 Agent 管理页）
import { ElInputTag, ElMessageBox } from "element-plus";
import { message } from "@/utils/message";
import {
  type TracingAnalyzeRequest,
  type TracingAnalyzeResult,
  type TracingClickHouseTest,
  type TracingFieldKey,
  type TracingJaegerTest,
  type TracingSettings,
  analyzeTracingFields,
  getTracingConfig,
  saveTracingConfig,
  testTracingConfig
} from "@/api/system";
import {
  type FieldOption,
  type FormError,
  type SuggestionRow,
  type TestView,
  AGENT_PAGE,
  FIELD_KEYS,
  FIELD_META,
  HELP_URL,
  LABEL_MAX_LENGTH,
  LEVEL_LABELS,
  LOOKBACK_OPTIONS,
  MASK_KEY_MAX_LENGTH,
  MAX_CUSTOM_FIELDS,
  MAX_FIELD_NAMES,
  MAX_HEADERS,
  MAX_MASK_KEYS,
  MAX_TIMEOUT,
  MIN_TIMEOUT,
  TRACE_JSON_MAX_BYTES,
  analysisSummary,
  applyAllSuggestions,
  builtinMaskKeys,
  capabilityRows,
  classifyValue,
  cleanNames,
  clickhouseErrors,
  clickhouseExpanded,
  clickhouseTestView,
  clusterKeyCheck,
  clusterKeySuggestions,
  clusterOverview,
  emptyForm,
  errorText,
  fieldDefaults,
  fieldLabels,
  fieldLevels,
  fieldOptions,
  filterKeys,
  formFromSettings,
  formPayload,
  jaegerErrors,
  jaegerTestView,
  keepsSavedHeader,
  levelLabel,
  matchLabel,
  mergeObserved,
  missingColumnLines,
  nextRowId,
  observedFromAnalyze,
  observedFromClickHouse,
  observedKeySuggestions,
  observedText,
  otelEnvExample,
  passwordPlaceholder,
  payloadKey,
  requestTimeoutMs,
  sameList,
  sourceText,
  suggestionRows,
  testPayload,
  traceJsonError,
  validateForm
} from "./tracing-utils";

defineOptions({ name: "SystemTracing" });

const loading = ref(false);
// 读取成功过才能保存：读取失败时不能用默认值覆盖已保存的配置
const loaded = ref(false);
const loadError = ref("");
const saving = ref(false);
const switching = ref(false);
const testingJaeger = ref(false);
const testingClickHouse = ref(false);
const analyzing = ref(false);
const settings = ref<TracingSettings | null>(null);
const form = reactive(emptyForm());
// 加载 / 保存后的表单内容，用来判断有没有未保存的修改
const snapshot = ref("");
// 已保存的 Jaeger 查询地址：没保存过地址时不能打开启用开关
const savedUrl = ref("");
const updatedBy = ref("");
const updatedAt = ref("");
// ClickHouse 加速默认折叠，启用了或者填过地址时展开
const chExpanded = ref(false);
const jaegerTest = ref<TracingJaegerTest | null>(null);
const chTest = ref<TracingClickHouseTest | null>(null);
const analysis = ref<TracingAnalyzeResult | null>(null);
const identify = reactive({
  mode: "sample" as "sample" | "json",
  service: "",
  lookback: "15m",
  json: ""
});
const keyFilter = reactive({ text: "", level: "" });
const fileInput = ref<HTMLInputElement | null>(null);

const busy = computed(() => loading.value || saving.value);
const dirty = computed(
  () => loaded.value && payloadKey(form) !== snapshot.value
);
const labels = computed(() => fieldLabels(settings.value));
const levels = computed(() => fieldLevels(settings.value));
const defaults = computed(() => fieldDefaults(settings.value));
const builtinMasks = computed(() => builtinMaskKeys(settings.value));
const clusters = computed(() => settings.value?.clusters ?? []);
const services = computed(() => jaegerTest.value?.services ?? []);
const modifiedText = computed(() =>
  updatedAt.value
    ? `上次修改：${updatedBy.value || "-"} · ${dayjs(updatedAt.value).format("YYYY-MM-DD HH:mm:ss")}`
    : "尚未保存过"
);

const jaegerView = computed<TestView | null>(() =>
  jaegerTest.value ? jaegerTestView(jaegerTest.value) : null
);
const capabilities = computed(() =>
  jaegerTest.value?.ok ? capabilityRows(jaegerTest.value.capabilities) : []
);
const chView = computed<TestView | null>(() =>
  chTest.value ? clickhouseTestView(chTest.value) : null
);
const chMissing = computed(() => missingColumnLines(chTest.value));
const chValues = computed(() =>
  (chTest.value?.cluster_values ?? []).map(item => ({
    ...item,
    label: matchLabel(
      classifyValue(item.value, item.env, clusters.value, item.ambiguous)
    )
  }))
);

// 测试 ClickHouse、识别字段时在链路里看到的集群取值，归到各集群
const clusterView = computed(() =>
  clusterOverview(
    clusters.value,
    mergeObserved([
      ...observedFromClickHouse(chTest.value),
      ...observedFromAnalyze(analysis.value, form.cluster_key)
    ])
  )
);
const envExample = computed(() =>
  otelEnvExample(form.cluster_key, clusters.value[0]?.env ?? "")
);

const options = computed(
  () =>
    Object.fromEntries(
      FIELD_KEYS.map(key => [
        key,
        fieldOptions(
          levels.value[key],
          defaults.value[key],
          form.fields[key],
          analysis.value?.keys
        )
      ])
    ) as Record<TracingFieldKey, FieldOption[]>
);
const isDefaultField = (key: TracingFieldKey) =>
  sameList(cleanNames(form.fields[key]), defaults.value[key]);
const allDefault = computed(() => FIELD_KEYS.every(isDefaultField));

const candidates = computed(() =>
  (analysis.value?.cluster_candidates ?? []).map(item => ({
    ...item,
    values: (item.values ?? []).map(value => ({
      ...value,
      label: matchLabel(
        classifyValue(value.value, value.env, clusters.value, value.ambiguous)
      )
    }))
  }))
);
const suggestedClusterKey = computed(
  () => analysis.value?.suggestions?.cluster_key ?? ""
);
const clusterCheck = computed(() =>
  clusterKeyCheck(analysis.value, form.cluster_key)
);
const suggestions = computed(() =>
  suggestionRows(form.fields, analysis.value, labels.value)
);
const applicable = computed(
  () => suggestions.value.filter(row => row.state === "apply").length
);
const filteredKeys = computed(() =>
  filterKeys(analysis.value?.keys ?? [], keyFilter.text, keyFilter.level)
);

// ---------------------------------------------------------------------------
// 读取和保存
// ---------------------------------------------------------------------------
function applySettings(data: TracingSettings) {
  settings.value = data;
  Object.assign(form, formFromSettings(data));
  snapshot.value = payloadKey(form);
  savedUrl.value = data.url ?? "";
  updatedBy.value = data.updated_by ?? "";
  updatedAt.value = data.updated_at ?? "";
}

async function load() {
  if (busy.value) return;
  loading.value = true;
  loadError.value = "";
  try {
    const res = await getTracingConfig();
    applySettings(res.data);
    if (!loaded.value) chExpanded.value = clickhouseExpanded(res.data);
    loaded.value = true;
  } catch (reason) {
    loadError.value = errorText(reason, "读取链路追踪配置失败");
  } finally {
    loading.value = false;
  }
}

async function confirmDiscard(): Promise<boolean> {
  try {
    await ElMessageBox.confirm("有未保存的修改，是否放弃？", "未保存的修改", {
      confirmButtonText: "放弃修改",
      cancelButtonText: "继续编辑",
      type: "warning"
    });
    return true;
  } catch {
    return false;
  }
}

async function reset() {
  if (dirty.value && !(await confirmDiscard())) return;
  await load();
}

/** 提示第一个错误并滚动到它所在的卡片 */
function showErrors(errors: FormError[]) {
  const first = errors[0];
  if (first.section === "clickhouse") chExpanded.value = true;
  message(
    errors.length > 1
      ? `${first.text}（另有 ${errors.length - 1} 处要修改）`
      : first.text,
    { type: "warning" }
  );
  document
    .getElementById(`tracing-${first.section}`)
    ?.scrollIntoView({ behavior: "smooth", block: "start" });
}

/** 保存即启用 */
async function save() {
  if (!loaded.value || busy.value) return;
  const errors = validateForm(form);
  if (errors.length) {
    showErrors(errors);
    return;
  }
  saving.value = true;
  try {
    const res = await saveTracingConfig({
      ...formPayload(form),
      enabled: true
    });
    applySettings(res.data);
    message("已保存并启用", { type: "success" });
  } catch {
    // 接口返回的原因（例如 400 某一项不合法）http 封装已经弹出提示
  } finally {
    saving.value = false;
  }
}

/** 右上角的开关：拨动立即保存，只传 enabled，表单里没保存的修改保留 */
async function toggleEnabled(): Promise<boolean> {
  const next = !form.enabled;
  if (next && !savedUrl.value) {
    message("请先填写 Jaeger 查询地址并点底部的「保存」，保存后会自动启用", {
      type: "warning"
    });
    return false;
  }
  switching.value = true;
  try {
    const res = await saveTracingConfig({ enabled: next });
    updatedBy.value = res.data.updated_by ?? "";
    updatedAt.value = res.data.updated_at ?? "";
    message(next ? "已启用" : "已停用", { type: "success" });
    return true;
  } catch {
    return false;
  } finally {
    switching.value = false;
  }
}

// ---------------------------------------------------------------------------
// 表单行
// ---------------------------------------------------------------------------
function addHeader() {
  form.headers.push({ id: nextRowId(), name: "", value: "" });
}

function addCustomField() {
  form.custom_fields.push({ id: nextRowId(), key: "", label: "" });
}

function restoreField(key: TracingFieldKey) {
  form.fields[key] = [...defaults.value[key]];
}

function restoreAllFields() {
  form.fields = fieldDefaults(settings.value);
}

/** 清除已保存的 ClickHouse 密码：确认后只做标记，保存或测试时带 clear_password: true */
async function clearPassword() {
  try {
    await ElMessageBox.confirm(
      "清除后 KubeDoor 连接 ClickHouse 时不再带密码，适用于改用没有密码的账号。点底部的「保存」后生效；保存前点「测试」也按清除后的配置连接。",
      "清除 ClickHouse 密码",
      { type: "warning", confirmButtonText: "清除", cancelButtonText: "取消" }
    );
  } catch {
    return;
  }
  form.clickhouse.clear_password = true;
  form.clickhouse.password = "";
}

// ---------------------------------------------------------------------------
// 测试连接
// ---------------------------------------------------------------------------
function testToast(view: TestView, ok: string) {
  message(
    view.type === "success" ? ok : "测试结果有需要处理的地方，见页面上的说明",
    { type: view.type === "error" ? "warning" : view.type }
  );
}

async function runJaegerTest() {
  if (!loaded.value || testingJaeger.value) return;
  const errors = jaegerErrors(form, true);
  if (errors.length) {
    showErrors(errors);
    return;
  }
  testingJaeger.value = true;
  jaegerTest.value = null;
  try {
    // 服务列表 + capabilities 两个请求
    const res = await testTracingConfig(
      "jaeger",
      testPayload(form),
      requestTimeoutMs(form.timeout, 2)
    );
    jaegerTest.value = res.data;
    testToast(jaegerTestView(res.data), "连接正常");
  } catch {
    // http 封装已经弹出提示
  } finally {
    testingJaeger.value = false;
  }
}

async function runClickHouseTest() {
  if (!loaded.value || testingClickHouse.value) return;
  const errors = clickhouseErrors(form, true);
  if (errors.length) {
    showErrors(errors);
    return;
  }
  testingClickHouse.value = true;
  chTest.value = null;
  try {
    // version、两张表的 DESCRIBE、各集群 Span 数：最多 4 条 SQL
    const res = await testTracingConfig(
      "clickhouse",
      testPayload(form),
      requestTimeoutMs(form.clickhouse.timeout, 4)
    );
    chTest.value = res.data;
    testToast(clickhouseTestView(res.data), "连接正常");
  } catch {
    // http 封装已经弹出提示
  } finally {
    testingClickHouse.value = false;
  }
}

// ---------------------------------------------------------------------------
// 识别字段
// ---------------------------------------------------------------------------
async function runAnalyze() {
  if (!loaded.value || analyzing.value) return;
  let request: TracingAnalyzeRequest;
  let timeout: number;
  if (identify.mode === "sample") {
    const errors = jaegerErrors(form, true);
    if (errors.length) {
      showErrors(errors);
      return;
    }
    const service = identify.service.trim();
    if (!service) {
      message("请选择或输入服务名", { type: "warning" });
      return;
    }
    request = {
      mode: "sample",
      service,
      lookback: identify.lookback,
      config: testPayload(form)
    };
    // 搜一次 + 最多取 3 条完整链路
    timeout = requestTimeoutMs(form.timeout, 5);
  } else {
    const error = traceJsonError(identify.json);
    if (error) {
      message(error, { type: "warning" });
      return;
    }
    request = {
      mode: "json",
      trace_json: identify.json.trim(),
      config: testPayload(form)
    };
    timeout = 60000;
  }
  analyzing.value = true;
  try {
    const res = await analyzeTracingFields(request, timeout);
    analysis.value = res.data;
    keyFilter.text = "";
    keyFilter.level = "";
    if (res.data.traces) message("识别完成，结果在下方", { type: "success" });
    else
      message(
        "没有取到链路：换个服务、放宽时间范围，或者改用「粘贴 Trace JSON」",
        { type: "warning" }
      );
  } catch {
    // http 封装已经弹出提示
  } finally {
    analyzing.value = false;
  }
}

function pickFile() {
  fileInput.value?.click();
}

async function readFile(event: Event) {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  input.value = "";
  if (!file) return;
  if (file.size > TRACE_JSON_MAX_BYTES) {
    message(
      `文件有 ${(file.size / 1024 / 1024).toFixed(1)} MB，超过了 5 MB：换一条 Span 少一些的链路`,
      { type: "warning" }
    );
    return;
  }
  identify.json = await file.text();
}

function setClusterKey(key: string) {
  form.cluster_key = key;
  message(`集群字段已设为 ${key}，点底部的「保存」后生效`, {
    type: "success"
  });
}

function applySuggestion(row: SuggestionRow) {
  form.fields[row.key] = [...row.suggested];
  message(`已套用到字段映射「${row.label}」，点底部的「保存」后生效`, {
    type: "success"
  });
}

function applyAll() {
  const { fields, applied } = applyAllSuggestions(form.fields, analysis.value);
  if (!applied.length) return;
  form.fields = fields;
  message(`已套用 ${applied.length} 项字段映射，点底部的「保存」后生效`, {
    type: "success"
  });
}

function queryClusterKeys(
  query: string,
  callback: (items: { value: string; note: string }[]) => void
) {
  callback(clusterKeySuggestions(query, analysis.value));
}

function queryObservedKeys(
  query: string,
  callback: (items: { value: string }[]) => void
) {
  callback(observedKeySuggestions(query, analysis.value));
}

// ---------------------------------------------------------------------------
// 离开页面前提醒未保存的修改
// ---------------------------------------------------------------------------
function beforeUnload(event: BeforeUnloadEvent) {
  if (!dirty.value && !saving.value) return;
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
  <div class="system-tracing">
    <div class="page-heading">
      <div>
        <h2>链路追踪</h2>
        <p>
          给 AI 助手查链路用：排查接口报错（400 / 502 / 504
          等）、超时、变慢时，AI 按集群搜索 Jaeger
          里的链路，找出最先出错或最耗时的服务，再结合 Pod
          日志、事件和指标给出原因。需要 <b>Jaeger 2.23 及以上</b>（只用 api_v3
          接口，不需要开 Jaeger 的 MCP）；ClickHouse
          加速可选。对集群有只读及以上权限的账号都可以让 AI
          查这个集群的链路。点底部的「保存」后自动启用，右上角的开关可以随时停用
          / 启用，立即生效。
          <a
            :href="HELP_URL"
            target="_blank"
            rel="noopener noreferrer"
            class="link"
            >配置说明</a
          >
        </p>
      </div>
      <div class="heading-actions">
        <!-- 拨动立即生效：只改启用状态，表单里没保存的修改不受影响 -->
        <el-switch
          v-model="form.enabled"
          :before-change="toggleEnabled"
          :loading="switching"
          :disabled="!loaded"
          active-text="启用"
          inactive-text="停用"
        />
      </div>
    </div>

    <el-alert
      type="warning"
      :closable="false"
      show-icon
      class="page-alert"
      title="AI 查链路时，Span 的名称、URL、SQL 语句等属性会作为对话内容发给模型服务商（打码字段的值会换成 ***），请先确认符合公司的数据安全要求再启用"
    />
    <el-alert
      v-if="loadError"
      type="error"
      :closable="false"
      show-icon
      class="page-alert"
      :title="`读取链路追踪配置失败：${loadError}`"
    >
      <el-button size="small" :loading="loading" @click="load">
        重新读取
      </el-button>
    </el-alert>

    <div v-loading="loading" class="cards">
      <el-empty
        v-if="!loaded && !loading"
        description="还没有读取到配置，请点「重新读取」"
      />
      <template v-if="loaded">
        <!-- Jaeger 连接 -->
        <el-card id="tracing-jaeger" shadow="never" class="tracing-card">
          <template #header><strong>Jaeger 连接</strong></template>
          <el-form label-width="130px" @submit.prevent>
            <el-form-item label="Jaeger 查询地址" required>
              <el-input
                v-model="form.url"
                clearable
                placeholder="http://jaeger-query.observability:16686"
              />
              <div class="hint">
                填 Jaeger UI 的地址即可；Jaeger 配置了 base_path
                时把路径带上，例如 https://ops.example.com/jaeger。由 KubeDoor
                控制端（kubedoor-master）直接访问，要能从 master 所在的 Pod 连上
              </div>
            </el-form-item>
            <el-form-item label="Jaeger UI 地址">
              <el-input
                v-model="form.ui_url"
                clearable
                placeholder="可选，不填和查询地址相同"
              />
              <div class="hint">
                AI 回答里的链路链接用这个地址拼成 &lt;地址&gt;/trace/&lt;链路
                ID&gt;；查询地址是集群内地址、浏览器打不开时填外部地址
              </div>
            </el-form-item>
            <el-form-item label="请求头">
              <div class="headers">
                <div
                  v-for="(item, index) in form.headers"
                  :key="item.id"
                  class="header-row"
                >
                  <el-input
                    v-model="item.name"
                    placeholder="名称，例如 Authorization"
                    class="header-name"
                  />
                  <el-input
                    v-model="item.value"
                    type="password"
                    show-password
                    autocomplete="new-password"
                    :placeholder="
                      keepsSavedHeader(form, item)
                        ? '已设置，留空表示不修改'
                        : '值，例如 Bearer <令牌>'
                    "
                  />
                  <el-button
                    link
                    type="danger"
                    @click="form.headers.splice(index, 1)"
                  >
                    删除
                  </el-button>
                </div>
                <el-button
                  size="small"
                  :disabled="form.headers.length >= MAX_HEADERS"
                  @click="addHeader"
                >
                  添加请求头
                </el-button>
                <div class="hint">
                  Jaeger 前面有鉴权代理时填写。值加密保存，保存后不再显示；最多
                  {{ MAX_HEADERS }} 个
                </div>
              </div>
            </el-form-item>
            <el-form-item label="超时（秒）">
              <el-input-number
                v-model="form.timeout"
                :min="MIN_TIMEOUT"
                :max="MAX_TIMEOUT"
                :precision="0"
              />
              <span class="hint inline">
                单次请求最多等多久（{{ MIN_TIMEOUT }}~{{ MAX_TIMEOUT }} 秒）
              </span>
            </el-form-item>
            <el-form-item label="HTTPS 证书">
              <el-checkbox v-model="form.tls_verify">
                校验 HTTPS 证书
              </el-checkbox>
              <span class="hint inline">自签证书时可以关闭（不推荐）</span>
            </el-form-item>
            <el-form-item>
              <el-button
                type="primary"
                plain
                :loading="testingJaeger"
                @click="runJaegerTest"
              >
                测试连接
              </el-button>
              <span class="hint inline">
                用表单里当前的配置，不用先保存；查服务数量、Jaeger
                版本是否满足要求和后端支持的搜索能力
              </span>
            </el-form-item>
          </el-form>
          <template v-if="jaegerTest && jaegerView">
            <el-alert
              :type="jaegerView.type"
              :title="jaegerView.text"
              :closable="false"
              show-icon
            />
            <el-descriptions
              v-if="capabilities.length"
              :column="1"
              border
              size="small"
              class="result-block"
            >
              <el-descriptions-item
                v-for="row in capabilities"
                :key="row.label"
                :label="row.label"
              >
                <span :class="{ 'danger-text': row.ok === false }">
                  {{ row.value }}
                </span>
              </el-descriptions-item>
            </el-descriptions>
            <ul v-if="jaegerTest.messages?.length" class="rules">
              <li v-for="(text, index) in jaegerTest.messages" :key="index">
                {{ text }}
              </li>
            </ul>
          </template>
        </el-card>

        <!-- 集群字段 -->
        <el-card id="tracing-cluster" shadow="never" class="tracing-card">
          <template #header><strong>集群字段</strong></template>
          <p class="card-note">
            一个 Jaeger 往往保存多个 K8S 集群的链路。KubeDoor
            用链路资源属性里的一个字段区分集群：AI
            在某个集群里提问时，搜索只返回这个集群的链路。按链路 ID
            查看时返回整条链路，包括其他集群的 Span。
          </p>
          <el-form label-width="130px" @submit.prevent>
            <el-form-item label="集群字段">
              <!-- el-autocomplete 的根节点在 Tooltip 片段里，scoped 的 class 落不到它上面，宽度由外层控制 -->
              <div class="cluster-key-input">
                <el-autocomplete
                  v-model="form.cluster_key"
                  :fetch-suggestions="queryClusterKeys"
                  clearable
                  placeholder="例如 k8sname；留空表示不按集群过滤"
                >
                  <template #default="{ item }">
                    <div class="suggestion">
                      <span>{{ item.value }}</span>
                      <span class="option-note">{{ item.note }}</span>
                    </div>
                  </template>
                </el-autocomplete>
              </div>
            </el-form-item>
          </el-form>
          <el-alert
            v-if="!form.cluster_key.trim() && clusters.length > 1"
            type="warning"
            :closable="false"
            show-icon
            class="block-alert"
            title="没有填集群字段：AI 在任何一个集群里提问，都会查到所有集群的链路。Jaeger 里只有一个集群的链路时才建议留空"
          />

          <div class="section-title">怎么填</div>
          <ul class="rules">
            <li>
              在应用的 OTel Agent 里用环境变量写了集群标识的（如下），等号前面的
              <code>k8sname</code> 就是字段名，这里填 <code>k8sname</code>：
              <pre class="example">{{ envExample }}</pre>
              已经设置了 OTEL_RESOURCE_ATTRIBUTES 的，用逗号接在后面，例如
              <code>service.version=1.0.0,k8sname=…</code>
            </li>
            <li>
              用 OpenTelemetry Operator 或 Collector 的 k8sattributes
              处理器加的，一般是 <code>k8s.cluster.name</code>
            </li>
            <li>
              不确定时用页面下方的「识别字段」取几条链路，取值能对上 KubeDoor
              集群的字段会列出来，点「设为集群字段」即可
            </li>
            <li>
              链路里的取值要等于 KubeDoor
              的集群名，或者这个集群的某个别名：例如集群名是
              prod-penglai、链路里写的是 cassmall-prod-penglai，就到
              <router-link :to="AGENT_PAGE" class="link"
                >Agent 管理</router-link
              >
              页给 prod-penglai 加别名 cassmall-prod-penglai
            </li>
          </ul>

          <div class="section-title">KubeDoor 的集群和别名</div>
          <el-table
            :data="clusterView.rows"
            border
            size="small"
            empty-text="还没有集群：在 Agent 管理页安装 Agent 后会出现在这里"
          >
            <el-table-column prop="env" label="集群名" min-width="160" />
            <el-table-column label="别名" min-width="220">
              <template #default="{ row }">
                <div v-if="row.aliases.length" class="tag-list">
                  <el-tag
                    v-for="alias in row.aliases"
                    :key="alias"
                    size="small"
                    effect="plain"
                  >
                    {{ alias }}
                  </el-tag>
                </div>
                <span v-else class="muted">—</span>
              </template>
            </el-table-column>
            <el-table-column label="链路里看到的取值" min-width="260">
              <template #default="{ row }">
                <div v-if="row.observed.length" class="tag-list">
                  <el-tooltip
                    v-for="item in row.observed"
                    :key="item.value"
                    :content="`来自${sourceText(item)}`"
                    placement="top"
                  >
                    <el-tag size="small" type="success" effect="plain">
                      {{ observedText(item) }}
                    </el-tag>
                  </el-tooltip>
                </div>
                <span v-else class="muted">—</span>
              </template>
            </el-table-column>
          </el-table>
          <div class="hint">
            「链路里看到的取值」来自 ClickHouse 加速的「测试」（最近 10
            分钟）和「识别字段」的样本（按当前填的集群字段），测试或识别之后显示
          </div>
          <el-alert
            v-if="clusterView.unmatched.length"
            type="warning"
            :closable="false"
            show-icon
            class="block-alert"
            title="这些取值对不上 KubeDoor 的集群名或别名"
          >
            <div class="tag-list">
              <el-tooltip
                v-for="item in clusterView.unmatched"
                :key="item.value"
                :content="`来自${sourceText(item)}`"
                placement="top"
              >
                <el-tag size="small" type="warning" effect="plain">
                  {{ observedText(item) }}
                </el-tag>
              </el-tooltip>
            </div>
            <div class="alert-text">
              如果它们是 KubeDoor 里的集群，到
              <router-link :to="AGENT_PAGE" class="link"
                >Agent 管理</router-link
              >
              页给对应集群加别名（别名填链路里的取值）；对不上的链路，AI
              在任何集群里都搜不到
            </div>
          </el-alert>
          <el-alert
            v-if="clusterView.ambiguous.length"
            type="warning"
            :closable="false"
            show-icon
            class="block-alert"
            title="这些取值同时是几个集群的名称或别名"
          >
            <div
              v-for="item in clusterView.ambiguous"
              :key="item.value"
              class="alert-text"
            >
              {{ item.value }}：{{ item.envs.join("、") || "几个集群" }}
            </div>
            <div class="alert-text">
              在这几个集群里都会搜到它的链路，Span 上也只能显示原值；到 Agent
              管理页去掉重复的别名
            </div>
          </el-alert>
          <el-alert
            v-if="clusterView.emptySpans"
            type="info"
            :closable="false"
            show-icon
            class="block-alert"
            :title="`ClickHouse 里最近 10 分钟有 ${clusterView.emptySpans} 个 Span 没有集群字段（k8s_cluster 为空）：这些应用还没设置集群属性，按集群搜索时搜不到它们`"
          />
        </el-card>

        <!-- ClickHouse 加速 -->
        <el-card
          id="tracing-clickhouse"
          shadow="never"
          class="tracing-card"
          :body-style="chExpanded ? undefined : { padding: '0' }"
        >
          <template #header>
            <div
              class="card-header clickable"
              @click="chExpanded = !chExpanded"
            >
              <div class="card-title">
                <strong>ClickHouse 加速（可选）</strong>
                <el-tag
                  size="small"
                  effect="plain"
                  :type="form.clickhouse.enabled ? 'success' : 'info'"
                >
                  {{ form.clickhouse.enabled ? "已启用" : "未启用" }}
                </el-tag>
                <span v-if="!chExpanded" class="hint">
                  Jaeger 用 ClickHouse 存储时可以开启，点击展开
                </span>
              </div>
              <el-button link type="primary">
                {{ chExpanded ? "收起" : "展开" }}
              </el-button>
            </div>
          </template>
          <div v-show="chExpanded">
            <ul class="rules first">
              <li>
                Jaeger 用 ClickHouse
                存储时，它的查询接口不能比较状态码大小、不能模糊匹配、结果不按时间排序，也不能统计。开启后，搜索和统计直接查
                ClickHouse 里的扁平搜索表：状态码可以比较大小，URL
                和查询参数可以模糊匹配，能按时间取最新的请求，AI
                还能按时间、Pod、接口、状态码分组统计；查看单条链路仍然走 Jaeger
              </li>
              <li>
                搜索表、汇总表和物化视图要先在 ClickHouse
                里建好，建表语句见帮助文档
                <a
                  :href="HELP_URL"
                  target="_blank"
                  rel="noopener noreferrer"
                  class="link"
                  >《链路追踪配置说明》</a
                >；物化视图里取集群的属性要和上面的「集群字段」是同一个
              </li>
              <li>
                建议给 KubeDoor 单独建一个只读账号（readonly =
                2，只授权这两张表的 SELECT），不要用 Jaeger 写数据的账号
              </li>
            </ul>
            <el-form label-width="130px" @submit.prevent>
              <el-form-item label="启用">
                <el-switch v-model="form.clickhouse.enabled" />
                <span class="hint inline">随底部的「保存」一起生效</span>
              </el-form-item>
              <el-form-item
                label="HTTP 地址"
                :required="form.clickhouse.enabled"
              >
                <el-input
                  v-model="form.clickhouse.url"
                  clearable
                  placeholder="http://10.0.0.88:8123"
                />
                <div class="hint">
                  ClickHouse 的 HTTP 接口（默认端口 8123），KubeDoor
                  控制端要能访问
                </div>
              </el-form-item>
              <el-form-item label="库名" required>
                <el-input
                  v-model="form.clickhouse.database"
                  placeholder="jaeger"
                  class="short-input"
                />
                <span class="hint inline">
                  集群部署时填查询库（Distributed 表所在的库）
                </span>
              </el-form-item>
              <el-form-item label="搜索表" required>
                <el-input
                  v-model="form.clickhouse.table"
                  placeholder="trace_search"
                  class="short-input"
                />
              </el-form-item>
              <el-form-item label="汇总表">
                <el-input
                  v-model="form.clickhouse.dim_table"
                  clearable
                  placeholder="trace_search_dim"
                  class="short-input"
                />
                <span class="hint inline">
                  可选，用来列出服务和接口；没建可以留空
                </span>
              </el-form-item>
              <el-form-item label="账号">
                <el-input
                  v-model="form.clickhouse.username"
                  autocomplete="off"
                  placeholder="例如 kubedoor_ro"
                  class="short-input"
                />
              </el-form-item>
              <el-form-item label="密码">
                <!-- 点了「清除」后不能再填：保存 / 测试时带 clear_password，master 会忽略同时传来的密码 -->
                <el-input
                  v-model="form.clickhouse.password"
                  type="password"
                  show-password
                  autocomplete="new-password"
                  :disabled="form.clickhouse.clear_password"
                  :placeholder="passwordPlaceholder(form.clickhouse)"
                  class="short-input"
                />
                <template v-if="form.clickhouse.clear_password">
                  <el-tag
                    size="small"
                    type="warning"
                    effect="plain"
                    class="inline-tag"
                  >
                    将清除
                  </el-tag>
                  <el-button
                    link
                    type="primary"
                    class="inline-tag"
                    @click="form.clickhouse.clear_password = false"
                  >
                    撤销
                  </el-button>
                </template>
                <template v-else-if="form.clickhouse.has_password">
                  <el-tag
                    size="small"
                    type="success"
                    effect="plain"
                    class="inline-tag"
                  >
                    已配置
                  </el-tag>
                  <el-button
                    link
                    type="danger"
                    class="inline-tag"
                    @click="clearPassword"
                  >
                    清除
                  </el-button>
                </template>
                <span class="hint inline">
                  加密保存，保存后不再显示{{
                    form.clickhouse.has_password
                      ? "；改用没有密码的账号时点「清除」"
                      : ""
                  }}
                </span>
              </el-form-item>
              <el-form-item label="超时（秒）">
                <el-input-number
                  v-model="form.clickhouse.timeout"
                  :min="MIN_TIMEOUT"
                  :max="MAX_TIMEOUT"
                  :precision="0"
                />
                <span class="hint inline">
                  单条 SQL 最多执行多久（{{ MIN_TIMEOUT }}~{{ MAX_TIMEOUT }}
                  秒）
                </span>
              </el-form-item>
              <el-form-item label="HTTPS 证书">
                <el-checkbox v-model="form.clickhouse.tls_verify">
                  校验 HTTPS 证书
                </el-checkbox>
              </el-form-item>
              <el-form-item>
                <el-button
                  type="primary"
                  plain
                  :loading="testingClickHouse"
                  @click="runClickHouseTest"
                >
                  测试
                </el-button>
                <span class="hint inline">
                  用表单里当前的配置，不用先保存；检查能否连上、表里有没有需要的列，统计最近
                  10 分钟各集群取值的 Span 数
                </span>
              </el-form-item>
            </el-form>
            <template v-if="chTest && chView">
              <el-alert
                :type="chView.type"
                :title="chView.text"
                :closable="false"
                show-icon
              />
              <ul v-if="chMissing.length" class="rules">
                <li v-for="line in chMissing" :key="line" class="danger-text">
                  {{ line }}：搜索表、汇总表的列名要和帮助文档里的建表语句一致
                </li>
              </ul>
              <el-table
                v-if="chValues.length"
                :data="chValues"
                border
                size="small"
                class="result-block"
              >
                <el-table-column label="k8s_cluster 取值" min-width="200">
                  <template #default="{ row }">
                    <code v-if="row.value">{{ row.value }}</code>
                    <span v-else class="muted">（空）</span>
                  </template>
                </el-table-column>
                <el-table-column
                  prop="spans"
                  label="最近 10 分钟 Span 数"
                  width="170"
                />
                <el-table-column label="对应的 KubeDoor 集群" min-width="220">
                  <template #default="{ row }">
                    <el-tag size="small" effect="plain" :type="row.label.type">
                      {{ row.label.text }}
                    </el-tag>
                  </template>
                </el-table-column>
              </el-table>
              <ul v-if="chTest.messages?.length" class="rules">
                <li v-for="(text, index) in chTest.messages" :key="index">
                  {{ text }}
                </li>
              </ul>
            </template>
          </div>
        </el-card>

        <!-- 字段映射 -->
        <el-card id="tracing-fields" shadow="never" class="tracing-card">
          <template #header>
            <div class="card-header">
              <strong>字段映射</strong>
              <el-button
                link
                type="primary"
                :disabled="allDefault"
                @click="restoreAllFields"
              >
                全部恢复默认
              </el-button>
            </div>
          </template>
          <ul class="rules first">
            <li>
              告诉 KubeDoor 每类信息在链路的哪些属性里。OpenTelemetry 的 HTTP
              语义约定有新旧两版：新版（如 Java Agent 2.x）用
              http.request.method、http.response.status_code、url.path、url.full、server.address、client.address；旧版用
              http.method、http.status_code、http.target、http.url、http.host、net.peer.name。默认两版都填上了
            </li>
            <li>
              搜索时同时匹配所有候选属性名（「或」的关系）；从链路里取
              Pod、状态码等值时，按顺序取第一个有值的。每项最多
              {{ MAX_FIELD_NAMES }}
              个，可以输入新的属性名后按回车添加；留空表示不用这一项
            </li>
            <li>
              一般不用改；应用用了自定义的属性名时在对应行添加，下方的「识别字段」也会按样本里实际出现的属性给出建议
            </li>
          </ul>
          <div v-for="key in FIELD_KEYS" :key="key" class="field-row">
            <div class="field-label">
              <span>{{ labels[key] }}</span>
              <el-tag
                v-if="levels[key] === 'event'"
                size="small"
                type="info"
                effect="plain"
              >
                异常事件
              </el-tag>
              <el-tag
                v-if="!isDefaultField(key)"
                size="small"
                type="warning"
                effect="plain"
              >
                自定义
              </el-tag>
            </div>
            <div class="field-body">
              <div class="field-control">
                <el-select
                  v-model="form.fields[key]"
                  multiple
                  filterable
                  allow-create
                  default-first-option
                  :reserve-keyword="false"
                  :multiple-limit="MAX_FIELD_NAMES"
                  placeholder="不设置（AI 不能按这一项搜索）"
                  class="field-select"
                >
                  <el-option
                    v-for="option in options[key]"
                    :key="option.value"
                    :label="option.value"
                    :value="option.value"
                  >
                    <span>{{ option.value }}</span>
                    <span v-if="option.note" class="option-note">
                      {{ option.note }}
                    </span>
                  </el-option>
                </el-select>
                <el-button
                  link
                  type="primary"
                  :disabled="isDefaultField(key)"
                  @click="restoreField(key)"
                >
                  恢复默认
                </el-button>
              </div>
              <div class="hint">
                {{ FIELD_META[key].hint }}
                <span class="default-value">
                  默认：{{ defaults[key].join("、") || "（无）" }}
                </span>
              </div>
            </div>
          </div>
        </el-card>

        <!-- 业务字段 -->
        <el-card id="tracing-custom" shadow="never" class="tracing-card">
          <template #header><strong>业务字段</strong></template>
          <p class="card-note">
            应用在链路里记录了业务标识（例如订单号、用户
            ID）时，在这里登记属性名和中文说明。AI 看到「订单号
            SO2026…」时就知道按哪个属性查（精确匹配，Span 属性和资源属性都查）。
          </p>
          <div
            v-for="(row, index) in form.custom_fields"
            :key="row.id"
            class="custom-row"
          >
            <div class="custom-key">
              <el-autocomplete
                v-model="row.key"
                :fetch-suggestions="queryObservedKeys"
                :trigger-on-focus="!!analysis"
                placeholder="属性名，例如 biz.order_no"
              />
            </div>
            <el-input
              v-model="row.label"
              :maxlength="LABEL_MAX_LENGTH"
              placeholder="中文说明，例如 订单号"
              class="custom-label"
            />
            <el-button
              link
              type="danger"
              @click="form.custom_fields.splice(index, 1)"
            >
              删除
            </el-button>
          </div>
          <el-button
            size="small"
            :disabled="form.custom_fields.length >= MAX_CUSTOM_FIELDS"
            @click="addCustomField"
          >
            添加业务字段
          </el-button>
          <span class="hint inline">最多 {{ MAX_CUSTOM_FIELDS }} 个</span>
        </el-card>

        <!-- 打码字段 -->
        <el-card id="tracing-mask" shadow="never" class="tracing-card">
          <template #header><strong>打码字段</strong></template>
          <el-input-tag
            v-model="form.mask_keys"
            :max="MAX_MASK_KEYS"
            :maxlength="MASK_KEY_MAX_LENGTH"
            clearable
            placeholder="输入属性名后按回车添加，例如 x-sign"
          />
          <ul class="rules">
            <li>
              AI 查看 Span 详情、识别字段的示例值里，这些属性的值会换成
              ***，不会发给模型服务商。属性名（不区分大小写）等于某一项，或者以「.某项」「_某项」「-某项」结尾都算，例如
              http.request.header.authorization、db.password
            </li>
            <li>
              URL 类的值（url.full、url.query、http.target、db.connection_string
              等）里，参数名是敏感名的参数值换成 ***，user:pass@
              里的密码也会打码
            </li>
            <li>
              内置已经打码下面这些，这里只需要添加额外的（最多
              {{ MAX_MASK_KEYS }} 个）：
              <div class="tag-list builtin">
                <el-tag
                  v-for="name in builtinMasks"
                  :key="name"
                  size="small"
                  type="info"
                  effect="plain"
                >
                  {{ name }}
                </el-tag>
              </div>
            </li>
          </ul>
        </el-card>

        <!-- 识别字段 -->
        <el-card id="tracing-identify" shadow="never" class="tracing-card">
          <template #header><strong>识别字段</strong></template>
          <p class="card-note">
            取几条真实的链路，列出里面出现的属性，帮你确定集群字段和字段映射。用表单里当前的配置，不用先保存；样本只用来识别，不保存。
          </p>
          <el-radio-group v-model="identify.mode" class="mode-switch">
            <el-radio-button value="sample">从 Jaeger 取样</el-radio-button>
            <el-radio-button value="json">粘贴 Trace JSON</el-radio-button>
          </el-radio-group>
          <template v-if="identify.mode === 'sample'">
            <div class="identify-row">
              <el-select
                v-model="identify.service"
                filterable
                allow-create
                default-first-option
                clearable
                :placeholder="
                  services.length
                    ? '选择或输入服务名'
                    : '输入服务名，或点右边的「加载服务列表」'
                "
                class="service-select"
              >
                <el-option
                  v-for="name in services"
                  :key="name"
                  :label="name"
                  :value="name"
                />
              </el-select>
              <el-select v-model="identify.lookback" class="lookback-select">
                <el-option
                  v-for="item in LOOKBACK_OPTIONS"
                  :key="item.value"
                  :label="item.label"
                  :value="item.value"
                />
              </el-select>
              <el-button
                type="primary"
                :loading="analyzing"
                @click="runAnalyze"
              >
                识别
              </el-button>
              <el-button
                link
                type="primary"
                :loading="testingJaeger"
                @click="runJaegerTest"
              >
                加载服务列表
              </el-button>
            </div>
            <div class="hint">
              在 Jaeger 上按服务搜最近的链路（不按集群过滤），取最多 3
              条完整链路分析；服务名一般就是 Deployment
              名。服务列表来自上面的「测试连接」
            </div>
          </template>
          <template v-else>
            <el-input
              v-model="identify.json"
              type="textarea"
              :rows="8"
              placeholder="粘贴一条链路的 JSON：Jaeger UI 链路页的「Trace JSON」，或者 Jaeger 接口 /api/v3/traces/<链路 ID> 的返回，新旧两种格式都可以"
              class="json-input"
            />
            <div class="identify-row">
              <el-button
                type="primary"
                :loading="analyzing"
                @click="runAnalyze"
              >
                识别
              </el-button>
              <el-button @click="pickFile">从文件读取</el-button>
              <span class="hint inline">最大 5 MB</span>
              <input
                ref="fileInput"
                type="file"
                accept=".json,application/json"
                hidden
                @change="readFile"
              />
            </div>
          </template>

          <template v-if="analysis">
            <el-divider />
            <div class="analysis-summary">{{ analysisSummary(analysis) }}</div>
            <ul v-if="analysis.notes?.length" class="rules">
              <li v-for="(text, index) in analysis.notes" :key="index">
                {{ text }}
              </li>
            </ul>

            <div class="section-title">集群字段候选</div>
            <el-alert
              v-if="clusterCheck"
              :type="clusterCheck.type"
              :title="clusterCheck.text"
              :closable="false"
              show-icon
              class="block-alert"
            />
            <el-table
              v-if="candidates.length"
              :data="candidates"
              border
              size="small"
            >
              <el-table-column label="资源属性" min-width="170">
                <template #default="{ row }">
                  <code>{{ row.key }}</code>
                  <el-tag
                    v-if="row.key === suggestedClusterKey"
                    size="small"
                    type="success"
                    class="inline-tag"
                  >
                    推荐
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column
                label="样本里的取值 → 对上的集群"
                min-width="300"
              >
                <template #default="{ row }">
                  <div class="tag-list">
                    <el-tag
                      v-for="item in row.values"
                      :key="item.value"
                      size="small"
                      effect="plain"
                      :type="item.label.type"
                    >
                      {{ item.value || "（空）" }} → {{ item.label.text }}
                    </el-tag>
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="操作" width="130">
                <template #default="{ row }">
                  <el-tag
                    v-if="form.cluster_key.trim() === row.key"
                    size="small"
                    type="info"
                  >
                    当前
                  </el-tag>
                  <el-button
                    v-else
                    link
                    type="primary"
                    @click="setClusterKey(row.key)"
                  >
                    设为集群字段
                  </el-button>
                </template>
              </el-table-column>
            </el-table>
            <el-alert
              v-else
              type="warning"
              :closable="false"
              show-icon
              title="样本里没有哪个资源属性的取值能对上 KubeDoor 的集群名或别名"
            >
              <div class="alert-text">
                按上面「集群字段」的说明给应用加上集群属性（例如
                OTEL_RESOURCE_ATTRIBUTES=k8sname=&lt;集群名&gt;），或者到
                <router-link :to="AGENT_PAGE" class="link">
                  Agent 管理
                </router-link>
                页给集群加别名（别名填链路里的取值，可以在下面的「全部属性」里找）
              </div>
            </el-alert>

            <div class="section-title section-actions">
              <span>字段映射建议</span>
              <el-button size="small" :disabled="!applicable" @click="applyAll">
                全部套用（{{ applicable }}）
              </el-button>
            </div>
            <el-table :data="suggestions" border size="small">
              <el-table-column prop="label" label="字段" width="150" />
              <el-table-column label="当前候选" min-width="220">
                <template #default="{ row }">
                  <code v-for="name in row.current" :key="name" class="name">
                    {{ name }}
                  </code>
                  <span v-if="!row.current.length" class="muted">（无）</span>
                </template>
              </el-table-column>
              <el-table-column label="样本里出现的" min-width="220">
                <template #default="{ row }">
                  <code v-for="name in row.suggested" :key="name" class="name">
                    {{ name }}
                  </code>
                  <span v-if="row.state === 'none'" class="muted">
                    没有出现已知的属性名
                  </span>
                </template>
              </el-table-column>
              <el-table-column label="操作" width="90">
                <template #default="{ row }">
                  <el-button
                    v-if="row.state === 'apply'"
                    link
                    type="primary"
                    @click="applySuggestion(row)"
                  >
                    套用
                  </el-button>
                  <span v-else-if="row.state === 'same'" class="muted">
                    已一致
                  </span>
                  <span v-else class="muted">—</span>
                </template>
              </el-table-column>
            </el-table>
            <div class="hint">
              套用 =
              只保留样本里出现过的属性名（搜索时少几个「或」条件）。还有别的服务用其他属性名时，在字段映射里再加上，或者点「恢复默认」；样本里没有出现已知属性名的项，可以在下面的「全部属性」里找到实际的属性名，加到字段映射里
            </div>

            <div class="section-title section-actions">
              <span>全部属性（{{ (analysis.keys ?? []).length }}）</span>
              <div class="key-filter">
                <el-input
                  v-model="keyFilter.text"
                  size="small"
                  clearable
                  placeholder="按属性名筛选"
                  class="key-filter-input"
                />
                <el-select
                  v-model="keyFilter.level"
                  size="small"
                  clearable
                  placeholder="全部层级"
                  class="key-filter-level"
                >
                  <el-option
                    v-for="(label, value) in LEVEL_LABELS"
                    :key="value"
                    :label="label"
                    :value="value"
                  />
                </el-select>
              </div>
            </div>
            <el-table
              :data="filteredKeys"
              border
              size="small"
              max-height="420"
              empty-text="没有符合条件的属性"
            >
              <el-table-column label="属性名" min-width="220">
                <template #default="{ row }">
                  <code>{{ row.key }}</code>
                </template>
              </el-table-column>
              <el-table-column label="层级" width="80">
                <template #default="{ row }">
                  {{ levelLabel(row.level) }}
                </template>
              </el-table-column>
              <el-table-column label="类型" width="110">
                <template #default="{ row }">
                  {{ (row.types ?? []).join("、") }}
                </template>
              </el-table-column>
              <el-table-column prop="count" label="次数" width="90" sortable />
              <el-table-column
                label="示例"
                min-width="280"
                show-overflow-tooltip
              >
                <template #default="{ row }">
                  {{ (row.examples ?? []).join(" | ") }}
                </template>
              </el-table-column>
            </el-table>
          </template>
        </el-card>

        <!-- 保存：内容超过一屏时固定在底部 -->
        <el-affix position="bottom" :offset="0">
          <div class="save-bar">
            <el-button type="primary" :loading="saving" @click="save">
              保存
            </el-button>
            <el-button :disabled="busy" @click="reset">重置</el-button>
            <el-tag v-if="dirty" type="warning" effect="plain">
              有未保存的修改
            </el-tag>
            <span class="hint inline">
              保存后自动启用 · {{ modifiedText }}
            </span>
          </div>
        </el-affix>
      </template>
    </div>
  </div>
</template>

<style scoped lang="scss">
.system-tracing {
  max-width: 1280px;
  padding: 16px 16px 0;

  @media (width <= 640px) {
    padding: 8px 8px 0;
  }
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
    max-width: 960px;
    margin: 0;
    line-height: 1.7;
    color: var(--el-text-color-secondary);
  }
}

.heading-actions,
.card-header,
.card-title {
  display: flex;
  gap: 8px;
  align-items: center;
}

.card-header {
  justify-content: space-between;

  &.clickable {
    cursor: pointer;
  }
}

.card-title {
  flex-wrap: wrap;
}

// el-alert 自带 margin: 0，工具类压不过，用带作用域的类
.page-alert,
.block-alert {
  margin-bottom: 16px;
}

.block-alert {
  margin-top: 12px;
}

.cards {
  min-height: 120px;
}

.tracing-card {
  margin-bottom: 16px;
  scroll-margin-top: 12px;
}

.card-note {
  margin: 0 0 16px;
  font-size: 13px;
  line-height: 1.7;
  color: var(--el-text-color-regular);
}

// 表单项里的提示独占一行（在输入框下面）；inline 的跟在控件后面
.hint {
  display: block;
  width: 100%;
  font-size: 12px;
  line-height: 1.6;
  color: var(--el-text-color-secondary);

  &.inline {
    display: inline;
    width: auto;
    margin-left: 8px;
  }
}

.headers {
  width: 100%;
}

.header-row,
.custom-row,
.identify-row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  margin-bottom: 8px;
}

.header-name {
  flex: 0 0 220px;
}

.header-row .el-input:not(.header-name) {
  flex: 1;
  min-width: 200px;
}

.cluster-key-input {
  width: 360px;
  max-width: 100%;
}

// 两个 el-autocomplete 撑满外层的 div
.cluster-key-input,
.custom-key {
  :deep(.el-autocomplete) {
    width: 100%;
  }
}

.short-input {
  width: 300px;
  max-width: 100%;
}

.inline-tag {
  margin-left: 8px;
}

.suggestion {
  display: flex;
  gap: 12px;
  justify-content: space-between;
}

.option-note {
  margin-left: 12px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.section-title {
  margin: 16px 0 8px;
  font-weight: 600;
}

.section-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  justify-content: space-between;
}

.rules {
  padding-left: 20px;
  margin: 8px 0 4px;
  font-size: 13px;
  line-height: 1.8;
  color: var(--el-text-color-regular);
  list-style: disc;

  &.first {
    margin-top: 0;
    margin-bottom: 16px;
  }
}

.example {
  padding: 8px 12px;
  margin: 6px 0;
  font-family: Menlo, Consolas, monospace;
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  background: var(--el-fill-color-light);
  border-radius: 4px;
}

code {
  padding: 0 4px;
  font-family: Menlo, Consolas, monospace;
  font-size: 12px;
  background: var(--el-fill-color-light);
  border-radius: 3px;
}

code.name {
  display: inline-block;
  margin: 2px 6px 2px 0;
}

.tag-list {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 6px;

  &.builtin {
    margin-top: 4px;
  }
}

.alert-text {
  margin-top: 6px;
  line-height: 1.7;
}

.result-block {
  margin-top: 12px;

  // 测试结果的说明表：标签列不要占一半宽度
  :deep(.el-descriptions__label) {
    width: 140px;
    white-space: nowrap;
  }
}

.field-row {
  display: grid;
  grid-template-columns: 170px minmax(0, 1fr);
  gap: 12px;
  padding: 10px 0;
  border-top: 1px solid var(--el-border-color-lighter);

  @media (width <= 768px) {
    grid-template-columns: minmax(0, 1fr);
    gap: 4px;
  }
}

.field-label {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
  min-height: 32px;
  font-size: 14px;
  font-weight: 500;
}

.field-control {
  display: flex;
  gap: 8px;
  align-items: center;
}

.field-select {
  flex: 1;
  min-width: 0;
}

.default-value {
  margin-left: 6px;
}

.custom-key {
  flex: 0 0 300px;
  max-width: 100%;
}

.custom-label {
  flex: 0 0 240px;
  max-width: 100%;
}

.mode-switch {
  margin-bottom: 12px;
}

.service-select {
  width: 360px;
  max-width: 100%;
}

.lookback-select {
  width: 150px;
}

.json-input {
  margin-bottom: 8px;

  :deep(textarea) {
    font-family: Menlo, Consolas, monospace;
    font-size: 12px;
  }
}

.analysis-summary {
  font-size: 13px;
  color: var(--el-text-color-regular);
}

.key-filter {
  display: flex;
  gap: 8px;
  font-weight: normal;
}

.key-filter-input {
  width: 200px;
}

.key-filter-level {
  width: 120px;
}

.muted {
  color: var(--el-text-color-secondary);
}

.danger-text {
  color: var(--el-color-danger);
}

.link {
  color: var(--el-color-primary);

  &:hover {
    text-decoration: underline;
  }
}

.save-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 12px;
  align-items: center;
  padding: 12px 16px;
  margin-bottom: 16px;
  background: var(--el-bg-color);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 4px;
  box-shadow: 0 -2px 8px rgb(0 0 0 / 6%);

  .hint.inline {
    margin-left: 0;
  }
}

// 窄屏：请求头名称、业务字段两个输入框各占一行
.header-name,
.custom-key,
.custom-label {
  @media (width <= 640px) {
    flex: 1 1 100%;
  }
}
</style>
