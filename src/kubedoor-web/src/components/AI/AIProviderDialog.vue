<script setup lang="ts">
import { computed, reactive, ref, watch } from "vue";
import { useRouter } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";
import {
  aiApi,
  type AIProvider,
  type AIProviderRequest,
  type AIReasoningEffort
} from "@/api/ai";
import { useAIStoreHook } from "@/store/modules/ai";
import {
  followModelLimits,
  modelLimitsFor,
  type AIModelLimits
} from "@/utils/ai";
import AIModelLimitsFields from "./AIModelLimitsFields.vue";
import AIReasoningEffortField from "./AIReasoningEffortField.vue";

const visible = defineModel<boolean>({ default: false });
const ai = useAIStoreHook();
const router = useRouter();
// 滑块：false = 企微专用模型（管理员配置、只读），true = 自定义模型（自己填写）；点「保存」才生效
const custom = ref(false);
const form = reactive<AIProvider>({ base_url: "", api_key: "", model: "" });
const limits = ref<AIModelLimits>(modelLimitsFor(""));
const effort = ref<AIReasoningEffort | null>(null);
const testing = ref(false);
const result = ref("");
const successful = ref(false);
const shared = computed(() => ai.sharedModel);
// 企微专用模型实际生效的上下文设置：没填的项按模型名推断
const sharedLimits = computed(() =>
  modelLimitsFor(shared.value?.model || "", shared.value)
);
function load() {
  const { base_url, api_key, model } = ai.provider;
  Object.assign(form, { base_url, api_key, model });
  limits.value = modelLimitsFor(model, ai.provider);
  effort.value = ai.provider.reasoning_effort ?? null;
}
watch(visible, value => {
  if (value) {
    custom.value = ai.modelMode === "custom";
    load();
    result.value = "";
  }
});
// 模型名变化时，没手动改过的上下文设置按新模型名重新预填。
// 同步触发：load() 先设模型名、再载入已保存的值，保存的值不会被改写。
watch(
  () => form.model,
  (model, previous) => {
    limits.value = followModelLimits(limits.value, previous, model);
  },
  { flush: "sync" }
);
watch(
  () => [
    custom.value,
    form.base_url,
    form.api_key,
    form.model,
    limits.value,
    effort.value
  ],
  () => {
    result.value = "";
  }
);
const settings = (): AIProvider => ({
  ...form,
  ...limits.value,
  reasoning_effort: effort.value
});

function valid() {
  if (!form.base_url.trim() || !form.model.trim()) {
    ElMessage.warning("请填写 Base URL 和模型名。");
    return false;
  }
  try {
    const url = new URL(form.base_url.trim());
    if (
      !["https:", "http:"].includes(url.protocol) ||
      url.username ||
      url.password ||
      url.search ||
      url.hash
    )
      throw new Error();
  } catch {
    ElMessage.warning(
      "Base URL 需要是 HTTP/HTTPS API 基址，不能包含账号密码、查询参数或片段。"
    );
    return false;
  }
  return true;
}
async function test() {
  let target: AIProviderRequest;
  if (custom.value) {
    if (!valid()) return;
    target = settings();
  } else {
    if (!shared.value?.available) {
      ElMessage.warning("管理员还没有配置企微专用模型。");
      return;
    }
    target = { source: "wecom" };
  }
  testing.value = true;
  result.value = "";
  try {
    const response = await aiApi.testProvider(target);
    successful.value = response.success !== false && response.ok !== false;
    const tools =
      response.tool_calling ??
      response.supports_tools ??
      response.tools_supported;
    if (tools === false) successful.value = false;
    result.value = String(
      response.message ||
        response.warning ||
        (successful.value ? "模型连接成功" : "模型连接失败")
    );
    if (tools !== undefined && !response.warning)
      result.value += tools
        ? "，支持工具调用。"
        : "；未通过工具调用检查，请确认模型及网关的工具调用支持。";
  } catch (reason) {
    successful.value = false;
    result.value = reason instanceof Error ? reason.message : "模型测试失败";
  } finally {
    if (custom.value && form.api_key)
      result.value = result.value.split(form.api_key).join("[已隐藏]");
    testing.value = false;
  }
}
function save() {
  if (custom.value && !valid()) return;
  try {
    if (custom.value) ai.saveProvider(settings());
    else ai.useSharedModel();
    visible.value = false;
    if (custom.value)
      ElMessage.success("已改用自定义模型，配置保存在当前账号的浏览器存储。");
    else if (shared.value?.available) ElMessage.success("已改用企微专用模型。");
    else
      ElMessage.warning(
        "已改用企微专用模型，但管理员还没有配置，暂时不能发送。"
      );
  } catch (reason) {
    ElMessage.error(
      reason instanceof Error ? reason.message : "浏览器无法保存配置"
    );
  }
}
async function forget() {
  try {
    await ElMessageBox.confirm(
      "清除当前账号保存在此浏览器中的自定义模型配置？清除后默认使用企微专用模型。",
      "清除模型配置",
      { type: "warning" }
    );
    ai.forgetProvider();
    load();
    ElMessage.success("模型配置已清除");
  } catch {
    /* Closing confirmation is not an error. */
  }
}
function openWecomSettings() {
  visible.value = false;
  ai.visible = false;
  void router.push("/system/wecom-ai");
}
</script>

