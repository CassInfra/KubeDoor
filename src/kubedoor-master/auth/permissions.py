"""操作权限表(Web「权限与审批」页)和 AI 在 Pod 内执行命令用的命令清单。

  - 行 = 操作类型(OPERATIONS),列 = 集群有效权限 只读 / 读写 / 管理员,外加「审批」列
    (AI 执行这类操作前要不要提问人确认)。配置和具体集群无关。
  - 判定:principal.can(op, env) = 该集群的有效权限那一列是否勾选(auth/principal.py)。
    「管理员」列也可以取消,取消后包括系统管理员在内谁都不能做(相当于全站关闭)。
  - 命令清单:查看命令 / 高危命令,只用于 AI 的 pod_exec 和 kubectl exec 的分类
    (分类在 kubedoor-tools 和 agent 里做,清单由 master 下发);网页终端不受清单影响。

存储在 auth_settings(key = permission_policy),value 格式:
  {"ops": {key: {"read": bool, "rw": bool, "admin": bool, "approval": bool}},
   "commands": {"view": [...], "danger": [...]}}
读取时和代码里的默认值合并:新增的操作类型用默认值,已删除的 key 忽略;固定的审批值以代码为准。

缓存:进程内缓存,保存后立即更新,另有 10 秒 TTL 兜底(直接改库时)。鉴权中间件每个请求先
await load_policy()(没过期不查库),之后 principal.can 只读缓存,不碰数据库。
"""

import copy
import difflib
import json
import time
from pathlib import Path

import yaml
from loguru import logger

SETTING_KEY = 'permission_policy'
CACHE_TTL = 10  # 秒
RETRY_SECONDS = 2  # 读库失败时沿用旧缓存,隔这么久再试
LEVELS = ('read', 'rw', 'admin')  # 权限表的三列(集群有效权限);none 什么都不能做
LEVEL_LABELS = {'read': '只读', 'rw': '读写', 'admin': '管理员'}


class PolicyError(ValueError):
    """保存的配置不合法(返回给前端的提示)。"""


# ---------------------------------------------------------------------------
# 操作类型(权限表的行)
# ---------------------------------------------------------------------------
WEB, AI, WEB_AI = '网页', 'AI', '网页+AI'


def _op(key, group, name, desc, applies, rw=True, approval=False, fixed=False, na=False, highlight=False):
    """read 默认都不勾;rw 默认按现有行为;admin 默认都勾。
    na:审批列显示「—」(不是 AI 操作,或者是查看),审批固定为 false;fixed:审批固定为 approval 的值。"""
    return {
        'key': key, 'group': group, 'name': name, 'desc': desc, 'applies': applies,
        'defaults': {'read': False, 'rw': rw, 'admin': True},
        'approval': False if na else approval,
        'approval_fixed': fixed or na,
        'approval_na': na,
        'highlight': highlight,
    }


