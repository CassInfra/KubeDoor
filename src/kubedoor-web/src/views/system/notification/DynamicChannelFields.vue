<script setup lang="ts">
import { computed, reactive, watch } from "vue";
import type { ProviderFieldSpec, ProviderSchema } from "@/api/notification";
import {
  type ChannelFormV2,
  type TestView,
  FALLBACK_PROVIDERS,
  detectFeishuPresetFromUrl,
  detectProviderFromUrl,
  findProviderSchema,
  formatHeadersJson,
  formatRateLimit,
  isFieldVisible,
  parseHeadersValue,
  providerLabel,
  secretPlaceholderV2
} from "./notification-utils";

defineOptions({ name: "DynamicChannelFields" });

const form = defineModel<ChannelFormV2>("form", { required: true });

const props = withDefaults(
  defineProps<{
    provider?: ProviderSchema | null;
    providers?: ProviderSchema[];
    testing?: boolean;
    result?: TestView | null;
    disabled?: boolean;
  }>(),
  {
    provider: null,
    providers: () => FALLBACK_PROVIDERS,
    testing: false,
    result: null,
    disabled: false
  }
);

defineEmits<{ test: [] }>();

const activeProvider = computed<ProviderSchema>(() => {
  if (props.provider) return props.provider;
  return (
    findProviderSchema(form.value.provider, props.providers) ??
    FALLBACK_PROVIDERS[0]
  );
});

const visibleFields = computed<ProviderFieldSpec[]>(() =>
  (activeProvider.value.fields ?? []).filter(field =>
    isFieldVisible(field, form.value.settings, activeProvider.value.fields)
  )
);

const rateLimitText = computed(() =>
  formatRateLimit(activeProvider.value.default_rate_limit)
);

/** 根据输入的 Webhook URL 给出的域名提示（自动匹配飞书/Lark，或提醒类型不匹配） */
const urlMismatchHint = computed(() => {
  const token = (form.value.secrets?.token || "").trim();
  if (!token) return "";
  const detected = detectProviderFromUrl(token);
  if (detected && detected !== activeProvider.value.id) {
    return `检测到该地址属于「${providerLabel(detected, props.providers)}」，与当前通道类型「${activeProvider.value.label}」不一致`;
  }
  return "";
});

function handleSecretInput(fieldKey: string, value: string) {
  if (!form.value.secrets) form.value.secrets = {};
  form.value.secrets[fieldKey] = value;
  if (fieldKey === "token" && activeProvider.value.id === "feishu") {
    const preset = detectFeishuPresetFromUrl(value);
    if (
      preset &&
      Object.hasOwn(form.value.settings, "domain_preset") &&
      form.value.settings.domain_preset !== preset
    ) {
      form.value.settings.domain_preset = preset;
    }
  }
}

/* ==================== Headers Field (KV / JSON Editor) ==================== */

interface HeaderRow {
  key: string;
  value: string;
}

const headersJsonMode = reactive<Record<string, boolean>>({});
const headersJsonText = reactive<Record<string, string>>({});
const headersJsonError = reactive<Record<string, string>>({});
const headerRows = reactive<Record<string, HeaderRow[]>>({});

function syncHeadersState(fieldKey: string, raw: unknown) {
  try {
    const parsed = parseHeadersValue(raw);
    headerRows[fieldKey] = Object.entries(parsed).map(([key, value]) => ({
      key,
      value
    }));
    headersJsonText[fieldKey] = formatHeadersJson(parsed);
    headersJsonError[fieldKey] = "";
  } catch {
    headerRows[fieldKey] = [];
    headersJsonText[fieldKey] = typeof raw === "string" ? raw : "{}";
  }
}

watch(
  () => [
    form.value.id,
    form.value.provider,
    visibleFields.value.map(f => f.key).join(",")
  ],
  () => {
    for (const field of visibleFields.value) {
      if (field.kind === "headers") {
        syncHeadersState(field.key, form.value.settings?.[field.key]);
      }
    }
  },
  { immediate: true }
);

