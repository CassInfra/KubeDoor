"""事件告警规则的存储、快照、旧文件导入、接口和试运行(k8s_event/rules_store.py、rules_api.py)。"""
import asyncio
import json
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from loguru import logger

import auth
import general_settings
from auth.principal import Principal
from k8s_event import event_processor, rules_api, rules_schema, rules_store
from k8s_event.event_alert_processor import EventAlertProcessor
from test_auth_flow import add_user, basic, fake  # noqa: F401

ADMIN = Principal('kubedoor', 'admin', user_id=1)
UPDATED_AT = datetime(2026, 11, 15, 2, 0, tzinfo=timezone.utc)
KEY = rules_store.SETTING_KEY


def rule(name, conditions, enabled=True, severity='warning'):
    return {'name': name, 'enabled': enabled, 'severity': severity, 'conditions': conditions}


def ignore(name, conditions, enabled=True):
    return {'name': name, 'enabled': enabled, 'conditions': conditions}


def rules_doc(alert=(), ignores=()):
    return {'global_ignore_rules': list(ignores), 'alert_rules': list(alert)}


SAVED = rules_doc([rule('保存的规则', {'reason': {'contains': ['oom']}})])


class FakeStore:
    """auth_settings 里只有 event_alert_rules 这一行(读写都做一次 JSON 往返,和数据库一样)"""

    def __init__(self, value=None):
        self.row = None if value is None else {'value': value, 'updated_by': 'kubedoor', 'updated_at': UPDATED_AT}
        self.puts = []
        self.reads = 0
        self.error = None

    def _copy(self):
        if self.row is None:
            return None
        return {**self.row, 'value': json.loads(json.dumps(self.row['value']))}

    async def get_setting_row(self, key):
        assert key == KEY
        self.reads += 1
        if self.error is not None:
            raise self.error
        return self._copy()

    async def put_setting_row(self, key, value, updated_by):
        assert key == KEY
        if self.error is not None:
            raise self.error
        self.puts.append(json.loads(json.dumps(value)))
        self.row = {'value': json.loads(json.dumps(value)), 'updated_by': updated_by, 'updated_at': UPDATED_AT}
        return self._copy()


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(rules_store, '_snapshot', None)
    monkeypatch.setattr(rules_store, '_summary', None)
    monkeypatch.setattr(rules_store, '_refresh_failed', False)
    monkeypatch.setattr(general_settings, 'value', lambda name: {'alert_dedup_window': 300}[name])
    store = FakeStore()
    monkeypatch.setattr(rules_store, 'store', store)
    return store


@pytest.fixture
def audits(monkeypatch):
    records = []
    monkeypatch.setattr(rules_api.audit, 'record_bg',
                        lambda username, action, **kwargs: records.append({'username': username, 'action': action, **kwargs}))
    return records


@pytest.fixture
def logs():
    lines = []
    handler_id = logger.add(lambda message: lines.append(str(message)), level='DEBUG')
    yield lines
    logger.remove(handler_id)


