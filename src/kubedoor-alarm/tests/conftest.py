"""kubedoor-alarm 测试共用的夹具。

- 所有用例都不会真的发网络请求:requests 的底层发送被换成直接报错,用例用 fake_http 模拟 master 和 IM。
- kubedoor-alarm.py 在 import 时会建 PostgreSQL 连接池,alarm_app 用假的 psycopg 模块加载它,不连数据库。
- 通用配置默认不经过 master(alarm_settings);要测向 master 取配置的流程时用 settings_resolver。
"""
import contextlib
import importlib.util
import json
import pathlib
import sys
import types

import pytest
import requests

ALARM_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ALARM_DIR))

import notify_client  # noqa: E402
import settings_client  # noqa: E402


class FakeResponse:
    def __init__(self, status=200, payload=None, text=None):
        self.status_code = status
        self._payload = payload
        if text is None:
            text = json.dumps(payload, ensure_ascii=False) if payload is not None else ''
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError('not json')
        return self._payload


def master_ok(data):
    """master 内部接口的成功返回;data 为 None 表示没有配置。"""
    return FakeResponse(200, {'success': True, 'data': data})


# master /api/internal/settings 返回的通用配置(alarm 只用前四项)
MASTER_SETTINGS = {
    'prom_k8s_tag_key': 'k8s',
    'kubedoor_exturl': 'https://kubedoor.example.com',
    'alertmanager_exturl': 'http://alertmanager.example.com',
    'silence_cache_ttl': 15,
    'retention_ai_checkpoint_days': 7,
}


class FakeHTTP:
    """替换 requests.get / requests.post,记录调用。

    master: 依次返回的结果(FakeResponse 或异常),每次 GET 取一个;
    im: 每次 POST 的结果(FakeResponse 或异常),默认企微 / 钉钉风格的成功返回。
    取配置的异常会被 ChannelResolver 吞掉,所以多出来的 GET 记在 unexpected 里,用例结束时检查。
    """

    def __init__(self):
        self.master = []
        self.gets = []
        self.posts = []
        self.unexpected = []
        self.im = FakeResponse(200, {'errcode': 0, 'errmsg': 'ok'})

    def get(self, url, params=None, headers=None, timeout=None, **kwargs):
        self.gets.append({'url': url, 'params': params, 'headers': headers, 'timeout': timeout})
        if not self.master:
            self.unexpected.append(url)
            raise AssertionError(f'没有为第 {len(self.gets)} 次请求 master 准备返回')
        outcome = self.master.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def post(self, url, headers=None, data=None, timeout=None, **kwargs):
        body = json.loads(data.decode('utf-8') if isinstance(data, bytes) else data)
        self.posts.append({'url': url, 'headers': headers, 'json': body, 'timeout': timeout})
        if isinstance(self.im, Exception):
            raise self.im
        return self.im


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    """兜底:没有被 fake_http 接管的请求一律失败,并在用例结束时报出来。"""
    attempts = []

    def no_network(self, method, url, *args, **kwargs):
        attempts.append(f'{method} {url}')
        raise AssertionError(f'测试里不能发真实的网络请求: {method} {url}')

    monkeypatch.setattr(requests.sessions.Session, 'request', no_network)
    yield
    assert not attempts, f'测试试图发真实的网络请求: {attempts}'


@pytest.fixture
def fake_http(monkeypatch):
    http = FakeHTTP()
    monkeypatch.setattr(requests, 'get', http.get)
    monkeypatch.setattr(requests, 'post', http.post)
    yield http
    assert not http.unexpected, f'多请求了 master: {http.unexpected}'


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def resolver(monkeypatch, clock):
    """换掉模块级的通知配置缓存:每个用例从空缓存开始,时间由 clock 控制。"""
    fresh = notify_client.ChannelResolver(clock=clock)
    monkeypatch.setattr(notify_client, '_resolver', fresh)
    return fresh


@pytest.fixture
def internal_env(monkeypatch):
    monkeypatch.setenv('AI_INTERNAL_TOKEN', 'internal-secret')
    monkeypatch.delenv('MASTER_INTERNAL_URL', raising=False)


