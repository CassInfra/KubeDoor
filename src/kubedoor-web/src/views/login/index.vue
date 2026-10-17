<script setup lang="ts">
import { useI18n } from "vue-i18n";
import { useRoute, useRouter } from "vue-router";
import { message } from "@/utils/message";
import { useNav } from "@/layout/hooks/useNav";
import type { FormInstance, FormRules } from "element-plus";
import { useLayout } from "@/layout/hooks/useLayout";
import { useUserStoreHook } from "@/store/modules/user";
import { initRouter, getTopMenu } from "@/router/utils";
import { loginBg } from "./utils/static";
import { createSceneFx, REF_W, type SceneFx } from "./utils/scene-fx";
import { useRenderIcon } from "@/components/ReIcon/src/hooks";
import { ref, reactive, watch, onMounted, onBeforeUnmount } from "vue";
import { useTranslationLang } from "@/layout/hooks/useTranslationLang";
import { useDataThemeChange } from "@/layout/hooks/useDataThemeChange";

import dayIcon from "@/assets/svg/day.svg?component";
import darkIcon from "@/assets/svg/dark.svg?component";
import globalization from "@/assets/svg/globalization.svg?component";
import Lock from "@iconify-icons/ri/lock-fill";
import Check from "@iconify-icons/ep/check";
import User from "@iconify-icons/ri/user-3-fill";

defineOptions({
  name: "Login"
});
const router = useRouter();
const route = useRoute();
const loading = ref(false);
const leaving = ref(false);
const bgLoaded = ref(false);
const ruleFormRef = ref<FormInstance>();
const errorMessage = ref("");
const pageRef = ref<HTMLElement>();
const stageRef = ref<HTMLElement>();
const fxRef = ref<HTMLCanvasElement>();
let fx: SceneFx | null = null;
let resizeObserver: ResizeObserver | null = null;

const { initStorage } = useLayout();
initStorage();

const { t } = useI18n();
const { dataTheme, overallStyle, dataThemeChange } = useDataThemeChange();
dataThemeChange(overallStyle.value);
const { title, getDropdownItemStyle, getDropdownItemClass } = useNav();
const { locale, translationCh, translationEn } = useTranslationLang();

const ruleForm = reactive({
  username: "",
  password: "",
  remember: false
});

const rules: FormRules = {
  username: [{ required: true, message: "请输入账号", trigger: "blur" }],
  password: [{ required: true, message: "请输入口令", trigger: "blur" }]
};

/** 登录后去哪:优先回到登录前要访问的页面(含 Grafana 看板这类非前端路由) */
async function goAfterLogin() {
  const redirect =
    typeof route.query.redirect === "string" ? route.query.redirect : "";
  if (redirect.startsWith("/grafana/")) {
    window.location.href = redirect;
    return;
  }
  await initRouter();
  if (redirect.startsWith("/") && !redirect.startsWith("/login")) {
    await router.push(redirect);
  } else {
    await router.push(getTopMenu(true).path);
  }
}

const onLogin = async (formEl: FormInstance | undefined) => {
  if (!formEl || loading.value) return;
  const valid = await formEl.validate().catch(() => false);
  if (!valid) return;
  loading.value = true;
  errorMessage.value = "";
  try {
    const res = await useUserStoreHook().loginByUsername({
      username: ruleForm.username.trim(),
      password: ruleForm.password,
      remember: ruleForm.remember
    });
    if (!res?.success) {
      errorMessage.value = res?.message || t("login.pureLoginFail");
      return;
    }
    message(t("login.pureLoginSuccess"), { type: "success" });
    // 推门而入:月洞门泛起几道圆波,登录框淡出
    fx?.burst();
    leaving.value = true;
    await goAfterLogin();
  } catch (error: any) {
    leaving.value = false;
    const data = error?.response?.data;
    errorMessage.value =
      data?.message || data?.error || t("login.pureLoginFail");
    ruleForm.password = "";
  } finally {
    loading.value = false;
  }
};

