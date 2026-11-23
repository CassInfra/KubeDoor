<script setup lang="ts">
import RobotIcon from "@iconify-icons/ri/robot-2-line";
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import {
  aiApi,
  AIRequestError,
  type AIMessage,
  type AIShareView
} from "@/api/ai";
import { IconifyIconOffline } from "@/components/ReIcon";
import { useAIStoreHook } from "@/store/modules/ai";
import { aiRoleLabel, normalizeAIMessages, shareRunNotice } from "@/utils/ai";
import AIBrandAvatar from "@/components/AI/AIBrandAvatar.vue";
import AIMarkdown from "@/components/AI/AIMarkdown.vue";
import AIToolGroup from "@/components/AI/AIToolGroup.vue";
import AIMemoryAttachments from "@/components/AI/AIMemoryAttachments.vue";

defineOptions({ name: "AIShare" });

/**
 * AI 会话分享页：只读、匿名（不显示分享人）。任何登录用户凭分享码查看；查看人没有某个集群的只读权限时，
 * 服务端不返回那些消息里工具调用的参数和结果。分享是实时的：会话还有进行中的一轮时每 5 秒刷新一次。
 */
const REFRESH_MS = 5000;
const route = useRoute();
const router = useRouter();
const code = computed(() => String(route.params.code || ""));
const view = ref<AIShareView | null>(null);
const messages = ref<AIMessage[]>([]);
const loading = ref(false);
const missing = ref(false);
const error = ref("");
const expanded = ref(new Set<string>());
let timer: number | undefined;
let controller: AbortController | null = null;
let generation = 0;

const runNotice = computed(() =>
  shareRunNotice(view.value?.active_run?.status)
);
const role = (message: AIMessage) => aiRoleLabel(message.role, true);
const time = (value?: string) => {
  if (!value) return "";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
};

function setExpanded(id: string, value: boolean) {
  const next = new Set(expanded.value);
  if (value) next.add(id);
  else next.delete(id);
  expanded.value = next;
}

/** 会话还在进行时定时刷新；页面在后台时先不请求，等切回来再刷新 */
function schedule() {
  window.clearTimeout(timer);
  timer = undefined;
  if (!view.value?.active_run) return;
  timer = window.setTimeout(() => {
    if (document.visibilityState === "hidden") schedule();
    else void load();
  }, REFRESH_MS);
}

async function load() {
  const current = ++generation;
  controller?.abort();
  window.clearTimeout(timer);
  if (!code.value) {
    missing.value = true;
    return;
  }
  const request = new AbortController();
  controller = request;
  loading.value = true;
  try {
    const data = await aiApi.sharedSession(code.value, request.signal);
    if (current !== generation) return;
    view.value = data;
    messages.value = normalizeAIMessages(data.messages);
    missing.value = false;
    error.value = "";
  } catch (reason) {
    if (current !== generation || request.signal.aborted) return;
    if (reason instanceof AIRequestError && reason.status === 404) {
      missing.value = true;
      view.value = null;
      messages.value = [];
    } else {
      error.value = reason instanceof Error ? reason.message : "读取分享失败";
    }
  } finally {
    if (current === generation) {
      loading.value = false;
      controller = null;
      schedule();
    }
  }
}

function openConsole() {
  void router.push("/monit/overview");
}

// 「访问 花折 AI」：回到控制台并直接打开 AI 助手（对话框挂在控制台布局里，先打开再跳过去）
const ai = useAIStoreHook();
function visitAI() {
  void ai.open();
  openConsole();
}

watch(
  code,
  () => {
    view.value = null;
    messages.value = [];
    missing.value = false;
    error.value = "";
    expanded.value = new Set();
    void load();
  },
  { immediate: true }
);
onBeforeUnmount(() => {
  generation++;
  controller?.abort();
  window.clearTimeout(timer);
});
</script>

