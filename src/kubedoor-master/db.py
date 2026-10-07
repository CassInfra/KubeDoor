#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PostgreSQL 统一数据访问层(kubedoor-master)

master 侧所有数据访问都经由本模块,包括 utils.py 的管控/资源查询、
k8s_event 的事件读写,以及 istio_route 的路由表操作。

架构约定:
  master 主体是 aiohttp 异步,所有 handler 跑在主 event loop。
  asyncpg 连接池绑定主 loop,因此本模块提供两类入口:
    1) 异步 API(pg_fetch / pg_fetchrow / pg_fetchval / pg_execute / pg_executemany)
       —— 供 aiohttp handler 及其它 async 函数直接 await。
    2) 同步桥(*_sync 系列)—— 供仍以同步形式存在、经 run_in_executor 在
       线程池执行的工具函数调用;内部用 run_coroutine_threadsafe 提交到主 loop。
       ⚠️ 同步桥禁止在主 loop 线程内调用(会死锁,_run_sync 检测到会直接抛错),仅限 executor / 其它线程。

参数占位符统一用 asyncpg 的 $1,$2...(注意与 psycopg 的 %s 不同)。
"""

import os
import asyncio
import threading
from typing import Any, List, Optional, Sequence

import asyncpg
from loguru import logger

# ---------------------------------------------------------------------------
# 连接配置(环境变量),统一 PG_ 前缀,替代原 CK_* / DB_*
# ---------------------------------------------------------------------------
PG_HOST = os.environ.get('PG_HOST', 'localhost')
PG_PORT = int(os.environ.get('PG_PORT', '5432'))
PG_USER = os.environ.get('PG_USER', 'postgres')
PG_PASSWORD = os.environ.get('PG_PASSWORD', '')
PG_DATABASE = os.environ.get('PG_DATABASE', 'kubedoor')
PG_POOL_MIN = int(os.environ.get('PG_POOL_MIN', '2'))
PG_POOL_MAX = int(os.environ.get('PG_POOL_MAX', '20'))
# 单条语句默认超时(秒);热路径 admis 另行更短超时
PG_CMD_TIMEOUT = float(os.environ.get('PG_CMD_TIMEOUT', '60'))

# 全局连接池与其绑定的主 event loop 引用
_pool: Optional[asyncpg.Pool] = None
_loop: Optional[asyncio.AbstractEventLoop] = None
_init_lock = threading.Lock()


async def init_pool() -> asyncpg.Pool:
    """在主 event loop 中初始化连接池。应在 aiohttp on_startup 中调用一次。"""
    global _pool, _loop
    if _pool is not None:
        return _pool
    _loop = asyncio.get_running_loop()
    _pool = await asyncpg.create_pool(
        host=PG_HOST,
        port=PG_PORT,
        user=PG_USER,
        password=PG_PASSWORD,
        database=PG_DATABASE,
        min_size=PG_POOL_MIN,
        max_size=PG_POOL_MAX,
        command_timeout=PG_CMD_TIMEOUT,
    )
    logger.info(
        f"PostgreSQL 连接池初始化完成 - {PG_USER}@{PG_HOST}:{PG_PORT}/{PG_DATABASE} "
        f"(min={PG_POOL_MIN}, max={PG_POOL_MAX})"
    )
    return _pool


async def close_pool() -> None:
    """关闭连接池。应在 aiohttp on_cleanup 中调用。"""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        logger.info("PostgreSQL 连接池已关闭")


def get_pool() -> asyncpg.Pool:
    """获取已初始化的连接池;未初始化则抛错(避免隐式在错误 loop 建池)。"""
    if _pool is None:
        raise RuntimeError("PostgreSQL 连接池尚未初始化,请先在 on_startup 调用 init_pool()")
    return _pool


# ---------------------------------------------------------------------------
# 异步 API —— 供 aiohttp handler 及 async 函数直接 await
# ---------------------------------------------------------------------------
async def pg_fetch(sql: str, *args, timeout: Optional[float] = None) -> List[asyncpg.Record]:
    """执行 SELECT,返回 Record 列表(可按索引或列名访问)。"""
    async with get_pool().acquire() as conn:
        return await conn.fetch(sql, *args, timeout=timeout)


async def pg_fetchrow(sql: str, *args, timeout: Optional[float] = None) -> Optional[asyncpg.Record]:
    """执行 SELECT,返回首行或 None。"""
    async with get_pool().acquire() as conn:
        return await conn.fetchrow(sql, *args, timeout=timeout)


async def pg_fetchval(sql: str, *args, column: int = 0, timeout: Optional[float] = None) -> Any:
    """执行 SELECT,返回首行指定列的标量值。"""
    async with get_pool().acquire() as conn:
        return await conn.fetchval(sql, *args, column=column, timeout=timeout)


async def pg_execute(sql: str, *args, timeout: Optional[float] = None) -> str:
    """执行 INSERT/UPDATE/DELETE/DDL,返回状态串(如 'UPDATE 3')。"""
    async with get_pool().acquire() as conn:
        return await conn.execute(sql, *args, timeout=timeout)


async def pg_executemany(sql: str, args_iter: Sequence[Sequence[Any]], timeout: Optional[float] = None) -> None:
    """批量执行(高效插入)。args_iter 为参数元组序列。"""
    async with get_pool().acquire() as conn:
        await conn.executemany(sql, args_iter, timeout=timeout)


async def pg_copy_records(table: str, records: Sequence[Sequence[Any]], columns: Sequence[str]) -> str:
    """COPY 批量写入(最快路径,用于指标/迁移大批量插入)。"""
    async with get_pool().acquire() as conn:
        return await conn.copy_records_to_table(table, records=records, columns=list(columns))


async def run_ddl_file(sql_file_path: str) -> None:
    """执行 DDL 脚本文件(建表)。用连接直接 execute 整个脚本。"""
    with open(sql_file_path, 'r', encoding='utf-8') as f:
        ddl = f.read()
    async with get_pool().acquire() as conn:
        await conn.execute(ddl)
    logger.info(f"已执行 DDL 脚本: {sql_file_path}")


# ---------------------------------------------------------------------------
# 同步桥 —— 供 executor 线程里的同步函数调用(禁止在主 loop 线程调用)
# ---------------------------------------------------------------------------
def _run_sync(coro):
    """把协程提交到主 loop 执行并阻塞取结果。仅限非主-loop 线程。"""
    if _loop is None:
        raise RuntimeError("事件循环未就绪,init_pool() 尚未在主 loop 执行")
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is _loop:
        # 在主 loop 线程里阻塞等主 loop 执行协程,会永久死锁,整个 master 假死。
        # 直接报错,让问题只落在这一个请求上。
        coro.close()
        raise RuntimeError("同步桥不能在主 event loop 线程里调用(会死锁),请改用异步 API 或放进 asyncio.to_thread")
    fut = asyncio.run_coroutine_threadsafe(coro, _loop)
    return fut.result()


def pg_fetch_sync(sql: str, *args, timeout: Optional[float] = None) -> List[asyncpg.Record]:
    return _run_sync(pg_fetch(sql, *args, timeout=timeout))


def pg_fetchrow_sync(sql: str, *args, timeout: Optional[float] = None) -> Optional[asyncpg.Record]:
    return _run_sync(pg_fetchrow(sql, *args, timeout=timeout))


def pg_fetchval_sync(sql: str, *args, column: int = 0, timeout: Optional[float] = None) -> Any:
    return _run_sync(pg_fetchval(sql, *args, column=column, timeout=timeout))


def pg_execute_sync(sql: str, *args, timeout: Optional[float] = None) -> str:
    return _run_sync(pg_execute(sql, *args, timeout=timeout))


def pg_executemany_sync(sql: str, args_iter: Sequence[Sequence[Any]], timeout: Optional[float] = None) -> None:
    return _run_sync(pg_executemany(sql, args_iter, timeout=timeout))
