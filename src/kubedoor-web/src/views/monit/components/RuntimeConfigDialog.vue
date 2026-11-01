<template>
  <el-dialog
    v-model="visible"
    title="获取运行配置"
    width="95vw"
    top="2.5vh"
    append-to-body
    :close-on-click-modal="false"
    class="runtime-config-dialog"
    @closed="reset"
  >
    <div class="runtime-config">
      <div class="meta">
        <span v-for="item in metaItems" :key="item.label" class="meta-item">
          <span class="meta-label">{{ item.label }}</span>
          <span class="meta-value">{{ item.value }}</span>
        </span>
      </div>
      <el-alert
        v-if="inferred"
        type="warning"
        :title="inferred"
        :closable="false"
        show-icon
      />

      <div class="toolbar">
        <el-select
          v-if="containers.length > 1"
          v-model="form.container"
          placeholder="默认容器(第一个)"
          clearable
          class="container-select"
          @change="query"
        >
          <el-option
            v-for="name in containers"
            :key="name"
            :label="name"
            :value="name"
          />
        </el-select>
        <el-input
          v-model="form.keys"
          class="keys-input"
          clearable
          maxlength="500"
          placeholder="按配置名过滤,正则,不区分大小写,例如 datasource|redis"
          @keyup.enter="query"
          @clear="query"
        />
        <el-button type="primary" :icon="Search" @click="query">查询</el-button>
        <el-tooltip
          content="附带每一项配置来自哪里、打码的值从哪里补回等说明"
          placement="top"
        >
          <el-switch
            v-model="form.detail"
            active-text="配置来源"
            @change="query"
          />
        </el-tooltip>
        <el-tooltip
          content="不用接口的缓存,重新到 Pod 里采集一次"
          placement="top"
        >
          <el-button :icon="Refresh" @click="load(true)">重新采集</el-button>
        </el-tooltip>
        <span class="spacer" />
        <el-checkbox v-model="wrap">自动换行</el-checkbox>
        <el-button-group>
          <el-button
            :disabled="!text"
            title="在内容里查找,同 Ctrl+F"
            @click="viewRef?.find()"
            >查找</el-button
          >
          <el-button :disabled="!text" @click="viewRef?.expandAll()"
            >全部展开</el-button
          >
          <el-button :disabled="!text" @click="viewRef?.collapseAll()"
            >全部折叠</el-button
          >
        </el-button-group>
        <el-button :icon="CopyDocument" :disabled="!text" @click="copy"
          >复制</el-button
        >
      </div>

      <el-alert
        v-if="error"
        :type="errorCode === 'timeout' ? 'warning' : 'error'"
        :title="error"
        :closable="false"
        show-icon
      />
      <div
        v-loading="loading"
        :element-loading-text="loadingText"
        class="content"
      >
        <JsonView
          v-if="text"
          ref="viewRef"
          :value="text"
          :word-wrap="wrap"
          height="100%"
        />
        <el-empty
          v-else-if="!loading"
          :image-size="80"
          description="没有取到配置"
        />
      </div>
    </div>
  </el-dialog>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref, shallowRef } from "vue";
import dayjs from "dayjs";
import { CopyDocument, Refresh, Search } from "@element-plus/icons-vue";
import { getRuntimeConfig, type RuntimeConfigResult } from "@/api/monit";
import { copyText } from "@/utils/clipboard";
import { message } from "@/utils/message";
import JsonView from "@/components/JsonView.vue";

defineOptions({ name: "RuntimeConfigDialog" });

// 接口约定见「系统管理 → 获取运行配置接口」,master 调用(func_manager/runtime_config.py);
// 返回的 JSON 原样展示(含明文密码,要「查看运行配置」权限)

type ContainerInput =
  | string
  | { name?: string; is_init?: boolean }
  | null
  | undefined;