OPERATIONS = (
    # ---- 终端与命令 ----
    _op('pod.shell', '终端与命令', '网页 Pod 终端',
        '在网页上打开容器的交互式 shell,能执行任何命令,无法按命令限制。', WEB, na=True),
    _op('node.shell', '终端与命令', '网页节点终端',
        '在网页上打开节点终端:在该节点上起一个特权 Pod,以 root 进入宿主机。', WEB, na=True),
    _op('pod.exec.view', '终端与命令', 'AI 在 Pod 内执行查看命令',
        'AI 在容器里执行的每个命令都在「查看命令」清单里(参数还要通过内置检查),只读取信息。', AI),
    _op('pod.exec.other', '终端与命令', 'AI 在 Pod 内执行其他命令',
        'AI 在容器里执行既不是查看命令、也不是高危命令的命令,例如 touch、curl -X POST、kubectl cp。', AI),
    _op('pod.exec.danger', '终端与命令', 'AI 在 Pod 内执行高危命令',
        'AI 在容器里执行命中「高危命令」清单的命令(如 rm、kill、dd),固定需要提问人确认。', AI,
        approval=True, fixed=True),
    # ---- 工作负载 ----
    _op('workload.restart', '工作负载', '重启',
        '重启 Deployment / StatefulSet / DaemonSet。', WEB_AI, approval=True),
    _op('workload.scale', '工作负载', '扩缩容',
        '调整副本数,包括写入资源管控表的副本数。', WEB_AI, approval=True),
    _op('workload.schedule', '工作负载', '定时 / 周期重启、扩缩容',
        '新建定时或周期执行的重启、扩缩容任务。', WEB_AI, approval=True),
    _op('workload.image', '工作负载', '更新镜像',
        '更新 Deployment 的镜像。没有此权限的只读用户,在网页上仍可按 UPDATE_IMAGE 配置的'
        '集群、时间段和用户白名单更新(「管理员」列取消时白名单也不生效)。', WEB_AI, approval=True),
    # ---- Pod ----
    _op('pod.delete', 'Pod', '删除 Pod',
        '删除一个或多个 Pod(含 AI 用 kubectl drain 驱逐节点上的 Pod)。', WEB_AI, approval=True),
    _op('pod.isolate', 'Pod', '隔离 Pod',
        '把 Pod 从 Service 摘除并保留现场,用于排查问题。', WEB_AI, approval=True),
    _op('pod.jvm', 'Pod', 'JVM 诊断',
        '对 Java 进程执行 Dump / Jstack / JFR / 内存分析。', WEB_AI, approval=True),
    # ---- 资源 ----
    _op('resource.apply', '资源', '新建 / 编辑资源 YAML',
        '通过 YAML 新建或修改资源;AI 的 kubectl apply / patch / label 等修改。'
        '改 kube-system/coredns 还要「CoreDNS 修改」,改 Istio 资源还要「Istio 修改」。', WEB_AI, approval=True),
    _op('resource.delete', '资源', '删除资源',
        '删除资源(Pod 另见「删除 Pod」)。删 kube-system/coredns、Istio 资源同样还要对应的网络权限。',
        WEB_AI, approval=True),
    _op('resource.secret', '资源', '查看 Secret 内容',
        '查看 Secret 的 YAML(含解码前的数据);查看其他资源只要能访问该集群。', WEB_AI, na=True),
    _op('ai.k8s_write', '资源', 'AI 通用修改',
        'AI 用 kubectl / istioctl / K8S API 修改集群,能改的范围很大;'
        '除这一项外还要具体操作(重启、删除资源等)的权限。', AI, approval=True, highlight=True),
    _op('ai.diagnostic', '资源', 'AI 诊断工具',
        'AI 在 KubeDoor 自己的容器里使用 curl、openssl、yq -i、nc、nmap、tcpdump、iperf3 这类'
        '会发出请求、扫描端口、抓包或改写文件的诊断工具。', AI),
    # ---- 节点 ----
    _op('node.cordon', '节点', '禁止 / 允许调度',
        '把节点设为禁止调度(cordon)或恢复调度(uncordon)。', WEB_AI, approval=True),
    _op('balance.run', '节点', '节点均衡',
        '节点均衡的分析、执行、清理;修改全局配置按全局角色对应的列判断。', WEB_AI, approval=True),
    # ---- 资源管控 ----
    _op('control.edit', '资源管控', '资源管控',
        '新增 / 修改 / 同步管控服务,修改 requests/limits、JVM 等管控值,采集高峰期数据。', WEB_AI, approval=True),
    # ---- 网络 ----
    _op('network.coredns', '网络', 'CoreDNS 修改',
        '修改 kube-system/coredns:CoreDNS 管理页,或者通过资源 YAML / AI 修改。', WEB_AI,
        rw=False, approval=True),
    _op('network.tcp', '网络', '端口测试',
        '从集群内向任意地址发起 TCP 连接(CoreDNS 管理页的端口测试)。', WEB, na=True),
    _op('network.istio', '网络', 'Istio 修改',
        '全局VS管理里的所有修改,以及通过资源 YAML / AI 修改 Istio 资源。'
        '组级修改(组、组标准、规则)要求组内每个集群都有此权限。', WEB_AI, rw=False, approval=True),
    # ---- 告警 ----
    _op('alert.operate', '告警', '告警处理状态', '修改告警的处理状态。', WEB, na=True),
    _op('alert.silence', '告警', '告警屏蔽',
        '新建 / 修改 / 解除 / 续期 / 删除告警屏蔽规则;没限定集群的规则按全局角色对应的列判断。', WEB, na=True),
)
OPS_BY_KEY = {item['key']: item for item in OPERATIONS}
OP_KEYS = tuple(OPS_BY_KEY)

