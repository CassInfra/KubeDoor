"""接口权限表:每个 (方法, 路径) 需要什么权限。

按顺序匹配,第一条命中的规则生效;没登记的 /api/* 落到最后一条,按「目标集群读写」处理(默认拒绝)。
新增接口时在这里登记,tests/test_auth_policy.py 会检查 master 自己注册的路由都有显式规则。

规则种类(kind):
  public        不需要登录(登录接口、nginx 子请求鉴权接口自己判定)
  session       任何已登录的人(包括必须先改密的会话):/api/auth/*
  login         任何已登录的人;按集群过滤数据由 handler 自己做
  global_read   全局角色是只读及以上(与集群无关的数据)
  global_write  全局角色是读写或管理员(与集群无关的写操作)
  admin         管理员
  env_read      目标集群可读(集群参数必填)
  env_read_opt  带了集群参数就校验可读;没带由 handler 按可见集群过滤
  env_write     目标集群可写
  agent         kubedoor-agent 注册通道(中间件单独处理)
  internal      AI 服务内部桥(内部 token + 用户名),handler 再按集群校验
  custom        自定义校验函数
"""

import re
from dataclasses import dataclass, field

from .principal import PERM_LABELS


class Deny(Exception):
    def __init__(self, status, message, code='forbidden'):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code


def no_env_access(env):
    return Deny(403, f'没有集群 {env} 的访问权限', 'cluster_forbidden')


def need_env_write(principal, env):
    if principal.can_read(env):
        return Deny(403, f'当前账号对集群 {env} 只有只读权限,不能执行此操作', 'cluster_read_only')
    return no_env_access(env)


NEED_GLOBAL_WRITE = '需要全局读写权限(与集群无关的配置只看全局权限)'


@dataclass(frozen=True)
class Rule:
    methods: frozenset
    pattern: re.Pattern
    kind: str
    env: tuple = ()  # 集群参数来源:(('query', 'env'), ('body', 'k8s'), ...)
    many: bool = False  # 集群参数是数组
    check: object = None  # custom 校验:async def check(request, principal) -> None,不通过抛 Deny
    write: bool = False  # 写操作:记审计日志
    extra: dict = field(default_factory=dict)

    def matches(self, method, path):
        method = 'GET' if method == 'HEAD' else method  # aiohttp 给每个 GET 路由自动注册了 HEAD
        return (not self.methods or method in self.methods) and self.pattern.fullmatch(path) is not None


def _rule(methods, path, kind, env=None, many=False, check=None, write=None):
    method_set = frozenset(m.strip().upper() for m in methods.split('|')) if methods else frozenset()
    sources = ()
    if env:
        sources = tuple(tuple(item.split('.', 1)) for item in env.split('|'))
    if write is None:
        write = kind in ('env_write', 'global_write') or (
            kind in ('admin', 'custom') and bool(method_set) and not method_set <= {'GET', 'HEAD', 'OPTIONS'})
    return Rule(method_set, re.compile(path), kind, sources, many, check, write)


# ---------------------------------------------------------------------------
# 取集群参数
# ---------------------------------------------------------------------------
async def _json_body(request):
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


async def extract_envs(rule, request):
    """按规则取出请求里的目标集群列表(去重保序,过滤空值)。"""
    envs = []
    body = None
    for source, name in rule.env:
        if source == 'query':
            values = [request.query.get(name)]
        else:
            if body is None:
                body = await _json_body(request)
            value = body.get(name)
            values = value if isinstance(value, list) else [value]
        for value in values:
            if isinstance(value, str) and value.strip() and value.strip() not in envs:
                envs.append(value.strip())
    return envs


def _require_envs(envs):
    if not envs:
        raise Deny(400, '缺少 K8S 集群参数', 'missing_env')


def check_read_all(principal, envs):
    for env in envs:
        if not principal.can_read(env):
            raise no_env_access(env)


def check_write_all(principal, envs):
    for env in envs:
        if not principal.can_write(env):
            raise need_env_write(principal, env)


