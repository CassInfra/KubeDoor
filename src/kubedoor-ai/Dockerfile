# Build from the repository root; see build.sh.
FROM registry.cn-shenzhen.aliyuncs.com/starsl/python:3.13-slim
ARG TARGETARCH
ARG KUBECTL_VERSION=v1.37.1
ARG KUBECTL_SHA256
ARG ISTIO_VERSION=1.31.1
ARG YQ_VERSION=v4.54.1
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple/
ARG DEBIAN_MIRROR=https://mirrors.aliyun.com/debian
ARG DEBIAN_SECURITY_MIRROR=https://mirrors.aliyun.com/debian-security
ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_EXTRA_INDEX_URL="" \
    PIP_CONFIG_FILE=/dev/null \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
RUN set -eu; \
    for source_file in /etc/apt/sources.list /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources; do \
        [ -f "$source_file" ] || continue; \
        sed -i -E \
            -e "s#https?://(deb\\.debian\\.org|security\\.debian\\.org)/debian-security/?#${DEBIAN_SECURITY_MIRROR}#g" \
            -e "s#https?://security\\.debian\\.org/?#${DEBIAN_SECURITY_MIRROR}#g" \
            -e "s#https?://deb\\.debian\\.org/debian/?#${DEBIAN_MIRROR}#g" \
            "$source_file"; \
    done; \
    apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl jq dnsutils openssl ripgrep \
    && rm -rf /var/lib/apt/lists/*
COPY deploy/tools/install-ai-tools.sh /tmp/install-ai-tools.sh
RUN sh /tmp/install-ai-tools.sh && rm /tmp/install-ai-tools.sh
COPY src/kubedoor-tools /opt/kubedoor-tools
COPY src/kubedoor-ai/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir /opt/kubedoor-tools -r requirements.txt
COPY src/kubedoor-ai /app
RUN useradd --create-home --uid 10001 kubedoor && chown -R kubedoor:kubedoor /app
USER kubedoor
EXPOSE 8000
CMD ["python", "kubedoor-ai.py"]
