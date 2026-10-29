"""登录身份与权限计算(纯函数,不碰数据库)。

权限模型:
  全局角色 role:
    admin          系统管理员:所有集群都是「管理员」权限,不能单独授权;另外能做系统管理
                   (用户与权限、LDAP、企微、Agent 管理、权限与审批)
    cluster_admin  集群管理员:没有单独授权的集群按「管理员」;不能做系统管理
    rw             读写:没有单独授权的集群按「读写」
    read           只读:没有单独授权的集群按「只读」(默认)
    none           无:默认看不到任何集群,只能访问单独授权的集群
    agent          Agent 账号:只能用来让 kubedoor-agent 连接 master,不能登录 Web
  集群授权 clusters:{env: admin / rw / read / none},优先于全局角色;系统管理员和 Agent 账号不能设置。

  某个集群的有效权限 = 系统管理员 → admin;否则有集群授权就用授权,没有就按全局角色兜底。
  查看只要有效权限是 read 及以上;重启、删 Pod、改 CoreDNS 这类操作查操作权限表(auth/permissions.py,
  Web「权限与审批」页):can(op, env) = op 这一行在该集群有效权限那一列是否勾选,系统管理员也受「管理员」列约束。
  与集群无关的配置(没限定集群的告警屏蔽、节点均衡全局配置)按全局角色对应的那一列查表(can_global);
  AI 共享记忆等只看全局角色(global_write)。
"""

from dataclasses import dataclass, field
from types import MappingProxyType

from . import permissions

ROLES = ('admin', 'cluster_admin', 'rw', 'read', 'none', 'agent')
HUMAN_ROLES = ('admin', 'cluster_admin', 'rw', 'read', 'none')
CLUSTER_PERMS = ('admin', 'rw', 'read', 'none')

ROLE_LABELS = {
    'admin': '系统管理员', 'cluster_admin': '集群管理员', 'rw': '读写', 'read': '只读', 'none': '无',
    'agent': 'Agent 账号',
}
PERM_LABELS = {'admin': '管理员', 'rw': '读写', 'read': '只读', 'none': '无'}

_ROLE_DEFAULT_PERM = {'admin': 'admin', 'cluster_admin': 'admin', 'rw': 'rw', 'read': 'read', 'none': 'none',
                      'agent': 'none'}
_READABLE = ('read', 'rw', 'admin')


@dataclass(frozen=True)
class Principal:
    username: str
    role: str
    user_id: int | None = None
    source: str = 'local'  # local / ldap / system
    clusters: MappingProxyType = field(default_factory=lambda: MappingProxyType({}))
    via: str = 'session'  # session / basic / internal / system / anonymous-agent
    session_hash: str | None = None
    must_change_password: bool = False
    display_name: str = ''
    builtin: bool = False

    @property
    def is_admin(self):
        return self.role == 'admin'

    @property
    def is_human(self):
        return self.role in HUMAN_ROLES

    @property
    def is_agent(self):
        return self.role == 'agent'

    @property
    def default_permission(self):
        return _ROLE_DEFAULT_PERM.get(self.role, 'none')

    def permission(self, env):
        if self.role == 'admin':
            return 'admin'
        if not self.is_human:
            return 'none'
        return self.clusters.get(env, self.default_permission)

    def can_read(self, env):
        return bool(env) and self.permission(env) in _READABLE

    def can_write(self, env):
        """集群读写及以上(不看操作权限表):兜底规则、AI 旧动作、旧 agent 兼容用。"""
        return bool(env) and self.permission(env) in ('rw', 'admin')

    def can(self, op, env, policy=None):
        """能否在集群 env 执行操作 op:查权限表里该集群有效权限那一列。看不到的集群、未知操作一律不能。
        policy 不传就用 permissions 的缓存(请求里由鉴权中间件先 await permissions.load_policy())。"""
        return bool(env) and permissions.allowed(op, self.permission(env), policy)

    def can_global(self, op, policy=None):
        """与集群无关的配置(没限定集群的告警屏蔽、节点均衡全局配置):按全局角色对应的那一列查表。"""
        return self.is_human and permissions.allowed(op, self.default_permission, policy)

    def can_any(self, op, policy=None):
        """至少一个集群能做 op(粗检,具体集群由 handler 再判断):全局角色那一列,或者单独授权的某个集群。"""
        return self.can_global(op, policy) or any(self.can(op, env, policy) for env in self.clusters)

    def allowed_ops(self, env, policy=None):
        """该用户在集群 env 允许的全部操作。"""
        return [op for op in permissions.OP_KEYS if self.can(op, env, policy)]

    @property
    def global_read(self):
        return self.role in ('admin', 'cluster_admin', 'rw', 'read')

    @property
    def global_write(self):
        return self.role in ('admin', 'cluster_admin', 'rw')

    def env_scope(self):
        """数据过滤范围:
        ('all', ())     全部集群可见
        ('deny', envs)  除 envs 以外都可见(全局可读,个别集群设了「无」)
        ('allow', envs) 只有 envs 可见(全局「无」,个别集群单独授权)
        """
        if self.role == 'admin':
            return 'all', ()
        if self.global_read:
            hidden = tuple(sorted(env for env, perm in self.clusters.items() if perm == 'none'))
            return ('deny', hidden) if hidden else ('all', ())
        visible = tuple(sorted(env for env, perm in self.clusters.items() if perm in _READABLE))
        return 'allow', visible

    def filter_envs(self, envs):
        return [env for env in envs if self.can_read(env)]

    def to_public(self):
        """给前端 / AI 服务的权限描述(只含集群授权,有效权限由使用方按同样规则推导)。"""
        return {
            'username': self.username,
            'display_name': self.display_name,
            'role': self.role,
            'role_label': ROLE_LABELS.get(self.role, self.role),
            'source': self.source,
            'default_permission': self.default_permission,
            'clusters': dict(self.clusters),
            'is_admin': self.is_admin,
            'builtin': self.builtin,
            'must_change_password': self.must_change_password,
        }


def make_principal(user, clusters=None, via='session', session_hash=None, must_change=None):
    """由 auth_users 行(dict-like)和集群授权构造 Principal。系统管理员/Agent 的集群授权一律忽略。"""
    role = user['role']
    grants = {} if role in ('admin', 'agent') else dict(clusters or {})
    return Principal(
        username=user['username'],
        role=role,
        user_id=user['id'],
        source=user['source'],
        clusters=MappingProxyType(grants),
        via=via,
        session_hash=session_hash,
        must_change_password=bool(user.get('must_change_password')) if must_change is None else must_change,
        display_name=user.get('display_name') or '',
        builtin=bool(user.get('builtin')),
    )


# 集群内部系统调用(CronJob 带内部 token、没有用户身份):等同系统管理员
SYSTEM = Principal(username='system', role='admin', user_id=None, source='system', via='system')


def env_condition(principal, column, params):
    """按可见集群拼 SQL 过滤条件(asyncpg 占位符)。全部可见返回 None。

    column 是服务端写死的列名,不是用户输入。"""
    mode, envs = principal.env_scope()
    if mode == 'all':
        return None
    params.append(list(envs))
    placeholder = f'${len(params)}'
    if mode == 'deny':
        return f'NOT ({column} = ANY({placeholder}::text[]))'
    return f'{column} = ANY({placeholder}::text[])'