class StaticSettings:
    """代替 settings_client 的配置缓存:不经过 master,用例改 values 后下一次取配置就生效。"""

    def __init__(self, values):
        self.values = values

    def current(self):
        return dict(self.values)


@pytest.fixture(autouse=True)
def alarm_settings(monkeypatch):
    """通用配置默认不经过 master:集群标签名是 k8s,配置了 KubeDoor 和 Alertmanager 地址。

    autouse:模块级的配置缓存不会在用例之间串值,也不会去请求 master。返回的 dict 可以直接改。
    """
    values = {key: MASTER_SETTINGS[key] for key in settings_client.DEFAULTS}
    monkeypatch.setattr(settings_client, '_resolver', StaticSettings(values))
    return values


@pytest.fixture
def settings_resolver(monkeypatch, clock, alarm_settings):
    """换成真实的通用配置缓存:从空缓存开始,时间由 clock 控制,向 master 取配置走 fake_http。"""
    fresh = settings_client.SettingsResolver(clock=clock)
    monkeypatch.setattr(settings_client, '_resolver', fresh)
    return fresh


# ---------------------------------------------------------------------------
# 加载 kubedoor-alarm.py(假的 PostgreSQL 连接池)
# ---------------------------------------------------------------------------
class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeConnection:
    def __init__(self, pool):
        self._pool = pool

    def execute(self, sql, params=None):
        self._pool.executed.append((sql, params))
        if sql.lstrip().upper().startswith('SELECT') and 'FROM alert_silences' in sql:
            return FakeResult(self._pool.silence_rows)
        if sql.lstrip().upper().startswith('SELECT') and 'FROM k8s_pod_alert_days' in sql:
            return FakeResult(self._pool.alert_rows)
        return FakeResult([])


class FakePool:
    """代替 psycopg_pool.ConnectionPool。

    silence_rows: alert_silences 的查询结果 (id, matchers, starts_at, ends_at);
    alert_rows: k8s_pod_alert_days「当天是否已有这条告警」的查询结果;
    fail: 为 True 时取连接直接报错(模拟数据库不可用)。
    """

    def __init__(self, *args, **kwargs):
        self.silence_rows = []
        self.alert_rows = []
        self.executed = []
        self.fail = False

    @contextlib.contextmanager
    def connection(self, timeout=None):
        if self.fail:
            raise RuntimeError('database unavailable')
        yield FakeConnection(self)

    def writes(self):
        """执行过的 INSERT / UPDATE k8s_pod_alert_days:[(语句, 参数), ...]。"""
        return [(sql, params) for sql, params in self.executed
                if sql.startswith(('INSERT INTO k8s_pod_alert_days', 'UPDATE k8s_pod_alert_days'))]


def _fake_psycopg_modules():
    psycopg = types.ModuleType('psycopg')
    rows = types.ModuleType('psycopg.rows')
    rows.tuple_row = object()
    conninfo = types.ModuleType('psycopg.conninfo')
    conninfo.make_conninfo = lambda **kwargs: 'fake-conninfo'
    psycopg.rows, psycopg.conninfo = rows, conninfo
    pool = types.ModuleType('psycopg_pool')
    pool.ConnectionPool = FakePool
    return {'psycopg': psycopg, 'psycopg.rows': rows, 'psycopg.conninfo': conninfo, 'psycopg_pool': pool}


@pytest.fixture
def alarm_app(monkeypatch, resolver, internal_env, alarm_settings):
    """加载 kubedoor-alarm.py,返回模块;通用配置见 alarm_settings(集群标签名 k8s,配置了 KubeDoor 地址)。"""
    for name, module in _fake_psycopg_modules().items():
        monkeypatch.setitem(sys.modules, name, module)
    import silence

    spec = importlib.util.spec_from_file_location('kubedoor_alarm_under_test', ALARM_DIR / 'kubedoor-alarm.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # 屏蔽规则每次匹配都重新读(ttl=0),用例改 pool.silence_rows 立即生效
    monkeypatch.setattr(silence, '_engine', silence.SilenceEngine(module.pool, ttl=0))
    return module
