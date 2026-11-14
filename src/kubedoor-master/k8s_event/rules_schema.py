"""K8S 事件告警规则的结构定义与校验(纯函数,不碰数据库)。

规则存 auth_settings(key=event_alert_rules),结构与以前的 alert_rules.json 相同:
    {"global_ignore_rules": [规则...], "alert_rules": [规则...]}
    规则 = {"name": "...", "enabled": true, "severity": "warning"(只有告警规则有), "conditions": {字段: {运算符: 值}}}

- check / validate:页面保存、试运行时的严格校验。不合法的写法一律拒绝,错误信息指明位置
  (例如「告警规则第 3 条「重启事件告警」的条件 count：greater_than 的值必须是数字」);
  合法时返回规范化后的规则(补上 enabled / severity 的默认值,名称去掉首尾空白)。
- legacy_structure_ok:导入旧规则文件时的宽松检查(顶层是对象、两个键存在时是数组),不改变用户原来的规则行为。

Web 页面(src/kubedoor-web/src/views/system/event-alert-rules/rules-utils.ts)有一份同样的校验,错误信息逐字一致;
两边共用 tests/fixtures/event_rules_cases.json 里的用例,改这里要同步改那边。
"""

import math

# 事件字段(与 event_processor._process_event_data 组装的字段一致),顺序即页面下拉的顺序
FIELDS = (
    'k8s', 'namespace', 'kind', 'name', 'reason', 'message', 'level', 'count', 'eventStatus',
    'reportingComponent', 'reportingInstance', 'firstTimestamp', 'lastTimestamp', 'eventUid',
)
NUMERIC_FIELD = 'count'
# 运算符。一个字段写了多个运算符时,匹配器按这个顺序只认第一个(保存时只允许写一个)
LIST_OPERATORS = ('contains', 'not_contains', 'starts_with', 'not_starts_with', 'ends_with', 'not_ends_with')
SCALAR_OPERATORS = ('equals', 'not_equals')
NUMERIC_OPERATORS = ('greater_than', 'less_than', 'greater_equal', 'less_equal')
OPERATORS = LIST_OPERATORS + SCALAR_OPERATORS + NUMERIC_OPERATORS
SEVERITIES = ('critical', 'warning', 'info')
DEFAULT_SEVERITY = 'warning'

IGNORE_KEY = 'global_ignore_rules'
ALERT_KEY = 'alert_rules'
GROUPS = ((IGNORE_KEY, '忽略规则'), (ALERT_KEY, '告警规则'))
RULE_KEYS = {IGNORE_KEY: ('name', 'enabled', 'conditions'), ALERT_KEY: ('name', 'enabled', 'severity', 'conditions')}

MAX_RULES = 200  # 每组最多多少条
MAX_VALUES = 200  # 一个条件最多多少个值
MAX_TEXT = 1000  # 一个值最长多少字符
MAX_NAME = 100  # 规则名称最长多少字符
NAME_IN_MESSAGE = 30  # 错误信息里的规则名称最多显示多少字符


class RulesInvalid(ValueError):
    """规则不合法。issues 是 [{'path': [...], 'message': '...'}];str() 是第一处问题(多处时注明共几处)"""

    def __init__(self, issues):
        self.issues = issues
        more = f'（共 {len(issues)} 处问题）' if len(issues) > 1 else ''
        super().__init__(issues[0]['message'] + more)


def _join(items):
    return '、'.join(items)


def _rule_label(group_label, index, rule):
    """「告警规则第 3 条「重启事件告警」」;没有名称时只有序号"""
    label = f'{group_label}第 {index + 1} 条'
    name = rule.get('name') if isinstance(rule, dict) else None
    if isinstance(name, str) and name.strip():
        name = name.strip()
        if len(name) > NAME_IN_MESSAGE:
            name = name[:NAME_IN_MESSAGE] + '…'
        label += f'「{name}」'
    return label


