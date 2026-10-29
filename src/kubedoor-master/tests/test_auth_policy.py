"""auth 纯逻辑的单元测试:口令哈希、权限计算、按集群过滤、接口权限表。不需要数据库。"""
import pathlib
import sys
from types import MappingProxyType

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from auth import passwords, policy  # noqa: E402
from auth.principal import Principal, env_condition, make_principal  # noqa: E402


@pytest.fixture(autouse=True)
def fast_scrypt(monkeypatch):
    monkeypatch.setattr(passwords, 'SCRYPT_N', 2 ** 8)
    monkeypatch.setattr(passwords, 'SCRYPT_P', 1)


def user(role, clusters=None):
    return Principal('u', role, user_id=1, clusters=MappingProxyType(clusters or {}))


# ---------------------------------------------------------------- 口令
def test_hash_and_verify_roundtrip_with_unique_salt():
    first, second = passwords.hash_password('S3cure-pass'), passwords.hash_password('S3cure-pass')
    assert first != second and first.startswith('scrypt$')
    assert passwords.verify_password('S3cure-pass', first)
    assert not passwords.verify_password('S3cure-pasS', first)
    assert not passwords.verify_password('', first)
    assert not passwords.verify_password('x', None) and not passwords.verify_password('x', 'garbage')


def test_needs_rehash_when_parameters_change(monkeypatch):
    encoded = passwords.hash_password('S3cure-pass')
    assert not passwords.needs_rehash(encoded)
    monkeypatch.setattr(passwords, 'SCRYPT_N', 2 ** 9)
    assert passwords.needs_rehash(encoded)
    assert passwords.verify_password('S3cure-pass', encoded)  # 老参数的哈希照样能校验


@pytest.mark.parametrize('password,error', [
    ('short', '至少'), ('kubedoor', '太简单'), ('aaaaaaaaaa', '太简单'), (' padded-pass ', '空格'),
    ('alice-smith', None), ('x' * 129, '最多'),
])
def test_password_policy(password, error):
    result = passwords.validate_new_password(password, 'bob')
    assert (result is None) if error is None else (error in result)
    assert passwords.validate_new_password('Alice2026x', 'alice2026x') == '口令不能和用户名相同'


# ---------------------------------------------------------------- 权限计算
def test_cluster_grant_overrides_global_role():
    p = user('read', {'prod': 'rw', 'secret': 'none'})
    assert p.permission('prod') == 'rw' and p.can_write('prod')
    assert p.permission('other') == 'read' and p.can_read('other') and not p.can_write('other')
    assert not p.can_read('secret')
    assert p.filter_envs(['other', 'prod', 'secret']) == ['other', 'prod']

    rw = user('rw', {'prod': 'read'})
    assert rw.can_write('staging') and not rw.can_write('prod') and rw.can_read('prod')


def test_global_none_only_sees_granted_clusters():
    p = user('none', {'team-a': 'read'})
    assert p.can_read('team-a') and not p.can_read('new-cluster')
    assert p.env_scope() == ('allow', ('team-a',))
    assert not p.global_read and not p.global_write


def test_admin_and_agent_ignore_cluster_grants():
    admin = make_principal({'id': 1, 'username': 'a', 'role': 'admin', 'source': 'local'}, {'prod': 'none'})
    assert admin.permission('prod') == 'admin' and admin.can_write('prod') and admin.env_scope() == ('all', ())
    agent = make_principal({'id': 2, 'username': 'g', 'role': 'agent', 'source': 'local'}, {'prod': 'rw'})
    assert not agent.can_read('prod') and not agent.is_human


def test_env_condition_sql():
    params = []
    assert env_condition(user('read'), 'env', params) is None and params == []
    assert env_condition(user('read', {'x': 'none'}), 'env', params) == 'NOT (env = ANY($1::text[]))'
    assert params == [['x']]
    params = ['first']
    assert env_condition(user('none', {'a': 'rw', 'b': 'none'}), 'k8s', params) == 'k8s = ANY($2::text[])'
    assert params == ['first', ['a']]


