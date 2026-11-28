<template>
  <el-dialog
    v-model="visible"
    width="980px"
    top="6vh"
    append-to-body
    :close-on-click-modal="false"
    class="env-dialog"
    @closed="reset"
  >
    <template #header>
      <div class="env-dialog-header">
        <span class="env-dialog-icon">
          <el-icon><Key /></el-icon>
        </span>
        <div class="env-dialog-titles">
          <div class="env-dialog-title">获取 ENV</div>
          <div class="env-dialog-subtitle">
            Deployment 主容器里声明的环境变量
          </div>
        </div>
      </div>
    </template>

    <div class="env-panel">
      <div class="meta">
        <span v-for="item in metaItems" :key="item.label" class="meta-item">
          <span class="meta-label">{{ item.label }}</span>
          <span class="meta-value">{{ item.value }}</span>
        </span>
      </div>

      <template v-if="current">
        <div class="container-card">
          <div class="container-main">
            <div class="container-name">
              <el-icon class="container-icon"><Box /></el-icon>
              <span class="container-title">{{
                current.name || "未命名容器"
              }}</span>
              <el-tag
                v-if="isMain"
                size="small"
                type="success"
                effect="plain"
                round
                disable-transitions
                >主容器</el-tag
              >
            </div>
            <div class="container-image">
              {{ current.image || "未设置镜像" }}
            </div>
          </div>
          <div class="container-stats">
            <div class="stat">
              <span class="stat-value">{{ current.env.length }}</span>
              <span class="stat-label">环境变量</span>
            </div>
            <div class="stat">
              <span class="stat-value">{{ current.envFrom.length }}</span>
              <span class="stat-label">整包引用</span>
            </div>
            <div class="stat">
              <span class="stat-value">{{ secretCount }}</span>
              <span class="stat-label">来自 Secret</span>
            </div>
          </div>
          <el-select
            v-if="(data?.containers.length ?? 0) > 1"
            v-model="activeIndex"
            class="container-select"
          >
            <el-option
              v-for="(item, index) in data?.containers"
              :key="index"
              :label="item.name || `容器 ${index + 1}`"
              :value="index"
            />
          </el-select>
        </div>

        <div v-if="current.envFrom.length" class="envfrom">
          <span class="envfrom-label">整包引用</span>
          <el-tag
            v-for="item in current.envFrom"
            :key="`${item.kind}-${item.name}-${item.prefix}`"
            :type="item.kind === 'secret' ? 'danger' : 'primary'"
            effect="plain"
            size="small"
            disable-transitions
          >
            {{ item.kind === "secret" ? "Secret" : "ConfigMap" }} /
            {{ item.name }}
            <template v-if="item.prefix">（前缀 {{ item.prefix }}）</template>
          </el-tag>
        </div>
      </template>

      <div class="toolbar">
        <el-input
          v-model="keyword"
          class="search-input"
          clearable
          placeholder="搜索变量名或来源"
          :prefix-icon="Search"
        />
        <el-switch v-model="hideSensitive" active-text="隐藏敏感值" />
        <span class="spacer" />
        <el-button
          :icon="CopyDocument"
          :disabled="!current?.env.length"
          @click="copyAll"
          >复制全部</el-button
        >
        <el-button :icon="Refresh" :loading="loading" @click="load"
          >重新获取</el-button
        >
      </div>

      <el-alert
        v-if="error"
        type="error"
        :title="error"
        :closable="false"
        show-icon
      />

      <div
        v-if="current || loading"
        v-loading="loading"
        element-loading-text="正在获取 Deployment 配置…"
        class="table-wrap"
      >
        <el-table v-if="filtered.length" :data="filtered" max-height="50vh">
          <el-table-column label="变量名" min-width="240">
            <template #default="{ row }">
              <span class="env-name">{{ row.name }}</span>
            </template>
          </el-table-column>
          <el-table-column label="值" min-width="340">
            <template #default="{ row }">
              <div class="value-cell">
                <template v-if="row.source === 'literal'">
                  <span v-if="isMasked(row)" class="value-masked">{{
                    ENV_MASK
                  }}</span>
                  <span v-else class="value-text">{{
                    row.value === "" ? '""' : row.value
                  }}</span>
                  <el-tooltip
                    :content="isMasked(row) ? '显示这个值' : '复制这一行'"
                    placement="top"
                  >
                    <el-button
                      link
                      class="value-action"
                      :icon="isMasked(row) ? View : CopyDocument"
                      @click="isMasked(row) ? toggleReveal(row) : copyRow(row)"
                    />
                  </el-tooltip>
                </template>
                <span v-else class="value-ref">
                  <el-icon><Lock /></el-icon>
                  <span>引用值,页面不展示</span>
                </span>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="来源" width="240">
            <template #default="{ row }">
              <div class="source-cell">
                <el-tag
                  :type="tagType(row.source)"
                  size="small"
                  disable-transitions
                  >{{ row.sourceLabel }}</el-tag
                >
                <span v-if="row.sourceDetail" class="source-detail">{{
                  row.sourceDetail
                }}</span>
              </div>
            </template>
          </el-table-column>
        </el-table>
        <el-empty
          v-else-if="!loading && current"
          :image-size="72"
          :description="emptyText"
        />
      </div>
    </div>

    <template #footer>
      <el-button type="primary" @click="visible = false">关闭</el-button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { computed, reactive, ref, shallowRef, watch } from "vue";
