"""操作权限表(auth/permissions.py)和按操作的权限判断(Principal.can)。不需要数据库。"""
import json
import pathlib
import time
from datetime import datetime, timezone
from types import MappingProxyType

import pytest

from auth import permissions
from auth.permissions import PolicyError, identify_targets_master, validate_policy
from auth.principal import Principal, make_principal

DEFAULT = permissions.default_policy()


def user(role, clusters=None):
    return Principal('u', role, user_id=1, clusters=MappingProxyType(clusters or {}))


def ops_with(**changes):
    """默认操作表上改几行:ops_with(**{'pod.delete': {'rw': False}})。"""
    ops = permissions.default_ops()
    for key, values in changes.items():
        ops[key].update(values)
    return ops


def commands_with(view=None, danger=None):
    return {'view': DEFAULT['commands']['view'] if view is None else view,
            'danger': DEFAULT['commands']['danger'] if danger is None else danger}


# ---------------------------------------------------------------- 定义与默认值
def test_defaults_keep_existing_behaviour():
    """默认值 = 改造前的行为(读写及以上能做),只有 CoreDNS / Istio 修改改成只有管理员。"""
    ops = DEFAULT['ops']
    assert set(ops) == set(permissions.OP_KEYS) and len(permissions.OP_KEYS) == 25
    for key, row in ops.items():
        assert row['read'] is False and row['admin'] is True
        assert row['rw'] is (key not in ('network.coredns', 'network.istio')), key
    assert permissions.operations_levels(DEFAULT)['network.istio'] == ['admin']
    assert permissions.operations_levels(DEFAULT)['pod.delete'] == ['rw', 'admin']


def test_approval_columns():
    by_key = permissions.OPS_BY_KEY
    na = {key for key, item in by_key.items() if item['approval_na']}
    # 审批列为「—」的:不是 AI 操作,或者是查看
    assert na == {'pod.shell', 'node.shell', 'resource.secret', 'network.tcp', 'alert.operate', 'alert.silence'}
    for key in na:
        assert by_key[key]['approval'] is False and by_key[key]['approval_fixed'] is True
    danger = by_key['pod.exec.danger']
    assert danger['approval'] is True and danger['approval_fixed'] is True and not danger['approval_na']
    no_approval = {key for key, item in by_key.items() if not item['approval'] and not item['approval_na']}
    assert no_approval == {'pod.exec.view', 'pod.exec.other', 'ai.diagnostic'}
    assert [key for key, item in by_key.items() if item['highlight']] == ['ai.k8s_write']
    for item in permissions.definitions():
        assert item['group'] and item['name'] and item['desc'] and item['applies'] in ('网页', 'AI', '网页+AI')


def test_default_commands_match_kubedoor_tools():
    commands = permissions.default_commands()
    assert 'find -delete' in commands['danger'] and 'mkfs*' in commands['danger'] and 'curl' in commands['view']
    validate_policy(permissions.default_ops(), commands)  # 默认值本身要能通过校验(find 和 find -delete 不算重名)
    tools = pathlib.Path(__file__).resolve().parents[2] / 'kubedoor-tools' / 'kubedoor_tools' / 'default_commands.json'
    if not tools.exists():
        pytest.skip('没有 kubedoor-tools 的默认清单')
    assert json.loads(tools.read_text(encoding='utf-8')) == commands


# ---------------------------------------------------------------- 校验
def test_validate_monotonic_and_types():
    with pytest.raises(PolicyError, match='勾选了只读就必须勾选读写'):
        validate_policy(ops_with(**{'pod.delete': {'read': True, 'rw': False}}), DEFAULT['commands'])
    with pytest.raises(PolicyError, match='勾选了读写就必须勾选管理员'):
        validate_policy(ops_with(**{'pod.delete': {'admin': False}}), DEFAULT['commands'])
    with pytest.raises(PolicyError, match='true 或 false'):
        validate_policy(ops_with(**{'pod.delete': {'rw': 'yes'}}), DEFAULT['commands'])
    with pytest.raises(PolicyError, match='未知的操作类型'):
        validate_policy({**permissions.default_ops(), 'pod.nope': {}}, DEFAULT['commands'])
    with pytest.raises(PolicyError, match='ops 必须是对象'):
        validate_policy(None, DEFAULT['commands'])
    # 全部取消(全站关闭)是合法的;没传的行用默认值
    closed = validate_policy({'pod.delete': {'read': False, 'rw': False, 'admin': False, 'approval': True}},
                             DEFAULT['commands'])
    assert closed['ops']['pod.delete']['admin'] is False and closed['ops']['pod.isolate'] == DEFAULT['ops']['pod.isolate']


