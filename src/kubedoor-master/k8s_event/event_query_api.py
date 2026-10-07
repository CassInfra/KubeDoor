#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
K8S事件查询API模块
提供K8S事件的高级查询接口
"""

import asyncio
from datetime import datetime
from aiohttp import web
from loguru import logger
from utils import day_range
from .pg_event_client import get_pg_event_client


def serialize_datetime_objects(data):
    """将数据中的datetime对象转换为字符串格式"""
    if isinstance(data, list):
        return [serialize_datetime_objects(item) for item in data]
    elif isinstance(data, tuple):
        return tuple(serialize_datetime_objects(item) for item in data)
    elif isinstance(data, dict):
        return {key: serialize_datetime_objects(value) for key, value in data.items()}
    elif isinstance(data, datetime):
        # timestamptz 由 asyncpg 解码成 UTC 时间,先转本地时区(容器 TZ=Asia/Shanghai)再格式化
        return data.astimezone().strftime('%Y-%m-%d %H:%M:%S')
    else:
        return data


async def get_k8s_events_menu_options(request):
    """获取K8S事件查询的菜单选项"""
    try:
        # 获取查询参数
        k8s = request.query.get('k8s')
        start_time_str = request.query.get('start_time')
        end_time_str = request.query.get('end_time')
        namespace = request.query.get('namespace')  # 可选参数

        # 验证必填参数
        if not all([k8s, start_time_str, end_time_str]):
            return web.json_response({"code": 400, "message": "缺少必填参数: k8s, start_time, end_time"})
        try:
            start_ts, end_ts = day_range(start_time_str, end_time_str)
        except ValueError:
            return web.json_response({"code": 400, "message": "start_time / end_time 格式应为 YYYY-MM-DD"})

        # 获取 PG 事件客户端
        pg_client = get_pg_event_client()

        # 查询各字段的唯一值 - 并发执行
        # (PG 列名小写, 返回给前端的 key 保持驼峰以兼容现有前端)
        menu_fields = [
            ('namespace', 'namespace'),
            ('kind', 'kind'),
            ('name', 'name'),
            ('reason', 'reason'),
            ('reportingcomponent', 'reportingComponent'),
            ('reportinginstance', 'reportingInstance'),
        ]

        async def query_field_options(field_key):
            """异步查询单个字段的选项。field_key=(列名, 返回key)"""
            field, out_key = field_key
            try:
                # 构建WHERE条件（移除IS NOT NULL条件以包含空值）
                where_conditions = ["k8s = $1", "lasttimestamp >= $2", "lasttimestamp < $3"]
                params = [k8s, start_ts, end_ts]

                # 如果传入了namespace参数，根据值添加相应的过滤条件
                if namespace and field != 'namespace':
                    if namespace == "[全部]":
                        # [全部]表示不添加namespace过滤条件，查询所有namespace
                        pass
                    elif namespace == "[空值]":
                        # [空值]表示查询namespace为空的记录
                        where_conditions.append("(namespace IS NULL OR namespace = '')")
                    else:
                        # 具体的namespace值
                        params.append(namespace)
                        where_conditions.append(f"namespace = ${len(params)}")

                where_clause = " AND ".join(where_conditions)

                # field 为服务端固定白名单(menu_fields),非用户输入,可安全内插为标识符
                sql = f"""
                SELECT DISTINCT {field}
                FROM k8s_events
                WHERE {where_clause}
                ORDER BY {field}
                LIMIT 1000
                """
                # 在线程池中执行同步的数据库查询
                loop = asyncio.get_running_loop()
                result = await loop.run_in_executor(None, lambda: pg_client.pool.execute_query(sql, params))
                # 包含所有值，包括空值（None、空字符串等）
                field_values = ["[全部]"]  # 在列表第一个位置添加"[全部]"选项
                for row in result:
                    value = row[0] if row[0] else "[空值]"
                    if value not in field_values:  # 去重
                        field_values.append(value)
                return out_key, field_values

            except Exception as e:
                logger.error(f"查询{field}字段选项失败: {e}")
                return out_key, []

        # 并发执行所有字段查询
        tasks = [query_field_options(fk) for fk in menu_fields]
        results = await asyncio.gather(*tasks)

        # 构建结果字典
        menu_options = {key: options for key, options in results}

        return web.json_response({"success": True, "data": menu_options})

    except Exception as e:
        logger.error(f"获取菜单选项失败: {e}")
        return web.json_response({"code": 500, "message": f"获取菜单选项失败: {str(e)}"})


async def query_k8s_events_handler(request):
    """查询K8S事件接口"""
    try:
        # 获取请求体数据
        data = await request.json()

        # 验证必填参数
        required_fields = ['k8s', 'start_time', 'end_time', 'limit']
        for field in required_fields:
            if field not in data:
                return web.json_response({"code": 400, "message": f"缺少必填参数: {field}"})

        # 获取 PG 事件客户端
        pg_client = get_pg_event_client()

        # query_events_advanced 走同步桥,必须放进线程执行;
        # 直接在 handler 里调用会让主 loop 死锁,整个 master 假死
        result = await asyncio.to_thread(
            pg_client.query_events_advanced,
            k8s=data['k8s'],
            start_time=data.get('start_time'),
            end_time=data.get('end_time'),
            limit=int(data['limit']),
            namespace=data.get('namespace'),
            count=data.get('count'),
            level=data.get('level'),
            kind=data.get('kind'),
            name=data.get('name'),
            reason=data.get('reason'),
            reporting_component=data.get('reportingComponent'),
            reporting_instance=data.get('reportingInstance'),
            message=data.get('message'),
        )

        # 序列化datetime对象
        serialized_result = serialize_datetime_objects(result)

        return web.json_response({"success": True, "data": serialized_result, "total": len(serialized_result)})

    except Exception as e:
        logger.error(f"查询K8S事件失败: {e}")
        return web.json_response({"code": 500, "message": f"查询失败: {str(e)}"})
