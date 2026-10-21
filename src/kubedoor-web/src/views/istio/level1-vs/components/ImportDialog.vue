<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { message } from "@/utils/message";
import {
  type IstioGroupCluster,
  type ItemResult,
  type MatrixRow,
  importIstio
} from "@/api/istio";
import { conflictText } from "../utils";

defineOptions({ name: "IstioImportDialog" });

/** 纳管:以基准集群的现状生成组标准,勾选的集群纳入该 VS,各集群 hosts 取自己的现状;
 *  和其他组冲突的(同一个 VS 只能属于一个组)先不纳管 */
const props = defineProps<{
  groupId: number;
  rows: MatrixRow[];
  clusters: IstioGroupCluster[];
  /** 指定基准集群(从对比里点「以 X 为准纳管」) */
  presetBase?: string;
}>();
const emit = defineEmits<{ (e: "done"): void }>();
const visible = defineModel<boolean>({ default: false });

interface Item {
  row: MatrixRow;
  base: string;
  clusters: string[];
}

const items = ref<Item[]>([]);
const saving = ref(false);
const results = ref<ItemResult[]>([]);

const clusterNames = computed(() => props.clusters.map(c => c.k8s_name));

watch(
  visible,
  open => {
    if (!open) return;
    results.value = [];
    items.value = props.rows.map(row => {
      const present = clusterNames.value.filter(k => row.cells[k]?.in_cluster);
      const base =
        props.presetBase && present.includes(props.presetBase)
          ? props.presetBase
          : (row.versions[0]?.clusters[0] ?? present[0] ?? "");
      return { row, base, clusters: present };
    });
  },
  { immediate: true }
);

function versionOf(item: Item, k8s: string) {
  return item.row.cells[k8s]?.version;
}

const ready = computed(() =>
  items.value.filter(
    item => item.base && item.clusters.length && !item.row.conflicts.length
  )
);
const blocked = computed(
  () => items.value.filter(item => item.row.conflicts.length).length
);

async function submit() {
  if (!ready.value.length) {
    message("请为每个 VS 选择基准集群和部署集群", { type: "warning" });
    return;
  }
  saving.value = true;
  try {
    const res = await importIstio(
      props.groupId,
      ready.value.map(item => ({
        namespace: item.row.namespace,
        name: item.row.name,
        base: item.base,
        clusters: item.clusters
      }))
    );
    results.value = res.data;
    const failed = res.data.filter(r => !r.success);
    if (!failed.length) {
      message(`已纳管 ${res.data.length} 个 VirtualService`, {
        type: "success"
      });
      visible.value = false;
    } else {
      message(`${failed.length} 个纳管失败,见列表`, { type: "warning" });
    }
    emit("done");
  } finally {
    saving.value = false;
  }
}

function resultOf(item: Item) {
  return results.value.find(
    r => r.namespace === item.row.namespace && r.name === item.row.name
  );
}
</script>

<template>
  <el-dialog
    v-model="visible"
    :title="`纳管 VirtualService(${items.length} 个)`"
    width="1000px"
    :close-on-click-modal="false"
  >
    <el-alert
      type="info"
      :closable="false"
      show-icon
      class="mb-3"
      title="以「基准集群」的现状生成组标准(规则、默认路由、gateways 全组共用);勾选的集群纳入这个 VS,hosts 和规则里的域名取各自的现状(集群里没有的,hosts 先用基准集群的,域名要在纳管后补上)。和组标准不一样的集群会显示「待下发」,看过差异再下发即可拉齐。"
    />
    <el-alert
      v-if="blocked"
      type="error"
      :closable="false"
      show-icon
      class="mb-3"
      :title="`有 ${blocked} 个 VS 也在其他组里(同一个 VS 只能属于一个组),这次不纳管:先在不该管它的组里忽略它`"
    />
    <el-table :data="items" border size="small" max-height="520">
      <el-table-column label="VirtualService" min-width="200">
        <template #default="{ row }">
          <div>
            {{ row.row.name }}
            <el-tooltip
              v-if="row.row.conflicts.length"
              :content="`也在${conflictText(row.row.conflicts)}里`"
              placement="top"
            >
              <el-tag size="small" type="danger" effect="dark">冲突</el-tag>
            </el-tooltip>
          </div>
          <div class="sub">{{ row.row.namespace }}</div>
        </template>
      </el-table-column>
      <el-table-column label="版本" width="150">
        <template #default="{ row }">
          <el-tooltip
            v-for="v in row.row.versions"
            :key="v.label"
            :content="v.clusters.join('、')"
            placement="top"
          >
            <el-tag
              size="small"
              :type="row.row.versions.length > 1 ? 'warning' : 'success'"
              class="mr-1"
              >{{ v.label }} × {{ v.clusters.length }}</el-tag
            >
          </el-tooltip>
        </template>
      </el-table-column>
      <el-table-column label="基准集群" width="210">
        <template #default="{ row }">
          <el-select v-model="row.base" size="small">
            <el-option
              v-for="k in clusterNames.filter(
                k => row.row.cells[k]?.in_cluster
              )"
              :key="k"
              :label="`${k}(版本 ${versionOf(row, k)})`"
              :value="k"
            />
          </el-select>
        </template>
      </el-table-column>
      <el-table-column label="部署到" min-width="320">
        <template #default="{ row }">
          <el-checkbox-group v-model="row.clusters" size="small">
            <el-checkbox v-for="k in clusterNames" :key="k" :value="k">
              {{ k }}
              <span v-if="!row.row.cells[k]?.in_cluster" class="sub"
                >(集群里没有)</span
              >
            </el-checkbox>
          </el-checkbox-group>
        </template>
      </el-table-column>
      <el-table-column v-if="results.length" label="结果" width="180">
        <template #default="{ row }">
          <template v-if="resultOf(row)">
            <el-tag v-if="resultOf(row)?.success" type="success" size="small"
              >已纳管</el-tag
            >
            <el-tooltip v-else :content="resultOf(row)?.error" placement="top">
              <el-tag type="danger" size="small">失败</el-tag>
            </el-tooltip>
          </template>
        </template>
      </el-table-column>
    </el-table>
    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button type="primary" :loading="saving" @click="submit">
        纳管 {{ ready.length }} 个
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.sub {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
</style>
