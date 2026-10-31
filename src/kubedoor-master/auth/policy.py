"""接口权限表:每个 (方法, 路径) 需要什么权限。

按顺序匹配,第一条命中的规则生效;没登记的 /api/* 落到最后一条,按「目标集群读写及以上」处理(默认拒绝)。
新增接口时在这里登记,tests/test_auth_policy.py 会检查 master 自己注册的路由都有显式规则。

规则种类(kind):
  public        不需要登录(登录接口、nginx 子请求鉴权接口自己判定)
  session       任何已登录的人(包括必须先改密的会话):/api/auth/*
  login         任何已登录的人;按集群过滤数据由 handler 自己做
  global_read   全局角色是只读及以上(与集群无关的数据)
  global_write  全局角色是读写及以上(与集群无关的写操作)
  admin         系统管理员
  env_read      目标集群可读(集群参数必填)
  env_read_opt  带了集群参数就校验可读;没带由 handler 按可见集群过滤
  env_write     目标集群读写及以上(兜底规则)
  op            操作权限表里的一个操作(rule.op),每个目标集群都要允许(集群参数必填)
  op_any        至少一个集群允许 rule.op(粗检,handler 再按具体集群判断;Istio 用)
  agent         kubedoor-agent 注册通道(中间件单独处理)
  internal      AI 服务内部桥(内部 token + 用户名),handler 再按集群校验
  custom        自定义校验函数

操作权限表见 auth/permissions.py(Web「权限与审批」页)。鉴权前先 await permissions.load_policy() 刷新缓存,
之后 principal.can / handler 里的判断都读同一份缓存。
"""

import asyncio
import re
from dataclasses import dataclass, field

from loguru import logger

from . import permissions
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


def need_op(principal, op, env, code='operation_forbidden'):
    """没有操作权限:看不到该集群按无权访问处理,否则说明当前权限和被拒的操作。"""
    perm = principal.permission(env)
    if not principal.can_read(env):
        return no_env_access(env)
    return Deny(403, f'当前账号在集群 {env} 是「{PERM_LABELS.get(perm, perm)}」权限,'
                     f'不能执行「{permissions.op_name(op)}」', code)


def need_global_op(op, what=''):
    return Deny(403, f'{what}当前账号的全局角色不能执行「{permissions.op_name(op)}」'
                     '(与集群无关的配置按全局角色判断)', 'operation_forbidden')


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
    op: str = ''  # kind 为 op / op_any 时的操作类型
    extra: dict = field(default_factory=dict)

    def matches(self, method, path):
        method = 'GET' if method == 'HEAD' else method  # aiohttp 给每个 GET 路由自动注册了 HEAD
        return (not self.methods or method in self.methods) and self.pattern.fullmatch(path) is not None


def _rule(methods, path, kind, env=None, many=False, check=None, write=None, op=''):
    method_set = frozenset(m.strip().upper() for m in methods.split('|')) if methods else frozenset()
    sources = ()
    if env:
        sources = tuple(tuple(item.split('.', 1)) for item in env.split('|'))
    if kind in ('op', 'op_any') and op not in permissions.OPS_BY_KEY:
        raise ValueError(f'{path}: 未知的操作类型 {op!r}')
    if write is None:
        write = kind in ('env_write', 'global_write', 'op', 'op_any') or (
            kind in ('admin', 'custom') and bool(method_set) and not method_set <= {'GET', 'HEAD', 'OPTIONS'})
    return Rule(method_set, re.compile(path), kind, sources, many, check, write, op)


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


def _query_env(request):
    env = (request.query.get('env') or '').strip()
    _require_envs([env] if env else [])
    return env


def check_read_all(principal, envs):
    for env in envs:
        if not principal.can_read(env):
            raise no_env_access(env)


def check_write_all(principal, envs):
    for env in envs:
        if not principal.can_write(env):
            raise need_env_write(principal, env)


def check_op_all(principal, op, envs, code='operation_forbidden'):
    for env in envs:
        if not principal.can(op, env):
            raise need_op(principal, op, env, code)


