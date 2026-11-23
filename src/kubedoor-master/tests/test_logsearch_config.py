"""历史日志配置（logsearch/config.py）：校验、密钥沿用 / 清除、交叉引用、公开格式、审计项。"""
import copy
import json

import pytest

from auth.crypto import decrypt_secret
from logsearch import config
from logsearch.common import LogError
from logsearch_fakes import sample_config


@pytest.fixture(autouse=True)
def encryption_key(monkeypatch):
    monkeypatch.setenv('AI_ENCRYPTION_KEY', '22' * 32)


def saved():
    return config.normalize_config(sample_config())


def test_normalize_full_config_and_defaults():
    cfg = saved()
    ck, es = cfg['connections']
    assert ck['auth_type'] == 'basic' and ck['url'] == 'http://127.0.0.1:9' and ck['timeout'] == 10
    assert ck['password'].startswith('v1:') and decrypt_secret(ck['password']) == 'ch-pass'
    assert es['auth_type'] == 'basic' and decrypt_secret(es['password']) == 'es-pass' and es['api_key'] == ''
    assert es['max_rows_to_read'] == 1_000_000_000  # 默认值
    ingress, app, old, other = cfg['sources']
    assert ingress['fields']['message'] == 'message' and 'client_ip' not in ingress['fields']  # 只存非空项
    assert ingress['level_values'] == {'error': ['ERROR', 'FATAL']}
    assert app['kind'] == 'app' and app['duration_unit'] == 'ms' and old['enabled'] is False
    assert other['kind'] == 'other' and other['fields'] == {} and other['correlations'] == []
    assert cfg['clusters'][0]['bindings'][0]['conditions'] == [{'field': 'env', 'op': 'in',
                                                               'values': ['prod-a', 'prod-a2']}]
    assert cfg['clusters'][1]['note'] == ''
    # 存的配置再补默认值（新版本加的键）
    merged = config.merged({'connections': [{'id': 'x'}], 'sources': [{'id': 's'}], 'clusters': [{'env': 'e'}]})
    assert merged['connections'][0]['max_concurrency'] == 4 and merged['sources'][0]['mask_fields'] == []
    assert merged['clusters'][0]['bindings'] == [] and merged['enabled'] is False


def test_partial_update_keeps_saved_values_and_secrets():
    first = saved()
    toggled = config.normalize_config({'enabled': False}, first)
    assert toggled == {**first, 'enabled': False}
    # 连接整体替换：密码留空沿用同 id 已保存的；没传的字段沿用
    form = copy.deepcopy(sample_config()['connections'])
    form[0].update(password='', name='新名字')
    form[1].update(password=None)
    second = config.normalize_config({'connections': form}, first)
    assert second['connections'][0]['password'] == first['connections'][0]['password']
    assert second['connections'][1]['password'] == first['connections'][1]['password']
    assert second['connections'][0]['name'] == '新名字' and second['sources'] == first['sources']
    # 换了新密码
    form[0]['password'] = 'new-pass'
    third = config.normalize_config({'connections': form}, first)
    assert decrypt_secret(third['connections'][0]['password']) == 'new-pass'