const LANG_LABELS: Record<string, string> = {
  java: "Java",
  node: "Node.js",
  python: "Python",
  native: "C / C++ / Go"
};
const CENTER_LABELS: Record<string, string> = {
  "image-files": "镜像里的文件",
  none: "没用配置中心",
  unknown: "未知"
};

const visible = ref(false);
const loading = ref(false);
const target = reactive({ env: "", namespace: "", deployment: "", pod: "" });
const form = reactive({ container: "", keys: "", detail: false });
/** Pod 的业务容器(不含 init 容器):多于一个时可以选 */
const containers = ref<string[]>([]);
// 配置可能有几 MB,不做深层响应式
const result = shallowRef<RuntimeConfigResult | null>(null);
const text = ref("");
const error = ref("");
const errorCode = ref("");
const wrap = ref(true);
const waited = ref(0);
const viewRef = ref<InstanceType<typeof JsonView> | null>(null);
// 只认最后一次请求:关掉后或者重新查询时,之前没回来的结果丢掉
let seq = 0;
let timer = 0;

const str = (value: unknown) =>
  typeof value === "string" || typeof value === "number"
    ? String(value).trim()
    : "";

function formatTime(value: unknown) {
  const raw = str(value);
  if (!raw) return "";
  const time = dayjs(raw);
  return time.isValid() ? time.format("YYYY-MM-DD HH:mm:ss") : raw;
}

