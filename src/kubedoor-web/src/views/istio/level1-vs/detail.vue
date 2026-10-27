<script setup lang="ts">
import { computed, h as vnode, onMounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessageBox } from "element-plus";
import { ArrowLeft, Edit, Plus, Warning } from "@element-plus/icons-vue";
import { message } from "@/utils/message";
import { useClusterPermission } from "@/utils/permission";
import {
  type ApplyItem,
  type IstioRoute,
  type VSClusterRow,
  type VSDetail,
  deleteIstioLiveVS,
  deleteIstioRoute,
  deleteIstioVS,
  getIstioVS,
  removeIstioVSCluster,
  reorderIstioRoutes,
  resetIstioVS
} from "@/api/istio";
import RouteDialog from "./components/RouteDialog.vue";
import VSDialog from "./components/VSDialog.vue";
import HostsDialog from "./components/HostsDialog.vue";
import CompareDialog from "./components/CompareDialog.vue";
import ApplyDialog from "./components/ApplyDialog.vue";
import LiveDialog from "./components/LiveDialog.vue";
import RollbackDialog from "./components/RollbackDialog.vue";
import {
  ENV_TYPE_LABEL,
  ENV_TYPE_TAG,
  STATUS_META,
  conflictText,
  describeMatch,
  displayRoute,
  domainBlockCount,
  formatDomain,
  formatTime,
  forwardSummary,
  scopeText,
  toYaml
} from "./utils";

defineOptions({ name: "Level1VirtualServiceDetail" });

const route = useRoute();
const router = useRouter();
const { isAdmin } = useClusterPermission();

const vs = ref<VSDetail | null>(null);
const loading = ref(false);
const keyword = ref("");

const vsId = computed(() => Number(route.params.id));
const boundClusters = computed(() =>
  (vs.value?.clusters ?? []).filter(c => c.bound)
);
/** 规则编辑时服务 / 端口候选取哪个集群:优先已纳入的测试集群 */
const refEnv = computed(() => {
  const list = vs.value?.clusters ?? [];
  const bound = list.filter(c => c.bound);
  return (
    bound.find(c => c.env_type === "test")?.k8s_name ??
    bound[0]?.k8s_name ??
    list[0]?.k8s_name ??
    ""
  );
});

const filteredRoutes = computed(() => {
  const kw = keyword.value.trim().toLowerCase();
  const routes = vs.value?.routes ?? [];
  if (!kw) return routes;
  return routes.filter(r =>
    [r.name, JSON.stringify(r.match_rules), JSON.stringify(r.forward_detail)]
      .join(" ")
      .toLowerCase()
      .includes(kw)
  );
});

const defaultRoute = computed(() => {
  const v = vs.value;
  if (!v || (!v.df_forward_type && !v.df_extra)) return "";
  const entry: Record<string, any> = {};
  if (v.df_forward_type) entry[v.df_forward_type] = v.df_forward_detail;
  if (v.df_forward_timeout) entry.timeout = v.df_forward_timeout;
  return toYaml({ ...entry, ...(v.df_extra ?? {}) });
});

async function load() {
  loading.value = true;
  try {
    const res = await getIstioVS(vsId.value);
    vs.value = res.data;
  } finally {
    loading.value = false;
  }
}

function goBack() {
  router.push({
    path: "/istio/level1-vs",
    query: { group: String(vs.value?.group_id ?? route.query.group ?? "") }
  });
}

/** 规则在哪些已纳入的集群还没设置域名 */
function missingClusters(r: IstioRoute) {
  if (!domainBlockCount(r.match_rules)) return [];
  return boundClusters.value
    .filter(c => !r.domains[c.k8s_name])
    .map(c => c.k8s_name);
}

// ---------------------------------------------------------------- 规则
const routeDialog = ref({ visible: false, route: null as IstioRoute | null });

async function removeRoute(r: IstioRoute) {
  await ElMessageBox.confirm(
    `删除规则「${r.name || describeMatch(r.match_rules)}」?下发后集群里才会删除。`,
    "删除规则",
    { type: "warning", confirmButtonText: "删除" }
  );
  await deleteIstioRoute(r.id);
  message("已删除,下发后生效", { type: "success" });
  await load();
}