# ---------------------------------------------------------------- 接口权限表
@pytest.mark.parametrize('method,path,kind', [
    ('POST', '/api/auth/login', 'public'),
    ('GET', '/api/auth/me', 'session'),
    ('DELETE', '/api/admin/users/3', 'admin'),
    ('GET', '/ws', 'agent'),
    ('GET', '/ws/pod-logs', 'env_read'),
    ('GET', '/ws/pod-exec', 'op'),  # 终端能执行任意命令
    ('GET', '/ws/node-exec', 'op'),  # 节点终端在宿主机以 root 执行
    ('GET', '/api/agent/pods', 'env_read'),
    ('HEAD', '/api/agent/pods', 'env_read'),
    ('GET', '/api/pod/task_status/abc', 'env_read'),
    ('GET', '/api/pod/delete_pod', 'op'),  # GET 但会删 Pod
    ('GET', '/api/pod/auto_dump', 'op'),
    ('GET', '/api/admis_switch', 'admin'),
    ('GET', '/api/agent/istio/vs', 'admin'),
    ('POST', '/api/scale', 'op'),
    ('PUT', '/api/scale', 'op'),  # 转发给 agent 的修改不限方法
    ('POST', '/api/db/agent/collect', 'admin'),
    ('POST', '/api/db/agent/aliases', 'admin'),
    ('GET', '/api/agent/res/content', 'custom'),
    ('GET', '/api/agent/coredns', 'env_read'),
    ('GET', '/api/agent/net/resolve', 'env_read'),  # 从 agent 所在 Pod 解析域名
    ('GET', '/api/agent/net/tcp', 'op'),  # 端口测试会从集群内向任意地址发起连接
    ('POST', '/api/agent/coredns/save', 'op'),
    ('GET', '/api/agent/coredns/save', 'op'),  # 只登记了 GET 的只读路径,别的子路径不放宽
    ('POST', '/api/db/silence/add', 'custom'),
    ('PUT', '/api/load-balance/config', 'custom'),
    ('POST', '/api/agent/res/ops', 'custom'),
    ('DELETE', '/api/agent/res/delete', 'custom'),
    ('POST', '/api/istio/groups', 'op_any'),
    ('GET', '/api/istio/groups', 'login'),
    ('GET', '/api/admin/permission-policy', 'admin'),
    ('PUT', '/api/admin/permission-policy', 'admin'),
    ('GET', '/api/ai/internal/permission-policy', 'internal'),
    ('POST', '/api/admis', 'env_write'),  # 准入 webhook 由 K8S 直接调 agent,不经过 master
    ('GET', '/api/something/new', 'env_write'),  # 没登记的接口默认按集群写
])
def test_rule_matching(method, path, kind):
    assert policy.match(method, path).kind == kind


def test_every_master_route_has_explicit_rule():
    """master 自己注册的路由都要在 policy.RULES 里显式登记,不能落到最后的兜底规则。"""
    import importlib.util

    master = pathlib.Path(__file__).resolve().parent.parent / 'kubedoor-master.py'
    spec = importlib.util.spec_from_file_location('kdmaster_routes', master)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except ImportError as exc:  # 本地没装云镜像 SDK 时跳过
        pytest.skip(f'无法加载 kubedoor-master.py: {exc}')
    fallback = policy.RULES[-1]
    missing = []
    for route in module.app.router.routes():
        path = route.resource.canonical
        if route.method == 'HEAD' or path == '/api/{tail}':
            continue
        sample = path.replace('{user_id}', '1')
        if policy.match(route.method, sample) is fallback:
            missing.append(f'{route.method} {path}')
    assert missing == []


class Req:
    """policy 校验用的最小请求对象。"""

    def __init__(self, method='GET', path='/', query=None, body=None):
        self.method, self.path, self.query = method, path, query or {}
        self._body = body or {}
        self.state = {}

    async def json(self):
        return self._body

    def __setitem__(self, key, value):
        self.state[key] = value


