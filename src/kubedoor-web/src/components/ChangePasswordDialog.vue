<script setup lang="ts">
import { computed, reactive, ref, watch, onBeforeUnmount } from "vue";
import type { FormInstance, FormRules } from "element-plus";
import { emitter } from "@/utils/mitt";
import { message } from "@/utils/message";
import { changePasswordApi } from "@/api/auth";
import { useUserStoreHook } from "@/store/modules/user";

defineOptions({
  name: "ChangePasswordDialog"
});

const user = useUserStoreHook();
const visible = ref(false);
const saving = ref(false);
const formRef = ref<FormInstance>();
const form = reactive({ old: "", next: "", confirm: "" });

/** 出厂口令或管理员重置过的口令:必须先改,不能关闭弹窗 */
const forced = computed(() => user.mustChangePassword);

const rules: FormRules = {
  old: [{ required: true, message: "请输入当前口令", trigger: "blur" }],
  next: [
    { required: true, message: "请输入新口令", trigger: "blur" },
    { min: 8, max: 128, message: "口令长度 8~128 位", trigger: "blur" }
  ],
  confirm: [
    {
      validator: (_rule, value, callback) =>
        value === form.next
          ? callback()
          : callback(new Error("两次输入不一致")),
      trigger: "blur"
    }
  ]
};

function open() {
  if (!user.canChangePassword) {
    message("LDAP 账号的口令请在 LDAP 里修改", { type: "warning" });
    return;
  }
  form.old = form.next = form.confirm = "";
  visible.value = true;
}

async function submit() {
  if (!formRef.value || saving.value) return;
  if (!(await formRef.value.validate().catch(() => false))) return;
  saving.value = true;
  try {
    const res = await changePasswordApi({
      old_password: form.old,
      new_password: form.next
    });
    message(res?.message || "口令已修改", { type: "success" });
    visible.value = false;
    await user.fetchMe();
  } catch {
    // 错误提示由请求拦截器统一弹出
  } finally {
    saving.value = false;
  }
}

watch(
  () => [user.loaded, user.mustChangePassword],
  () => {
    if (user.loaded && user.mustChangePassword && !visible.value) open();
  },
  { immediate: true }
);

const onRequired = () => {
  if (!visible.value) open();
};
emitter.on("openChangePassword", open);
emitter.on("passwordChangeRequired", onRequired);
onBeforeUnmount(() => {
  emitter.off("openChangePassword", open);
  emitter.off("passwordChangeRequired", onRequired);
});
</script>

<template>
  <el-dialog
    v-model="visible"
    title="修改口令"
    width="440px"
    :close-on-click-modal="false"
    :close-on-press-escape="!forced"
    :show-close="!forced"
    append-to-body
  >
    <el-alert
      v-if="forced"
      class="mb-4"
      type="warning"
      show-icon
      :closable="false"
      title="当前口令是出厂口令或管理员重置的临时口令,请先修改后再继续使用。"
    />
    <el-form ref="formRef" :model="form" :rules="rules" label-width="90px">
      <el-form-item label="当前口令" prop="old">
        <el-input
          v-model="form.old"
          type="password"
          show-password
          autocomplete="current-password"
        />
      </el-form-item>
      <el-form-item label="新口令" prop="next">
        <el-input
          v-model="form.next"
          type="password"
          show-password
          autocomplete="new-password"
          placeholder="至少 8 位,不能太简单"
        />
      </el-form-item>
      <el-form-item label="确认新口令" prop="confirm">
        <el-input
          v-model="form.confirm"
          type="password"
          show-password
          autocomplete="new-password"
          @keyup.enter="submit"
        />
      </el-form-item>
    </el-form>
    <p class="text-xs text-gray-400">修改后其它终端上的登录会自动退出。</p>
    <template #footer>
      <el-button v-if="forced" @click="user.logOut()">退出登录</el-button>
      <el-button v-else @click="visible = false">取消</el-button>
      <el-button type="primary" :loading="saving" @click="submit">
        确定
      </el-button>
    </template>
  </el-dialog>
</template>
