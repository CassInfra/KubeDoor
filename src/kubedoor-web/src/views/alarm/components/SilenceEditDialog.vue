<template>
  <el-dialog
    v-model="visible"
    :title="isEdit ? '编辑屏蔽规则' : '新建屏蔽规则'"
    width="760px"
    top="6vh"
    destroy-on-close
    :close-on-click-modal="false"
    class="silence-dialog"
    @open="handleOpen"
  >
    <el-form ref="formRef" :model="form" label-position="top">
      <!-- 匹配条件 -->
      <el-form-item>
        <template #label>
          <div class="label-row">
            <span>匹配条件 <span class="required">*</span></span>
            <el-button link type="primary" @click="addMatcher">
              <el-icon><Plus /></el-icon>
              添加条件
            </el-button>
          </div>
        </template>

        <div class="matcher-list">
          <div
            v-for="(matcher, index) in form.matchers"
            :key="index"
            class="matcher-row"
          >
            <el-select
              v-model="matcher.key"
              class="matcher-key"
              placeholder="标签名"
              filterable
              allow-create
              default-first-option
              @change="() => handleKeyChange(matcher)"
            >
              <el-option
                v-for="item in COMMON_MATCHER_KEYS"
                :key="item.value"
                :label="item.label"
                :value="item.value"
              >
                <span>{{ item.label }}</span>
                <span class="option-hint">{{ item.value }}</span>
              </el-option>
            </el-select>

            <el-select v-model="matcher.op" class="matcher-op">
              <el-option
                v-for="item in MATCHER_OPS"
                :key="item.value"
                :label="item.label"
                :value="item.value"
              >
                <span>{{ item.label }}</span>
                <span class="option-hint">{{ item.tip }}</span>
              </el-option>
            </el-select>

            <el-select
              v-model="matcher.value"
              class="matcher-value"
              :placeholder="
                isRegexOp(matcher.op) ? '正则表达式（需完全匹配）' : '标签值'
              "
              filterable
              allow-create
              default-first-option
              :loading="valueLoading[matcher.key]"
            >
              <el-option
                v-for="item in valueOptions[matcher.key] || []"
                :key="item"
                :label="item"
                :value="item"
              />
            </el-select>

            <el-button
              class="matcher-remove"
              link
              :disabled="form.matchers.length === 1"
              @click="removeMatcher(index)"
            >
              <el-icon><Delete /></el-icon>
            </el-button>
          </div>
        </div>

        <div class="form-tip">
          条件之间为「且」关系，全部满足才会屏蔽；<code>=~</code> 为正则且需
          <b>完全匹配</b>整个标签值（如
          <code>order-.*</code>）。标签名可自由输入任意 Prometheus 标签。
        </div>
      </el-form-item>

      <!-- 屏蔽时长 -->
      <el-form-item>
        <template #label>
          <span>屏蔽时长 <span class="required">*</span></span>
        </template>
        <div class="duration-presets">
          <el-button
            v-for="preset in DURATION_PRESETS"
            :key="preset.label"
            :type="activePreset === preset.label ? 'primary' : ''"
            size="small"
            @click="applyPreset(preset)"
          >
            {{ preset.label }}
          </el-button>
        </div>
        <div class="time-range">
          <el-date-picker
            v-model="form.starts_at"
            type="datetime"
            placeholder="开始时间"
            format="YYYY-MM-DD HH:mm:ss"
            value-format="YYYY-MM-DD HH:mm:ss"
            @change="activePreset = ''"
          />
          <span class="time-sep">至</span>
          <el-date-picker
            v-if="!form.forever"
            v-model="form.ends_at"
            type="datetime"
            placeholder="结束时间"
            format="YYYY-MM-DD HH:mm:ss"
            value-format="YYYY-MM-DD HH:mm:ss"
            @change="activePreset = ''"
          />
          <el-tag v-else type="warning" effect="plain" class="forever-tag">
            长期有效（不自动过期）
          </el-tag>
        </div>
        <div v-if="durationText" class="form-tip">{{ durationText }}</div>
      </el-form-item>

      <el-row :gutter="16">
        <el-col :span="8">
          <el-form-item label="创建人">
            <el-input v-model="form.created_by" placeholder="操作人" />
          </el-form-item>
        </el-col>
        <el-col :span="16">
          <el-form-item label="备注">
            <el-input
              v-model="form.comment"
              placeholder="屏蔽原因，如：大促期间已知容量告警"
            />
          </el-form-item>
        </el-col>
      </el-row>

      <!-- 命中预览 -->
      <el-form-item label="命中预览">
        <div class="preview-box" :class="{ 'is-loading': previewLoading }">
          <div class="preview-head">
            <span v-if="previewLoading" class="preview-summary">
              <el-icon class="is-loading"><Loading /></el-icon>
              正在匹配历史告警…
            </span>
            <span v-else-if="previewError" class="preview-summary error">
              {{ previewError }}
            </span>
            <span v-else-if="preview" class="preview-summary">
              近 {{ preview.days }} 天将匹配
              <b class="hit-num">{{ preview.matched }}</b> 条告警记录（共触发
              {{ preview.firings }} 次）
            </span>
            <span v-else class="preview-summary muted">
              填写匹配条件后自动预览
            </span>
            <el-select
              v-model="previewDays"
              size="small"
              class="preview-days"
              @change="runPreview"
            >
              <el-option label="近1天" :value="1" />
              <el-option label="近3天" :value="3" />
              <el-option label="近7天" :value="7" />
              <el-option label="近30天" :value="30" />
            </el-select>
          </div>

          <el-alert
            v-if="preview?.unsupported_keys?.length"
            type="warning"
            :closable="false"
            show-icon
            class="preview-alert"
          >
            标签 {{ preview.unsupported_keys.join("、") }}
            不在告警表中，预览时已跳过，实际生效范围会比这里更小。
          </el-alert>

          <div v-if="preview?.samples?.length" class="preview-samples">
            <div
              v-for="(sample, index) in preview.samples"
              :key="index"
              class="preview-sample"
            >
              <span
                class="sample-name"
                :style="{ color: severityColor(sample.severity) }"
                >{{ sample.alert_name }}</span
              >
              <span class="sample-path">
                {{ sample.env }} / {{ sample.namespace }} / {{ sample.pod }}
              </span>
              <span class="sample-count">×{{ sample.firings }}</span>
            </div>
          </div>
          <div
            v-else-if="preview && !previewLoading && !previewError"
            class="preview-empty"
          >
            近 {{ preview.days }} 天没有匹配到历史告警，请确认条件是否写对。
          </div>
        </div>
      </el-form-item>
    </el-form>

    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button type="primary" :loading="saving" @click="handleSubmit">
        {{ isEdit ? "保存" : "确定屏蔽" }}
      </el-button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { ref, reactive, computed, watch } from "vue";