<template>
  <div class="ai-share">
    <header class="share-header">
      <div class="share-brand">
        <AIBrandAvatar :size="40" />
        <div class="share-heading">
          <h1 :title="view?.title">{{ view?.title || "AI 会话分享" }}</h1>
          <p v-if="view">
            分享于 {{ time(view.shared_at) }} · 最后更新
            {{ time(view.updated_at) }}
          </p>
          <p v-else>花折 AI 会话分享</p>
        </div>
      </div>
      <div class="share-actions">
        <el-button
          :loading="loading"
          :disabled="missing && !code"
          @click="load()"
          >刷新</el-button
        >
        <button type="button" class="visit-ai" @click="visitAI">
          <span class="visit-effects" aria-hidden="true">
            <span class="visit-sheen" />
          </span>
          <IconifyIconOffline
            class="visit-robot"
            :icon="RobotIcon"
            width="18"
            height="18"
          />
          <span class="visit-label">访问 花折 AI</span>
          <span class="visit-arrow" aria-hidden="true">→</span>
        </button>
      </div>
    </header>

    <main v-loading="loading && !view && !missing" class="share-body">
      <el-result
        v-if="missing"
        icon="warning"
        title="分享链接无效或已关闭"
        sub-title="请向分享人确认链接是否正确，或者分享是否已经关闭。"
      >
        <template #extra>
          <el-button type="primary" @click="openConsole"
            >打开 KubeDoor</el-button
          >
        </template>
      </el-result>
      <template v-else>
        <el-alert
          v-if="error"
          :title="error"
          type="error"
          show-icon
          :closable="false"
          class="share-alert"
        />
        <el-alert
          v-if="view"
          type="info"
          show-icon
          :closable="false"
          class="share-alert"
          title="只读的 AI 会话分享：可以查看问答和工具调用，不能继续提问或审批；会话之后的新消息也会同步到这里。"
        />
        <section v-if="view" class="message-list" aria-label="AI 对话">
          <p v-if="!messages.length && !runNotice" class="share-empty">
            这个会话还没有内容。
          </p>
          <article
            v-for="message in messages"
            :key="message.id"
            :class="[
              'chat-message',
              message.role === 'user' ? 'user-message' : 'assistant-message'
            ]"
          >
            <div class="message-heading">
              <span class="message-role"
                ><span class="role-icon" aria-hidden="true">{{
                  role(message).icon
                }}</span
                >{{ role(message).name }}</span
              >
              <el-tag v-if="message.scope?.env" size="small" effect="plain">{{
                message.scope.env
              }}</el-tag>
              <span v-if="message.scope?.namespace" class="message-scope">{{
                message.scope.namespace
              }}</span>
              <span class="message-time">{{ time(message.created_at) }}</span>
            </div>
            <AIMemoryAttachments
              v-if="message.memories?.length"
              :memories="message.memories"
            />
            <p v-if="message.tools_hidden" class="tools-hidden">
              🔒 工具调用的参数和结果需要集群
              {{ message.scope?.env || "" }} 的只读权限才能查看
            </p>
            <AIToolGroup
              v-if="message.tools?.length"
              :tools="message.tools"
              :expanded="expanded.has(message.id)"
              :active="false"
              readonly
              @update:expanded="value => setExpanded(message.id, value)"
            />
            <AIMarkdown
              v-if="message.content && message.role === 'assistant'"
              :content="message.content"
            />
            <div v-else-if="message.content" class="message-text">
              {{ message.content }}
            </div>
          </article>
          <div v-if="runNotice" class="run-notice">
            <span class="run-spinner" aria-hidden="true" />{{ runNotice }}
            <span class="run-hint">每 5 秒自动刷新</span>
          </div>
        </section>
      </template>
    </main>
  </div>
</template>

<style scoped lang="scss">
.ai-share {
  height: 100%;
  overflow-y: auto;
  color: var(--el-text-color-primary);
  background: var(--el-bg-color-page);
}

.share-header {
  position: sticky;
  top: 0;
  z-index: 2;
  display: flex;
  flex-wrap: wrap;
  gap: 12px 20px;
  align-items: center;
  justify-content: space-between;
  // 左右各留 10%，和下面占 80% 宽的内容对齐
  padding: 14px 10%;
  background: var(--el-bg-color);
  border-bottom: 1px solid var(--el-border-color-lighter);
}

.share-brand {
  display: flex;
  gap: 12px;
  align-items: center;
  min-width: 0;
}

