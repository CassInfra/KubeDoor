<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from "vue";
import dayjs from "dayjs";
import { ElMessage, ElMessageBox } from "element-plus";
import { copyTextToClipboard } from "@pureadmin/utils";
import { emitter } from "@/utils/mitt";
import {
  AIRequestError,
  aiApi,
  type IMBindingState,
  type IMPlatform,
  type WeComBinding
} from "@/api/ai";

defineOptions({
  name: "WecomBindDialog"
});

/**
 * 右上角用户菜单「绑定 IM 账号」：为自己的花折账号生成 6 位绑定码，
 * 成员在企业微信或飞书/Lark 里把「绑定 123456」发给机器人后，这里轮询到新绑定就提示成功。
 */
const PLATFORM_LABELS: Record<IMPlatform, string> = {
  wecom: "企业微信",
  feishu: "飞书 / Lark"
};
const platformLabel = (platform: string) =>
  PLATFORM_LABELS[platform as IMPlatform] || platform;

const visible = ref(false);
const loading = ref(false);
const generating = ref(false);
const binding = ref<IMBindingState | null>(null);
const error = ref("");
const code = ref("");
const expiresAt = ref(0);
const now = ref(Date.now());
let timer: ReturnType<typeof setInterval> | undefined;
/** 每次获取绑定码或关掉弹窗都加一：还在路上的上一轮轮询回来时认得出自己已经过时 */
let generation = 0;

const command = computed(() => `绑定 ${code.value}`);
const remaining = computed(() =>
  Math.max(0, Math.floor((expiresAt.value - now.value) / 1000))
);
const countdown = computed(
  () =>
    `${Math.floor(remaining.value / 60)}:${String(remaining.value % 60).padStart(2, "0")}`
);
const supportedPlatformText = computed(() => {
  const platforms = binding.value?.platforms?.length
    ? binding.value.platforms
    : (["wecom", "feishu"] as IMPlatform[]);
  return platforms.map(platformLabel).join("或");
});

const isNotFound = (err: unknown) =>
  (err instanceof AIRequestError && err.status === 404) ||
  (typeof err === "object" &&
    err !== null &&
    "status" in err &&
    (err as { status?: number }).status === 404);

function normalizeLegacyBinding(legacy: WeComBinding): IMBindingState {
  const members = (legacy.members || []).map(item => ({
    bot_id: "legacy-wecom",
    bot_name: "企业微信机器人",
    platform: "wecom" as const,
    external_user_id: item.wecom_userid,
    self_name: item.self_name,
    bound_by: item.bound_by,
    bound_at: item.bound_at,
    last_seen_at: item.last_seen_at
  }));
  return {
    enabled: legacy.enabled,
    connected: legacy.connected,
    platforms: legacy.enabled || members.length ? ["wecom"] : [],
    bots: legacy.enabled
      ? [
          {
            id: "legacy-wecom",
            name: "企业微信机器人",
            platform: "wecom",
            enabled: legacy.enabled,
            connected: legacy.connected
          }
        ]
      : [],
    members,
    code_minutes: legacy.code_minutes
  };
}

/** 同一个 IM 成员重新绑定时 bound_at 会变，一起比较才认得出这次的绑定 */
const bindingKeys = (value: IMBindingState | null) =>
  new Set(
    (value?.members || []).map(
      item => `${item.bot_id}:${item.external_user_id}@${item.bound_at}`
    )
  );

function formatTime(value: string | null) {
  return value ? dayjs(value).format("YYYY-MM-DD HH:mm") : "—";
}

function stopTimer() {
  generation += 1;
  if (timer) clearInterval(timer);
  timer = undefined;
}

async function fetchBinding(): Promise<IMBindingState> {
  try {
    return await aiApi.imBinding();
  } catch (err) {
    if (!isNotFound(err)) throw err;
    return normalizeLegacyBinding(await aiApi.wecomBinding());
  }
}

async function requestBindCode() {
  try {
    return await aiApi.createIMBindCode();
  } catch (err) {
    if (!isNotFound(err)) throw err;
    return await aiApi.createWecomBindCode();
  }
}

async function load() {
  loading.value = true;
  error.value = "";
  try {
    binding.value = await fetchBinding();
  } catch (err) {
    binding.value = null;
    error.value =
      err instanceof Error ? err.message : "读取 IM 账号绑定失败，请稍后重试";
  } finally {
    loading.value = false;
  }
}

function open() {
  stopTimer();
  code.value = "";
  visible.value = true;
  void load();
}

async function generate() {
  generating.value = true;
  try {
    const result = await requestBindCode();
    const known = bindingKeys(binding.value);
    stopTimer();
    const current = generation;
    code.value = result.code;
    // 按浏览器自己的时钟倒计时，不拿服务器给的绝对时间比（两边时钟可能对不上）
    const minutes = result.minutes ?? binding.value?.code_minutes ?? 10;
    now.value = Date.now();
    expiresAt.value = now.value + minutes * 60_000;
    let ticks = 0;
    // 每秒刷新倒计时，每 4 秒看一次有没有绑上新的 IM 成员
    timer = setInterval(async () => {
      if (current !== generation) return;
      now.value = Date.now();
      if (remaining.value <= 0) {
        stopTimer();
        return;
      }
      if (++ticks % 4) return;
      try {
        const latest = await fetchBinding();
        if (current !== generation) return;
        binding.value = latest;
        if ([...bindingKeys(latest)].some(key => !known.has(key))) {
          stopTimer();
          code.value = "";
          ElMessage.success("IM 账号绑定成功。");
        }
      } catch {
        // 网络抖动时下一轮再看
      }
    }, 1000);
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : "获取绑定码失败");
  } finally {
    generating.value = false;
  }
}

