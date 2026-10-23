<script setup lang="ts">
import { computed, nextTick, ref, watch } from "vue";
import { Delete } from "@element-plus/icons-vue";
import { isOldAgent, oldAgentTip } from "@/api/coredns";
import {
  resolveFromAgent,
  tcpCheckFromAgent,
  type ProbeSource,
  type ResolveAnswer,
  type TcpResult
} from "@/api/netProbe";

defineOptions({ name: "CoreDnsNetToolsDialog" });

/**
 * 解析测试 / 端口测试:都从 kubedoor-agent 所在 Pod 发起。
 * 解析和进 Pod 执行 ping 一样走系统解析器;端口测试相当于 nc -zv 主机 端口
 */
const props = defineProps<{
  modelValue: boolean;
  env: string;
  mode: "dns" | "tcp";
  /** 端口测试会从集群内向任意地址发起连接,需要集群读写权限 */
  canTcp: boolean;
}>();
const emit = defineEmits<{ (e: "update:modelValue", value: boolean): void }>();

const visible = computed({
  get: () => props.modelValue,
  set: value => emit("update:modelValue", value)
});

type Family = "any" | "4" | "6";
interface Entry {
  id: number;
  at: string;
  env: string;
  source?: ProbeSource;
  error?: string;
  dns?: { name: string; family: Family; answer: ResolveAnswer };
  tcp?: TcpResult;
}

const tab = ref<"dns" | "tcp">("dns");
const name = ref("");
const family = ref<Family>("any");
const host = ref("");
const port = ref("");
const loading = ref(false);
const history = ref<Entry[]>([]);
const panel = ref<HTMLElement>();
let seq = 0;

const HOST = /^[A-Za-z0-9_.:-]+$/;
const nameError = computed(() => {
  const value = name.value.trim();
  return value && !HOST.test(value) ? "只能填域名或 IP" : "";
});
const hostError = computed(() => {
  const value = host.value.trim();
  return value && !HOST.test(value) ? "只能填域名或 IP" : "";
});
const portError = computed(() => {
  const value = port.value.trim();
  if (!value) return "";
  const n = Number(value);
  return Number.isInteger(n) && n > 0 && n < 65536
    ? ""
    : "端口要在 1 ~ 65535 之间";
});

watch(
  () => props.modelValue,
  open => {
    if (open) tab.value = props.mode;
  }
);

/** 打开(动画结束后 el-dialog 会把焦点移到自己身上,所以在 opened 里聚焦)和切换页签时,光标放到第一个输入框 */
async function focusInput() {
  await nextTick();
  panel.value
    ?.querySelector<HTMLInputElement>(`[data-tab="${tab.value}"] input`)
    ?.focus();
}
watch(tab, focusInput);

const now = () => new Date().toLocaleTimeString("zh-CN", { hour12: false });

function push(entry: Omit<Entry, "id" | "at" | "env">) {
  history.value.unshift({ id: ++seq, at: now(), env: props.env, ...entry });
  history.value.splice(20);
}

function requestError(e: any) {
  return e?.response?.data?.error || e?.message || "请求失败";
}

async function runDns() {
  const value = name.value.trim();
  if (!value || nameError.value || loading.value) return;
  loading.value = true;
  try {
    const res = await resolveFromAgent(props.env, [value], family.value);
    if (isOldAgent(res)) push({ error: oldAgentTip("网络测试") });
    else if (!res.success || !res.data)
      push({ error: res.error || res.message || "解析失败" });
    else
      push({
        source: res.data.source,
        dns: {
          name: value,
          family: family.value,
          answer: res.data.results[value]
        }
      });
  } catch (e) {
    push({ error: requestError(e) });
  } finally {
    loading.value = false;
  }
}

async function runTcp() {
  const target = host.value.trim();
  const value = Number(port.value.trim());
  if (
    !props.canTcp ||
    !target ||
    hostError.value ||
    !port.value.trim() ||
    portError.value ||
    loading.value
  )
    return;
  loading.value = true;
  try {
    const res = await tcpCheckFromAgent(props.env, target, value);
    if (isOldAgent(res)) push({ error: oldAgentTip("网络测试") });
    else if (!res.success || !res.data)
      push({ error: res.error || res.message || "测试失败" });
    else push({ source: res.data.source, tcp: res.data });
  } catch (e) {
    push({ error: requestError(e) });
  } finally {
    loading.value = false;
  }
}