function formatElapsed(ms: number) {
  return ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)} 秒`;
}

/** 顶部的服务信息:返回里有的就显示,还没返回时显示要看的是哪个服务 */
const metaItems = computed(() => {
  const data = result.value;
  const cfg: Record<string, any> =
    data?.config && typeof data.config === "object" ? data.config : {};
  const items: { label: string; value: string }[] = [];
  const add = (label: string, value: string) => {
    if (value) items.push({ label, value });
  };
  add("集群", str(cfg.cluster) || target.env);
  add("命名空间", str(cfg.namespace) || target.namespace);
  add("Deployment", str(cfg.deployment) || target.deployment);
  // pod 是 null:Deployment 的副本数为 0,没有 Pod
  add(
    "Pod",
    cfg.pod === null ? "没有 Pod(副本数为 0)" : str(cfg.pod) || target.pod
  );
  add("容器", str(cfg.container));
  const lang = str(cfg.lang);
  add("语言", LANG_LABELS[lang] || str(cfg.lang_label) || lang);
  if (Array.isArray(cfg.centers)) {
    add(
      "配置来源",
      cfg.centers
        .map(str)
        .filter(Boolean)
        .map(name => CENTER_LABELS[name] || name)
        .join("、")
    );
  }
  add("采集时间", formatTime(cfg.collected_at));
  if (typeof cfg.cached === "boolean")
    add("是否缓存", cfg.cached ? "是" : "否");
  if (data && typeof data.elapsed_ms === "number") {
    add(
      "耗时",
      formatElapsed(data.elapsed_ms) +
        (data.attempts > 1 ? `(请求 ${data.attempts} 次)` : "")
    );
  }
  // 镜像很长,放最后,换行时不留空档
  add("镜像", str(cfg.image));
  return items;
});

/** 接口拿不到运行中的进程(容器没在运行、actuator 不通)时按配置来源推断,值不一定是进程里实际用的 */
const inferred = computed(() => {
  const cfg = result.value?.config;
  if (!cfg || typeof cfg !== "object" || cfg.basis !== "inferred") return "";
  const reason = str(cfg.reason);
  return `不是从运行中的进程取到的,按配置来源推断${reason ? `:${reason}` : ""}`;
});

const loadingText = computed(() =>
  waited.value >= 3
    ? `正在获取运行配置,已等待 ${waited.value} 秒(首次采集要几秒,接口忙时会多等一会儿)`
    : "正在获取运行配置…"
);

function startTimer() {
  stopTimer();
  waited.value = 0;
  timer = window.setInterval(() => waited.value++, 1000);
}

function stopTimer() {
  if (timer) window.clearInterval(timer);
  timer = 0;
}

function errorMessage(err: any): string {
  const data = err?.response?.data;
  if (data && typeof data === "object" && (data.message || data.error)) {
    return data.message || data.error;
  }
  if (err?.code === "ECONNABORTED") return "请求超时,请稍后重试";
  const status = err?.response?.status;
  return status ? `获取失败(HTTP ${status})` : "获取失败,请检查网络后重试";
}

function toText(config: unknown) {
  try {
    return JSON.stringify(config, null, 2) ?? "";
  } catch {
    return String(config ?? "");
  }
}

/** 同一个服务重新查询时,旧内容留在加载遮罩下面,拿到新结果再换;出错就清掉,不让旧内容和错误混在一起 */
async function load(refresh = false) {
  const current = ++seq;
  loading.value = true;
  error.value = "";
  errorCode.value = "";
  startTimer();
  try {
    const res = await getRuntimeConfig({
      env: target.env,
      namespace: target.namespace,
      deployment: target.deployment || undefined,
      pod: target.pod || undefined,
      container: form.container || undefined,
      keys: form.keys.trim() || undefined,
      detail: form.detail,
      refresh
    });
    if (current !== seq) return;
    if (!res?.success || !res.data) {
      result.value = null;
      text.value = "";
      error.value = res?.message || "获取失败";
      return;
    }
    result.value = res.data;
    text.value = toText(res.data.config);
  } catch (err: any) {
    if (current !== seq) return;
    result.value = null;
    text.value = "";
    errorCode.value = err?.response?.data?.code ?? "";
    error.value = errorMessage(err);
  } finally {
    if (current === seq) {
      loading.value = false;
      stopTimer();
    }
  }
}

function query() {
  load(false);
}

async function copy() {
  if (await copyText(text.value)) {
    message("已复制", { type: "success" });
  } else {
    message("复制失败,可以点一下内容,按 Ctrl+A 全选后再复制", {
      type: "warning"
    });
  }
}

/** deployment 和 pod 给一个;containers 是 Pod 的容器(Pod 列表里的格式),多个时可以在弹窗里选 */
function open(row: {
  env: string;
  namespace: string;
  deployment?: string;
  pod?: string;
  containers?: ContainerInput[];
}) {
  target.env = row.env;
  target.namespace = row.namespace;
  target.deployment = row.pod ? "" : (row.deployment ?? "");
  target.pod = row.pod ?? "";
  containers.value = (row.containers ?? [])
    .map(item =>
      typeof item === "string" ? item : item && !item.is_init ? item.name : ""
    )
    .filter((name): name is string => !!name);
  form.container = "";
  form.keys = "";
  form.detail = false;
  result.value = null;
  text.value = "";
  visible.value = true;
  load();
}

function reset() {
  // 关闭动画还没结束就又打开了:不能把新打开的这次清掉
  if (visible.value) return;
  seq++;
  loading.value = false;
  stopTimer();
  // 关掉后释放内容(可能有几 MB)和编辑器
  result.value = null;
  text.value = "";
  error.value = "";
  errorCode.value = "";
}

onBeforeUnmount(stopTimer);

defineExpose({ open });
</script>

<style lang="scss" scoped>
.runtime-config {
  display: flex;
  flex-direction: column;
  gap: 8px;
  height: 100%;
  min-height: 0;
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
  color: var(--el-text-color-primary);
  word-break: break-all;
}

.toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}

.container-select {
  width: 200px;
}

.keys-input {
  width: 420px;
}

.spacer {
  flex: 1;
}

.content {
  position: relative;
  flex: 1;
  min-height: 0;
}
</style>

<style lang="scss">
// 弹窗挂在 body 上,scoped 样式管不到:整体占浏览器 95% 的宽高,正文撑满剩下的高度
.runtime-config-dialog {
  display: flex;
  flex-direction: column;
  height: 95vh;
  margin-bottom: 0;

  .el-dialog__body {
    flex: 1;
    min-height: 0;
  }
}
</style>