def _is_number(value):
    """JSON 数字:不能是布尔(Python 里 bool 是 int 的子类),不能是 NaN / Infinity"""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and math.isfinite(value)


def _field_hint(field):
    for known in FIELDS:
        if known.lower() == field.lower():
            return f'（字段名区分大小写，应为 {known}）'
    return f'（可用字段：{_join(FIELDS)}）'


def _check_values(head, op, value, fail):
    """列表型运算符的值:非空的字符串数组,不含空串;不合法时记问题并返回 False"""
    if not isinstance(value, list):
        fail(f'{head} 的值必须是字符串数组，例如 ["abc"]', (op,))
        return False
    if not value:
        fail(f'{head} 至少要填一个值', (op,))
        return False
    if len(value) > MAX_VALUES:
        fail(f'{head} 最多 {MAX_VALUES} 个值', (op,))
        return False
    for index, item in enumerate(value):
        problem = ''
        if not isinstance(item, str):
            problem = '必须是字符串'
        elif not item:
            problem = '不能为空'
        elif not item.strip():
            problem = '只有空格'
        elif len(item) > MAX_TEXT:
            problem = f'最长 {MAX_TEXT} 个字符'
        if problem:
            fail(f'{head} 的第 {index + 1} 个值{problem}', (op, index))
            return False
    return True


def _check_condition(where, field, spec, path, issues):
    """校验一个字段的条件,返回规范化后的 {运算符: 值};不合法时记一处问题并返回 None"""
    def fail(message, sub=()):
        issues.append({'path': [*path, *sub], 'message': message})

    if field not in FIELDS:
        fail(f'{where}的条件字段 {field} 不存在{_field_hint(field)}')
        return None
    head = f'{where}的条件 {field}'
    if not isinstance(spec, dict):
        fail(f'{head} 必须写成 {{"运算符": 值}}，例如 {{"contains": ["abc"]}}')
        return None
    if not spec:
        fail(f'{head} 缺少运算符')
        return None
    allowed = OPERATORS if field == NUMERIC_FIELD else LIST_OPERATORS + SCALAR_OPERATORS
    for op in spec:
        if op in NUMERIC_OPERATORS and field != NUMERIC_FIELD:
            fail(f'{head}：{op} 只能用于 count 字段', (op,))
            return None
        if op not in allowed:
            fail(f'{head}：不认识的运算符 {op}（可用：{_join(allowed)}）', (op,))
            return None
    if len(spec) > 1:
        fail(f'{head} 只能写一个运算符，现在写了 {_join(spec)}')
        return None
    (op, value), = spec.items()
    head = f'{head}：{op}'
    if op in LIST_OPERATORS:
        return {op: list(value)} if _check_values(head, op, value, fail) else None
    if op in SCALAR_OPERATORS:
        if isinstance(value, str):
            if len(value) > MAX_TEXT:
                fail(f'{head} 的值最长 {MAX_TEXT} 个字符', (op,))
                return None
        elif not _is_number(value):
            fail(f'{head} 的值必须是字符串或数字', (op,))
            return None
        return {op: value}
    if not _is_number(value):
        fail(f'{head} 的值必须是数字', (op,))
        return None
    return {op: value}


