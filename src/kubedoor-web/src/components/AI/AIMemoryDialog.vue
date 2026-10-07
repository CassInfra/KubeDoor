<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { ElMessageBox } from "element-plus";
import {
  aiApi,
  AIRequestError,
  type AIMemory,
  type AIMemoryDraft,
  type AIMemorySummary
} from "@/api/ai";
import AIMarkdown from "./AIMarkdown.vue";
import AIMemoryEditor from "./AIMemoryEditor.vue";

const visible = defineModel<boolean>({ default: false });
const props = defineProps<{
  selected: AIMemorySummary[];
  writable: boolean;
  busy: boolean;
  owner: string;
  secret?: string;
}>();
const emit = defineEmits<{
  select: [memories: AIMemorySummary[]];
  deleted: [id: string];
}>();
const rows = ref<AIMemorySummary[]>([]);
const picked = ref(new Map<string, AIMemorySummary>());
const selectedItems = computed(() => [...picked.value.values()]);
const search = ref("");
const filter = ref("");
const page = ref(1);
const pageSize = 20;
const total = ref(0);
const listLoading = ref(false);
const detailLoading = ref(false);
const detail = ref<AIMemory | null>(null);
const detailId = ref("");
const deleting = ref("");
const error = ref("");
const editorVisible = ref(false);
const editorDraft = ref<AIMemoryDraft & { id?: string; version?: number }>({
  title: "",
  content: ""
});
let listGeneration = 0;
let detailGeneration = 0;
let managerGeneration = 0;
let listController: AbortController | null = null;
let detailController: AbortController | null = null;
const redact = (text: string) =>
  props.secret ? text.split(props.secret).join("[已隐藏]") : text;
const errorText = (reason: unknown) =>
  redact(
    reason instanceof Error ? reason.message : "记忆请求失败，请稍后重试。"
  );
function summary(memory: AIMemory): AIMemorySummary {
  const { content: _content, ...metadata } = memory;
  return metadata;
}
function stopRequests() {
  listGeneration++;
  detailGeneration++;
  listController?.abort();
  detailController?.abort();
  listLoading.value = false;
  detailLoading.value = false;
}
watch(
  visible,
  opened => {
    managerGeneration++;
    stopRequests();
    rows.value = [];
    detail.value = null;
    detailId.value = "";
    error.value = "";
    editorVisible.value = false;
    deleting.value = "";
    if (opened) {
      picked.value = new Map(
        props.selected.map(memory => [memory.id, { ...memory }])
      );
      page.value = 1;
      void loadList();
    } else picked.value.clear();
  },
  { immediate: true }
);
watch(
  () => props.owner,
  () => {
    visible.value = false;
    search.value = "";
    filter.value = "";
    stopRequests();
  }
);
onBeforeUnmount(() => {
  managerGeneration++;
  stopRequests();
});

