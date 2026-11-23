<script setup lang="ts">
// 日志源的编辑抽屉：改的是副本，点「确定」才写回表单（写进数据库还要点页面底部的「保存」）
import { computed, ref, watch } from "vue";
// ElInputTag 没有在 plugins/elementPlus.ts 全局注册，这里单独引入
import { ElInputTag } from "element-plus";
import MarkdownEditor from "./MarkdownEditor.vue";
import {
  type LogSourcesForm,
  type ScanMap,
  type SemanticMeta,
  type SourceForm,
  DURATION_UNITS,
  KIND_OPTIONS,
  LEVEL_KEYS,
  LEVEL_EXAMPLES,
  LEVEL_LABELS,
  LEVEL_VALUE_MAX_LENGTH,
  MAX_COLUMNS,
  MAX_CORRELATIONS,
  MAX_DEFAULT_FIELDS,
  MAX_LEVEL_VALUES,
  MAX_MASK_FIELDS,
  MAX_MASK_PATTERNS,
  SOURCE_DESCRIPTION_MAX_LENGTH,
  cloneDeep,
  columnOptions,
  correlationForm,
  nextRowId,
  scanGroupFor,
  sourceErrors,
  targetOptions,
  typeLabel
} from "../log-sources-utils";

defineOptions({ name: "LogSourceDrawer" });

const visible = defineModel<boolean>("visible", { required: true });

const props = defineProps<{
  /** 要编辑的日志源（打开时复制一份）；新建时是 emptySource 的结果 */
  source: SourceForm | null;
  creating: boolean;
  form: LogSourcesForm;
  scans: ScanMap;
  semantic: SemanticMeta[];
  builtinMasks: string[];
}>();

const emit = defineEmits<{
  save: [source: SourceForm, originalId: string | null];
}>();

const draft = ref<SourceForm | null>(null);
const errors = ref<string[]>([]);
const top = ref<HTMLElement | null>(null);
/** 字段说明的输入框共用一个 datalist（几百行时比下拉框轻） */
const listId = `log-columns-${nextRowId()}`;

const title = computed(() =>
  props.creating
    ? "添加日志源"
    : `编辑日志源：${props.source?.name || props.source?.id || ""}`
);
const group = computed(() =>
  draft.value ? scanGroupFor(props.scans, props.form, draft.value) : null
);
const fieldOptions = computed(() => columnOptions(group.value));
const timeOptions = computed(() => columnOptions(group.value, true));
const targets = computed(() => targetOptions(group.value));
const connection = computed(() =>
  props.form.connections.find(item => item.id === draft.value?.connection)
);
const hasMessage = computed(() => !!draft.value?.fields.message?.trim());
const describedCount = computed(
  () => draft.value?.columns.filter(row => row.description.trim()).length ?? 0
);

watch(visible, open => {
  if (!open || !props.source) return;
  draft.value = cloneDeep(props.source);
  errors.value = [];
});

function addCorrelation() {
  draft.value?.correlations.push(
    correlationForm({ key: props.form.correlation_keys[0]?.key ?? "" })
  );
}

function addColumn() {
  draft.value?.columns.push({ rowId: nextRowId(), name: "", description: "" });
}

/** 把扫描到的字段里还没有的加进字段说明（说明留空，写了说明的才会保存） */
function importColumns() {
  const source = draft.value;
  if (!source || !group.value) return;
  const names = new Set(source.columns.map(row => row.name.trim()));
  for (const item of group.value.columns) {
    if (source.columns.length >= MAX_COLUMNS) break;
    if (names.has(item.name)) continue;
    source.columns.push({
      rowId: nextRowId(),
      name: item.name,
      description: ""
    });
    names.add(item.name);
  }
}

/** 去掉没写说明的字段说明行 */
function dropEmptyColumns() {
  if (draft.value)
    draft.value.columns = draft.value.columns.filter(row =>
      row.description.trim()
    );
}