# 查看命令的内置参数检查(在 kubedoor-tools 里实现;这里只是给页面的说明)。不满足时这条命令不算查看命令,
# 继续按高危 / 其他命令判断,按那一行的权限和审批处理
BUILTIN_CHECKS = {
    'curl': '只允许 GET / HEAD / OPTIONS;不发数据、不上传、不写文件、不读配置;只访问 http/https',
    'wget': '只能 -O - 或 --spider',
    'nc': '只能 -z,不能 -e、-c、-l',
    'find': '不能 -exec、-execdir、-ok、-okdir、-delete、-fprint*、-fls',
    'sort': '不能 -o / --output;也不能 --compress-program(执行程序)、--files0-from',
    'date': '不能 -s / --set;不以 + 开头的位置参数(如 date 0101120026)也会设置时间',
    'ip': '不能 add / del / delete / set / change / replace / flush / append / prepend / exec 等修改'
          '(缩写如 ip a a 同样不行),不能修改 netns,不能 -batch',
    'mount': '只能不带参数或 -l',
    'env': '只能不带参数',
    'hostname': '不能带位置参数,不能 -F / --file、-b / --boot',
    'uniq': '只能有一个位置参数(第二个是输出文件,会被覆盖)',
    'ss': '不能 -K / --kill(关闭连接)、-D / --diag(写文件)',
    'file': '不能 -C / --compile(写 .mgc 文件)',
    'jps': '不能 -J(给 JVM 传参)',
    'grep': '递归搜索(-r / -R)要写明绝对路径的目录,不能是 /、/var、/run 这类包含 ServiceAccount token 的上级目录',
}
# 别名和同类命令用同一条说明(页面按命令名逐个查)
for _alias, _base in (('ncat', 'nc'), ('netcat', 'nc'), ('jstat', 'jps'), ('egrep', 'grep'), ('fgrep', 'grep')):
    BUILTIN_CHECKS[_alias] = BUILTIN_CHECKS[_base]


def op_name(op):
    return OPS_BY_KEY.get(op, {}).get('name', op)


def definitions():
    """给「权限与审批」页的行定义(有序)。"""
    return [copy.deepcopy(item) for item in OPERATIONS]


# ---------------------------------------------------------------------------
# 默认值
# ---------------------------------------------------------------------------
_DEFAULT_COMMANDS_FILE = Path(__file__).with_name('default_commands.json')
_default_commands_cache = None


def default_commands():
    """默认命令清单(auth/default_commands.json,和 kubedoor-tools 里的那份保持一致)。"""
    global _default_commands_cache
    if _default_commands_cache is None:
        data = json.loads(_DEFAULT_COMMANDS_FILE.read_text(encoding='utf-8'))
        _default_commands_cache = {'view': list(data['view']), 'danger': list(data['danger'])}
    return copy.deepcopy(_default_commands_cache)


def default_ops():
    return {item['key']: {**item['defaults'], 'approval': item['approval']} for item in OPERATIONS}


def default_policy():
    return {'ops': default_ops(), 'commands': default_commands()}


# ---------------------------------------------------------------------------
# 校验(保存时,严格)
# ---------------------------------------------------------------------------
MAX_COMMANDS = 300
MAX_COMMAND_LEN = 64
SHELL_META = frozenset(';&|<>()$`\t\n\r"\'\\')
_LIST_LABELS = {'view': '查看命令', 'danger': '高危命令'}


