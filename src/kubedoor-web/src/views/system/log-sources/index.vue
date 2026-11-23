<script setup lang="ts">
import {
  computed,
  nextTick,
  onBeforeUnmount,
  onMounted,
  reactive,
  ref
} from "vue";
import { onBeforeRouteLeave } from "vue-router";
import dayjs from "dayjs";
import { ElMessageBox } from "element-plus";
import { message } from "@/utils/message";
import {
  type LogSourcesSettings,
  getLogSourcesConfig,
  saveLogSourcesConfig
} from "@/api/system";
import ArchitectureCard from "./components/ArchitectureCard.vue";
import ClustersCard from "./components/ClustersCard.vue";
import ConnectionsCard from "./components/ConnectionsCard.vue";
import CorrelationsCard from "./components/CorrelationsCard.vue";
import SmartConfigCard from "./components/SmartConfigCard.vue";
import SourcesCard from "./components/SourcesCard.vue";
import {
  type FormError,
  type ScanMap,
  HELP_URL,
  builtinMaskNames,
  emptyForm,
  errorText,
  formFromSettings,
  formPayload,
  payloadKey,
  semanticFields,
  validateForm
} from "./log-sources-utils";

defineOptions({ name: "SystemLogSources" });

const loading = ref(false);
// 读取成功过才能保存：读取失败时不能用空表单覆盖已保存的配置
const loaded = ref(false);
const loadError = ref("");
const saving = ref(false);
const switching = ref(false);
const settings = ref<LogSourcesSettings | null>(null);
// 子组件用 v-model:form 拿到同一个对象，只改它的属性（不整体替换）
const form = reactive(emptyForm());
// 加载 / 保存后的表单内容，用来判断有没有未保存的修改
const snapshot = ref("");
const updatedBy = ref("");
const updatedAt = ref("");
/** 各连接最近一次的扫描结果：日志源、集群绑定的字段和取值候选也用它 */
const scans = ref<ScanMap>({});
/** 「集群」卡片里选中的集群 */
const selectedEnv = ref("");
const smart = ref<InstanceType<typeof SmartConfigCard> | null>(null);

const busy = computed(() => loading.value || saving.value);
const dirty = computed(
  () => loaded.value && payloadKey(form) !== snapshot.value
);
const known = computed(() => settings.value?.clusters_known ?? []);
const semantic = computed(() => semanticFields(settings.value));
const builtinMasks = computed(() => builtinMaskNames(settings.value));
const overview = computed(() => {
  const clusters = form.clusters.filter(item => item.bindings.length).length;
  return `${form.connections.length} 个连接 · ${form.sources.length} 个日志源 · ${clusters} 个集群有绑定`;
});
const modifiedText = computed(() =>
  updatedAt.value
    ? `上次修改：${updatedBy.value || "-"} · ${dayjs(updatedAt.value).format("YYYY-MM-DD HH:mm:ss")}`
    : "尚未保存过"
);

// ---------------------------------------------------------------------------
// 读取和保存
// ---------------------------------------------------------------------------
function applySettings(data: LogSourcesSettings) {
  settings.value = data;
  Object.assign(form, formFromSettings(data));
  snapshot.value = payloadKey(form);
  updatedBy.value = data.updated_by ?? "";
  updatedAt.value = data.updated_at ?? "";
}

async function load() {
  if (busy.value) return;
  loading.value = true;
  loadError.value = "";
  try {
    const res = await getLogSourcesConfig();
    applySettings(res.data);
    loaded.value = true;
  } catch (reason) {
    loadError.value = errorText(reason, "读取历史日志配置失败");
  } finally {
    loading.value = false;
  }
}

async function confirmDiscard(): Promise<boolean> {
  try {
    await ElMessageBox.confirm("有未保存的修改，是否放弃？", "未保存的修改", {
      confirmButtonText: "放弃修改",
      cancelButtonText: "继续编辑",
      type: "warning"
    });
    return true;
  } catch {
    return false;
  }
}

async function reset() {
  if (dirty.value && !(await confirmDiscard())) return;
  await load();
}

function scrollTo(section: string) {
  document
    .getElementById(`log-sources-${section}`)
    ?.scrollIntoView({ behavior: "smooth", block: "start" });
}

/** 提示第一个错误并滚动到它所在的卡片；出错的是集群绑定时先选中那个集群 */
function showErrors(errors: FormError[]) {
  const first = errors[0];
  if (first.section === "clusters" && first.env) selectedEnv.value = first.env;
  message(
    errors.length > 1
      ? `${first.text}（另有 ${errors.length - 1} 处要修改）`
      : first.text,
    { type: "warning", duration: 5000 }
  );
  void nextTick(() => scrollTo(first.section));
}

/** 保存表单内容（不改启用状态，启用由右上角的开关单独控制） */
async function save() {
  if (!loaded.value || busy.value) return;
  const errors = validateForm(form);
  if (errors.length) {
    showErrors(errors);
    return;
  }
  saving.value = true;
  try {
    const res = await saveLogSourcesConfig(formPayload(form));
    applySettings(res.data);
    message(
      res.data.enabled
        ? "已保存，立即生效"
        : "已保存。打开右上角的开关后 AI 才能查询历史日志",
      { type: "success", duration: 4000 }
    );
  } catch {
    // 接口返回的原因（例如 400 某一项不合法）http 封装已经弹出提示
  } finally {
    saving.value = false;
  }
}

