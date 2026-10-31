<template>
  <div class="system-code-info">
    <el-card v-loading="loading">
      <template #header>
        <div class="card-header">
          <span>获取代码接口</span>
          <el-switch
            v-model="form.enabled"
            active-text="启用"
            inactive-text="停用"
          />
        </div>
      </template>
      <el-alert type="info" :closable="false" class="mb-4">
        <template #title>
          Deployment 页「操作 → 获取代码」:master 带上下面配置的请求头,把这个
          Deployment 的集群、命名空间、名称和镜像 POST
          到这个接口;接口返回代码页面、下载链接、Commit
          说明三个链接,在弹窗里显示(点击在新窗口打开)。接口格式见下面的「接口约定」,修改后立即生效。
        </template>
      </el-alert>

      <el-form :model="form" label-width="120px" class="code-info-form">
        <el-form-item label="接口地址" required>
          <el-input
            v-model="form.url"
            placeholder="https://code-api.example.com/repo-url"
          />
          <div class="hint">
            由 master 在服务端发起 POST 请求,地址要能从 master 所在的 Pod 访问到
          </div>
        </el-form-item>
        <el-form-item label="请求头">
          <div class="headers">
            <div
              v-for="(item, index) in headers"
              :key="item.id"
              class="header-row"
            >
              <el-input
                v-model="item.name"
                placeholder="名称,例如 Authorization"
                class="header-name"
              />
              <el-input
                v-model="item.value"
                type="password"
                show-password
                autocomplete="new-password"
                :placeholder="
                  keepsSaved(item)
                    ? '已设置,留空表示不修改'
                    : '值,例如 Bearer <接口令牌>'
                "
              />
              <el-button link type="danger" @click="headers.splice(index, 1)">
                删除
              </el-button>
            </div>
            <el-button
              size="small"
              :disabled="headers.length >= MAX_HEADERS"
              @click="addHeader"
            >
              添加请求头
            </el-button>
            <div class="hint">
              比如认证用的 Authorization: Bearer
              &lt;接口令牌&gt;。值加密保存,保存后不再显示;最多
              {{ MAX_HEADERS }} 个
            </div>
          </div>
        </el-form-item>
        <el-form-item label="超时(秒)">
          <el-input-number v-model="form.timeout" :min="1" :max="60" />
        </el-form-item>
        <el-form-item label="HTTPS 证书">
          <el-checkbox v-model="form.tls_verify">校验 HTTPS 证书</el-checkbox>
          <span class="hint inline">接口用自签证书时可以关闭(不推荐)</span>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="saving" @click="save">
            保存
          </el-button>
          <el-button @click="load">重置</el-button>
          <span v-if="updatedBy" class="hint inline">
            上次修改:{{ updatedBy }} · {{ formatTime(updatedAt) }}
          </span>
        </el-form-item>
      </el-form>
    </el-card>

    <el-card class="mt-3">
      <template #header>接口约定(开发接口时按这个实现)</template>
      <div class="section-title">请求:master 发给接口</div>
      <pre class="example">{{ REQUEST_EXAMPLE }}</pre>
      <el-table :data="REQUEST_FIELDS" border size="small" class="mt-2">
        <el-table-column label="字段" width="130">
          <template #default="{ row }">
            <code>{{ row.field }}</code>
          </template>
        </el-table-column>
        <el-table-column prop="desc" label="说明" />
      </el-table>
      <ul class="rules">
        <li>
          每次都会传这 4 个字段,接口用不到的字段不处理就行(比如只靠 namespace +
          image 就能找到代码,其他字段忽略即可)
        </li>
        <li>
          请求发到上面配置的接口地址,带上上面配置的请求头;Content-Type 固定为
          application/json
        </li>
      </ul>

      <div class="section-title">返回:接口返回给 master</div>
      <pre class="example">{{ RESPONSE_EXAMPLE }}</pre>
      <el-table :data="RESPONSE_FIELDS" border size="small" class="mt-2">
        <el-table-column label="字段" width="130">
          <template #default="{ row }">
            <code>{{ row.field }}</code>
          </template>
        </el-table-column>
        <el-table-column prop="label" label="弹窗里显示为" width="120" />
        <el-table-column prop="desc" label="说明" />
      </el-table>
      <ul class="rules">
        <li>
          返回 HTTP 200,body 是 JSON 对象;链接都要是 http:// 或 https://
          开头的完整地址,其他格式(比如 javascript:)会被忽略
        </li>
        <li>
          链接是在用户的浏览器里新窗口打开的,要返回浏览器能访问到的地址,不要用
          127.0.0.1
          这类只有接口服务自己能访问的地址;下载链接可以像示例那样带有效期和签名
        </li>
        <li>
          查不到的字段可以不返回或者返回空字符串,弹窗里显示「未提供」;三个都没有按失败处理
        </li>
        <li>
          找不到代码等出错情况返回非 2xx 状态码,body 带上
          <code>{"message": "原因"}</code>,弹窗里会显示这个原因
        </li>
        <li>
          超过上面配置的超时时间没返回按失败处理;不跟随重定向(返回 3xx
          按失败处理);响应最大 1MB
        </li>
      </ul>
    </el-card>

    <el-card class="mt-3">
      <template #header>测试(用当前表单里的配置,不需要先保存)</template>
      <el-form label-width="120px">
        <el-form-item
          v-for="item in REQUEST_FIELDS"
          :key="item.field"
          :label="item.field"
        >
          <el-input v-model="test[item.field]" :placeholder="item.example" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" plain :loading="testing" @click="runTest">
            测试
          </el-button>
        </el-form-item>
      </el-form>
      <template v-if="testResult">
        <el-alert
          :type="testResult.ok ? 'success' : 'error'"
          :title="testSummary"
          :closable="false"
          show-icon
        />
        <el-descriptions :column="1" border size="small" class="mt-3">
          <el-descriptions-item
            v-for="item in RESPONSE_FIELDS"
            :key="item.field"
            :label="item.label"
          >
            <a
              v-if="isHttpUrl(testResult.links[item.field])"
              :href="testResult.links[item.field]"
              target="_blank"
              rel="noopener noreferrer"
              class="code-link"
              >{{ testResult.links[item.field] }}</a
            >
            <span v-else-if="testResult.invalid[item.field]" class="error-text">
              不是 http(s) 地址,已忽略:{{ testResult.invalid[item.field] }}
            </span>
            <span v-else class="muted">未提供</span>
          </el-descriptions-item>
        </el-descriptions>
        <div class="section-title">发出的请求体</div>
        <pre class="example">{{ testRequestText }}</pre>
        <div class="section-title">
          原始响应{{ testResult.status ? `(HTTP ${testResult.status})` : "" }}
        </div>
        <pre class="example">{{ testResult.body || "(空)" }}</pre>
      </template>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import dayjs from "dayjs";
