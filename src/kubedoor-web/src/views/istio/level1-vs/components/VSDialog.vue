<script setup lang="ts">
import { computed, reactive, ref, watch } from "vue";
import { Refresh } from "@element-plus/icons-vue";
import { message } from "@/utils/message";
import {
  type IstioGroupCluster,
  type VSDetail,
  type VSFields,
  createIstioVS,
  getServiceFirstPort,
  updateIstioVS
} from "@/api/istio";
import { getPromNamespace, getPromServices } from "@/api/monit";
import { ENV_TYPE_LABEL, parseHostsText, toYaml } from "../utils";

defineOptions({ name: "IstioVSDialog" });

/**
 * 新建组标准 VS(选部署到哪些集群并分别填 hosts),或编辑 gateways 和默认路由。
 * 默认路由能用表单表示时用表单,否则(多个目标、权重、subset 等)用 JSON 编辑,保证不丢字段。
 */
const props = defineProps<{
  groupId: number;
  clusters: IstioGroupCluster[];
  /** 编辑时传入;为空是新建 */
  vs: VSDetail | null;
  /** gateways 候选(组里已有 VS 用到的) */
  gatewayOptions: string[];
}>();
const emit = defineEmits<{ (e: "saved", vsId: number): void }>();
const visible = defineModel<boolean>({ default: false });

const DEFAULT_HEADERS = '{"request": {"set": {"X-Forwarded-Proto": "https"}}}';
const TIMEOUT_PATTERN = /^\d+(\.\d+)?(ns|us|µs|ms|s|m|h)$/;
const NAME_PATTERN = /^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$/;

const form = reactive({
  namespace: "istio-system",
  name: "",
  gateways: [] as string[],
  df_type: "" as "" | "route" | "delegate",
  json_mode: false,
  json_text: "",
  service: "",
  service_ns: "",
  port: null as number | null,
  headers_enabled: false,
  headers: DEFAULT_HEADERS,
  timeout: "",
  delegate_name: "",
  delegate_namespace: ""
});
const members = ref<{ k8s_name: string; selected: boolean; hosts: string }[]>(
  []
);
const saving = ref(false);
const namespaceOptions = ref<string[]>([]);
const serviceOptions = ref<string[]>([]);

const isEdit = computed(() => !!props.vs);
const refEnv = computed(
  () =>
    members.value.find(m => m.selected)?.k8s_name ||
    props.vs?.clusters.find(c => c.bound)?.k8s_name ||
    props.clusters[0]?.k8s_name ||
    ""
);
const keptYaml = computed(() => {
  const kept: Record<string, any> = {};
  if (props.vs?.df_extra) kept["默认路由的其他字段"] = props.vs.df_extra;
  if (props.vs?.extra_spec) kept["spec 的其他字段"] = props.vs.extra_spec;
  return Object.keys(kept).length ? toYaml(kept) : "";
});

/** 默认路由能否用简单表单表示:一个目标,只有 host / port / headers */
function simpleRoute(detail: any) {
  if (!Array.isArray(detail) || detail.length !== 1) return null;
  const [item] = detail;
  const dest = item?.destination || {};
  if (Object.keys(item).some(k => !["destination", "headers"].includes(k)))
    return null;
  if (Object.keys(dest).some(k => !["host", "port"].includes(k))) return null;
  const parts = String(dest.host || "")
    .replace(/\.svc\.cluster\.local$/, "")
    .split(".");
  if (parts.length > 2) return null;
  return {
    service: parts[0] || "",
    namespace: parts[1] || props.vs?.namespace || "",
    port: dest.port?.number ?? null,
    headers: item.headers
  };
}

