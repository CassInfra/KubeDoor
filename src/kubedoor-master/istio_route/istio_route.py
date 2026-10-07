#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Istio VirtualService 路由管理函数库

提供数据库操作和业务逻辑函数，供 kubedoor-master.py 调用
"""

import json
import re
from typing import Dict, Any, Optional, List
import os
from datetime import datetime
import psycopg
from psycopg.rows import dict_row
from pydantic import BaseModel, Field, field_validator, ValidationError
from aiohttp import web
from loguru import logger


# Istio/K8S 的 timeout 使用 Go duration 格式，必须带单位后缀（如 10s、1m、500ms、1.5s）
# 纯数字（如 "7"）会被 Istio 校验拒绝，导致下发无效，因此在入库前拦截
DURATION_PATTERN = re.compile(r'^\d+(\.\d+)?(ns|us|µs|ms|s|m|h)$')


def validate_timeout(value: Optional[str]) -> Optional[str]:
    """校验并规范化 timeout 字段。

    - 去除首尾空格
    - 空字符串视为未设置（返回 None）
    - 必须符合 Go duration 格式（带单位后缀），否则抛出 ValueError
    """
    if value is None:
        return None
    value = value.strip()
    if value == "":
        return None
    if not DURATION_PATTERN.match(value):
        raise ValueError(f"timeout格式错误：'{value}' 必须带时间单位后缀（如 10s、1m、500ms），不能是纯数字")
    return value


def format_validation_error(exc: ValidationError) -> str:
    """将 pydantic 的 ValidationError 转成简洁可读的中文提示"""
    msgs = []
    for err in exc.errors():
        loc = ".".join(str(x) for x in err.get('loc', ())) or "参数"
        msg = err.get('msg', '校验失败')
        # 去掉 pydantic 自动附加的 "Value error, " 前缀
        msg = msg.replace("Value error, ", "")
        msgs.append(f"{loc}: {msg}")
    return "；".join(msgs)


def datetime_serializer(obj):
    """JSON序列化时处理datetime对象"""
    if isinstance(obj, datetime):
        return obj.strftime('%Y-%m-%d %H:%M:%S')
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


def safe_json_response(data, status=200):
    """安全的JSON响应，处理datetime序列化"""
    try:
        return web.json_response(
            data, status=status, dumps=lambda x: json.dumps(x, default=datetime_serializer, ensure_ascii=False)
        )
    except Exception as e:
        logger.error(f"JSON序列化错误: {e}")
        return web.json_response({"error": "数据序列化失败"}, status=500)


try:
    from dotenv import load_dotenv
    import os

    # 尝试加载.env文件
    env_path = os.path.join(os.path.dirname(__file__), '.env')
    load_dotenv(env_path)
except (ImportError, FileNotFoundError):
    pass

# PostgreSQL 连接配置：优先从环境变量读取（PG_*），端口默认 5432，库名默认 kubedoor
db_host = os.environ.get('PG_HOST', 'localhost')
db_port = int(os.environ.get('PG_PORT') or '5432')
db_user = os.environ.get('PG_USER', 'postgres')
db_password = os.environ.get('PG_PASSWORD', '')
db_name = os.environ.get('PG_DATABASE', 'kubedoor')


# ==================== Pydantic模型定义 ====================


# VS级别模型
class VSCreateRequest(BaseModel):
    """创建VirtualService请求模型"""

    name: str = Field(..., description="VirtualService名称")
    namespace: str = Field(default="default", description="命名空间")
    gateways: Optional[List[str]] = Field(default=None, description="网关列表")
    hosts: List[str] = Field(..., description="主机列表")
    protocol: str = Field(default="http", description="协议类型")
    df_forward_type: Optional[str] = Field(default=None, description="默认路由类型: route或delegate")
    df_forward_detail: Optional[Dict[str, Any] | List[Dict[str, Any]]] = Field(default=None, description="默认路由详情")
    df_forward_timeout: Optional[str] = Field(default=None, description="默认路由超时")
    k8s_clusters: List[str] = Field(..., description="关联的K8S集群列表")

    @field_validator('df_forward_timeout')
    @classmethod
    def _validate_df_forward_timeout(cls, v):
        return validate_timeout(v)


class VSUpdateRequest(BaseModel):
    """更新VirtualService请求模型"""

    name: Optional[str] = Field(default=None, description="VirtualService名称")
    namespace: Optional[str] = Field(default=None, description="命名空间")
    gateways: Optional[List[str]] = Field(default=None, description="网关列表")
    hosts: Optional[List[str]] = Field(default=None, description="主机列表")
    protocol: Optional[str] = Field(default=None, description="协议类型")
    df_forward_type: Optional[str] = Field(default=None, description="默认路由类型: route或delegate")
    df_forward_detail: Optional[Dict[str, Any] | List[Dict[str, Any]]] = Field(default=None, description="默认路由详情")
    df_forward_timeout: Optional[str] = Field(default=None, description="默认路由超时")

    @field_validator('df_forward_timeout')
    @classmethod
    def _validate_df_forward_timeout(cls, v):
        return validate_timeout(v)


class VSResponse(BaseModel):
    """VirtualService响应模型"""

    id: int
    name: str
    namespace: str
    gateways: Optional[List[str]]
    hosts: List[str]
    protocol: str
    df_forward_type: Optional[str]
    df_forward_detail: Optional[Dict[str, Any] | List[Dict[str, Any]]]
    df_forward_timeout: Optional[str]
    created_at: datetime
    updated_at: datetime


# HTTP路由级别模型
class HTTPRouteCreateRequest(BaseModel):
    """创建HTTP路由请求模型"""

    name: Optional[str] = Field(default=None, description="路由规则名称")
    match_rules: List[Dict[str, Any]] = Field(..., description="匹配规则")
    rewrite_rules: Optional[Dict[str, Any]] = Field(default=None, description="重写规则")
    forward_type: str = Field(..., description="转发类型: route或delegate")
    forward_detail: Dict[str, Any] | List[Dict[str, Any]] = Field(..., description="转发详情")
    timeout: Optional[str] = Field(default=None, description="超时设置")
    priority: Optional[int] = Field(default=None, description="优先级，不传则自动追加到最后")

    @field_validator('timeout')
    @classmethod
    def _validate_timeout(cls, v):
        return validate_timeout(v)

    @field_validator('match_rules')
    @classmethod
    def _validate_match_rules(cls, v):
        if not v:
            raise ValueError("匹配规则不能为空：请至少填写一条有效的匹配规则（URI/Authority/Headers）")
        return v

    @field_validator('forward_detail')
    @classmethod
    def _validate_forward_detail(cls, v):
        if not v:
            raise ValueError("转发详情不能为空：请完整填写服务名、命名空间和端口，或委托的VS名称和命名空间")
        return v


class HTTPRouteUpdateRequest(BaseModel):
    """更新HTTP路由请求模型"""

    name: Optional[str] = Field(default=None, description="路由规则名称")
    match_rules: List[Dict[str, Any]] = Field(..., description="匹配规则")
    rewrite_rules: Optional[Dict[str, Any]] = Field(default=None, description="重写规则")
    forward_type: str = Field(..., description="转发类型: route或delegate")
    forward_detail: Dict[str, Any] | List[Dict[str, Any]] = Field(..., description="转发详情")
    timeout: Optional[str] = Field(default=None, description="超时设置")
    priority: Optional[int] = Field(default=None, description="优先级，不传则自动追加到最后")

    @field_validator('timeout')
    @classmethod
    def _validate_timeout(cls, v):
        return validate_timeout(v)

    @field_validator('match_rules')
    @classmethod
    def _validate_match_rules(cls, v):
        if not v:
            raise ValueError("匹配规则不能为空：请至少填写一条有效的匹配规则（URI/Authority/Headers）")
        return v

    @field_validator('forward_detail')
    @classmethod
    def _validate_forward_detail(cls, v):
        if not v:
            raise ValueError("转发详情不能为空：请完整填写服务名、命名空间和端口，或委托的VS名称和命名空间")
        return v


class HTTPRouteResponse(BaseModel):
    """HTTP路由响应模型"""

    id: int
    vs_global_id: int
    name: Optional[str]
    priority: int
    match_rules: Optional[List[Dict[str, Any]]]
    rewrite_rules: Optional[Dict[str, Any]]
    forward_type: str
    forward_detail: Dict[str, Any] | List[Dict[str, Any]]
    timeout: Optional[str]
    created_at: datetime
    updated_at: datetime


# 通用响应模型
class MessageResponse(BaseModel):
    """通用消息响应模型"""

    message: str
    data: Optional[Dict[str, Any]] = None


async def connect_database():
    """连接数据库

    直接连接到 PostgreSQL；数据库需由部署方预先创建（不再自动建库）。
    连接默认 row_factory=dict_row，游标默认返回 dict 行。
    注意这里是同步连接，会阻塞主 loop：connect_timeout 保证库连不上时最多卡 5 秒，
    不设的话要等系统 TCP 超时（约 2 分钟），期间整个 master 无响应。
    """
    connection = psycopg.connect(
        host=db_host,
        port=db_port,
        user=db_user,
        password=db_password,
        dbname=db_name,
        autocommit=False,
        row_factory=dict_row,
        connect_timeout=5,
    )
    return connection


async def close_database(connection):
    """关闭数据库连接"""
    if connection and not connection.closed:
        connection.close()


async def reorder_route_priorities(vs_global_id: int, connection):
    """重新整理路由规则的优先级，确保连续性"""
    cursor = connection.cursor(row_factory=dict_row)

    # 获取所有路由规则，按当前优先级排序
    cursor.execute(
        """
    SELECT id FROM vs_http_routes WHERE vs_global_id = %s ORDER BY priority ASC, id ASC
    """,
        (vs_global_id,),
    )

    routes = cursor.fetchall()

    # 重新分配优先级
    for index, route in enumerate(routes):
        new_priority = (index + 1) * 10  # 使用10的倍数，便于后续插入
        cursor.execute(
            """
        UPDATE vs_http_routes SET priority = %s WHERE id = %s
        """,
            (new_priority, route['id']),
        )

    connection.commit()
    print(f"已重新整理 {len(routes)} 条路由规则的优先级")


async def insert_route_with_priority(
    vs_global_id: int, route_data: Dict[str, Any], priority: int = None, connection=None
):
    """插入路由规则

    Args:
        vs_global_id: VirtualService全局配置ID
        route_data: 路由规则数据
        priority: 优先级（可选，不传则自动追加到最后）
        connection: 数据库连接
    """
    cursor = connection.cursor(row_factory=dict_row)

    # 如果没有传入priority，自动追加到最后
    if priority is None:
        cursor.execute(
            "SELECT MAX(priority) as max_priority FROM vs_http_routes WHERE vs_global_id = %s", (vs_global_id,)
        )
        result = cursor.fetchone()
        priority = (result['max_priority'] or 0) + 10
    else:
        # 检查priority是否已存在
        cursor.execute(
            "SELECT id FROM vs_http_routes WHERE vs_global_id = %s AND priority = %s", (vs_global_id, priority)
        )
        if cursor.fetchone():
            raise ValueError(f"Priority {priority} already exists")

    # 插入新路由规则
    cursor.execute(
        """
    INSERT INTO vs_http_routes
    (vs_global_id, name, priority, match_rules, rewrite_rules, forward_type, forward_detail, timeout)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    RETURNING id
    """,
        (
            vs_global_id,
            route_data.get('name'),
            priority,
            route_data.get('match_rules'),
            route_data.get('rewrite_rules'),
            route_data['forward_type'],
            route_data['forward_detail'],
            route_data.get('timeout'),
        ),
    )

    new_id = cursor.fetchone()['id']
    connection.commit()
    print(f"已插入新的路由规则，优先级: {priority}")
    return new_id


async def get_routes_by_priority(vs_global_id: int, connection):
    """获取路由规则列表，按优先级排序"""
    cursor = connection.cursor(row_factory=dict_row)

    # 获取路由规则，按优先级排序
    cursor.execute(
        """
    SELECT id, vs_global_id, name, priority, match_rules, rewrite_rules, forward_type, forward_detail, timeout, created_at, updated_at
    FROM vs_http_routes 
    WHERE vs_global_id = %s
    ORDER BY priority ASC, id ASC
    """,
        (vs_global_id,),
    )

    routes = cursor.fetchall()
    return routes


async def generate_virtualservice_json(vs_id: int, connection) -> Optional[Dict[str, Any]]:
    """根据vs_id生成VirtualService JSON配置"""
    cursor = connection.cursor(row_factory=dict_row)

    # 获取全局配置
    cursor.execute(
        """
    SELECT * FROM vs_global WHERE id = %s
    """,
        (vs_id,),
    )

    global_config = cursor.fetchone()
    if not global_config:
        return None

    # 获取路由规则，按优先级排序
    cursor.execute(
        """
    SELECT * FROM vs_http_routes WHERE vs_global_id = %s ORDER BY priority ASC, id ASC
    """,
        (global_config['id'],),
    )

    routes = cursor.fetchall()

    # 构建VirtualService结构
    vs_config = {
        "apiVersion": "networking.istio.io/v1beta1",
        "kind": "VirtualService",
        "metadata": {"name": global_config['name'], "namespace": global_config['namespace']},
        "spec": {"hosts": json.loads(global_config['hosts'])},
    }

    # 添加gateways（如果有）
    if global_config['gateways']:
        vs_config['spec']['gateways'] = json.loads(global_config['gateways'])

    # 构建HTTP路由规则
    http_routes = []
    for route in routes:
        http_route = {}

        # 添加name（如果有）
        if route['name']:
            http_route['name'] = route['name']

        # 添加match规则
        if route['match_rules']:
            http_route['match'] = json.loads(route['match_rules'])

        # 添加rewrite规则
        if route['rewrite_rules']:
            http_route['rewrite'] = json.loads(route['rewrite_rules'])

        # 添加转发规则
        if route['forward_type'] == 'route':
            http_route['route'] = json.loads(route['forward_detail'])
        elif route['forward_type'] == 'delegate':
            http_route['delegate'] = json.loads(route['forward_detail'])

        # 添加timeout
        if route['timeout']:
            http_route['timeout'] = route['timeout']

        http_routes.append(http_route)

    # 添加默认路由（如果配置了）
    if global_config['df_forward_type'] and global_config['df_forward_detail']:
        default_route = {}

        # 默认路由没有match条件，直接添加转发规则
        if global_config['df_forward_type'] == 'route':
            default_route['route'] = json.loads(global_config['df_forward_detail'])
        elif global_config['df_forward_type'] == 'delegate':
            default_route['delegate'] = json.loads(global_config['df_forward_detail'])

        # 添加默认路由的timeout
        if global_config['df_forward_timeout']:
            default_route['timeout'] = global_config['df_forward_timeout']

        http_routes.append(default_route)

    vs_config['spec']['http'] = http_routes

    # 返回JSON格式的配置
    return vs_config


# ==================== K8S数据同步函数 ====================
async def sync_vs_from_k8s(cluster_name: str, vs_data_list: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    将从K8S采集到的VirtualService数据写入数据库
    包含清理现有数据和同步新数据的完整流程

    Args:
        cluster_name: K8S集群名称
        vs_data_list: VirtualService数据列表

    Returns:
        Dict[str, Any]: 同步结果统计

    Raises:
        Exception: 当同步过程中发生错误时抛出异常
    """
    connection = await connect_database()
    if not connection:
        logger.error("数据库连接失败")
        return safe_json_response({"error": "数据库连接失败"}, status=500)
    cursor = None
    processed_count = 0

    try:
        # autocommit=False 下事务在首个语句执行时隐式开启
        cursor = connection.cursor(row_factory=dict_row)

        # ========== 清理现有数据逻辑 ==========
        logger.info(f"开始清理集群 '{cluster_name}' 的现有数据")

        # 1. 查找指定集群的所有vs_id
        cursor.execute("SELECT vs_id FROM k8s_cluster WHERE k8s_name = %s", (cluster_name,))
        cluster_records = cursor.fetchall()

        if cluster_records:
            vs_ids = [record['vs_id'] for record in cluster_records]
            vs_ids_str = ','.join(map(str, vs_ids))
            logger.info(f"找到集群 '{cluster_name}' 关联的VirtualService ID: {vs_ids}")

            # 查询这些VS ID关联的所有集群（因为VS ID和集群是多对多关系）
            cursor.execute(
                f"SELECT k8s_name, COUNT(*) as count FROM k8s_cluster WHERE vs_id IN ({vs_ids_str}) GROUP BY k8s_name",
            )
            affected_clusters = cursor.fetchall()
            affected_cluster_info = {cluster['k8s_name']: cluster['count'] for cluster in affected_clusters}

            # 2. 删除vs_http_routes表中的相关数据
            delete_routes_sql = f"DELETE FROM vs_http_routes WHERE vs_global_id IN ({vs_ids_str})"
            cursor.execute(delete_routes_sql)
            routes_deleted = cursor.rowcount
            logger.info(f"删除了 {routes_deleted} 条HTTP路由记录")

            # 3. 删除vs_global表中的相关数据
            delete_global_sql = f"DELETE FROM vs_global WHERE id IN ({vs_ids_str})"
            cursor.execute(delete_global_sql)
            global_deleted = cursor.rowcount
            logger.info(f"删除了 {global_deleted} 条全局配置记录")

            # 4. 删除k8s_cluster表中的相关数据
            delete_cluster_sql = f"DELETE FROM k8s_cluster WHERE vs_id IN ({vs_ids_str})"
            cursor.execute(delete_cluster_sql)
            cluster_deleted = cursor.rowcount

            # 按集群分别显示删除的记录数
            for cluster_name_affected, record_count in affected_cluster_info.items():
                logger.info(f"{cluster_name_affected} 集群删除了 {record_count} 条关联记录")
        else:
            logger.info(f"集群 '{cluster_name}' 没有找到相关数据")

        logger.info(f"集群 '{cluster_name}' 数据清理完成，开始同步新数据")

        # ========== 同步新数据逻辑 ==========

        logger.info(f"开始同步集群 '{cluster_name}' 的VirtualService数据，共 {len(vs_data_list)} 个")

        for vs_data in vs_data_list:
            try:
                # 提取基本信息
                vs_name = vs_data.get('name')
                vs_namespace = vs_data.get('namespace', 'default')
                spec = vs_data.get('spec', {})

                # 1. 检查是否已存在相同name+namespace的记录
                cursor.execute("SELECT id FROM vs_global WHERE name = %s AND namespace = %s", (vs_name, vs_namespace))
                existing_record = cursor.fetchone()

                if existing_record:
                    return {
                        "success": False,
                        "error": f"VirtualService '{vs_name}' 在命名空间 '{vs_namespace}' 中已存在，ID: {existing_record['id']}",
                    }

                # 2. 写入vs_global表
                gateways = json.dumps(spec.get('gateways', []))
                hosts = json.dumps(spec.get('hosts', []))

                cursor.execute(
                    """
                    INSERT INTO vs_global (name, namespace, gateways, hosts, protocol)
                    VALUES (%s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (vs_name, vs_namespace, gateways, hosts, 'http'),
                )

                vs_global_id = cursor.fetchone()['id']
                logger.info(f"创建VirtualService '{vs_name}' (ID: {vs_global_id})")

                # 3. 写入k8s_cluster表
                cursor.execute(
                    """
                    INSERT INTO k8s_cluster (k8s_name, vs_id)
                    VALUES (%s, %s)
                    """,
                    (cluster_name, vs_global_id),
                )

                # 4. 处理HTTP路由规则
                http_routes = spec.get('http', [])
                default_route_found = False
                priority_counter = 10  # 优先级计数器，从10开始

                for route_data in http_routes:
                    # 检查是否有match字段
                    if 'match' in route_data:
                        # 有match字段，写入vs_http_routes表
                        route_name = route_data.get('name')
                        match_rules = json.dumps(route_data.get('match', []))
                        rewrite_rules = json.dumps(route_data.get('rewrite')) if route_data.get('rewrite') else None
                        timeout = route_data.get('timeout')

                        # 确定forward_type和forward_detail
                        forward_type = None
                        forward_detail = None

                        if 'route' in route_data:
                            forward_type = 'route'
                            forward_detail = json.dumps(route_data['route'])
                        elif 'delegate' in route_data:
                            forward_type = 'delegate'
                            forward_detail = json.dumps(route_data['delegate'])

                        if forward_type:
                            cursor.execute(
                                """
                                INSERT INTO vs_http_routes 
                                (vs_global_id, name, priority, match_rules, rewrite_rules, forward_type, forward_detail, timeout)
                                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                                """,
                                (
                                    vs_global_id,
                                    route_name,
                                    priority_counter,
                                    match_rules,
                                    rewrite_rules,
                                    forward_type,
                                    forward_detail,
                                    timeout,
                                ),
                            )
                            logger.debug(f"添加HTTP路由规则: {route_name or 'unnamed'} (优先级: {priority_counter})")
                            priority_counter += 10  # 优先级递增10
                    else:
                        # 没有match字段，更新vs_global表的默认路由
                        if not default_route_found:  # 只处理第一个默认路由
                            df_forward_type = None
                            df_forward_detail = None
                            df_timeout = route_data.get('timeout')

                            if 'route' in route_data:
                                df_forward_type = 'route'
                                df_forward_detail = json.dumps(route_data['route'])
                            elif 'delegate' in route_data:
                                df_forward_type = 'delegate'
                                df_forward_detail = json.dumps(route_data['delegate'])

                            if df_forward_type:
                                cursor.execute(
                                    """
                                    UPDATE vs_global 
                                    SET df_forward_type = %s, df_forward_detail = %s, df_forward_timeout = %s
                                    WHERE id = %s
                                    """,
                                    (df_forward_type, df_forward_detail, df_timeout, vs_global_id),
                                )
                                logger.debug(f"更新默认路由配置: {df_forward_type}")
                                default_route_found = True

                processed_count += 1

            except Exception as e:
                logger.error(f"处理VirtualService '{vs_data.get('name', 'unknown')}' 时发生错误: {e}")
                raise e

        # 提交事务
        connection.commit()
        logger.info(f"集群 '{cluster_name}' VirtualService数据同步完成，共处理 {processed_count} 个")

        return {
            "success": True,
            "message": f"成功同步 {processed_count} 个VirtualService",
            "processed": processed_count,
            "cluster_name": cluster_name,
        }

    except psycopg.Error as e:
        # 数据库错误，回滚事务
        if connection:
            connection.rollback()
        error_msg = f"同步集群 '{cluster_name}' VirtualService数据时发生数据库错误: {e}"
        logger.error(error_msg)
        raise Exception(error_msg)

    except Exception as e:
        # 其他错误，回滚事务
        if connection:
            connection.rollback()
        error_msg = f"同步集群 '{cluster_name}' VirtualService数据时发生错误: {e}"
        logger.error(error_msg)
        raise Exception(error_msg)

    finally:
        if cursor:
            cursor.close()


# ==================== VS级别操作函数 ====================
async def get_vs_list_by_k8s_cluster(k8s_name: str, connection, namespace: str = None):
    """根据K8S集群名称获取关联的VirtualService列表"""
    cursor = connection.cursor(row_factory=dict_row)

    if namespace:
        # 如果提供了namespace参数，添加namespace过滤条件
        cursor.execute(
            """
            SELECT 
                v.id,
                v.name,
                v.namespace,
                v.gateways,
                v.hosts,
                v.protocol,
                v.df_forward_type,
                v.df_forward_detail,
                v.df_forward_timeout,
                v.created_at,
                v.updated_at,
                k.updated_at as relation_updated_at,
                COALESCE(COUNT(r.id), 0) as route_count
            FROM vs_global v
            INNER JOIN k8s_cluster k ON v.id = k.vs_id
            LEFT JOIN vs_http_routes r ON v.id = r.vs_global_id
            WHERE k.k8s_name = %s AND v.namespace = %s
            GROUP BY v.id, v.name, v.namespace, v.gateways, v.hosts, v.protocol, 
                     v.df_forward_type, v.df_forward_detail, v.df_forward_timeout, 
                     v.created_at, v.updated_at, k.updated_at
            ORDER BY v.created_at DESC
            """,
            (k8s_name, namespace),
        )
    else:
        # 如果没有提供namespace参数，查询所有namespace的VS
        cursor.execute(
            """
            SELECT 
                v.id,
                v.name,
                v.namespace,
                v.gateways,
                v.hosts,
                v.protocol,
                v.df_forward_type,
                v.df_forward_detail,
                v.df_forward_timeout,
                v.created_at,
                v.updated_at,
                k.updated_at as relation_updated_at,
                COALESCE(COUNT(r.id), 0) as route_count
            FROM vs_global v
            INNER JOIN k8s_cluster k ON v.id = k.vs_id
            LEFT JOIN vs_http_routes r ON v.id = r.vs_global_id
            WHERE k.k8s_name = %s
            GROUP BY v.id, v.name, v.namespace, v.gateways, v.hosts, v.protocol, 
                     v.df_forward_type, v.df_forward_detail, v.df_forward_timeout, 
                     v.created_at, v.updated_at, k.updated_at
            ORDER BY v.created_at DESC
            """,
            (k8s_name,),
        )
    return cursor.fetchall()


async def create_vs(vs_request: VSCreateRequest, connection):
    """创建VirtualService"""
    cursor = connection.cursor()

    # 检查VS名称是否已存在
    cursor.execute(
        "SELECT id FROM vs_global WHERE name = %s AND namespace = %s", (vs_request.name, vs_request.namespace)
    )
    if cursor.fetchone():
        raise ValueError("VirtualService名称已存在")

    # 插入新的VS
    cursor.execute(
        """INSERT INTO vs_global
           (name, namespace, hosts, gateways, protocol, df_forward_type, df_forward_detail, df_forward_timeout, created_at, updated_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           RETURNING id""",
        (
            vs_request.name,
            vs_request.namespace,
            json.dumps(vs_request.hosts),
            json.dumps(vs_request.gateways) if vs_request.gateways else None,
            vs_request.protocol,
            vs_request.df_forward_type,
            json.dumps(vs_request.df_forward_detail) if vs_request.df_forward_detail else None,
            vs_request.df_forward_timeout,
            datetime.now(),
            datetime.now(),
        ),
    )

    new_id = cursor.fetchone()['id']
    connection.commit()
    return new_id


async def get_vs_by_name(vs_name: str, namespace: str, connection):
    """根据名称获取VirtualService详情"""
    cursor = connection.cursor(row_factory=dict_row)
    cursor.execute("SELECT * FROM vs_global WHERE name = %s AND namespace = %s", (vs_name, namespace))
    return cursor.fetchone()


async def get_vs_by_id(vs_id: int, connection):
    """根据ID获取VirtualService详情"""
    cursor = connection.cursor(row_factory=dict_row)
    cursor.execute("SELECT * FROM vs_global WHERE id = %s", (vs_id,))
    return cursor.fetchone()


async def get_k8s_clusters_by_vs(vs_id: int, connection):
    """根据VirtualService ID获取关联的所有K8S集群"""
    cursor = connection.cursor(row_factory=dict_row)
    cursor.execute(
        """
        SELECT 
            k.id,
            k.k8s_name,
            k.updated_at
        FROM k8s_cluster k
        WHERE k.vs_id = %s
        ORDER BY k.k8s_name
        """,
        (vs_id,),
    )
    return cursor.fetchall()


async def add_k8s_cluster_relations(vs_id: int, k8s_clusters: List[str], connection):
    """为VirtualService添加K8S集群关联关系"""
    if not k8s_clusters:
        return

    cursor = connection.cursor()
    for k8s_name in k8s_clusters:
        cursor.execute(
            """
            INSERT INTO k8s_cluster (k8s_name, vs_id)
            VALUES (%s, %s)
            ON CONFLICT (k8s_name, vs_id) DO UPDATE SET
                updated_at = now()
            """,
            (k8s_name, vs_id),
        )
    connection.commit()


async def update_k8s_cluster_relations(vs_id: int, k8s_clusters: List[str], connection):
    """编辑VirtualService的K8S集群关联关系

    Args:
        vs_id: VirtualService的ID
        k8s_clusters: K8S集群名称列表
        connection: 数据库连接
    """
    cursor = connection.cursor()

    # 先删除该vs_id的所有关联关系
    cursor.execute("DELETE FROM k8s_cluster WHERE vs_id = %s", (vs_id,))

    # 如果有新的K8S集群列表，则添加关联关系
    if k8s_clusters:
        for k8s_name in k8s_clusters:
            cursor.execute(
                """
                INSERT INTO k8s_cluster (k8s_name, vs_id)
                VALUES (%s, %s)
                """,
                (k8s_name, vs_id),
            )

    # 提交事务
    connection.commit()


async def update_vs_by_id(vs_id: int, vs_request: VSUpdateRequest, connection):
    """根据ID更新VirtualService"""
    cursor = connection.cursor()

    # 检查VS是否存在
    cursor.execute("SELECT id FROM vs_global WHERE id = %s", (vs_id,))
    if not cursor.fetchone():
        raise ValueError("VirtualService不存在")

    # 更新VS，所有字段都写入数据库（包括None值）
    cursor.execute(
        """UPDATE vs_global SET 
           name = %s, namespace = %s, gateways = %s, hosts = %s, protocol = %s, 
           df_forward_type = %s, df_forward_detail = %s, df_forward_timeout = %s, updated_at = %s 
           WHERE id = %s""",
        (
            vs_request.name,
            vs_request.namespace,
            json.dumps(vs_request.gateways) if vs_request.gateways else None,
            json.dumps(vs_request.hosts) if vs_request.hosts else None,
            vs_request.protocol,
            vs_request.df_forward_type,
            json.dumps(vs_request.df_forward_detail) if vs_request.df_forward_detail else None,
            vs_request.df_forward_timeout,
            datetime.now(),
            vs_id,
        ),
    )

    connection.commit()


async def delete_vs(vs_name: str, namespace: str, connection):
    """删除VirtualService"""
    cursor = connection.cursor()

    # 获取VS ID
    cursor.execute("SELECT id FROM vs_global WHERE name = %s AND namespace = %s", (vs_name, namespace))
    vs_record = cursor.fetchone()
    if not vs_record:
        raise ValueError("VirtualService不存在")

    vs_global_id = vs_record['id']

    # 删除相关的HTTP路由
    cursor.execute("DELETE FROM vs_http_routes WHERE vs_global_id = %s", (vs_global_id,))

    # 删除VS
    cursor.execute("DELETE FROM vs_global WHERE name = %s AND namespace = %s", (vs_name, namespace))

    connection.commit()


async def delete_vs_by_id(vs_id: int, connection):
    """根据ID删除VirtualService"""
    cursor = connection.cursor()

    # 检查VS是否存在
    cursor.execute("SELECT id FROM vs_global WHERE id = %s", (vs_id,))
    if not cursor.fetchone():
        raise ValueError("VirtualService不存在")

    # 删除相关的HTTP路由
    cursor.execute("DELETE FROM vs_http_routes WHERE vs_global_id = %s", (vs_id,))

    # 删除VS
    cursor.execute("DELETE FROM vs_global WHERE id = %s", (vs_id,))

    connection.commit()


# ==================== HTTP路由级别操作函数 ====================


async def create_route(vs_global_id: int, route_request: HTTPRouteCreateRequest, connection):
    """创建HTTP路由"""
    return await insert_route_with_priority(
        vs_global_id,
        {
            'name': route_request.name,
            'match_rules': json.dumps(route_request.match_rules),
            'rewrite_rules': json.dumps(route_request.rewrite_rules) if route_request.rewrite_rules else None,
            'forward_type': route_request.forward_type,
            'forward_detail': json.dumps(route_request.forward_detail),
            'timeout': route_request.timeout,
        },
        route_request.priority,
        connection,
    )


async def get_route_by_id(route_id: int, connection):
    """根据ID获取HTTP路由详情"""
    cursor = connection.cursor(row_factory=dict_row)
    cursor.execute("SELECT * FROM vs_http_routes WHERE id = %s", (route_id,))
    return cursor.fetchone()


async def update_route(route_id: int, vs_id: int, route_request: HTTPRouteUpdateRequest, connection):
    """更新HTTP路由"""
    cursor = connection.cursor(row_factory=dict_row)

    vs_global_id = vs_id
    priority = route_request.priority

    # 处理优先级逻辑
    if priority is not None:
        # 检查priority是否已存在（排除当前路由）
        cursor.execute(
            "SELECT id FROM vs_http_routes WHERE vs_global_id = %s AND priority = %s AND id != %s",
            (vs_global_id, priority, route_id),
        )
        if cursor.fetchone():
            raise ValueError(f"Priority {priority} already exists")
    else:
        # 如果没有传入priority，自动追加到最后
        cursor.execute(
            "SELECT MAX(priority) as max_priority FROM vs_http_routes WHERE vs_global_id = %s", (vs_global_id,)
        )
        result = cursor.fetchone()
        priority = (result['max_priority'] or 0) + 10

    # 更新HTTP路由
    cursor.execute(
        """UPDATE vs_http_routes 
           SET name = %s, match_rules = %s, rewrite_rules = %s, forward_type = %s, forward_detail = %s, timeout = %s, priority = %s, updated_at = %s 
           WHERE id = %s""",
        (
            route_request.name,
            json.dumps(route_request.match_rules),
            json.dumps(route_request.rewrite_rules) if route_request.rewrite_rules else None,
            route_request.forward_type,
            json.dumps(route_request.forward_detail),
            route_request.timeout,
            priority,
            datetime.now(),
            route_id,
        ),
    )

    connection.commit()


async def delete_route(route_id: int, connection):
    """删除HTTP路由"""
    cursor = connection.cursor()

    # 获取路由信息
    cursor.execute("SELECT vs_global_id FROM vs_http_routes WHERE id = %s", (route_id,))
    result = cursor.fetchone()
    if not result:
        raise ValueError("HTTP路由不存在")

    vs_global_id = result['vs_global_id']

    # 删除HTTP路由
    cursor.execute("DELETE FROM vs_http_routes WHERE id = %s", (route_id,))

    connection.commit()

    # 重新整理优先级
    await reorder_route_priorities(vs_global_id, connection)


async def update_route_priority(route_id: int, new_priority: int, connection):
    """更新路由优先级"""
    cursor = connection.cursor()

    # 获取路由信息
    cursor.execute("SELECT vs_global_id, priority FROM vs_http_routes WHERE id = %s", (route_id,))
    result = cursor.fetchone()
    if not result:
        raise ValueError("HTTP路由不存在")

    vs_global_id, current_priority = result['vs_global_id'], result['priority']

    if current_priority == new_priority:
        return  # 优先级没有变化

    # 检查新优先级是否已存在
    cursor.execute(
        "SELECT id FROM vs_http_routes WHERE vs_global_id = %s AND priority = %s AND id != %s",
        (vs_global_id, new_priority, route_id),
    )
    if cursor.fetchone():
        raise ValueError(f"Priority {new_priority} already exists")

    # 更新优先级
    cursor.execute(
        "UPDATE vs_http_routes SET priority = %s, updated_at = %s WHERE id = %s",
        (new_priority, datetime.now(), route_id),
    )

    connection.commit()


# ==================== Istio Route 相关处理函数 ====================
async def get_vs_list_handler(request):
    """根据K8S集群名称获取VirtualService列表，或通过vs_id获取单个VS详情"""
    try:
        logger.info(f"get_vs_list_handler 请求参数: {dict(request.query)}")

        connection = await connect_database()
        if not connection:
            logger.error("数据库连接失败")
            return safe_json_response({"error": "数据库连接失败"}, status=500)

        # 从query参数中获取vs_id，如果有则返回单个VS详情
        vs_id = request.query.get('vs_id')
        if vs_id:
            try:
                vs_id = int(vs_id)
                logger.info(f"查询单个VS详情，vs_id: {vs_id}")
                vs_detail = await get_vs_by_id(vs_id, connection)

                if vs_detail:
                    # 获取关联的K8S集群列表
                    logger.info(f"查询VS关联的K8S集群列表，vs_id: {vs_id}")
                    k8s_clusters = await get_k8s_clusters_by_vs(vs_id, connection)
                    await close_database(connection)

                    # 将集群列表添加到VS详情中
                    vs_detail['k8s_clusters'] = k8s_clusters
                    logger.info(f"找到VS详情: {vs_detail['name']}，关联 {len(k8s_clusters)} 个K8S集群")
                    return safe_json_response({"data": vs_detail})
                else:
                    await close_database(connection)
                    logger.warning(f"未找到VS，vs_id: {vs_id}")
                    return safe_json_response({"error": "VirtualService not found"}, status=404)
            except ValueError:
                await close_database(connection)
                logger.error(f"vs_id格式错误: {vs_id}")
                return safe_json_response({"error": "vs_id必须是有效的整数"}, status=400)

        # 从query参数中获取k8s_cluster，必须提供
        k8s_cluster = request.query.get('k8s_cluster')
        # 从query参数中获取namespace，可选参数
        namespace = request.query.get('namespace')

        if not k8s_cluster:
            await close_database(connection)
            logger.error("缺少k8s_cluster参数")
            return safe_json_response({"error": "缺少必需的参数: k8s_cluster"}, status=400)

        # 根据K8S集群名称查询关联的VirtualService
        if namespace:
            logger.info(f"查询K8S集群关联的VS列表，k8s_cluster: {k8s_cluster}, namespace: {namespace}")
        else:
            logger.info(f"查询K8S集群关联的VS列表，k8s_cluster: {k8s_cluster}")
        vs_list = await get_vs_list_by_k8s_cluster(k8s_cluster, connection, namespace)
        logger.info(f"查询到 {len(vs_list)} 个VirtualService")

        await close_database(connection)
        return safe_json_response({"data": vs_list})
    except Exception as e:
        logger.error(f"get_vs_list_handler 异常: {e}")
        return safe_json_response({"error": str(e)}, status=500)


async def create_vs_handler(request):
    """创建VirtualService"""
    try:
        data = await request.json()
        logger.info(f"create_vs_handler 请求数据: {data}")
        vs_request = VSCreateRequest(**data)

        connection = await connect_database()
        if not connection:
            logger.error("数据库连接失败")
            return safe_json_response({"error": "数据库连接失败"}, status=500)

        logger.info(f"创建新的VS: {vs_request.name}/{vs_request.namespace}")
        vs_id = await create_vs(vs_request, connection)
        logger.info(f"VS创建成功，vs_id: {vs_id}")

        # 添加K8S集群关联关系（k8s_clusters为必填字段）
        logger.info(f"添加K8S集群关联关系，集群列表: {vs_request.k8s_clusters}")
        await add_k8s_cluster_relations(vs_id, vs_request.k8s_clusters, connection)
        logger.info(f"成功关联 {len(vs_request.k8s_clusters)} 个K8S集群")

        await close_database(connection)
        return safe_json_response({"success": True, "message": "VirtualService创建成功", "id": vs_id})
    except ValidationError as e:
        err_msg = format_validation_error(e)
        logger.error(f"create_vs_handler 参数校验失败: {err_msg}")
        return safe_json_response({"error": err_msg}, status=400)
    except Exception as e:
        logger.error(f"create_vs_handler 异常: {e}")
        return safe_json_response({"error": str(e)}, status=500)


async def update_vs_handler(request):
    """更新VirtualService"""
    try:
        vs_id = request.query.get('vs_id')
        logger.info(f"update_vs_handler 请求参数: vs_id={vs_id}")

        if not vs_id:
            logger.error("缺少vs_id参数")
            return safe_json_response({"error": "缺少必需的参数: vs_id"}, status=400)

        try:
            vs_id = int(vs_id)
        except ValueError:
            logger.error(f"vs_id格式错误: {vs_id}")
            return safe_json_response({"error": "vs_id必须是有效的整数"}, status=400)

        data = await request.json()
        logger.info(f"update_vs_handler 请求数据: {data}")
        vs_request = VSUpdateRequest(**data)

        connection = await connect_database()
        if not connection:
            logger.error("数据库连接失败")
            return safe_json_response({"error": "数据库连接失败"}, status=500)

        logger.info(f"更新VS，vs_id: {vs_id}")
        await update_vs_by_id(vs_id, vs_request, connection)
        await close_database(connection)

        logger.info(f"VS更新成功，vs_id: {vs_id}")
        return safe_json_response({"success": True, "message": "VirtualService更新成功"})
    except ValidationError as e:
        err_msg = format_validation_error(e)
        logger.error(f"update_vs_handler 参数校验失败: {err_msg}")
        return safe_json_response({"error": err_msg}, status=400)
    except Exception as e:
        logger.error(f"update_vs_handler 异常: {e}")
        return safe_json_response({"error": str(e)}, status=500)


async def delete_vs_handler(request):
    """删除VirtualService"""
    try:
        vs_id = request.query.get('vs_id')
        logger.info(f"delete_vs_handler 请求参数: vs_id={vs_id}")

        if not vs_id:
            logger.error("缺少vs_id参数")
            return safe_json_response({"error": "缺少必需的参数: vs_id"}, status=400)

        try:
            vs_id = int(vs_id)
        except ValueError:
            logger.error(f"vs_id格式错误: {vs_id}")
            return safe_json_response({"error": "vs_id必须是有效的整数"}, status=400)

        connection = await connect_database()
        if not connection:
            logger.error("数据库连接失败")
            return safe_json_response({"error": "数据库连接失败"}, status=500)

        logger.info(f"删除VS，vs_id: {vs_id}")
        await delete_vs_by_id(vs_id, connection)
        await close_database(connection)

        logger.info(f"VS删除成功，vs_id: {vs_id}")
        return safe_json_response({"success": True, "message": "VirtualService删除成功"})
    except Exception as e:
        logger.error(f"delete_vs_handler 异常: {e}")
        return safe_json_response({"error": str(e)}, status=500)


async def get_routes_handler(request):
    """获取指定VirtualService的HTTP路由列表或单个路由详情"""
    try:
        vs_id = request.query.get('vs_id')
        route_id = request.query.get('route_id')
        logger.info(f"get_routes_handler 请求参数: vs_id={vs_id}, route_id={route_id}")

        if not vs_id:
            logger.error("缺少vs_id参数")
            return safe_json_response({"error": "缺少必需的参数: vs_id"}, status=400)

        try:
            vs_id = int(vs_id)
            if route_id:
                route_id = int(route_id)
        except ValueError:
            logger.error(f"参数格式错误: vs_id={vs_id}, route_id={route_id}")
            return safe_json_response({"error": "vs_id和route_id必须是有效的整数"}, status=400)

        connection = await connect_database()
        if not connection:
            logger.error("数据库连接失败")
            return safe_json_response({"error": "数据库连接失败"}, status=500)

        # 如果提供了route_id，返回单个路由详情
        if route_id:
            logger.info(f"查询单个路由详情，route_id: {route_id}")
            route_detail = await get_route_by_id(route_id, connection)
            await close_database(connection)

            if route_detail:
                logger.info(f"找到路由详情: {route_detail.get('name', 'unnamed')}")
                return safe_json_response({"data": route_detail})
            else:
                logger.warning(f"未找到路由，route_id: {route_id}")
                return safe_json_response({"error": "HTTP Route not found"}, status=404)

        # 否则返回VS的所有路由列表
        logger.info(f"查询VS的所有路由列表，vs_id: {vs_id}")
        routes = await get_routes_by_priority(vs_id, connection)
        await close_database(connection)

        logger.info(f"查询到 {len(routes)} 个路由")
        return safe_json_response({"data": routes})
    except Exception as e:
        logger.error(f"get_routes_handler 异常: {e}")
        return safe_json_response({"error": str(e)}, status=500)


async def create_route_handler(request):
    """创建HTTP路由"""
    try:
        vs_id = request.query.get('vs_id')
        logger.info(f"create_route_handler 请求参数: vs_id={vs_id}")

        if not vs_id:
            logger.error("缺少vs_id参数")
            return safe_json_response({"error": "缺少必需的参数: vs_id"}, status=400)

        try:
            vs_id = int(vs_id)
        except ValueError:
            logger.error(f"vs_id格式错误: {vs_id}")
            return safe_json_response({"error": "vs_id必须是有效的整数"}, status=400)

        data = await request.json()
        logger.info(f"create_route_handler 请求数据: {data}")
        route_request = HTTPRouteCreateRequest(**data)

        connection = await connect_database()
        if not connection:
            logger.error("数据库连接失败")
            return safe_json_response({"error": "数据库连接失败"}, status=500)

        logger.info(f"创建新路由规则，vs_id: {vs_id}")
        route_id = await create_route(vs_id, route_request, connection)
        await close_database(connection)

        logger.info(f"路由规则创建成功，route_id: {route_id}")
        return safe_json_response({"success": True, "message": "HTTP路由规则创建成功", "id": route_id})
    except ValidationError as e:
        err_msg = format_validation_error(e)
        logger.error(f"create_route_handler 参数校验失败: {err_msg}")
        return safe_json_response({"error": err_msg}, status=400)
    except Exception as e:
        logger.error(f"create_route_handler 异常: {e}")
        return safe_json_response({"error": str(e)}, status=500)


async def update_route_handler(request):
    """更新HTTP路由"""
    try:
        route_id = request.query.get('route_id')
        vs_id = request.query.get('vs_id')
        logger.info(f"update_route_handler 请求参数: route_id={route_id}, vs_id={vs_id}")

        if not route_id:
            logger.error("缺少route_id参数")
            return safe_json_response({"error": "缺少必需的参数: route_id"}, status=400)

        if not vs_id:
            logger.error("缺少vs_id参数")
            return safe_json_response({"error": "缺少必需的参数: vs_id"}, status=400)

        try:
            route_id = int(route_id)
            vs_id = int(vs_id)
        except ValueError:
            logger.error(f"参数格式错误: route_id={route_id}, vs_id={vs_id}")
            return safe_json_response({"error": "route_id和vs_id必须是有效的整数"}, status=400)

        data = await request.json()
        logger.info(f"update_route_handler 请求数据: {data}")
        route_request = HTTPRouteUpdateRequest(**data)

        connection = await connect_database()
        if not connection:
            logger.error("数据库连接失败")
            return safe_json_response({"error": "数据库连接失败"}, status=500)

        logger.info(f"更新路由，route_id: {route_id}, vs_id: {vs_id}")
        await update_route(route_id, vs_id, route_request, connection)
        await close_database(connection)

        logger.info(f"路由更新成功，route_id: {route_id}")
        return safe_json_response({"success": True, "message": "HTTP路由更新成功"})
    except ValidationError as e:
        err_msg = format_validation_error(e)
        logger.error(f"update_route_handler 参数校验失败: {err_msg}")
        return safe_json_response({"error": err_msg}, status=400)
    except Exception as e:
        logger.error(f"update_route_handler 异常: {e}")
        return safe_json_response({"error": str(e)}, status=500)


async def delete_route_handler(request):
    """删除HTTP路由"""
    try:
        route_id = request.query.get('route_id')
        logger.info(f"delete_route_handler 请求参数: route_id={route_id}")

        if not route_id:
            logger.error("缺少route_id参数")
            return safe_json_response({"error": "缺少必需的参数: route_id"}, status=400)

        try:
            route_id = int(route_id)
        except ValueError:
            logger.error(f"route_id格式错误: {route_id}")
            return safe_json_response({"error": "route_id必须是有效的整数"}, status=400)

        connection = await connect_database()
        if not connection:
            logger.error("数据库连接失败")
            return safe_json_response({"error": "数据库连接失败"}, status=500)

        logger.info(f"删除路由，route_id: {route_id}")
        await delete_route(route_id, connection)
        await close_database(connection)

        logger.info(f"路由删除成功，route_id: {route_id}")
        return safe_json_response({"success": True, "message": "HTTP路由删除成功"})
    except Exception as e:
        logger.error(f"delete_route_handler 异常: {e}")
        return safe_json_response({"error": str(e)}, status=500)


async def reorder_routes_handler(request):
    """重新整理路由优先级"""
    try:
        vs_id = request.query.get('vs_id')
        logger.info(f"reorder_routes_handler 请求参数: vs_id={vs_id}")

        if not vs_id:
            logger.error("缺少vs_id参数")
            return safe_json_response({"error": "缺少必需的参数: vs_id"}, status=400)

        try:
            vs_id = int(vs_id)
        except ValueError:
            logger.error(f"vs_id格式错误: {vs_id}")
            return safe_json_response({"error": "vs_id必须是有效的整数"}, status=400)

        connection = await connect_database()
        if not connection:
            logger.error("数据库连接失败")
            return safe_json_response({"error": "数据库连接失败"}, status=500)

        logger.info(f"重新整理路由优先级，vs_id: {vs_id}")
        await reorder_route_priorities(vs_id, connection)
        await close_database(connection)

        logger.info(f"路由优先级重新整理成功，vs_id: {vs_id}")
        return safe_json_response({"success": True, "message": "路由优先级重新整理成功"})
    except Exception as e:
        logger.error(f"reorder_routes_handler 异常: {e}")
        return safe_json_response({"error": str(e)}, status=500)


async def generate_json_handler(request):
    """生成VirtualService JSON配置"""
    try:
        vs_id = request.query.get('vs_id')
        logger.info(f"generate_json_handler 请求参数: vs_id={vs_id}")

        if not vs_id:
            logger.error("缺少vs_id参数")
            return safe_json_response({"error": "缺少必需的参数: vs_id"}, status=400)

        try:
            vs_id = int(vs_id)
        except ValueError:
            logger.error(f"vs_id格式错误: {vs_id}")
            return safe_json_response({"error": "vs_id必须是有效的整数"}, status=400)

        connection = await connect_database()
        if not connection:
            logger.error("数据库连接失败")
            return safe_json_response({"error": "数据库连接失败"}, status=500)

        logger.info(f"生成VirtualService JSON配置，vs_id: {vs_id}")
        json_config = await generate_virtualservice_json(vs_id, connection)
        await close_database(connection)

        if json_config:
            logger.info(f"JSON配置生成成功，vs_id: {vs_id}")
            return json_config
        else:
            logger.warning(f"未找到VirtualService，vs_id: {vs_id}")
            return safe_json_response({"error": "VirtualService not found"}, status=404)
    except Exception as e:
        logger.error(f"generate_json_handler 异常: {e}")
        return safe_json_response({"error": str(e)}, status=500)


async def health_check_handler(request):
    """健康检查"""
    try:
        logger.info("health_check_handler 健康检查请求")

        # 检查数据库连接
        connection = await connect_database()
        if connection:
            await close_database(connection)
            db_status = True
            logger.info("数据库连接正常")
        else:
            db_status = False
            logger.warning("数据库连接失败")

        return safe_json_response(
            {
                "status": "healthy" if db_status else "unhealthy",
                "database": "connected" if db_status else "disconnected",
            }
        )
    except Exception as e:
        logger.error(f"health_check_handler 异常: {e}")
        return safe_json_response({"error": str(e)}, status=500)


async def update_k8s_vs_handler(request):
    """编辑VirtualService的K8S集群关联关系"""
    try:
        # 获取请求体数据
        body = await request.json()
        logger.info(f"update_k8s_vs_handler 请求体: {body}")

        vs_id = body.get('vs_id')
        k8s_clusters = body.get('k8s_clusters', [])

        if vs_id is None:
            logger.error("缺少vs_id参数")
            return safe_json_response({"error": "缺少必需的参数: vs_id"}, status=400)

        try:
            vs_id = int(vs_id)
        except ValueError:
            logger.error(f"vs_id格式错误: {vs_id}")
            return safe_json_response({"error": "vs_id必须是有效的整数"}, status=400)

        if not isinstance(k8s_clusters, list):
            logger.error(f"k8s_clusters格式错误: {k8s_clusters}")
            return safe_json_response({"error": "k8s_clusters必须是数组"}, status=400)

        connection = await connect_database()
        if not connection:
            logger.error("数据库连接失败")
            return safe_json_response({"error": "数据库连接失败"}, status=500)

        # 检查VS是否存在
        cursor = connection.cursor()
        cursor.execute("SELECT id FROM vs_global WHERE id = %s", (vs_id,))
        if not cursor.fetchone():
            await close_database(connection)
            logger.error(f"VirtualService不存在，vs_id: {vs_id}")
            return safe_json_response({"error": "VirtualService不存在"}, status=404)

        logger.info(f"编辑K8S集群关联关系，vs_id: {vs_id}, k8s_clusters: {k8s_clusters}")
        await update_k8s_cluster_relations(vs_id, k8s_clusters, connection)
        await close_database(connection)

        logger.info(f"K8S集群关联关系编辑成功，vs_id: {vs_id}")
        return safe_json_response({"success": True, "message": "K8S集群关联关系编辑成功"})

    except Exception as e:
        logger.error(f"update_k8s_vs_handler 异常: {e}")
        return safe_json_response({"error": str(e)}, status=500)
