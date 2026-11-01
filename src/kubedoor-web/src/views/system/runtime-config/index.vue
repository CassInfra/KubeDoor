<template>
  <div class="system-runtime-config">
    <div class="left-column">
      <el-card v-loading="loading">
        <template #header>
          <div class="card-header">
            <span>获取运行配置接口</span>
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
            Deployment 页、Pod 页「操作 → 获取运行配置」:master
            带上下面配置的请求头,把集群、命名空间和 Deployment(或 Pod)名称 POST
            到这个接口;接口返回这个 Pod
            实际运行的配置(JSON),在弹窗里格式化展示,可以按层级折叠、搜索。接口格式见「接口约定」。点「保存」后自动启用,右上角的开关可以随时停用
            / 启用,立即生效。
          </template>
        </el-alert>

        <el-form :model="form" label-width="120px">
          <el-form-item label="接口地址" required>
            <el-input
              v-model="form.url"
              placeholder="https://pod2config.example.com/pod-config"
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
            <el-input-number
              v-model="form.timeout"
              :min="MIN_TIMEOUT"
              :max="MAX_TIMEOUT"
            />
            <div class="hint">
              一次获取最多等多久({{ MIN_TIMEOUT }}~{{ MAX_TIMEOUT }}
              秒),包括接口返回
              202(还在采集)后的等待和重试;到时间还没拿到就提示用户稍后再试
            </div>
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
              v-model="form.ai_enabled"
              :before-change="toggleAi"
              :loading="switchingAi"
              active-text="允许 AI 获取运行配置"
            />
          </div>
        </template>
        <ul class="rules ai-rules">
          <li>
            打开后,AI 助手(网页和企业微信)可以按 Deployment 或 Pod
            获取运行配置,用的就是这个接口;和网页一样按提问人在该集群的「查看运行配置」权限判断
          </li>
          <li>
            给模型看的配置里,密码、密钥换成占位符,模型看不到明文;配置项的名称和其他值(地址、账号、开关、超时等)照常给模型
          </li>
          <li>
            排障时 AI 可以用配置里的连接信息只读查询 MySQL / Redis /
            Elasticsearch:要「AI 查询数据库 / Redis /
            ES」权限,默认每次查询都要提问人批准;执行时由服务端把占位符换成真实密码,查询结果里出现的密码也会打码
          </li>
          <li>
            查询从业务集群里的 agent
            发起(业务库通常只有业务集群里能连上),需要新版 agent
          </li>
          <li>关掉开关后,AI 不能获取运行配置,也不能用配置里的密码查询</li>
        </ul>
        <el-alert
          type="warning"
          :closable="false"
          show-icon
          title="配置项的名称、非密码的值和查询到的数据会作为对话内容发给模型服务商,请先确认符合公司的数据安全要求再打开"
        />
      </el-card>

      <el-card class="mt-3">
        <template #header>测试(用当前表单里的配置,不需要先保存)</template>
        <div class="hint test-hint">
          入参都可以不填,没填的字段不放进请求体;实际调用时 master 还会带上 wait
        </div>
        <el-form label-width="120px">
          <el-form-item
            v-for="item in TEST_FIELDS"
            :key="item.field"
            :label="item.field"
          >
            <el-input
              v-model="test[item.field]"
              :placeholder="`可选,例如 ${item.example}`"
              clearable
            />
          </el-form-item>
          <el-form-item label="detail">
            <el-checkbox v-model="test.detail">附带配置来源</el-checkbox>
          </el-form-item>
          <el-form-item label="refresh">
            <el-checkbox v-model="test.refresh">不用缓存,重新采集</el-checkbox>
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
          <div class="section-title">发出的请求体</div>
          <pre class="example">{{ testRequestText }}</pre>
          <template v-if="testText">
            <div class="section-title result-title">
              <span>返回的 JSON</span>
              <span>
                <el-button link type="primary" @click="testViewRef?.find()">
                  查找
                </el-button>
                <el-button
                  link
                  type="primary"
                  @click="testViewRef?.expandAll()"
                >
                  全部展开
                </el-button>
                <el-button
                  link
                  type="primary"
                  @click="testViewRef?.collapseAll()"
                >
                  全部折叠
                </el-button>
                <el-button link type="primary" @click="copyTestResult">
                  复制
                </el-button>
              </span>
            </div>
            <JsonView ref="testViewRef" :value="testText" height="480px" />
          </template>
          <template v-else>
            <div class="section-title">
              原始响应{{
                testResult.status ? `(HTTP ${testResult.status})` : ""
              }}
            </div>
            <pre class="example">{{ testResult.body || "(空)" }}</pre>
          </template>
        </template>
      </el-card>
    </div>

    <el-card>
      <template #header>接口约定(开发接口时按这个实现)</template>
      <div class="section-title">请求:master 发给接口</div>
      <pre class="example">{{ REQUEST_EXAMPLE }}</pre>
      <el-table :data="REQUEST_FIELDS" border size="small" class="mt-2">
        <el-table-column label="字段" width="120">
          <template #default="{ row }">
            <code>{{ row.field }}</code>
          </template>
        </el-table-column>
        <el-table-column prop="required" label="必填" width="160" />
        <el-table-column prop="desc" label="说明" />
      </el-table>
      <ul class="rules">
        <li>
          Deployment 页的按钮传 deployment,Pod 页和 Deployment 明细里 Pod
          的按钮传 pod;可选字段只在有值时传,wait 每次都传
        </li>
        <li>
          请求发到配置的接口地址,带上配置的请求头;Content-Type 固定为
          application/json
        </li>
      </ul>

      <div class="section-title">返回:接口返回给 master</div>
      <pre class="example">{{ RESPONSE_EXAMPLE }}</pre>
      <div class="hint table-hint">建议包含的字段:</div>
      <el-table :data="RESPONSE_FIELDS" border size="small">
        <el-table-column label="字段" width="220">
          <template #default="{ row }">
            <code>{{ row.field }}</code>
          </template>
        </el-table-column>
        <el-table-column prop="desc" label="说明" />
      </el-table>
      <ul class="rules">
        <li>
          返回 HTTP 200,body 是 JSON 对象:KubeDoor
          原样展示(弹窗里格式化成可以按层级折叠的
          JSON),上面的字段是建议,没有的就不显示;AI
          获取时按这些字段整理,密码、密钥换成占位符
        </li>
        <li>
          首次采集比较慢时可以先返回 HTTP 202,带上响应头
          <code>Retry-After: 秒数</code>,接口在后台接着采:master 等 Retry-After
          秒(缺省 3 秒,按 1~10 秒取)后用同样的请求体再请求(wait
          重新计算),直到配置的超时时间用完;到时间还没采完就提示用户稍后再试
        </li>
        <li>
          出错返回非 2xx 状态码,body 带上
          <code>{"error": "原因"}</code>(也认
          message、msg、detail),弹窗里会显示这个原因
        </li>
        <li>
          不跟随重定向(返回 3xx 按失败处理);响应最大
          10MB;每次请求最多等剩余时间再加 5 秒
        </li>
        <li>
          返回的是明文配置(含密码):网页上只有「查看运行配置」权限的账号能获取(「权限与审批」里默认读写及以上),每次获取都记操作审计(谁、哪个服务、结果和耗时,不记配置值);接口自己也要认证(比如上面的
          Bearer 令牌),只给 KubeDoor 调用
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
import { copyText } from "@/utils/clipboard";
import JsonView from "@/components/JsonView.vue";
import {
  type RuntimeConfigRequest,
  type RuntimeConfigSettings,
  type RuntimeConfigTestResult,
  getRuntimeConfigSettings,
  saveRuntimeConfigSettings,
  testRuntimeConfigSettings
} from "@/api/system";