import {
  Box,
  CopyDocument,
  Key,
  Lock,
  Refresh,
  Search,
  View
} from "@element-plus/icons-vue";
import { getServiceContent } from "@/api/service";
import { copyText } from "@/utils/clipboard";
import { message } from "@/utils/message";
import {
  ENV_MASK,
  DeploymentEnvError,
  filterEnvRows,
  parseDeploymentEnv,
  toEnvText,
  type DeploymentEnvView,
  type EnvValueSource,
  type EnvVarView
} from "../utils/deploymentEnv";

defineOptions({ name: "EnvDialog" });

// Deployment 的 YAML 已经能按集群读权限看到(同「编辑」),这里只是把主容器的 env 挑出来展示

const TAG_TYPES: Record<
  EnvValueSource,
  "success" | "primary" | "danger" | "info" | "warning"
> = {
  literal: "success",
  configmap: "primary",
  secret: "danger",
  field: "info",
  resource: "warning",
  other: "info"
};

const visible = ref(false);
const loading = ref(false);
const target = reactive({ env: "", namespace: "", deployment: "" });
// YAML 可能不小,不做深层响应式
const data = shallowRef<DeploymentEnvView | null>(null);
const activeIndex = ref(0);
const keyword = ref("");
const hideSensitive = ref(true);
const revealed = ref(new Set<string>());
const error = ref("");
// 只认最后一次请求:关掉后或者重新获取时,之前没回来的结果丢掉
let seq = 0;

const metaItems = computed(() => [
  { label: "集群", value: target.env || "-" },
  { label: "命名空间", value: target.namespace || "-" },
  { label: "Deployment", value: target.deployment || "-" }
]);

const current = computed(
  () => data.value?.containers[activeIndex.value] ?? data.value?.main ?? null
);
const isMain = computed(() => activeIndex.value === 0);
const filtered = computed(() =>
  filterEnvRows(current.value?.env ?? [], keyword.value)
);

const secretCount = computed(() => {
  const container = current.value;
  if (!container) return 0;
  return (
    container.env.filter(item => item.fromSecret).length +
    container.envFrom.filter(item => item.kind === "secret").length
  );
});

const emptyText = computed(() => {
  if (!current.value?.env.length && !current.value?.envFrom.length) {
    return "主容器没有配置环境变量";
  }
  return "没有匹配的环境变量";
});

// 开关「隐藏敏感值」时清掉单个变量的显示状态
watch(hideSensitive, () => {
  revealed.value = new Set();
});

