<script setup lang="ts">
import { computed } from "vue";
import { Bottom, Delete, Plus, Top } from "@element-plus/icons-vue";
import {
  type ConditionForm,
  type Issue,
  type RuleForm,
  FIELDS,
  FIELD_HELP,
  FIELD_LABELS,
  OPERATOR_HELP,
  OPERATOR_LABELS,
  SEVERITIES,
  SEVERITY_LABELS,
  changeField,
  changeOperator,
  cleanValues,
  issueField,
  newCondition,
  operatorKind,
  operatorsFor,
  valueSuggestions
} from "../rules-utils";

defineOptions({ name: "EventRuleCard" });

/** 一条忽略规则或告警规则:启用开关、名称、级别(告警规则)、按行填写的条件 */
const rule = defineModel<RuleForm>("rule", { required: true });
const props = defineProps<{
  kind: "ignore" | "alert";
  index: number;
  total: number;
  /** 这条规则上的问题(保存、试运行时校验出来的) */
  issues: Issue[];
  clusters: string[];
  disabled: boolean;
}>();
const emit = defineEmits<{
  (e: "move", delta: number): void;
  (e: "copy"): void;
  (e: "remove"): void;
}>();

const isAlert = computed(() => props.kind === "alert");
const usedFields = computed(() =>
  rule.value.conditions.map(item => item.field)
);
const ruleIssues = computed(() =>
  props.issues.filter(item => !issueField(item))
);

function fieldIssues(field: string) {
  return props.issues.filter(item => issueField(item) === field);
}

/** 字段下拉:同一条规则里每个字段只能用一次 */
function fieldOptions(condition: ConditionForm) {
  return FIELDS.map(field => ({
    value: field,
    label: `${FIELD_LABELS[field]}（${field}）`,
    help: FIELD_HELP[field],
    disabled: field !== condition.field && usedFields.value.includes(field)
  }));
}

function setValues(condition: ConditionForm, values: unknown[]) {
  condition.values = cleanValues(values);
}

function addCondition() {
  rule.value.conditions.push(newCondition(usedFields.value));
}

function removeCondition(position: number) {
  rule.value.conditions.splice(position, 1);
}
</script>

