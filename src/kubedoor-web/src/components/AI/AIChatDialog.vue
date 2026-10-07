<script setup lang="ts">
import { computed, nextTick, ref, watch } from "vue";
import { useMediaQuery } from "@vueuse/core";
import { ElMessage, ElMessageBox } from "element-plus";
import { useAIStoreHook } from "@/store/modules/ai";
import { formatAIStatus } from "@/utils/ai";
import type { AIMessage, AISession, AIMemoryDraft } from "@/api/ai";
import AIScopeSelector from "./AIScopeSelector.vue";
import AIProviderDialog from "./AIProviderDialog.vue";
import AIToolGroup from "./AIToolGroup.vue";
import AIMarkdown from "./AIMarkdown.vue";
import AIMemoryDialog from "./AIMemoryDialog.vue";
import AIMemoryEditor from "./AIMemoryEditor.vue";
import AIMemoryAttachments from "./AIMemoryAttachments.vue";

const ai = useAIStoreHook();
const mobile = useMediaQuery("(max-width: 760px)");
const providerVisible = ref(false);
const memoryVisible = ref(false);
const memoryEditorVisible = ref(false);
const memoryDraft = ref<AIMemoryDraft>({ title: "", content: "" });
let summaryGeneration = 0;
const showSessions = ref(false);
const input = ref("");
const operating = ref(false);
const scrollRef = ref<HTMLElement | null>(null);
const followBottom = ref(true);
const expandedToolGroups = ref(new Set<string>());
const selectedSession = computed(() =>
  ai.sessions.find(item => item.id === ai.sessionId)
);
const skillLabel = (name: string) =>
  ai.bootstrap?.skills.find(skill => (skill.id || skill.name) === name)?.name ||
  name;
const canSend = computed(
  () =>
    !!input.value.trim() &&
    !ai.busy &&
    ai.sessionReady &&
    !operating.value &&
    ai.configured &&
    ai.clusterAvailable &&
    ai.bootstrap?.enabled
);
const runStatus = computed(() =>
  ai.activeRun ? formatAIStatus(ai.activeRun.status) : ""
);
const canSummarize = computed(
  () =>
    ai.configured &&
    ai.sessionReady &&
    !ai.busy &&
    !ai.summarizingMemory &&
    !operating.value &&
    !!ai.sessionId &&
    !selectedSession.value?.draft &&
    ai.messages.some(
      message => message.role === "assistant" && !!message.content.trim()
    )
);
watch(
  () => [ai.visible, ai.bootstrap?.username, ai.sessionId],
  () => {
    summaryGeneration++;
    memoryEditorVisible.value = false;
    memoryVisible.value = false;
    memoryDraft.value = { title: "", content: "" };
  }
);

async function summarize() {
  if (!canSummarize.value) return;
  const generation = summaryGeneration;
  const owner = ai.bootstrap?.username;
  const session = ai.sessionId;
  try {
    const draft = await ai.summarizeMemory();
    if (
      !draft ||
      generation !== summaryGeneration ||
      owner !== ai.bootstrap?.username ||
      session !== ai.sessionId ||
      !ai.visible
    )
      return;
    memoryDraft.value = {
      title: redact(draft.title),
      content: redact(draft.content)
    };
    memoryEditorVisible.value = true;
  } catch (reason) {
    if (generation === summaryGeneration && ai.visible)
      ElMessage.error(
        redact(
          reason instanceof Error
            ? reason.message
            : "会话总结失败，请稍后重试。"
        )
      );
  }
}

function toolGroupKeys(message: AIMessage) {
  // Cards retain their ID when later events add approval/action metadata.
  // Keep every card as an anchor so parallel history reordering is harmless.
  return (message.tools || []).map(tool => `${ai.sessionId}:${tool.id}`);
}
function isToolGroupExpanded(message: AIMessage) {
  return toolGroupKeys(message).some(key => expandedToolGroups.value.has(key));
}
function setToolGroupExpanded(message: AIMessage, expanded: boolean) {
  for (const key of toolGroupKeys(message)) {
    if (expanded) expandedToolGroups.value.add(key);
    else expandedToolGroups.value.delete(key);
  }
  void scrollToBottom();
}

