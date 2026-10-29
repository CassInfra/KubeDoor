<script setup lang="ts">
import { computed, reactive, ref, watch } from "vue";
import { ElMessageBox } from "element-plus";
import {
  ArrowDown,
  ArrowUp,
  Delete,
  Plus,
  View
} from "@element-plus/icons-vue";
import { message } from "@/utils/message";
import {
  type EnvType,
  type GroupPreview,
  type IstioGroup,
  type VSPattern,
  createIstioGroup,
  deleteIstioGroup,
  previewIstioGroup,
  updateIstioGroup
} from "@/api/istio";
import PatternList from "./PatternList.vue";
import { ENV_TYPE_LABEL, conflictText, scopeText } from "../utils";
import { useIstioPermission } from "../useIstioPermission";

defineOptions({ name: "IstioGroupDialog" });

const props = defineProps<{
  groups: IstioGroup[];
  /** 能看到的集群,以及各自已在哪些组里;can_istio = 当前账号在这个集群有「Istio 修改」权限 */
  clusters: { k8s_name: string; groups: string[]; can_istio?: boolean }[];
  /** 打开时选中的组 */
  current?: number | null;
}>();
const emit = defineEmits<{ (e: "saved", groupId: number | null): void }>();
const visible = defineModel<boolean>({ default: false });

interface MemberForm {
  k8s_name: string;
  env_type: EnvType;
  remark: string;
}

const selected = ref<number | null>(null); // null = 新建
const form = reactive({
  name: "",
  description: "",
  clusters: [] as MemberForm[],
  include_patterns: [] as VSPattern[],
  ignore_patterns: [] as VSPattern[]
});
const adding = ref<string[]>([]);
const saving = ref(false);
const preview = ref<GroupPreview | null>(null);
const previewing = ref(false);
const previewStale = ref(false);
const previewFilter = ref<"all" | "visible" | "ignored" | "conflict">("all");

const selectedGroup = computed(() =>
  props.groups.find(group => group.id === selected.value)
);

const { clusterCan, groupCan } = useIstioPermission();
/** 新建组随时可以(只能加有权限的集群);已有的组要组内每个集群都有「Istio 修改」权限才能改、删 */
const editable = computed(
  () => selected.value === null || groupCan(selectedGroup.value)
);

/** 可加入的集群:还不在本组的(一个集群可以属于多个组,如共用的测试集群) */
const addable = computed(() => {
  const inForm = new Set(form.clusters.map(c => c.k8s_name));
  const own = selectedGroup.value?.name;
  return props.clusters
    .filter(c => !inForm.has(c.k8s_name))
    .map(c => ({
      k8s_name: c.k8s_name,
      others: c.groups.filter(g => g !== own),
      allowed: clusterCan(c)
    }));
});

function copyPatterns(items: VSPattern[] | undefined) {
  return (items ?? []).map(item => ({ ...item }));
}

function load(groupId: number | null) {
  selected.value = groupId;
  const group = props.groups.find(g => g.id === groupId);
  form.name = group?.name ?? "";
  form.description = group?.description ?? "";
  form.clusters = (group?.clusters ?? []).map(c => ({
    k8s_name: c.k8s_name,
    env_type: c.env_type,
    remark: c.remark
  }));
  form.include_patterns = copyPatterns(group?.include_patterns);
  form.ignore_patterns = copyPatterns(group?.ignore_patterns);
  adding.value = [];
  preview.value = null;
  previewStale.value = false;
}

watch(
  visible,
  open => {
    if (open) load(props.current ?? props.groups[0]?.id ?? null);
  },
  { immediate: true }
);

// 规则或集群改了,之前的预览就过时了
watch(
  () => [form.clusters, form.include_patterns, form.ignore_patterns],
  () => {
    if (preview.value) previewStale.value = true;
  },
  { deep: true }
);

function addClusters() {
  for (const name of adding.value) {
    const guess: EnvType = /beta|test|dev|uat|sit|stg|staging/i.test(name)
      ? "test"
      : "prod";
    form.clusters.push({ k8s_name: name, env_type: guess, remark: "" });
  }
  adding.value = [];
}

