<script setup lang="ts">
// 「日志源」卡片：日志源表格（启用开关直接改）、编辑抽屉；删除日志源时连带删除各集群里它的绑定
import { computed, reactive } from "vue";
import { ElMessageBox } from "element-plus";
import { message } from "@/utils/message";
import SourceDrawer from "./SourceDrawer.vue";
import {
  type LogSourcesForm,
  type ScanMap,
  type SemanticMeta,
  type SourceForm,
  MAX_SOURCES,
  boundEnvs,
  emptySource,
  kindLabel,
  removeSource,
  sourceUsage,
  typeLabel,
  upsertSource
} from "../log-sources-utils";

defineOptions({ name: "LogSourcesCard" });

const form = defineModel<LogSourcesForm>("form", { required: true });

defineProps<{
  scans: ScanMap;
  semantic: SemanticMeta[];
  builtinMasks: string[];
}>();

const drawer = reactive({
  visible: false,
  creating: false,
  source: null as SourceForm | null
});
const usage = computed(() => sourceUsage(form.value));

function connectionText(id: string) {
  const conn = form.value.connections.find(item => item.id === id);
  if (!conn) return `${id || "（未选）"}：连接不存在`;
  return `${conn.name || conn.id}（${typeLabel(conn.type)}）`;
}

/** 说明的第一行（表格里的摘要） */
const firstLine = (text: string) =>
  (text ?? "")
    .split("\n")
    .map(line => line.replace(/^[#\-*>\s]+/, "").trim())
    .find(Boolean) ?? "";

function add() {
  if (!form.value.connections.length) {
    message("先在「连接」里添加 ClickHouse 或 Elasticsearch 连接", {
      type: "warning"
    });
    return;
  }
  drawer.source = emptySource(form.value);
  drawer.creating = true;
  drawer.visible = true;
}

function edit(source: SourceForm) {
  drawer.source = source;
  drawer.creating = false;
  drawer.visible = true;
}

function onSave(source: SourceForm, originalId: string | null) {
  Object.assign(form.value, upsertSource(form.value, source, originalId));
  message(
    originalId
      ? `已修改日志源 ${source.id}，点底部的「保存」后生效`
      : `已添加日志源 ${source.id}：到「集群」里给它绑定集群，再点底部的「保存」`,
    { type: "success" }
  );
}

async function remove(source: SourceForm) {
  const envs = boundEnvs(form.value, source.id);
  const name = source.name || source.id;
  try {
    await ElMessageBox.confirm(
      envs.length
        ? `删除日志源「${name}」？会一并删除 ${envs.length} 个集群（${envs.join("、")}）里它的绑定。点底部的「保存」后生效`
        : `删除日志源「${name}」？点底部的「保存」后生效`,
      "删除日志源",
      { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" }
    );
  } catch {
    return;
  }
  Object.assign(form.value, removeSource(form.value, source.id));
}
</script>

<template>
  <el-card shadow="never" class="log-card">
    <template #header>
      <div class="card-header">
        <strong>日志源</strong>
        <el-button
          size="small"
          type="primary"
          plain
          :disabled="form.sources.length >= MAX_SOURCES"
          @click="add"
        >
          添加日志源
        </el-button>
      </div>
    </template>
    <p class="card-note">
      一类结构和含义相同的日志，例如「入口网关访问日志」「Java
      应用日志」：定义时间字段、正文 / 状态码 / Pod 等语义字段、给 AI
      的说明和字段说明、关联字段和打码。各集群的表 /
      索引在下面的「集群」里绑定。
    </p>
    <el-table
      :data="form.sources"
      row-key="rowId"
      border
      size="small"
      empty-text="还没有日志源：用「智能配置」扫描生成，或者点右上角手动添加"
    >
      <el-table-column label="名称" min-width="220">
        <template #default="{ row }">
          <div>{{ row.name || "（未命名）" }}</div>
          <div
            v-if="firstLine(row.description)"
            class="sub-line"
            :title="row.description"
          >
            {{ firstLine(row.description) }}
          </div>
        </template>
      </el-table-column>
      <el-table-column label="ID" width="150">
        <template #default="{ row }">
          <code>{{ row.id }}</code>
        </template>
      </el-table-column>
      <el-table-column label="类型" width="90">
        <template #default="{ row }">{{ kindLabel(row.kind) }}</template>
      </el-table-column>
      <el-table-column label="连接" min-width="170" show-overflow-tooltip>
        <template #default="{ row }">
          <span
            :class="{
              'danger-text': !form.connections.some(
                c => c.id === row.connection
              )
            }"
          >
            {{ connectionText(row.connection) }}
          </span>
        </template>
      </el-table-column>
      <el-table-column label="绑定的集群" width="110">
        <template #default="{ row }">
          <el-tooltip
            v-if="usage[row.id]"
            :content="boundEnvs(form, row.id).join('、')"
            placement="top"
          >
            <span>{{ usage[row.id] }} 个</span>
          </el-tooltip>
          <span v-else class="warning-text">没绑定</span>
        </template>
      </el-table-column>
      <el-table-column label="启用" width="80">
        <template #default="{ row }">
          <el-switch v-model="row.enabled" size="small" />
        </template>
      </el-table-column>
      <el-table-column label="操作" width="120" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" @click="edit(row)">编辑</el-button>
          <el-button link type="danger" @click="remove(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>
    <SourceDrawer
      v-model:visible="drawer.visible"
      :source="drawer.source"
      :creating="drawer.creating"
      :form="form"
      :scans="scans"
      :semantic="semantic"
      :builtin-masks="builtinMasks"
      @save="onSave"
    />
  </el-card>
</template>

<style scoped lang="scss">
.sub-line {
  overflow: hidden;
  font-size: 12px;
  color: var(--el-text-color-secondary);
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
