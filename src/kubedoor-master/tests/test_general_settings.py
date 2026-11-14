"""通用配置：配置项校验、地址工具、存储格式与内存快照（general_settings/fields.py、service.py）。

FakeStorage 和 fresh_state 也给 test_general_settings_api.py、test_general_settings_migrate.py 用。
"""
import asyncio
import json
import threading
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from loguru import logger

import general_settings
from agent_install import bundle
from auth.crypto import decrypt_secret, encrypt_secret
from general_settings import api as gs_api
from general_settings import fields, service

UPDATED_AT = datetime(2026, 11, 15, 2, 0, tzinfo=timezone.utc)
KEY = '11' * 32
PASSWORD = 'S3cret-P@ss'  # 测试用的密码：响应、库里、日志、审计、汇总里都不能出现明文
PROM_URL = 'http://monit:S3cret-P%40ss@vm.example.com:8428'
SPEC_KEYS = [
    'prom_type', 'prom_url', 'metrics_write_url', 'prom_k8s_tag_key', 'kubedoor_exturl', 'alertmanager_exturl',
    'oss_url', 'image_repo', 'image_vmagent', 'image_kube_state_metrics', 'image_node_exporter', 'node_shell_image',
    'retention_events_days', 'retention_alerts_days', 'retention_resources_days', 'retention_audit_days',
    'retention_ai_checkpoint_days', 'auth_session_hours', 'auth_idle_hours', 'auth_remember_days',
    'auth_login_max_failures', 'auth_lock_minutes', 'auth_cookie_secure', 'agent_auth_required',
    'alert_dedup_window', 'silence_cache_ttl', 'admis_cache_ttl',
]


def _copy(value):
    return json.loads(json.dumps(value))


class FakeStorage:
    """auth_settings 的内存版：general_settings 这一行 + 其他 key（notification 等）；读写都做一次 JSON 往返"""

    def __init__(self, value=None, updated_by='kubedoor', others=None):
        self.row = None if value is None else {'value': _copy(value), 'updated_by': updated_by,
                                               'updated_at': UPDATED_AT}
        self.others = {key: _copy(item) for key, item in (others or {}).items()}
        self.error = None  # 读写都失败
        self.write_error = None  # 只有写失败
        self.writes = []  # [(value, updated_by)]
        self.inserts = []  # [(key, value, updated_by)]

    def current_row(self):
        return None if self.row is None else {**self.row, 'value': _copy(self.row['value'])}

    async def read(self):
        if self.error is not None:
            raise self.error
        return self.current_row()

    async def update(self, change, updated_by):
        if self.error is not None:
            raise self.error
        value = change(_copy(self.row['value']) if self.row else {})
        if value is None:
            return self.current_row(), False
        if self.write_error is not None:
            raise self.write_error
        self.writes.append((_copy(value), updated_by))
        self.row = {'value': _copy(value), 'updated_by': updated_by, 'updated_at': UPDATED_AT}
        return self.current_row(), True

    async def setting_exists(self, key):
        if self.error is not None:
            raise self.error
        return key in self.others

    async def insert_absent(self, key, value, updated_by):
        if self.error is not None or self.write_error is not None:
            raise self.error or self.write_error
        if key in self.others:
            return False
        self.others[key] = _copy(value)
        self.inserts.append((key, _copy(value), updated_by))
        return True


def stored_value(**items):
    """存库格式：三个地址加密，其他原样"""
    return {key: encrypt_secret(item) if key in fields.SECRET_KEYS else item for key, item in items.items()}


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    """每个用例从「快照没加载、库是空的」开始"""
    monkeypatch.setenv('AI_ENCRYPTION_KEY', KEY)
    monkeypatch.setattr(service, '_snapshot', service.Snapshot())
    monkeypatch.setattr(service, '_summary', None)
    monkeypatch.setattr(service, '_refresh_failed', False)
    monkeypatch.setattr(service, '_warned', set())
    monkeypatch.setattr(service, 'write_lock', asyncio.Lock())
    monkeypatch.setattr(service, 'storage', FakeStorage())
    monkeypatch.setattr(gs_api, '_last_test', {})


@pytest.fixture
def storage(monkeypatch):
    """换成指定内容的内存库：storage(value, others={...})，返回 FakeStorage"""

    def install(value=None, **kwargs):
        fake = FakeStorage(value, **kwargs)
        monkeypatch.setattr(service, 'storage', fake)
        return fake

    return install


@pytest.fixture
def logs():
    lines = []
    handler_id = logger.add(lambda message: lines.append(str(message)), level='DEBUG')
    yield lines
    logger.remove(handler_id)