async function loadList() {
  const generation = ++listGeneration;
  const owner = props.owner;
  listController?.abort();
  const controller = new AbortController();
  listController = controller;
  listLoading.value = true;
  error.value = "";
  try {
    const data = await aiApi.listMemories(
      { search: filter.value, page: page.value, page_size: pageSize },
      controller.signal
    );
    if (
      generation !== listGeneration ||
      controller.signal.aborted ||
      owner !== props.owner ||
      !visible.value
    )
      return;
    rows.value = data.memories || [];
    total.value = data.total || 0;
    for (const row of rows.value)
      if (picked.value.has(row.id)) picked.value.set(row.id, row);
  } catch (reason) {
    if (
      generation === listGeneration &&
      !controller.signal.aborted &&
      owner === props.owner
    )
      error.value = errorText(reason);
  } finally {
    if (generation === listGeneration) listLoading.value = false;
  }
}
function searchMemories() {
  filter.value = search.value.trim();
  page.value = 1;
  void loadList();
}
function changePage(value: number) {
  page.value = value;
  void loadList();
}
function toggle(row: AIMemorySummary, selected: boolean) {
  if (props.busy) return;
  if (selected && picked.value.size >= 10 && !picked.value.has(row.id)) {
    error.value = "每轮最多引入 10 条记忆。";
    return;
  }
  if (selected) picked.value.set(row.id, row);
  else picked.value.delete(row.id);
}
function applySelection() {
  if (props.busy) return;
  emit("select", selectedItems.value);
  visible.value = false;
}
async function readMemory(row: AIMemorySummary, edit = false) {
  if (!edit && detailId.value === row.id) {
    detailController?.abort();
    detailGeneration++;
    detailId.value = "";
    detail.value = null;
    detailLoading.value = false;
    return;
  }
  const generation = ++detailGeneration;
  const owner = props.owner;
  detailController?.abort();
  const controller = new AbortController();
  detailController = controller;
  detailId.value = row.id;
  detail.value = null;
  detailLoading.value = true;
  error.value = "";
  try {
    const memory = await aiApi.memory(row.id, controller.signal);
    if (
      generation !== detailGeneration ||
      controller.signal.aborted ||
      owner !== props.owner ||
      !visible.value
    )
      return;
    detail.value = memory;
    if (edit && props.writable) {
      editorDraft.value = memory;
      editorVisible.value = true;
    }
  } catch (reason) {
    if (
      generation === detailGeneration &&
      !controller.signal.aborted &&
      owner === props.owner
    )
      error.value = errorText(reason);
  } finally {
    if (generation === detailGeneration) detailLoading.value = false;
  }
}
function create() {
  if (props.writable) {
    editorDraft.value = { title: "", content: "" };
    editorVisible.value = true;
  }
}
function saved(memory: AIMemory) {
  if (!visible.value) return;
  if (picked.value.has(memory.id)) picked.value.set(memory.id, summary(memory));
  if (detailId.value === memory.id) detail.value = memory;
  void loadList();
}
async function remove(row: AIMemorySummary) {
  if (!props.writable || deleting.value) return;
  const owner = props.owner;
  const generation = listGeneration;
  const context = managerGeneration;
  try {
    await ElMessageBox.confirm(
      `删除全局记忆“${redact(row.title)}”？其他用户也将无法再引入，已发送消息中的快照会保留。`,
      "删除全局记忆",
      { type: "warning" }
    );
    if (
      owner !== props.owner ||
      !visible.value ||
      generation !== listGeneration ||
      !props.writable
    )
      return;
    deleting.value = row.id;
    await aiApi.deleteMemory(row.id, row.version);
    if (
      owner !== props.owner ||
      !visible.value ||
      context !== managerGeneration
    )
      return;
    picked.value.delete(row.id);
    emit("deleted", row.id);
    if (detailId.value === row.id) {
      detailGeneration++;
      detailController?.abort();
      detailLoading.value = false;
      detail.value = null;
      detailId.value = "";
    }
    if (rows.value.length === 1 && page.value > 1) page.value--;
    await loadList();
  } catch (reason) {
    if (
      reason === "cancel" ||
      reason === "close" ||
      owner !== props.owner ||
      !visible.value ||
      context !== managerGeneration
    )
      return;
    error.value =
      reason instanceof AIRequestError && reason.status === 409
        ? "该记忆已更新，删除未执行。请刷新列表后查看最新版本。"
        : errorText(reason);
  } finally {
    if (owner === props.owner && context === managerGeneration)
      deleting.value = "";
  }
}
const updated = (memory: AIMemorySummary) =>
  new Date(memory.updated_at).toLocaleString();
</script>

