<script setup lang="ts">
import { ref } from "vue";
import type { AIMemoryAttachment } from "@/api/ai";
import AIMarkdown from "./AIMarkdown.vue";

const props = defineProps<{
  memories: AIMemoryAttachment[];
  secret?: string;
}>();
const expanded = ref(false);
const redact = (text: string) =>
  props.secret ? text.split(props.secret).join("[已隐藏]") : text;
function toggle(event: Event) {
  expanded.value = (event.target as HTMLDetailsElement).open;
}
</script>

<template>
  <details class="memory-attachments" @toggle="toggle">
    <summary>
      本轮引入记忆（{{ memories.length }}）<span>发送时的版本</span>
    </summary>
    <template v-if="expanded"
      ><section
        v-for="memory in memories"
        :key="memory.id"
        class="memory-attachment"
      >
        <div class="memory-attachment-heading">
          <strong>{{ redact(memory.title) }}</strong
          ><span>v{{ memory.version }}</span>
        </div>
        <AIMarkdown :content="redact(memory.content)" /></section
    ></template>
  </details>
</template>

<style scoped lang="scss">
.memory-attachments {
  margin: 12px 0;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
}

.memory-attachments > summary {
  padding: 10px 12px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
  cursor: pointer;
}

.memory-attachments > summary span {
  margin-left: 10px;
  font-size: 11px;
}

.memory-attachment {
  padding: 12px;
  border-top: 1px solid var(--el-border-color-lighter);
}

.memory-attachment-heading {
  display: flex;
  gap: 10px;
  margin-bottom: 10px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.memory-attachment-heading strong {
  overflow-wrap: anywhere;
}
</style>
