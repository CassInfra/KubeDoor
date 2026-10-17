"""操作审计:登录、账号与权限变更、所有写操作。写库失败只打日志,不影响业务。"""

import asyncio
import json
from datetime import datetime

from loguru import logger

import db

_tasks = set()

# 查询参数里这些键不入审计(可能带凭据)
_SECRET_KEYS = ('password', 'token', 'secret', 'authorization', 'kubeconfig', 'api_key')


def safe_params(params, limit=20):
    data = {}
    for key, value in list(params.items())[:limit]:
        lowered = key.lower()
        data[key] = '***' if any(word in lowered for word in _SECRET_KEYS) else str(value)[:200]
    return data


async def record(username, action, *, env='', target='', detail=None, ip='', success=True):
    try:
        await db.pg_execute(
            'INSERT INTO audit_logs (username, action, env, target, detail, ip, success) '
            'VALUES ($1,$2,$3,$4,$5::jsonb,$6,$7)',
            username or '', action, env or '', (target or '')[:500],
            json.dumps(detail, ensure_ascii=False, default=str) if detail is not None else None,
            (ip or '')[:64], bool(success),
        )
    except Exception as exc:
        logger.warning(f'写审计日志失败: {action} {target}: {exc}')


def record_bg(username, action, **kwargs):
    """在后台写审计,不阻塞请求。"""
    try:
        task = asyncio.get_running_loop().create_task(record(username, action, **kwargs))
    except RuntimeError:
        return
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def query(username='', action='', env='', keyword='', start=None, end=None, success=None, page=1, page_size=20):
    where, params = [], []

    def add(sql, value):
        params.append(value)
        where.append(sql.replace('?', f'${len(params)}'))

    if username:
        add('lower(username) = lower(?)', username)
    if action:
        add('action = ?', action)
    if env:
        add('env = ?', env)
    if keyword:
        params.append(f'%{keyword}%')
        placeholder = f'${len(params)}'
        where.append(f'(target ILIKE {placeholder} OR detail::text ILIKE {placeholder})')
    if start:
        add('ts >= ?', datetime.fromisoformat(start).astimezone())
    if end:
        add('ts < ?', datetime.fromisoformat(end).astimezone())
    if success is not None:
        add('success = ?', bool(success))
    clause = ('WHERE ' + ' AND '.join(where)) if where else ''
    total = await db.pg_fetchval(f'SELECT count(*) FROM audit_logs {clause}', *params)
    params.extend([page_size, (page - 1) * page_size])
    rows = await db.pg_fetch(
        f'SELECT id, ts, username, action, env, target, detail, ip, success FROM audit_logs {clause} '
        f'ORDER BY ts DESC, id DESC LIMIT ${len(params) - 1} OFFSET ${len(params)}',
        *params,
    )
    return rows, total


async def actions():
    rows = await db.pg_fetch(
        "SELECT DISTINCT action FROM audit_logs WHERE ts >= now() - INTERVAL '90 days' ORDER BY action LIMIT 500")
    return [row['action'] for row in rows]
