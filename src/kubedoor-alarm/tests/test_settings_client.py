"""settings_client:向 master 取通用配置、缓存与过期、失败时沿用上次的值、一次都没取到时的后备值,
以及用到这些配置的屏蔽引擎(集群标签、规则缓存秒数)跟着配置走。"""
import logging
import threading
import types

import pytest
import requests

import settings_client
import silence
from conftest import MASTER_SETTINGS, FakePool, FakeResponse, master_ok

# alarm 用的四项(master 还会多返回别的,例如给 AI 服务的检查点保留天数)
VALUES = {key: MASTER_SETTINGS[key] for key in settings_client.DEFAULTS}
LEGACY_NAMES = ('PROM_K8S_TAG_KEY', 'KUBEDOOR_EXTURL', 'ALERTMANAGER_EXTURL', 'SILENCE_CACHE_TTL')


@pytest.fixture
def no_legacy_env(monkeypatch):
    for name in LEGACY_NAMES:
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# 取配置
# ---------------------------------------------------------------------------
def test_fetch_calls_master_as_system_identity(fake_http, settings_resolver, internal_env):
    fake_http.master = [master_ok(MASTER_SETTINGS)]
    assert settings_client.current() == VALUES
    # 只带内部令牌、不带 X-User-Name:master 只对系统身份开放这个接口
    assert fake_http.gets == [{
        'url': 'http://kubedoor-master/api/internal/settings',
        'params': None,
        'headers': {'X-Kubedoor-Token': 'internal-secret'},
        'timeout': 5,
    }]


def test_master_url_from_env(fake_http, settings_resolver, internal_env, monkeypatch):
    monkeypatch.setenv('MASTER_INTERNAL_URL', 'http://master.test:8080/')
    fake_http.master = [master_ok(MASTER_SETTINGS)]
    assert settings_client.value('prom_k8s_tag_key') == 'k8s'
    assert fake_http.gets[0]['url'] == 'http://master.test:8080/api/internal/settings'


def test_values_are_normalized(fake_http, settings_resolver, internal_env):
    fake_http.master = [master_ok({
        'prom_k8s_tag_key': ' cluster ', 'kubedoor_exturl': ' https://kd.example.com/ ',
        'alertmanager_exturl': None, 'silence_cache_ttl': 0, 'retention_ai_checkpoint_days': 30, 'other': 1,
    })]
    assert settings_client.current() == {
        'prom_k8s_tag_key': 'cluster', 'kubedoor_exturl': 'https://kd.example.com',
        'alertmanager_exturl': '', 'silence_cache_ttl': 0,
    }


def test_current_returns_a_copy(fake_http, settings_resolver, internal_env):
    fake_http.master = [master_ok(MASTER_SETTINGS)]
    settings_client.current()['prom_k8s_tag_key'] = 'changed'
    assert settings_client.value('prom_k8s_tag_key') == 'k8s'


def test_cache_hit_within_ttl_then_refetch_after_expiry(fake_http, settings_resolver, clock, internal_env):
    changed = {**MASTER_SETTINGS, 'silence_cache_ttl': 60}
    fake_http.master = [master_ok(MASTER_SETTINGS), master_ok(changed)]
    assert settings_client.value('silence_cache_ttl') == 15
    clock.now += 14.9
    assert settings_client.value('silence_cache_ttl') == 15
    assert len(fake_http.gets) == 1
    clock.now += 0.2  # 距第一次取配置 15.1 秒,已过期
    assert settings_client.value('silence_cache_ttl') == 60
    assert len(fake_http.gets) == 2


@pytest.mark.parametrize('failure', [
    requests.ConnectionError('connection refused'),
    requests.Timeout('read timed out'),
    FakeResponse(404, text='404: Not Found'),  # 还没有这个接口的旧 master
    FakeResponse(403, {'success': False, 'code': 'forbidden', 'message': '仅限集群内服务用内部 token 调用'}),
    FakeResponse(503, {'success': False, 'code': 'not_ready', 'message': '通用配置还没有加载'}),
    FakeResponse(200, {'success': False, 'message': 'boom'}),
    FakeResponse(200, text='<html>not json</html>'),
    master_ok(None),
    master_ok('settings'),
    master_ok({k: v for k, v in MASTER_SETTINGS.items() if k != 'kubedoor_exturl'}),
    master_ok({**MASTER_SETTINGS, 'prom_k8s_tag_key': ' '}),
    master_ok({**MASTER_SETTINGS, 'kubedoor_exturl': 123}),
    master_ok({**MASTER_SETTINGS, 'silence_cache_ttl': -1}),
    master_ok({**MASTER_SETTINGS, 'silence_cache_ttl': True}),
    master_ok({**MASTER_SETTINGS, 'silence_cache_ttl': 86400}),
], ids=['connect', 'timeout', 'http404', 'http403', 'http503', 'not-success', 'not-json', 'no-data', 'bad-data',
        'missing-key', 'blank-tag-key', 'bad-url', 'negative-ttl', 'bool-ttl', 'huge-ttl'])