function onThemeSwitch(dark: boolean) {
  dataThemeChange(dark ? "dark" : "light");
}

/** 背景上的点击 / 移动交给动效(湖面泛起水波),登录框和右上角工具栏上的不算 */
function onScenePointer(e: PointerEvent) {
  if (!fx) return;
  const target = e.target as Element | null;
  if (target?.closest(".login-card, .login-toolbar")) return;
  fx.pointer(e.clientX, e.clientY, e.type === "pointerdown");
}

/** 使用公共函数，避免`removeEventListener`失效 */
function onkeypress({ code }: KeyboardEvent) {
  if (["Enter", "NumpadEnter"].includes(code)) {
    onLogin(ruleFormRef.value);
  }
}

watch(dataTheme, dark => fx?.setDark(dark));

/**
 * 登录框跟着背景一起缩放:窗口变小时月洞门也变小,框不缩就会压到门环。
 * 缩放比例随背景走,但不小于 0.74(再小字就看不清了)、不大于 1.15。
 */
function updateCardScale() {
  const stage = stageRef.value;
  if (!stage) return;
  const k = stage.getBoundingClientRect().width / REF_W;
  const scale = Math.min(1.15, Math.max(0.74, k));
  pageRef.value?.style.setProperty("--login-scale", scale.toFixed(3));
}

onMounted(async () => {
  window.document.addEventListener("keypress", onkeypress);
  updateCardScale();
  if (pageRef.value) {
    resizeObserver = new ResizeObserver(() => {
      updateCardScale();
      fx?.resize();
    });
    resizeObserver.observe(pageRef.value);
  }
  const reduceMotion = window.matchMedia(
    "(prefers-reduced-motion: reduce)"
  ).matches;
  if (!reduceMotion && fxRef.value && stageRef.value) {
    fx = createSceneFx(fxRef.value, stageRef.value, dataTheme.value);
  }
  // 已经登录(比如从 Grafana 被重定向过来)就直接跳走
  try {
    await useUserStoreHook().fetchMe();
    await goAfterLogin();
  } catch {
    // 未登录,停留在登录页
  }
});

onBeforeUnmount(() => {
  window.document.removeEventListener("keypress", onkeypress);
  resizeObserver?.disconnect();
  fx?.destroy();
  fx = null;
});
</script>

