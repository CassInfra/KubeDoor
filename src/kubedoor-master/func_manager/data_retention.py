#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
过期数据清理

只追加的大表按时间列保留固定天数,顺带清掉过期的登录会话。master 启动后先等一会儿,之后每 24 小时
清理一轮。按批删除,每批之间稍作停顿,避免一次删太多行长时间占锁、超过语句超时。

保留天数在「系统管理 → 通用配置 → 数据保留」设置(每一轮重新取,改了下一轮生效),0 表示不清理该表:
  retention_events_days     k8s_events          默认 90
  retention_alerts_days     k8s_pod_alert_days  默认 365
  retention_resources_days  k8s_resources       默认 365
  retention_audit_days      audit_logs          默认 180
"""

import asyncio

from loguru import logger

import db
import general_settings

# (表名, 时间列, 通用配置里保留天数的 key)。表名和列名会直接拼进 SQL,只能写死在这里,不能来自外部输入
RETENTION_RULES = [
    ('k8s_events', 'lasttimestamp', 'retention_events_days'),
    ('k8s_pod_alert_days', 'start_time', 'retention_alerts_days'),
    ('k8s_resources', 'date', 'retention_resources_days'),
    ('audit_logs', 'ts', 'retention_audit_days'),
]

BATCH_SIZE = 5000
BATCH_PAUSE = 0.2          # 批与批之间的停顿(秒)
RUN_INTERVAL = 24 * 3600   # 两轮清理的间隔(秒)
INITIAL_DELAY = 300        # 启动后先等 5 分钟,不和建表、agent 重连抢资源


async def purge_table(table: str, ts_column: str, days: int, batch_size: int = BATCH_SIZE) -> int:
    """分批删除 table 中 ts_column 早于 days 天前的行,返回删除总行数。"""
    sql = (
        f"DELETE FROM {table} WHERE ctid IN ("
        f"SELECT ctid FROM {table} "
        f"WHERE {ts_column} < now() - make_interval(days => $1) "
        f"LIMIT {int(batch_size)})"
    )
    total = 0
    while True:
        status = await db.pg_execute(sql, days)  # 形如 'DELETE 5000'
        deleted = int(status.split()[-1])
        total += deleted
        if deleted < batch_size:
            return total
        await asyncio.sleep(BATCH_PAUSE)


async def purge_expired() -> None:
    """按 RETENTION_RULES 清理一轮。保留天数每一轮从通用配置重新取;单张表失败只记日志,不影响其它表。"""
    for table, ts_column, setting in RETENTION_RULES:
        days = general_settings.value(setting)
        if days <= 0:
            continue
        try:
            deleted = await purge_table(table, ts_column, days)
            if deleted:
                logger.info(f"🧹过期数据清理:{table} 删除 {deleted} 行({ts_column} 早于 {days} 天前)")
        except Exception as e:
            logger.error(f"过期数据清理失败 {table}: {e}")
    try:
        from auth.store import store

        await store.cleanup_sessions()
    except Exception as e:
        logger.error(f"清理过期登录会话失败: {e}")


async def retention_loop() -> None:
    """后台任务入口,在 master 的 on_startup 里 create_task。"""
    await asyncio.sleep(INITIAL_DELAY)
    while True:
        await purge_expired()
        await asyncio.sleep(RUN_INTERVAL)