defineOptions({ name: "SystemRuntimeConfig" });

// 接口约定:改了要和 master func_manager/runtime_config.py 一起改
const MAX_HEADERS = 10;
const MIN_TIMEOUT = 5;
const MAX_TIMEOUT = 120;

type TextField =
  | "k8s"
  | "namespace"
  | "deployment"
  | "pod"
  | "container"
  | "keys";

const TEST_FIELDS: { field: TextField; example: string }[] = [
  { field: "k8s", example: "prod" },
  { field: "namespace", example: "vip" },
  { field: "deployment", example: "user-service" },
  { field: "pod", example: "user-service-7d9c6b5f4-x2k8p" },
  { field: "container", example: "user-service" },
  { field: "keys", example: "datasource|redis" }
];

const REQUEST_FIELDS: { field: string; required: string; desc: string }[] = [
  {
    field: "k8s",
    required: "是",
    desc: "集群名(KubeDoor 里的 K8S 集群名称)"
  },
  { field: "namespace", required: "是", desc: "命名空间" },
  {
    field: "deployment",
    required: "和 pod 二选一",
    desc: "Deployment 名称(Deployment 页的按钮、AI 按 Deployment 获取时传):由接口挑一个 Pod,返回里写明用的是哪个"
  },
  {
    field: "pod",
    required: "和 deployment 二选一",
    desc: "Pod 名称(Pod 页、Deployment 明细里 Pod 的按钮,AI 按 Pod 获取时传)"
  },
  {
    field: "container",
    required: "否",
    desc: "容器名(Pod 有多个容器时可以在弹窗里选);不传就用第一个容器"
  },
  {
    field: "keys",
    required: "否",
    desc: "按配置名过滤的正则,不区分大小写:只返回名称匹配的配置项和环境变量。用户在弹窗里填的原样转给接口(最长 500 字符,KubeDoor 不检查正则语法,不合法请返回 400 和原因)"
  },
  {
    field: "detail",
    required: "否",
    desc: "true:附带配置来源,每一项来自哪里、打码的值从哪里补回等(弹窗里的「配置来源」开关)"
  },
  {
    field: "refresh",
    required: "否",
    desc: "true:不用缓存,重新采集(弹窗里的「重新采集」)"
  },
  {
    field: "wait",
    required: "每次都传",
    desc: "这次请求还能等几秒(1~120 的整数,按配置的超时时间算出的剩余时间):接口可以等到这个时间再返回,还没采完就返回 202"
  }
];