# ---------------------------------------------------------------------------
# 自定义校验
# ---------------------------------------------------------------------------
async def update_image_check(request, principal):
    """改镜像:有「更新镜像」权限直接放行;没有的只读用户继续走 UPDATE_IMAGE 白名单(集群、时间段、用户名,
    由 kubedoor-master.py 判断)。「管理员」列也取消(全站关闭)时白名单也不放行。"""
    env = _query_env(request)
    if principal.can('workload.image', env):
        request['update_image_direct'] = True
        return
    if principal.permission(env) == 'read' and permissions.allowed('workload.image', 'admin'):
        request['update_image_direct'] = False
        return
    raise need_op(principal, 'workload.image', env)


async def res_content_check(request, principal):
    """查看资源 YAML:Secret 内容要「查看 Secret 内容」权限(同 K8S 内置 view 角色不含 Secret),其他资源可读即可。"""
    env = _query_env(request)
    if (request.query.get('resource_type') or '').strip().lower() in ('secret', 'secrets'):
        check_op_all(principal, 'resource.secret', [env], 'secret_forbidden')
        return
    check_read_all(principal, [env])


_LARGE_YAML = 64 * 1024  # 超过这么大的 YAML 放到线程里解析,免得卡住 event loop


async def res_ops_check(request, principal):
    """新建 / 编辑资源 YAML:要「新建 / 编辑资源 YAML」;YAML 里有 kube-system/coredns 或 Istio 资源,
    还要「CoreDNS 修改」/「Istio 修改」(解析不了的 YAML 按都涉及处理)。"""
    env = _query_env(request)
    check_op_all(principal, 'resource.apply', [env])
    content = (await _json_body(request)).get('yaml_content')
    if isinstance(content, str) and len(content) > _LARGE_YAML:
        extra, parsed = await asyncio.to_thread(permissions.yaml_targets, content)
    else:
        extra, parsed = permissions.yaml_targets(content)
    for op in extra:
        if not principal.can(op, env):
            denied = need_op(principal, op, env)
            if not parsed:
                denied.message = f'YAML 解析失败,无法判断是否涉及 CoreDNS / Istio,按涉及处理:{denied.message}'
            raise denied


async def res_delete_check(request, principal):
    """删除资源:要「删除资源」;删的是 kube-system/coredns 或 Istio 资源还要对应的网络权限。"""
    env = _query_env(request)
    check_op_all(principal, 'resource.delete', [env])
    query = request.query
    extra = permissions.identify_targets_master(resource_type=query.get('resource_type'),
                                                name=query.get('resource_name'), namespace=query.get('namespace'))
    for op in extra:
        check_op_all(principal, op, [env])


async def res_upsert_check(request, principal):
    """新增 / 编辑管控服务:当前集群要「资源管控」;勾选了同步到其它集群,目标集群也要。"""
    body = await _json_body(request)
    env = body.get('env')
    _require_envs([env] if isinstance(env, str) and env.strip() else [])
    envs = [env.strip()]
    sync_env = body.get('sync_env')
    if isinstance(sync_env, str) and sync_env.strip() and sync_env.strip() not in envs:
        envs.append(sync_env.strip())
    check_op_all(principal, 'control.edit', envs)


async def load_balance_config_check(request, principal):
    """节点均衡全局配置与集群无关:按全局角色查「节点均衡」那一行。"""
    if not principal.can_global('balance.run'):
        raise need_global_op('balance.run', '节点均衡的全局配置对所有集群生效,')


async def alert_operate_check(request, principal):
    """修改告警处理状态:按这条告警所属集群查「告警处理状态」。"""
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
    check_op_all(principal, 'alert.operate', [env])


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


_GLOBAL_SILENCE = '没有限定集群(env=xxx)的屏蔽规则对所有集群生效,'


def check_silence_write(principal, matchers):
    """改屏蔽规则:限定了集群的,这些集群都要「告警屏蔽」;全局规则按全局角色查表。"""
    envs = silence_envs(matchers)
    if envs is None:
        if not principal.can_global('alert.silence'):
            raise need_global_op('alert.silence', _GLOBAL_SILENCE)
        return
    check_op_all(principal, 'alert.silence', envs)


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
        elif not principal.can_global('alert.silence'):
            raise need_global_op('alert.silence', _GLOBAL_SILENCE)


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
    # CoreDNS:查看 coredns ConfigMap;从 agent 所在 Pod 解析域名
    'agent/coredns', 'agent/net/resolve',
])