function redact(value: string) {
  return ai.provider.api_key
    ? value.split(ai.provider.api_key).join("[已隐藏]")
    : value;
}
function onScroll() {
  const element = scrollRef.value;
  if (element)
    followBottom.value =
      element.scrollHeight - element.scrollTop - element.clientHeight < 100;
}
async function scrollToBottom(force = false) {
  await nextTick();
  if (scrollRef.value && (force || followBottom.value))
    scrollRef.value.scrollTop = scrollRef.value.scrollHeight;
}
watch(
  () => [
    ai.messages.length,
    ai.messages.at(-1)?.content,
    ai.messages.at(-1)?.tools?.length,
    ai.pendingTools.length
  ],
  () => void scrollToBottom()
);
watch(
  () => ai.visible,
  value => {
    if (value) void scrollToBottom(true);
  }
);

async function action(work: () => Promise<unknown>) {
  if (operating.value) return;
  operating.value = true;
  try {
    await work();
  } catch (reason) {
    if (reason !== "cancel" && reason !== "close")
      ElMessage.error(
        redact(
          reason instanceof Error ? reason.message : "操作失败，请稍后重试。"
        )
      );
  } finally {
    operating.value = false;
  }
}
async function selectSession(session: AISession) {
  if (ai.busy || operating.value) return;
  showSessions.value = false;
  try {
    await ai.selectSession(session.id);
    if (ai.sessionId !== session.id) return;
    followBottom.value = true;
    showSessions.value = false;
    await scrollToBottom(true);
  } catch (reason) {
    ElMessage.error(reason instanceof Error ? reason.message : "历史加载失败");
  }
}
async function newSession() {
  await action(async () => {
    await ai.createSession();
    input.value = "";
    showSessions.value = false;
  });
}
async function rename() {
  if (!selectedSession.value) return;
  await action(async () => {
    const { value } = await ElMessageBox.prompt("输入会话名称", "重命名会话", {
      inputValue: selectedSession.value!.title,
      inputPattern: /\S/,
      inputErrorMessage: "名称不能为空"
    });
    await ai.renameSession(ai.sessionId, value);
  });
}
async function remove() {
  if (!selectedSession.value) return;
  await action(async () => {
    await ElMessageBox.confirm(
      `删除会话“${selectedSession.value!.title}”及其聊天历史？`,
      "删除会话",
      { type: "warning" }
    );
    await ai.deleteSession(ai.sessionId);
  });
}
async function send() {
  if (!canSend.value) return;
  await action(async () => {
    const message = input.value;
    await ai.send(message);
    input.value = "";
    followBottom.value = true;
    await scrollToBottom(true);
  });
}
function keydown(event: KeyboardEvent) {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    void send();
  }
}
async function refresh() {
  await action(async () => {
    await ai.refreshBootstrap();
    await ai.refreshSessions();
    if (ai.sessionId) await ai.selectSession(ai.sessionId);
  });
}
async function copy(message: AIMessage) {
  try {
    await navigator.clipboard.writeText(redact(message.content));
    ElMessage.success("已复制");
  } catch {
    ElMessage.warning("浏览器不允许复制，请手动选择文本。");
  }
}
const timestamp = (value?: string) => {
  if (!value) return "";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
};
</script>

