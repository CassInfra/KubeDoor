<script setup lang="ts">
import { computed, ref } from "vue";
import { useMediaQuery } from "@vueuse/core";
import ReCol from "@/components/ReCol";
import {
  createJvmFormRules,
  createPodCountManualRules,
  formRules
} from "../utils/rule";
import { JVM_PARAMETERS } from "../utils/jvm";
import { FormProps } from "../utils/types";
import addLine from "@iconify-icons/ri/add-line";
import { transformI18n } from "@/plugins/i18n";
import { getNamespace } from "@/api/resource";

const props = withDefaults(defineProps<FormProps>(), {
  formInline: () => ({
    env: "",
    namespace: "",
    deployment: "",
    pod_count_manual: "",
    limit_cpu_m: "",
    limit_mem_mb: "",
    request_cpu_m: "",
    request_mem_mb: "",
    pod_count: ""
  }),
  namespace: () => [],
  envList: () => [],
  isEdit: false
});

const formRef = ref();
const newFormInline = ref(props.formInline);
const rules = {
  ...formRules,
  ...createPodCountManualRules(newFormInline.value, props.isEdit),
  ...createJvmFormRules(newFormInline.value)
};
const hasJvmParameters = computed(() =>
  JVM_PARAMETERS.some(
    ({ inputKey }) => newFormInline.value[inputKey] !== undefined
  )
);
// 与 el-col 的 lg 断点一致:≥1200px 时 JVM 参数一行两个、右列标签收窄,
// 否则一行一个,标签和表单其它项同宽
const jvmTwoColumns = useMediaQuery("(min-width: 1200px)");
const namespaceList = ref(props.namespace);

function getRef() {
  return formRef.value;
}

async function onFormEnvChange(val: string) {
  newFormInline.value.namespace = "";
  namespaceList.value = [];
  const namespaceRes = await getNamespace(val);
  namespaceList.value = namespaceRes.data;
}

defineExpose({ getRef });
</script>

