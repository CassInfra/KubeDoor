"""历史日志的纯函数：打码（mask.py）、截断和结果大小（common.py）、模式归类（patterns.py）、
SQL / DSL 构造（build_sql.py、build_dsl.py）、参数校验（params.py）。"""
import json

import pytest

from logsearch import common, config, params, patterns, schema
from logsearch.build_dsl import DslPlan, deployment_lucene, lucene_regex
from logsearch.build_sql import SqlPlan
from logsearch.clickhouse import Params, classify_error, encode_param, kind_of, unwrap
from logsearch.common import LogError
from logsearch.context import Ctx, default_output, fetch_list, make_row, text_regex, trace_of
from logsearch.mask import BUILTIN, MASK, Masker
from logsearch_fakes import CH_COLUMNS, ES_FIELDS, NOW, sample_config


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    monkeypatch.setenv('AI_ENCRYPTION_KEY', '33' * 32)
    monkeypatch.setattr(common, '_clock', lambda: NOW)


def bound(source='ingress', env='prod-a', cfg=None):
    cfg = config.merged(cfg or config.normalize_config(sample_config()))
    item = Ctx(cfg, env, None).bound(source)
    if item.type == 'clickhouse':
        item.schema = {name: {'name': name, 'type': type_, 'kind': kind_of(type_)} for name, type_ in CH_COLUMNS}
    else:
        item.schema = schema.parse_field_caps({'fields': ES_FIELDS})
    return item


# ---------------------------------------------------------------------------
# 打码
# ---------------------------------------------------------------------------
def test_mask_columns_and_text():
    masker = Masker(['host', 'Client.Card'], [r'card=(\d{4})', r'SECRET-\w+'])
    assert {'sign', 'passphrase', 'password', 'authorization'} <= set(BUILTIN)
    assert masker.value('password', 'x') == MASK and masker.value('db.password', 'x') == MASK
    assert masker.value('user_token', 'x') == MASK and masker.value('X-Api-Key', 'x') == MASK
    assert masker.value('HOST', 'a.example.com') == MASK and masker.value('client.card', '1') == MASK
    assert masker.value('tokens', 'x') == 'x' and masker.value('password', '') == '' and \
        masker.value('password', None) is None
    assert masker.value('n', 5) == 5
    text = masker.text('GET /a?token=abc&x=1&sign=zz password=p1, -Dspring.datasource.password=p2 --api-key=k1 '
                       'Authorization: Bearer abcdefghijkl jdbc:mysql://root:s3cret@10.0.0.1:3306/db '
                       'mysql root:pw123@db.example.com card=1234 id=SECRET-xyz Basic dXNlcjpwYXNz '
                       '{"password": "p3", "name":"n", "secret_key":"k2", "pin":12} {\\"access_token\\":\\"t1\\"}')
    for secret in ('abc&', 'zz', 'p1', 'p2', 'k1', 'abcdefghijkl', 's3cret', 'pw123', '1234', 'SECRET-xyz',
                   'dXNlcjpwYXNz', 'p3', 'k2', 't1'):
        assert secret not in text, secret
    assert 'x=1' in text and '"name":"n"' in text and '"pin":12' in text
    assert 'mysql://root:***@10.0.0.1' in text and 'card=***' in text and 'Authorization: *** ***' in text
    assert masker.text('x Bearer abcdefghijkl') == 'x Bearer ***'
    assert masker.text('session: 5 active, time: 10:30') == 'session: *** active, time: 10:30'
    assert masker.text('Basic auth failed') == 'Basic auth failed'  # 不是凭据
    assert masker.text('HTTP Basic Authentication failed') == 'HTTP Basic Authentication failed'
    assert masker.text('') == ''


def test_mask_nested_values():
    masker = Masker(['meta.ip'])
    value = masker.value('', {'kubernetes': {'pod': 'p', 'secret': 's'}, 'meta': {'ip': '1.2.3.4'},
                              'list': [{'password': 'x'}, 'token=1']})
    assert value == {'kubernetes': {'pod': 'p', 'secret': MASK}, 'meta': {'ip': MASK},
                     'list': [{'password': MASK}, 'token=***']}
    assert masker.row({'password': 'a', 'msg': 'pwd=1'}) == {'password': MASK, 'msg': 'pwd=***'}