<template>
  <el-dialog
    v-model="ai.visible"
    class="kubedoor-ai-dialog"
    width="min(1500px, 96vw)"
    top="2vh"
    :fullscreen="mobile"
    append-to-body
    :close-on-click-modal="false"
    :close-on-press-escape="false"
  >
    <template #header>
      <div class="ai-header">
        <span class="ai-badge">AI</span>
        <span class="ai-title">KubeDoor 助手</span>
        <el-tag
          v-if="ai.bootstrap?.permission === 'read'"
          size="small"
          effect="plain"
          >只读</el-tag
        >
        <span v-if="ai.bootstrap?.username" class="ai-owner">{{
          ai.bootstrap.username
        }}</span>
        <div class="header-actions">
          <el-button
            v-if="mobile"
            size="small"
            @click="showSessions = !showSessions"
            >会话</el-button
          >
          <el-button
            size="small"
            :disabled="operating || ai.loading"
            @click="refresh"
            >刷新</el-button
          >
          <el-button
            size="small"
            :disabled="!ai.bootstrap?.username"
            @click="providerVisible = true"
            >模型设置</el-button
          >
        </div>
      </div>
    </template>

    <div v-loading="ai.loading && !ai.bootstrap" class="ai-body">
      <el-alert
        v-if="ai.error"
        :title="redact(ai.error)"
        type="error"
        show-icon
        closable
        @close="ai.error = ''"
      />
      <el-alert
        v-if="ai.bootstrap && !ai.bootstrap.enabled"
        title="AI 服务未启用，请在服务端启用后重试。"
        type="info"
        :closable="false"
        show-icon
      />
      <div v-if="ai.bootstrap?.enabled" class="ai-workspace">
        <aside :class="['session-sidebar', { 'mobile-open': showSessions }]">
          <el-button
            type="primary"
            plain
            :disabled="ai.busy || operating"
            class="new-session"
            @click="newSession"
            >＋ 新建会话</el-button
          >
          <div class="session-list">
            <button
              v-for="session in ai.sessions"
              :key="session.id"
              :class="['session-item', { active: session.id === ai.sessionId }]"
              :disabled="ai.busy || operating"
              :title="session.title"
              @click="selectSession(session)"
            >
              <span>{{ session.title || "新会话" }}</span>
              <small>{{
                session.draft
                  ? "未发送"
                  : session.id === ai.sessionId && ai.sessionLoading
                    ? "正在同步…"
                    : timestamp(session.updated_at || session.created_at)
              }}</small>
            </button>
            <p v-if="!ai.sessions.length" class="sidebar-empty">
              新建会话，开始处理集群问题。
            </p>
          </div>
          <div class="session-actions">
            <el-button
              link
              :disabled="!selectedSession || ai.busy || operating"
              @click="rename"
              >重命名</el-button
            >
            <el-button
              link
              type="danger"
              :disabled="!selectedSession || ai.busy || operating"
              @click="remove"
              >删除</el-button
            >
          </div>
          <p class="sidebar-note">历史保存在服务端，仅当前账号可见。</p>
        </aside>

        <main class="conversation">
          <div class="context-section">
            <AIScopeSelector
              :model-value="ai.scope"
              :clusters="ai.bootstrap.clusters"
              :disabled="ai.busy || operating || !ai.sessionReady"
              @update:model-value="ai.changeScope"
            />
            <div class="context-extra">
              <span>资源选择提供对话上下文；每轮仅操作选中的集群。</span>
              <el-checkbox
                v-model="ai.autoApprove"
                class="auto-approve"
                :disabled="!ai.writable"
                :title="
                  ai.writable
                    ? '勾选后自动批准工具操作；取消勾选后恢复逐次确认。'
                    : '当前账号只读，不能批准执行。'
                "
                >自动批准执行</el-checkbox
              >
              <el-select
                v-if="ai.bootstrap.skills.length"
                v-model="ai.skillIds"
                class="skill-selector"
                multiple
                collapse-tags
                collapse-tags-tooltip
                :disabled="ai.busy"
                placeholder="Skills：自动匹配"
                aria-label="指定 Skills"
              >
                <el-option
                  v-for="skill in ai.bootstrap.skills"
                  :key="skill.id || skill.name"
                  :value="skill.id || skill.name"
                  :label="skill.name"
                  :title="skill.description"
                >
                  <span>{{ skill.name }}</span>
                </el-option>
              </el-select>
            </div>
            <div v-if="ai.skillIds.length" class="skill-descriptions">
              <span v-for="id in ai.skillIds" :key="id"
                >{{ skillLabel(id) }}：{{
                  ai.bootstrap.skills.find(
                    skill => (skill.id || skill.name) === id
                  )?.description || "使用此 Skill"
                }}</span
              >
            </div>
          </div>

          <div
            ref="scrollRef"
            v-loading="ai.sessionLoading && !ai.messages.length"
            class="message-list"
            role="log"
            aria-label="AI 对话"
            @scroll="onScroll"
          >
            <div v-if="!ai.messages.length" class="conversation-empty">
              <div class="empty-symbol">AI</div>
              <h3>描述你要处理的 Kubernetes 问题</h3>
              <p>可以查询日志、分析资源、修改配置或排查 Istio 路由。</p>
              <p>
                {{
                  ai.autoApprove
                    ? "助手会选择合适的数据来源，工具操作将自动批准执行。"
                    : "助手会选择合适的数据来源，修改前展示操作供你批准。"
                }}
              </p>
              <el-button
                v-if="!ai.configured"
                type="primary"
                @click="providerVisible = true"
                >配置模型</el-button
              >
            </div>
            <article
              v-for="message in ai.messages"
              :key="message.id"
              :class="[
                'chat-message',
                message.role === 'user' ? 'user-message' : 'assistant-message'
              ]"
            >
              <div class="message-heading">
                <span>{{
                  message.role === "user"
                    ? "你"
                    : message.role === "system"
                      ? "系统"
                      : "AI 助手"
                }}</span>
                <el-tag v-if="message.scope?.env" size="small" effect="plain">{{
                  message.scope.env
                }}</el-tag>
                <span v-if="message.scope?.namespace" class="message-scope">{{
                  message.scope.namespace
                }}</span>
                <span class="message-time">{{
                  timestamp(message.created_at)
                }}</span>
                <el-button
                  v-if="message.content"
                  link
                  size="small"
                  @click="copy(message)"
                  >复制</el-button
                >
              </div>
              <AIMemoryAttachments
                v-if="message.memories?.length"
                :memories="message.memories"
                :secret="ai.provider.api_key"
              />
              <AIToolGroup
                v-if="message.tools?.length"
                :tools="message.tools"
                :expanded="isToolGroupExpanded(message)"
                :active="ai.busy && message === ai.messages.at(-1)"
                :writable="ai.writable && ai.sessionReady"
                :deciding="ai.deciding"
                :secret="ai.provider.api_key"
                @update:expanded="value => setToolGroupExpanded(message, value)"
                @decide="
                  (id, decision) => action(() => ai.decide(id, decision))
                "
              />
              <AIMarkdown
                v-if="message.content && message.role === 'assistant'"
                :content="redact(message.content)"
              />
              <div v-else-if="message.content" class="message-text">
                {{ redact(message.content) }}
              </div>
              <span
                v-else-if="
                  message.role === 'assistant' &&
                  ai.busy &&
                  !message.tools?.length
                "
                class="thinking"
                >正在思考并检查集群…</span
              >
            </article>
          </div>

          <div class="composer">
            <div v-if="ai.selectedMemories.length" class="selected-memories">
              <span>下一轮引入：</span
              ><el-tag
                v-for="memory in ai.selectedMemories"
                :key="memory.id"
                :closable="!ai.busy"
                :title="redact(memory.title)"
                @close="ai.removeSelectedMemory(memory.id)"
                >{{ redact(memory.title) }}</el-tag
              >
            </div>
            <div v-if="!ai.sessionReady" class="stream-notice">
              正在核验会话状态，可继续输入或切换；核验完成后可发送和审批。
            </div>
            <div v-if="ai.streamError" class="stream-notice">
              {{ redact(ai.streamError) }}
            </div>
            <div v-if="ai.busy" class="run-notice">
              <span
                >{{ ai.activeRun?.scope?.env || ai.scope.env }} · {{ runStatus
                }}{{
                  ai.streamState === "reconnecting" ? " · 正在重连" : ""
                }}</span
              >
              <span class="run-hint"
                >关闭弹框后继续执行；切换集群或会话前请停止当前轮次。</span
              >
            </div>
            <el-input
              v-model="input"
              type="textarea"
              :autosize="{ minRows: 2, maxRows: 5 }"
              maxlength="30000"
              placeholder="描述问题或操作要求…（Enter 发送，Shift + Enter 换行）"
              :disabled="ai.busy || !ai.bootstrap.enabled"
              @keydown="keydown"
            />
            <div class="composer-actions">
              <span class="model-status" :title="ai.provider.model">{{
                ai.configured ? ai.provider.model : "尚未配置模型"
              }}</span>
              <span
                v-if="ai.selectedCluster && !ai.liveAvailable"
                class="unavailable"
                >可查询指标和历史数据，实时操作暂不可用</span
              >
              <div class="memory-actions">
                <el-button
                  :loading="ai.summarizingMemory"
                  :disabled="!canSummarize"
                  @click="summarize"
                  >总结为记忆</el-button
                >
                <el-button @click="memoryVisible = true"
                  >引入记忆（{{ ai.selectedMemories.length }}）</el-button
                >
                <el-button
                  v-if="ai.busy"
                  type="danger"
                  plain
                  :loading="ai.cancelling"
                  :disabled="!!ai.deciding"
                  @click="action(() => ai.cancel())"
                  >停止</el-button
                >
                <el-button
                  v-else
                  type="primary"
                  :loading="ai.submitting || operating"
                  :disabled="!canSend"
                  @click="send"
                  >发送</el-button
                >
              </div>
            </div>
          </div>
        </main>
      </div>
    </div>
    <AIProviderDialog v-model="providerVisible" />
    <AIMemoryDialog
      v-model="memoryVisible"
      :selected="ai.selectedMemories"
      :writable="ai.writable"
      :busy="ai.busy || !ai.sessionReady"
      :owner="ai.bootstrap?.username || ''"
      :secret="ai.provider.api_key"
      @select="ai.setSelectedMemories"
      @deleted="ai.removeSelectedMemory"
    />
    <AIMemoryEditor
      v-model="memoryEditorVisible"
      :draft="memoryDraft"
      :writable="ai.writable"
      :owner="ai.bootstrap?.username || ''"
      :secret="ai.provider.api_key"
      @saved="ElMessage.success('记忆已保存，可通过引入记忆选择。')"
    />
  </el-dialog>
