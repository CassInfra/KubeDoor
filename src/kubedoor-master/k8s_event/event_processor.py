#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
K8S事件处理器模块
负责处理从kubedoor-agent接收到的K8S事件数据，并存储到ClickHouse
"""

from typing import Dict, Any, Optional
from datetime import datetime, timezone, timedelta
from loguru import logger
from .clickhouse_client import get_clickhouse_client
from .event_alert_processor import EventAlertProcessor


class K8SEventProcessor:
    """K8S事件处理器类"""

    def __init__(self):
        """初始化事件处理器"""
        self.clickhouse_client = get_clickhouse_client()
        self.alert_processor = EventAlertProcessor()
        logger.info("K8S事件处理器已初始化")

    def process_event_message(self, message_data: Dict[str, Any]) -> bool:
        """
        处理从WebSocket接收到的K8S事件消息

        Args:
            message_data: 包含K8S事件数据的消息字典

        Returns:
            bool: 处理是否成功
        """
        try:
            # 验证消息类型
            if message_data.get('type') != 'k8s_event':
                logger.warning(f"无效的消息类型: {message_data.get('type')}")
                return False

            # 提取事件数据
            event_data = message_data.get('data', {})
            if not event_data:
                logger.warning("接收到空的事件数据")
                return False

            # 处理事件数据
            processed_data = self._process_event_data(event_data)
            if not processed_data:
                logger.warning("处理事件数据失败")
                return False

            # 存储到ClickHouse
            self.clickhouse_client.upsert_event(processed_data)

            # 处理告警规则匹配
            try:
                # 从事件数据中提取msgToken
                msg_token = event_data.get('msgToken')
                alert_info = self.alert_processor.process_event(processed_data, msg_token)
                if alert_info:
                    logger.debug(f"触发告警: {alert_info['alert_id']} - {alert_info['message']}")
            except Exception as e:
                logger.error(f"处理告警规则失败: {e}")

            logger.debug(
                f"成功处理K8S事件: {processed_data.get('eventUid')} " f"命名空间: {processed_data.get('namespace')}"
            )
            return True

        except Exception as e:
            logger.error(f"处理K8S事件消息失败: {e}")
            logger.error(f"消息数据: {message_data}")
            return False

    def _process_event_data(self, event_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        处理和转换事件数据为ClickHouse格式

        Args:
            event_data: 原始事件数据

        Returns:
            处理后的事件数据字典，如果处理失败返回None
        """
        try:
            # 验证必需字段
            required_fields = ['eventUid', 'eventStatus', 'level', 'kind', 'namespace', 'name']
            for field in required_fields:
                if field not in event_data:
                    logger.error(f"缺少必需字段: {field}")
                    return None

            # 转换时间戳格式
            first_timestamp = self._parse_timestamp(event_data.get('firstTimestamp'))
            last_timestamp = self._parse_timestamp(event_data.get('lastTimestamp'))

            if not first_timestamp or not last_timestamp:
                logger.error("无效的时间戳格式")
                return None

            # 构建处理后的数据
            processed_data = {
                'eventUid': str(event_data['eventUid']),
                'eventStatus': str(event_data['eventStatus']),
                'level': str(event_data['level']),
                'count': int(event_data.get('count', 1)),
                'kind': str(event_data['kind']),
                'k8s': str(event_data.get('k8s', '')),
                'namespace': str(event_data['namespace']),
                'name': str(event_data['name']),
                'reason': str(event_data.get('reason', '')),
                'message': str(event_data.get('message', '')),
                'firstTimestamp': first_timestamp,
                'lastTimestamp': last_timestamp,
                'reportingComponent': str(event_data.get('reportingComponent', '')),
                'reportingInstance': str(event_data.get('reportingInstance', '')),
            }

            # 验证数据完整性
            if not self._validate_processed_data(processed_data):
                return None

            return processed_data

        except Exception as e:
            logger.error(f"处理事件数据失败: {e}")
            logger.error(f"事件数据: {event_data}")
            return None

    def _parse_timestamp(self, timestamp_str: str) -> Optional[datetime]:
        """
        解析时间戳字符串为datetime对象，转换为北京时间

        Args:
            timestamp_str: 时间戳字符串，格式为 2025-08-28T11:16:47Z

        Returns:
            北京时间的datetime对象，解析失败或为None时返回当前北京时间
        """
        # 如果时间戳为空或None，返回当前北京时间
        if not timestamp_str:
            beijing_tz = timezone(timedelta(hours=8))
            return datetime.now(beijing_tz).replace(tzinfo=None)

        try:
            # 解析固定格式的UTC时间：2025-08-28T11:16:47Z
            utc_time = datetime.strptime(timestamp_str, '%Y-%m-%dT%H:%M:%SZ')

            # 设置UTC时区
            utc_time = utc_time.replace(tzinfo=timezone.utc)

            # 转换为北京时间（UTC+8）
            beijing_tz = timezone(timedelta(hours=8))
            beijing_time = utc_time.astimezone(beijing_tz)

            # 返回不带时区信息的datetime对象
            return beijing_time.replace(tzinfo=None)

        except Exception as e:
            logger.error(f"解析时间戳失败 '{timestamp_str}': {e}")
            # 解析失败时返回当前北京时间
            beijing_tz = timezone(timedelta(hours=8))
            return datetime.now(beijing_tz).replace(tzinfo=None)

    def _validate_processed_data(self, data: Dict[str, Any]) -> bool:
        """
        验证处理后的数据完整性

        Args:
            data: 处理后的数据字典

        Returns:
            bool: 验证是否通过
        """
        try:
            # 检查必需字段
            required_fields = [
                'eventUid',
                'eventStatus',
                'level',
                'count',
                'kind',
                'namespace',
                'name',
                'firstTimestamp',
                'lastTimestamp',
            ]

            for field in required_fields:
                if field not in data or data[field] is None:
                    logger.error(f"验证失败: 缺少或为空的字段 '{field}'")
                    return False

            # 检查字段类型和值
            if not isinstance(data['count'], int) or data['count'] < 0:
                logger.error(f"无效的计数值: {data['count']}")
                return False

            if data['eventStatus'] not in ['ADDED', 'MODIFIED', 'DELETED']:
                logger.error(f"无效的事件状态: {data['eventStatus']}")
                return False

            if data['level'] not in ['Normal', 'Warning']:
                logger.error(f"无效的级别: {data['level']}")
                return False

            # 检查时间戳逻辑
            if data['lastTimestamp'] < data['firstTimestamp']:
                logger.error("最后时间戳不能早于首次时间戳")
                return False

            return True

        except Exception as e:
            logger.error(f"验证错误: {e}")
            return False