# ---------------------------------------------------------------------------
# 截断、结果大小
# ---------------------------------------------------------------------------
def test_clip_text():
    assert common.clip_text('a' * 2000) == 'a' * 2000
    assert common.clip_text('a' * 2005) == 'a' * 2000 + '…（省略 5 字）'
    lines = [f'line {i}' for i in range(30)]
    lines[15] = 'Caused by: java.io.IOException: boom'
    stack = common.clip_text('\n'.join(lines))
    kept = stack.split('\n')
    assert kept[:10] == lines[:10] and kept[10] == '…（省略 5 行）'
    assert kept[11:14] == lines[15:18] and kept[14] == '…（省略 10 行）' and kept[15:] == lines[28:]
    huge = common.clip_text('\n'.join(['x' * 1000] * 20))
    assert len(huge) < 3100 and huge.endswith('字）')
    assert common.clip_text('a\n') == 'a\n' and common.clip_text(5) == 5
    assert common.clip_value({'a': ['b' * 3000]})['a'][0].endswith('（省略 1000 字）')
    assert common.first_line('  第一行\n第二行') == '第一行' and common.first_line(None) == ''


def test_fit_rows():
    data = {'rows': [{'v': 'x' * 1000} for i in range(100)], 'returned': 100, 'truncated': False, 'hint': ''}
    common.fit_rows(data, hint='缩小范围', limit=20000)
    assert data['truncated'] and data['hint'] == '缩小范围' and 10 <= data['returned'] < 20
    assert common.size_of(data) <= 20000
    single = {'rows': [{f'f{i}': 'y' * 3000 for i in range(30)}], 'returned': 1}
    common.fit_rows(single, limit=5000)
    assert single['returned'] == 1 and common.size_of(single) <= 5000 and single['truncated']
    small = {'rows': [1], 'returned': 1}
    assert common.fit_rows(small) == {'rows': [1], 'returned': 1}


def test_time_helpers():
    assert common.resolve_window({}) == (NOW - 3600, NOW)
    assert common.resolve_window({'lookback': '31d'}) == (NOW - 31 * 86400, NOW)
    assert common.resolve_window({'start': '2026-11-23 15:00', 'end': '2026-11-23T15:30:00+08:00'}) == \
        (NOW - 3600, NOW - 1800)
    for body, message in (({'lookback': '32d'}, '最长 31 天'), ({'lookback': '1w'}, '格式是数字加单位'),
                          ({'start': '2026-11-23 17:00'}, '开始时间要早于结束时间'),
                          ({'start': '2026-10-01 00:00'}, '时间范围最长 31 天'), ({'end': 'yesterday'}, '要写北京时间'),
                          ({'lookback': '0m'}, '要大于 0')):
        with pytest.raises(LogError) as error:
            common.resolve_window(body)
        assert message in error.value.message
    assert common.iso_ms(1_795_420_729_366) == '2026-11-23T15:58:49.366+08:00'
    assert common.tidy_number(3.0) == 3 and common.tidy_number(1.23456, 2) == 1.23 and \
        common.tidy_number(float('nan')) is None


# ---------------------------------------------------------------------------
# 模式归类
# ---------------------------------------------------------------------------
def test_normalize_patterns():
    assert patterns.normalize('ERROR order 12345 failed for 550e8400-e29b-41d4-a716-446655440000 at 10.0.0.1:8080 '
                              'trace 0af7651916cd43dd8448eb211c80319c user ab12cd34ef56gh took -3.5ms\nstack') == \
        'ERROR order <NUM> failed for <UUID> at <IP> trace <HEX> user <ID> took <NUM>ms'
    assert patterns.normalize('msg "' + 'x' * 40 + '"   end') == 'msg <STR> end'
    assert patterns.normalize('log4j v2 utf8') == 'log4j v2 utf8'
    assert patterns.normalize(None) == ''


