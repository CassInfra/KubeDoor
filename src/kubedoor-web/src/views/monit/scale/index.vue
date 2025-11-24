<script setup lang="ts">
import { ref } from "vue";
import { transformI18n } from "@/plugins/i18n";
import ReCol from "@/components/ReCol";
import { getNodeResourceRank } from "@/api/monit";
import { ElMessage } from "element-plus";
// import { message } from "@/utils/message";

import Warning from "@iconify-icons/ep/warning-filled";

type Props = {
  isScale: boolean;
  content: string;
  showInterval: boolean;
  showAddLabel: boolean;
  params?: Record<string, any>;
};

const props = withDefaults(defineProps<Props>(), {
  isScale: true,
  content: "",
  showInterval: false,
  showAddLabel: false,
  params: () => ({})
});
const formRef = ref();
const form = ref({
  interval: 0,
  type: 1,
  time: "",
  cron: "",
  add_label: props.showAddLabel ? true : false,
  temp: false,
  strategy: "cpu"
});

const podCount = ref(props.params?.podCount || 0);

// 调度到指定节点相关变量
const schedulerRef = ref(false); // 调度到指定节点
const resourceTypeRef = ref("cpu"); // 资源类型选择
const nodeListRef = ref([]); // 节点列表
const selectedNodesRef = ref([]); // 选中的节点

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

// 获取节点资源信息的函数
const fetchNodeResources = async (
  resourceType: string,
  namespace: string,
  deployment: string
) => {
  try {
    const result = await getNodeResourceRank(
      props.params?.env || "",
      resourceType,
      namespace,
      deployment
    );
    if (result.success && result.data) {
      nodeListRef.value = result.data;
      selectedNodesRef.value = []; // 重置选中的节点
      // 更新节点列表显示
      const nodeListContainer = document.getElementById("nodeListContainer");
      if (nodeListContainer) {
        nodeListContainer.style.display = "block";
        // 重新渲染节点列表
        renderNodeList();
      }
    }
  } catch (error) {
    console.error("获取节点资源信息失败:", error);
    ElMessage.error("获取节点资源信息失败");
  }
};

// 渲染节点列表
const renderNodeList = () => {
  const nodeListContainer = document.getElementById("nodeListContainer");
  if (!nodeListContainer) return;

  // 清空现有内容
  nodeListContainer.innerHTML = "";

  // 创建节点列表
  nodeListRef.value.forEach((node: any) => {
    const nodeItem = document.createElement("div");
    nodeItem.style.cssText =
      "display: flex; align-items: center; margin-bottom: 8px; padding: 8px; border: 1px solid #e4e7ed; border-radius: 4px;";

    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.style.marginRight = "8px";
    checkbox.addEventListener("change", e => {
      const target = e.target as HTMLInputElement;
      if (target.checked) {
        if (!selectedNodesRef.value.includes(node.name)) {
          selectedNodesRef.value.push(node.name);
        }
      } else {
        const index = selectedNodesRef.value.indexOf(node.name);
        if (index > -1) {
          selectedNodesRef.value.splice(index, 1);
        }
      }
    });

    const label = document.createElement("span");
    // 当资源类型是Pod数时不显示百分号
    const percentText =
      resourceTypeRef.value === "pod" ? node.percent : `${node.percent}%`;
    // 设置颜色样式：percentText为蓝色，node.cpod_num不等于0时为红色
    const cpodNumColor =
      node.cpod_num !== 0 ? "color: red;" : "color: inherit;";
    label.innerHTML = `${node.name} (<span style="color: blue;">${percentText}</span>，<span style="${cpodNumColor}">${node.cpod_num}Pod</span>)`;

    nodeItem.appendChild(checkbox);
    nodeItem.appendChild(label);
    nodeListContainer.appendChild(nodeItem);
  });
};