function toggleHeadersMode(fieldKey: string) {
  const next = !headersJsonMode[fieldKey];
  headersJsonMode[fieldKey] = next;
  if (next) {
    headersJsonText[fieldKey] = formatHeadersJson(
      form.value.settings?.[fieldKey]
    );
    headersJsonError[fieldKey] = "";
  } else {
    syncHeadersState(fieldKey, form.value.settings?.[fieldKey]);
  }
}

function commitHeaderRows(fieldKey: string) {
  const rows = headerRows[fieldKey] ?? [];
  const obj: Record<string, string> = {};
  for (const row of rows) {
    const k = (row.key || "").trim();
    if (!k) continue;
    obj[k] = row.value ?? "";
  }
  form.value.settings[fieldKey] = obj;
  headersJsonText[fieldKey] = formatHeadersJson(obj);
  headersJsonError[fieldKey] = "";
}

function addHeaderRow(fieldKey: string) {
  if (!headerRows[fieldKey]) headerRows[fieldKey] = [];
  headerRows[fieldKey].push({ key: "", value: "" });
}

function removeHeaderRow(fieldKey: string, index: number) {
  headerRows[fieldKey]?.splice(index, 1);
  commitHeaderRows(fieldKey);
}

function handleHeadersJsonInput(fieldKey: string, text: string) {
  headersJsonText[fieldKey] = text;
  try {
    const parsed = parseHeadersValue(text);
    form.value.settings[fieldKey] = parsed;
    headersJsonError[fieldKey] = "";
  } catch (err) {
    headersJsonError[fieldKey] = (err as Error).message;
  }
}
</script>