</template>

<style lang="scss">
.kubedoor-ai-dialog {
  --el-dialog-padding-primary: 20px;

  display: flex;
  flex-direction: column;
  max-height: 96dvh;
  margin-bottom: 2vh;

  .el-dialog__body {
    display: flex;
    flex-direction: column;
    min-height: 0;
    padding: 0;
    overflow: hidden;
  }

  .el-dialog__header {
    flex-shrink: 0;
    padding-right: 32px;
    margin-right: 0;
  }

  &.is-fullscreen {
    max-height: 100dvh;
    margin: 0;
  }
}
</style>

<style scoped lang="scss">
.ai-header {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  align-items: center;
  min-height: 34px;
}

.ai-badge {
  display: grid;
  place-items: center;
  width: 30px;
  height: 30px;
  font-size: 13px;
  font-weight: 700;
  color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
  border-radius: 8px;
}

.ai-title {
  font-size: 17px;
  font-weight: 600;
}

.ai-owner {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.header-actions {
  display: flex;
  gap: 8px;
  margin-left: auto;
}

.header-actions .el-button + .el-button {
  margin-left: 0;
}

.ai-body {
  display: flex;
  flex-direction: column;
  min-height: 0;
}

.ai-body > .el-alert {
  margin-bottom: 12px;
}

.ai-workspace {
  display: flex;
  height: min(85dvh, 1000px);
  min-height: 0;
  border-top: 1px solid var(--el-border-color-lighter);
}

.session-sidebar {
  display: flex;
  flex-shrink: 0;
  flex-direction: column;
  width: 212px;
  padding: 16px 12px 8px 0;
  border-right: 1px solid var(--el-border-color-lighter);
}

.new-session {
  width: 100%;
}

.session-list {
  flex: 1;
  padding: 12px 0;
  overflow-y: auto;
}

.session-item {
  display: flex;
  flex-direction: column;
  gap: 6px;
  width: 100%;
  padding: 12px 10px;
  color: var(--el-text-color-primary);
  text-align: left;
  cursor: pointer;
  background: transparent;
  border: 0;
  border-radius: 6px;
}

.session-item span {
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.session-item small {
  font-size: 10px;
  color: var(--el-text-color-secondary);
}

.session-item:hover,
.session-item.active {
  background: var(--el-color-primary-light-9);
}

.session-item.active span {
  color: var(--el-color-primary);
}

.session-item:disabled {
  cursor: default;
  opacity: 0.7;
}

.session-actions {
  display: flex;
  gap: 12px;
  padding-top: 10px;
  border-top: 1px solid var(--el-border-color-lighter);
}

.sidebar-note,
.sidebar-empty {
  margin-top: 12px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--el-text-color-secondary);
}

.conversation {
  display: flex;
  flex: 1;
  flex-direction: column;
  min-width: 0;
}

.context-section {
  padding: 14px 18px 12px;
  border-bottom: 1px solid var(--el-border-color-lighter);
}

.context-extra {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  align-items: center;
  margin-top: 10px;
  font-size: 11px;
  color: var(--el-text-color-secondary);
}

.auto-approve {
  height: 32px;
  margin-right: 0;
}

.context-extra > span {
  flex: 1;
}

.skill-selector {
  width: 220px;
}

.skill-descriptions {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-top: 8px;
  font-size: 11px;
  color: var(--el-text-color-secondary);
}

.message-list {
  flex: 1;
  min-height: 0;
  padding: 20px 18px;
  overflow-y: auto;
  overscroll-behavior: contain;
}

.conversation-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  min-height: 280px;
  color: var(--el-text-color-secondary);
  text-align: center;
}

