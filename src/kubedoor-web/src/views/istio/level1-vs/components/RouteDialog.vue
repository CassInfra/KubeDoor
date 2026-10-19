<script setup lang="ts">
import { computed, reactive, ref, watch } from "vue";
import { ElMessageBox } from "element-plus";
import { Delete, Plus, Refresh } from "@element-plus/icons-vue";
import { message } from "@/utils/message";
import {
  type DomainItem,
  type DomainLists,
  type EnvType,
  type IstioRoute,
  type RoutePayload,
  DOMAIN_KEY,
  createIstioRoute,
  getServiceFirstPort,
  updateIstioRoute
} from "@/api/istio";
import { getPromNamespace, getPromServices } from "@/api/monit";
import {
  ENV_TYPE_LABEL,
  ENV_TYPE_TAG,
  formatDomain,
  suggestReplace,
  suggestRouteName,
  toYaml
} from "../utils";

defineOptions({ name: "IstioRouteDialog" });

interface ClusterOption {
  k8s_name: string;
  env_type: EnvType;
  hosts: string[];
}

/** 新增 / 编辑组标准的一条 http 规则。表单不认识的字段(其他匹配条件、目标字段等)编辑时原样保留;
 *  域名条件按集群设置:每个已纳入的集群从自己的 hosts 里选,下发时每个域名展开成一个匹配条目 */
const props = defineProps<{
  vsId: number | null;
  /** 编辑的规则;为空是新增 */
  route: IstioRoute | null;
  /** 服务 / 命名空间 / 端口候选从哪个集群取 */
  refEnv: string;
  /** VS 所在命名空间:目标 host 只写了服务名时按这个补全 */
  vsNamespace: string;
  /** 已纳入该 VS 的集群和各自的 hosts */
  clusters: ClusterOption[];
}>();
const emit = defineEmits<{ (e: "saved"): void }>();
const visible = defineModel<boolean>({ default: false });

const DEFAULT_HEADERS = '{"request": {"set": {"X-Forwarded-Proto": "https"}}}';
const TIMEOUT_PATTERN = /^\d+(\.\d+)?(ns|us|µs|ms|s|m|h)$/;

interface DomainForm {
  enabled: boolean;
  /** 集群 → 选中的域名 */
  values: Record<string, string[]>;
  /** 集群 → 域名 → 原来的写法(authority / host 头、exact / prefix / regex),保存时原样带回 */
  items: Record<string, Record<string, DomainItem>>;
  keyword: string;
}

interface MatchForm {
  uri: { enabled: boolean; type: string; value: string };
  domain: DomainForm;
  headers: {
    enabled: boolean;
    items: { key: string; type: string; value: string }[];
  };
  rest: Record<string, any>;
}

interface TargetForm {
  service: string;
  namespace: string;
  /** host 不是「服务.命名空间」格式时原样保留 */
  rawHost: string;
  port: number | null;
  subset: { enabled: boolean; value: string };
  weight: { enabled: boolean; value: number };
  headers: { enabled: boolean; value: string };
  rest: Record<string, any>;
  destRest: Record<string, any>;
}

const emptyDomain = (): DomainForm => ({
  enabled: false,
  values: Object.fromEntries(props.clusters.map(c => [c.k8s_name, []])),
  items: Object.fromEntries(props.clusters.map(c => [c.k8s_name, {}])),
  keyword: ""
});

const emptyMatch = (): MatchForm => ({
  uri: { enabled: true, type: "prefix", value: "" },
  domain: emptyDomain(),
  headers: { enabled: false, items: [{ key: "", type: "exact", value: "" }] },
  rest: {}
});

const emptyTarget = (): TargetForm => ({
  service: "",
  namespace: "",
  rawHost: "",
  port: null,
  subset: { enabled: false, value: "" },
  weight: { enabled: false, value: 100 },
  headers: { enabled: false, value: DEFAULT_HEADERS },
  rest: {},
  destRest: {}
});

