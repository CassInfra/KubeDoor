<script setup lang="ts">
import {
  computed,
  nextTick,
  onBeforeUnmount,
  reactive,
  ref,
  shallowRef,
  watch,
  type ComponentPublicInstance
} from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { RefreshRight } from "@element-plus/icons-vue";
import * as monaco from "monaco-editor";
import {
  getCoreDns,
  isOldAgent,
  OLD_AGENT_TIP,
  saveCoreDns,
  type CoreDnsConfigMap
} from "@/api/coredns";
import { COREFILE_KEY, validateCorefile } from "../corefile";
import { alertErrors, confirmChange } from "../confirm";
import { BLACK_THEME, languageOf, setupMonaco } from "../monaco";

defineOptions({ name: "CoreDnsAdvancedEditDialog" });

/**
 * 高级编辑:直接编辑 ConfigMap data 里的每个文件。每个文件一个伸缩栏,同时最多展开一个,默认展开 Corefile;
 * 编辑框黑底、按文件类型着色,只在第一次展开时创建
 */
const props = defineProps<{
  modelValue: boolean;
  env: string;
  configmap: CoreDnsConfigMap | null;
  readonly: boolean;
  /** 表格里还没保存的修改数,提示这里编辑的是集群里已保存的内容 */
  pending: number;
}>();
const emit = defineEmits<{
  (e: "update:modelValue", value: boolean): void;
  /** 保存成功 / 重新加载后,把集群里最新的 ConfigMap 交给页面 */
  (e: "saved", value: CoreDnsConfigMap): void;
  (e: "loaded", value: CoreDnsConfigMap): void;
}>();

const visible = computed({
  get: () => props.modelValue,
  set: value => emit("update:modelValue", value)
});

interface FileItem {
  name: string;
  /** 集群里的原始内容(确认框对比用) */
  original: string;
  /** 刚载入编辑框时的内容(Monaco 可能统一了换行符),判断有没有改过用 */
  initial: string;
}

const MIN_EDITOR_HEIGHT = 200;
const base = shallowRef<CoreDnsConfigMap | null>(null);
const files = ref<FileItem[]>([]);
const status = reactive<Record<string, { dirty: boolean; lines: number }>>({});
const active = ref("");
const editorHeight = ref(MIN_EDITOR_HEIGHT);
const filesBox = ref<HTMLElement>();
const saving = ref(false);
const reloading = ref(false);

const models = new Map<string, monaco.editor.ITextModel>();
const editors = new Map<string, monaco.editor.IStandaloneCodeEditor>();
const containers = new Map<string, HTMLElement>();
let observer: ResizeObserver | null = null;

const resourceVersion = computed(
  () => base.value?.metadata?.resourceVersion || ""
);
const dirtyCount = computed(
  () => Object.values(status).filter(item => item.dirty).length
);

// Corefile 排第一,其余按名字排
const order = (a: string, b: string) =>
  a === COREFILE_KEY ? -1 : b === COREFILE_KEY ? 1 : a.localeCompare(b);

function disposeAll() {
  editors.forEach(editor => editor.dispose());
  models.forEach(model => model.dispose());
  editors.clear();
  models.clear();
  for (const key of Object.keys(status)) delete status[key];
}

function load(configmap: CoreDnsConfigMap, keep = "") {
  disposeAll();
  base.value = configmap;
  const data = configmap.data || {};
  const items: FileItem[] = [];
  for (const name of Object.keys(data).sort(order)) {
    const model = monaco.editor.createModel(data[name] ?? "", languageOf(name));
    const item = {
      name,
      original: data[name] ?? "",
      initial: model.getValue()
    };
    models.set(name, model);
    status[name] = { dirty: false, lines: model.getLineCount() };
    model.onDidChangeContent(() => {
      const state = status[name];
      if (!state) return;
      state.dirty = model.getValue() !== item.initial;
      state.lines = model.getLineCount();
    });
    items.push(item);
  }
  files.value = items;
  const names = items.map(item => item.name);
  active.value = names.includes(keep) ? keep : (names[0] ?? "");
}

function setContainer(
  name: string,
  el: Element | ComponentPublicInstance | null
) {
  if (el instanceof HTMLElement) containers.set(name, el);
  else containers.delete(name);
}

function ensureEditor(name: string) {
  if (!name || editors.has(name)) return editors.get(name);
  const el = containers.get(name);
  const model = models.get(name);
  if (!el || !model) return undefined;
  const editor = monaco.editor.create(el, {
    model,
    theme: BLACK_THEME,
    automaticLayout: true,
    minimap: { enabled: false },
    scrollBeyondLastLine: false,
    fontSize: 14,
    readOnly: props.readonly
  });
  editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () => save());
  editors.set(name, editor);
  return editor;
}

