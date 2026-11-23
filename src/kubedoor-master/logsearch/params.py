"""AI 参数的解析和校验（约定 §4.1）：时间窗口、keyword、level、k8s_*、filters，以及条件的中文描述。

字段名都要在实时字段表里（不在就 400 field_not_found，带相近的字段名）；日志源没配对应的语义字段时 400 invalid_request。
"""

import re

from . import config as log_config
from . import schema as field_schema
from .common import check_keys, has_control, invalid, list_param, resolve_window, text_param

PARAM_HINTS = {
    'namespace': 'k8s_namespace', 'pod': 'k8s_pod', 'deployment': 'k8s_deployment',
    'query': 'keyword', 'q': 'keyword', 'text': 'keyword', 'index': 'source', 'table': 'source', 'size': 'limit',
    'trace_id': 'log_correlate 的 key + value',
}
TIME_KEYS = frozenset({'start', 'end', 'lookback'})
FILTER_KEYS = frozenset({'source', *TIME_KEYS, 'keyword', 'level', 'k8s_namespace', 'k8s_pod', 'k8s_deployment',
                         'filters'})
FILTER_OPS = ('eq', 'ne', 'in', 'not_in', 'contains', 'not_contains', 'prefix', 'gt', 'gte', 'lt', 'lte', 'exists',
              'not_exists', 'regex')
MAX_FILTERS = 10
MAX_IN_VALUES = 100
MAX_VALUE = 500
MAX_REGEX = 200
OP_TEXT = {'eq': '=', 'ne': '≠', 'gt': '>', 'gte': '≥', 'lt': '<', 'lte': '≤'}
K8S_PARAMS = (('k8s_namespace', 'namespace', '命名空间'), ('k8s_pod', 'pod', 'Pod'),
              ('k8s_deployment', 'pod', 'Pod'))


def source_id(body):
    value = body.get('source')
    if not isinstance(value, str) or not value.strip():
        raise invalid('source 必填（日志源 ID，用 log_sources 查看）')
    text = value.strip()
    if len(text) > 32 or has_control(text):
        raise invalid('source 是日志源 ID，最长 32 个字符')
    return text


def _filter_value(value, label, max_len=MAX_VALUE):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise invalid(f'{label}要是字符串或数字')
    if isinstance(value, str):
        if len(value) > max_len or has_control(value):
            raise invalid(f'{label}不能换行，最长 {max_len} 个字符')
    elif value != value:
        raise invalid(f'{label}不是合法的数字')
    return value


def parse_filters(items, bound):
    """filters → [{field, op, value}]；字段要在实时字段表里"""
    if items is None:
        return []
    if not isinstance(items, list):
        raise invalid('filters 要是数组：[{field, op, value}]')
    if len(items) > MAX_FILTERS:
        raise invalid(f'filters 最多 {MAX_FILTERS} 项')
    result = []
    for index, item in enumerate(items):
        label = f'filters 第 {index + 1} 项'
        if not isinstance(item, dict):
            raise invalid(f'{label}要是对象：{{field, op, value}}')
        unknown = [str(key) for key in item if key not in ('field', 'op', 'value')]
        if unknown:
            raise invalid(f'{label}不支持的键：{"、".join(unknown[:5])}（只能有 field、op、value）')
        name = item.get('field')
        if not isinstance(name, str) or not log_config.FIELD_NAME.fullmatch(name.strip()):
            raise invalid(f'{label}的 field 不是合法的字段名')
        name = name.strip()
        field_schema.require(bound.schema, name, '', bound.id)
        op = item.get('op')
        if op not in FILTER_OPS:
            raise invalid(f'{label}的 op 只能是 {" / ".join(FILTER_OPS)}')
        value = item.get('value')
        if op in ('exists', 'not_exists'):
            if value is not None:
                raise invalid(f'{label}：{op} 不带 value')
        elif op in ('in', 'not_in'):
            if not isinstance(value, list) or not 1 <= len(value) <= MAX_IN_VALUES:
                raise invalid(f'{label}：{op} 的 value 要是 1~{MAX_IN_VALUES} 项的数组')
            value = [_filter_value(entry, f'{label}的 value 里的值') for entry in value]
        elif op == 'regex':
            if not isinstance(value, str) or not value:
                raise invalid(f'{label}：regex 的 value 要是正则表达式字符串')
            value = _filter_value(value, f'{label}的正则', MAX_REGEX)
        else:
            if value is None:
                raise invalid(f'{label}：{op} 要带 value')
            value = _filter_value(value, f'{label}的 value')
        result.append({'field': name, 'op': op, 'value': value})
    return result


def level_values(bound, level):
    """error / warn / info / debug → 日志源配置的取值（没配就用 [原值, 大写, 小写, 首字母大写]）；其他按原值"""
    key = level.lower()
    if key in log_config.LEVELS:
        configured = bound.source['level_values'].get(key)
        if configured:
            return list(configured)
        return list(dict.fromkeys([level, level.upper(), level.lower(), level.capitalize()]))
    return [level]