def _check_ops(ops):
    if not isinstance(ops, dict):
        raise PolicyError('ops 必须是对象')
    unknown = sorted(key for key in ops if key not in OPS_BY_KEY)
    if unknown:
        raise PolicyError(f"未知的操作类型:{', '.join(unknown[:5])}")
    result = {}
    for item in OPERATIONS:
        key, name = item['key'], item['name']
        given = ops.get(key)
        if given is None:  # 页面没传的行(比如新加的)用默认值
            given = {}
        elif not isinstance(given, dict):
            raise PolicyError(f'「{name}」的取值必须是对象')
        values = {}
        for field, default in (*item['defaults'].items(), ('approval', item['approval'])):
            value = given.get(field, default)
            if not isinstance(value, bool):
                raise PolicyError(f'「{name}」的 {field} 必须是 true 或 false')
            values[field] = value
        if values['read'] and not values['rw']:
            raise PolicyError(f'「{name}」:勾选了只读就必须勾选读写(只读 ⊆ 读写 ⊆ 管理员)')
        if values['rw'] and not values['admin']:
            raise PolicyError(f'「{name}」:勾选了读写就必须勾选管理员(只读 ⊆ 读写 ⊆ 管理员)')
        if item['approval_fixed']:  # 高危固定审批;「—」行不审批
            values['approval'] = item['approval']
        result[key] = values
    return result


def _check_item(kind, raw):
    label = _LIST_LABELS[kind]
    if not isinstance(raw, str):
        raise PolicyError(f'{label}里的每一项都必须是字符串')
    bad = sorted({ch for ch in raw if ch in SHELL_META})
    if bad:
        shown = ' '.join({'\t': '制表符', '\n': '换行', '\r': '回车'}.get(ch, ch) for ch in bad)
        raise PolicyError(f'{label}「{raw.strip()}」不能包含引号和 shell 元字符({shown})')
    item = ' '.join(raw.split())
    if not item:
        raise PolicyError(f'{label}里不能有空项')
    if len(item) > MAX_COMMAND_LEN:
        raise PolicyError(f'{label}「{item[:20]}…」太长(最多 {MAX_COMMAND_LEN} 个字符)')
    words = item.split(' ')
    if '/' in words[0]:
        raise PolicyError(f'{label}「{item}」只填命令名,不带路径(按命令的文件名匹配)')
    if kind == 'view':
        if len(words) > 1 or '*' in item:
            raise PolicyError(f'查看命令「{item}」只能是单个命令名,不能带参数或 *')
        return item
    if len(words) > 2:
        raise PolicyError(f'高危命令「{item}」只能是「命令」「前缀*」或「命令 参数」')
    if len(words) == 2:
        if '*' in item:
            raise PolicyError(f'高危命令「{item}」:「命令 参数」的写法不能带 *')
        return item
    if '*' in item and (item.count('*') > 1 or not item.endswith('*') or item == '*'):
        raise PolicyError(f'高危命令「{item}」:* 只能写在命令名末尾,表示前缀(如 mkfs*)')
    return item


def _check_commands(commands):
    if not isinstance(commands, dict):
        raise PolicyError('commands 必须是对象')
    result = {}
    for kind, label in _LIST_LABELS.items():
        items = commands.get(kind)
        if not isinstance(items, list):
            raise PolicyError(f'{label}清单必须是数组')
        if len(items) > MAX_COMMANDS:
            raise PolicyError(f'{label}清单最多 {MAX_COMMANDS} 项')
        cleaned = []
        for raw in items:
            item = _check_item(kind, raw)
            if item in cleaned:
                raise PolicyError(f'{label}「{item}」重复')
            cleaned.append(item)
        result[kind] = cleaned
    # 查看命令先判断,和高危命令重名的话高危检查就形同虚设。「命令 参数」是对某个命令的细化
    # (默认的 find 和 find -delete),不算重名
    plain = {item for item in result['danger'] if ' ' not in item and not item.endswith('*')}
    prefixes = [item[:-1] for item in result['danger'] if item.endswith('*')]
    for item in result['view']:
        if item in plain or any(item.startswith(prefix) for prefix in prefixes):
            raise PolicyError(f'「{item}」同时在查看命令和高危命令里,请从其中一个清单删除')
    return result


