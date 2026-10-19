<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from "vue";
import * as monaco from "monaco-editor";

defineOptions({ name: "IstioMonacoDiff" });

/** 左右对比 YAML(只读);original 在左,modified 在右 */
const props = withDefaults(
  defineProps<{
    original: string;
    modified: string;
    height?: string;
  }>(),
  { height: "420px" }
);

const container = ref<HTMLElement>();
let editor: monaco.editor.IStandaloneDiffEditor | null = null;
let models: monaco.editor.ITextModel[] = [];

function setModels() {
  if (!editor) return;
  const original = monaco.editor.createModel(props.original || "", "yaml");
  const modified = monaco.editor.createModel(props.modified || "", "yaml");
  editor.setModel({ original, modified });
  models.forEach(model => model.dispose());
  models = [original, modified];
}

onMounted(() => {
  if (!container.value) return;
  editor = monaco.editor.createDiffEditor(container.value, {
    readOnly: true,
    originalEditable: false,
    automaticLayout: true,
    renderSideBySide: true,
    minimap: { enabled: false },
    scrollBeyondLastLine: false,
    fontSize: 13,
    theme: "vs-dark",
    // 规则多的 VS(上百条)只看改动附近,相同的部分折叠,点开可展开
    hideUnchangedRegions: { enabled: true, contextLineCount: 4 }
  });
  setModels();
});

watch(() => [props.original, props.modified], setModels);

onBeforeUnmount(() => {
  editor?.dispose();
  models.forEach(model => model.dispose());
  editor = null;
  models = [];
});
</script>

<template>
  <div ref="container" class="istio-monaco-diff" :style="{ height }" />
</template>

<style scoped>
.istio-monaco-diff {
  width: 100%;
  overflow: hidden;
  border: 1px solid var(--el-border-color);
  border-radius: 4px;
}
</style>