/** 展开的编辑框占满剩下的高度:总高度减去所有伸缩栏标题 */
function measure() {
  const box = filesBox.value;
  if (!box) return;
  const headers = Array.from(
    box.querySelectorAll<HTMLElement>(".el-collapse-item__header")
  ).reduce((sum, el) => sum + el.offsetHeight, 0);
  const collapse = box.querySelector<HTMLElement>(".el-collapse");
  const borders = collapse ? collapse.offsetHeight - collapse.clientHeight : 0;
  editorHeight.value = Math.max(
    MIN_EDITOR_HEIGHT,
    Math.floor(box.clientHeight - headers - borders - 2)
  );
}

async function showActive(focus = true) {
  await nextTick();
  measure();
  const editor = ensureEditor(active.value);
  if (focus) editor?.focus();
}

async function open() {
  if (!props.configmap) return;
  setupMonaco();
  load(props.configmap, COREFILE_KEY);
  await nextTick();
  if (filesBox.value && !observer) {
    observer = new ResizeObserver(() => measure());
    observer.observe(filesBox.value);
  }
  await showActive();
}

/** 关闭动画结束后再销毁,免得淡出时编辑框先变空 */
function cleanup() {
  observer?.disconnect();
  observer = null;
  disposeAll();
  files.value = [];
  containers.clear();
  base.value = null;
  monaco.editor.setTheme("vs-dark");
}

watch(
  () => props.modelValue,
  value => {
    if (value) open();
  }
);
watch(active, name => {
  if (name) showActive();
});
onBeforeUnmount(cleanup);

async function confirmDiscard(message: string) {
  if (!dirtyCount.value) return true;
  try {
    await ElMessageBox.confirm(message, "提示", {
      type: "warning",
      confirmButtonText: "放弃修改",
      cancelButtonText: "继续编辑"
    });
    return true;
  } catch {
    return false;
  }
}

async function beforeClose(done: () => void) {
  if (saving.value) return;
  if (await confirmDiscard("还有修改没有保存,确定关闭吗?")) done();
}

const requestClose = () => beforeClose(() => (visible.value = false));

async function reload() {
  if (!(await confirmDiscard("重新加载会丢掉编辑框里的修改,确定吗?"))) return;
  reloading.value = true;
  try {
    const res = await getCoreDns(props.env);
    if (res.success && res.data) {
      load(res.data, active.value);
      await showActive(false);
      emit("loaded", res.data);
      ElMessage.success("已加载集群里最新的配置");
    } else {
      ElMessage.error(
        isOldAgent(res) ? OLD_AGENT_TIP : res.error || "加载失败"
      );
    }
  } finally {
    reloading.value = false;
  }
}

async function save() {
  if (!base.value || props.readonly || saving.value) return;
  const changed = files.value.filter(
    file => models.get(file.name)?.getValue() !== file.initial
  );
  if (!changed.length) {
    ElMessage.info("内容没有变化");
    return;
  }
  const values: Record<string, string> = {};
  for (const file of changed)
    values[file.name] = models.get(file.name)!.getValue();
  const notes: string[] = [];
  const corefile = changed.find(file => file.name === COREFILE_KEY);
  if (corefile) {
    const text = values[COREFILE_KEY];
    if (!text.trim()) {
      active.value = COREFILE_KEY;
      ElMessage.error("Corefile 不能为空");
      return;
    }
    const { errors, warnings } = validateCorefile(text);
    // 原来就有错的不拦(否则没法在这里修),只提示
    if (errors.length && !validateCorefile(corefile.original).errors.length) {
      active.value = COREFILE_KEY;
      alertErrors("Corefile 有错误,不能保存", errors);
      return;
    }
    notes.push(...errors, ...warnings);
  }
  const confirmed = await confirmChange({
    env: props.env,
    title:
      changed.length > 1
        ? `确认保存(${changed.length} 个文件)`
        : `确认保存 ${changed[0].name}`,
    diffs: changed.map(file => ({
      name: file.name,
      before: file.original,
      after: values[file.name]
    })),
    notes
  });
  if (!confirmed) return;
  saving.value = true;
  try {
    const res = await saveCoreDns(
      props.env,
      resourceVersion.value,
      { data: values },
      `高级编辑 ${changed.map(file => file.name).join("、")}`
    );
    if (res.success && res.data) {
      ElMessage.success("已保存,CoreDNS 检测到配置变化后自动生效");
      emit("saved", res.data);
      visible.value = false;
    } else if (res.conflict) {
      ElMessageBox.alert(
        "配置在你打开之后被修改过(别人或别的页面保存过)。请先复制你的修改,点「重新加载」拿到最新内容后再改。",
        "保存冲突",
        { type: "warning" }
      );
    } else {
      ElMessage.error(
        isOldAgent(res) ? OLD_AGENT_TIP : res.error || "保存失败"
      );
    }
  } finally {
    saving.value = false;
  }
}
</script>

