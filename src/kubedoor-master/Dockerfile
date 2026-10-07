FROM registry.cn-shenzhen.aliyuncs.com/starsl/python:3.14-alpine

WORKDIR /
COPY requirements.txt .
# asyncpg / psycopg 在 alpine(musl) 上需要编译:补 postgresql 开发头与 libpq 运行库
RUN sed -i 's/dl-cdn.alpinelinux.org/repo.huaweicloud.com/g' /etc/apk/repositories && \
    apk add --no-cache gcc musl-dev postgresql-dev libpq && \
    pip3 install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/ --no-cache-dir
ADD . /
EXPOSE 80
CMD ["python3", "/kubedoor-master.py"]