function copy() {
  if (copyTextToClipboard(command.value)) {
    ElMessage.success("已复制，到企业微信或飞书/Lark 里发给机器人即可。");
  } else {
    ElMessage.warning("浏览器不允许复制，请手动输入。");
  }
}

async function unbind(botId: string, userid: string) {
  try {
    await ElMessageBox.confirm(
      "解除后，该 IM 成员不再按你的花折账号使用机器人。",
      "解除绑定",
      { type: "warning", confirmButtonText: "解除" }
    );
    if (botId === "legacy-wecom") {
      await aiApi.unbindWecom(userid);
    } else {
      try {
        await aiApi.unbindSelfIM(botId, userid);
      } catch (err) {
        if (!isNotFound(err)) throw err;
        await aiApi.unbindWecom(userid);
      }
    }
    await load();
    ElMessage.success("已解除绑定。");
  } catch (err) {
    if (err !== "cancel" && err !== "close") {
      ElMessage.error(err instanceof Error ? err.message : "解除绑定失败");
    }
  }
}

emitter.on("openWecomBind", open);
onBeforeUnmount(() => {
  emitter.off("openWecomBind", open);
  stopTimer();
});
</script>

<template>
  <el-dialog
    v-model="visible"
    title="绑定 IM 账号"
    width="540px"
    append-to-body
    @closed="stopTimer"
  >
    <div v-loading="loading" class="im-bind">
      <el-alert
        v-if="error"
        :title="error"
        type="error"
        :closable="false"
        show-icon
      />
      <template v-else-if="binding">
        <el-alert
          v-if="!binding.enabled"
          title="管理员还没有启用企业微信或飞书 AI 机器人。"
          type="info"
          :closable="false"
          show-icon
        />
        <template v-else>
          <p class="tip">
            把{{ supportedPlatformText }}里的花折 AI
            机器人绑定到你的花折账号，之后在 IM
            里按你的权限查询和操作集群（修改照常需要你审批）。
          </p>
          <el-alert
            v-if="!binding.connected"
            title="当前没有已连上的 IM 机器人，发出绑定码后可能暂时收不到回复。"
            type="warning"
            :closable="false"
            show-icon
            class="mb-3"
          />
          <div v-if="code" class="code-box">
            <div class="code">{{ code }}</div>
            <div class="hint">
              在企业微信或飞书/Lark 里给机器人发送：<b>{{ command }}</b>
              <el-button link type="primary" @click="copy">复制</el-button>
            </div>
            <div v-if="remaining > 0" class="hint">
              {{ countdown }} 后过期，绑定成功后这里会自动提示。
            </div>
            <div v-else class="hint expired">绑定码已过期，请重新获取。</div>
          </div>
          <el-button type="primary" :loading="generating" @click="generate">{{
            code ? "重新获取绑定码" : "获取绑定码"
          }}</el-button>
        </template>
        <el-divider content-position="left">已绑定的 IM 成员</el-divider>
        <el-empty
          v-if="!binding.members.length"
          description="还没有绑定"
          :image-size="60"
        />
        <div
          v-for="item in binding.members"
          :key="`${item.bot_id}:${item.external_user_id}`"
          class="member"
        >
          <div class="member-info">
            <div class="member-title">
              <el-tag
                size="small"
                :type="item.platform === 'feishu' ? 'primary' : 'success'"
                >{{ platformLabel(item.platform) }}</el-tag
              >
              <span>{{
                item.self_name ||
                (item.platform === "feishu" ? "飞书成员" : "企业微信成员")
              }}</span>
              <span v-if="item.bot_name" class="bot-badge">{{
                item.bot_name
              }}</span>
            </div>
            <div class="sub" :title="item.external_user_id">
              {{ item.external_user_id }}
            </div>
            <div class="sub">
              绑定：{{ formatTime(item.bound_at) }} · 最后使用：{{
                formatTime(item.last_seen_at)
              }}
            </div>
          </div>
          <el-button
            link
            type="danger"
            @click="unbind(item.bot_id, item.external_user_id)"
            >解除</el-button
          >
        </div>
      </template>
    </div>
  </el-dialog>
</template>

<style scoped>
.im-bind {
  min-height: 120px;
}

.tip {
  margin: 0 0 12px;
  line-height: 1.6;
  color: var(--el-text-color-regular);
}

.code-box {
  padding: 14px 16px;
  margin-bottom: 12px;
  text-align: center;
  background: var(--el-fill-color-light);
  border-radius: 6px;
}

.code {
  font-family: Consolas, Menlo, monospace;
  font-size: 30px;
  font-weight: 600;
  letter-spacing: 8px;
}

.hint {
  margin-top: 6px;
  font-size: 13px;
  color: var(--el-text-color-secondary);
}

.hint.expired {
  color: var(--el-color-danger);
}

.member {
  display: flex;
  gap: 12px;
  align-items: center;
  justify-content: space-between;
  padding: 8px 0;
  border-bottom: 1px solid var(--el-border-color-lighter);
}

.member-info {
  min-width: 0;
}

.member-title {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
}

.bot-badge {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.sub {
  overflow: hidden;
  font-size: 12px;
  color: var(--el-text-color-secondary);
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
