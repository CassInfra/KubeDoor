FROM registry.cn-shenzhen.aliyuncs.com/starsl/node:24-bookworm-slim AS builder
WORKDIR /kubedoor-web
# 使用 Debian 构建环境，减少新版 Alpine 与旧构建机内核的兼容问题。
RUN npm config set registry https://registry.npmmirror.com --location=global && \
    npm install -g pnpm@10.34.6

# 单独缓存依赖，并区分依赖安装与前端编译的错误。
COPY package.json pnpm-lock.yaml .npmrc ./
RUN pnpm install --frozen-lockfile --reporter=append-only --registry=https://registry.npmmirror.com

COPY . .
RUN pnpm run build

# 官方 nginx 镜像(自带 auth_request 模块);nginx 配置由部署时的 ConfigMap nginx-config 挂载
FROM registry.cn-shenzhen.aliyuncs.com/starsl/nginx:alpine
WORKDIR /www
COPY --from=builder /kubedoor-web/dist dist/
