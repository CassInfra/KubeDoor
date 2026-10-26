<script setup lang="ts">
import { computed, h as vnode, ref, watch } from "vue";
import { ElMessageBox } from "element-plus";
import { Back, Right, Switch } from "@element-plus/icons-vue";
import { message } from "@/utils/message";
import {
  type CompareResult,
  type IstioGroupCluster,
  type RouteDiffRow,
  type SyncPayload,
  type SyncPick,
  type SyncPreview,
  compareIstio,
  previewIstioSync,
  resetIstioVS,
  syncIstioVS
} from "@/api/istio";
import MonacoDiff from "./MonacoDiff.vue";
import LineDiff from "./LineDiff.vue";
import { ENV_TYPE_LABEL, toYaml } from "../utils";

defineOptions({ name: "IstioCompareDialog" });

/** 对比两个对象(组标准 / 组标准 · 集群 / 集群现状)。一边是组标准、一边是集群现状时,管理员可以把现状里的
 *  规则整条同步到组标准(只能这个方向),也能同步 gateways、默认路由、其他字段;选好后「保存组标准」一次写入 */
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

type Side = "left" | "right";
type Field = "gateways" | "default" | "extra";

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

// ---------------------------------------------------------------- 从集群现状同步到组标准(先选,保存时一次写入)
/** 选了要同步的规则:`组标准序号:现状序号` → 两边的序号 */
const picks = ref<Record<string, SyncPick>>({});
const fieldPicks = ref<Record<Field, boolean>>({
  gateways: false,
  default: false,
  extra: false
});
/** 选的时候两边的版本;重新加载对比后变了,选的就作废(对齐结果可能不一样) */
const pickedOn = ref("");
const saving = ref(false);

/** 两个对比对象里哪边是组标准、哪边是集群现状;不是一边一个就不能同步 */
function syncSides(left: string, right: string) {
  const sides = [left, right];
  const std = sides.findIndex(side => side.startsWith("standard"));
  const live = sides.findIndex(side => side.startsWith("cluster@"));
  if (std < 0 || live < 0) return null;
  return {
    std: (std === 0 ? "left" : "right") as Side,
    live: (live === 0 ? "left" : "right") as Side,
    standard: sides[std],
    k8s: sides[live].slice("cluster@".length)
  };
}

const sync = computed(() => {
  if (!props.isAdmin || props.vsId === null || !result.value) return null;
  const sides = syncSides(result.value.left.side, result.value.right.side);
  if (!sides) return null;
  const std = result.value[sides.std];
  const live = result.value[sides.live];
  if (!std.exists || !live.exists || !std.version || !live.version) return null;
  return {
    ...sides,
    versions: `${std.version}:${live.version}`,
    /** 组标准按该集群的域名显示时,「只是域名不同」说明存的域名和现状不一样,也可以同步 */
    domains:
      sides.standard === `standard@${sides.k8s}` &&
      props.boundClusters.includes(sides.k8s)
  };
});
/** 箭头指向组标准一侧 */
const arrow = computed(() => (sync.value?.std === "right" ? Right : Back));

const pickCount = computed(
  () =>
    Object.keys(picks.value).length +
    Object.values(fieldPicks.value).filter(Boolean).length
);
const dirty = computed(() => pickCount.value > 0);

function clearPicks() {
  picks.value = {};
  fieldPicks.value = { gateways: false, default: false, extra: false };
  pickedOn.value = "";
}

function remember() {
  if (!pickedOn.value && sync.value) pickedOn.value = sync.value.versions;
}

function indexes(row: RouteDiffRow): SyncPick {
  const s = sync.value!;
  return {
    standard_index: row[`${s.std}_index`],
    live_index: row[`${s.live}_index`]
  };
}
const pickKey = (p: SyncPick) =>
  `${p.standard_index ?? "-"}:${p.live_index ?? "-"}`;

interface SyncAction {
  label: string;
  pending: string;
  note: string;
  danger?: boolean;
}

/** 一行规则能怎么同步:只有现状有的加入,两边不同的替换,只有组标准有的删除,只是顺序不同的调整顺序 */
function rowAction(row: RouteDiffRow): SyncAction | null {
  if (!sync.value) return null;
  const { standard_index: std, live_index: live } = indexes(row);
  if (std === null)
    return {
      label: "加入组标准",
      pending: "待加入",
      note: "保存后把这条规则加入组标准,位置和现状里一样"
    };
  if (live === null)
    return {
      label: "从组标准删除",
      pending: "待删除",
      note: "保存后从组标准删除这条规则(其他集群下发时也会删掉)",
      danger: true
    };
  if (row.op === "changed" || (row.op === "domains" && sync.value.domains))
    return {
      label: "用现状替换",
      pending: row.moved ? "待替换并调整顺序" : "待替换",
      note: `保存后组标准的这条规则换成现状里的内容${row.moved ? ",并按现状调整顺序" : ""}`
    };
  if (row.moved)
    return {
      label: "按现状调整顺序",
      pending: "待调整顺序",
      note: "保存后按现状里的位置调整这条规则的顺序"
    };
  return null;
}

