<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { message } from "@/utils/message";
import {
  type BindResult,
  type IstioGroupCluster,
  type MatrixRow,
  bindIstio
} from "@/api/istio";
import {
  ENV_TYPE_LABEL,
  ENV_TYPE_TAG,
  STATUS_META,
  conflictText
} from "../utils";

defineOptions({ name: "IstioBindDialog" });

/** 纳入:所选 VS 在这些集群里「未纳入」的(集群里有同名 VS、组标准还没部署到该集群),组标准部署到该集群,
 *  hosts 和规则里的域名取各自的现状;只改花折里的数据,不动集群 */
const props = defineProps<{
  groupId: number;
  rows: MatrixRow[];
  /** 在哪些集群里纳入(矩阵当前显示的集群) */
  clusters: IstioGroupCluster[];
}>();
const emit = defineEmits<{ (e: "done"): void }>();
const visible = defineModel<boolean>({ default: false });

interface Item {
  key: string;
  vsId: number;
  row: MatrixRow;
  cluster: IstioGroupCluster;
  /** 规则(不算 hosts 和域名)和组标准一样:纳入后就是「一致」 */
  same: boolean;
  checked: boolean;
}

const items = ref<Item[]>([]);
const saving = ref(false);
/** 提交过的项的结果,按 key */
const results = ref<Record<string, BindResult>>({});

const keyOf = (vsId: number, k8s: string) => `${vsId}@${k8s}`;

watch(
  visible,
  open => {
    if (!open) return;
    results.value = {};
    items.value = props.rows.flatMap(row => {
      const vsId = row.vs_id;
      if (vsId === null) return [];
      return props.clusters
        .filter(c => row.cells[c.k8s_name]?.status === "unbound")
        .map(cluster => {
          const version = row.cells[cluster.k8s_name].version;
          return {
            key: keyOf(vsId, cluster.k8s_name),
            vsId,
            row,
            cluster,
            same: !!version && version === row.standard_version,
            checked: !row.conflicts.length
          };
        });
    });
  },
  { immediate: true }
);

/** 和其他组冲突的不能纳入(同一个 VS 只能属于一个组);已经纳入成功的不再提交 */
function selectable(item: Item) {
  return !item.row.conflicts.length && !results.value[item.key]?.success;
}
const pickable = computed(() => items.value.filter(selectable));
const checkedItems = computed(() => pickable.value.filter(i => i.checked));
const allChecked = computed(
  () => pickable.value.length > 0 && pickable.value.every(i => i.checked)
);
const someChecked = computed(
  () => !allChecked.value && pickable.value.some(i => i.checked)
);
function toggleAll(value: boolean) {
  for (const item of pickable.value) item.checked = value;
}

const diffCount = computed(() => items.value.filter(i => !i.same).length);
const blocked = computed(
  () => items.value.filter(i => i.row.conflicts.length).length
);

function hostsOf(item: Item) {
  return item.row.cells[item.cluster.k8s_name]?.hosts ?? [];
}

const resultTags = computed(() =>
  Object.fromEntries(
    Object.entries(results.value).map(([key, r]) => {
      if (!r.success) {
        return [
          key,
          { type: "danger" as const, label: "失败", tip: r.error || "纳入失败" }
        ];
      }
      const meta = STATUS_META[r.status ?? "synced"];
      return [
        key,
        { type: meta.type, label: `已纳入 · ${meta.label}`, tip: meta.desc }
      ];
    })
  )
);

async function submit() {
  const picked = checkedItems.value;
  if (!picked.length) {
    message("请勾选要纳入的项", { type: "warning" });
    return;
  }
  saving.value = true;
  try {
    const res = await bindIstio(
      props.groupId,
      picked.map(i => ({ vs_id: i.vsId, k8s_name: i.cluster.k8s_name }))
    );
    const next = { ...results.value };
    for (const r of res.data) next[keyOf(r.vs_id, r.k8s_name)] = r;
    results.value = next;
    const ok = res.data.filter(r => r.success);
    const failed = res.data.length - ok.length;
    const notes = (["pending", "incomplete"] as const)
      .map(status => ({
        label: STATUS_META[status].label,
        count: ok.filter(r => r.status === status).length
      }))
      .filter(n => n.count)
      .map(n => `${n.label} ${n.count} 项`);
    const text = `已纳入 ${ok.length} 项${notes.length ? `,其中${notes.join("、")}` : ""}`;
    if (!failed) {
      message(text, { type: "success", duration: 5000 });
      visible.value = false;
    } else {
      message(`${text};${failed} 项失败,见列表`, {
        type: "warning",
        duration: 6000
      });
    }
    emit("done");
  } finally {
    saving.value = false;
  }
}
</script>

