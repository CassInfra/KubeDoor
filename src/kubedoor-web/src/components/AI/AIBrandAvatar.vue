<script setup lang="ts">
import RobotIcon from "@iconify-icons/ri/robot-2-line";
import { computed } from "vue";
import { IconifyIconOffline } from "@/components/ReIcon";

/**
 * 花折 AI 的头像：和顶栏「AI 助手」按钮同一套渐变、机器人图标和光泽，带轻微动效
 * （渐变流动、光泽扫过、机器人轻微浮动、角上的星光闪烁；系统设置减少动态效果时都关掉）。
 */
const props = withDefaults(defineProps<{ size?: number }>(), { size: 32 });
const icon = computed(() => String(Math.round(props.size * 0.6)));
</script>

<template>
  <span
    class="ai-brand-avatar"
    :style="{ '--avatar-size': `${size}px` }"
    aria-hidden="true"
  >
    <span class="avatar-sheen" />
    <IconifyIconOffline
      class="avatar-robot"
      :icon="RobotIcon"
      :width="icon"
      :height="icon"
    />
    <span class="avatar-spark">✦</span>
  </span>
</template>

<style lang="scss" scoped>
.ai-brand-avatar {
  position: relative;
  display: inline-grid;
  flex-shrink: 0;
  place-items: center;
  width: var(--avatar-size);
  height: var(--avatar-size);
  overflow: hidden;
  color: #fff;
  background: linear-gradient(
    135deg,
    #075bc6,
    #6341ce 45%,
    #b92789 80%,
    #ba315f
  );
  background-size: 220% 220%;
  isolation: isolate;
  border-radius: calc(var(--avatar-size) * 0.3);
  box-shadow:
    0 4px 12px rgb(92 61 201 / 30%),
    inset 0 1px 0 rgb(255 255 255 / 25%);
  animation: avatar-flow 8s ease-in-out infinite;
}

.avatar-robot {
  position: relative;
  filter: drop-shadow(0 1px 1px rgb(24 18 68 / 30%));
  animation: avatar-float 3.6s ease-in-out infinite;
}

.avatar-sheen {
  position: absolute;
  top: -20%;
  left: -60%;
  width: 40%;
  height: 140%;
  pointer-events: none;
  background: linear-gradient(
    90deg,
    transparent,
    rgb(255 255 255 / 35%),
    transparent
  );
  transform: skewX(-22deg);
  animation: avatar-sheen 5s ease-in-out infinite;
}

.avatar-spark {
  position: absolute;
  top: 1px;
  right: 3px;
  font-size: calc(var(--avatar-size) * 0.24);
  line-height: 1;
  color: #e1faff;
  animation: avatar-twinkle 3.2s ease-in-out infinite;
}

@keyframes avatar-flow {
  0%,
  100% {
    background-position: 0% 50%;
  }

  50% {
    background-position: 100% 50%;
  }
}

@keyframes avatar-float {
  0%,
  100% {
    transform: translateY(1px) rotate(-4deg);
  }

  50% {
    transform: translateY(-1px) rotate(4deg);
  }
}

@keyframes avatar-sheen {
  0%,
  60% {
    transform: translateX(0) skewX(-22deg);
  }

  100% {
    transform: translateX(560%) skewX(-22deg);
  }
}

@keyframes avatar-twinkle {
  0%,
  100% {
    opacity: 0.35;
    transform: scale(0.75);
  }

  50% {
    opacity: 1;
    transform: scale(1.15);
  }
}

@media (prefers-reduced-motion: reduce) {
  .ai-brand-avatar,
  .avatar-robot,
  .avatar-sheen,
  .avatar-spark {
    animation: none;
  }
}
</style>