const isPicked = (row: RouteDiffRow) =>
  !!sync.value && pickKey(indexes(row)) in picks.value;

function pick(row: RouteDiffRow) {
  remember();
  const p = indexes(row);
  picks.value = { ...picks.value, [pickKey(p)]: p };
}

function unpick(row: RouteDiffRow) {
  const { [pickKey(indexes(row))]: _, ...rest } = picks.value;
  picks.value = rest;
}

/** gateways / 默认路由 / 其他字段:现状里没有的,同步就是从组标准删掉 */
function fieldAction(field: Field): SyncAction | null {
  if (!sync.value || !diff.value || diff.value[field].same) return null;
  const live = diff.value[field][sync.value.live];
  if (field !== "gateways" && !(live && Object.keys(live).length))
    return {
      label: "从组标准删除",
      pending: "待删除",
      note: "保存后从组标准删除",
      danger: true
    };
  return { label: "用现状替换", pending: "待替换", note: "保存后改成现状里的" };
}

function pickField(field: Field) {
  remember();
  fieldPicks.value = { ...fieldPicks.value, [field]: true };
}

/** 两边显示的内容:选了同步的,组标准一侧显示保存后的样子(和现状一样;现状里没有的划掉) */
function view(left: unknown, right: unknown, picked: boolean) {
  const empty = (value: unknown) =>
    value == null ||
    (typeof value === "object" && !Object.keys(value as object).length);
  const texts = {
    left: empty(left) ? "" : yaml(left),
    right: empty(right) ? "" : yaml(right)
  };
  const s = sync.value;
  const titles = {
    leftTitle: sideTitle("left", picked),
    rightTitle: sideTitle("right", picked)
  };
  if (!picked || !s) return { ...texts, ...titles, strike: "" as const };
  if (!texts[s.live]) return { ...texts, ...titles, strike: s.std };
  return {
    left: texts[s.live],
    right: texts[s.live],
    ...titles,
    strike: "" as const
  };
}

function sideTitle(side: Side, picked: boolean) {
  const label = result.value?.[side].label ?? "";
  return picked && sync.value?.std === side ? `${label}(保存后)` : label;
}

const rowView = (row: RouteDiffRow) => view(row.left, row.right, isPicked(row));
const fieldView = (field: Field) =>
  view(
    diff.value?.[field].left,
    diff.value?.[field].right,
    fieldPicks.value[field]
  );

function payload(): SyncPayload {
  const s = sync.value!;
  return {
    group_id: props.groupId,
    namespace: props.namespace,
    name: props.name,
    standard: s.standard,
    k8s_name: s.k8s,
    rules: Object.values(picks.value),
    ...fieldPicks.value,
    standard_version: result.value![s.std].version!,
    live_version: result.value![s.live].version!
  };
}

async function confirmDiscard() {
  try {
    await ElMessageBox.confirm(
      `从集群现状同步的 ${pickCount.value} 处修改还没保存,放弃这些修改?`,
      "放弃修改",
      {
        type: "warning",
        confirmButtonText: "放弃",
        cancelButtonText: "继续编辑"
      }
    );
    return true;
  } catch {
    return false;
  }
}

async function discard() {
  if (await confirmDiscard()) clearPicks();
}