def test_clear_secrets_and_auth_none():
    first = config.normalize_config(sample_config(connections=[
        {'id': 'es', 'name': 'ES', 'type': 'elasticsearch', 'url': 'http://es:9200', 'auth_type': 'api_key',
         'api_key': 'aWQ6a2V5', 'headers': [{'name': 'X-Proxy', 'value': 'p1'}]},
        {'id': 'ck', 'name': 'CH', 'type': 'clickhouse', 'url': 'http://ch:8123', 'password': 'pw'}]))
    es = first['connections'][0]
    assert decrypt_secret(es['api_key']) == 'aWQ6a2V5' and decrypt_secret(es['headers'][0]['value']) == 'p1'
    # 请求头的值留空按名称沿用
    form = [{'id': 'es', 'name': 'ES', 'type': 'elasticsearch', 'url': 'http://es:9200', 'auth_type': 'api_key',
             'headers': [{'name': 'x-proxy', 'value': ''}]},
            {'id': 'ck', 'name': 'CH', 'type': 'clickhouse', 'url': 'http://ch:8123', 'clear_password': True,
             'password': 'ignored'}]
    second = config.normalize_config({'connections': form}, first)
    assert second['connections'][0]['api_key'] == es['api_key']
    assert second['connections'][0]['headers'] == [{'name': 'x-proxy', 'value': es['headers'][0]['value']}]
    assert second['connections'][1]['password'] == ''  # 清除：忽略同时传来的密码
    assert config.public_config(second)['connections'][1]['has_password'] is False
    # 清除 API Key 后认证方式还是 api_key：报错
    form[0]['clear_api_key'] = True
    with pytest.raises(ValueError, match='要填写 API Key'):
        config.normalize_config({'connections': form}, first)
    # auth_type=none 时清掉账号和密钥
    form[0].update(auth_type='none', username='someone')
    third = config.normalize_config({'connections': form}, first)
    assert third['connections'][0]['username'] == '' and third['connections'][0]['api_key'] == ''
    # ClickHouse 的认证方式固定 basic
    form[1]['auth_type'] = 'none'
    assert config.normalize_config({'connections': form}, first)['connections'][1]['auth_type'] == 'basic'


def test_public_config_and_changed_items():
    first = saved()
    data = config.public_config(first)
    text = json.dumps(data, ensure_ascii=False)
    assert 'ch-pass' not in text and 'v1:' not in text
    assert data['connections'][0]['has_password'] is True and data['connections'][0]['has_api_key'] is False
    assert 'password' not in data['connections'][0] and data['connections'][0]['headers'] == []
    # 新增的连接只记 connections.<id>
    assert config.changed_items({}, first) == ['enabled', 'architecture', 'connections.ck', 'connections.es',
                                               'correlation_keys',
                                               'sources.ingress', 'sources.app', 'sources.old', 'sources.other',
                                               'clusters.prod-a', 'clusters.prod-b']
    form = sample_config()
    form['connections'][0]['password'] = ''  # 沿用：不算改了密码
    form['connections'][1]['password'] = 'changed'
    form['connections'][1]['headers'] = [{'name': 'X-A', 'value': 'b'}]
    form['sources'] = form['sources'][:3]
    form['clusters'][0]['note'] = '改了说明'
    form['clusters'][1]['bindings'][0]['source'] = 'ingress'
    second = config.normalize_config({**form, 'enabled': False}, first)
    assert config.changed_items(first, second) == ['enabled', 'connections.es.password', 'connections.es.headers',
                                                   'sources.other', 'clusters.prod-a']
    assert config.changed_items(first, first) == []


def test_fingerprint_changes_with_connection():
    conn = saved()['connections'][0]
    assert config.fingerprint(conn) == config.fingerprint(dict(conn))
    assert config.fingerprint(conn) != config.fingerprint({**conn, 'url': 'http://other:8123'})
    assert config.fingerprint(conn) == config.fingerprint({**conn, 'max_concurrency': 9})  # 并发数不影响缓存


def _with(path, value):
    form = sample_config()
    target = form
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return form