def test_group_patterns():
    samples = []
    for index in range(6):  # 前 3 个词相同、只有一个位置不同 → 合并成 <*>
        samples.append((1000 + index, f'Timeout calling service svc-{"abcdef"[index]} after retry', 'ERROR'))
    for index in range(3):
        samples.append((2000 + index, f'Started job {index} ok', 'INFO'))
    samples.append((3000, '', None))
    result = patterns.group(samples, total=1000, top=10)
    first = result[0]
    assert first['pattern'] == 'Timeout calling service <*> after retry' and first['count'] == 6
    assert first['ratio'] == 0.6 and first['estimated'] == 600 and first['levels'] == {'ERROR': 6}
    assert first['example'] == 'Timeout calling service svc-a after retry'
    assert first['first'] == common.iso_ms(1000) and first['last'] == common.iso_ms(1005)
    assert result[1]['pattern'] == 'Started job <NUM> ok' and result[1]['count'] == 3
    assert result[2]['pattern'] == '（空正文）'
    assert patterns.group([], 0, 5) == [] and len(patterns.group(samples, None, 1)) == 1
    assert patterns.group(samples, None, 1)[0]['estimated'] is None


def test_group_patterns_does_not_snowball():
    """真实 Java 日志的教训：行首时间戳不算信息；合并出的 <*> 不算一样（否则同样长度的行全并成一类）；
    开头两个有信息的词不同不合并"""
    assert patterns.normalize('2026-11-23 15:22:45,660 ERROR http-nio-9209-exec-16 [a.B: 244] - boom') == \
        'ERROR http-nio-<NUM>-exec-<NUM> [a.B: <NUM>] - boom'
    assert patterns.normalize('[2026-11-23T07:20:46.308Z] WARN x') == 'WARN x'
    assert patterns.informative('[a.B:') and not patterns.informative('<NUM>]') and not patterns.informative('-')
    assert patterns.informative('执行错误') and not patterns.informative('<*>')
    lines = ['ERROR main [OrderService: 10] - order {} not found',
             'ERROR main [StockService: 20] - stock {} is negative',
             'ERROR main [PayService: 30] - pay {} timed out',
             'ERROR main [UserService: 40] - user {} locked out',
             'ERROR worker [OrderService: 10] - order {} not found']
    samples = [(index, f'2026-11-23 15:22:{index:02d},000 ' + line.format(index), 'ERROR')
               for index, line in enumerate(lines * 3)]
    result = patterns.group(samples, total=None, top=10)
    assert sorted(item['count'] for item in result) == [3, 3, 3, 3, 3]
    assert all('<*>' not in item['pattern'] for item in result)
    assert {item['pattern'] for item in result} >= {'ERROR main [OrderService: <NUM>] - order <NUM> not found',
                                                     'ERROR worker [OrderService: <NUM>] - order <NUM> not found'}
    # 同一类里只有个别词不同的照样合并
    similar = [(index, f'ERROR main [OrderService: 10] - order {name} not found in warehouse east', 'ERROR')
               for index, name in enumerate(['apple', 'pear', 'plum'])]
    [merged] = patterns.group(similar, total=None, top=10)
    assert merged['pattern'] == 'ERROR main [OrderService: <NUM>] - order <*> not found in warehouse east'


# ---------------------------------------------------------------------------
# ClickHouse 构造
# ---------------------------------------------------------------------------
def test_types_and_params():
    assert unwrap('LowCardinality(Nullable(String))') == 'String'
    assert [kind_of(t) for t in ('String', 'FixedString(4)', "Enum8('a' = 1)", 'UUID', 'IPv4', 'UInt64', 'Int8',
                                 'Float32', 'Decimal(10, 2)', 'Bool', 'Date', "DateTime64(3, 'UTC')",
                                 'Array(String)', 'Map(String, String)')] == \
        ['string', 'string', 'enum', 'uuid', 'ip', 'int', 'int', 'float', 'float', 'bool', 'date', 'date', 'other',
         'other']
    p = Params()
    assert p.add('x', 'String', 'a') == '{kd_x:String}' and p.add('kd_x', 'Int64', 2) == '{kd_x2:Int64}'
    assert encode_param('Array(String)', ["a'b", 'c\\d']) == "['a\\'b','c\\\\d']"
    assert encode_param('Array(Float64)', [1, 2.5]) == '[1.0,2.5]' and encode_param('UInt8', True) == '1'
    with pytest.raises(ValueError):
        encode_param('Map', 1)
    rendered = p.render('SELECT {kd_x:String}, {kd_x2:Int64}, {other:String}')
    assert rendered == "SELECT 'a', 2, {other:String}"


