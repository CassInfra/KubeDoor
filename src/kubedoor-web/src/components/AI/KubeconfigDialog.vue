<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { aiApi, type AIConnection } from "@/api/ai";

const visible = defineModel<boolean>({ default: false });
const props = defineProps<{ env: string; connection?: AIConnection }>();
const emit = defineEmits<{ changed: [] }>();
const fileInput = ref<HTMLInputElement | null>(null);
const filename = ref("");
const content = ref("");
const contexts = ref<{ name: string; namespace?: string; server?: string }[]>(
  []
);
const context = ref("");
const working = ref(false);
const tested = ref(false);
const testSuccess = ref(false);
const result = ref("");
const saved = ref<AIConnection | undefined>();
const selectedContext = computed(() =>
  contexts.value.find(item => item.name === context.value)
);

function clearCandidate() {
  content.value = "";
  filename.value = "";
  contexts.value = [];
  context.value = "";
  tested.value = false;
  result.value = "";
  if (fileInput.value) fileInput.value.value = "";
}
watch(
  visible,
  value => {
    clearCandidate();
    if (value) saved.value = props.connection;
  },
  { immediate: true }
);
watch(context, () => {
  tested.value = false;
  result.value = "";
});

async function upload(event: Event) {
  const file = (event.target as HTMLInputElement).files?.[0];
  if (!file) return;
  clearCandidate();
  if (file.size > 1024 * 1024) {
    ElMessage.warning("Kubeconfig 文件不能超过 1 MiB。");
    return;
  }
  working.value = true;
  try {
    const candidate = await file.text();
    const parsed = await aiApi.parseConnection(candidate);
    if (!parsed.contexts?.length) throw new Error("文件中没有可用的 context。");
    content.value = candidate;
    filename.value = file.name;
    contexts.value = parsed.contexts;
    context.value = parsed.contexts.some(
      item => item.name === parsed.current_context
    )
      ? parsed.current_context!
      : parsed.contexts.length === 1
        ? parsed.contexts[0].name
        : "";
  } catch (reason) {
    ElMessage.error(
      reason instanceof Error ? reason.message : "解析 Kubeconfig 失败"
    );
  } finally {
    working.value = false;
    if (fileInput.value) fileInput.value.value = "";
  }
}

async function test(candidate = true) {
  if (candidate && (!content.value || !context.value)) {
    ElMessage.warning("请先上传文件并选择 context。");
    return;
  }
  working.value = true;
  result.value = "";
  try {
    const response = await aiApi.testConnection(
      props.env,
      candidate ? content.value : undefined,
      candidate ? context.value : undefined
    );
    testSuccess.value =
      response.success !== false && response.ok !== false && !response.error;
    result.value = String(
      response.message ||
        response.error ||
        (testSuccess.value ? "连接测试成功" : "连接测试失败")
    );
    if (response.permissions)
      result.value += `\n权限摘要：${typeof response.permissions === "string" ? response.permissions : JSON.stringify(response.permissions)}`;
    tested.value = candidate && testSuccess.value;
    if (!candidate) emit("changed");
  } catch (reason) {
    testSuccess.value = false;
    tested.value = false;
    result.value = reason instanceof Error ? reason.message : "连接测试失败";
  } finally {
    working.value = false;
  }
}

async function save() {
  if (!content.value || !context.value || !tested.value) return;
  working.value = true;
  try {
    await aiApi.saveConnection(props.env, content.value, context.value);
    emit("changed");
    visible.value = false;
    ElMessage.success("Kubeconfig 已保存并绑定到该集群");
  } catch (reason) {
    ElMessage.error(
      reason instanceof Error ? reason.message : "保存失败，原配置保留"
    );
  } finally {
    working.value = false;
  }
}

async function remove() {
  try {
    await ElMessageBox.confirm(
      `删除 ${props.env} 的共享 Kubeconfig？后续操作可继续使用在线 Agent。`,
      "删除 Kubeconfig",
      { type: "warning" }
    );
  } catch {
    return;
  }
  working.value = true;
  try {
    await aiApi.deleteConnection(props.env);
    saved.value = undefined;
    clearCandidate();
    emit("changed");
    ElMessage.success("共享 Kubeconfig 已删除");
  } catch (reason) {
    ElMessage.error(reason instanceof Error ? reason.message : "删除失败");
  } finally {
    working.value = false;
  }
}
</script>

