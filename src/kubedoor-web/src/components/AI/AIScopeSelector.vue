<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref, watch } from "vue";
import { aiApi, type AICluster, type AIResource, type AIScope } from "@/api/ai";
import { parseResourceKey, resourceKey } from "@/utils/ai";

const props = defineProps<{
  modelValue: AIScope;
  clusters: AICluster[];
  disabled?: boolean;
}>();
const emit = defineEmits<{ "update:modelValue": [value: AIScope] }>();
const namespaces = ref<AIResource[]>([]);
const deployments = ref<AIResource[]>([]);
const pods = ref<AIResource[]>([]);
type ResourceKind = "namespaces" | "deployments" | "pods";
const lists = { namespaces, deployments, pods };
const loading = reactive({
  namespaces: false,
  deployments: false,
  pods: false
});
const errors = reactive({ namespaces: "", deployments: "", pods: "" });
const error = computed(() =>
  [...new Set(Object.values(errors).filter(Boolean))].join("；")
);
const controllers = new Map<ResourceKind, AbortController>();
const available = computed(() => {
  const cluster = props.clusters.find(
    item => item.env === props.modelValue.env
  );
  return !!(cluster?.online || cluster?.configured);
});
const deployment = computed(() => resourceKey(props.modelValue.deployment));
const pod = computed(() => resourceKey(props.modelValue.pod));
const namespace = computed(() => props.modelValue.namespace || "");
const label = (resource: AIResource) =>
  resource.namespace ? `${resource.namespace}/${resource.name}` : resource.name;

function changeCluster(env: string) {
  emit("update:modelValue", {
    env,
    namespace: null,
    deployment: null,
    pod: null
  });
}
function changeNamespace(value: string) {
  emit("update:modelValue", {
    ...props.modelValue,
    namespace: value || null,
    deployment: null,
    pod: null
  });
}
function changeDeployment(value: string) {
  emit("update:modelValue", {
    ...props.modelValue,
    deployment: parseResourceKey(value),
    pod: null
  });
}
function changePod(value: string) {
  emit("update:modelValue", {
    ...props.modelValue,
    pod: parseResourceKey(value)
  });
}

async function loadLevel(kind: ResourceKind) {
  controllers.get(kind)?.abort();
  controllers.delete(kind);
  const snapshot: AIScope = JSON.parse(JSON.stringify(props.modelValue));
  lists[kind].value = [];
  errors[kind] = "";
  loading[kind] = false;
  if (!snapshot.env || !available.value) return;
  if (kind !== "namespaces" && !snapshot.namespace) return;
  if (kind === "pods" && !snapshot.deployment) return;
  const controller = new AbortController();
  controllers.set(kind, controller);
  loading[kind] = true;
  try {
    const response = await aiApi.resources(kind, snapshot, controller.signal);
    if (controllers.get(kind) !== controller || controller.signal.aborted)
      return;
    lists[kind].value = response.items || [];
  } catch (reason) {
    if (controllers.get(kind) !== controller || controller.signal.aborted)
      return;
    errors[kind] =
      reason instanceof Error ? reason.message : "资源列表加载失败";
  } finally {
    if (controllers.get(kind) === controller) {
      loading[kind] = false;
      controllers.delete(kind);
    }
  }
}

function retry() {
  for (const kind of ["namespaces", "deployments", "pods"] as ResourceKind[])
    if (errors[kind]) void loadLevel(kind);
}

watch(
  [() => props.modelValue.env, available],
  () => void loadLevel("namespaces"),
  { immediate: true }
);
watch(
  [() => props.modelValue.env, namespace, available],
  () => void loadLevel("deployments"),
  { immediate: true }
);
watch(
  [() => props.modelValue.env, namespace, deployment, available],
  () => void loadLevel("pods"),
  { immediate: true }
);
onBeforeUnmount(() => {
  for (const controller of controllers.values()) controller.abort();
  controllers.clear();
});
</script>

<template>
  <div class="scope-panel">
    <div class="scope-controls">
      <label>
        <span>K8S 集群</span>
        <el-select
          :model-value="modelValue.env"
          :disabled="disabled"
          filterable
          placeholder="选择集群"
          @change="changeCluster"
        >
          <el-option
            v-for="cluster in clusters"
            :key="cluster.env"
            :value="cluster.env"
            :label="cluster.env"
          >
            <span>{{ cluster.env }}</span>
            <small class="cluster-state">{{
              cluster.online
                ? "Agent 在线"
                : cluster.configured
                  ? "已配置直连"
                  : "仅指标 / 历史"
            }}</small>
          </el-option>
        </el-select>
      </label>
      <label>
        <span>命名空间</span>
        <el-select
          :model-value="namespace"
          :empty-values="[null, undefined]"
          :disabled="disabled || !modelValue.env"
          :loading="loading.namespaces"
          filterable
          @change="changeNamespace"
        >
          <el-option label="全部" value="" />
          <el-option
            v-for="item in namespaces"
            :key="item.name"
            :label="item.name"
            :value="item.name"
          />
        </el-select>
      </label>
      <label>
        <span>Deployment</span>
        <el-select
          :model-value="deployment"
          :empty-values="[null, undefined]"
          :disabled="disabled || !modelValue.env || !modelValue.namespace"
          :loading="loading.deployments"
          filterable
          @change="changeDeployment"
        >
          <el-option label="全部" value="" />
          <el-option
            v-for="item in deployments"
            :key="resourceKey(item)"
            :label="label(item)"
            :value="resourceKey(item)"
          />
        </el-select>
      </label>
      <label>
        <span>Pod</span>
        <el-select
          :model-value="pod"
          :empty-values="[null, undefined]"
          :disabled="
            disabled ||
            !modelValue.env ||
            !modelValue.namespace ||
            !modelValue.deployment
          "
          :loading="loading.pods"
          filterable
          @change="changePod"
        >
          <el-option label="全部" value="" />
          <el-option
            v-for="item in pods"
            :key="resourceKey(item)"
            :label="label(item)"
            :value="resourceKey(item)"
          />
        </el-select>
      </label>
    </div>
    <div v-if="error" class="scope-error">
      <span>部分资源列表不可用：{{ error }}</span>
      <el-button
        link
        type="primary"
        :disabled="Object.values(loading).some(Boolean) || disabled"
        @click="retry"
        >重试</el-button
      >
    </div>
  </div>
</template>

<style scoped lang="scss">
.scope-controls {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;

  label > span {
    display: block;
    margin-bottom: 6px;
    font-size: 12px;
    color: var(--el-text-color-regular);
  }

  .el-select {
    width: 100%;
  }
}

.cluster-state {
  float: right;
  margin-left: 16px;
  color: var(--el-text-color-secondary);
}

.scope-error {
  display: flex;
  gap: 12px;
  align-items: center;
  margin-top: 8px;
  font-size: 12px;
  color: var(--el-color-danger);
}

@media (width <= 760px) {
  .scope-controls {
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 8px;
  }
}
</style>
