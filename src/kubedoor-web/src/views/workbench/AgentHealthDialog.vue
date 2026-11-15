<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { agentInstallApi, type AgentHealth } from "@/api/agent-install";
import { installError } from "./install-utils";
import {
  formatCheckedAt,
  groupChecks,
  healthSummary,
  healthTagType,
  repairHint
} from "./health-utils";

/**
 * Agent 列表版本号菜单里的「修复」：先检查 Agent 各组件和功能，再按需「修复（覆盖安装）」。
 * 修复按钮随时能点（检查失败、Agent 离线时正是要修复的时候），点了交给父组件打开修复模式的安装对话框
 */
const visible = defineModel<boolean>({ default: false });
const props = defineProps<{ env: string }>();
const emit = defineEmits<{ repair: [env: string] }>();
const health = ref<AgentHealth>();
const loading = ref(false);
const error = ref("");
let generation = 0;

const summary = computed(() =>
  health.value ? healthSummary(health.value) : undefined
);
const groups = computed(() => groupChecks(health.value?.checks || []));
const hint = computed(() => repairHint(health.value?.kubeconfig));
const checkedAt = computed(() =>
  formatCheckedAt(health.value?.checked_at || "")
);

async function load() {
  const current = ++generation;
  const env = props.env;
  // 换了集群就不显示上一个集群的结果；同一个集群重新检查时保留旧结果，检查中盖一层加载
  if (health.value?.env !== env) health.value = undefined;
  loading.value = true;
  error.value = "";
  try {
    const result = await agentInstallApi.health(env);
    if (current !== generation) return;
    health.value = result;
  } catch (reason) {
    if (current !== generation) return;
    error.value = installError(reason);
  } finally {
    if (current === generation) loading.value = false;
  }
}

function repair() {
  const env = props.env;
  visible.value = false;
  emit("repair", env);
}

watch(
  [visible, () => props.env],
  ([open]) => {
    if (open) void load();
    else {
      generation++;
      loading.value = false;
    }
  },
  { immediate: true }
);
onBeforeUnmount(() => {
  generation++;
});
</script>

<template>
  <el-dialog
    v-model="visible"
    :title="`检查与修复 Agent：${env}`"
    width="min(760px, 96vw)"
    top="6vh"
    append-to-body
  >
    <!-- 检查项多时在弹框里滚动，底部的「修复（覆盖安装）」一直看得见 -->
    <div v-loading="loading" class="agent-health">
      <div class="health-scroll">
        <el-alert
          v-if="error"
          :title="`健康检查失败：${error}`"
          type="error"
          show-icon
          :closable="false"
          class="section-space"
        />
        <template v-if="health && summary">
          <el-alert
            :title="summary.title"
            :type="summary.type"
            show-icon
            :closable="false"
          />
          <div class="health-meta">
            <span
              >Agent 版本：{{ health.version || "未知" }}（推荐
              {{ health.recommended_version || "未知" }}）</span
            >
            <span>命名空间：{{ health.namespace || "未知" }}</span>
            <span v-if="checkedAt">检查时间：{{ checkedAt }}</span>
          </div>
          <section v-for="group in groups" :key="group.key">
            <h4 class="section-title">{{ group.label }}</h4>
            <div
              v-for="check in group.checks"
              :key="check.key"
              class="check-row"
            >
              <el-tag :type="healthTagType(check.status)" size="small">{{
                check.name
              }}</el-tag>
              <span>{{ check.message }}</span>
            </div>
          </section>
        </template>
        <p v-else-if="loading" class="muted">
          正在检查…Agent 在线时要经 Agent 查询集群，可能需要几十秒。
        </p>
      </div>
    </div>
    <template #footer>
      <div class="health-actions">
        <span class="repair-hint">{{ hint }}</span>
        <div class="health-buttons">
          <el-button @click="visible = false">关闭</el-button>
          <el-button :loading="loading" @click="load">重新检查</el-button>
          <el-button type="primary" @click="repair">修复（覆盖安装）</el-button>
        </div>
      </div>
    </template>
  </el-dialog>
</template>

<style scoped>
.agent-health {
  min-height: 160px;
}

/* 弹框距顶部 6vh，标题栏和底部按钮约 180px：其余高度给检查结果，超出在这里滚动 */
.health-scroll {
  max-height: calc(88vh - 180px);
  padding-right: 4px;
  overflow-y: auto;
}

.health-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 20px;
  margin-top: 12px;
  font-size: 13px;
  color: var(--el-text-color-secondary);
}

.section-space {
  margin-bottom: 12px;
}

.section-title {
  margin: 18px 0 6px;
}

.check-row {
  display: flex;
  gap: 12px;
  align-items: flex-start;
  padding: 6px 0;
  line-height: 1.6;
  overflow-wrap: anywhere;
}

/* 检查项名称一样宽（最长的 kube-state-metrics），说明对齐成一列 */
.check-row .el-tag {
  flex-shrink: 0;
  min-width: 124px;
}

.muted {
  margin: 12px 0;
  line-height: 1.8;
  color: var(--el-text-color-secondary);
}

/* 说明在左、按钮在右；窄屏时说明占满一行，按钮换到下一行靠右 */
.health-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 12px 16px;
  align-items: center;
  justify-content: flex-end;
}

.repair-hint {
  flex: 1 1 280px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--el-text-color-secondary);
  text-align: left;
}

.health-buttons {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  justify-content: flex-end;
}

.health-buttons .el-button + .el-button {
  margin-left: 0;
}
</style>
