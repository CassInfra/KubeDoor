FROM registry.cn-shenzhen.aliyuncs.com/starsl/python:3.14-alpine
ARG TARGETARCH
ARG KUBECTL_VERSION=v1.37.1
ARG KUBECTL_SHA256
ARG ISTIO_VERSION=1.31.1
ARG YQ_VERSION=v4.54.1
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple/
ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_EXTRA_INDEX_URL="" \
    PIP_CONFIG_FILE=/dev/null \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 设置工作目录
WORKDIR /app

COPY src/kubedoor-agent/requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

# 安装 runit 和其他必要工具
RUN sed -i 's/dl-cdn.alpinelinux.org/repo.huaweicloud.com/g' /etc/apk/repositories && apk add --no-cache runit bash curl jq bind-tools openssl ripgrep ca-certificates coreutils
COPY deploy/tools/install-ai-tools.sh /tmp/install-ai-tools.sh
RUN sh /tmp/install-ai-tools.sh && rm /tmp/install-ai-tools.sh
COPY src/kubedoor-tools /opt/kubedoor-tools
RUN pip install --no-cache-dir /opt/kubedoor-tools

COPY src/kubedoor-agent /app/

# 创建服务目录
RUN mkdir -p /etc/service/kubedoor-agent /etc/service/pod-mgr

# 配置 kubedoor-agent 的 run 脚本
RUN echo -e '#!/bin/sh\nexec python3 /app/kubedoor-agent.py' > /etc/service/kubedoor-agent/run && \
    chmod +x /etc/service/kubedoor-agent/run


# 配置 pod-mgr 的 run 脚本
RUN echo -e '#!/bin/sh\nexec python3 /app/pod-mgr.py' > /etc/service/pod-mgr/run && \
    chmod +x /etc/service/pod-mgr/run

# 启动 runit
CMD ["runsvdir", "/etc/service"]
