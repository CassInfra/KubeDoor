# -*- coding: utf-8 -*-
"""
事件监控配置文件
"""

# WebSocket连接配置
WEBSOCKET_RECONNECT_DELAY = 5  # WebSocket重连延迟（秒）
WEBSOCKET_MAX_RETRIES = 5      # WebSocket最大重试次数

# K8s事件监控配置
K8S_EVENT_STREAM_TIMEOUT = 300  # watch 的服务端超时（秒），到点正常断开后接着 watch，实际再加 0~60 秒随机
K8S_EVENT_RETRY_DELAY = 2       # list/watch 出错后的初始重试延迟（秒），指数退避，最大60秒

# 健康检查配置
HEALTH_CHECK_INTERVAL = 30      # 健康检查间隔（秒）
EVENT_TIMEOUT_THRESHOLD = 600   # K8s事件 watch 超过此时间（秒）没有任何活动（连上/收到事件/list 成功）则告警
STATS_REPORT_INTERVAL = 120     # 统计信息报告间隔（秒）

# 心跳配置
HEARTBEAT_INTERVAL = 5          # 心跳间隔（秒）
HEARTBEAT_ACK_TIMEOUT = 30      # master 回过 heartbeat_ack 后超过这么久没再回，判定连接失效并重连（旧版 master 不回 ack，不启用）

# 与 master 的 WebSocket 连接
WS_MAX_MSG_SIZE = 16 * 1024 * 1024  # 单条消息上限，与 master 一致；超限的回包改回错误，免得 master 断开整条连接
WS_PING_INTERVAL = 20           # aiohttp 协议层 ping：这么久没收到 master 的任何消息就 ping，半个间隔内没有 pong 判定连接失效
WS_SEND_TIMEOUT = 60            # 单条消息发送超时（秒）：master 不读或链路卡死时，超时判定连接失效
WS_CLOSE_TIMEOUT = 5            # 断开时等待关闭握手的上限（秒），超时直接断开底层连接

# 日志配置
LOG_EVENT_DETAILS = False       # 是否记录事件详细信息
LOG_WEBSOCKET_DETAILS = True    # 是否记录WebSocket详细信息