@pytest.mark.parametrize('form, message', [
    ('x', '配置格式不正确'),
    ({'enabled': 'yes'}, '「启用」要是 true 或 false'),
    ({'architecture': 'a' * 20001}, '网络架构说明最长 20000'),
    ({'architecture': 'a\x00b'}, '网络架构说明不能有控制字符'),
    ({'connections': 'x'}, '连接要是列表'),
    ({'connections': [{'id': 'A'}]}, '第 1 个连接的 ID要小写字母开头'),
    (_with(('connections', 1, 'id'), 'ck'), '连接 ID ck 重复了'),
    (_with(('connections', 0, 'name'), ''), '连接 ck的名称不能为空'),
    (_with(('connections', 0, 'name'), 'n' * 51), '最长 50'),
    (_with(('connections', 0, 'type'), 'mysql'), '类型只能是 clickhouse 或 elasticsearch'),
    (_with(('connections', 0, 'url'), 'ftp://x'), '要以 http:// 或 https:// 开头'),
    (_with(('connections', 0, 'url'), 'http://ch:8123/?db=1'), '不能带查询参数和 #'),
    (_with(('connections', 0, 'url'), ''), '连接 ck的地址不能为空'),
    (_with(('connections', 1, 'auth_type'), 'token'), '认证方式只能是 none / basic / api_key'),
    (_with(('connections', 1, 'username'), ''), '认证方式是账号密码时要填写账号'),
    (_with(('connections', 0, 'username'), 'a b'), '账号不能有空格'),
    (_with(('connections', 0, 'password'), 'a\nb'), '密码不能换行'),
    (_with(('connections', 0, 'timeout'), 4), '超时时间（秒）取 5~120'),
    (_with(('connections', 0, 'timeout'), 'x'), '必须是整数'),
    (_with(('connections', 0, 'max_concurrency'), 21), '并发数取 1~20'),
    (_with(('connections', 0, 'max_rows_to_read'), -1), '读取行数上限取 0~'),
    (_with(('connections', 0, 'allow_raw'), 'yes'), '「允许原生查询」要是 true 或 false'),
    (_with(('connections', 0, 'headers'), [{'name': 'Host', 'value': 'x'}]), '连接 ck：请求头 Host 由系统设置'),
    ({'correlation_keys': [{'key': 'Trace', 'label': 'x'}]}, '第 1 个关联字段的 key要小写字母开头'),
    ({'correlation_keys': [{'key': 'a-b', 'label': 'x'}]}, '只能用小写字母、数字、_，'),
    ({'correlation_keys': [{'key': 'a', 'label': ''}]}, '关联字段 a的名称不能为空'),
    ({'correlation_keys': [{'key': 'a', 'label': 'x' * 31}]}, '最长 30'),
    ({'correlation_keys': [{'key': 'a', 'label': 'x', 'pattern': '^[0-9]+$'}]}, '不要带 ^ 和 $'),
    ({'correlation_keys': [{'key': 'a', 'label': 'x', 'pattern': '[0-9'}]}, '不是合法的正则表达式'),
    ({'correlation_keys': [{'key': 'a', 'label': 'x', 'pattern': '[0-9]*'}]}, '不能匹配空字符串'),
    ({'correlation_keys': [{'key': 'a', 'label': 'x'}, {'key': 'a', 'label': 'y'}]}, '关联字段 a 重复了'),
    ({'correlation_keys': [{'key': 'a', 'label': 'x', 'trace': True}, {'key': 'b', 'label': 'y', 'trace': True}],
      'sources': []}, '只能有一个关联字段是链路 ID'),
    (_with(('sources', 0, 'connection'), 'nope'), '日志源 ingress：连接 nope 不存在'),
    (_with(('sources', 1, 'id'), 'ingress'), '日志源 ID ingress 重复了'),
    (_with(('sources', 0, 'time_field'), ''), '日志源 ingress的时间字段不合法'),
    (_with(('sources', 0, 'time_field'), 'a b'), '字段名只能用字母、数字和 _ . @ -'),
    (_with(('sources', 0, 'kind'), 'metrics'), '类型只能是 access / app / other'),
    (_with(('sources', 0, 'duration_unit'), 'min'), '耗时单位只能是 ms / s / us / ns'),
    (_with(('sources', 0, 'fields'), {'nope': 'x'}), '语义字段里没有 nope'),
    (_with(('sources', 0, 'fields'), {'message': 'a b'}), '语义字段「正文」不合法'),
    (_with(('sources', 0, 'level_values'), {'fatal': ['F']}), '级别取值只能是 error / warn / info / debug'),
    (_with(('sources', 0, 'level_values'), {'error': ['x'] * 21}), '最多 20 个'),
    (_with(('sources', 0, 'level_values'), {'error': ['x' * 51]}), '最长 50'),
    (_with(('sources', 0, 'correlations'), [{'key': 'nope', 'field': 'a'}]), '关联字段里没有登记这个 key'),
    (_with(('sources', 0, 'correlations'), [{'key': 'trace_id', 'text': 'TID:'}]),
     '日志源 ingress的关联字段 trace_id：正文写法要包含一个 {value}'),
    (_with(('sources', 0, 'correlations'), [{'key': 'trace_id', 'text': '{value}{value}'}]), '要包含一个 {value}'),
    (_with(('sources', 0, 'correlations'), [{'key': 'trace_id', 'text': '{value}'}]), '还要有固定的文字'),
    (_with(('sources', 0, 'correlations'), [{'key': 'trace_id', 'field': 'a', 'text': 'T:{value}'}]), '二选一'),
    (_with(('sources', 0, 'correlations'), [{'key': 'trace_id', 'field': 'a'}, {'key': 'trace_id', 'field': 'b'}]),
     '登记了两次'),
    (_with(('sources', 1, 'correlations'), [{'key': 'trace_id', 'text': 'T:{value}'}]) | {'sources': [
        {**sample_config()['sources'][1], 'fields': {}, 'correlations': [{'key': 'trace_id', 'text': 'T:{value}'}]}],
        'clusters': []}, '用正文写法要先配置正文字段'),
    (_with(('sources', 0, 'columns'), [{'name': 'a', 'description': 'x'}, {'name': 'a', 'description': 'y'}]),
     '字段说明里 a 重复了'),
    (_with(('sources', 0, 'columns'), [{'name': 'a', 'description': 'x' * 301}]), '最长 300'),
    (_with(('sources', 0, 'default_fields'), ['a'] * 51), '默认返回字段最多 50 项'),
    (_with(('sources', 0, 'mask_patterns'), ['(']), '打码正则「(」不是合法的正则表达式'),
    (_with(('sources', 0, 'default_target'), 'access'), '默认表 / 索引要写成 库.表'),
    (_with(('sources', 0, 'cluster_field'), 'a b'), '集群字段不合法'),
    (_with(('clusters', 1, 'env'), 'prod-a'), '集群 prod-a 重复了'),
    (_with(('clusters', 0, 'env'), 'prod a'), '第 1 个集群的名称不能为空、不能有空格'),
    (_with(('clusters', 0, 'note'), 'x' * 4001), '最长 4000'),
    (_with(('clusters', 0, 'bindings', 0, 'source'), 'nope'), '集群 prod-a 的日志源 nope：日志源不存在'),
    (_with(('clusters', 0, 'bindings', 1, 'source'), 'ingress'), '同一个集群里绑定了两次'),
    (_with(('clusters', 0, 'bindings', 0, 'connection'), 'es'), '连接 es 和日志源的连接类型不同'),
    (_with(('clusters', 0, 'bindings', 0, 'connection'), 'zz'), '连接 zz 不存在'),
    (_with(('clusters', 0, 'bindings', 0, 'target'), 'a.b.c'), '表 / 索引要写成 库.表'),
    (_with(('clusters', 0, 'bindings', 0, 'target'), '1db.t'), '要写成 库.表'),
    (_with(('clusters', 0, 'bindings', 0, 'target'), 'd' * 65 + '.t'), '要写成 库.表'),
    (_with(('clusters', 0, 'bindings', 1, 'target'), 'remote:app-*'), '不能带 :'),
    (_with(('clusters', 0, 'bindings', 1, 'target'), '.security'), '不能以 . 开头'),
    (_with(('clusters', 0, 'bindings', 1, 'target'), 'App-Logs'), '小写'),
    (_with(('clusters', 0, 'bindings', 1, 'target'), ','.join(['a'] * 21)), '最多 20 项'),
    (_with(('clusters', 0, 'bindings', 0, 'conditions'), [{'field': 'env', 'op': 'like', 'values': ['x']}]),
     'op 只能是 eq / ne / in / not_in / prefix'),
    (_with(('clusters', 0, 'bindings', 0, 'conditions'), [{'field': 'env', 'op': 'eq', 'values': ['a', 'b']}]),
     'eq 要正好 1 个值'),
    (_with(('clusters', 0, 'bindings', 0, 'conditions'), [{'field': 'env', 'op': 'in', 'values': []}]),
     'in 要有 1~50 个值'),
    (_with(('clusters', 0, 'bindings', 0, 'conditions'), [{'field': 'env', 'op': 'eq', 'values': [True]}]),
     '只能是文字或数字'),
    (_with(('clusters', 0, 'bindings', 0, 'conditions'), [{'field': 'env', 'op': 'eq', 'values': ['x' * 201]}]),
     '最长 200'),
    (_with(('clusters', 0, 'bindings', 0, 'conditions'), [{'field': 'env', 'op': 'eq', 'values': ['a']}] * 6),
     '最多 5 个'),
    (_with(('clusters', 0, 'bindings', 0, 'note'), 'x' * 2001), '最长 2000'),
    ({'enabled': True, 'connections': [], 'sources': [], 'clusters': []}, '启用前请至少添加一个连接'),
])
def test_validation_errors(form, message):
    with pytest.raises(ValueError) as error:
        config.normalize_config(form)
    assert message in str(error.value), str(error.value)


