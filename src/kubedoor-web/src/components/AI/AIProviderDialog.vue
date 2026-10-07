<script setup lang="ts">
import { reactive, ref, watch } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { aiApi, type AIProvider } from "@/api/ai";
import { useAIStoreHook } from "@/store/modules/ai";

const visible = defineModel<boolean>({ default: false });
const ai = useAIStoreHook();
const form = reactive<AIProvider>({ base_url: "", api_key: "", model: "" });
const testing = ref(false);
const result = ref("");
const successful = ref(false);
watch(visible, value => {
  if (value) {
    Object.assign(form, ai.provider);
    result.value = "";
  }
});
watch(
  () => [form.base_url, form.api_key, form.model],
  () => {
    result.value = "";
  }
);

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
  if (!valid()) return;
  testing.value = true;
  result.value = "";
  try {
    const response = await aiApi.testProvider({ ...form });
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
    if (form.api_key)
      result.value = result.value.split(form.api_key).join("[已隐藏]");
    testing.value = false;
  }
}
function save() {
  if (!valid()) return;
  try {
    ai.saveProvider({ ...form });
    visible.value = false;
    ElMessage.success("模型配置已保存到当前账号的浏览器存储。");
  } catch (reason) {
    ElMessage.error(
      reason instanceof Error ? reason.message : "浏览器无法保存配置"
    );
  }
}
async function forget() {
  try {
    await ElMessageBox.confirm(
      "清除当前账号保存在此浏览器中的模型配置？",
      "清除模型配置",
      { type: "warning" }
    );
    ai.forgetProvider();
    Object.assign(form, ai.provider);
    ElMessage.success("模型配置已清除");
  } catch {
    /* Closing confirmation is not an error. */
  }
}
</script>

<template>
  <el-dialog
    v-model="visible"
    title="模型设置"
    width="min(520px, 94vw)"
    append-to-body
    :close-on-click-modal="false"
  >
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
    </el-form>
    <p class="settings-hint">
      使用兼容 OpenAI 的接口。配置仅保存到此浏览器，执行请求时发送给服务端，API
      Key 不写入会话历史。
    </p>
    <el-alert
      v-if="result"
      :title="result"
      :type="successful ? 'success' : 'error'"
      :closable="false"
      show-icon
    />
    <template #footer>
      <div class="settings-actions">
        <el-button link type="danger" :disabled="testing" @click="forget"
          >清除配置</el-button
        >
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
