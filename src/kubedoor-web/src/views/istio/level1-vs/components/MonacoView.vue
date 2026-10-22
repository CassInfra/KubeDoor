<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from "vue";
import * as monaco from "monaco-editor";

defineOptions({ name: "IstioMonacoView" });

/** 只读查看 YAML:黑底、按 YAML 着色,可以折叠、Ctrl+F 搜索 */
const props = withDefaults(
  defineProps<{
    value: string;
    height?: string;
  }>(),
  { height: "420px" }
);

// Monaco 的主题是全局的:创建时切到黑底,销毁时换回其他编辑器(对比、YAML 编辑)用的 vs-dark
const THEME = "kubedoor-black";
monaco.editor.defineTheme(THEME, {
  base: "vs-dark",
  inherit: true,
  rules: [],
  colors: {
    "editor.background": "#000000",
    "editorGutter.background": "#000000"
  }
});

const container = ref<HTMLElement>();
let editor: monaco.editor.IStandaloneCodeEditor | null = null;

onMounted(() => {
  if (!container.value) return;
  editor = monaco.editor.create(container.value, {
    value: props.value,
    language: "yaml",
    theme: THEME,
    readOnly: true,
    domReadOnly: true,
    automaticLayout: true,
    minimap: { enabled: false },
    scrollBeyondLastLine: false,
    renderLineHighlight: "none",
    fontSize: 13
  });
});

watch(
  () => props.value,
  value => editor?.setValue(value)
);

onBeforeUnmount(() => {
  editor?.dispose();
  editor = null;
  monaco.editor.setTheme("vs-dark");
});
</script>

<template>
  <div ref="container" class="istio-monaco-view" :style="{ height }" />
</template>

<style scoped>
.istio-monaco-view {
  width: 100%;
  overflow: hidden;
  border-radius: 4px;
}
</style>
