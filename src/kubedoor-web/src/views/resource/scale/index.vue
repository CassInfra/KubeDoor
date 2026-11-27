<script setup lang="ts">
import { ref } from "vue";
import { transformI18n } from "@/plugins/i18n";
import ReCol from "@/components/ReCol";
// import { message } from "@/utils/message";

import Warning from "@iconify-icons/ep/warning-filled";

type ScaleContentItem = {
  env?: string;
  namespace?: string;
  deployment?: string;
  podCount?: number | string;
};

type Props = {
  isScale: boolean;
  content?: string;
  items?: ScaleContentItem[];
  showInterval: boolean;
  showAddLabel: number; //-1: 多条数据显示默认为false, 0: 不显示, 1: 显示默认为true
};

const props = withDefaults(defineProps<Props>(), {
  isScale: true,
  content: "",
  items: () => [],
  showInterval: false,
  showAddLabel: -1
});
const formRef = ref();
const form = ref({
  interval: 0,
  type: 1,
  time: "",
  cron: "",
  add_label: props.showAddLabel > 0 ? true : false
});

const validateData = (rule, value, callback) => {
  const inputTime = new Date(value);
  const cstOffset = 8 * 60 * 60 * 1000; // CST是UTC+8，转换为毫秒
  const inputCSTTime = inputTime.getTime() + cstOffset;

  // 获取当前时间的CST
  const currentCSTTime = Date.now() + cstOffset;

  // 比较时间
  if (inputCSTTime < currentCSTTime) {
    callback(new Error(transformI18n("resource.rules.futureTime")));
  } else {
    callback();
  }
};

const formRules = {
  type: [{ required: true, message: transformI18n("resource.rules.type") }],
  time: [
    { required: true, message: transformI18n("resource.rules.time") },
    { validator: validateData, trigger: "blur" }
  ],
  cron: [{ required: true, message: transformI18n("resource.rules.cron") }]
};

function getData() {
  return new Promise((resolve, reject) => {
    formRef.value.validate((valid: any) => {
      if (valid) {
        const tempData = JSON.parse(JSON.stringify(form.value));
        if (tempData.time) {
          // 转换时间
          const date = new Date(tempData.time);
          const cstOffset = 8 * 60; // CST是UTC+8
          const cstDate = new Date(date.getTime() + cstOffset * 60 * 1000);

          const dateArray = [
            cstDate.getUTCFullYear(), // 年
            cstDate.getUTCMonth() + 1, // 月（注意：月份从0开始，所以要加1）
            cstDate.getUTCDate(), // 日
            cstDate.getUTCHours(), // 小时
            cstDate.getUTCMinutes() // 分钟
          ];
          tempData.time = dateArray;
        }
        resolve(tempData);
      }
    });
  });
}

defineExpose({ getData });
</script>

<template>
  <div>
    <el-form
      ref="formRef"
      :rules="formRules"
      :model="form"
      label-width="80px"
      label-position="left"
    >
      <el-row :gutter="30">
        <re-col :value="20" :xs="24" :sm="24">
          <div style="margin-bottom: 15px">
            <IconifyIconOffline
              :icon="Warning"
              class="inline text-[24px] align-bottom text-[#e6a23c] mr-2"
            />
            <span>
              {{
                props.isScale
                  ? transformI18n("resource.message.isExecuteScale")
                  : transformI18n("resource.message.isExecuteReboot")
              }}
            </span>
          </div>
        </re-col>
        <re-col :offset="2" :value="20" :xs="24" :sm="24">
          <div style="margin-bottom: 15px">
            <template v-if="props.items?.length">
              <div
                v-for="(item, index) in props.items"
                :key="`${item.env ?? ''}-${item.namespace ?? ''}-${item.deployment ?? ''}-${index}`"
                style="margin-bottom: 6px"
              >
                <strong style="margin-right: 5px">Env: </strong>
                <i style="margin-right: 8px">
                  <span style="color: red">{{ item.env }}</span>
                </i>
                <strong style="margin-right: 5px">Namespace: </strong>
                <i style="margin-right: 8px">
                  <span style="color: red">{{ item.namespace }}</span>
                </i>
                <strong style="margin-right: 5px">Deployment: </strong>
                <i style="margin-right: 8px">
                  <span style="color: red">{{ item.deployment }}</span>
                </i>
                <template v-if="item.podCount !== undefined">
                  <strong style="margin-right: 5px">Pod数量: </strong>
                  <i>
                    <span style="color: red">{{ item.podCount }}</span>
                  </i>
                </template>
              </div>
            </template>
            <span v-else>{{ props.content }}</span>
          </div>
        </re-col>
        <template v-if="props.isScale">
          <re-col
            v-if="props.showAddLabel !== 0"
            :offset="2"
            :value="20"
            :xs="24"
            :sm="24"
          >
            <el-form-item
              class="addLabel_form_item"
              :label="transformI18n('resource.form.addLabel')"
              label-width="180px"
              prop="add_label"
            >
              <el-checkbox v-model="form.add_label" />
            </el-form-item>
          </re-col>
        </template>
        <template v-if="!props.showInterval">
          <re-col :offset="2" :value="20" :xs="24" :sm="24">
            <el-form-item
              :label="transformI18n('resource.form.type')"
              prop="type"
            >
              <el-radio-group v-model="form.type">
                <el-radio :label="1">{{
                  transformI18n("resource.form.ExecuteImmediately")
                }}</el-radio>
                <el-radio :label="2">
                  {{ transformI18n("resource.form.ScheduledExecution") }}
                </el-radio>
                <el-radio :label="3">
                  {{ transformI18n("resource.form.PeriodicExecution") }}
                </el-radio>
              </el-radio-group>
            </el-form-item>
          </re-col>
          <re-col :offset="2" :value="20" :xs="24" :sm="24">
            <el-form-item
              v-if="form.type === 2"
              :label="transformI18n('resource.form.time')"
              prop="time"
            >
              <el-date-picker
                v-model="form.time"
                type="datetime"
                :placeholder="transformI18n('resource.placeholder')"
                :disabledDate="
                  (time: any) => {
                    const yesterday = new Date();
                    yesterday.setDate(yesterday.getDate() - 1);
                    yesterday.setHours(23, 59, 59, 999);
                    return time.getTime() < yesterday.getTime();
                  }
                "
              />
            </el-form-item>
          </re-col>
          <re-col :offset="2" :value="20" :xs="24" :sm="24">
            <el-form-item v-if="form.type === 3" label="Cron" prop="cron">
              <el-input
                v-model="form.cron"
                :placeholder="transformI18n('resource.form.cronPlaceholder')"
              />
            </el-form-item>
          </re-col>
        </template>
        <re-col
          v-if="props.showInterval"
          :offset="2"
          :value="20"
          :xs="24"
          :sm="24"
        >
          <el-form-item
            :label="transformI18n('resource.form.interval')"
            prop="interval"
          >
            <el-input v-model="form.interval" type="number" />
          </el-form-item>
        </re-col>
      </el-row>
    </el-form>
  </div>
</template>

<style scoped>
.addLabel_form_item {
  :deep(.el-form-item__label) {
    color: var(--el-color-danger);
  }
}
</style>
