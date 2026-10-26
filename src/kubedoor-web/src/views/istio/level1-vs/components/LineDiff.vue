<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { foldSame, sideBySide } from "../lineDiff";

defineOptions({ name: "IstioLineDiff" });

/** 左右对照的逐行对比(BCompare 风格):两边按行对齐,不同的行标红、行内不同的字加深,
 *  一边没有的行显示灰色占位;strike 标出要删掉的一边(划掉);
 *  给了 context 时,离不同的行超过 context 行的相同行折叠起来,点开再看 */
const props = withDefaults(
  defineProps<{
    left: string;
    right: string;
    leftTitle?: string;
    rightTitle?: string;
    strike?: "left" | "right" | "";
    maxHeight?: string;
    context?: number;
  }>(),
  {
    leftTitle: "",
    rightTitle: "",
    strike: "",
    maxHeight: "360px",
    context: undefined
  }
);

const SIDES = ["left", "right"] as const;
const rows = computed(() => sideBySide(props.left, props.right));
const opened = ref(new Set<number>());
watch(rows, () => (opened.value = new Set()));
const items = computed(() =>
  props.context === undefined
    ? rows.value.map((row, index) => ({ kind: "row" as const, row, index }))
    : foldSame(rows.value, props.context, opened.value)
);

function open(start: number) {
  opened.value = new Set([...opened.value, start]);
}
</script>

<template>
  <div class="line-diff" :style="{ maxHeight }">
    <template v-if="leftTitle || rightTitle">
      <div class="title">{{ leftTitle }}</div>
      <div class="title">{{ rightTitle }}</div>
    </template>
    <template
      v-for="item in items"
      :key="item.kind === 'fold' ? `fold-${item.start}` : item.index"
    >
      <div v-if="item.kind === 'fold'" class="fold" @click="open(item.start)">
        ⋯ 两边相同的 {{ item.count }} 行,点击展开 ⋯
      </div>
      <template v-else>
        <div
          v-for="side in SIDES"
          :key="side"
          :class="[
            'line',
            item.row[side] ? `is-${item.row.type}` : 'is-blank',
            { 'is-strike': strike === side }
          ]"
        >
          <span class="no">{{ item.row[side]?.no }}</span>
          <span class="text"
            ><span
              v-for="(part, i) in item.row[side]?.parts ?? []"
              :key="i"
              :class="{ mark: part.diff }"
              >{{ part.text }}</span
            ></span
          >
        </div>
      </template>
    </template>
    <template v-if="!rows.length">
      <div class="line is-blank"><span class="text">(无)</span></div>
      <div class="line is-blank"><span class="text">(无)</span></div>
    </template>
  </div>
</template>

<style scoped>
.line-diff {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  column-gap: 8px;
  overflow: auto;
  font-family: Consolas, Monaco, monospace;
  font-size: 12px;
  line-height: 18px;
}

.title {
  position: sticky;
  top: 0;
  z-index: 1;
  padding: 2px 8px;
  font-family: var(--el-font-family);
  font-weight: 600;
  background: var(--el-fill-color);
}

.line {
  display: flex;
  min-width: 0;
  background: var(--el-fill-color-lighter);
}

.no {
  flex: none;
  width: 34px;
  padding-right: 6px;
  color: var(--el-text-color-placeholder);
  text-align: right;
  user-select: none;
}

.text {
  flex: 1;
  min-width: 0;
  padding-right: 8px;
  word-break: break-all;
  white-space: pre-wrap;
}

/* 不同的行标红,行内不同的字加深;只有一边有的行,另一边是灰色斜纹占位 */
.is-changed,
.is-left,
.is-right {
  background: var(--el-color-danger-light-9);
}

.mark {
  background: var(--el-color-danger-light-7);
  border-radius: 2px;
}

.is-blank {
  background: repeating-linear-gradient(
    135deg,
    var(--el-fill-color-light) 0 4px,
    var(--el-fill-color) 4px 8px
  );
}

.is-strike .text {
  color: var(--el-text-color-secondary);
  text-decoration: line-through;
}

.fold {
  grid-column: 1 / -1;
  padding: 1px 8px;
  font-family: var(--el-font-family);
  color: var(--el-color-primary);
  text-align: center;
  cursor: pointer;
  background: var(--el-fill-color-light);
}

.fold:hover {
  background: var(--el-fill-color);
}
</style>
