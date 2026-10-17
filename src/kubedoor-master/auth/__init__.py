"""KubeDoor 账号与权限(认证 + 授权 + 审计)。

业务代码只需要:
    from auth import principal_of, env_condition
    principal = principal_of(request)
    principal.can_read(env) / can_write(env) / filter_envs(envs) / env_scope()

详细设计见 auth/README.md。
"""

from .context import ANONYMOUS, client_ip, principal_of
from .principal import (
    CLUSTER_PERMS, HUMAN_ROLES, PERM_LABELS, ROLE_LABELS, ROLES, SYSTEM, Principal, env_condition,
)


def setup(app, clients):
    """注册认证中间件和接口。clients 是 master 的 agent 注册表(env -> 连接信息)。"""
    import utils

    from . import admin_api, handlers
    from .middleware import create_middleware

    async def known_envs():
        names = set(await utils.get_k8s_names())
        names.update(clients)
        return sorted(names)

    async def cluster_list():
        return [(env, bool(clients.get(env, {}).get('online'))) for env in await known_envs()]

    handlers.known_envs = known_envs
    admin_api.cluster_list = cluster_list
    app.middlewares.append(create_middleware())
    handlers.register_routes(app)
    admin_api.register_routes(app)


async def on_startup(app):
    """建内置账号。必须排在建表(init_db_and_schema)之后。"""
    from .seed import seed_builtin_accounts

    await seed_builtin_accounts()


__all__ = [
    'ANONYMOUS', 'CLUSTER_PERMS', 'HUMAN_ROLES', 'PERM_LABELS', 'ROLE_LABELS', 'ROLES', 'SYSTEM',
    'Principal', 'client_ip', 'env_condition', 'on_startup', 'principal_of', 'setup',
]
