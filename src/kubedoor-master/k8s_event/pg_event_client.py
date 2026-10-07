#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PostgreSQL 客户端模块(K8S 事件)

去重语义:事件按 (eventuid, k8s) 唯一,写入走
  INSERT ... ON CONFLICT (eventuid, k8s) DO UPDATE
同一事件重复发生时原地更新 count / lasttimestamp,一个事件始终只有一行。

⚠️ 本模块方法为同步接口(经 asyncio.to_thread 在线程池执行),
   内部走 db.py 同步桥;占位符统一 asyncpg 风格 $1,$2...
"""

from typing import Dict, List, Optional, Any
from datetime import datetime
from loguru import logger

import db
from utils import day_range
from .connection_pool import get_connection_pool


class PgEventClient:
    """PostgreSQL K8S 事件客户端。"""

    def __init__(self):
        self.pool = get_connection_pool()
        logger.info("PgEventClient 初始化完成，使用 asyncpg 连接池")

    def upsert_event(self, event_data: Dict[str, Any]) -> None:
        """插入或更新 K8S 事件。同一事件(eventuid + k8s)重复发生时原地更新,始终只有一行。"""
        try:
            # - level:告警流程会把命中规则的事件标成"已告警",事件后续再更新也保留这个标记
            # - firsttimestamp 取较早值:agent 送来的时间为空时会被填成当前时间,不能让它往后漂
            # - WHERE:只接受不比库里旧的数据,agent 重连后重放的旧状态不会覆盖新数据
            sql = """
                INSERT INTO k8s_events (
                    eventuid, eventstatus, level, count, kind, k8s, namespace,
                    name, reason, message, firsttimestamp, lasttimestamp,
                    reportingcomponent, reportinginstance
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
                ON CONFLICT (eventuid, k8s) DO UPDATE SET
                    eventstatus = EXCLUDED.eventstatus,
                    level = CASE WHEN k8s_events.level = '已告警'
                                 THEN k8s_events.level ELSE EXCLUDED.level END,
                    count = EXCLUDED.count,
                    kind = EXCLUDED.kind,
                    namespace = EXCLUDED.namespace,
                    name = EXCLUDED.name,
                    reason = EXCLUDED.reason,
                    message = EXCLUDED.message,
                    firsttimestamp = LEAST(k8s_events.firsttimestamp, EXCLUDED.firsttimestamp),
                    lasttimestamp = EXCLUDED.lasttimestamp,
                    reportingcomponent = EXCLUDED.reportingcomponent,
                    reportinginstance = EXCLUDED.reportinginstance
                WHERE EXCLUDED.lasttimestamp >= k8s_events.lasttimestamp
            """
            params = [
                event_data.get('eventUid', ''),
                event_data.get('eventStatus', ''),
                event_data.get('level', ''),
                int(event_data.get('count', 0)),
                event_data.get('kind', ''),
                event_data.get('k8s', ''),
                event_data.get('namespace', ''),
                event_data.get('name', ''),
                event_data.get('reason', ''),
                event_data.get('message', ''),
                event_data.get('firstTimestamp'),
                event_data.get('lastTimestamp'),
                event_data.get('reportingComponent', ''),
                event_data.get('reportingInstance', ''),
            ]
            self.pool.execute_command(sql, params)
            logger.debug(f"已更新事件: {event_data.get('eventUid')} 在命名空间 {event_data.get('namespace')}")
        except Exception as e:
            logger.error(f"更新事件数据失败: {e}")
            logger.error(f"事件数据: {event_data}")
            raise

    def query_events_advanced(
        self,
        k8s: str,
        start_time,
        end_time,
        limit: int,
        namespace: str = None,
        count: int = None,
        level: str = None,
        kind: str = None,
        name: str = None,
        reason: str = None,
        reporting_component: str = None,
        reporting_instance: str = None,
        message: str = None,
    ) -> List[Any]:
        """高级查询 K8S 事件(参数化,ILIKE 大小写不敏感包含匹配)。"""
        try:
            where = []
            params: List[Any] = []

            def add(cond, val):
                params.append(val)
                where.append(cond.replace('?', f'${len(params)}'))

            add("k8s = ?", k8s)
            start_ts, end_ts = day_range(start_time, end_time)
            add("lasttimestamp >= ?", start_ts)
            add("lasttimestamp < ?", end_ts)

            if namespace and namespace != "[全部]":
                if namespace == "[空值]":
                    where.append("(namespace IS NULL OR namespace = '')")
                else:
                    add("namespace = ?", namespace)
            if count is not None:
                add("count >= ?", count)
            if level:
                add("level = ?", level)
            if kind and kind != "[全部]":
                if kind == "[空值]":
                    where.append("(kind IS NULL OR kind = '')")
                else:
                    add("kind = ?", kind)
            if name and name != "[全部]":
                if name == "[空值]":
                    where.append("(name IS NULL OR name = '')")
                else:
                    add("name = ?", name)
            if reason and reason != "[全部]":
                if reason == "[空值]":
                    where.append("(reason IS NULL OR reason = '')")
                else:
                    add("reason ILIKE ?", f'%{reason}%')
            if reporting_component and reporting_component != "[全部]":
                if reporting_component == "[空值]":
                    where.append("(reportingcomponent IS NULL OR reportingcomponent = '')")
                else:
                    add("reportingcomponent = ?", reporting_component)
            if reporting_instance and reporting_instance != "[全部]":
                if reporting_instance == "[空值]":
                    where.append("(reportinginstance IS NULL OR reportinginstance = '')")
                else:
                    add("reportinginstance = ?", reporting_instance)
            if message:
                add("message ILIKE ?", f'%{message}%')

            where_clause = " AND ".join(where)
            params.append(int(limit))
            sql = f"""
            SELECT
                eventstatus, level, count, kind, k8s, namespace, name,
                reason, message, firsttimestamp, lasttimestamp,
                reportingcomponent, reportinginstance
            FROM k8s_events
            WHERE {where_clause}
            ORDER BY lasttimestamp DESC
            LIMIT ${len(params)}
            """
            logger.debug(f"执行SQL: {sql}")
            logger.debug(f"参数: {params}")
            return self.pool.execute_query(sql, params)
        except Exception as e:
            logger.error(f"高级查询事件失败: {e}")
            raise

    def close(self) -> None:
        logger.info("PgEventClient 关闭（连接池由 db.py 统一管理）")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


# 全局客户端实例
_pg_event_client: Optional[PgEventClient] = None


def get_pg_event_client() -> PgEventClient:
    """获取全局 PG 事件客户端实例。"""
    global _pg_event_client
    if _pg_event_client is None:
        _pg_event_client = PgEventClient()
    return _pg_event_client


async def init_pg_tables_async():
    """异步初始化 PostgreSQL 表结构(执行 db.sql)。在 master on_startup 中 await。"""
    import os

    sql_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'db.sql')
    await db.run_ddl_file(sql_file)
    logger.info("PostgreSQL 表结构初始化完成")


def init_pg_tables():
    """同步桥版本(仅供非主-loop 线程调用)。"""
    db._run_sync(init_pg_tables_async())