def _check_rule(group, group_label, index, rule, issues):
    """校验一条规则,返回规范化后的规则;有问题时返回 None(问题记进 issues)"""
    path = [group, index]
    where = _rule_label(group_label, index, rule)
    keys = RULE_KEYS[group]
    if not isinstance(rule, dict):
        issues.append({'path': path, 'message': f'{where}必须是对象（包含 {_join(keys)}）'})
        return None
    before = len(issues)
    for key in rule:
        if key in keys:
            continue
        if key == 'severity':
            message = f'{where}不能有 severity：级别只对告警规则有用'
        else:
            message = f'{where}不能有 {key}：只能有 {_join(keys)}'
        issues.append({'path': [*path, key], 'message': message})
    name = rule.get('name')
    name_path = [*path, 'name'] if 'name' in rule else path
    if name is not None and not isinstance(name, str):
        issues.append({'path': name_path, 'message': f'{where}的名称 name 必须是字符串'})
    elif name is None or not name.strip():
        issues.append({'path': name_path, 'message': f'{where}缺少名称 name'})
    elif len(name.strip()) > MAX_NAME:
        issues.append({'path': name_path, 'message': f'{where}的名称最长 {MAX_NAME} 个字符'})
    enabled = rule.get('enabled', True)
    if not isinstance(enabled, bool):
        issues.append({'path': [*path, 'enabled'], 'message': f'{where}的 enabled 只能是 true 或 false'})
    severity = rule.get('severity', DEFAULT_SEVERITY)
    if group == ALERT_KEY and severity not in SEVERITIES:
        issues.append({'path': [*path, 'severity'],
                       'message': f'{where}的级别 severity 只能是 critical、warning 或 info'})
    conditions = rule.get('conditions')
    conditions_path = [*path, 'conditions'] if 'conditions' in rule else path
    normalized = {}
    if conditions is None:
        issues.append({'path': conditions_path, 'message': f'{where}缺少条件 conditions'})
    elif not isinstance(conditions, dict):
        issues.append({'path': conditions_path,
                       'message': f'{where}的条件 conditions 必须是对象，例如 {{"reason": {{"contains": ["OOMKilling"]}}}}'})
    elif not conditions:
        issues.append({'path': conditions_path,
                       'message': f'{where}至少要有一个条件（没有条件会匹配所有事件）'})
    else:
        for field, spec in conditions.items():
            checked = _check_condition(where, field, spec, [*path, 'conditions', field], issues)
            if checked is not None:
                normalized[field] = checked
    if len(issues) > before:
        return None
    result = {'name': name.strip(), 'enabled': enabled}
    if group == ALERT_KEY:
        result['severity'] = severity
    result['conditions'] = normalized
    return result


def check(value):
    """严格校验,返回 (规范化后的规则, 问题列表);有问题时规则为 None。问题按在规则里出现的顺序排列"""
    if not isinstance(value, dict):
        return None, [{'path': [], 'message': '规则必须是一个 JSON 对象，包含 global_ignore_rules 和 alert_rules 两个数组'}]
    issues = []
    known = [key for key, _ in GROUPS]
    for key in value:
        if key not in known:
            issues.append({'path': [key], 'message': f'顶层不能有 {key}：只能有 {known[0]} 和 {known[1]}'})
    result = {}
    for key, label in GROUPS:
        if key not in value:
            issues.append({'path': [], 'message': f'缺少 {key}（{label}）：没有规则时写成空数组 []'})
            continue
        rules = value[key]
        if not isinstance(rules, list):
            issues.append({'path': [key], 'message': f'{key}（{label}）必须是数组'})
            continue
        if len(rules) > MAX_RULES:
            issues.append({'path': [key], 'message': f'{label}最多 {MAX_RULES} 条，现在有 {len(rules)} 条'})
            continue
        result[key] = [_check_rule(key, label, index, rule, issues) for index, rule in enumerate(rules)]
    if issues:
        return None, issues
    return result, []


def validate(value):
    """严格校验;合法返回规范化后的规则,不合法抛 RulesInvalid"""
    result, issues = check(value)
    if issues:
        raise RulesInvalid(issues)
    return result


def legacy_structure_ok(value):
    """导入旧规则文件时的宽松检查:顶层是对象,global_ignore_rules / alert_rules 存在时必须是数组"""
    if not isinstance(value, dict):
        return False
    return all(isinstance(value[key], list) for key, _ in GROUPS if key in value)


def summary(value):
    """审计用:每组的条数和名称(不记条件内容)"""
    result = {}
    for key, _ in GROUPS:
        rules = value.get(key) if isinstance(value, dict) else None
        rules = rules if isinstance(rules, list) else []
        result[key] = {'count': len(rules),
                       'names': [str(rule.get('name', '')) if isinstance(rule, dict) else '' for rule in rules]}
    return result
