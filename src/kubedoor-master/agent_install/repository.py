"""任务状态持久化；明文可见字段与加密安装材料分开保存。"""

import json

import asyncpg
import db

from .kubernetes import InstallError


def unpack(row):
    if not row:
        return None
    result = dict(row)
    result['id'] = str(result['id'])
    result['payload'] = json.loads(result['payload']) if isinstance(result['payload'], str) else result['payload']
    return result


class Repository:
    async def create(self, task):
        await db.pg_execute(
            'INSERT INTO agent_installations(id,status,revision,env,target_uid,payload,encrypted,expires_at) '
            'VALUES($1::uuid,$2,$3,$4,$5,$6::jsonb,$7,$8)', task['id'], task['status'], task['revision'],
            task.get('env', ''), task.get('target_uid', ''), json.dumps(task['payload']), task['encrypted'], task['expires_at'])

    async def get(self, task_id):
        return unpack(await db.pg_fetchrow('SELECT * FROM agent_installations WHERE id=$1::uuid', task_id))

    async def list(self):
        return [unpack(row) for row in await db.pg_fetch(
            'SELECT * FROM agent_installations ORDER BY created_at DESC LIMIT 50')]

    async def unfinished(self):
        return [unpack(row) for row in await db.pg_fetch("SELECT * FROM agent_installations WHERE status='running'")]

    async def save(self, task):
        revision = task['revision']
        try:
            row = await db.pg_fetchrow(
                'UPDATE agent_installations SET status=$2,revision=revision+1,env=$3,target_uid=$4,'
                'payload=$5::jsonb,encrypted=$6,expires_at=$7,updated_at=now() '
                'WHERE id=$1::uuid AND revision=$8 RETURNING revision,created_at,updated_at',
                task['id'], task['status'], task.get('env', ''), task.get('target_uid', ''),
                json.dumps(task['payload']), task['encrypted'], task['expires_at'], revision)
        except asyncpg.UniqueViolationError:
            raise InstallError('这个集群或集群标识已有安装任务正在执行', 409) from None
        if not row:
            raise InstallError('安装任务已更新，请刷新后重试', 409)
        task.update(dict(row))

    async def same_cluster(self, env, target_uid, exclude):
        return [unpack(row) for row in await db.pg_fetch(
            'SELECT * FROM agent_installations WHERE id<>$1::uuid AND (env=$2 OR target_uid=$3) '
            "AND status NOT IN ('draft','expired') ORDER BY created_at DESC", exclude, env, target_uid)]

    async def expire(self):
        await db.pg_execute(
            "UPDATE agent_installations SET encrypted='',status=CASE WHEN status IN ('draft','ready') "
            "THEN 'expired' ELSE status END,updated_at=now(),revision=revision+1 "
            "WHERE expires_at<now() AND status<>'running' AND encrypted<>''")