watch(
  visible,
  open => {
    if (!open) return;
    const vs = props.vs;
    Object.assign(form, {
      namespace: vs?.namespace ?? "istio-system",
      name: vs?.name ?? "",
      gateways: [...(vs?.gateways ?? [])],
      df_type: vs?.df_forward_type ?? "",
      json_mode: false,
      json_text: vs?.df_forward_detail
        ? JSON.stringify(vs.df_forward_detail, null, 2)
        : "",
      service: "",
      service_ns: "",
      port: null,
      headers_enabled: false,
      headers: DEFAULT_HEADERS,
      timeout: vs?.df_forward_timeout ?? "",
      delegate_name: "",
      delegate_namespace: ""
    });
    if (vs?.df_forward_type === "route") {
      const simple = simpleRoute(vs.df_forward_detail);
      if (simple) {
        Object.assign(form, {
          service: simple.service,
          service_ns: simple.namespace,
          port: simple.port,
          headers_enabled: !!simple.headers,
          headers: simple.headers
            ? JSON.stringify(simple.headers)
            : DEFAULT_HEADERS
        });
      } else {
        form.json_mode = true;
      }
    } else if (vs?.df_forward_type === "delegate") {
      form.delegate_name = vs.df_forward_detail?.name ?? "";
      form.delegate_namespace = vs.df_forward_detail?.namespace ?? "";
    }
    members.value = props.clusters.map(c => ({
      k8s_name: c.k8s_name,
      selected: false,
      hosts: ""
    }));
    loadNamespaces();
  },
  { immediate: true }
);

/** 名称以 -ext / -int 结尾时自动带出对应网关(和现有 VS 的命名习惯一致) */
function onNameBlur() {
  if (isEdit.value || form.gateways.length) return;
  if (form.name.endsWith("-ext")) form.gateways = ["istio-system/external"];
  else if (form.name.endsWith("-int"))
    form.gateways = ["istio-system/internal"];
}

function copyHosts(target: { hosts: string }, from: string) {
  const source = members.value.find(m => m.k8s_name === from);
  if (source) target.hosts = source.hosts;
}

async function loadNamespaces() {
  if (!refEnv.value) return;
  try {
    const res: any = await getPromNamespace(refEnv.value);
    namespaceOptions.value = Array.isArray(res?.data) ? res.data : [];
  } catch {
    namespaceOptions.value = [];
  }
}

async function loadServices() {
  if (!refEnv.value || !form.service_ns) return;
  try {
    const res: any = await getPromServices(refEnv.value, form.service_ns);
    serviceOptions.value = Array.isArray(res?.data) ? res.data : [];
  } catch {
    serviceOptions.value = [];
  }
}

async function refreshPort() {
  if (!form.service || !form.service_ns || !refEnv.value) {
    message("请先填写服务名和命名空间", { type: "warning" });
    return;
  }
  const res: any = await getServiceFirstPort(
    refEnv.value,
    form.service_ns,
    form.service
  );
  if (res?.data?.first_port) form.port = res.data.first_port;
  else message("未能获取到端口号", { type: "error" });
}

function switchToJson() {
  const fields = buildDefault();
  form.json_text =
    typeof fields !== "string" && fields.df_forward_detail
      ? JSON.stringify(fields.df_forward_detail, null, 2)
      : form.json_text;
  form.json_mode = true;
}

function buildDefault(): Omit<VSFields, "gateways"> | string {
  if (!form.df_type) {
    return {
      df_forward_type: null,
      df_forward_detail: null,
      df_forward_timeout: null
    };
  }
  const timeout = form.timeout.trim();
  if (timeout && !TIMEOUT_PATTERN.test(timeout))
    return "默认路由超时必须带单位,如 30s";
  let detail: any;
  if (form.json_mode) {
    try {
      detail = JSON.parse(form.json_text);
    } catch {
      return "默认路由的转发详情不是合法的 JSON";
    }
  } else if (form.df_type === "route") {
    if (!form.service.trim() || !form.service_ns.trim() || !form.port) {
      return "默认路由请填写服务名、命名空间和端口";
    }
    const item: Record<string, any> = {
      destination: {
        host: `${form.service.trim()}.${form.service_ns.trim()}.svc.cluster.local`,
        port: { number: form.port }
      }
    };
    if (form.headers_enabled && form.headers.trim()) {
      try {
        item.headers = JSON.parse(form.headers);
      } catch {
        return "默认路由的 Headers 不是合法的 JSON";
      }
    }
    detail = [item];
  } else {
    if (!form.delegate_name.trim() || !form.delegate_namespace.trim()) {
      return "请填写委托的下级 VS 名称和命名空间";
    }
    detail = {
      name: form.delegate_name.trim(),
      namespace: form.delegate_namespace.trim()
    };
  }
  return {
    df_forward_type: form.df_type,
    df_forward_detail: detail,
    df_forward_timeout: timeout || null
  };
}

