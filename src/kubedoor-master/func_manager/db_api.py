#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据库 REST 接口(前端不直传 SQL,全部走这里的参数化接口)

全部使用 asyncpg 参数化查询,彻底消除 SQL 注入。
查询类接口统一返回 JSONCompact 风格结构(前端 Vue 视图按此格式解析):
    {"success": true, "data": [[...行...]], "meta": [{"name": "列名"}, ...]}
写入类接口返回 {"success": true, "msg": "..."}。
"""

import json
from datetime import datetime, date
from decimal import Decimal
from aiohttp import web
from loguru import logger

import utils
from db import pg_fetch, pg_fetchval, pg_execute


def _cell(v):
    """把 PG 返回值序列化为 JSON 友好的标量(对齐原 CK JSONCompact 输出)。"""
    if isinstance(v, datetime):
        # timestamptz 由 asyncpg 解码成 UTC 时间,先转本地时区(容器 TZ=Asia/Shanghai)再格式化
        return v.astimezone().strftime('%Y-%m-%d %H:%M:%S')
    if isinstance(v, date):
        return v.strftime('%Y-%m-%d')
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, bool):
        return 1 if v else 0  # 前端历史上按 CK 的 0/1 处理布尔
    return v


def _table_response(rows, columns):
    """构建 {success,data,meta} 结构。rows 为 asyncpg.Record 列表,columns 为列名序列。"""
    data = [[_cell(r[i]) for i in range(len(columns))] for r in rows]
    meta = [{"name": c} for c in columns]
    return web.json_response({"success": True, "data": data, "meta": meta, "rows": len(data)})


# ============================================================================
# 资源管控表 k8s_res_control
# ============================================================================
RES_CONTROL_COLUMNS = [
    'env', 'namespace', 'deployment', 'pod_count_init', 'pod_count', 'pod_count_manual',
    'p95_pod_cpu_pct', 'p95_pod_mem_pct', 'request_cpu_m', 'request_mem_mb',
    'limit_cpu_m', 'limit_mem_mb', 'update', 'pod_mem_saved_mb', 'pod_qps',
    'pod_g1gc_qps', 'pod_count_ai', 'pod_qps_ai', 'pod_load_ai', 'pod_g1gc_qps_ai', 'update_ai',
    'jvm_xms_bytes', 'jvm_xmx_bytes', 'jvm_xss_bytes', 'jvm_max_metaspace_bytes',
    'p95_pod_heap_pct',
    'p95_pod_g1e_pct',
]


_JVM_FIELDS = ('jvm_xms_bytes', 'jvm_xmx_bytes', 'jvm_xss_bytes', 'jvm_max_metaspace_bytes')


def _jvm_updates(data):
    """仅更新请求明确携带的 JVM 字段，旧版表单不会清空已采集值。"""
    values = {}
    for field in _JVM_FIELDS:
        if field not in data:
            continue
        value = data[field]
        if value is not None and (type(value) is not int or not 0 <= value <= 9007199254740991):
            raise web.HTTPBadRequest(text=f'{field} 必须是非负整数的字节数或 null')
        values[field] = value
    xms, xmx = values.get('jvm_xms_bytes'), values.get('jvm_xmx_bytes')
    if xms is not None and xmx is not None and xms > xmx and xmx > 0:
        raise web.HTTPBadRequest(text='Xms 不能大于 Xmx')
    return values


def _pod_count_error(message):
    """带 message 的 400，前端 http 拦截器会把 message 提示给用户。"""
    return web.HTTPBadRequest(
        text=json.dumps({"success": False, "message": message}, ensure_ascii=False),
        content_type='application/json',
    )


def _pod_count_manual(data):
    """指定Pod：0 表示暂不启动 Pod，-1 表示按 AI推荐 / 当日Pod 管控。"""
    try:
        count = int(data['pod_count_manual'])
    except (KeyError, TypeError, ValueError):
        raise _pod_count_error('指定Pod 必须是整数')
    if count < -1:
        raise _pod_count_error('指定Pod 不能小于 -1')
    return count


async def _ensure_pod_count_fallback(env, namespace, deployment):
    """指定Pod=-1 时准入回退到 AI推荐 / 当日Pod。手动新增且还没采集过的服务当日Pod 为 0，
    回退会把副本数改成 0，所以只允许采集过高峰期数据或已有 AI推荐的服务设为 -1。"""
    has_fallback = await pg_fetchval(
        'SELECT "update" IS NOT NULL OR pod_count_ai >= 0 FROM k8s_res_control '
        'WHERE env = $1 AND namespace = $2 AND deployment = $3',
        env, namespace, deployment,
    )
    if has_fallback is False:
        raise _pod_count_error('该服务还没有采集到高峰期数据，指定Pod 不能为 -1，请填写 Pod 数(0 表示暂不启动)')


async def res_envs(request):
    """distinct env(资源管控表)"""
    rows = await pg_fetch("SELECT DISTINCT env FROM k8s_res_control ORDER BY env")
    return _table_response(rows, ['env'])


async def res_namespaces(request):
    rows = await pg_fetch(
        "SELECT DISTINCT namespace FROM k8s_res_control WHERE env = $1 ORDER BY namespace",
        request.query.get('env'),
    )
    return _table_response(rows, ['namespace'])


async def res_deployments(request):
    rows = await pg_fetch(
        "SELECT DISTINCT deployment FROM k8s_res_control WHERE env = $1 AND namespace = $2 ORDER BY deployment",
        request.query.get('env'), request.query.get('namespace'),
    )
    return _table_response(rows, ['deployment'])


async def res_max_day(request):
    """取 update 出现次数最多的那天(格式 YYYY-MM-DD)"""
    rows = await pg_fetch(
        "SELECT to_char(\"update\", 'YYYY-MM-DD') AS d FROM k8s_res_control "
        "WHERE env = $1 GROUP BY d ORDER BY count(*) DESC LIMIT 1",
        request.query.get('env'),
    )
    return _table_response(rows, ['d'])


async def res_list(request):
    """资源管控列表(带过滤)。返回全列 {data,meta}。"""
    q = request.query
    where = []
    params = []

    def add(cond, val):
        params.append(val)
        return cond.replace('?', f'${len(params)}')

    if q.get('namespace'):
        where.append(add("namespace = ?", q.get('namespace')))
    if q.get('deployment'):
        where.append(add("deployment = ?", q.get('deployment')))
    if q.get('env'):
        where.append(add("env = ?", q.get('env')))
    if q.get('keyword'):
        kw = f"%{q.get('keyword')}%"
        params.append(kw)
        p1 = f"${len(params)}"
        params.append(kw)
        p2 = f"${len(params)}"
        where.append(f"(deployment ILIKE {p1} OR namespace ILIKE {p2})")

    cols = ", ".join(f'"{c}"' if c == 'update' else c for c in RES_CONTROL_COLUMNS)
    sql = f"SELECT {cols} FROM k8s_res_control"
    if where:
        sql += " WHERE " + " AND ".join(where)
    rows = await pg_fetch(sql, *params)
    return _table_response(rows, RES_CONTROL_COLUMNS)


async def res_add(request):
    """新增管控服务(原前端 addData 的 INSERT)"""
    d = await request.json()
    pod_count_manual = _pod_count_manual(d)
    if pod_count_manual < 0:
        # 手动新增的服务没有采集数据(当日Pod=0)，-1 会被准入回退成 0 副本
        raise _pod_count_error('新增服务必须填写指定Pod(0 表示暂不启动 Pod)，不能为 -1')
    jvm = _jvm_updates(d)
    columns = ['env', 'namespace', 'deployment', 'pod_count_manual', 'limit_cpu_m',
               'limit_mem_mb', 'request_cpu_m', 'request_mem_mb', *jvm]
    values = [d['env'], d['namespace'], d['deployment'], pod_count_manual,
              int(d['limit_cpu_m']), int(d['limit_mem_mb']), int(d['request_cpu_m']),
              int(d['request_mem_mb']), *jvm.values()]
    await pg_execute(
        f"INSERT INTO k8s_res_control ({','.join(columns)}) "
        f"VALUES ({','.join(f'${i}' for i in range(1, len(values) + 1))})",
        *values,
    )
    utils.invalidate_admis_cache()
    return web.json_response({"success": True, "msg": "新增成功"})


async def res_edit(request):
    """编辑管控服务，保存后于下次发布或滚动重启应用。"""
    d = await request.json()
    pod_count_manual = _pod_count_manual(d)
    if pod_count_manual == -1:
        await _ensure_pod_count_fallback(d['env'], d['namespace'], d['deployment'])
    jvm = _jvm_updates(d)
    values = [int(d['limit_mem_mb']), int(d['limit_cpu_m']), pod_count_manual]
    assignments = ['limit_mem_mb=$1', 'limit_cpu_m=$2', 'pod_count_manual=$3']
    for field, value in jvm.items():
        values.append(value)
        assignments.append(f'{field}=${len(values)}')
    offset = len(values)
    values.extend([d['env'], d['namespace'], d['deployment']])
    await pg_execute(
        f"UPDATE k8s_res_control SET {', '.join(assignments)} "
        f"WHERE env=${offset + 1} AND namespace=${offset + 2} AND deployment=${offset + 3}",
        *values,
    )
    utils.invalidate_admis_cache()
    return web.json_response({"success": True, "msg": "更新成功"})


async def res_update_pod_count(request):
    """仅更新 pod_count_manual(监控页 updatePodCount)"""
    d = await request.json()
    pod_count_manual = _pod_count_manual(d)
    if pod_count_manual == -1:
        await _ensure_pod_count_fallback(d['env'], d['namespace'], d['deployment_name'])
    await pg_execute(
        "UPDATE k8s_res_control SET pod_count_manual=$1 WHERE env=$2 AND namespace=$3 AND deployment=$4",
        pod_count_manual, d['env'], d['namespace'], d['deployment_name'],
    )
    utils.invalidate_admis_cache()
    return web.json_response({"success": True, "msg": "更新成功"})


# ============================================================================
# 高峰期资源表 k8s_resources
# ============================================================================
RESOURCES_COLUMNS = [
    'date', 'env', 'namespace', 'deployment', 'pod_count', 'p95_pod_load',
    'p95_pod_cpu_pct', 'p95_pod_wss_mb', 'p95_pod_wss_pct', 'limit_pod_cpu_m',
    'limit_pod_mem_mb', 'request_pod_cpu_m', 'request_pod_mem_mb', 'p95_pod_qps',
    'p95_pod_g1gc_qps', 'pod_jvm_max_mb', 'p95_pod_heap_pct', 'p95_pod_g1e_pct',
]


async def res_collection(request):
    """某天某环境的高峰期采集数据(getCollection)"""
    d = request.query.get('date')
    env = request.query.get('env')
    cols = ", ".join(RESOURCES_COLUMNS)
    start, end = utils.day_range(d, d)
    rows = await pg_fetch(
        f"SELECT {cols} FROM k8s_resources WHERE date >= $1 AND date < $2 AND env = $3",
        start, end, env,
    )
    return _table_response(rows, RESOURCES_COLUMNS)


# ============================================================================
# agent 状态表 k8s_agent_status
# ============================================================================
async def agent_update_admission(request):
    d = await request.json()
    await pg_execute(
        "UPDATE k8s_agent_status SET admission=$1, admission_namespace=$2 WHERE env=$3",
        bool(d['admission']), d['admission_namespace'], d['env'],
    )
    utils.invalidate_admis_cache()
    return web.json_response({"success": True, "msg": "更新成功"})


async def agent_update_collect(request):
    d = await request.json()
    peak = d.get('peak_hours', '') or ''
    await pg_execute(
        "UPDATE k8s_agent_status SET collect=$1, peak_hours=$2 WHERE env=$3",
        bool(d['collect']), peak, d['env'],
    )
    return web.json_response({"success": True, "msg": "更新成功"})


async def agent_update_nms_not_confirm(request):
    d = await request.json()
    await pg_execute(
        "UPDATE k8s_agent_status SET nms_not_confirm=$1 WHERE env=$2",
        bool(int(d['nms_not_confirm'])), d['env'],
    )
    utils.invalidate_admis_cache()
    return web.json_response({"success": True, "msg": "更新成功"})


async def agent_update_scheduler(request):
    d = await request.json()
    await pg_execute(
        "UPDATE k8s_agent_status SET scheduler=$1 WHERE env=$2",
        bool(int(d['scheduler'])), d['env'],
    )
    utils.invalidate_admis_cache()
    return web.json_response({"success": True, "msg": "更新成功"})


async def agent_show_add_label(request):
    """是否已开启固定节点均衡模式(showAddLabel:SELECT 1 exists)"""
    env = request.query.get('env')
    namespace = request.query.get('namespace')
    val = await pg_fetchval(
        "SELECT 1 FROM k8s_agent_status WHERE env=$1 AND admission=true AND scheduler=true "
        "AND admission_namespace LIKE $2 LIMIT 1",
        env, f'%"{namespace}"%',
    )
    rows = [[1]] if val else []
    return web.json_response({"success": True, "data": rows, "meta": [{"name": "exists"}], "rows": len(rows)})


# ============================================================================
# Pod 告警表 k8s_pod_alert_days
# ============================================================================
ALERT_COLUMNS = [
    'fingerprint', 'alert_status', 'send_resolved', 'count_firing', 'count_resolved',
    'start_time', 'end_time', 'severity', 'alert_group', 'alert_name', 'env',
    'namespace', 'container', 'pod', 'description', 'operate', 'silenced', 'silence_id',
]


async def alert_envs(request):
    rows = await pg_fetch("SELECT DISTINCT env FROM k8s_pod_alert_days ORDER BY env")
    return _table_response(rows, ['env'])


async def alert_names(request):
    rows = await pg_fetch("SELECT DISTINCT alert_name FROM k8s_pod_alert_days ORDER BY alert_name")
    return _table_response(rows, ['alert_name'])


def _alert_filters(q, params):
    """基于查询/JSON 参数构建告警过滤 where 子句(参数化)。q 为 dict。"""
    where = []

    def add(cond, val):
        params.append(val)
        return cond.replace('?', f'${len(params)}')

    def add_in(col, vals):
        placeholders = []
        for v in vals:
            params.append(v)
            placeholders.append(f'${len(params)}')
        return f"{col} IN ({','.join(placeholders)})"

    if q.get('alertName'):
        where.append(add_in('alert_name', q['alertName']))
    if q.get('env'):
        where.append(add_in('env', q['env']))
    if q.get('operate'):
        where.append(add("operate = ?", q['operate']))
    if q.get('status'):
        where.append(add_in('alert_status', q['status']))
    if q.get('severity'):
        where.append(add_in('severity', q['severity']))
    if q.get('startTime'):
        # asyncpg 的 timestamptz 参数必须传 datetime;不带时区的按容器本地时区(Asia/Shanghai)解释
        where.append(add("start_time >= ?", datetime.fromisoformat(q['startTime'])))
    if q.get('namespace'):
        where.append(add("namespace = ?", q['namespace']))
    if q.get('pod'):
        where.append(add("pod = ?", q['pod']))
    # 屏蔽过滤:'1'/'0' 分别表示只看已屏蔽/未屏蔽,不传则不过滤
    if q.get('silenced') not in (None, ''):
        where.append(add("silenced = ?", str(q['silenced']) in ('1', 'true', 'True')))
    return where


async def alert_total(request):
    """按 alert_name 聚合统计(getAlarmTotal)"""
    q = await request.json()
    params = []
    where = []
    if q.get('env'):
        params.append(q['env'])
        where.append(f"env = ${len(params)}")
    if q.get('startTime'):
        params.append(datetime.fromisoformat(q['startTime']))
        where.append(f"start_time >= ${len(params)}")
    where_clause = ("WHERE " + " AND ".join(where)) if where else ""
    sql = f"""
        SELECT alert_name,
               COUNT(*) AS total,
               COUNT(*) FILTER (WHERE alert_status = 'firing') AS firing_count,
               COUNT(*) FILTER (WHERE alert_status = 'resolved') AS resolved_count,
               (array_agg(severity))[1] AS severity
        FROM k8s_pod_alert_days
        {where_clause}
        GROUP BY alert_name
        ORDER BY LENGTH((array_agg(severity))[1]) DESC, firing_count DESC
    """
    rows = await pg_fetch(sql, *params)
    return _table_response(rows, ['alert_name', 'total', 'firing_count', 'resolved_count', 'severity'])


async def alert_detail(request):
    """告警明细分页(getAlarmDetail)。返回全列 {data,meta}。"""
    q = await request.json()
    params = []
    where = _alert_filters(q, params)
    where_clause = ("WHERE " + " AND ".join(where)) if where else ""
    page = int(q.get('page', 1))
    page_size = int(q.get('pageSize', 20))
    offset = (page - 1) * page_size
    params.append(page_size)
    limit_p = f"${len(params)}"
    params.append(offset)
    offset_p = f"${len(params)}"
    cols = ", ".join(ALERT_COLUMNS)
    sql = f"SELECT {cols} FROM k8s_pod_alert_days {where_clause} ORDER BY start_time DESC LIMIT {limit_p} OFFSET {offset_p}"
    rows = await pg_fetch(sql, *params)
    return _table_response(rows, ALERT_COLUMNS)


async def alert_detail_total(request):
    """告警明细总数(getAlarmDetailTotal)"""
    q = await request.json()
    params = []
    where = _alert_filters(q, params)
    where_clause = ("WHERE " + " AND ".join(where)) if where else ""
    sql = f"SELECT COUNT(*) AS total FROM k8s_pod_alert_days {where_clause}"
    rows = await pg_fetch(sql, *params)
    return _table_response(rows, ['total'])


async def alert_update_operate(request):
    """修改 operate 状态(updateOperate)"""
    d = await request.json()
    # start_time 是列表里展示的本地时间字符串(_cell 输出),转回 datetime 才能和 timestamptz 比较
    await pg_execute(
        "UPDATE k8s_pod_alert_days SET operate=$1 WHERE start_time=$2 AND fingerprint=$3",
        d['operate'], datetime.fromisoformat(d['start_time']), d['fingerprint'],
    )
    return web.json_response({"success": True, "msg": "更新成功"})


def register_routes(app):
    """把所有 REST 接口注册到 aiohttp app。"""
    # 资源管控
    app.router.add_get("/api/db/res/envs", res_envs)
    app.router.add_get("/api/db/res/namespaces", res_namespaces)
    app.router.add_get("/api/db/res/deployments", res_deployments)
    app.router.add_get("/api/db/res/max_day", res_max_day)
    app.router.add_get("/api/db/res/list", res_list)
    app.router.add_post("/api/db/res/add", res_add)
    app.router.add_post("/api/db/res/edit", res_edit)
    app.router.add_post("/api/db/res/pod_count", res_update_pod_count)
    app.router.add_get("/api/db/res/collection", res_collection)
    # agent 状态
    app.router.add_post("/api/db/agent/admission", agent_update_admission)
    app.router.add_post("/api/db/agent/collect", agent_update_collect)
    app.router.add_post("/api/db/agent/nms_not_confirm", agent_update_nms_not_confirm)
    app.router.add_post("/api/db/agent/scheduler", agent_update_scheduler)
    app.router.add_get("/api/db/agent/show_add_label", agent_show_add_label)
    # 告警
    app.router.add_get("/api/db/alert/envs", alert_envs)
    app.router.add_get("/api/db/alert/names", alert_names)
    app.router.add_post("/api/db/alert/total", alert_total)
    app.router.add_post("/api/db/alert/detail", alert_detail)
    app.router.add_post("/api/db/alert/detail_total", alert_detail_total)
    app.router.add_post("/api/db/alert/operate", alert_update_operate)
