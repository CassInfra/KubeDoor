#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
K8S事件告警处理器
集成规则匹配引擎到事件处理流程中
"""

import time
from typing import Dict, List, Any, Optional
from datetime import datetime
from loguru import logger
from .alert_rule_matcher import AlertRuleMatcher
from .pg_event_client import get_pg_event_client
from utils import send_msg, ALERT_DEDUP_WINDOW


class EventAlertProcessor:
    """事件告警处理器"""

    def __init__(self, rules_file: str = None):
        """初始化事件告警处理器

        Args:
            rules_file: 规则文件路径
        """
        self.rule_matcher = AlertRuleMatcher(rules_file)
        self.stats = {
            'total_events': 0,
            'ignored_events': 0,
            'matched_events': 0,
            'alerts_sent': 0,
            'errors': 0,
            'dedup_blocked': 0,
        }
        # 告警去重缓存: {eventUid: last_alert_timestamp}
        self._alert_cache = {}
        self._dedup_window = ALERT_DEDUP_WINDOW

    def process_event(self, event: Dict[str, Any], msg_token: str = None) -> Optional[Dict[str, Any]]:
        """处理单个事件

        Args:
            event: K8S事件数据
            msg_token: 消息令牌，用于发送告警

        Returns:
            Optional[Dict]: 如果触发告警则返回告警信息，否则返回None
        """
        try:
            self.stats['total_events'] += 1

            # 硬编码忽略 eventStatus=DELETED 的事件，不做任何告警处理
            if event.get('eventStatus') == 'DELETED':
                self.stats['ignored_events'] += 1
                logger.debug(f"DELETED事件被直接忽略: {event.get('eventUid')}")
                return None

            # 检查是否应该忽略事件
            if self.rule_matcher.should_ignore_event(event):
                self.stats['ignored_events'] += 1
                logger.debug(f"事件被忽略: {event.get('eventUid')}")
                return None

            # 匹配告警规则
            alert_result = self.rule_matcher.match_alert_rules(event)

            if alert_result:
                self.stats['matched_events'] += 1
                event_uid = event.get('eventUid')
                # 更新数据库中对应eventUid的level字段为"已告警"
                try:
                    pg_client = get_pg_event_client()
                    # PG 标准 UPDATE(替代原 CK ALTER TABLE UPDATE)
                    update_sql = "UPDATE k8s_events SET level = '已告警' WHERE eventuid = $1"
                    pg_client.pool.execute_command(update_sql, [event_uid])
                    logger.info(f"已更新eventUid {event_uid} 的level字段为'已告警'")
                except Exception as update_e:
                    logger.error(f"更新level字段失败: {update_e}")

                # 检查告警去重
                if self._should_skip_alert(event_uid):
                    self.stats['dedup_blocked'] += 1
                    # 构建资源信息
                    kind = event.get('kind', 'Unknown')
                    name = event.get('name', 'Unknown')
                    reason = event.get('reason', 'Unknown')
                    count = event.get('count', 0)
                    k8s_cluster = event.get('k8s', 'Unknown')
                    resource_info = f"{kind}/{name}"

                    # 获取上次告警时间
                    last_alert_time = self._alert_cache.get(event_uid)
                    last_alert_str = "未知"
                    if last_alert_time:
                        last_alert_datetime = datetime.fromtimestamp(last_alert_time)
                        last_alert_str = last_alert_datetime.strftime('%Y-%m-%d %H:%M:%S')

                    logger.warning(
                        f"💤告警被去重阻止(上次告警: {last_alert_str} 时间窗口:{self._dedup_window}秒) - 集群: {k8s_cluster} 资源: {resource_info} 原因: {reason}：{count}次"
                    )
                    return None

                # 构建告警信息
                alert_info = self._build_alert_info(alert_result)

                # 发送告警
                try:
                    # 直接使用kubedoor的send_msg函数发送告警
                    response = send_msg(alert_info['message'], msg_token)
                    self.stats['alerts_sent'] += 1

                    # 记录告警时间用于去重
                    self._record_alert(event_uid)

                    logger.info(f"告警已发送: {alert_info['alert_id']}, 响应: {response}")
                except Exception as e:
                    logger.error(f"发送告警失败: {e}")
                    self.stats['errors'] += 1

                return alert_info

            return None

        except Exception as e:
            self.stats['errors'] += 1
            logger.error(f"处理事件失败: {e}")
            return None

    def process_events_batch(self, events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """批量处理事件

        Args:
            events: K8S事件数据列表

        Returns:
            List[Dict]: 触发的告警信息列表
        """
        alerts = []

        for event in events:
            alert_info = self.process_event(event)
            if alert_info:
                alerts.append(alert_info)

        logger.info(f"批量处理完成: 处理 {len(events)} 个事件，触发 {len(alerts)} 个告警")
        return alerts

    def _build_alert_info(self, alert_result: Dict[str, Any]) -> Dict[str, Any]:
        """构建告警信息

        Args:
            alert_result: 规则匹配结果

        Returns:
            Dict: 告警信息
        """
        rule = alert_result['rule']
        event = alert_result['event']
        severity = rule.get('severity', 'warning')

        # 根据严重级别选择emoji
        emoji_map = {'critical': '🚨', 'warning': '⚠️', 'info': 'ℹ️'}
        emoji = emoji_map.get(severity, '⚠️')

        # 构建固定格式的告警消息
        alert_message = f"""
{emoji} K8S事件告警: {rule.get('name', 'Unknown')}
🔥 级别: {severity.upper()}
⏰ 时间: {event.get('firstTimestamp', 'Unknown')}~~{event.get('lastTimestamp', 'Unknown')}