import { message } from "@/utils/message";
import {
  type CodeInfoConfig,
  type CodeInfoLinks,
  type CodeInfoRequest,
  type CodeInfoTestResult,
  getCodeInfoConfig,
  saveCodeInfoConfig,
  testCodeInfoConfig
} from "@/api/system";

defineOptions({ name: "SystemCodeInfo" });

// 接口约定:改了要和 master func_manager/code_info.py 一起改
const MAX_HEADERS = 10;
const COMMIT = "9f9a76c5f372e60a247af4b60e2ff131323d3231";

const REQUEST_FIELDS: {
  field: keyof CodeInfoRequest;
  desc: string;
  example: string;
}[] = [
  {
    field: "k8s",
    desc: "集群名(KubeDoor 里的 K8S 集群名称)",
    example: "prod"
  },
  { field: "namespace", desc: "命名空间", example: "vip" },
  { field: "deployment", desc: "Deployment 名称", example: "user-service" },
  {
    field: "image",
    desc: "Deployment 第一个容器的完整镜像地址(含 tag),点击时从集群实时读取",
    example:
      "registry.example.com/team/user-service:release-E14-1-b9342-9f9a76c5"
  }
];

const RESPONSE_FIELDS: {
  field: keyof CodeInfoLinks;
  label: string;
  desc: string;
}[] = [
  {
    field: "web_url",
    label: "代码页面",
    desc: "GitLab 上这个仓库,或者这个 commit 的代码页面"
  },
  {
    field: "download_url",
    label: "下载链接",
    desc: "这个 commit 的代码压缩包"
  },
  {
    field: "commits_url",
    label: "Commit 说明",
    desc: "这个仓库最近一些 commit 的提交说明"
  }
];

const REQUEST_EXAMPLE = [
  "POST /repo-url HTTP/1.1",
  "Host: code-api.example.com",
  "Authorization: Bearer <接口令牌>",
  "Content-Type: application/json",
  "",
  JSON.stringify(
    Object.fromEntries(REQUEST_FIELDS.map(item => [item.field, item.example])),
    null,
    2
  )
].join("\n");

const RESPONSE_EXAMPLE = [
  "HTTP/1.1 200 OK",
  "Content-Type: application/json; charset=utf-8",
  "",
  JSON.stringify(
    {
      web_url: `https://gitlab.example.com/vip/user-service/tree/${COMMIT}`,
      download_url: `https://code-api.example.com/archive/164/${COMMIT}/user-service-${COMMIT}.tar.gz?expires=1793543448&sig=ac172bd85931b93f75364e0dff5f8f75`,
      commits_url: `https://code-api.example.com/commits/164/${COMMIT}?expires=1793543448&sig=ac172bd85931b93f75364e0dff5f8f75`
    },
    null,
    2
  )
].join("\n");

interface HeaderRow {
  id: number;
  name: string;
  value: string;
  /** 加载时已经保存过值 */
  has_value: boolean;
  /** 加载时的名称:改了名称就不能再沿用原来的值 */
  savedName: string;
}

