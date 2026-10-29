<h1>花折 - KubeDoor</h1>

## 介绍

KubeDoor 的 Web 控制台：Vue 3 + TypeScript + Element Plus + Pinia（基于 pure-admin）。

node版本 >= "^18.18.0 || ^20.9.0 || >=22.0.0"

## 命令

```bash
# 安装 pnpm
npm install -g pnpm

# 安装依赖
pnpm install

# 格式化
pnpm prettier --write .
pnpm prettier --write .\src\views\monit\index.vue

# 启动
pnpm dev
# 打包
pnpm build
npm run build
```

本地开发时 `/api` 按 `vite.config.ts` 的 `server.proxy` 转发（默认 `http://127.0.0.1:8080`），
可以先执行 `kubectl -n kubedoor port-forward svc/kubedoor-web 8080:80` 指向一套已部署的 KubeDoor。

## 登录页

`src/views/login/`：水墨月洞门背景 + 毛玻璃登录框，登录框放在月洞门的门洞里。

- 背景图是 `src/assets/login/login-bg-{1280,1920,2560}.webp`（16:9），按视口 cover 铺满；登录框的中心对准月洞门的圆心，整个框落在门环里面、不压到门框。窗口变小时门洞跟着变小，登录框也按背景的缩放比例一起缩（`index.vue` 的 `updateCardScale`，范围 0.74~1.15）；竖屏或窄屏时把月洞门移到屏幕正中，登录框保持原大小。
- 动效在 `utils/scene-fx.ts`（Canvas 2D）：湖面水波（随机泛起，鼠标划过、点击湖面、花瓣落水时也会泛起）、门环外缓慢转动的刻度光弧和向外扩散的圆波、门环上流动的光点、梅花飘落、莲花升起的萤光；登录成功时月洞门连续发出几道圆波。
- 动效坐标都是背景图在 1920x1080 下的像素位置。换背景图时，要同步改 `scene-fx.ts` 里的坐标和 `index.vue` 里的 `--gate-x` / `--gate-y`。
- 系统开启「减少动态效果」（`prefers-reduced-motion`）时不加载动效；标签页切到后台时暂停绘制。深色主题是夜景：背景压暗，光效改为叠加发光。
- 登录调用 master 的 `/api/auth/login`（先匹配本地账号，本地没有再走 LDAP），会话是 HttpOnly Cookie；登录前要访问的页面（包括 `/grafana/` 看板）由 `redirect` 参数带回。

## 账号与权限相关页面

- 「系统管理」（`src/views/system/`，仅系统管理员可见）：`users` 用户与权限（本地 / LDAP 账号、全局角色、按集群授权、重置口令、强制下线），`permissions` 权限与审批（每级集群权限能做哪些操作、AI 做这类操作前要不要批准、Pod 内的查看 / 高危命令清单），`ldap` LDAP 设置（含连接测试），`wecom-ai` 企微 AI 机器人，`ai-usage` AI 用量看板📊，`audit` 操作审计。
- 页面里取当前用户的权限用 `usePermission()`（`src/utils/usePermission.ts`）：`permissionOf(env)` 是对某个集群的有效权限（管理员 / 读写 / 只读 / 无），`can(op, env)` 判断能不能在该集群做「权限与审批」里的某种操作（按 `/api/auth/me` 返回的 `operations` 查表），`canGlobal(op)` 用于和具体集群无关的配置（按全局角色查表），改镜像、告警屏蔽另有 `canUpdateImage`、`canSilence`。判断逻辑是 `src/utils/permission.ts` 里的纯函数（单测 `tests/permission.test.cjs`，这个文件只能有 type import）。
- 集群下拉框旁的 `ClusterPermTag` 在「只读」集群上显示「只读」，在「管理员」集群上显示「管理员」；没有权限的按钮置灰。修改一律由 master 按集群和操作类型校验，前端判断只是体验优化。
- 修改口令是 `src/components/ChangePasswordDialog.vue`；账号被标记为必须改密（如内置超级管理员用出厂口令登录）时会强制弹出，改完才能继续使用。