import { ElMessage } from "element-plus";
import { Plus, Delete, Loading } from "@element-plus/icons-vue";
import dayjs from "dayjs";
import {
  addSilence,
  editSilence,
  previewSilence,
  getSilenceLabelValues,
  COMMON_MATCHER_KEYS,
  MATCHER_OPS,
  type SilenceMatcher,
  type SilenceOp,
  type SilenceRule,
  type SilencePreviewResult
} from "@/api/silence";
import { useUserStoreHook } from "@/store/modules/user";

const props = defineProps<{
  modelValue: boolean;
  /** 传入则为编辑模式 */
  rule?: SilenceRule | null;
  /** 预填的匹配条件，用于「一键屏蔽」和「克隆」 */
  prefill?: SilenceMatcher[] | null;
}>();

const emit = defineEmits<{
  (e: "update:modelValue", value: boolean): void;
  (e: "saved"): void;
}>();

const visible = computed({
  get: () => props.modelValue,
  set: value => emit("update:modelValue", value)
});

const isEdit = computed(() => !!props.rule?.id);

const TIME_FORMAT = "YYYY-MM-DD HH:mm:ss";

const DURATION_PRESETS = [
  { label: "30分钟", minutes: 30 },
  { label: "1小时", minutes: 60 },
  { label: "2小时", minutes: 120 },
  { label: "6小时", minutes: 360 },
  { label: "12小时", minutes: 720 },
  { label: "1天", minutes: 1440 },
  { label: "3天", minutes: 4320 },
  { label: "7天", minutes: 10080 },
  { label: "长期", minutes: 0 }
];

const formRef = ref();
const saving = ref(false);
const activePreset = ref("2小时");

const form = reactive({
  matchers: [] as SilenceMatcher[],
  starts_at: "",
  ends_at: "",
  forever: false,
  comment: "",
  created_by: ""
});

