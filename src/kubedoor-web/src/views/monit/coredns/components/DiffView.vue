<script setup lang="ts">
import { computed } from "vue";
import { diffLines } from "../diff";

defineOptions({ name: "CoreDnsDiffView" });

/** 保存前确认框里展示改了哪些行(只用原生元素:ElMessageBox 里渲染时拿不到全局注册的组件) */
const props = withDefaults(
  defineProps<{
    before: string;
    after: string;
    context?: number;
    maxHeight?: string;
  }>(),
  { context: 2, maxHeight: "46vh" }
);

const result = computed(() =>
  diffLines(props.before, props.after, props.context)
);
const sign = { same: " ", add: "+", del: "-" };
</script>

<template>
  <div class="coredns-diff">
    <div class="coredns-diff__summary">
      <span class="is-add">+{{ result.added }}</span>
      <span class="is-del">-{{ result.removed }}</span>
      <span>行</span>
    </div>
    <div class="coredns-diff__body" :style="{ maxHeight }">
      <div v-if="!result.hunks.length" class="coredns-diff__empty">
        没有变化
      </div>
      <div v-for="(hunk, index) in result.hunks" :key="index">
        <div v-if="index > 0" class="coredns-diff__gap">⋯</div>
        <div
          v-for="(line, i) in hunk"
          :key="i"
          :class="['coredns-diff__line', `is-${line.type}`]"
        >
          <span class="coredns-diff__no">{{ line.oldNo || "" }}</span>
          <span class="coredns-diff__no">{{ line.newNo || "" }}</span>
          <span class="coredns-diff__sign">{{ sign[line.type] }}</span>
          <span class="coredns-diff__text">{{
            line.text.replace(/\r$/, "")
          }}</span>
        </div>
      </div>
    </div>
  </div>
</template>

<style lang="scss">
/* 二次确认框(confirm.ts 里的 ElMessageBox)挂在 body 下,样式不能 scoped */
.coredns-confirm {
  width: min(860px, 92vw);
  max-width: none;

  .coredns-confirm__content p {
    margin: 0 0 8px;
  }

  .coredns-confirm__notes {
    padding-left: 18px;
    margin: 0 0 8px;
    color: var(--el-color-warning);
  }

  .coredns-confirm__file + .coredns-confirm__file {
    margin-top: 12px;
  }

  .coredns-confirm__file-name {
    margin-bottom: 4px;
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-weight: 600;
  }
}
</style>

<style scoped>
.coredns-diff__summary {
  display: flex;
  gap: 8px;
  margin-bottom: 6px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.coredns-diff__summary .is-add {
  color: var(--el-color-success);
}

.coredns-diff__summary .is-del {
  color: var(--el-color-danger);
}

.coredns-diff__body {
  overflow: auto;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
  line-height: 20px;
  background: var(--el-fill-color-lighter);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 4px;
}

.coredns-diff__line {
  display: flex;
  min-width: max-content;
  white-space: pre;
}

.coredns-diff__line.is-add {
  background: var(--el-color-success-light-9);
}

.coredns-diff__line.is-del {
  background: var(--el-color-danger-light-9);
}

.coredns-diff__no {
  flex: none;
  width: 40px;
  padding-right: 6px;
  color: var(--el-text-color-placeholder);
  text-align: right;
  user-select: none;
}

.coredns-diff__sign {
  flex: none;
  width: 16px;
  text-align: center;
  user-select: none;
}

.is-add .coredns-diff__sign {
  color: var(--el-color-success);
}

.is-del .coredns-diff__sign {
  color: var(--el-color-danger);
}

.coredns-diff__text {
  padding-right: 12px;
}

.coredns-diff__gap,
.coredns-diff__empty {
  padding: 0 12px;
  color: var(--el-text-color-placeholder);
}
</style>
