<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { ElMessageBox } from "element-plus";
import { Switch } from "@element-plus/icons-vue";
import { message } from "@/utils/message";
import {
  type CompareResult,
  type IstioGroupCluster,
  compareIstio,
  resetIstioVS
} from "@/api/istio";
import MonacoDiff from "./MonacoDiff.vue";
import { ENV_TYPE_LABEL, toYaml } from "../utils";

defineOptions({ name: "IstioCompareDialog" });

const props = defineProps<{
  groupId: number;
  namespace: string;
  name: string;
  clusters: IstioGroupCluster[];
  /** 组标准 id,没有就是未纳管 */
  vsId: number | null;
  /** 已纳入组标准的集群 */
  boundClusters: string[];
  isAdmin: boolean;
  left: string;
  right: string;
}>();
const emit = defineEmits<{
  (e: "changed"): void;
  (e: "apply", k8sName: string): void;
  (e: "import", base: string): void;
}>();
const visible = defineModel<boolean>({ default: false });

const leftSide = ref("");
const rightSide = ref("");
const result = ref<CompareResult | null>(null);
const loading = ref(false);
const onlyDiff = ref(true);
const tab = ref("summary");

const options = computed(() => {
  const items: { label: string; value: string }[] = [];
  if (props.vsId !== null) {
    items.push({ label: "组标准(不含 hosts)", value: "standard" });
    for (const c of props.clusters) {
      if (props.boundClusters.includes(c.k8s_name)) {
        items.push({
          label: `组标准 · ${c.k8s_name}`,
          value: `standard@${c.k8s_name}`
        });
      }
    }
  }
  for (const c of props.clusters) {
    items.push({
      label: `${c.k8s_name} 现状(${ENV_TYPE_LABEL[c.env_type]})`,
      value: `cluster@${c.k8s_name}`
    });
  }
  return items;
});

async function load() {
  if (!leftSide.value || !rightSide.value) return;
  loading.value = true;
  try {
    const res = await compareIstio(
      props.groupId,
      props.namespace,
      props.name,
      leftSide.value,
      rightSide.value
    );
    result.value = res.data;
  } finally {
    loading.value = false;
  }
}

watch(
  visible,
  open => {
    if (!open) return;
    leftSide.value = props.left;
    rightSide.value = props.right;
    tab.value = "summary";
    result.value = null;
    load();
  },
  { immediate: true }
);

function swap() {
  [leftSide.value, rightSide.value] = [rightSide.value, leftSide.value];
  load();
}

const diff = computed(() => result.value?.diff);
const routeRows = computed(() =>
  (diff.value?.routes ?? []).filter(
    r => !onlyDiff.value || r.op !== "same" || r.moved
  )
);
const leftYaml = computed(() => toYaml(result.value?.left.spec, true));
const rightYaml = computed(() => toYaml(result.value?.right.spec, true));

const OP_META: Record<
  string,
  { label: string; type: "success" | "warning" | "danger" | "info" }
> = {
  same: { label: "相同", type: "success" },
  domains: { label: "只是域名不同", type: "info" },
  changed: { label: "不同", type: "warning" },
  left_only: { label: "仅左侧有", type: "danger" },
  right_only: { label: "仅右侧有", type: "danger" }
};

/** 组标准一侧还没设置域名的规则 */
const missingDomains = computed(() =>
  [result.value?.left, result.value?.right]
    .filter(side => side?.missing_domains?.length)
    .map(side => `${side?.label}:${side?.missing_domains?.join("、")}`)
);

/** 对比里涉及的集群现状(右侧优先),用于「以现状为准」「下发」 */
const clusterSide = computed(() => {
  for (const side of [rightSide.value, leftSide.value]) {
    const [kind, k8s] = side.split("@");
    if (kind === "cluster" && k8s) {
      const exists =
        side === rightSide.value
          ? result.value?.right.exists
          : result.value?.left.exists;
      return { k8s, exists: !!exists };
    }
  }
  return null;
});

