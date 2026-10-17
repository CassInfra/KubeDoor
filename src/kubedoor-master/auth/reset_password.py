"""忘记口令时的救急工具,在 master 容器里执行(直接改库,不经过 Web):

    kubectl -n kubedoor exec -it deploy/kubedoor-master -- python3 -m auth.reset_password kubedoor

按提示输入新口令。重置后该账号的所有登录会话下线,下次登录必须先修改口令(Agent 账号不登录 Web,不要求)。
只能重置本地账号(LDAP 账号的口令在 LDAP 里管理)。账号如果因为输错口令被临时锁定,
等锁定时间过去(默认 15 分钟)或重启 master 即可。
"""

import asyncio
import getpass
import sys

import asyncpg

import db
from auth import passwords


async def reset(username, password):
    conn = await asyncpg.connect(host=db.PG_HOST, port=db.PG_PORT, user=db.PG_USER,
                                 password=db.PG_PASSWORD, database=db.PG_DATABASE, timeout=10)
    try:
        row = await conn.fetchrow(
            'SELECT id, username, source, role FROM auth_users WHERE lower(username)=lower($1)', username)
        if row is None:
            return f'账号 {username} 不存在'
        if row['source'] != 'local':
            return f'{row["username"]} 是 LDAP 账号,口令请在 LDAP 里修改'
        await conn.execute(
            'UPDATE auth_users SET password_hash=$1, must_change_password=$2, password_default=false, '
            'enabled=true, password_changed_at=now(), updated_at=now() WHERE id=$3',
            passwords.hash_password(password), row['role'] != 'agent', row['id'],
        )
        await conn.execute('DELETE FROM auth_sessions WHERE user_id=$1', row['id'])
        await conn.execute(
            "INSERT INTO audit_logs (username, action, target, detail) VALUES ('system', 'user.reset_password', $1, "
            "'{\"via\": \"reset_password 命令行\"}'::jsonb)", row['username'])
        return None
    finally:
        await conn.close()


def main():
    if len(sys.argv) != 2:
        print('用法: python3 -m auth.reset_password <用户名>')
        return 2
    username = sys.argv[1]
    password = getpass.getpass('新口令: ')
    if password != getpass.getpass('再输一次: '):
        print('两次输入不一致')
        return 1
    error = passwords.validate_new_password(password, username)
    if error:
        print(error)
        return 1
    error = asyncio.run(reset(username, password))
    if error:
        print(error)
        return 1
    print(f'已重置 {username} 的口令,所有会话已下线;下次登录需要先修改口令。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
