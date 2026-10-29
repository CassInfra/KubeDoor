<template>
  <div class="system-ldap">
    <el-card v-loading="loading">
      <template #header>
        <div class="card-header">
          <span>LDAP 认证</span>
          <el-switch
            v-model="form.enabled"
            active-text="启用"
            inactive-text="停用"
          />
        </div>
      </template>
      <el-alert type="info" :closable="false" class="mb-4">
        <template #title>
          登录时先匹配本地账号,本地没有再走 LDAP。认证方式为「服务账号搜索用户 →
          用户 DN + 口令绑定」; 也可以只填「用户 DN
          模板」直接绑定(不需要服务账号)。空口令一律拒绝。修改后立即生效。
        </template>
      </el-alert>

      <el-form
        ref="formRef"
        :model="form"
        label-width="140px"
        class="ldap-form"
      >
        <el-divider content-position="left">服务器</el-divider>
        <el-form-item label="服务器地址" required>
          <el-input
            v-model="form.url"
            placeholder="ldap://ldap.example.com:389 或 ldaps://ldap.example.com:636,多个用空格分隔(按顺序故障切换)"
          />
        </el-form-item>
        <el-form-item label="加密">
          <el-checkbox v-model="form.start_tls" :disabled="isLdaps">
            StartTLS
          </el-checkbox>
          <el-checkbox v-model="form.tls_verify">校验服务器证书</el-checkbox>
          <span class="hint">
            ldaps:// 已是加密连接;自签证书可以在下面填
            CA,或者关闭证书校验(不推荐)
          </span>
        </el-form-item>
        <el-form-item v-if="form.start_tls || isLdaps" label="CA 证书(PEM)">
          <el-input
            v-model="form.ca_cert"
            type="textarea"
            :rows="3"
            placeholder="-----BEGIN CERTIFICATE-----(可选,公网 CA 签发的证书不用填)"
          />
        </el-form-item>
        <el-form-item label="超时(秒)">
          <el-input-number v-model="form.timeout" :min="1" :max="30" />
        </el-form-item>

        <el-divider content-position="left">查找用户</el-divider>
        <el-form-item label="服务账号 DN">
          <el-input
            v-model="form.bind_dn"
            placeholder="cn=readonly,dc=example,dc=com(留空=匿名搜索;AD 可填 readonly@corp.example.com)"
          />
        </el-form-item>
        <el-form-item label="服务账号口令">
          <el-input
            v-model="bindPassword"
            type="password"
            show-password
            autocomplete="new-password"
            :disabled="clearPassword"
            :placeholder="
              form.has_bind_password ? '已设置,留空表示不修改' : '服务账号口令'
            "
          />
          <el-checkbox
            v-if="form.has_bind_password"
            v-model="clearPassword"
            class="ml-3"
          >
            清除
          </el-checkbox>
        </el-form-item>
        <el-form-item label="搜索起点 Base DN">
          <el-input
            v-model="form.base_dn"
            placeholder="ou=people,dc=example,dc=com"
          />
        </el-form-item>
        <el-form-item label="用户过滤条件">
          <el-input v-model="form.user_filter" />
          <div class="hint">
            {username}
            会替换成登录名(已做转义)。OpenLDAP:(&amp;(objectClass=inetOrgPerson)(uid={username}));
            AD:(&amp;(objectClass=user)(sAMAccountName={username}));只允许某个组登录可加
            (memberOf=cn=kubedoor,ou=groups,dc=example,dc=com)
          </div>
        </el-form-item>
        <el-form-item label="用户 DN 模板">
          <el-input
            v-model="form.user_dn_template"
            placeholder="可选:uid={username},ou=people,dc=example,dc=com 或 {username}@corp.example.com"
          />
          <div class="hint">填了就直接用这个 DN 绑定,不再用服务账号搜索</div>
        </el-form-item>
        <el-form-item label="属性映射">
          <div class="attr-row">
            <el-input v-model="form.attr_username" placeholder="uid">
              <template #prepend>用户名</template>
            </el-input>
            <el-input v-model="form.attr_display_name" placeholder="cn">
              <template #prepend>显示名</template>
            </el-input>
            <el-input v-model="form.attr_email" placeholder="mail">
              <template #prepend>邮箱</template>
            </el-input>
          </div>
        </el-form-item>

        <el-divider content-position="left">新用户</el-divider>
        <el-form-item label="首次登录自动开通">
          <el-switch v-model="form.auto_create" />
          <span class="hint">
            关闭后只有系统管理员在「用户与权限」里添加过的 LDAP 账号才能登录
          </span>
        </el-form-item>
        <el-form-item label="默认全局权限">
          <el-radio-group v-model="form.default_role">
            <el-radio value="read">只读</el-radio>
            <el-radio value="none">无(只能访问单独授权的集群)</el-radio>
          </el-radio-group>
        </el-form-item>

        <el-form-item>
          <el-button type="primary" :loading="saving" @click="save">
            保存
          </el-button>
          <el-button @click="load">重置</el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <el-card class="mt-3">
      <template #header>测试(用当前表单里的配置,不需要先保存)</template>
      <div class="test-row">
        <el-input
          v-model="test.username"
          placeholder="测试登录名(可选)"
          style="width: 200px"
        />
        <el-input
          v-model="test.password"
          type="password"
          show-password
          placeholder="测试口令(可选)"
          style="width: 200px"
        />
        <el-button type="primary" plain :loading="testing" @click="runTest">
          测试
        </el-button>
      </div>
      <el-timeline v-if="testResult" class="mt-4">
        <el-timeline-item
          v-for="(step, index) in testResult.steps"
          :key="index"
          :type="step.ok ? 'success' : 'danger'"
          :timestamp="step.name"
          placement="top"
        >
          {{ step.detail || (step.ok ? "通过" : "失败") }}
        </el-timeline-item>
      </el-timeline>
      <el-descriptions
        v-if="testResult?.user"
        :column="1"
        border
        size="small"
        class="mt-2"
      >
        <el-descriptions-item label="DN">{{
          testResult.user.dn
        }}</el-descriptions-item>
        <el-descriptions-item label="用户名">
          {{ testResult.user.username }}
        </el-descriptions-item>
        <el-descriptions-item label="显示名">
          {{ testResult.user.display_name }}
        </el-descriptions-item>
        <el-descriptions-item label="邮箱">{{
          testResult.user.email
        }}</el-descriptions-item>
      </el-descriptions>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import type { FormInstance } from "element-plus";
