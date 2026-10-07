# JVM 启动参数采集与管控

在「高峰资源 → 高峰资源管控」页统一查看和修改 Java 微服务的 `-Xms`、`-Xmx`、`-Xss`、
`-XX:MaxMetaspaceSize`，改动由准入控制在下一次发布或重启时写回 Deployment。同页红色的
`P95podHeap%`、`P95podG1E%` 两列是选定高峰日（最近 10 天里集群 CPU 总消耗最大的一天）高峰时段的
堆内存使用率 P95 和 G1 Eden 占用率 P95（依赖时序库里的 JVM 内存指标），只作调参参考，不会自动修改参数。

- **采集**：「Agent管理」页开启「自动采集」后每天自动采集，也可以点「采集历史数据」列的
  「采集」立即采集；每次采完 CPU/内存后顺带读取各 Deployment 当前的 JVM 参数，只补空值，
  不覆盖已有的管控值。
- **查看与修改**：表格中蓝色的 `Xms`、`Xmx`、`Xss`、`MaxMeta`（MaxMetaspaceSize）四列，`Xss`
  单位为 k，其余为 m，未采集显示 `-`。点行上的「配置」，在弹窗里修改已采集的项（未采集的项不能新增）：
  「保存」只写数据库，等下一次发布或重启时生效；「保存并重启」保存后打开「微服务重启」窗口，
  重启时即生效。
- **前提**：该集群在「Agent管理」页打开了「准入控制」，且服务所在命名空间在「管控命名空间」里。
  准入控制同时会按管控表改写副本数和第一个容器的 requests/limits，开启前请先阅读
  [K8S 资源管控功能说明](../help/K8S资源管控功能说明.md)。
- **只替换已有参数**：只处理 Deployment 第一个容器的 `args`，且 `args[0]` 必须恰好是 `java`；
  只替换其中已经写了的这几个参数，不新增，其他参数和顺序不变。写在 `command`、环境变量
  （如 `JAVA_OPTS`）、启动脚本里的参数，以及 `-jar` / 主类之后的应用参数都不处理。

下文是采集、写回与指标口径的实现细节。

资源管控支持 `Xms`、`Xmx`、`Xss`、`MaxMetaspaceSize`。采集和写回都严格限定为
Deployment 的 `spec.template.spec.containers[0].args`，且 `args[0]` 必须精确为 `java`。
`command`、环境变量、启动脚本、其他容器以及 `-jar` / 主类 / 模块入口后的应用参数不参与处理。

## 采集与数据库升级

- 「Agent管理」页的历史采集接口 `/api/init_peak_data` 和自动采集 `/api/cron_peak_data` 在原有资源管控
  更新成功后，每个集群额外执行一次 `/api/agent/jvm/configs`。Agent 按每页 500 个 Deployment
  批量 LIST，不对每个服务分别 GET，也不执行 Pod 命令。
- 当前启动配置只补入 `k8s_res_control`，不会填入过去日期的资源历史记录。
  单条 SQL 批量补空：已有管控值保持不变，只填充尚为 NULL 的 JVM 字段。
- master 启动时执行 `db.sql`，以幂等 `ADD COLUMN IF NOT EXISTS` 为现有数据库补上
  `jvm_xms_bytes`、`jvm_xmx_bytes`、`jvm_xss_bytes`、`jvm_max_metaspace_bytes` 四个 nullable bigint 字段。
  NULL 表示未采集；0 保留显式默认值语义。新建数据库同样包含这些字段。
- 新旧 agent 的准入协议兼容。旧 agent 不采集或应用 JVM 配置；JVM 补采失败时保留原 CPU/内存
  采集结果，并返回 warning。升级 master 和 agent 后重新执行一次采集即可补齐已有服务。

## 展示、编辑与生效

- `/resource/index` 蓝色展示四个字段：`Xss` 使用 `k`（KiB），其余三项使用 `m`（MiB），未采集显示 `-`。
  编辑只允许修改已采集项；保存不更新运行中的 Pod，下一次镜像发布或 Deployment 滚动重启时生效。
- 数据库保存精确字节数，页面用十进制精确转换。例如 `-Xss512k` 显示并编辑为 `512k`，保存后仍为
  524288 字节。写回时 `Xss` 使用整数 `k`（例如 1 MiB 写作 `-Xss1024k`），其余三项优先使用整数
  `m`。JVM 命令不接受小数容量，遇到不能整除的大小则使用较小单位或字节，保证实际大小不变。