const form = reactive({
  name: "",
  priority: null as number | null,
  timeout: "",
  match_rules: [emptyMatch()],
  rewrite: { enabled: false, uri: "/", rest: {} as Record<string, any> },
  forward_type: "route" as "route" | "delegate",
  forward_detail: [emptyTarget()],
  delegate_name: "",
  delegate_namespace: ""
});
const saving = ref(false);
const namespaceOptions = ref<string[]>([]);
const serviceOptions = ref<string[]>([]);
const copy = reactive({ from: "", to: "", find: "", replace: "" });

const extraYaml = computed(() =>
  props.route?.extra ? toYaml(props.route.extra) : ""
);
const hasDomainBlock = computed(() =>
  form.match_rules.some(rule => rule.domain.enabled)
);

function omit(obj: Record<string, any> | null | undefined, keys: string[]) {
  const rest: Record<string, any> = {};
  for (const [k, v] of Object.entries(obj || {})) {
    if (!keys.includes(k)) rest[k] = v;
  }
  return rest;
}

/** 匹配条目 → 表单;domains 是这个域名块在各集群的域名(不是域名块时为 null) */
function parseMatch(
  cond: Record<string, any>,
  domains: Record<string, DomainItem[]> | null
): MatchForm {
  const item = emptyMatch();
  item.uri.enabled = false;
  const uriOp = Object.keys(cond.uri || {})[0];
  if (uriOp)
    Object.assign(item.uri, {
      enabled: true,
      type: uriOp,
      value: cond.uri[uriOp]
    });
  if (domains) {
    item.domain.enabled = true;
    for (const [k8s, list] of Object.entries(domains)) {
      item.domain.values[k8s] = list.map(d => d.value);
      item.domain.items[k8s] = Object.fromEntries(list.map(d => [d.value, d]));
    }
  }
  const headers = Object.entries(cond.headers || {});
  if (headers.length) {
    item.headers.enabled = true;
    item.headers.items = headers.map(([key, spec]: [string, any]) => {
      const op = Object.keys(spec || {})[0] || "exact";
      return { key, type: op, value: spec?.[op] ?? "" };
    });
  }
  // 不是按集群设置的 authority(很少见)放在其他条件里原样保留
  item.rest = omit(cond, ["uri", "headers", DOMAIN_KEY]);
  return item;
}

function parseTarget(entry: Record<string, any>): TargetForm {
  const item = emptyTarget();
  const dest = entry.destination || {};
  const host = String(dest.host || "");
  const short = host.replace(/\.svc\.cluster\.local$/, "");
  const parts = short.split(".");
  if (host && parts.length === 1) {
    item.service = parts[0];
    item.namespace = props.vsNamespace;
  } else if (parts.length === 2) {
    [item.service, item.namespace] = parts;
  } else {
    item.rawHost = host;
  }
  item.port = dest.port?.number ?? null;
  item.subset = { enabled: !!dest.subset, value: dest.subset || "" };
  item.weight = {
    enabled: entry.weight !== undefined,
    value: entry.weight ?? 100
  };
  item.headers = {
    enabled: !!entry.headers,
    value: entry.headers ? JSON.stringify(entry.headers) : DEFAULT_HEADERS
  };
  item.rest = omit(entry, ["destination", "weight", "headers"]);
  item.destRest = omit(dest, ["host", "port", "subset"]);
  return item;
}