<template>
  <el-dialog
    v-model="visible"
    width="90%"
    top="5vh"
    append-to-body
    :close-on-click-modal="false"
    :before-close="beforeClose"
    class="coredns-advanced-dialog"
    @closed="cleanup"
  >
    <template #header>
      <div class="adv-header">
        <span class="adv-header__title">高级编辑</span>
        <span class="adv-header__meta">
          {{ env }} · kube-system/coredns · resourceVersion
          {{ resourceVersion || "-" }}
        </span>
        <el-tag v-if="dirtyCount" size="small" type="warning">
          {{ dirtyCount }} 个文件未保存
        </el-tag>
        <el-tag v-if="readonly" size="small" type="info">只读</el-tag>
      </div>
    </template>

    <div class="adv-body">
      <el-alert
        v-if="pending"
        type="info"
        show-icon
        :closable="false"
        class="adv-alert"
        :title="`表格里还有 ${pending} 处修改没有保存:这里编辑的是集群里已保存的内容,在这里保存后会尽量保留表格里的修改`"
      />
      <div ref="filesBox" class="adv-files">
        <el-empty
          v-if="!files.length"
          description="ConfigMap coredns 里没有 data"
        />
        <el-collapse v-else v-model="active" accordion class="adv-collapse">
          <el-collapse-item
            v-for="file in files"
            :key="file.name"
            :name="file.name"
          >
            <template #title>
              <div class="file-title">
                <span class="file-title__name">{{ file.name }}</span>
                <span class="file-title__meta">
                  {{ status[file.name]?.lines ?? 0 }} 行
                </span>
                <el-tag
                  v-if="status[file.name]?.dirty"
                  size="small"
                  type="warning"
                  disable-transitions
                  >已修改</el-tag
                >
              </div>
            </template>
            <div
              :ref="el => setContainer(file.name, el)"
              class="file-editor"
              :style="{ height: `${editorHeight}px` }"
            />
          </el-collapse-item>
        </el-collapse>
      </div>
    </div>

    <template #footer>
      <div class="adv-footer">
        <span class="adv-footer__tip">
          直接编辑 ConfigMap 里的文件;Ctrl+S 保存,Ctrl+/
          注释或取消注释选中的行。保存前会检查 Corefile
          并展示改动,同时确认打开之后没有被别人改过
        </span>
        <el-button :icon="RefreshRight" :loading="reloading" @click="reload"
          >重新加载</el-button
        >
        <el-button @click="requestClose">关闭</el-button>
        <el-button
          type="primary"
          :loading="saving"
          :disabled="readonly || !dirtyCount"
          @click="save"
        >
          保存
        </el-button>
      </div>
    </template>
  </el-dialog>
</template>

<style lang="scss">
.coredns-advanced-dialog {
  display: flex;
  flex-direction: column;
  height: 90vh;
  margin-bottom: 0;

  .el-dialog__header,
  .el-dialog__footer {
    flex-shrink: 0;
  }

  .el-dialog__body {
    flex: 1;
    min-height: 0;
    padding-top: 4px;
    padding-bottom: 4px;
  }
}
</style>

<style scoped>
.adv-header {
  display: flex;
  gap: 10px;
  align-items: center;
}

.adv-header__title {
  font-size: 16px;
  font-weight: 600;
}

.adv-header__meta {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.adv-body {
  display: flex;
  flex-direction: column;
  height: 100%;
}

.adv-alert {
  flex: none;
  margin-bottom: 8px;
}

.adv-files {
  flex: 1;
  min-height: 0;
  overflow: hidden auto;
}

.adv-collapse {
  --el-collapse-header-height: 42px;
}

.adv-collapse :deep(.el-collapse-item__header) {
  padding: 0 12px;
}

.adv-collapse :deep(.el-collapse-item__content) {
  padding: 0;
}

.file-title {
  display: flex;
  gap: 10px;
  align-items: center;
}

.file-title__name {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 14px;
  font-weight: 600;
}

.file-title__meta {
  font-size: 12px;
  font-weight: normal;
  color: var(--el-text-color-secondary);
}

.file-editor {
  width: 100%;
  overflow: hidden;
  background: #000;
}

.adv-footer {
  display: flex;
  gap: 8px;
  align-items: center;
  justify-content: flex-end;
}

.adv-footer__tip {
  margin-right: auto;
  font-size: 12px;
  color: var(--el-text-color-secondary);
  text-align: left;
}

.adv-footer .el-button + .el-button {
  margin-left: 0;
}
</style>
