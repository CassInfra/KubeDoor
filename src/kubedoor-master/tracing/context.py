"""一次请求用到的东西：配置、当前集群取值、集群反查表、打码、Jaeger / ClickHouse 客户端。"""

from . import config as tracing_config
from . import fields as field_map
from .clickhouse import ClickHouseClient
from .jaeger import JaegerClient
from .mask import Masker


class Ctx:
    def __init__(self, cfg, env, aliases, session):
        self.cfg, self.env, self.session = cfg, env, session
        self.aliases = aliases or {}
        self.cluster_key = cfg['cluster_key']
        # 没配集群字段：不按集群过滤（加速表的 k8s_cluster 也不过滤）
        self.cluster_values = field_map.cluster_values(env, self.aliases) if env and self.cluster_key else []
        self.lookup = field_map.cluster_lookup(self.aliases)
        self.fields = cfg['fields']
        self.masker = Masker(cfg['mask_keys'])
        self._jaeger = self._clickhouse = None

    @property
    def jaeger(self):
        if self._jaeger is None:
            self._jaeger = JaegerClient(self.cfg, self.session)
        return self._jaeger

    @property
    def has_clickhouse(self):
        return tracing_config.clickhouse_ready(self.cfg)

    @property
    def has_dim(self):
        return self.has_clickhouse and bool(self.cfg['clickhouse']['dim_table'])

    @property
    def clickhouse(self):
        if self._clickhouse is None:
            self._clickhouse = ClickHouseClient(self.cfg, self.session)
        return self._clickhouse

    def trace_url(self, trace_id):
        return tracing_config.trace_url(self.cfg, trace_id)

    def cluster_info(self):
        return {'key': self.cluster_key, 'values': list(self.cluster_values)}

    def resolve(self, value):
        return field_map.resolve_cluster(value, self.lookup)