async def authorize(principal, method, path, query=None, body=None):
    request = Req(method, path, query, body)
    await policy.authorize(policy.match(method, path), request, principal)
    return request


@pytest.mark.asyncio
async def test_env_rules_check_permission_of_target_cluster():
    p = user('read', {'prod': 'none', 'dev': 'rw'})
    await authorize(p, 'GET', '/api/agent/pods', {'env': 'staging'})
    with pytest.raises(policy.Deny) as denied:
        await authorize(p, 'GET', '/api/agent/pods', {'env': 'prod'})
    assert denied.value.status == 403 and 'prod' in denied.value.message
    with pytest.raises(policy.Deny) as denied:
        await authorize(p, 'POST', '/api/scale', {'env': 'staging'})
    assert denied.value.code == 'operation_forbidden'
    assert denied.value.message == '当前账号在集群 staging 是「只读」权限,不能执行「扩缩容」'
    with pytest.raises(policy.Deny) as denied:  # 看不到的集群按无权访问
        await authorize(p, 'POST', '/api/scale', {'env': 'prod'})
    assert denied.value.code == 'cluster_forbidden'
    await authorize(p, 'POST', '/api/scale', {'env': 'dev'})
    with pytest.raises(policy.Deny) as denied:  # 兜底规则仍按集群读写
        await authorize(p, 'POST', '/api/something/new', {'env': 'staging'})
    assert denied.value.code == 'cluster_read_only'
    with pytest.raises(policy.Deny) as denied:
        await authorize(p, 'GET', '/api/agent/pods')
    assert denied.value.status == 400


@pytest.mark.asyncio
async def test_body_env_list_and_optional_env():
    p = user('none', {'a': 'read'})
    await authorize(p, 'POST', '/api/db/alert/detail', body={'env': ['a']})
    await authorize(p, 'POST', '/api/db/alert/detail', body={})  # 不带集群由 handler 过滤
    with pytest.raises(policy.Deny):
        await authorize(p, 'POST', '/api/db/alert/detail', body={'env': ['a', 'b']})


@pytest.mark.asyncio
async def test_update_image_lets_read_users_fall_back_to_whitelist():
    reader = user('read', {'hidden': 'none'})
    request = await authorize(reader, 'POST', '/api/update-image', {'env': 'prod'})
    assert request.state['update_image_direct'] is False
    request = await authorize(user('rw'), 'POST', '/api/update-image', {'env': 'prod'})
    assert request.state['update_image_direct'] is True
    with pytest.raises(policy.Deny):
        await authorize(reader, 'POST', '/api/update-image', {'env': 'hidden'})


@pytest.mark.asyncio
async def test_secret_content_requires_write():
    reader = user('read')
    await authorize(reader, 'GET', '/api/agent/res/content', {'env': 'prod', 'resource_type': 'configmap'})
    with pytest.raises(policy.Deny) as denied:
        await authorize(reader, 'GET', '/api/agent/res/content', {'env': 'prod', 'resource_type': 'secret'})
    assert denied.value.code == 'secret_forbidden'
    await authorize(user('read', {'prod': 'rw'}), 'GET', '/api/agent/res/content',
                    {'env': 'prod', 'resource_type': 'Secret'})


@pytest.mark.asyncio
async def test_resource_sync_needs_write_on_both_clusters():
    p = user('read', {'prod': 'rw'})
    await authorize(p, 'POST', '/api/db/res/edit', body={'env': 'prod'})
    with pytest.raises(policy.Deny):
        await authorize(p, 'POST', '/api/db/res/edit', body={'env': 'prod', 'sync_env': 'staging'})


