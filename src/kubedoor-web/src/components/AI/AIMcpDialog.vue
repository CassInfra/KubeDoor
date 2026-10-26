<script setup lang="ts">
import { computed, reactive, ref, watch } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import {
  aiApi,
  type AIMcpApproval,
  type AIMcpServer,
  type AIMcpTestResult
} from "@/api/ai";
import { useAIStoreHook } from "@/store/modules/ai";
import {
  MCP_APPROVALS,
  MCP_MAX_HEADERS,
  MCP_TRANSPORTS,
  emptyMcpForm,
  maskedUrl,
  mcpFormFrom,
  mcpNeedsApproval,
  mcpPayload,
  mcpStatus,
  transportLabel,
  validateMcpForm,
  type AIMcpForm,
  type AIMcpHeaderRow
} from "@/utils/aiMcp";

const visible = defineModel<boolean>({ default: false });
const ai = useAIStoreHook();
const loading = ref(false);
const error = ref("");
/** 正在切换开关、重新连接或删除的 MCP */
const working = ref("");
const expanded = ref(new Set<string>());

const editorVisible = ref(false);
const editingId = ref("");
const form = reactive<AIMcpForm>(emptyMcpForm());
const saving = ref(false);
const testing = ref(false);
const testResult = ref<AIMcpTestResult | null>(null);

const errorText = (reason: unknown) =>
  reason instanceof Error ? reason.message : "请求失败，请稍后重试。";

async function load() {
  loading.value = true;
  error.value = "";
  try {
    await ai.refreshMcpServers();
  } catch (reason) {
    error.value = errorText(reason);
  } finally {
    loading.value = false;
  }
}
watch(visible, value => {
  if (value) void load();
});

function toggleTools(id: string) {
  if (expanded.value.has(id)) expanded.value.delete(id);
  else expanded.value.add(id);
}

async function patch(
  server: AIMcpServer,
  data: { enabled?: boolean; approval?: AIMcpApproval }
) {
  working.value = server.id;
  try {
    ai.upsertMcpServer(await aiApi.patchMcpServer(server.id, data));
  } catch (reason) {
    ElMessage.error(errorText(reason));
  } finally {
    working.value = "";
  }
}
const changeApproval = (server: AIMcpServer, approval: AIMcpApproval) =>
  patch(server, { approval });

async function reconnect(server: AIMcpServer) {
  working.value = server.id;
  try {
    const updated = await aiApi.refreshMcpServer(server.id);
    ai.upsertMcpServer(updated);
    if (updated.last_error)
      ElMessage.warning(`连接失败：${updated.last_error}`);
    else ElMessage.success(`已重新获取，共 ${updated.tools.length} 个工具`);
  } catch (reason) {
    ElMessage.error(errorText(reason));
  } finally {
    working.value = "";
  }
}

async function remove(server: AIMcpServer) {
  try {
    await ElMessageBox.confirm(
      `删除 MCP「${server.name}」？对话里已经调用过的记录会保留。`,
      "删除 MCP",
      { type: "warning" }
    );
  } catch {
    return;
  }
  working.value = server.id;
  try {
    await aiApi.deleteMcpServer(server.id);
    ai.dropMcpServer(server.id);
    ElMessage.success("已删除");
  } catch (reason) {
    ElMessage.error(errorText(reason));
  } finally {
    working.value = "";
  }
}

function openEditor(server?: AIMcpServer) {
  editingId.value = server?.id || "";
  Object.assign(form, server ? mcpFormFrom(server) : emptyMcpForm());
  testResult.value = null;
  editorVisible.value = true;
}
function addHeader() {
  if (form.headers.length < MCP_MAX_HEADERS)
    form.headers.push({ name: "", value: "", saved: null });
}
const keepsSaved = (row: AIMcpHeaderRow) =>
  !!row.saved && row.saved.toLowerCase() === row.name.trim().toLowerCase();
