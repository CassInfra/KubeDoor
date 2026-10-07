<script setup lang="ts">
import { computed } from "vue";
import { renderAIMarkdown } from "@/utils/aiMarkdown";

const props = withDefaults(defineProps<{ content: string }>(), { content: "" });
const rendered = computed(() => renderAIMarkdown(props.content));
</script>

<template>
  <div class="ai-markdown" v-html="rendered" />
</template>

<style scoped lang="scss">
.ai-markdown {
  min-width: 0;
  font-size: 14px;
  line-height: 1.7;
  color: var(--el-text-color-primary);
  overflow-wrap: anywhere;
  white-space: normal;

  :deep(> :first-child) {
    margin-top: 0;
  }

  :deep(> :last-child) {
    margin-bottom: 0;
  }

  :deep(p) {
    margin: 0 0 12px;
  }

  :deep(h1),
  :deep(h2),
  :deep(h3),
  :deep(h4),
  :deep(h5),
  :deep(h6) {
    margin: 20px 0 10px;
    font-weight: 600;
    line-height: 1.4;
  }

  :deep(h1) {
    font-size: 1.5em;
  }

  :deep(h2) {
    font-size: 1.3em;
  }

  :deep(h3) {
    font-size: 1.15em;
  }

  :deep(h4),
  :deep(h5),
  :deep(h6) {
    font-size: 1em;
  }

  :deep(ul),
  :deep(ol) {
    padding-left: 1.5em;
    margin: 0 0 12px;
  }

  :deep(ul) {
    list-style: disc;
  }

  :deep(ol) {
    list-style: decimal;
  }

  :deep(li + li) {
    margin-top: 4px;
  }

  :deep(li > p) {
    margin-bottom: 6px;
  }

  :deep(a) {
    color: var(--el-color-primary);
    text-decoration: underline;
    text-underline-offset: 3px;
  }

  :deep(a:focus-visible),
  :deep(.ai-markdown-table:focus-visible) {
    outline: 2px solid var(--el-color-primary);
    outline-offset: 2px;
  }

  :deep(code) {
    padding: 2px 5px;
    font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
    font-size: 0.9em;
    background: var(--el-fill-color);
    border-radius: 4px;
  }

  :deep(pre) {
    max-width: 100%;
    padding: 12px;
    margin: 0 0 12px;
    overflow: auto;
    line-height: 1.5;
    white-space: pre;
    background: var(--el-fill-color-light);
    border: 1px solid var(--el-border-color-lighter);
    border-radius: 6px;
  }

  :deep(pre code) {
    padding: 0;
    font-size: 12px;
    overflow-wrap: normal;
    background: transparent;
    border-radius: 0;
  }

  :deep(blockquote) {
    padding: 2px 12px;
    margin: 0 0 12px;
    color: var(--el-text-color-secondary);
    border-left: 3px solid var(--el-border-color);
  }

  :deep(blockquote > :last-child) {
    margin-bottom: 0;
  }

  :deep(hr) {
    margin: 16px 0;
    border: 0;
    border-top: 1px solid var(--el-border-color);
  }

  :deep(.ai-markdown-table) {
    max-width: 100%;
    margin-bottom: 12px;
    overflow-x: auto;
    border: 1px solid var(--el-border-color-lighter);
    border-radius: 6px;
  }

  :deep(table) {
    width: 100%;
    border-spacing: 0;
    border-collapse: collapse;
  }

  :deep(th),
  :deep(td) {
    min-width: 6em;
    padding: 8px 12px;
    text-align: left;
    vertical-align: top;
    border-bottom: 1px solid var(--el-border-color-lighter);
  }

  :deep(th) {
    font-weight: 600;
    white-space: nowrap;
    background: var(--el-fill-color-light);
  }

  :deep(tbody tr:last-child td) {
    border-bottom: 0;
  }
}
</style>