# ---------------------------------------------------------------------------
# 定义表
# ---------------------------------------------------------------------------
def test_fields_follow_the_spec_table():
    assert list(fields.FIELDS) == SPEC_KEYS
    assert general_settings.DEFAULTS == {
        'prom_type': 'Victoria-Metrics-Single', 'prom_url': '', 'metrics_write_url': '',
        'prom_k8s_tag_key': 'origin_prometheus', 'kubedoor_exturl': '', 'alertmanager_exturl': '', 'oss_url': '',
        'image_repo': bundle.BASE['image_repo'], 'image_vmagent': bundle.BASE['image_vmagent'],
        'image_kube_state_metrics': bundle.BASE['image_kube_state_metrics'],
        'image_node_exporter': bundle.BASE['image_node_exporter'], 'node_shell_image': '',
        'retention_events_days': 90, 'retention_alerts_days': 365, 'retention_resources_days': 365,
        'retention_audit_days': 180, 'retention_ai_checkpoint_days': 7,
        'auth_session_hours': 12.0, 'auth_idle_hours': 4.0, 'auth_remember_days': 7.0,
        'auth_login_max_failures': 5, 'auth_lock_minutes': 15, 'auth_cookie_secure': 'auto',
        'agent_auth_required': True, 'alert_dedup_window': 300, 'silence_cache_ttl': 15, 'admis_cache_ttl': 60,
    }
    types = {'int': int, 'number': float, 'bool': bool}
    for spec in fields.FIELDS.values():
        assert type(spec.default) is types.get(spec.kind, str), spec.key
        assert spec.label and spec.group in fields.GROUPS
    # 页面分组按约定的顺序出现
    assert list(dict.fromkeys(spec.group for spec in fields.FIELDS.values())) == list(fields.GROUPS)
    assert fields.SECRET_KEYS == ('prom_url', 'metrics_write_url', 'oss_url')
    assert general_settings.LABELS['metrics_write_url'] == '远程写地址'
    envs = {key: spec.env for key, spec in fields.FIELDS.items()}
    assert envs['prom_url'] == 'PROM_URL' and envs['auth_cookie_secure'] == 'AUTH_COOKIE_SECURE'
    assert envs['retention_ai_checkpoint_days'] == 'RETENTION_AI_CHECKPOINT_DAYS' and envs['oss_url'] == 'OSS_URL'


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('key, raw, expected', [
    ('prom_type', 'Prometheus', 'Prometheus'),
    ('prom_type', ' victoria-metrics-cluster ', 'Victoria-Metrics-Cluster'),
    ('prom_url', ' http://vm:8428/ ', 'http://vm:8428'),
    ('prom_url', 'HTTP://VM:8428', 'http://VM:8428'),
    ('prom_url', 'http://[::1]:8428', 'http://[::1]:8428'),
    ('prom_url', 'https://monit:p@ss@vm.example.com:8481/select/0/prometheus/',
     'https://monit:p%40ss@vm.example.com:8481/select/0/prometheus'),  # 密码里的 @ 统一编码
    ('prom_url', 'http://monit:@vm:8428', 'http://monit@vm:8428'),
    ('prom_url', 'http://monit@vm:8428', 'http://monit@vm:8428'),
    ('prom_url', 'http://127.0.0.1:8428', 'http://127.0.0.1:8428'),  # 本地开发也能用
    ('metrics_write_url', 'http://vm:8428/api/v1/write/?extra_label=env=prod',
     'http://vm:8428/api/v1/write?extra_label=env=prod'),  # vmagent 的远程写地址可以带查询参数
    ('prom_k8s_tag_key', ' origin_prometheus ', 'origin_prometheus'),
    ('prom_k8s_tag_key', '_k' + 'x' * 126, '_k' + 'x' * 126),
    ('kubedoor_exturl', 'https://kd.example.com/', 'https://kd.example.com'),
    ('kubedoor_exturl', 'http://10.0.0.1:30080', 'http://10.0.0.1:30080'),
    ('alertmanager_exturl', 'https://am.example.com/alertmanager/', 'https://am.example.com/alertmanager'),
    ('oss_url', 'http://u:p%2Fq@oss:9000/bucket/', 'http://u:p%2Fq@oss:9000/bucket'),
    ('image_repo', 'harbor.local:5000/kubedoor//', 'harbor.local:5000/kubedoor'),
    ('image_vmagent', 'harbor.local/vm/vmagent:v1.100.0', 'harbor.local/vm/vmagent:v1.100.0'),
    ('node_shell_image', 'harbor.local/alpine@sha256:abc', 'harbor.local/alpine@sha256:abc'),
    ('node_shell_image', 'r/' + 'x' * 510, 'r/' + 'x' * 510),  # 与 agent 的上限 512 一致
    ('retention_events_days', '0', 0),
    ('retention_events_days', 3650, 3650),
    ('retention_events_days', 30.0, 30),
    ('retention_ai_checkpoint_days', ' 14 ', 14),
    ('auth_session_hours', '0.5', 0.5),
    ('auth_session_hours', 720, 720.0),
    ('auth_idle_hours', 0, 0.0),  # 0 = 不限空闲时间(原来的语义)
    ('auth_remember_days', '365', 365.0),
    ('auth_login_max_failures', 100, 100),
    ('auth_lock_minutes', '1440', 1440),
    ('auth_cookie_secure', 'TRUE', 'true'),
    ('agent_auth_required', False, False),
    ('alert_dedup_window', 0, 0),
    ('silence_cache_ttl', 1, 1),
    ('admis_cache_ttl', 0, 0),
])
def test_clean_accepts_and_normalizes(key, raw, expected):
    result = fields.clean(key, raw)
    assert result == expected and type(result) is type(expected)


