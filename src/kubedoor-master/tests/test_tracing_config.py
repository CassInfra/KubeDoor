"""链路追踪：配置校验和公开格式（tracing/config.py）、字段映射和集群取值（fields.py）、打码（mask.py）、
时间参数（common.py）。"""
import json
import re
from datetime import datetime

import pytest

from auth.crypto import decrypt_secret
from tracing import common, config, fields
from tracing.mask import Masker


@pytest.fixture(autouse=True)
def encryption_key(monkeypatch):
    monkeypatch.setenv('AI_ENCRYPTION_KEY', '11' * 32)


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
def test_defaults_and_merged_fill_missing_fields():
    cfg = config.merged(None)
    assert cfg['enabled'] is False and cfg['timeout'] == 30 and cfg['clickhouse']['timeout'] == 20
    assert cfg['clickhouse']['database'] == 'jaeger' and cfg['clickhouse']['dim_table'] == 'trace_search_dim'
    assert list(cfg['fields']) == list(fields.FIELD_KEYS)
    # 存的映射缺项时用默认补（新版本加的字段），不认识的丢掉
    cfg = config.merged({'fields': {'status_code': ['code'], 'gone': ['x']}, 'clickhouse': {'url': 'http://ch:8123'}})
    assert cfg['fields']['status_code'] == ['code'] and cfg['fields']['method'] == fields.FIELD_DEFAULTS['method']
    assert 'gone' not in cfg['fields'] and cfg['clickhouse']['url'] == 'http://ch:8123'
    assert cfg['clickhouse']['table'] == 'trace_search'
    # 默认值不会被改动（深拷贝）
    cfg['fields']['route'].append('x')
    assert config.DEFAULTS['fields']['route'] == ['http.route']


def test_normalize_config_full():
    cfg = config.normalize_config({
        'enabled': True, 'url': ' http://10.113.0.108:31686/ ', 'ui_url': 'https://jaeger.example.com/ui/',
        'headers': [{'name': 'Authorization', 'value': 'Bearer t0ken'}], 'timeout': '60', 'tls_verify': False,
        'cluster_key': 'k8sname', 'fields': {'status_code': [' http.response.status_code ', 'http.status_code',
                                                             'http.status_code']},
        'custom_fields': [{'key': 'biz.order_no', 'label': ' 订单号 '}], 'mask_keys': ['biz.card', 'BIZ.CARD', 'x'],
        'clickhouse': {'enabled': True, 'url': 'http://10.113.0.88:8123', 'username': 'reader', 'password': 'p@ss',
                       'dim_table': '', 'timeout': 30},
        'unknown': 1,
    })
    assert cfg['url'] == 'http://10.113.0.108:31686' and cfg['ui_url'] == 'https://jaeger.example.com/ui'
    assert cfg['timeout'] == 60 and cfg['tls_verify'] is False and cfg['cluster_key'] == 'k8sname'
    assert cfg['fields']['status_code'] == ['http.response.status_code', 'http.status_code']  # 去空白、去重
    assert cfg['fields']['method'] == fields.FIELD_DEFAULTS['method']  # 没传的用原值（默认）
    assert cfg['custom_fields'] == [{'key': 'biz.order_no', 'label': '订单号'}]
    assert cfg['mask_keys'] == ['biz.card', 'x']  # 不区分大小写去重
    assert 'unknown' not in cfg
    assert decrypt_secret(cfg['headers'][0]['value']) == 'Bearer t0ken'
    ch = cfg['clickhouse']
    assert ch['password'].startswith('v1:') and decrypt_secret(ch['password']) == 'p@ss'
    assert ch['dim_table'] == '' and ch['database'] == 'jaeger' and ch['timeout'] == 30 and ch['enabled'] is True


def test_partial_update_keeps_saved_values_and_secrets():
    first = config.normalize_config({
        'enabled': True, 'url': 'http://jaeger:16686', 'headers': [{'name': 'X-Token', 'value': 'abc'}],
        'clickhouse': {'url': 'http://ch:8123', 'password': 'secret'}})
    # 右上角开关：只传 enabled
    toggled = config.normalize_config({'enabled': False}, first)
    assert toggled == {**first, 'enabled': False}
    # clickhouse 只传了一部分：其余沿用（密码留空沿用）
    second = config.normalize_config({'clickhouse': {'enabled': True, 'password': '', 'table': 'spans_search'}}, first)
    assert second['clickhouse']['password'] == first['clickhouse']['password']
    assert second['clickhouse']['url'] == 'http://ch:8123' and second['clickhouse']['table'] == 'spans_search'
    # 请求头的值留空沿用
    third = config.normalize_config({'headers': [{'name': 'x-token', 'value': ''}]}, first)
    assert third['headers'] == [{'name': 'x-token', 'value': first['headers'][0]['value']}]


