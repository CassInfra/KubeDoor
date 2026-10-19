<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { message } from "@/utils/message";
import {
  type DomainItem,
  type DomainLists,
  type IstioRoute,
  type VSDetail,
  getIstioVS,
  saveIstioVSCluster
} from "@/api/istio";
import {
  cleanHosts,
  describeMatch,
  domainBlockCount,
  formatDomain,
  forwardSummary,
  parseHostsText,
  replaceDomains,
  suggestReplace
} from "../utils";

defineOptions({ name: "IstioHostsDialog" });

/** 编辑某个集群的 hosts 和规则里的域名(各集群独立),可以从别的集群复制并替换集群标识后再改;
 *  没纳入的集群保存后就纳入 */
const props = defineProps<{
  vsId: number | null;
  k8sName: string;
}>();
const emit = defineEmits<{ (e: "saved"): void }>();
const visible = defineModel<boolean>({ default: false });

type Source = "stored" | "live" | "copied" | "none";

const detail = ref<VSDetail | null>(null);
const loading = ref(false);
const saving = ref(false);
const text = ref("");
/** 规则 id → 各域名块里选中的域名 */
const values = ref<Record<number, string[][]>>({});
/** 规则 id → 域名 → 原来的写法(authority / host 头等),保存时原样带回 */
const items = ref<Record<number, Record<string, DomainItem>>>({});
const sources = ref<Record<number, Source>>({});
const copy = ref({ from: "", find: "", replace: "" });

const cluster = computed(() =>
  detail.value?.clusters.find(c => c.k8s_name === props.k8sName)
);
const bound = computed(() => !!cluster.value?.bound);
const parsedHosts = computed(() => parseHostsText(text.value));
const domainRoutes = computed(() =>
  (detail.value?.routes ?? [])
    .map((route, index) => ({ route, position: index + 1 }))
    .filter(({ route }) => domainBlockCount(route.match_rules))
);

/** 可复制的来源:其他集群(纳入了的用组标准里的 hosts 和域名,没纳入的用集群现状) */
const copySources = computed(() =>
  (detail.value?.clusters ?? []).filter(
    c => c.k8s_name !== props.k8sName && c.hosts.length
  )
);

function setDomains(route: IstioRoute, lists: DomainLists, source: Source) {
  const count = domainBlockCount(route.match_rules);
  const blocks = Array.from({ length: count }, (_, i) => lists[i] ?? []);
  values.value[route.id] = blocks.map(block => block.map(d => d.value));
  items.value[route.id] = Object.fromEntries(
    blocks.flat().map(d => [d.value, d])
  );
  sources.value[route.id] = source;
}

function domainsOf(k8s: string, route: IstioRoute): [DomainLists, Source] {
  const row = detail.value?.clusters.find(c => c.k8s_name === k8s);
  if (row?.bound && route.domains[k8s]) return [route.domains[k8s], "stored"];
  if (row?.live_domains?.[route.id])
    return [row.live_domains[route.id], "live"];
  return [[], "none"];
}

async function load() {
  if (props.vsId === null) return;
  loading.value = true;
  detail.value = null;
  try {
    const res = await getIstioVS(props.vsId);
    detail.value = res.data;
    text.value = cleanHosts(cluster.value?.hosts ?? []).join("\n");
    values.value = {};
    items.value = {};
    sources.value = {};
    for (const { route } of domainRoutes.value) {
      setDomains(route, ...domainsOf(props.k8sName, route));
    }
  } finally {
    loading.value = false;
  }
}

watch(
  visible,
  open => {
    if (!open) return;
    copy.value = { from: "", find: "", replace: "" };
    load();
  },
  { immediate: true }
);

watch(
  () => copy.value.from,
  from => {
    if (from) Object.assign(copy.value, suggestReplace(from, props.k8sName));
  }
);

function replaceText(value: string) {
  const { find, replace } = copy.value;
  return find ? value.split(find).join(replace) : value;
}

/** 从其他集群复制 hosts 和各规则的域名(把集群标识换掉);append 只追加 hosts */
function copyFrom(append: boolean) {
  const source = copySources.value.find(c => c.k8s_name === copy.value.from);
  if (!source) return;
  const hosts = source.hosts.map(replaceText);
  text.value = cleanHosts(
    append ? [...parsedHosts.value, ...hosts] : hosts
  ).join("\n");
  if (append) return;
  for (const { route } of domainRoutes.value) {
    const [lists] = domainsOf(source.k8s_name, route);
    setDomains(
      route,
      replaceDomains(lists, copy.value.find, copy.value.replace),
      lists.length ? "copied" : "none"
    );
  }
  message("已复制,请核对后保存", { type: "success" });
}

function options(routeId: number, block: number) {
  const selected = values.value[routeId]?.[block] ?? [];
  return Array.from(new Set([...parsedHosts.value, ...selected])).map(value => {
    const item = items.value[routeId]?.[value];
    const label = item ? formatDomain(item) : value;
    return {
      value,
      label: parsedHosts.value.includes(value)
        ? label
        : `${label}(不在 hosts 里)`
    };
  });
}

function routeLabel(route: IstioRoute) {
  return `${route.name || describeMatch(route.match_rules)} → ${forwardSummary(route)}`;
}

