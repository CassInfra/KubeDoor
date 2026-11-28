<script setup lang="ts">
import { onMounted, reactive, ref } from "vue";
import { ElMessage } from "element-plus";
import dayjs from "dayjs";
import {
  aiApi,
  type AIPublicShareConfig,
  type AIPublicShareConfigInput
} from "@/api/ai";

defineOptions({ name: "SystemAIPublicShare" });

const loading = ref(false);
const saving = ref(false);
const testing = ref(false);
const loaded = ref(false);
const config = ref<AIPublicShareConfig | null>(null);
const testResult = ref("");
const formatTime = (value: string | null) =>
  value ? dayjs(value).format("YYYY-MM-DD HH:mm") : "";
const form = reactive({
  account_id: "",
  api_token: "",
  worker_name: "kubedoor-ai-share",
  kv_namespace_id: "",
  public_base_url: ""
});

function payload(): AIPublicShareConfigInput {
  return {
    account_id: form.account_id.trim(),
    api_token: form.api_token || undefined,
    worker_name: form.worker_name.trim(),
    kv_namespace_id: form.kv_namespace_id.trim(),
    public_base_url: form.public_base_url.trim()
  };
}

function fill(value: AIPublicShareConfig) {
  config.value = value;
  form.account_id = value.account_id || "";
  form.worker_name = value.worker_name || "kubedoor-ai-share";
  form.kv_namespace_id = value.kv_namespace_id || "";
  form.public_base_url = value.public_base_url || "";
  form.api_token = "";
  loaded.value = true;
}

async function load() {
  loading.value = true;
  testResult.value = "";
  try {
    fill(await aiApi.publicShareConfig());
  } catch (error) {
    ElMessage.error(
      error instanceof Error ? error.message : "读取公网分享配置失败"
    );
  } finally {
    loading.value = false;
  }
}

async function save() {
  if (!loaded.value) {
    ElMessage.warning("配置还没读取成功，请刷新后重试。");
    return;
  }
  saving.value = true;
  try {
    fill(await aiApi.savePublicShareConfig(payload()));
    testResult.value = "";
    ElMessage.success(
      "配置已保存，Worker 已部署。发布公网分享时会用到这份配置。"
    );
  } catch (error) {
    ElMessage.error(
      error instanceof Error ? error.message : "保存公网分享配置失败"
    );
  } finally {
    saving.value = false;
  }
}

async function test() {
  if (!loaded.value) {
    ElMessage.warning("配置还没读取成功，请刷新后重试。");
    return;
  }
  testing.value = true;
  testResult.value = "";
  try {
    const result = await aiApi.testPublicShareConfig(payload());
    testResult.value = result.kv_namespace_id
      ? `连接成功：Account ${result.account_id}，Worker ${result.worker_name} 可以部署，KV 权限有效。`
      : `连接成功：Account ${result.account_id}，Worker ${result.worker_name} 可以部署；保存时会自动创建 KV Namespace。`;
    ElMessage.success("Cloudflare 连接成功。");
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "测试失败");
  } finally {
    testing.value = false;
  }
}

onMounted(load);
</script>

