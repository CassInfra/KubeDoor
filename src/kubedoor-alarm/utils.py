import os

# 环境变量
# PostgreSQL 连接配置(统一 PG_ 前缀,替代原 CK_*)
PG_HOST = os.environ.get('PG_HOST', 'localhost')
PG_PORT = os.environ.get('PG_PORT', '5432')
PG_USER = os.environ.get('PG_USER', 'postgres')
PG_PASSWORD = os.environ.get('PG_PASSWORD', '')
PG_DATABASE = os.environ.get('PG_DATABASE', 'kubedoor')
MSG_TOKEN = os.environ.get('MSG_TOKEN')
MSG_TYPE = os.environ.get('MSG_TYPE')
PROM_K8S_TAG_KEY = os.environ.get('PROM_K8S_TAG_KEY')
DEFAULT_AT = os.environ.get('DEFAULT_AT')
ALERTMANAGER_EXTURL = os.environ.get('ALERTMANAGER_EXTURL')
LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO')
# 告警屏蔽规则的内存缓存 TTL(秒),决定新建/解除屏蔽后最长多久生效
SILENCE_CACHE_TTL = int(os.environ.get('SILENCE_CACHE_TTL', '15'))
# KubeDoor Web 地址,用于在通知里给出"屏蔽该告警"的直达链接;为空则退回 Alertmanager 链接
KUBEDOOR_EXTURL = os.environ.get('KUBEDOOR_EXTURL', '')
