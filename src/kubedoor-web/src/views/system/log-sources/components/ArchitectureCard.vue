<script setup lang="ts">
// 「网络架构说明」卡片：全局一段 Markdown，告诉 AI 请求怎么流转、各层对应哪个日志源
import { ElMessageBox } from "element-plus";
import MarkdownEditor from "./MarkdownEditor.vue";
import {
  type LogSourcesForm,
  ARCHITECTURE_MAX_LENGTH,
  architectureTemplate
} from "../log-sources-utils";

defineOptions({ name: "LogArchitectureCard" });

const form = defineModel<LogSourcesForm>("form", { required: true });

/** 插入示例：空的直接填，有内容时确认后追加到末尾 */
async function insertTemplate() {
  const template = architectureTemplate(form.value);
  const current = form.value.architecture.trim();
  if (!current) {
    form.value.architecture = template;
    return;
  }
  try {
    await ElMessageBox.confirm(
      "已经写了内容，示例会追加到末尾，改成实际的情况后再保存",
      "插入示例",
      { type: "info", confirmButtonText: "追加", cancelButtonText: "取消" }
    );
  } catch {
    return;
  }
  form.value.architecture = `${current}\n\n${template}`;
}
</script>

<template>
  <el-card shadow="never" class="log-card">
    <template #header><strong>网络架构说明</strong></template>
    <p class="card-note">
      告诉 AI 请求怎么流转（入口 → 网关 → 服务）、每一层对应哪个日志源（写日志源
      ID）、哪些 ID 能把各层串起来、各集群有什么不同。AI
      查历史日志前会先看这段说明，排查「接口 504
      是哪一层超时」这类问题时按它逐层查。
    </p>
    <MarkdownEditor
      v-model="form.architecture"
      :rows="12"
      :maxlength="ARCHITECTURE_MAX_LENGTH"
      placeholder="例如：&#10;- 入口：客户端 → 入口网关（日志源 ingress）→ 各服务 Pod（日志源 app）&#10;- 入口网关生成 request_id，一路透传到服务日志&#10;- 测试环境没有入口网关"
    >
      <template #actions>
        <el-button size="small" @click="insertTemplate">插入示例</el-button>
      </template>
    </MarkdownEditor>
  </el-card>
</template>