<template>
  <div v-loading="loading" class="ai-public-share">
    <div class="page-heading">
      <div>
        <h2>AI 公网分享</h2>
        <p>
          把花折 AI 的会话发布成脱敏的静态快照到 Cloudflare
          Workers。公网链接不需要登录
          KubeDoor；内容是发布时的快照，之后的新消息不会同步。
        </p>
      </div>
      <el-button :disabled="loading || saving" @click="load"
        >重新加载</el-button
      >
    </div>

    <el-alert
      type="info"
      :closable="false"
      show-icon
      class="page-alert"
      title="公网分享只包含发布时的内容；工具调用只显示名称，不能展开参数和结果；正文会自动脱敏密码、令牌、API Key、IP、邮箱、手机号等。"
    />
    <el-alert
      v-if="testResult"
      type="success"
      :closable="false"
      show-icon
      class="page-alert"
      :title="testResult"
    />
    <el-alert
      v-if="config?.warning"
      type="warning"
      :closable="false"
      show-icon
      class="page-alert"
      :title="config.warning"
    />

    <section class="settings-section">
      <div class="section-heading">
        <div>
          <h3>Cloudflare 配置</h3>
          <p>
            {{ config?.configured ? "已配置" : "尚未配置" }}
            <span v-if="config?.updated_at">
              · 更新于 {{ formatTime(config.updated_at) }}
            </span>
            <span v-if="config?.updated_by"> · {{ config.updated_by }}</span>
          </p>
        </div>
        <el-tag :type="config?.configured ? 'success' : 'info'" effect="plain">
          {{ config?.configured ? "已配置" : "未配置" }}
        </el-tag>
      </div>

      <el-form label-width="160px" class="config-form">
        <el-form-item label="Account ID" required>
          <el-input
            v-model="form.account_id"
            placeholder="Cloudflare 账号 ID（32 位十六进制）"
            autocomplete="off"
          />
          <div class="field-tip">
            Cloudflare 控制台右侧的 Account ID，也可以在 Workers & Pages
            的地址栏里看到。换 Account
            后旧账号的快照不会迁移，旧链接仍指向旧账号。
          </div>
        </el-form-item>

        <el-form-item label="API Token" required>
          <el-input
            v-model="form.api_token"
            type="password"
            show-password
            autocomplete="new-password"
            :placeholder="
              config?.api_token_set
                ? '已保存，留空保持不变'
                : 'Cloudflare API Token'
            "
          />
          <div class="field-tip">
            建议 新建一个自定义 Token，至少给 Account → Workers Scripts →
            Edit、Workers KV Storage → Edit；Token 用 AI_ENCRYPTION_KEY
            加密保存，页面只显示是否已设置。
          </div>
        </el-form-item>

        <el-form-item label="Worker 名称" required>
          <el-input
            v-model="form.worker_name"
            placeholder="kubedoor-ai-share"
            autocomplete="off"
          />
          <div class="field-tip">
            只能用小写字母、数字和连字符。保存时会自动部署这个
            Worker，并覆盖同名 Worker 的脚本和绑定（只保留本功能需要的 KV
            绑定）；改名字会部署一个新的 Worker，旧 Worker
            不会自动删除，旧链接仍可能可访问，确认不需要后请在 Cloudflare 删除旧
            Worker。
          </div>
        </el-form-item>

        <el-form-item label="KV Namespace ID">
          <el-input
            v-model="form.kv_namespace_id"
            placeholder="留空自动创建 kubedoor-ai-share-kv"
            autocomplete="off"
          />
          <div class="field-tip">
            存放公网快照的 Workers KV
            命名空间。留空时自动创建并绑定；填了已存在的 ID 就直接使用。换
            Namespace 不会迁移旧快照，旧链接仍读原来的
            Namespace，需要的话先取消公网分享。
          </div>
        </el-form-item>

        <el-form-item label="公网访问地址" required>
          <el-input
            v-model="form.public_base_url"
            placeholder="https://kubedoor-ai-share.你的子域.workers.dev"
            autocomplete="off"
          />
          <div class="field-tip">
            Worker 的访问地址，分享链接是「地址/s/分享码」。用 workers.dev
            子域名或已经在 Cloudflare 绑定到这个 Worker 的自定义域名。
          </div>
        </el-form-item>

        <div class="form-actions">
          <el-button :loading="testing" :disabled="saving" @click="test"
            >测试连接</el-button
          >
          <el-button
            type="primary"
            :loading="saving"
            :disabled="testing"
            @click="save"
            >保存并部署</el-button
          >
        </div>
      </el-form>
    </section>

    <section class="settings-section">
      <div class="section-heading">
        <div>
          <h3>公网分享规则</h3>
          <p>发布前请确认内容可以公开。</p>
        </div>
      </div>
      <ul class="rule-list">
        <li>公网链接不需要登录 KubeDoor，任何拿到链接的人都能查看。</li>
        <li>
          只包含发布时的内容，之后的新消息不会同步。再次发布会生成新链接，旧链接失效。
        </li>
        <li>
          工具调用只显示名称和状态，不包含参数、结果、集群、命名空间和记忆附件。
        </li>
        <li>
          正文自动脱敏密码、令牌、API
          Key、Bearer、IP、邮箱、手机号、证件号等；分享前仍建议人工检查一遍。
        </li>
        <li>
          取消公网分享会删除 Cloudflare
          上的快照；删除会话时也会先清理公网快照，清理失败会阻止删除会话。
        </li>
      </ul>
    </section>
  </div>
</template>

<style scoped>
.ai-public-share {
  display: grid;
  gap: 18px;
  padding: 20px;
}

.page-heading {
  display: flex;
  gap: 16px;
  align-items: flex-start;
  justify-content: space-between;
}

.page-heading h2 {
  margin: 0;
  font-size: 20px;
  font-weight: 600;
}

.page-heading p {
  margin: 7px 0 0;
  line-height: 1.6;
  color: var(--el-text-color-secondary);
}

.page-alert {
  border-radius: 10px;
}

.settings-section {
  padding: 18px 20px;
  background: var(--el-bg-color);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 14px;
}

.section-heading {
  display: flex;
  gap: 16px;
  align-items: flex-start;
  justify-content: space-between;
  margin-bottom: 16px;
}

.section-heading h3 {
  margin: 0;
  font-size: 16px;
  font-weight: 600;
}

.section-heading p {
  margin: 5px 0 0;
  font-size: 12px;
  line-height: 1.5;
  color: var(--el-text-color-secondary);
}

.config-form {
  max-width: 820px;
}

.field-tip {
  margin-top: 4px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--el-text-color-secondary);
}

.form-actions {
  display: flex;
  gap: 8px;
  justify-content: flex-end;
}

.rule-list {
  padding-left: 20px;
  margin: 0;
  font-size: 13px;
  line-height: 1.9;
  color: var(--el-text-color-regular);
}

@media (width <= 640px) {
  .ai-public-share {
    padding: 12px;
  }

  .page-heading,
  .section-heading {
    flex-direction: column;
  }

  .settings-section {
    padding: 14px;
  }

  :deep(.el-form-item) {
    display: block;
  }

  :deep(.el-form-item__label) {
    width: 100% !important;
    text-align: left;
  }
}
</style>