<template>
  <el-form
    ref="formRef"
    :model="newFormInline"
    :rules="rules"
    label-width="145px"
  >
    <el-row :gutter="20">
      <re-col :value="20" :xs="24" :sm="24">
        <el-form-item :label="transformI18n('resource.column.k8s')" prop="env">
          <el-select
            v-model="newFormInline.env"
            :placeholder="transformI18n('resource.placeholder')"
            allow-create
            filterable
            :disabled="isEdit"
            @change="onFormEnvChange"
          >
            <el-option
              v-for="item in props.envList"
              :key="item[0]"
              :label="item[0]"
              :value="item[0]"
            />
          </el-select>
        </el-form-item>
      </re-col>
      <re-col :value="20" :xs="24" :sm="24">
        <el-form-item
          :label="transformI18n('resource.column.namespace')"
          prop="namespace"
        >
          <!-- <el-input v-model="newFormInline.namespace" :disabled="isEdit" /> -->
          <el-select
            v-model="newFormInline.namespace"
            :placeholder="transformI18n('resource.placeholder')"
            allow-create
            filterable
            :disabled="isEdit"
          >
            <el-option
              v-for="item in namespaceList"
              :key="item[0]"
              :label="item[0]"
              :value="item[0]"
            />
          </el-select>
        </el-form-item>
      </re-col>
      <re-col :value="20" :xs="24" :sm="24">
        <el-form-item
          :label="transformI18n('resource.column.deployment')"
          prop="deployment"
        >
          <el-input v-model="newFormInline.deployment" :disabled="isEdit" />
        </el-form-item>
      </re-col>
      <re-col v-if="isEdit" :value="20" :xs="24" :sm="24">
        <el-form-item
          :label="transformI18n('resource.column.podCount')"
          prop="pod_count"
        >
          <el-input
            v-model="newFormInline.pod_count"
            type="number"
            :disabled="isEdit"
          />
        </el-form-item>
      </re-col>
      <re-col :value="20" :xs="24" :sm="24">
        <el-form-item
          :label="transformI18n('resource.column.podCountManual')"
          prop="pod_count_manual"
        >
          <el-input v-model="newFormInline.pod_count_manual" type="number" />
        </el-form-item>
      </re-col>
      <re-col :value="20" :xs="24" :sm="24">
        <el-form-item
          :label="transformI18n('resource.column.requestCpuM')"
          prop="request_cpu_m"
        >
          <el-input
            v-model="newFormInline.request_cpu_m"
            type="number"
            :disabled="isEdit"
          />
        </el-form-item>
      </re-col>
      <re-col :value="20" :xs="24" :sm="24">
        <el-form-item
          :label="transformI18n('resource.column.limitCpuM')"
          prop="limit_cpu_m"
        >
          <el-input v-model="newFormInline.limit_cpu_m" type="number" />
        </el-form-item>
      </re-col>
      <re-col :value="20" :xs="24" :sm="24">
        <el-form-item
          :label="transformI18n('resource.column.requestMemMb')"
          prop="request_mem_mb"
        >
          <el-input
            v-model="newFormInline.request_mem_mb"
            type="number"
            :disabled="isEdit"
          />
        </el-form-item>
      </re-col>

      <re-col :value="20" :xs="24" :sm="24">
        <el-form-item
          :label="transformI18n('resource.column.limitMemMb')"
          prop="limit_mem_mb"
        >
          <el-input v-model="newFormInline.limit_mem_mb" type="number" />
        </el-form-item>
      </re-col>
      <template v-if="hasJvmParameters">
        <!-- 左列用表单的标签宽度,和上面的输入框对齐;右列标签窄,多分一格给左列,两个输入框差不多宽 -->
        <re-col
          v-for="(field, index) in JVM_PARAMETERS"
          :key="field.key"
          :value="index % 2 ? 9 : 11"
          :xs="24"
          :sm="24"
          :md="20"
        >
          <el-form-item
            :label="field.label"
            :prop="field.inputKey"
            :label-width="jvmTwoColumns && index % 2 ? '80px' : undefined"
            class="jvm-parameter"
          >
            <el-input
              v-model="newFormInline[field.inputKey]"
              inputmode="decimal"
              :disabled="newFormInline[field.inputKey] === undefined"
              placeholder="-"
            >
              <template #suffix>{{ field.unit }}</template>
            </el-input>
          </el-form-item>
        </re-col>
      </template>
    </el-row>
    <div class="warning-box">
      <p>
        ⚠️需求值与限制值的调整，仅在[开启管控]后执行重启时才会应用到微服务，否则改动仅会写入数据库。
      </p>
      <p v-if="hasJvmParameters">
        Xss 使用 k（KiB），其他 JVM 参数使用
        m（MiB）；仅可修改已采集项，保存后在下次发布或重启时生效。
      </p>
    </div>
  </el-form>
</template>

<style scoped>
.jvm-parameter :deep(.el-form-item__label),
.jvm-parameter :deep(.el-input__inner),
.jvm-parameter :deep(.el-input__suffix) {
  color: #409eff;
}

.jvm-parameter :deep(.el-input__suffix) {
  font-weight: 700;
}

/* 一行两个时输入框窄,错误提示不换行,免得压到下一行 */
.jvm-parameter :deep(.el-form-item__error) {
  white-space: nowrap;
}

.avatar-uploader .el-upload {
  position: relative;
  overflow: hidden;
  cursor: pointer;
}

.avatar-uploader-icon {
  width: 145px;
  height: 145px;
  font-size: 40px;
  line-height: 145px;
  color: #8c939d;
  text-align: center;
  border: 1px dashed #d9d9d9;
  border-radius: 6px;
}

.avatar-uploader-icon:hover {
  color: #409eff;
  border-color: #409eff;
}

.avatar {
  display: block;
  width: 145px;
  height: 145px;
}

.warning-box {
  padding: 10px 15px;
  margin: 10px 0;
  font-size: 14px;
  color: #5f5f5f;
  background-color: #fff9ed;
  border-left: 4px solid #e6a23c;
  border-radius: 4px;
}

.warning-box p {
  margin: 8px 0;
  line-height: 1.5;
}
</style>
