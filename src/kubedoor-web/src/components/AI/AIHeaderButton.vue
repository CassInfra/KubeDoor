<script setup lang="ts">
import RobotIcon from "@iconify-icons/ri/robot-2-line";
import { IconifyIconOffline } from "@/components/ReIcon";

defineProps<{ expanded: boolean }>();
const emit = defineEmits<{ open: [] }>();
</script>

<template>
  <button
    type="button"
    class="ai-header-button"
    title="打开 AI 助手"
    aria-label="打开 AI 助手"
    aria-haspopup="dialog"
    :aria-expanded="expanded"
    @click="emit('open')"
  >
    <span class="ai-header-effects" aria-hidden="true">
      <span class="ai-header-sheen" />
      <span class="ai-header-spark">✦</span>
    </span>
    <span class="ai-header-robot" aria-hidden="true">
      <IconifyIconOffline :icon="RobotIcon" width="22" height="22" />
    </span>
    <span class="ai-header-label">AI 助手</span>
  </button>
</template>

<style lang="scss" scoped>
.ai-header-button {
  position: absolute;
  top: 7px;
  left: 50%;
  z-index: 1;
  display: inline-flex;
  gap: 8px;
  align-items: center;
  justify-content: center;
  height: 34px;
  padding: 0 16px 0 10px;
  font-size: 13px;
  font-weight: 600;
  color: #fff;
  white-space: nowrap;
  cursor: pointer;
  background: linear-gradient(
    115deg,
    #075bc6,
    #6341ce 42%,
    #b92789 76%,
    #ba315f
  );
  background-size: 240% 240%;
  isolation: isolate;
  border: 1px solid rgb(255 255 255 / 35%);
  border-radius: 999px;
  box-shadow:
    0 4px 12px rgb(92 61 201 / 25%),
    inset 0 1px 0 rgb(255 255 255 / 20%);
  transition:
    transform 200ms ease,
    box-shadow 200ms ease;
  transform: translateX(-50%);
  animation: ai-color-flow 9s ease-in-out infinite;

  &::before {
    position: absolute;
    inset: -3px;
    z-index: -1;
    pointer-events: none;
    content: "";
    background: linear-gradient(110deg, #22b9e8, #8b5cf6 50%, #ed5cba);
    filter: blur(7px);
    border-radius: inherit;
    opacity: 0.3;
    animation: ai-halo-pulse 4.5s ease-in-out infinite;
  }

  &:hover {
    box-shadow:
      0 6px 18px rgb(92 61 201 / 38%),
      inset 0 1px 0 rgb(255 255 255 / 30%);
    transform: translate(-50%, -1px) scale(1.03);
  }

  &:active {
    transform: translateX(-50%) scale(0.97);
  }

  &:focus-visible {
    outline: 2px solid var(--el-color-primary);
    outline-offset: 4px;
  }
}

.ai-header-effects {
  position: absolute;
  inset: 0;
  overflow: hidden;
  pointer-events: none;
  border-radius: inherit;
}

.ai-header-sheen {
  position: absolute;
  top: -20%;
  left: -50%;
  width: 35%;
  height: 140%;
  background: linear-gradient(
    90deg,
    transparent,
    rgb(255 255 255 / 28%),
    transparent
  );
  transform: skewX(-22deg);
  animation: ai-sheen-sweep 6s ease-in-out infinite;
}

.ai-header-spark {
  position: absolute;
  top: 3px;
  right: 7px;
  font-size: 8px;
  color: #e1faff;
  opacity: 0.75;
  animation: ai-spark-twinkle 3.2s ease-in-out infinite;
}

.ai-header-robot {
  position: relative;
  display: inline-flex;
  flex-shrink: 0;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  color: #fff;
  background: rgb(255 255 255 / 14%);
  border: 1px solid rgb(255 255 255 / 24%);
  border-radius: 9px;
  box-shadow: 0 3px 6px rgb(28 27 91 / 22%);
  animation: ai-robot-float 3.6s ease-in-out infinite;
}

.ai-header-label {
  position: relative;
  text-shadow: 0 1px 2px rgb(24 18 68 / 20%);
  letter-spacing: 0.5px;
}

@keyframes ai-color-flow {
  0%,
  100% {
    background-position: 0% 50%;
  }

  50% {
    background-position: 100% 50%;
  }
}

@keyframes ai-halo-pulse {
  0%,
  100% {
    opacity: 0.25;
  }

  50% {
    opacity: 0.5;
  }
}

@keyframes ai-sheen-sweep {
  0%,
  65% {
    transform: translateX(0) skewX(-22deg);
  }

  100% {
    transform: translateX(500%) skewX(-22deg);
  }
}

@keyframes ai-spark-twinkle {
  0%,
  100% {
    opacity: 0.4;
    transform: scale(0.8);
  }

  50% {
    opacity: 1;
    transform: scale(1.15);
  }
}

@keyframes ai-robot-float {
  0%,
  100% {
    transform: translateY(1px) rotate(-3deg);
  }

  50% {
    transform: translateY(-2px) rotate(3deg);
  }
}

@media (prefers-reduced-motion: reduce) {
  .ai-header-button,
  .ai-header-button::before,
  .ai-header-sheen,
  .ai-header-spark,
  .ai-header-robot {
    transition: none;
    animation: none;
  }
}
</style>