const RESPONSE_FIELDS: { field: string; desc: string }[] = [
  {
    field: "cluster、namespace、deployment、pod、container、image",
    desc: "实际看的是哪个集群、哪个 Pod 的哪个容器,弹窗顶部会显示;pod 为 null 表示没有 Pod(副本数为 0)"
  },
  {
    field: "lang",
    desc: "语言:java、node、python、native(C / C++ / Go)"
  },
  {
    field: "centers",
    desc: "配置来源(数组),例如 config-server、apollo、nacos、configmap"
  },
  {
    field: "collected_at、cached",
    desc: "采集时间(ISO 8601,带时区)、这次是不是缓存里的结果"
  },
  { field: "started_at", desc: "容器这次启动的时间" },
  {
    field: "basis、reason",
    desc: "runtime:从运行中的进程取到的;inferred:拿不到进程(容器没在运行等),按配置来源推断,reason 写原因,弹窗里会提示"
  },
  {
    field: "config",
    desc: "生效的配置:平铺的「配置名: 值」,或者嵌套的对象"
  },
  { field: "other_config", desc: "不在 config 里、但进程在用的配置" },
  { field: "env", desc: "进程的环境变量" },
  {
    field: "files",
    desc: '进程读的配置文件:[{"path", "from", "content"}],from 是来源(比如哪个 ConfigMap)'
  },
  {
    field: "sources、filled、unresolved、notes",
    desc: "detail 为 true 时附带:配置源和各自提供了哪些配置、打码的值从哪里补回、补不回的配置名、说明"
  }
];