function move(index: number, delta: number) {
  const target = index + delta;
  if (target < 0 || target >= form.clusters.length) return;
  const [item] = form.clusters.splice(index, 1);
  form.clusters.splice(target, 0, item);
}

/** 去掉没填的规则 */
function cleanPatterns(items: VSPattern[]) {
  return items
    .map(item => ({ type: item.type, pattern: item.pattern.trim() }))
    .filter(item => item.pattern);
}

async function runPreview() {
  if (!form.clusters.length) {
    message("先加入集群", { type: "warning" });
    return;
  }
  previewing.value = true;
  try {
    const res = await previewIstioGroup({
      group_id: selected.value,
      clusters: form.clusters.map(c => c.k8s_name),
      include_patterns: cleanPatterns(form.include_patterns),
      ignore_patterns: cleanPatterns(form.ignore_patterns)
    });
    preview.value = res.data;
    previewStale.value = false;
  } finally {
    previewing.value = false;
  }
}

const previewRows = computed(() => {
  const items = preview.value?.items ?? [];
  if (previewFilter.value === "visible") return items.filter(i => i.visible);
  if (previewFilter.value === "ignored") return items.filter(i => !i.visible);
  if (previewFilter.value === "conflict")
    return items.filter(i => i.conflicts.length);
  return items;
});

async function save() {
  if (!form.name.trim()) {
    message("请填写组名", { type: "warning" });
    return;
  }
  const removed = (selectedGroup.value?.clusters ?? [])
    .map(c => c.k8s_name)
    .filter(name => !form.clusters.some(c => c.k8s_name === name));
  if (removed.length) {
    await ElMessageBox.confirm(
      `移出集群 ${removed.join("、")} 会删除它在本组各 VirtualService 里的 hosts 和域名;不再属于任何组的集群,采集到的现状也会删除(集群里的 VirtualService 不受影响)。确定吗?`,
      "移出集群",
      { type: "warning" }
    );
  }
  const payload = {
    name: form.name.trim(),
    description: form.description,
    clusters: form.clusters,
    include_patterns: cleanPatterns(form.include_patterns),
    ignore_patterns: cleanPatterns(form.ignore_patterns)
  };
  saving.value = true;
  try {
    if (selected.value === null) {
      const res = await createIstioGroup(payload);
      message("Istio 组已创建", { type: "success" });
      emit("saved", res.data.id);
    } else {
      await updateIstioGroup(selected.value, payload);
      message("Istio 组已保存", { type: "success" });
      emit("saved", selected.value);
    }
    visible.value = false;
  } finally {
    saving.value = false;
  }
}

async function remove() {
  const group = selectedGroup.value;
  if (!group) return;
  await ElMessageBox.confirm(
    `删除组「${group.name}」会删除它的组标准、各集群 hosts 和域名、忽略记录;只在这个组里的集群,采集到的现状也会删除。集群里的 VirtualService 不受影响。确定吗?`,
    "删除 Istio 组",
    { type: "warning", confirmButtonText: "删除" }
  );
  await deleteIstioGroup(group.id);
  message("Istio 组已删除", { type: "success" });
  emit("saved", null);
  visible.value = false;
}
</script>

