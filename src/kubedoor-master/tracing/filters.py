"""Jaeger 结构化过滤 query.filter 的构造（约定 §1.3、§5.3）。

- 所有条件都写进 filter（用了 filter 就不能再带 query.serviceName 等参数，否则 Jaeger 返回 400）
- 顶层是一个 Call：{"op": ..., "args": [Expression, ...]}；参数里的子调用写成 {"call": {...}}；
  and / or 只有一个参数时直接用那个参数
- 语义字段（状态码、请求方法……）按字段映射的候选属性名 OR；attr 不写 level = 搜 span + resource 两层
- ClickHouse 后端只有 and / or / not / eq，比较大小只对 span.duration：状态码一律 eq，「4xx」「5xx」和范围展开成
  标准状态码；报错一律 span.status = error（error=true 属性查不到）
"""

from .common import invalid
from .fields import FIELD_LABELS, FIELD_LEVELS

STANDARD_CODES = (
    100, 101, 102, 103,
    200, 201, 202, 203, 204, 205, 206, 207, 208, 226,
    300, 301, 302, 303, 304, 305, 306, 307, 308,
    *range(400, 419), 421, 422, 423, 424, 425, 426, 428, 429, 431, 451,
    *range(500, 509), 510, 511,
)
MAX_STATUS_EQ = 200
# 语义字段参数 → 字段映射的 key
SEMANTIC_PARAMS = (
    ('method', 'method'), ('route', 'route'), ('path', 'path'), ('url', 'url'), ('host', 'host'),
    ('client_ip', 'client_ip'), ('exception_type', 'exception_type'),
    ('k8s_pod', 'pod'), ('k8s_namespace', 'namespace'), ('k8s_deployment', 'deployment'),
)


def scalar(value):
    return {'scalar': {'value': str(value)}}


def field(name, level):
    return {'field': {'name': name, 'level': level}}


def attr(key, level=''):
    return {'attr': {'key': key, 'level': level}} if level else {'attr': {'key': key}}


def call(op, *args):
    return {'call': {'op': op, 'args': list(args)}}


def combine(op, items):
    """and / or：没有参数返回 None，一个参数直接返回它"""
    items = [item for item in items if item]
    if not items:
        return None
    if len(items) == 1:
        return items[0]
    return call(op, *items)


def expand_status(codes, ranges):
    """状态码 + 范围 → 排好序的标准状态码（范围只展开标准状态码；明确写的码原样保留）"""
    result = set(codes)
    for low, high in ranges:
        result.update(code for code in STANDARD_CODES if low <= code <= high)
    return sorted(result)


def _candidates(field_cfg, key):
    names = field_cfg.get(key) or []
    if not names:
        raise invalid(f'字段映射里「{FIELD_LABELS[key]}」没有属性名，不能按它查（请系统管理员在「系统管理 → 链路追踪」配置）')
    return names


def _semantic(field_cfg, key, value):
    level = FIELD_LEVELS[key]
    return combine('or', [call('eq', attr(name, level), scalar(value)) for name in _candidates(field_cfg, key)])


def build_filter(spec, field_cfg, cluster_key='', cluster_values=()):
    """search 的条件 → (filter Call 或 None, 用到的字段映射 {key: [属性名]}, 用到的运算符, 用到的层级)"""
    conditions, used = [], {}
    if spec.get('service'):
        conditions.append(call('eq', field('service', 'resource'), scalar(spec['service'])))
    if spec.get('operation'):
        conditions.append(call('eq', field('name', 'span'), scalar(spec['operation'])))
    if spec.get('span_kind'):
        conditions.append(call('eq', field('kind', 'span'), scalar(spec['span_kind'])))
    if spec.get('error'):
        conditions.append(call('eq', field('status', 'span'), scalar('error')))
    codes = expand_status(spec.get('status_codes') or (), spec.get('status_ranges') or ())
    if codes:
        names = _candidates(field_cfg, 'status_code')
        if len(codes) * len(names) > MAX_STATUS_EQ:
            raise invalid(f'状态码条件展开后有 {len(codes) * len(names)} 个（状态码 × 字段映射的属性名），'
                          f'超过 {MAX_STATUS_EQ} 个：请缩小范围，或者配置 ClickHouse 加速')
        used['status_code'] = list(names)
        conditions.append(combine('or', [call('eq', attr(name), scalar(code)) for code in codes for name in names]))
    if spec.get('min_duration'):
        conditions.append(call('gte', field('duration', 'span'), scalar(spec['min_duration'])))
    if spec.get('max_duration'):
        conditions.append(call('lte', field('duration', 'span'), scalar(spec['max_duration'])))
    for _, key in SEMANTIC_PARAMS:
        value = (spec.get('fields') or {}).get(key)
        if value:
            used[key] = list(_candidates(field_cfg, key))
            conditions.append(_semantic(field_cfg, key, value))
    for key, value in (spec.get('attributes') or {}).items():
        conditions.append(call('eq', attr(key), scalar(value)))
    if cluster_key and cluster_values:
        conditions.append(combine('or', [call('eq', attr(cluster_key, 'resource'), scalar(value))
                                         for value in cluster_values]))
    expression = combine('and', conditions)
    if expression is None:
        return None, used, set(), set()
    ops, levels = set(), set()
    _collect(expression, ops, levels)
    return expression['call'], used, ops, levels


def _collect(expression, ops, levels):
    if 'call' in expression:
        ops.add(expression['call']['op'])
        for arg in expression['call']['args']:
            _collect(arg, ops, levels)
    elif 'attr' in expression:
        level = expression['attr'].get('level')
        levels.update([level] if level else ['span', 'resource'])
    elif 'field' in expression:
        levels.add(expression['field']['level'])


def check_capabilities(caps, ops, levels):
    """Jaeger 的后端能力够不够用；不够抛 invalid_request（缺 and / or / eq 由调用方先判 version_unsupported）"""
    flt = ((caps or {}).get('search') or {}).get('filter') or {}
    missing_ops = sorted(op for op in ops if op not in (flt.get('operators') or []))
    if missing_ops:
        hint = '（按耗时过滤要 gte / lte）' if {'gte', 'lte'} & set(missing_ops) else ''
        raise invalid(f'这个 Jaeger 的存储后端不支持运算符 {"、".join(missing_ops)}{hint}，去掉相应的条件再查')
    missing_levels = sorted(level for level in levels if level not in (flt.get('levels') or []))
    if missing_levels:
        raise invalid(f'这个 Jaeger 的存储后端不支持按 {"、".join(missing_levels)} 层级的属性过滤，去掉相应的条件再查')
