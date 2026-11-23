<script setup lang="ts">
// 连接的编辑对话框：改的是副本，点「确定」才写回表单（写进数据库还要点页面底部的「保存」）
import { computed, ref, watch } from "vue";
import { type LogConnectionType, testLogConnection } from "@/api/system";
import {
  type ConnectionForm,
  type InfoRow,
  API_KEY_MAX_LENGTH,
  DEFAULT_MAX_ROWS,
  ES_AUTH_OPTIONS,
  MAX_CONCURRENCY,
  MAX_HEADERS,
  MAX_ROWS_LIMIT,
  MAX_TIMEOUT,
  MIN_CONCURRENCY,
  MIN_TIMEOUT,
  PASSWORD_MAX_LENGTH,
  TYPE_OPTIONS,
  apiKeyPlaceholder,
  authOf,
  cloneDeep,
  connectionErrors,
  connectionTestPayload,
  errorText,
  keepsSavedHeader,
  nextRowId,
  passwordPlaceholder,
  requestTimeoutMs,
  testRows,
  testSummary,
  uniqueId
} from "../log-sources-utils";

defineOptions({ name: "LogConnectionDialog" });

const visible = defineModel<boolean>("visible", { required: true });

const props = defineProps<{
  /** 要编辑的连接（打开时复制一份）；新建时是 emptyConnection 的结果 */
  connection: ConnectionForm | null;
  /** 新建（还不在表单里） */
  creating: boolean;
  /** 其他连接的 ID：检查重复 */
  usedIds: string[];
}>();

const emit = defineEmits<{
  save: [conn: ConnectionForm, originalId: string | null];
}>();

const draft = ref<ConnectionForm | null>(null);
/** 新建时自动生成的 ID：没改过时跟着类型变（ck / es） */
const autoId = ref("");
const errors = ref<string[]>([]);
const testing = ref(false);
const result = ref<{ ok: boolean; text: string; rows: InfoRow[] } | null>(null);

const title = computed(() =>
  props.creating
    ? "添加连接"
    : `编辑连接：${props.connection?.name || props.connection?.id || ""}`
);
const isClickHouse = computed(() => draft.value?.type === "clickhouse");
const auth = computed(() => (draft.value ? authOf(draft.value) : "none"));

watch(visible, open => {
  if (!open || !props.connection) return;
  draft.value = cloneDeep(props.connection);
  autoId.value = props.creating ? props.connection.id : "";
  errors.value = [];
  result.value = null;
});

function onTypeChange(type: LogConnectionType) {
  const conn = draft.value;
  if (!conn) return;
  conn.auth_type =
    type === "clickhouse" ? "basic" : conn.username.trim() ? "basic" : "none";
  const names = ["日志 ClickHouse", "日志 Elasticsearch"];
  if (names.includes(conn.name))
    conn.name = type === "clickhouse" ? names[0] : names[1];
  if (autoId.value && conn.id === autoId.value) {
    conn.id = uniqueId(
      type === "clickhouse" ? "ck" : "es",
      props.usedIds,
      "conn"
    );
    autoId.value = conn.id;
  }
}

function addHeader() {
  draft.value?.headers.push({ id: nextRowId(), name: "", value: "" });
}

/** 打开对话框后的问题：ID 重复 + 连接本身的问题 */
function problems(conn: ConnectionForm): string[] {
  const list = connectionErrors(conn);
  const id = conn.id.trim();
  if (id && props.usedIds.includes(id))
    list.unshift(`ID：${id} 和别的连接重复了`);
  return list;
}

async function runTest() {
  const conn = draft.value;
  if (!conn || testing.value) return;
  errors.value = problems(conn);
  if (errors.value.length) return;
  testing.value = true;
  result.value = null;
  try {
    // 版本、账号、集群等几条查询
    const res = await testLogConnection(
      connectionTestPayload(conn),
      conn.id.trim(),
      requestTimeoutMs(conn.timeout, 2)
    );
    result.value = {
      ok: true,
      text: testSummary(res.data),
      rows: testRows(res.data)
    };
  } catch (reason) {
    result.value = {
      ok: false,
      text: `连接失败：${errorText(reason, "未知原因")}`,
      rows: []
    };
  } finally {
    testing.value = false;
  }
}

function confirm() {
  const conn = draft.value;
  if (!conn) return;
  errors.value = problems(conn);
  if (errors.value.length) return;
  conn.id = conn.id.trim();
  emit("save", conn, props.creating ? null : (props.connection?.id ?? null));
  visible.value = false;
}
</script>

