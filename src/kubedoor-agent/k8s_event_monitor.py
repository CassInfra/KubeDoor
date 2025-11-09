#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
K8S事件实时监控模块
使用kubernetes_asyncio库异步监听K8S事件，并通过WebSocket推送到kubedoor-master
"""

import asyncio
import json
from datetime import datetime
from kubernetes_asyncio import client, watch
from kubernetes_asyncio.client.rest import ApiException
from loguru import logger
from utils import PROM_K8S_TAG_VALUE, MSG_TOKEN
from event_monitor_config import *


class K8sEventMonitor:
    """K8S事件监听器"""

    def __init__(self, core_v1_api):
        self.core_v1 = core_v1_api
        self.ws_conn = None
        self.monitor_task = None
        self.is_running = False
        self.last_event_time = None
        self.event_count = 0

    def set_websocket_connection(self, ws_conn):
        """设置WebSocket连接"""
        old_conn = self.ws_conn
        self.ws_conn = ws_conn
        if old_conn != ws_conn:
            if ws_conn:
                logger.info("✅ WebSocket连接已更新")
            else:
                logger.info("🔌 WebSocket连接已清空")

    def is_websocket_healthy(self):
        """检查WebSocket连接是否健康"""
        if not self.ws_conn:
            return False
        return not self.ws_conn.closed

    def format_event_data(self, event):
        """格式化事件数据为指定的JSON格式"""
        try:
            event_type = event['type']  # ADDED, MODIFIED, DELETED
            raw_object = event['raw_object']

            # 提取metadata信息
            metadata = raw_object.get('metadata', {})
            event_uid = metadata.get('uid', '')

            # 提取involvedObject信息
            involved_object = raw_object.get('involvedObject', {})
            kind = involved_object.get('kind', '')
            namespace = involved_object.get('namespace', '')
            name = involved_object.get('name', '')

            # 提取其他字段
            level = raw_object.get('type', '')  # Normal, Warning
            count = raw_object.get('count', 0)
            reason = raw_object.get('reason', '')
            message = raw_object.get('message', '')

            # 直接使用原始时间戳
            first_timestamp = raw_object.get('firstTimestamp')
            last_timestamp = raw_object.get('lastTimestamp')

            # 报告组件信息
            source = raw_object.get('source', {})
            reporting_component = source.get('component', '')
            reporting_instance = source.get('host', '')

            # 构造事件数据
            event_data = {
                "eventUid": event_uid,
                "eventStatus": event_type,
                "level": level,
                "count": count,
                "kind": kind,
                "k8s": PROM_K8S_TAG_VALUE,
                "namespace": namespace,
                "name": name,
                "reason": reason,
                "message": message,
                "firstTimestamp": first_timestamp,
                "lastTimestamp": last_timestamp,
                "reportingComponent": reporting_component,
                "reportingInstance": reporting_instance,
                "msgToken": MSG_TOKEN,
            }

            return event_data

        except Exception as e:
            logger.error(f"格式化事件数据失败: {e}")
            logger.debug(f"原始事件数据: {json.dumps(event, indent=2, ensure_ascii=False)}")
            return None

    async def send_event_to_master(self, event_data):
        """通过WebSocket发送事件数据到kubedoor-master"""
        if not self.is_websocket_healthy():
            logger.warning("WebSocket连接不健康，无法发送事件")
            return

        try:
            # 构造WebSocket消息
            ws_message = {"type": "k8s_event", "data": event_data, "timestamp": datetime.now().isoformat()}

            await self.ws_conn.send_json(ws_message)
            
            # 更新统计信息
            self.event_count += 1
            self.last_event_time = datetime.now()
            
            logger.debug(f"事件已发送 (#{self.event_count}): {event_data['kind']}/{event_data['name']} - {event_data['reason']}")

        except Exception as e:
            logger.error(f"发送事件到master失败: {e}")
            # 连接异常时清空连接引用
            self.ws_conn = None

    async def monitor_events(self, namespace=None):
        """监控K8S事件，带重连机制"""
        retry_count = 0
        max_retries = 5
        base_delay = 1  # 基础重试延迟（秒）
        
        while self.is_running and retry_count < max_retries:
            try:
                logger.info("🚀 开始监控K8S事件...")
                logger.info(f"📍 监控范围: {'所有命名空间' if not namespace else f'命名空间 {namespace}'}")
                
                if retry_count > 0:
                    logger.info(f"🔄 第 {retry_count} 次重试监控K8S事件")

                # 创建事件监听器
                w = watch.Watch()

                # 开始监听事件
                if namespace:
                    stream = w.stream(self.core_v1.list_namespaced_event, namespace=namespace)
                else:
                    stream = w.stream(self.core_v1.list_event_for_all_namespaces)

                # 重置重试计数器（成功建立连接）
                retry_count = 0

                async for event in stream:
                    if not self.is_running:
                        logger.info("事件监控已停止")
                        return

                    try:
                        # 格式化事件数据
                        event_data = self.format_event_data(event)

                        if event_data:
                            # 发送事件到master
                            await self.send_event_to_master(event_data)

                            # 记录事件日志
                            logger.debug(
                                f"📨 [{event_data['eventStatus']}] {event_data['level']} - "
                                f"{event_data['kind']}/{event_data['name']} - {event_data['reason']} - "
                                f"首次: {event_data['firstTimestamp']} 最后: {event_data['lastTimestamp']}"
                            )

                    except Exception as e:
                        logger.error(f"处理事件时出错: {e}")
                        continue

            except asyncio.CancelledError:
                logger.info("⏹️ 事件监控被取消")
                return
            except ApiException as e:
                retry_count += 1
                if retry_count >= K8S_EVENT_MAX_RETRIES:
                    logger.error(f"K8s API异常达到最大重试次数({K8S_EVENT_MAX_RETRIES}): {e}")
                    break
                
                delay = min(K8S_EVENT_RETRY_DELAY ** retry_count, 60)  # 指数退避，最大60秒
                logger.warning(f"K8s API异常，{delay}秒后重试 (第{retry_count}/{K8S_EVENT_MAX_RETRIES}次): {e}")
                await asyncio.sleep(delay)
                continue

            except Exception as e:
                retry_count += 1
                if retry_count >= K8S_EVENT_MAX_RETRIES:
                    logger.error(f"监控事件时发生异常达到最大重试次数({K8S_EVENT_MAX_RETRIES}): {e}")
                    break
                
                delay = min(K8S_EVENT_RETRY_DELAY ** retry_count, 60)  # 指数退避，最大60秒
                logger.warning(f"监控事件异常，{delay}秒后重试 (第{retry_count}/{K8S_EVENT_MAX_RETRIES}次): {e}")
                await asyncio.sleep(delay)
                continue
        
        self.is_running = False

    async def start_monitoring(self, namespace=None):
        """启动事件监控"""
        if self.is_running:
            logger.warning("事件监控已在运行中")
            return

        # 重置统计信息
        self.event_count = 0
        self.last_event_time = None
        
        self.is_running = True
        self.monitor_task = asyncio.create_task(self.monitor_events(namespace))
        logger.info(f"🎯 K8S事件监控已启动 (WebSocket健康: {self.is_websocket_healthy()})")

    async def stop_monitoring(self):
        """停止事件监控"""
        if not self.is_running:
            return

        self.is_running = False
        if self.monitor_task:
            self.monitor_task.cancel()
            try:
                await self.monitor_task
            except asyncio.CancelledError:
                pass
            self.monitor_task = None

        # 输出统计信息
        if self.event_count > 0:
            logger.info(f"🛑 K8S事件监控已停止 (共处理 {self.event_count} 个事件，最后事件时间: {self.last_event_time})")
        else:
            logger.info("🛑 K8S事件监控已停止 (未处理任何事件)")
