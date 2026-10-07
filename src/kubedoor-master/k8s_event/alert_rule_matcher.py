#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
K8S事件告警规则匹配引擎
支持对事件字段的包含/不包含判断以及count字段的数值比较
"""

import json
from typing import Dict, Any, Optional
from loguru import logger


class AlertRuleMatcher:
    """告警规则匹配器"""

    def __init__(self, rules_file: str = None):
        """初始化规则匹配器

        Args:
            rules_file: 规则文件路径，默认 k8s_event/rules/alert_rules.json（工作目录为 /）。
                镜像内不自带规则文件，由 ConfigMap kubedoor-master-file-cfg 挂载
                （源文件 deploy/manifests/master/alert_rules.json）。
        """
        if rules_file is None:
            rules_file = "k8s_event/rules/alert_rules.json"

        self.rules_file = rules_file
        self.rules = []
        self.global_ignore_rules = []
        self.load_rules()

    def load_rules(self) -> None:
        """加载告警规则"""
        try:
            with open(self.rules_file, 'r', encoding='utf-8') as f:
                config = json.load(f)
                self.rules = config.get('alert_rules', [])
                self.global_ignore_rules = config.get('global_ignore_rules', [])

            logger.info(f"加载了 {len(self.rules)} 条告警规则")

        except Exception as e:
            logger.error(f"加载告警规则失败(规则文件需由 ConfigMap kubedoor-master-file-cfg 挂载到 {self.rules_file}): {e}")
            self.rules = []
            self.global_config = {}

    def reload_rules(self) -> None:
        """重新加载规则"""
        self.load_rules()

    def should_ignore_event(self, event: Dict[str, Any]) -> bool:
        """检查事件是否应该被忽略

        Args:
            event: K8S事件数据

        Returns:
            bool: True表示应该忽略，False表示不应该忽略
        """
        # 检查全局忽略规则（按优先级顺序）
        for ignore_rule in self.global_ignore_rules:
            # 跳过禁用的规则
            if not ignore_rule.get('enabled', True):
                continue

            # 检查规则条件
            conditions = ignore_rule.get('conditions', {})
            if self._match_conditions(event, conditions):
                return True

        return False

    def match_alert_rules(self, event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """匹配告警规则

        Args:
            event: K8S事件数据

        Returns:
            Optional[Dict]: 匹配的规则信息，如果没有匹配则返回None
        """
        # 首先检查是否应该忽略
        if self.should_ignore_event(event):
            logger.debug(f"事件被忽略: {event.get('eventUid')}")
            return None

        # 按数组顺序匹配规则
        for rule in self.rules:
            if not rule.get('enabled', True):
                continue

            if self._match_rule(event, rule):
                logger.info(f"事件匹配规则: {rule.get('name')} - {event.get('eventUid')}")
                return {'rule': rule, 'event': event}

        logger.debug(f"事件未匹配任何规则: {event.get('eventUid')}")
        return None

    def _match_rule(self, event: Dict[str, Any], rule: Dict[str, Any]) -> bool:
        """检查事件是否匹配规则

        Args:
            event: K8S事件数据
            rule: 告警规则

        Returns:
            bool: 是否匹配
        """
        conditions = rule.get('conditions', {})
        return self._match_conditions(event, conditions)

    def _match_conditions(self, event: Dict[str, Any], conditions: Dict[str, Any]) -> bool:
        """检查事件是否匹配条件组

        Args:
            event: K8S事件数据
            conditions: 条件组

        Returns:
            bool: 是否匹配
        """
        for field_name, field_conditions in conditions.items():
            if not self._match_field_condition(event, field_name, field_conditions):
                return False
        return True

    def _match_field_condition(self, event: Dict[str, Any], field_name: str, field_conditions: Dict[str, Any]) -> bool:
        """检查单个字段条件

        Args:
            event: K8S事件数据
            field_name: 字段名
            field_conditions: 字段条件

        Returns:
            bool: 是否匹配
        """
        field_value = event.get(field_name)

        # 如果字段不存在，根据条件类型决定是否匹配
        if field_value is None:
            # 对于not_contains、not_starts_with和not_ends_with，字段不存在时认为匹配
            if (
                'not_contains' in field_conditions
                or 'not_starts_with' in field_conditions
                or 'not_ends_with' in field_conditions
            ):
                return True
            # 对于其他条件，字段不存在时不匹配
            return False

        # 转换为字符串进行匹配（除了数值比较）
        field_str = str(field_value)

        # contains条件：字段值包含任一指定值
        if 'contains' in field_conditions:
            contains_values = field_conditions['contains']
            if not isinstance(contains_values, list):
                contains_values = [contains_values]

            for value in contains_values:
                if str(value).lower() in field_str.lower():
                    return True
            return False

        # not_contains条件：字段值不包含任何指定值
        if 'not_contains' in field_conditions:
            not_contains_values = field_conditions['not_contains']
            if not isinstance(not_contains_values, list):
                not_contains_values = [not_contains_values]

            for value in not_contains_values:
                if str(value).lower() in field_str.lower():
                    return False
            return True

        # starts_with条件：字段值以指定值开头
        if 'starts_with' in field_conditions:
            starts_values = field_conditions['starts_with']
            if not isinstance(starts_values, list):
                starts_values = [starts_values]

            for value in starts_values:
                if field_str.lower().startswith(str(value).lower()):
                    return True
            return False

        # not_starts_with条件：字段值不以任何指定值开头
        if 'not_starts_with' in field_conditions:
            not_starts_values = field_conditions['not_starts_with']
            if not isinstance(not_starts_values, list):
                not_starts_values = [not_starts_values]

            for value in not_starts_values:
                if field_str.lower().startswith(str(value).lower()):
                    return False
            return True

        # ends_with条件：字段值以指定值结尾
        if 'ends_with' in field_conditions:
            ends_values = field_conditions['ends_with']
            if not isinstance(ends_values, list):
                ends_values = [ends_values]

            for value in ends_values:
                if field_str.lower().endswith(str(value).lower()):
                    return True
            return False

        # not_ends_with条件：字段值不以任何指定值结尾
        if 'not_ends_with' in field_conditions:
            not_ends_values = field_conditions['not_ends_with']
            if not isinstance(not_ends_values, list):
                not_ends_values = [not_ends_values]

            for value in not_ends_values:
                if field_str.lower().endswith(str(value).lower()):
                    return False
            return True

        # equals条件：字段值等于指定值
        if 'equals' in field_conditions:
            return field_str.lower() == str(field_conditions['equals']).lower()

        # not_equals条件：字段值不等于指定值
        if 'not_equals' in field_conditions:
            return field_str.lower() != str(field_conditions['not_equals']).lower()

        # 数值比较条件（仅对count字段有效）
        if field_name == 'count':
            try:
                field_num = int(field_value)

                if 'greater_than' in field_conditions:
                    return field_num > field_conditions['greater_than']

                if 'less_than' in field_conditions:
                    return field_num < field_conditions['less_than']

                if 'greater_equal' in field_conditions:
                    return field_num >= field_conditions['greater_equal']

                if 'less_equal' in field_conditions:
                    return field_num <= field_conditions['less_equal']

            except (ValueError, TypeError):
                logger.warning(f"无法将count字段转换为数值: {field_value}")
                return False

        # 如果没有匹配的条件类型，默认返回True
        return True

    def get_rule_stats(self) -> Dict[str, Any]:
        """获取规则统计信息

        Returns:
            Dict: 规则统计信息
        """
        enabled_rules = [r for r in self.rules if r.get('enabled', True)]
        disabled_rules = [r for r in self.rules if not r.get('enabled', True)]

        return {
            'total_rules': len(self.rules),
            'enabled_rules': len(enabled_rules),
            'disabled_rules': len(disabled_rules),
            'rules_file': str(self.rules_file),
        }