# ---------------------------------------------------------------------------
# 自定义校验
# ---------------------------------------------------------------------------
async def update_image_check(request, principal):
    """改镜像:对集群有读写权限直接放行;只读用户继续走 UPDATE_IMAGE 白名单(时间段 + 用户名)。"""
    env = request.query.get('env')
    _require_envs([env] if env else [])
    if principal.can_write(env):
        request['update_image_direct'] = True
        return
    if not principal.can_read(env):
        raise no_env_access(env)
    request['update_image_direct'] = False


async def res_content_check(request, principal):
    """查看资源 YAML:Secret 内容需要读写权限(同 K8S 内置 view 角色不含 Secret)。"""
    env = request.query.get('env')
    _require_envs([env] if env else [])
    if (request.query.get('resource_type') or '').strip().lower() in ('secret', 'secrets'):
        if not principal.can_write(env):
            raise Deny(403, f'查看 Secret 内容需要集群 {env} 的读写权限', 'secret_forbidden')
        return
    check_read_all(principal, [env])


async def res_upsert_check(request, principal):
    """新增/编辑管控服务:当前集群可写;勾选了同步到其它集群,目标集群也要可写。"""
    body = await _json_body(request)
    env = body.get('env')
    _require_envs([env] if isinstance(env, str) and env else [])
    envs = [env]
    sync_env = body.get('sync_env')
    if isinstance(sync_env, str) and sync_env.strip():
        envs.append(sync_env.strip())
    check_write_all(principal, envs)


async def alert_operate_check(request, principal):
    """修改告警处理状态:按这条告警所属集群判断。"""
    from datetime import datetime

    import db

    body = await _json_body(request)
    try:
        start_time = datetime.fromisoformat(str(body.get('start_time')))
    except ValueError:
        raise Deny(400, 'start_time 格式不正确', 'bad_request')
    env = await db.pg_fetchval(
        'SELECT env FROM k8s_pod_alert_days WHERE start_time=$1 AND fingerprint=$2 LIMIT 1',
        start_time, str(body.get('fingerprint') or ''),
    )
    if env is None:
        raise Deny(404, '告警不存在', 'not_found')
    check_write_all(principal, [env])


SILENCE_ENV_KEYS = ('env', 'k8s', 'cluster')


def silence_envs(matchers):
    """屏蔽规则作用的集群:有 env/k8s/cluster 的「=」条件就是这些集群,否则是全局规则(返回 None)。"""
    envs = []
    for matcher in matchers or []:
        if not isinstance(matcher, dict):
            continue
        if str(matcher.get('key') or '').strip() in SILENCE_ENV_KEYS and (matcher.get('op') or '=') == '=':
            value = str(matcher.get('value') or '').strip()
            if value and value not in envs:
                envs.append(value)
    return envs or None


def silence_visible(principal, matchers):
    envs = silence_envs(matchers)
    if envs is None:
        return True  # 全局规则对所有人可见(它也会屏蔽当前用户能看到的集群的告警)
    return any(principal.can_read(env) for env in envs)


def check_silence_write(principal, matchers):
    envs = silence_envs(matchers)
    if envs is None:
        if not principal.global_write:
            raise Deny(403, '没有限定集群(env=xxx)的屏蔽规则对所有集群生效,' + NEED_GLOBAL_WRITE, 'global_write_required')
        return
    check_write_all(principal, envs)


async def _silence_matchers(silence_id):
    import json

    import db

    try:
        silence_id = int(silence_id)
    except (TypeError, ValueError):
        raise Deny(400, '缺少规则 ID', 'bad_request')
    raw = await db.pg_fetchval('SELECT matchers FROM alert_silences WHERE id=$1', silence_id)
    if raw is None:
        raise Deny(404, '规则不存在', 'not_found')
    return json.loads(raw) if isinstance(raw, str) else raw


async def silence_write_check(request, principal):
    body = await _json_body(request)
    action = request.path.rsplit('/', 1)[-1]
    if action in ('edit', 'revoke', 'extend', 'delete'):
        check_silence_write(principal, await _silence_matchers(body.get('id')))
    if action in ('add', 'edit'):
        matchers = body.get('matchers')
        if isinstance(matchers, str):
            import json

            try:
                matchers = json.loads(matchers)
            except ValueError:
                matchers = None
        if isinstance(matchers, list):
            check_silence_write(principal, matchers)
        elif not principal.global_write:
            raise Deny(403, NEED_GLOBAL_WRITE, 'global_write_required')