function confirmContent(preview: SyncPreview) {
  const s = preview.summary;
  const changes = [
    s.added && `加入 ${s.added} 条规则`,
    s.replaced && `替换 ${s.replaced} 条规则`,
    s.deleted && `删除 ${s.deleted} 条规则`,
    s.moved && `调整 ${s.moved} 条规则的顺序`,
    s.gateways && "gateways",
    s.default && "默认路由",
    s.extra && "其他字段"
  ].filter(Boolean);
  const list = (items: string[]) =>
    vnode(
      "ul",
      { style: "margin: 4px 0; padding-left: 18px" },
      items.map(item => vnode("li", null, item))
    );
  return vnode("div", null, [
    vnode(
      "p",
      null,
      `把 ${sync.value?.k8s} 现状里的这些内容写入组标准:${changes.join("、") || "和组标准一样,没有变化"}。`
    ),
    vnode(
      "p",
      null,
      "组标准全组共用:保存后要下发才会在集群里生效,其他集群下发时也会改成这样。"
    ),
    preview.missing_domains.length
      ? vnode("div", { style: "color: var(--el-color-danger)" }, [
          vnode(
            "p",
            null,
            "保存后这些集群缺少规则的域名,在「编辑 hosts 和域名」里补上之前不能下发:"
          ),
          list(
            preview.missing_domains.map(
              m => `${m.k8s_name}:${m.rules.join("、")}`
            )
          )
        ])
      : null,
    s.lint.length
      ? vnode("div", { style: "color: var(--el-color-warning)" }, [
          vnode(
            "p",
            null,
            "这些规则不符合命名规范(可以保存,之后在详情页修改):"
          ),
          list(s.lint)
        ])
      : null
  ]);
}

async function save() {
  if (!sync.value || !dirty.value) return;
  const data = payload();
  saving.value = true;
  try {
    const preview = await previewIstioSync(data);
    await ElMessageBox.confirm(confirmContent(preview.data), "保存组标准", {
      type: "warning",
      confirmButtonText: "保存",
      cancelButtonText: "取消"
    });
    const res = await syncIstioVS(data);
    message(res.msg || "已保存", { type: "success" });
    clearPicks();
    emit("changed");
    await load();
  } catch (e) {
    // 接口出错(比如打开对比之后被改了):重新加载,选的同步作废时会提示
    if (typeof e !== "string") await load();
  } finally {
    saving.value = false;
  }
}

// ---------------------------------------------------------------- 加载与切换
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
    if (dirty.value && sync.value?.versions !== pickedOn.value) {
      clearPicks();
      message("组标准或集群现状已经变了,刚才选的同步作废,请重新选择", {
        type: "warning"
      });
    }
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
    clearPicks();
    load();
  },
  { immediate: true }
);

/** 换对比对象:换成别的组标准显示方式或别的集群,选的同步就作废 */
async function changeSide(which: Side, value: string) {
  const next =
    which === "left" ? [value, rightSide.value] : [leftSide.value, value];
  if (dirty.value) {
    const now = syncSides(leftSide.value, rightSide.value);
    const then = syncSides(next[0], next[1]);
    if (!then || now?.standard !== then.standard || now?.k8s !== then.k8s) {
      if (!(await confirmDiscard())) return;
      clearPicks();
    }
  }
  [leftSide.value, rightSide.value] = next;
  load();
}

function swap() {
  [leftSide.value, rightSide.value] = [rightSide.value, leftSide.value];
  load();
}

async function beforeClose(done: () => void) {
  if (!dirty.value || (await confirmDiscard())) {
    clearPicks();
    done();
  }
}

const diff = computed(() => result.value?.diff);
const routeRows = computed(() =>
  (diff.value?.routes ?? []).filter(
    r => !onlyDiff.value || r.op !== "same" || r.moved || isPicked(r)
  )
);
const pickedRules = computed(() => Object.keys(picks.value).length);

// YAML 对比:有没保存的同步时,组标准一侧显示保存后的内容
const draft = ref<Record<string, any> | null>(null);
const draftError = ref("");
let draftSeq = 0;
async function loadDraft() {
  const seq = ++draftSeq;
  draft.value = null;
  draftError.value = "";
  if (!dirty.value || !sync.value) return;
  try {
    const res = await previewIstioSync(payload(), true);
    if (seq === draftSeq) draft.value = res.data.spec;
  } catch (e: any) {
    if (seq === draftSeq)
      draftError.value = e?.response?.data?.error || "取保存后的组标准失败";
  }
}
const pickSignature = computed(() =>
  JSON.stringify([picks.value, fieldPicks.value])
);
watch([tab, pickSignature], () => {
  if (tab.value === "yaml") loadDraft();
});

function sideYaml(side: Side) {
  const spec =
    dirty.value && sync.value?.std === side && draft.value
      ? draft.value
      : result.value?.[side].spec;
  return toYaml(spec, true);
}
const leftYaml = computed(() => sideYaml("left"));
const rightYaml = computed(() => sideYaml("right"));

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

const FIELDS: { key: Field; label: string }[] = [
  { key: "gateways", label: "gateways" },
  { key: "default", label: "默认路由" },
  { key: "extra", label: "其他字段" }
];

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

function rowClass({ row }: { row: RouteDiffRow }) {
  return isPicked(row) ? "row-picked" : "";
}
</script>

