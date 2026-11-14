"""旧配置迁移（general_settings/legacy.py）和命令行 `python3 -m general_settings migrate`（cli.py）。"""
import io
import json
import os
import pathlib
import socket
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import general_settings
from agent_install import bundle
from auth.crypto import decrypt_secret
from general_settings import cli, legacy, service
from test_general_settings import PASSWORD, PROM_URL, fresh_state, logs, storage  # noqa: F401

MASTER = pathlib.Path(__file__).resolve().parent.parent
WECOM = 'a1b2c3d4-0000-4bc4-97a0-0ec2c3f5a1b2'
WRITE_URL = 'http://w:wpass@vm.example.com:8428/api/v1/write'
SECRETS = (PASSWORD, 'S3cret', 'wpass', WECOM, 'vm.example.com', 'secret-host')


def no_values(text):
    return not any(secret in text for secret in SECRETS)


# ---------------------------------------------------------------------------
# 旧值的解析
# ---------------------------------------------------------------------------
def test_legacy_values_from_environment():
    env = {'PROM_TYPE': 'VictoriaMetrics', 'PROM_URL': f' {PROM_URL}/ ', 'PROM_K8S_TAG_KEY': '',
           'RETENTION_EVENTS_DAYS': '-1', 'AUTH_COOKIE_SECURE': 'yes', 'AGENT_AUTH_REQUIRED': 'off',
           'ALERT_DEDUP_WINDOW': '5m', 'NODE_SHELL_IMAGE': '   ', 'LOG_LEVEL': 'DEBUG', 'PG_HOST': 'pg'}
    assert legacy.legacy_values(env) == {
        'prom_type': ('PROM_TYPE', 'Victoria-Metrics-Single'),
        'prom_url': ('PROM_URL', PROM_URL),
        'retention_events_days': ('RETENTION_EVENTS_DAYS', 0),
        'auth_cookie_secure': ('AUTH_COOKIE_SECURE', 'true'),
        'agent_auth_required': ('AGENT_AUTH_REQUIRED', False),
        'alert_dedup_window': ('ALERT_DEDUP_WINDOW', legacy.INVALID),
    }


def test_agent_install_defaults_are_read_with_environment_taking_precedence():
    defaults = {'namespace': 'kubedoor', 'oss_url': 'http://oss:9000/b/', 'image_repo': 'harbor.local/kd',
                'image_vmagent': bundle.BASE['image_vmagent'], 'image_kube_state_metrics': 'harbor.local/ksm:v2',
                'image_node_exporter': 5, 'master_url': 'https://kd.example.com/', 'prom_k8s_tag_key': 'cluster',
                'agent_tag': '1.0.0', 'remote_write_url': 'http://x/api/v1/write'}
    found = legacy.legacy_values({'AGENT_INSTALL_DEFAULTS': json.dumps(defaults), 'PROM_K8S_TAG_KEY': 'origin_k8s'})
    assert found == {
        'oss_url': ('AGENT_INSTALL_DEFAULTS.oss_url', 'http://oss:9000/b'),
        'image_repo': ('AGENT_INSTALL_DEFAULTS.image_repo', 'harbor.local/kd'),
        'image_vmagent': ('AGENT_INSTALL_DEFAULTS.image_vmagent', bundle.BASE['image_vmagent']),
        'image_kube_state_metrics': ('AGENT_INSTALL_DEFAULTS.image_kube_state_metrics', 'harbor.local/ksm:v2'),
        'image_node_exporter': ('AGENT_INSTALL_DEFAULTS.image_node_exporter', legacy.INVALID),  # 不是字符串
        'kubedoor_exturl': ('AGENT_INSTALL_DEFAULTS.master_url', 'https://kd.example.com'),  # KUBEDOOR_EXTURL 为空时
        'prom_k8s_tag_key': ('PROM_K8S_TAG_KEY', 'origin_k8s'),  # 环境变量有值时以它为准
    }
    # OSS_URL、KUBEDOOR_EXTURL、旧脚本的 IMAGE_* 变量有值时优先;agent_tag 不导入
    found = legacy.legacy_values({'AGENT_INSTALL_DEFAULTS': json.dumps(defaults), 'OSS_URL': 'http://oss2/b',
                                  'KUBEDOOR_EXTURL': 'https://kd2.example.com', 'IMAGE_REPO': 'r2/kd'})
    assert found['oss_url'] == ('OSS_URL', 'http://oss2/b')
    assert found['kubedoor_exturl'] == ('KUBEDOOR_EXTURL', 'https://kd2.example.com')
    assert found['image_repo'] == ('IMAGE_REPO', 'r2/kd')
    assert found['prom_k8s_tag_key'] == ('AGENT_INSTALL_DEFAULTS.prom_k8s_tag_key', 'cluster')
    assert '1.0.0' not in json.dumps({key: str(item) for key, (_, item) in found.items()})