async def _vs_clusters(vs_id):
    import db

    try:
        vs_id = int(vs_id)
    except (TypeError, ValueError):
        raise Deny(400, 'vs_id 格式不正确', 'bad_request')
    rows = await db.pg_fetch('SELECT k8s_name FROM k8s_cluster WHERE vs_id=$1', vs_id)
    return [row['k8s_name'] for row in rows]


async def _route_vs(route_id):
    import db

    try:
        route_id = int(route_id)
    except (TypeError, ValueError):
        raise Deny(400, 'route_id 格式不正确', 'bad_request')
    vs_id = await db.pg_fetchval('SELECT vs_global_id FROM vs_http_routes WHERE id=$1', route_id)
    if vs_id is None:
        raise Deny(404, '路由不存在', 'not_found')
    return vs_id


def _check_vs_write(principal, clusters):
    if clusters:
        check_write_all(principal, clusters)
    elif not principal.global_write:
        raise Deny(403, '该 VirtualService 没有关联集群,' + NEED_GLOBAL_WRITE, 'global_write_required')


def _check_vs_read(principal, clusters):
    """VS 关联了多个集群时,任一关联集群可读即可查看;没有关联集群的按全局权限。"""
    if clusters:
        if not any(principal.can_read(env) for env in clusters):
            raise Deny(403, '没有该 VirtualService 所属集群的访问权限', 'cluster_forbidden')
    elif not principal.global_read:
        raise Deny(403, '需要全局只读及以上权限', 'global_read_required')


async def _referenced_vs_ids(request):
    """请求里引用到的所有 VS:query 的 vs_id,以及 route_id 所属的 VS。

    Istio 的 handler 有的按 vs_id、有的按 route_id 取数或修改(比如取单个路由、删路由只看 route_id),
    两个参数可能同时出现且指向不同的 VS,所以必须每个都校验,不能只看其中一个。"""
    vs_ids = []
    if request.query.get('vs_id'):
        vs_ids.append(request.query.get('vs_id'))
    if request.query.get('route_id'):
        vs_ids.append(await _route_vs(request.query.get('route_id')))
    return vs_ids


async def istio_read_check(request, principal):
    """Istio 查询:按引用到的 VS(vs_id / route_id)判断;只按集群列 VS 时检查 k8s_cluster。"""
    vs_ids = await _referenced_vs_ids(request)
    for vs_id in vs_ids:
        _check_vs_read(principal, await _vs_clusters(vs_id))
    if vs_ids:
        return
    cluster = request.query.get('k8s_cluster')
    if cluster:
        check_read_all(principal, [cluster])
        return
    if not principal.global_read:
        raise Deny(403, '需要全局只读及以上权限', 'global_read_required')


async def istio_vs_write_check(request, principal):
    if request.method == 'POST':
        body = await _json_body(request)
        clusters = body.get('k8s_clusters')
        clusters = [c for c in clusters if isinstance(c, str) and c] if isinstance(clusters, list) else []
        _check_vs_write(principal, clusters)
        return
    vs_ids = await _referenced_vs_ids(request)
    if not vs_ids:
        raise Deny(400, '缺少 vs_id', 'bad_request')
    for vs_id in vs_ids:
        _check_vs_write(principal, await _vs_clusters(vs_id))


async def istio_vs_k8s_check(request, principal):
    """修改 VS 关联的集群:原来关联的和新关联的集群都要可写。"""
    body = await _json_body(request)
    clusters = await _vs_clusters(body.get('vs_id'))
    new = body.get('k8s_clusters')
    for env in new if isinstance(new, list) else []:
        if isinstance(env, str) and env and env not in clusters:
            clusters.append(env)
    _check_vs_write(principal, clusters)


async def istio_route_write_check(request, principal):
    vs_ids = await _referenced_vs_ids(request)
    if not vs_ids:
        raise Deny(400, '缺少 vs_id 或 route_id', 'bad_request')
    for vs_id in vs_ids:
        _check_vs_write(principal, await _vs_clusters(vs_id))