.empty-symbol {
  display: grid;
  place-items: center;
  width: 48px;
  height: 48px;
  margin-bottom: 8px;
  font-size: 20px;
  font-weight: 700;
  color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
  border-radius: 14px;
}

.conversation-empty h3 {
  margin: 12px 0;
  font-size: 16px;
  color: var(--el-text-color-primary);
}

.conversation-empty p {
  margin: 4px 0;
  font-size: 13px;
  line-height: 1.7;
}

.conversation-empty .el-button {
  margin-top: 18px;
}

.chat-message {
  padding: 14px;
  margin-bottom: 22px;
  background: var(--el-fill-color-lighter);
  border-radius: 10px;
}

.user-message {
  background: var(--el-color-primary-light-9);
}

.message-heading {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  margin-bottom: 10px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.message-heading > span:first-child {
  font-weight: 600;
  color: var(--el-text-color-primary);
}

.message-time {
  margin-left: auto;
  font-size: 11px;
}

.message-text {
  font-size: 14px;
  line-height: 1.75;
  color: var(--el-text-color-primary);
  overflow-wrap: anywhere;
  white-space: pre-wrap;
}

.thinking {
  font-size: 13px;
  color: var(--el-text-color-secondary);
}

.composer {
  padding: 12px 18px 0;
  border-top: 1px solid var(--el-border-color-lighter);
}

.composer-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  align-items: center;
  margin-top: 10px;
}