// 处理调度器勾选变化
const handleSchedulerChange = () => {
  if (!schedulerRef.value) {
    // 取消勾选时重置相关状态
    resourceTypeRef.value = "cpu";
    nodeListRef.value = [];
    selectedNodesRef.value = [];
    const nodeListContainer = document.getElementById("nodeListContainer");
    if (nodeListContainer) {
      nodeListContainer.style.display = "none";
    }
  }
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
        resolve({
          podCount: podCount.value,
          tempData: tempData,
          temp: form.value.temp,
          strategy: form.value.strategy,
          scheduler: schedulerRef.value,
          selectedNodes: selectedNodesRef.value
        });
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
          <div style="margin-bottom: 15px" v-html="props.content" />
        </re-col>
        <template v-if="props.isScale && props.params">
          <re-col :offset="2" :value="20" :xs="24" :sm="24">
            <el-form-item
              :label="transformI18n('resource.column.podCountManual')"
              prop="podCount"
            >
              <el-slider v-model="podCount" show-input />
            </el-form-item>
          </re-col>
          <re-col
            v-if="props.showAddLabel"
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
          <re-col :offset="2" :value="20" :xs="24" :sm="24">
            <div style="display: flex; gap: 20px; align-items: center">
              <label
                style="display: flex; align-items: center; cursor: pointer"
              >
                <el-checkbox v-model="form.temp" style="margin-right: 8px" />
                <span>临时扩容</span>
              </label>
              <label
                style="display: flex; align-items: center; cursor: pointer"
              >
                <el-checkbox
                  v-model="schedulerRef"
                  style="margin-right: 8px"
                  @change="handleSchedulerChange"
                />
                <span>调度到指定节点</span>
              </label>
            </div>
          </re-col>
          <!-- 调度到指定节点的资源类型选择 -->
          <re-col v-if="schedulerRef" :offset="2" :value="20" :xs="24" :sm="24">
            <el-form-item :label="'资源类型'" label-width="80px">
              <div style="display: flex; flex-wrap: wrap; gap: 12px">
                <label
                  style="
                    display: flex;
                    align-items: center;
                    font-size: 12px;
                    cursor: pointer;
                  "
                >
                  <input
                    type="radio"
                    name="resourceType"
                    value="cpu"
                    style="margin-right: 4px"
                    @change="
                      resourceTypeRef = 'cpu';
                      fetchNodeResources(
                        'cpu',
                        props.params?.namespace || '',
                        props.params?.deployment || ''
                      );
                    "
                  />
                  <span>当前CPU</span>
                </label>
                <label
                  style="
                    display: flex;
                    align-items: center;
                    font-size: 12px;
                    cursor: pointer;
                  "
                >
                  <input
                    type="radio"
                    name="resourceType"
                    value="mem"
                    style="margin-right: 4px"
                    @change="
                      resourceTypeRef = 'mem';
                      fetchNodeResources(
                        'mem',
                        props.params?.namespace || '',
                        props.params?.deployment || ''
                      );
                    "
                  />
                  <span>当前内存</span>
                </label>
                <label
                  style="
                    display: flex;
                    align-items: center;
                    font-size: 12px;
                    cursor: pointer;
                  "
                >
                  <input
                    type="radio"
                    name="resourceType"
                    value="peak_cpu"
                    style="margin-right: 4px"
                    @change="
                      resourceTypeRef = 'peak_cpu';
                      fetchNodeResources(
                        'peak_cpu',
                        props.params?.namespace || '',
                        props.params?.deployment || ''
                      );
                    "
                  />
                  <span>峰值CPU</span>
                </label>
                <label
                  style="
                    display: flex;
                    align-items: center;
                    font-size: 12px;
                    cursor: pointer;
                  "
                >
                  <input
                    type="radio"
                    name="resourceType"
                    value="peak_mem"
                    style="margin-right: 4px"
                    @change="
                      resourceTypeRef = 'peak_mem';
                      fetchNodeResources(
                        'peak_mem',
                        props.params?.namespace || '',
                        props.params?.deployment || ''
                      );
                    "
                  />
                  <span>峰值内存</span>
                </label>
                <label
                  style="
                    display: flex;
                    align-items: center;
                    font-size: 12px;
                    cursor: pointer;
                  "
                >
                  <input
                    type="radio"
                    name="resourceType"
                    value="pod"
                    style="margin-right: 4px"
                    @change="
                      resourceTypeRef = 'pod';
                      fetchNodeResources(
                        'pod',
                        props.params?.namespace || '',
                        props.params?.deployment || ''
                      );
                    "
                  />
                  <span>Pod数</span>
                </label>
              </div>
            </el-form-item>
          </re-col>
          <!-- 节点列表容器 -->
          <re-col v-if="schedulerRef" :offset="2" :value="20" :xs="24" :sm="24">
            <div
              id="nodeListContainer"
              style="
                display: none;
                max-height: 200px;
                padding: 8px;
                overflow-y: auto;
                border: 1px solid #e4e7ed;
                border-radius: 4px;
              "
            />
          </re-col>
          <re-col
            v-if="props.showAddLabel && form.add_label"
            :offset="2"
            :value="20"
            :xs="24"
            :sm="24"
          >
            <el-form-item
              :label="'扩缩容策略(基于节点):'"
              label-width="100px"
              prop="strategy"
            >
              <el-radio-group v-model="form.strategy">
                <el-radio label="cpu">当前CPU</el-radio>
                <el-radio label="mem">当前内存</el-radio>
                <el-radio label="peak_cpu">峰值CPU</el-radio>
                <el-radio label="peak_mem">峰值内存</el-radio>
                <el-radio label="pod">Pod数</el-radio>
              </el-radio-group>
            </el-form-item>
          </re-col>
        </template>
        <template v-if="!props.isScale && props.params">
          <!-- 重启对话框的调度到指定节点功能 -->
          <re-col :offset="2" :value="20" :xs="24" :sm="24">
            <div style="display: flex; gap: 20px; align-items: center">
              <label
                style="display: flex; align-items: center; cursor: pointer"
              >
                <el-checkbox
                  v-model="schedulerRef"
                  style="margin-right: 8px"
                  @change="handleSchedulerChange"
                />
                <span>调度到指定节点</span>
              </label>
            </div>
          </re-col>
          <!-- 调度到指定节点的资源类型选择 -->
          <re-col v-if="schedulerRef" :offset="2" :value="20" :xs="24" :sm="24">
            <el-form-item :label="'资源类型'" label-width="80px">
              <div style="display: flex; flex-wrap: wrap; gap: 12px">
                <label
                  style="
                    display: flex;
                    align-items: center;
                    font-size: 12px;
                    cursor: pointer;
                  "
                >
                  <input
                    type="radio"
                    name="resourceType"
                    value="cpu"
                    style="margin-right: 4px"
                    @change="
                      resourceTypeRef = 'cpu';
                      fetchNodeResources(
                        'cpu',
                        props.params?.namespace || '',
                        props.params?.deployment || ''
                      );
                    "
                  />
                  <span>当前CPU</span>
                </label>
                <label
                  style="
                    display: flex;
                    align-items: center;
                    font-size: 12px;
                    cursor: pointer;
                  "
                >
                  <input
                    type="radio"
                    name="resourceType"
                    value="mem"
                    style="margin-right: 4px"
                    @change="
                      resourceTypeRef = 'mem';
                      fetchNodeResources(
                        'mem',
                        props.params?.namespace || '',
                        props.params?.deployment || ''
                      );
                    "
                  />
                  <span>当前内存</span>
                </label>
                <label
                  style="
                    display: flex;
                    align-items: center;
                    font-size: 12px;
                    cursor: pointer;
                  "
                >
                  <input
                    type="radio"
                    name="resourceType"
                    value="peak_cpu"
                    style="margin-right: 4px"
                    @change="
                      resourceTypeRef = 'peak_cpu';
                      fetchNodeResources(
                        'peak_cpu',
                        props.params?.namespace || '',
                        props.params?.deployment || ''
                      );
                    "
                  />
                  <span>峰值CPU</span>
                </label>
                <label
                  style="
                    display: flex;
                    align-items: center;
                    font-size: 12px;
                    cursor: pointer;
                  "
                >
                  <input
                    type="radio"
                    name="resourceType"
                    value="peak_mem"
                    style="margin-right: 4px"
                    @change="
                      resourceTypeRef = 'peak_mem';
                      fetchNodeResources(
                        'peak_mem',
                        props.params?.namespace || '',
                        props.params?.deployment || ''
                      );
                    "
                  />
                  <span>峰值内存</span>
                </label>
                <label
                  style="
                    display: flex;
                    align-items: center;
                    font-size: 12px;
                    cursor: pointer;
                  "
                >
                  <input
                    type="radio"
                    name="resourceType"
                    value="pod"
                    style="margin-right: 4px"
                    @change="
                      resourceTypeRef = 'pod';
                      fetchNodeResources(
                        'pod',
                        props.params?.namespace || '',
                        props.params?.deployment || ''
                      );
                    "
                  />
                  <span>Pod数</span>
                </label>
              </div>
            </el-form-item>
          </re-col>
          <!-- 节点列表容器 -->
          <re-col v-if="schedulerRef" :offset="2" :value="20" :xs="24" :sm="24">
            <div
              id="nodeListContainer"
              style="
                display: none;
                max-height: 200px;
                padding: 8px;
                overflow-y: auto;
                border: 1px solid #e4e7ed;
                border-radius: 4px;
              "
            />
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