/** 右上角的开关：拨动立即保存，只传 enabled，表单里没保存的修改保留 */
async function toggleEnabled(): Promise<boolean> {
  const next = !form.enabled;
  if (next && !settings.value?.connections?.length) {
    message("先添加连接、日志源并绑定集群，点底部的「保存」后再打开", {
      type: "warning"
    });
    return false;
  }
  switching.value = true;
  try {
    const res = await saveLogSourcesConfig({ enabled: next });
    if (settings.value) settings.value.enabled = !!res.data.enabled;
    updatedBy.value = res.data.updated_by ?? "";
    updatedAt.value = res.data.updated_at ?? "";
    message(next ? "已启用" : "已停用", { type: "success" });
    return true;
  } catch {
    return false;
  } finally {
    switching.value = false;
  }
}

/** 「连接」卡片里点了「扫描」：滚到「智能配置」并开始扫描 */
function scanConnection(id: string) {
  scrollTo("smart");
  void smart.value?.start(id);
}

/** 没保存过的连接改了 ID：扫描结果跟着换 key 和连接 ID */
function onRenamed(from: string, to: string) {
  const scan = scans.value[from];
  if (!scan) return;
  const next = {
    ...scans.value,
    [to]: { ...scan, connection: { ...scan.connection, id: to } }
  };
  delete next[from];
  scans.value = next;
}

// ---------------------------------------------------------------------------
// 离开页面前提醒未保存的修改
// ---------------------------------------------------------------------------
function beforeUnload(event: BeforeUnloadEvent) {
  if (!dirty.value && !saving.value) return;
  event.preventDefault();
  event.returnValue = "";
}

onBeforeRouteLeave(async () => {
  if (saving.value) {
    message("正在保存，请稍候", { type: "info" });
    return false;
  }
  return !dirty.value || (await confirmDiscard());
});
onMounted(() => {
  void load();
  window.addEventListener("beforeunload", beforeUnload);
});
onBeforeUnmount(() => window.removeEventListener("beforeunload", beforeUnload));
</script>

<template>
  <div class="system-log-sources">
    <div class="page-heading">
      <div>
        <h2>历史日志</h2>
        <p>
          给 AI 助手查容器标准输出之外的历史日志：登记 ClickHouse /
          Elasticsearch 里的访问日志、网关日志、应用日志，再按集群绑定表 /
          索引。AI
          在某个集群里提问时只能查这个集群绑定了的日志源；对集群有只读及以上权限的账号都可以让
          AI 查。右上角的开关随时停用 / 启用，立即生效。
          <a
            :href="HELP_URL"
            target="_blank"
            rel="noopener noreferrer"
            class="link"
            >配置说明</a
          >
        </p>
      </div>
      <div class="heading-actions">
        <!-- 拨动立即生效：只改启用状态，表单里没保存的修改不受影响 -->
        <el-switch
          v-model="form.enabled"
          :before-change="toggleEnabled"
          :loading="switching"
          :disabled="!loaded"
          active-text="启用"
          inactive-text="停用"
        />
      </div>
    </div>

    <el-alert
      v-if="loadError"
      type="error"
      :closable="false"
      show-icon
      class="page-alert"
      :title="`读取历史日志配置失败：${loadError}`"
    >
      <el-button size="small" :loading="loading" @click="load">
        重新读取
      </el-button>
    </el-alert>

    <div v-loading="loading" class="cards">
      <el-empty
        v-if="!loaded && !loading"
        description="还没有读取到配置，请点「重新读取」"
      />
      <template v-if="loaded">
        <el-card id="log-sources-intro" shadow="never" class="log-card">
          <template #header><strong>说明</strong></template>
          <ul class="rules first">
            <li>
              AI
              能做什么：按时间、服务、关键字查日志明细和统计趋势，把报错归并成几类，按请求
              ID 把入口、网关、服务各层的日志串成一次请求；日志里的链路 ID
              可以接着用链路追踪查整条链路
            </li>
            <li>
              配置步骤：① 添加连接 → ② 用「智能配置」扫描库表 /
              索引，生成日志源和集群绑定的草稿 → ③ 检查日志源的字段和说明 → ④
              在「集群」里核对每个集群的表 /
              索引和过滤条件，点「预览」看实际执行的查询 → ⑤
              保存，打开右上角的开关
            </li>
            <li>
              集群和库表的对应关系只由「集群」里的绑定决定（不按名称猜）：AI
              的每次查询都会带上绑定的表 / 索引和过滤条件，绕不过去
            </li>
          </ul>
          <el-alert
            type="warning"
            :closable="false"
            show-icon
            class="block-alert"
            title="日志内容会作为对话上下文发给模型服务商（敏感字段打码后）：请先确认符合公司的数据安全要求再启用"
          />
          <el-alert type="info" :closable="false" show-icon class="block-alert">
            <template #title>
              建议给 KubeDoor 建只读账号：ClickHouse 用只读用户，只授权日志库的
              SELECT；Elasticsearch 只给日志索引的读权限。做法见
              <a
                :href="HELP_URL"
                target="_blank"
                rel="noopener noreferrer"
                class="link"
                >帮助文档</a
              >
            </template>
          </el-alert>
        </el-card>

        <ConnectionsCard
          id="log-sources-connections"
          v-model:form="form"
          @scan="scanConnection"
          @renamed="onRenamed"
        />
        <SmartConfigCard
          id="log-sources-smart"
          ref="smart"
          v-model:form="form"
          v-model:scans="scans"
          :known="known"
        />
        <SourcesCard
          id="log-sources-sources"
          v-model:form="form"
          :scans="scans"
          :semantic="semantic"
          :builtin-masks="builtinMasks"
        />
        <ClustersCard
          id="log-sources-clusters"
          v-model:form="form"
          v-model:selected="selectedEnv"
          :known="known"
          :scans="scans"
        />
        <CorrelationsCard id="log-sources-correlations" v-model:form="form" />
        <ArchitectureCard id="log-sources-architecture" v-model:form="form" />

        <!-- 保存：内容超过一屏时固定在底部 -->
        <el-affix position="bottom" :offset="0">
          <div class="save-bar">
            <el-button type="primary" :loading="saving" @click="save">
              保存
            </el-button>
            <el-button :disabled="busy" @click="reset">重置</el-button>
            <el-tag v-if="dirty" type="warning" effect="plain">
              有未保存的修改
            </el-tag>
            <span class="hint inline">
              {{ overview }} · {{ form.enabled ? "已启用" : "未启用" }} ·
              {{ modifiedText }}
            </span>
          </div>
        </el-affix>
      </template>
    </div>
  </div>
