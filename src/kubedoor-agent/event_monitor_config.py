# -*- coding: utf-8 -*-
"""
事件监控配置文件
"""

# WebSocket连接配置
WEBSOCKET_RECONNECT_DELAY = 5  # WebSocket重连延迟（秒）
WEBSOCKET_MAX_RETRIES = 5      # WebSocket最大重试次数

# K8s事件监控配置
K8S_EVENT_STREAM_TIMEOUT = 300  # K8s事件流超时时间（秒）
K8S_EVENT_RETRY_DELAY = 2       # K8s事件重试延迟（秒）
K8S_EVENT_MAX_RETRIES = 5       # K8s事件最大重试次数

# 健康检查配置
HEALTH_CHECK_INTERVAL = 30      # 健康检查间隔（秒）
EVENT_TIMEOUT_THRESHOLD = 300   # 事件超时阈值（秒）- 超过此时间没有事件则认为异常
STATS_REPORT_INTERVAL = 120     # 统计信息报告间隔（秒）

# 心跳配置
HEARTBEAT_INTERVAL = 5          # 心跳间隔（秒）

# 日志配置
LOG_EVENT_DETAILS = False       # 是否记录事件详细信息
LOG_WEBSOCKET_DETAILS = True    # 是否记录WebSocket详细信息