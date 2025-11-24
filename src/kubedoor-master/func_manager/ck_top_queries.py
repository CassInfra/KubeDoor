#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ClickHouse Top10 查询接口实现
"""

import asyncio
from datetime import date, datetime
from decimal import Decimal
from typing import Any, List, Sequence

from aiohttp import web
from loguru import logger

from k8s_event import get_clickhouse_client


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
    client = get_clickhouse_client()
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: client.pool.execute_query(sql, params or None))


def _build_exception_events_sql(env: str | None) -> tuple[str, List[Any], List[str]]:
    sql = """
    SELECT
        k8s AS env,
        namespace,
        reason,
        kind,
        name,
        message,
        count,
        round(count / sum(count) OVER () * 100, 2) AS ratio_percent
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
                eventUid,
                row_number() OVER (PARTITION BY eventUid ORDER BY lastTimestamp DESC) AS rn
            FROM kubedoor.k8s_events
            PREWHERE
                toDate(lastTimestamp) = today()
                AND eventStatus != 'DELETED' AND level <> 'Normal'
    """
    params: List[Any] = []
    if env:
        sql += "                AND k8s = %s\n"
        params.append(env)

    sql += """
        )
        WHERE rn = 1
        ORDER BY
            count DESC
        LIMIT 10
    )
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
    sql = """
    SELECT
        env,
        namespace,
        alert_name,
        pod,
        description,
        count_firing,
        round(count_firing / sum(count_firing) OVER () * 100, 2) AS ratio_percent
    FROM
    (
        SELECT
            env,
            namespace,
            alert_name,
            pod,
            description,
            count_firing
        FROM kubedoor.k8s_pod_alert_days
        PREWHERE
            (
                toDate(end_time) = today()
                OR toDate(start_time) = today()
            )
    """
    params: List[Any] = []
    if env:
        sql += "            AND env = %s\n"
        params.append(env)

    sql += """
        ORDER BY
            count_firing DESC
        LIMIT 10
    )
    ORDER BY
        count_firing DESC
    """
    columns = ["env", "namespace", "alert_name", "pod", "description", "count_firing", "ratio_percent"]
    return sql, params, columns


def _build_alert_daily_sql(env: str | None) -> tuple[str, List[Any], List[str]]:
    sql = """
    SELECT
        toDate(start_time) AS day,
        if(
            day = today(),
            '今天',
            case
                when toDayOfWeek(day) = 1 then '周一'
                when toDayOfWeek(day) = 2 then '周二'
                when toDayOfWeek(day) = 3 then '周三'
                when toDayOfWeek(day) = 4 then '周四'
                when toDayOfWeek(day) = 5 then '周五'
                when toDayOfWeek(day) = 6 then '周六'
                when toDayOfWeek(day) = 7 then '周日'
            end
        ) AS day_label,
        sum(count_firing) AS daily_alert_count
    FROM kubedoor.k8s_pod_alert_days
    PREWHERE
        toDate(start_time) >= today() - 9
        AND toDate(start_time) <= today()
    """
    params: List[Any] = []
    if env:
        sql += "        AND env = %s\n"
        params.append(env)

    sql += """
    GROUP BY
        day
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