.share-heading {
  min-width: 0;

  h1 {
    margin: 0;
    overflow: hidden;
    font-size: 18px;
    font-weight: 600;
    line-height: 1.4;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  p {
    margin: 2px 0 0;
    font-size: 12px;
    color: var(--el-text-color-secondary);
  }
}

.share-actions {
  display: flex;
  gap: 10px;
  align-items: center;

  .el-button + .el-button {
    margin-left: 0;
  }
}

// 「访问 花折 AI」：和顶栏「AI 助手」按钮同一套渐变，颜色流动、光泽扫过、外圈光晕呼吸，悬停时上浮、箭头前移
.visit-ai {
  position: relative;
  display: inline-flex;
  gap: 8px;
  align-items: center;
  justify-content: center;
  height: 36px;
  padding: 0 18px 0 14px;
  font-size: 14px;
  font-weight: 600;
  color: #fff;
  white-space: nowrap;
  cursor: pointer;
  background: linear-gradient(
    115deg,
    #075bc6,
    #6341ce 42%,
    #b92789 76%,
    #ba315f
  );
  background-size: 240% 240%;
  isolation: isolate;
  border: 1px solid rgb(255 255 255 / 35%);
  border-radius: 999px;
  box-shadow:
    0 4px 12px rgb(92 61 201 / 28%),
    inset 0 1px 0 rgb(255 255 255 / 20%);
  transition:
    transform 200ms ease,
    box-shadow 200ms ease;
  animation: visit-flow 8s ease-in-out infinite;

  &::before {
    position: absolute;
    inset: -3px;
    z-index: -1;
    pointer-events: none;
    content: "";
    background: linear-gradient(110deg, #22b9e8, #8b5cf6 50%, #ed5cba);
    filter: blur(8px);
    border-radius: inherit;
    opacity: 0.35;
    animation: visit-halo 4s ease-in-out infinite;
  }

  &:hover {
    box-shadow:
      0 8px 20px rgb(92 61 201 / 40%),
      inset 0 1px 0 rgb(255 255 255 / 30%);
    transform: translateY(-1px) scale(1.03);

    .visit-arrow {
      transform: translateX(3px);
    }
  }

  &:active {
    transform: scale(0.97);
  }

  &:focus-visible {
    outline: 2px solid var(--el-color-primary);
    outline-offset: 4px;
  }
}

.visit-effects {
  position: absolute;
  inset: 0;
  overflow: hidden;
  pointer-events: none;
  border-radius: inherit;
}

.visit-sheen {
  position: absolute;
  top: -20%;
  left: -50%;
  width: 35%;
  height: 140%;
  background: linear-gradient(
    90deg,
    transparent,
    rgb(255 255 255 / 32%),
    transparent
  );
  transform: skewX(-22deg);
  animation: visit-sheen 4.5s ease-in-out infinite;
}

.visit-robot {
  position: relative;
  animation: visit-float 3.6s ease-in-out infinite;
}

.visit-label {
  position: relative;
  text-shadow: 0 1px 2px rgb(24 18 68 / 20%);
  letter-spacing: 0.5px;
}

.visit-arrow {
  position: relative;
  transition: transform 200ms ease;
}

@keyframes visit-flow {
  0%,
  100% {
    background-position: 0% 50%;
  }

  50% {
    background-position: 100% 50%;
  }
}

@keyframes visit-halo {
  0%,
  100% {
    opacity: 0.25;
  }

  50% {
    opacity: 0.55;
  }
}

@keyframes visit-sheen {
  0%,
  60% {
    transform: translateX(0) skewX(-22deg);
  }

  100% {
    transform: translateX(520%) skewX(-22deg);
  }
}

@keyframes visit-float {
  0%,
  100% {
    transform: translateY(1px) rotate(-4deg);
  }

  50% {
    transform: translateY(-1px) rotate(4deg);
  }
}

// 内容占浏览器宽度的 80%（手机上铺满）
.share-body {
  width: 80%;
  min-height: 240px;
  padding: 20px 0;
  margin: 0 auto;
}

.share-alert {
  margin-bottom: 16px;
}

.share-empty {
  padding: 40px 0;
  font-size: 13px;
  color: var(--el-text-color-secondary);
  text-align: center;
}

.chat-message {
  padding: 14px;
  margin-bottom: 18px;
  background: var(--el-bg-color);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 10px;
}

.user-message {
  background: var(--el-color-primary-light-9);
  border-color: transparent;
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

.message-role {
  font-weight: 600;
  color: var(--el-text-color-primary);
}

.role-icon {
  margin-right: 4px;
  font-size: 14px;
  line-height: 1;
}

.message-time {
  margin-left: auto;
  font-size: 11px;
}

.message-text {
  font-size: 14px;
  line-height: 1.75;
  overflow-wrap: anywhere;
  white-space: pre-wrap;
}

.tools-hidden {
  padding: 8px 12px;
  margin: 0 0 12px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
  background: var(--el-fill-color-light);
  border-radius: 6px;
}

.run-notice {
  display: flex;
  gap: 8px;
  align-items: center;
  padding: 4px 2px 20px;
  font-size: 13px;
  color: var(--el-color-primary);
}

.run-hint {
  font-size: 11px;
  color: var(--el-text-color-secondary);
}

.run-spinner {
  width: 12px;
  height: 12px;
  border: 1.5px solid var(--el-color-primary-light-7);
  border-top-color: currentcolor;
  border-radius: 50%;
  animation: ai-share-spin 800ms linear infinite;
}

@keyframes ai-share-spin {
  to {
    transform: rotate(360deg);
  }
}

@media (prefers-reduced-motion: reduce) {
  .run-spinner,
  .visit-ai,
  .visit-ai::before,
  .visit-sheen,
  .visit-robot {
    transition: none;
    animation: none;
  }
}

@media (width <= 760px) {
  .share-header {
    padding: 10px 12px;
  }

  .share-actions {
    width: 100%;

    .el-button,
    .visit-ai {
      flex: 1;
    }
  }

  .share-body {
    width: auto;
    padding: 12px 8px;
  }

  .chat-message {
    padding: 12px 10px;
  }

  .message-time {
    width: 100%;
    margin-left: 0;
  }
}
</style>