.memory-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  margin-left: auto;
}

.memory-actions .el-button + .el-button {
  margin-left: 0;
}

.selected-memories {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
  max-height: 96px;
  margin-bottom: 10px;
  overflow-y: auto;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.selected-memories :deep(.el-tag__content) {
  max-width: 220px;
  overflow: hidden;
  text-overflow: ellipsis;
}

.model-status {
  max-width: 40%;
  overflow: hidden;
  font-size: 12px;
  color: var(--el-text-color-secondary);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.unavailable {
  font-size: 12px;
  color: var(--el-color-warning);
}

.stream-notice {
  margin-bottom: 8px;
  font-size: 12px;
  color: var(--el-color-warning);
}

.run-notice {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 10px;
  font-size: 12px;
  color: var(--el-color-primary);
}

.run-hint {
  font-size: 11px;
  color: var(--el-text-color-secondary);
}

@media (width <= 760px) {
  .ai-workspace {
    position: relative;
    height: calc(100dvh - 118px);
    min-height: 0;
  }

  .ai-owner {
    display: none;
  }

  .ai-title {
    font-size: 15px;
  }

  .header-actions {
    width: 100%;
    margin-left: 0;
  }

  .session-sidebar {
    position: absolute;
    inset: 0 auto 0 0;
    z-index: 2;
    display: none;
    width: 230px;
    padding: 12px;
    background: var(--el-bg-color);
    box-shadow: 4px 0 10px var(--el-border-color-lighter);
  }

  .session-sidebar.mobile-open {
    display: flex;
  }

  .context-section,
  .message-list,
  .composer {
    padding-right: 8px;
    padding-left: 8px;
  }

  .context-extra {
    flex-direction: column;
    align-items: stretch;
  }

  .skill-selector {
    width: 100%;
  }

  .chat-message {
    padding: 12px 10px;
  }

  .message-time {
    display: none;
  }

  .run-hint {
    display: none;
  }

  .memory-actions {
    justify-content: flex-end;
    width: 100%;
  }

  .selected-memories :deep(.el-tag__content) {
    max-width: 150px;
  }
}
</style>