function confirm() {
  const source = draft.value;
  if (!source) return;
  const list = sourceErrors(props.form, source);
  const id = source.id.trim();
  if (
    id &&
    props.form.sources.some(
      item => item.rowId !== source.rowId && item.id === id
    )
  )
    list.unshift(`ID：${id} 和别的日志源重复了`);
  errors.value = list;
  if (list.length) {
    top.value?.scrollIntoView({ behavior: "smooth", block: "start" });
    return;
  }
  source.id = id;
  emit("save", source, props.creating ? null : (props.source?.id ?? null));
  visible.value = false;
}
</script>

<template>
  <el-drawer
    v-model="visible"
    :title="title"
    size="880px"
    :close-on-click-modal="false"
    destroy-on-close
    class="log-source-drawer"
  >
    <div v-if="draft" ref="top" class="drawer-body">
      <el-alert
        v-if="errors.length"
        type="error"
        :closable="false"
        show-icon
        title="请先修改这些地方"
        class="drawer-alert"
      >
        <ul class="problem-list">
          <li v-for="item in errors" :key="item">{{ item }}</li>
        </ul>
      </el-alert>

      <el-form label-width="120px" @submit.prevent>
        <div class="part-title">基本信息</div>
        <el-form-item label="ID" required>
          <el-input v-model="draft.id" maxlength="32" class="short-input" />
          <span class="field-hint inline">
            AI 用它指定日志源；改了会同步各集群的绑定
          </span>
        </el-form-item>
        <el-form-item label="名称" required>
          <el-input
            v-model="draft.name"
            maxlength="50"
            placeholder="例如 入口网关访问日志"
            class="short-input"
          />
        </el-form-item>
        <el-form-item label="类型">
          <el-radio-group v-model="draft.kind">
            <el-radio-button
              v-for="item in KIND_OPTIONS"
              :key="item.value"
              :value="item.value"
            >
              {{ item.label }}
            </el-radio-button>
          </el-radio-group>
          <span class="field-hint inline">
            {{ KIND_OPTIONS.find(item => item.value === draft.kind)?.hint }}
          </span>
        </el-form-item>
        <el-form-item label="连接" required>
          <el-select
            v-model="draft.connection"
            placeholder="选择连接"
            class="short-input"
          >
            <el-option
              v-for="item in form.connections"
              :key="item.rowId"
              :label="`${item.name || item.id}（${item.id}）`"
              :value="item.id"
            >
              <span>{{ item.name || item.id }}</span>
              <span class="option-note">
                {{ typeLabel(item.type) }} · {{ item.id }}
              </span>
            </el-option>
          </el-select>
          <span class="field-hint inline">
            各集群绑定默认用这个连接；绑定里也可以单独指定同类型的其他连接
          </span>
        </el-form-item>
        <el-form-item label="启用">
          <el-switch v-model="draft.enabled" />
          <span class="field-hint inline">
            停用后 AI 查不到这个日志源，集群绑定保留
          </span>
        </el-form-item>
        <el-form-item label="说明">
          <MarkdownEditor
            v-model="draft.description"
            :rows="7"
            :maxlength="SOURCE_DESCRIPTION_MAX_LENGTH"
            placeholder="给 AI 看：用途、在请求链路里的位置、字段规律、查询建议。例如&#10;- 入口网关的访问日志，每个请求一行&#10;- 大表：先按时间和集群字段过滤，时间窗口不要超过 1 天"
          />
        </el-form-item>

        <div class="part-title">
          字段
          <span v-if="group" class="part-note">
            候选来自扫描结果（{{ group.name_guess || group.ref }}，{{
              group.columns.length
            }}
            个字段），也可以手填
          </span>
          <span v-else class="part-note">
            没有扫描结果，字段名要手填；在「智能配置」里扫描这个连接后会有候选
          </span>
        </div>
        <el-form-item label="时间字段" required>
          <el-select
            v-model="draft.time_field"
            filterable
            allow-create
            default-first-option
            clearable
            value-on-clear=""
            placeholder="例如 @timestamp、start_time"
            class="field-select"
          >
            <el-option
              v-for="item in timeOptions"
              :key="item.value"
              :label="item.value"
              :value="item.value"
            >
              <span>{{ item.value }}</span>
              <span class="option-note">{{ item.note }}</span>
            </el-option>
          </el-select>
          <span class="field-hint inline">按它过滤时间范围和排序</span>
        </el-form-item>
        <el-form-item
          v-for="item in semantic"
          :key="item.key"
          :label="item.label"
        >
          <el-select
            v-model="draft.fields[item.key]"
            filterable
            allow-create
            default-first-option
            clearable
            value-on-clear=""
            placeholder="不设置"
            class="field-select"
          >
            <el-option
              v-for="option in fieldOptions"
              :key="option.value"
              :label="option.value"
              :value="option.value"
            >
              <span>{{ option.value }}</span>
              <span class="option-note">{{ option.note }}</span>
            </el-option>
          </el-select>
          <span class="field-hint inline">{{ item.hint }}</span>
        </el-form-item>
        <el-form-item label="耗时单位">
          <el-select v-model="draft.duration_unit" class="unit-select">
            <el-option
              v-for="item in DURATION_UNITS"
              :key="item.value"
              :label="item.label"
              :value="item.value"
            />
          </el-select>
          <span class="field-hint inline">耗时字段的单位</span>
        </el-form-item>
        <el-form-item label="级别取值">
          <div class="levels">
            <div v-for="key in LEVEL_KEYS" :key="key" class="level-row">
              <span class="level-name">{{ key }} {{ LEVEL_LABELS[key] }}</span>
              <el-input-tag
                :model-value="draft.level_values[key]"
                :max="MAX_LEVEL_VALUES"
                :maxlength="LEVEL_VALUE_MAX_LENGTH"
                clearable
                :placeholder="`输入取值后回车，例如 ${LEVEL_EXAMPLES[key]}`"
                class="level-input"
                @update:model-value="
                  value => (draft.level_values[key] = value ?? [])
                "
              />
            </div>
            <div class="field-hint">
              AI 按 error / warn
              等过滤时匹配这些取值；不填时按级别名的大写、小写、首字母大写匹配。数字级别（例如
              50）也可以
            </div>
          </div>
        </el-form-item>

        <div class="part-title">
          关联字段
          <span class="part-note">
            这类日志里串起请求的 ID 在哪：一列，或者正文里的写法
          </span>
        </div>
        <div v-if="!form.correlation_keys.length" class="part-empty">
          还没有关联字段：先在页面下方的「关联字段」卡片添加（例如
          trace_id、request_id），再回来登记
        </div>
        <template v-else>
          <div
            v-for="(row, index) in draft.correlations"
            :key="row.rowId"
            class="line-row"
          >
            <el-select
              v-model="row.key"
              placeholder="关联字段"
              class="key-select"
            >
              <el-option
                v-for="item in form.correlation_keys"
                :key="item.rowId"
                :label="`${item.key}（${item.label}）`"
                :value="item.key"
              />
            </el-select>
            <el-radio-group v-model="row.mode" size="small">
              <el-radio-button value="field">列</el-radio-button>
              <el-radio-button value="text" :disabled="!hasMessage">
                正文写法
              </el-radio-button>
            </el-radio-group>
            <el-select
              v-if="row.mode === 'field'"
              v-model="row.field"
              filterable
              allow-create
              default-first-option
              placeholder="字段名"
              class="value-select"
            >
              <el-option
                v-for="option in fieldOptions"
                :key="option.value"
                :label="option.value"
                :value="option.value"
              />
            </el-select>
            <el-input
              v-else
              v-model="row.text"
              maxlength="100"
              placeholder='例如 TID:{value} 或 "traceId":"{value}"'
              class="value-select mono-input"
            />
            <el-button
              link
              type="danger"
              @click="draft.correlations.splice(index, 1)"
            >
              删除
            </el-button>
          </div>
          <el-button
            size="small"
            :disabled="draft.correlations.length >= MAX_CORRELATIONS"
            @click="addCorrelation"
          >
            登记关联字段
          </el-button>
          <span class="field-hint inline">
            正文写法要先配置「正文」字段，{value} 代表取值；最多
            {{ MAX_CORRELATIONS }} 个
          </span>
        </template>

        <div class="part-title">
          字段说明
          <span class="part-note">
            给 AI
            看的字段含义（枚举值、单位、和别的字段的关系）；只有写了说明的字段才保存，已写
            {{ describedCount }} 个
          </span>
        </div>
        <div class="part-actions">
          <el-button size="small" :disabled="!group" @click="importColumns">
            从扫描结果带出字段
          </el-button>
          <el-button
            size="small"
            :disabled="draft.columns.length >= MAX_COLUMNS"
            @click="addColumn"
          >
            添加
          </el-button>
          <el-button
            v-if="draft.columns.length > describedCount"
            size="small"
            link
            type="primary"
            @click="dropEmptyColumns"
          >
            去掉没写说明的行
          </el-button>
        </div>
        <datalist :id="listId">
          <option
            v-for="item in fieldOptions"
            :key="item.value"
            :value="item.value"
          />
        </datalist>
        <div v-if="draft.columns.length" class="columns">
          <div
            v-for="(row, index) in draft.columns"
            :key="row.rowId"
            class="line-row"
          >
            <el-input
              v-model="row.name"
              :list="listId"
              placeholder="字段名"
              class="column-name mono-input"
            />
            <el-input
              v-model="row.description"
              maxlength="300"
              placeholder="说明，例如 上游集群名，格式 outbound|端口||服务名"
              class="column-desc"
            />
            <el-button
              link
              type="danger"
              @click="draft.columns.splice(index, 1)"
            >
              删除
            </el-button>
          </div>
        </div>

        <div class="part-title">查询与打码</div>
        <el-form-item label="默认返回字段">
          <el-select
            v-model="draft.default_fields"
            multiple
            filterable
            allow-create
            default-first-option
            :multiple-limit="MAX_DEFAULT_FIELDS"
            placeholder="不填：时间 + 已配置的语义字段 + 关联字段"
            class="wide-select"
          >
            <el-option
              v-for="item in fieldOptions"
              :key="item.value"
              :label="item.value"
              :value="item.value"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="打码字段">
          <el-select
            v-model="draft.mask_fields"
            multiple
            filterable
            allow-create
            default-first-option
            :multiple-limit="MAX_MASK_FIELDS"
            placeholder="整列的值换成 ***，例如 x-sign"
            class="wide-select"
          >
            <el-option
              v-for="item in fieldOptions"
              :key="item.value"
              :label="item.value"
              :value="item.value"
            />
          </el-select>
          <div class="field-hint">
            内置已经打码这些名称（以及 .名称、_名称、-名称 结尾的列），文本里的
            名称=值、"名称": "值"、user:pass@、Bearer 也会打码：
          </div>
          <div class="tag-list">
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
        </el-form-item>
        <el-form-item label="打码正则">
          <div class="patterns">
            <div
              v-for="index in draft.mask_patterns.length"
              :key="index"
              class="line-row"
            >
              <el-input
                v-model="draft.mask_patterns[index - 1]"
                maxlength="200"
                placeholder="例如 card=(\d+)"
                class="pattern-input mono-input"
              />
              <el-button
                link
                type="danger"
                @click="draft.mask_patterns.splice(index - 1, 1)"
              >
                删除
              </el-button>
            </div>
            <el-button
              size="small"
              :disabled="draft.mask_patterns.length >= MAX_MASK_PATTERNS"
              @click="draft.mask_patterns.push('')"
            >
              添加正则
            </el-button>
            <span class="field-hint inline">
              有捕获组时只替换第 1 组，否则替换整段；最多
              {{ MAX_MASK_PATTERNS }} 个
            </span>
          </div>
        </el-form-item>

        <div class="part-title">
          集群绑定的默认值
          <span class="part-note">在「集群」里添加绑定时默认用这些</span>
        </div>
        <el-form-item label="默认表 / 索引">
          <el-select
            v-model="draft.default_target"
            filterable
            allow-create
            default-first-option
            clearable
            value-on-clear=""
            :placeholder="
              connection?.type === 'elasticsearch'
                ? '索引，例如 app-logs-*（多个用逗号分隔）'
                : '库.表，例如 logs.ingress_access'
            "
            class="field-select"
          >
            <el-option
              v-for="item in targets"
              :key="item.value"
              :label="item.value"
              :value="item.value"
            >
              <span>{{ item.value }}</span>
              <span class="option-note">{{ item.note }}</span>
            </el-option>
          </el-select>
        </el-form-item>
        <el-form-item label="集群字段">
          <el-select
            v-model="draft.cluster_field"
            filterable
            allow-create
            default-first-option
            clearable
            value-on-clear=""
            placeholder="例如 env、k8s_cluster"
            class="field-select"
          >
            <el-option
              v-for="item in fieldOptions"
              :key="item.value"
              :label="item.value"
              :value="item.value"
            >
              <span>{{ item.value }}</span>
              <span class="option-note">{{ item.note }}</span>
            </el-option>
          </el-select>
          <span class="field-hint inline">
            区分集群的字段：新建绑定时默认出条件「字段 ∈
            {集群名}」；一表一集群时留空
          </span>
        </el-form-item>
      </el-form>
    </div>
    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button type="primary" @click="confirm">确定</el-button>
    </template>
  </el-drawer>
