"""语义字段映射（约定 §2.2）和集群取值（§2.3）。

字段映射：每个语义字段（状态码、请求方法……）对应 0~5 个候选属性名，查询时候选之间 OR；顺序 = 优先级
（取 Span 的派生值时取第一个有值的）。新旧两版 OpenTelemetry 语义约定的名字都在默认候选里。

集群：链路里用一个资源属性（配置的 cluster_key，例如 k8sname）区分集群，取值要等于 KubeDoor 的集群名或别名
（k8s_agent_status.aliases，Agent 管理页维护）。
"""

# (key, 名称, 层级, 默认候选)；层级空 = 不限（Jaeger 过滤里 attr 不写 level，搜 span + resource 两层）
FIELDS = (
    ('status_code', 'HTTP 状态码', '', ('http.response.status_code', 'http.status_code')),
    ('method', '请求方法', '', ('http.request.method', 'http.method')),
    ('route', '路由模板', '', ('http.route',)),
    ('path', '请求路径', '', ('url.path', 'http.target')),
    ('url', '完整 URL', '', ('url.full', 'http.url')),
    ('query', '查询参数', '', ('url.query',)),
    ('host', '域名 / 被调方地址', '', ('server.address', 'http.host', 'net.host.name', 'net.peer.name')),
    ('client_ip', '客户端 IP', '', ('client.address', 'http.client_ip', 'net.sock.peer.addr')),
    ('exception_type', '异常类型', 'event', ('exception.type',)),
    ('pod', 'Pod', '', ('k8s.pod.name', 'host.name')),
    ('namespace', '命名空间', '', ('k8s.namespace.name',)),
    ('deployment', 'Deployment', '', ('k8s.deployment.name',)),
)
FIELD_KEYS = tuple(item[0] for item in FIELDS)
FIELD_LABELS = {key: label for key, label, _, _ in FIELDS}
FIELD_LEVELS = {key: level for key, _, level, _ in FIELDS}
FIELD_DEFAULTS = {key: list(defaults) for key, _, _, defaults in FIELDS}
MAX_CANDIDATES = 5


def default_fields():
    return {key: list(values) for key, values in FIELD_DEFAULTS.items()}


def effective_fields(stored):
    """存的映射缺项时用默认补（新版本加的字段、旧配置）；不认识的键丢掉"""
    stored = stored if isinstance(stored, dict) else {}
    result = {}
    for key in FIELD_KEYS:
        value = stored.get(key)
        result[key] = [item for item in value if isinstance(item, str) and item] if isinstance(value, list) \
            else list(FIELD_DEFAULTS[key])
    return result


# ---------------------------------------------------------------------------
# 集群
# ---------------------------------------------------------------------------
def cluster_values(env, aliases):
    """当前集群在链路里的取值 = [集群名] + 它的别名（去空去重）"""
    values = []
    for value in [env, *((aliases or {}).get(env) or [])]:
        text = str(value).strip() if value is not None else ''
        if text and text not in values:
            values.append(text)
    return values


AMBIGUOUS = object()


def cluster_lookup(aliases):
    """链路里的集群取值 → KubeDoor 集群；一个值对应多个集群时记为有歧义"""
    lookup = {}
    for env, names in (aliases or {}).items():
        for value in cluster_values(env, {env: names}):
            current = lookup.get(value)
            if current is None:
                lookup[value] = env
            elif current is not AMBIGUOUS and current != env:
                lookup[value] = AMBIGUOUS
    return lookup


def resolve_cluster(value, lookup):
    """{value, env}：对不上或有歧义时 env 为 null（有歧义时另带 ambiguous: true）"""
    env = lookup.get(value) if value not in (None, '') else None
    if env is AMBIGUOUS:
        return {'value': value, 'env': None, 'ambiguous': True}
    return {'value': value, 'env': env}


async def load_aliases():
    """所有集群及其别名 {env: [别名]}；utils 顶层会连带导入 db 等模块，用到时才导入"""
    import utils

    return await utils.get_k8s_aliases()