@pytest.mark.parametrize('raw', ['{bad', '[1, 2]', '"text"', 'null'])
def test_unparseable_agent_install_defaults_marks_its_items_invalid(raw):
    found = legacy.legacy_values({'AGENT_INSTALL_DEFAULTS': raw, 'KUBEDOOR_EXTURL': 'https://kd.example.com',
                                  'OSS_URL': ''})
    assert {key for key, (_, item) in found.items() if item is legacy.INVALID} == {
        'oss_url', 'image_repo', 'image_vmagent', 'image_kube_state_metrics', 'image_node_exporter',
        'prom_k8s_tag_key'}
    assert found['kubedoor_exturl'] == ('KUBEDOOR_EXTURL', 'https://kd.example.com')


def test_empty_agent_install_defaults_import_nothing():
    assert legacy.legacy_values({'AGENT_INSTALL_DEFAULTS': '{}'}) == {}
    assert legacy.legacy_values({'AGENT_INSTALL_DEFAULTS': '  '}) == {}


# ---------------------------------------------------------------------------
# 6.1 环境变量 → 通用配置
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_import_only_absent_valid_non_default_values(storage, logs):
    fake = storage({'prom_type': 'Prometheus', 'retention_audit_days': 30})  # 页面已经改过的
    env = {
        'PROM_TYPE': 'Victoria-Metrics-Cluster',  # 库里已有:不动
        'PROM_URL': PROM_URL, 'METRICS_WRITE_URL': WRITE_URL,
        'PROM_K8S_TAG_KEY': 'origin_prometheus',  # 等于默认值:不写
        'RETENTION_EVENTS_DAYS': '30', 'RETENTION_ALERTS_DAYS': '365', 'RETENTION_AUDIT_DAYS': '7',
        'KUBEDOOR_EXTURL': 'https://kd-secret-host.example.com/path',  # 不合法(带路径)
        'AUTH_SESSION_HOURS': 'twelve', 'AUTH_IDLE_HOURS': '0', 'MSG_TOKEN': '',
        'AGENT_INSTALL_DEFAULTS': json.dumps({'agent_tag': '1.0.0', 'image_repo': bundle.BASE['image_repo']}),
    }
    result = await legacy.import_general(env)
    assert result == {'imported': ['prom_url', 'metrics_write_url', 'retention_events_days', 'auth_idle_hours'],
                      'present': ['prom_type', 'retention_audit_days'],
                      'invalid': ['kubedoor_exturl', 'auth_session_hours']}
    [(value, author)] = fake.writes
    assert author == 'legacy-import' and fake.row['updated_by'] == 'legacy-import'
    assert value['prom_type'] == 'Prometheus' and value['retention_audit_days'] == 30  # 页面改过的不覆盖
    assert value['prom_url'].startswith('v1:') and decrypt_secret(value['prom_url']) == PROM_URL
    assert decrypt_secret(value['metrics_write_url']) == WRITE_URL
    assert value['retention_events_days'] == 30 and value['auth_idle_hours'] == 0.0
    assert set(value) == {'prom_type', 'retention_audit_days', 'prom_url', 'metrics_write_url',
                          'retention_events_days', 'auth_idle_hours', '_legacy_imported'}
    # 记下导入过的项(页面看不到)
    assert value['_legacy_imported'] == ['prom_url', 'metrics_write_url', 'retention_events_days', 'auth_idle_hours']
    assert no_values(json.dumps(value))
    # 快照马上更新
    assert general_settings.value('prom_url') == PROM_URL and general_settings.value('auth_idle_hours') == 0.0
    # invalid 记 warning,只有变量名,不打印值
    text = '\n'.join(logs)
    assert 'KUBEDOOR_EXTURL' in text and 'AUTH_SESSION_HOURS' in text
    assert 'twelve' not in text and no_values(text)
    # 再执行一次:都已经在库里了,不再写
    again = await legacy.import_general(env)
    assert again['imported'] == [] and again['invalid'] == ['kubedoor_exturl', 'auth_session_hours']
    assert len(fake.writes) == 1


