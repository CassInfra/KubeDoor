# KubeDoor 官网（GitHub Pages）

纯静态页面（HTML + CSS + 原生 JS），无构建步骤、无外部 CDN 与字体（仅 GitHub Star 数会请求 api.github.com，3 秒超时、失败静默，结果缓存 6 小时）。

- 本地预览：直接用浏览器打开 `index.html`，或在本目录执行 `python -m http.server 8000` 后访问 <http://localhost:8000/>。
- 部署：推送到 `main` 且改动了 `src/kubedoor-web/public/site/**` 或 `.github/workflows/pages.yml` 时自动发布，也可在 Actions 页手动运行（workflow_dispatch）。
- 本地开发分支为 `master`，同步公开仓库时请推送到 `main`（工作流只监听 `main`，页面文档链接也指向 `main`）。
- 首次启用：仓库 Settings → Pages → Build and deployment → Source 选择 **GitHub Actions**。
- 工作流里的 action 固定到了提交 SHA（注释标明版本号），升级时同步修改 SHA 与注释。
- `404.html` 使用 `/KubeDoor/` 绝对路径（本地 `file://` 打开时会自动改成相对路径）；改用自定义域名或在本地 http.server 根目录预览时需同步修改。

## 同时是 Web 控制台的「关于」页

本目录在 `src/kubedoor-web/public/site/`，Vite 构建时原样复制到 `dist/site/`，`/about` 路由用 iframe 嵌入 `/site/index.html`（`404.html` 只给 Pages 用）。因此：

- `index.html` 引用的资源一律用相对路径（Pages 上在 `/KubeDoor/` 下，控制台里在 `/site/` 下）。
- 外链保持 `target="_blank"`，否则在控制台里点击会把 iframe 整个带走。
- 改了官网，下一版 web 镜像里的「关于」页也会跟着变。

## 上线前检查

页面里的文档链接都指向 `https://github.com/CassInfra/KubeDoor/blob/main/<路径>`，开启 Pages 前请确认这些文件已经推送到公开仓库的 `main`（help/、screenshot/、README.md、README.EN.md、src/kubedoor-web/public/site/、.github/ 在本地仓库中目前是未跟踪文件，记得先 `git add`；同时保留公开仓库里的 `LICENSE`）：

```bash
for p in deploy/README.md docs/ai-assistant.md docs/alert-silence.md docs/jvm-resource-control.md \
         help/FAQ.md help/K8S资源管控功能说明.md help/K8S事件告警规则配置说明.md help/K8S微服务镜像更新配置说明.md \
         README.EN.md LICENSE; do
  gh api "repos/CassInfra/KubeDoor/contents/$p" >/dev/null 2>&1 || echo "MISSING $p"
done
```

其它发布事项：

- 时间线里的当前版本（`tl-current`，目前为 2.3.0）、首屏的版本标签（`hero-pill`）要和 README 的升级说明、最新的 GitHub Release 一起更新。
- 上线后把仓库 About 的简介改为新定位「AI 驱动的多 K8S 集群智能管控平台」，Website 填 <https://cassinfra.github.io/KubeDoor/>。
- 截图中的集群名、IP 等敏感信息做了模糊处理；替换截图时请先自行处理后再放进 `assets/img/shots/`。
- 「账号与权限」展示带里的 `assets/img/shots/login.webp` 是 Web 登录页（`src/kubedoor-web/src/views/login/`）的截图，登录页改版后记得同步替换。