def test_silence_scope():
    assert policy.silence_envs([{'key': 'env', 'op': '=', 'value': 'prod'}]) == ['prod']
    assert policy.silence_envs([{'key': 'env', 'op': '=~', 'value': 'prod|dev'}]) is None
    assert policy.silence_envs([{'key': 'alertname', 'op': '=', 'value': 'x'}]) is None
    cluster_rw = user('read', {'prod': 'rw'})
    policy.check_silence_write(cluster_rw, [{'key': 'k8s', 'op': '=', 'value': 'prod'}])
    with pytest.raises(policy.Deny) as denied:
        policy.check_silence_write(cluster_rw, [{'key': 'alertname', 'op': '=', 'value': 'x'}])
    assert denied.value.code == 'operation_forbidden'
    policy.check_silence_write(user('rw'), [{'key': 'alertname', 'op': '=', 'value': 'x'}])
    hidden = user('read', {'prod': 'none'})
    assert not policy.silence_visible(hidden, [{'key': 'env', 'op': '=', 'value': 'prod'}])
    assert policy.silence_visible(hidden, [{'key': 'alertname', 'op': '=', 'value': 'x'}])


@pytest.mark.asyncio
async def test_silence_edit_checks_existing_and_new_matchers(monkeypatch):
    import db

    async def fetchval(sql, *args):
        return '[{"key": "env", "op": "=", "value": "prod"}]'

    monkeypatch.setattr(db, 'pg_fetchval', fetchval)
    p = user('read', {'staging': 'rw'})
    with pytest.raises(policy.Deny):  # 原规则属于 prod,没有 prod 写权限
        await authorize(p, 'POST', '/api/db/silence/edit',
                        body={'id': 1, 'matchers': [{'key': 'env', 'op': '=', 'value': 'staging'}]})
    p = user('read', {'staging': 'rw', 'prod': 'rw'})
    await authorize(p, 'POST', '/api/db/silence/edit',
                    body={'id': 1, 'matchers': [{'key': 'env', 'op': '=', 'value': 'staging'}]})


def test_top_queries_keep_cluster_filter_and_add_visibility():
    from func_manager import top_queries

    hidden = user('read', {'secret': 'none'})
    for build, column in ((top_queries._build_pod_alerts_sql, 'env'),
                          (top_queries._build_exception_events_sql, 'k8s'),
                          (top_queries._build_alert_daily_sql, 'env')):
        sql, params, _ = build('prod', hidden)
        assert params == ['prod', ['secret']]
        assert f'{column} = $1' in sql and f'NOT ({column} = ANY($2::text[]))' in sql
        sql, params, _ = build(None, user('none', {'prod': 'read'}))
        assert params == [['prod']] and f'{column} = ANY($1::text[])' in sql
        sql, params, _ = build(None, user('admin'))
        assert params == [] and 'ANY' not in sql


@pytest.mark.asyncio
@pytest.mark.parametrize('method,path', [
    ('POST', '/api/istio/groups'), ('PUT', '/api/istio/groups'), ('DELETE', '/api/istio/groups'),
    ('POST', '/api/istio/collect'), ('POST', '/api/istio/import'), ('POST', '/api/istio/vs/reset'),
    ('POST', '/api/istio/vs/sync/preview'), ('POST', '/api/istio/vs/sync'),
    ('POST', '/api/istio/ignore'), ('POST', '/api/istio/unignore'), ('POST', '/api/istio/live/delete'),
    ('POST', '/api/istio/live/rollback'),
    ('POST', '/api/istio/vs'), ('PUT', '/api/istio/vs'), ('DELETE', '/api/istio/vs'),
    ('PUT', '/api/istio/vs/cluster'), ('DELETE', '/api/istio/vs/cluster'),
    ('POST', '/api/istio/httproute'), ('PUT', '/api/istio/httproute'), ('DELETE', '/api/istio/httproute'),
    ('POST', '/api/istio/httproute/reorder'), ('POST', '/api/istio/apply/preview'), ('POST', '/api/istio/apply'),
])
async def test_istio_changes_need_istio_permission_on_some_cluster(method, path):
    """粗检:至少一个集群有「Istio 修改」(默认只有管理员列);具体集群由 handler 再判断(test_istio_api)。"""
    rule = policy.match(method, path)
    assert rule.kind == 'op_any' and rule.op == 'network.istio'
    for principal in (user('rw'), user('read', {'a': 'rw'})):  # 所有集群读写也不行
        with pytest.raises(policy.Deny) as denied:
            await authorize(principal, method, path, {'env': 'a', 'vs_id': '1'})
        assert denied.value.code == 'operation_forbidden'
    for principal in (user('admin'), user('cluster_admin'), user('none', {'a': 'admin'}),
                      user('cluster_admin', {'a': 'read'})):  # 后者其他集群仍按全局角色是管理员
        await authorize(principal, method, path, {'env': 'a', 'vs_id': '1'})


