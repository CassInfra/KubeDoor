"""ClickHouse 结构化查询构造（约定 §5.2）：表 `db`.`table`、字段 `name`，值一律参数绑定（名字前缀 kd_）。

- 时间：<t> >= fromUnixTimestamp64Milli({kd_from:Int64}) AND <t> < fromUnixTimestamp64Milli({kd_to:Int64})；
  时间字段要是 Date / DateTime / DateTime64；输出 toUnixTimestamp64Milli(toDateTime64(<t>, 3)) AS kd_ms
- 每条查询都带集群绑定的条件（conditions 全部 AND）
- 按字段类型选参数类型：字符串类 String（UUID / IP / Enum 用 toString(col) 比较），整数 Int64（超范围 UInt64），
  小数 Float64，Bool 用 toUInt8；时间类字段不支持按值过滤（只能用时间窗口）
"""

from .clickhouse import Params, column, table_ref
from .common import LogError, invalid
from .params import deployment_regex

MS_OFFSET = 8 * 3600 * 1000  # 时间桶按北京时间对齐
_CMP = {'gt': '>', 'gte': '>=', 'lt': '<', 'lte': '<='}
_INT64_MAX = 2 ** 63 - 1


def _int_value(value, label):
    if isinstance(value, bool):
        raise invalid(f'{label}要是整数')
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    text = str(value).strip()
    if text.lstrip('-').isdigit():
        return int(text)
    raise invalid(f'{label}要是整数（收到 {str(value)[:40]}）')


def _float_value(value, label):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise invalid(f'{label}要是数字（收到 {str(value)[:40]}）') from None
    if number != number:
        raise invalid(f'{label}要是数字')
    return number


def _bool_value(value, label):
    text = str(value).strip().lower()
    if text in ('1', 'true'):
        return 1
    if text in ('0', 'false'):
        return 0
    raise invalid(f'{label}要是 true / false')


def _is_number(value):
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    try:
        float(str(value))
        return True
    except ValueError:
        return False


