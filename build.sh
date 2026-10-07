#!/bin/bash
# 用法: ./build.sh <master|agent|web|alarm|ai> <版本号>

# ----------------------------
# 1. 接收参数
# ----------------------------
COMPONENT_SUFFIX=$1
VERSION=$2

if [ -z "$COMPONENT_SUFFIX" ] || [ -z "$VERSION" ]; then
    echo "❌ 错误：缺少必要参数"
    echo "用法: $0 <组件后缀> <版本号>"
    echo "示例: $0 master 1.5.0"
    exit 1
fi

# 镜像地址
IMAGE="swr.cn-south-1.myhuaweicloud.com/starsl.cn/kubedoor-${COMPONENT_SUFFIX}:${VERSION}"

echo "🚀 开始构建组件: kubedoor-$COMPONENT_SUFFIX 版本: $VERSION"
echo "镜像标签: $IMAGE"

# ----------------------------
# 2. 更新代码
# ----------------------------
echo "➡️ 执行 git pull ..."
git pull
if [ $? -ne 0 ]; then
    echo "❌ git pull 失败"
    exit 1
fi
cd "/root/kubedoor/src/kubedoor-${COMPONENT_SUFFIX}"
if [ $? -ne 0 ]; then
    echo "❌ 无法切换到目录: /root/kubedoor/src/kubedoor-${COMPONENT_SUFFIX}"
    exit 1
fi
# ----------------------------
# 3. 构建 Docker 镜像
# ----------------------------
echo "➡️ 构建 Docker 镜像 ..."
# Docker 29+ 默认的 containerd 镜像存储会让 buildx 自动附加 provenance,镜像变成 OCI image index,
# 华为云 SWR 基础版拒收(push 报 "Invalid image, fail to parse 'manifest.json'")。
# 关掉后输出 Docker v2 单 manifest;用环境变量而非 --provenance=false,旧版 legacy builder 也兼容。
export BUILDX_NO_DEFAULT_ATTESTATIONS=1
if [ "$COMPONENT_SUFFIX" = "agent" ] || [ "$COMPONENT_SUFFIX" = "ai" ]; then
    # Agent 和 AI 镜像从仓库根目录安装共享工具库和 CLI。
    docker build -f "Dockerfile" -t "$IMAGE" /root/kubedoor
else
    docker build -t "$IMAGE" .
fi
if [ $? -ne 0 ]; then
    echo "❌ Docker 构建失败"
    exit 1
fi

# ----------------------------
# 4. 推送 Docker 镜像
# ----------------------------
echo "➡️ 推送 Docker 镜像 ..."
docker push "$IMAGE"
if [ $? -ne 0 ]; then
    echo "❌ Docker 推送失败"
    exit 1
fi

echo "✅ 镜像 $IMAGE 构建并推送完成"