async function resetFromCluster() {
  const target = clusterSide.value;
  if (!target || props.vsId === null) return;
  await ElMessageBox.confirm(
    `用 ${target.k8s} 的现状替换组标准的规则、默认路由和 gateways?各集群 hosts 不变,其他集群规则里的域名也保持各自的;其他集群下次下发时会改成这个版本。`,
    "以集群现状更新组标准",
    { type: "warning" }
  );
  await resetIstioVS(props.vsId, target.k8s);
  message("组标准已更新,下发后生效", { type: "success" });
  emit("changed");
  load();
}

function yaml(value: unknown) {
  return toYaml(value, true);
}
</script>

<template>
  <el-dialog
    v-model="visible"
    :title="`对比 · ${namespace}/${name}`"
    width="1100px"
    top="5vh"
    :close-on-click-modal="false"
  >
    <div class="side-row">
      <el-select v-model="leftSide" style="flex: 1" @change="load">
        <el-option
          v-for="item in options"
          :key="item.value"
          :label="item.label"
          :value="item.value"
        />
      </el-select>
      <el-button :icon="Switch" circle title="左右交换" @click="swap" />
      <el-select v-model="rightSide" style="flex: 1" @change="load">
        <el-option
          v-for="item in options"
          :key="item.value"
          :label="item.label"
          :value="item.value"
        />
      </el-select>
    </div>

    <div v-loading="loading" class="compare-body">
      <template v-if="result && diff">
        <el-alert
          v-if="!result.left.exists || !result.right.exists"
          type="warning"
          :closable="false"
          show-icon
          :title="`${!result.left.exists ? result.left.label : result.right.label} 里没有这个 VirtualService`"
          class="mb-2"
        />
        <el-alert
          v-else-if="diff.same"
          type="success"
          :closable="false"
          show-icon
          title="两边完全一致"
          class="mb-2"
        />
        <el-alert
          v-else-if="diff.shared_same"
          type="success"
          :closable="false"
          show-icon
          title="规则、默认路由、gateways 一致,只有 hosts 不同"
          class="mb-2"
        />
        <el-alert
          v-else-if="diff.rules_same"
          type="success"
          :closable="false"
          show-icon
          title="规则一致,只有 hosts 或规则里的域名不同(这两样各集群独立维护)"
          class="mb-2"
        />
        <el-alert
          v-for="item in missingDomains"
          :key="item"
          type="error"
          :closable="false"
          show-icon
          :title="`还没设置这些规则的域名,不能下发 —— ${item}`"
          class="mb-2"
        />

        <el-tabs v-model="tab">
          <el-tab-pane label="差异摘要" name="summary">
            <el-descriptions :column="1" border size="small">
              <el-descriptions-item label="hosts">
                <span v-if="diff.hosts.same" class="ok">相同</span>
                <template v-else>
                  <div v-if="diff.hosts.left_only.length">
                    仅左侧:
                    <el-tag
                      v-for="h in diff.hosts.left_only"
                      :key="h"
                      size="small"
                      class="mr-1"
                      >{{ h }}</el-tag
                    >
                  </div>
                  <div v-if="diff.hosts.right_only.length">
                    仅右侧:
                    <el-tag
                      v-for="h in diff.hosts.right_only"
                      :key="h"
                      size="small"
                      type="warning"
                      class="mr-1"
                      >{{ h }}</el-tag
                    >
                  </div>
                  <div
                    v-if="
                      !diff.hosts.left_only.length &&
                      !diff.hosts.right_only.length
                    "
                  >
                    只有顺序不同
                  </div>
                  <div class="hint">hosts 各集群独立,不同是正常的</div>
                </template>
              </el-descriptions-item>
              <el-descriptions-item label="gateways">
                <span v-if="diff.gateways.same" class="ok">相同</span>
                <span v-else>
                  左:{{ diff.gateways.left.join(", ") || "无" }};右:{{
                    diff.gateways.right.join(", ") || "无"
                  }}
                </span>
              </el-descriptions-item>
              <el-descriptions-item label="默认路由">
                <span v-if="diff.default.same" class="ok">相同</span>
                <div v-else class="pair">
                  <pre>{{ yaml(diff.default.left) || "无" }}</pre>
                  <pre>{{ yaml(diff.default.right) || "无" }}</pre>
                </div>
              </el-descriptions-item>
              <el-descriptions-item v-if="!diff.extra.same" label="其他字段">
                <div class="pair">
                  <pre>{{ yaml(diff.extra.left) || "无" }}</pre>
                  <pre>{{ yaml(diff.extra.right) || "无" }}</pre>
                </div>
              </el-descriptions-item>
              <el-descriptions-item label="http 规则">
                <span
                  v-if="
                    !diff.summary.left_only &&
                    !diff.summary.right_only &&
                    !diff.summary.changed &&
                    !diff.summary.domains &&
                    !diff.summary.moved
                  "
                  class="ok"
                  >相同({{ diff.routes.length }} 条)</span
                >
                <span v-else>
                  仅左侧有 {{ diff.summary.left_only }} 条,仅右侧有
                  {{ diff.summary.right_only }} 条,内容不同
                  {{ diff.summary.changed }} 条,只是域名不同
                  {{ diff.summary.domains }} 条,顺序不同
                  {{ diff.summary.moved }} 条
                </span>
              </el-descriptions-item>
            </el-descriptions>

            <div class="route-head">
              <span>http 规则(按匹配条件对齐)</span>
              <el-checkbox v-model="onlyDiff">只看差异</el-checkbox>
            </div>
            <el-table :data="routeRows" size="small" border max-height="360">
              <el-table-column type="expand">
                <template #default="{ row }">
                  <div class="pair">
                    <pre>{{ yaml(row.left) || "(无)" }}</pre>
                    <pre>{{ yaml(row.right) || "(无)" }}</pre>
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="差异" width="190">
                <template #default="{ row }">
                  <el-tag size="small" :type="OP_META[row.op].type">{{
                    OP_META[row.op].label
                  }}</el-tag>
                  <el-tag v-if="row.moved" size="small" type="info" class="ml-1"
                    >顺序不同</el-tag
                  >
                </template>
              </el-table-column>
              <el-table-column label="左侧位置" width="80" align="center">
                <template #default="{ row }">{{
                  row.left_index === null ? "-" : row.left_index + 1
                }}</template>
              </el-table-column>
              <el-table-column label="右侧位置" width="80" align="center">
                <template #default="{ row }">{{
                  row.right_index === null ? "-" : row.right_index + 1
                }}</template>
              </el-table-column>
              <el-table-column
                label="规则"
                prop="label"
                min-width="300"
                show-overflow-tooltip
              />
            </el-table>
          </el-tab-pane>
          <el-tab-pane label="YAML 对比" name="yaml" lazy>
            <MonacoDiff
              :original="leftYaml"
              :modified="rightYaml"
              height="560px"
            />
          </el-tab-pane>
        </el-tabs>
      </template>
    </div>

    <template #footer>
      <template v-if="isAdmin && clusterSide">
        <el-button
          v-if="vsId === null && clusterSide.exists"
          type="primary"
          plain
          @click="emit('import', clusterSide.k8s)"
        >
          以 {{ clusterSide.k8s }} 为准纳管
        </el-button>
        <el-button
          v-if="vsId !== null && clusterSide.exists"
          type="warning"
          plain
          @click="resetFromCluster"
        >
          以 {{ clusterSide.k8s }} 现状更新组标准
        </el-button>
        <el-button
          v-if="vsId !== null && boundClusters.includes(clusterSide.k8s)"
          type="primary"
          plain
          @click="emit('apply', clusterSide.k8s)"
        >
          下发组标准到 {{ clusterSide.k8s }}
        </el-button>
      </template>
      <el-button @click="visible = false">关闭</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.side-row {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-bottom: 12px;
}

.compare-body {
  min-height: 200px;
}

.ok {
  color: var(--el-color-success);
}

.hint {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.pair {
  display: flex;
  gap: 8px;
}

.pair pre {
  flex: 1;
  min-width: 0;
  max-height: 300px;
  padding: 6px 8px;
  margin: 0;
  overflow: auto;
  font-size: 12px;
  background: var(--el-fill-color-light);
  border-radius: 4px;
}

.route-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin: 12px 0 6px;
  font-weight: 600;
}
</style>
