<script setup lang="ts">
// 「智能配置」卡片：扫描连接里的库表 / 索引 → 勾选组 → 按规则生成或 AI 生成说明 → 草稿对话框里勾选 → 应用到表单（不保存）
import { computed, onBeforeUnmount, ref } from "vue";
// ElInputTag 没有在 plugins/elementPlus.ts 全局注册，这里单独引入（同链路追踪页）
import { ElInputTag } from "element-plus";
import { message } from "@/utils/message";
import {
  type LogKnownCluster,
  type LogScanGroup,
  scanLogConnection
} from "@/api/system";
import { describeLogSources } from "@/api/ai";
import DraftDialog from "./DraftDialog.vue";
import ScanGroupDetail from "./ScanGroupDetail.vue";
import {
  type DraftPicks,
  type DraftState,
  type LogSourcesForm,
  type ScanMap,
  HINT_MAX_LENGTH,
  cleanNames,
  connectionErrors,
  describeErrorText,
  describePayload,
  draftFromDescribe,
  draftFromScan,
  errorText,
  formatCount,
  groupSummary,
  groupUsedBy,
  kindLabel,
  mergeDraft,
  testPayload,
  typeLabel
} from "../log-sources-utils";

defineOptions({ name: "LogSmartConfigCard" });

const form = defineModel<LogSourcesForm>("form", { required: true });
/** 各连接最近一次的扫描结果（日志源、集群绑定的候选也用它） */
const scans = defineModel<ScanMap>("scans", { required: true });

const props = defineProps<{ known: LogKnownCluster[] }>();

const connection = ref("");
const include = ref<string[]>([]);
const exclude = ref<string[]>([]);
const scanning = ref(false);
const scanError = ref("");
const describing = ref(false);
const seconds = ref(0);
const hint = ref("");
const picked = ref<string[]>([]);
const draft = ref<DraftState | null>(null);
const draftVisible = ref(false);
let timer = 0;

const scan = computed(() => scans.value[connection.value] ?? null);
// 表格数据只跟着扫描结果变：el-table 的数据换了实例会清空勾选，表单的修改不能影响它
const rows = computed(() =>
  (scan.value?.groups ?? []).map(group => ({
    group,
    summary: groupSummary(group)
  }))
);
/** 组 → 已经用了它的表 / 索引的日志源 */
const used = computed<Record<string, string[]>>(() =>
  Object.fromEntries(
    (scan.value?.groups ?? []).map(group => [
      group.ref,
      groupUsedBy(form.value, connection.value, group)
    ])
  )
);
const targetCount = computed(() =>
  rows.value.reduce((sum, row) => sum + row.summary.targets, 0)
);

function startTimer() {
  seconds.value = 0;
  window.clearInterval(timer);
  timer = window.setInterval(() => seconds.value++, 1000);
}

function stopTimer() {
  window.clearInterval(timer);
}

onBeforeUnmount(stopTimer);

async function runScan() {
  if (scanning.value) return;
  const conn = form.value.connections.find(
    item => item.id === connection.value
  );
  if (!conn) {
    message("请先选择连接", { type: "warning" });
    return;
  }
  const problems = connectionErrors(conn);
  if (problems.length) {
    message(`连接 ${conn.id} · ${problems[0]}：在「连接」里修改后再扫描`, {
      type: "warning"
    });
    return;
  }
  scanning.value = true;
  scanError.value = "";
  startTimer();
  try {
    const res = await scanLogConnection(
      testPayload(form.value, conn.id),
      conn.id,
      cleanNames(include.value),
      cleanNames(exclude.value)
    );
    scans.value = { ...scans.value, [conn.id]: res.data };
    // 表格数据换了实例，el-table 会自己清空勾选
    picked.value = [];
    message(
      res.data.partial
        ? "扫描超过 90 秒，只返回了已扫描到的部分：可以用「只扫描」缩小范围后再扫"
        : `扫描完成：${res.data.groups?.length ?? 0} 组`,
      { type: res.data.partial ? "warning" : "success" }
    );
  } catch (reason) {
    // http 封装已经弹出提示，页面上也留一份
    scanError.value = errorText(reason, "扫描失败");
  } finally {
    scanning.value = false;
    stopTimer();
  }
}

