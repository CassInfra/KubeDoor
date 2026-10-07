#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
K8S事件处理模块
提供 K8S 事件数据在 PostgreSQL 中的存储与查询能力
"""

from .pg_event_client import (
    PgEventClient,
    get_pg_event_client,
    init_pg_tables,
    init_pg_tables_async,
)
from .event_processor import (
    K8SEventProcessor,
    get_event_processor,
    process_k8s_event_async,
    reload_alert_rules,
    get_alert_stats
)
from .alert_rule_matcher import AlertRuleMatcher
from .event_alert_processor import EventAlertProcessor

__all__ = [
    'PgEventClient',
    'get_pg_event_client',
    'init_pg_tables',
    'init_pg_tables_async',
    'K8SEventProcessor',
    'get_event_processor',
    'process_k8s_event_async',
    'reload_alert_rules',
    'get_alert_stats',
    'AlertRuleMatcher',
    'EventAlertProcessor',
]

__version__ = '1.0.0'