const isRegexOp = (op: SilenceOp) => op === "=~" || op === "!~";

// -- 标签候选值 -------------------------------------------------------------
const valueOptions = reactive<Record<string, string[]>>({});
const valueLoading = reactive<Record<string, boolean>>({});

const loadValues = async (key: string) => {
  if (!key || valueOptions[key] || valueLoading[key]) return;
  valueLoading[key] = true;
  try {
    const res = await getSilenceLabelValues(key);
    valueOptions[key] = res.data || [];
  } catch {
    valueOptions[key] = [];
  } finally {
    valueLoading[key] = false;
  }
};

const handleKeyChange = (matcher: SilenceMatcher) => {
  loadValues(matcher.key);
};

// -- 匹配条件增删 -----------------------------------------------------------
const addMatcher = () => {
  form.matchers.push({ key: "", op: "=", value: "" });
};

const removeMatcher = (index: number) => {
  form.matchers.splice(index, 1);
};

// -- 时长 -------------------------------------------------------------------
const applyPreset = (preset: { label: string; minutes: number }) => {
  activePreset.value = preset.label;
  form.starts_at = dayjs().format(TIME_FORMAT);
  if (preset.minutes === 0) {
    form.forever = true;
    form.ends_at = "";
  } else {
    form.forever = false;
    form.ends_at = dayjs().add(preset.minutes, "minute").format(TIME_FORMAT);
  }
};

const durationText = computed(() => {
  if (form.forever) return "该规则不会自动过期，需要手动解除。";
  if (!form.starts_at || !form.ends_at) return "";
  const start = dayjs(form.starts_at);
  const end = dayjs(form.ends_at);
  if (!end.isAfter(start)) return "";
  const minutes = end.diff(start, "minute");
  if (minutes < 60) return `共屏蔽 ${minutes} 分钟`;
  if (minutes < 1440) return `共屏蔽 ${(minutes / 60).toFixed(1)} 小时`;
  return `共屏蔽 ${(minutes / 1440).toFixed(1)} 天`;
});

// -- 命中预览 ---------------------------------------------------------------
const preview = ref<SilencePreviewResult | null>(null);
const previewLoading = ref(false);
const previewError = ref("");
const previewDays = ref(7);
let previewTimer: ReturnType<typeof setTimeout> | null = null;

const validMatchers = () =>
  form.matchers.filter(item => item.key && (item.value || item.op === "="));

const runPreview = async () => {
  const matchers = validMatchers();
  if (!matchers.length) {
    preview.value = null;
    previewError.value = "";
    return;
  }
  previewLoading.value = true;
  previewError.value = "";
  try {
    const res = await previewSilence(matchers, previewDays.value);
    preview.value = res;
  } catch (error: any) {
    preview.value = null;
    previewError.value =
      error?.response?.data?.msg || "预览失败，请检查匹配条件";
  } finally {
    previewLoading.value = false;
  }
};

const schedulePreview = () => {
  if (previewTimer) clearTimeout(previewTimer);
  previewTimer = setTimeout(runPreview, 500);
};

watch(() => JSON.stringify(form.matchers), schedulePreview);

const severityColor = (severity: string) => {
  const map: Record<string, string> = {
    critical: "#F56C6C",
    warning: "#E6A23C",
    notice: "#409EFF",
    info: "#909399"
  };
  return map[(severity || "").toLowerCase()] || "var(--el-text-color-primary)";
};

// -- 打开/提交 --------------------------------------------------------------
const handleOpen = () => {
  preview.value = null;
  previewError.value = "";

  if (props.rule) {
    form.matchers = props.rule.matchers.map(item => ({ ...item }));
    form.starts_at = dayjs(props.rule.starts_at).format(TIME_FORMAT);
    form.forever = !props.rule.ends_at;
    form.ends_at = props.rule.ends_at
      ? dayjs(props.rule.ends_at).format(TIME_FORMAT)
      : "";
    form.comment = props.rule.comment;
    form.created_by = props.rule.created_by;
    activePreset.value = "";
  } else {
    form.matchers = props.prefill?.length
      ? props.prefill.map(item => ({ ...item }))
      : [{ key: "", op: "=", value: "" }];
    form.comment = "";
    form.created_by =
      useUserStoreHook()?.nickname || useUserStoreHook()?.username || "";
    applyPreset(DURATION_PRESETS[2]); // 默认 2 小时
  }

  form.matchers.forEach(item => loadValues(item.key));
  runPreview();
};