async def client_for(principal=ADMIN):
    @web.middleware
    async def inject(request, handler):
        request['principal'] = principal
        return await handler(request)

    app = web.Application(middlewares=[inject])
    rules_api.register_routes(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


# ---------------------------------------------------------------------------
# 快照
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_no_row_uses_default_rules_without_writing(fresh, monkeypatch):
    insert = AsyncMock()
    monkeypatch.setattr(rules_store, 'insert_absent', insert)
    assert await rules_store.refresh() is True
    snapshot = rules_store.current()
    assert snapshot.source == rules_store.SOURCE_DEFAULT and snapshot.loaded
    assert [rule['name'] for rule in snapshot.alert_rules][0] == '关键事件告警'
    assert len(snapshot.ignore_rules) == 2 and len(snapshot.alert_rules) == 7
    assert fresh.puts == [] and not insert.called


@pytest.mark.asyncio
async def test_refresh_loads_database_rules_and_keeps_old_snapshot_on_failure(fresh, logs):
    fresh.row = {'value': SAVED, 'updated_by': 'kubedoor', 'updated_at': UPDATED_AT}
    assert await rules_store.refresh() is True
    loaded = rules_store.current()
    assert loaded.source == rules_store.SOURCE_DATABASE and loaded.alert_rules[0]['name'] == '保存的规则'
    fresh.error = ConnectionError('db down')
    assert await rules_store.refresh() is False
    assert await rules_store.refresh() is False
    assert rules_store.current() is loaded  # 读库失败保留上一份
    assert sum('读取事件告警规则失败' in line for line in logs) == 1  # 只在第一次失败时提示
    fresh.error = None
    assert await rules_store.refresh() is True
    assert rules_store.current() is loaded  # 内容没变不重建
    assert any('读取事件告警规则已恢复' in line for line in logs)
    fresh.row = None  # 库里的行被删掉:回到内置默认规则
    await rules_store.refresh()
    assert rules_store.current().source == rules_store.SOURCE_DEFAULT


@pytest.mark.asyncio
async def test_failure_before_first_load_keeps_default_rules(fresh):
    fresh.error = ConnectionError('db down')
    assert await rules_store.refresh() is False
    snapshot = rules_store.current()
    assert snapshot.source == rules_store.SOURCE_DEFAULT and not snapshot.loaded and len(snapshot.alert_rules) == 7


@pytest.mark.asyncio
async def test_refresh_does_not_roll_back_a_save_made_while_reading(fresh):
    fresh.row = {'value': SAVED, 'updated_by': 'kubedoor', 'updated_at': UPDATED_AT}
    await rules_store.refresh()
    stale, gate = fresh._copy(), asyncio.Event()

    async def slow_read(key):  # 刷新先读到了保存之前的旧值,读库还没返回时页面保存了
        await gate.wait()
        return stale

    fresh.get_setting_row = slow_read
    task = asyncio.create_task(rules_store.refresh())
    await asyncio.sleep(0)
    saved = rules_doc([rule('刚保存的', {'k8s': {'contains': ['x']}})])
    rules_store.apply(saved)
    gate.set()
    assert await task is True
    assert rules_store.current().alert_rules[0]['name'] == '刚保存的'
    del fresh.get_setting_row  # 换回正常的读库
    fresh.row = {'value': saved, 'updated_by': 'kubedoor', 'updated_at': UPDATED_AT}
    assert await rules_store.refresh() is True  # 下一次刷新照常
    assert rules_store.current().alert_rules[0]['name'] == '刚保存的'


def test_snapshot_is_read_only_and_lenient(logs):
    snapshot = rules_store.apply({'global_ignore_rules': 'x', 'alert_rules': [rule('a', {'k8s': {'contains': ['p']}}), 3]})
    assert snapshot.ignore_rules == () and len(snapshot.alert_rules) == 1 and snapshot.skipped == 2
    with pytest.raises(TypeError):
        snapshot.alert_rules[0]['name'] = 'b'
    with pytest.raises(AttributeError):
        snapshot.alert_rules[0]['conditions']['k8s']['contains'].append('q')
    assert any('格式不对的内容匹配时跳过' in line for line in logs)
    assert rules_store.build('not-a-dict', 'test').skipped == 1


def test_default_rules_are_a_fresh_copy_and_survive_missing_file(monkeypatch, tmp_path, logs):
    first = rules_store.default_rules()
    first['alert_rules'].clear()
    assert len(rules_store.default_rules()['alert_rules']) == 7
    monkeypatch.setattr(rules_store, '_default_text', None)
    monkeypatch.setattr(rules_store, '_default_failed', False)
    monkeypatch.setattr(rules_store, 'DEFAULT_FILE', tmp_path / 'missing.json')
    assert rules_store.default_rules() == {'global_ignore_rules': [], 'alert_rules': []}
    assert rules_store.default_rules() == {'global_ignore_rules': [], 'alert_rules': []}
    assert sum('读取内置默认事件告警规则' in line for line in logs) == 1


def test_default_file_is_the_former_setup_template():
    text = rules_store.DEFAULT_FILE.read_text(encoding='utf-8')
    assert '\r' not in text  # 逐字取自原来 setup 下的 alert_rules.json(LF,结尾没有换行)
    assert json.loads(text) == rules_store.default_rules()


# ---------------------------------------------------------------------------
# 接口
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_get_without_saved_rules_returns_defaults(fresh):
    client = await client_for()
    try:
        response = await client.get('/api/admin/event-alert-rules')
        assert response.status == 200
        data = (await response.json())['data']
        assert data == {'rules': rules_store.default_rules(), 'source': 'default', 'updated_by': None,
                        'updated_at': None}
        response = await client.get('/api/admin/event-alert-rules/default')
        assert (await response.json())['data'] == {'rules': rules_store.default_rules()}
        assert fresh.puts == []
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_put_saves_takes_effect_immediately_and_audits(fresh, audits, monkeypatch):
    sent = Mock(return_value=True)
    from k8s_event import event_alert_processor as module
    monkeypatch.setattr(module, 'notify', sent)
    monkeypatch.setattr(module, 'get_pg_event_client', lambda: SimpleNamespace(pool=SimpleNamespace(execute_command=Mock())))
    processor = EventAlertProcessor()  # 运行中的处理器:保存后不重建
    event = {'eventUid': 'u1', 'eventStatus': 'ADDED', 'level': 'Normal', 'count': 1, 'kind': 'Pod', 'k8s': 'prod',
             'namespace': 'vip', 'name': 'p', 'reason': 'Pulled', 'message': 'image pulled'}
    assert processor.process_event(dict(event)) is None  # 默认规则不管 Pulled
    body = rules_doc([{'name': ' 拉取镜像 ', 'conditions': {'reason': {'equals': 'pulled'}}}],
                     [ignore('忽略测试集群', {'k8s': {'starts_with': ['test-']}})])
    client = await client_for()
    try:
        response = await client.put('/api/admin/event-alert-rules', json={'rules': body})
        assert response.status == 200
        data = (await response.json())['data']
    finally:
        await client.close()
    expected = rules_doc([rule('拉取镜像', {'reason': {'equals': 'pulled'}})],
                         [ignore('忽略测试集群', {'k8s': {'starts_with': ['test-']}})])
    assert fresh.puts == [expected]
    assert data == {'rules': expected, 'source': 'database', 'updated_by': 'kubedoor',
                    'updated_at': UPDATED_AT.astimezone().isoformat()}
    snapshot = rules_store.current()
    assert snapshot.source == 'database' and snapshot.alert_rules[0]['name'] == '拉取镜像'
    assert 'K8S事件告警: 拉取镜像' in processor.process_event(dict(event))['message']
    assert audits == [{'username': 'kubedoor', 'action': 'event_rules.update', 'ip': '127.0.0.1',
                       'detail': {'global_ignore_rules': {'count': 1, 'names': ['忽略测试集群']},
                                  'alert_rules': {'count': 1, 'names': ['拉取镜像']}}}]


@pytest.mark.asyncio
async def test_put_rejects_invalid_rules_with_locations(fresh, audits):
    client = await client_for()
    try:
        bad = rules_doc([rule('A', {'reason': {'contains': ['x']}}), rule('B', {'k8s': {'contains': ['y']}}),
                         rule('重启事件告警', {'count': {'greater_than': '1'}})])
        response = await client.put('/api/admin/event-alert-rules', json={'rules': bad})
        assert response.status == 422
        body = await response.json()
        assert body['success'] is False and body['code'] == 'invalid_rules'
        assert body['message'] == '告警规则第 3 条「重启事件告警」的条件 count：greater_than 的值必须是数字'
        assert body['errors'] == [{'path': ['alert_rules', 2, 'conditions', 'count', 'greater_than'],
                                   'message': body['message']}]
        response = await client.put('/api/admin/event-alert-rules', json={'rules': {'alert_rules': []}})
        assert (await response.json())['message'] == '缺少 global_ignore_rules（忽略规则）：没有规则时写成空数组 []'
        response = await client.put('/api/admin/event-alert-rules', json={})
        assert response.status == 422
        for raw in ('[]', 'not json'):
            response = await client.put('/api/admin/event-alert-rules', data=raw,
                                        headers={'Content-Type': 'application/json'})
            assert response.status == 400
            assert (await response.json())['message'] == '请求体必须是 JSON 对象'
    finally:
        await client.close()
    assert fresh.puts == [] and audits == [] and rules_store.current().source == 'default'


@pytest.mark.asyncio
async def test_database_errors_return_503_and_keep_snapshot(fresh, audits):
    rules_store.apply(SAVED)
    before = rules_store.current()
    fresh.error = ConnectionError('db down')
    client = await client_for()
    try:
        response = await client.get('/api/admin/event-alert-rules')
        assert response.status == 503 and (await response.json())['code'] == 'db_unavailable'
        response = await client.put('/api/admin/event-alert-rules', json={'rules': rules_doc()})
        assert response.status == 503
    finally:
        await client.close()
    assert rules_store.current() is before and audits == []


@pytest.mark.asyncio
async def test_handlers_reject_non_admin(fresh, audits):
    client = await client_for(Principal('alice', 'cluster_admin', user_id=2))
    try:
        for method, path in (('GET', ''), ('PUT', ''), ('GET', '/default'), ('POST', '/preview')):
            response = await client.request(method, '/api/admin/event-alert-rules' + path, json={'rules': rules_doc()})
            assert response.status == 403
            assert (await response.json())['code'] == 'forbidden'
    finally:
        await client.close()
    assert fresh.puts == [] and fresh.reads == 0 and audits == []


@pytest.mark.asyncio
async def test_real_auth_middleware_limits_rules_to_system_admin(fake, fresh, audits, monkeypatch):  # noqa: F811
    await add_user(fake, 'admin-user', 'admin')
    await add_user(fake, 'cluster-user', 'cluster_admin')
    monkeypatch.setattr(rules_api.db, 'pg_fetch', AsyncMock(return_value=[]))
    app = web.Application()
    auth.setup(app, {})
    rules_api.register_routes(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        endpoints = (('GET', ''), ('PUT', ''), ('GET', '/default'), ('POST', '/preview'))
        for method, path in endpoints:
            url = '/api/admin/event-alert-rules' + path
            assert (await client.request(method, url, json={'rules': rules_doc()})).status == 401
            headers = {'Authorization': basic('cluster-user', 'Str0ng-pass!')}
            assert (await client.request(method, url, headers=headers, json={'rules': rules_doc()})).status == 403
        headers = {'Authorization': basic('admin-user', 'Str0ng-pass!')}
        for method, path in endpoints:
            response = await client.request(method, '/api/admin/event-alert-rules' + path, headers=headers,
                                            json={'rules': rules_doc(), 'hours': 1})
            assert response.status == 200, (method, path, await response.text())
    finally:
        await client.close()
    assert fresh.puts == [rules_doc()]
    assert [record['action'] for record in audits].count('event_rules.update') == 1


# ---------------------------------------------------------------------------
# 旧规则文件导入
# ---------------------------------------------------------------------------
@pytest.fixture
def inserted(monkeypatch, fresh):
    """INSERT ... ON CONFLICT DO NOTHING 的内存版:库里没有时写入"""
    calls = []

    async def insert(value, updated_by):
        calls.append((value, updated_by))
        if fresh.row is not None:
            return False
        fresh.row = {'value': value, 'updated_by': updated_by, 'updated_at': UPDATED_AT}
        return True

    monkeypatch.setattr(rules_store, 'insert_absent', insert)
    return calls


@pytest.mark.asyncio
async def test_import_legacy_file_statuses(tmp_path, fresh, inserted, logs):
    path = tmp_path / 'alert_rules.json'
    assert await rules_api.import_legacy_file(str(path)) == 'absent'
    path.write_text('{"alert_rules": [', encoding='utf-8')
    assert await rules_api.import_legacy_file(str(path)) == 'invalid'
    assert any('不是有效的规则 JSON' in line for line in logs)
    # 不做严格校验:单个字符串、没有 severity、不认识的运算符都原样导入
    legacy = {'global_ignore_rules': [], 'alert_rules': [{'name': '旧规则', 'conditions': {'reason': {'contain': 'OOM'}}}],
              'comment': 'kept'}
    path.write_text(chr(0xFEFF) + json.dumps(legacy, ensure_ascii=False), encoding='utf-8')  # 带 BOM 也能读
    assert await rules_api.import_legacy_file(str(path)) == 'imported'
    assert inserted == [(legacy, 'legacy-import')] and fresh.row['value'] == legacy
    path.write_text('this file is not read any more', encoding='utf-8')
    assert await rules_api.import_legacy_file(str(path)) == 'present'  # 库里已有:不读文件
    assert len(inserted) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('content', [
    '[]', '"rules"', '{"global_ignore_rules": {}, "alert_rules": []}', '{"alert_rules": "x"}',
    '{"alert_rules": [{"count": {"greater_than": NaN}}]}', b'\xff\xfe\x00{', '',
])
async def test_invalid_legacy_files_are_not_imported(tmp_path, fresh, inserted, content):
    path = tmp_path / 'alert_rules.json'
    path.write_bytes(content if isinstance(content, bytes) else content.encode('utf-8'))
    assert await rules_api.import_legacy_file(str(path)) == 'invalid'
    assert inserted == [] and fresh.row is None


@pytest.mark.asyncio
async def test_legacy_file_too_large_or_unreadable(tmp_path, fresh, inserted, monkeypatch):
    path = tmp_path / 'alert_rules.json'
    path.write_text(json.dumps(rules_doc()), encoding='utf-8')
    monkeypatch.setattr(rules_api, 'LEGACY_MAX_BYTES', 10)
    assert await rules_api.import_legacy_file(str(path)) == 'invalid'
    assert await rules_api.import_legacy_file(str(tmp_path)) == 'invalid'  # 是个目录
    assert inserted == []


@pytest.mark.asyncio
async def test_import_race_reports_present_and_database_errors_raise(tmp_path, fresh, monkeypatch):
    path = tmp_path / 'alert_rules.json'
    path.write_text(json.dumps(rules_doc()), encoding='utf-8')
    monkeypatch.setattr(rules_store, 'insert_absent', AsyncMock(return_value=False))  # 别人刚写进去
    assert await rules_api.import_legacy_file(str(path)) == 'present'
    fresh.error = ConnectionError('db down')
    with pytest.raises(ConnectionError):
        await rules_api.import_legacy_file(str(path))


@pytest.mark.asyncio
async def test_insert_sql_never_updates_existing_rows(monkeypatch):
    query = AsyncMock(return_value=None)
    monkeypatch.setattr(rules_store.db, 'pg_fetchrow', query)
    assert await rules_store.insert_absent(SAVED, 'legacy-import') is False
    sql, key, value, author = query.call_args.args
    assert 'ON CONFLICT (key) DO NOTHING' in sql and 'DO UPDATE' not in sql.upper()
    assert key == KEY and json.loads(value) == SAVED and author == 'legacy-import'
    query.return_value = {'key': KEY}
    assert await rules_store.insert_absent(SAVED, 'legacy-import') is True


def test_default_legacy_path_is_the_old_configmap_mount():
    assert rules_api.LEGACY_FILE == '/k8s_event/rules/alert_rules.json'
    assert rules_api.import_legacy_file.__defaults__ == ('/k8s_event/rules/alert_rules.json',)


# ---------------------------------------------------------------------------
# 启停
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_startup_imports_loads_and_starts_refresh(tmp_path, fresh, inserted, monkeypatch):
    path = tmp_path / 'alert_rules.json'
    path.write_text(json.dumps(SAVED, ensure_ascii=False), encoding='utf-8')
    monkeypatch.setattr(rules_api, 'LEGACY_FILE', str(path))
    monkeypatch.setattr(event_processor, '_event_processor', None)
    monkeypatch.setattr(event_processor, 'K8SEventProcessor', Mock(return_value='processor'))
    app = {}
    await rules_api.on_startup(app)
    try:
        assert rules_store.current().source == 'database' and rules_store.current().alert_rules[0]['name'] == '保存的规则'
        assert event_processor._event_processor == 'processor'  # 提前建好,不等第一条事件
        task = app[rules_api.REFRESH_TASK]
        assert not task.done()
    finally:
        await rules_api.on_cleanup(app)
    assert task.cancelled()
    await rules_api.on_cleanup({})  # 没启动过也能调用


@pytest.mark.asyncio
async def test_startup_survives_import_and_database_failures(fresh, monkeypatch, logs):
    monkeypatch.setattr(rules_api, 'import_legacy_file', AsyncMock(side_effect=ConnectionError('db down')))
    monkeypatch.setattr(event_processor, '_event_processor', None)
    monkeypatch.setattr(event_processor, 'K8SEventProcessor', Mock(return_value='processor'))
    fresh.error = ConnectionError('db down')
    app = {}
    await rules_api.on_startup(app)
    await rules_api.on_cleanup(app)
    assert any('导入旧的事件告警规则文件失败' in line for line in logs)
    assert rules_store.current().source == 'default'


@pytest.mark.asyncio
async def test_refresh_loop_survives_errors(monkeypatch):
    calls = []

    async def refresh():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError('boom')
        if len(calls) == 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(rules_store, 'refresh', refresh)
    with pytest.raises(asyncio.CancelledError):
        await rules_store.refresh_loop(interval=0)
    assert len(calls) == 3


def test_event_processor_is_created_once_across_threads(monkeypatch):
    created = []

    def build():
        created.append(1)
        return object()

    monkeypatch.setattr(event_processor, '_event_processor', None)
    monkeypatch.setattr(event_processor, 'K8SEventProcessor', build)
    barrier = threading.Barrier(8)
    results = []

    def work():
        barrier.wait()
        results.append(event_processor.get_event_processor())

    threads = [threading.Thread(target=work) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(created) == 1 and len({id(item) for item in results}) == 1


@pytest.mark.asyncio
async def test_reload_alert_rules_refreshes_from_database(fresh):
    fresh.row = {'value': SAVED, 'updated_by': 'kubedoor', 'updated_at': UPDATED_AT}
    assert await event_processor.reload_alert_rules() is True
    assert rules_store.current().alert_rules[0]['name'] == '保存的规则'


# ---------------------------------------------------------------------------
# 试运行
# ---------------------------------------------------------------------------
NOW = datetime(2026, 11, 15, 2, 0, tzinfo=timezone.utc)


def row(n, **changes):
    data = {'eventuid': f'uid-{n}', 'eventstatus': 'ADDED', 'level': 'Warning', 'count': 1, 'kind': 'Pod',
            'k8s': 'prod-a', 'namespace': 'vip', 'name': f'pod-{n}', 'reason': 'BackOff',
            'message': 'Back-off restarting failed container', 'firsttimestamp': NOW - timedelta(minutes=n),
            'lasttimestamp': NOW - timedelta(minutes=n), 'reportingcomponent': 'kubelet', 'reportinginstance': 'node-1'}
    data.update(changes)
    return data


PREVIEW_RULES = rules_doc(
    [rule('关键事件', {'reason': {'contains': ['OOMKilling']}, 'level': {'contains': ['Warning']}}, severity='critical'),
     rule('重启', {'message': {'contains': ['restart']}}),
     rule('全部 Warning', {'level': {'equals': 'Warning'}}),
     rule('新增的', {'eventStatus': {'equals': 'ADDED'}}, enabled=False)],
    [ignore('测试集群', {'k8s': {'starts_with': ['test-']}}),
     ignore('停用的忽略', {'reason': {'contains': ['BackOff']}}, enabled=False)])


def test_run_preview_counts_hits_shadowed_and_examples():
    rows = [
        row(1),  # 重启(全部 Warning 也能命中 → shadowed)
        row(2, k8s='test-a'),  # 被忽略
        row(3, reason='OOMKilling', message='oom', level='已告警'),  # 已告警按 Warning 算 → 关键事件
        row(4, level='Normal', message='pulled'),  # 什么都不命中
        row(5, eventstatus='DELETED', count=3),  # 按删除前的状态算(次数>1 → MODIFIED),照样参与
        row(6, message='x' * 400, reason='Unhealthy'),  # 全部 Warning
    ]
    result = rules_api.run_preview(rules_store.build(PREVIEW_RULES, 'preview'), rows, limit=10, examples=1)
    assert {key: result[key] for key in ('limit', 'total', 'truncated', 'ignored', 'matched', 'unmatched',
                                          'deleted', 'alerted_level')} == {
        'limit': 10, 'total': 6, 'truncated': False, 'ignored': 1, 'matched': 4, 'unmatched': 1, 'deleted': 1,
        'alerted_level': 1}
    ignore_stats, alert_stats = result['ignore_rules'], result['alert_rules']
    assert [(item['name'], item['enabled'], item['hits']) for item in ignore_stats] == [
        ('测试集群', True, 1), ('停用的忽略', False, 0)]
    assert [(item['name'], item['severity'], item['hits'], item['shadowed']) for item in alert_stats] == [
        ('关键事件', 'critical', 1, 0), ('重启', 'warning', 2, 0), ('全部 Warning', 'warning', 1, 3),
        ('新增的', 'warning', 0, 0)]
    example = alert_stats[0]['examples'][0]
    assert example == {'k8s': 'prod-a', 'namespace': 'vip', 'kind': 'Pod', 'name': 'pod-3', 'reason': 'OOMKilling',
                       'message': 'oom', 'count': 1, 'level': '已告警', 'eventStatus': 'ADDED',
                       'firstTimestamp': '2026-11-15 09:57:00', 'lastTimestamp': '2026-11-15 09:57:00'}
    assert len(alert_stats[1]['examples']) == 1  # 每条规则最多 examples 个例子
    long_message = alert_stats[2]['examples'][0]['message']
    assert len(long_message) == rules_api.PREVIEW_MESSAGE + 1 and long_message.endswith('…')


def test_run_preview_uses_the_same_event_shape_as_live_processing(monkeypatch):
    seen = []
    rules = rules_doc([rule('看事件', {'k8s': {'not_equals': 'zzz'}})])
    ruleset = rules_store.build(rules, 'preview')
    from k8s_event import alert_rule_matcher

    original = alert_rule_matcher.match_conditions

    def spy(event, conditions):
        seen.append(dict(event))
        return original(event, conditions)

    monkeypatch.setattr(alert_rule_matcher, 'match_conditions', spy)
    rules_api.run_preview(ruleset, [row(1, eventstatus='DELETED', count=1, level='已告警')])
    event = seen[0]
    assert set(event) == set(rules_schema.FIELDS)
    assert event['eventStatus'] == 'ADDED' and event['level'] == 'Warning'
    assert event['firstTimestamp'] == datetime(2026, 11, 15, 9, 59)  # 北京时间,不带时区,和实时处理一样
    assert str(event['lastTimestamp']) == '2026-11-15 09:59:00'


def test_run_preview_reports_truncation():
    result = rules_api.run_preview(rules_store.build(rules_doc(), 'preview'), [row(n) for n in range(4)], limit=3)
    assert result['truncated'] is True and result['total'] == 3 and result['unmatched'] == 3


@pytest.mark.asyncio
async def test_preview_endpoint_queries_recent_events_off_the_loop(monkeypatch):
    query = AsyncMock(return_value=[row(1), row(2, k8s='test-a')])
    monkeypatch.setattr(rules_api.db, 'pg_fetch', query)
    threads = []
    original = rules_api.run_preview

    def spy(*args, **kwargs):
        threads.append(threading.current_thread() is threading.main_thread())
        return original(*args, **kwargs)

    monkeypatch.setattr(rules_api, 'run_preview', spy)
    client = await client_for()
    try:
        response = await client.post('/api/admin/event-alert-rules/preview',
                                     json={'rules': PREVIEW_RULES, 'hours': 6, 'env': ' prod-a '})
        assert response.status == 200
        data = (await response.json())['data']
        assert data['hours'] == 6 and data['env'] == 'prod-a' and data['total'] == 2 and data['ignored'] == 1
        sql, *args = query.call_args.args
        assert args == [6, 'prod-a'] and 'AND k8s = $2' in sql
        assert f'LIMIT {rules_api.PREVIEW_LIMIT + 1}' in sql and 'ORDER BY lasttimestamp DESC' in sql
        assert query.call_args.kwargs == {'timeout': rules_api.PREVIEW_TIMEOUT}
        response = await client.post('/api/admin/event-alert-rules/preview', json={'rules': PREVIEW_RULES})
        assert (await response.json())['data']['hours'] == 24
        sql, *args = query.call_args.args
        assert args == [24] and 'k8s =' not in sql
    finally:
        await client.close()
    assert threads == [False, False]  # 匹配在线程里跑


@pytest.mark.asyncio
@pytest.mark.parametrize('body,status,code', [
    ({'rules': PREVIEW_RULES, 'hours': 0}, 422, 'invalid_request'),
    ({'rules': PREVIEW_RULES, 'hours': 169}, 422, 'invalid_request'),
    ({'rules': PREVIEW_RULES, 'hours': '24'}, 422, 'invalid_request'),
    ({'rules': PREVIEW_RULES, 'hours': True}, 422, 'invalid_request'),
    ({'rules': PREVIEW_RULES, 'env': 3}, 422, 'invalid_request'),
    ({'rules': rules_doc([rule('坏', {'count': {'greater_than': 'x'}})])}, 422, 'invalid_rules'),
    ({'hours': 1}, 422, 'invalid_rules'),
])
async def test_preview_rejects_bad_requests(monkeypatch, body, status, code):
    query = AsyncMock(return_value=[])
    monkeypatch.setattr(rules_api.db, 'pg_fetch', query)
    client = await client_for()
    try:
        response = await client.post('/api/admin/event-alert-rules/preview', json=body)
        assert response.status == status and (await response.json())['code'] == code
    finally:
        await client.close()
    assert not query.called


@pytest.mark.asyncio
async def test_preview_reports_database_errors(monkeypatch):
    monkeypatch.setattr(rules_api.db, 'pg_fetch', AsyncMock(side_effect=asyncio.TimeoutError()))
    client = await client_for()
    try:
        response = await client.post('/api/admin/event-alert-rules/preview', json={'rules': PREVIEW_RULES})
        assert response.status == 503 and (await response.json())['code'] == 'db_unavailable'
        response = await client.post('/api/admin/event-alert-rules/preview', data='[]',
                                     headers={'Content-Type': 'application/json'})
        assert response.status == 400
    finally:
        await client.close()
