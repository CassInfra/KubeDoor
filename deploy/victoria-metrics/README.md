# 独立安装 VictoriaMetrics

此目录通过 Docker Compose 部署 VictoriaMetrics 单机版，默认镜像为 `registry.cn-shenzhen.aliyuncs.com/starsl/victoria-metrics:stable`。实例和数据独立于 KubeDoor 的安装、升级和卸载。

## 安装

在能被 KubeDoor 所在 K8S 集群访问的服务器上执行：

```bash
cd deploy/victoria-metrics
cp .env.example .env
vi .env                       # 修改 VM_PASSWORD，按需调整端口和保留时间
docker compose config --quiet
docker compose up -d
docker compose ps
```

默认监听 `8428`，保留最近 `30d` 的指标，使用 HTTP Basic Auth。`VM_USER`、`VM_PASSWORD` 都不能为空；密码含 `$` 时，使用 `.env` 的单引号写法，例如 `VM_PASSWORD='my$secret'`。`.env` 填写原始密码，无需 URL 编码。

查看日志或检查服务：

```bash
docker compose logs --tail=100 -f victoria-metrics
curl --fail --user monit http://127.0.0.1:8428/health
```

`curl` 会提示输入密码。若修改了 `.env` 中的账号或端口，请同步调整检查命令。修改 `.env` 后再次执行 `docker compose up -d`，新配置即随容器重建生效，指标数据保留。

## 接入 KubeDoor

假设这台服务器的地址为 `192.168.1.10`，在 KubeDoor 网页安装向导的“时序数据库”步骤中选择 VictoriaMetrics 单机版，填写：

- 服务地址：`http://192.168.1.10:8428`。
- 认证用户名、密码：与本目录 `.env` 中的 `VM_USER`、`VM_PASSWORD` 一致，直接填写原文。
- 集群指标标签：新安装保持 `origin_prometheus`；接入已有监控时填写现有指标使用的标签名。

向导会自动生成 `/api/v1/write` 写入地址并验证连接。地址必须能从 K8S Pod 访问，不能填写该服务器本机的 `127.0.0.1`。目标集群中的 Agent 通过 KubeDoor 的统一入口写入指标，不必保存 VM 的账号密码。

已有[旧配置式部署](../../tools/legacy-install/README.md)继续在原配置中填写连接地址：

```bash
PROM_URL=http://monit:your-url-encoded-password@192.168.1.10:8428
PROM_TYPE=Victoria-Metrics-Single
METRICS_WRITE_URL=http://monit:your-url-encoded-password@192.168.1.10:8428/api/v1/write
```

将示例密码替换为 `.env` 中的实际密码。放进 URL 的账号、密码需要 URL 编码，例如 `@` 编码为 `%40`，`#` 编码为 `%23`，`$` 编码为 `%24`。地址使用 K8S Pod 可访问的服务器 IP 或域名，不能填写服务器本机的 `127.0.0.1`。

`PROM_URL` 用于指标查询；`METRICS_WRITE_URL` 用于网页安装 Agent 时由 Master 转发指标。通过脚本安装 Agent 时，可将相同的写入地址填写为 `REMOTE_WRITE_URL`。VictoriaMetrics 的镜像、存储、认证、保留时间均在此目录管理。

修改旧部署的连接地址后，在仓库根目录执行 `./tools/legacy-install/install.sh master -c /path/to/original/kubedoor.conf` 应用配置。网页安装不读取此文件。

## 数据与维护

指标保存在 Docker 命名卷 `kubedoor-vmdata`。`docker compose down` 停止服务并保留数据，之后可用 `docker compose up -d` 重新启动；`docker compose down -v` 会删除该命名卷及全部指标数据。

升级镜像：

```bash
docker compose pull
docker compose up -d
```

已有 K8S PVC 中的指标不会自动迁移到此命名卷；需要保留历史数据时，应先按 [VictoriaMetrics 备份与恢复文档](https://docs.victoriametrics.com/victoriametrics/single-server-victoriametrics/#how-to-work-with-snapshots)完成迁移，再切换连接地址。
