<template>
  <div class="system-audit">
    <div class="toolbar">
      <el-date-picker
        v-model="range"
        type="datetimerange"
        range-separator="至"
        start-placeholder="开始时间"
        end-placeholder="结束时间"
        value-format="YYYY-MM-DDTHH:mm:ss"
        style="width: 360px"
      />
      <el-input
        v-model="query.username"
        placeholder="用户"
        clearable
        style="width: 140px"
      />
      <el-select
        v-model="query.action"
        placeholder="操作类型"
        clearable
        filterable
        style="width: 170px"
      >
        <el-option
          v-for="action in actions"
          :key="action"
          :label="actionLabel(action)"
          :value="action"
        />
      </el-select>
      <el-input
        v-model="query.env"
        placeholder="集群"
        clearable
        style="width: 140px"
      />
      <el-input
        v-model="query.keyword"
        placeholder="对象 / 详情关键字"
        clearable
        style="width: 180px"
      />
      <el-select
        v-model="query.success"
        placeholder="结果"
        clearable
        style="width: 100px"
      >
        <el-option label="成功" value="1" />
        <el-option label="失败" value="0" />
      </el-select>
      <el-button type="primary" @click="search">查询</el-button>
    </div>

    <el-card v-loading="loading">
      <el-table :data="rows" stripe style="width: 100%">
        <el-table-column type="expand">
          <template #default="{ row }">
            <pre class="detail">{{ JSON.stringify(row.detail, null, 2) }}</pre>
          </template>
        </el-table-column>
        <el-table-column label="时间" width="170">
          <template #default="{ row }">{{ formatTime(row.ts) }}</template>
        </el-table-column>
        <el-table-column prop="username" label="用户" width="140" />
        <el-table-column label="操作" width="150">
          <template #default="{ row }">{{ actionLabel(row.action) }}</template>
        </el-table-column>
        <el-table-column
          prop="env"
          label="集群"
          width="140"
          show-overflow-tooltip
        />
        <el-table-column
          prop="target"
          label="对象"
          min-width="260"
          show-overflow-tooltip
        />
        <el-table-column prop="ip" label="来源 IP" width="140" />
        <el-table-column label="结果" width="80" align="center">
          <template #default="{ row }">
            <el-tag size="small" :type="row.success ? 'success' : 'danger'">
              {{ row.success ? "成功" : "失败" }}
            </el-tag>
          </template>
        </el-table-column>
      </el-table>
      <div class="pager">
        <el-pagination
          v-model:current-page="query.page"
          v-model:page-size="query.pageSize"
          :total="total"
          :page-sizes="[20, 50, 100, 200]"
          layout="total, sizes, prev, pager, next"
          @current-change="load"
          @size-change="search"
        />
      </div>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from "vue";
import dayjs from "dayjs";
import { type AuditRecord, getAuditActions, getAuditLogs } from "@/api/system";

defineOptions({ name: "SystemAudit" });

const ACTION_LABELS: Record<string, string> = {
  login: "登录",
  logout: "退出登录",
  "password.change": "修改口令",
  "user.create": "新建用户",
  "user.update": "修改用户",
  "user.delete": "删除用户",
  "user.reset_password": "重置口令",
  "user.logout": "强制下线",
  "user.clusters": "修改集群授权",
  "user.auto_create": "LDAP 自动开通",
  "ldap.update": "修改 LDAP 设置",
  "api.write": "写操作",
  "api.denied": "越权被拒",
  "ai.agent_execute": "AI 执行修改"
};

const loading = ref(false);
const rows = ref<AuditRecord[]>([]);
const total = ref(0);
const actions = ref<string[]>([]);
const range = ref<[string, string] | null>(null);
const query = reactive({
  username: "",
  action: "",
  env: "",
  keyword: "",
  success: "",
  page: 1,
  pageSize: 20
});

function actionLabel(action: string) {
  return ACTION_LABELS[action] ?? action;
}

function formatTime(value: string) {
  return value ? dayjs(value).format("YYYY-MM-DD HH:mm:ss") : "";
}

async function load() {
  loading.value = true;
  try {
    const params: Record<string, any> = { ...query };
    if (range.value) {
      params.start = range.value[0];
      params.end = range.value[1];
    }
    const res = await getAuditLogs(params);
    rows.value = res.data ?? [];
    total.value = res.total ?? 0;
  } finally {
    loading.value = false;
  }
}

function search() {
  query.page = 1;
  load();
}

onMounted(async () => {
  load();
  try {
    actions.value = (await getAuditActions()).data ?? [];
  } catch {
    actions.value = Object.keys(ACTION_LABELS);
  }
  for (const action of Object.keys(ACTION_LABELS)) {
    if (!actions.value.includes(action)) actions.value.push(action);
  }
});
</script>

<style lang="scss" scoped>
.system-audit {
  padding: 4px;
}

.toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  margin-bottom: 8px;
}

.pager {
  display: flex;
  justify-content: flex-end;
  margin-top: 12px;
}

.detail {
  padding: 8px 16px;
  margin: 0;
  font-size: 12px;
  white-space: pre-wrap;
}
</style>
