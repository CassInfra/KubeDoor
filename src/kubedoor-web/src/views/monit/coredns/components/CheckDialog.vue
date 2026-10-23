<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { CircleCheckFilled, Loading } from "@element-plus/icons-vue";
import { isOldAgent, oldAgentTip } from "@/api/coredns";
import {
  resolveFromAgent,
  type ResolveAnswer,
  type ResolveResult
} from "@/api/netProbe";
import { dnsTarget, sameIP, type CheckTarget } from "../corefile";

defineOptions({ name: "CoreDnsCheckDialog" });

/**
 * 检查某条记录是否生效:在 kubedoor-agent 所在 Pod 里解析域名(和进业务 Pod 执行 ping 一样),
 * 每秒一次,直到解析结果符合预期,或者关掉弹框
 */
const props = defineProps<{
  modelValue: boolean;
  env: string;
  hasReload: boolean;
  target: CheckTarget | null;
}>();
const emit = defineEmits<{ (e: "update:modelValue", value: boolean): void }>();

const visible = computed({
  get: () => props.modelValue,
  set: value => emit("update:modelValue", value)
});

const INTERVAL = 1000;
const running = ref(false);
const effective = ref(false);
const rounds = ref(0);
const elapsed = ref(0);
const result = ref<ResolveResult | null>(null);
const error = ref("");

let generation = 0;
let pollTimer: ReturnType<typeof setTimeout> | undefined;
let clockTimer: ReturnType<typeof setInterval> | undefined;
let startedAt = 0;

const expected = computed(() =>
  props.target ? dnsTarget(props.target.ip) : null
);
const names = computed(() => props.target?.names ?? []);

/** 启用的记录:解析结果里有期望的 IP;停用的记录:不再解析到它(域名不存在也算) */
function answerOk(answer?: ResolveAnswer): boolean {
  if (!answer || !expected.value || !props.target) return false;
  const has = answer.ips.some(ip => sameIP(ip, expected.value!.ip));
  if (props.target.enabled) return has;
  return !has && ["ok", "not_found", "no_data"].includes(answer.code);
}

const allOk = () =>
  !!result.value &&
  names.value.every(name => answerOk(result.value!.results[name]));

function stop() {
  generation++;
  running.value = false;
  clearTimeout(pollTimer);
  clearInterval(clockTimer);
  pollTimer = clockTimer = undefined;
}

async function poll(gen: number) {
  const started = Date.now();
  try {
    const res = await resolveFromAgent(
      props.env,
      names.value,
      expected.value!.type === "AAAA" ? "6" : "4",
      true
    );
    if (gen !== generation) return;
    if (isOldAgent(res)) {
      error.value = oldAgentTip("从 agent 检查解析");
      return stop();
    }
    if (!res.success || !res.data) {
      error.value = res.error || res.message || "检查失败";
      if (/格式不正确|缺少|最多|family/.test(error.value)) return stop();
    } else {
      error.value = "";
      result.value = res.data;
      rounds.value++;
      effective.value = allOk();
      if (effective.value) return stop();
    }
  } catch (e: any) {
    if (gen !== generation) return;
    // master 暂时连不上 agent 之类:提示后接着查
    error.value = e?.response?.data?.error || e?.message || "请求失败";
  }
  if (gen !== generation) return;
  pollTimer = setTimeout(
    () => poll(gen),
    Math.max(0, INTERVAL - (Date.now() - started))
  );
}

function start() {
  stop();
  if (!props.target || !expected.value) return;
  const gen = generation;
  running.value = true;
  effective.value = false;
  error.value = "";
  result.value = null;
  rounds.value = 0;
  elapsed.value = 0;
  startedAt = Date.now();
  clockTimer = setInterval(() => {
    elapsed.value = Math.round((Date.now() - startedAt) / 1000);
  }, 1000);
  poll(gen);
}

watch(
  () => [props.modelValue, props.target] as const,
  ([open]) => (open && props.target ? start() : stop()),
  { immediate: true }
);
onBeforeUnmount(stop);

const source = computed(() => result.value?.source);

function ipTagType(ip: string): "success" | "danger" | "info" {
  if (!expected.value || !sameIP(ip, expected.value.ip)) return "info";
  return props.target!.enabled ? "success" : "danger";
}
</script>