<template>
  <div
    ref="pageRef"
    class="login-page select-none"
    :class="{ 'is-dark': dataTheme }"
    @pointerdown="onScenePointer"
    @pointermove="onScenePointer"
  >
    <div ref="stageRef" class="login-stage">
      <img
        class="login-stage__bg"
        :class="{ 'is-loaded': bgLoaded }"
        :src="loginBg.src"
        :srcset="loginBg.srcset"
        sizes="(min-aspect-ratio: 16/9) max(100vw, 1400px), max(177.78vh, 1400px)"
        alt=""
        draggable="false"
        fetchpriority="high"
        @load="bgLoaded = true"
      />
      <div class="login-stage__night" />

      <section
        class="login-card"
        :class="{ 'is-leaving': leaving }"
        aria-labelledby="login-title"
      >
        <header class="login-card__head" :style="{ '--i': 0 }">
          <p class="login-card__brand">{{ title }}</p>
          <h1 id="login-title" class="login-card__title">
            {{ t("login.pureLoginTitle") }}
          </h1>
        </header>

        <el-form
          ref="ruleFormRef"
          class="login-card__form"
          :model="ruleForm"
          :rules="rules"
          size="large"
          @submit.prevent
        >
          <el-form-item prop="username" :style="{ '--i': 1 }">
            <el-input
              v-model="ruleForm.username"
              clearable
              autocomplete="username"
              :placeholder="t('login.pureUsername')"
              :prefix-icon="useRenderIcon(User)"
            />
          </el-form-item>

          <el-form-item prop="password" :style="{ '--i': 2 }">
            <el-input
              v-model="ruleForm.password"
              clearable
              show-password
              autocomplete="current-password"
              :placeholder="t('login.purePassword')"
              :prefix-icon="useRenderIcon(Lock)"
            />
          </el-form-item>

          <div class="login-card__row" :style="{ '--i': 3 }">
            <el-checkbox v-model="ruleForm.remember">
              {{ t("login.pureRemember") }}
            </el-checkbox>
          </div>

          <p v-if="errorMessage" class="login-card__error" role="alert">
            {{ errorMessage }}
          </p>

          <el-button
            class="login-card__submit"
            :style="{ '--i': 4 }"
            type="primary"
            :loading="loading"
            @click="onLogin(ruleFormRef)"
          >
            {{ t("login.pureLogin") }}
          </el-button>
        </el-form>
      </section>
    </div>

    <canvas ref="fxRef" class="login-fx" aria-hidden="true" />

    <div class="login-toolbar">
      <!-- 国际化 -->
      <el-dropdown trigger="click">
        <globalization
          class="login-toolbar__icon"
          role="button"
          aria-label="语言 / Language"
        />
        <template #dropdown>
          <el-dropdown-menu class="translation">
            <el-dropdown-item
              :style="getDropdownItemStyle(locale, 'zh')"
              :class="['dark:!text-white', getDropdownItemClass(locale, 'zh')]"
              @click="translationCh"
            >
              <IconifyIconOffline
                v-show="locale === 'zh'"
                class="check-zh"
                :icon="Check"
              />
              简体中文
            </el-dropdown-item>
            <el-dropdown-item
              :style="getDropdownItemStyle(locale, 'en')"
              :class="['dark:!text-white', getDropdownItemClass(locale, 'en')]"
              @click="translationEn"
            >
              <span v-show="locale === 'en'" class="check-en">
                <IconifyIconOffline :icon="Check" />
              </span>
              English
            </el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
      <!-- 主题 -->
      <el-switch
        v-model="dataTheme"
        inline-prompt
        :active-icon="dayIcon"
        :inactive-icon="darkIcon"
        @change="onThemeSwitch"
      />
    </div>
  </div>
</template>

<style lang="scss" scoped>
@keyframes login-card-in {
  from {
    opacity: 0;
    transform: translate(-50%, calc(-50% + 18px)) scale(var(--card-scale));
  }
}

@keyframes login-item-in {
  from {
    opacity: 0;
    transform: translateY(10px);
  }
}

.login-page {
  position: fixed;
  inset: 0;
  overflow: hidden;
  color: #1e2a33;
  background: #e6d9c4;
  container-type: size;

  &.is-dark {
    color: #e3ebf2;
    background: #0b1219;
  }
}

/*
 * 背景按 16:9 cover 铺满:.login-stage 就是 cover 之后背景图的实际区域(可能大于视口),
 * 登录框的中心对准月洞门的圆心(--gate-x / --gate-y),整个框要落在门环里面、不压到门框;
 * 动效画布按同一区域对齐。窗口比 1400px 还窄时背景保持 1400px 宽、裁掉左右两边,
 * 这样门洞不会小到放不下登录框(框再缩小字就看不清了)。
 */
