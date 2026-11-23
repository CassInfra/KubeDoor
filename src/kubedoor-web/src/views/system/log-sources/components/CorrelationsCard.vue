<script setup lang="ts">
// 「关联字段」卡片：串起同一个请求的 ID（trace_id、request_id 等）、链路 ID 开关、被哪些日志源登记；
// 「验证」从各日志源抽取值到其他日志源里查，看能不能对上
import { computed, reactive } from "vue";
import { ElMessageBox } from "element-plus";
import { message } from "@/utils/message";
import { type LogVerifyResult, verifyLogCorrelation } from "@/api/system";
import {
  type KeyForm,
  type LogSourcesForm,
  KEY_DESCRIPTION_MAX_LENGTH,
  KEY_LABEL_MAX_LENGTH,
  MAX_KEYS,
  PATTERN_MAX_LENGTH,
  TRACING_PAGE,
  cloneDeep,
  errorText,
  keyErrors,
  keyForm,
  keyUsage,
  removeKey,
  setTraceKey,
  upsertKey,
  verifyEnvs,
  verifyErrors,
  verifyMatrix,
  verifyPayload
} from "../log-sources-utils";

defineOptions({ name: "LogCorrelationsCard" });

const form = defineModel<LogSourcesForm>("form", { required: true });

const dialog = reactive({
  visible: false,
  creating: false,
  original: "",
  draft: null as KeyForm | null,
  errors: [] as string[]
});
const verify = reactive({
  key: "",
  env: "",
  loading: false,
  result: null as LogVerifyResult | null,
  error: "",
  problems: [] as string[]
});

const usage = computed<Record<string, string[]>>(() =>
  Object.fromEntries(
    form.value.correlation_keys.map(item => [
      item.key,
      keyUsage(form.value, item.key)
    ])
  )
);
const envs = computed(() =>
  verify.key ? verifyEnvs(form.value, verify.key) : []
);
const matrix = computed(() => verifyMatrix(verify.result));

function add() {
  dialog.draft = keyForm({});
  dialog.creating = true;
  dialog.original = "";
  dialog.errors = [];
  dialog.visible = true;
}

function edit(row: KeyForm) {
  dialog.draft = cloneDeep(row);
  dialog.creating = false;
  dialog.original = row.key;
  dialog.errors = [];
  dialog.visible = true;
}

function confirm() {
  const row = dialog.draft;
  if (!row) return;
  row.key = row.key.trim();
  const list = keyErrors(row);
  if (
    row.key &&
    form.value.correlation_keys.some(
      item => item.rowId !== row.rowId && item.key === row.key
    )
  )
    list.unshift(`Key：${row.key} 已经有了`);
  dialog.errors = list;
  if (list.length) return;
  Object.assign(
    form.value,
    upsertKey(form.value, row, dialog.creating ? null : dialog.original)
  );
  dialog.visible = false;
  message(`已保存关联字段 ${row.key}，点底部的「保存」后生效`, {
    type: "success"
  });
}

