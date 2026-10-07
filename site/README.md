# KubeDoor 官网（GitHub Pages）

纯静态页面（HTML + CSS + 原生 JS），无构建步骤、无外部 CDN 与字体（仅 GitHub Star 数会请求 api.github.com，3 秒超时、失败静默，结果缓存 6 小时）。

- 本地预览：直接用浏览器打开 `index.html`，或在本目录执行 `python -m http.server 8000` 后访问 <http://localhost:8000/>。
- 部署：推送到 `main` 且改动了 `site/**` 或 `.github/workflows/pages.yml` 时自动发布，也可在 Actions 页手动运行（workflow_dispatch）。
- 本地开发分支为 `master`，同步公开仓库时请推送到 `main`（工作流只监听 `main`，页面文档链接也指向 `main`）。
- 首次启用：仓库 Settings → Pages → Build and deployment → Source 选择 **GitHub Actions**。
- 工作流里的 action 固定到了提交 SHA（注释标明版本号），升级时同步修改 SHA 与注释。
- `404.html` 使用 `/KubeDoor/` 绝对路径（本地 `file://` 打开时会自动改成相对路径）；改用自定义域名或在本地 http.server 根目录预览时需同步修改。

## 上线前检查

页面里的文档链接都指向 `https://github.com/CassInfra/KubeDoor/blob/main/<路径>`，开启 Pages 前请确认这些文件已经推送到公开仓库的 `main`（help/、screenshot/、README.md、README.EN.md、site/、.github/ 在本地仓库中目前是未跟踪文件，记得先 `git add`；同时保留公开仓库里的 `LICENSE`）：

```bash
for p in deploy/README.md docs/ai-assistant.md docs/alert-silence.md docs/jvm-resource-control.md \
         help/FAQ.md help/K8S资源管控功能说明.md help/K8S事件告警规则配置说明.md help/K8S微服务镜像更新配置说明.md \
         README.EN.md LICENSE; do
  gh api "repos/CassInfra/KubeDoor/contents/$p" >/dev/null 2>&1 || echo "MISSING $p"
done
```

其它发布事项：

- 页面标注当前版本为 2.1.0，GitHub Releases 目前只到 1.7.0，建议补发 2.0.0 / 2.1.0 的 Release 与变更说明。
- 上线后把仓库 About 的简介改为新定位「AI 驱动的多 K8S 集群智能管控平台」，Website 填 <https://cassinfra.github.io/KubeDoor/>。
- 截图中的集群名、IP 等敏感信息做了模糊处理；替换截图时请先自行处理后再放进 `assets/img/shots/`。