async function save() {
  if (props.vsId === null) return;
  if (!parsedHosts.value.length) {
    message("至少要有一个 host", { type: "warning" });
    return;
  }
  const domains: Record<number, DomainLists> = {};
  for (const { route, position } of domainRoutes.value) {
    const blocks = (values.value[route.id] ?? []).map(list =>
      list
        .map(v => v.trim())
        .filter(Boolean)
        .map(
          (value): DomainItem =>
            items.value[route.id]?.[value] ?? {
              field: "authority",
              op: "exact",
              value
            }
        )
    );
    const literal = (route.match_rules ?? []).length - blocks.length;
    if (!literal && !blocks.some(block => block.length)) {
      message(`第 ${position} 条规则还没选域名`, { type: "warning" });
      return;
    }
    domains[route.id] = blocks;
  }
  saving.value = true;
  try {
    await saveIstioVSCluster(
      props.vsId,
      props.k8sName,
      parsedHosts.value,
      domains
    );
    message(bound.value ? "已保存,下发后生效" : "已纳入该集群,下发后生效", {
      type: "success"
    });
    emit("saved");
    visible.value = false;
  } finally {
    saving.value = false;
  }
}
</script>

<template>
  <el-dialog
    v-model="visible"
    :title="`${bound ? '编辑 hosts 和域名' : '部署到集群'} · ${k8sName}`"
    width="900px"
    top="6vh"
    :close-on-click-modal="false"
  >
    <div v-loading="loading" class="hosts-body">
      <template v-if="detail">
        <div class="vs-label">{{ detail.namespace }}/{{ detail.name }}</div>
        <el-alert
          v-if="!bound"
          type="info"
          :closable="false"
          show-icon
          class="mb-3"
          title="保存后这个 VirtualService 会纳入该集群:规则、默认路由、gateways 用组标准,hosts 和规则里的域名用下面填的。下发后才会在集群里生效。"
        />
        <div class="copy-row">
          <span class="label">从</span>
          <el-select
            v-model="copy.from"
            placeholder="其他集群"
            clearable
            style="width: 230px"
          >
            <el-option
              v-for="c in copySources"
              :key="c.k8s_name"
              :label="`${c.k8s_name}(${c.bound ? '组标准' : '集群现状'},${c.hosts.length} 个 host)`"
              :value="c.k8s_name"
            />
          </el-select>
          <span class="label">复制,把</span>
          <el-input
            v-model="copy.find"
            placeholder="如 penglai"
            style="width: 110px"
          />
          <span class="label">换成</span>
          <el-input
            v-model="copy.replace"
            placeholder="如 kunlun"
            style="width: 110px"
          />
          <el-button :disabled="!copy.from" @click="copyFrom(false)"
            >复制 hosts 和域名</el-button
          >
          <el-button :disabled="!copy.from" @click="copyFrom(true)"
            >只追加 hosts</el-button
          >
        </div>
        <el-input
          v-model="text"
          type="textarea"
          :rows="8"
          placeholder="每行一个 host(也可以用逗号、空格分隔)"
          class="mt-2 hosts-text"
        />
        <div class="tip">
          共 {{ parsedHosts.length }} 个,保存时自动去掉空行和重复项
        </div>

        <template v-if="domainRoutes.length">
          <div class="section-title">
            规则里的域名条件
            <span class="tip"
              >这些规则按域名区分,各集群分别设置要匹配的域名(从上面的 hosts
              里选)</span
            >
          </div>
          <el-table
            :data="domainRoutes"
            size="small"
            border
            max-height="340"
            :row-key="r => r.route.id"
          >
            <el-table-column label="规则" min-width="260">
              <template #default="{ row }">
                <div>
                  第 {{ row.position }} 条
                  <el-tag
                    v-if="sources[row.route.id] === 'live'"
                    size="small"
                    type="info"
                    >取自集群现状</el-tag
                  >
                  <el-tag
                    v-else-if="sources[row.route.id] === 'copied'"
                    size="small"
                    type="warning"
                    >复制来的,请核对</el-tag
                  >
                  <el-tag
                    v-else-if="sources[row.route.id] === 'none'"
                    size="small"
                    type="danger"
                    >未设置</el-tag
                  >
                </div>
                <div class="sub">{{ routeLabel(row.route) }}</div>
              </template>
            </el-table-column>
            <el-table-column label="本集群的域名" min-width="420">
              <template #default="{ row }">
                <div
                  v-for="(_, block) in values[row.route.id]"
                  :key="block"
                  class="block-row"
                >
                  <span
                    v-if="values[row.route.id].length > 1"
                    class="sub block-no"
                    >条件组 {{ block + 1 }}</span
                  >
                  <el-select
                    v-model="values[row.route.id][block]"
                    multiple
                    filterable
                    allow-create
                    default-first-option
                    size="small"
                    placeholder="选择或输入域名"
                    class="block-select"
                  >
                    <el-option
                      v-for="o in options(row.route.id, block)"
                      :key="o.value"
                      :label="o.label"
                      :value="o.value"
                    />
                  </el-select>
                </div>
              </template>
            </el-table-column>
          </el-table>
        </template>
      </template>
    </div>
    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button
        type="primary"
        :loading="saving"
        :disabled="!detail"
        @click="save"
        >保存</el-button
      >
    </template>
  </el-dialog>
</template>

<style scoped>
.hosts-body {
  min-height: 120px;
}

.vs-label {
  margin-bottom: 12px;
  font-weight: 600;
}

.copy-row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}

.label {
  font-size: 13px;
  color: var(--el-text-color-regular);
}

.hosts-text :deep(textarea) {
  font-family: Consolas, Monaco, monospace;
}

.tip,
.sub {
  margin-top: 4px;
  font-size: 12px;
  font-weight: 400;
  color: var(--el-text-color-secondary);
}

.section-title {
  margin: 14px 0 6px;
  font-weight: 600;
}

.block-row {
  display: flex;
  gap: 6px;
  align-items: center;
}

.block-row + .block-row {
  margin-top: 4px;
}

.block-no {
  flex: none;
  margin-top: 0;
}

.block-select {
  flex: 1;
  min-width: 0;
}
</style>