async function save() {
  const fields = buildDefault();
  if (typeof fields === "string") {
    message(fields, { type: "warning" });
    return;
  }
  const payload: VSFields = { gateways: form.gateways, ...fields };
  saving.value = true;
  try {
    if (props.vs) {
      await updateIstioVS(props.vs.id, payload);
      message("已保存,下发后生效", { type: "success" });
      emit("saved", props.vs.id);
    } else {
      if (!NAME_PATTERN.test(form.name)) {
        message("VS 名称只能包含小写字母、数字、中横线和点", {
          type: "warning"
        });
        return;
      }
      const clusters = members.value
        .filter(m => m.selected)
        .map(m => ({ k8s_name: m.k8s_name, hosts: parseHostsText(m.hosts) }));
      if (!clusters.length) {
        message("至少选择一个要部署的集群", { type: "warning" });
        return;
      }
      const noHosts = clusters
        .filter(c => !c.hosts.length)
        .map(c => c.k8s_name);
      if (noHosts.length) {
        message(`请填写 ${noHosts.join("、")} 的 hosts`, { type: "warning" });
        return;
      }
      const res = await createIstioVS({
        group_id: props.groupId,
        namespace: form.namespace.trim(),
        name: form.name.trim(),
        clusters,
        ...payload
      });
      message("已创建,配好规则后到各集群下发", { type: "success" });
      emit("saved", res.data.id);
    }
    visible.value = false;
  } finally {
    saving.value = false;
  }
}
</script>

