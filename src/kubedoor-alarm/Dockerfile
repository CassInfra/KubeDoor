FROM registry.cn-shenzhen.aliyuncs.com/starsl/python:3.14-alpine
ADD . /
WORKDIR /
# psycopg 在 alpine(musl) 上需要编译:补 gcc、musl-dev、postgresql 开发头与 libpq 运行库
RUN sed -i 's/dl-cdn.alpinelinux.org/repo.huaweicloud.com/g' /etc/apk/repositories && \
    apk add --no-cache gcc musl-dev postgresql-dev libpq && \
    pip3 install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple/ --no-cache-dir
EXPOSE 80
CMD ["python3", "/kubedoor-alarm.py"]