<template>
  <el-dialog
    v-model="visible"
    width="min(560px, 94vw)"
    append-to-body
    :close-on-click-modal="false"
  >
    <template #header>
      <div class="settings-header">
        <span class="settings-title">模型设置</span>
        <el-switch
          v-model="custom"
          class="mode-switch"
          inactive-text="企微专用模型"
          active-text="自定义模型"
          :disabled="testing"
          aria-label="切换企微专用模型和自定义模型"
        />
      </div>
    </template>
    <template v-if="!custom">
      <el-alert
        v-if="!shared?.available"
        type="warning"
        :closable="false"
        show-icon
        title="管理员还没有配置企微专用模型"
        class="shared-alert"
      >
        <p class="alert-text">
          企微专用模型在「系统管理 → 企微 AI
          机器人」配置，配置好后所有账号都能直接使用；现在可以先滑到「自定义模型」自己填写。
        </p>
        <el-button
          v-if="ai.bootstrap?.is_admin"
          link
          type="primary"
          @click="openWecomSettings"
          >去配置</el-button
        >
      </el-alert>
      <el-form v-else label-position="top" class="shared-form">
        <el-form-item label="Base URL">
          <el-input :model-value="shared.base_url" disabled />
        </el-form-item>
        <el-form-item label="API Key">
          <el-input
            :model-value="
              shared.api_key_set ? '已配置，由管理员设置' : '未配置'
            "
            disabled
          />
        </el-form-item>
        <el-form-item label="模型名（model ID）">
          <el-input :model-value="shared.model" disabled />
        </el-form-item>
        <AIModelLimitsFields
          :model-value="sharedLimits"
          :model="shared.model"
          readonly
        />
        <AIReasoningEffortField
          :model-value="shared.reasoning_effort"
          disabled
        />
      </el-form>
      <p v-if="shared?.available" class="settings-hint">
        由系统管理员在「系统管理 → 企微 AI
        机器人」配置，所有账号共用，这里只读；API Key
        只在服务端使用，不会发到浏览器。
      </p>
    </template>
    <template v-else>
      <el-form :model="form" label-position="top" @submit.prevent="save">
        <el-form-item label="Base URL">
          <el-input
            v-model="form.base_url"
            placeholder="https://api.deepseek.com"
            :disabled="testing"
          />
          <p class="field-hint">
            DeepSeek 官方填 https://api.deepseek.com，也支持
            https://api.deepseek.com/v1。其他供应商填写其 API 基址并保留
            /v1、/api/openai/v1 等实际前缀；请求会自动追加
            /chat/completions。完整的 /chat/completions 地址也会自动识别。
          </p>
        </el-form-item>
        <el-form-item label="API Key">
          <el-input
            v-model="form.api_key"
            type="password"
            show-password
            autocomplete="off"
            placeholder="本地模型允许留空"
            :disabled="testing"
          />
        </el-form-item>
        <el-form-item label="模型名（model ID）">
          <el-input
            v-model="form.model"
            placeholder="例如 deepseek-flash（按供应商模型 ID）"
            :disabled="testing"
          />
          <p class="field-hint">
            填写支持工具调用的模型
            ID，需与供应商文档或模型列表一致；页面展示名或自定义别称可能无法调用。
          </p>
        </el-form-item>
        <AIModelLimitsFields
          v-model="limits"
          :model="form.model"
          :disabled="testing"
        />
        <AIReasoningEffortField v-model="effort" :disabled="testing" />
      </el-form>
      <p class="settings-hint">
        使用兼容 OpenAI
        的接口。配置仅保存到此浏览器，执行请求时发送给服务端，API Key
        不写入会话历史。
      </p>
    </template>
    <el-alert
      v-if="result"
      :title="result"
      :type="successful ? 'success' : 'error'"
      :closable="false"
      show-icon
    />
    <template #footer>
      <div class="settings-actions">
        <el-button
          v-if="custom"
          link
          type="danger"
          :disabled="testing"
          @click="forget"
          >清除配置</el-button
        >
        <span v-else />
        <div>
          <el-button :loading="testing" @click="test">测试连接</el-button>
          <el-button type="primary" :disabled="testing" @click="save"
            >保存</el-button
          >
        </div>
      </div>
    </template>
  </el-dialog>
</template>

<style scoped lang="scss">
.settings-header {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 20px;
  align-items: center;
  min-height: 24px;
}

.settings-title {
  font-size: var(--el-dialog-title-font-size, 18px);
  line-height: var(--el-dialog-font-line-height, 24px);
  color: var(--el-text-color-primary);
}

// 滑块不用灰色：企微专用模型绿色、自定义模型蓝色，选中一侧的文字和滑块同色
// （对话框输入框下方的模型名也用这两个颜色）
.mode-switch {
  --el-switch-on-color: var(--el-color-primary);
  --el-switch-off-color: var(--el-color-success);

  :deep(.el-switch__label--left.is-active) {
    color: var(--el-color-success);
  }

  :deep(.el-switch__label--right.is-active) {
    color: var(--el-color-primary);
  }
}

.shared-alert {
  margin-bottom: 16px;
}

.alert-text {
  margin: 4px 0;
  line-height: 1.6;
}

.field-hint {
  width: 100%;
  margin: 8px 0 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--el-text-color-secondary);
}

.settings-hint {
  margin: 0 0 16px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--el-text-color-secondary);
}

.settings-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
</style>