def test_sql_plan_conditions():
    plan = SqlPlan(bound())
    spec = params.parse_spec({'keyword': 'Timeout', 'level': 'error', 'k8s_deployment': 'web.api',
                              'k8s_namespace': 'shop', 'filters': [
                                  {'field': 'status', 'op': 'gte', 'value': 500},
                                  {'field': 'path', 'op': 'in', 'value': ['/a', '/b']},
                                  {'field': 'duration', 'op': 'gt', 'value': '1.5'},
                                  {'field': 'host', 'op': 'not_contains', 'value': 'health'},
                                  {'field': 'request_id', 'op': 'exists'},
                                  {'field': 'pod', 'op': 'regex', 'value': '^web'},
                                  {'field': 'method', 'op': 'prefix', 'value': 'PO'},
                              ]}, plan.bound, params.FILTER_KEYS)
    where = plan.where(spec, 1000, 2000)
    assert '`start_time` >= fromUnixTimestamp64Milli({kd_from:Int64})' in where
    assert '`start_time` < fromUnixTimestamp64Milli({kd_to:Int64})' in where
    assert '`env` IN {kd_vs:Array(String)}' in where  # 集群绑定条件一定在
    assert 'toString(`level`) IN {kd_lv:Array(String)}' in where
    assert 'match(toString(`pod`), {kd_dep:String})' in where and '`namespace` = {kd_v:String}' in where
    assert '`status` >= {kd_n:Int64}' in where and '`duration` > {kd_n2:Float64}' in where
    assert '`path` IN {kd_vs2:Array(String)}' in where
    assert 'ifNull(positionCaseInsensitive(toString(`host`), {kd_c:String}), 0) = 0' in where
    assert "(isNotNull(`request_id`) AND toString(`request_id`) != '')" in where
    assert 'positionCaseInsensitive(toString(`message`), {kd_kw:String}) > 0' in where
    values = plan.params.url_params()
    assert values['param_kd_vs'] == "['prod-a','prod-a2']" and values['param_kd_lv'] == "['ERROR','FATAL']"
    assert values['param_kd_dep'] == r'^web\\.api-[a-z0-9]{1,10}-[a-z0-9]{5}$'  # TSV 转义后的反斜杠
    assert values['param_kd_from'] == '1000' and values['param_kd_n'] == '500' and values['param_kd_kw'] == 'Timeout'
    sql = plan.select_rows(spec, 1000, 2000, ['path', 'status'], 50, 'asc')
    assert sql.startswith('SELECT toUnixTimestamp64Milli(toDateTime64(`start_time`, 3)) AS kd_ms, `path`, `status`')
    assert 'FROM `logs`.`access`' in sql and sql.endswith('ORDER BY `start_time` ASC\nLIMIT {kd_limit:UInt32}')
    assert plan.count(spec, 1, 2).startswith('SELECT count() AS kd_count\nFROM `logs`.`access`')


@pytest.mark.parametrize('field, op, value, expected', [
    ('status', 'eq', '200', '`status` = {kd_v:Int64}'),
    ('status', 'in', [200, 18446744073709551615], '`status` IN {kd_vs:Array(UInt64)}'),
    ('duration', 'ne', 1.5, '`duration` != {kd_v:Float64}'),
    ('env', 'not_in', ['a'], '`env` NOT IN {kd_vs:Array(String)}'),
    ('path', 'gt', 'm', 'toString(`path`) > {kd_s:String}'),
    ('path', 'lte', 5, 'toFloat64OrNull(`path`) <= {kd_n:Float64}'),
    ('request_id', 'not_exists', None, "(isNull(`request_id`) OR toString(`request_id`) = '')"),
    ('host', 'contains', 'a', 'positionCaseInsensitive(toString(`host`), {kd_c:String}) > 0'),
])
def test_sql_compare(field, op, value, expected):
    plan = SqlPlan(bound())
    assert plan.compare(field, op, value) == expected


