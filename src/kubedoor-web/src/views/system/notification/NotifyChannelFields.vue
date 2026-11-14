<script setup lang="ts">
// 一个通知通道的表单项：IM 类型、机器人地址、默认 @ 人、发送测试消息。
// 消息通知页的全局配置和「安装 Agent」对话框共用；放在调用方的 el-form 里，跟随外层表单的标签设置和 disabled。
import type { NotifyChannel } from "@/api/notification";
import {
  type ChannelForm,
  type TestView,
  TYPE_OPTIONS,
  tokenPlaceholder,
  typeHint
} from "./notification-utils";

defineOptions({ name: "NotifyChannelFields" });

/** 表单内容：直接改调用方传进来的对象 */
const form = defineModel<ChannelForm>("form", { required: true });

withDefaults(
  defineProps<{
    /** 已保存的配置：按它生成机器人地址的 placeholder（留空保留 / 换了类型要重填） */
    saved?: NotifyChannel | null;
    /** 机器人地址的 placeholder；不传时按 saved 生成 */
    placeholder?: string;
    /** 正在发送测试消息 */
    testing?: boolean;
    /** 测试结果 */
    result?: TestView | null;
  }>(),
  { saved: null, placeholder: "", testing: false, result: null }
);

defineEmits<{ test: [] }>();
</script>

<template>
  <el-form-item label="IM 类型">
    <el-select v-model="form.type" class="notify-type">
      <el-option
        v-for="item in TYPE_OPTIONS"
        :key="item.value"
        :label="item.label"
        :value="item.value"
      />
    </el-select>
  </el-form-item>
  <el-form-item label="机器人地址">
    <el-input
      v-model="form.token"
      type="password"
      show-password
      autocomplete="new-password"
      :placeholder="placeholder || tokenPlaceholder(form.type, saved)"
    />
    <div class="notify-hint">{{ typeHint(form.type).tokenHelp }}</div>
  </el-form-item>
  <el-form-item label="默认 @ 人">
    <el-input
      v-model="form.at"
      clearable
      :placeholder="typeHint(form.type).at"
    />
    <div class="notify-hint">
      通知里 @ 这个人，只能填一个；指标告警规则的 annotations 里写了 at
      的，以规则为准
    </div>
  </el-form-item>
  <el-form-item>
    <el-button :loading="testing" @click="$emit('test')">
      发送测试消息
    </el-button>
    <span v-if="result" :class="['notify-test-result', result.type]">
      {{ result.text }}
    </span>
  </el-form-item>
</template>

<style lang="scss" scoped>
.notify-type {
  width: 200px;
}

// 输入框下面的说明独占一行
.notify-hint {
  display: block;
  width: 100%;
  font-size: 12px;
  line-height: 1.6;
  color: var(--el-text-color-secondary);
}

.notify-test-result {
  flex: 1;
  min-width: 0;
  margin-left: 12px;
  font-size: 13px;
  line-height: 1.6;
  word-break: break-all;

  &.success {
    color: var(--el-color-success);
  }

  &.warning {
    color: var(--el-color-warning);
  }

  &.error {
    color: var(--el-color-danger);
  }
}
</style>