async def istio_apply_check(request, principal):
    """把数据库里的 VS 应用到集群:目标集群要可写,被应用的 VS 也要看得到(不能借此读取别的集群的 VS)。"""
    env = request.query.get('env')
    _require_envs([env] if env else [])
    check_write_all(principal, [env])
    vs_ids = await _referenced_vs_ids(request)
    if not vs_ids:
        raise Deny(400, '缺少 vs_id', 'bad_request')
    for vs_id in vs_ids:
        _check_vs_read(principal, await _vs_clusters(vs_id))


# ---------------------------------------------------------------------------
# 规则表
# ---------------------------------------------------------------------------
_AGENT_READ_PATHS = '|'.join([
    'get_dpm_pods', 'events', 'nodes', 'nodes/list', 'cci/schedule-profile',
    'agent/configmaps', 'agent/namespaces', 'agent/services', 'agent/service/endpoints',
    'agent/service/first-port', 'agent/ingresses', 'agent/ingress/rules', 'agent/pods',
    'agent/jvm/configs', 'agent/statefulsets', 'agent/statefulset/pods', 'agent/daemonsets',
    'agent/daemonset/pods', 'load-balance/isolated', 'load-balance/nodes-cpu', 'load-balance/namespaces',
    'pod/get_logs', 'pod/download_logs', 'pod/get_previous_logs', r'pod/task_status/[^/]+',
])

RULES = (
    # ---- 登录 / 账号自助 ----
    _rule('POST', '/api/auth/login', 'public'),
    _rule('', '/api/auth/verify', 'public'),
    _rule('POST', '/api/auth/logout', 'public'),  # 会话失效时也要能清 Cookie
    _rule('GET|POST', '/api/auth/(me|password)', 'session'),
    # ---- 系统管理:用户、集群授权、LDAP、审计 ----
    _rule('', '/api/admin/.+', 'admin'),
    # ---- AI 服务内部桥 ----
    _rule('', '/api/ai/internal/.+', 'internal'),
    # ---- WebSocket ----
    _rule('GET', '/ws', 'agent'),
    _rule('GET', '/ws/(pod-logs|workload-status)', 'env_read', env='query.env'),
    # ---- 集群列表(handler 按可见集群过滤)----
    _rule('GET', '/api/(prom_env|agent_names|agent_status)', 'login'),
    _rule('GET', '/api/prom_(ns|services|query|node_rank)', 'env_read', env='query.env'),
    _rule('GET', '/api/prom_overview', 'env_read_opt', env='query.env'),
    _rule('GET', '/api/stats/(top10_events|top10_pod_alerts|alert_daily)', 'env_read_opt', env='query.env'),
    _rule('POST', '/api/image/tags', 'login'),
    _rule('POST', '/api/events/query', 'env_read', env='body.k8s'),
    _rule('GET', '/api/events/menu', 'env_read', env='query.k8s'),
    # ---- 高峰期采集 ----
    _rule('GET', '/api/init_peak_data', 'env_write', env='query.env'),
    _rule('GET', '/api/cron_peak_data', 'admin', write=True),  # CronJob 带内部 token(系统身份)调用
    # ---- 资源管控(数据库)----
    _rule('GET', '/api/db/res/envs', 'login'),
    _rule('GET', '/api/db/res/(namespaces|deployments|max_day|collection)', 'env_read', env='query.env'),
    _rule('GET', '/api/db/res/list', 'env_read_opt', env='query.env'),
    _rule('POST', '/api/db/res/(add|edit)', 'custom', check=res_upsert_check),
    _rule('POST', '/api/db/res/pod_count', 'env_write', env='body.env'),
    # ---- Agent 管理(管理员)----
    _rule('POST', '/api/db/agent/(admission|collect|nms_not_confirm|scheduler)', 'admin'),
    _rule('GET', '/api/admis_switch', 'admin', write=True),
    _rule('GET', '/api/db/agent/show_add_label', 'env_read', env='query.env'),
    # ---- 告警 ----
    _rule('GET', '/api/db/alert/(envs|names)', 'login'),
    _rule('POST', '/api/db/alert/total', 'env_read_opt', env='body.env'),
    _rule('POST', '/api/db/alert/(detail|detail_total)', 'env_read_opt', env='body.env', many=True),
    _rule('POST', '/api/db/alert/operate', 'custom', check=alert_operate_check),
    # ---- 告警屏蔽 ----
    _rule('GET', '/api/db/silence/(list|label_values)', 'login'),
    _rule('POST', '/api/db/silence/preview', 'login'),
    _rule('POST', '/api/db/silence/(add|edit|revoke|extend|delete)', 'custom', check=silence_write_check),
    # ---- Istio ----
    _rule('GET', '/api/istio/health', 'login'),
    _rule('GET', '/api/istio/(vs|httproute)', 'custom', check=istio_read_check),
    _rule('POST|PUT|DELETE', '/api/istio/vs', 'custom', check=istio_vs_write_check),
    _rule('POST', '/api/istio/vs/k8s', 'custom', check=istio_vs_k8s_check),
    _rule('POST|PUT|DELETE', '/api/istio/httproute(/reorder)?', 'custom', check=istio_route_write_check),
    # 会把 K8S 里的 VirtualService 同步写回数据库,按写处理
    _rule('GET', '/api/agent/istio/vs', 'env_write', env='query.env'),
    # 把数据库里的 VS 应用到集群:目标集群可写 + 能看到这个 VS
    _rule('POST', '/api/agent/istio/vs/apply', 'custom', check=istio_apply_check),
    # ---- 节点均衡 ----
    _rule('GET', '/api/load-balance/(config|status|plan|logs)', 'global_read'),
    _rule('PUT', '/api/load-balance/config', 'global_write'),
    _rule('POST', '/api/load-balance/check-pods', 'env_read', env='query.env'),
    _rule('POST', '/api/load-balance/(analyze|execute|cleanup)', 'env_write', env='query.env'),
    # ---- 转发给 agent 的只读接口 ----
    _rule('GET', f'/api/({_AGENT_READ_PATHS})', 'env_read', env='query.env'),
    _rule('GET', '/api/agent/res/content', 'custom', check=res_content_check),
    # ---- 改镜像:读写直接放行,只读用户走 UPDATE_IMAGE 白名单 ----
    _rule('POST', '/api/update-image', 'custom', check=update_image_check, write=True),
    # ---- 其余 /api/* (转发给 agent 的写操作,如扩缩容、重启、删 Pod、exec 等)----
    _rule('', '/api/.*', 'env_write', env='query.env'),
)

