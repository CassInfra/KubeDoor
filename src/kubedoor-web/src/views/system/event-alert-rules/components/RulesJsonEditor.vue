<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from "vue";
import * as monaco from "monaco-editor";
import type { MarkerRange } from "../rules-utils";

defineOptions({ name: "EventRulesJsonEditor" });

/**
 * 事件告警规则的 JSON 编辑框:黑底、按 JSON 着色、按缩进折叠,Ctrl+S 保存。
 * 语法错误和不符合规则的地方由页面算好位置(markers)传进来,用 setModelMarkers 标在出错的行上
 */
const props = withDefaults(
  defineProps<{
    modelValue: string;
    /** 语法错误(红色) */
    errors?: MarkerRange[];
    /** 不符合规则的地方(黄色) */
    warnings?: MarkerRange[];
    readonly?: boolean;
    height?: string;
  }>(),
  { errors: () => [], warnings: () => [], readonly: false, height: "62vh" }
);
const emit = defineEmits<{
  (e: "update:modelValue", value: string): void;
  (e: "save"): void;
}>();

// 主题、语言和 src/components/JsonView.vue 同名同内容(谁先定义都一样)。Monaco 的主题是全局的:
// 创建时切到黑底,销毁时换回其他编辑器(对比、YAML 编辑)用的 vs-dark
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

// 不用 Monaco 自带的 json:它的折叠、校验、悬浮提示都要 web worker,项目里没配 MonacoEnvironment
const LANGUAGE = "kubedoor-json";
if (!monaco.languages.getLanguages().some(item => item.id === LANGUAGE)) {
  monaco.languages.register({ id: LANGUAGE });
  monaco.languages.setLanguageConfiguration(LANGUAGE, {
    brackets: [
      ["{", "}"],
      ["[", "]"]
    ]
  });
  monaco.languages.setMonarchTokensProvider(LANGUAGE, {
    tokenPostfix: ".json",
    brackets: [
      { open: "{", close: "}", token: "delimiter.bracket" },
      { open: "[", close: "]", token: "delimiter.array" }
    ],
    tokenizer: {
      root: [
        [/\s+/, "white"],
        [/"(?:[^"\\]|\\.)*"(?=\s*:)/, "string.key"],
        [/"(?:[^"\\]|\\.)*"/, "string.value"],
        [/-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/, "number"],
        [/\b(?:true|false|null)\b/, "constant"],
        [/[{}[\]]/, "@brackets"],
        [/[,:]/, "delimiter"]
      ]
    }
  });
}

const OWNER = "event-alert-rules";
// 输入时自动补全成对的括号和引号(只读的 JsonView 用不到,这里补上;全局只设一次)
let editingConfigured = false;
function configureEditing() {
  if (editingConfigured) return;
  editingConfigured = true;
  monaco.languages.setLanguageConfiguration(LANGUAGE, {
    brackets: [
      ["{", "}"],
      ["[", "]"]
    ],
    autoClosingPairs: [
      { open: "{", close: "}" },
      { open: "[", close: "]" },
      { open: '"', close: '"', notIn: ["string"] }
    ],
    surroundingPairs: [
      { open: "{", close: "}" },
      { open: "[", close: "]" },
      { open: '"', close: '"' }
    ]
  });
}

const container = ref<HTMLElement>();
let editor: monaco.editor.IStandaloneCodeEditor | null = null;
let model: monaco.editor.ITextModel | null = null;

function toMarkers(
  items: MarkerRange[],
  severity: monaco.MarkerSeverity
): monaco.editor.IMarkerData[] {
  return items.map(item => ({ ...item, severity }));
}

function applyMarkers() {
  if (!model) return;
  monaco.editor.setModelMarkers(model, OWNER, [
    ...toMarkers(props.errors, monaco.MarkerSeverity.Error),
    ...toMarkers(props.warnings, monaco.MarkerSeverity.Warning)
  ]);
}

onMounted(() => {
  if (!container.value) return;
  configureEditing();
  model = monaco.editor.createModel(props.modelValue, LANGUAGE);
  model.updateOptions({ tabSize: 2, insertSpaces: true });
  model.onDidChangeContent(() => {
    const value = model?.getValue() ?? "";
    if (value !== props.modelValue) emit("update:modelValue", value);
  });
  editor = monaco.editor.create(container.value, {
    model,
    theme: THEME,
    readOnly: props.readonly,
    automaticLayout: true,
    minimap: { enabled: false },
    scrollBeyondLastLine: false,
    fontSize: 13,
    wordWrap: "on",
    folding: true,
    foldingStrategy: "indentation",
    showFoldingControls: "always",
    stickyScroll: { enabled: true, defaultModel: "indentationModel" },
    unicodeHighlight: { ambiguousCharacters: false },
    fixedOverflowWidgets: true
  });
  editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () =>
    emit("save")
  );
  applyMarkers();
});

// 页面换了内容(切换模式、恢复默认、重新加载):整体替换,能用 Ctrl+Z 撤回。
// 用 model 的编辑而不是 editor.executeEdits:编辑框只读(保存、加载中)时 executeEdits 会被忽略
watch(
  () => props.modelValue,
  value => {
    if (!model || model.getValue() === value) return;
    model.pushStackElement();
    model.pushEditOperations(
      [],
      [{ range: model.getFullModelRange(), text: value }],
      () => null
    );
    model.pushStackElement();
  }
);
watch(() => [props.errors, props.warnings], applyMarkers);
watch(
  () => props.readonly,
  value => editor?.updateOptions({ readOnly: value })
);

/** 跳到某一行并选中出错的地方 */
function reveal(line: number, column = 1) {
  if (!editor) return;
  editor.revealLineInCenter(line);
  editor.setPosition({ lineNumber: line, column });
  editor.focus();
}

defineExpose({ reveal });

onBeforeUnmount(() => {
  editor?.dispose();
  model?.dispose();
  editor = null;
  model = null;
  monaco.editor.setTheme("vs-dark");
});
</script>

<template>
  <div ref="container" class="rules-json-editor" :style="{ height }" />
</template>

<style scoped>
.rules-json-editor {
  width: 100%;
  overflow: hidden;
  background: #000;
  border-radius: 4px;
}
</style>
