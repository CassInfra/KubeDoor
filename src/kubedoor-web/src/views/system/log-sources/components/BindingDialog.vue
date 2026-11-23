<script setup lang="ts">
// 集群绑定的编辑对话框：日志源、连接、表 / 索引、过滤条件、说明；改的是副本，点「确定」才写回表单
import { computed, ref, watch } from "vue";
import {
  type BindingForm,
  type ConditionForm,
  type LogSourcesForm,
  type ScanMap,
  CONDITION_OPS,
  MAX_CONDITIONS,
  bindingErrors,
  cloneDeep,
  columnOptions,
  conditionsText,
  defaultBinding,
  groupOfTarget,
  isMultiOp,
  scanGroupFor,
  targetOptions,
  typeLabel,
  valueOptions
} from "../log-sources-utils";

defineOptions({ name: "LogBindingDialog" });

const visible = defineModel<boolean>("visible", { required: true });

const props = defineProps<{
  /** 要编辑的绑定（打开时复制一份）；新建时 source 为空 */
  binding: BindingForm | null;
  creating: boolean;
  env: string;
  /** 集群的别名：条件取值能对上别名时用表里的写法 */
  aliases: string[];
  form: LogSourcesForm;
  scans: ScanMap;
}>();

const emit = defineEmits<{
  save: [binding: BindingForm];
  /** 用对话框里还没确定的内容预览 */
  preview: [binding: BindingForm];
}>();

const draft = ref<BindingForm | null>(null);
const errors = ref<string[]>([]);

const source = computed(
  () => props.form.sources.find(item => item.id === draft.value?.source) ?? null
);
const sourceConnection = computed(
  () =>
    props.form.connections.find(item => item.id === source.value?.connection) ??
    null
);
/** 和日志源的连接同类型的连接：多于一个时才让选 */
const sameType = computed(() =>
  sourceConnection.value
    ? props.form.connections.filter(
        item => item.type === sourceConnection.value.type
      )
    : []
);
const connection = computed(() =>
  draft.value?.connection
    ? props.form.connections.find(item => item.id === draft.value.connection)
    : sourceConnection.value
);
const scan = computed(() =>
  connection.value ? (props.scans[connection.value.id] ?? null) : null
);
const group = computed(
  () =>
    groupOfTarget(scan.value, draft.value?.target ?? "") ??
    (source.value ? scanGroupFor(props.scans, props.form, source.value) : null)
);
const targets = computed(() => targetOptions(group.value));
const fieldOptions = computed(() => columnOptions(group.value));
/** 这个集群里已经绑定了的日志源（自己除外） */
const bound = computed(
  () =>
    props.form.clusters
      .find(item => item.env === props.env)
      ?.bindings.filter(item => item.rowId !== draft.value?.rowId)
      .map(item => item.source) ?? []
);
const isClickHouse = computed(() => connection.value?.type !== "elasticsearch");

watch(visible, open => {
  if (!open || !props.binding) return;
  draft.value = cloneDeep(props.binding);
  errors.value = [];
});

/** 新建时选了日志源：按它的默认表 / 索引、集群字段和扫描建议填默认值 */
function onSourceChange(id: string) {
  const item = props.form.sources.find(row => row.id === id);
  if (!props.creating || !draft.value || !item) return;
  const next = defaultBinding(
    item,
    props.env,
    props.scans[item.connection],
    props.aliases
  );
  draft.value.connection = "";
  draft.value.target = next.target;
  draft.value.conditions = next.conditions;
}

const valuesFor = (cond: ConditionForm) =>
  valueOptions(scan.value, draft.value?.target ?? "", cond.field);

function setSingle(cond: ConditionForm, value: unknown) {
  cond.values =
    typeof value === "string" || typeof value === "number"
      ? value === ""
        ? []
        : [value]
      : [];
}

function onOpChange(cond: ConditionForm) {
  if (!isMultiOp(cond.op)) cond.values = cond.values.slice(0, 1);
}

function addCondition() {
  draft.value?.conditions.push({
    field: source.value?.cluster_field || "",
    op: "in",
    values: []
  });
}

function check(): boolean {
  const binding = draft.value;
  if (!binding) return false;
  const list = bindingErrors(props.form, binding);
  if (binding.source && bound.value.includes(binding.source))
    list.unshift(`日志源：${binding.source} 在这个集群已经绑定过了`);
  errors.value = list;
  return !list.length;
}

function confirm() {
  if (!check()) return;
  emit("save", draft.value);
  visible.value = false;
}

function preview() {
  if (check()) emit("preview", cloneDeep(draft.value));
}
</script>