@pytest.mark.asyncio
async def test_imported_items_are_not_imported_again_after_page_deletes_them(storage, monkeypatch):
    """网页安装的 Secret kubedoor-connection 一直挂在 master 上(PROM_URL、METRICS_WRITE_URL);迁移没清理时
    kubedoor-config 也还在。导入过的项在页面上清空 / 恢复默认后,重启 master 不能再从环境变量导入回来"""
    fake = storage()
    env = {'PROM_URL': PROM_URL, 'METRICS_WRITE_URL': WRITE_URL, 'RETENTION_EVENTS_DAYS': '30'}
    assert (await legacy.import_general(env))['imported'] == ['prom_url', 'metrics_write_url',
                                                             'retention_events_days']
    await service.save({'metrics_write_url': None, 'retention_events_days': 90}, 'kubedoor')  # 清空、恢复默认
    assert set(fake.row['value']) == {'prom_url', '_legacy_imported'}
    assert await legacy.import_general(env) == {'imported': [], 'present': ['prom_url'], 'invalid': []}
    assert general_settings.value('metrics_write_url') == '' and general_settings.value('retention_events_days') == 90
    # 页面上看不到这个记录,也提交不了
    from general_settings import api as gs_api

    payload = gs_api.payload(fake.current_row())
    assert payload['stored'] == ['prom_url'] and '_legacy_imported' not in payload['values']
    # 新出现的旧配置(以前没导入过)照常导入,记录合并
    result = await legacy.import_general({**env, 'ADMIS_CACHE_TTL': '120'})
    assert result['imported'] == ['admis_cache_ttl']
    assert fake.row['value']['_legacy_imported'] == ['prom_url', 'metrics_write_url', 'retention_events_days',
                                                     'admis_cache_ttl']
    # 整行删掉(重置)以后可以重新导入
    fake.row = None
    assert (await legacy.import_general(env))['imported'] == ['prom_url', 'metrics_write_url',
                                                             'retention_events_days']


@pytest.mark.asyncio
async def test_broken_import_record_is_ignored(storage):
    fake = storage({'_legacy_imported': 'prom_url'})  # 格式不对:当成没有记录
    assert (await legacy.import_general({'PROM_URL': PROM_URL}))['imported'] == ['prom_url']
    assert fake.row['value']['_legacy_imported'] == ['prom_url']


@pytest.mark.asyncio
async def test_import_writes_nothing_when_nothing_to_import(storage):
    fake = storage()
    result = await legacy.import_general({'PROM_K8S_TAG_KEY': 'origin_prometheus', 'RETENTION_EVENTS_DAYS': '90',
                                          'AUTH_COOKIE_SECURE': 'on', 'AGENT_AUTH_REQUIRED': 'true'})
    assert result == {'imported': [], 'present': [], 'invalid': []}
    assert fake.writes == [] and fake.row is None  # 没有可导入的:库里不留空行


