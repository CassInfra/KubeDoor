<script setup lang="ts">
import { computed, h as vnode, nextTick, ref, watch } from "vue";
import { ElMessageBox } from "element-plus";
import { message } from "@/utils/message";
import {
  type ApplyItem,
  type ApplyPreviewItem,
  type ApplyResult,
  type IstioGroupCluster,
  applyIstio,
  previewIstioApply
} from "@/api/istio";
import MonacoDiff from "./MonacoDiff.vue";
import { ENV_TYPE_LABEL, ENV_TYPE_TAG, toYaml } from "../utils";

defineOptions({ name: "IstioApplyDialog" });

/** 下发:先从集群取最新现状逐项预览差异,勾选后执行(同一集群依次、不同集群并行) */
const props = defineProps<{
  items: ApplyItem[];
  clusters: IstioGroupCluster[];
}>();
const emit = defineEmits<{ (e: "done"): void }>();
const visible = defineModel<boolean>({ default: false });

const loading = ref(false);
const applying = ref(false);
const preview = ref<ApplyPreviewItem[]>([]);
const results = ref<ApplyResult[]>([]);
const tableRef = ref();
const selection = ref<ApplyPreviewItem[]>([]);

const envType = (k8s: string) =>
  props.clusters.find(c => c.k8s_name === k8s)?.env_type ?? "test";

const key = (item: ApplyItem) => `${item.vs_id}@${item.k8s_name}`;
const selectable = (row: ApplyPreviewItem) => !row.error;

async function load() {
  loading.value = true;
  results.value = [];
  preview.value = [];
  try {
    const res = await previewIstioApply(props.items);
    preview.value = res.data;
    await nextTick();
    for (const row of preview.value) {
      tableRef.value?.toggleRowSelection(row, !row.error && !row.same);
    }
  } finally {
    loading.value = false;
  }
}

watch(
  visible,
  open => {
    if (open) load();
  },
  { immediate: true }
);

const changedCount = computed(
  () => preview.value.filter(r => !r.error && !r.same).length
);
/** 下发后会新出现重复域名的项(这次下发带来的,集群里原来没有) */
const dupCount = computed(
  () => preview.value.filter(r => r.domain_issues.some(i => i.new)).length
);
/** 勾选的项里这次下发会带来的重复域名,确认下发时列出 */
const newIssues = computed(() =>
  selection.value.flatMap(r =>
    r.domain_issues
      .filter(i => i.new)
      .map(i => `${r.k8s_name} · ${r.namespace}/${r.name}:${i.text}`)
  )
);
const prodSelected = computed(() =>
  Array.from(
    new Set(
      selection.value
        .filter(r => envType(r.k8s_name) === "prod")
        .map(r => r.k8s_name)
    )
  )
);

/** 变化摘要:现状 → 下发,右侧多出来的是新增 */
function changesOf(row: ApplyPreviewItem) {
  const d = row.diff;
  if (!d || row.same) return [];
  const items: string[] = [];
  if (d.summary.right_only) items.push(`新增 ${d.summary.right_only} 条规则`);
  if (d.summary.left_only) items.push(`删除 ${d.summary.left_only} 条规则`);
  if (d.summary.changed) items.push(`修改 ${d.summary.changed} 条规则`);
  if (d.summary.domains) items.push(`${d.summary.domains} 条规则的域名变化`);
  if (d.summary.moved) items.push(`${d.summary.moved} 条规则顺序变化`);
  if (!d.hosts.same) items.push("hosts 变化");
  if (!d.gateways.same) items.push("gateways 变化");
  if (!d.default.same) items.push("默认路由变化");
  if (!d.extra.same) items.push("其他字段变化");
  return items;
}

function stateOf(row: ApplyPreviewItem) {
  const result = results.value.find(r => key(r) === key(row));
  if (result) {
    return result.success
      ? { label: "已下发", type: "success" as const, tip: "" }
      : { label: "下发失败", type: "danger" as const, tip: result.error ?? "" };
  }
  if (row.error)
    return { label: "无法预览", type: "danger" as const, tip: row.error };
  if (row.same) return { label: "无变化", type: "success" as const, tip: "" };
  if (!row.exists)
    return {
      label: "将新建",
      type: "warning" as const,
      tip: "集群里还没有,下发会创建"
    };
  return { label: "有变化", type: "warning" as const, tip: "" };
}

async function submit() {
  if (!selection.value.length) {
    message("请勾选要下发的项", { type: "warning" });
    return;
  }
  const prod = prodSelected.value;
  const fresh = newIssues.value;
  const question = prod.length
    ? `包含生产集群 ${prod.join("、")},确认已核对差异,下发 ${selection.value.length} 项?`
    : `下发 ${selection.value.length} 项?`;
  // 有这次下发带来的重复域名:列出来,确认后照样可以下发(同一个域名按路径分到几条规则是常见写法;
  // 把域名从一个 VirtualService 挪到另一个、只先下发了新的那个时,也会短暂重复)
  const content = fresh.length
    ? vnode("div", null, [
        vnode("p", null, question),
        vnode(
          "p",
          { class: "dup-confirm-title" },
          `下发后会新出现 ${fresh.length} 处重复的域名(同一个集群里的域名不能重复):`
        ),
        ...fresh
          .slice(0, 10)
          .map(text =>
            vnode("div", { class: "dup-confirm-item" }, `· ${text}`)
          ),
        fresh.length > 10
          ? vnode(
              "div",
              { class: "dup-confirm-item" },
              `…另有 ${fresh.length - 10} 处,见列表`
            )
          : null
      ])
    : question;
  await ElMessageBox.confirm(content, "确认下发", {
    type: prod.length || fresh.length ? "warning" : "info",
    confirmButtonText: fresh.length ? "仍要下发" : "下发",
    customClass: fresh.length ? "istio-apply-confirm" : undefined
  });
  applying.value = true;
  try {
    const res = await applyIstio(
      selection.value.map(r => ({ vs_id: r.vs_id, k8s_name: r.k8s_name }))
    );
    results.value = res.data;
    const failed = res.data.filter(r => !r.success).length;
    message(
      failed ? `${failed} 项下发失败,见列表` : `已下发 ${res.data.length} 项`,
      { type: failed ? "warning" : "success" }
    );
    emit("done");
  } finally {
    applying.value = false;
  }
}
</script>

