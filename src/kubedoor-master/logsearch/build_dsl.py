"""Elasticsearch 结构化查询构造（约定 §5.3）。

- bool.filter = 时间 range（epoch_millis）+ 集群绑定条件 + level + k8s + filters；keyword 放 bool.must 的 match_phrase；
  排除放 must_not
- 精确类操作（eq / ne / in / not_in / prefix / regex / k8s / level）对 text 字段改用 .keyword 子字段，没有就
  eq / in 降级为 match_phrase（prefix / regex 报 400）；contains 对 text 用 match_phrase、对 keyword 用 wildcard
- 排序 [{<t>: {order, unmapped_type: date}}]；时间用 fields 的 epoch_millis 取（date / date_nanos 都对）
"""

import re

from .common import LogError, invalid

_LUCENE_RESERVED = re.compile(r'([.?+*|{}\[\]()"\\#@&<>~])')
_WILDCARD = re.compile(r'([*?\\])')
MAX_BUCKETS = 500


def lucene_escape(text):
    return _LUCENE_RESERVED.sub(r'\\\1', text)


def lucene_regex(pattern):
    """Lucene 的正则总是完整匹配、不认 ^ $：去掉开头的 ^ 和结尾没转义的 $"""
    text = pattern
    if text.startswith('^'):
        text = text[1:]
    if text.endswith('$') and not text.endswith('\\$'):
        text = text[:-1]
    return text


def deployment_lucene(name):
    return f'{lucene_escape(name)}-[a-z0-9]{{1,10}}-[a-z0-9]{{5}}'


def time_range(field, start_ms, end_ms):
    return {'range': {field: {'gte': int(start_ms), 'lt': int(end_ms), 'format': 'epoch_millis'}}}