<template>
  <el-card
    shadow="never"
    :class="[
      'rule-card',
      { 'is-off': !rule.enabled, 'has-issues': issues.length > 0 }
    ]"
  >
    <template #header>
      <div class="rule-header">
        <span v-if="isAlert" class="rule-order" title="匹配顺序">
          {{ index + 1 }}
        </span>
        <el-switch
          v-model="rule.enabled"
          inline-prompt
          active-text="启用"
          inactive-text="停用"
          :disabled="disabled"
        />
        <el-input
          v-model="rule.name"
          class="rule-name"
          :maxlength="100"
          :disabled="disabled"
          :placeholder="
            isAlert
              ? '规则名称（会出现在通知标题「K8S事件告警: 名称」里）'
              : '规则名称'
          "
        />
        <el-select
          v-if="isAlert"
          v-model="rule.severity"
          class="rule-severity"
          :disabled="disabled"
        >
          <el-option
            v-for="item in SEVERITIES"
            :key="item"
            :label="`${SEVERITY_LABELS[item]}（${item}）`"
            :value="item"
          />
        </el-select>
        <div class="rule-actions">
          <template v-if="isAlert">
            <el-tooltip content="上移（排在前面的先匹配）" placement="top">
              <el-button
                link
                :icon="Top"
                aria-label="上移"
                :disabled="disabled || index === 0"
                @click="emit('move', -1)"
              />
            </el-tooltip>
            <el-tooltip content="下移" placement="top">
              <el-button
                link
                :icon="Bottom"
                aria-label="下移"
                :disabled="disabled || index === total - 1"
                @click="emit('move', 1)"
              />
            </el-tooltip>
          </template>
          <el-button
            link
            type="primary"
            :disabled="disabled"
            @click="emit('copy')"
          >
            复制
          </el-button>
          <el-popconfirm
            title="删除这条规则？保存后才生效"
            confirm-button-text="删除"
            cancel-button-text="取消"
            width="230"
            @confirm="emit('remove')"
          >
            <template #reference>
              <el-button link type="danger" :disabled="disabled">
                删除
              </el-button>
            </template>
          </el-popconfirm>
        </div>
      </div>
    </template>

    <div
      v-for="item in ruleIssues"
      :key="item.message"
      class="issue rule-issue"
    >
      {{ item.message }}
    </div>
    <div class="caption">
      {{
        isAlert
          ? "下面的条件全部满足时发告警"
          : "下面的条件全部满足时不告警（不再看告警规则）"
      }}
    </div>
    <div
      v-for="(condition, position) in rule.conditions"
      :key="condition.key"
      class="condition"
    >
      <div class="condition-row">
        <span class="joiner">{{ position === 0 ? "当" : "并且" }}</span>
        <el-select
          :model-value="condition.field"
          class="condition-field"
          filterable
          :disabled="disabled"
          @update:model-value="value => changeField(condition, value)"
        >
          <el-option
            v-for="item in fieldOptions(condition)"
            :key="item.value"
            :label="item.label"
            :value="item.value"
            :disabled="item.disabled"
          >
            <span>{{ item.label }}</span>
            <span class="option-help">{{ item.help }}</span>
          </el-option>
        </el-select>
        <el-select
          :model-value="condition.op"
          class="condition-op"
          :disabled="disabled"
          @update:model-value="value => changeOperator(condition, value)"
        >
          <el-option
            v-for="op in operatorsFor(condition.field)"
            :key="op"
            :label="OPERATOR_LABELS[op]"
            :value="op"
          >
            <span>{{ OPERATOR_LABELS[op] }}</span>
            <span class="option-help">{{ OPERATOR_HELP[op] }}</span>
          </el-option>
        </el-select>
        <el-select
          v-if="operatorKind(condition.op) === 'list'"
          :model-value="condition.values"
          class="condition-value"
          multiple
          filterable
          allow-create
          default-first-option
          :reserve-keyword="false"
          placeholder="输入后按回车，可以填多个"
          :disabled="disabled"
          @update:model-value="values => setValues(condition, values)"
        >
          <el-option
            v-for="item in valueSuggestions(condition.field, clusters)"
            :key="item"
            :label="item"
            :value="item"
          />
        </el-select>
        <el-input-number
          v-else-if="operatorKind(condition.op) === 'number'"
          v-model="condition.num"
          class="condition-value number"
          :controls="false"
          placeholder="填一个数字"
          :disabled="disabled"
        />
        <el-input
          v-else
          v-model="condition.text"
          class="condition-value"
          :placeholder="
            condition.field === 'count'
              ? '填一个数字'
              : '整个值相等（不区分大小写），可以留空表示值为空'
          "
          :disabled="disabled"
        />
        <el-button
          link
          type="danger"
          :icon="Delete"
          aria-label="删除这个条件"
          :disabled="disabled"
          @click="removeCondition(position)"
        />
      </div>
      <div
        v-for="item in fieldIssues(condition.field)"
        :key="item.message"
        class="issue"
      >
        {{ item.message }}
      </div>
    </div>
    <div v-if="!rule.conditions.length" class="no-condition">
      还没有条件（没有条件会匹配所有事件，不能保存）
    </div>
    <el-button
      link
      type="primary"
      :icon="Plus"
      :disabled="disabled || rule.conditions.length >= FIELDS.length"
      @click="addCondition"
    >
      添加条件
    </el-button>
  </el-card>
</template>

<style scoped lang="scss">
.rule-card {
  margin-bottom: 12px;

  &.is-off :deep(.el-card__body) {
    opacity: 0.55;
  }

  &.has-issues {
    border-color: var(--el-color-danger-light-5);
  }

  :deep(.el-card__header) {
    padding: 10px 16px;
  }

  :deep(.el-card__body) {
    padding: 12px 16px;
  }
}

.rule-header,
.condition-row,
.rule-actions {
  display: flex;
  gap: 8px;
  align-items: center;
}

.rule-header {
  flex-wrap: wrap;
}

.rule-order {
  min-width: 24px;
  height: 24px;
  font-size: 13px;
  font-weight: 600;
  line-height: 24px;
  color: var(--el-color-primary);
  text-align: center;
  background: var(--el-color-primary-light-9);
  border-radius: 12px;
}

.rule-name {
  flex: 1;
  min-width: 220px;
}

.rule-severity {
  width: 150px;
}

.rule-actions {
  gap: 4px;
  margin-left: auto;
}

.caption,
.no-condition {
  margin-bottom: 8px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.condition {
  margin-bottom: 8px;
}

.joiner {
  width: 32px;
  font-size: 13px;
  color: var(--el-text-color-secondary);
  text-align: right;
}

.condition-field {
  width: 190px;
}

.condition-op {
  width: 130px;
}

.condition-value {
  flex: 1;
  min-width: 200px;

  &.number {
    flex: none;
    width: 160px;
  }
}

.option-help {
  margin-left: 12px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.issue {
  margin: 4px 0 0 40px;
  font-size: 12px;
  line-height: 1.5;
  color: var(--el-color-danger);

  &.rule-issue {
    margin: 0 0 8px;
  }
}

@media (width <= 900px) {
  .condition-row {
    flex-wrap: wrap;
  }

  .joiner {
    width: auto;
  }

  .condition-field,
  .condition-op {
    width: calc(50% - 30px);
  }
}
</style>