<template>
  <el-dialog
    v-model="visible"
    :title="target ? `检查解析是否生效(第 ${target.line} 行)` : '检查解析'"
    width="min(820px, 94vw)"
    top="10vh"
    append-to-body
    destroy-on-close
    class="coredns-check-dialog"
  >
    <template v-if="target">
      <div class="check-head">
        <div class="check-head__row">
          <span class="check-head__label">域名</span>
          <el-tag
            v-for="name in target.names"
            :key="name"
            effect="plain"
            class="mono"
            >{{ name }}</el-tag
          >
        </div>
        <div class="check-head__row">
          <span class="check-head__label">期望</span>
          <span v-if="target.enabled">
            解析到 <b class="mono">{{ target.ip }}</b>
          </span>
          <span v-else>
            不再解析到 <b class="mono">{{ target.ip }}</b
            >(这条记录已停用)
          </span>
        </div>
      </div>

      <div
        class="check-status"
        :class="{ 'is-ok': effective, 'is-stopped': !running && !effective }"
      >
        <template v-if="effective">
          <el-icon class="check-status__icon"><CircleCheckFilled /></el-icon>
          <span>
            已生效
            <span class="muted"
              >(检查 {{ rounds }} 次,用时 {{ elapsed }} 秒)</span
            >
          </span>
        </template>
        <template v-else-if="running">
          <el-icon class="check-status__icon is-loading"><Loading /></el-icon>
          <span>
            还没生效,每秒检查一次 · 已检查 {{ rounds }} 次 · 已等待
            {{ elapsed }} 秒
          </span>
        </template>
        <template v-else>
          <span>检查已停止</span>
        </template>
      </div>

      <el-alert
        v-if="error"
        type="error"
        :closable="false"
        show-icon
        class="check-alert"
        :title="error"
      />
      <el-alert
        v-if="target.dirty"
        type="info"
        :closable="false"
        show-icon
        class="check-alert"
        title="这条记录有未保存的修改,这里检查的是已经保存到集群的内容"
      />
      <el-alert
        v-if="!hasReload"
        type="warning"
        :closable="false"
        show-icon
        class="check-alert"
        title="Corefile 没有启用 reload 插件:修改不会自动生效,需要重启 CoreDNS"
      />

      <el-table v-if="result" :data="names" border size="small">
        <el-table-column label="域名" min-width="220">
          <template #default="{ row }">
            <span class="mono">{{ row }}</span>
          </template>
        </el-table-column>
        <el-table-column label="当前解析结果" min-width="260">
          <template #default="{ row }">
            <template v-if="result.results[row]">
              <el-tag
                v-for="ip in result.results[row].ips"
                :key="ip"
                size="small"
                :type="ipTagType(ip)"
                class="mono check-ip"
                disable-transitions
                >{{ ip }}</el-tag
              >
              <span v-if="result.results[row].error" class="muted">
                {{ result.results[row].error }}
              </span>
              <span class="muted"> · {{ result.results[row].ms }}ms</span>
            </template>
          </template>
        </el-table-column>
        <el-table-column label="结果" width="90" align="center">
          <template #default="{ row }">
            <el-tag
              size="small"
              :type="answerOk(result.results[row]) ? 'success' : 'danger'"
              disable-transitions
            >
              {{ answerOk(result.results[row]) ? "已生效" : "未生效" }}
            </el-tag>
          </template>
        </el-table-column>
      </el-table>
      <div v-else-if="running" class="check-placeholder">正在解析…</div>

      <div class="check-tip">
        <div v-if="source">
          从 kubedoor-agent 所在 Pod
          <span class="mono">{{ source.pod }}</span>
          <template v-if="source.ip">({{ source.ip }})</template>
          解析,和进业务 Pod 执行 ping 一样走它的 /etc/resolv.conf(DNS
          {{ source.nameservers.join("、") || "-" }})。
        </div>
        <div>
          CoreDNS
          有多个副本时会陆续加载新配置,刚生效的一小段时间里偶尔还可能解析到旧结果。
        </div>
      </div>
    </template>
  </el-dialog>
</template>

<style scoped>
.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}

.muted {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.check-head {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-bottom: 12px;
}

.check-head__row {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
}

.check-head__label {
  width: 40px;
  color: var(--el-text-color-secondary);
}

.check-status {
  display: flex;
  gap: 8px;
  align-items: center;
  padding: 10px 12px;
  margin-bottom: 12px;
  background: var(--el-color-primary-light-9);
  border-radius: 6px;
}

.check-status.is-ok {
  background: var(--el-color-success-light-9);
}

.check-status.is-stopped {
  background: var(--el-fill-color-light);
}

.check-status__icon {
  font-size: 18px;
  color: var(--el-color-primary);
}

.is-ok .check-status__icon {
  color: var(--el-color-success);
}

.check-alert {
  margin-bottom: 12px;
}

.check-ip {
  margin: 2px 4px 2px 0;
}

.check-placeholder {
  padding: 24px;
  color: var(--el-text-color-secondary);
  text-align: center;
}

.check-tip {
  margin-top: 12px;
  font-size: 12px;
  line-height: 1.7;
  color: var(--el-text-color-secondary);
}
</style>