class SqlPlan:
    def __init__(self, bound):
        self.bound = bound
        self.fields = bound.schema
        self.params = Params()
        self.table = table_ref(bound.target)
        info = self.fields.get(bound.time_field)
        if info is None:
            raise LogError(400, f'日志源 {bound.id} 的时间字段 {bound.time_field} 在表 {bound.target} 里不存在，'
                                '请系统管理员检查配置', 'field_not_found')
        if info['kind'] != 'date':
            raise LogError(400, f'时间字段 {bound.time_field} 的类型是 {info["type"]}，不支持（要是 Date / DateTime / '
                                'DateTime64）', 'invalid_request')
        self.time = column(bound.time_field)
        self.ms = f'toUnixTimestamp64Milli(toDateTime64({self.time}, 3))'

    def kind(self, name):
        return self.fields[name]['kind']

    # ---- 单个条件 ----
    def _typed(self, name, values):
        """等值类比较：(列表达式, 参数类型, 转换后的值)"""
        kind, col = self.kind(name), column(name)
        label = f'字段 {name} 的取值'
        if kind == 'date':
            raise invalid(f'字段 {name} 是时间类型，不支持按值过滤；用 start / end 限定时间')
        if kind == 'string':
            return col, 'String', [str(value) for value in values]
        if kind == 'int':
            numbers = [_int_value(value, label) for value in values]
            return col, 'UInt64' if any(number > _INT64_MAX for number in numbers) else 'Int64', numbers
        if kind == 'float':
            return col, 'Float64', [_float_value(value, label) for value in values]
        if kind == 'bool':
            return f'toUInt8({col})', 'UInt8', [_bool_value(value, label) for value in values]
        return f'toString({col})', 'String', [str(value) for value in values]

    def compare(self, name, op, value):
        col = column(name)
        p = self.params
        if op == 'exists':
            return f"(isNotNull({col}) AND toString({col}) != '')"
        if op == 'not_exists':
            return f"(isNull({col}) OR toString({col}) = '')"
        if op == 'contains':
            return f'positionCaseInsensitive(toString({col}), {p.add("c", "String", str(value))}) > 0'
        if op == 'not_contains':
            return f'ifNull(positionCaseInsensitive(toString({col}), {p.add("c", "String", str(value))}), 0) = 0'
        if op == 'prefix':
            return f'startsWith(toString({col}), {p.add("pre", "String", str(value))})'
        if op == 'regex':
            return f'match(toString({col}), {p.add("re", "String", str(value))})'
        kind = self.kind(name)
        if op in _CMP:
            sign = _CMP[op]
            if kind == 'date':
                raise invalid(f'字段 {name} 是时间类型，不支持按值比较；用 start / end 限定时间')
            if kind == 'int':
                number = _int_value(value, f'字段 {name} 的取值')
                type_ = 'UInt64' if number > _INT64_MAX else 'Int64'
                return f'{col} {sign} {p.add("n", type_, number)}'
            if kind == 'float' or (kind == 'string' and _is_number(value)):
                expr = col if kind == 'float' else f'toFloat64OrNull({col})'
                return f'{expr} {sign} {p.add("n", "Float64", _float_value(value, f"字段 {name} 的取值"))}'
            return f'toString({col}) {sign} {p.add("s", "String", str(value))}'
        values = value if isinstance(value, list) else [value]
        expr, type_, converted = self._typed(name, values)
        if op == 'eq':
            return f'{expr} = {p.add("v", type_, converted[0])}'
        if op == 'ne':
            return f'{expr} != {p.add("v", type_, converted[0])}'
        if op == 'in':
            return f'{expr} IN {p.add("vs", f"Array({type_})", converted)}'
        if op == 'not_in':
            return f'{expr} NOT IN {p.add("vs", f"Array({type_})", converted)}'
        raise invalid(f'不支持的条件 {op}')

    # ---- 条件组合 ----
    def time_conds(self, start_ms, end_ms):
        return [f'{self.time} >= fromUnixTimestamp64Milli({self.params.add("from", "Int64", int(start_ms))})',
                f'{self.time} < fromUnixTimestamp64Milli({self.params.add("to", "Int64", int(end_ms))})']

    def binding_conds(self):
        conds = []
        for cond in self.bound.binding['conditions']:
            if cond['field'] not in self.fields:
                raise LogError(400, f'集群 {self.bound.env} 绑定日志源 {self.bound.id} 用的过滤字段 {cond["field"]} 在表 '
                                    f'{self.bound.target} 里不存在，请系统管理员检查配置', 'field_not_found')
            value = cond['values'] if cond['op'] in ('in', 'not_in') else cond['values'][0]
            conds.append(self.compare(cond['field'], cond['op'], value))
        return conds

    def spec_conds(self, spec):
        conds = []
        p = self.params
        if spec.get('level'):
            level = spec['level']
            conds.append(f'toString({column(level["field"])}) IN '
                         f'{p.add("lv", "Array(String)", [str(value) for value in level["values"]])}')
        for item in spec.get('k8s') or []:
            if item['param'] == 'k8s_deployment':
                conds.append(f'match(toString({column(item["field"])}), '
                             f'{p.add("dep", "String", deployment_regex(item["value"]))})')
            else:
                conds.append(self.compare(item['field'], 'eq', item['value']))
        for item in spec.get('filters') or []:
            conds.append(self.compare(item['field'], item['op'], item['value']))
        message = self.bound.semantic('message')
        if spec.get('keyword'):
            conds.append(f'positionCaseInsensitive(toString({column(message)}), '
                         f'{p.add("kw", "String", spec["keyword"])}) > 0')
        for text in spec.get('exclude') or []:
            conds.append(f'ifNull(positionCaseInsensitive(toString({column(message)}), '
                         f'{p.add("ex", "String", text)}), 0) = 0')
        return conds

    def where(self, spec, start_ms, end_ms, extra=()):
        conds = self.time_conds(start_ms, end_ms) + self.binding_conds() + self.spec_conds(spec) + list(extra)
        return '\n  AND '.join(conds)

    # ---- 查询 ----
    def select_rows(self, spec, start_ms, end_ms, columns, limit, order='desc', extra=()):
        """明细：kd_ms + 选中的列，按时间排序"""
        cols = ''.join(f', {column(name)}' for name in columns)
        direction = 'ASC' if order == 'asc' else 'DESC'
        return (f'SELECT {self.ms} AS kd_ms{cols}\nFROM {self.table}\nWHERE {self.where(spec, start_ms, end_ms, extra)}'
                f'\nORDER BY {self.time} {direction}\nLIMIT {self.params.add("limit", "UInt32", int(limit))}')

    def count(self, spec, start_ms, end_ms, extra=()):
        return f'SELECT count() AS kd_count\nFROM {self.table}\nWHERE {self.where(spec, start_ms, end_ms, extra)}'

    def bucket_expr(self, interval_ms):
        """时间桶（毫秒，按北京时间对齐）"""
        iv = self.params.add('iv', 'Int64', int(interval_ms))
        return f'intDiv({self.ms} + {MS_OFFSET}, {iv}) * {iv} - {MS_OFFSET}'

    def metric_expr(self, metric):
        op, name = metric['op'], metric.get('field')
        if op == 'count':
            return 'count()'
        col = column(name)
        if op == 'uniq':
            return f'uniq({col})'
        kind = self.kind(name)
        if op in ('min', 'max') and kind == 'date':
            return f'{op}({col})'
        if kind in ('int', 'float'):
            value = col
        elif kind == 'string':
            value = f'toFloat64OrNull({col})'
        else:
            raise invalid(f'字段 {name} 的类型是 {self.fields[name]["type"]}，不能算 {op}')
        if op in ('avg', 'min', 'max', 'sum'):
            return f'{op}({value})'
        return f'quantile({int(op[1:]) / 100})({value})'