</template>

<style scoped lang="scss">
.drawer-body {
  padding-right: 8px;
}

.drawer-alert {
  margin-bottom: 12px;
}

.problem-list {
  padding-left: 18px;
  margin: 4px 0 0;
  line-height: 1.7;
  list-style: disc;
}

.part-title {
  padding-top: 12px;
  margin: 8px 0 12px;
  font-size: 15px;
  font-weight: 600;
  border-top: 1px solid var(--el-border-color-lighter);

  &:first-of-type {
    padding-top: 0;
    border-top: 0;
  }
}

.part-note {
  margin-left: 8px;
  font-size: 12px;
  font-weight: normal;
  color: var(--el-text-color-secondary);
}

.part-empty {
  margin-bottom: 8px;
  font-size: 13px;
  color: var(--el-text-color-secondary);
}

.part-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  margin-bottom: 8px;
}

.field-hint {
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

.short-input {
  width: 300px;
  max-width: 100%;
}

.field-select {
  width: 300px;
  max-width: 100%;
}

.unit-select {
  width: 160px;
}

.wide-select {
  width: 100%;
}

.levels,
.patterns {
  width: 100%;
}

.level-row {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-bottom: 6px;
}

.level-name {
  flex: 0 0 96px;
  font-size: 13px;
  color: var(--el-text-color-regular);
}

.level-input {
  flex: 1;
}

.line-row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  margin-bottom: 8px;
}

.key-select {
  width: 220px;
}

.value-select {
  flex: 1;
  min-width: 200px;
}

.columns {
  max-height: 420px;
  padding-right: 4px;
  overflow: auto;
}

.column-name {
  flex: 0 0 240px;
}

.column-desc {
  flex: 1;
  min-width: 240px;
}

.pattern-input {
  flex: 1;
  min-width: 240px;
}

.mono-input :deep(input) {
  font-family: Menlo, Consolas, monospace;
  font-size: 12px;
}

.tag-list {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 6px;
  margin-top: 4px;
}

.option-note {
  margin-left: 12px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
</style>
