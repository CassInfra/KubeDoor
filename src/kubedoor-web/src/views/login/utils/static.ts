import bg1280 from "@/assets/login/login-bg-1280.webp";
import bg1920 from "@/assets/login/login-bg-1920.webp";
import bg2560 from "@/assets/login/login-bg-2560.webp";

/**
 * 登录页背景(16:9 水墨月洞门)。登录框和动效按图里元素的位置对齐,
 * 换图时要同步改 scene-fx.ts 里的坐标和 index.vue 里的 --gate-x / --gate-y。
 */
export const loginBg = {
  src: bg1920,
  srcset: `${bg1280} 1280w, ${bg1920} 1920w, ${bg2560} 2560w`
};