<template>
  <el-dialog
    v-model="visible"
    :title="`纳入集群(${items.length} 项)`"
    width="1000px"
    :close-on-click-modal="false"
  >
    <el-alert
      type="info"
      :closable="false"
      show-icon
      class="mb-3"
      title="这些 VirtualService 已有组标准,集群里也有同名的,只是组标准还没部署到该集群。纳入后规则、默认路由、gateways 用组标准,hosts 和规则里的域名取该集群的现状,集群当前的内容记为「上次下发」。纳入只改花折里的数据,不动集群。"
    />
    <div class="summary">
      规则和组标准一致
      {{ items.length - diffCount }} 项(纳入后「一致」)<template
        v-if="diffCount"
        >;<span class="warn">不同 {{ diffCount }} 项</span
        >,纳入后「待下发」,下发会用组标准覆盖集群里的内容,下发前先在矩阵里「对比」看差异</template
      ><template v-if="blocked"
        >;<span class="danger"
          >{{ blocked }} 项和其他组冲突,不能纳入</span
        ></template
      >
    </div>
    <el-table
      :data="items"
      :row-key="(item: Item) => item.key"
      border
      size="small"
      max-height="520"
    >
      <el-table-column width="44" align="center">
        <template #header>
          <el-checkbox
            :model-value="allChecked"
            :indeterminate="someChecked"
            :disabled="!pickable.length"
            @change="value => toggleAll(!!value)"
          />
        </template>
        <template #default="{ row }">
          <el-checkbox v-model="row.checked" :disabled="!selectable(row)" />
        </template>
      </el-table-column>
      <el-table-column label="VirtualService" min-width="220">
        <template #default="{ row }">
          <div>
            {{ row.row.name }}
            <el-tooltip
              v-if="row.row.conflicts.length"
              :content="`也在${conflictText(row.row.conflicts)}里:先在不该管它的组里忽略它`"
              placement="top"
            >
              <el-tag size="small" type="danger" effect="dark">冲突</el-tag>
            </el-tooltip>
          </div>
          <div class="sub">{{ row.row.namespace }}</div>
        </template>
      </el-table-column>
      <el-table-column label="集群" width="190">
        <template #default="{ row }">
          <div class="cluster">
            <span>{{ row.cluster.k8s_name }}</span>
            <el-tag size="small" :type="ENV_TYPE_TAG[row.cluster.env_type]">{{
              ENV_TYPE_LABEL[row.cluster.env_type]
            }}</el-tag>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="规则" width="130">
        <template #default="{ row }">
          <el-tag v-if="row.same" size="small" type="success"
            >和组标准一致</el-tag
          >
          <el-tooltip
            v-else
            content="纳入后「待下发」:下发会用组标准覆盖集群里的内容,下发前先「对比」看差异"
            placement="top"
          >
            <el-tag size="small" type="warning">和组标准不同</el-tag>
          </el-tooltip>
        </template>
      </el-table-column>
      <el-table-column label="hosts(取自现状)" min-width="240">
        <template #default="{ row }">
          <div
            v-for="h in hostsOf(row).slice(0, 2)"
            :key="h"
            class="host"
            :title="h"
          >
            {{ h }}
          </div>
          <div v-if="hostsOf(row).length > 2" class="sub">
            共 {{ hostsOf(row).length }} 个
          </div>
        </template>
      </el-table-column>
      <el-table-column
        v-if="Object.keys(results).length"
        label="结果"
        width="170"
      >
        <template #default="{ row }">
          <el-tooltip
            v-if="resultTags[row.key]"
            :content="resultTags[row.key].tip"
            placement="top"
          >
            <el-tag size="small" :type="resultTags[row.key].type">{{
              resultTags[row.key].label
            }}</el-tag>
          </el-tooltip>
        </template>
      </el-table-column>
    </el-table>
    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button
        type="primary"
        :loading="saving"
        :disabled="!checkedItems.length"
        @click="submit"
      >
        纳入 {{ checkedItems.length }} 项
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.summary {
  margin-bottom: 8px;
  font-size: 13px;
  color: var(--el-text-color-regular);
}

.warn {
  color: var(--el-color-warning);
}

.danger {
  color: var(--el-color-danger);
}

.sub {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.cluster {
  display: flex;
  gap: 6px;
  align-items: center;
}

.host {
  overflow: hidden;
  font-family: Consolas, Monaco, monospace;
  font-size: 12px;
  line-height: 18px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
