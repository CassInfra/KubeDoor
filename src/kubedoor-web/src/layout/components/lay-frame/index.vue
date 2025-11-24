<script setup lang="ts">
import { getConfig } from "@/config";
import { useMultiFrame } from "@/layout/hooks/useMultiFrame";
import { useMultiTagsStoreHook } from "@/store/modules/multiTags";
import { type Component, shallowRef, watch, computed } from "vue";
import { type RouteRecordRaw, RouteLocationNormalizedLoaded } from "vue-router";

const props = defineProps<{
  currRoute: RouteLocationNormalizedLoaded;
  currComp: Component;
}>();

const compList = shallowRef([]);
const { setMap, getMap, MAP, delMap } = useMultiFrame();

const keep = computed(() => {
  return (
    getConfig().KeepAlive &&
    props.currRoute.meta?.keepAlive &&
    !!props.currRoute.meta?.frameSrc
  );
});
// 避免重新渲染 LayFrame
const normalComp = computed(() => !keep.value && props.currComp);

const viewKey = computed(() => {
  if (props.currRoute.meta?.usePathKey) {
    return props.currRoute.path;
  }
  return props.currRoute.fullPath;
});

watch(useMultiTagsStoreHook().multiTags, (tags: any) => {
  if (!Array.isArray(tags) || !keep.value) {
    return;
  }
  const iframeTags = tags.filter(i => i.meta?.frameSrc);
  // tags必须是小于MAP，才是做了关闭动作，因为MAP插入的顺序在tags变化后发生
  if (iframeTags.length < MAP.size) {
    for (const i of MAP.keys()) {
      if (!tags.some(s => s.path === i)) {
        delMap(i);
        compList.value = getMap();
      }
    }
  }
});

watch(
  () => viewKey.value,
  key => {
    const multiTags = useMultiTagsStoreHook().multiTags as RouteRecordRaw[];
    const iframeTags = multiTags.filter(i => i.meta?.frameSrc);
    if (keep.value) {
      if (iframeTags.length !== MAP.size) {
        const sameKey = [...MAP.keys()].find(i => key === i);
        if (!sameKey) {
          // 添加缓存
          setMap(key, props.currComp);
        }
      }
    }

    if (MAP.size > 0) {
      compList.value = getMap();
    }
  },
  {
    immediate: true
  }
);
</script>
<template>
  <template v-for="[fullPath, Comp] in compList" :key="fullPath">
    <div v-show="fullPath === viewKey" class="w-full h-full">
      <slot
        :fullPath="fullPath"
        :Comp="Comp"
        :frameInfo="{
          frameSrc: currRoute.meta?.frameSrc,
          fullPath: currRoute.fullPath
        }"
      />
    </div>
  </template>
  <div v-show="!keep" class="w-full h-full">
    <slot
      :Comp="normalComp"
      :fullPath="viewKey"
      :frameInfo="{
        frameSrc: currRoute.meta?.frameSrc,
        fullPath: currRoute.fullPath
      }"
    />
  </div>
</template>