// 改了连接配置，上一次的测试结果就不作数了
watch(
  () => [form.transport, form.url, JSON.stringify(form.headers)],
  () => {
    testResult.value = null;
  }
);
const transportHint = computed(
  () => MCP_TRANSPORTS.find(item => item.value === form.transport)?.hint
);
const approvalHint = computed(
  () => MCP_APPROVALS.find(item => item.value === form.approval)?.hint
);
const testSummary = computed(() => {
  const result = testResult.value;
  if (!result) return "";
  if (!result.success) return result.error || "连接失败";
  const names = result.tools.map(tool => tool.name);
  return (
    `连接成功，共 ${names.length} 个工具` +
    (names.length
      ? `：${names.slice(0, 8).join("、")}${names.length > 8 ? " 等" : ""}`
      : "")
  );
});

async function test() {
  const problem = validateMcpForm(form, false);
  if (problem) {
    ElMessage.warning(problem);
    return;
  }
  testing.value = true;
  testResult.value = null;
  try {
    testResult.value = await aiApi.testMcpServer({
      ...mcpPayload(form),
      ...(editingId.value ? { id: editingId.value } : {})
    });
  } catch (reason) {
    testResult.value = { success: false, error: errorText(reason), tools: [] };
  } finally {
    testing.value = false;
  }
}

async function save() {
  const problem = validateMcpForm(form);
  if (problem) {
    ElMessage.warning(problem);
    return;
  }
  saving.value = true;
  try {
    const payload = mcpPayload(form);
    const saved = editingId.value
      ? await aiApi.updateMcpServer(editingId.value, payload)
      : await aiApi.createMcpServer(payload);
    ai.upsertMcpServer(saved);
    editorVisible.value = false;
    if (saved.last_error)
      ElMessage.warning(`已保存，但连接失败：${saved.last_error}`);
    else ElMessage.success(`已保存，共 ${saved.tools.length} 个工具`);
  } catch (reason) {
    ElMessage.error(errorText(reason));
  } finally {
    saving.value = false;
  }
}
</script>

