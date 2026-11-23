<script setup lang="ts">
// 绑定的预览：按这个集群的绑定查最近 15 分钟最新 5 条，显示实际执行的查询（参数已代入）
import { computed, ref, watch } from "vue";
import { type LogPreviewResult, previewLogBinding } from "@/api/system";
import {
  type LogSourcesForm,
  bindingConnection,
  errorText,
  previewColumns,
  previewErrors,
  previewPayload,
  problemText,
  requestTimeoutMs,
  valueText
} from "../log-sources-utils";

defineOptions({ name: "LogPreviewDialog" });

const visible = defineModel<boolean>("visible", { required: true });

const props = defineProps<{
  /** 预览用的表单（绑定对话框里还没确定的修改也在里面） */
  form: LogSourcesForm | null;
  env: string;
  source: string;
}>();

const loading = ref(false);
const result = ref<LogPreviewResult | null>(null);
const problems = ref<string[]>([]);
const error = ref("");

const columns = computed(() => previewColumns(result.value?.rows));
const query = computed(() => valueText(result.value?.query, 2));
const schemaProblems = computed(() =>
  (result.value?.schema_problems ?? []).map(problemText).filter(Boolean)
);
const resultError = computed(() => problemText(result.value?.error));

watch(visible, open => {
  if (open) void run();
});

async function run() {
  const form = props.form;
  if (!form || loading.value) return;
  result.value = null;
  error.value = "";
  problems.value = previewErrors(form, props.env, props.source);
  if (problems.value.length) return;
  const binding = form.clusters
    .find(item => item.env === props.env)
    ?.bindings.find(item => item.source === props.source);
  const conn = binding ? bindingConnection(form, binding) : null;
  loading.value = true;
  try {
    // 字段表 + 查数据 + 总数
    const res = await previewLogBinding(
      previewPayload(form, props.env, props.source),
      props.env,
      props.source,
      requestTimeoutMs(conn?.timeout ?? 30, 3)
    );
    result.value = res.data;
  } catch (reason) {
    error.value = errorText(reason, "预览失败");
  } finally {
    loading.value = false;
  }
}
</script>

<template>
  <el-dialog
    v-model="visible"
    :title="`预览：${env} / ${source}`"
    width="1040px"
    top="6vh"
    append-to-body
    class="log-preview-dialog"
  >
    <div v-loading="loading" class="preview-body">
      <el-alert
        v-if="problems.length"
        type="warning"
        :closable="false"
        show-icon
        title="先修改这些地方再预览"
      >
        <ul class="problem-list">
          <li v-for="item in problems" :key="item">{{ item }}</li>
        </ul>
      </el-alert>
      <el-alert
        v-else-if="error"
        type="error"
        :closable="false"
        show-icon
        :title="`预览失败：${error}`"
      />
      <template v-else-if="result">
        <el-alert
          :type="result.ok ? 'success' : 'error'"
          :closable="false"
          show-icon
          :title="
            result.ok
              ? `最近 15 分钟最新 ${result.rows?.length ?? 0} 条${
                  typeof result.total === 'number'
                    ? `（共 ${result.total} 条）`
                    : ''
                } · 耗时 ${result.elapsed_ms ?? 0} ms`
              : `查询失败：${resultError || '未知原因'}`
          "
        />
        <el-alert
          v-if="schemaProblems.length"
          type="warning"
          :closable="false"
          show-icon
          title="配置里引用了、但表 / 索引里没有的字段"
          class="gap-top"
        >
          <ul class="problem-list">
            <li v-for="item in schemaProblems" :key="item">{{ item }}</li>
          </ul>
        </el-alert>
        <div v-if="query" class="part-title">实际执行的查询</div>
        <pre v-if="query" class="query">{{ query }}</pre>
        <template v-if="result.ok">
          <div class="part-title">数据</div>
          <el-table
            v-if="result.rows?.length"
            :data="result.rows"
            border
            size="small"
            max-height="360"
          >
            <el-table-column
              v-for="column in columns"
              :key="column"
              :label="column"
              min-width="160"
              show-overflow-tooltip
            >
              <template #default="{ row }">{{
                valueText(row[column])
              }}</template>
            </el-table-column>
          </el-table>
          <el-empty
            v-else
            :image-size="60"
            description="最近 15 分钟没有数据：核对表 / 索引和过滤条件的取值，或者这个集群最近确实没有日志"
          />
        </template>
      </template>
    </div>
    <template #footer>
      <el-button :loading="loading" @click="run">重新预览</el-button>
      <el-button type="primary" @click="visible = false">关闭</el-button>
    </template>
  </el-dialog>
</template>

<style scoped lang="scss">
.preview-body {
  min-height: 120px;
}

.problem-list {
  padding-left: 18px;
  margin: 4px 0 0;
  line-height: 1.7;
  list-style: disc;
}

.gap-top {
  margin-top: 12px;
}

.part-title {
  margin: 14px 0 6px;
  font-weight: 600;
}

.query {
  max-height: 240px;
  padding: 8px 12px;
  margin: 0;
  overflow: auto;
  font-family: Menlo, Consolas, monospace;
  font-size: 12px;
  line-height: 1.6;
  word-break: break-all;
  white-space: pre-wrap;
  background: var(--el-fill-color-light);
  border-radius: 4px;
}
</style>