/** 「连接」卡片里点了「扫描」：选中这个连接并开始扫描 */
async function start(id: string) {
  connection.value = id;
  await runScan();
}

defineExpose({ start });

function onSelection(selection: { group: LogScanGroup }[]) {
  picked.value = selection.map(row => row.group.ref);
}

function generateByRules() {
  if (!scan.value || !picked.value.length) {
    message("先在上面的表格里勾选要用的组", { type: "warning" });
    return;
  }
  draft.value = draftFromScan(scan.value, picked.value, form.value);
  draftVisible.value = true;
}

async function generateByAI() {
  if (describing.value) return;
  if (!scan.value || !picked.value.length) {
    message("先在上面的表格里勾选要用的组", { type: "warning" });
    return;
  }
  const request = describePayload(
    scan.value,
    picked.value,
    form.value,
    hint.value
  );
  if (request.tooLarge) {
    message("勾选的组太大，发不出去：少勾选几个组再试", { type: "error" });
    return;
  }
  if (request.dropped.length)
    message(`请求太大，已精简：${request.dropped.join("；")}`, {
      type: "warning",
      duration: 6000
    });
  const current = scan.value;
  describing.value = true;
  startTimer();
  try {
    const res = await describeLogSources(request.payload);
    const sent = request.payload.scan.groups.map(group => group.ref);
    const next = draftFromDescribe(res, current, sent, form.value);
    next.notes.unshift(...request.dropped);
    draft.value = next;
    draftVisible.value = true;
  } catch (reason) {
    message(describeErrorText(reason), { type: "error", duration: 6000 });
  } finally {
    describing.value = false;
    stopTimer();
  }
}

function applyDraft(picks: DraftPicks) {
  if (!draft.value) return;
  const { form: next, summary } = mergeDraft(form.value, draft.value, picks);
  Object.assign(form.value, next);
  draftVisible.value = false;
  message(
    summary.length
      ? `已应用到表单：${summary.join("，")}。检查后点底部的「保存」`
      : "没有勾选要应用的内容",
    { type: summary.length ? "success" : "info", duration: 5000 }
  );
}
</script>