🎯 事件详情:
• 集群: {event.get('k8s', 'Unknown')}【{event.get('namespace', 'Unknown')}】
• 资源: {event.get('kind', 'Unknown')}/{event.get('name', 'Unknown')}
• 原因: {event.get('reason', 'Unknown')}：{event.get('count', 0)}次
• 消息: {event.get('message', 'Unknown')}
• 来源: {event.get('reportingComponent', 'Unknown')}/{event.get('reportingInstance', 'Unknown')}
"""

        alert_info = {
            'alert_id': f"{rule.get('name', 'unknown')}_{event.get('eventUid', 'unknown')}_{int(datetime.now().timestamp())}",
            'message': alert_message,
        }

        return alert_info

    def reload_rules(self) -> None:
        """重新加载规则"""
        self.rule_matcher.reload_rules()
        logger.info("告警规则已重新加载")

    def get_stats(self) -> Dict[str, Any]:
        """获取处理统计信息

        Returns:
            Dict: 统计信息
        """
        rule_stats = self.rule_matcher.get_rule_stats()

        return {
            'processor_stats': self.stats.copy(),
            'rule_stats': rule_stats,
            'alert_rate': self.stats['matched_events'] / max(self.stats['total_events'], 1) * 100,
        }

    def reset_stats(self) -> None:
        """重置统计信息"""
        self.stats = {
            'total_events': 0,
            'ignored_events': 0,
            'matched_events': 0,
            'alerts_sent': 0,
            'errors': 0,
            'dedup_blocked': 0,
        }
        logger.info("统计信息已重置")

    def _should_skip_alert(self, event_uid: str) -> bool:
        """检查是否应该跳过告警（去重检查）

        Args:
            event_uid: 事件唯一标识

        Returns:
            bool: True表示应该跳过告警，False表示可以发送告警
        """
        if not event_uid:
            return False

        current_time = time.time()

        # 懒清理：检查时顺便清理过期的缓存项
        self._lazy_cleanup_cache(current_time)

        # 检查是否在去重时间窗口内
        last_alert_time = self._alert_cache.get(event_uid)
        if last_alert_time and (current_time - last_alert_time) < self._dedup_window:
            return True

        return False

    def _record_alert(self, event_uid: str) -> None:
        """记录告警时间

        Args:
            event_uid: 事件唯一标识
        """
        if event_uid:
            self._alert_cache[event_uid] = time.time()

    def _lazy_cleanup_cache(self, current_time: float) -> None:
        """懒清理过期的缓存项

        Args:
            current_time: 当前时间戳
        """
        # 只有当缓存项超过一定数量时才进行清理，避免频繁清理
        if len(self._alert_cache) < 100:
            return

        # 清理过期的缓存项
        expired_keys = [
            key for key, timestamp in self._alert_cache.items() if (current_time - timestamp) >= self._dedup_window
        ]

        for key in expired_keys:
            del self._alert_cache[key]

        if expired_keys:
            logger.debug(f"清理了 {len(expired_keys)} 个过期的告警去重缓存项")

    def get_dedup_cache_info(self) -> Dict[str, Any]:
        """获取去重缓存信息

        Returns:
            Dict: 缓存信息统计
        """
        current_time = time.time()
        active_count = sum(
            1 for timestamp in self._alert_cache.values() if (current_time - timestamp) < self._dedup_window
        )

        return {
            'total_cache_items': len(self._alert_cache),
            'active_cache_items': active_count,
            'dedup_window_seconds': self._dedup_window,
            'dedup_blocked_count': self.stats.get('dedup_blocked', 0),
        }