async function reorder() {
  await reorderIstioRoutes(vsId.value);
  message("优先级已整理为 10、20、30……", { type: "success" });
  await load();
}

// ---------------------------------------------------------------- 集群
const vsDialog = ref(false);
const compare = ref({ visible: false, left: "", right: "" });
const applyState = ref({ visible: false, items: [] as ApplyItem[] });
const hostsState = ref({ visible: false, k8sName: "" });
const liveState = ref({ visible: false, cluster: null as VSClusterRow | null });
const rollbackState = ref({
  visible: false,
  cluster: null as VSClusterRow | null
});

function openCompare(c: VSClusterRow) {
  compare.value = {
    visible: true,
    left: c.bound ? `standard@${c.k8s_name}` : "standard",
    right: `cluster@${c.k8s_name}`
  };
}

function openApply(clusters: VSClusterRow[]) {
  if (!clusters.length) {
    message("还没有纳入任何集群", { type: "info" });
    return;
  }
  applyState.value = {
    visible: true,
    items: clusters.map(c => ({ vs_id: vsId.value, k8s_name: c.k8s_name }))
  };
}

function openHosts(c: VSClusterRow) {
  hostsState.value = { visible: true, k8sName: c.k8s_name };
}

function openLive(c: VSClusterRow) {
  liveState.value = { visible: true, cluster: c };
}

function openRollback(c: VSClusterRow) {
  rollbackState.value = { visible: true, cluster: c };
}

async function resetFrom(c: VSClusterRow) {
  await ElMessageBox.confirm(
    `用 ${c.k8s_name} 的现状替换组标准的规则、默认路由和 gateways?各集群 hosts 不变,其他集群规则里的域名也保持各自的;其他集群下次下发时会改成这个版本。`,
    "以集群现状更新组标准",
    { type: "warning" }
  );
  await resetIstioVS(vsId.value, c.k8s_name);
  message("组标准已更新,下发后生效", { type: "success" });
  await load();
}

async function unbind(c: VSClusterRow) {
  await ElMessageBox.confirm(
    `不再部署到 ${c.k8s_name}?只删除花折里的部署关系和该集群的 hosts,集群里的 VirtualService 不受影响(如需删除请到集群处理)。`,
    "不再部署到该集群",
    { type: "warning" }
  );
  await removeIstioVSCluster(vsId.value, c.k8s_name);
  message("已移出", { type: "success" });
  await load();
}

/** 删除集群里的 VirtualService;本组标准部署到了该集群的,同时不再部署 */
async function deleteLive(c: VSClusterRow) {
  const v = vs.value;
  if (!v) return;
  const lines = [
    vnode(
      "p",
      null,
      `从集群 ${c.k8s_name} 删除 ${v.namespace}/${v.name}?删除后立即生效,依赖它的域名路由会失效。`
    ),
    c.env_type === "prod"
      ? vnode(
          "p",
          { style: "color: var(--el-color-danger); font-weight: 600" },
          "这是生产集群。"
        )
      : null,
    c.bound
      ? vnode(
          "p",
          null,
          "组标准部署到了该集群:同时不再部署(删除花折里该集群的 hosts 和域名),组标准和其他集群不受影响。"
        )
      : null,
    vnode(
      "p",
      null,
      "删除前会先备份,可以用「回退」恢复;删除前的内容也会记在审计日志(istio.delete)里。"
    )
  ];
  await ElMessageBox.confirm(
    vnode("div", null, lines),
    "删除集群里的 VirtualService",
    {
      type: "warning",
      confirmButtonText: "删除",
      confirmButtonClass: "el-button--danger"
    }
  );
  const res = await deleteIstioLiveVS(
    v.group_id,
    c.k8s_name,
    v.namespace,
    v.name
  );
  message(res.msg || "已删除", { type: "success" });
  await load();
}

