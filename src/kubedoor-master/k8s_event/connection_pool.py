#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PostgreSQL 连接池兼容层(k8s_event 模块内部使用)

统一复用 master 的 db.py(asyncpg)连接池,本文件是一层薄封装,对外暴露
execute_query / execute_command 两个同步接口,供 pg_event_client.py 调用。

⚠️ 这些方法通过 db.py 的同步桥(_sync)提交到主 event loop 执行,
   仅可在 executor / 其它线程调用(k8s_event 全程经 asyncio.to_thread 执行,满足)。
   占位符使用 asyncpg 风格 $1,$2...(非 %s)。
"""

import threading
from typing import Any, List, Optional
from loguru import logger

import db


class PgConnectionPool:
    """PostgreSQL 连接池兼容包装(单例)。"""

    _instance: Optional['PgConnectionPool'] = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, '_initialized'):
            return
        self.database = db.PG_DATABASE
        self._initialized = True
        logger.info(f"PG 连接池兼容层就绪(复用 db.py asyncpg 池,DB: {self.database})")

    def execute_query(self, query: str, parameters=None) -> List[Any]:
        """执行 SELECT,返回行列表(每行可按索引访问)。占位符 $1,$2...。"""
        args = list(parameters) if parameters else []
        rows = db.pg_fetch_sync(query, *args)
        return [tuple(r) for r in rows]

    def execute_command(self, command: str, parameters=None) -> Any:
        """执行 INSERT/UPDATE/DELETE/DDL。占位符 $1,$2...。"""
        args = list(parameters) if parameters else []
        return db.pg_execute_sync(command, *args)


# 全局连接池实例
_connection_pool: Optional[PgConnectionPool] = None
_pool_lock = threading.Lock()


def get_connection_pool() -> PgConnectionPool:
    """获取全局连接池实例(兼容旧接口名)。"""
    global _connection_pool
    if _connection_pool is None:
        with _pool_lock:
            if _connection_pool is None:
                _connection_pool = PgConnectionPool()
    return _connection_pool