@pytest.mark.parametrize('key, raw, message', [
    ('prom_type', 'influxdb', '时序库类型只能选 VictoriaMetrics 单机版（Victoria-Metrics-Single）'),
    ('prom_url', 'vm:8428', '查询地址必须是完整的 http:// 或 https:// 地址'),
    ('prom_url', 'ftp://vm:8428', '查询地址必须是完整的 http:// 或 https:// 地址'),
    ('prom_url', 'http://vm:8428/#/x', '查询地址不能包含锚点'),
    ('prom_url', 'http://vm:8428?a=1', '查询地址不能包含查询参数'),
    ('prom_url', 'http://vm 1:8428', '查询地址不能包含空白、反斜杠或控制字符'),
    ('prom_url', 'http://vm:8428\\x', '查询地址不能包含空白、反斜杠或控制字符'),
    ('prom_url', 'http://vm:8428/\x00', '查询地址不能包含空白、反斜杠或控制字符'),
    ('prom_url', 'http://vm:99999', '查询地址格式不正确'),
    ('prom_url', 'http://[::1', '查询地址格式不正确'),
    ('prom_url', 'http://vm:0', '查询地址的端口无效'),
    ('prom_url', 'http://:pw@vm:8428', '查询地址的 @ 前面缺少账号'),
    ('prom_url', 'http://vm/' + 'x' * 2048, '查询地址太长'),
    ('prom_url', 8428, '查询地址必须是字符串'),
    ('kubedoor_exturl', 'https://kd.example.com/kubedoor', 'KubeDoor 访问地址只填写协议、主机和端口'),
    ('kubedoor_exturl', 'https://admin:pw@kd.example.com', 'KubeDoor 访问地址不需要填写账号或密码'),
    ('kubedoor_exturl', 'https://kd.example.com/?a=1', 'KubeDoor 访问地址不能包含查询参数'),
    ('alertmanager_exturl', 'https://am.example.com?a=1', 'Alertmanager 外部地址不能包含查询参数'),
    ('alertmanager_exturl', 'https://admin@am.example.com', 'Alertmanager 外部地址不需要填写账号或密码'),
    ('oss_url', 'http://oss:9000/bucket?sign=1', '对象存储上传地址不能包含查询参数'),
    ('prom_k8s_tag_key', '1cluster', '集群指标标签 key 只能包含字母、数字和下划线'),
    ('prom_k8s_tag_key', 'k8s-cluster', '集群指标标签 key 只能包含字母、数字和下划线'),
    ('prom_k8s_tag_key', 'k' * 129, '集群指标标签 key 只能包含'),
    ('image_repo', '/kubedoor', '镜像仓库只能包含字母、数字和 . _ : / -'),
    ('image_repo', 'harbor local/kd', '镜像仓库只能包含'),
    ('image_repo', 'r/' + 'x' * 255, '镜像仓库只能包含'),
    ('image_vmagent', 'vm agent:v1', 'vmagent 镜像不能包含空白、引号、反斜杠或控制字符'),
    ('image_kube_state_metrics', "ksm'x", 'kube-state-metrics 镜像不能包含'),
    ('image_node_exporter', 'r/' + 'x' * 255, 'node-exporter 镜像不能包含'),
    ('node_shell_image', 'r/' + 'x' * 511, '节点终端镜像不能包含'),
    ('retention_events_days', -1, 'K8S 事件保留天数必须是 0 到 3650 之间的整数'),
    ('retention_events_days', 3651, 'K8S 事件保留天数必须是 0 到 3650 之间的整数'),
    ('retention_events_days', '1.5', 'K8S 事件保留天数必须是整数'),
    ('retention_events_days', 1.5, 'K8S 事件保留天数必须是整数'),
    ('retention_events_days', True, 'K8S 事件保留天数必须是整数'),
    ('retention_events_days', 'abc', 'K8S 事件保留天数必须是整数'),
    ('retention_events_days', [30], 'K8S 事件保留天数必须是整数'),
    ('auth_session_hours', 0, '登录会话有效期（小时）必须大于 0，最大 720'),
    ('auth_session_hours', 720.5, '登录会话有效期（小时）必须大于 0，最大 720'),
    ('auth_session_hours', 'nan', '登录会话有效期（小时）必须是数字'),
    ('auth_session_hours', float('inf'), '登录会话有效期（小时）必须是数字'),
    ('auth_session_hours', 10 ** 400, '登录会话有效期（小时）必须大于 0'),
    ('auth_idle_hours', -0.1, '空闲超时（小时）必须在 0 到 720 之间'),
    ('auth_remember_days', 0, '「记住我」有效天数必须大于 0，最大 365'),
    ('auth_remember_days', 366, '「记住我」有效天数必须大于 0，最大 365'),
    ('auth_login_max_failures', 0, '登录失败锁定次数必须是 1 到 100 之间的整数'),
    ('auth_login_max_failures', 101, '登录失败锁定次数必须是 1 到 100 之间的整数'),
    ('auth_lock_minutes', 0, '锁定时长（分钟）必须是 1 到 1440 之间的整数'),
    ('auth_cookie_secure', 'yes', 'Cookie Secure 只能选 auto、true、false'),
    ('agent_auth_required', 'true', 'Agent 连接必须带账号只能是 true 或 false'),
    ('agent_auth_required', 1, 'Agent 连接必须带账号只能是 true 或 false'),
    ('alert_dedup_window', 86401, '事件告警去重窗口（秒）必须是 0 到 86400 之间的整数'),
    ('silence_cache_ttl', 0, '告警屏蔽规则缓存（秒）必须是 1 到 3600 之间的整数'),
    ('admis_cache_ttl', 3601, '准入查询缓存（秒）必须是 0 到 3600 之间的整数'),
])
def test_clean_rejects_with_chinese_reason(key, raw, message):
    with pytest.raises(ValueError) as info:
        fields.clean(key, raw)
    assert str(info.value).startswith(message), str(info.value)