<template>
  <el-dialog
    v-model="visible"
    :title="`${env} · AI Kubeconfig`"
    width="min(620px, 94vw)"
    append-to-body
    :close-on-click-modal="false"
    :close-on-press-escape="!working"
    :show-close="!working"
  >
    <el-descriptions
      v-if="saved?.configured !== false && saved?.context"
      :column="1"
      border
      size="small"
      class="saved-connection"
    >
      <el-descriptions-item label="已保存 context">{{
        saved.context
      }}</el-descriptions-item>
      <el-descriptions-item v-if="saved.server" label="API Server">{{
        saved.server
      }}</el-descriptions-item>
      <el-descriptions-item label="最近测试"
        >{{ saved.test_status || "未测试" }}
        {{ saved.tested_at || "" }}</el-descriptions-item
      >
    </el-descriptions>
    <p class="connection-hint">
      该配置由有写权限的用户共同管理，用于 AI
      需要直接访问集群的操作。上传内容保存在服务端，界面仅展示连接摘要。
    </p>
    <div class="upload-row">
      <input
        ref="fileInput"
        class="file-input"
        type="file"
        accept=".yaml,.yml,.json,text/yaml,application/yaml,application/json"
        :disabled="working"
        @change="upload"
      />
      <el-button :disabled="working" @click="fileInput?.click()">{{
        saved?.context ? "上传替换文件" : "上传 Kubeconfig"
      }}</el-button>
      <span>{{ filename || "支持 YAML / JSON，最大 1 MiB" }}</span>
    </div>
    <el-form v-if="content" label-position="top" class="candidate-form">
      <el-form-item label="绑定 context">
        <el-select
          v-model="context"
          filterable
          :disabled="working"
          placeholder="选择该集群的 context"
        >
          <el-option
            v-for="item in contexts"
            :key="item.name"
            :value="item.name"
            :label="item.name"
          />
        </el-select>
      </el-form-item>
      <el-descriptions v-if="selectedContext" :column="1" size="small">
        <el-descriptions-item label="API Server">{{
          selectedContext.server || "未提供"
        }}</el-descriptions-item>
        <el-descriptions-item label="默认命名空间">{{
          selectedContext.namespace || "default"
        }}</el-descriptions-item>
      </el-descriptions>
      <p class="connection-hint">
        先测试候选配置再保存。测试失败或关闭此弹框会保留原配置。
      </p>
    </el-form>
    <el-alert
      v-if="result"
      :title="result"
      :type="testSuccess ? 'success' : 'error'"
      :closable="false"
      show-icon
      class="test-result"
    />
    <template #footer>
      <div class="connection-actions">
        <el-button
          v-if="saved?.context"
          link
          type="danger"
          :disabled="working"
          @click="remove"
          >删除已保存配置</el-button
        >
        <div>
          <el-button
            :loading="working"
            :disabled="(!content && !saved?.context) || (!!content && !context)"
            @click="test(!!content)"
            >测试连接</el-button
          >
          <el-button
            type="primary"
            :disabled="working || !tested || !content"
            @click="save"
            >{{ saved?.context ? "保存替换" : "保存" }}</el-button
          >
        </div>
      </div>
    </template>
  </el-dialog>
</template>

<style scoped lang="scss">
.file-input {
  display: none;
}

.saved-connection {
  margin-bottom: 16px;
}

.connection-hint {
  margin: 12px 0;
  font-size: 12px;
  line-height: 1.7;
  color: var(--el-text-color-secondary);
}

.upload-row {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  align-items: center;
}

.upload-row > span {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  overflow-wrap: anywhere;
}

.candidate-form {
  margin-top: 20px;
}

.candidate-form .el-select {
  width: 100%;
}

.connection-actions {
  display: flex;
  gap: 10px;
  align-items: center;
  justify-content: space-between;
}

.test-result {
  margin-top: 16px;
  white-space: pre-wrap;
}
</style>