<template>
  <div class="dynamic-channel-fields">
    <div class="provider-summary">
      <div class="provider-summary-head">
        <span class="provider-label">{{ activeProvider.label }}</span>
        <el-tag v-if="rateLimitText" size="small" type="info" effect="plain">
          限流：{{ rateLimitText }}
        </el-tag>
        <el-tag
          v-if="activeProvider.supports_at"
          size="small"
          type="success"
          effect="plain"
        >
          支持 @ 人
        </el-tag>
        <el-tag
          v-if="activeProvider.supports_markdown"
          size="small"
          effect="plain"
        >
          Markdown
        </el-tag>
      </div>
      <p v-if="activeProvider.description" class="provider-desc">
        {{ activeProvider.description }}
      </p>
    </div>

    <template v-for="field in visibleFields" :key="field.key">
      <el-form-item :label="field.label" :required="!!field.required">
        <!-- Secret Field -->
        <template v-if="field.kind === 'secret' || field.secret">
          <el-input
            :model-value="form.secrets[field.key] ?? ''"
            type="password"
            show-password
            autocomplete="new-password"
            :disabled="disabled"
            :placeholder="secretPlaceholderV2(field, form)"
            @update:model-value="val => handleSecretInput(field.key, val)"
          />
          <div
            v-if="field.key === 'token' && urlMismatchHint"
            class="dynamic-warn"
          >
            {{ urlMismatchHint }}
          </div>
        </template>

        <!-- Select Field -->
        <template v-else-if="field.kind === 'select'">
          <el-select
            v-model="form.settings[field.key]"
            :disabled="disabled"
            class="full-width"
          >
            <el-option
              v-for="opt in field.options ?? []"
              :key="opt.value"
              :label="opt.label"
              :value="opt.value"
            />
          </el-select>
        </template>

        <!-- Number Field -->
        <template v-else-if="field.kind === 'number'">
          <el-input-number
            v-model="form.settings[field.key]"
            :disabled="disabled"
            :min="field.min ?? 0"
            :max="field.max ?? 999999"
            controls-position="right"
          />
        </template>

        <!-- Boolean Field -->
        <template v-else-if="field.kind === 'boolean'">
          <el-switch v-model="form.settings[field.key]" :disabled="disabled" />
        </template>

        <!-- Textarea Field -->
        <template v-else-if="field.kind === 'textarea'">
          <el-input
            v-model="form.settings[field.key]"
            type="textarea"
            :rows="3"
            :disabled="disabled"
            :placeholder="field.placeholder"
          />
        </template>

        <!-- Headers Field (KV / JSON Editor) -->
        <template v-else-if="field.kind === 'headers'">
          <div class="headers-editor">
            <div class="headers-toolbar">
              <el-button
                v-if="!headersJsonMode[field.key]"
                size="small"
                :disabled="disabled"
                @click="addHeaderRow(field.key)"
              >
                添加 Header
              </el-button>
              <el-button
                link
                type="primary"
                size="small"
                :disabled="disabled"
                @click="toggleHeadersMode(field.key)"
              >
                {{
                  headersJsonMode[field.key]
                    ? "切换到键值对视图"
                    : "切换到 JSON 编辑"
                }}
              </el-button>
            </div>
            <div v-if="!headersJsonMode[field.key]" class="headers-rows">
              <div
                v-for="(row, idx) in headerRows[field.key] ?? []"
                :key="idx"
                class="header-row"
              >
                <el-input
                  v-model="row.key"
                  placeholder="Header 名（如 X-Source）"
                  :disabled="disabled"
                  @input="commitHeaderRows(field.key)"
                />
                <el-input
                  v-model="row.value"
                  placeholder="Header 值"
                  :disabled="disabled"
                  @input="commitHeaderRows(field.key)"
                />
                <el-button
                  link
                  type="danger"
                  :disabled="disabled"
                  @click="removeHeaderRow(field.key, idx)"
                >
                  移除
                </el-button>
              </div>
              <div
                v-if="!(headerRows[field.key] ?? []).length"
                class="dynamic-hint"
              >
                暂无自定义请求头，可点击「添加 Header」
              </div>
            </div>
            <div v-else class="headers-json">
              <el-input
                :model-value="headersJsonText[field.key] ?? '{}'"
                type="textarea"
                :rows="4"
                :disabled="disabled"
                placeholder='{"X-Source": "kubedoor"}'
                @update:model-value="
                  val => handleHeadersJsonInput(field.key, val)
                "
              />
              <div v-if="headersJsonError[field.key]" class="dynamic-warn">
                {{ headersJsonError[field.key] }}
              </div>
            </div>
          </div>
        </template>

        <!-- Text Field (default) -->
        <template v-else>
          <el-input
            v-model="form.settings[field.key]"
            clearable
            :disabled="disabled"
            :placeholder="field.placeholder"
          />
        </template>

        <div v-if="field.help" class="dynamic-hint">{{ field.help }}</div>
      </el-form-item>
    </template>

    <el-form-item>
      <el-button :loading="testing" :disabled="disabled" @click="$emit('test')">
        发送测试消息
      </el-button>
      <span v-if="result" :class="['dynamic-test-result', result.type]">
        {{ result.text }}
      </span>
    </el-form-item>
  </div>
</template>

<style lang="scss" scoped>
.dynamic-channel-fields {
  width: 100%;
}

.provider-summary {
  padding: 10px 12px;
  margin-bottom: 16px;
  background: var(--el-fill-color-light);
  border-radius: 6px;
}

.provider-summary-head {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}

.provider-label {
  font-size: 14px;
  font-weight: 600;
  color: var(--el-text-color-primary);
}

.provider-desc {
  margin: 6px 0 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--el-text-color-secondary);
}

.full-width {
  width: 100%;
}

.dynamic-hint {
  display: block;
  width: 100%;
  margin-top: 2px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--el-text-color-secondary);
}

.dynamic-warn {
  display: block;
  width: 100%;
  margin-top: 4px;
  font-size: 12px;
  line-height: 1.5;
  color: var(--el-color-warning);
}

.headers-editor {
  width: 100%;
}

.headers-toolbar {
  display: flex;
  gap: 12px;
  align-items: center;
  margin-bottom: 8px;
}

.headers-rows {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.header-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) auto;
  gap: 8px;
  align-items: center;
}

.dynamic-test-result {
  flex: 1;
  min-width: 0;
  margin-left: 12px;
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
</style>