RULES = (
    # ---- 登录 / 账号自助 ----
    _rule('POST', '/api/auth/login', 'public'),
    _rule('', '/api/auth/verify', 'public'),
    _rule('POST', '/api/auth/logout', 'public'),  # 会话失效时也要能清 Cookie
    _rule('GET|POST', '/api/auth/(me|password)', 'session'),
    # ---- 系统管理:用户、集群授权、LDAP、审计、权限与审批、获取代码接口 ----
    _rule('', '/api/admin/.+', 'admin'),
    # ---- AI 服务内部桥 ----
    _rule('', '/api/ai/internal/.+', 'internal'),
    # ---- WebSocket ----
    _rule('GET', '/ws', 'agent'),
    _rule('GET', '/ws/(pod-logs|workload-status)', 'env_read', env='query.env'),
    # Pod 终端能在容器里执行任意命令(handler 另记 pod.exec 审计)
    _rule('GET', '/ws/pod-exec', 'op', env='query.env', op='pod.shell'),
    # 节点终端在宿主机上以 root 执行命令(handler 记 node.exec 审计)
    _rule('GET', '/ws/node-exec', 'op', env='query.env', op='node.shell'),
    # ---- 集群列表(handler 按可见集群过滤)----
    _rule('GET', '/api/(prom_env|agent_names|agent_status)', 'login'),
    _rule('GET', '/api/prom_(ns|services|query|node_rank)', 'env_read', env='query.env'),
    _rule('GET', '/api/prom_overview', 'env_read_opt', env='query.env'),
    _rule('GET', '/api/stats/(top10_events|top10_pod_alerts|alert_daily)', 'env_read_opt', env='query.env'),
    _rule('POST', '/api/image/tags', 'login'),
    # 获取代码:master 读 Deployment 的镜像,POST 到管理员配置的接口(handler 记 code_info.get 审计)
    _rule('GET', '/api/code-info', 'env_read', env='query.env'),
    _rule('POST', '/api/events/query', 'env_read', env='body.k8s'),
    _rule('GET', '/api/events/menu', 'env_read', env='query.k8s'),
    # ---- 高峰期采集 ----
    _rule('GET', '/api/init_peak_data', 'op', env='query.env', op='control.edit'),
    _rule('GET', '/api/cron_peak_data', 'admin', write=True),  # CronJob 带内部 token(系统身份)调用
    # ---- 资源管控(数据库)----
    _rule('GET', '/api/db/res/envs', 'login'),
    _rule('GET', '/api/db/res/(namespaces|deployments|max_day|collection)', 'env_read', env='query.env'),
    _rule('GET', '/api/db/res/list', 'env_read_opt', env='query.env'),
    _rule('POST', '/api/db/res/(add|edit)', 'custom', check=res_upsert_check),
    _rule('POST', '/api/db/res/pod_count', 'op', env='body.env', op='workload.scale'),
    # ---- Agent 管理(系统管理员)----
    _rule('POST', '/api/db/agent/(admission|collect|nms_not_confirm|scheduler|aliases)', 'admin'),
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
    # ---- Istio(设计见 istio_route/README.md):查看按集群读权限,由 handler 过滤;修改要「Istio 修改」:
    #      这里只粗检至少一个集群有,handler 再按组(组内每个集群)或目标集群细检 ----
    _rule('GET', '/api/istio/.*', 'login'),
    _rule('POST|PUT|DELETE', '/api/istio/.*', 'op_any', op='network.istio'),
    # 集群里的 VS 只能经 /api/istio/* 读写(下发内容由 master 按组标准生成),通用转发直接拒绝
    _rule('', '/api/agent/istio/.*', 'admin'),
    # ---- 节点均衡 ----
    _rule('GET', '/api/load-balance/(config|status|plan|logs)', 'global_read'),
    _rule('PUT', '/api/load-balance/config', 'custom', check=load_balance_config_check, write=True),
    _rule('POST', '/api/load-balance/check-pods', 'env_read', env='query.env'),
    _rule('POST', '/api/load-balance/(analyze|execute|cleanup)', 'op', env='query.env', op='balance.run'),
    # ---- 转发给 agent 的只读接口 ----
    _rule('GET', f'/api/({_AGENT_READ_PATHS})', 'env_read', env='query.env'),
    _rule('GET', '/api/agent/res/content', 'custom', check=res_content_check),
    # ---- 转发给 agent 的修改:按操作权限表(不限方法,换个方法也绕不过去)----
    _rule('', '/api/restart', 'op', env='query.env', op='workload.restart'),
    _rule('', '/api/agent/(statefulset|daemonset)/restart', 'op', env='query.env', op='workload.restart'),
    _rule('', '/api/scale', 'op', env='query.env', op='workload.scale'),
    _rule('', '/api/agent/statefulset/scale', 'op', env='query.env', op='workload.scale'),
    _rule('', '/api/cron', 'op', env='query.env', op='workload.schedule'),
    # 改镜像:有「更新镜像」权限直接放行,没有的只读用户走 UPDATE_IMAGE 白名单
    _rule('', '/api/update-image', 'custom', check=update_image_check, write=True),
    _rule('', '/api/pod/(delete_pod|delete_pods)', 'op', env='query.env', op='pod.delete'),  # delete_pod 是 GET
    _rule('', '/api/pod/modify_pod', 'op', env='query.env', op='pod.isolate'),
    _rule('', '/api/pod/auto_(dump|jstack|jfr|jvm_mem)', 'op', env='query.env', op='pod.jvm'),
    _rule('', '/api/agent/res/ops', 'custom', check=res_ops_check, write=True),
    _rule('', '/api/agent/res/delete', 'custom', check=res_delete_check, write=True),
    _rule('', '/api/nodes/(cordon|uncordon)', 'op', env='query.env', op='node.cordon'),
    _rule('', '/api/agent/coredns/save', 'op', env='query.env', op='network.coredns'),
    # 端口测试会从集群内向任意地址发起连接
    _rule('', '/api/agent/net/tcp', 'op', env='query.env', op='network.tcp'),
    # ---- 其余 /api/*:按集群读写及以上 ----
    _rule('', '/api/.*', 'env_write', env='query.env'),
)

