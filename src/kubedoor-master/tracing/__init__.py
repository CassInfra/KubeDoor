"""链路追踪(Jaeger api_v3 + 可选的 ClickHouse 加速表),给 AI 助手查链路用。

- 配置存 auth_settings(key=tracing),页面「系统管理 → 链路追踪」,接口 /api/admin/tracing*(系统管理员)
- AI 经 POST /api/ai/internal/tracing/{op}?env= 调用(内部 token + X-User-Name,对该集群只读及以上)
- 单条链路一律走 Jaeger;搜索、统计、服务和接口列表配置了 ClickHouse 加速表时优先用它

模块:
  common.py     错误类型、时间解析和格式化(北京时间)、带过期的 LRU 缓存
  fields.py     语义字段映射的默认值、集群取值和反查
  mask.py       敏感值打码
  config.py     配置的默认值、校验、公开格式
  filters.py    Jaeger 结构化过滤(query.filter)的构造
  jaeger.py     Jaeger api_v3 客户端(capabilities、服务列表缓存)
  clickhouse.py ClickHouse HTTP 客户端和 SQL 构造(参数一律绑定)
  model.py      OTLP / Jaeger UI 旧格式的解析和 Span 归一化
  analysis.py   概览、调用树、根因、关键路径、N+1
  search.py     搜索(选路、时间切片、扫描)和统计
  ops.py        服务、接口、取链路、看 Span、字段、识别字段
  api.py        路由注册

文件名和外部接口约定见 .cache-ai-tests/tracing/SPEC.md(开发约定,不随代码发布)。
"""