watch(
  visible,
  open => {
    if (!open) return;
    const r = props.route;
    form.name = r?.name ?? "";
    form.priority = r?.priority ?? null;
    form.timeout = r?.timeout ?? "";
    let block = 0;
    form.match_rules = r?.match_rules?.length
      ? r.match_rules.map(cond => {
          if (!cond?.[DOMAIN_KEY]) return parseMatch(cond, null);
          const index = block++;
          const domains = Object.fromEntries(
            props.clusters.map(c => [
              c.k8s_name,
              r.domains?.[c.k8s_name]?.[index] ?? []
            ])
          );
          return parseMatch(cond, domains);
        })
      : [emptyMatch()];
    form.rewrite = {
      enabled: !!r?.rewrite_rules,
      uri: r?.rewrite_rules?.uri ?? "/",
      rest: omit(r?.rewrite_rules, ["uri"])
    };
    form.forward_type = r?.forward_type === "delegate" ? "delegate" : "route";
    form.forward_detail =
      form.forward_type === "route" &&
      Array.isArray(r?.forward_detail) &&
      r.forward_detail.length
        ? r.forward_detail.map(parseTarget)
        : [emptyTarget()];
    form.delegate_name =
      r?.forward_type === "delegate" ? (r.forward_detail?.name ?? "") : "";
    form.delegate_namespace =
      r?.forward_type === "delegate" ? (r.forward_detail?.namespace ?? "") : "";
    Object.assign(copy, { from: "", to: "", find: "", replace: "" });
    if (!namespaceOptions.value.length) loadNamespaces();
  },
  { immediate: true }
);

// ---------------------------------------------------------------- 域名(按集群)
function domainOptions(rule: MatchForm, c: ClusterOption) {
  const selected = rule.domain.values[c.k8s_name] ?? [];
  const items = rule.domain.items[c.k8s_name] ?? {};
  return Array.from(new Set([...c.hosts, ...selected])).map(value => {
    const item = items[value];
    const label = item ? formatDomain(item) : value;
    return {
      value,
      label: c.hosts.includes(value) ? label : `${label}(不在 hosts 里)`
    };
  });
}

/** 各集群从自己的 hosts 里选出包含关键字的域名 */
function pickByKeyword(rule: MatchForm) {
  const keyword = rule.domain.keyword.trim();
  if (!keyword) {
    message("请先输入关键字,如服务名", { type: "warning" });
    return;
  }
  const none: string[] = [];
  for (const c of props.clusters) {
    const found = c.hosts.filter(h => h.includes(keyword));
    rule.domain.values[c.k8s_name] = found;
    if (!found.length) none.push(c.k8s_name);
  }
  if (none.length) {
    message(`${none.join("、")} 的 hosts 里没有包含「${keyword}」的域名`, {
      type: "warning"
    });
  }
}

watch(
  () => [copy.from, copy.to],
  ([from, to]) => {
    if (from && to) Object.assign(copy, suggestReplace(from, to));
  }
);

/** 把一个集群各条件的域名复制到另一个集群,复制时把集群标识换掉(如 penglai → kunlun) */
function copyDomains() {
  for (const rule of form.match_rules) {
    if (!rule.domain.enabled) continue;
    const items = rule.domain.items[copy.from] ?? {};
    const mapped = (rule.domain.values[copy.from] ?? []).map(value => {
      const source = items[value] ?? { field: "authority", op: "exact", value };
      const next = copy.find
        ? value.split(copy.find).join(copy.replace)
        : value;
      return { ...source, value: next } as DomainItem;
    });
    rule.domain.values[copy.to] = mapped.map(item => item.value);
    rule.domain.items[copy.to] = Object.fromEntries(
      mapped.map(item => [item.value, item])
    );
  }
  message(`已复制到 ${copy.to},请核对`, { type: "success" });
}

// ---------------------------------------------------------------- 名称跟着重写走
function buildMatch() {
  const match: Record<string, any>[] = [];
  const domains: Record<string, DomainLists> = Object.fromEntries(
    props.clusters.map(c => [c.k8s_name, []])
  );
  for (const rule of form.match_rules) {
    const cond: Record<string, any> = { ...rule.rest };
    if (rule.uri.enabled && rule.uri.value.trim()) {
      cond.uri = { [rule.uri.type]: rule.uri.value.trim() };
    }
    if (rule.headers.enabled) {
      const headers: Record<string, any> = {};
      for (const h of rule.headers.items) {
        if (h.key.trim() && h.value.trim())
          headers[h.key.trim()] = { [h.type]: h.value.trim() };
      }
      if (Object.keys(headers).length) cond.headers = headers;
    }
    if (rule.domain.enabled) {
      cond[DOMAIN_KEY] = true;
      for (const c of props.clusters) {
        const items = rule.domain.items[c.k8s_name] ?? {};
        const values = (rule.domain.values[c.k8s_name] ?? [])
          .map(v => v.trim())
          .filter(Boolean);
        domains[c.k8s_name].push(
          values.map(
            value => items[value] ?? { field: "authority", op: "exact", value }
          )
        );
      }
    }
    if (Object.keys(cond).length) match.push(cond);
  }
  return { match, domains: hasDomainBlock.value ? domains : {} };
}

