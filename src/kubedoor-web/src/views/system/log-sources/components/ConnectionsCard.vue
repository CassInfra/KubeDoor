<script setup lang="ts">
// 「连接」卡片：ClickHouse / Elasticsearch 连接的表格，编辑、测试、扫描、删除
import { computed, reactive, ref } from "vue";
import { ElMessageBox } from "element-plus";
import { message } from "@/utils/message";
import { type LogConnectionType, testLogConnection } from "@/api/system";
import ConnectionDialog from "./ConnectionDialog.vue";
import {
  type ConnectionForm,
  type InfoRow,
  type LogSourcesForm,
  MAX_CONNECTIONS,
  authText,
  connectionErrors,
  connectionUsage,
  emptyConnection,
  errorText,
  removeConnection,
  requestTimeoutMs,
  testPayload,
  testRows,
  testSummary,
  typeLabel,
  upsertConnection
} from "../log-sources-utils";

defineOptions({ name: "LogConnectionsCard" });

const form = defineModel<LogSourcesForm>("form", { required: true });

const emit = defineEmits<{
  /** 点了某个连接的「扫描」：交给「智能配置」卡片 */
  scan: [id: string];
  /** 没保存过的连接改了 ID：扫描结果跟着换 key */
  renamed: [from: string, to: string];
}>();

const dialog = reactive({
  visible: false,
  creating: false,
  connection: null as ConnectionForm | null
});
const usedIds = computed(() =>
  form.value.connections
    .filter(item => item.rowId !== dialog.connection?.rowId)
    .map(item => item.id)
);
const testingId = ref("");
const result = ref<{
  id: string;
  ok: boolean;
  text: string;
  rows: InfoRow[];
} | null>(null);

function add(type: LogConnectionType) {
  dialog.connection = emptyConnection(
    type,
    form.value.connections.map(item => item.id)
  );
  dialog.creating = true;
  dialog.visible = true;
}

function edit(conn: ConnectionForm) {
  dialog.connection = conn;
  dialog.creating = false;
  dialog.visible = true;
}

function onSave(conn: ConnectionForm, originalId: string | null) {
  Object.assign(form.value, upsertConnection(form.value, conn, originalId));
  if (originalId && originalId !== conn.id) {
    emit("renamed", originalId, conn.id);
    if (result.value?.id === originalId) result.value = null;
  }
  message(
    `${originalId ? "已修改" : "已添加"}连接 ${conn.id}，点底部的「保存」后生效`,
    { type: "success" }
  );
}