def test_sql_compare_types():
    item = bound()
    item.schema.update({'ok': {'name': 'ok', 'type': 'Bool', 'kind': 'bool'},
                        'uid': {'name': 'uid', 'type': 'UUID', 'kind': 'uuid'},
                        'day': {'name': 'day', 'type': 'Date', 'kind': 'date'}})
    plan = SqlPlan(item)
    assert plan.compare('ok', 'eq', 'true') == 'toUInt8(`ok`) = {kd_v:UInt8}'
    assert plan.compare('uid', 'eq', 'x') == 'toString(`uid`) = {kd_v2:String}'
    for op, value, message in (('eq', 'x', '是时间类型，不支持按值过滤'), ('gt', 'x', '不支持按值比较')):
        with pytest.raises(LogError, match=message):
            plan.compare('day', op, value)
    for field, value, message in (('status', 'abc', '要是整数'), ('duration', 'x', '要是数字'), ('ok', 'maybe', 'true / false')):
        with pytest.raises(LogError, match=message):
            plan.compare(field, 'eq', value)
    assert plan.metric_expr({'op': 'p95', 'field': 'duration'}) == 'quantile(0.95)(`duration`)'
    assert plan.metric_expr({'op': 'avg', 'field': 'path'}) == 'avg(toFloat64OrNull(`path`))'
    assert plan.metric_expr({'op': 'max', 'field': 'day'}) == 'max(`day`)'
    assert plan.metric_expr({'op': 'uniq', 'field': 'pod'}) == 'uniq(`pod`)'
    with pytest.raises(LogError, match='不能算 sum'):
        plan.metric_expr({'op': 'sum', 'field': 'uid'})


def test_sql_plan_time_field_checks():
    item = bound()
    item.schema['start_time'] = {'name': 'start_time', 'type': 'String', 'kind': 'string'}
    with pytest.raises(LogError, match='时间字段 start_time 的类型是 String，不支持'):
        SqlPlan(item)
    del item.schema['start_time']
    with pytest.raises(LogError) as error:
        SqlPlan(item)
    assert error.value.code == 'field_not_found'
    item = bound()
    del item.schema['env']
    with pytest.raises(LogError, match='用的过滤字段 env 在表 logs.access 里不存在'):
        SqlPlan(item).binding_conds()


# ---------------------------------------------------------------------------
# Elasticsearch 构造
# ---------------------------------------------------------------------------
def test_dsl_query_structure():
    item = bound('app')
    plan = DslPlan(item)
    spec = params.parse_spec({'keyword': 'NullPointer', 'level': 'warn', 'k8s_deployment': 'order-api',
                              'filters': [{'field': 'message', 'op': 'eq', 'value': 'x'},
                                          {'field': 'path', 'op': 'in', 'value': ['/a', '/b']},
                                          {'field': 'appname', 'op': 'contains', 'value': 'a*b'},
                                          {'field': 'latency', 'op': 'gte', 'value': 100},
                                          {'field': 'trace_id', 'op': 'not_exists'},
                                          {'field': 'podname', 'op': 'regex', 'value': '^web-.*$'},
                                          {'field': 'appname', 'op': 'ne', 'value': 'x'}]},
                             item, params.FILTER_KEYS)
    query = plan.query(spec, 1000, 2000)['bool']
    assert query['filter'][0] == {'range': {'@timestamp': {'gte': 1000, 'lt': 2000, 'format': 'epoch_millis'}}}
    assert query['filter'][1] == {'term': {'env': 'prod-a'}}  # 集群绑定条件
    assert {'terms': {'level': ['warn', 'WARN', 'Warn']}} in query['filter']
    assert {'regexp': {'podname': {'value': 'order-api-[a-z0-9]{1,10}-[a-z0-9]{5}'}}} in query['filter']
    assert {'term': {'message.keyword': 'x'}} in query['filter']  # text 用 .keyword
    assert {'match_phrase': {'path': '/a'}} in query['filter'][-4]['bool']['should']  # 没有 .keyword：降级
    assert {'wildcard': {'appname': {'value': '*a\\*b*', 'case_insensitive': True}}} in query['filter']
    assert {'range': {'latency': {'gte': 100}}} in query['filter']
    assert {'regexp': {'podname': {'value': 'web-.*'}}} in query['filter']
    assert query['must'] == [{'match_phrase': {'message': 'NullPointer'}}]
    assert {'exists': {'field': 'trace_id'}} in query['must_not'] and {'term': {'appname': 'x'}} in query['must_not']
    body = plan.search_body(spec, 1000, 2000, ['message', 'trace_id'], 10)
    assert body['_source'] == ['@timestamp', 'message', 'trace_id'] and body['size'] == 10 and body['track_total_hits'] == 10000
    assert body['fields'][0] == {'field': '@timestamp', 'format': 'epoch_millis'}
    assert body['sort'] == [{'@timestamp': {'order': 'desc', 'unmapped_type': 'date'}}]


