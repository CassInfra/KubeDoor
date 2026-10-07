<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref, watch } from "vue";
import {
  aiApi,
  AIRequestError,
  type AIMemory,
  type AIMemoryDraft
} from "@/api/ai";
import AIMarkdown from "./AIMarkdown.vue";

const visible = defineModel<boolean>({ default: false });
const props = defineProps<{
  draft: AIMemoryDraft & { id?: string; version?: number };
  writable: boolean;
  owner: string;
  secret?: string;
}>();
const emit = defineEmits<{ saved: [memory: AIMemory] }>();
const form = reactive<AIMemoryDraft>({ title: "", content: "" });
const saving = ref(false);
const reloading = ref(false);
const error = ref("");
const conflict = ref(false);
const tab = ref("edit");
const version = ref<number | undefined>();
const editing = computed(() => !!props.draft.id);
let generation = 0;
let controller: AbortController | null = null;
const redact = (value: string) =>
  props.secret ? value.split(props.secret).join("[已隐藏]") : value;
const errorText = (reason: unknown) =>
  redact(
    reason instanceof Error ? reason.message : "记忆保存失败，请稍后重试。"
  );

watch(
  [visible, () => props.draft],
  ([opened]) => {
    generation++;
    controller?.abort();
    saving.value = false;
    reloading.value = false;
    error.value = "";
    conflict.value = false;
    if (opened) {
      form.title = redact(props.draft.title);
      form.content = redact(props.draft.content);
      version.value = props.draft.version;
      tab.value = "edit";
    } else {
      form.title = "";
      form.content = "";
    }
  },
  { immediate: true }
);
watch(
  () => props.owner,
  () => {
    visible.value = false;
  }
);
onBeforeUnmount(() => {
  generation++;
  controller?.abort();
});

async function save() {
  if (!props.writable || saving.value || reloading.value) return;
  if (!form.title.trim() || !form.content.trim()) {
    error.value = "请填写记忆标题和正文。";
    return;
  }
  const current = generation;
  const owner = props.owner;
  saving.value = true;
  error.value = "";
  try {
    const draft = { title: form.title.trim(), content: form.content };
    const memory = props.draft.id
      ? await aiApi.updateMemory(props.draft.id, {
          ...draft,
          version: version.value!
        })
      : await aiApi.createMemory(draft);
    if (
      current !== generation ||
      owner !== props.owner ||
      !visible.value ||
      !props.writable
    )
      return;
    emit("saved", memory);
    visible.value = false;
  } catch (reason) {
    if (current !== generation || owner !== props.owner) return;
    conflict.value = reason instanceof AIRequestError && reason.status === 409;
    error.value = conflict.value
      ? "该记忆已被其他用户修改，当前编辑已保留。可载入最新版本后重新编辑。"
      : errorText(reason);
  } finally {
    if (current === generation) saving.value = false;
  }
}

async function reload() {
  if (!props.draft.id || reloading.value) return;
  const current = generation;
  const owner = props.owner;
  controller?.abort();
  controller = new AbortController();
  const signal = controller.signal;
  reloading.value = true;
  try {
    const memory = await aiApi.memory(props.draft.id, signal);
    if (
      current !== generation ||
      owner !== props.owner ||
      signal.aborted ||
      !visible.value
    )
      return;
    form.title = redact(memory.title);
    form.content = redact(memory.content);
    version.value = memory.version;
    conflict.value = false;
    error.value = "";
  } catch (reason) {
    if (current === generation && !signal.aborted)
      error.value = errorText(reason);
  } finally {
    if (current === generation) reloading.value = false;
  }
}
</script>

<template>
  <el-dialog
    v-model="visible"
    class="global-memory-editor"
    :title="editing ? '编辑全局记忆' : '保存为全局记忆'"
    width="min(760px, 96vw)"
    top="4vh"
    append-to-body
    :close-on-click-modal="false"
    :close-on-press-escape="!saving"
    :show-close="!saving"
  >
    <p class="memory-hint">
      保存后所有登录用户都能查看和引入。正文支持
      Markdown；仅点击保存才写入记忆库。
    </p>
    <p v-if="!writable" class="memory-hint">
      当前账号只读，可编辑和预览草稿；有写权限才能保存到全局记忆库。
    </p>
    <el-alert
      v-if="error"
      :title="error"
      type="error"
      show-icon
      :closable="false"
    />
    <el-form label-position="top" @submit.prevent="save">
      <el-form-item label="标题"
        ><el-input
          v-model="form.title"
          maxlength="200"
          show-word-limit
          :disabled="saving || reloading"
      /></el-form-item>
      <el-tabs v-model="tab">
        <el-tab-pane label="编辑正文" name="edit"
          ><el-input
            v-model="form.content"
            type="textarea"
            :autosize="{ minRows: 10, maxRows: 18 }"
            maxlength="20000"
            show-word-limit
            placeholder="记录有助于后续排查的事实、处理方法和注意事项…"
            :disabled="saving || reloading"
        /></el-tab-pane>
        <el-tab-pane label="预览" name="preview"
          ><div class="memory-preview">
            <AIMarkdown :content="redact(form.content)" /></div
        ></el-tab-pane>
      </el-tabs>
    </el-form>
    <template #footer>
      <div class="memory-editor-actions">
        <el-button
          v-if="conflict"
          :loading="reloading"
          :disabled="saving"
          @click="reload"
          >载入最新版本（覆盖当前编辑）</el-button
        >
        <span class="action-spacer" />
        <el-button :disabled="saving" @click="visible = false">取消</el-button>
        <el-button
          type="primary"
          :loading="saving"
          :disabled="!writable || reloading || conflict"
          @click="save"
          >保存</el-button
        >
      </div>
    </template>
  </el-dialog>
</template>

<style lang="scss">
.global-memory-editor {
  display: flex;
  flex-direction: column;
  max-height: 92dvh;

  .el-dialog__body {
    min-height: 0;
    overflow-y: auto;
  }

  .el-dialog__header,
  .el-dialog__footer {
    flex-shrink: 0;
  }
}
</style>
<style scoped lang="scss">
.memory-hint {
  margin: 0 0 14px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--el-text-color-secondary);
}

.el-alert {
  margin-bottom: 14px;
}

.memory-preview {
  max-height: 54dvh;
  padding: 12px;
  overflow-y: auto;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
}

.memory-editor-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}

.memory-editor-actions .el-button + .el-button {
  margin-left: 0;
}

.action-spacer {
  flex: 1;
}

@media (width <= 760px) {
  .memory-editor-actions {
    justify-content: flex-end;
  }

  .action-spacer {
    display: none;
  }
}
</style>
