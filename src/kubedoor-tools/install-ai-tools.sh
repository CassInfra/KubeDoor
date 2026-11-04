#!/bin/sh
# 镜像构建脚本：由 kubedoor-agent 和 kubedoor-ai 的 Dockerfile 在构建时调用，
# 安装 AI 工具网关（kubedoor-tools）依赖的 kubectl、istioctl 和 yq，部署时无需手动执行。
# 版本可用 build arg 覆盖：KUBECTL_VERSION、KUBECTL_SHA256、ISTIO_VERSION、YQ_VERSION。
set -eu

arch="${TARGETARCH:-}"
if [ -z "$arch" ]; then
    case "$(uname -m)" in
        x86_64) arch=amd64 ;;
        aarch64) arch=arm64 ;;
        *) printf 'Unsupported architecture\n' >&2; exit 1 ;;
    esac
fi
case "$arch" in amd64|arm64) ;; *) exit 1 ;; esac

kubectl_version="${KUBECTL_VERSION:-v1.37.1}"
kubectl_sha256="${KUBECTL_SHA256:-}"
istio_version="${ISTIO_VERSION:-1.31.1}"
yq_version="${YQ_VERSION:-v4.54.1}"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
cd "$work"

# GitHub 社区镜像提供官方二进制的压缩包。固定默认版本的官方校验值，
# 构建无需访问 dl.k8s.io；自定义版本须通过 KUBECTL_SHA256 指定校验值。
if [ -z "$kubectl_sha256" ]; then
    case "${kubectl_version}:${arch}" in
        v1.37.1:amd64) kubectl_sha256=65691ff77eb6fa44c908b77a1082c9f092c3b9733b5cefabec0d1104890e21a8 ;;
        v1.37.1:arm64) kubectl_sha256=ff749f4b78d9c4f1ec87307df9b50119ed819e2094aa9810cb9acffc3286c8c7 ;;
        *)
            printf 'KUBECTL_SHA256 is required for kubectl %s (%s); pass the official checksum for this version and architecture.\n' "$kubectl_version" "$arch" >&2
            exit 1
            ;;
    esac
fi
case "$kubectl_sha256" in
    ''|*[!0-9a-fA-F]*) printf 'Invalid kubectl SHA256\n' >&2; exit 1 ;;
esac
if [ "${#kubectl_sha256}" -ne 64 ]; then
    printf 'Invalid kubectl SHA256\n' >&2
    exit 1
fi
curl -fsSL --retry 3 "https://github.com/ChampiYann/kubectl-binaries/releases/download/${kubectl_version}/kubectl-linux-${arch}.tar.gz" -o kubectl.tar.gz
tar -xzOf kubectl.tar.gz ./kubectl > kubectl
printf '%s  kubectl\n' "$kubectl_sha256" | sha256sum -c -
install -m 0755 kubectl /usr/local/bin/kubectl

curl -fsSL --retry 3 "https://github.com/istio/istio/releases/download/${istio_version}/istio-${istio_version}-linux-${arch}.tar.gz" -o istio.tar.gz
tar -xzf istio.tar.gz "istio-${istio_version}/bin/istioctl"
install -m 0755 "istio-${istio_version}/bin/istioctl" /usr/local/bin/istioctl
curl -fsSL --retry 3 "https://github.com/mikefarah/yq/releases/download/${yq_version}/yq_linux_${arch}" -o yq
install -m 0755 yq /usr/local/bin/yq