def test_validate_forces_fixed_approvals():
    result = validate_policy(ops_with(**{'pod.exec.danger': {'approval': False}, 'pod.shell': {'approval': True},
                                         'workload.restart': {'approval': False}}), DEFAULT['commands'])
    assert result['ops']['pod.exec.danger']['approval'] is True  # 高危固定审批
    assert result['ops']['pod.shell']['approval'] is False  # 「—」行不审批
    assert result['ops']['workload.restart']['approval'] is False  # 其他可以改


@pytest.mark.parametrize('kind,item,error', [
    ('view', '', '不能有空项'),
    ('view', '   ', '不能有空项'),
    ('view', 'x' * 65, '太长'),
    ('view', 'ls -l', '单个命令名'),
    ('view', 'ls*', '单个命令名'),
    ('view', '/bin/ls', '不带路径'),
    ('danger', 'rm -rf /tmp', '只能是'),
    ('danger', '*', '只能写在命令名末尾'),
    ('danger', 'mk*fs', '只能写在命令名末尾'),
    ('danger', 'mkfs**', '只能写在命令名末尾'),
    ('danger', 'find -del*', '不能带 *'),
    ('danger', 42, '必须是字符串'),
] + [('danger', f'rm{ch}x', 'shell 元字符') for ch in ';&|<>()$`\t\n\r"\'\\'])
def test_validate_command_items(kind, item, error):
    commands = commands_with(**{kind: [*DEFAULT['commands'][kind], item]})
    with pytest.raises(PolicyError, match=error):
        validate_policy(permissions.default_ops(), commands)


def test_validate_command_lists():
    ops = permissions.default_ops()
    result = validate_policy(ops, commands_with(view=[' ls ', 'cat'], danger=['rm', 'mkfs*', 'find  -delete', 'ip netns']))
    assert result['commands'] == {'view': ['ls', 'cat'], 'danger': ['rm', 'mkfs*', 'find -delete', 'ip netns']}
    # 清单可以为空(查看清单清空 = 都算其他命令)
    assert validate_policy(ops, commands_with(view=[], danger=[]))['commands'] == {'view': [], 'danger': []}
    with pytest.raises(PolicyError, match='重复'):
        validate_policy(ops, commands_with(view=['ls', 'ls ']))
    with pytest.raises(PolicyError, match='最多 300 项'):
        validate_policy(ops, commands_with(view=[f'c{i}' for i in range(301)]))
    with pytest.raises(PolicyError, match='必须是数组'):
        validate_policy(ops, {'view': 'ls', 'danger': []})
    with pytest.raises(PolicyError, match='commands 必须是对象'):
        validate_policy(ops, None)
    # 和高危命令重名(同名或匹配前缀)不行;「命令 参数」是细化,不算重名
    with pytest.raises(PolicyError, match='「rm」同时在查看命令和高危命令里'):
        validate_policy(ops, commands_with(view=['ls', 'rm']))
    with pytest.raises(PolicyError, match='mkfs.ext4'):
        validate_policy(ops, commands_with(view=['mkfs.ext4']))
    validate_policy(ops, commands_with(view=['sort'], danger=['sort -o']))


# ---------------------------------------------------------------- 读取(合并默认值)
def test_merge_stored_values():
    stored = {
        'ops': {
            'pod.delete': {'read': False, 'rw': False, 'admin': True, 'approval': False},
            'pod.isolate': {'read': True, 'rw': False, 'admin': True},  # 直接改库改成不单调:按少给收紧
            'pod.jvm': {'rw': 'x'},  # 类型不对用默认值
            'pod.exec.danger': {'approval': False},  # 固定审批以代码为准
            'removed.op': {'read': True},  # 已删除的操作忽略
        },
        'commands': {'view': ['ls', 'ls', '', 3], 'danger': 'rm'},
    }
    merged = permissions.merge_stored(json.dumps(stored))
    assert merged['ops']['pod.delete'] == {'read': False, 'rw': False, 'admin': True, 'approval': False}
    assert merged['ops']['pod.isolate'] == {'read': False, 'rw': False, 'admin': True, 'approval': True}
    assert merged['ops']['pod.jvm'] == DEFAULT['ops']['pod.jvm']
    assert merged['ops']['pod.exec.danger']['approval'] is True
    assert 'removed.op' not in merged['ops'] and merged['ops']['network.tcp'] == DEFAULT['ops']['network.tcp']
    assert merged['commands'] == {'view': ['ls'], 'danger': DEFAULT['commands']['danger']}
    assert permissions.merge_stored('not json') == DEFAULT