def test_clear_clickhouse_password():
    first = config.normalize_config({'clickhouse': {'url': 'http://ch:8123', 'password': 'secret'}})
    # 「清除」：清空已保存的密码，忽略同时传来的 password
    cleared = config.normalize_config({'clickhouse': {'clear_password': True, 'password': 'new'}}, first)
    assert cleared['clickhouse']['password'] == '' and 'clear_password' not in cleared['clickhouse']
    assert config.public_config(cleared)['clickhouse']['has_password'] is False
    assert config.changed_items(first, cleared) == ['clickhouse.password']
    # clear_password 不是 true 时照常（留空沿用）
    kept = config.normalize_config({'clickhouse': {'clear_password': False, 'password': ''}}, first)
    assert kept['clickhouse']['password'] == first['clickhouse']['password']


@pytest.mark.parametrize('raw, message', [
    ('x', '配置格式不正确'),
    ({'enabled': True}, '启用时必须填写 Jaeger 查询地址'),
    ({'url': 'ftp://jaeger'}, 'Jaeger 查询地址要以 http:// 或 https:// 开头'),
    ({'url': 'http://jaeger:16686/?x=1'}, '不能带查询参数和 #'),
    ({'url': 'http://jaeger:16686/#a'}, '不能带查询参数和 #'),
    ({'ui_url': 'javascript:alert(1)'}, 'Jaeger UI 地址'),
    ({'url': 'http://j/' + 'a' * 2100}, '最长 2048'),
    ({'timeout': 4}, '取 5~120 秒'),
    ({'timeout': 121}, '取 5~120 秒'),
    ({'timeout': 'x'}, '必须是整数'),
    ({'timeout': True}, '必须是整数'),
    ({'timeout': 7.5}, '必须是整数'),
    ({'cluster_key': 'k8s name'}, '集群字段只能用'),
    ({'cluster_key': 'x' * 129}, '集群字段只能用'),
    ({'fields': []}, '字段映射格式不正确'),
    ({'fields': {'nope': []}}, '字段映射里没有 nope'),
    ({'fields': {'status_code': 'a'}}, '要是属性名列表'),
    ({'fields': {'status_code': ['a b']}}, '不能有空格'),
    ({'fields': {'status_code': ['']}}, '不能为空'),
    ({'fields': {'status_code': ['x' * 201]}}, '最长 200'),
    ({'fields': {'status_code': ['a', 'b', 'c', 'd', 'e', 'f']}}, '最多 5 个'),
    ({'custom_fields': [{'key': 'a', 'label': ''}]}, '说明不能为空'),
    ({'custom_fields': [{'key': 'a', 'label': 'x' * 51}]}, '最长 50'),
    ({'custom_fields': [{'key': 'a', 'label': '甲'}, {'key': 'a', 'label': '乙'}]}, '重复'),
    ({'custom_fields': [{'key': f'k{i}', 'label': 'x'} for i in range(51)]}, '最多 50'),
    ({'custom_fields': 'a'}, '业务字段格式不正确'),
    ({'mask_keys': ['ok', 'a b']}, '打码字段不能为空'),
    ({'mask_keys': ['x' * 129]}, '最长 128'),
    ({'mask_keys': [f'k{i}' for i in range(51)]}, '最多 50'),
    ({'clickhouse': 'x'}, 'ClickHouse 配置格式不正确'),
    ({'clickhouse': {'enabled': True}}, '启用 ClickHouse 加速时必须填写 HTTP 地址'),
    ({'clickhouse': {'url': 'mysql://x'}}, 'ClickHouse HTTP 地址'),
    ({'clickhouse': {'database': 'jaeger;drop'}}, 'ClickHouse 库名只能用'),
    ({'clickhouse': {'table': '1abc'}}, 'ClickHouse 搜索表名只能用'),
    ({'clickhouse': {'table': ''}}, 'ClickHouse 搜索表名只能用'),
    ({'clickhouse': {'dim_table': 'a`b'}}, 'ClickHouse 汇总表名只能用'),
    ({'clickhouse': {'timeout': 3}}, 'ClickHouse 超时时间取 5~120 秒'),
    ({'clickhouse': {'username': 'a b'}}, 'ClickHouse 账号不能有空格'),
    ({'clickhouse': {'password': 'a\nb'}}, 'ClickHouse 密码不能换行'),
    ({'headers': [{'name': 'Host', 'value': 'x'}]}, '由系统设置'),
])
def test_normalize_config_rejects(raw, message):
    with pytest.raises(ValueError, match=re.escape(message)):
        config.normalize_config(raw)


