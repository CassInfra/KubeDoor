#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
K8S 事件告警规则匹配引擎

规则来自 rules_store 的只读快照(库里保存的规则;库里没有时是内置默认规则),每个事件只取一次快照,
页面保存后下一个事件就用新规则。匹配语义与以前读规则文件时完全一致(tests/test_event_rules_matcher.py
拿旧实现逐项对照):
- 忽略规则之间是 OR:任一启用的忽略规则命中即忽略;告警规则按顺序,第一条启用且命中的生效;不写 enabled 视为启用
- 一条规则里各字段的条件是 AND
- 一个字段写了多个运算符时只认第一个(顺序见 rules_schema.OPERATORS);不认识的运算符视为成立;
  数值运算符只对 count 有效;字符串比较不区分大小写
- 事件里没有这个字段时:写了 not_contains / not_starts_with / not_ends_with 的视为成立,其它不成立
页面保存时会做严格校验(rules_schema),上面这些「宽松」的情况只会出现在从旧规则文件原样导入的规则里。
"""

from collections.abc import Mapping
from typing import Any, Dict, Optional, Tuple

from loguru import logger

from . import rules_store

_NEGATIVE_OPERATORS = ('not_contains', 'not_starts_with', 'not_ends_with')
_EMPTY = rules_store.freeze({})


def _thaw(value):
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _text(value):
    """条件值转成比较用的小写字符串。快照里的数组是 tuple、对象是 MappingProxyType,先还原成 list / dict,
    保证与以前直接读 JSON 时 str() 的结果一样"""
    if isinstance(value, (tuple, Mapping)):
        value = _thaw(value)
    return str(value).lower()


def _values(raw):
    """列表型运算符的值;写成单个值时等同于只有一个元素的数组"""
    return raw if isinstance(raw, (list, tuple)) else (raw,)


def match_field_condition(event: Dict[str, Any], field_name: str, field_conditions) -> bool:
    """单个字段的条件是否成立(语义见模块说明)"""
    if not isinstance(field_conditions, Mapping):
        return False
    field_value = event.get(field_name)
    if field_value is None:
        return any(op in field_conditions for op in _NEGATIVE_OPERATORS)

    # 转换为字符串进行匹配(除了数值比较)
    field_str = str(field_value).lower()
    # contains:字段值包含任一指定值
    if 'contains' in field_conditions:
        return any(_text(value) in field_str for value in _values(field_conditions['contains']))
    # not_contains:字段值不包含任何指定值
    if 'not_contains' in field_conditions:
        return not any(_text(value) in field_str for value in _values(field_conditions['not_contains']))
    # starts_with:字段值以任一指定值开头
    if 'starts_with' in field_conditions:
        return any(field_str.startswith(_text(value)) for value in _values(field_conditions['starts_with']))
    # not_starts_with:字段值不以任何指定值开头
    if 'not_starts_with' in field_conditions:
        return not any(field_str.startswith(_text(value)) for value in _values(field_conditions['not_starts_with']))
    # ends_with:字段值以任一指定值结尾
    if 'ends_with' in field_conditions:
        return any(field_str.endswith(_text(value)) for value in _values(field_conditions['ends_with']))
    # not_ends_with:字段值不以任何指定值结尾
    if 'not_ends_with' in field_conditions:
        return not any(field_str.endswith(_text(value)) for value in _values(field_conditions['not_ends_with']))
    if 'equals' in field_conditions:
        return field_str == _text(field_conditions['equals'])
    if 'not_equals' in field_conditions:
        return field_str != _text(field_conditions['not_equals'])

    # 数值比较(仅对 count 字段有效)
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

    # 没有认识的运算符:视为成立
    return True


def match_conditions(event: Dict[str, Any], conditions) -> bool:
    """一条规则的所有字段条件都成立(AND);没有条件时成立"""
    if not isinstance(conditions, Mapping):
        return False
    return all(match_field_condition(event, field, spec) for field, spec in conditions.items())


def rule_enabled(rule) -> bool:
    return bool(rule.get('enabled', True))


def rule_matches(rule, event: Dict[str, Any]) -> bool:
    """启用的规则命中这个事件(不看是否启用请直接用 match_conditions)"""
    return rule_enabled(rule) and match_conditions(event, rule.get('conditions', _EMPTY))


def first_match(rules, event: Dict[str, Any]) -> Optional[int]:
    """按顺序第一条启用且命中的规则的下标;没有返回 None。纯函数,不记日志(试运行会对上万个事件调用)"""
    for index, rule in enumerate(rules):
        if rule_matches(rule, event):
            return index
    return None


class AlertRuleMatcher:
    """告警规则匹配器。

    不传 ruleset 时每次匹配读 rules_store 的当前快照(页面保存后立即生效);传了就固定用这一份(试运行、测试)。
    """

    def __init__(self, ruleset: Optional[rules_store.RuleSet] = None):
        self._ruleset = ruleset

    @property
    def ruleset(self) -> rules_store.RuleSet:
        return self._ruleset if self._ruleset is not None else rules_store.current()

    def evaluate(self, event: Dict[str, Any], ruleset=None) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """一次取完结果:(是否被忽略, 命中的告警规则 {'rule', 'event'} 或 None)。
        忽略规则和告警规则用同一份快照,中途保存了规则也不会混用新旧两份"""
        if ruleset is None:
            ruleset = self.ruleset
        if first_match(ruleset.ignore_rules, event) is not None:
            logger.debug(f"事件被忽略: {event.get('eventUid')}")
            return True, None
        index = first_match(ruleset.alert_rules, event)
        if index is None:
            logger.debug(f"事件未匹配任何规则: {event.get('eventUid')}")
            return False, None
        rule = ruleset.alert_rules[index]
        logger.info(f"事件匹配规则: {rule.get('name')} - {event.get('eventUid')}")
        return False, {'rule': rule, 'event': event}

    def should_ignore_event(self, event: Dict[str, Any]) -> bool:
        """事件是否被忽略规则忽略(任一启用的忽略规则命中)"""
        return first_match(self.ruleset.ignore_rules, event) is not None

    def match_alert_rules(self, event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """先看忽略规则,再按顺序匹配告警规则;返回 {'rule', 'event'},没有命中返回 None"""
        return self.evaluate(event)[1]

    def get_rule_stats(self) -> Dict[str, Any]:
        """规则统计信息"""
        ruleset = self.ruleset
        enabled = sum(1 for rule in ruleset.alert_rules if rule_enabled(rule))
        return {
            'source': ruleset.source,
            'loaded': ruleset.loaded,
            'total_rules': len(ruleset.alert_rules),
            'enabled_rules': enabled,
            'disabled_rules': len(ruleset.alert_rules) - enabled,
            'ignore_rules': len(ruleset.ignore_rules),
            'enabled_ignore_rules': sum(1 for rule in ruleset.ignore_rules if rule_enabled(rule)),
        }