<template>
  <el-dialog
    v-model="visible"
    class="ai-mcp-dialog"
    title="MCP 管理"
    width="min(860px, 96vw)"
    top="5vh"
    append-to-body
    :close-on-click-modal="false"
  >
    <p class="mcp-hint">
      接入你自己的 MCP 服务（Streamable HTTP 或 SSE），启用后对话时 AI
      会按需调用其中的工具。配置只有你自己能看到；URL
      和请求头加密保存，请求头的值不会回显。
    </p>
    <el-alert
      v-if="error"
      :title="error"
      type="error"
      show-icon
      closable
      @close="error = ''"
    />
    <div class="mcp-toolbar">
      <span
        >共 {{ ai.mcpServers.length }} 个，已启用
        {{ ai.enabledMcpCount }} 个</span
      >
      <el-button :disabled="loading" @click="load">刷新</el-button>
      <el-button type="primary" @click="openEditor()">添加 MCP</el-button>
    </div>
    <div v-loading="loading" class="mcp-list" role="list" aria-label="MCP 列表">
      <section
        v-for="server in ai.mcpServers"
        :key="server.id"
        :class="['mcp-card', { off: !server.enabled }]"
        role="listitem"
      >
        <div class="mcp-heading">
          <strong class="mcp-name">{{ server.name }}</strong>
          <el-tag size="small" effect="plain">{{
            transportLabel(server.transport)
          }}</el-tag>
          <el-tooltip
            :disabled="!mcpStatus(server).detail"
            :content="mcpStatus(server).detail"
            placement="top"
          >
            <el-tag size="small" :type="mcpStatus(server).type">{{
              mcpStatus(server).label
            }}</el-tag>
          </el-tooltip>
          <el-switch
            class="mcp-switch"
            :model-value="server.enabled"
            :loading="working === server.id"
            :disabled="!!working && working !== server.id"
            inline-prompt
            active-text="启用"
            inactive-text="停用"
            :aria-label="`启用 ${server.name}`"
            @change="value => patch(server, { enabled: !!value })"
          />
        </div>
        <div class="mcp-url">{{ maskedUrl(server.url) }}</div>
        <div v-if="server.last_error" class="mcp-error">
          {{ server.last_error }}
        </div>
        <div class="mcp-meta">
          <span v-if="server.server_info?.name"
            >服务 {{ server.server_info.title || server.server_info.name
            }}{{
              server.server_info.version ? ` ${server.server_info.version}` : ""
            }}</span
          >
          <span v-if="server.headers.length"
            >请求头 {{ server.headers.map(item => item.name).join("、") }}</span
          >
        </div>
        <div class="mcp-actions">
          <span class="mcp-label">调用审批</span>
          <el-select
            class="mcp-approval"
            size="small"
            :model-value="server.approval"
            :disabled="!!working"
            @change="value => changeApproval(server, value)"
          >
            <el-option
              v-for="item in MCP_APPROVALS"
              :key="item.value"
              :value="item.value"
              :label="item.label"
              :title="item.hint"
            />
          </el-select>
          <div class="mcp-buttons">
            <el-button
              link
              :disabled="!server.tools.length"
              @click="toggleTools(server.id)"
              >{{
                expanded.has(server.id)
                  ? "收起工具"
                  : `查看工具（${server.tools.length}）`
              }}</el-button
            >
            <el-button
              link
              :loading="working === server.id"
              :disabled="!!working"
              title="重新连接并获取工具列表"
              @click="reconnect(server)"
              >重新连接</el-button
            >
            <el-button link :disabled="!!working" @click="openEditor(server)"
              >编辑</el-button
            >
            <el-button
              link
              type="danger"
              :disabled="!!working"
              @click="remove(server)"
              >删除</el-button
            >
          </div>
        </div>
        <ul v-if="expanded.has(server.id)" class="mcp-tools">
          <li v-for="tool in server.tools" :key="tool.name">
            <code>{{ tool.name }}</code>
            <el-tag
              v-if="tool.read_only"
              size="small"
              type="success"
              effect="plain"
              >只读</el-tag
            >
            <el-tag
              v-if="mcpNeedsApproval(server.approval, tool.read_only)"
              size="small"
              type="warning"
              effect="plain"
              >需批准</el-tag
            >
            <span class="mcp-tool-text"
              >{{
                tool.title && tool.title !== tool.name ? `${tool.title}：` : ""
              }}{{ tool.description }}</span
            >
          </li>
        </ul>
      </section>
      <el-empty
        v-if="!loading && !ai.mcpServers.length"
        description="还没有接入 MCP"
        :image-size="54"
      >
        <el-button type="primary" @click="openEditor()">添加 MCP</el-button>
      </el-empty>
    </div>

    <el-dialog
      v-model="editorVisible"
      :title="editingId ? '编辑 MCP' : '添加 MCP'"
      width="min(620px, 94vw)"
      append-to-body
      :close-on-click-modal="false"
      :close-on-press-escape="!saving"
      :show-close="!saving"
    >
      <el-form label-position="top" @submit.prevent="save">
        <el-form-item label="名称">
          <el-input
            v-model="form.name"
            maxlength="40"
            placeholder="例如 amap、jira；AI 调用时用它指定服务"
            :disabled="saving"
          />
        </el-form-item>
        <el-form-item label="连接方式">
          <el-radio-group v-model="form.transport" :disabled="saving">
            <el-radio-button
              v-for="item in MCP_TRANSPORTS"
              :key="item.value"
              :value="item.value"
              >{{ item.label }}</el-radio-button
            >
          </el-radio-group>
          <p class="field-hint">{{ transportHint }}</p>
        </el-form-item>
        <el-form-item label="URL">
          <el-input
            v-model="form.url"
            placeholder="https://example.com/mcp"
            :disabled="saving"
          />
          <p class="field-hint">
            由 AI 服务从集群里访问这个地址，不支持 OAuth
            登录，鉴权信息填在请求头或 URL 参数里。
          </p>
        </el-form-item>
        <el-form-item label="请求头（可选）">
          <div class="header-rows">
            <div
              v-for="(row, index) in form.headers"
              :key="index"
              class="header-row"
            >
              <el-input
                v-model="row.name"
                class="header-name"
                placeholder="名称，如 Authorization"
                :disabled="saving"
              />
              <el-input
                v-model="row.value"
                class="header-value"
                type="password"
                show-password
                autocomplete="new-password"
                :placeholder="
                  keepsSaved(row) ? '已保存，留空不修改' : '值，如 Bearer xxx'
                "
                :disabled="saving"
              />
              <el-button
                link
                type="danger"
                :disabled="saving"
                @click="form.headers.splice(index, 1)"
                >删除</el-button
              >
            </div>
            <el-button
              link
              type="primary"
              :disabled="saving || form.headers.length >= MCP_MAX_HEADERS"
              @click="addHeader"
              >＋ 添加请求头</el-button
            >
          </div>
        </el-form-item>
        <el-form-item label="调用审批">
          <el-select v-model="form.approval" :disabled="saving">
            <el-option
              v-for="item in MCP_APPROVALS"
              :key="item.value"
              :value="item.value"
              :label="item.label"
            />
          </el-select>
          <p class="field-hint">
            {{ approvalHint }}。是否只读以 MCP
            服务对工具的声明为准；对话框里勾选「自动批准执行」时同样自动批准。
          </p>
        </el-form-item>
        <el-form-item label="启用">
          <el-switch v-model="form.enabled" :disabled="saving" />
        </el-form-item>
      </el-form>
      <el-alert
        v-if="testResult"
        :title="testSummary"
        :type="testResult.success ? 'success' : 'error'"
        :closable="false"
        show-icon
      />
      <template #footer>
        <div class="editor-actions">
          <el-button :loading="testing" :disabled="saving" @click="test"
            >测试连接</el-button
          >
          <div>
            <el-button :disabled="saving" @click="editorVisible = false"
              >取消</el-button
            >
            <el-button
              type="primary"
              :loading="saving"
              :disabled="testing"
              @click="save"
              >保存</el-button
            >
          </div>
        </div>
      </template>
    </el-dialog>
  </el-dialog>