async function removeVS() {
  if (!vs.value) return;
  await ElMessageBox.confirm(
    `删除 ${vs.value.namespace}/${vs.value.name} 的组标准?各集群里的 VirtualService 不受影响(如需删除请到集群处理)。`,
    "删除组标准",
    { type: "warning", confirmButtonText: "删除" }
  );
  await deleteIstioVS(vsId.value);
  message("组标准已删除", { type: "success" });
  goBack();
}

onMounted(load);
</script>

<template>
  <div v-loading="loading" class="vs-detail">
    <div class="head">
      <el-button :icon="ArrowLeft" @click="goBack">返回</el-button>
      <template v-if="vs">
        <div class="title">
          <span class="ns">{{ vs.namespace }}/</span>{{ vs.name }}
          <el-tag size="small" class="ml-2">组 {{ vs.group.name }}</el-tag>
        </div>
        <span class="sub">
          组标准最近修改:{{ vs.updated_by || "-" }}
          {{ formatTime(vs.updated_at) }}
        </span>
        <div class="spacer" />
        <template v-if="isAdmin">
          <el-button type="primary" @click="openApply(boundClusters)"
            >下发到集群…</el-button
          >
          <el-button type="danger" plain @click="removeVS"
            >删除组标准</el-button
          >
        </template>
      </template>
    </div>

    <template v-if="vs">
      <el-alert
        v-if="vs.lint.length"
        type="warning"
        :closable="false"
        show-icon
        class="mb-3"
      >
        <template #title
          >规范问题(用了重写就要有名称,没用重写就不要名称;域名条件里的域名要在该集群的
          hosts 里)</template
        >
        <div v-for="item in vs.lint" :key="item">{{ item }}</div>
      </el-alert>
      <el-alert
        v-if="vs.domain_issues.length"
        type="warning"
        :closable="false"
        show-icon
        class="mb-3"
      >
        <template #title
          >域名重复(同一个集群里,多个 VirtualService 的 hosts 不能重复,同一个
          VirtualService
          里不同规则的域名也不能重复;按要下发的内容检查,集群里其他组的、被忽略的
          VirtualService 也算)</template
        >
        <div v-for="item in vs.domain_issues" :key="item">{{ item }}</div>
      </el-alert>
      <el-alert
        v-if="!vs.scope.visible"
        type="warning"
        :closable="false"
        show-icon
        class="mb-3"
        :title="`这个 VS 在组「${vs.group.name}」里已忽略(${scopeText(vs.scope)}):列表里不显示,也不能下发;在列表页「查看忽略」里取消忽略后恢复`"
      />
      <el-alert
        v-if="vs.conflicts.length"
        type="error"
        :closable="false"
        show-icon
        class="mb-3"
        :title="`同一个 VS 只能属于一个组:也在${conflictText(vs.conflicts)}里,下发会被拒绝;请在不该管它的组里忽略它`"
      />

      <el-card shadow="never" class="mb-3">
        <template #header>
          <div class="card-head">
            <b>集群与 Hosts</b>
            <span class="sub"
              >hosts 和规则里的域名各集群独立;规则其余部分、默认路由、gateways
              用组标准</span
            >
          </div>
        </template>
        <el-table :data="vs.clusters" border size="small">
          <el-table-column label="集群" min-width="200">
            <template #default="{ row }">
              <el-tag size="small" :type="ENV_TYPE_TAG[row.env_type]">{{
                ENV_TYPE_LABEL[row.env_type]
              }}</el-tag>
              <b class="ml-1">{{ row.k8s_name }}</b>
              <span v-if="row.remark" class="sub ml-1">{{ row.remark }}</span>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="120">
            <template #default="{ row }">
              <el-tooltip placement="top">
                <template #content>
                  <div>{{ STATUS_META[row.status].desc }}</div>
                  <div v-for="item in row.missing_domains" :key="item">
                    · {{ item }}
                  </div>
                </template>
                <el-tag size="small" :type="STATUS_META[row.status].type">{{
                  row.status === "none"
                    ? "不部署"
                    : STATUS_META[row.status].label
                }}</el-tag>
              </el-tooltip>
            </template>
          </el-table-column>
          <el-table-column label="hosts" min-width="320">
            <template #default="{ row }">
              <div :class="{ dim: !row.bound }">
                <div v-for="h in row.hosts" :key="h" class="host">{{ h }}</div>
                <span v-if="!row.hosts.length" class="sub">—</span>
                <div v-if="!row.bound && row.hosts.length" class="sub">
                  (集群现状里的,未纳入)
                </div>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="最近下发" width="170">
            <template #default="{ row }">
              <template v-if="row.applied_at">
                <div>{{ row.applied_by }}</div>
                <div class="sub">{{ formatTime(row.applied_at) }}</div>
              </template>
              <span v-else class="sub">-</span>
              <div v-if="row.rolled_back" class="rolled-back">
                <div>{{ row.rolled_back.by }} 回退了</div>
                <div class="sub">{{ formatTime(row.rolled_back.at) }}</div>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="430">
            <template #default="{ row }">
              <el-button
                v-if="row.in_cluster"
                link
                type="primary"
                @click="openLive(row)"
                >查看现状</el-button
              >
              <el-button link type="primary" @click="openCompare(row)"
                >对比</el-button
              >
              <template v-if="isAdmin">
                <el-button link type="primary" @click="openHosts(row)">{{
                  row.bound ? "编辑 hosts 和域名" : "部署到此集群"
                }}</el-button>
                <el-button
                  v-if="row.bound"
                  link
                  type="warning"
                  @click="openApply([row])"
                  >下发</el-button
                >
                <el-tooltip
                  v-if="row.bound || row.in_cluster || row.backup_count"
                  :disabled="row.backup_count > 0"
                  content="还没有备份:花折下发、回退、删除这个 VirtualService 之前才会备份"
                  placement="top"
                >
                  <span class="op">
                    <el-button
                      link
                      type="warning"
                      :disabled="!row.backup_count"
                      @click="openRollback(row)"
                      >回退</el-button
                    >
                  </span>
                </el-tooltip>
                <el-dropdown
                  v-if="row.in_cluster || row.bound"
                  trigger="click"
                  class="op"
                >
                  <el-button link type="info">更多</el-button>
                  <template #dropdown>
                    <el-dropdown-menu>
                      <el-dropdown-item
                        v-if="row.in_cluster"
                        @click="resetFrom(row)"
                      >
                        以此集群现状更新组标准
                      </el-dropdown-item>
                      <el-dropdown-item v-if="row.bound" @click="unbind(row)">
                        不再部署到此集群
                      </el-dropdown-item>
                      <el-dropdown-item
                        v-if="row.in_cluster"
                        divided
                        @click="deleteLive(row)"
                      >
                        <span class="danger-text">从集群删除</span>
                      </el-dropdown-item>
                    </el-dropdown-menu>
                  </template>
                </el-dropdown>
              </template>
            </template>
          </el-table-column>
        </el-table>
      </el-card>

      <el-card shadow="never" class="mb-3">
        <template #header>
          <div class="card-head">
            <b>共用配置</b>
            <span class="sub">全组一致,修改后需要下发到各集群</span>
            <div class="spacer" />
            <el-button
              v-if="isAdmin"
              size="small"
              :icon="Edit"
              @click="vsDialog = true"
              >编辑</el-button
            >
          </div>
        </template>
        <el-descriptions :column="1" border size="small">
          <el-descriptions-item label="gateways">
            <el-tag
              v-for="g in vs.gateways"
              :key="g"
              size="small"
              class="mr-1"
              >{{ g }}</el-tag
            >
            <span v-if="!vs.gateways.length" class="sub">无</span>
          </el-descriptions-item>
          <el-descriptions-item label="默认路由">
            <pre v-if="defaultRoute" class="yaml">{{ defaultRoute }}</pre>
            <span v-else class="sub">无(没有匹配上任何规则的请求返回 404)</span>
          </el-descriptions-item>
          <el-descriptions-item v-if="vs.extra_spec" label="spec 其他字段">
            <pre class="yaml">{{ toYaml(vs.extra_spec) }}</pre>
            <div class="sub">表单不支持编辑,下发时原样保留</div>
          </el-descriptions-item>
        </el-descriptions>
      </el-card>

      <el-card shadow="never">
        <template #header>
          <div class="card-head">
            <b>HTTP 规则({{ vs.routes.length }})</b>
            <span class="sub">按优先级从小到大匹配,默认路由在最后</span>
            <div class="spacer" />
            <el-input
              v-model="keyword"
              size="small"
              placeholder="搜索规则"
              clearable
              style="width: 200px"
            />
            <template v-if="isAdmin">
              <el-button size="small" @click="reorder">整理优先级</el-button>
              <el-button
                size="small"
                type="primary"
                :icon="Plus"
                @click="routeDialog = { visible: true, route: null }"
                >新增规则</el-button
              >
            </template>
          </div>
        </template>
        <el-table
          :data="filteredRoutes"
          border
          size="small"
          empty-text="还没有规则"
        >
          <el-table-column type="expand" width="36">
            <template #default="{ row }">
              <div class="route-expand">
                <template v-if="domainBlockCount(row.match_rules)">
                  <div class="sub">各集群匹配的域名(多个域名之间是「或」)</div>
                  <div
                    v-for="c in boundClusters"
                    :key="c.k8s_name"
                    class="domain-line"
                  >
                    <b class="domain-cluster">{{ c.k8s_name }}</b>
                    <template v-if="row.domains[c.k8s_name]">
                      <span
                        v-for="(block, b) in row.domains[c.k8s_name]"
                        :key="b"
                        class="domain-block"
                      >
                        <span
                          v-if="row.domains[c.k8s_name].length > 1"
                          class="sub"
                          >条件组 {{ b + 1 }}:</span
                        >
                        <el-tag
                          v-for="d in block"
                          :key="d.value"
                          size="small"
                          class="mr-1"
                          >{{ formatDomain(d) }}</el-tag
                        >
                        <span v-if="!block.length" class="sub">无</span>
                      </span>
                    </template>
                    <el-tag v-else size="small" type="danger">未设置</el-tag>
                  </div>
                </template>
                <div v-else class="sub">没有域名条件,各集群的匹配条件相同</div>
                <pre class="yaml mt-2">{{ toYaml(displayRoute(row)) }}</pre>
              </div>
            </template>
          </el-table-column>
          <el-table-column
            prop="priority"
            label="优先级"
            width="70"
            align="center"
          />
          <el-table-column label="名称" width="200" show-overflow-tooltip>
            <template #default="{ row }">
              <span>{{ row.name || "-" }}</span>
              <el-tooltip v-if="row.issue" :content="row.issue" placement="top">
                <el-icon class="warn ml-1"><Warning /></el-icon>
              </el-tooltip>
            </template>
          </el-table-column>
          <el-table-column
            label="匹配条件"
            min-width="260"
            show-overflow-tooltip
          >
            <template #default="{ row }">
              {{ describeMatch(row.match_rules) }}
              <el-tag
                v-if="missingClusters(row).length"
                size="small"
                type="danger"
                class="ml-1"
                >缺少 {{ missingClusters(row).join("、") }} 的域名</el-tag
              >
            </template>
          </el-table-column>
          <el-table-column label="重写" width="100" show-overflow-tooltip>
            <template #default="{ row }">{{
              row.rewrite_rules?.uri ?? (row.rewrite_rules ? "见详情" : "-")
            }}</template>
          </el-table-column>
          <el-table-column label="转发" min-width="280" show-overflow-tooltip>
            <template #default="{ row }">{{ forwardSummary(row) }}</template>
          </el-table-column>
          <el-table-column prop="timeout" label="超时" width="70" />
          <el-table-column label="其他字段" width="90" align="center">
            <template #default="{ row }">
              <el-tooltip v-if="row.extra" placement="top">
                <template #content>
                  <pre class="tip-yaml">{{ toYaml(row.extra) }}</pre>
                </template>
                <el-tag size="small" type="info">有</el-tag>
              </el-tooltip>
              <span v-else class="sub">-</span>
            </template>
          </el-table-column>
          <el-table-column
            v-if="isAdmin"
            label="操作"
            width="110"
            fixed="right"
          >
            <template #default="{ row }">
              <el-button
                link
                type="primary"
                @click="routeDialog = { visible: true, route: row }"
                >编辑</el-button
              >
              <el-button link type="danger" @click="removeRoute(row)"
                >删除</el-button
              >
            </template>
          </el-table-column>
        </el-table>
      </el-card>

      <RouteDialog
        v-model="routeDialog.visible"
        :vs-id="vs.id"
        :route="routeDialog.route"
        :ref-env="refEnv"
        :vs-namespace="vs.namespace"
        :clusters="boundClusters"
        @saved="load"
      />
      <VSDialog
        v-model="vsDialog"
        :group-id="vs.group_id"
        :clusters="vs.clusters"
        :vs="vs"
        :gateway-options="vs.gateways"
        @saved="load"
      />
      <HostsDialog
        v-model="hostsState.visible"
        :vs-id="vs.id"
        :k8s-name="hostsState.k8sName"
        @saved="load"
      />
      <LiveDialog
        v-if="liveState.cluster"
        v-model="liveState.visible"
        :group-id="vs.group_id"
        :k8s-name="liveState.cluster.k8s_name"
        :env-type="liveState.cluster.env_type"
        :namespace="vs.namespace"
        :name="vs.name"
      />
      <RollbackDialog
        v-if="rollbackState.cluster"
        v-model="rollbackState.visible"
        :group-id="vs.group_id"
        :k8s-name="rollbackState.cluster.k8s_name"
        :env-type="rollbackState.cluster.env_type"
        :namespace="vs.namespace"
        :name="vs.name"
        :bound="rollbackState.cluster.bound"
        @done="load"
      />
      <CompareDialog
        v-model="compare.visible"
        :group-id="vs.group_id"
        :namespace="vs.namespace"
        :name="vs.name"
        :clusters="vs.clusters"
        :vs-id="vs.id"
        :bound-clusters="boundClusters.map(c => c.k8s_name)"
        :is-admin="isAdmin"
        :left="compare.left"
        :right="compare.right"
        @changed="load"
        @apply="
          k8s => openApply(vs?.clusters.filter(c => c.k8s_name === k8s) ?? [])
        "
      />
      <ApplyDialog
        v-model="applyState.visible"
        :items="applyState.items"
        :clusters="vs.clusters"
        @done="load"
      />
    </template>
  </div>