<template>
  <el-dialog
    v-model="visible"
    :title="`对比 · ${namespace}/${name}`"
    width="90%"
    top="5vh"
    class="istio-compare-dialog"
    body-class="istio-compare-body"
    :close-on-click-modal="false"
    :before-close="beforeClose"
  >
    <div class="side-row">
      <el-select
        :model-value="leftSide"
        style="flex: 1"
        @update:model-value="(v: string) => changeSide('left', v)"
      >
        <el-option
          v-for="item in options"
          :key="item.value"
          :label="item.label"
          :value="item.value"
        />
      </el-select>
      <el-button :icon="Switch" circle title="左右交换" @click="swap" />
      <el-select
        :model-value="rightSide"
        style="flex: 1"
        @update:model-value="(v: string) => changeSide('right', v)"
      >
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
          v-else-if="sync"
          type="info"
          :closable="false"
          show-icon
          :title="`可以把 ${sync.k8s} 现状里的规则整条同步到组标准(只能从现状到组标准):在要同步的规则后面点按钮,选好后点「保存组标准」`"
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

        <el-tabs v-model="tab" class="compare-tabs">
          <el-tab-pane label="差异摘要" name="summary">
            <div class="summary-pane">
              <el-descriptions :column="1" border size="small">
                <el-descriptions-item
                  label="hosts"
                  label-class-name="desc-label"
                >
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
                    <div class="hint">
                      hosts 各集群独立,不同是正常的,也不同步
                    </div>
                  </template>
                </el-descriptions-item>
                <template v-for="f in FIELDS" :key="f.key">
                  <el-descriptions-item
                    v-if="f.key !== 'extra' || !diff.extra.same"
                    :label="f.label"
                    label-class-name="desc-label"
                  >
                    <span v-if="diff[f.key].same" class="ok">相同</span>
                    <template v-else>
                      <div v-if="fieldAction(f.key)" class="field-sync">
                        <template v-if="fieldPicks[f.key]">
                          <el-tag size="small" type="success">{{
                            fieldAction(f.key)?.pending
                          }}</el-tag>
                          <span class="hint">{{
                            fieldAction(f.key)?.note
                          }}</span>
                          <el-button
                            link
                            type="primary"
                            @click="fieldPicks[f.key] = false"
                            >撤销</el-button
                          >
                        </template>
                        <el-button
                          v-else
                          size="small"
                          plain
                          :type="
                            fieldAction(f.key)?.danger ? 'danger' : 'primary'
                          "
                          :icon="arrow"
                          @click="pickField(f.key)"
                          >{{ fieldAction(f.key)?.label }}</el-button
                        >
                      </div>
                      <LineDiff
                        v-bind="fieldView(f.key)"
                        :context="2"
                        max-height="240px"
                      />
                    </template>
                  </el-descriptions-item>
                </template>
                <el-descriptions-item
                  label="http 规则"
                  label-class-name="desc-label"
                >
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
                  <el-tag
                    v-if="pickedRules"
                    size="small"
                    type="success"
                    class="ml-2"
                    >已选 {{ pickedRules }} 条同步到组标准</el-tag
                  >
                </el-descriptions-item>
              </el-descriptions>

              <div class="route-head">
                <span>http 规则(按匹配条件对齐,展开看逐行对比)</span>
                <el-checkbox v-model="onlyDiff">只看差异</el-checkbox>
              </div>
              <div class="route-table">
                <el-table
                  :data="routeRows"
                  :row-class-name="rowClass"
                  size="small"
                  border
                  height="100%"
                >
                  <el-table-column type="expand">
                    <template #default="{ row }">
                      <div class="expand">
                        <el-alert
                          v-if="isPicked(row)"
                          type="success"
                          :closable="false"
                          show-icon
                          :title="rowAction(row)?.note"
                          class="mb-2"
                        />
                        <LineDiff v-bind="rowView(row)" max-height="420px" />
                      </div>
                    </template>
                  </el-table-column>
                  <el-table-column label="差异" width="190">
                    <template #default="{ row }">
                      <el-tag size="small" :type="OP_META[row.op].type">{{
                        OP_META[row.op].label
                      }}</el-tag>
                      <el-tag
                        v-if="row.moved"
                        size="small"
                        type="info"
                        class="ml-1"
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
                    min-width="320"
                    show-overflow-tooltip
                  >
                    <template #default="{ row }">
                      {{ row.label
                      }}<span v-if="row.target" class="target">
                        → {{ row.target }}</span
                      >
                    </template>
                  </el-table-column>
                  <el-table-column v-if="sync" label="同步到组标准" width="220">
                    <template #default="{ row }">
                      <template v-if="rowAction(row)">
                        <template v-if="isPicked(row)">
                          <el-tag size="small" type="success">{{
                            rowAction(row)?.pending
                          }}</el-tag>
                          <el-button
                            link
                            type="primary"
                            class="ml-2"
                            @click="unpick(row)"
                            >撤销</el-button
                          >
                        </template>
                        <el-button
                          v-else
                          size="small"
                          plain
                          :type="rowAction(row)?.danger ? 'danger' : 'primary'"
                          :icon="arrow"
                          @click="pick(row)"
                          >{{ rowAction(row)?.label }}</el-button
                        >
                      </template>
                    </template>
                  </el-table-column>
                </el-table>
              </div>
            </div>
          </el-tab-pane>
          <el-tab-pane label="YAML 对比" name="yaml" lazy>
            <div class="yaml-pane">
              <el-alert
                v-if="dirty && draftError"
                type="error"
                :closable="false"
                show-icon
                :title="draftError"
                class="mb-2"
              />
              <el-alert
                v-else-if="dirty"
                type="success"
                :closable="false"
                show-icon
                :title="`组标准一侧显示的是保存后的内容(含 ${pickCount} 处还没保存的同步)`"
                class="mb-2"
              />
              <div class="yaml-editor">
                <MonacoDiff
                  :original="leftYaml"
                  :modified="rightYaml"
                  height="100%"
                />
              </div>
            </div>
          </el-tab-pane>
        </el-tabs>
      </template>
    </div>

    <template #footer>
      <div class="footer">
        <span v-if="dirty" class="dirty-tip"
          >已选 {{ pickCount }} 处从
          {{ sync?.k8s }} 现状同步到组标准,保存后才写入</span
        >
        <template v-if="dirty">
          <el-button @click="discard">放弃修改</el-button>
          <el-button type="primary" :loading="saving" @click="save"
            >保存组标准</el-button
          >
        </template>
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
            :disabled="dirty"
            :title="dirty ? '先保存或放弃同步的修改' : ''"
            @click="resetFromCluster"
          >
            以 {{ clusterSide.k8s }} 现状更新组标准
          </el-button>
          <el-button
            v-if="vsId !== null && boundClusters.includes(clusterSide.k8s)"
            type="primary"
            plain
            :disabled="dirty"
            :title="dirty ? '先保存或放弃同步的修改' : ''"
            @click="emit('apply', clusterSide.k8s)"
          >
            下发组标准到 {{ clusterSide.k8s }}
          </el-button>
        </template>
        <el-button @click="beforeClose(() => (visible = false))"
          >关闭</el-button
        >
      </div>
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
  display: flex;
  flex: 1;
  flex-direction: column;
  min-height: 200px;
}

