<template>
  <div class="system-code-info">
    <div class="left-column">
      <el-card v-loading="loading">
        <template #header>
          <div class="card-header">
            <span>获取代码接口</span>
            <!-- 拨动立即生效:只改启用状态,表单里没保存的修改不受影响 -->
            <el-switch
              v-model="form.enabled"
              :before-change="toggleEnabled"
              :loading="switching"
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
            说明三个链接,在弹窗里显示(点击在新窗口打开)。接口格式见「接口约定」。点「保存」后自动启用,右上角的开关可以随时停用
            / 启用,立即生效。
          </template>
        </el-alert>

        <el-form :model="form" label-width="120px" class="code-info-form">
          <el-form-item label="接口地址" required>
            <el-input
              v-model="form.url"
              placeholder="https://code-api.example.com/repo-url"
            />
            <div class="hint">
              由 master 在服务端发起 POST 请求,地址要能从 master 所在的 Pod
              访问到
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
        <template #header>
          <div class="card-header">
            <span>AI 助手</span>
            <!-- 同右上角的启用开关:拨动立即生效,只改这一项 -->
            <el-switch
              v-model="form.ai_download"
              :before-change="toggleAiDownload"
              :loading="switchingAi"
              active-text="允许 AI 下载并分析源码"
            />
          </div>
        </template>
        <ul class="rules ai-rules">
          <li>
            AI 助手(网页和企业微信)可以按 Deployment 或 Pod
            查服务当前的镜像和上面三个链接,用的就是这个接口,不需要另外开启
          </li>
          <li>
            打开右上角的开关后,排障需要看源码时(比如日志里有异常堆栈),AI
            会下载这个版本的源码,在 kubedoor-ai
            服务里解压、搜索和阅读代码,再给出结论
          </li>
          <li>
            源码由 kubedoor-ai 在服务端 GET 请求 download_url
            下载(不带上面的请求头,会跟随重定向),地址要能从 kubedoor-ai 所在的
            Pod 访问到;「校验 HTTPS 证书」同样适用
          </li>
          <li>
            下载的源码按镜像缓存在 kubedoor-ai 的临时目录,24
            小时没用自动删除;关掉开关后,已下载的源码 AI 也不能再用
          </li>
        </ul>
        <el-alert
          type="warning"
          :closable="false"
          show-icon
          title="AI 读到的源码片段会作为对话内容发给模型服务商,请先确认符合公司的代码安全要求再打开"
        />
      </el-card>

      <el-card class="mt-3">
        <template #header>测试(用当前表单里的配置,不需要先保存)</template>
        <div class="hint test-hint">
          4 个入参都可以不填,没填的字段不放进请求体;实际调用时 4 个字段都会传
        </div>
        <el-form label-width="120px">
          <el-form-item
            v-for="item in REQUEST_FIELDS"
            :key="item.field"
            :label="item.field"
          >
            <el-input
              v-model="test[item.field]"
              :placeholder="`可选,例如 ${item.example}`"
              clearable
            />
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
              <span
                v-else-if="testResult.invalid[item.field]"
                class="error-text"
              >
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

    <el-card>
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
          请求发到配置的接口地址,带上配置的请求头;Content-Type 固定为
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
          超过配置的超时时间没返回按失败处理;不跟随重定向(返回 3xx
          按失败处理);响应最大 1MB
        </li>
        <li>
          允许 AI 下载并分析源码时,kubedoor-ai 会 GET 下载
          download_url:不带请求头,跟随重定向;支持 .tar.gz / .tgz / .zip /
          .tar.bz2 / .tar.xz,压缩包最大 300MB
        </li>
      </ul>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import dayjs from "dayjs";