# 没有任何规则命中(非 /api、/ws 路径):只有系统管理员能访问
FALLBACK = _rule('', '.*', 'admin')


def match(method, path):
    for rule in RULES:
        if rule.matches(method, path):
            return rule
    return FALLBACK


async def _refresh_policy():
    try:
        await permissions.load_policy()
    except Exception as exc:
        logger.error(f'读取权限与审批配置失败: {exc}')
        raise Deny(503, '读取权限配置失败,请稍后重试', 'policy_unavailable')


async def authorize(rule, request, principal):
    """校验已登录用户能否访问;不通过抛 Deny。登录状态与改密限制由中间件先判断。"""
    kind = rule.kind
    if kind in ('public', 'agent'):
        return
    # 后面的 principal.can 和 handler 里的判断都读 permissions 的缓存,这里先刷新(没过期不查库)
    await _refresh_policy()
    if kind in ('session', 'login'):
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
            raise Deny(403, '需要系统管理员权限', 'admin_required')
        return
    if kind == 'custom':
        await rule.check(request, principal)
        return
    if kind == 'op_any':
        if not principal.can_any(rule.op):
            raise Deny(403, f'当前账号在所有集群都不能执行「{permissions.op_name(rule.op)}」', 'operation_forbidden')
        return
    envs = await extract_envs(rule, request)
    if kind == 'env_read':
        _require_envs(envs)
        check_read_all(principal, envs)
    elif kind == 'env_read_opt':
        check_read_all(principal, envs)
    elif kind == 'op':
        _require_envs(envs)
        check_op_all(principal, rule.op, envs)
    elif kind == 'env_write':
        if not envs and principal.is_admin:
            return
        _require_envs(envs)
        check_write_all(principal, envs)
    else:
        raise Deny(403, '未知的权限规则', 'forbidden')


def perm_label(perm):
    return PERM_LABELS.get(perm, perm)