@pytest.mark.asyncio
@pytest.mark.parametrize('method,path', [
    # 集群里的 VS 不能经通用转发直接读写(下发内容必须由 master 按组标准生成)
    ('GET', '/api/agent/istio/vs'), ('GET', '/api/agent/istio/vs/detail'), ('POST', '/api/agent/istio/vs/apply'),
    ('DELETE', '/api/agent/istio/vs/delete'),
])
async def test_agent_istio_relay_is_admin_only(method, path):
    assert policy.match(method, path).kind == 'admin'
    with pytest.raises(policy.Deny) as denied:
        await authorize(user('cluster_admin'), method, path, {'env': 'a'})
    assert denied.value.code == 'admin_required'
    await authorize(user('admin'), method, path, {'env': 'a'})


@pytest.mark.parametrize('path', ['/api/istio/groups', '/api/istio/groups/preview', '/api/istio/matrix',
                                  '/api/istio/compare', '/api/istio/live', '/api/istio/live/backups',
                                  '/api/istio/vs', '/api/istio/vs/check'])
def test_istio_reads_are_filtered_by_handlers(path):
    """查看只要登录,能看到哪些集群由 handler 按集群读权限过滤。"""
    assert policy.match('GET', path).kind == 'login'


def test_pepper_protects_hashes_and_upgrades_old_ones(monkeypatch):
    monkeypatch.delenv('AI_ENCRYPTION_KEY', raising=False)
    plain = passwords.hash_password('S3cure-pass')
    assert plain.startswith('scrypt$')
    monkeypatch.setenv('AI_ENCRYPTION_KEY', 'ab' * 32)
    # 老的未加 pepper 的哈希仍能校验,但登录后会被重新哈希
    assert passwords.verify_password('S3cure-pass', plain) and passwords.needs_rehash(plain)
    peppered = passwords.hash_password('S3cure-pass')
    assert peppered.startswith('scrypt-p1$') and not passwords.needs_rehash(peppered)
    assert passwords.verify_password('S3cure-pass', peppered)
    # 没有密钥(或换了密钥)就算拿到哈希也校验不了,无法离线破解
    monkeypatch.setenv('AI_ENCRYPTION_KEY', 'cd' * 32)
    assert not passwords.verify_password('S3cure-pass', peppered)
    monkeypatch.delenv('AI_ENCRYPTION_KEY')
    assert not passwords.verify_password('S3cure-pass', peppered)


