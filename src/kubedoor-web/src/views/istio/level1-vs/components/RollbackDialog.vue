<script setup lang="ts">
import { computed, h as vnode, ref, watch } from "vue";
import { ElMessageBox } from "element-plus";
import { Refresh } from "@element-plus/icons-vue";
import { message } from "@/utils/message";
import {
  type EnvType,
  type VSBackup,
  type VSBackups,
  getIstioBackups,
  rollbackIstioLiveVS
} from "@/api/istio";
import MonacoDiff from "./MonacoDiff.vue";
import {
  BACKUP_ACTION_LABEL,
  ENV_TYPE_LABEL,
  ENV_TYPE_TAG,
  formatTime,
  toYaml
} from "../utils";

defineOptions({ name: "IstioRollbackDialog" });

/** 回退集群里的 VirtualService:花折下发、回退、删除它之前,都先备份集群里当时的内容(每个集群留最近几份);
 *  选一份,看过和集群现状的差异后写回集群。只改集群,不改组标准 */
const props = defineProps<{
  groupId: number;
  k8sName: string;
  envType?: EnvType;
  namespace: string;
  name: string;
  /** 组标准部署到了这个集群:回退后下次下发会再用组标准覆盖 */
  bound: boolean;
}>();
const emit = defineEmits<{ (e: "done"): void }>();
const visible = defineModel<boolean>({ default: false });

const loading = ref(false);
const rolling = ref(false);
const data = ref<VSBackups | null>(null);
const error = ref("");
const selectedId = ref<number | null>(null);

const backups = computed(() => data.value?.backups ?? []);
const live = computed(() => data.value?.live ?? null);
const selected = computed(
  () => backups.value.find(b => b.id === selectedId.value) ?? null
);
const target = computed(() => `${props.namespace}/${props.name}`);

async function load() {
  loading.value = true;
  error.value = "";
  try {
    const res = await getIstioBackups(
      props.groupId,
      props.k8sName,
      props.namespace,
      props.name
    );
    data.value = res.data;
    // 默认选最近一份和现状不一样的(一般就是最近一次下发前的);刷新后保持原来的选择
    const list = res.data.backups;
    if (!list.some(b => b.id === selectedId.value)) {
      selectedId.value = (list.find(b => !b.same) ?? list[0])?.id ?? null;
    }
  } catch (e: any) {
    data.value = null;
    error.value = e?.response?.data?.error || "取备份失败,请稍后重试";
  } finally {
    loading.value = false;
  }
}

watch(
  visible,
  open => {
    if (!open) return;
    data.value = null;
    selectedId.value = null;
    load();
  },
  { immediate: true }
);

/** 「2026-10-26 10:00:00 下发前」 */
const pointOf = (b: VSBackup) =>
  `${formatTime(b.created_at)} ${BACKUP_ACTION_LABEL[b.action].label}`;

function summary(b: VSBackup) {
  if (!b.spec) return "集群里还没有这个 VirtualService";
  const hosts = b.spec.hosts?.length ?? 0;
  const http = b.spec.http?.length ?? 0;
  return `${hosts} 个 hosts、${http} 条 http 规则`;
}

function select(row: VSBackup) {
  selectedId.value = row.id;
}

const rowClass = ({ row }: { row: VSBackup }) =>
  row.id === selectedId.value ? "row-selected" : "";

async function rollback(b: VSBackup) {
  const lines = [
    vnode(
      "p",
      null,
      b.spec
        ? `把集群 ${props.k8sName} 里的 ${target.value} 回退到 ${pointOf(b)}的内容?`
        : `${pointOf(b)}集群 ${props.k8sName} 里还没有 ${target.value},回退会把它从集群删除,依赖它的域名路由会失效。`
    ),
    props.envType === "prod"
      ? vnode(
          "p",
          { style: "color: var(--el-color-danger); font-weight: 600" },
          "这是生产集群。"
        )
      : null,
    vnode("p", null, "回退前会先备份集群现在的内容,回退错了可以再回退回来。"),
    props.bound
      ? vnode(
          "p",
          null,
          "只改集群,不改组标准:之后这个集群的状态是「不一致」,下次下发会再用组标准覆盖。"
        )
      : null
  ];
  await ElMessageBox.confirm(
    vnode("div", null, lines),
    "回退集群里的 VirtualService",
    {
      type: "warning",
      confirmButtonText: "回退",
      confirmButtonClass: "el-button--danger"
    }
  );
  rolling.value = true;
  try {
    const res = await rollbackIstioLiveVS(
      props.groupId,
      props.k8sName,
      props.namespace,
      props.name,
      b.id
    );
    message(res.msg || "已回退", { type: "success" });
    emit("done");
  } finally {
    rolling.value = false;
    await load();
  }
}
</script>

