#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
过期数据清理

三张只追加的大表按时间列保留固定天数。master 启动后先等一会儿,之后每 24 小时
清理一轮。按批删除,每批之间稍作停顿,避免一次删太多行长时间占锁、超过语句超时。

保留天数可用环境变量调整,设为 0 表示不清理该表:
  RETENTION_EVENTS_DAYS     k8s_events          默认 90
  RETENTION_ALERTS_DAYS     k8s_pod_alert_days  默认 365
  RETENTION_RESOURCES_DAYS  k8s_resources       默认 365
"""

import os
import asyncio

from loguru import logger

import db

# (表名, 时间列, 保留天数)。表名和列名会直接拼进 SQL,只能写死在这里,不能来自外部输入
RETENTION_RULES = [
    ('k8s_events', 'lasttimestamp', int(os.environ.get('RETENTION_EVENTS_DAYS', '90'))),
    ('k8s_pod_alert_days', 'start_time', int(os.environ.get('RETENTION_ALERTS_DAYS', '365'))),
    ('k8s_resources', 'date', int(os.environ.get('RETENTION_RESOURCES_DAYS', '365'))),
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
    """按 RETENTION_RULES 清理一轮。单张表失败只记日志,不影响其它表。"""
    for table, ts_column, days in RETENTION_RULES:
        if days <= 0:
            continue
        try:
            deleted = await purge_table(table, ts_column, days)
            if deleted:
                logger.info(f"🧹过期数据清理:{table} 删除 {deleted} 行({ts_column} 早于 {days} 天前)")
        except Exception as e:
            logger.error(f"过期数据清理失败 {table}: {e}")


async def retention_loop() -> None:
    """后台任务入口,在 master 的 on_startup 里 create_task。"""
    await asyncio.sleep(INITIAL_DELAY)
    while True:
        await purge_expired()
        await asyncio.sleep(RUN_INTERVAL)