# 没有任何规则命中(非 /api、/ws 路径):只有管理员能访问
FALLBACK = _rule('', '.*', 'admin')


def match(method, path):
    for rule in RULES:
        if rule.matches(method, path):
            return rule
    return FALLBACK


async def authorize(rule, request, principal):
    """校验已登录用户能否访问;不通过抛 Deny。登录状态与改密限制由中间件先判断。"""
    kind = rule.kind
    if kind in ('public', 'session', 'login', 'agent'):
        return
    if kind == 'internal':
        if principal.via not in ('internal', 'system'):
            raise Deny(403, '仅限内部服务调用', 'forbidden')
        return
    if kind == 'global_read':
        if not principal.global_read:
            raise Deny(403, '需要全局只读及以上权限', 'global_read_required')
        return
    if kind == 'global_write':
        if not principal.global_write:
            raise Deny(403, NEED_GLOBAL_WRITE, 'global_write_required')
        return
    if kind == 'admin':
        if not principal.is_admin:
            raise Deny(403, '需要管理员权限', 'admin_required')
        return
    if kind == 'custom':
        await rule.check(request, principal)
        return
    envs = await extract_envs(rule, request)
    if kind == 'env_read':
        _require_envs(envs)
        check_read_all(principal, envs)
    elif kind == 'env_read_opt':
        check_read_all(principal, envs)
    elif kind == 'env_write':
        if not envs and principal.is_admin:
            return
        _require_envs(envs)
        check_write_all(principal, envs)
    else:
        raise Deny(403, '未知的权限规则', 'forbidden')


def perm_label(perm):
    return PERM_LABELS.get(perm, perm)