.login-stage {
  --stage-w: max(100vw, 100vh * 16 / 9, 1400px);
  --stage-h: calc(var(--stage-w) * 9 / 16);
  --gate-x: 64.65%;
  --gate-y: 50.28%;

  position: absolute;
  top: calc((100vh - var(--stage-h)) / 2);
  left: calc((100vw - var(--stage-w)) / 2);
  width: var(--stage-w);
  height: var(--stage-h);

  @supports (width: 1cqw) {
    --stage-w: max(100cqw, 100cqh * 16 / 9, 1400px);

    top: calc((100cqh - var(--stage-h)) / 2);

    /* 居中;视口偏窄时整体左移,保证门洞里的登录框不被右边裁掉 */
    left: max(
      100cqw - var(--stage-w),
      min(
        (100cqw - var(--stage-w)) / 2,
        100cqw - 200px - var(--stage-w) * 0.6465
      )
    );

    /* 竖屏 / 手机:把月洞门移到屏幕正中 */
    @media (aspect-ratio <= 1/1), (width <= 760px) {
      left: clamp(
        100cqw - var(--stage-w),
        50cqw - var(--stage-w) * 0.6465,
        0px
      );
    }
  }
}

.login-stage__bg {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  pointer-events: none;
  object-fit: cover;
  opacity: 0;
  transition:
    opacity 0.8s ease,
    filter 0.6s ease;

  &.is-loaded {
    opacity: 1;
  }

  .is-dark & {
    filter: brightness(0.5) saturate(0.85) contrast(1.08);
  }
}

/* 深色:夜色,中间留一圈月光让月洞门更亮 */
.login-stage__night {
  position: absolute;
  inset: 0;
  pointer-events: none;
  background: radial-gradient(
    circle at var(--gate-x) var(--gate-y),
    rgb(40 90 130 / 0%) 0,
    rgb(8 16 28 / 35%) 30%,
    rgb(5 10 18 / 55%) 100%
  );
  opacity: 0;
  transition: opacity 0.6s ease;

  .is-dark & {
    opacity: 1;
  }
}

.login-fx {
  position: absolute;
  inset: 0;
  z-index: 1;
  width: 100%;
  height: 100%;
  pointer-events: none;
}

/* 毛玻璃登录框:尽量通透,只靠模糊、细亮边和一点阴影跟背景区分 */
.login-card {
  /* --login-scale 由脚本按背景的缩放比例写入:窗口小了门洞跟着小,框也要跟着缩 */
  --card-scale: var(--login-scale, 1);

  position: absolute;
  top: var(--gate-y);
  left: var(--gate-x);
  z-index: 2;
  box-sizing: border-box;
  width: min(340px, 100vw - 32px);
  padding: 26px 28px 24px;
  user-select: text;
  background: linear-gradient(
    160deg,
    rgb(255 255 255 / 30%),
    rgb(255 255 255 / 8%)
  );
  backdrop-filter: blur(14px) saturate(135%);
  border: 1px solid rgb(255 255 255 / 55%);
  border-radius: 20px;
  box-shadow:
    0 18px 44px -20px rgb(30 42 51 / 45%),
    inset 0 1px 0 rgb(255 255 255 / 65%);
  transition:
    opacity 0.45s ease,
    transform 0.45s ease;
  transform: translate(-50%, -50%) scale(var(--card-scale));
  animation: login-card-in 0.9s cubic-bezier(0.2, 0.8, 0.2, 1) 0.15s backwards;

  /* 竖屏 / 手机上月洞门挪到了屏幕中间,框保持原大小 */
  @media (aspect-ratio <= 1/1), (width <= 760px) {
    --card-scale: 1;
  }

  @media (height <= 460px) {
    --card-scale: 0.84;
  }

  @media (prefers-reduced-motion: reduce) {
    animation: none;
  }

  &.is-leaving {
    opacity: 0;
    transform: translate(-50%, -50%) scale(calc(var(--card-scale) * 0.94));
  }

  .is-dark & {
    background: linear-gradient(
      160deg,
      rgb(22 34 48 / 45%),
      rgb(10 16 24 / 20%)
    );
    border-color: rgb(140 210 255 / 22%);
    box-shadow:
      0 18px 48px -18px rgb(0 0 0 / 70%),
      0 0 32px -12px rgb(92 206 245 / 35%),
      inset 0 1px 0 rgb(255 255 255 / 10%);
  }
}

