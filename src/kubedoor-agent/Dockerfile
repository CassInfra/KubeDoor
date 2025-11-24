FROM registry.cn-shenzhen.aliyuncs.com/starsl/python:3.11-alpine

# 设置工作目录
WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple/

# 安装 runit 和其他必要工具
RUN sed -i 's/dl-cdn.alpinelinux.org/repo.huaweicloud.com/g' /etc/apk/repositories && apk add --no-cache runit bash

ADD . /app/

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
