#!/bin/bash
# 用法: ./build.sh [master|agent|web|alarm|ai] <版本号>
# 只传版本号时依次构建全部组件

ALL_COMPONENTS="master agent web alarm ai"

# ----------------------------
# 1. 接收参数
# ----------------------------
case $# in
    1) COMPONENTS=$ALL_COMPONENTS; VERSION=$1 ;;
    2) COMPONENTS=$1; VERSION=$2 ;;
    *) COMPONENTS=""; VERSION="" ;;
esac
# 版本号不能是组件名:防止漏写版本号(./build.sh master)时被当成版本号,把全部组件打成 :master 推上去
case " $ALL_COMPONENTS " in
    *" $VERSION "*) VERSION="" ;;
esac

if [ -z "$COMPONENTS" ] || [ -z "$VERSION" ]; then
    echo "❌ 错误：参数不正确"
    echo "用法: $0 [组件后缀] <版本号>"
    echo "示例: $0 master 1.5.0    # 只构建 master"
    echo "      $0 1.5.0           # 依次构建全部组件: $ALL_COMPONENTS"
    exit 1
fi

echo "🚀 开始构建组件: $COMPONENTS 版本: $VERSION"

# ----------------------------
# 2. 更新代码
# ----------------------------
# 以脚本所在目录(仓库根目录)为准,不依赖仓库放在哪、从哪个目录执行
cd "$(dirname "$0")" || exit 1
echo "➡️ 执行 git pull ..."
git pull
if [ $? -ne 0 ]; then
    echo "❌ git pull 失败"
    exit 1
fi

# ----------------------------
# 3. 构建并推送 Docker 镜像
# ----------------------------
# Docker 29+ 默认的 containerd 镜像存储会让 buildx 自动附加 provenance,镜像变成 OCI image index,
# 华为云 SWR 基础版拒收(push 报 "Invalid image, fail to parse 'manifest.json'")。
# 关掉后输出 Docker v2 单 manifest;用环境变量而非 --provenance=false,旧版 legacy builder 也兼容。
export BUILDX_NO_DEFAULT_ATTESTATIONS=1

build_and_push() {
    local component=$1
    local component_dir="src/kubedoor-${component}"
    local image="swr.cn-south-1.myhuaweicloud.com/starsl.cn/kubedoor-${component}:${VERSION}"

    if [ ! -f "$component_dir/Dockerfile" ]; then
        echo "❌ 找不到 Dockerfile: $PWD/$component_dir/Dockerfile"
        return 1
    fi

    echo "➡️ 构建 Docker 镜像: $image"
    if [ "$component" = "agent" ] || [ "$component" = "ai" ]; then
        # Agent 和 AI 镜像从仓库根目录安装共享工具库和 CLI。
        docker build -f "$component_dir/Dockerfile" -t "$image" .
    else
        docker build -t "$image" "$component_dir"
    fi
    if [ $? -ne 0 ]; then
        echo "❌ Docker 构建失败: $image"
        return 1
    fi

    echo "➡️ 推送 Docker 镜像: $image"
    docker push "$image"
    if [ $? -ne 0 ]; then
        echo "❌ Docker 推送失败: $image"
        return 1
    fi

    echo "✅ 镜像 $image 构建并推送完成"
}

# 某个组件失败不中断后面的组件,最后汇总
SUCCEEDED=""
FAILED=""
for COMPONENT in $COMPONENTS; do
    if build_and_push "$COMPONENT"; then
        SUCCEEDED="$SUCCEEDED $COMPONENT"
    else
        FAILED="$FAILED $COMPONENT"
    fi
done

# ----------------------------
# 4. 汇总
# ----------------------------
echo "----------------------------"
[ -n "$SUCCEEDED" ] && echo "✅ 成功:$SUCCEEDED (版本 $VERSION)"
if [ -n "$FAILED" ]; then
    echo "❌ 失败:$FAILED"
    exit 1
fi
