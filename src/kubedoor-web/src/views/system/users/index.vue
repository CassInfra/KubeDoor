<template>
  <div class="system-users">
    <div class="toolbar">
      <div class="filters">
        <el-input
          v-model="filters.keyword"
          placeholder="用户名 / 显示名 / 备注"
          clearable
          style="width: 220px"
        >
          <template #prefix>
            <el-icon><Search /></el-icon>
          </template>
        </el-input>
        <el-select
          v-model="filters.source"
          placeholder="账号来源"
          clearable
          style="width: 120px"
        >
          <el-option label="本地" value="local" />
          <el-option label="LDAP" value="ldap" />
        </el-select>
        <el-select
          v-model="filters.role"
          placeholder="全局权限"
          clearable
          style="width: 130px"
        >
          <el-option
            v-for="item in ROLE_OPTIONS"
            :key="item.value"
            :label="item.label"
            :value="item.value"
          />
        </el-select>
      </div>
      <div class="actions">
        <el-button plain type="primary" @click="load">
          <el-icon style="margin-right: 4px"><RefreshRight /></el-icon>
          刷新
        </el-button>
        <el-button type="primary" @click="openCreate('local')">
          <el-icon style="margin-right: 4px"><Plus /></el-icon>
          新建本地账号
        </el-button>
        <el-button @click="openCreate('ldap')">添加 LDAP 账号</el-button>
      </div>
    </div>

    <el-alert type="info" :closable="false" class="mb-2">
      <template #title>
        集群权限
        =「单独授权」优先,没有单独授权的集群按「全局权限」兜底;系统管理员在所有集群都是「管理员」,不能再单独授权。
        集群权限分管理员 / 读写 / 只读 / 无,各自能做哪些操作在「<router-link
          to="/system/permissions"
          class="link"
          >权限与审批</router-link
        >」里配置(CoreDNS、Istio 默认只有管理员能改)。 LDAP
        用户首次登录会自动开通(默认全局权限在「LDAP
        设置」里配置),也可以先在这里添加再授权。
      </template>
    </el-alert>

    <el-card v-loading="loading">
      <el-table :data="filteredUsers" style="width: 100%" stripe>
        <el-table-column label="用户名" min-width="190">
          <template #default="{ row }">
            <div class="name-cell">
              <span class="username">{{ row.username }}</span>
              <el-tag v-if="row.super_admin" size="small" type="danger">
                超级管理员
              </el-tag>
              <el-tag v-else-if="row.builtin" size="small" type="primary">
                内置
              </el-tag>
              <el-tag
                v-if="row.password_default"
                size="small"
                type="warning"
                effect="dark"
              >
                出厂口令
              </el-tag>
              <el-tag
                v-else-if="row.must_change_password"
                size="small"
                type="warning"
              >
                待改密
              </el-tag>
            </div>
            <div v-if="row.display_name || row.comment" class="sub-text">
              {{ [row.display_name, row.comment].filter(Boolean).join(" · ") }}
            </div>
          </template>
        </el-table-column>
        <el-table-column label="来源" width="80" align="center">
          <template #default="{ row }">
            <el-tag size="small" effect="plain">
              {{ row.source === "ldap" ? "LDAP" : "本地" }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="全局权限" width="110" align="center">
          <template #default="{ row }">
            <el-tag size="small" :type="roleTag(row.role)">
              {{ ROLE_LABELS[row.role] || row.role }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="单独授权的集群" min-width="260">
          <template #default="{ row }">
            <span
              v-if="row.role === 'admin' || row.role === 'agent'"
              class="sub-text"
            >
              {{ row.role === "admin" ? "全部集群管理员" : "不访问集群数据" }}
            </span>
            <template v-else-if="grantList(row).length">
              <el-tag
                v-for="grant in grantList(row).slice(0, 4)"
                :key="grant.env"
                size="small"
                :type="PERM_TAG[grant.permission]"
                class="grant-tag"
              >
                {{ grant.env }}:{{ PERM_LABELS[grant.permission] }}
              </el-tag>
              <el-tag
                v-if="grantList(row).length > 4"
                size="small"
                class="grant-tag"
              >
                +{{ grantList(row).length - 4 }}
              </el-tag>
            </template>
            <span v-else class="sub-text">无(全部按全局权限)</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="80" align="center">
          <template #default="{ row }">
            <el-switch
              :model-value="row.enabled"
              :disabled="row.super_admin || row.id === selfId"
              @change="toggleEnabled(row)"
            />
          </template>
        </el-table-column>
        <el-table-column label="最近登录" min-width="170">
          <template #default="{ row }">
            <template v-if="row.last_login_at">
              <div>{{ formatTime(row.last_login_at) }}</div>
              <div class="sub-text">
                {{ row.last_login_ip }}
                <span v-if="row.session_count">
                  · 在线会话 {{ row.session_count }}</span
                >
              </div>
            </template>
            <span v-else class="sub-text">从未登录</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="300" fixed="right">
          <template #default="{ row }">
            <el-tooltip
              v-if="row.super_admin"
              content="内置超级管理员只能由它自己登录后修改口令,其它操作一律不允许"
              placement="top"
            >
              <span class="sub-text">受保护</span>
            </el-tooltip>
            <template v-else>
              <el-button
                link
                type="primary"
                :disabled="row.role === 'admin' || row.role === 'agent'"
                @click="openClusters(row)"
              >
                集群权限
              </el-button>
              <el-button link type="primary" @click="openEdit(row)">
                编辑
              </el-button>
              <el-button
                v-if="row.source === 'local'"
                link
                type="warning"
                :disabled="row.id === selfId"
                @click="openReset(row)"
              >
                重置口令
              </el-button>
              <el-button
                link
                type="warning"
                :disabled="!row.session_count"
                @click="kick(row)"
              >
                下线
              </el-button>
              <el-button
                link
                type="danger"
                :disabled="row.builtin || row.id === selfId"
                @click="remove(row)"
              >
                删除
              </el-button>
            </template>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 新建 / 编辑 -->
    <el-dialog
      v-model="editVisible"
      :title="editForm.id ? `编辑 ${editForm.username}` : createTitle"
      width="520px"
      :close-on-click-modal="false"
    >
      <el-form
        ref="editRef"
        :model="editForm"
        :rules="editRules"
        label-width="96px"
      >
        <el-form-item label="用户名" prop="username">
          <el-input
            v-model="editForm.username"
            :disabled="!!editForm.id"
            :placeholder="
              editForm.source === 'ldap'
                ? '与 LDAP 里的登录名一致'
                : '字母、数字、. _ @ -'
            "
          />
        </el-form-item>
        <el-form-item label="全局权限" prop="role">
          <el-select
            v-model="editForm.role"
            style="width: 100%"
            :disabled="editForm.builtin || editForm.id === selfId"
          >
            <el-option
              v-for="item in roleOptions"
              :key="item.value"
              :label="item.label"
              :value="item.value"
            />
          </el-select>
          <div class="field-hint">{{ roleDesc(editForm.role) }}</div>
        </el-form-item>
        <template v-if="!editForm.id && editForm.source === 'local'">
          <el-form-item label="初始口令" prop="password">
            <el-input
              v-model="editForm.password"
              type="password"
              show-password
              autocomplete="new-password"
            />
            <div class="field-hint">
              {{
                editForm.role === "agent"
                  ? "Agent 账号的口令要写进各集群 agent 的 MASTER_WS"
                  : "用户首次登录后必须自己修改"
              }}
            </div>
          </el-form-item>
          <el-form-item label="确认口令" prop="confirm">
            <el-input
              v-model="editForm.confirm"
              type="password"
              show-password
              autocomplete="new-password"
            />
          </el-form-item>
        </template>
        <el-form-item label="显示名">
          <el-input v-model="editForm.display_name" maxlength="100" />
        </el-form-item>
        <el-form-item label="邮箱">
          <el-input v-model="editForm.email" maxlength="200" />
        </el-form-item>
        <el-form-item label="备注">
          <el-input v-model="editForm.comment" maxlength="500" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="saveEdit">
          保存
        </el-button>
      </template>
    </el-dialog>

    <!-- 重置口令 -->
    <el-dialog
      v-model="resetVisible"
      :title="`重置 ${resetForm.username} 的口令`"
      width="440px"
      :close-on-click-modal="false"
    >
      <el-alert
        :closable="false"
        type="warning"
        class="mb-4"
        :title="
          resetForm.role === 'agent'
            ? '改完后要同步更新所有用这个账号连接的 agent(MASTER_WS),否则它们会连不上。'
            : '重置后该用户的所有登录会话会下线,下次登录必须修改口令。'
        "
      />
      <el-form
        ref="resetRef"
        :model="resetForm"
        :rules="resetRules"
        label-width="90px"
      >
        <el-form-item label="新口令" prop="password">
          <el-input
            v-model="resetForm.password"
            type="password"
            show-password
            autocomplete="new-password"
          />
        </el-form-item>
        <el-form-item label="确认口令" prop="confirm">
          <el-input
            v-model="resetForm.confirm"
            type="password"
            show-password
            autocomplete="new-password"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="resetVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="saveReset">
          确定
        </el-button>
      </template>
    </el-dialog>

    <!-- 集群权限 -->
    <el-dialog
      v-model="clusterVisible"
      :title="`${clusterUser?.username ?? ''} 的集群权限`"
      width="640px"
      :close-on-click-modal="false"
    >
      <p class="mb-3">
        全局权限:
        <el-tag size="small" :type="roleTag(clusterUser?.role)">
          {{ ROLE_LABELS[clusterUser?.role] }}
        </el-tag>
        <span class="sub-text ml-2">
          没有单独授权的集群一律按「{{
            PERM_LABELS[roleDefaultPermission(clusterUser?.role ?? "none")]
          }}」处理
        </span>
      </p>
      <el-table
        v-loading="clusterLoading"
        :data="clusterRows"
        max-height="420"
        size="small"
      >
        <el-table-column label="集群" min-width="200">
          <template #default="{ row }">
            <span>{{ row.env }}</span>
            <el-tag
              v-if="row.known"
              size="small"
              :type="row.online ? 'success' : 'info'"
              effect="plain"
              class="ml-2"
            >
              {{ row.online ? "在线" : "离线" }}
            </el-tag>
            <el-tag
              v-else
              size="small"
              type="warning"
              effect="plain"
              class="ml-2"
            >
              未接入
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="权限" width="220">
          <template #default="{ row }">
            <el-select
              v-model="row.permission"
              size="small"
              style="width: 190px"
            >
              <el-option
                :label="`按全局权限(${PERM_LABELS[roleDefaultPermission(clusterUser?.role ?? 'none')]})`"
                value=""
              />
              <el-option
                v-for="item in PERM_OPTIONS"
                :key="item.value"
                :label="item.label"
                :value="item.value"
              />
            </el-select>
          </template>
        </el-table-column>
        <el-table-column label="生效" width="90" align="center">
          <template #default="{ row }">
            <el-tag size="small" :type="PERM_TAG[effective(row)]">
              {{ PERM_LABELS[effective(row)] }}
            </el-tag>
          </template>
        </el-table-column>
      </el-table>
      <div class="add-cluster">
        <el-input
          v-model="extraEnv"
          size="small"
          placeholder="给还没接入的集群预先授权:输入集群名(K8S_NAME)"
          style="width: 340px"
          @keyup.enter="addExtraEnv"
        />
        <el-button size="small" @click="addExtraEnv">添加</el-button>
      </div>
      <template #footer>
        <el-button @click="clusterVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="saveClusters">
          保存
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import { ElMessageBox, type FormInstance, type FormRules } from "element-plus";
import { Plus, RefreshRight, Search } from "@element-plus/icons-vue";
import dayjs from "dayjs";
import { message } from "@/utils/message";
import { useUserStoreHook } from "@/store/modules/user";
import type { ClusterPermission } from "@/api/auth";
import {
  type SystemUser,
  createUser,
  deleteUser,
  getUserClusters,
  getUsers,
  logoutUser,
  resetUserPassword,
  setUserClusters,
  updateUser
} from "@/api/system";
import {
  PERM_LABELS,
  PERM_OPTIONS,
  PERM_TAG,
  ROLE_LABELS,
  ROLE_OPTIONS,
  roleDefaultPermission
} from "@/utils/permission";

defineOptions({ name: "SystemUsers" });

const currentUser = useUserStoreHook();
const loading = ref(false);
const saving = ref(false);
const users = ref<SystemUser[]>([]);
const filters = reactive({ keyword: "", source: "", role: "" });
const selfId = computed(
  () => users.value.find(u => u.username === currentUser.username)?.id
);

const filteredUsers = computed(() => {
  const keyword = filters.keyword.trim().toLowerCase();
  return users.value.filter(user => {
    if (filters.source && user.source !== filters.source) return false;
    if (filters.role && user.role !== filters.role) return false;
    if (!keyword) return true;
    return [user.username, user.display_name, user.comment, user.email]
      .filter(Boolean)
      .some(text => text.toLowerCase().includes(keyword));
  });
});

function roleTag(role?: string) {
  return ROLE_OPTIONS.find(item => item.value === role)?.tag ?? "info";
}

function roleDesc(role?: string) {
  return ROLE_OPTIONS.find(item => item.value === role)?.desc ?? "";
}

function grantList(user: SystemUser) {
  return Object.entries(user.clusters ?? {})
    .map(([env, permission]) => ({ env, permission }))
    .sort((a, b) => a.env.localeCompare(b.env));
}

function formatTime(value: string) {
  return value ? dayjs(value).format("YYYY-MM-DD HH:mm:ss") : "";
}

async function load() {
  loading.value = true;
  try {
    const res = await getUsers();
    users.value = res.data ?? [];
  } finally {
    loading.value = false;
  }
}

// ------------------------------------------------------------ 新建 / 编辑
const editVisible = ref(false);
const editRef = ref<FormInstance>();
const editForm = reactive({
  id: 0,
  username: "",
  source: "local" as "local" | "ldap",
  role: "read",
  password: "",
  confirm: "",
  display_name: "",
  email: "",
  comment: "",
  builtin: false
});

const createTitle = computed(() =>
  editForm.source === "ldap" ? "添加 LDAP 账号" : "新建本地账号"
);

const roleOptions = computed(() =>
  ROLE_OPTIONS.filter(
    item => item.value !== "agent" || editForm.source === "local"
  )
);

const editRules: FormRules = {
  username: [
    { required: true, message: "请输入用户名", trigger: "blur" },
    {
      pattern: /^[A-Za-z0-9][A-Za-z0-9._@-]{0,63}$/,
      message: "只能包含字母、数字和 . _ @ -,以字母或数字开头",
      trigger: "blur"
    }
  ],
  role: [{ required: true, message: "请选择全局权限", trigger: "change" }],
  password: [
    { required: true, message: "请输入初始口令", trigger: "blur" },
    { min: 8, max: 128, message: "口令长度 8~128 位", trigger: "blur" }
  ],
  confirm: [
    {
      validator: (_rule, value, callback) =>
        value === editForm.password
          ? callback()
          : callback(new Error("两次输入不一致")),
      trigger: "blur"
    }
  ]
};

function openCreate(source: "local" | "ldap") {
  Object.assign(editForm, {
    id: 0,
    username: "",
    source,
    role: "read",
    password: "",
    confirm: "",
    display_name: "",
    email: "",
    comment: "",
    builtin: false
  });
  editVisible.value = true;
  editRef.value?.clearValidate();
}

function openEdit(user: SystemUser) {
  Object.assign(editForm, {
    id: user.id,
    username: user.username,
    source: user.source,
    role: user.role,
    password: "",
    confirm: "",
    display_name: user.display_name,
    email: user.email,
    comment: user.comment,
    builtin: user.builtin
  });
  editVisible.value = true;
  editRef.value?.clearValidate();
}

async function saveEdit() {
  if (!editRef.value) return;
  if (!(await editRef.value.validate().catch(() => false))) return;
  const original = users.value.find(u => u.id === editForm.id);
  if (
    editForm.id &&
    original &&
    original.role !== editForm.role &&
    editForm.role === "admin" &&
    Object.keys(original.clusters ?? {}).length
  ) {
    const ok = await ElMessageBox.confirm(
      "设为系统管理员后会清空该用户的集群单独授权(系统管理员在所有集群都是管理员),继续吗?",
      "提示",
      { type: "warning" }
    ).catch(() => false);
    if (!ok) return;
  }
  saving.value = true;
  try {
    const payload = {
      role: editForm.role as SystemUser["role"],
      display_name: editForm.display_name,
      email: editForm.email,
      comment: editForm.comment
    };
    if (editForm.id) {
      await updateUser(editForm.id, payload);
      message("已保存", { type: "success" });
    } else {
      await createUser({
        ...payload,
        username: editForm.username.trim(),
        source: editForm.source,
        password: editForm.source === "local" ? editForm.password : undefined
      });
      message("账号已创建", { type: "success" });
    }
    editVisible.value = false;
    await load();
  } finally {
    saving.value = false;
  }
}

async function toggleEnabled(user: SystemUser) {
  const enable = !user.enabled;
  if (!enable) {
    const ok = await ElMessageBox.confirm(
      user.role === "agent"
        ? `禁用后所有用 ${user.username} 连接的 agent 都会被拒绝(已连上的不受影响),确定吗?`
        : `禁用后 ${user.username} 会立即下线且不能再登录,确定吗?`,
      "禁用账号",
      { type: "warning" }
    ).catch(() => false);
    if (!ok) return;
  }
  await updateUser(user.id, { enabled: enable });
  message(enable ? "已启用" : "已禁用", { type: "success" });
  await load();
}

async function kick(user: SystemUser) {
  const ok = await ElMessageBox.confirm(
    `让 ${user.username} 的所有登录会话立即下线?`,
    "强制下线",
    { type: "warning" }
  ).catch(() => false);
  if (!ok) return;
  await logoutUser(user.id);
  message("已下线", { type: "success" });
  await load();
}

async function remove(user: SystemUser) {
  const ok = await ElMessageBox.confirm(
    `删除账号 ${user.username} 及其集群授权?` +
      (user.source === "ldap" ? "(LDAP 用户再次登录会按默认权限重新开通)" : ""),
    "删除账号",
    { type: "warning", confirmButtonText: "删除" }
  ).catch(() => false);
  if (!ok) return;
  await deleteUser(user.id);
  message("已删除", { type: "success" });
  await load();
}

// ------------------------------------------------------------ 重置口令
const resetVisible = ref(false);
const resetRef = ref<FormInstance>();
const resetForm = reactive({
  id: 0,
  username: "",
  role: "",
  password: "",
  confirm: ""
});
const resetRules: FormRules = {
  password: [
    { required: true, message: "请输入新口令", trigger: "blur" },
    { min: 8, max: 128, message: "口令长度 8~128 位", trigger: "blur" }
  ],
  confirm: [
    {
      validator: (_rule, value, callback) =>
        value === resetForm.password
          ? callback()
          : callback(new Error("两次输入不一致")),
      trigger: "blur"
    }
  ]
};

function openReset(user: SystemUser) {
  Object.assign(resetForm, {
    id: user.id,
    username: user.username,
    role: user.role,
    password: "",
    confirm: ""
  });
  resetVisible.value = true;
  resetRef.value?.clearValidate();
}

async function saveReset() {
  if (!resetRef.value) return;
  if (!(await resetRef.value.validate().catch(() => false))) return;
  saving.value = true;
  try {
    await resetUserPassword(resetForm.id, resetForm.password);
    message("口令已重置", { type: "success" });
    resetVisible.value = false;
    await load();
  } finally {
    saving.value = false;
  }
}

// ------------------------------------------------------------ 集群权限
type ClusterRow = {
  env: string;
  online: boolean;
  known: boolean;
  permission: ClusterPermission | "";
};
const clusterVisible = ref(false);
const clusterLoading = ref(false);
const clusterUser = ref<SystemUser | null>(null);
const clusterRows = ref<ClusterRow[]>([]);
const extraEnv = ref("");

function effective(row: ClusterRow): ClusterPermission {
  return (
    (row.permission as ClusterPermission) ||
    roleDefaultPermission(clusterUser.value?.role ?? "none")
  );
}

async function openClusters(user: SystemUser) {
  clusterUser.value = user;
  clusterRows.value = [];
  extraEnv.value = "";
  clusterVisible.value = true;
  clusterLoading.value = true;
  try {
    const res = await getUserClusters(user.id);
    const grants = Object.fromEntries(
      res.data.clusters.map(item => [item.env, item.permission])
    );
    const rows: ClusterRow[] = res.data.known.map(item => ({
      env: item.env,
      online: item.online,
      known: true,
      permission: grants[item.env] ?? ""
    }));
    for (const [env, permission] of Object.entries(grants)) {
      if (!rows.some(row => row.env === env)) {
        rows.push({ env, online: false, known: false, permission });
      }
    }
    clusterRows.value = rows.sort((a, b) => a.env.localeCompare(b.env));
  } finally {
    clusterLoading.value = false;
  }
}

function addExtraEnv() {
  const env = extraEnv.value.trim();
  if (!env) return;
  if (clusterRows.value.some(row => row.env === env)) {
    message(`集群 ${env} 已在列表里`, { type: "warning" });
    return;
  }
  clusterRows.value.push({
    env,
    online: false,
    known: false,
    permission: "read"
  });
  extraEnv.value = "";
}

async function saveClusters() {
  if (!clusterUser.value) return;
  saving.value = true;
  try {
    await setUserClusters(
      clusterUser.value.id,
      clusterRows.value
        .filter(row => row.permission)
        .map(row => ({
          env: row.env,
          permission: row.permission as ClusterPermission
        }))
    );
    message("集群权限已保存,立即生效", { type: "success" });
    clusterVisible.value = false;
    await load();
  } finally {
    saving.value = false;
  }
}

onMounted(load);
</script>

<style lang="scss" scoped>
.system-users {
  padding: 4px;
}

.toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;

  .filters,
  .actions {
    display: flex;
    gap: 8px;
    align-items: center;
  }
}

.name-cell {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  align-items: center;

  .username {
    font-weight: 600;
  }
}

.sub-text {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.grant-tag {
  margin: 2px 4px 2px 0;
}

.field-hint {
  margin-top: 4px;
  font-size: 12px;
  line-height: 1.5;
  color: var(--el-text-color-secondary);
}

.add-cluster {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-top: 12px;
}

.link {
  color: var(--el-color-primary);
}
</style>