<template>
  <el-dialog
    v-model="visible"
    :title="
      isEdit ? `编辑 ${vs?.namespace}/${vs?.name}` : '新建 VirtualService'
    "
    width="860px"
    top="5vh"
    :close-on-click-modal="false"
  >
    <el-form label-width="100px">
      <template v-if="!isEdit">
        <el-row :gutter="16">
          <el-col :span="9">
            <el-form-item label="命名空间" required>
              <el-input v-model.trim="form.namespace" />
            </el-form-item>
          </el-col>
          <el-col :span="15">
            <el-form-item label="名称" required>
              <el-input
                v-model.trim="form.name"
                placeholder="小写字母、数字、中横线和点,如 b2c-ext"
                @blur="onNameBlur"
              />
            </el-form-item>
          </el-col>
        </el-row>
      </template>
      <el-form-item label="gateways">
        <el-select
          v-model="form.gateways"
          multiple
          filterable
          allow-create
          default-first-option
          placeholder="如 istio-system/internal,全组共用"
          style="width: 100%"
        >
          <el-option
            v-for="g in gatewayOptions"
            :key="g"
            :label="g"
            :value="g"
          />
        </el-select>
      </el-form-item>

      <el-form-item label="默认路由">
        <el-radio-group v-model="form.df_type">
          <el-radio-button value="">无</el-radio-button>
          <el-radio-button value="route">route</el-radio-button>
          <el-radio-button value="delegate">delegate</el-radio-button>
        </el-radio-group>
        <span class="hint ml-2">没有匹配条件、放在最后的规则,全组共用</span>
      </el-form-item>

      <template v-if="form.df_type">
        <el-form-item v-if="form.json_mode" label="转发详情">
          <el-input
            v-model="form.json_text"
            type="textarea"
            :rows="8"
            class="mono"
          />
        </el-form-item>
        <template v-else-if="form.df_type === 'route'">
          <el-form-item label="目标服务">
            <div class="inline">
              <el-select
                v-model="form.service"
                filterable
                allow-create
                default-first-option
                placeholder="服务名"
                style="flex: 2"
                @focus="loadServices"
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
                v-model="form.service_ns"
                filterable
                allow-create
                default-first-option
                placeholder="命名空间"
                style="flex: 1"
                @change="loadServices"
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
          </el-form-item>
          <el-form-item label="端口">
            <div class="inline">
              <el-input-number
                v-model="form.port"
                :min="1"
                :max="65535"
                controls-position="right"
                style="width: 160px"
              />
              <el-button
                :icon="Refresh"
                title="获取服务第一个端口"
                @click="refreshPort"
              />
              <el-checkbox
                v-model="form.headers_enabled"
                label="Headers"
                class="ml-4"
              />
              <el-input
                v-model="form.headers"
                :disabled="!form.headers_enabled"
              />
            </div>
          </el-form-item>
        </template>
        <template v-else>
          <el-form-item label="下级 VS">
            <div class="inline">
              <el-input v-model.trim="form.delegate_name" placeholder="名称" />
              <el-input
                v-model.trim="form.delegate_namespace"
                placeholder="命名空间"
              />
            </div>
          </el-form-item>
        </template>
        <el-form-item label="超时">
          <div class="inline">
            <el-input
              v-model.trim="form.timeout"
              placeholder="如 30s,选填"
              style="width: 160px"
            />
            <el-button
              v-if="!form.json_mode"
              link
              type="primary"
              @click="switchToJson"
            >
              用 JSON 编辑转发详情
            </el-button>
          </div>
        </el-form-item>
      </template>

      <el-form-item v-if="keptYaml" label="保留字段">
        <div class="block">
          <div class="hint">表单不支持编辑这些字段,保存和下发时原样保留</div>
          <pre class="extra">{{ keptYaml }}</pre>
        </div>
      </el-form-item>

      <el-form-item v-if="!isEdit" label="部署到">
        <div class="block">
          <div class="hint">
            hosts 各集群独立,每行一个;可以先填一个集群再复制到其他集群后修改
          </div>
          <div v-for="m in members" :key="m.k8s_name" class="member">
            <el-checkbox v-model="m.selected">
              {{ m.k8s_name }}
              <span class="hint"
                >({{
                  ENV_TYPE_LABEL[
                    clusters.find(c => c.k8s_name === m.k8s_name)?.env_type ??
                      "test"
                  ]
                }})</span
              >
            </el-checkbox>
            <div v-if="m.selected" class="member-hosts">
              <el-input
                v-model="m.hosts"
                type="textarea"
                :rows="2"
                placeholder="每行一个 host"
              />
              <el-select
                placeholder="复制自"
                size="small"
                style="width: 150px"
                @change="from => copyHosts(m, from)"
              >
                <el-option
                  v-for="o in members.filter(
                    x => x.selected && x.k8s_name !== m.k8s_name && x.hosts
                  )"
                  :key="o.k8s_name"
                  :label="o.k8s_name"
                  :value="o.k8s_name"
                />
              </el-select>
            </div>
          </div>
        </div>
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button type="primary" :loading="saving" @click="save">保存</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.inline {
  display: flex;
  gap: 8px;
  align-items: center;
  width: 100%;
}

.block {
  width: 100%;
}

.hint {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.mono :deep(textarea),
.extra {
  font-family: Consolas, Monaco, monospace;
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

.member {
  padding: 4px 0;
  border-bottom: 1px dashed var(--el-border-color-lighter);
}

.member-hosts {
  display: flex;
  gap: 8px;
  align-items: flex-start;
  margin: 4px 0 4px 24px;
}
</style>
