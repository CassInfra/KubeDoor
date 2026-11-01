<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from "vue";
import * as monaco from "monaco-editor";

defineOptions({ name: "JsonView" });

/**
 * 只读查看 JSON:黑底、按 JSON 着色,按层级折叠、Ctrl+F 搜索。
 * 获取运行配置的弹窗、「获取运行配置接口」配置页的测试结果都用它
 */
const props = withDefaults(
  defineProps<{
    /** 要显示的 JSON 文本:调用方 JSON.stringify(data, null, 2) 好再传进来,复制时用同一份 */
    value: string;
    height?: string;
    /** 自动换行(长的地址、配置文件内容不用横向滚动) */
    wordWrap?: boolean;
  }>(),
  { height: "420px", wordWrap: true }
);

// 主题和 istio 的 MonacoView 同名同内容(谁先定义都一样)。Monaco 的主题是全局的:
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

// 不用 Monaco 自带的 json:它的折叠、校验、悬浮提示都要 web worker,项目里没配 MonacoEnvironment,会一直报错。
// 这里注册一个只负责着色的语言;折叠按缩进,JSON.stringify 缩进过的文本按缩进折叠就是按层级折叠
const LANGUAGE = "kubedoor-json";
if (!monaco.languages.getLanguages().some(item => item.id === LANGUAGE)) {
  monaco.languages.register({ id: LANGUAGE });
  monaco.languages.setLanguageConfiguration(LANGUAGE, {
    brackets: [
      ["{", "}"],
      ["[", "]"]
    ]
  });
  // token 都带 .json 后缀,直接用上 vs-dark 里 JSON 的配色:键浅蓝、字符串橙色、数字浅绿;
  // true / false / null 用 constant(蓝色),和字符串区分开
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

const container = ref<HTMLElement>();
let editor: monaco.editor.IStandaloneCodeEditor | null = null;

onMounted(() => {
  if (!container.value) return;
  editor = monaco.editor.create(container.value, {
    value: props.value,
    language: LANGUAGE,
    theme: THEME,
    readOnly: true,
    domReadOnly: true,
    automaticLayout: true,
    minimap: { enabled: false },
    scrollBeyondLastLine: false,
    renderLineHighlight: "none",
    fontSize: 13,
    wordWrap: props.wordWrap ? "on" : "off",
    folding: true,
    foldingStrategy: "indentation",
    // 折叠箭头一直显示,不用鼠标移上去才看得到
    showFoldingControls: "always",
    // 往下滚动时把所在的上层键固定在顶部,看得出当前在哪一段
    stickyScroll: { enabled: true, defaultModel: "indentationModel" },
    // 中文的全角标点不算「容易混淆的字符」,不加框;配置值里的零宽空格这类不可见字符照常标出来
    unicodeHighlight: { ambiguousCharacters: false }
  });
});

watch(
  () => props.value,
  value => {
    if (!editor) return;
    editor.setValue(value);
    editor.setScrollPosition({ scrollTop: 0, scrollLeft: 0 });
  }
);

watch(
  () => props.wordWrap,
  wrap => editor?.updateOptions({ wordWrap: wrap ? "on" : "off" })
);

/** 全部展开 */
async function expandAll() {
  await editor?.getAction("editor.unfoldAll")?.run();
}

/**
 * 全部折叠:最外层保持展开,里面每一层都折叠起来,
 * 所以先看到顶层的各项,展开某一项时它下面的各项仍是折叠的,可以一层层往下看
 */
async function collapseAll() {
  if (!editor) return;
  await editor.getAction("editor.foldAll")?.run();
  await editor
    .getAction("editor.unfold")
    ?.run({ selectionLines: [0], levels: 1 });
  editor.setPosition({ lineNumber: 1, column: 1 });
  editor.setScrollPosition({ scrollTop: 0, scrollLeft: 0 });
}

/** 打开搜索框(同在内容里按 Ctrl+F) */
async function find() {
  if (!editor) return;
  editor.focus();
  await editor.getAction("actions.find")?.run();
}

defineExpose({ expandAll, collapseAll, find });

onBeforeUnmount(() => {
  editor?.dispose();
  editor = null;
  monaco.editor.setTheme("vs-dark");
});
</script>

<template>
  <div ref="container" class="json-view" :style="{ height }" />
</template>

<style scoped>
.json-view {
  width: 100%;
  overflow: hidden;
  background: #000;
  border-radius: 4px;
}
</style>