</template>

<style scoped lang="scss">
.mcp-hint,
.field-hint {
  margin: 0 0 12px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--el-text-color-secondary);
}

.field-hint {
  width: 100%;
  margin: 6px 0 0;
}

.mcp-toolbar {
  display: flex;
  gap: 8px;
  align-items: center;
  margin: 12px 0;
  font-size: 12px;
  color: var(--el-text-color-secondary);

  > span {
    flex: 1;
  }

  .el-button + .el-button {
    margin-left: 0;
  }
}

.mcp-list {
  min-height: 120px;
  max-height: 64vh;
  overflow-y: auto;
}

.mcp-card {
  padding: 12px 14px;
  margin-bottom: 10px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;

  &.off {
    background: var(--el-fill-color-lighter);

    .mcp-name {
      color: var(--el-text-color-secondary);
    }
  }
}

.mcp-heading {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}

.mcp-name {
  font-size: 14px;
  overflow-wrap: anywhere;
}

.mcp-switch {
  margin-left: auto;
}

.mcp-url {
  margin-top: 6px;
  overflow: hidden;
  font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
  font-size: 12px;
  color: var(--el-text-color-secondary);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.mcp-error {
  margin-top: 6px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--el-color-danger);
}

.mcp-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 14px;
  margin-top: 6px;
  font-size: 12px;
  color: var(--el-text-color-secondary);

  &:empty {
    display: none;
  }
}

.mcp-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  margin-top: 10px;
}

.mcp-label {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.mcp-approval {
  width: 170px;
}

.mcp-buttons {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  margin-left: auto;

  .el-button + .el-button {
    margin-left: 0;
  }
}

.mcp-tools {
  padding: 8px 0 0;
  margin: 10px 0 0;
  list-style: none;
  border-top: 1px dashed var(--el-border-color-lighter);

  li {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    align-items: baseline;
    padding: 4px 0;
    font-size: 12px;
    line-height: 1.6;
  }

  code {
    font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
    color: var(--el-text-color-primary);
  }
}

.mcp-tool-text {
  flex-basis: 100%;
  color: var(--el-text-color-secondary);
  overflow-wrap: anywhere;
}

.header-rows {
  display: flex;
  flex-direction: column;
  gap: 8px;
  align-items: flex-start;
  width: 100%;
}

.header-row {
  display: flex;
  gap: 8px;
  align-items: center;
  width: 100%;
}

.header-name {
  flex: 2;
}

.header-value {
  flex: 3;
}

.editor-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

@media (width <= 760px) {
  .mcp-buttons {
    width: 100%;
    margin-left: 0;
  }

  .header-row {
    flex-wrap: wrap;
  }

  .header-name,
  .header-value {
    flex-basis: 100%;
  }
}
</style>