def test_public_config_hides_secrets():
    cfg = config.normalize_config({
        'url': 'http://jaeger:16686', 'headers': [{'name': 'Authorization', 'value': 'Bearer s3cret'}],
        'clickhouse': {'url': 'http://ch:8123', 'username': 'reader', 'password': 'ch-s3cret'}})
    public = config.public_config(cfg)
    text = json.dumps(public)
    assert 's3cret' not in text and cfg['headers'][0]['value'] not in text and cfg['clickhouse']['password'] not in text
    assert public['headers'] == [{'name': 'Authorization', 'has_value': True}]
    assert public['clickhouse']['has_password'] is True and 'password' not in public['clickhouse']
    assert public['clickhouse']['username'] == 'reader'
    assert config.public_config(None)['clickhouse']['has_password'] is False


def test_changed_items_lists_names_only():
    first = config.normalize_config({'url': 'http://a:1', 'headers': [{'name': 'X-A', 'value': 'v1'}],
                                     'clickhouse': {'password': 'p1'}})
    same = config.normalize_config({'headers': [{'name': 'X-A', 'value': 'v1'}], 'clickhouse': {'password': 'p1'}},
                                   first)
    assert config.changed_items(first, same) == []  # 重新加密了，但值没变
    changed = config.normalize_config({'url': 'http://b:1', 'headers': [{'name': 'X-A', 'value': 'v2'}],
                                       'cluster_key': 'k8sname', 'clickhouse': {'password': 'p2', 'timeout': 9}},
                                      first)
    assert config.changed_items(first, changed) == ['url', 'headers', 'cluster_key', 'clickhouse.password',
                                                    'clickhouse.timeout']
    assert config.changed_items({}, config.normalize_config({'enabled': False})) == []


def test_trace_url_and_fingerprints():
    cfg = config.merged({'url': 'http://jaeger:16686'})
    assert config.trace_url(cfg, 'abc') == 'http://jaeger:16686/trace/abc'
    cfg['ui_url'] = 'https://ui.example.com/jaeger'
    assert config.trace_url(cfg, 'abc') == 'https://ui.example.com/jaeger/trace/abc'
    assert config.trace_url(config.merged({}), 'abc') == ''
    other = config.merged({'url': 'http://jaeger:16687'})
    assert config.jaeger_fingerprint(cfg) != config.jaeger_fingerprint(other)
    assert config.clickhouse_fingerprint(cfg) == config.clickhouse_fingerprint(config.merged({'url': 'x'}))


class FakeStore:
    def __init__(self, value=None, broken=False):
        self.value, self.broken = value, broken

    async def get_setting(self, key, default=None):
        assert key == config.SETTING_KEY
        if self.broken:
            raise RuntimeError('db down')
        return self.value if self.value is not None else default


@pytest.mark.asyncio
async def test_load_config(monkeypatch):
    monkeypatch.setattr(config, 'store', FakeStore({'url': 'http://jaeger:16686'}))
    assert (await config.load_config())['url'] == 'http://jaeger:16686'
    monkeypatch.setattr(config, 'store', FakeStore(broken=True))
    with pytest.raises(common.TracingError) as info:
        await config.load_config()
    assert info.value.status == 503 and info.value.code == 'config_unavailable'


# ---------------------------------------------------------------------------
# 集群取值
# ---------------------------------------------------------------------------
def test_cluster_values_and_lookup():
    aliases = {'prod': ['蓬莱', ' ', 'prod', 'shared'], 'test': ['shared'], 'kunlun': []}
    assert fields.cluster_values('prod', aliases) == ['prod', '蓬莱', 'shared']
    assert fields.cluster_values('new', aliases) == ['new']
    lookup = fields.cluster_lookup(aliases)
    assert fields.resolve_cluster('蓬莱', lookup) == {'value': '蓬莱', 'env': 'prod'}
    assert fields.resolve_cluster('kunlun', lookup) == {'value': 'kunlun', 'env': 'kunlun'}
    assert fields.resolve_cluster('other', lookup) == {'value': 'other', 'env': None}
    # 一个取值对应多个集群：有歧义，显示原值
    assert fields.resolve_cluster('shared', lookup) == {'value': 'shared', 'env': None, 'ambiguous': True}
    assert fields.resolve_cluster('', lookup) == {'value': '', 'env': None}