const suggestedName = computed(() => suggestRouteName(buildMatch().match));

function onRewriteToggle(value: unknown) {
  if (value && !form.name) form.name = suggestedName.value;
  if (!value) form.name = "";
}

// ---------------------------------------------------------------- 服务候选
async function loadNamespaces() {
  if (!props.refEnv) return;
  try {
    const res: any = await getPromNamespace(props.refEnv);
    namespaceOptions.value = Array.isArray(res?.data) ? res.data : [];
  } catch {
    namespaceOptions.value = [];
  }
}

async function loadServices(namespace: string) {
  if (!props.refEnv || !namespace) {
    serviceOptions.value = [];
    return;
  }
  try {
    const res: any = await getPromServices(props.refEnv, namespace);
    serviceOptions.value = Array.isArray(res?.data) ? res.data : [];
  } catch {
    serviceOptions.value = [];
  }
}

async function refreshPort(target: TargetForm) {
  if (!target.service || !target.namespace) {
    message("请先填写服务名和命名空间", { type: "warning" });
    return;
  }
  if (!props.refEnv) {
    message("没有可用的集群来查询端口", { type: "warning" });
    return;
  }
  const res: any = await getServiceFirstPort(
    props.refEnv,
    target.namespace,
    target.service
  );
  if (res?.data?.first_port) {
    target.port = res.data.first_port;
    message(`已获取端口 ${res.data.first_port}(来自 ${props.refEnv})`, {
      type: "success"
    });
  } else {
    message("未能获取到端口号", { type: "error" });
  }
}

// ---------------------------------------------------------------- 提交
function buildPayload(): RoutePayload | string {
  const { match, domains } = buildMatch();
  if (!match.length)
    return "请至少填写一条有效的匹配条件(URI / 域名 / Headers)";
  const literal = match.filter(cond => !cond[DOMAIN_KEY]).length;
  for (const [k8s, lists] of Object.entries(domains)) {
    if (!literal && !lists.some(items => items.length)) {
      return `集群 ${k8s} 一个域名都没选:这条规则在该集群会匹配所有请求`;
    }
  }
  const timeout = form.timeout.trim();
  if (timeout && !TIMEOUT_PATTERN.test(timeout)) {
    return "超时必须带单位,如 10s、1m、500ms,不能是纯数字";
  }
  let rewrite: Record<string, any> | null = null;
  if (form.rewrite.enabled) {
    if (!form.rewrite.uri.trim()) return "启用重写时请填写重写后的 URI";
    rewrite = { ...form.rewrite.rest, uri: form.rewrite.uri.trim() };
  }
  const name = form.name.trim();
  if (rewrite && !name)
    return "启用重写时必须填写名称(访问日志里用它展示原始前缀)";

  let detail: any;
  if (form.forward_type === "route") {
    detail = [];
    for (const [index, t] of form.forward_detail.entries()) {
      const host =
        t.rawHost.trim() ||
        (t.service.trim() && t.namespace.trim()
          ? `${t.service.trim()}.${t.namespace.trim()}.svc.cluster.local`
          : "");
      const empty = !host && (t.port === null || t.port === undefined);
      if (empty) continue;
      if (!host || !t.port)
        return `转发目标 ${index + 1} 不完整:需要服务名、命名空间和端口`;
      const destination: Record<string, any> = {
        ...t.destRest,
        host,
        port: { number: t.port }
      };
      if (t.subset.enabled && t.subset.value.trim())
        destination.subset = t.subset.value.trim();
      const item: Record<string, any> = { ...t.rest, destination };
      if (t.weight.enabled) item.weight = t.weight.value;
      if (t.headers.enabled && t.headers.value.trim()) {
        try {
          item.headers = JSON.parse(t.headers.value);
        } catch {
          return `转发目标 ${index + 1} 的 Headers 不是合法的 JSON`;
        }
      }
      detail.push(item);
    }
    if (!detail.length) return "请至少完整填写一个转发目标";
  } else {
    if (!form.delegate_name.trim() || !form.delegate_namespace.trim()) {
      return "委托模式下请填写下级 VS 的名称和命名空间";
    }
    detail = {
      name: form.delegate_name.trim(),
      namespace: form.delegate_namespace.trim()
    };
  }
  return {
    name: rewrite ? name : null,
    priority: form.priority || null,
    match_rules: match,
    rewrite_rules: rewrite,
    forward_type: form.forward_type,
    forward_detail: detail,
    timeout: timeout || null,
    domains
  };
}

