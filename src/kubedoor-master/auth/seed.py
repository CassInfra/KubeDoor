"""内置账号初始化:master 每次启动执行,已存在就跳过(不会覆盖改过的口令)。

  kubedoor      内置超级管理员。出厂口令 kubedoor,首次登录强制修改
  Up4biLko1dNh  内置 Agent 连接账号。出厂口令 qCa22jDkfc9y(与旧版 nginx 预置账号一致,
                已部署的跨集群 agent 升级后不用改 MASTER_WS)

可用环境变量 KUBEDOOR_INIT_ADMIN_PASSWORD / KUBEDOOR_INIT_AGENT_PASSWORD 指定初始口令,只在首次创建时生效。
"""

import os

from loguru import logger

from .store import hash_password, store

SUPER_ADMIN = 'kubedoor'
AGENT_ACCOUNT = 'Up4biLko1dNh'

BUILTIN_ACCOUNTS = (
    (SUPER_ADMIN, 'admin', 'KUBEDOOR_INIT_ADMIN_PASSWORD', 'kubedoor', '内置超级管理员'),
    (AGENT_ACCOUNT, 'agent', 'KUBEDOOR_INIT_AGENT_PASSWORD', 'qCa22jDkfc9y', '内置 Agent 连接账号'),
)


async def seed_builtin_accounts():
    for username, role, env_key, factory, comment in BUILTIN_ACCOUNTS:
        if await store.get_user_by_name(username) is not None:
            continue
        password = os.environ.get(env_key, '').strip() or factory
        is_factory = password == factory
        created = await store.create_user(
            username, 'local', role, await hash_password(password), comment=comment, builtin=True,
            must_change=role == 'admin' and is_factory, password_default=is_factory,
        )
        if created:
            logger.info(f'已创建内置账号 {username}({comment}){",使用出厂口令" if is_factory else ""}')