</template>

<style scoped lang="scss">
.system-log-sources {
  max-width: 1280px;
  padding: 16px 16px 0;

  @media (width <= 640px) {
    padding: 8px 8px 0;
  }
}

.page-heading {
  display: flex;
  flex-wrap: wrap;
  gap: 12px 24px;
  align-items: flex-start;
  justify-content: space-between;
  margin-bottom: 16px;

  h2 {
    margin: 0 0 6px;
    font-size: 22px;
    font-weight: 600;
  }

  p {
    max-width: 960px;
    margin: 0;
    line-height: 1.7;
    color: var(--el-text-color-secondary);
  }
}

.heading-actions {
  display: flex;
  gap: 8px;
  align-items: center;
}

// el-alert 自带 margin: 0，工具类压不过，用带作用域的类
.page-alert {
  margin-bottom: 16px;
}

.cards {
  min-height: 120px;
}

.save-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 12px;
  align-items: center;
  padding: 12px 16px;
  margin-bottom: 16px;
  background: var(--el-bg-color);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 4px;
  box-shadow: 0 -2px 8px rgb(0 0 0 / 6%);

  .hint.inline {
    margin-left: 0;
  }
}
</style>

<style lang="scss">
// 各卡片（子组件）共用的样式：限定在这个页面里（对话框、抽屉都渲染在页面内）
.system-log-sources {
  .log-card {
    margin-bottom: 16px;
    scroll-margin-top: 12px;
  }

  .card-header {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    align-items: center;
    justify-content: space-between;
  }

  .row-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    align-items: center;

    .el-button + .el-button {
      margin-left: 0;
    }
  }

  .card-note {
    margin: 0 0 12px;
    font-size: 13px;
    line-height: 1.7;
    color: var(--el-text-color-regular);
  }

  // 提示独占一行；inline 的跟在控件后面
  .hint {
    display: block;
    width: 100%;
    font-size: 12px;
    line-height: 1.6;
    color: var(--el-text-color-secondary);

    &.inline {
      display: inline;
      width: auto;
      margin-left: 8px;
    }
  }

  .rules {
    padding-left: 20px;
    margin: 8px 0 4px;
    font-size: 13px;
    line-height: 1.8;
    color: var(--el-text-color-regular);
    list-style: disc;

    &.first {
      margin-top: 0;
      margin-bottom: 12px;
    }
  }

  .section-title {
    margin: 16px 0 8px;
    font-weight: 600;
  }

  .section-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    align-items: center;
    justify-content: space-between;
  }

  .block-alert {
    margin: 12px 0 0;
  }

  .tag-list {
    display: flex;
    flex-wrap: wrap;
    gap: 4px 6px;
  }

  .muted {
    color: var(--el-text-color-secondary);
  }

  .danger-text {
    color: var(--el-color-danger);
  }

  .warning-text {
    color: var(--el-color-warning);
  }

  .link {
    color: var(--el-color-primary);

    &:hover {
      text-decoration: underline;
    }
  }

  .mono {
    font-family: Menlo, Consolas, monospace;
    font-size: 12px;
  }

  code {
    padding: 0 4px;
    font-family: Menlo, Consolas, monospace;
    font-size: 12px;
    background: var(--el-fill-color-light);
    border-radius: 3px;
  }
}
</style>