class SettingsStore:
    def __init__(self, row=None):
        self.row, self.reads, self.fail = row, 0, False

    async def get_setting_row(self, key):
        assert key == 'permission_policy'
        self.reads += 1
        if self.fail:
            raise RuntimeError('db down')
        return self.row

    async def put_setting_row(self, key, value, updated_by):
        self.row = {'value': value, 'updated_by': updated_by, 'updated_at': datetime(2026, 10, 1, tzinfo=timezone.utc)}
        return self.row


@pytest.fixture
def settings(monkeypatch):
    from auth import store as store_module

    fake = SettingsStore()
    monkeypatch.setattr(store_module, 'store', fake)
    permissions.invalidate_cache()
    return fake


@pytest.mark.asyncio
async def test_load_save_and_cache(settings, monkeypatch):
    loaded = await permissions.load_policy()
    assert loaded['ops'] == DEFAULT['ops'] and permissions.version_of(loaded) == 0 and settings.reads == 1
    await permissions.load_policy()
    assert settings.reads == 1  # 10 秒内读缓存

    saved = await permissions.save_policy(ops_with(**{'pod.delete': {'rw': False}}), DEFAULT['commands'], 'alice')
    assert saved['updated_by'] == 'alice'
    assert permissions.version_of(saved) == int(datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp() * 1000)
    assert (await permissions.load_policy())['ops']['pod.delete']['rw'] is False  # 保存后立即生效,不查库
    assert settings.reads == 1 and not user('rw').can('pod.delete', 'prod')
    with pytest.raises(PolicyError):
        await permissions.save_policy(ops_with(**{'pod.delete': {'admin': False}}), DEFAULT['commands'], 'alice')

    # 过期后重新读库;读库失败时沿用旧缓存
    monkeypatch.setattr(time, 'monotonic', lambda: 10 ** 9)
    settings.fail = True
    assert (await permissions.load_policy())['ops']['pod.delete']['rw'] is False
    permissions.invalidate_cache()
    with pytest.raises(RuntimeError):
        await permissions.load_policy()
    settings.fail = False
    assert (await permissions.load_policy(force=True))['updated_by'] == 'alice'


def test_policy_diff():
    after = {'ops': ops_with(**{'pod.delete': {'rw': False}, 'workload.scale': {'approval': False}}),
             'commands': commands_with(view=[*DEFAULT['commands']['view'], 'jcmd'],
                                       danger=[c for c in DEFAULT['commands']['danger'] if c != 'dd'])}
    assert permissions.policy_diff(DEFAULT, after) == {
        'ops': {'pod.delete': {'name': '删除 Pod', 'rw': {'from': True, 'to': False}},
                'workload.scale': {'name': '扩缩容', 'approval': {'from': True, 'to': False}}},
        'commands': {'view': ['+jcmd'], 'danger': ['-dd']},
    }
    assert permissions.policy_diff(DEFAULT, DEFAULT) == {}


# ---------------------------------------------------------------- 按操作的权限判断
def test_can_by_cluster_permission(set_policy):
    reader = user('read', {'dev': 'rw', 'ops': 'admin', 'secret': 'none'})
    assert not reader.can('pod.delete', 'prod') and reader.can('pod.delete', 'dev')
    assert not reader.can('network.coredns', 'dev') and reader.can('network.coredns', 'ops')
    assert not reader.can('pod.delete', 'secret') and not reader.can('pod.delete', '')
    assert not reader.can('no.such.op', 'ops')  # 未知操作一律不能
    assert reader.allowed_ops('prod') == []
    assert 'network.istio' in reader.allowed_ops('ops') and 'network.istio' not in reader.allowed_ops('dev')
    set_policy({'pod.exec.view': {'read': True}})
    assert reader.can('pod.exec.view', 'prod') and not reader.can('pod.exec.view', 'secret')


def test_cluster_admin_role():
    admin = make_principal({'id': 2, 'username': 'c', 'role': 'cluster_admin', 'source': 'local'},
                           {'prod': 'rw', 'hidden': 'none'})
    assert admin.permission('staging') == 'admin' and admin.permission('prod') == 'rw'
    assert admin.can('network.istio', 'staging') and not admin.can('network.istio', 'prod')
    assert not admin.can_read('hidden') and admin.env_scope() == ('deny', ('hidden',))
    assert admin.global_read and admin.global_write and not admin.is_admin
    assert admin.can_global('network.coredns') and admin.can_any('network.istio')
    assert admin.to_public()['role_label'] == '集群管理员'


def test_system_admin_is_bound_by_admin_column(set_policy):
    admin = make_principal({'id': 1, 'username': 'a', 'role': 'admin', 'source': 'local'}, {'prod': 'read'})
    assert admin.permission('prod') == 'admin' and admin.can('network.coredns', 'prod')
    assert admin.allowed_ops('any') == list(permissions.OP_KEYS)
    set_policy({'pod.delete': {'read': False, 'rw': False, 'admin': False}})
    assert not admin.can('pod.delete', 'prod') and not admin.can_global('pod.delete')
    assert not user('rw').can('pod.delete', 'prod')