const familyText = { any: "", "4": "(IPv4)", "6": "(IPv6)" };
const hostPort = (ip: string, value: number) =>
  ip.includes(":") ? `[${ip}]:${value}` : `${ip}:${value}`;
</script>

<template>
  <el-dialog
    v-model="visible"
    title="网络测试(从 kubedoor-agent 发起)"
    width="min(860px, 94vw)"
    top="8vh"
    append-to-body
    class="coredns-net-dialog"
    @opened="focusInput"
  >
    <div ref="panel">
      <el-tabs v-model="tab">
        <el-tab-pane label="解析测试" name="dns">
          <div class="net-form" data-tab="dns">
            <el-input
              v-model="name"
              placeholder="要解析的域名,例如 beta-order2-es.casstime.cn"
              clearable
              class="mono"
              style="width: 400px"
              @keyup.enter="runDns"
            />
            <el-select v-model="family" style="width: 110px">
              <el-option label="全部" value="any" />
              <el-option label="IPv4" value="4" />
              <el-option label="IPv6" value="6" />
            </el-select>
            <el-button
              type="primary"
              :loading="loading"
              :disabled="!name.trim() || !!nameError"
              @click="runDns"
            >
              解析
            </el-button>
          </div>
          <div class="net-hint">
            <span v-if="nameError" class="is-error">{{ nameError }}</span>
            <span v-else>
              在 kubedoor-agent 所在 Pod 里解析,和进 Pod 执行 ping 一样走
              /etc/resolv.conf (会补 search 域,短名字如 kubedoor-master.kubedoor
              也能解析)
            </span>
          </div>
        </el-tab-pane>
        <el-tab-pane label="端口测试" name="tcp">
          <el-alert
            v-if="!canTcp"
            type="info"
            :closable="false"
            show-icon
            class="net-alert"
            title="端口测试会从集群内向任意地址发起连接,需要这个集群的读写权限"
          />
          <div class="net-form" data-tab="tcp">
            <el-input
              v-model="host"
              placeholder="主机:域名或 IP"
              clearable
              class="mono"
              style="width: 340px"
              :disabled="!canTcp"
              @keyup.enter="runTcp"
            />
            <span class="net-colon">:</span>
            <el-input
              v-model="port"
              placeholder="端口"
              class="mono"
              style="width: 110px"
              :disabled="!canTcp"
              @keyup.enter="runTcp"
            />
            <el-button
              type="primary"
              :loading="loading"
              :disabled="
                !canTcp ||
                !host.trim() ||
                !port.trim() ||
                !!hostError ||
                !!portError
              "
              @click="runTcp"
            >
              测试
            </el-button>
          </div>
          <div class="net-hint">
            <span v-if="hostError || portError" class="is-error">{{
              hostError || portError
            }}</span>
            <span v-else>
              相当于在 kubedoor-agent 所在 Pod 里执行 nc -zv 主机
              端口:域名会先解析,每个地址各连一次(TCP)
            </span>
          </div>
        </el-tab-pane>
      </el-tabs>

      <div v-if="history.length" class="net-history">
        <div class="net-history__head">
          <span>测试记录</span>
          <el-button link size="small" :icon="Delete" @click="history = []"
            >清空</el-button
          >
        </div>
        <div
          v-for="item in history"
          :key="item.id"
          class="net-entry"
          :class="{
            'is-ok': item.dns ? item.dns.answer.ips.length : item.tcp?.ok,
            'is-fail':
              item.error ||
              (item.dns
                ? !item.dns.answer.ips.length
                : item.tcp && !item.tcp.ok)
          }"
        >
          <div class="net-entry__title">
            <template v-if="item.dns">
              <span class="net-entry__icon">{{
                item.dns.answer.ips.length ? "✓" : "✗"
              }}</span>
              <span>解析</span>
              <b class="mono">{{ item.dns.name }}</b
              >{{ familyText[item.dns.family] }}
              <span v-if="item.dns.answer.ips.length">
                →
                <el-tag
                  v-for="ip in item.dns.answer.ips"
                  :key="ip"
                  size="small"
                  type="success"
                  class="mono net-ip"
                  disable-transitions
                  >{{ ip }}</el-tag
                >
              </span>
              <span v-else class="net-entry__error">{{
                item.dns.answer.error
              }}</span>
              <span class="muted">{{ item.dns.answer.ms }}ms</span>
            </template>
            <template v-else-if="item.tcp">
              <span class="net-entry__icon">{{ item.tcp.ok ? "✓" : "✗" }}</span>
              <span>端口</span>
              <b class="mono">{{ hostPort(item.tcp.host, item.tcp.port) }}</b>
              <span :class="item.tcp.ok ? 'net-entry__ok' : 'net-entry__error'">
                {{ item.tcp.ok ? "可以连通" : "连不通" }}
              </span>
            </template>
            <template v-else>
              <span class="net-entry__icon">✗</span>
              <span class="net-entry__error">{{ item.error }}</span>
            </template>
            <span class="net-entry__time muted"
              >{{ item.env }} · {{ item.at }}</span
            >
          </div>

          <div v-if="item.tcp" class="net-entry__body mono">
            <div
              v-if="item.tcp.dns && !item.tcp.dns.ips.length"
              class="net-entry__error"
            >
              域名解析失败:{{ item.tcp.dns.error }}
            </div>
            <div v-for="attempt in item.tcp.attempts" :key="attempt.ip">
              <span :class="attempt.ok ? 'net-entry__ok' : 'net-entry__error'">
                {{ attempt.ok ? "✓" : "✗" }}
                {{ hostPort(attempt.ip, item.tcp.port) }}
                {{ attempt.ok ? "连接成功" : attempt.error }}
              </span>
              <span class="muted net-entry__meta">
                {{ attempt.ms }}ms<template v-if="attempt.local">
                  · 源地址 {{ attempt.local }}</template
                >
              </span>
            </div>
          </div>
          <div v-if="item.source" class="net-entry__source muted">
            源 Pod {{ item.source.pod
            }}<template v-if="item.source.ip">({{ item.source.ip }})</template>
            <template v-if="item.dns && item.source.nameservers.length">
              · DNS {{ item.source.nameservers.join("、") }}
            </template>
          </div>
        </div>
      </div>
    </div>
  </el-dialog>