import { ElMessageBox } from "element-plus";
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
  {
    field: "deployment",
    desc: "Deployment 名称(AI 按 Pod 查时是 Pod 所属的 Deployment)",
    example: "user-service"
  },
  {
    field: "image",
    desc: "Deployment 第一个容器的完整镜像地址(含 tag),从集群实时读取;AI 按 Pod 查时是这个 Pod 第一个容器的镜像",
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
    desc: "这个 commit 的代码压缩包;允许 AI 分析源码时 AI 也从这里下载"
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
const switching = ref(false);
const switchingAi = ref(false);
const testing = ref(false);
const updatedBy = ref("");
const updatedAt = ref("");
// 已保存的接口地址:没保存过地址时不能打开开关
const savedUrl = ref("");
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
  tls_verify: true,
  ai_download: false
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

/** 表单内容(不含启用状态):请求头的值留空表示沿用已保存的值,名称和值都空的行忽略 */
function formPayload() {
  return {
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
  form.ai_download = data.ai_download;
  savedUrl.value = data.url;
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

/** 保存即启用 */
async function save() {
  if (!form.url.trim()) {
    message("请填写接口地址", { type: "warning" });
    return;
  }
  saving.value = true;
  try {
    const res = await saveCodeInfoConfig({ ...formPayload(), enabled: true });
    applyConfig(res.data);
    message("已保存并启用", { type: "success" });
  } finally {
    saving.value = false;
  }
}

/** 卡片右上角的开关:拨动立即保存,只传这一项(地址、请求头沿用已保存的),表单里没保存的修改保留 */
async function saveSwitch(
  data: Partial<CodeInfoConfig>,
  done: string
): Promise<boolean> {
  try {
    const res = await saveCodeInfoConfig(data);
    updatedBy.value = res.data.updated_by ?? "";
    updatedAt.value = res.data.updated_at ?? "";
    message(done, { type: "success" });
    return true;
  } catch {
    return false;
  }
}

async function toggleEnabled(): Promise<boolean> {
  const next = !form.enabled;
  if (next && !savedUrl.value) {
    message("请先填写接口地址并点「保存」,保存后会自动启用", {
      type: "warning"
    });
    return false;
  }
  switching.value = true;
  try {
    return await saveSwitch({ enabled: next }, next ? "已启用" : "已停用");
  } finally {
    switching.value = false;
  }
}

/** 打开前确认:源码会发给模型服务商 */
async function toggleAiDownload(): Promise<boolean> {
  const next = !form.ai_download;
  if (next) {
    try {
      await ElMessageBox.confirm(
        "打开后,AI 排障时读到的源码片段会作为对话内容发给模型服务商。请确认符合公司的代码安全要求。",
        "允许 AI 下载并分析源码",
        { type: "warning", confirmButtonText: "打开", cancelButtonText: "取消" }
      );
    } catch {
      return false;
    }
  }
  switchingAi.value = true;
  try {
    return await saveSwitch(
      { ai_download: next },
      next ? "已允许 AI 下载并分析源码" : "已关闭 AI 下载源码"
    );
  } finally {
    switchingAi.value = false;
  }
}

async function runTest() {
  testing.value = true;
  testResult.value = null;
  try {
    // 入参都可以不填,没填的不放进请求体
    const request: Partial<CodeInfoRequest> = {};
    for (const item of REQUEST_FIELDS) {
      const value = test[item.field].trim();
      if (value) request[item.field] = value;
    }
    // 测试不看启用状态
    const config = { ...formPayload(), enabled: false };
    const res = await testCodeInfoConfig({ config, request });
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
// 两列:左边配置 + 测试,右边接口约定;窄屏时接口约定排到下面
.system-code-info {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 12px;
  align-items: start;
  max-width: 1800px;
  padding: 4px;

  @media (width <= 1280px) {
    grid-template-columns: minmax(0, 1fr);
  }

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

.test-hint {
  margin-bottom: 12px;
}

.ai-rules {
  margin: 0 0 12px;
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
  flex: 0 0 200px;
}

.section-title {
  margin: 12px 0 6px;
  font-weight: 600;

  &:first-child {
    margin-top: 0;
  }
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