@pytest.mark.asyncio
@pytest.mark.parametrize('old, expected', [
    ('Victoria-Metrics-Cluster', 'Victoria-Metrics-Cluster'),
    ('victoriametrics-cluster', 'Victoria-Metrics-Cluster'),
    ('Prometheus', 'Prometheus'),
    ('thanos', 'Prometheus'),
])
async def test_prom_type_mapping(storage, old, expected):
    fake = storage()
    result = await legacy.import_general({'PROM_TYPE': old})
    assert result['imported'] == ['prom_type']
    assert fake.row['value'] == {'prom_type': expected, '_legacy_imported': ['prom_type']}


@pytest.mark.asyncio
async def test_import_raises_on_database_error(storage):
    storage().error = RuntimeError('db down')
    with pytest.raises(RuntimeError):
        await legacy.import_general({'PROM_URL': PROM_URL})


# ---------------------------------------------------------------------------
# 6.2 MSG_* → 消息通知的全局配置
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize('env, others, expected', [
    ({}, {}, 'absent'),
    ({'MSG_TYPE': 'wecom', 'MSG_TOKEN': '  '}, {}, 'absent'),
    ({'MSG_TYPE': 'wecom', 'MSG_TOKEN': WECOM}, {'notification': {'default': None}}, 'present'),
    ({'MSG_TYPE': 'qq', 'MSG_TOKEN': WECOM}, {}, 'invalid'),
    ({'MSG_TOKEN': WECOM}, {}, 'invalid'),  # 没有 MSG_TYPE
    ({'MSG_TYPE': 'dingding', 'MSG_TOKEN': 'bad token!'}, {}, 'invalid'),
    ({'MSG_TYPE': 'dingding', 'MSG_TOKEN': f'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={WECOM}'}, {},
     'invalid'),  # 企微的地址配了钉钉
    ({'MSG_TYPE': 'wecom', 'MSG_TOKEN': WECOM, 'DEFAULT_AT': 'x' * 129}, {}, 'invalid'),
])
async def test_notification_statuses(storage, logs, env, others, expected):
    fake = storage(others=others)
    assert await legacy.import_notification(env) == expected
    assert fake.inserts == [] and no_values('\n'.join(logs))


@pytest.mark.asyncio
async def test_notification_import(storage, monkeypatch):
    from notify import service as notify_service

    fake = storage()
    refresh = AsyncMock(return_value=True)
    monkeypatch.setattr(notify_service, 'refresh', refresh)
    env = {'MSG_TYPE': ' Wecom ', 'MSG_TOKEN': f'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={WECOM}',
           'DEFAULT_AT': ' zhangsan '}
    assert await legacy.import_notification(env) == 'imported'
    [(key, value, author)] = fake.inserts
    assert key == 'notification' and author == 'legacy-import'
    assert value['default']['type'] == 'wecom' and value['default']['at'] == 'zhangsan'
    assert value['default']['token'].startswith('v1:') and decrypt_secret(value['default']['token']) == WECOM
    assert set(value) == {'default'} and WECOM not in json.dumps(value)
    refresh.assert_awaited_once()  # 本进程的消息通知马上生效
    # 存的格式消息通知能直接用
    assert notify_service.build_snapshot(value).default['token'] == WECOM
    # 再执行一次:库里已有,不再写
    assert await legacy.import_notification(env) == 'present' and len(fake.inserts) == 1


@pytest.mark.asyncio
async def test_notification_import_without_refresh_and_lost_race(storage, monkeypatch):
    from notify import service as notify_service

    fake = storage()
    refresh = AsyncMock(side_effect=RuntimeError('refresh failed'))
    monkeypatch.setattr(notify_service, 'refresh', refresh)
    env = {'MSG_TYPE': 'slack', 'MSG_TOKEN': 'T0000/B0000/xyz123'}
    assert await legacy.import_notification(env, refresh=False) == 'imported'
    refresh.assert_not_awaited()
    fake.others.clear()
    assert await legacy.import_notification(env) == 'imported'  # 刷新失败不影响导入
    # 查的时候还没有、写的时候别人先写了(ON CONFLICT DO NOTHING 没写进去)
    monkeypatch.setattr(fake, 'insert_absent', AsyncMock(return_value=False))
    fake.others.clear()
    assert await legacy.import_notification(env) == 'present'