def test_dsl_errors_and_helpers():
    item = bound('app')
    plan = DslPlan(item)
    for op in ('prefix', 'regex'):
        with pytest.raises(LogError, match=f'不能用 {op}'):
            plan.clause('path', op, 'x')
    with pytest.raises(LogError, match='不能用 contains'):
        plan.clause('latency', 'contains', '1')
    with pytest.raises(LogError, match='不能分组'):
        plan.agg_field('path')
    assert plan.agg_field('message') == 'message.keyword'
    with pytest.raises(LogError, match='不能算 avg'):
        plan.metric_agg({'op': 'avg', 'field': 'appname'})
    assert plan.metric_agg({'op': 'p99', 'field': 'latency'}) == {'percentiles': {'field': 'latency',
                                                                                  'percents': [99.0]}}
    assert plan.metric_agg({'op': 'uniq', 'field': 'message'}) == {'cardinality': {'field': 'message.keyword'}}
    assert plan.metric_agg({'op': 'max', 'field': '@timestamp'}) == {'max': {'field': '@timestamp'}}
    assert lucene_regex('^a.*$') == 'a.*' and lucene_regex('a\\$') == 'a\\$'
    assert deployment_lucene('a.b') == 'a\\.b-[a-z0-9]{1,10}-[a-z0-9]{5}'
    base = plan.base_filter(1, 2)
    assert base == {'bool': {'filter': [{'range': {'@timestamp': {'gte': 1, 'lt': 2, 'format': 'epoch_millis'}}},
                                        {'term': {'env': 'prod-a'}}]}}
    item.schema['@timestamp']['kind'] = 'keyword'
    with pytest.raises(LogError, match='要是 date / date_nanos'):
        DslPlan(item)


def test_field_caps_parsing():
    fields = schema.parse_field_caps({'fields': {
        '_id': {'_id': {'type': '_id'}}, 'msg': {'text': {'type': 'text', 'searchable': True}},
        'msg.keyword': {'keyword': {'type': 'keyword', 'aggregatable': True}},
        'code': {'long': {'type': 'long'}, 'keyword': {'type': 'keyword'}},
        'n': {'long': {'type': 'long'}, 'integer': {'type': 'integer'}}, 'u': {'unmapped': {}},
        'obj': {'object': {'type': 'object'}}, 'flag': {'boolean': {'type': 'boolean'}},
        'addr': {'ip': {'type': 'ip'}}, 'geo': {'geo_point': {'type': 'geo_point'}}}})
    assert '_id' not in fields and 'u' not in fields
    assert fields['msg']['keyword'] == 'msg.keyword' and fields['msg']['kind'] == 'text'
    assert fields['code']['type'] == 'keyword|long' and fields['code']['kind'] == 'keyword'
    assert [fields[name]['kind'] for name in ('n', 'obj', 'flag', 'addr', 'geo')] == \
        ['number', 'object', 'bool', 'ip', 'other']
    assert schema.similar({'status_code': 1, 'path': 2, 'response_status': 3}, 'status')[:2] == \
        ['status_code', 'response_status']
    with pytest.raises(LogError) as error:
        schema.require({'status_code': 1}, 'statuz', '', 'ingress')
    assert error.value.code == 'field_not_found' and '相近的字段：status_code' in error.value.message