const handleSubmit = async () => {
  const matchers = form.matchers.filter(item => item.key.trim());
  if (!matchers.length) {
    ElMessage.warning("请至少填写一个匹配条件");
    return;
  }
  const emptyRegex = matchers.find(
    item => isRegexOp(item.op) && !item.value.trim()
  );
  if (emptyRegex) {
    ElMessage.warning(`标签 ${emptyRegex.key} 的正则不能为空`);
    return;
  }
  if (!form.starts_at) {
    ElMessage.warning("请选择开始时间");
    return;
  }
  if (!form.forever && !form.ends_at) {
    ElMessage.warning("请选择结束时间，或改用「长期」");
    return;
  }
  if (!form.forever && !dayjs(form.ends_at).isAfter(dayjs(form.starts_at))) {
    ElMessage.warning("结束时间必须晚于开始时间");
    return;
  }

  // 带上时区偏移发送，避免前后端时区不一致造成生效时间偏移
  const payload = {
    matchers,
    starts_at: dayjs(form.starts_at).format(),
    ends_at: form.forever ? null : dayjs(form.ends_at).format(),
    comment: form.comment.trim(),
    created_by: form.created_by.trim()
  };

  saving.value = true;
  try {
    if (isEdit.value) {
      await editSilence({ ...payload, id: props.rule!.id });
      ElMessage.success("屏蔽规则已更新");
    } else {
      await addSilence(payload);
      ElMessage.success("屏蔽规则已创建");
    }
    visible.value = false;
    emit("saved");
  } catch (error: any) {
    ElMessage.error(error?.response?.data?.msg || "保存失败");
  } finally {
    saving.value = false;
  }
};
</script>

<style scoped>
.label-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
}

.required {
  color: var(--el-color-danger);
}

.matcher-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  width: 100%;
}

.matcher-row {
  display: flex;
  gap: 8px;
  align-items: center;
  width: 100%;
}

.matcher-key {
  width: 180px;
}

.matcher-op {
  width: 84px;
}

.matcher-value {
  flex: 1;
}

.matcher-remove {
  width: 24px;
  color: var(--el-text-color-secondary);
}

.matcher-remove:hover:not(.is-disabled) {
  color: var(--el-color-danger);
}

.option-hint {
  float: right;
  margin-left: 16px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.form-tip {
  margin-top: 6px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--el-text-color-secondary);
}

.form-tip code {
  padding: 0 4px;
  background: var(--el-fill-color-light);
  border-radius: 3px;
}

.duration-presets {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  width: 100%;
  margin-bottom: 10px;
}

.duration-presets .el-button + .el-button {
  margin-left: 0;
}

.time-range {
  display: flex;
  gap: 10px;
  align-items: center;
  width: 100%;
}

.time-sep {
  color: var(--el-text-color-secondary);
}

.forever-tag {
  height: 32px;
  line-height: 30px;
}

/* 命中预览 */
.preview-box {
  width: 100%;
  padding: 12px 14px;
  background: var(--el-fill-color-lighter);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
}

.preview-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.preview-summary {
  font-size: 13px;
  color: var(--el-text-color-regular);
}

.preview-summary.muted {
  color: var(--el-text-color-secondary);
}

.preview-summary.error {
  color: var(--el-color-danger);
}

.hit-num {
  font-size: 16px;
  color: var(--el-color-primary);
}

.preview-days {
  width: 100px;
}

.preview-alert {
  margin-top: 8px;
}

.preview-samples {
  max-height: 132px;
  margin-top: 10px;
  overflow-y: auto;
}

.preview-sample {
  display: flex;
  gap: 10px;
  align-items: center;
  padding: 3px 0;
  font-size: 12px;
  line-height: 1.6;
  border-top: 1px dashed var(--el-border-color-lighter);
}

.preview-sample:first-child {
  border-top: none;
}

.sample-name {
  flex-shrink: 0;
  font-weight: 600;
}

.sample-path {
  flex: 1;
  overflow: hidden;
  color: var(--el-text-color-secondary);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.sample-count {
  flex-shrink: 0;
  font-weight: 600;
  color: var(--el-color-danger);
}

.preview-empty {
  margin-top: 10px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
</style>
