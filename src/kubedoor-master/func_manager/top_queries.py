#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Top10 / 趋势统计查询接口(数据源:PostgreSQL)
"""

import asyncio
from datetime import date, datetime
from decimal import Decimal
from typing import Any, List, Sequence

from aiohttp import web
from loguru import logger

from k8s_event import get_pg_event_client


def _serialize_value(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def _rows_to_dicts(rows: Sequence[Sequence[Any]], columns: Sequence[str]) -> List[dict]:
    data: List[dict] = []
    for row in rows:
        item = {column: _serialize_value(row[idx]) for idx, column in enumerate(columns)}
        data.append(item)
    return data


async def _run_query(sql: str, params: List[Any]) -> List[Sequence[Any]]:
    client = get_pg_event_client()
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: client.pool.execute_query(sql, params or None))


def _build_exception_events_sql(env: str | None) -> tuple[str, List[Any], List[str]]:
    # k8s_events 以 (eventuid, k8s) 唯一,每个事件只有一行;
    # 下面的 row_number() 按 eventuid 取最新一条只是兜底
    params: List[Any] = []
    env_filter = ""
    if env:
        params.append(env)
        env_filter = f"                AND k8s = ${len(params)}\n"
    sql = f"""
    SELECT
        k8s AS env,
        namespace,
        reason,
        kind,
        name,
        message,
        count,
        round(count::numeric / sum(count) OVER () * 100, 2) AS ratio_percent
    FROM
    (
        SELECT
            k8s,
            namespace,
            reason,
            kind,
            name,
            message,
            count
        FROM
        (
            SELECT
                k8s,
                namespace,
                reason,
                kind,
                name,
                message,
                count,
                eventuid,
                row_number() OVER (PARTITION BY eventuid ORDER BY lasttimestamp DESC) AS rn
            FROM k8s_events
            WHERE
                lasttimestamp::date = CURRENT_DATE
                AND eventstatus != 'DELETED' AND level <> 'Normal'
{env_filter}        ) t
        WHERE rn = 1
        ORDER BY
            count DESC
        LIMIT 10
    ) t2
    ORDER BY
        count DESC
    """
    columns = [
        "env",
        "namespace",
        "reason",
        "kind",
        "name",
        "message",
        "count",
        "ratio_percent",
    ]
    return sql, params, columns


def _build_pod_alerts_sql(env: str | None) -> tuple[str, List[Any], List[str]]:
    params: List[Any] = []
    env_filter = ""
    if env:
        params.append(env)
        env_filter = f"            AND env = ${len(params)}\n"
    sql = f"""
    SELECT
        env,
        namespace,
        alert_name,
        pod,
        description,
        count_firing,
        round(count_firing::numeric / sum(count_firing) OVER () * 100, 2) AS ratio_percent
    FROM
    (
        SELECT
            env,
            namespace,
            alert_name,
            pod,
            description,
            count_firing
        FROM k8s_pod_alert_days
        WHERE
            (
                end_time::date = CURRENT_DATE
                OR start_time::date = CURRENT_DATE
            )
{env_filter}        ORDER BY
            count_firing DESC
        LIMIT 10
    ) t
    ORDER BY
        count_firing DESC
    """
    columns = ["env", "namespace", "alert_name", "pod", "description", "count_firing", "ratio_percent"]
    return sql, params, columns


def _build_alert_daily_sql(env: str | None) -> tuple[str, List[Any], List[str]]:
    params: List[Any] = []
    env_filter = ""
    if env:
        params.append(env)
        env_filter = f"        AND env = ${len(params)}\n"
    # 按 start_time::date 分组,星期几也要从这个分组表达式上取,否则 PG 会报 GroupingError
    sql = f"""
    SELECT
        start_time::date AS day,
        CASE
            WHEN start_time::date = CURRENT_DATE THEN '今天'
            WHEN extract(isodow FROM start_time::date) = 1 THEN '周一'
            WHEN extract(isodow FROM start_time::date) = 2 THEN '周二'
            WHEN extract(isodow FROM start_time::date) = 3 THEN '周三'
            WHEN extract(isodow FROM start_time::date) = 4 THEN '周四'
            WHEN extract(isodow FROM start_time::date) = 5 THEN '周五'
            WHEN extract(isodow FROM start_time::date) = 6 THEN '周六'
            WHEN extract(isodow FROM start_time::date) = 7 THEN '周日'
        END AS day_label,
        sum(count_firing) AS daily_alert_count
    FROM k8s_pod_alert_days
    WHERE
        start_time::date >= CURRENT_DATE - 9
        AND start_time::date <= CURRENT_DATE
{env_filter}    GROUP BY
        start_time::date
    ORDER BY
        day ASC
    """
    columns = ["day", "day_label", "daily_alert_count"]
    return sql, params, columns


async def top10_events_handler(request: web.Request) -> web.Response:
    env = request.query.get("env")
    sql, params, columns = _build_exception_events_sql(env)
    try:
        rows = await _run_query(sql, params)
        data = _rows_to_dicts(rows, columns)
        return web.json_response({"success": True, "data": data})
    except Exception as exc:
        logger.error(f"查询异常事件TOP10失败: {exc}")
        return web.json_response({"message": str(exc)}, status=500)


async def top10_pod_alerts_handler(request: web.Request) -> web.Response:
    env = request.query.get("env")
    sql, params, columns = _build_pod_alerts_sql(env)
    try:
        rows = await _run_query(sql, params)
        data = _rows_to_dicts(rows, columns)
        return web.json_response({"success": True, "data": data})
    except Exception as exc:
        logger.error(f"查询Pod告警TOP10失败: {exc}")
        return web.json_response({"message": str(exc)}, status=500)


async def alert_daily_stats_handler(request: web.Request) -> web.Response:
    env = request.query.get("env")
    sql, params, columns = _build_alert_daily_sql(env)
    try:
        rows = await _run_query(sql, params)
        data = _rows_to_dicts(rows, columns)
        return web.json_response({"success": True, "data": data})
    except Exception as exc:
        logger.error(f"查询每日告警统计失败: {exc}")
        return web.json_response({"message": str(exc)}, status=500)