const loading = ref(false);
const saving = ref(false);
const testing = ref(false);
const updatedBy = ref("");
const updatedAt = ref("");
const headers = ref<HeaderRow[]>([]);
const testResult = ref<CodeInfoTestResult | null>(null);
const test = reactive<CodeInfoRequest>({
  k8s: "",
  namespace: "",
  deployment: "",
  image: ""
});
let headerSeq = 0;

const form = reactive({
  enabled: false,
  url: "",
  timeout: 10,
  tls_verify: true
});

const testSummary = computed(() => {
  const result = testResult.value;
  if (!result) return "";
  const meta = [
    result.status ? `HTTP ${result.status}` : "",
    `耗时 ${result.elapsed_ms} ms`
  ]
    .filter(Boolean)
    .join(",");
  return result.ok
    ? `测试通过(${meta})`
    : `测试未通过:${result.error}(${meta})`;
});

const testRequestText = computed(() =>
  JSON.stringify(testResult.value?.request ?? {}, null, 2)
);

/** 后端只放行 http(s) 地址,这里再挡一次 javascript: 之类的链接 */
const isHttpUrl = (value?: string) => !!value && /^https?:\/\//i.test(value);

const keepsSaved = (item: HeaderRow) =>
  item.has_value &&
  item.name.trim().toLowerCase() === item.savedName.toLowerCase();

function formatTime(value: string) {
  return value ? dayjs(value).format("YYYY-MM-DD HH:mm:ss") : "";
}

function addHeader() {
  headers.value.push({
    id: ++headerSeq,
    name: "",
    value: "",
    has_value: false,
    savedName: ""
  });
}

/** 提交给后端的配置:请求头的值留空表示沿用已保存的值,名称和值都空的行忽略 */
function payload(): CodeInfoConfig {
  return {
    enabled: form.enabled,
    url: form.url.trim(),
    timeout: form.timeout,
    tls_verify: form.tls_verify,
    headers: headers.value
      .filter(item => item.name.trim() || item.value)
      .map(item => ({ name: item.name.trim(), value: item.value || null }))
  };
}

function applyConfig(data: CodeInfoConfig) {
  form.enabled = data.enabled;
  form.url = data.url;
  form.timeout = data.timeout;
  form.tls_verify = data.tls_verify;
  headers.value = (data.headers ?? []).map(item => ({
    id: ++headerSeq,
    name: item.name,
    value: "",
    has_value: !!item.has_value,
    savedName: item.name
  }));
  updatedBy.value = data.updated_by ?? "";
  updatedAt.value = data.updated_at ?? "";
}

async function load() {
  loading.value = true;
  try {
    const res = await getCodeInfoConfig();
    applyConfig(res.data);
  } finally {
    loading.value = false;
  }
}

async function save() {
  saving.value = true;
  try {
    const res = await saveCodeInfoConfig(payload());
    applyConfig(res.data);
    message("获取代码接口已保存", { type: "success" });
  } finally {
    saving.value = false;
  }
}

async function runTest() {
  testing.value = true;
  testResult.value = null;
  try {
    const request = Object.fromEntries(
      REQUEST_FIELDS.map(item => [item.field, test[item.field].trim()])
    ) as unknown as CodeInfoRequest;
    const res = await testCodeInfoConfig({ config: payload(), request });
    testResult.value = res.data;
    message(res.data.ok ? "测试通过" : "测试未通过,请看下面的结果", {
      type: res.data.ok ? "success" : "warning"
    });
  } finally {
    testing.value = false;
  }
}

onMounted(load);
</script>

<style lang="scss" scoped>
.system-code-info {
  max-width: 980px;
  padding: 4px;

  :deep(.el-descriptions__label) {
    width: 110px;
    white-space: nowrap;
  }
}

.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.hint {
  display: block;
  font-size: 12px;
  line-height: 1.6;
  color: var(--el-text-color-secondary);

  &.inline {
    display: inline;
    margin-left: 8px;
  }
}

.headers {
  width: 100%;
}

.header-row {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-bottom: 8px;
}

.header-name {
  flex: 0 0 240px;
}

.section-title {
  margin: 12px 0 6px;
  font-weight: 600;
}

.example {
  padding: 10px 12px;
  margin: 0;
  font-family: Menlo, Consolas, monospace;
  font-size: 12px;
  line-height: 1.6;
  word-break: break-all;
  white-space: pre-wrap;
  background: var(--el-fill-color-light);
  border-radius: 4px;
}

.rules {
  padding-left: 20px;
  margin: 8px 0 4px;
  font-size: 13px;
  line-height: 1.8;
  color: var(--el-text-color-regular);
  list-style: disc;
}

.code-link {
  color: var(--el-color-primary);
  word-break: break-all;

  &:hover {
    text-decoration: underline;
  }
}

.muted {
  color: var(--el-text-color-secondary);
}

.error-text {
  color: var(--el-color-danger);
  word-break: break-all;
}
</style>