def test_global_and_any_cluster_checks(set_policy):
    assert user('rw').can_global('alert.silence') and not user('read').can_global('alert.silence')
    assert not user('none', {'prod': 'admin'}).can_global('alert.silence')
    assert not user('agent').can_global('alert.silence') and not user('agent').can('pod.delete', 'prod')
    assert not user('rw').can_any('network.istio') and user('none', {'prod': 'admin'}).can_any('network.istio')
    set_policy({'network.istio': {'rw': True}})
    assert user('rw').can_any('network.istio') and user('rw').can('network.istio', 'prod')


# ---------------------------------------------------------------- 目标识别
COREDNS = 'apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: coredns\n  namespace: kube-system\n'


@pytest.mark.parametrize('content,expected', [
    (COREDNS, ['network.coredns']),
    (COREDNS.replace('kube-system', 'default'), []),
    ('apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: coredns\n', []),  # 不写命名空间按 default
    ('kind: ConfigMap\nmetadata:\n  name: coredns\n  namespace: kube-system\n', ['network.coredns']),
    ('apiVersion: networking.istio.io/v1\nkind: VirtualService\nmetadata:\n  name: a\n', ['network.istio']),
    ('apiVersion: security.istio.io/v1\nkind: AuthorizationPolicy\nmetadata:\n  name: a\n', ['network.istio']),
    ('kind: DestinationRule\nmetadata:\n  name: a\n', ['network.istio']),  # 没写 apiVersion 按 kind
    ('apiVersion: gateway.networking.k8s.io/v1\nkind: Gateway\nmetadata:\n  name: a\n', []),  # K8S Gateway API
    ('apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: coredns\n  namespace: kube-system\n', []),
    (f'apiVersion: v1\nkind: Service\nmetadata:\n  name: a\n---\n{COREDNS}---\n', ['network.coredns']),
    ('{"apiVersion": "v1", "kind": "List", "items": [{"apiVersion": "networking.istio.io/v1beta1", '
     '"kind": "Gateway", "metadata": {"name": "g"}}, ' + json.dumps({
         'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {'name': 'coredns', 'namespace': 'kube-system'}}) + ']}',
     ['network.coredns', 'network.istio']),
    ('', []),
    ('a: [1', ['network.coredns', 'network.istio']),  # 解析失败:按都涉及处理
    ('- just\n- a list\n', []),
])
def test_identify_targets_from_yaml(content, expected):
    assert identify_targets_master(yaml_content=content) == expected


def test_identify_targets_conservative_cases():
    assert permissions.yaml_targets('a: [1') == (['network.coredns', 'network.istio'], False)
    assert permissions.yaml_targets(COREDNS) == (['network.coredns'], True)
    assert identify_targets_master(yaml_content={'kind': 'ConfigMap'}) == ['network.coredns', 'network.istio']
    deep = {'kind': 'List', 'items': []}
    node = deep
    for _ in range(6):  # 嵌套太深
        child = {'kind': 'List', 'items': []}
        node['items'].append(child)
        node = child
    assert identify_targets_master(yaml_content=json.dumps(deep)) == ['network.coredns', 'network.istio']
    many = '\n---\n'.join('kind: Secret\nmetadata:\n  name: s' for _ in range(5001))
    assert identify_targets_master(yaml_content=many) == ['network.coredns', 'network.istio']


@pytest.mark.parametrize('resource_type,name,namespace,expected', [
    ('configmap', 'coredns', 'kube-system', ['network.coredns']),
    ('ConfigMaps', 'coredns', 'kube-system', ['network.coredns']),
    ('cm', 'coredns', 'kube-system', ['network.coredns']),
    ('configmap', 'coredns', 'default', []),
    ('configmap', 'other', 'kube-system', []),
    ('secret', 'coredns', 'kube-system', []),
    ('virtualservice', 'a', 'default', ['network.istio']),
    ('VirtualServices', 'a', 'default', ['network.istio']),
    ('vs', 'a', 'default', ['network.istio']),
    ('gw', 'a', 'default', ['network.istio']),
    ('peerauthentications', 'a', 'default', ['network.istio']),
    ('virtualservices.networking.istio.io', 'a', 'default', ['network.istio']),
    ('gateways.gateway.networking.k8s.io', 'a', 'default', []),
    ('deployment', 'coredns', 'kube-system', []),
    ('', 'coredns', 'kube-system', []),
])
def test_identify_targets_from_resource(resource_type, name, namespace, expected):
    assert identify_targets_master(resource_type=resource_type, name=name, namespace=namespace) == expected