</template>

<style scoped>
.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}

.mono :deep(.el-input__inner) {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}

.muted {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.net-form {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}

.net-colon {
  font-weight: 600;
  color: var(--el-text-color-secondary);
}

.net-hint {
  margin-top: 6px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.net-hint .is-error {
  color: var(--el-color-danger);
}

.net-alert {
  margin-bottom: 10px;
}

.net-history {
  max-height: 46vh;
  margin-top: 14px;
  overflow: auto;
}

.net-history__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 6px;
  font-size: 13px;
  color: var(--el-text-color-secondary);
}

.net-entry {
  padding: 8px 10px;
  margin-bottom: 8px;
  background: var(--el-fill-color-lighter);
  border-left: 3px solid var(--el-border-color);
  border-radius: 4px;
}

.net-entry.is-ok {
  border-left-color: var(--el-color-success);
}

.net-entry.is-fail {
  border-left-color: var(--el-color-danger);
}

.net-entry__title {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
}

.net-entry__icon {
  font-weight: 700;
}

.is-ok .net-entry__icon,
.net-entry__ok {
  color: var(--el-color-success);
}

.is-fail .net-entry__icon,
.net-entry__error {
  color: var(--el-color-danger);
}

.net-entry__time {
  margin-left: auto;
}

.net-entry__body {
  margin-top: 6px;
  font-size: 12px;
  line-height: 1.8;
}

.net-entry__source {
  margin-top: 4px;
}

.net-entry__meta {
  margin-left: 8px;
}

.net-ip {
  margin-right: 4px;
}
</style>
