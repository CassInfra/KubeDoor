"""通用配置（Web「系统管理 → 通用配置」）：原来 kubedoor-config 里除 PG 连接外的配置。

存 PostgreSQL，运行时从内存快照读取。

其他 master 代码一律这样取值（不要 from general_settings import value，也不要在模块顶层取值：
测试可以 monkeypatch.setattr(general_settings, 'value', ...)，也避免循环 import）：

    import general_settings

    general_settings.value('prom_url')          # 同步、线程安全；返回生效值（库里的或默认值）
    general_settings.value('prom_k8s_tag_key')  # 永远非空

- fields.py：配置项定义（中文名称、校验、默认值、旧环境变量名）、masked()
- service.py：存储（auth_settings key=general_settings，三个地址加密）、快照、value()
- api.py：GET/PUT /api/admin/general-settings、POST .../test-metrics、GET /api/internal/settings、启停
- legacy.py：旧配置迁移（环境变量 → 通用配置，MSG_* → 消息通知）；cli.py：python3 -m general_settings migrate

注意循环 import：auth.store、auth.middleware、utils 等在顶层 import general_settings，所以这个包在模块顶层只能
import db、auth.crypto、auth.audit、auth.context、agent_install.bundle 这些不读通用配置的模块；
notify、image_settings、k8s_event 在函数里延迟 import。
"""

from .api import on_cleanup, on_startup, register_routes
from .fields import DEFAULTS, FIELDS, LABELS, PASSWORD_MASK, masked, url_credentials
from .legacy import import_legacy
from .service import SETTING_KEY, is_loaded, refresh, value

__all__ = [
    'DEFAULTS', 'FIELDS', 'LABELS', 'PASSWORD_MASK', 'SETTING_KEY', 'import_legacy', 'is_loaded', 'masked',
    'on_cleanup', 'on_startup', 'refresh', 'register_routes', 'url_credentials', 'value',
]