def test_errors_never_echo_the_submitted_address():
    for raw in ('http://monit:S3cret-P@ss@vm:8428?x=1', 'vm-secret-host:8428', 'http://monit:S3cret@vm:0'):
        with pytest.raises(ValueError) as info:
            fields.clean('prom_url', raw)
        assert 'S3cret' not in str(info.value) and 'secret-host' not in str(info.value)


def test_clean_rejects_unknown_key():
    with pytest.raises(KeyError):
        fields.clean('PROM_URL', 'http://vm:8428')


def test_blank_values_mean_delete():
    assert fields.is_blank(None) and fields.is_blank('') and fields.is_blank('   ')
    assert not fields.is_blank(0) and not fields.is_blank(False) and not fields.is_blank('0')


@pytest.mark.parametrize('previous, submitted, expected', [
    # 账号、协议、主机、端口都没变:换回已保存的密码(路径可以改)
    (PROM_URL, 'http://monit:******@vm.example.com:8428', PROM_URL),
    (PROM_URL, 'http://monit:******@VM.example.com:8428/select/0/prometheus/',
     'http://monit:S3cret-P%40ss@VM.example.com:8428/select/0/prometheus'),
    (PROM_URL, 'http://monit:%2A%2A%2A%2A%2A%2A@vm.example.com:8428', PROM_URL),  # 编码过的 ****** 也认
])
def test_mask_restores_saved_password(previous, submitted, expected):
    assert fields.clean('prom_url', submitted, previous=previous) == expected


@pytest.mark.parametrize('previous', [
    '',  # 没有已保存的
    'http://monit@vm.example.com:8428',  # 已保存的没有密码
    'http://other:S3cret-P%40ss@vm.example.com:8428',  # 账号变了
    'http://monit:S3cret-P%40ss@evil.example.com:8428',  # 主机变了:不能把密码发到别处
    'http://monit:S3cret-P%40ss@vm.example.com:9428',  # 端口变了
    'https://monit:S3cret-P%40ss@vm.example.com:8428',  # 协议变了
])
def test_mask_needs_matching_saved_password(previous):
    with pytest.raises(ValueError, match='请重新填写查询地址的密码'):
        fields.clean('prom_url', 'http://monit:******@vm.example.com:8428', previous=previous)


def test_mask_is_a_literal_password_when_not_replacing():
    """旧配置迁移(previous=None)不做替换,****** 就是密码本身"""
    assert fields.clean('prom_url', 'http://monit:******@vm:8428') == 'http://monit:%2A%2A%2A%2A%2A%2A@vm:8428'


