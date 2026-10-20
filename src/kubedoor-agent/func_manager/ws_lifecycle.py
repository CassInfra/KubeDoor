"""Supervise the long-running tasks belonging to one master connection."""

import asyncio
import json
import time

from loguru import logger


async def run_connection_tasks(ws, monitor, process_request, heartbeat, health_check):
    tasks = []
    try:
        # start_monitoring launches its own workers and returns immediately.
        await monitor.start_monitoring()
        tasks = [asyncio.create_task(process_request(ws)), asyncio.create_task(heartbeat(ws)),
                 asyncio.create_task(health_check())]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if not task.cancelled() and task.exception():
                raise task.exception()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await monitor.stop_monitoring()


class MasterConnection:
    """到 master 的 WebSocket(包一层 aiohttp 的 ClientWebSocketResponse),agent 各模块都通过它收发

    以前连接"假死"时 agent 发现不了(只看 ws.closed),所以这里补了几道判断:
    - 每次发送都有超时:master 不读(接收循环卡住)或链路卡死时,发送方不会永远卡在 drain 上;
      超时一次就判定连接失效,之后的发送立即失败,心跳任务发现后断开重连;
    - master 每收到一次心跳回一个 heartbeat_ack:回过 ack 之后超过 ack_timeout 秒没再回,
      说明 master 没在处理这条连接上的消息,同样判定失效。旧版 master 不回 ack,不启用这项判断;
    - 关闭有时间上限:对端不读时 aiohttp 发 close 帧可能一直卡在 drain 上,超时后直接断开底层连接。
    """

    def __init__(self, ws, send_timeout, ack_timeout):
        self.ws = ws
        self.send_timeout = send_timeout
        self.ack_timeout = ack_timeout
        self.broken = None  # 判定连接失效的原因
        self.last_ack = None  # 最近一次收到 heartbeat_ack 的时间(monotonic)

    @property
    def closed(self):
        return self.broken is not None or self.ws.closed

    def __aiter__(self):
        return self.ws.__aiter__()

    async def send_str(self, data):
        if self.closed:
            raise ConnectionResetError(f"与 master 的连接已断开{f'({self.broken})' if self.broken else ''}")
        try:
            await asyncio.wait_for(self.ws.send_str(data), self.send_timeout)
        except asyncio.TimeoutError:
            self.mark_broken(f"发送超过 {self.send_timeout} 秒仍未完成")
            raise ConnectionResetError("发送到 master 超时") from None

    async def send_json(self, data):
        await self.send_str(json.dumps(data))

    def mark_broken(self, reason):
        if self.broken is None:
            self.broken = reason
            logger.error(f"与 master 的连接已失效({reason}),断开重连")

    def on_ack(self):
        self.last_ack = time.monotonic()

    def check_alive(self):
        """心跳任务每次发心跳前调用:连接已失效时抛 ConnectionResetError,调用方据此断开重连"""
        if self.last_ack is not None and time.monotonic() - self.last_ack > self.ack_timeout:
            self.mark_broken(f"master 已超过 {self.ack_timeout} 秒没有回应心跳")
        if self.broken is not None:
            raise ConnectionResetError(self.broken)

    async def close(self, timeout):
        try:
            await asyncio.wait_for(self.ws.close(), timeout)
        except Exception:
            # 超时被取消时 aiohttp 会直接关掉底层连接(_response.close()),这里不用再管
            pass