<template>
  <el-dialog
    v-model="visible"
    class="global-memory-dialog"
    title="全局记忆"
    width="min(980px, 96vw)"
    top="3vh"
    append-to-body
    :close-on-click-modal="false"
  >
    <div class="memory-manager-body">
      <p class="memory-hint">
        所有登录用户可查看和引入；有写权限的用户可维护。选中的记忆只带入下一轮，发送成功后清空选择。
      </p>
      <el-alert
        v-if="error"
        :title="error"
        type="error"
        show-icon
        closable
        @close="error = ''"
      />
      <div class="memory-search">
        <el-input
          v-model="search"
          clearable
          placeholder="搜索记忆标题"
          @keyup.enter="searchMemories"
          @clear="searchMemories"
        /><el-button @click="searchMemories">搜索</el-button
        ><el-button v-if="writable" type="primary" @click="create"
          >新增记忆</el-button
        >
      </div>
      <div v-if="selectedItems.length" class="memory-selected">
        <span>已选 {{ selectedItems.length }}/10：</span
        ><el-tag
          v-for="memory in selectedItems"
          :key="memory.id"
          :closable="!busy"
          @close="toggle(memory, false)"
          >{{ redact(memory.title) }}</el-tag
        >
      </div>
      <div
        v-loading="listLoading"
        class="memory-list"
        role="list"
        aria-label="全局记忆列表"
      >
        <div
          v-for="memory in rows"
          :key="memory.id"
          class="memory-row"
          role="listitem"
        >
          <el-checkbox
            :model-value="picked.has(memory.id)"
            :disabled="busy"
            :aria-label="`引入 ${redact(memory.title)}`"
            @change="value => toggle(memory, !!value)"
          />
          <div class="memory-row-info">
            <button
              class="memory-title"
              :title="redact(memory.title)"
              @click="readMemory(memory)"
            >
              {{ redact(memory.title) }}</button
            ><small
              >v{{ memory.version }} · {{ memory.updated_by }} ·
              {{ updated(memory) }}</small
            >
          </div>
          <div v-if="writable" class="memory-row-actions">
            <el-button
              link
              :disabled="!!deleting"
              @click="readMemory(memory, true)"
              >编辑</el-button
            ><el-button
              link
              type="danger"
              :loading="deleting === memory.id"
              :disabled="!!deleting && deleting !== memory.id"
              @click="remove(memory)"
              >删除</el-button
            >
          </div>
        </div>
        <el-empty
          v-if="!listLoading && !rows.length"
          description="暂无匹配的记忆"
          :image-size="54"
        />
      </div>
      <el-pagination
        :current-page="page"
        :page-size="pageSize"
        :total="total"
        layout="prev, pager, next, total"
        :pager-count="5"
        small
        @current-change="changePage"
      />
      <section
        v-if="detailId"
        v-loading="detailLoading"
        class="memory-detail"
        aria-label="记忆正文"
      >
        <template v-if="detail"
          ><div class="memory-detail-heading">
            <strong>{{ redact(detail.title) }}</strong
            ><span>v{{ detail.version }}</span>
          </div>
          <AIMarkdown :content="redact(detail.content)" /></template
        ><span v-else class="memory-hint">正在加载正文…</span>
      </section>
    </div>
    <template #footer
      ><div class="memory-dialog-actions">
        <span v-if="busy" class="memory-hint"
          >会话正在执行或核验，完成后可以引入。</span
        ><el-button @click="visible = false">取消</el-button
        ><el-button type="primary" :disabled="busy" @click="applySelection"
          >引入下一轮（{{ selectedItems.length }}）</el-button
        >
      </div></template
    >
    <AIMemoryEditor
      v-model="editorVisible"
      :draft="editorDraft"
      :writable="writable"
      :owner="owner"
      :secret="secret"
      @saved="saved"
    />
  </el-dialog>
</template>

<style lang="scss">
.global-memory-dialog {
  display: flex;
  flex-direction: column;
  max-height: 94dvh;

  .el-dialog__body {
    min-height: 0;
    overflow: hidden;
  }

  .el-dialog__header,
  .el-dialog__footer {
    flex-shrink: 0;
  }
}
</style>
<style scoped lang="scss">
.memory-manager-body {
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-height: 0;
  max-height: 70dvh;
}

.memory-hint {
  margin: 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--el-text-color-secondary);
}

.memory-search {
  display: flex;
  gap: 8px;
  align-items: center;
}

.memory-search .el-button + .el-button {
  margin-left: 0;
}

.memory-selected {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
  font-size: 12px;
}

.memory-selected :deep(.el-tag__content) {
  max-width: 200px;
  overflow: hidden;
  text-overflow: ellipsis;
}

.memory-list {
  min-height: 80px;
  max-height: 36dvh;
  overflow-y: auto;
}

.memory-row {
  display: flex;
  gap: 10px;
  align-items: center;
  padding: 12px 0;
  border-bottom: 1px solid var(--el-border-color-lighter);
}

.memory-row-info {
  display: flex;
  flex: 1;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
}

.memory-row-info small {
  font-size: 11px;
  color: var(--el-text-color-secondary);
}

.memory-title {
  overflow: hidden;
  font-size: 14px;
  font-weight: 600;
  color: var(--el-color-primary);
  text-align: left;
  text-overflow: ellipsis;
  white-space: nowrap;
  cursor: pointer;
  background: none;
  border: 0;
}

.memory-row-actions {
  display: flex;
  flex-shrink: 0;
  gap: 8px;
}

.memory-row-actions .el-button + .el-button {
  margin-left: 0;
}

.memory-detail {
  min-height: 50px;
  max-height: 28dvh;
  padding: 12px;
  overflow-y: auto;
  background: var(--el-fill-color-lighter);
  border-radius: 6px;
}

.memory-detail-heading {
  display: flex;
  gap: 10px;
  align-items: center;
  margin-bottom: 12px;
  font-size: 13px;
}

.memory-dialog-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  justify-content: flex-end;
}

.memory-dialog-actions .el-button + .el-button {
  margin-left: 0;
}

.memory-dialog-actions > span {
  flex: 1;
  text-align: left;
}

@media (width <= 760px) {
  .memory-search {
    flex-wrap: wrap;
  }

  .memory-search .el-input {
    width: 100%;
  }

  .memory-selected :deep(.el-tag__content) {
    max-width: 150px;
  }

  .memory-row-info small {
    font-size: 10px;
  }

  .memory-row {
    gap: 6px;
  }

  .memory-detail-heading {
    align-items: flex-start;
  }
}
</style>
