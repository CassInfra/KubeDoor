#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
K8S事件告警处理器
集成规则匹配引擎到事件处理流程中

- 规则:每个事件读一次 rules_store 的当前快照(页面保存后立即生效),处理器本身不用重建,
  所以去重记录和统计在保存规则后照常保留
- 去重窗口:通用配置「事件告警去重窗口」(alert_dedup_window,秒,0 表示不去重),每次用时取,改了马上生效
- 事件在线程池里并发处理(process_k8s_event_async),去重缓存的检查和记录在同一把锁里完成,
  同一个事件同时来两次也只发一条
"""

import threading
import time
from typing import Dict, List, Any, Optional
from datetime import datetime
from loguru import logger

import general_settings

from .alert_rule_matcher import AlertRuleMatcher
from .pg_event_client import get_pg_event_client
from notify import notify

DEFAULT_DEDUP_WINDOW = 300  # 秒:读不到通用配置时用
_window_warned = False


def dedup_window() -> int:
    """告警去重窗口(秒):通用配置 alert_dedup_window,每次用时取;取不到时用 300 并记一次 warning"""
    global _window_warned
    try:
        return max(int(general_settings.value('alert_dedup_window')), 0)
    except Exception as exc:
        if not _window_warned:
            _window_warned = True
            logger.warning(f'读取通用配置「事件告警去重窗口」失败,按 {DEFAULT_DEDUP_WINDOW} 秒处理:{type(exc).__name__}: {exc}')
        return DEFAULT_DEDUP_WINDOW


def _new_stats() -> Dict[str, int]:
    return {
        'total_events': 0,
        'ignored_events': 0,
        'matched_events': 0,
        'alerts_sent': 0,
        'errors': 0,
        'dedup_blocked': 0,
    }


class EventAlertProcessor:
    """事件告警处理器"""

    def __init__(self, rule_matcher: Optional[AlertRuleMatcher] = None):
        """初始化事件告警处理器

        Args:
            rule_matcher: 规则匹配器;不传时用读当前规则快照的匹配器
        """
        self.rule_matcher = rule_matcher or AlertRuleMatcher()
        self.stats = _new_stats()
        # 告警去重缓存: {eventUid: last_alert_timestamp},读写都在 _cache_lock 里
        self._alert_cache = {}
        self._cache_lock = threading.Lock()

    def process_event(self, event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """处理单个事件

        Args:
            event: K8S事件数据(k8s 字段是集群名,按它选择消息通知的群机器人)

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

            # 先看忽略规则,再按顺序匹配告警规则(同一份规则快照)
            ignored, alert_result = self.rule_matcher.evaluate(event)
            if ignored:
                self.stats['ignored_events'] += 1
                return None

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

                # 检查告警去重:窗口内没发过就记下这次(检查和记录是一步,并发处理同一事件也只发一条)
                window = dedup_window()
                claimed, last_alert_time = self._claim_alert(event_uid, window)
                if not claimed:
                    self.stats['dedup_blocked'] += 1
                    # 构建资源信息
                    kind = event.get('kind', 'Unknown')
                    name = event.get('name', 'Unknown')
                    reason = event.get('reason', 'Unknown')
                    count = event.get('count', 0)
                    k8s_cluster = event.get('k8s', 'Unknown')
                    resource_info = f"{kind}/{name}"

                    last_alert_str = "未知"
                    if last_alert_time:
                        last_alert_datetime = datetime.fromtimestamp(last_alert_time)
                        last_alert_str = last_alert_datetime.strftime('%Y-%m-%d %H:%M:%S')

                    logger.warning(
                        f"💤告警被去重阻止(上次告警: {last_alert_str} 时间窗口:{window}秒) - 集群: {k8s_cluster} 资源: {resource_info} 原因: {reason}：{count}次"
                    )
                    return None

                # 构建告警信息(出错时撤销这次去重记录,事件下次更新时再试)
                try:
                    alert_info = self._build_alert_info(alert_result)
                except Exception:
                    self._release_alert(event_uid, last_alert_time)
                    raise

                # 发送告警:按事件所在集群选择群机器人(集群单独配置优先,其次全局配置),
                # 只放进发送队列(按机器人限速),不等结果;发送失败由 notify 记日志
                try:
                    if notify(alert_info['message'], env=event.get('k8s')):
                        self.stats['alerts_sent'] += 1
                        logger.info(f"告警已提交发送: {alert_info['alert_id']}")
                except Exception as e:
                    # 没提交出去:撤销这次去重记录,事件下次更新时再试
                    self._release_alert(event_uid, last_alert_time)
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
        # 从旧规则文件原样导入的规则 severity 可能不是字符串:按 warning 显示
        severity = rule.get('severity', 'warning')
        if not isinstance(severity, str) or not severity:
            severity = 'warning'

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
        self.stats = _new_stats()
        logger.info("统计信息已重置")

    def _claim_alert(self, event_uid: str, window: int):
        """去重:窗口内没发过就记下这次并返回 (True, 上次时间);发过返回 (False, 上次时间)。
        没有 eventUid 的事件不去重"""
        if not event_uid:
            return True, None
        current_time = time.time()
        with self._cache_lock:
            # 懒清理：检查时顺便清理过期的缓存项
            self._lazy_cleanup_cache(current_time, window)
            last_alert_time = self._alert_cache.get(event_uid)
            if last_alert_time and (current_time - last_alert_time) < window:
                return False, last_alert_time
            self._alert_cache[event_uid] = current_time
            return True, last_alert_time

    def _release_alert(self, event_uid: str, previous: Optional[float]) -> None:
        """撤销 _claim_alert 记下的这次(恢复成之前的记录)"""
        if not event_uid:
            return
        with self._cache_lock:
            if previous is None:
                self._alert_cache.pop(event_uid, None)
            else:
                self._alert_cache[event_uid] = previous

    def _lazy_cleanup_cache(self, current_time: float, window: int) -> None:
        """懒清理过期的缓存项(调用方持有 _cache_lock)

        Args:
            current_time: 当前时间戳
            window: 去重窗口(秒)
        """
        # 只有当缓存项超过一定数量时才进行清理，避免频繁清理
        if len(self._alert_cache) < 100:
            return

        # 清理过期的缓存项
        expired_keys = [key for key, timestamp in self._alert_cache.items() if (current_time - timestamp) >= window]

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
        window = dedup_window()
        with self._cache_lock:
            total = len(self._alert_cache)
            active_count = sum(1 for timestamp in self._alert_cache.values() if (current_time - timestamp) < window)

        return {
            'total_cache_items': total,
            'active_cache_items': active_count,
            'dedup_window_seconds': window,
            'dedup_blocked_count': self.stats.get('dedup_blocked', 0),
        }