<template>
  <el-dialog
    v-model="visible"
    title="Istio 组管理"
    width="1000px"
    top="5vh"
    :close-on-click-modal="false"
  >
    <div class="group-dialog">
      <div class="group-list">
        <div
          v-for="group in groups"
          :key="group.id"
          :class="['group-item', { active: selected === group.id }]"
          @click="load(group.id)"
        >
          <div class="group-name">{{ group.name }}</div>
          <div class="group-sub">{{ group.clusters.length }} 个集群</div>
        </div>
        <div
          :class="['group-item', 'new', { active: selected === null }]"
          @click="load(null)"
        >
          <el-icon><Plus /></el-icon> 新建组
        </div>
      </div>

      <el-form label-width="80px" class="group-form">
        <el-alert
          v-if="!editable"
          type="info"
          :closable="false"
          show-icon
          class="mb-3"
          title="当前账号不是在这个组的每个集群都有「Istio 修改」权限,只能查看,不能修改或删除这个组"
        />
        <el-form-item label="组名" required>
          <el-input
            v-model="form.name"
            maxlength="100"
            placeholder="一套业务的集群,如 cassmall"
          />
        </el-form-item>
        <el-form-item label="说明">
          <el-input
            v-model="form.description"
            type="textarea"
            :rows="2"
            placeholder="选填"
          />
        </el-form-item>
        <el-form-item label="集群">
          <div class="block">
            <div class="hint">
              一个组 = 共用的测试集群 +
              本项目的生产集群;一个集群可以加入多个组(如多个项目共用的测试集群),同一个
              VS 只能属于一个组,用下面的包含 /
              忽略规则划分。列表顺序就是页面上集群列的顺序。
            </div>
            <el-table :data="form.clusters" size="small" border class="mt-2">
              <el-table-column label="集群" min-width="180">
                <template #default="{ row }">{{ row.k8s_name }}</template>
              </el-table-column>
              <el-table-column label="类型" width="110">
                <template #default="{ row }">
                  <el-select v-model="row.env_type" size="small">
                    <el-option
                      v-for="(label, value) in ENV_TYPE_LABEL"
                      :key="value"
                      :label="label"
                      :value="value"
                    />
                  </el-select>
                </template>
              </el-table-column>
              <el-table-column label="备注" min-width="140">
                <template #default="{ row }">
                  <el-input
                    v-model="row.remark"
                    size="small"
                    maxlength="255"
                    placeholder="如 项目A"
                  />
                </template>
              </el-table-column>
              <el-table-column label="操作" width="120" align="center">
                <template #default="{ $index }">
                  <el-button
                    link
                    :icon="ArrowUp"
                    :disabled="$index === 0"
                    @click="move($index, -1)"
                  />
                  <el-button
                    link
                    :icon="ArrowDown"
                    :disabled="$index === form.clusters.length - 1"
                    @click="move($index, 1)"
                  />
                  <el-button
                    link
                    type="danger"
                    :icon="Delete"
                    @click="form.clusters.splice($index, 1)"
                  />
                </template>
              </el-table-column>
            </el-table>
            <div class="add-row">
              <el-select
                v-model="adding"
                multiple
                filterable
                collapse-tags
                collapse-tags-tooltip
                placeholder="选择要加入的集群"
                style="flex: 1"
              >
                <el-option
                  v-for="item in addable"
                  :key="item.k8s_name"
                  :label="item.k8s_name"
                  :value="item.k8s_name"
                  :disabled="!item.allowed"
                >
                  <span>{{ item.k8s_name }}</span>
                  <span v-if="!item.allowed" class="option-sub">
                    没有「Istio 修改」权限
                  </span>
                  <span v-else-if="item.others.length" class="option-sub">
                    已在组 {{ item.others.join("、") }}
                  </span>
                </el-option>
              </el-select>
              <el-button
                :icon="Plus"
                :disabled="!adding.length"
                @click="addClusters"
              >
                加入
              </el-button>
            </div>
          </div>
        </el-form-item>
        <el-form-item label="VS 范围">
          <div class="block">
            <div class="hint">
              通配符:* 任意多个字符,? 一个字符;正则为全匹配。规则里有 /
              时匹配「命名空间/名称」,否则只匹配名称。先看忽略规则,再看包含规则;单个
              VS 也可以在列表里手动忽略,或在「查看忽略」里取消忽略。
            </div>
            <div class="pattern-cols">
              <div class="pattern-col">
                <div class="col-title">包含的 VS</div>
                <PatternList
                  v-model="form.include_patterns"
                  empty-text="不填 = 组内集群里的全部 VS"
                />
              </div>
              <div class="pattern-col">
                <div class="col-title">忽略的 VS</div>
                <PatternList
                  v-model="form.ignore_patterns"
                  empty-text="不忽略"
                />
              </div>
            </div>
            <div class="preview-bar">
              <el-button
                size="small"
                :icon="View"
                :loading="previewing"
                @click="runPreview"
              >
                预览效果
              </el-button>
              <template v-if="preview">
                <span :class="['preview-sum', { stale: previewStale }]">
                  按当前规则:共 {{ preview.total }} 个 VS,本组
                  {{ preview.visible }} 个,忽略
                  {{ preview.ignored }} 个,与其他组冲突
                  {{ preview.conflict }} 个
                  <template v-if="previewStale">(规则改了,请重新预览)</template>
                </span>
                <el-radio-group v-model="previewFilter" size="small">
                  <el-radio-button value="all">全部</el-radio-button>
                  <el-radio-button value="visible">本组</el-radio-button>
                  <el-radio-button value="ignored">忽略</el-radio-button>
                  <el-radio-button value="conflict">冲突</el-radio-button>
                </el-radio-group>
              </template>
              <span v-else class="hint">按已采集的现状看规则的效果,不保存</span>
            </div>
            <template v-if="preview">
              <el-alert
                v-if="preview.uncollected.length"
                type="warning"
                :closable="false"
                show-icon
                class="mt-2"
                :title="`${preview.uncollected.join('、')} 还没采集过现状,预览里不包括它们`"
              />
              <el-table
                :data="previewRows"
                size="small"
                border
                max-height="260"
                class="mt-2"
                empty-text="没有"
              >
                <el-table-column label="VirtualService" min-width="200">
                  <template #default="{ row }">
                    <span class="sub">{{ row.namespace }}/</span>{{ row.name }}
                    <el-tag
                      v-if="row.managed"
                      size="small"
                      type="info"
                      class="ml-1"
                      >已纳管</el-tag
                    >
                  </template>
                </el-table-column>
                <el-table-column label="结果" min-width="220">
                  <template #default="{ row }">
                    <el-tag
                      size="small"
                      :type="row.visible ? 'success' : 'info'"
                      class="mr-1"
                      >{{ row.visible ? "本组" : "忽略" }}</el-tag
                    >
                    <span class="sub">{{ scopeText(row) }}</span>
                  </template>
                </el-table-column>
                <el-table-column label="与其他组冲突" min-width="220">
                  <template #default="{ row }">
                    <span v-if="row.conflicts.length" class="danger">{{
                      conflictText(row.conflicts)
                    }}</span>
                  </template>
                </el-table-column>
              </el-table>
            </template>
          </div>
        </el-form-item>
      </el-form>
    </div>

    <template #footer>
      <el-button
        v-if="selected !== null"
        type="danger"
        plain
        style="float: left"
        :disabled="!editable"
        @click="remove"
      >
        删除组
      </el-button>
      <el-button @click="visible = false">取消</el-button>
      <el-button
        type="primary"
        :loading="saving"
        :disabled="!editable"
        @click="save"
        >保存</el-button
      >
    </template>
  </el-dialog>