# ---------------------------------------------------------------------------
# 参数
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('body, message', [
    ({'namespace': 'a'}, 'namespace 请用 k8s_namespace'),
    ({'query': 'x'}, 'query 请用 keyword'),
    ({'size': 1}, 'size 请用 limit'),
    ({'trace_id': 'x'}, 'trace_id 请用 log_correlate 的 key + value'),
    ({'keyword': 'a\nb'}, 'keyword 不能换行'),
    ({'keyword': 'x' * 501}, '最长 500'),
    ({'filters': {}}, 'filters 要是数组'),
    ({'filters': [{}] * 11}, 'filters 最多 10 项'),
    ({'filters': ['x']}, 'filters 第 1 项要是对象'),
    ({'filters': [{'field': 'status', 'op': 'eq', 'value': 1, 'x': 1}]}, '不支持的键：x'),
    ({'filters': [{'field': 'a b', 'op': 'eq', 'value': 1}]}, '不是合法的字段名'),
    ({'filters': [{'field': 'statuz', 'op': 'eq', 'value': 1}]}, '相近的字段：status'),
    ({'filters': [{'field': 'status', 'op': 'like', 'value': 1}]}, 'op 只能是'),
    ({'filters': [{'field': 'status', 'op': 'exists', 'value': 1}]}, 'exists 不带 value'),
    ({'filters': [{'field': 'status', 'op': 'in', 'value': 1}]}, 'in 的 value 要是 1~100 项的数组'),
    ({'filters': [{'field': 'status', 'op': 'in', 'value': [True]}]}, '要是字符串或数字'),
    ({'filters': [{'field': 'path', 'op': 'regex', 'value': 'x' * 201}]}, '最长 200'),
    ({'filters': [{'field': 'path', 'op': 'regex', 'value': 5}]}, 'regex 的 value 要是正则表达式字符串'),
    ({'filters': [{'field': 'path', 'op': 'eq'}]}, 'eq 要带 value'),
    ({'filters': [{'field': 'path', 'op': 'eq', 'value': float('nan')}]}, '不是合法的数字'),
])
def test_parse_spec_errors(body, message):
    with pytest.raises(LogError) as error:
        params.parse_spec(body, bound(), params.FILTER_KEYS)
    assert message in error.value.message


def test_parse_spec_requires_semantic_fields():
    cfg = sample_config()
    cfg['sources'][0]['fields'] = {}
    cfg['sources'][0]['correlations'] = [{'key': 'request_id', 'field': 'request_id'}]
    item = bound(cfg=config.normalize_config(cfg))
    for body, message in (({'keyword': 'x'}, '没有配置正文字段，不能按 keyword 搜正文'),
                          ({'level': 'error'}, '没有配置级别字段'),
                          ({'k8s_pod': 'p'}, '没有配置 Pod 字段，不能按 Pod 过滤；用 filters'),
                          ({'k8s_deployment': 'd'}, '不能按 Deployment 过滤')):
        with pytest.raises(LogError) as error:
            params.parse_spec(body, item, params.FILTER_KEYS)
        assert message in error.value.message and error.value.code == 'invalid_request'
    item = bound()
    del item.schema['message']
    with pytest.raises(LogError) as error:
        params.parse_spec({'keyword': 'x'}, item, params.FILTER_KEYS)
    assert error.value.code == 'field_not_found'


def test_level_values_and_describe():
    item = bound()
    assert params.level_values(item, 'ERROR') == ['ERROR', 'FATAL']  # 配置的取值
    assert params.level_values(item, 'warn') == ['warn', 'WARN', 'Warn']
    assert params.level_values(item, '50') == ['50']
    spec = params.parse_spec({'keyword': 'k', 'level': 'error', 'k8s_namespace': 'ns', 'k8s_pod': 'p',
                              'k8s_deployment': 'd', 'filters': [
                                  {'field': 'status', 'op': 'gte', 'value': 500},
                                  {'field': 'path', 'op': 'not_in', 'value': list(range(12))},
                                  {'field': 'host', 'op': 'prefix', 'value': 'a'},
                                  {'field': 'pod', 'op': 'regex', 'value': 'x'},
                                  {'field': 'env', 'op': 'contains', 'value': 'y'},
                                  {'field': 'env', 'op': 'not_contains', 'value': 'z'},
                                  {'field': 'env', 'op': 'exists'}, {'field': 'env', 'op': 'not_exists'},
                                  {'field': 'env', 'op': 'in', 'value': ['q']}]},
                             item, params.FILTER_KEYS)
    spec['exclude'] = ['noise']
    assert params.describe(item, spec) == [
        '集群绑定：env ∈ {prod-a, prod-a2}', '正文包含「k」', '级别 = error（取值 ERROR、FATAL）', '命名空间 = ns', 'Pod = p',
        'Deployment = d（按 Pod 名匹配）', '状态码 ≥ 500', '路径 ∉ {0, 1, 2, 3, 4, 5, 6, 7, 8, 9} 等 12 个',
        '域名 / Host 以「a」开头', 'Pod 匹配正则 x', 'env 包含「y」', 'env 不包含「z」', 'env 有值', 'env 没有值', 'env ∈ {q}',
        '正文不包含「noise」']
    assert params.binding_text({'conditions': []}) == ['集群绑定：整个表 / 索引都属于这个集群']
    assert params.parse_exclude({'exclude': [' a ', 'a', 'b']}) == ['a', 'b']
    with pytest.raises(LogError):
        params.parse_exclude({'exclude': ['x' * 201]})