# ---------------------------------------------------------------------------
# 地址工具
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('url, expected', [
    ('', ''),
    (None, ''),
    ('http://vm:8428', 'http://vm:8428'),
    ('http://monit@vm:8428', 'http://monit@vm:8428'),
    ('http://monit:@vm:8428', 'http://monit:@vm:8428'),
    (PROM_URL + '/select/0/prometheus', 'http://monit:******@vm.example.com:8428/select/0/prometheus'),
    ('https://w:pw@vm/api/v1/write?extra_label=a=b', 'https://w:******@vm/api/v1/write?extra_label=a=b'),
    ('http://a:p@ss@vm/x', 'http://a:******@vm/x'),  # 没编码的 @ 也不漏
    ('monit:pw@vm:8428', 'monit:******@vm:8428'),  # 没有协议也不漏
])
def test_masked(url, expected):
    assert general_settings.masked(url) == expected


def test_url_credentials():
    assert general_settings.url_credentials(PROM_URL + '/x') == ('http://vm.example.com:8428/x', 'monit', PASSWORD)
    assert general_settings.url_credentials('http://vm:8428') == ('http://vm:8428', '', '')


@pytest.mark.parametrize('key, text, expected', [
    ('prom_type', 'Victoria-Metrics-Cluster', 'Victoria-Metrics-Cluster'),
    ('prom_type', 'VictoriaMetrics', 'Victoria-Metrics-Single'),
    ('prom_type', 'victoria cluster', 'Victoria-Metrics-Cluster'),
    ('prom_type', 'prometheus', 'Prometheus'),
    ('prom_type', 'thanos', 'Prometheus'),
    ('auth_cookie_secure', 'YES', 'true'),
    ('auth_cookie_secure', '0', 'false'),
    ('auth_cookie_secure', 'on', 'auto'),  # 原来认不出的值都按 auto
    ('agent_auth_required', 'Off', False),
    ('agent_auth_required', 'no', False),
    ('agent_auth_required', 'whatever', True),  # 原来:不是 false/0/no/off 就是真
    ('retention_events_days', '-1', 0),  # 原来负数也是不清理
    ('retention_ai_checkpoint_days', '-3', 0),
    ('auth_idle_hours', '-1', 0.0),  # 原来不大于 0 就不限
    ('auth_session_hours', ' 24 ', 24.0),
    ('prom_url', 'http://vm:8428/', 'http://vm:8428'),
])
def test_legacy_values_keep_old_semantics(key, text, expected):
    result = fields.legacy(key, text)
    assert result == expected and type(result) is type(expected)


@pytest.mark.parametrize('key, text', [
    ('auth_lock_minutes', '0'), ('auth_session_hours', '-1'), ('silence_cache_ttl', '0'),
    ('alert_dedup_window', '5m'), ('prom_url', 'vm:8428'), ('prom_k8s_tag_key', 'k8s-name'),
])
def test_legacy_rejects_values_new_rules_cannot_express(key, text):
    with pytest.raises(ValueError):
        fields.legacy(key, text)


# ---------------------------------------------------------------------------
# 快照
# ---------------------------------------------------------------------------
def test_value_uses_defaults_before_loading():
    assert not general_settings.is_loaded()
    for key, default in general_settings.DEFAULTS.items():
        assert general_settings.value(key) == default and type(general_settings.value(key)) is type(default)
    with pytest.raises(KeyError):
        general_settings.value('PROM_URL')


@pytest.mark.asyncio
async def test_refresh_loads_stored_values(storage, logs):
    storage(stored_value(prom_url=PROM_URL, prom_type='Prometheus', retention_events_days=30,
                         auth_session_hours=2.5, agent_auth_required=False, future_key='x'))
    assert await general_settings.refresh() is True
    assert general_settings.is_loaded()
    assert general_settings.value('prom_url') == PROM_URL  # 解密后的完整地址
    assert general_settings.value('prom_type') == 'Prometheus'
    assert general_settings.value('retention_events_days') == 30
    assert general_settings.value('auth_session_hours') == 2.5
    assert general_settings.value('agent_auth_required') is False
    assert general_settings.value('metrics_write_url') == '' and general_settings.value('oss_url') == ''
    assert general_settings.value('prom_k8s_tag_key') == 'origin_prometheus'
    assert service.current().stored == {'prom_url', 'prom_type', 'retention_events_days', 'auth_session_hours',
                                         'agent_auth_required'}
    # 概况日志只有 key 名
    assert any('通用配置已加载' in line and 'prom_url' in line for line in logs)
    assert PASSWORD not in '\n'.join(logs) and 'vm.example.com' not in '\n'.join(logs)


