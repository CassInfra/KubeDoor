<script setup lang="ts">
// Markdown 文本框：普通 textarea（等宽字体）+ 预览（用 AI 助手的 Markdown 渲染，不允许 HTML）
import { ref } from "vue";
import AIMarkdown from "@/components/AI/AIMarkdown.vue";

defineOptions({ name: "LogMarkdownEditor" });

const model = defineModel<string>({ required: true });

withDefaults(
  defineProps<{
    rows?: number;
    maxlength?: number;
    placeholder?: string;
  }>(),
  { rows: 8, maxlength: undefined, placeholder: "" }
);

const mode = ref<"edit" | "preview">("edit");
</script>

<template>
  <div class="markdown-editor">
    <div class="editor-bar">
      <el-radio-group v-model="mode" size="small">
        <el-radio-button value="edit">编辑</el-radio-button>
        <el-radio-button value="preview">预览</el-radio-button>
      </el-radio-group>
      <span class="editor-tip">支持 Markdown</span>
      <div class="editor-actions"><slot name="actions" /></div>
    </div>
    <el-input
      v-show="mode === 'edit'"
      v-model="model"
      type="textarea"
      :rows="rows"
      :maxlength="maxlength"
      :show-word-limit="!!maxlength"
      :placeholder="placeholder"
      class="markdown-input"
    />
    <div
      v-if="mode === 'preview'"
      class="markdown-preview"
      :style="{ minHeight: `${rows * 20 + 12}px` }"
    >
      <AIMarkdown v-if="model.trim()" :content="model" />
      <span v-else class="preview-empty">（空）</span>
    </div>
  </div>
</template>

<style scoped lang="scss">
.markdown-editor {
  width: 100%;
}

.editor-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  margin-bottom: 6px;
}

.editor-tip {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.editor-actions {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-left: auto;
}

.markdown-input :deep(textarea) {
  font-family: Menlo, Consolas, monospace;
  font-size: 12px;
  line-height: 1.6;
}

.markdown-preview {
  padding: 8px 12px;
  overflow: auto;
  border: 1px solid var(--el-border-color);
  border-radius: 4px;
}

.preview-empty {
  color: var(--el-text-color-secondary);
}
</style>
