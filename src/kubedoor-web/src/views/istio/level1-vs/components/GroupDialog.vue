<script setup lang="ts">
import { computed, reactive, ref, watch } from "vue";
import { ElMessageBox } from "element-plus";
import { ArrowDown, ArrowUp, Delete, Plus } from "@element-plus/icons-vue";
import { message } from "@/utils/message";
import {
  type EnvType,
  type IstioGroup,
  createIstioGroup,
  deleteIstioGroup,
  updateIstioGroup
} from "@/api/istio";
import { ENV_TYPE_LABEL } from "../utils";

defineOptions({ name: "IstioGroupDialog" });

const props = defineProps<{
  groups: IstioGroup[];
  /** 所有已接入的集群及所属组(空串表示未分组) */
  clusters: { k8s_name: string; group: string }[];
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
  clusters: [] as MemberForm[]
});
const adding = ref<string[]>([]);
const saving = ref(false);

const selectedGroup = computed(() =>
  props.groups.find(group => group.id === selected.value)
);

/** 可加入的集群:未分组的,或本来就属于当前组(被移出后又加回来) */
const addable = computed(() => {
  const inForm = new Set(form.clusters.map(c => c.k8s_name));
  const own = selectedGroup.value?.name;
  return props.clusters.filter(
    c => !inForm.has(c.k8s_name) && (!c.group || c.group === own)
  );
});

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
  adding.value = [];
}

watch(
  visible,
  open => {
    if (open) load(props.current ?? props.groups[0]?.id ?? null);
  },
  { immediate: true }
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
      `移出集群 ${removed.join("、")} 会删除它在本组各 VirtualService 里的 hosts 和采集到的现状(集群里的 VirtualService 不受影响),确定吗?`,
      "移出集群",
      { type: "warning" }
    );
  }
  const payload = {
    name: form.name.trim(),
    description: form.description,
    clusters: form.clusters
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
    `删除组「${group.name}」会删除它的组标准、各集群 hosts 和采集到的现状;集群里的 VirtualService 不受影响。确定吗?`,
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
    width="860px"
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

      <el-form label-width="72px" class="group-form">
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
          <div class="member-box">
            <el-alert
              type="info"
              :closable="false"
              show-icon
              title="一个组 = 共用的测试集群 + 各项目的生产集群;一个集群只能属于一个组。列表顺序就是页面上集群列的顺序。"
            />
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
                placeholder="选择要加入的集群(已属于其他组的不能选)"
                style="flex: 1"
              >
                <el-option
                  v-for="item in addable"
                  :key="item.k8s_name"
                  :label="item.k8s_name"
                  :value="item.k8s_name"
                />
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
      </el-form>
    </div>

    <template #footer>
      <el-button
        v-if="selected !== null"
        type="danger"
        plain
        style="float: left"
        @click="remove"
      >
        删除组
      </el-button>
      <el-button @click="visible = false">取消</el-button>
      <el-button type="primary" :loading="saving" @click="save">保存</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.group-dialog {
  display: flex;
  gap: 16px;
}

.group-list {
  flex: 0 0 170px;
  max-height: 520px;
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
}

.member-box {
  width: 100%;
}

.add-row {
  display: flex;
  gap: 8px;
  margin-top: 8px;
}
</style>