const tagType = (source: EnvValueSource) => TAG_TYPES[source] ?? "info";

const isMasked = (row: EnvVarView) =>
  row.source === "literal" &&
  row.sensitive &&
  hideSensitive.value &&
  !revealed.value.has(row.name);

function toggleReveal(row: EnvVarView) {
  const next = new Set(revealed.value);
  if (next.has(row.name)) {
    next.delete(row.name);
  } else {
    next.add(row.name);
  }
  revealed.value = next;
}

function errorMessage(err: any): string {
  if (err instanceof DeploymentEnvError) return err.message;
  const body = err?.response?.data;
  if (body?.message || body?.error) return body.message || body.error;
  if (err?.code === "ECONNABORTED") return "请求超时,请稍后重试";
  const status = err?.response?.status;
  return status ? `获取失败(HTTP ${status})` : "获取失败,请检查网络后重试";
}

async function load() {
  const currentSeq = ++seq;
  loading.value = true;
  error.value = "";
  try {
    // 错误在弹窗里展示,不用全局的红色提示(否则同一个错误弹两次)
    const res = await getServiceContent(
      target.env,
      target.namespace,
      target.deployment,
      "deployment",
      { silent: true }
    );
    if (currentSeq !== seq) return;
    if (!res?.success || !res.data) {
      data.value = null;
      error.value = res?.message || res?.error || "获取 Deployment 配置失败";
      return;
    }
    const parsed = parseDeploymentEnv(res.data);
    data.value = parsed;
    activeIndex.value = 0;
    if (!parsed.containers.length) {
      error.value = "Deployment 的 Pod 模板里没有容器";
    }
  } catch (err: any) {
    if (currentSeq !== seq) return;
    data.value = null;
    error.value = errorMessage(err);
  } finally {
    if (currentSeq === seq) loading.value = false;
  }
}

async function copyAll() {
  const rows = current.value?.env ?? [];
  if (!rows.length) return;
  if (await copyText(toEnvText(rows))) {
    message("已复制", { type: "success" });
  } else {
    message("复制失败,请手动选择文本", { type: "warning" });
  }
}

async function copyRow(row: EnvVarView) {
  if (await copyText(`${row.name}=${row.value}`)) {
    message("已复制", { type: "success" });
  } else {
    message("复制失败,请手动选择文本", { type: "warning" });
  }
}

function open(row: { env: string; namespace: string; deployment: string }) {
  target.env = row.env;
  target.namespace = row.namespace;
  target.deployment = row.deployment;
  keyword.value = "";
  hideSensitive.value = true;
  revealed.value = new Set();
  data.value = null;
  activeIndex.value = 0;
  error.value = "";
  visible.value = true;
  load();
}

function reset() {
  // 关闭动画还没结束就又打开了:不能把新打开的这次清掉
  if (visible.value) return;
  seq++;
  loading.value = false;
  data.value = null;
  error.value = "";
  keyword.value = "";
  revealed.value = new Set();
}

defineExpose({ open });
</script>

<style lang="scss" scoped>
$mono: "SFMono-Regular", consolas, "Liberation Mono", menlo, monospace;

.env-dialog-header {
  display: flex;
  gap: 10px;
  align-items: center;
  padding-right: 28px;
}

.env-dialog-icon {
  display: flex;
  flex: none;
  align-items: center;
  justify-content: center;
  width: 34px;
  height: 34px;
  font-size: 18px;
  color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
  border-radius: 9px;
}

.env-dialog-titles {
  min-width: 0;
}

.env-dialog-title {
  font-size: 16px;
  font-weight: 600;
  line-height: 22px;
  color: var(--el-text-color-primary);
}

.env-dialog-subtitle {
  margin-top: 2px;
  font-size: 12px;
  line-height: 18px;
  color: var(--el-text-color-secondary);
}