# 全局事件处理器实例
_event_processor: Optional[K8SEventProcessor] = None


def get_event_processor() -> K8SEventProcessor:
    """获取全局事件处理器实例"""
    global _event_processor
    if _event_processor is None:
        _event_processor = K8SEventProcessor()
    return _event_processor


def reload_alert_rules() -> None:
    """重新加载告警规则"""
    processor = get_event_processor()
    processor.alert_processor.reload_rules()
    logger.info("告警规则已重新加载")


def get_alert_stats() -> Dict[str, Any]:
    """获取告警统计信息

    Returns:
        Dict: 告警统计信息
    """
    processor = get_event_processor()
    return processor.alert_processor.get_stats()


async def process_k8s_event_async(message_data: Dict[str, Any]) -> bool:
    """
    处理K8S事件消息的便捷函数（异步版本）

    使用asyncio.to_thread将同步操作转为异步，避免阻塞事件循环

    Args:
        message_data: 事件消息数据

    Returns:
        bool: 处理是否成功
    """
    import asyncio

    def _sync_process():
        processor = get_event_processor()
        return processor.process_event_message(message_data)

    try:
        # 使用线程池执行同步操作，避免阻塞事件循环
        result = await asyncio.to_thread(_sync_process)
        return result
    except Exception as e:
        logger.error(f"异步处理K8S事件失败: {e}")
        return False