@pytest.mark.asyncio
async def test_bad_stored_values_fall_back_and_are_reported_once(storage, monkeypatch, logs):
    monkeypatch.setenv('AI_ENCRYPTION_KEY', '22' * 32)
    sealed_with_old_key = encrypt_secret(PROM_URL)
    monkeypatch.setenv('AI_ENCRYPTION_KEY', KEY)  # 密钥换过
    storage({'prom_url': sealed_with_old_key, 'oss_url': encrypt_secret('ftp://oss-secret-host/b'),
             'metrics_write_url': 123, 'retention_events_days': 'abc', 'auth_cookie_secure': 'maybe',
             'agent_auth_required': 'yes', 'node_shell_image': ''})
    assert await general_settings.refresh() and await general_settings.refresh()
    snapshot = service.current()
    assert snapshot.values['prom_url'] == '' and snapshot.problems['prom_url'] == service.UNDECRYPTABLE
    assert snapshot.problems['prom_url'] == '已保存的地址无法解密（加密密钥变了？），请重新填写'
    assert snapshot.values['oss_url'] == '' and snapshot.problems['oss_url'].endswith('请重新填写')
    assert snapshot.values['metrics_write_url'] == '' and '格式不正确' in snapshot.problems['metrics_write_url']
    assert snapshot.values['retention_events_days'] == 90
    assert snapshot.problems['retention_events_days'].endswith('当前使用默认值')
    assert snapshot.values['auth_cookie_secure'] == 'auto' and snapshot.values['agent_auth_required'] is True
    assert snapshot.values['node_shell_image'] == '' and 'node_shell_image' not in snapshot.problems
    assert set(snapshot.problems) == {'prom_url', 'oss_url', 'metrics_write_url', 'retention_events_days',
                                      'auth_cookie_secure', 'agent_auth_required'}
    warnings = [line for line in logs if '通用配置「' in line]
    assert len(warnings) == 6  # 每个问题只提示一次
    text = '\n'.join(logs)
    assert PASSWORD not in text and 'oss-secret-host' not in text


@pytest.mark.asyncio
async def test_refresh_failure_keeps_previous_snapshot(storage, logs):
    fake = storage(stored_value(prom_type='Prometheus'))
    assert await general_settings.refresh()
    fake.error = RuntimeError('db down')
    assert await general_settings.refresh() is False
    assert await general_settings.refresh() is False
    assert general_settings.value('prom_type') == 'Prometheus' and general_settings.is_loaded()
    assert sum('读取通用配置失败' in line for line in logs) == 1  # 只在第一次失败时提示
    fake.error = None
    fake.row['value'] = {'prom_type': 'Victoria-Metrics-Cluster'}
    assert await general_settings.refresh()
    assert general_settings.value('prom_type') == 'Victoria-Metrics-Cluster'
    assert any('读取通用配置已恢复' in line for line in logs)


@pytest.mark.asyncio
async def test_refresh_failure_before_first_load_keeps_defaults(storage):
    storage().error = RuntimeError('db down')
    assert await general_settings.refresh() is False
    assert not general_settings.is_loaded()
    assert general_settings.value('prom_k8s_tag_key') == 'origin_prometheus'


@pytest.mark.asyncio
async def test_refresh_loop_follows_database_and_survives_errors(storage, monkeypatch):
    fake = storage(stored_value(prom_type='Prometheus'))
    real_refresh = service.refresh
    calls = []

    async def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError('unexpected')  # refresh 本身不抛,这里模拟意外:循环不能退出
        return await real_refresh()

    monkeypatch.setattr(service, 'refresh', flaky)
    task = asyncio.create_task(service.refresh_loop(0.01))
    try:
        for _ in range(200):
            await asyncio.sleep(0.01)
            if general_settings.value('prom_type') == 'Prometheus':
                break
        assert general_settings.value('prom_type') == 'Prometheus'
        fake.row['value'] = {'prom_type': 'Victoria-Metrics-Cluster'}  # 直接改库
        for _ in range(200):
            await asyncio.sleep(0.01)
            if general_settings.value('prom_type') == 'Victoria-Metrics-Cluster':
                break
        assert general_settings.value('prom_type') == 'Victoria-Metrics-Cluster'
    finally:
        task.cancel()


@pytest.mark.asyncio
async def test_value_is_safe_from_other_threads(storage):
    storage(stored_value(prom_url=PROM_URL))
    await general_settings.refresh()
    assert await asyncio.to_thread(general_settings.value, 'prom_url') == PROM_URL
    seen, stop = set(), threading.Event()

    def reader():
        while not stop.is_set():
            seen.add(general_settings.value('prom_type'))

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        for index in range(200):
            service.apply({'value': {'prom_type': 'Prometheus' if index % 2 else 'Victoria-Metrics-Cluster'},
                           'updated_by': 'x', 'updated_at': None})
    finally:
        stop.set()
        thread.join()
    assert seen <= {'Prometheus', 'Victoria-Metrics-Cluster', 'Victoria-Metrics-Single'}