- 沿用集群/命名空间/服务的现有准入管控范围。每个非 NULL 管控字段只替换对应已有合法启动参数；
  未采集或发布 args 中未出现的参数不新增。同名参数重复出现时统一替换，其他参数和顺序保持不变。
  删除 Pod 后自动补建不会重新经过 Deployment 准入管控。

## 高峰期堆内存使用率 P95

- 每日高峰采集通过 `heap_usage_percent` 查询 `jvm_memory_used_bytes{area="heap"}` 和
  `jvm_memory_max_bytes{area="heap"}`，仅增加一次集群级 PromQL 查询，不逐个服务查询 Agent 或 Pod。
  按集群、命名空间、Pod、容器、内存池 `id` 去重后，分别将各容器的 heap used/max 求和。
  分母仅累加正数 max：G1 Eden/Survivor 等池可能返回 -1（未定义上限），不能将 -1 纳入总上限。
- 每个 Pod 沿用原 heap 指标的最大使用量容器口径，用 `topk(1, ...)` 选定容器后，按容器标签
  匹配其 used/max；避免分子、分母来自不同容器。随后通过 `kube_pod_owner` 关联 ReplicaSet，
  对同一时刻的各 Pod 比值求平均，最后计算高峰时段 `quantile_over_time(0.95, ...) * 100`。
  没有有效正数 max 时不生成使用率，页面显示 `-`。
- 结果保存在 `k8s_resources.p95_pod_heap_pct`，并随最近 10 天 CPU 使用最高日的其他资源数据
  同步到 `k8s_res_control.p95_pod_heap_pct`。master 启动时为两张表幂等补列。
  已有数据库的旧 `p80_pod_heap_mb`、`p90_pod_heap_pct` 列保留，不再读写；旧值不会迁移成 P95。
  升级后重新采集最近 10 天高峰数据，以填充选定高峰日的 P95 使用率。
  没有 JVM heap 指标、有效上限或查询失败时为 NULL，不影响原有 CPU/内存采集。
- `/resource/index` 在 `Xms` 前以红色展示 `P95podHeap%`，未采集显示 `-`。
  这是堆的使用率观测值，不会自动替换 Xms/Xmx，也不会写入 Deployment。
  CPU/内存百分比列继续显示 P80，历史 `p95_*` 数据库/API 字段名保持兼容。

## 高峰期 G1 Eden Space 使用率 P95

- `g1e_usage_percent` 只查询 `area="heap", id="G1 Eden Space"` 的
  `jvm_memory_used_bytes` 和 `jvm_memory_committed_bytes`，使用 `used / committed`。
  去重后，按 Pod 选 Eden 使用量最大的容器，并匹配同一容器的 committed；
  通过 `kube_pod_owner` 关联 ReplicaSet，同一时刻各 Pod 比值求平均后，计算
  `quantile_over_time(0.95, ...) * 100`。committed 缺失或非正数时不生成占比。
- 每个环境每天增加一次集群级查询。结果写入 `k8s_resources.p95_pod_g1e_pct`，
  随同一选定高峰日的数据同步到 `k8s_res_control.p95_pod_g1e_pct`。
  master 启动时为两张表幂等补上 nullable 字段；旧记录和未配置 G1 指标的服务为 NULL。
  堆或 Eden 的任一查询失败时，不影响其他资源指标的采集。
- 页面在 `P95podHeap%` 后、`Xms` 前红色展示 `P95podG1E%`，保留两位小数，缺失显示 `-`。
  这是 Eden 池使用量相对其当前 committed 容量的占比，容量会随 GC 动态变化，
  与整个 heap 的 used/max 使用率分开统计；仅供展示，不参与启动参数管控。
  升级后重新采集最近 10 天高峰数据，以填充所选高峰日的 Eden 占比。

## 本地验证

```powershell
cd src/kubedoor-master
python -m pytest tests -q
cd ../kubedoor-agent
python -m pytest tests -q
cd ../kubedoor-web
node --test tests/jvm.test.cjs
pnpm typecheck
pnpm build
```

测试覆盖严格采集条件、参数对应关系、应用参数隔离、单位精度、部分字段和重复参数、分页、
600 个服务单次数据库更新、旧准入协议兼容、失败重试，以及生产 ES2015 编译后的页面单位转换。

堆和 Eden 使用率的 PromQL 实值回归位于 `tests/test_peak_heap_promql.py` 和
`tests/test_peak_g1e_promql.py`：安装 `promtool` 到 PATH，
或将 `PROMTOOL_PATH` 指向其可执行文件后运行 pytest；未提供工具时仅跳过这组实值回归。
测试覆盖多内存池求和、无效上限、重复采集、同容器容量匹配、P95 分位数和跨 Pod 聚合顺序。