<template>
  <el-dialog
    v-model="visible"
    :title="title"
    width="760px"
    :close-on-click-modal="false"
    class="log-connection-dialog"
  >
    <template v-if="draft">
      <el-alert
        v-if="errors.length"
        type="error"
        :closable="false"
        show-icon
        title="请先修改这些地方"
        class="dialog-alert"
      >
        <ul class="problem-list">
          <li v-for="item in errors" :key="item">{{ item }}</li>
        </ul>
      </el-alert>
      <el-form label-width="130px" @submit.prevent>
        <el-form-item label="类型">
          <el-radio-group
            v-model="draft.type"
            :disabled="draft.saved"
            @change="value => onTypeChange(value as LogConnectionType)"
          >
            <el-radio-button
              v-for="item in TYPE_OPTIONS"
              :key="item.value"
              :value="item.value"
            >
              {{ item.label }}
            </el-radio-button>
          </el-radio-group>
          <span v-if="draft.saved" class="field-hint inline">
            已保存的连接不能改类型，需要时新建一个
          </span>
        </el-form-item>
        <el-form-item label="ID" required>
          <el-input
            v-model="draft.id"
            :disabled="draft.saved"
            maxlength="32"
            class="short-input"
          />
          <span class="field-hint inline">
            {{
              draft.saved
                ? "已保存的连接不能改 ID（密码按 ID 保存）"
                : "小写字母、数字、_ 和 -，以字母开头；日志源用它引用连接"
            }}
          </span>
        </el-form-item>
        <el-form-item label="名称" required>
          <el-input v-model="draft.name" maxlength="50" class="short-input" />
        </el-form-item>
        <el-form-item label="地址" required>
          <el-input
            v-model="draft.url"
            clearable
            :placeholder="
              isClickHouse
                ? 'http://clickhouse.example.com:8123'
                : 'https://es.example.com:9200'
            "
          />
          <div class="field-hint">
            {{
              isClickHouse
                ? "ClickHouse 的 HTTP 接口（默认端口 8123，HTTPS 一般是 8443）；集群部署时填能查到分布式表的节点或负载均衡地址"
                : "Elasticsearch / OpenSearch 的地址；前面有网关时可以带路径前缀，例如 https://gateway.example.com/es"
            }}。由 KubeDoor 控制端（kubedoor-master）直接访问
          </div>
        </el-form-item>
        <el-form-item v-if="!isClickHouse" label="认证方式">
          <el-radio-group v-model="draft.auth_type">
            <el-radio
              v-for="item in ES_AUTH_OPTIONS"
              :key="item.value"
              :value="item.value"
            >
              {{ item.label }}
            </el-radio>
          </el-radio-group>
        </el-form-item>
        <template v-if="auth === 'basic'">
          <el-form-item label="账号" :required="!isClickHouse">
            <el-input
              v-model="draft.username"
              autocomplete="off"
              :placeholder="
                isClickHouse ? '不填为 default' : '例如 kubedoor_ro'
              "
              class="short-input"
            />
          </el-form-item>
          <el-form-item label="密码" :required="!isClickHouse">
            <!-- 点了「清除」后不能再填：保存 / 测试时带 clear_password，master 忽略同时传来的密码 -->
            <el-input
              v-model="draft.password"
              :maxlength="PASSWORD_MAX_LENGTH"
              type="password"
              show-password
              autocomplete="new-password"
              :disabled="draft.clear_password"
              :placeholder="passwordPlaceholder(draft)"
              class="short-input"
            />
            <template v-if="draft.clear_password">
              <el-tag size="small" type="warning" effect="plain" class="gap">
                将清除
              </el-tag>
              <el-button
                link
                type="primary"
                class="gap"
                @click="draft.clear_password = false"
              >
                撤销
              </el-button>
            </template>
            <template v-else-if="draft.has_password">
              <el-tag size="small" type="success" effect="plain" class="gap">
                已保存
              </el-tag>
              <el-button
                link
                type="danger"
                class="gap"
                @click="
                  draft.clear_password = true;
                  draft.password = '';
                "
              >
                清除
              </el-button>
            </template>
            <div class="field-hint">
              加密保存，保存后不再显示；留空表示沿用已保存的密码
            </div>
          </el-form-item>
        </template>
        <el-form-item v-if="auth === 'api_key'" label="API Key" required>
          <el-input
            v-model="draft.api_key"
            :maxlength="API_KEY_MAX_LENGTH"
            type="password"
            show-password
            autocomplete="new-password"
            :disabled="draft.clear_api_key"
            :placeholder="apiKeyPlaceholder(draft)"
          />
          <template v-if="draft.clear_api_key">
            <el-tag size="small" type="warning" effect="plain" class="gap">
              将清除
            </el-tag>
            <el-button
              link
              type="primary"
              class="gap"
              @click="draft.clear_api_key = false"
            >
              撤销
            </el-button>
          </template>
          <template v-else-if="draft.has_api_key">
            <el-tag size="small" type="success" effect="plain" class="gap">
              已保存
            </el-tag>
            <el-button
              link
              type="danger"
              class="gap"
              @click="
                draft.clear_api_key = true;
                draft.api_key = '';
              "
            >
              清除
            </el-button>
          </template>
          <div class="field-hint">
            Elasticsearch 创建 API Key 时返回的 encoded 值（base64 编码的
            id:key），原样粘贴；加密保存
          </div>
        </el-form-item>
        <el-form-item label="请求头">
          <div class="headers">
            <div
              v-for="(item, index) in draft.headers"
              :key="item.id"
              class="header-row"
            >
              <el-input
                v-model="item.name"
                placeholder="名称，例如 X-Scope-OrgID"
                class="header-name"
              />
              <el-input
                v-model="item.value"
                type="password"
                show-password
                autocomplete="new-password"
                :placeholder="
                  keepsSavedHeader(draft, item)
                    ? '已设置，留空表示不修改'
                    : '值'
                "
                class="header-value"
              />
              <el-button
                link
                type="danger"
                @click="draft.headers.splice(index, 1)"
              >
                删除
              </el-button>
            </div>
            <el-button
              size="small"
              :disabled="draft.headers.length >= MAX_HEADERS"
              @click="addHeader"
            >
              添加请求头
            </el-button>
            <span class="field-hint inline">
              前面有鉴权网关时填写；值加密保存，最多 {{ MAX_HEADERS }} 个
            </span>
          </div>
        </el-form-item>
        <el-form-item label="超时（秒）">
          <el-input-number
            v-model="draft.timeout"
            :min="MIN_TIMEOUT"
            :max="MAX_TIMEOUT"
            :precision="0"
          />
          <span class="field-hint inline">
            单次查询最多等多久（{{ MIN_TIMEOUT }}~{{ MAX_TIMEOUT }} 秒）{{
              isClickHouse ? "，同时作为 max_execution_time" : ""
            }}
          </span>
        </el-form-item>
        <el-form-item label="并发">
          <el-input-number
            v-model="draft.max_concurrency"
            :min="MIN_CONCURRENCY"
            :max="MAX_CONCURRENCY"
            :precision="0"
          />
          <span class="field-hint inline">
            同一连接同时进行的查询数（{{ MIN_CONCURRENCY }}~{{
              MAX_CONCURRENCY
            }}），多个 AI 会话一起查时排队
          </span>
        </el-form-item>
        <el-form-item v-if="isClickHouse" label="单次扫描行数上限">
          <el-input-number
            v-model="draft.max_rows_to_read"
            :min="0"
            :max="MAX_ROWS_LIMIT"
            :step="DEFAULT_MAX_ROWS"
            :precision="0"
            controls-position="right"
            class="rows-input"
          />
          <div class="field-hint">
            单次查询每个分片最多读取多少行（ClickHouse 的
            max_rows_to_read），超过时查询报错，AI 会改为加条件或缩短时间；0
            表示不限
          </div>
        </el-form-item>
        <el-form-item label="HTTPS 证书">
          <el-checkbox v-model="draft.tls_verify">校验 HTTPS 证书</el-checkbox>
          <span class="field-hint inline">自签证书时可以关闭（不推荐）</span>
        </el-form-item>
        <el-form-item label="原生查询">
          <el-switch v-model="draft.allow_raw" />
          <span class="field-hint inline">
            允许 AI 写{{
              isClickHouse ? " SQL" : " DSL、ES|QL"
            }}查询：只读，查询里自动套上集群绑定和时间范围；关闭后只能用结构化查询
          </span>
        </el-form-item>
      </el-form>
      <template v-if="result">
        <el-alert
          :type="result.ok ? 'success' : 'error'"
          :title="result.text"
          :closable="false"
          show-icon
        />
        <el-descriptions
          v-if="result.rows.length"
          :column="1"
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
    </template>
    <template #footer>
      <el-button :loading="testing" @click="runTest">测试连接</el-button>
      <el-button @click="visible = false">取消</el-button>
      <el-button type="primary" @click="confirm">确定</el-button>
    </template>
  </el-dialog>
</template>

<style scoped lang="scss">
.dialog-alert {
  margin-bottom: 12px;
}

.problem-list {
  padding-left: 18px;
  margin: 4px 0 0;
  line-height: 1.7;
  list-style: disc;
}

.field-hint {
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

.short-input {
  width: 280px;
  max-width: 100%;
}

.rows-input {
  width: 220px;
}

.gap {
  margin-left: 8px;
}

.headers {
  width: 100%;
}

.header-row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  margin-bottom: 8px;
}

.header-name {
  flex: 0 0 220px;
}

.header-value {
  flex: 1;
  min-width: 180px;
}

.test-rows {
  margin-top: 12px;

  :deep(.el-descriptions__label) {
    width: 120px;
    white-space: nowrap;
  }
}

.warn-text {
  color: var(--el-color-warning);
}
</style>