# ---------------------------------------------------------------------------
# 合并与保存
# ---------------------------------------------------------------------------
def test_merge_deletes_blank_and_default_values_and_keeps_others():
    current = {'prom_url': encrypt_secret(PROM_URL), 'retention_events_days': 30, 'image_repo': 'old/repo',
               'future_key': 'x'}
    new, changed = service.merge(current, {
        'retention_events_days': None, 'prom_type': 'Prometheus', 'auth_session_hours': '12', 'oss_url': '',
        'image_repo': bundle.BASE['image_repo'] + '/', 'alert_dedup_window': '600', 'node_shell_image': '  '})
    assert new == {'prom_url': current['prom_url'], 'prom_type': 'Prometheus', 'alert_dedup_window': 600,
                   'future_key': 'x'}
    # 按定义顺序;等于默认值的 12 小时本来就没存,不算修改
    assert changed == ['prom_type', 'image_repo', 'retention_events_days', 'alert_dedup_window']


def test_merge_encrypts_addresses_and_keeps_ciphertext_when_unchanged():
    write = 'http://w:wpass@vm:8428/api/v1/write'
    new, changed = service.merge({}, {'prom_url': PROM_URL + '/', 'metrics_write_url': write})
    assert changed == ['prom_url', 'metrics_write_url']
    assert new['prom_url'].startswith('v1:') and decrypt_secret(new['prom_url']) == PROM_URL
    assert decrypt_secret(new['metrics_write_url']) == write
    assert PASSWORD not in json.dumps(new) and 'S3cret' not in json.dumps(new) and 'wpass' not in json.dumps(new)
    # 密码 ******、地址没变:保留原密文,不算修改
    same, changed = service.merge(new, {'prom_url': 'http://monit:******@vm.example.com:8428'})
    assert same['prom_url'] == new['prom_url'] and changed == []
    # 重新填了同一个地址(密文每次不一样):按明文比较,也不算修改
    same, changed = service.merge(new, {'prom_url': PROM_URL})
    assert same['prom_url'] == new['prom_url'] and changed == []
    moved, changed = service.merge(new, {'prom_url': 'http://monit:******@vm.example.com:8428/select/0/prometheus'})
    assert decrypt_secret(moved['prom_url']) == PROM_URL + '/select/0/prometheus' and changed == ['prom_url']
    with pytest.raises(ValueError, match='请重新填写查询地址的密码'):
        service.merge(new, {'prom_url': 'http://monit:******@evil.example.com:8428'})
    with pytest.raises(ValueError, match='请重新填写远程写地址的密码'):
        service.merge({}, {'metrics_write_url': 'http://w:******@vm:8428/api/v1/write'})


def test_merge_with_undecryptable_saved_address(monkeypatch):
    monkeypatch.setenv('AI_ENCRYPTION_KEY', '22' * 32)
    current = {'oss_url': encrypt_secret('http://u:p@oss/b')}
    monkeypatch.setenv('AI_ENCRYPTION_KEY', KEY)
    with pytest.raises(ValueError, match='请重新填写对象存储上传地址的密码'):
        service.merge(current, {'oss_url': 'http://u:******@oss/b'})
    new, changed = service.merge(current, {'oss_url': 'http://u:new@oss/b'})  # 重新填写就能保存
    assert decrypt_secret(new['oss_url']) == 'http://u:new@oss/b' and changed == ['oss_url']


def test_without_encryption_key_addresses_fall_back_to_plain(monkeypatch):
    """没有有效的 AI_ENCRYPTION_KEY 时退回 plain:(同消息通知),不拒绝保存"""
    monkeypatch.delenv('AI_ENCRYPTION_KEY')
    new, _ = service.merge({}, {'oss_url': 'http://u:p@oss/b'})
    assert new['oss_url'] == 'plain:http://u:p@oss/b'
    assert service.decode('oss_url', new['oss_url']) == ('http://u:p@oss/b', None)


@pytest.mark.asyncio
async def test_save_writes_once_and_replaces_snapshot(storage):
    fake = storage(stored_value(prom_type='Prometheus'))
    row, changed = await service.save({'prom_url': PROM_URL, 'prom_type': None}, 'kubedoor')
    assert changed == ['prom_type', 'prom_url'] and row['updated_by'] == 'kubedoor'
    assert fake.writes[0][1] == 'kubedoor' and set(fake.writes[0][0]) == {'prom_url'}
    assert general_settings.value('prom_url') == PROM_URL  # 不等后台刷新
    assert general_settings.value('prom_type') == 'Victoria-Metrics-Single'
    with pytest.raises(ValueError):
        await service.save({'retention_events_days': -1, 'prom_type': 'Prometheus'}, 'kubedoor')
    assert len(fake.writes) == 1 and general_settings.value('prom_type') == 'Victoria-Metrics-Single'