# ---------------------------------------------------------------- 操作权限表
@pytest.mark.parametrize('method,path,op', [
    ('GET', '/ws/pod-exec', 'pod.shell'), ('GET', '/ws/node-exec', 'node.shell'),
    ('POST', '/api/restart', 'workload.restart'), ('POST', '/api/agent/statefulset/restart', 'workload.restart'),
    ('POST', '/api/agent/daemonset/restart', 'workload.restart'),
    ('POST', '/api/scale', 'workload.scale'), ('POST', '/api/agent/statefulset/scale', 'workload.scale'),
    ('POST', '/api/db/res/pod_count', 'workload.scale'), ('POST', '/api/cron', 'workload.schedule'),
    ('GET', '/api/pod/delete_pod', 'pod.delete'), ('DELETE', '/api/pod/delete_pods', 'pod.delete'),
    ('POST', '/api/pod/modify_pod', 'pod.isolate'),
    ('GET', '/api/pod/auto_dump', 'pod.jvm'), ('GET', '/api/pod/auto_jstack', 'pod.jvm'),
    ('GET', '/api/pod/auto_jfr', 'pod.jvm'), ('GET', '/api/pod/auto_jvm_mem', 'pod.jvm'),
    ('POST', '/api/nodes/cordon', 'node.cordon'), ('POST', '/api/nodes/uncordon', 'node.cordon'),
    ('POST', '/api/load-balance/analyze', 'balance.run'), ('POST', '/api/load-balance/execute', 'balance.run'),
    ('POST', '/api/load-balance/cleanup', 'balance.run'),
    ('GET', '/api/init_peak_data', 'control.edit'),
    ('POST', '/api/agent/coredns/save', 'network.coredns'), ('GET', '/api/agent/net/tcp', 'network.tcp'),
])
def test_operation_rules(method, path, op):
    rule = policy.match(method, path)
    assert (rule.kind, rule.op) == ('op', op) and rule.write


@pytest.mark.asyncio
async def test_operation_columns_and_system_admin(set_policy):
    reader, rw, cluster_admin, admin = user('read'), user('rw'), user('cluster_admin'), user('admin')
    with pytest.raises(policy.Deny):
        await authorize(reader, 'GET', '/ws/pod-exec', {'env': 'prod'})
    await authorize(rw, 'GET', '/ws/pod-exec', {'env': 'prod'})
    # CoreDNS 默认只有管理员
    with pytest.raises(policy.Deny) as denied:
        await authorize(rw, 'POST', '/api/agent/coredns/save', {'env': 'prod'})
    assert denied.value.message == '当前账号在集群 prod 是「读写」权限,不能执行「CoreDNS 修改」'
    await authorize(cluster_admin, 'POST', '/api/agent/coredns/save', {'env': 'prod'})
    await authorize(user('rw', {'prod': 'admin'}), 'POST', '/api/agent/coredns/save', {'env': 'prod'})
    with pytest.raises(policy.Deny):  # 集群管理员可以单独降低某个集群的权限
        await authorize(user('cluster_admin', {'prod': 'rw'}), 'POST', '/api/agent/coredns/save', {'env': 'prod'})
    # 勾上只读列后只读用户也能开终端;取消管理员列后包括系统管理员在内谁都不能重启
    set_policy({'pod.shell': {'read': True}, 'workload.restart': {'read': False, 'rw': False, 'admin': False}})
    await authorize(reader, 'GET', '/ws/pod-exec', {'env': 'prod'})
    for principal in (rw, cluster_admin, admin):
        with pytest.raises(policy.Deny) as denied:
            await authorize(principal, 'POST', '/api/restart', {'env': 'prod'})
        assert denied.value.code == 'operation_forbidden'
    with pytest.raises(policy.Deny) as denied:  # 不带集群参数的操作一律 400(系统管理员也是)
        await authorize(admin, 'POST', '/api/scale')
    assert denied.value.status == 400


@pytest.mark.asyncio
async def test_update_image_whitelist_only_for_read_users(set_policy):
    set_policy({'workload.image': {'rw': False}})
    with pytest.raises(policy.Deny):  # 读写用户没有该操作就是没有,不走白名单
        await authorize(user('rw'), 'POST', '/api/update-image', {'env': 'prod'})
    request = await authorize(user('read'), 'POST', '/api/update-image', {'env': 'prod'})
    assert request.state['update_image_direct'] is False
    request = await authorize(user('cluster_admin'), 'POST', '/api/update-image', {'env': 'prod'})
    assert request.state['update_image_direct'] is True
    set_policy({'workload.image': {'rw': False, 'admin': False}})  # 全站关闭:白名单也不放行
    with pytest.raises(policy.Deny):
        await authorize(user('read'), 'POST', '/api/update-image', {'env': 'prod'})


