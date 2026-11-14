import os

# 环境变量
# PostgreSQL 连接配置(统一 PG_ 前缀,替代原 CK_*)
PG_HOST = os.environ.get('PG_HOST', 'localhost')
PG_PORT = os.environ.get('PG_PORT', '5432')
PG_USER = os.environ.get('PG_USER', 'postgres')
PG_PASSWORD = os.environ.get('PG_PASSWORD', '')
PG_DATABASE = os.environ.get('PG_DATABASE', 'kubedoor')
LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO')
# 集群指标标签 key、KubeDoor / Alertmanager 访问地址、告警屏蔽规则缓存秒数在 master 的
# 「系统管理 → 通用配置」里维护,由 settings_client 调 master 的内部接口获取(处理告警时取,带缓存)。
# 通知配置(IM 类型、机器人 token、默认 @ 人)在 master 的「系统管理 → 消息通知」里维护,由 notify_client 获取。
# 两者都用 MASTER_INTERNAL_URL(默认 http://kubedoor-master)、AI_INTERNAL_TOKEN(内部令牌)两个环境变量,在调用时读取