.env-panel {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.meta {
  display: flex;
  flex-wrap: wrap;
  gap: 2px 20px;
  font-size: 13px;
  line-height: 22px;
}

.meta-item {
  display: inline-flex;
  gap: 6px;
  max-width: 100%;
}

.meta-label {
  flex: none;
  color: var(--el-text-color-secondary);
}

.meta-value {
  min-width: 0;
  font-weight: 500;
  color: var(--el-text-color-primary);
  word-break: break-all;
}

.container-card {
  display: flex;
  flex-wrap: wrap;
  gap: 12px 16px;
  align-items: center;
  padding: 12px 16px;
  background: linear-gradient(
    135deg,
    var(--el-color-primary-light-9),
    var(--el-fill-color-blank) 65%
  );
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 10px;
}

.container-main {
  flex: 1 1 320px;
  min-width: 0;
}

.container-name {
  display: flex;
  gap: 8px;
  align-items: center;
  min-width: 0;
}

.container-icon {
  flex: none;
  font-size: 16px;
  color: var(--el-color-primary);
}

.container-title {
  overflow: hidden;
  font-size: 15px;
  font-weight: 600;
  color: var(--el-text-color-primary);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.container-image {
  margin-top: 4px;
  overflow: hidden;
  font-size: 12px;
  color: var(--el-text-color-secondary);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.container-stats {
  display: flex;
  flex: none;
  gap: 24px;
}

.stat {
  display: flex;
  flex-direction: column;
  align-items: center;
  min-width: 56px;
  line-height: 1.2;
}

.stat-value {
  font-size: 20px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  color: var(--el-color-primary);
}

.stat-label {
  margin-top: 2px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.container-select {
  width: 190px;
}

.envfrom {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
  font-size: 13px;
}

.envfrom-label {
  flex: none;
  margin-right: 2px;
  color: var(--el-text-color-secondary);
}

.toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 12px;
  align-items: center;
}

.search-input {
  width: 260px;
}

.spacer {
  flex: 1;
}

// 加载中和空状态时也占住高度,弹窗不会一闪一闪地变高
.table-wrap {
  min-height: 320px;
  overflow: hidden;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 10px;

  :deep(.el-table) {
    --el-table-header-bg-color: var(--el-fill-color-light);
  }

  :deep(.el-table th.el-table__cell) {
    font-weight: 600;
    color: var(--el-text-color-regular);
  }
}

.env-name {
  font-family: $mono;
  font-size: 13px;
  font-weight: 600;
  color: var(--el-text-color-primary);
  word-break: break-all;
}

.value-cell {
  display: flex;
  gap: 6px;
  align-items: flex-start;
}

.value-text {
  font-family: $mono;
  font-size: 13px;
  line-height: 20px;
  color: var(--el-text-color-primary);
  word-break: break-all;
  white-space: pre-wrap;
}

.value-masked {
  font-family: $mono;
  font-size: 13px;
  line-height: 20px;
  color: var(--el-text-color-placeholder);
  letter-spacing: 2px;
}

.value-action {
  flex: none;
  margin-top: -2px;
  color: var(--el-text-color-secondary);

  &:hover {
    color: var(--el-color-primary);
  }
}

.value-ref {
  display: inline-flex;
  gap: 4px;
  align-items: center;
  font-size: 12px;
  line-height: 20px;
  color: var(--el-text-color-secondary);
}

.source-cell {
  display: flex;
  flex-direction: column;
  gap: 4px;
  align-items: flex-start;
}

.source-detail {
  font-family: $mono;
  font-size: 12px;
  color: var(--el-text-color-secondary);
  word-break: break-all;
}
</style>

<style lang="scss">
// 弹窗挂在 body 上,scoped 样式管不到:标题栏加一条分隔线,正文留出间距
.env-dialog {
  .el-dialog__header {
    padding-bottom: 12px;
    border-bottom: 1px solid var(--el-border-color-lighter);
  }

  .el-dialog__body {
    padding-top: 16px;
  }
}
</style>