def validate_policy(ops, commands):
    """校验并规整页面提交的配置,返回 {'ops', 'commands'};不合法抛 PolicyError。
    单调(只读 ⊆ 读写 ⊆ 管理员);固定的审批值以代码为准;清单规则见 _check_item / _check_commands。"""
    return {'ops': _check_ops(ops), 'commands': _check_commands(commands)}


# ---------------------------------------------------------------------------
# 读取(合并默认值,宽松)
# ---------------------------------------------------------------------------
def _merge_ops(stored):
    stored = stored if isinstance(stored, dict) else {}
    result = {}
    for item in OPERATIONS:
        given = stored.get(item['key'])
        given = given if isinstance(given, dict) else {}
        values = {}
        for field, default in (*item['defaults'].items(), ('approval', item['approval'])):
            value = given.get(field, default)
            values[field] = value if isinstance(value, bool) else default
        # 库里被直接改成不单调时,按少给的方向收紧
        values['rw'] = values['rw'] and values['admin']
        values['read'] = values['read'] and values['rw']
        if item['approval_fixed']:
            values['approval'] = item['approval']
        result[item['key']] = values
    return result


def _merge_commands(stored):
    stored = stored if isinstance(stored, dict) else {}
    defaults = default_commands()
    result = {}
    for kind in _LIST_LABELS:
        items = stored.get(kind)
        if not isinstance(items, list):
            result[kind] = defaults[kind]
            continue
        cleaned = []
        for raw in items:
            item = ' '.join(raw.split()) if isinstance(raw, str) else ''
            if item and item not in cleaned:
                cleaned.append(item)
        result[kind] = cleaned
    return result


def merge_stored(value):
    """库里的 value(可能是旧版本保存的)→ 完整的 {'ops', 'commands'}。"""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = None
    value = value if isinstance(value, dict) else {}
    return {'ops': _merge_ops(value.get('ops')), 'commands': _merge_commands(value.get('commands'))}


def _from_row(row):
    if row is None:
        return {**default_policy(), 'updated_by': '', 'updated_at': None}
    return {**merge_stored(row.get('value')), 'updated_by': row.get('updated_by') or '',
            'updated_at': row.get('updated_at')}


# ---------------------------------------------------------------------------
# 缓存与读写
# ---------------------------------------------------------------------------
_cache = {'policy': None, 'expires': 0.0}


def invalidate_cache():
    _cache.update(policy=None, expires=0.0)


def current_policy():
    """缓存里的配置(只读,不要修改);还没读过库时是默认值。请求里由鉴权中间件先 await load_policy()。"""
    return _cache['policy'] or _from_row(None)


async def load_policy(force=False):
    """读取配置(合并默认值),缓存 CACHE_TTL 秒。读库失败时有旧缓存就沿用,没有就抛异常(按无权限处理)。

    返回 {'ops', 'commands', 'updated_by', 'updated_at'},只读,不要修改。"""
    now = time.monotonic()
    cached = _cache['policy']
    if cached is not None and not force and now < _cache['expires']:
        return cached
    from .store import store  # 延迟导入:store → principal → permissions

    try:
        row = await store.get_setting_row(SETTING_KEY)
    except Exception as exc:
        if cached is None:
            raise
        logger.warning(f'读取权限与审批配置失败,继续使用缓存: {exc}')
        _cache['expires'] = now + RETRY_SECONDS
        return cached
    policy = _from_row(row)
    _cache.update(policy=policy, expires=now + CACHE_TTL)
    return policy


async def save_policy(ops, commands, username):
    """校验后保存,立即更新缓存;返回保存后的配置。不合法抛 PolicyError。"""
    normalized = validate_policy(ops, commands)
    from .store import store

    row = await store.put_setting_row(SETTING_KEY, normalized, username)
    policy = {**normalized, 'updated_by': row.get('updated_by') or username, 'updated_at': row.get('updated_at')}
    _cache.update(policy=policy, expires=time.monotonic() + CACHE_TTL)
    return policy