class DslPlan:
    def __init__(self, bound):
        self.bound = bound
        self.fields = bound.schema
        info = self.fields.get(bound.time_field)
        if info is None:
            raise LogError(400, f'日志源 {bound.id} 的时间字段 {bound.time_field} 在索引 {bound.target} 里不存在，'
                                '请系统管理员检查配置', 'field_not_found')
        if info['kind'] != 'date':
            raise LogError(400, f'时间字段 {bound.time_field} 的类型是 {info["type"]}，不支持（要是 date / date_nanos）',
                           'invalid_request')
        self.time = bound.time_field

    def info(self, name):
        info = self.fields[name]
        if info.get('runtime'):
            self.bound.runtime_used.add(name)
        return info

    def exact(self, name):
        """精确匹配用的字段：text 用 .keyword 子字段，没有返回 None"""
        info = self.info(name)
        if info['kind'] == 'text':
            return info.get('keyword')
        return name

    def agg_field(self, name, purpose='分组'):
        info = self.info(name)
        if info['kind'] == 'text':
            if info.get('keyword'):
                return info['keyword']
            raise invalid(f'字段 {name} 是全文字段（text）而且没有 .keyword 子字段，不能{purpose}')
        if info.get('aggregatable') is False:
            raise invalid(f'字段 {name} 不能聚合（{info["type"]}），不能{purpose}')
        return name

    def _phrases(self, name, values):
        if len(values) == 1:
            return {'match_phrase': {name: str(values[0])}}
        return {'bool': {'should': [{'match_phrase': {name: str(value)}} for value in values],
                         'minimum_should_match': 1}}

    def clause(self, name, op, value):
        """单个条件 → ('filter' | 'must_not', 查询)"""
        info = self.info(name)
        kind = info['kind']
        if op == 'exists':
            return 'filter', {'exists': {'field': name}}
        if op == 'not_exists':
            return 'must_not', {'exists': {'field': name}}
        if op in ('contains', 'not_contains'):
            bucket = 'filter' if op == 'contains' else 'must_not'
            if kind == 'text':
                return bucket, {'match_phrase': {name: str(value)}}
            if kind == 'keyword':
                escaped = _WILDCARD.sub(r'\\\1', str(value))
                return bucket, {'wildcard': {name: {'value': f'*{escaped}*', 'case_insensitive': True}}}
            raise invalid(f'字段 {name} 的类型是 {info["type"]}，不能用 {op}')
        if op in ('gt', 'gte', 'lt', 'lte'):
            return 'filter', {'range': {name: {op: value}}}
        exact = self.exact(name)
        if op in ('eq', 'ne', 'in', 'not_in'):
            values = value if isinstance(value, list) else [value]
            bucket = 'filter' if op in ('eq', 'in') else 'must_not'
            if exact is None:
                return bucket, self._phrases(name, values)
            if op in ('eq', 'ne'):
                return bucket, {'term': {exact: values[0]}}
            return bucket, {'terms': {exact: values}}
        if op in ('prefix', 'regex'):
            if exact is None:
                raise invalid(f'字段 {name} 是全文字段（text）而且没有 .keyword 子字段，不能用 {op}')
            if op == 'prefix':
                return 'filter', {'prefix': {exact: {'value': str(value)}}}
            return 'filter', {'regexp': {exact: {'value': lucene_regex(str(value))}}}
        raise invalid(f'不支持的条件 {op}')

    def binding(self):
        """集群绑定条件 → (filter 列表, must_not 列表)"""
        filters, must_not = [], []
        for cond in self.bound.binding['conditions']:
            if cond['field'] not in self.fields:
                raise LogError(400, f'集群 {self.bound.env} 绑定日志源 {self.bound.id} 用的过滤字段 {cond["field"]} 在索引 '
                                    f'{self.bound.target} 里不存在，请系统管理员检查配置', 'field_not_found')
            value = cond['values'] if cond['op'] in ('in', 'not_in') else cond['values'][0]
            bucket, query = self.clause(cond['field'], cond['op'], value)
            (filters if bucket == 'filter' else must_not).append(query)
        return filters, must_not

    def base_filter(self, start_ms, end_ms):
        """时间 + 集群绑定（原生查询外面套的这一层）"""
        filters, must_not = self.binding()
        query = {'bool': {'filter': [time_range(self.time, start_ms, end_ms), *filters]}}
        if must_not:
            query['bool']['must_not'] = must_not
        return query

    def query(self, spec, start_ms, end_ms, extra=()):
        """完整的 bool 查询；extra 是 (bucket, 查询) 列表"""
        filters, must_not = self.binding()
        filters.insert(0, time_range(self.time, start_ms, end_ms))
        must = []
        if spec.get('level'):
            level = spec['level']
            exact = self.exact(level['field'])
            filters.append({'terms': {exact: level['values']}} if exact else self._phrases(level['field'],
                                                                                          level['values']))
        for item in spec.get('k8s') or []:
            if item['param'] == 'k8s_deployment':
                exact = self.exact(item['field'])
                if exact is None:
                    raise invalid(f'Pod 字段 {item["field"]} 是全文字段而且没有 .keyword 子字段，不能按 Deployment 匹配')
                filters.append({'regexp': {exact: {'value': deployment_lucene(item['value'])}}})
            else:
                filters.append(self.clause(item['field'], 'eq', item['value'])[1])
        for item in spec.get('filters') or []:
            bucket, query = self.clause(item['field'], item['op'], item['value'])
            (filters if bucket == 'filter' else must_not).append(query)
        message = self.bound.semantic('message')
        if spec.get('keyword'):
            must.append({'match_phrase': {message: spec['keyword']}})
        for text in spec.get('exclude') or []:
            must_not.append({'match_phrase': {message: text}})
        for bucket, query in extra:
            {'filter': filters, 'must': must, 'must_not': must_not}[bucket].append(query)
        result = {'filter': filters}
        if must:
            result['must'] = must
        if must_not:
            result['must_not'] = must_not
        return {'bool': result}

    def sort(self, order='desc'):
        return [{self.time: {'order': order, 'unmapped_type': 'date'}}]

    def time_fields(self):
        return [{'field': self.time, 'format': 'epoch_millis'}]

    def search_body(self, spec, start_ms, end_ms, fields, size, order='desc', extra=(), track_total=10000):
        """明细查询：_source 取选中的字段；fields 另取时间（epoch_millis）和选中的字段（运行时字段不在 _source 里）"""
        body = {'query': self.query(spec, start_ms, end_ms, extra), 'sort': self.sort(order), 'size': int(size),
                'track_total_hits': track_total}
        if fields:
            body['_source'] = [self.time, *[name for name in fields if name != self.time]]
            body['fields'] = self.time_fields() + [name for name in fields if name != self.time]
        else:
            body['fields'] = self.time_fields()
        return body

    def metric_agg(self, metric):
        op, name = metric['op'], metric.get('field')
        if op == 'uniq':
            return {'cardinality': {'field': self.agg_field(name, '算 uniq')}}
        info = self.info(name)
        if info['kind'] != 'number' and not (op in ('min', 'max') and info['kind'] == 'date'):
            raise invalid(f'字段 {name} 的类型是 {info["type"]}，不能算 {op}')
        if op in ('avg', 'min', 'max', 'sum'):
            return {op: {'field': name}}
        return {'percentiles': {'field': name, 'percents': [float(op[1:])]}}


def es_time(hit, time_field):
    """一条命中的时间（毫秒）：fields 的 epoch_millis > 排序值"""
    values = (hit.get('fields') or {}).get(time_field)
    if isinstance(values, list) and values:
        try:
            return int(float(values[0]))
        except (TypeError, ValueError):
            pass
    sort = hit.get('sort')
    if isinstance(sort, list) and sort and isinstance(sort[0], (int, float)):
        return int(sort[0])
    return None