async function remove(conn: ConnectionForm) {
  const usage = connectionUsage(form.value, conn.id);
  if (usage.sources.length || usage.bindings.length) {
    const where = [
      usage.sources.length ? `日志源 ${usage.sources.join("、")}` : "",
      usage.bindings.length ? `集群绑定 ${usage.bindings.join("、")}` : ""
    ]
      .filter(Boolean)
      .join("；");
    message(
      `连接 ${conn.id} 还在被使用（${where}），先把它们改用别的连接再删除`,
      { type: "warning", duration: 5000 }
    );
    return;
  }
  try {
    await ElMessageBox.confirm(
      `删除连接「${conn.name || conn.id}」？点底部的「保存」后生效${
        conn.saved ? "，已保存的密码和请求头一起删除" : ""
      }`,
      "删除连接",
      { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" }
    );
  } catch {
    return;
  }
  Object.assign(form.value, removeConnection(form.value, conn.id));
  if (result.value?.id === conn.id) result.value = null;
}

async function test(conn: ConnectionForm) {
  if (testingId.value) return;
  const problems = connectionErrors(conn);
  if (problems.length) {
    message(`连接 ${conn.id} · ${problems[0]}：点「编辑」修改后再测试`, {
      type: "warning"
    });
    return;
  }
  testingId.value = conn.id;
  result.value = null;
  try {
    const res = await testLogConnection(
      testPayload(form.value, conn.id),
      conn.id,
      requestTimeoutMs(conn.timeout, 2)
    );
    result.value = {
      id: conn.id,
      ok: true,
      text: `${conn.name || conn.id}：${testSummary(res.data)}`,
      rows: testRows(res.data)
    };
  } catch (reason) {
    result.value = {
      id: conn.id,
      ok: false,
      text: `${conn.name || conn.id}：连接失败，${errorText(reason, "未知原因")}`,
      rows: []
    };
  } finally {
    testingId.value = "";
  }
}

function scan(conn: ConnectionForm) {
  const problems = connectionErrors(conn);
  if (problems.length) {
    message(`连接 ${conn.id} · ${problems[0]}：点「编辑」修改后再扫描`, {
      type: "warning"
    });
    return;
  }
  emit("scan", conn.id);
}
</script>

<template>
  <el-card shadow="never" class="log-card">
    <template #header>
      <div class="card-header">
        <strong>连接</strong>
        <div class="row-actions">
          <el-button
            size="small"
            type="primary"
            plain
            :disabled="form.connections.length >= MAX_CONNECTIONS"
            @click="add('clickhouse')"
          >
            添加 ClickHouse
          </el-button>
          <el-button
            size="small"
            type="primary"
            plain
            :disabled="form.connections.length >= MAX_CONNECTIONS"
            @click="add('elasticsearch')"
          >
            添加 Elasticsearch
          </el-button>
        </div>
      </div>
    </template>
    <p class="card-note">
      日志存在哪：KubeDoor
      控制端（kubedoor-master）直接访问这些地址查日志，要能从 master 所在的 Pod
      连上。ClickHouse 用 HTTP 接口；Elasticsearch 7.10 及以上，OpenSearch
      也可以（不能用 ES|QL）。一个连接可以给多个日志源用。
    </p>
    <el-table
      :data="form.connections"
      row-key="rowId"
      border
      size="small"
      empty-text="还没有连接：点右上角添加 ClickHouse 或 Elasticsearch"
    >
      <el-table-column label="名称" min-width="170">
        <template #default="{ row }">
          <span>{{ row.name || "（未命名）" }}</span>
          <el-tag
            v-if="!row.saved"
            size="small"
            type="warning"
            effect="plain"
            class="gap"
          >
            未保存
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="ID" width="120">
        <template #default="{ row }">
          <code>{{ row.id }}</code>
        </template>
      </el-table-column>
      <el-table-column label="类型" width="120">
        <template #default="{ row }">{{ typeLabel(row.type) }}</template>
      </el-table-column>
      <el-table-column
        prop="url"
        label="地址"
        min-width="230"
        show-overflow-tooltip
      />
      <el-table-column label="认证" min-width="160" show-overflow-tooltip>
        <template #default="{ row }">{{ authText(row) }}</template>
      </el-table-column>
      <el-table-column label="原生查询" width="90">
        <template #default="{ row }">
          <span :class="row.allow_raw ? '' : 'muted'">
            {{ row.allow_raw ? "允许" : "不允许" }}
          </span>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="200" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" @click="edit(row)">编辑</el-button>
          <el-button
            link
            type="primary"
            :loading="testingId === row.id"
            @click="test(row)"
          >
            测试
          </el-button>
          <el-button link type="primary" @click="scan(row)">扫描</el-button>
          <el-button link type="danger" @click="remove(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>
    <template v-if="result">
      <el-alert
        :type="result.ok ? 'success' : 'error'"
        :title="result.text"
        show-icon
        class="block-alert"
        @close="result = null"
      />
      <el-descriptions
        v-if="result.rows.length"
        :column="2"
        border
        size="small"
        class="test-rows"
      >
        <el-descriptions-item
          v-for="row in result.rows"
          :key="row.label"
          :label="row.label"
        >
          <span :class="{ 'warn-text': row.warn }">{{ row.value }}</span>
        </el-descriptions-item>
      </el-descriptions>
    </template>
    <ConnectionDialog
      v-model:visible="dialog.visible"
      :connection="dialog.connection"
      :creating="dialog.creating"
      :used-ids="usedIds"
      @save="onSave"
    />
  </el-card>
</template>

<style scoped lang="scss">
.gap {
  margin-left: 6px;
}

.test-rows {
  :deep(.el-descriptions__label) {
    width: 90px;
    white-space: nowrap;
  }
}

.warn-text {
  color: var(--el-color-warning);
}
</style>
