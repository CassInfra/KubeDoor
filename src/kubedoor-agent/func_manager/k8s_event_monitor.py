#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
K8S事件实时监控模块
分页 list + watch 全集群事件(逐行解析原始 JSON),通过WebSocket批量推送到kubedoor-master

- 不用 kubernetes_asyncio.watch.Watch:它对每个事件都要 loads/dumps 再反序列化成模型对象,
  遇到 410 还会拿同一个过期的 resourceVersion 重试一次(必然再 410)。
- events 默认不进 apiserver 的 watch cache,watch 直接打到 etcd,没有 BOOKMARK:集群安静超过
  etcd compaction 窗口(约 5~10 分钟)后,用旧 resourceVersion 续 watch 必然 410。
  410 时重新 list,和已经发给 master 的版本(eventUid → resourceVersion)比对,只补发新增/变化的事件,
  不再把集群里的事件全部重推一遍(master 的告警去重窗口只有几分钟,全量重推会重复告警)。
- watch 带服务端超时和 sock_read 超时,连接静默断掉也能发现。
- 每次连上 master 都会重新 list:断线期间漏掉的事件补发,master 已经有的不重发。
"""

import asyncio
import json
import random
import time
from datetime import datetime

from kubernetes_asyncio.client.rest import ApiException
from loguru import logger
from utils import PROM_K8S_TAG_VALUE, MSG_TOKEN
from func_manager.event_monitor_config import *

PAGE_SIZE = 500
YIELD_EVERY = 100  # 每处理多少个事件让出一次事件循环
MAX_BACKOFF = 60


class K8sEventMonitor:
    """K8S事件监听器，带背压控制"""

    def __init__(self, core_v1_api):
        self.core_v1 = core_v1_api
        self.ws_conn = None
        self.monitor_task = None
        self.batch_sender_task = None
        self.is_running = False
        self.last_event_time = None
        self.last_alive_time = None  # watch 最近一次确认连通的时间(连上 / 收到任何一行 / list 成功)
        self.event_count = 0
        # 事件队列，用于背压控制
        self.event_queue = asyncio.Queue(maxsize=1000)
        self._rv = None  # 下次 watch 的起点,None 表示先 list
        self._sent = {}  # eventUid -> 已发给 master 的 resourceVersion,跨 master 重连保留

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

    async def _forward(self, event_type, raw_object):
        """格式化后放进发送队列;队列满时等发送任务腾出位置(背压),不丢事件"""
        event_data = self.format_event_data({"type": event_type, "raw_object": raw_object})
        if not event_data:
            return
        resource_version = (raw_object.get("metadata") or {}).get("resourceVersion")
        await self.event_queue.put((event_data, resource_version))
        self.last_event_time = datetime.now()
        logger.debug(
            f"📨 [{event_data['eventStatus']}] {event_data['level']} - "
            f"{event_data['kind']}/{event_data['name']} - {event_data['reason']} - "
            f"首次: {event_data['firstTimestamp']} 最后: {event_data['lastTimestamp']}"
        )

    def _mark_sent(self, batch):
        for event_data, resource_version in batch:
            uid = event_data.get("eventUid")
            if not uid:
                continue
            if event_data.get("eventStatus") == "DELETED":
                self._sent.pop(uid, None)
            elif resource_version:
                self._sent[uid] = resource_version

    async def _batch_send_events(self):
        """批量发送事件到 master，实现背压控制"""
        batch = []
        batch_size = 10
        batch_timeout = 1.0  # 1秒超时

        while self.is_running:
            try:
                # 等待事件或超时
                try:
                    batch.append(await asyncio.wait_for(self.event_queue.get(), timeout=batch_timeout))
                except asyncio.TimeoutError:
                    pass

                # 达到批量大小或有数据且队列为空时发送
                if len(batch) >= batch_size or (batch and self.event_queue.empty()):
                    if self.is_websocket_healthy():
                        try:
                            ws_message = {
                                "type": "k8s_event_batch",
                                "data": [event_data for event_data, _ in batch],
                                "timestamp": datetime.now().isoformat(),
                            }
                            await self.ws_conn.send_json(ws_message)
                            self.event_count += len(batch)
                            self._mark_sent(batch)
                            logger.debug(f"批量发送 {len(batch)} 个事件")
                        except Exception as e:
                            logger.error(f"批量发送事件失败: {e}")
                            self.ws_conn = None
                    # 没发出去的不记进 _sent,下次重新 list 时会补发
                    batch = []

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"批量发送任务异常: {e}")
                await asyncio.sleep(1)

    async def _relist(self):
        """分页 list 全部事件,补发 master 还没有的版本,watch 起点设成 list 的 resourceVersion"""
        listed = set()
        forwarded = 0
        cont = None
        while True:
            kwargs = {"limit": PAGE_SIZE, "_preload_content": False, "_request_timeout": 60}
            if cont:
                kwargs["_continue"] = cont
            # _preload_content=False 时 kubernetes_asyncio 不会对非 2xx 抛异常,要自己判断
            resp = await self.core_v1.list_event_for_all_namespaces(**kwargs)
            try:
                body = await resp.read()
                if resp.status != 200:
                    raise ApiException(status=resp.status, reason=body[:300].decode("utf-8", "replace"))
            finally:
                resp.release()
            data = json.loads(body)
            for obj in data.get("items") or []:
                item_meta = obj.get("metadata") or {}
                uid = item_meta.get("uid")
                if not uid:
                    continue
                listed.add(uid)
                sent_version = self._sent.get(uid)
                if sent_version != item_meta.get("resourceVersion"):
                    await self._forward("ADDED" if sent_version is None else "MODIFIED", obj)
                    forwarded += 1
            meta = data.get("metadata") or {}
            cont = meta.get("continue")
            if not cont:
                break
            await asyncio.sleep(0)
        # 已经过期删除的事件不用再记
        for uid in [uid for uid in self._sent if uid not in listed]:
            del self._sent[uid]
        self._rv = meta.get("resourceVersion")
        self.last_alive_time = datetime.now()
        log = logger.info if forwarded else logger.debug
        log(f"📋 K8S事件 list 完成: 共 {len(listed)} 条，补发 {forwarded} 条")

    async def _watch(self):
        """从 self._rv 开始 watch,服务端到点正常断开时返回(外层循环接着 watch)"""
        timeout = K8S_EVENT_STREAM_TIMEOUT + random.randint(0, 60)
        resp = await self.core_v1.list_event_for_all_namespaces(
            watch=True,
            resource_version=self._rv,
            allow_watch_bookmarks=True,
            timeout_seconds=timeout,
            _preload_content=False,
            # sock_read 比服务端超时多 30 秒:连接静默断掉时也能发现
            _request_timeout=(10, timeout + 30),
        )
        try:
            if resp.status != 200:
                body = await resp.read()
                raise ApiException(status=resp.status, reason=body[:300].decode("utf-8", "replace"))
            self.last_alive_time = datetime.now()
            handled = 0
            while True:
                line = await resp.content.readline()
                if not line:
                    return
                self.last_alive_time = datetime.now()
                event = json.loads(line)
                event_type = event.get("type")
                obj = event.get("object") or {}
                if event_type == "ERROR":
                    raise ApiException(status=obj.get("code"), reason=f"{obj.get('reason')}: {obj.get('message')}")
                if event_type in ("ADDED", "MODIFIED", "DELETED"):
                    await self._forward(event_type, obj)
                elif event_type != "BOOKMARK":
                    continue
                resource_version = (obj.get("metadata") or {}).get("resourceVersion")
                if resource_version:
                    self._rv = resource_version
                handled += 1
                if handled % YIELD_EVERY == 0:
                    await asyncio.sleep(0)
        finally:
            resp.release()

    async def monitor_events(self):
        """list + watch 主循环:正常断开从最后的 resourceVersion 续上,410 重新 list,其它异常退避后重试"""
        backoff = K8S_EVENT_RETRY_DELAY
        try:
            while self.is_running:
                relisted = False  # 这一轮的 watch 是不是从刚 list 出来的 resourceVersion 开始
                try:
                    if self._rv is None:
                        relisted = True
                        await self._relist()
                    started = time.monotonic()
                    await self._watch()
                    if time.monotonic() - started >= 1:
                        backoff = K8S_EVENT_RETRY_DELAY
                        continue
                    # 刚连上就被断开(apiserver / 代理异常),退避一下,别空转
                    logger.warning(f"K8S事件 watch 连上即被断开，{backoff}秒后重试")
                except asyncio.CancelledError:
                    raise
                except ApiException as e:
                    if e.status == 410:
                        self._rv = None
                        if not relisted:
                            # resourceVersion 已被 etcd 压缩(集群安静太久),立即重新 list 补齐
                            logger.debug(f"K8S事件 resourceVersion 已过期，重新 list: {e.reason}")
                            continue
                    logger.warning(f"K8S事件 list/watch 失败({e.status} {e.reason})，{backoff}秒后重试")
                except Exception as e:
                    logger.warning(f"K8S事件 list/watch 异常({type(e).__name__}: {e})，{backoff}秒后重试")
                await asyncio.sleep(backoff + random.random())
                backoff = min(backoff * 2, MAX_BACKOFF)
        finally:
            self.is_running = False

    async def start_monitoring(self):
        """启动事件监控(每次连上 master 调用):先 list 补发断线期间漏掉的事件,再接着 watch"""
        if self.is_running:
            logger.warning("事件监控已在运行中")
            return

        # 重置统计信息
        self.event_count = 0
        self.last_event_time = None
        self.last_alive_time = datetime.now()
        # 上个连接没发出去的事件不在 _sent 里,list 时会补发
        self.event_queue = asyncio.Queue(maxsize=1000)
        self._rv = None

        self.is_running = True
        # 启动监控任务和批量发送任务
        self.monitor_task = asyncio.create_task(self.monitor_events())
        self.batch_sender_task = asyncio.create_task(self._batch_send_events())
        logger.info(f"🎯 K8S事件监控已启动 (WebSocket健康: {self.is_websocket_healthy()})")

    async def stop_monitoring(self):
        """停止事件监控"""
        if not self.is_running:
            return

        self.is_running = False

        # 停止监控任务
        if self.monitor_task:
            self.monitor_task.cancel()
            try:
                await self.monitor_task
            except asyncio.CancelledError:
                pass
            self.monitor_task = None

        # 停止批量发送任务
        if self.batch_sender_task:
            self.batch_sender_task.cancel()
            try:
                await self.batch_sender_task
            except asyncio.CancelledError:
                pass
            self.batch_sender_task = None

        # 输出统计信息
        if self.event_count > 0:
            logger.info(
                f"🛑 K8S事件监控已停止 (共处理 {self.event_count} 个事件，最后事件时间: {self.last_event_time})"
            )
        else:
            logger.info("🛑 K8S事件监控已停止 (未处理任何事件)")