def test_master_failure_keeps_last_good_settings(fake_http, settings_resolver, clock, internal_env, failure, caplog):
    changed = {**MASTER_SETTINGS, 'prom_k8s_tag_key': 'cluster'}
    fake_http.master = [master_ok(MASTER_SETTINGS), failure, master_ok(changed)]
    assert settings_client.current() == VALUES
    clock.now += 16
    with caplog.at_level(logging.WARNING):
        assert settings_client.current() == VALUES
    assert '沿用上次取到的配置' in caplog.text
    # 失败后同样等一个 TTL 再试,不让每条告警都去等 master 超时
    clock.now += 10
    assert settings_client.current() == VALUES
    assert len(fake_http.gets) == 2
    clock.now += 6
    assert settings_client.value('prom_k8s_tag_key') == 'cluster'  # master 恢复
    assert len(fake_http.gets) == 3


def test_never_fetched_uses_legacy_env_then_defaults(fake_http, settings_resolver, clock, internal_env,
                                                     no_legacy_env, monkeypatch, caplog):
    monkeypatch.setenv('PROM_K8S_TAG_KEY', 'k8s')
    monkeypatch.setenv('KUBEDOOR_EXTURL', 'https://old.example.com/')
    monkeypatch.setenv('SILENCE_CACHE_TTL', 'abc')  # 不合法:用默认值
    fake_http.master = [requests.ConnectionError('connection refused'), master_ok({**MASTER_SETTINGS, 'silence_cache_ttl': 30})]
    with caplog.at_level(logging.WARNING):
        assert settings_client.current() == {
            'prom_k8s_tag_key': 'k8s', 'kubedoor_exturl': 'https://old.example.com',
            'alertmanager_exturl': '', 'silence_cache_ttl': 15,
        }
    assert '之前也没有取到过' in caplog.text
    # 失败也记下时间:master 不可用期间每 15 秒才试一次
    clock.now += 14
    assert settings_client.value('kubedoor_exturl') == 'https://old.example.com'
    assert len(fake_http.gets) == 1
    clock.now += 1
    assert settings_client.current() == {**VALUES, 'silence_cache_ttl': 30}
    assert len(fake_http.gets) == 2


@pytest.mark.parametrize('env, expected', [
    ({}, settings_client.DEFAULTS),
    ({'PROM_K8S_TAG_KEY': ' cluster ', 'KUBEDOOR_EXTURL': 'https://kd.example.com/',
      'ALERTMANAGER_EXTURL': 'http://am.example.com:9093', 'SILENCE_CACHE_TTL': ' 30 '},
     {'prom_k8s_tag_key': 'cluster', 'kubedoor_exturl': 'https://kd.example.com',
      'alertmanager_exturl': 'http://am.example.com:9093', 'silence_cache_ttl': 30}),
    ({'PROM_K8S_TAG_KEY': '  ', 'KUBEDOOR_EXTURL': '', 'SILENCE_CACHE_TTL': '-1'}, settings_client.DEFAULTS),
    ({'SILENCE_CACHE_TTL': '0'}, {**settings_client.DEFAULTS, 'silence_cache_ttl': 0}),
    ({'SILENCE_CACHE_TTL': '99999'}, settings_client.DEFAULTS),
    ({'SILENCE_CACHE_TTL': '１５'}, settings_client.DEFAULTS),  # 全角数字不认
], ids=['defaults', 'legacy-env', 'blank-or-invalid', 'zero-ttl', 'huge-ttl', 'fullwidth-ttl'])
def test_fallback_values(no_legacy_env, monkeypatch, env, expected):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert settings_client.fallback_values() == expected