<template>
  <el-dialog
    v-model="visible"
    :title="
      creating ? `添加绑定：${env}` : `编辑绑定：${env} / ${binding?.source}`
    "
    width="840px"
    :close-on-click-modal="false"
    class="log-binding-dialog"
  >
    <template v-if="draft">
      <el-alert
        v-if="errors.length"
        type="error"
        :closable="false"
        show-icon
        title="请先修改这些地方"
        class="dialog-alert"
      >
        <ul class="problem-list">
          <li v-for="item in errors" :key="item">{{ item }}</li>
        </ul>
      </el-alert>
      <el-form label-width="110px" @submit.prevent>
        <el-form-item label="日志源" required>
          <el-select
            v-model="draft.source"
            filterable
            placeholder="选择日志源"
            class="wide-input"
            @change="onSourceChange"
          >
            <el-option
              v-for="item in form.sources"
              :key="item.rowId"
              :label="`${item.name || item.id}（${item.id}）`"
              :value="item.id"
              :disabled="bound.includes(item.id)"
            >
              <span>{{ item.name || item.id }}</span>
              <span class="option-note">
                {{ item.id }}{{ bound.includes(item.id) ? " · 已绑定" : "" }}
              </span>
            </el-option>
          </el-select>
        </el-form-item>
        <el-form-item v-if="sameType.length > 1" label="连接">
          <el-select
            v-model="draft.connection"
            clearable
            value-on-clear=""
            :placeholder="`同日志源（${sourceConnection?.name || sourceConnection?.id}）`"
            class="wide-input"
          >
            <el-option
              v-for="item in sameType"
              :key="item.rowId"
              :label="`${item.name || item.id}（${item.id}）`"
              :value="item.id"
            />
          </el-select>
          <div class="field-hint">
            这个集群的日志在另一个{{
              typeLabel(sourceConnection?.type ?? "")
            }}里时选；不选用日志源的连接
          </div>
        </el-form-item>
        <el-form-item label="表 / 索引" required>
          <el-select
            v-model="draft.target"
            filterable
            allow-create
            default-first-option
            clearable
            value-on-clear=""
            :placeholder="
              isClickHouse
                ? '库.表，例如 logs.ingress_access'
                : '例如 app-logs-*'
            "
            class="wide-input"
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
          <div class="field-hint">
            {{
              isClickHouse
                ? "ClickHouse 写 库.表；集群部署时写分布式表"
                : "Elasticsearch 写索引、别名或 data stream，可以用 * 通配，多个用逗号分隔，- 开头表示排除"
            }}
          </div>
        </el-form-item>
        <el-form-item label="过滤条件">
          <div class="conditions">
            <div
              v-for="(cond, index) in draft.conditions"
              :key="index"
              class="cond-row"
            >
              <el-select
                v-model="cond.field"
                filterable
                allow-create
                default-first-option
                placeholder="字段"
                class="cond-field"
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
              <el-select
                v-model="cond.op"
                class="cond-op"
                @change="onOpChange(cond)"
              >
                <el-option
                  v-for="item in CONDITION_OPS"
                  :key="item.value"
                  :label="item.label"
                  :value="item.value"
                />
              </el-select>
              <el-select
                v-if="isMultiOp(cond.op)"
                v-model="cond.values"
                multiple
                filterable
                allow-create
                default-first-option
                placeholder="取值，可以多个"
                class="cond-values"
              >
                <el-option
                  v-for="value in valuesFor(cond)"
                  :key="String(value)"
                  :label="String(value)"
                  :value="value"
                />
              </el-select>
              <el-select
                v-else
                :model-value="cond.values[0]"
                filterable
                allow-create
                default-first-option
                clearable
                placeholder="取值"
                class="cond-values"
                @update:model-value="value => setSingle(cond, value)"
              >
                <el-option
                  v-for="value in valuesFor(cond)"
                  :key="String(value)"
                  :label="String(value)"
                  :value="value"
                />
              </el-select>
              <el-button
                link
                type="danger"
                @click="draft.conditions.splice(index, 1)"
              >
                删除
              </el-button>
            </div>
            <el-button
              size="small"
              :disabled="draft.conditions.length >= MAX_CONDITIONS"
              @click="addCondition"
            >
              添加条件
            </el-button>
            <span class="field-hint inline">
              全部 AND；没有条件 = 整张表 /
              整个索引都属于这个集群。取值候选来自扫描结果
            </span>
            <div class="cond-preview">
              {{
                conditionsText(draft.conditions) ||
                (isClickHouse ? "整张表" : "整个索引")
              }}
            </div>
          </div>
        </el-form-item>
        <el-form-item label="说明">
          <el-input
            v-model="draft.note"
            type="textarea"
            :rows="2"
            maxlength="2000"
            show-word-limit
            placeholder="给 AI 看，例如「外部 nginx 不区分集群，含 prod-a、prod-b 两个集群的流量」"
          />
        </el-form-item>
      </el-form>
    </template>
    <template #footer>
      <el-button @click="preview">预览</el-button>
      <el-button @click="visible = false">取消</el-button>
      <el-button type="primary" @click="confirm">确定</el-button>
    </template>
  </el-dialog>
</template>

<style scoped lang="scss">
.dialog-alert {
  margin-bottom: 12px;
}

.problem-list {
  padding-left: 18px;
  margin: 4px 0 0;
  line-height: 1.7;
  list-style: disc;
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

.wide-input {
  width: 420px;
  max-width: 100%;
}

.conditions {
  width: 100%;
}

.cond-row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  margin-bottom: 8px;
}

.cond-field {
  width: 180px;
}

.cond-op {
  width: 130px;
}

.cond-values {
  flex: 1;
  min-width: 200px;
}

.cond-preview {
  padding: 4px 8px;
  margin-top: 8px;
  font-family: Menlo, Consolas, monospace;
  font-size: 12px;
  background: var(--el-fill-color-light);
  border-radius: 4px;
}

.option-note {
  margin-left: 12px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
</style>