async function remove(row: KeyForm) {
  const sources = usage.value[row.key] ?? [];
  try {
    await ElMessageBox.confirm(
      sources.length
        ? `删除关联字段 ${row.key}？会一并删除日志源 ${sources.join("、")} 里对它的登记。点底部的「保存」后生效`
        : `删除关联字段 ${row.key}？点底部的「保存」后生效`,
      "删除关联字段",
      { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" }
    );
  } catch {
    return;
  }
  Object.assign(form.value, removeKey(form.value, row.key));
  if (verify.key === row.key) {
    verify.key = "";
    verify.result = null;
  }
}

function toggleTrace(row: KeyForm, on: boolean) {
  Object.assign(form.value, setTraceKey(form.value, row.key, on));
}

function pickKey(key: string) {
  verify.key = key;
  verify.result = null;
  verify.error = "";
  verify.problems = [];
  if (!envs.value.includes(verify.env)) verify.env = envs.value[0] ?? "";
}

async function runVerify() {
  if (verify.loading) return;
  if (!verify.key || !verify.env) {
    message("先选关联字段和集群", { type: "warning" });
    return;
  }
  verify.result = null;
  verify.error = "";
  verify.problems = verifyErrors(form.value, verify.env, verify.key);
  if (verify.problems.length) return;
  verify.loading = true;
  try {
    const res = await verifyLogCorrelation(
      verifyPayload(form.value, verify.env, verify.key),
      verify.env,
      verify.key
    );
    verify.result = res.data;
  } catch (reason) {
    verify.error = errorText(reason, "验证失败");
  } finally {
    verify.loading = false;
  }
}

const LEVEL_TYPES = {
  good: "success",
  partial: "warning",
  none: "danger",
  empty: "info",
  error: "danger"
} as const;
</script>

<template>
  <el-card shadow="never" class="log-card">
    <template #header>
      <div class="card-header">
        <strong>关联字段</strong>
        <el-button
          size="small"
          type="primary"
          plain
          :disabled="form.correlation_keys.length >= MAX_KEYS"
          @click="add"
        >
          添加关联字段
        </el-button>
      </div>
    </template>
    <p class="card-note">
      不同日志源之间串起同一个请求的 ID，例如入口网关生成的
      request_id、应用日志里的链路
      ID。在日志源里登记它在哪一列或正文里怎么写，AI 就能按这个 ID
      把入口、网关、服务各层的日志串成一次请求。设为「链路 ID」的，AI
      拿到取值后可以接着用链路追踪查整条链路（要先配置<router-link
        :to="TRACING_PAGE"
        class="link"
        >链路追踪</router-link
      >）。
    </p>
    <el-table
      :data="form.correlation_keys"
      row-key="rowId"
      border
      size="small"
      empty-text="还没有关联字段：点右上角添加，或者用「智能配置」生成"
    >
      <el-table-column label="Key" width="150">
        <template #default="{ row }">
          <code>{{ row.key }}</code>
        </template>
      </el-table-column>
      <el-table-column prop="label" label="名称" width="130" />
      <el-table-column
        prop="description"
        label="说明"
        min-width="180"
        show-overflow-tooltip
      />
      <el-table-column label="取值格式" min-width="160" show-overflow-tooltip>
        <template #default="{ row }">
          <code v-if="row.pattern">{{ row.pattern }}</code>
          <span v-else class="muted">不限</span>
        </template>
      </el-table-column>
      <el-table-column label="链路 ID" width="80">
        <template #default="{ row }">
          <el-switch
            :model-value="row.trace"
            size="small"
            @update:model-value="value => toggleTrace(row, !!value)"
          />
        </template>
      </el-table-column>
      <el-table-column label="登记的日志源" min-width="170">
        <template #default="{ row }">
          <div v-if="usage[row.key]?.length" class="tag-list">
            <el-tag
              v-for="id in usage[row.key]"
              :key="id"
              size="small"
              effect="plain"
            >
              {{ id }}
            </el-tag>
          </div>
          <span v-else class="warning-text">还没有日志源登记</span>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="160" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" @click="edit(row)">编辑</el-button>
          <el-button link type="primary" @click="pickKey(row.key)">
            验证
          </el-button>
          <el-button link type="danger" @click="remove(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <template v-if="form.correlation_keys.length">
      <div class="section-title">验证</div>
      <p class="card-note">
        在一个集群里，从登记了这个关联字段的每个日志源各抽最近 1 小时最多 20
        个取值，到其他日志源里查（前后各 1
        小时），看能不能对上。用页面上还没保存的配置。
      </p>
      <div class="verify-bar">
        <el-select
          :model-value="verify.key"
          placeholder="关联字段"
          class="verify-select"
          @update:model-value="pickKey"
        >
          <el-option
            v-for="item in form.correlation_keys"
            :key="item.rowId"
            :label="`${item.key}（${item.label}）`"
            :value="item.key"
          />
        </el-select>
        <el-select
          v-model="verify.env"
          :placeholder="
            verify.key && !envs.length ? '没有能验证的集群' : '集群'
          "
          :disabled="!envs.length"
          class="verify-select"
        >
          <el-option v-for="env in envs" :key="env" :label="env" :value="env" />
        </el-select>
        <el-button
          type="primary"
          plain
          :loading="verify.loading"
          :disabled="!verify.key || !verify.env"
          @click="runVerify"
        >
          验证
        </el-button>
        <span v-if="verify.key && !envs.length" class="hint inline">
          要有集群同时绑定了至少两个登记了 {{ verify.key }} 的日志源
        </span>
      </div>
      <el-alert
        v-if="verify.problems.length"
        type="warning"
        :closable="false"
        show-icon
        title="先修改这些地方再验证"
        class="block-alert"
      >
        <ul class="rules">
          <li v-for="item in verify.problems" :key="item">{{ item }}</li>
        </ul>
      </el-alert>
      <el-alert
        v-if="verify.error"
        type="error"
        :closable="false"
        show-icon
        :title="`验证失败：${verify.error}`"
        class="block-alert"
      />
      <template v-if="verify.result">
        <el-table
          v-if="matrix.rows.length"
          :data="matrix.rows"
          border
          size="small"
          class="matrix"
        >
          <el-table-column label="取值来自 ↓ / 去查 →" min-width="150">
            <template #default="{ row }">
              <code>{{ row.from }}</code>
            </template>
          </el-table-column>
          <el-table-column
            v-for="(source, index) in matrix.sources"
            :key="source"
            :label="source"
            min-width="120"
          >
            <template #default="{ row }">
              <template v-if="row.cells[index]?.error">
                <el-tooltip :content="row.cells[index].error" placement="top">
                  <el-tag size="small" effect="plain" type="danger">
                    出错
                  </el-tag>
                </el-tooltip>
              </template>
              <template v-else-if="row.cells[index]">
                <el-tooltip
                  :disabled="!row.cells[index].examples.length"
                  :content="`对上的取值：${row.cells[index].examples.join('、')}`"
                  placement="top"
                >
                  <el-tag
                    size="small"
                    effect="plain"
                    :type="LEVEL_TYPES[row.cells[index].level]"
                  >
                    {{ row.cells[index].matched }} /
                    {{ row.cells[index].sampled }}
                  </el-tag>
                </el-tooltip>
              </template>
              <span v-else class="muted">—</span>
            </template>
          </el-table-column>
        </el-table>
        <div class="hint">
          对上的个数 /
          抽取的个数：绿色一半以上能对上，黄色部分对上，红色都没对上或查询出错（检查列名或正文写法，鼠标移上去看原因），灰色没抽到取值
        </div>
        <ul v-if="verify.result.notes?.length" class="rules">
          <li v-for="(item, index) in verify.result.notes" :key="index">
            {{ item }}
          </li>
        </ul>
      </template>
    </template>

    <el-dialog
      v-model="dialog.visible"
      :title="
        dialog.creating ? '添加关联字段' : `编辑关联字段：${dialog.original}`
      "
      width="640px"
      :close-on-click-modal="false"
    >
      <template v-if="dialog.draft">
        <el-alert
          v-if="dialog.errors.length"
          type="error"
          :closable="false"
          show-icon
          title="请先修改这些地方"
          class="block-alert"
        >
          <ul class="rules">
            <li v-for="item in dialog.errors" :key="item">{{ item }}</li>
          </ul>
        </el-alert>
        <el-form label-width="100px" @submit.prevent>
          <el-form-item label="Key" required>
            <el-input
              v-model="dialog.draft.key"
              maxlength="32"
              placeholder="例如 trace_id"
              class="key-input"
            />
            <span class="hint inline">
              小写字母、数字和 _；改了会同步日志源里的登记
            </span>
          </el-form-item>
          <el-form-item label="名称" required>
            <el-input
              v-model="dialog.draft.label"
              :maxlength="KEY_LABEL_MAX_LENGTH"
              placeholder="例如 链路 ID"
              class="key-input"
            />
          </el-form-item>
          <el-form-item label="说明">
            <el-input
              v-model="dialog.draft.description"
              type="textarea"
              :rows="2"
              :maxlength="KEY_DESCRIPTION_MAX_LENGTH"
              show-word-limit
              placeholder="给 AI 看，例如「入口网关生成，透传到服务日志」"
            />
          </el-form-item>
          <el-form-item label="取值格式">
            <el-input
              v-model="dialog.draft.pattern"
              :maxlength="PATTERN_MAX_LENGTH"
              placeholder="可选，例如 [0-9a-f]{32}"
              class="mono-input"
            />
            <div class="hint">
              取值的正则（不写 ^ $）：用来检查 AI
              传的值，也用来从正文里提取；不填表示不限
            </div>
          </el-form-item>
          <el-form-item label="链路 ID">
            <el-switch v-model="dialog.draft.trace" />
            <span class="hint inline">
              只能有一个；打开后其他关联字段的链路 ID 开关会关掉
            </span>
          </el-form-item>
        </el-form>
      </template>
      <template #footer>
        <el-button @click="dialog.visible = false">取消</el-button>
        <el-button type="primary" @click="confirm">确定</el-button>
      </template>
    </el-dialog>
  </el-card>
</template>

<style scoped lang="scss">
.verify-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  margin-bottom: 8px;
}

.verify-select {
  width: 220px;
}

.matrix {
  margin: 8px 0 4px;
}

.key-input {
  width: 260px;
}

.mono-input :deep(input) {
  font-family: Menlo, Consolas, monospace;
  font-size: 12px;
}
</style>
