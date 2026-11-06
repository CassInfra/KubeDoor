import os

# 环境变量
# PostgreSQL 连接配置(统一 PG_ 前缀,替代原 CK_*)
PG_HOST = os.environ.get('PG_HOST', 'localhost')
PG_PORT = os.environ.get('PG_PORT', '5432')
PG_USER = os.environ.get('PG_USER', 'postgres')
PG_PASSWORD = os.environ.get('PG_PASSWORD', '')
PG_DATABASE = os.environ.get('PG_DATABASE', 'kubedoor')
# 集群标签名:一批告警按它的值选通知配置(该集群单独配置的群,没有就用全局配置)
PROM_K8S_TAG_KEY = os.environ.get('PROM_K8S_TAG_KEY')
ALERTMANAGER_EXTURL = os.environ.get('ALERTMANAGER_EXTURL')
LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO')
# 告警屏蔽规则的内存缓存 TTL(秒),决定新建/解除屏蔽后最长多久生效
SILENCE_CACHE_TTL = int(os.environ.get('SILENCE_CACHE_TTL', '15'))
# KubeDoor Web 地址,用于在通知里给出"屏蔽该告警"的直达链接;为空则退回 Alertmanager 链接
KUBEDOOR_EXTURL = os.environ.get('KUBEDOOR_EXTURL', '')
# 通知配置(IM 类型、机器人 token、默认 @ 人)在 master 的「系统管理 → 消息通知」里维护,
# 由 notify_client 调 master 的内部接口获取:MASTER_INTERNAL_URL(默认 http://kubedoor-master)、
# AI_INTERNAL_TOKEN(内部令牌)两个环境变量在调用时读取