def test_missing_internal_token_uses_fallback_without_calling_master(fake_http, settings_resolver, no_legacy_env,
                                                                    monkeypatch, caplog):
    monkeypatch.delenv('AI_INTERNAL_TOKEN', raising=False)
    with caplog.at_level(logging.WARNING):
        assert settings_client.current() == settings_client.DEFAULTS
    assert fake_http.gets == []
    assert 'AI_INTERNAL_TOKEN' in caplog.text


def test_unexpected_fetch_error_is_treated_as_failure(clock, no_legacy_env, caplog):
    def broken():
        raise RuntimeError('bug')

    fresh = settings_client.SettingsResolver(fetch=broken, clock=clock)
    with caplog.at_level(logging.WARNING):
        assert fresh.current() == settings_client.DEFAULTS
    assert 'RuntimeError' in caplog.text


def test_fetch_logs_never_contain_token(fake_http, settings_resolver, clock, internal_env, caplog):
    fake_http.master = [master_ok(MASTER_SETTINGS), FakeResponse(503, {'success': False, 'message': 'unavailable'})]
    with caplog.at_level(logging.DEBUG):
        settings_client.current()
        clock.now += 20
        settings_client.current()
    assert 'unavailable' in caplog.text
    assert 'internal-secret' not in caplog.text


# ---------------------------------------------------------------------------
# 并发:过期后只让一个请求去 master 取
# ---------------------------------------------------------------------------
def test_expired_settings_are_refreshed_by_one_request_while_others_keep_going(clock):
    started, release = threading.Event(), threading.Event()
    calls = []
    newer = {**VALUES, 'kubedoor_exturl': 'https://kd-new.example.com'}

    def fetch():
        calls.append(1)
        if len(calls) == 1:
            return dict(VALUES)
        started.set()
        assert release.wait(5)
        return dict(newer)

    resolver = settings_client.SettingsResolver(fetch=fetch, clock=clock)
    assert resolver.current() == VALUES
    clock.now += 20
    results = []
    worker = threading.Thread(target=lambda: results.append(resolver.current()))
    worker.start()
    assert started.wait(5)
    # 另一个请求不等 master,先用正在用的配置
    assert resolver.current() == VALUES
    release.set()
    worker.join(5)
    assert results == [newer]
    assert resolver.current() == newer
    assert len(calls) == 2


def test_first_fetch_is_waited_for_instead_of_using_fallback(clock, no_legacy_env):
    started, release = threading.Event(), threading.Event()
    calls = []

    def fetch():
        calls.append(1)
        started.set()
        assert release.wait(5)
        return dict(VALUES)

    resolver = settings_client.SettingsResolver(fetch=fetch, clock=clock)
    results = []
    threads = [threading.Thread(target=lambda: results.append(resolver.current())) for _ in range(3)]
    threads[0].start()
    assert started.wait(5)
    for thread in threads[1:]:
        thread.start()
    # 一次都还没取到时不先用默认值(集群标签可能不是默认的),等第一次取完
    threads[1].join(0.2)
    assert threads[1].is_alive() and results == []
    release.set()
    for thread in threads:
        thread.join(5)
    assert results == [VALUES] * 3
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# 用到配置的地方:屏蔽引擎
# ---------------------------------------------------------------------------
def test_silence_env_alias_follows_cluster_label_in_settings(alarm_settings):
    labels = {'alertname': 'X', 'k8s': 'prod-a', 'cluster': 'prod-b'}
    assert silence.build_match_labels(labels)['env'] == 'prod-a'
    alarm_settings['prom_k8s_tag_key'] = 'cluster'
    assert silence.build_match_labels(labels)['env'] == 'prod-b'


def test_silence_cache_ttl_follows_settings_without_restart(alarm_settings, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(silence, 'time', types.SimpleNamespace(monotonic=lambda: now[0]))
    pool = FakePool()
    engine = silence.SilenceEngine(pool)  # 不传 ttl:按通用配置

    def loads():
        return sum('FROM alert_silences' in sql for sql, _ in pool.executed)

    alarm_settings['silence_cache_ttl'] = 30
    engine.match({'alertname': 'X'})
    assert loads() == 1
    now[0] += 20
    engine.match({'alertname': 'X'})
    assert loads() == 1  # 30 秒内用缓存的规则
    alarm_settings['silence_cache_ttl'] = 10  # 页面上改小,不用重启
    engine.match({'alertname': 'X'})
    assert loads() == 2
