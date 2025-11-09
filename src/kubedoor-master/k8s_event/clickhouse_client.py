#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ClickHouse客户端模块
提供ClickHouse数据库连接和操作功能
"""

import os
import re
from typing import Dict, List, Optional, Any, Tuple
from datetime import datetime
from loguru import logger
from .connection_pool import get_connection_pool


class ClickHouseClient:
    """ClickHouse客户端类"""

    def __init__(self):
        """
        初始化ClickHouse客户端
        使用连接池替代全局连接
        """
        # 使用连接池替代全局连接
        self.pool = get_connection_pool()
        logger.info("ClickHouseClient初始化完成，使用连接池模式")

    def _load_sql_statements(self, sql_file_path: str) -> List[str]:
        """加载并拆分SQL文件中的语句"""
        with open(sql_file_path, 'r', encoding='utf-8') as f:
            sql_content = f.read()
        return [stmt.strip() for stmt in sql_content.split(';') if stmt.strip()]

    def execute_sql_file(self, sql_file_path: str) -> None:
        """执行SQL文件

        Args:
            sql_file_path: SQL文件路径
        """
        try:
            for sql in self._load_sql_statements(sql_file_path):
                logger.debug(f"执行SQL: {sql[:100]}...")
                self.pool.execute_command(sql)

            logger.info(f"成功执行SQL文件: {sql_file_path}")
        except Exception as e:
            logger.error(f"执行SQL文件失败 {sql_file_path}: {e}")
            raise

    @staticmethod
    def _extract_database_name(statement: str) -> str:
        match = re.search(r"create\s+database\s+(?:if\s+not\s+exists\s+)?([`\"]?[\w]+[`\"]?)", statement, re.IGNORECASE)
        if not match:
            raise ValueError("无法解析数据库名称")
        return match.group(1).replace('`', '').replace('"', '').strip()

    @staticmethod
    def _extract_table_identifier(statement: str) -> str:
        match = re.search(
            r"create\s+table\s+(?:if\s+not\s+exists\s+)?([`\"\w\.]+)",
            statement,
            re.IGNORECASE,
        )
        if not match:
            raise ValueError("无法解析表名称")
        return match.group(1).replace('`', '').replace('"', '').strip()

    def _split_table_identifier(self, identifier: str) -> Tuple[str, str]:
        if '.' in identifier:
            db_name, table_name = identifier.split('.', 1)
        else:
            db_name = getattr(self.pool, 'database', None) or 'default'
            table_name = identifier
        return db_name.strip(), table_name.strip()

    def database_exists(self, database: str) -> bool:
        result = self.pool.execute_query(
            "SELECT count() FROM system.databases WHERE name = %s",
            [database],
        )
        return bool(result and result[0][0] > 0)

    def table_exists(self, database: str, table: str) -> bool:
        result = self.pool.execute_query(
            "SELECT count() FROM system.tables WHERE database = %s AND name = %s",
            [database, table],
        )
        return bool(result and result[0][0] > 0)

    def init_table(self) -> None:
        """初始化K8S事件表"""
        sql_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'create_table.sql')
        statements = self._load_sql_statements(sql_file)

        database_statements: List[Tuple[str, str]] = []
        table_statements: List[Tuple[str, str]] = []
        other_statements: List[str] = []

        for stmt in statements:
            normalized = stmt.lstrip().lower()
            if normalized.startswith('create database'):
                database_statements.append((self._extract_database_name(stmt), stmt))
            elif normalized.startswith('create table'):
                table_statements.append((self._extract_table_identifier(stmt), stmt))
            else:
                other_statements.append(stmt)

        for db_name, stmt in database_statements:
            existed = self.database_exists(db_name)
            logger.debug(f"执行数据库初始化SQL: {stmt[:100]}...")
            self.pool.execute_command(stmt)
            if not self.database_exists(db_name):
                raise RuntimeError(f"数据库 {db_name} 初始化失败")
            if existed:
                logger.info(f"数据库 {db_name} 已存在")
            else:
                logger.info(f"数据库 {db_name} 创建成功")

        for identifier, stmt in table_statements:
            db_name, table_name = self._split_table_identifier(identifier)
            existed = self.table_exists(db_name, table_name)
            logger.debug(f"执行表初始化SQL: {stmt[:100]}...")
            self.pool.execute_command(stmt)
            if not self.table_exists(db_name, table_name):
                raise RuntimeError(f"表 {db_name}.{table_name} 初始化失败")
            if existed:
                logger.info(f"表 {db_name}.{table_name} 已存在")
            else:
                logger.info(f"表 {db_name}.{table_name} 创建成功")

        for stmt in other_statements:
            logger.debug(f"执行附加SQL: {stmt[:100]}...")
            self.pool.execute_command(stmt)

        logger.info("ClickHouse表结构初始化完成")

    def upsert_event(self, event_data: Dict[str, Any]) -> None:
        """
        插入或更新K8S事件数据

        ClickHouse的ReplacingMergeTree引擎会自动处理重复数据
        相同eventUid的记录会被最新的lastTimestamp版本替换

        Args:
            event_data: 事件数据字典
        """
        try:
            # 准备插入数据 - 使用clickhouse-connect的insert方法
            with self.pool.get_client() as client:
                client.insert(
                    'k8s_events',
                    [
                        [
                            event_data.get('eventUid', ''),
                            event_data.get('eventStatus', ''),
                            event_data.get('level', ''),
                            event_data.get('count', 0),
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
                    ],
                    column_names=[
                        'eventUid',
                        'eventStatus',
                        'level',
                        'count',
                        'kind',
                        'k8s',
                        'namespace',
                        'name',
                        'reason',
                        'message',
                        'firstTimestamp',
                        'lastTimestamp',
                        'reportingComponent',
                        'reportingInstance',
                    ],
                )

            logger.debug(f"已更新事件: {event_data.get('eventUid')} 在命名空间 {event_data.get('namespace')}")

        except Exception as e:
            logger.error(f"更新事件数据失败: {e}")
            logger.error(f"事件数据: {event_data}")
            raise

    def query_events_advanced(
        self,
        k8s: str,
        start_time: datetime,
        end_time: datetime,
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
    ) -> List[Dict[str, Any]]:
        """高级查询K8S事件数据

        Args:
            k8s: K8S集群名称（必填）
            start_time: 开始时间（必填）
            end_time: 结束时间（必填）
            limit: 返回记录数限制（必填）
            namespace: 命名空间（可选）
            count: 事件发生次数，大于等于匹配（可选）
            level: 事件级别（可选）
            kind: 对象类型（可选）
            name: 对象名称（可选）
            reason: 事件原因（可选）
            reporting_component: 报告组件（可选）
            reporting_instance: 报告实例（可选）
            message: 事件消息，包含匹配（可选）

        Returns:
            事件数据列表，不包含eventUid和createdAt字段
        """
        try:
            where_conditions = []
            params = []

            # 必填条件
            where_conditions.append("k8s = %s")
            params.append(k8s)

            where_conditions.append("lastTimestamp >= %s")
            params.append(f'{start_time} 00:00:00')

            where_conditions.append("lastTimestamp <= %s")
            params.append(f'{end_time} 23:59:59')

            # 可选条件
            if namespace and namespace != "[全部]":
                if namespace == "[空值]":
                    where_conditions.append("(namespace IS NULL OR namespace = '')")
                else:
                    where_conditions.append("namespace = %s")
                    params.append(namespace)

            if count is not None:
                where_conditions.append("count >= %s")
                params.append(count)

            if level:
                where_conditions.append("level = %s")
                params.append(level)

            if kind and kind != "[全部]":
                if kind == "[空值]":
                    where_conditions.append("(kind IS NULL OR kind = '')")
                else:
                    where_conditions.append("kind = %s")
                    params.append(kind)

            if name and name != "[全部]":
                if name == "[空值]":
                    where_conditions.append("(name IS NULL OR name = '')")
                else:
                    where_conditions.append("name = %s")
                    params.append(name)

            if reason and reason != "[全部]":
                if reason == "[空值]":
                    where_conditions.append("(reason IS NULL OR reason = '')")
                else:
                    where_conditions.append("positionCaseInsensitive(reason, %s) > 0")
                    params.append(reason)

            if reporting_component and reporting_component != "[全部]":
                if reporting_component == "[空值]":
                    where_conditions.append("(reportingComponent IS NULL OR reportingComponent = '')")
                else:
                    where_conditions.append("reportingComponent = %s")
                    params.append(reporting_component)

            if reporting_instance and reporting_instance != "[全部]":
                if reporting_instance == "[空值]":
                    where_conditions.append("(reportingInstance IS NULL OR reportingInstance = '')")
                else:
                    where_conditions.append("reportingInstance = %s")
                    params.append(reporting_instance)

            if message:
                where_conditions.append("positionCaseInsensitive(message, %s) > 0")
                params.append(message)

            where_clause = " AND ".join(where_conditions)

            # 查询除了eventUid和createdAt之外的所有字段
            sql = f"""
            SELECT 
                eventStatus,
                level,
                count,
                kind,
                k8s,
                namespace,
                name,
                reason,
                message,
                firstTimestamp,
                lastTimestamp,
                reportingComponent,
                reportingInstance
            FROM k8s_events FINAL
            WHERE {where_clause}
            ORDER BY lastTimestamp DESC
            LIMIT {limit}
            """
            logger.debug(f"执行SQL: {sql}")
            logger.debug(f"参数: {params}")
            # 对于SELECT查询，使用execute_query
            result = self.pool.execute_query(sql, params)
            return result

        except Exception as e:
            logger.error(f"高级查询事件失败: {e}")
            raise

    def close(self) -> None:
        """关闭连接"""
        # 连接池会自动管理连接，无需手动关闭
        logger.info("ClickHouse客户端关闭（连接池自动管理）")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


# 全局客户端实例
_clickhouse_client: Optional[ClickHouseClient] = None


def get_clickhouse_client() -> ClickHouseClient:
    """获取全局ClickHouse客户端实例"""
    global _clickhouse_client
    if _clickhouse_client is None:
        _clickhouse_client = ClickHouseClient()
    return _clickhouse_client


def init_clickhouse_tables():
    """初始化ClickHouse表结构"""
    client = get_clickhouse_client()
    client.init_table()