def spaced(label):
    """中文里夹英文名词时两边加空格：Pod → 「 Pod 」"""
    return f' {label} ' if label.isascii() else label


def need_semantic(bound, key, purpose):
    name = bound.semantic(key)
    if not name:
        raise invalid(f'日志源 {bound.id} 没有配置{spaced(log_config.SEMANTIC_LABELS[key])}字段，不能{purpose}；'
                      '用 filters')
    field_schema.require(bound.schema, name, f'{log_config.SEMANTIC_LABELS[key]}', bound.id)
    return name


def parse_spec(body, bound, allowed, default_lookback='1h', hints=None):
    """search / stats / patterns 共用的过滤参数 → spec"""
    check_keys(body, allowed, {**PARAM_HINTS, **(hints or {})})
    spec = {'filters': [], 'level': None, 'keyword': '', 'k8s': [], 'exclude': []}
    spec['start'], spec['end'] = resolve_window(body, default_lookback)
    keyword = text_param(body, 'keyword', 500)
    if keyword:
        spec['keyword'] = keyword
        spec['message'] = need_semantic(bound, 'message', '按 keyword 搜正文')
    level = text_param(body, 'level', 50)
    if level:
        field = need_semantic(bound, 'level', '按 level 过滤')
        spec['level'] = {'field': field, 'name': level, 'values': level_values(bound, level)}
    for param, key, label in K8S_PARAMS:
        value = text_param(body, param, 253)
        if not value:
            continue
        what = '按 Deployment 过滤' if param == 'k8s_deployment' else f'按{spaced(label)}过滤'
        if not bound.semantic(key):
            raise invalid(f'日志源 {bound.id} 没有配置{spaced(label)}字段，不能{what}；用 filters')
        field = need_semantic(bound, key, what)
        spec['k8s'].append({'param': param, 'field': field, 'value': value})
    spec['filters'] = parse_filters(body.get('filters'), bound)
    return spec


def deployment_regex(name):
    """Deployment 名 → 它的 Pod 名的正则（RE2 / Python）"""
    return f'^{re.escape(name)}-[a-z0-9]{{1,10}}-[a-z0-9]{{5}}$'


def parse_exclude(body):
    items = list_param(body, 'exclude', 20)
    result = []
    for item in items:
        if not isinstance(item, str) or not item.strip() or len(item) > 200 or has_control(item):
            raise invalid('exclude 的每一项要是不超过 200 个字符的文字')
        if item.strip() not in result:
            result.append(item.strip())
    return result


# ---------------------------------------------------------------------------
# 中文描述
# ---------------------------------------------------------------------------
def _label(bound, name):
    for key, field in bound.source['fields'].items():
        if field == name:
            return log_config.SEMANTIC_LABELS[key]
    return name


def _values_text(values, limit=10):
    items = [str(value) for value in values[:limit]]
    more = f' 等 {len(values)} 个' if len(values) > limit else ''
    return '{' + ', '.join(items) + '}' + more


def condition_text(label, op, value):
    if op in OP_TEXT:
        return f'{label} {OP_TEXT[op]} {value}'
    if op == 'in':
        return f'{label} ∈ {_values_text(value)}'
    if op == 'not_in':
        return f'{label} ∉ {_values_text(value)}'
    if op == 'contains':
        return f'{label} 包含「{value}」'
    if op == 'not_contains':
        return f'{label} 不包含「{value}」'
    if op == 'prefix':
        return f'{label} 以「{value}」开头'
    if op == 'exists':
        return f'{label} 有值'
    if op == 'not_exists':
        return f'{label} 没有值'
    if op == 'regex':
        return f'{label} 匹配正则 {value}'
    return f'{label} {op} {value}'


def binding_text(binding):
    conditions = binding['conditions']
    if not conditions:
        return ['集群绑定：整个表 / 索引都属于这个集群']
    result = []
    for cond in conditions:
        value = cond['values'] if cond['op'] in ('in', 'not_in') else cond['values'][0]
        result.append('集群绑定：' + condition_text(cond['field'], cond['op'], value))
    return result


def describe(bound, spec):
    items = binding_text(bound.binding)
    if spec.get('keyword'):
        items.append(f'正文包含「{spec["keyword"]}」')
    if spec.get('level'):
        level = spec['level']
        items.append(f'级别 = {level["name"]}（取值 {"、".join(map(str, level["values"]))}）')
    for item in spec.get('k8s') or []:
        if item['param'] == 'k8s_namespace':
            items.append(f'命名空间 = {item["value"]}')
        elif item['param'] == 'k8s_pod':
            items.append(f'Pod = {item["value"]}')
        else:
            items.append(f'Deployment = {item["value"]}（按 Pod 名匹配）')
    for item in spec.get('filters') or []:
        items.append(condition_text(_label(bound, item['field']), item['op'], item['value']))
    for text in spec.get('exclude') or []:
        items.append(f'正文不包含「{text}」')
    return items
