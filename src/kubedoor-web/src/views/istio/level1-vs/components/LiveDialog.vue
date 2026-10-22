<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { CopyDocument, Refresh } from "@element-plus/icons-vue";
import { copyTextToClipboard } from "@pureadmin/utils";
import { message } from "@/utils/message";
import { type EnvType, type LiveVS, getIstioLive } from "@/api/istio";
import MonacoView from "./MonacoView.vue";
import { ENV_TYPE_LABEL, ENV_TYPE_TAG, formatTime, toYaml } from "../utils";

defineOptions({ name: "IstioLiveDialog" });

/** 查看集群里 VirtualService 的现状:每次打开都实时从集群取(不是采集的快照),
 *  去掉了 metadata.managedFields 和 kubectl 的 last-applied 注解 */
const props = defineProps<{
  groupId: number;
  k8sName: string;
  envType?: EnvType;
  namespace: string;
  name: string;
}>();
const visible = defineModel<boolean>({ default: false });

const loading = ref(false);
const live = ref<LiveVS | null>(null);
const error = ref("");
const fetchedAt = ref("");

const yamlText = computed(() => (live.value ? toYaml(live.value.object) : ""));
const resourceVersion = computed(
  () => live.value?.object?.metadata?.resourceVersion ?? ""
);

async function load() {
  loading.value = true;
  error.value = "";
  try {
    const res = await getIstioLive(
      props.groupId,
      props.k8sName,
      props.namespace,
      props.name
    );
    live.value = res.data;
    fetchedAt.value = new Date().toISOString();
  } catch (e: any) {
    live.value = null;
    error.value = e?.response?.data?.error || "取集群现状失败,请稍后重试";
  } finally {
    loading.value = false;
  }
}

watch(
  visible,
  open => {
    if (!open) return;
    live.value = null;
    load();
  },
  { immediate: true }
);

function copy() {
  if (copyTextToClipboard(yamlText.value)) {
    message("已复制", { type: "success" });
  } else {
    message("复制失败,可以在内容里 Ctrl+A 全选后复制", { type: "warning" });
  }
}
</script>

<template>
  <el-dialog
    v-model="visible"
    :title="`集群现状 · ${namespace}/${name}`"
    width="80%"
    top="10vh"
    destroy-on-close
  >
    <div class="live-wrap">
      <div class="live-bar">
        <el-tag v-if="envType" size="small" :type="ENV_TYPE_TAG[envType]">{{
          ENV_TYPE_LABEL[envType]
        }}</el-tag>
        <b>{{ k8sName }}</b>
        <span v-if="resourceVersion" class="sub"
          >resourceVersion {{ resourceVersion }}</span
        >
        <span v-if="live" class="sub"
          >实时获取于 {{ formatTime(fetchedAt) }}</span
        >
        <span class="spacer" />
        <el-button :icon="Refresh" :loading="loading" @click="load"
          >刷新</el-button
        >
        <el-button :icon="CopyDocument" :disabled="!yamlText" @click="copy"
          >复制 YAML</el-button
        >
      </div>
      <el-alert
        v-if="live && !live.full"
        type="warning"
        show-icon
        :closable="false"
        title="该集群的 agent 版本较旧,只取得到 spec,metadata 不完整;升级 agent 后可以看到完整内容"
      />
      <div v-loading="loading" class="live-body">
        <MonacoView v-if="yamlText" :value="yamlText" height="100%" />
        <el-empty v-else-if="error" :description="error" />
      </div>
    </div>
  </el-dialog>
</template>

<style scoped>
/* 对话框整体占浏览器高度的 80%:减去标题栏和上下内边距(73px) */
.live-wrap {
  display: flex;
  flex-direction: column;
  gap: 8px;
  height: calc(80vh - 73px);
}

.live-bar {
  display: flex;
  gap: 8px;
  align-items: center;
}

.spacer {
  flex: 1;
}

.sub {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.live-body {
  flex: 1;
  min-height: 0;
}
</style>