@pytest.mark.asyncio
async def test_import_legacy_reads_process_environment(storage, monkeypatch):
    fake = storage()
    for spec in general_settings.FIELDS.values():
        monkeypatch.delenv(spec.env, raising=False)
    for name in ('AGENT_INSTALL_DEFAULTS', 'MSG_TYPE', 'MSG_TOKEN', 'DEFAULT_AT'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('SILENCE_CACHE_TTL', '30')
    monkeypatch.setenv('MSG_TYPE', 'feishu')
    monkeypatch.setenv('MSG_TOKEN', '3f8c1a2b-1111-2222-3333-444455556666')
    monkeypatch.setattr('notify.service.refresh', AsyncMock(return_value=True))
    result = await general_settings.import_legacy()
    assert result == {'general_settings': {'imported': ['silence_cache_ttl'], 'present': [], 'invalid': []},
                      'notification': 'imported'}
    assert fake.row['value'] == {'silence_cache_ttl': 30, '_legacy_imported': ['silence_cache_ttl']}
    assert 'notification' in fake.others
    # 传 env 就只看它
    assert await general_settings.import_legacy({}) == {
        'general_settings': {'imported': [], 'present': ['silence_cache_ttl'], 'invalid': []},
        'notification': 'present'}


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------
@pytest.fixture
def cli_env(monkeypatch, storage):
    """命令行用的假数据库、假镜像设置导入、假事件告警规则导入"""
    fake = storage()
    init, close = AsyncMock(), AsyncMock()
    monkeypatch.setattr(cli.db, 'init_pool', init)
    monkeypatch.setattr(cli.db, 'close_pool', close)
    images = SimpleNamespace(import_legacy=AsyncMock(return_value={'rules': 'absent', 'registries': 'present'}))
    rules = SimpleNamespace(import_legacy_file=AsyncMock(return_value='imported'))
    monkeypatch.setattr(cli, '_image_settings_module', lambda: images)
    monkeypatch.setattr(cli, '_rules_api_module', lambda: rules)
    for spec in general_settings.FIELDS.values():
        monkeypatch.delenv(spec.env, raising=False)
    for name in ('AGENT_INSTALL_DEFAULTS', 'MSG_TYPE', 'MSG_TOKEN', 'DEFAULT_AT', 'UPDATE_IMAGE', 'REGISTRY_SECRET'):
        monkeypatch.delenv(name, raising=False)
    return SimpleNamespace(storage=fake, init=init, close=close, images=images, rules=rules, env=monkeypatch)


def run_cli(capsys, args, stdin=None):
    code = cli.main(args, stdin=stdin)
    captured = capsys.readouterr()
    lines = captured.out.strip().splitlines()
    return code, json.loads(lines[-1]), captured.out + captured.err


def test_cli_success(cli_env, capsys):
    cli_env.env.setenv('PROM_URL', PROM_URL)
    cli_env.env.setenv('UPDATE_IMAGE', '{"default": {"isOperationAllowed": false}}')
    code, summary, output = run_cli(capsys, ['migrate'])
    assert code == 0
    assert summary == {'ok': True, 'general_settings': {'imported': ['prom_url'], 'present': [], 'invalid': []},
                       'notification': 'absent', 'image_settings': {'rules': 'absent', 'registries': 'present'},
                       'event_rules': 'imported'}
    assert list(summary) == ['ok', 'general_settings', 'notification', 'image_settings', 'event_rules']
    assert output.strip().splitlines()[-1].startswith('{"ok": true')  # 最后一行是汇总
    cli_env.images.import_legacy.assert_awaited_once_with('{"default": {"isOperationAllowed": false}}', None)
    cli_env.rules.import_legacy_file.assert_awaited_once_with()
    cli_env.init.assert_awaited_once() and cli_env.close.assert_awaited_once()
    assert decrypt_secret(cli_env.storage.row['value']['prom_url']) == PROM_URL
    assert no_values(output)


@pytest.mark.parametrize('change', [
    lambda env: env.env.setenv('PROM_URL', 'vm-secret-host:8428'),
    lambda env: env.env.setenv('MSG_TOKEN', WECOM),  # 没有 MSG_TYPE
    lambda env: setattr(env.rules.import_legacy_file, 'return_value', 'invalid'),
    lambda env: setattr(env.images.import_legacy, 'return_value', {'rules': 'invalid', 'registries': 'absent'}),
])
def test_cli_reports_unimportable_config_with_exit_code_3(cli_env, capsys, change):
    change(cli_env)
    code, summary, output = run_cli(capsys, ['migrate'])
    assert code == 3 and summary['ok'] is False and 'error' not in summary
    assert no_values(output)


def test_cli_lists_invalid_keys(cli_env, capsys):
    cli_env.env.setenv('PROM_URL', 'vm-secret-host:8428')
    cli_env.env.setenv('AUTH_LOCK_MINUTES', '0')
    code, summary, _ = run_cli(capsys, ['migrate'])
    assert code == 3 and summary['general_settings']['invalid'] == ['prom_url', 'auth_lock_minutes']
    # 在页面上填好以后库里有了,下次就算 present
    cli_env.storage.row = {'value': {'prom_url': 'x', 'auth_lock_minutes': 30}, 'updated_by': 'kubedoor',
                           'updated_at': None}
    code, summary, _ = run_cli(capsys, ['migrate'])
    assert code == 0 and summary['general_settings']['present'] == ['prom_url', 'auth_lock_minutes']


def test_cli_database_unreachable(cli_env, capsys, logs):
    cli_env.init.side_effect = OSError(f'connect failed {PASSWORD}')
    code, summary, output = run_cli(capsys, ['migrate'])
    assert code == 1 and summary == {'ok': False, 'error': '无法连接数据库，请检查 PG_* 配置和网络（详情见日志）'}
    cli_env.close.assert_not_awaited() and cli_env.images.import_legacy.assert_not_awaited()
    assert PASSWORD not in output  # 异常详情只进日志


def test_cli_step_errors_do_not_stop_other_steps(cli_env, capsys):
    cli_env.storage.error = RuntimeError('db down')
    code, summary, output = run_cli(capsys, ['migrate'])
    assert code == 1 and summary['ok'] is False
    assert summary['general_settings'] == 'error' and summary['notification'] == 'error'
    assert summary['image_settings'] == {'rules': 'absent', 'registries': 'present'}
    assert summary['event_rules'] == 'imported'
    assert summary['error'].endswith('general_settings、notification')
    cli_env.close.assert_awaited_once()


def test_cli_rules_module_unavailable_is_an_error(cli_env, capsys, monkeypatch):
    def missing():
        raise ImportError('no rules_api yet')

    monkeypatch.setattr(cli, '_rules_api_module', missing)
    code, summary, _ = run_cli(capsys, ['migrate'])
    assert code == 1 and summary['event_rules'] == 'error'


def test_cli_env_json_replaces_process_environment(cli_env, capsys):
    cli_env.env.setenv('PROM_URL', 'http://process-env:8428')  # 被 --env-json 代替,不看
    payload = {'PROM_URL': PROM_URL, 'RETENTION_EVENTS_DAYS': 30, 'AGENT_AUTH_REQUIRED': False,
               'AGENT_INSTALL_DEFAULTS': {'image_repo': 'harbor.local/kd'}, 'UPDATE_IMAGE': {'default': {}},
               'REGISTRY_SECRET': None, 'MSG_TYPE': 'wecom', 'MSG_TOKEN': WECOM}
    stdin = io.BytesIO(json.dumps(payload, ensure_ascii=False).encode('utf-8'))
    code, summary, output = run_cli(capsys, ['migrate', '--env-json', '-'], stdin)
    assert code == 0
    assert summary['general_settings']['imported'] == ['prom_url', 'image_repo', 'retention_events_days',
                                                       'agent_auth_required']
    assert summary['notification'] == 'imported'
    value = cli_env.storage.row['value']
    assert decrypt_secret(value['prom_url']) == PROM_URL and value['image_repo'] == 'harbor.local/kd'
    assert value['agent_auth_required'] is False
    cli_env.images.import_legacy.assert_awaited_once_with('{"default": {}}', None)
    assert no_values(output)


@pytest.mark.parametrize('raw, message', [
    (b'{bad', '--env-json 读到的不是合法的 JSON'),
    (b'[1]', '--env-json 读到的 JSON 必须是对象'),
    (b'\xff\xfe', '--env-json 读到的内容不是 UTF-8'),
    (b' ' * (cli.MAX_STDIN + 1), '--env-json 的内容太大'),
], ids=['bad-json', 'not-object', 'not-utf8', 'too-large'])  # 2MB 的参数不能直接当用例名(Windows 环境变量有长度限制)
def test_cli_rejects_bad_env_json(cli_env, capsys, raw, message):
    code, summary, _ = run_cli(capsys, ['migrate', '--env-json', '-'], io.BytesIO(raw))
    assert code == 1 and summary['ok'] is False and summary['error'].startswith(message)
    cli_env.init.assert_not_awaited()


def test_cli_accepts_text_stdin(cli_env, capsys):
    code, summary, _ = run_cli(capsys, ['migrate', '--env-json', '-'], io.StringIO('{"SILENCE_CACHE_TTL": "20"}'))
    assert code == 0 and summary['general_settings']['imported'] == ['silence_cache_ttl']


@pytest.mark.parametrize('args', [[], ['migrate', '--env-json'], ['migrate', '--env-json', 'file.json'], ['other']])
def test_cli_usage(cli_env, capsys, args):
    code, summary, _ = run_cli(capsys, args)
    assert code == 1 and summary == {'ok': False, 'error': cli.USAGE}
    cli_env.init.assert_not_awaited()


def _run_module(args, **env):
    environment = {**os.environ, 'PYTHONIOENCODING': 'utf-8', **env}
    return subprocess.run([sys.executable, '-m', 'general_settings', *args], cwd=MASTER, env=environment,
                          capture_output=True, timeout=120, input=b'')


def test_module_entry_point_usage():
    result = _run_module([])
    assert result.returncode == 1
    assert json.loads(result.stdout.decode('utf-8').strip().splitlines()[-1]) == {'ok': False, 'error': cli.USAGE}


def test_module_entry_point_without_database():
    """真的跑 python -m general_settings migrate:连不上库返回 1,stdout 最后一行是汇总"""
    with socket.socket() as sock:  # 拿一个没人监听的端口
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    result = _run_module(['migrate'], PG_HOST='127.0.0.1', PG_PORT=str(port), PG_PASSWORD=PASSWORD)
    assert result.returncode == 1, result.stderr.decode('utf-8', 'replace')
    summary = json.loads(result.stdout.decode('utf-8').strip().splitlines()[-1])
    assert summary == {'ok': False, 'error': '无法连接数据库，请检查 PG_* 配置和网络（详情见日志）'}
    assert PASSWORD not in result.stdout.decode('utf-8')


def test_service_state_is_not_touched_by_import_errors(cli_env, capsys):
    """命令行出错时本进程的快照保持原样(没有半截写入)"""
    cli_env.storage.write_error = RuntimeError('disk full')
    cli_env.env.setenv('PROM_URL', PROM_URL)
    code, summary, _ = run_cli(capsys, ['migrate'])
    assert code == 1 and summary['general_settings'] == 'error'
    assert cli_env.storage.row is None and not service.is_loaded()