/** 有域名的条件里,某个集群一个都没选:这个条件在该集群不生效 */
function emptyDomainBlocks() {
  const gaps: string[] = [];
  form.match_rules.forEach((rule, index) => {
    if (!rule.domain.enabled) return;
    for (const c of props.clusters) {
      if (!(rule.domain.values[c.k8s_name] ?? []).length)
        gaps.push(`${c.k8s_name} 的条件 ${index + 1}`);
    }
  });
  return gaps;
}

async function save() {
  if (props.vsId === null) return;
  const payload = buildPayload();
  if (typeof payload === "string") {
    message(payload, { type: "warning" });
    return;
  }
  const gaps = emptyDomainBlocks();
  if (gaps.length) {
    await ElMessageBox.confirm(
      `${gaps.join("、")} 没选域名,这些条件在对应集群不生效。继续保存?`,
      "部分条件没选域名",
      { type: "warning" }
    );
  }
  saving.value = true;
  try {
    if (props.route) {
      await updateIstioRoute(props.route.id, payload);
    } else {
      await createIstioRoute(props.vsId, payload);
    }
    message("已保存,下发后生效", { type: "success" });
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
    :title="route ? '编辑 HTTP 规则' : '新增 HTTP 规则'"
    width="960px"
    top="5vh"
    :close-on-click-modal="false"
  >
    <el-form label-width="90px">
      <el-row :gutter="16">
        <el-col :span="8">
          <el-form-item label="优先级">
            <el-input-number
              v-model="form.priority"
              :min="1"
              :max="100000"
              placeholder="不填追加到最后"
              controls-position="right"
              style="width: 100%"
            />
          </el-form-item>
        </el-col>
        <el-col :span="8">
          <el-form-item label="超时">
            <el-input v-model.trim="form.timeout" placeholder="如 7s,选填" />
          </el-form-item>
        </el-col>
      </el-row>

      <el-form-item label="匹配条件">
        <div class="block">
          <el-card
            v-for="(rule, index) in form.match_rules"
            :key="index"
            shadow="never"
            class="sub-card"
          >
            <template #header>
              <div class="sub-head">
                <span>条件 {{ index + 1 }}(多个条件之间是「或」)</span>
                <el-button
                  v-if="form.match_rules.length > 1"
                  type="danger"
                  size="small"
                  link
                  :icon="Delete"
                  @click="form.match_rules.splice(index, 1)"
                />
              </div>
            </template>
            <el-row :gutter="10">
              <el-col :span="4"
                ><el-checkbox v-model="rule.uri.enabled" label="URI"
              /></el-col>
              <el-col :span="5">
                <el-select
                  v-model="rule.uri.type"
                  :disabled="!rule.uri.enabled"
                >
                  <el-option label="prefix" value="prefix" />
                  <el-option label="exact" value="exact" />
                  <el-option label="regex" value="regex" />
                </el-select>
              </el-col>
              <el-col :span="15">
                <el-input
                  v-model="rule.uri.value"
                  :disabled="!rule.uri.enabled"
                  placeholder="/user-service/"
                />
              </el-col>
            </el-row>
            <el-row :gutter="10" class="mt-2">
              <el-col :span="4">
                <el-checkbox
                  v-model="rule.domain.enabled"
                  label="域名"
                  :disabled="!clusters.length && !rule.domain.enabled"
                />
              </el-col>
              <el-col :span="20">
                <div v-if="!rule.domain.enabled" class="hint">
                  {{
                    clusters.length
                      ? "不限域名。按域名区分时勾选,各集群分别从自己的 hosts 里选(多个域名之间是「或」)"
                      : "还没有部署到任何集群:先在「集群与 Hosts」里部署,再按集群选域名"
                  }}
                </div>
                <template v-else>
                  <div class="inline">
                    <el-input
                      v-model="rule.domain.keyword"
                      size="small"
                      placeholder="关键字,如服务名"
                      clearable
                      style="width: 220px"
                      @keyup.enter="pickByKeyword(rule)"
                    />
                    <el-button size="small" @click="pickByKeyword(rule)"
                      >各集群按关键字选</el-button
                    >
                  </div>
                  <div
                    v-for="c in clusters"
                    :key="c.k8s_name"
                    class="domain-row"
                  >
                    <span class="domain-cluster" :title="c.k8s_name">
                      <el-tag size="small" :type="ENV_TYPE_TAG[c.env_type]">{{
                        ENV_TYPE_LABEL[c.env_type]
                      }}</el-tag>
                      {{ c.k8s_name }}
                    </span>
                    <el-select
                      v-model="rule.domain.values[c.k8s_name]"
                      multiple
                      filterable
                      allow-create
                      default-first-option
                      placeholder="从该集群的 hosts 里选,也可以直接输入"
                      class="domain-select"
                    >
                      <el-option
                        v-for="o in domainOptions(rule, c)"
                        :key="o.value"
                        :label="o.label"
                        :value="o.value"
                      />
                    </el-select>
                  </div>
                </template>
              </el-col>
            </el-row>
            <el-row :gutter="10" class="mt-2">
              <el-col :span="4"
                ><el-checkbox v-model="rule.headers.enabled" label="Headers"
              /></el-col>
              <el-col v-if="rule.headers.enabled" :span="20">
                <el-row
                  v-for="(header, hIndex) in rule.headers.items"
                  :key="hIndex"
                  :gutter="8"
                  class="mb-1"
                >
                  <el-col :span="7"
                    ><el-input v-model="header.key" placeholder="Header 名"
                  /></el-col>
                  <el-col :span="5">
                    <el-select v-model="header.type">
                      <el-option label="exact" value="exact" />
                      <el-option label="prefix" value="prefix" />
                      <el-option label="regex" value="regex" />
                    </el-select>
                  </el-col>
                  <el-col :span="8"
                    ><el-input v-model="header.value" placeholder="值"
                  /></el-col>
                  <el-col :span="4">
                    <el-button
                      v-if="rule.headers.items.length > 1"
                      link
                      type="danger"
                      :icon="Delete"
                      @click="rule.headers.items.splice(hIndex, 1)"
                    />
                    <el-button
                      link
                      type="primary"
                      :icon="Plus"
                      @click="
                        rule.headers.items.push({
                          key: '',
                          type: 'exact',
                          value: ''
                        })
                      "
                    />
                  </el-col>
                </el-row>
              </el-col>
            </el-row>
            <div v-if="Object.keys(rule.rest).length" class="kept">
              保留的其他条件:{{ JSON.stringify(rule.rest) }}
            </div>
          </el-card>
          <div class="inline">
            <el-button
              :icon="Plus"
              size="small"
              @click="form.match_rules.push(emptyMatch())"
            >
              添加条件
            </el-button>
            <template v-if="hasDomainBlock && clusters.length > 1">
              <span class="label copy-label">复制域名</span>
              <el-select
                v-model="copy.from"
                size="small"
                placeholder="从集群"
                style="width: 190px"
              >
                <el-option
                  v-for="c in clusters"
                  :key="c.k8s_name"
                  :label="c.k8s_name"
                  :value="c.k8s_name"
                />
              </el-select>
              <span class="label">到</span>
              <el-select
                v-model="copy.to"
                size="small"
                placeholder="到集群"
                style="width: 190px"
              >
                <el-option
                  v-for="c in clusters.filter(c => c.k8s_name !== copy.from)"
                  :key="c.k8s_name"
                  :label="c.k8s_name"
                  :value="c.k8s_name"
                />
              </el-select>
              <span class="label">把</span>
              <el-input
                v-model="copy.find"
                size="small"
                placeholder="如 penglai"
                style="width: 100px"
              />
              <span class="label">换成</span>
              <el-input
                v-model="copy.replace"
                size="small"
                placeholder="如 kunlun"
                style="width: 100px"
              />
              <el-button
                size="small"
                :disabled="!copy.from || !copy.to || copy.from === copy.to"
                @click="copyDomains"
                >复制</el-button
              >
            </template>
          </div>
        </div>
      </el-form-item>

      <el-row :gutter="16">
        <el-col :span="10">
          <el-form-item label="URI 重写">
            <div class="inline">
              <el-checkbox
                v-model="form.rewrite.enabled"
                @change="onRewriteToggle"
              />
              <el-input
                v-model="form.rewrite.uri"
                :disabled="!form.rewrite.enabled"
                placeholder="重写后的 URI,如 /"
              />
            </div>
          </el-form-item>
        </el-col>
        <el-col :span="14">
          <el-form-item label="名称" :required="form.rewrite.enabled">
            <div class="inline">
              <el-input
                v-model.trim="form.name"
                :disabled="!form.rewrite.enabled"
                :placeholder="
                  form.rewrite.enabled
                    ? '访问日志里展示的原始前缀'
                    : '不重写时不设置名称'
                "
              />
              <el-button
                v-if="form.rewrite.enabled"
                :disabled="!suggestedName"
                @click="form.name = suggestedName"
              >
                按前缀生成
              </el-button>
            </div>
            <div v-if="!form.rewrite.enabled && form.name" class="warn-tip">
              这条规则没用重写却设置了名称,保存时会清空名称;如果需要名称请启用重写
            </div>
          </el-form-item>
        </el-col>
      </el-row>

      <el-form-item label="转发类型">
        <el-radio-group v-model="form.forward_type">
          <el-radio-button value="route">route</el-radio-button>
          <el-radio-button value="delegate">delegate</el-radio-button>
        </el-radio-group>
      </el-form-item>

      <el-form-item v-if="form.forward_type === 'route'" label="转发目标">
        <div class="block">
          <el-card
            v-for="(target, index) in form.forward_detail"
            :key="index"
            shadow="never"
            class="sub-card"
          >
            <template #header>
              <div class="sub-head">
                <span>目标 {{ index + 1 }}</span>
                <el-button
                  v-if="form.forward_detail.length > 1"
                  type="danger"
                  size="small"
                  link
                  :icon="Delete"
                  @click="form.forward_detail.splice(index, 1)"
                />
              </div>
            </template>
            <div v-if="target.rawHost" class="inline">
              <span class="label">Host</span>
              <el-input v-model="target.rawHost" />
            </div>
            <div v-else class="inline">
              <el-select
                v-model="target.service"
                filterable
                allow-create
                default-first-option
                placeholder="服务名(先选命名空间)"
                style="flex: 2"
                @focus="loadServices(target.namespace)"
              >
                <el-option
                  v-for="s in serviceOptions"
                  :key="s"
                  :label="s"
                  :value="s"
                />
              </el-select>
              <span>.</span>
              <el-select
                v-model="target.namespace"
                filterable
                allow-create
                default-first-option
                placeholder="命名空间"
                style="flex: 1"
                @change="loadServices(target.namespace)"
              >
                <el-option
                  v-for="ns in namespaceOptions"
                  :key="ns"
                  :label="ns"
                  :value="ns"
                />
              </el-select>
              <span>.svc.cluster.local</span>
            </div>
            <div class="inline mt-2">
              <span class="label">端口</span>
              <el-input-number
                v-model="target.port"
                :min="1"
                :max="65535"
                controls-position="right"
                style="width: 140px"
              />
              <el-button
                v-if="!target.rawHost"
                :icon="Refresh"
                title="获取服务第一个端口"
                @click="refreshPort(target)"
              />
              <el-checkbox
                v-model="target.subset.enabled"
                label="Subset"
                class="ml-4"
              />
              <el-input
                v-model="target.subset.value"
                :disabled="!target.subset.enabled"
                style="width: 120px"
              />
              <el-checkbox
                v-model="target.weight.enabled"
                label="权重"
                class="ml-4"
              />
              <el-input-number
                v-model="target.weight.value"
                :disabled="!target.weight.enabled"
                :min="0"
                :max="100"
                controls-position="right"
                style="width: 110px"
              />
            </div>
            <div class="inline mt-2">
              <el-checkbox v-model="target.headers.enabled" label="Headers" />
              <el-input
                v-model="target.headers.value"
                :disabled="!target.headers.enabled"
                type="textarea"
                :rows="1"
              />
            </div>
            <div
              v-if="
                Object.keys(target.rest).length ||
                Object.keys(target.destRest).length
              "
              class="kept"
            >
              保留的其他字段:{{
                JSON.stringify({ ...target.rest, destination: target.destRest })
              }}
            </div>
          </el-card>
          <el-button
            :icon="Plus"
            size="small"
            @click="form.forward_detail.push(emptyTarget())"
          >
            添加目标
          </el-button>
        </div>
      </el-form-item>

      <el-row v-else :gutter="16">
        <el-col :span="12">
          <el-form-item label="下级 VS">
            <el-input v-model.trim="form.delegate_name" placeholder="名称" />
          </el-form-item>
        </el-col>
        <el-col :span="12">
          <el-form-item label="命名空间">
            <el-input
              v-model.trim="form.delegate_namespace"
              placeholder="下级 VS 的命名空间"
            />
          </el-form-item>
        </el-col>
      </el-row>

      <el-form-item v-if="extraYaml" label="其他字段">
        <div class="block">
          <div class="hint">表单不支持编辑这些字段,保存和下发时原样保留</div>
          <pre class="extra">{{ extraYaml }}</pre>
        </div>
      </el-form-item>
    </el-form>
    <template #footer>
      <span class="ref-env">服务 / 端口候选来自集群:{{ refEnv || "无" }}</span>
      <el-button @click="visible = false">取消</el-button>
      <el-button type="primary" :loading="saving" @click="save">保存</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.block {
  width: 100%;
}

.sub-card {
  margin-bottom: 8px;
}

.sub-card :deep(.el-card__header) {
  padding: 6px 12px;
}

.sub-card :deep(.el-card__body) {
  padding: 10px 12px;
}

.sub-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 13px;
}

.inline {
  display: flex;
  gap: 8px;
  align-items: center;
  width: 100%;
}

.label {
  flex: none;
  font-size: 13px;
  color: var(--el-text-color-regular);
}

.copy-label {
  margin-left: 12px;
}

.domain-row {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-top: 6px;
}

.domain-cluster {
  display: flex;
  flex: none;
  gap: 4px;
  align-items: center;
  width: 210px;
  overflow: hidden;
  font-size: 13px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.domain-select {
  flex: 1;
  min-width: 0;
}

.kept,
.hint {
  margin-top: 6px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
  word-break: break-all;
}

.extra {
  max-height: 200px;
  padding: 6px 8px;
  margin: 4px 0 0;
  overflow: auto;
  font-size: 12px;
  background: var(--el-fill-color-light);
  border-radius: 4px;
}

.warn-tip {
  font-size: 12px;
  line-height: 18px;
  color: var(--el-color-warning);
}

.ref-env {
  float: left;
  font-size: 12px;
  line-height: 32px;
  color: var(--el-text-color-secondary);
}
</style>