@pytest.mark.asyncio
async def test_resource_yaml_needs_target_operations():
    coredns = 'apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: coredns\n  namespace: kube-system\n'
    vs = 'apiVersion: networking.istio.io/v1\nkind: VirtualService\nmetadata:\n  name: a\n'
    plain = 'apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: app\n  namespace: kube-system\n'
    rw, cluster_admin = user('rw'), user('cluster_admin')
    await authorize(rw, 'POST', '/api/agent/res/ops', {'env': 'prod', 'method': 'apply'}, {'yaml_content': plain})
    for content, name in ((coredns, 'CoreDNS 修改'), (f'{plain}---\n{vs}', 'Istio 修改')):
        with pytest.raises(policy.Deny) as denied:
            await authorize(rw, 'POST', '/api/agent/res/ops', {'env': 'prod'}, {'yaml_content': content})
        assert denied.value.message.endswith(f'不能执行「{name}」')
        await authorize(cluster_admin, 'POST', '/api/agent/res/ops', {'env': 'prod'}, {'yaml_content': content})
    with pytest.raises(policy.Deny) as denied:  # 解析不了的 YAML 按都涉及处理
        await authorize(rw, 'POST', '/api/agent/res/ops', {'env': 'prod'}, {'yaml_content': 'a: [1'})
    assert denied.value.message.startswith('YAML 解析失败')
    with pytest.raises(policy.Deny) as denied:
        await authorize(user('read'), 'POST', '/api/agent/res/ops', {'env': 'prod'}, {'yaml_content': plain})
    assert denied.value.message.endswith('不能执行「新建 / 编辑资源 YAML」')


@pytest.mark.asyncio
async def test_resource_delete_needs_target_operations():
    query = {'env': 'prod', 'namespace': 'kube-system', 'resource_type': 'configmap', 'resource_name': 'coredns'}
    with pytest.raises(policy.Deny) as denied:
        await authorize(user('rw'), 'DELETE', '/api/agent/res/delete', query)
    assert denied.value.message.endswith('不能执行「CoreDNS 修改」')
    await authorize(user('cluster_admin'), 'DELETE', '/api/agent/res/delete', query)
    await authorize(user('rw'), 'DELETE', '/api/agent/res/delete', {**query, 'resource_name': 'other'})
    with pytest.raises(policy.Deny) as denied:
        await authorize(user('read'), 'DELETE', '/api/agent/res/delete', {**query, 'resource_name': 'other'})
    assert denied.value.message.endswith('不能执行「删除资源」')


@pytest.mark.asyncio
async def test_global_configs_use_global_role_column(set_policy):
    await authorize(user('rw'), 'PUT', '/api/load-balance/config')
    await authorize(user('cluster_admin'), 'PUT', '/api/load-balance/config')
    for principal in (user('read', {'prod': 'rw'}), user('none', {'prod': 'admin'})):
        with pytest.raises(policy.Deny) as denied:
            await authorize(principal, 'PUT', '/api/load-balance/config')
        assert denied.value.code == 'operation_forbidden'
    set_policy({'balance.run': {'rw': False}})
    with pytest.raises(policy.Deny):
        await authorize(user('rw'), 'PUT', '/api/load-balance/config')
    set_policy({'alert.silence': {'read': True}})
    policy.check_silence_write(user('read'), [{'key': 'alertname', 'op': '=', 'value': 'x'}])


@pytest.mark.asyncio
async def test_policy_unavailable_fails_closed(monkeypatch):
    from auth import permissions, store as store_module

    async def broken(key):
        raise RuntimeError('db down')

    permissions.invalidate_cache()
    monkeypatch.setattr(store_module.store, 'get_setting_row', broken)
    with pytest.raises(policy.Deny) as denied:
        await authorize(user('admin'), 'POST', '/api/scale', {'env': 'prod'})
    assert denied.value.status == 503
