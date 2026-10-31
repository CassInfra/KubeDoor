<template>
  <el-dialog
    v-model="visible"
    title="获取代码"
    width="760px"
    append-to-body
    @closed="reset"
  >
    <div v-loading="loading" class="code-info">
      <el-descriptions :column="1" border>
        <el-descriptions-item label="集群">{{
          target.env
        }}</el-descriptions-item>
        <el-descriptions-item label="Deployment">
          {{ target.namespace }}/{{ target.deployment }}
        </el-descriptions-item>
        <el-descriptions-item label="镜像">
          <span class="break-all">{{ image || "-" }}</span>
        </el-descriptions-item>
        <template v-if="links">
          <el-descriptions-item
            v-for="item in LINK_ITEMS"
            :key="item.key"
            :label="item.label"
          >
            <a
              v-if="isHttpUrl(links[item.key])"
              :href="links[item.key]"
              target="_blank"
              rel="noopener noreferrer"
              class="code-link"
              >{{ links[item.key] }}</a
            >
            <span v-else-if="invalid[item.key]" class="muted">
              接口返回的不是 http(s) 地址,已忽略
            </span>
            <span v-else class="muted">未提供</span>
          </el-descriptions-item>
        </template>
      </el-descriptions>
      <el-alert
        v-if="error"
        type="error"
        :title="error"
        :closable="false"
        show-icon
        class="mt-3"
      />
    </div>
    <template #footer>
      <el-button :loading="loading" @click="load">重新获取</el-button>
      <el-button type="primary" @click="visible = false">关闭</el-button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { reactive, ref } from "vue";
import { getCodeInfo } from "@/api/monit";
import type { CodeInfoInvalid, CodeInfoLinks } from "@/api/system";

// 接口约定见「系统管理 → 获取代码接口」,master 调用(func_manager/code_info.py)
const LINK_ITEMS: { key: keyof CodeInfoLinks; label: string }[] = [
  { key: "web_url", label: "代码页面" },
  { key: "download_url", label: "下载链接" },
  { key: "commits_url", label: "Commit 说明" }
];

const visible = ref(false);
const loading = ref(false);
const target = reactive({ env: "", namespace: "", deployment: "" });
const image = ref("");
const links = ref<CodeInfoLinks | null>(null);
const invalid = ref<CodeInfoInvalid>({});
const error = ref("");
// 只认最后一次请求:关掉后或者重新获取时,之前没回来的结果丢掉
let seq = 0;

/** 后端只放行 http(s) 地址,这里再挡一次 javascript: 之类的链接 */
const isHttpUrl = (value?: string) => !!value && /^https?:\/\//i.test(value);

function errorMessage(err: any): string {
  const data = err?.response?.data;
  if (data?.message || data?.error) return data.message || data.error;
  if (err?.code === "ECONNABORTED") return "请求超时,请稍后重试";
  const status = err?.response?.status;
  return status ? `获取失败(HTTP ${status})` : "获取失败,请检查网络后重试";
}

async function load() {
  const current = ++seq;
  loading.value = true;
  image.value = "";
  links.value = null;
  invalid.value = {};
  error.value = "";
  try {
    const res = await getCodeInfo(
      target.env,
      target.namespace,
      target.deployment
    );
    if (current !== seq) return;
    image.value = res.data?.request?.image ?? "";
    links.value = res.data?.links ?? null;
    invalid.value = res.data?.invalid ?? {};
  } catch (err: any) {
    if (current !== seq) return;
    // 接口出错时后端也带上发出去的请求体,能看到用的是哪个镜像
    image.value = err?.response?.data?.data?.request?.image ?? "";
    error.value = errorMessage(err);
  } finally {
    if (current === seq) loading.value = false;
  }
}

function open(row: { env: string; namespace: string; deployment: string }) {
  target.env = row.env;
  target.namespace = row.namespace;
  target.deployment = row.deployment;
  visible.value = true;
  load();
}

function reset() {
  seq++;
  loading.value = false;
}

defineExpose({ open });
</script>

<style lang="scss" scoped>
.code-info {
  min-height: 160px;

  :deep(.el-descriptions__label) {
    width: 110px;
    white-space: nowrap;
  }
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
</style>
