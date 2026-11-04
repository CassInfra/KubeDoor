FROM registry.cn-shenzhen.aliyuncs.com/starsl/python:3.14-alpine

WORKDIR /app
COPY src/kubedoor-setup/requirements.txt /tmp/setup-requirements.txt
RUN sed -i 's/dl-cdn.alpinelinux.org/repo.huaweicloud.com/g' /etc/apk/repositories && \
    apk add --no-cache gcc musl-dev postgresql-dev libpq && \
    pip install --no-cache-dir -r /tmp/setup-requirements.txt -i https://mirrors.aliyun.com/pypi/simple/ && \
    apk del gcc musl-dev postgresql-dev

COPY src/kubedoor-setup/ /app/src/kubedoor-setup/
COPY src/kubedoor-master/agent_install/bundle.py /app/src/kubedoor-master/agent_install/bundle.py
COPY src/kubedoor-master/auth/passwords.py /app/src/kubedoor-master/auth/passwords.py
COPY deploy/install.sh /app/deploy/install.sh

ENV PYTHONPATH=/app/src/kubedoor-setup \
    PYTHONUNBUFFERED=1 \
    KUBEDOOR_ROOT=/app
EXPOSE 8080
CMD ["python", "-m", "kubedoor_setup"]