/* 内容依次浮现 */
.login-card__head,
.login-card :deep(.el-form-item),
.login-card__row,
.login-card__submit {
  animation: login-item-in 0.7s cubic-bezier(0.2, 0.8, 0.2, 1) backwards;
  animation-delay: calc(0.35s + var(--i, 0) * 0.08s);

  @media (prefers-reduced-motion: reduce) {
    animation: none;
  }
}

.login-card__head {
  margin-bottom: 20px;
}

.login-card__brand {
  display: none;
  margin-bottom: 10px;
  font-size: 13px;
  letter-spacing: 1px;
  opacity: 0.7;

  /* 窄屏上看不到背景左边的标题,在登录框里补上 */
  @media (aspect-ratio <= 1/1), (width <= 760px) {
    display: block;
  }
}

.login-card__title {
  position: relative;
  padding-left: 12px;
  margin: 0;
  font-size: 20px;
  font-weight: 600;
  line-height: 28px;
  letter-spacing: 2px;

  /* 呼应背景里 KubeDoor 前那道青色竖线 */
  &::before {
    position: absolute;
    top: 5px;
    bottom: 5px;
    left: 0;
    width: 3px;
    content: "";
    background: linear-gradient(180deg, #7fe0ff, #2a93d0);
    border-radius: 2px;
    box-shadow: 0 0 8px rgb(92 206 245 / 70%);
  }
}

.login-card :deep(.el-form-item) {
  margin-bottom: 18px;
}

.login-card :deep(.el-input) {
  --el-input-bg-color: rgb(255 255 255 / 38%);
  --el-input-border-color: rgb(38 52 64 / 16%);
  --el-input-hover-border-color: rgb(38 52 64 / 34%);
  --el-input-focus-border-color: #2f9bd6;
  --el-input-text-color: #1e2a33;
  --el-input-placeholder-color: rgb(30 42 51 / 45%);
  --el-input-icon-color: rgb(30 42 51 / 50%);
  --el-input-clear-hover-color: #1e2a33;
  --el-input-border-radius: 10px;

  .el-input__wrapper {
    transition:
      box-shadow 0.25s ease,
      background-color 0.25s ease;
  }

  .el-input__wrapper.is-focus {
    background-color: rgb(255 255 255 / 55%);
    box-shadow:
      0 0 0 1px var(--el-input-focus-border-color) inset,
      0 0 0 3px rgb(47 155 214 / 16%);
  }

  /* 浏览器自动填充时不要盖上一层不透明的底色 */
  input:-webkit-autofill {
    -webkit-text-fill-color: var(--el-input-text-color);
    transition: background-color 600000s 0s;
  }
}

.login-page.is-dark .login-card :deep(.el-input) {
  --el-input-bg-color: rgb(8 14 22 / 38%);
  --el-input-border-color: rgb(160 205 235 / 18%);
  --el-input-hover-border-color: rgb(160 205 235 / 34%);
  --el-input-focus-border-color: #5ccef5;
  --el-input-text-color: #e3ebf2;
  --el-input-placeholder-color: rgb(227 235 242 / 45%);
  --el-input-icon-color: rgb(227 235 242 / 55%);
  --el-input-clear-hover-color: #e3ebf2;

  .el-input__wrapper.is-focus {
    background-color: rgb(8 14 22 / 55%);
    box-shadow:
      0 0 0 1px var(--el-input-focus-border-color) inset,
      0 0 0 3px rgb(92 206 245 / 18%);
  }
}

.login-card__row {
  display: flex;
  align-items: center;
  margin: -4px 0 14px;

  :deep(.el-checkbox) {
    --el-checkbox-text-color: currentcolor;
    --el-checkbox-checked-text-color: currentcolor;
    --el-checkbox-bg-color: rgb(255 255 255 / 40%);
    --el-checkbox-input-border-color: rgb(38 52 64 / 30%);
    --el-checkbox-input-border-color-hover: #2f9bd6;
    --el-checkbox-checked-bg-color: #2b3a47;
    --el-checkbox-checked-input-border-color: #2b3a47;

    height: 22px;
    opacity: 0.85;
  }
}

.login-page.is-dark .login-card__row :deep(.el-checkbox) {
  --el-checkbox-bg-color: rgb(8 14 22 / 40%);
  --el-checkbox-input-border-color: rgb(160 205 235 / 35%);
  --el-checkbox-checked-bg-color: #2a93d0;
  --el-checkbox-checked-input-border-color: #2a93d0;
}

.login-card__error {
  padding: 7px 10px;
  margin: -4px 0 14px;
  font-size: 13px;
  line-height: 18px;
  color: #b42318;
  background: rgb(220 60 50 / 10%);
  border: 1px solid rgb(220 60 50 / 25%);
  border-radius: 8px;

  .is-dark & {
    color: #ffb4a8;
    background: rgb(220 60 50 / 16%);
    border-color: rgb(255 120 100 / 30%);
  }
}

/* 墨色按钮,悬停时泛起门环那样的青光 */
.login-card__submit {
  --el-button-text-color: #f4efe6;
  --el-button-hover-text-color: #fff;
  --el-button-active-text-color: #fff;
  --el-button-bg-color: #23303c;
  --el-button-hover-bg-color: #23303c;
  --el-button-active-bg-color: #1a242e;
  --el-button-border-color: transparent;
  --el-button-hover-border-color: transparent;
  --el-button-active-border-color: transparent;

  position: relative;
  width: 100%;
  height: 42px;
  overflow: hidden;
  font-size: 15px;
  letter-spacing: 6px;
  background-image: linear-gradient(135deg, #34485a, #17212b);
  border-radius: 10px;
  box-shadow: 0 8px 20px -10px rgb(23 33 43 / 70%);
  transition:
    box-shadow 0.3s ease,
    transform 0.2s ease;

  /* 掠过的一道光 */
  &::after {
    position: absolute;
    inset: 0;
    pointer-events: none;
    content: "";
    background: linear-gradient(
      105deg,
      transparent 35%,
      rgb(160 235 255 / 35%) 50%,
      transparent 65%
    );
    transform: translateX(-120%);
  }

  &:hover,
  &:focus-visible {
    box-shadow:
      0 0 0 1px rgb(92 206 245 / 55%),
      0 10px 26px -8px rgb(47 155 214 / 70%);

    &::after {
      transition: transform 0.9s ease;
      transform: translateX(120%);
    }
  }

  &:active {
    transform: translateY(1px);
  }

  .is-dark & {
    --el-button-bg-color: #1d6f9f;
    --el-button-hover-bg-color: #1d6f9f;
    --el-button-active-bg-color: #175b83;

    background-image: linear-gradient(135deg, #2a9bd6, #155a85);
  }
}

.login-toolbar {
  position: absolute;
  top: 14px;
  right: 18px;
  z-index: 3;
  display: flex;
  gap: 14px;
  align-items: center;
  padding: 6px 12px;
  background: rgb(255 255 255 / 22%);
  backdrop-filter: blur(10px);
  border: 1px solid rgb(255 255 255 / 45%);
  border-radius: 999px;

  .is-dark & {
    background: rgb(10 16 24 / 35%);
    border-color: rgb(140 210 255 / 18%);
  }
}

.login-toolbar__icon {
  width: 20px;
  height: 20px;
  cursor: pointer;
  outline: none;
  transition: color 0.3s ease;

  &:hover {
    color: #2a93d0;
  }
}

:deep(.el-input-group__append, .el-input-group__prepend) {
  padding: 0;
}

.translation {
  ::v-deep(.el-dropdown-menu__item) {
    padding: 5px 40px;
  }

  .check-zh {
    position: absolute;
    left: 20px;
  }

  .check-en {
    position: absolute;
    left: 20px;
  }
}
</style>
