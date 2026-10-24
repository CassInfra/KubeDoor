export default {
  path: "/about",
  name: "about",
  // 直接嵌入官网（public/site，与 GitHub Pages 同一份文件，构建时原样复制到 dist/site）
  component: () => import("@/layout/frame.vue"),
  meta: {
    title: "关于",
    icon: "ep:info-filled",
    // 要写全 index.html：dev 下访问目录 /site/ 会被 Vite 回退成控制台首页
    frameSrc: "/site/index.html",
    rank: 999, // 设置为最大值，确保显示在菜单最后
    showLink: true
  }
} satisfies RouteConfigsTable;