</template>

<style scoped>
.group-dialog {
  display: flex;
  gap: 16px;
  max-height: 72vh;
}

.group-list {
  flex: 0 0 170px;
  overflow-y: auto;
  border-right: 1px solid var(--el-border-color-lighter);
}

.group-item {
  padding: 8px 12px;
  margin-right: 8px;
  cursor: pointer;
  border-radius: 4px;
}

.group-item:hover {
  background: var(--el-fill-color-light);
}

.group-item.active {
  color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
}

.group-item.new {
  display: flex;
  gap: 4px;
  align-items: center;
  color: var(--el-color-primary);
}

.group-name {
  font-weight: 600;
}

.group-sub {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.group-form {
  flex: 1;
  min-width: 0;
  padding-right: 8px;
  overflow-y: auto;
}

.block {
  width: 100%;
}

.hint,
.sub {
  font-size: 12px;
  line-height: 18px;
  color: var(--el-text-color-secondary);
}

.add-row {
  display: flex;
  gap: 8px;
  margin-top: 8px;
}

.option-sub {
  float: right;
  margin-left: 12px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.pattern-cols {
  display: flex;
  gap: 16px;
  margin-top: 8px;
}

.pattern-col {
  flex: 1;
  min-width: 0;
}

.col-title {
  margin-bottom: 4px;
  font-size: 13px;
  font-weight: 600;
}

.preview-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  margin-top: 4px;
}

.preview-sum {
  font-size: 13px;
}

.preview-sum.stale {
  color: var(--el-color-warning);
}

.danger {
  font-size: 12px;
  color: var(--el-color-danger);
}
</style>
