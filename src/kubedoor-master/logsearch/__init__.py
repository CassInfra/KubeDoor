"""历史日志查询（管理员登记的 ClickHouse / Elasticsearch 日志源），给 AI 助手查日志用。

- 配置存 auth_settings（key=log_sources），页面「系统管理 → 历史日志」，接口 /api/admin/log-sources*（系统管理员）
- AI 经 POST /api/ai/internal/logs/{op}?env= 调用（内部 token + X-User-Name，对该集群只读及以上），不记查询审计
- 只能查当前集群绑定了的日志源：每条查询都带上绑定的表 / 索引、时间窗口和集群过滤条件（原生查询同样，AI 绕不过去）

模块：
  common.py      错误类型、参数和时间（北京时间）、长文本截断、结果大小控制、每个连接的并发限制
  config.py      配置的默认值、校验、公开格式、审计
  mask.py        敏感值打码
  clickhouse.py  ClickHouse HTTP 客户端（参数绑定、只读设置、错误归类）
  elastic.py     Elasticsearch 客户端
  schema.py      实时字段表（缓存 10 分钟）
  context.py     一次请求用到的配置、日志源解析、客户端、行的整理
  params.py      AI 参数的解析和校验
  build_sql.py   ClickHouse 结构化查询构造
  build_dsl.py   Elasticsearch 结构化查询构造
  raw.py         原生查询（SQL / DSL / ES|QL）的校验与包装
  patterns.py    日志模式归类
  ops.py         7 个 AI 操作
  scan.py        扫描数据源、生成配置建议
  admin.py       测试连接、预览绑定、验证关联字段
  api.py         路由注册

文件名和外部接口约定见 .cache-ai-tests/log-sources/SPEC.md（开发约定，不随代码发布）。
"""