.compare-tabs {
  flex: 1;
  min-height: 0;
}

.compare-tabs :deep(.el-tabs__content) {
  flex: 1;
  min-height: 0;
}

.compare-tabs :deep(.el-tab-pane) {
  height: 100%;
}

.summary-pane {
  display: flex;
  flex-direction: column;
  height: 100%;
  overflow: auto;
}

.summary-pane :deep(.desc-label) {
  width: 100px;
}

.route-table {
  flex: 1;
  min-height: 300px;
}

.route-table :deep(.el-table__row.row-picked > td.el-table__cell) {
  background-color: var(--el-color-success-light-9);
}

.expand {
  padding: 4px 12px;
}

.yaml-pane {
  display: flex;
  flex-direction: column;
  height: 100%;
}

.yaml-editor {
  flex: 1;
  min-height: 0;
}

.ok {
  color: var(--el-color-success);
}

.hint {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.target {
  color: var(--el-text-color-secondary);
}

.field-sync {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-bottom: 6px;
}

.route-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin: 12px 0 6px;
  font-weight: 600;
}

.footer {
  display: flex;
  align-items: center;
  justify-content: flex-end;
}

.dirty-tip {
  margin-right: auto;
  font-size: 13px;
  color: var(--el-color-success);
}
</style>

<style>
/* 对话框占浏览器的 90%:内容区撑满,里面的表格、YAML 对比自己滚动 */
.istio-compare-dialog {
  display: flex;
  flex-direction: column;
  height: 90vh;
  margin-bottom: 0;
}

.istio-compare-body {
  display: flex;
  flex: 1;
  flex-direction: column;
  min-height: 0;
  overflow: hidden;
}
</style>