# ---------------------------------------------------------------------------
# 打码
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('key, value, expected', [
    ('http.request.header.authorization', 'Bearer x', '***'),
    ('Authorization', 'x', '***'),
    ('db.password', 'x', '***'),
    ('my_token', 1, '***'),
    ('x-api-key', 'k', '***'),
    ('tokens', 'keep', 'keep'),
    ('passwordless', 'keep', 'keep'),
    ('biz.card', '6222', '***'),  # 配置里加的
    ('url.full', 'https://u:p@a.com/x?token=abc&a=1&Password=2#frag', 'https://u:***@a.com/x?token=***&a=1&Password=***#frag'),
    ('url.query', 'a=1&access_token=xyz&b=2', 'a=1&access_token=***&b=2'),
    ('http.target', '/a?pwd=1&q=ok', '/a?pwd=***&q=ok'),
    ('db.connection_string', 'jdbc:mysql://root:pw@db:3306/x?user=a&password=b',
     'jdbc:mysql://root:***@db:3306/x?user=a&password=***'),
    ('db.connection_string', 'Server=db;User Id=sa;Password=pw;', 'Server=db;User Id=sa;Password=***;'),
    ('http.url', 'http://a.com/x', 'http://a.com/x'),
    ('url.path', '/token=1', '/token=1'),  # 不是 URL 类的键：原样
    ('process.command_line', 'java -Dspring.datasource.password=s3 -Dpassword=x --token=t -Xmx1g',
     'java -Dspring.datasource.password=*** -Dpassword=*** --token=*** -Xmx1g'),
    ('process.command_args', ['java', '-Dapi_key=1', 'x=2'], ['java', '-Dapi_key=***', 'x=2']),
    ('kv', {'password': 'p', 'a': {'secret': 's', 'b': 1}}, {'password': '***', 'a': {'secret': '***', 'b': 1}}),
])
def test_masker(key, value, expected):
    assert Masker(['biz.card']).value(key, value) == expected


# ---------------------------------------------------------------------------
# 时间参数
# ---------------------------------------------------------------------------
def test_time_parsing(monkeypatch):
    monkeypatch.setattr(common, '_clock', lambda: 1_795_203_000.0)
    beijing = datetime(2026, 11, 21, 3, 24, tzinfo=common.BEIJING).timestamp()
    assert common.parse_time('2026-11-21 03:24', 'start') == beijing
    assert common.parse_time('2026-11-21 03:24:05', 'start') == beijing + 5
    assert common.parse_time('2026/11/21 03:24', 'start') == beijing
    assert common.parse_time('2026-11-20T19:24:00Z', 'start') == beijing  # 带时区的照常
    assert common.parse_time('2026-11-21T03:24:00+08:00', 'start') == beijing
    assert common.iso(beijing + 5.123) == '2026-11-21T03:24:05.123+08:00'
    assert common.rfc3339(beijing + 0.9) == '2026-11-20T19:24:00Z'
    assert [common.parse_lookback(v) for v in ('15m', '1h', '2d', '30s')] == [900, 3600, 172800, 30]
    # 没给 start 时用 lookback（默认 1h），end 默认现在
    assert common.resolve_window({}) == (1_795_203_000.0 - 3600, 1_795_203_000.0)
    assert common.resolve_window({'lookback': '15m'}) == (1_795_203_000.0 - 900, 1_795_203_000.0)
    assert common.resolve_window({'start': '2026-11-21 03:24', 'end': '2026-11-21 03:30', 'lookback': '1d'}) == (
        beijing, beijing + 360)


@pytest.mark.parametrize('params, message', [
    ({'lookback': '1w'}, '数字加单位'),
    ({'lookback': 60}, '数字加单位'),
    ({'lookback': '0m'}, '要大于 0'),
    ({'lookback': '8d'}, '最长 7 天'),
    ({'start': 'yesterday'}, '北京时间 YYYY-MM-DD HH:mm'),
    ({'start': 123}, '北京时间'),
    ({'start': '2026-11-21 04:00', 'end': '2026-11-21 03:00'}, '开始时间要早于结束时间'),
    ({'start': '2026-11-01 00:00', 'end': '2026-11-21 00:00'}, '最长 7 天'),
])
def test_time_parsing_errors(params, message):
    with pytest.raises(common.TracingError, match=re.escape(message)) as info:
        common.resolve_window(params)
    assert info.value.code == 'invalid_request' and info.value.status == 400


def test_ttl_cache_lru_expiry_and_size():
    clock = [0.0]
    cache = common.TTLCache(max_items=2, ttl=10, max_bytes=100, clock=lambda: clock[0])
    cache.put('a', 1, size=10)
    cache.put('b', 2, size=10)
    assert cache.get('a') == 1  # a 变成最近使用
    cache.put('c', 3, size=10)  # 超过 2 个：淘汰最久没用的 b
    assert cache.get('b') is None and cache.get('a') == 1 and len(cache) == 2
    cache.put('big', 4, size=101)  # 单个超过上限：不缓存
    assert cache.get('big') is None
    cache.put('d', 5, size=95)  # 总量超过 100：从最旧的开始淘汰
    assert cache.get('d') == 5 and cache.get('a') is None
    clock[0] = 11
    assert cache.get('d') is None  # 过期
    cache.clear()
    assert len(cache) == 0
