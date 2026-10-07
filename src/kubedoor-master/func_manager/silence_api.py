#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
告警屏蔽规则 REST 接口(/api/db/silence/*)

规则存储在 alert_silences 表,由 kubedoor-alarm 侧的 silence.py 负责匹配与拦截,
本模块只负责 Web 端的增删改查、命中预览与候选值提示。

matcher 语义(与 Alertmanager Silence 一致):
    {"key": "namespace", "op": "=", "value": "payment"}
    op: '='(等于) '!='(不等于) '=~'(正则全匹配) '!~'(正则不匹配)
    同一规则内多个 matcher 为 AND;多条规则之间为 OR。

时间一律以 ISO 8601(带时区)返回,由前端按浏览器本地时区渲染,避免服务端时区歧义。
"""

import json
import re
from datetime import datetime, timezone

from aiohttp import web
from loguru import logger

from db import pg_fetch, pg_fetchrow, pg_fetchval, pg_execute

VALID_OPS = ('=', '!=', '=~', '!~')

# matcher key → k8s_pod_alert_days 列名。用于"命中预览"把匹配条件翻译成 SQL。
# 不在此表中的 key(自定义 Prometheus 标签)无法在历史告警表里预览,会被跳过并提示。
PREVIEW_COLUMNS = {
    'env': 'env',
    'k8s': 'env',
    'cluster': 'env',
    'namespace': 'namespace',
    'pod': 'pod',
    'container': 'container',
    'alertname': 'alert_name',
    'alert_name': 'alert_name',
    'alertgroup': 'alert_group',
    'alert_group': 'alert_group',
    'severity': 'severity',
    'description': 'description',
}

# 状态推导:不落库,按当前时间实时计算,与 silence.py 的匹配判断保持一致
STATUS_SQL = """
    CASE
        WHEN revoked_at IS NOT NULL THEN 'revoked'
        WHEN now() < starts_at THEN 'pending'
        WHEN ends_at IS NOT NULL AND now() >= ends_at THEN 'expired'
        ELSE 'active'
    END
"""

SELECT_COLUMNS = f"""
    id, matchers, starts_at, ends_at, comment, created_by,
    revoked_at, revoked_by, match_count, last_match_at, created_at, updated_at,
    {STATUS_SQL} AS status
"""


class SilenceError(Exception):
    """参数校验失败,由 handler 转成 400。"""


def _iso(value):
    return value.isoformat() if isinstance(value, datetime) else None


def _parse_time(value, field):
    """解析前端传来的时间。接受 ISO 8601 或 'YYYY-MM-DD HH:MM:SS'。

    不带时区的输入按服务器本地时区解释(与用户在页面上看到的时间一致)。
    """
    if value in (None, ''):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip().replace('Z', '+00:00')
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            try:
                parsed = datetime.strptime(text, '%Y-%m-%d %H:%M:%S')
            except ValueError:
                raise SilenceError(f'{field} 时间格式无法解析: {value}')
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed


def validate_matchers(raw):
    """校验并规范化 matchers。返回 list[dict],非法则抛 SilenceError。"""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raise SilenceError('matchers 不是合法的 JSON')
    if not isinstance(raw, list) or not raw:
        raise SilenceError('至少需要一个匹配条件')

    normalized = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise SilenceError(f'第 {index} 个匹配条件格式不正确')
        key = str(item.get('key') or '').strip()
        op = str(item.get('op') or '=').strip()
        value = item.get('value')
        value = '' if value is None else str(value)
        if not key:
            raise SilenceError(f'第 {index} 个匹配条件缺少标签名')
        if op not in VALID_OPS:
            raise SilenceError(f'第 {index} 个匹配条件的操作符非法: {op}(可选 {"、".join(VALID_OPS)})')
        if op in ('=~', '!~'):
            if not value:
                raise SilenceError(f'第 {index} 个匹配条件的正则不能为空')
            try:
                re.compile(value)
            except re.error as exc:
                raise SilenceError(f'第 {index} 个匹配条件的正则无法编译: {exc}')
        normalized.append({'key': key, 'op': op, 'value': value})
    return normalized


def _remaining_seconds(row, now):
    """生效中/待生效规则的剩余秒数;长期有效返回 None,已结束返回 0。"""
    status = row['status']
    if status in ('expired', 'revoked'):
        return 0
    if row['ends_at'] is None:
        return None
    delta = (row['ends_at'] - now).total_seconds()
    return max(0, int(delta))


def _serialize(row, now):
    matchers = row['matchers']
    if isinstance(matchers, str):
        try:
            matchers = json.loads(matchers)
        except ValueError:
            matchers = []
    return {
        'id': row['id'],
        'matchers': matchers,
        'starts_at': _iso(row['starts_at']),
        'ends_at': _iso(row['ends_at']),
        'comment': row['comment'],
        'created_by': row['created_by'],
        'revoked_at': _iso(row['revoked_at']),
        'revoked_by': row['revoked_by'],
        'match_count': row['match_count'],
        'last_match_at': _iso(row['last_match_at']),
        'created_at': _iso(row['created_at']),
        'updated_at': _iso(row['updated_at']),
        'status': row['status'],
        'remaining_seconds': _remaining_seconds(row, now),
    }


def _error(message, status=400):
    return web.json_response({'success': False, 'msg': message}, status=status)


def _affected(result):
    """从 asyncpg 的状态串('UPDATE 3' / 'DELETE 1')里取受影响行数。"""
    try:
        return int(str(result).rsplit(' ', 1)[-1])
    except (ValueError, IndexError):
        return 0


# ============================================================================
# 查询
# ============================================================================
async def silence_list(request):
    """屏蔽规则列表。

    query: status(active/pending/expired/revoked/all) keyword page pageSize
    响应同时带回各状态计数,供页面顶部标签页直接使用,省一次请求。
    """
    q = request.query
    status = (q.get('status') or 'all').strip()
    keyword = (q.get('keyword') or '').strip()
    page = max(1, int(q.get('page') or 1))
    page_size = min(200, max(1, int(q.get('pageSize') or 20)))

    params = []
    where = []
    if status and status != 'all':
        if status not in ('active', 'pending', 'expired', 'revoked'):
            return _error(f'状态取值非法: {status}')
        params.append(status)
        where.append(f"({STATUS_SQL.strip()}) = ${len(params)}")
    if keyword:
        params.append(f'%{keyword}%')
        placeholder = f'${len(params)}'
        # 备注、创建人、matchers 原文一起模糊搜
        conds = (
            f"comment ILIKE {placeholder} OR created_by ILIKE {placeholder} "
            f"OR matchers::text ILIKE {placeholder}"
        )
        # 纯数字额外按规则 ID 精确匹配,支持从告警详情页的「已屏蔽」标记直接定位规则
        if keyword.isdigit():
            params.append(int(keyword))
            conds += f" OR id = ${len(params)}"
        where.append(f'({conds})')
    where_clause = ('WHERE ' + ' AND '.join(where)) if where else ''

    params.append(page_size)
    limit_p = f'${len(params)}'
    params.append((page - 1) * page_size)
    offset_p = f'${len(params)}'

    rows = await pg_fetch(
        f"SELECT {SELECT_COLUMNS} FROM alert_silences {where_clause} "
        # 生效中的排最前,其次待生效,最后过期/解除;同组内按结束时间近的优先
        f"ORDER BY CASE ({STATUS_SQL.strip()}) "
        f"  WHEN 'active' THEN 0 WHEN 'pending' THEN 1 WHEN 'expired' THEN 2 ELSE 3 END, "
        f"created_at DESC LIMIT {limit_p} OFFSET {offset_p}",
        *params,
    )

    count_row = await pg_fetchrow(
        f"SELECT count(*) AS total, "
        f"count(*) FILTER (WHERE ({STATUS_SQL.strip()}) = 'active')  AS active, "
        f"count(*) FILTER (WHERE ({STATUS_SQL.strip()}) = 'pending') AS pending, "
        f"count(*) FILTER (WHERE ({STATUS_SQL.strip()}) = 'expired') AS expired, "
        f"count(*) FILTER (WHERE ({STATUS_SQL.strip()}) = 'revoked') AS revoked "
        f"FROM alert_silences"
    )

    now = datetime.now(timezone.utc)
    stats = {
        'total': count_row['total'],
        'active': count_row['active'],
        'pending': count_row['pending'],
        'expired': count_row['expired'],
        'revoked': count_row['revoked'],
    }
    # 分页总数:无筛选时直接用 stats,有筛选时按同样的 where 再数一次
    # (params 末尾两个是 limit/offset,计数时要去掉)
    if where_clause:
        filtered_total = await pg_fetchval(
            f"SELECT count(*) FROM alert_silences {where_clause}", *params[:-2]
        )
    else:
        filtered_total = stats['total']

    return web.json_response(
        {
            'success': True,
            'data': [_serialize(r, now) for r in rows],
            'total': filtered_total,
            'stats': stats,
        }
    )


async def silence_label_values(request):
    """某个标签在历史告警中的候选值,给新建表单的下拉做提示。"""
    key = (request.query.get('key') or '').strip()
    column = PREVIEW_COLUMNS.get(key)
    if not column:
        # 自定义标签没有历史值可提示,返回空列表而不是报错(前端仍可自由输入)
        return web.json_response({'success': True, 'data': []})
    limit = min(500, max(1, int(request.query.get('limit') or 200)))
    rows = await pg_fetch(
        f"SELECT DISTINCT {column} AS v FROM k8s_pod_alert_days "
        f"WHERE {column} <> '' AND start_time >= now() - INTERVAL '30 days' "
        f"ORDER BY v LIMIT {limit}"
    )
    return web.json_response({'success': True, 'data': [r['v'] for r in rows]})


async def silence_preview(request):
    """命中预览:用给定 matchers 去匹配最近 N 天的历史告警,估算屏蔽范围。

    自定义标签无法在告警表中还原,这类条件会被跳过并在 unsupported_keys 中返回,
    此时预览结果偏大(实际生效时条件更严),前端需给出提示。
    """
    body = await request.json()
    try:
        matchers = validate_matchers(body.get('matchers'))
    except SilenceError as exc:
        return _error(str(exc))
    days = min(90, max(1, int(body.get('days') or 7)))

    params = []
    conditions = []
    unsupported = []
    for matcher in matchers:
        column = PREVIEW_COLUMNS.get(matcher['key'])
        if not column:
            unsupported.append(matcher['key'])
            continue
        params.append(matcher['value'])
        placeholder = f'${len(params)}'
        op = matcher['op']
        if op == '=':
            conditions.append(f'{column} = {placeholder}')
        elif op == '!=':
            conditions.append(f'{column} <> {placeholder}')
        elif op == '=~':
            # PG 的 ~ 是部分匹配,补上锚点以对齐 Alertmanager/Python 的全匹配语义
            conditions.append(f"{column} ~ ('^(?:' || {placeholder} || ')$')")
        else:
            conditions.append(f"{column} !~ ('^(?:' || {placeholder} || ')$')")

    where = [f"start_time >= now() - INTERVAL '{days} days'"] + conditions
    where_clause = ' AND '.join(where)

    try:
        total_row = await pg_fetchrow(
            f"SELECT count(*) AS c, COALESCE(sum(count_firing), 0) AS firings "
            f"FROM k8s_pod_alert_days WHERE {where_clause}",
            *params,
        )
        samples = await pg_fetch(
            f"SELECT alert_name, env, namespace, pod, severity, sum(count_firing) AS firings "
            f"FROM k8s_pod_alert_days WHERE {where_clause} "
            f"GROUP BY alert_name, env, namespace, pod, severity "
            f"ORDER BY firings DESC LIMIT 10",
            *params,
        )
    except Exception as exc:
        # 正则语法 PG 不认(如 Python 特有的 (?i) 写法)时给出可读提示
        logger.warning(f'屏蔽规则预览失败: {exc}')
        return _error(f'预览失败,请检查正则是否为 PostgreSQL 兼容语法: {exc}')

    return web.json_response(
        {
            'success': True,
            'days': days,
            'matched': total_row['c'],
            'firings': int(total_row['firings']),
            'unsupported_keys': unsupported,
            'samples': [
                {
                    'alert_name': r['alert_name'],
                    'env': r['env'],
                    'namespace': r['namespace'],
                    'pod': r['pod'],
                    'severity': r['severity'],
                    'firings': int(r['firings']),
                }
                for r in samples
            ],
        }
    )


# ============================================================================
# 写入
# ============================================================================
async def silence_add(request):
    d = await request.json()
    try:
        matchers = validate_matchers(d.get('matchers'))
        starts_at = _parse_time(d.get('starts_at'), '开始时间') or datetime.now().astimezone()
        ends_at = _parse_time(d.get('ends_at'), '结束时间')
    except SilenceError as exc:
        return _error(str(exc))
    if ends_at and ends_at <= starts_at:
        return _error('结束时间必须晚于开始时间')

    row = await pg_fetchrow(
        "INSERT INTO alert_silences (matchers, starts_at, ends_at, comment, created_by) "
        "VALUES ($1::jsonb, $2, $3, $4, $5) RETURNING id",
        json.dumps(matchers, ensure_ascii=False),
        starts_at,
        ends_at,
        str(d.get('comment') or '').strip(),
        str(d.get('created_by') or '').strip(),
    )
    logger.info(f'新建告警屏蔽规则 #{row["id"]}: {matchers} 至 {ends_at or "长期"}')
    return web.json_response({'success': True, 'msg': '屏蔽规则已创建', 'id': row['id']})


async def silence_edit(request):
    d = await request.json()
    silence_id = d.get('id')
    if not silence_id:
        return _error('缺少规则 ID')
    try:
        matchers = validate_matchers(d.get('matchers'))
        starts_at = _parse_time(d.get('starts_at'), '开始时间')
        ends_at = _parse_time(d.get('ends_at'), '结束时间')
    except SilenceError as exc:
        return _error(str(exc))
    if starts_at is None:
        return _error('开始时间不能为空')
    if ends_at and ends_at <= starts_at:
        return _error('结束时间必须晚于开始时间')

    result = await pg_execute(
        "UPDATE alert_silences SET matchers = $1::jsonb, starts_at = $2, ends_at = $3, "
        "comment = $4, created_by = $5 WHERE id = $6",
        json.dumps(matchers, ensure_ascii=False),
        starts_at,
        ends_at,
        str(d.get('comment') or '').strip(),
        str(d.get('created_by') or '').strip(),
        int(silence_id),
    )
    if _affected(result) == 0:
        return _error('规则不存在', status=404)
    logger.info(f'更新告警屏蔽规则 #{silence_id}')
    return web.json_response({'success': True, 'msg': '屏蔽规则已更新'})


async def silence_revoke(request):
    """解除屏蔽:标记 revoked_at,规则立即失效但记录保留可追溯。"""
    d = await request.json()
    silence_id = d.get('id')
    if not silence_id:
        return _error('缺少规则 ID')
    result = await pg_execute(
        "UPDATE alert_silences SET revoked_at = now(), revoked_by = $1 "
        "WHERE id = $2 AND revoked_at IS NULL",
        str(d.get('revoked_by') or '').strip(),
        int(silence_id),
    )
    if _affected(result) == 0:
        return _error('规则不存在或已解除', status=404)
    logger.info(f'解除告警屏蔽规则 #{silence_id}')
    return web.json_response({'success': True, 'msg': '已解除屏蔽'})


async def silence_extend(request):
    """续期:把结束时间往后延,同时清掉解除标记让规则重新生效。"""
    d = await request.json()
    silence_id = d.get('id')
    if not silence_id:
        return _error('缺少规则 ID')
    try:
        ends_at = _parse_time(d.get('ends_at'), '结束时间')
    except SilenceError as exc:
        return _error(str(exc))

    now = datetime.now().astimezone()
    if ends_at and ends_at <= now:
        return _error('续期后的结束时间必须晚于当前时间')

    # 已过期的规则续期时,把开始时间也拉到当前,避免留下一段"历史空窗"
    result = await pg_execute(
        "UPDATE alert_silences SET ends_at = $1, revoked_at = NULL, revoked_by = '', "
        "starts_at = LEAST(starts_at, now()) WHERE id = $2",
        ends_at,
        int(silence_id),
    )
    if _affected(result) == 0:
        return _error('规则不存在', status=404)
    logger.info(f'续期告警屏蔽规则 #{silence_id} 至 {ends_at or "长期"}')
    return web.json_response({'success': True, 'msg': '已续期'})


async def silence_delete(request):
    d = await request.json()
    silence_id = d.get('id')
    if not silence_id:
        return _error('缺少规则 ID')
    result = await pg_execute("DELETE FROM alert_silences WHERE id = $1", int(silence_id))
    if _affected(result) == 0:
        return _error('规则不存在', status=404)
    logger.info(f'删除告警屏蔽规则 #{silence_id}')
    return web.json_response({'success': True, 'msg': '屏蔽规则已删除'})


def register_routes(app):
    app.router.add_get('/api/db/silence/list', silence_list)
    app.router.add_get('/api/db/silence/label_values', silence_label_values)
    app.router.add_post('/api/db/silence/preview', silence_preview)
    app.router.add_post('/api/db/silence/add', silence_add)
    app.router.add_post('/api/db/silence/edit', silence_edit)
    app.router.add_post('/api/db/silence/revoke', silence_revoke)
    app.router.add_post('/api/db/silence/extend', silence_extend)
    app.router.add_post('/api/db/silence/delete', silence_delete)