<template>
  <el-dialog
    v-model="visible"
    :title="`回退 · ${target}`"
    width="80%"
    top="6vh"
    :close-on-click-modal="false"
    destroy-on-close
  >
    <div v-loading="loading" class="rb-wrap">
      <div class="rb-bar">
        <el-tag v-if="envType" size="small" :type="ENV_TYPE_TAG[envType]">{{
          ENV_TYPE_LABEL[envType]
        }}</el-tag>
        <b>{{ k8sName }}</b>
        <span class="sub"
          >每个集群保留最近
          {{ data?.keep ?? 5 }}
          份备份,新的在前;点一行查看它和集群现状的差异</span
        >
        <span class="spacer" />
        <el-button :icon="Refresh" :loading="loading" @click="load"
          >刷新</el-button
        >
      </div>
      <el-alert type="info" :closable="false" show-icon>
        <template #title>
          <div>
            花折下发、回退、删除这个 VirtualService
            之前,都会先备份集群里当时的内容。回退就是把选中的备份写回集群(备份时集群里还没有的,回退就是删除);回退前也会备份,可以撤销。
          </div>
          <div v-if="bound">
            只改集群,不改组标准:回退后这个集群的状态是「不一致」,下次下发会再用组标准覆盖。
          </div>
        </template>
      </el-alert>
      <el-alert
        v-if="data?.live_error"
        type="error"
        :closable="false"
        show-icon
        :title="`取不到集群现状:${data.live_error}。备份照样列出,但现在不能回退`"
      />
      <el-empty v-if="error" :description="error" />
      <template v-else-if="data">
        <el-table
          :data="backups"
          border
          size="small"
          row-key="id"
          class="rb-table"
          :row-class-name="rowClass"
          empty-text="还没有备份:花折下发、回退、删除这个 VirtualService 之前才会备份"
          @row-click="select"
        >
          <el-table-column label="备份时间" width="170">
            <template #default="{ row }">{{
              formatTime(row.created_at)
            }}</template>
          </el-table-column>
          <el-table-column
            prop="created_by"
            label="执行人"
            width="150"
            show-overflow-tooltip
          />
          <el-table-column label="备份的是" width="130">
            <template #default="{ row }">
              <el-tag size="small" :type="BACKUP_ACTION_LABEL[row.action].type"
                >{{ BACKUP_ACTION_LABEL[row.action].label }}的内容</el-tag
              >
            </template>
          </el-table-column>
          <el-table-column label="内容" min-width="260">
            <template #default="{ row }">
              <span :class="{ sub: !row.exists }">{{ summary(row) }}</span>
              <span v-if="row.resource_version" class="sub ml-2"
                >resourceVersion {{ row.resource_version }}</span
              >
            </template>
          </el-table-column>
          <el-table-column label="和集群现状比" width="120" align="center">
            <template #default="{ row }">
              <span v-if="!live" class="sub">-</span>
              <el-tag v-else-if="row.same" size="small" type="success"
                >一样</el-tag
              >
              <el-tag v-else size="small" type="warning">有差异</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="110" align="center">
            <template #default="{ row }">
              <el-button
                link
                type="danger"
                :disabled="!live || row.same || rolling"
                @click.stop="rollback(row)"
                >回退到这里</el-button
              >
            </template>
          </el-table-column>
        </el-table>
        <div v-if="selected" class="rb-diff">
          <div class="rb-diff-head">
            <div>
              <b>集群现状</b>
              <span v-if="live?.resource_version" class="sub ml-2"
                >resourceVersion {{ live.resource_version }}</span
              >
              <span v-if="live && !live.exists" class="sub ml-2"
                >(集群里现在没有)</span
              >
            </div>
            <div>
              <b>回退后:{{ pointOf(selected) }}的内容</b>
              <el-tag
                v-if="!selected.spec"
                size="small"
                type="danger"
                class="ml-2"
                >回退会把它从集群删除</el-tag
              >
              <el-tag
                v-else-if="selected.same"
                size="small"
                type="success"
                class="ml-2"
                >和现状一样,回退不会有变化</el-tag
              >
            </div>
          </div>
          <div class="rb-diff-body">
            <MonacoDiff
              :original="toYaml(live?.spec, true)"
              :modified="toYaml(selected.spec, true)"
              height="100%"
            />
          </div>
        </div>
      </template>
    </div>
  </el-dialog>
</template>

<style scoped>
/* 对话框整体占浏览器高度的 88%:减去标题栏和上下内边距(73px) */
.rb-wrap {
  display: flex;
  flex-direction: column;
  gap: 8px;
  height: calc(88vh - 73px);
}

.rb-bar {
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

.rb-table {
  flex: none;
}

.rb-table :deep(.el-table__row) {
  cursor: pointer;
}

.rb-table :deep(.row-selected > td.el-table__cell) {
  background: var(--el-color-primary-light-9);
}

.rb-diff {
  display: flex;
  flex: 1;
  flex-direction: column;
  gap: 4px;
  min-height: 0;
}

.rb-diff-head {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
  font-size: 13px;
}

.rb-diff-body {
  flex: 1;
  min-height: 0;
}
</style>