def version_of(policy):
    """配置版本(AI 服务缓存用):库里 updated_at 的毫秒时间戳,没保存过是 0。"""
    updated_at = policy.get('updated_at')
    return int(updated_at.timestamp() * 1000) if updated_at else 0


# ---------------------------------------------------------------------------
# 判定
# ---------------------------------------------------------------------------
def allowed(op, level, policy=None):
    """权限表里 op 这一行、level(read / rw / admin)这一列是否勾选。未知操作、none 一律 False。"""
    if level not in LEVELS:
        return False
    row = (policy or current_policy())['ops'].get(op)
    return bool(row and row.get(level))


def allowed_levels(op, policy=None):
    policy = policy or current_policy()
    return [level for level in LEVELS if allowed(op, level, policy)]


def operations_levels(policy=None):
    """给前端 /api/auth/me:{op: 允许的集群权限级别}。前端 can(op, env) = 列表里有该集群的有效权限。"""
    policy = policy or current_policy()
    return {op: allowed_levels(op, policy) for op in OP_KEYS}


def needs_approval(op, policy=None):
    row = (policy or current_policy())['ops'].get(op)
    return bool(row and row.get('approval'))


# ---------------------------------------------------------------------------
# 页面 / 审计用
# ---------------------------------------------------------------------------
def admin_payload(policy):
    updated_at = policy.get('updated_at')
    return {
        'definitions': definitions(),
        'policy': {'ops': copy.deepcopy(policy['ops']), 'commands': copy.deepcopy(policy['commands'])},
        'defaults': default_policy(),
        'builtin_checks': dict(BUILTIN_CHECKS),
        'updated_by': policy.get('updated_by') or '',
        'updated_at': updated_at.isoformat() if updated_at else None,
        'version': version_of(policy),
    }


def policy_diff(before, after):
    """保存前后的差异(审计 permission.policy 的 detail):
    {'ops': {key: {'name': 名称, 字段: {'from', 'to'}}}, 'commands': {'view' / 'danger': ['+xx', '-yy']}};没变化返回 {}。"""
    changes = {}
    ops = {}
    for item in OPERATIONS:
        old, new = before['ops'].get(item['key'], {}), after['ops'].get(item['key'], {})
        fields = {field: {'from': old.get(field), 'to': new.get(field)}
                  for field in (*LEVELS, 'approval') if old.get(field) != new.get(field)}
        if fields:
            ops[item['key']] = {'name': item['name'], **fields}
    if ops:
        changes['ops'] = ops
    for kind in _LIST_LABELS:
        lines = [line for line in difflib.unified_diff(before['commands'][kind], after['commands'][kind],
                                                       lineterm='', n=0)
                 if line[:1] in '+-' and not line.startswith(('+++', '---'))]
        if lines:
            changes.setdefault('commands', {})[kind] = lines
    return changes


# ---------------------------------------------------------------------------
# 目标识别:改 kube-system/coredns 还要 network.coredns,改 Istio 资源还要 network.istio。
# kubedoor-tools 里有一份独立实现(AI 的 kubectl / api 用;master 镜像里没有 kubedoor-tools)。
# 判断不了的(YAML 解析失败、层级太深等)一律按都涉及处理
# ---------------------------------------------------------------------------
COREDNS_OP, ISTIO_OP = 'network.coredns', 'network.istio'
ISTIO_API_GROUPS = frozenset({'networking.istio.io', 'security.istio.io', 'telemetry.istio.io', 'extensions.istio.io'})
_ISTIO_KINDS = {
    'virtualservice': ('virtualservices', 'vs'),
    'destinationrule': ('destinationrules', 'dr'),
    'gateway': ('gateways', 'gw'),
    'serviceentry': ('serviceentries', 'se'),
    'sidecar': ('sidecars',),
    'workloadentry': ('workloadentries', 'we'),
    'workloadgroup': ('workloadgroups', 'wg'),
    'authorizationpolicy': ('authorizationpolicies', 'ap'),
    'peerauthentication': ('peerauthentications', 'pa'),
    'requestauthentication': ('requestauthentications', 'ra'),
    'telemetry': ('telemetries',),
    'wasmplugin': ('wasmplugins',),
    'envoyfilter': ('envoyfilters',),
    'proxyconfig': ('proxyconfigs',),
}
ISTIO_KINDS = frozenset(_ISTIO_KINDS)
ISTIO_RESOURCE_NAMES = frozenset(ISTIO_KINDS | {alias for aliases in _ISTIO_KINDS.values() for alias in aliases})
_CONFIGMAP_NAMES = frozenset({'configmap', 'configmaps', 'cm'})
_MAX_YAML_NODES = 5000
_MAX_LIST_DEPTH = 4