<template>
  <el-card shadow="never" class="log-card">
    <template #header><strong>智能配置</strong></template>
    <p class="card-note">
      扫描连接里的库表 /
      索引，按字段结构分组，猜测时间字段、正文、状态码等语义字段和区分集群的字段，并按取值给出各集群的绑定建议。勾选要用的组，「按规则生成」直接用识别结果，「AI
      生成说明」再让模型写日志源说明、字段说明和网络架构（用企微专用模型，样本会发给模型服务商）。生成的是草稿，勾选后应用到表单，检查后再保存。
    </p>
    <div class="scan-bar">
      <el-select
        v-model="connection"
        placeholder="选择连接"
        class="connection-select"
      >
        <el-option
          v-for="item in form.connections"
          :key="item.rowId"
          :label="`${item.name || item.id}（${item.id}）`"
          :value="item.id"
        >
          <span>{{ item.name || item.id }}</span>
          <span class="option-note"
            >{{ typeLabel(item.type) }} · {{ item.id }}</span
          >
        </el-option>
      </el-select>
      <el-input-tag
        v-model="include"
        :max="20"
        clearable
        placeholder="只扫描（可选），例如 logs.* 或 app-logs-*，回车添加"
        class="glob-input"
      />
      <el-input-tag
        v-model="exclude"
        :max="20"
        clearable
        placeholder="不扫描（可选），例如 *_tmp，回车添加"
        class="glob-input"
      />
      <el-button
        type="primary"
        :loading="scanning"
        :disabled="!connection"
        @click="runScan"
      >
        {{ scanning ? `扫描中 ${seconds} 秒` : "扫描" }}
      </el-button>
    </div>
    <div class="hint">
      ClickHouse 默认跳过 system、INFORMATION_SCHEMA 库，Elasticsearch 默认跳过
      . 开头的系统索引；最长 90
      秒，超时返回已扫描到的部分。扫描结果只保存在这个页面，刷新后要重新扫描。
    </div>
    <el-alert
      v-if="scanError"
      type="error"
      :title="`扫描失败：${scanError}`"
      show-icon
      class="block-alert"
      @close="scanError = ''"
    />

    <template v-if="scan">
      <div class="scan-summary">
        <span>
          {{ typeLabel(scan.connection?.type) }} {{ scan.connection?.version }}
          <template v-if="scan.connection?.timezone">
            · 时区 {{ scan.connection.timezone }}
          </template>
          · {{ rows.length }} 组、{{ targetCount }} 个表 / 索引 · 耗时
          {{ ((scan.elapsed_ms || 0) / 1000).toFixed(1) }} 秒
        </span>
        <el-tag v-if="scan.partial" size="small" type="warning" effect="plain">
          只扫描了一部分
        </el-tag>
      </div>
      <ul v-if="scan.notes?.length" class="rules">
        <li v-for="(item, index) in scan.notes" :key="index">{{ item }}</li>
      </ul>
      <el-table
        :data="rows"
        row-key="group.ref"
        border
        size="small"
        max-height="520"
        empty-text="没有扫描到表 / 索引：检查账号权限，或者调整「只扫描」「不扫描」"
        @selection-change="onSelection"
      >
        <el-table-column type="selection" width="40" />
        <el-table-column type="expand" width="36">
          <template #default="{ row }">
            <ScanGroupDetail :group="row.group" :scan="scan" />
          </template>
        </el-table-column>
        <el-table-column label="组" min-width="200" show-overflow-tooltip>
          <template #default="{ row }">
            <strong>{{ row.group.name_guess || row.group.ref }}</strong>
            <div class="sub-line">
              {{ row.group.targets[0]?.target
              }}{{
                row.summary.targets > 1 ? ` 等 ${row.summary.targets} 个` : ""
              }}
            </div>
          </template>
        </el-table-column>
        <el-table-column label="类型" width="90">
          <template #default="{ row }">
            {{ kindLabel(row.group.kind_guess) }}
          </template>
        </el-table-column>
        <el-table-column label="行数" width="100">
          <template #default="{ row }">{{
            formatCount(row.summary.rows)
          }}</template>
        </el-table-column>
        <el-table-column label="保留" width="90">
          <template #default="{ row }">{{ row.summary.retention }}</template>
        </el-table-column>
        <el-table-column label="时间范围" min-width="240">
          <template #default="{ row }">{{ row.summary.range }}</template>
        </el-table-column>
        <el-table-column label="字段数" width="80">
          <template #default="{ row }">{{ row.summary.fields }}</template>
        </el-table-column>
        <el-table-column label="已用于" min-width="130">
          <template #default="{ row }">
            <div v-if="used[row.group.ref]?.length" class="tag-list">
              <el-tag
                v-for="id in used[row.group.ref]"
                :key="id"
                size="small"
                type="success"
                effect="plain"
              >
                {{ id }}
              </el-tag>
            </div>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
      </el-table>

      <div class="generate">
        <el-input
          v-model="hint"
          type="textarea"
          :rows="2"
          :maxlength="HINT_MAX_LENGTH"
          show-word-limit
          placeholder="补充说明（可选，给 AI 看）：例如「入口流量先经过外部 nginx；prod-a、prod-b 共用一张表，用 env 区分」"
        />
        <div class="generate-actions">
          <span class="hint inline">已勾选 {{ picked.length }} 组</span>
          <el-button :disabled="!picked.length" @click="generateByRules">
            按规则生成
          </el-button>
          <el-button
            type="primary"
            :loading="describing"
            :disabled="!picked.length"
            @click="generateByAI"
          >
            {{
              describing
                ? `AI 生成中 ${seconds} 秒（最长 3 分钟）`
                : "AI 生成说明"
            }}
          </el-button>
        </div>
      </div>
    </template>

    <DraftDialog
      v-model:visible="draftVisible"
      :draft="draft"
      :form="form"
      :known="props.known"
      @apply="applyDraft"
    />
  </el-card>
</template>

<style scoped lang="scss">
.scan-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  margin-bottom: 6px;
}

.connection-select {
  width: 240px;
}

.glob-input {
  width: 260px;
}

.scan-summary {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  margin: 12px 0 8px;
  font-size: 13px;
}

.sub-line {
  overflow: hidden;
  font-size: 12px;
  color: var(--el-text-color-secondary);
  text-overflow: ellipsis;
}

.generate {
  margin-top: 12px;
}

.generate-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  justify-content: flex-end;
  margin-top: 8px;
}

.option-note {
  margin-left: 12px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
</style>