import { message } from "@/utils/message";
import {
  type LdapConfig,
  type LdapTestResult,
  getLdapConfig,
  saveLdapConfig,
  testLdapConfig
} from "@/api/system";

defineOptions({ name: "SystemLdap" });

const loading = ref(false);
const saving = ref(false);
const testing = ref(false);
const formRef = ref<FormInstance>();
const bindPassword = ref("");
const clearPassword = ref(false);
const testResult = ref<LdapTestResult | null>(null);
const test = reactive({ username: "", password: "" });

const form = reactive<LdapConfig>({
  enabled: false,
  url: "",
  start_tls: false,
  tls_verify: true,
  ca_cert: "",
  bind_dn: "",
  has_bind_password: false,
  base_dn: "",
  user_filter: "(&(objectClass=inetOrgPerson)(uid={username}))",
  user_dn_template: "",
  attr_username: "uid",
  attr_display_name: "cn",
  attr_email: "mail",
  auto_create: true,
  default_role: "read",
  timeout: 5
});

const isLdaps = computed(() => form.url.trim().startsWith("ldaps://"));

/** 提交给后端的配置:口令只在填了新值或勾选清除时才带上 */
function payload(): LdapConfig {
  const data: LdapConfig = { ...form };
  delete data.has_bind_password;
  if (clearPassword.value) data.bind_password = "";
  else if (bindPassword.value) data.bind_password = bindPassword.value;
  else data.bind_password = null;
  if (isLdaps.value) data.start_tls = false;
  return data;
}

async function load() {
  loading.value = true;
  try {
    const res = await getLdapConfig();
    Object.assign(form, res.data);
    bindPassword.value = "";
    clearPassword.value = false;
  } finally {
    loading.value = false;
  }
}

async function save() {
  saving.value = true;
  try {
    const res = await saveLdapConfig(payload());
    Object.assign(form, res.data);
    bindPassword.value = "";
    clearPassword.value = false;
    message("LDAP 设置已保存", { type: "success" });
  } finally {
    saving.value = false;
  }
}

async function runTest() {
  testing.value = true;
  testResult.value = null;
  try {
    const res = await testLdapConfig({
      config: { ...payload(), enabled: true },
      username: test.username.trim() || undefined,
      password: test.password || undefined
    });
    testResult.value = res.data;
    message(res.data.success ? "测试通过" : "测试未通过,请看下面的步骤", {
      type: res.data.success ? "success" : "warning"
    });
  } finally {
    testing.value = false;
  }
}

onMounted(load);
</script>

<style lang="scss" scoped>
.system-ldap {
  max-width: 980px;
  padding: 4px;
}

.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.hint {
  margin-left: 8px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--el-text-color-secondary);
}

.ldap-form .hint {
  display: block;
  margin-left: 0;
}

.attr-row {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 8px;
  width: 100%;
}

.test-row {
  display: flex;
  gap: 8px;
  align-items: center;
}
</style>