<template>
  <el-dialog
    v-model="visible"
    title="下发到集群"
    width="1100px"
    top="5vh"
    :close-on-click-modal="false"
  >
    <el-alert
      type="info"
      :closable="false"
      show-icon
      class="mb-2"
      title="下发内容 = 组标准(规则、默认路由、gateways)+ 该集群的 hosts 和规则里的域名。下面的差异是集群最新现状(左)→ 将要下发(右),展开行查看。"
    >
      <div>
        下发前会先备份集群里当时的内容,有问题可以在 VirtualService 详情「集群与
        Hosts」的「回退」里恢复(每个集群保留最近 5 份)。
      </div>
      <div>
        「变化」里列出这些都下发后同一个集群里重复的域名,这次下发带来的标红,确认下发时还会再列出来。
      </div>
    </el-alert>
    <el-alert
      v-if="prodSelected.length"
      type="warning"
      :closable="false"
      show-icon
      class="mb-2"
      :title="`已勾选生产集群:${prodSelected.join('、')}`"
    />
    <div class="summary">
      共 {{ preview.length }} 项,有变化 {{ changedCount }} 项,已勾选
      {{ selection.length }} 项<span v-if="dupCount" class="dup-sum"
        >,{{ dupCount }} 项下发后会新出现重复的域名</span
      >
    </div>
    <el-table
      ref="tableRef"
      v-loading="loading"
      :data="preview"
      :row-key="key"
      border
      size="small"
      max-height="560"
      @selection-change="selection = $event"
    >
      <el-table-column type="selection" width="40" :selectable="selectable" />
      <el-table-column type="expand">
        <template #default="{ row }">
          <div v-if="row.error" class="err">{{ row.error }}</div>
          <MonacoDiff
            v-else
            :original="toYaml(row.live, true)"
            :modified="toYaml(row.rendered, true)"
            height="420px"
          />
        </template>
      </el-table-column>
      <el-table-column label="VirtualService" min-width="220">
        <template #default="{ row }">
          {{ row.namespace }}/<b>{{ row.name }}</b>
        </template>
      </el-table-column>
      <el-table-column label="集群" min-width="200">
        <template #default="{ row }">
          {{ row.k8s_name }}
          <el-tag
            size="small"
            :type="ENV_TYPE_TAG[envType(row.k8s_name)]"
            class="ml-1"
            >{{ ENV_TYPE_LABEL[envType(row.k8s_name)] }}</el-tag
          >
        </template>
      </el-table-column>
      <el-table-column label="变化" min-width="360">
        <template #default="{ row }">
          <span v-if="row.error" class="err">{{ row.error }}</span>
          <el-tag
            v-for="item in changesOf(row)"
            :key="item"
            size="small"
            type="warning"
            effect="plain"
            class="mr-1"
            >{{ item }}</el-tag
          >
          <!-- 下发后重复的域名:这次下发带来的标红,集群里原来就有的标灰 -->
          <div v-for="issue in row.domain_issues" :key="issue.text" class="dup">
            <el-tag
              size="small"
              :type="issue.new ? 'danger' : 'info'"
              effect="plain"
              >{{ issue.new ? "这次下发带来" : "集群里原有" }}</el-tag
            >
            <span :class="{ 'dup-new': issue.new }">{{ issue.text }}</span>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="140">
        <template #default="{ row }">
          <el-tooltip
            :disabled="!stateOf(row).tip"
            :content="stateOf(row).tip"
            placement="top"
          >
            <el-tag size="small" :type="stateOf(row).type">{{
              stateOf(row).label
            }}</el-tag>
          </el-tooltip>
        </template>
      </el-table-column>
    </el-table>
    <template #footer>
      <el-button @click="load">重新预览</el-button>
      <el-button @click="visible = false">关闭</el-button>
      <el-button
        type="primary"
        :loading="applying"
        :disabled="!selection.length"
        @click="submit"
      >
        下发 {{ selection.length }} 项
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.summary {
  margin-bottom: 6px;
  font-size: 13px;
  color: var(--el-text-color-secondary);
}

.err {
  font-size: 12px;
  color: var(--el-color-danger);
}

.dup-sum,
.dup-new {
  color: var(--el-color-danger);
}

.dup {
  display: flex;
  gap: 6px;
  align-items: flex-start;
  margin-top: 4px;
  font-size: 12px;
  line-height: 20px;
  word-break: normal;
  overflow-wrap: anywhere;
}

.dup .el-tag {
  flex: none;
}
</style>

<style>
/* 确认下发时列出这次下发带来的重复域名:一条较长,加宽确认框(确认框挂在 body 下,不能用 scoped) */
.istio-apply-confirm {
  --el-messagebox-width: 640px;
}

.istio-apply-confirm .dup-confirm-title {
  margin-top: 8px;
  font-weight: 600;
  color: var(--el-color-danger);
}

.istio-apply-confirm .dup-confirm-item {
  font-size: 12px;
  line-height: 20px;
}
</style>