# ---------------------------------------------------------------------------
# 行：默认字段、链路 ID
# ---------------------------------------------------------------------------
def test_rows_trace_id_and_default_fields():
    item = bound()
    assert default_output(item) == ['message', 'level', 'namespace', 'pod', 'upstream_cluster', 'status', 'duration',
                                    'method', 'path', 'host', 'request_id']
    assert fetch_list(item, ['path']) == ['path', 'message']  # 链路 ID 按正文写法提取，要取正文
    values = {'path': '/a', 'message': 'ok TID:0af7651916cd43dd8448eb211c80319c done', 'host': 'h.example.com',
              'user_token': 'secret-value'}
    row = make_row(item, 1_795_420_729_366, values, ['path', 'host', 'user_token'])
    assert row == {'@time': '2026-11-23T15:58:49.366+08:00', 'path': '/a', 'host': MASK, 'user_token': MASK,
                   'trace_id': '0af7651916cd43dd8448eb211c80319c'}
    assert trace_of(item, {'message': 'TID:xyz'}) is None  # 不符合取值格式
    everything = make_row(item, None, {'start_time': 'x', 'a': 'b' * 3000}, [], all_fields=True)
    assert everything['@time'] is None and 'start_time' not in everything and everything['a'].endswith('（省略 1000 字）')
    app = bound('app')
    assert trace_of(app, {'trace_id': 'abc'}) == 'abc' and trace_of(app, {'trace_id': {'x': 1}}) is None
    assert text_regex('"traceId":"{value}"').search('{"traceId":"a-b","x":1}').group(1) == 'a-b'
    item.source['default_fields'] = ['path', 'start_time', 'gone']
    assert default_output(item) == ['path']


def test_classify_clickhouse_errors():
    cases = [
        (500, 'Code: 158. DB::Exception: Limit for rows (controlled by max_rows_to_read)', 'too_expensive', 400),
        (500, 'Code: 307. DB::Exception: Limit for bytes', 'too_expensive', 400),
        (500, 'Code: 159. DB::Exception: Timeout exceeded', 'timeout', 504),
        (516, 'Code: 516. DB::Exception: Authentication failed', 'datasource_unavailable', 502),
        (500, 'Code: 210. DB::Exception: Connection refused', 'datasource_unavailable', 502),
        (404, 'Code: 60. DB::Exception: Table logs.x does not exist', 'query_rejected', 400),
        (502, '<html>Bad Gateway</html>', 'datasource_unavailable', 502),
        (400, 'bad request without code', 'query_rejected', 400),
    ]
    for status, text, code, http in cases:
        error = classify_error(status, text)
        assert (error.code, error.status) == (code, http), text
    assert classify_error(500, 'x', {'X-ClickHouse-Exception-Code': '158'}).code == 'too_expensive'
    assert len(classify_error(400, 'Code: 62. ' + 'x' * 2000).message) < 600


def test_masker_used_by_scan_and_text_ids():
    from logsearch import scan

    ids = scan.text_ids(['a TID:0af7651916cd43dd8448eb211c80319c b', 'TID: 0af7651916cd43dd8448eb211c80319c',
                         '{"requestId":"550e8400-e29b-41d4-a716-446655440000"}', None])
    assert ids[0] == {'text': 'TID:{value}', 'shape': 'hex32', 'count': 2}
    assert ids[1] == {'text': '"requestId":"{value}"', 'shape': 'uuid', 'count': 1}
    assert json.dumps(ids)