# ---------------------------------------------------------------------------
# 真实的 Storage(假连接):事务、锁和 SQL
# ---------------------------------------------------------------------------
class _Context:
    def __init__(self, enter, exit_=None):
        self.enter, self.exit = enter, exit_

    async def __aenter__(self):
        return self.enter()

    async def __aexit__(self, kind, exc, tb):
        if self.exit:
            self.exit(kind)
        return False


class FakeConnection:
    def __init__(self, row):
        self.row, self.calls, self.transactions = row, [], []

    def transaction(self):
        return _Context(lambda: self.transactions.append('begin'),
                        lambda kind: self.transactions.append('rollback' if kind else 'commit'))

    async def execute(self, sql, *args):
        self.calls.append((sql, args))

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        if sql.startswith('SELECT'):
            return self.row
        return {'value': args[1], 'updated_by': args[2], 'updated_at': UPDATED_AT}


def _install_connection(monkeypatch, row):
    conn = FakeConnection(row)
    pool = type('Pool', (), {'acquire': lambda self: _Context(lambda: conn)})()
    monkeypatch.setattr(service.db, 'get_pool', lambda: pool)
    return conn


@pytest.mark.asyncio
async def test_storage_update_locks_reads_and_upserts_in_one_transaction(monkeypatch):
    conn = _install_connection(monkeypatch, {'value': json.dumps({'prom_type': 'Prometheus'}),
                                             'updated_by': 'kubedoor', 'updated_at': UPDATED_AT})
    row, wrote = await service.Storage().update(lambda current: {**current, 'retention_events_days': 30},
                                                'legacy-import')
    assert wrote and row == {'value': {'prom_type': 'Prometheus', 'retention_events_days': 30},
                             'updated_by': 'legacy-import', 'updated_at': UPDATED_AT}
    (lock, lock_args), (select, select_args), (upsert, upsert_args) = conn.calls
    assert 'pg_advisory_xact_lock' in lock and lock_args == ('auth_settings:general_settings',)
    assert select.endswith('FOR UPDATE') and select_args == ('general_settings',)
    assert upsert.startswith('INSERT INTO auth_settings') and 'ON CONFLICT (key) DO UPDATE' in upsert
    assert upsert_args[0] == 'general_settings' and upsert_args[2] == 'legacy-import'
    assert json.loads(upsert_args[1]) == {'prom_type': 'Prometheus', 'retention_events_days': 30}
    assert conn.transactions == ['begin', 'commit']


@pytest.mark.asyncio
async def test_storage_update_writes_nothing_when_unchanged_or_invalid(monkeypatch):
    conn = _install_connection(monkeypatch, None)
    assert await service.Storage().update(lambda current: None, 'x') == (None, False)
    assert len(conn.calls) == 2 and conn.transactions == ['begin', 'commit']  # 只加锁和读

    def broken(current):
        raise ValueError('查询地址必须是完整的 http:// 或 https:// 地址')

    conn = _install_connection(monkeypatch, None)
    with pytest.raises(ValueError):
        await service.Storage().update(broken, 'x')
    assert len(conn.calls) == 2 and conn.transactions == ['begin', 'rollback']


@pytest.mark.asyncio
async def test_storage_read_and_insert_sql(monkeypatch):
    fetchrow = AsyncMock(return_value={'value': '{"prom_type": "Prometheus"}', 'updated_by': 'kubedoor',
                                       'updated_at': UPDATED_AT})
    monkeypatch.setattr(service.db, 'pg_fetchrow', fetchrow)
    assert await service.Storage().read() == {'value': {'prom_type': 'Prometheus'}, 'updated_by': 'kubedoor',
                                              'updated_at': UPDATED_AT}
    assert fetchrow.call_args.args[1:] == ('general_settings',)
    fetchrow = AsyncMock(return_value=None)
    monkeypatch.setattr(service.db, 'pg_fetchrow', fetchrow)
    assert await service.Storage().read() is None
    assert await service.Storage().insert_absent('notification', {'default': None}, 'legacy-import') is False
    sql, key, value, author = fetchrow.call_args.args
    assert 'ON CONFLICT (key) DO NOTHING' in sql and key == 'notification' and author == 'legacy-import'
    assert json.loads(value) == {'default': None}
    fetchval = AsyncMock(return_value=1)
    monkeypatch.setattr(service.db, 'pg_fetchval', fetchval)
    assert await service.Storage().setting_exists('notification') is True
    assert fetchval.call_args.args[1:] == ('notification',)
