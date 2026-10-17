"""登录身份与权限计算(纯函数,不碰数据库)。

权限模型:
  全局角色 role:
    admin  管理员:所有集群读写 + 用户 / LDAP / Agent 管理
    rw     读写:所有集群读写(不能管理用户和 Agent)
    read   只读:所有集群只读(默认)
    none   无:默认看不到任何集群,只能访问单独授权的集群
    agent  Agent 账号:只能用来让 kubedoor-agent 连接 master,不能登录 Web
  集群授权 clusters:{env: rw / read / none},优先于全局角色;管理员和 Agent 账号不能设置。

  某个集群的有效权限 = 管理员 → rw;否则有集群授权就用授权,没有就按全局角色兜底。
  与集群无关的资源(全局屏蔽规则、节点均衡全局配置、AI 共享记忆等)只看全局角色。
"""

from dataclasses import dataclass, field
from types import MappingProxyType

ROLES = ('admin', 'rw', 'read', 'none', 'agent')
HUMAN_ROLES = ('admin', 'rw', 'read', 'none')
CLUSTER_PERMS = ('rw', 'read', 'none')

ROLE_LABELS = {'admin': '管理员', 'rw': '读写', 'read': '只读', 'none': '无', 'agent': 'Agent 账号'}
PERM_LABELS = {'rw': '读写', 'read': '只读', 'none': '无'}

_ROLE_DEFAULT_PERM = {'admin': 'rw', 'rw': 'rw', 'read': 'read', 'none': 'none', 'agent': 'none'}


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
            return 'rw'
        if not self.is_human:
            return 'none'
        return self.clusters.get(env, self.default_permission)

    def can_read(self, env):
        return bool(env) and self.permission(env) in ('read', 'rw')

    def can_write(self, env):
        return bool(env) and self.permission(env) == 'rw'

    @property
    def global_read(self):
        return self.role in ('admin', 'rw', 'read')

    @property
    def global_write(self):
        return self.role in ('admin', 'rw')

    @property
    def can_write_any(self):
        return self.global_write or any(p == 'rw' for p in self.clusters.values())

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
        visible = tuple(sorted(env for env, perm in self.clusters.items() if perm in ('read', 'rw')))
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
    """由 auth_users 行(dict-like)和集群授权构造 Principal。管理员/Agent 的集群授权一律忽略。"""
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


# 集群内部系统调用(CronJob 带内部 token、没有用户身份):等同管理员
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