const REQUEST_EXAMPLE = [
  "POST /pod-config HTTP/1.1",
  "Host: pod2config.example.com",
  "Authorization: Bearer <接口令牌>",
  "Content-Type: application/json",
  "",
  JSON.stringify(
    {
      k8s: "prod",
      namespace: "vip",
      deployment: "user-service",
      keys: "datasource|redis",
      wait: 60
    },
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
      cluster: "prod",
      namespace: "vip",
      deployment: "user-service",
      pod: "user-service-7d9c6b5f4-x2k8p",
      container: "user-service",
      image:
        "registry.example.com/team/user-service:release-E14-1-b9342-9f9a76c5",
      lang: "java",
      centers: ["config-server"],
      collected_at: "2026-11-01T00:28:20+08:00",
      cached: true,
      config: {
        "server.port": "8080",
        "spring.datasource.url": "jdbc:mysql://mysql.example.com:3306/user",
        "spring.datasource.username": "user",
        "spring.datasource.password": "<明文>",
        "spring.redis.host": "redis.example.com"
      },
      env: { SPRING_PROFILES_ACTIVE: "prod" }
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
const testResult = ref<RuntimeConfigTestResult | null>(null);
// 测试返回的 JSON 格式化后的文本(可能有几 MB,只算一次)
const testText = ref("");
const testViewRef = ref<InstanceType<typeof JsonView> | null>(null);
const test = reactive({
  k8s: "",
  namespace: "",
  deployment: "",
  pod: "",
  container: "",
  keys: "",
  detail: false,
  refresh: false
});
let headerSeq = 0;

const form = reactive({
  enabled: false,
  url: "",
  timeout: 60,
  tls_verify: true,
  ai_enabled: false
});

const testSummary = computed(() => {
  const result = testResult.value;
  if (!result) return "";
  const meta = [
    result.status ? `HTTP ${result.status}` : "",
    `耗时 ${result.elapsed_ms} ms`,
    result.attempts > 1 ? `请求 ${result.attempts} 次` : ""
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

/** 表单内容(不含两个开关):请求头的值留空表示沿用已保存的值,名称和值都空的行忽略 */
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

function applyConfig(data: RuntimeConfigSettings) {
  form.enabled = data.enabled;
  form.url = data.url;
  form.timeout = data.timeout;
  form.tls_verify = data.tls_verify;
  form.ai_enabled = data.ai_enabled;
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
    const res = await getRuntimeConfigSettings();
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
    const res = await saveRuntimeConfigSettings({
      ...formPayload(),
      enabled: true
    });
    applyConfig(res.data);
    message("已保存并启用", { type: "success" });
  } finally {
    saving.value = false;
  }
}

/** 卡片右上角的开关:拨动立即保存,只传这一项(地址、请求头沿用已保存的),表单里没保存的修改保留 */
async function saveSwitch(
  data: Partial<RuntimeConfigSettings>,
  done: string
): Promise<boolean> {
  try {
    const res = await saveRuntimeConfigSettings(data);
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

/** 打开前确认:配置的名称和非密码的值会发给模型服务商 */
async function toggleAi(): Promise<boolean> {
  const next = !form.ai_enabled;
  if (next) {
    try {
      await ElMessageBox.confirm(
        "打开后,AI 排障时可以获取服务的运行配置:密码、密钥换成占位符,模型看不到明文,但配置项的名称和其他值会作为对话内容发给模型服务商;有「AI 查询数据库 / Redis / ES」权限的账号,还可以让 AI 用配置里的连接信息只读查询(默认每次都要批准)。请确认符合公司的数据安全要求。",
        "允许 AI 获取运行配置",
        { type: "warning", confirmButtonText: "打开", cancelButtonText: "取消" }
      );
    } catch {
      return false;
    }
  }
  switchingAi.value = true;
  try {
    return await saveSwitch(
      { ai_enabled: next },
      next ? "已允许 AI 获取运行配置" : "已关闭 AI 获取运行配置"
    );
  } finally {
    switchingAi.value = false;
  }
}

async function runTest() {
  testing.value = true;
  testResult.value = null;
  testText.value = "";
  try {
    // 入参都可以不填,没填的不放进请求体
    const request: RuntimeConfigRequest = {};
    for (const item of TEST_FIELDS) {
      const value = test[item.field].trim();
      if (value) request[item.field] = value;
    }
    if (test.detail) request.detail = true;
    if (test.refresh) request.refresh = true;
    // 测试不看启用状态
    const config = { ...formPayload(), enabled: false };
    const res = await testRuntimeConfigSettings({ config, request });
    testResult.value = res.data;
    const body = res.data.config;
    testText.value =
      body && typeof body === "object" ? JSON.stringify(body, null, 2) : "";
    message(res.data.ok ? "测试通过" : "测试未通过,请看下面的结果", {
      type: res.data.ok ? "success" : "warning"
    });
  } finally {
    testing.value = false;
  }
}

async function copyTestResult() {
  if (await copyText(testText.value)) {
    message("已复制", { type: "success" });
  } else {
    message("复制失败,可以点一下内容,按 Ctrl+A 全选后再复制", {
      type: "warning"
    });
  }
}

onMounted(load);
</script>

<style lang="scss" scoped>
// 两列:左边配置 + AI + 测试,右边接口约定;窄屏时接口约定排到下面
.system-runtime-config {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 12px;
  align-items: start;
  max-width: 1800px;
  padding: 4px;

  @media (width <= 1280px) {
    grid-template-columns: minmax(0, 1fr);
  }
}

.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

// 表单项里的提示独占一行(在输入框下面);inline 的跟在控件后面
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

.test-hint {
  margin-bottom: 12px;
}

.table-hint {
  margin: 8px 0 4px;
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

.result-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
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
</style>