</template>

<style scoped>
.vs-detail {
  padding: 4px;
}

.head {
  display: flex;
  gap: 12px;
  align-items: center;
  margin-bottom: 12px;
}

.title {
  font-size: 18px;
  font-weight: 600;
}

.ns {
  font-weight: 400;
  color: var(--el-text-color-secondary);
}

.spacer {
  flex: 1;
}

.card-head {
  display: flex;
  gap: 10px;
  align-items: center;
}

.sub {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.host {
  font-family: Consolas, Monaco, monospace;
  font-size: 12px;
  line-height: 18px;
}

.dim {
  color: var(--el-text-color-secondary);
}

.yaml {
  max-height: 260px;
  padding: 6px 8px;
  margin: 0;
  overflow: auto;
  font-size: 12px;
  background: var(--el-fill-color-light);
  border-radius: 4px;
}

.tip-yaml {
  margin: 0;
  font-size: 12px;
}

.route-expand {
  padding: 4px 16px 4px 48px;
}

.domain-line {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  align-items: center;
  margin-top: 4px;
}

.domain-cluster {
  width: 200px;
  font-size: 12px;
}

.domain-block {
  margin-right: 8px;
}

.danger-text {
  color: var(--el-color-danger);
}

/* 操作列里和按钮并排的下拉菜单、外面包了一层提示的按钮:间距和按钮之间一样;
   el-dropdown 自带 vertical-align: top,不改会比旁边的按钮偏上 */
.op {
  display: inline-flex;
  margin-left: 12px;
  vertical-align: middle;
}

.rolled-back {
  margin-top: 4px;
  color: var(--el-color-warning);
}

.warn {
  color: var(--el-color-warning);
  vertical-align: middle;
}
</style>