def test_config_size_limit():
    base = sample_config()['sources'][0]
    columns = [{'name': f'c{i}', 'description': '长' * 300} for i in range(300)]
    form = sample_config(sources=[{**base, 'id': f's{i}', 'columns': columns} for i in range(12)], clusters=[])
    with pytest.raises(ValueError, match='配置太大'):
        config.normalize_config(form)


def test_form_validation_does_not_require_enabled_fields():
    form = {'enabled': True, 'connections': [], 'sources': [], 'clusters': [], 'correlation_keys': []}
    cfg = config.normalize_config(form, check_enabled=False)
    assert cfg['enabled'] is True and cfg['connections'] == []


def test_condition_values_and_targets_normalized():
    form = sample_config()
    form['clusters'][0]['bindings'][1]['target'] = ' app-logs-*, -app-logs-old '
    form['clusters'][0]['bindings'][0]['conditions'] = [{'field': 'env', 'op': 'eq', 'values': [3]},
                                                       {'field': 'env', 'op': 'not_in', 'values': [' x ', 1.5]}]
    cfg = config.normalize_config(form)
    binding = cfg['clusters'][0]['bindings']
    assert binding[1]['target'] == 'app-logs-*,-app-logs-old'
    assert binding[0]['conditions'] == [{'field': 'env', 'op': 'eq', 'values': [3]},
                                        {'field': 'env', 'op': 'not_in', 'values': ['x', 1.5]}]


@pytest.mark.asyncio
async def test_load_config(monkeypatch):
    class Store:
        def __init__(self, value=None, broken=False):
            self.value, self.broken = value, broken

        async def get_setting(self, key, default=None):
            assert key == 'log_sources'
            if self.broken:
                raise RuntimeError('db down')
            return self.value

    monkeypatch.setattr(config, 'store', Store({'enabled': True}))
    assert (await config.load_config())['enabled'] is True
    monkeypatch.setattr(config, 'store', Store(None))
    assert (await config.load_config())['connections'] == []
    monkeypatch.setattr(config, 'store', Store(broken=True))
    with pytest.raises(LogError) as error:
        await config.load_config()
    assert error.value.status == 503 and error.value.code == 'config_unavailable'