class _Undecidable(Exception):
    pass


def _api_group(api_version):
    if not isinstance(api_version, str) or not api_version.strip():
        return None  # 没写:按 kind 判断
    api_version = api_version.strip().lower()
    return api_version.split('/', 1)[0] if '/' in api_version else ''


def _object_targets(doc, found, budget, depth):
    budget[0] += 1
    if budget[0] > _MAX_YAML_NODES or depth > _MAX_LIST_DEPTH:
        raise _Undecidable()
    kind = doc.get('kind')
    kind = kind.strip().lower() if isinstance(kind, str) else ''
    items = doc.get('items')
    if kind.endswith('list') and isinstance(items, list):  # v1 List 等
        for item in items:
            if isinstance(item, dict):
                _object_targets(item, found, budget, depth + 1)
        return
    group = _api_group(doc.get('apiVersion'))
    if group in ISTIO_API_GROUPS or (group is None and kind in ISTIO_KINDS):
        found.add(ISTIO_OP)
    metadata = doc.get('metadata')
    metadata = metadata if isinstance(metadata, dict) else {}
    namespace = metadata.get('namespace') or 'default'  # 不写命名空间时 agent 用 default
    if kind == 'configmap' and group in ('', None) and namespace == 'kube-system' and metadata.get('name') == 'coredns':
        found.add(COREDNS_OP)


def yaml_targets(yaml_content):
    """YAML(可以多文档)→ (额外需要的操作, 是否解析成功)。解析失败时返回两个都有。"""
    if yaml_content is None or yaml_content == '':
        return [], True
    found = set()
    try:
        if not isinstance(yaml_content, str):
            raise _Undecidable()
        budget = [0]
        for doc in yaml.safe_load_all(yaml_content):
            if isinstance(doc, dict):
                _object_targets(doc, found, budget, 0)
    except Exception:
        return [COREDNS_OP, ISTIO_OP], False
    return [op for op in (COREDNS_OP, ISTIO_OP) if op in found], True


def resource_targets(resource_type, name, namespace):
    """资源类型(kind / 复数 / 简称,可带 .group)+ 名称 + 命名空间 → 额外需要的操作。"""
    found = []
    resource = (resource_type or '').strip().lower()
    base, _, group = resource.partition('.')
    if (group in ISTIO_API_GROUPS) if group else (base in ISTIO_RESOURCE_NAMES):
        found.append(ISTIO_OP)
    if base in _CONFIGMAP_NAMES and group in ('', 'v1') and (namespace or '').strip() == 'kube-system' \
            and (name or '').strip() == 'coredns':
        found.insert(0, COREDNS_OP)
    return found


def identify_targets_master(yaml_content=None, resource_type=None, name=None, namespace=None):
    """目标识别:返回额外需要的操作(network.coredns / network.istio 的子集,按这个顺序)。

    YAML(网页资源编辑的 yaml_content)和「资源类型 + 名称 + 命名空间」(网页删除资源)可以同时给,结果合并。"""
    found = set()
    if yaml_content is not None:
        found.update(yaml_targets(yaml_content)[0])
    if resource_type:
        found.update(resource_targets(resource_type, name, namespace))
    return [op for op in (COREDNS_OP, ISTIO_OP) if op in found]
