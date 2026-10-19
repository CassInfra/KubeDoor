"""Istio 管理接口的端到端测试:真实 PostgreSQL + 假 agent。

需要一个可以随意清空的测试库,设置 KUBEDOOR_TEST_PG_DSN 才运行,例如
  KUBEDOOR_TEST_PG_DSN=postgresql://postgres@127.0.0.1:55432/kubedoor python -m pytest tests/test_istio_api.py
会执行整份 db.sql 并清空 istio_* 表。
"""
import asyncio
import copy
import json
import os
import pathlib
import sys
from types import MappingProxyType

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import db  # noqa: E402
from auth import audit  # noqa: E402
from auth.principal import Principal  # noqa: E402
from istio_route import handlers  # noqa: E402
from istio_route import spec as S  # noqa: E402

PG_DSN = os.environ.get('KUBEDOOR_TEST_PG_DSN')
pytestmark = pytest.mark.skipif(not PG_DSN, reason='设置 KUBEDOOR_TEST_PG_DSN(可清空的测试库)才运行')
DB_SQL = pathlib.Path(__file__).resolve().parent.parent / 'db.sql'
NS = 'istio-system'


def route(prefix, port, name=None, rewrite=None):
    entry = {'match': [{'uri': {'prefix': prefix}}]}
    if name:
        entry = {'name': name, **entry}
    if rewrite:
        entry['rewrite'] = {'uri': rewrite}
    entry['route'] = [{'destination': {'host': f's{port}.b2c.svc.cluster.local', 'port': {'number': port}}}]
    entry['timeout'] = '7s'
    return entry


def vs(hosts, routes, gateway='istio-system/internal', **extra):
    return {'hosts': hosts, 'gateways': [gateway], 'http': routes, **extra}


class FakeAgent:
    """按集群保存 VirtualService;列出 / 下发两个接口和真 agent 的返回格式一致。"""

    def __init__(self):
        self.clusters = {}
        self.offline = set()
        self.applied = []

    def put(self, k8s_name, name, spec):
        self.clusters.setdefault(k8s_name, {})[(NS, name)] = copy.deepcopy(spec)

    async def call(self, env, path, query=None, method='GET', body=None, timeout=30):
        if env in self.offline:
            raise Exception(f'目标客户端 {env} 不在线')
        items = self.clusters.setdefault(env, {})
        if path == handlers.AGENT_LIST_PATH and method == 'GET':
            data = [{'name': name, 'namespace': ns, 'resourceVersion': '1', 'spec': copy.deepcopy(spec)}
                    for (ns, name), spec in items.items()]
            return {'success': True, 'data': data, 'total': len(data)}
        if path == handlers.AGENT_APPLY_PATH and method == 'POST':
            self.applied.append((env, copy.deepcopy(body)))
            if body['spec'].get('hosts') == ['reject.example.com']:
                return {'success': False, 'status': 422, 'error': 'Kubernetes API错误: Unprocessable Entity'}
            meta = body['metadata']
            items[(meta['namespace'], meta['name'])] = copy.deepcopy(body['spec'])
            return {'success': True, 'message': f"VirtualService {meta['name']} applied successfully"}
        raise AssertionError(f'unexpected agent call {method} {path}')


def principal_from(request):
    role = request.headers.get('X-Test-Role', 'admin')
    grants = json.loads(request.headers.get('X-Test-Clusters', '{}'))
    return Principal('tester' if role != 'admin' else 'admin', role, user_id=1, clusters=MappingProxyType(grants))


@web.middleware
async def fake_auth(request, handler):
    request['principal'] = principal_from(request)
    return await handler(request)


@pytest_asyncio.fixture
async def env(monkeypatch):
    import asyncpg

    pool = await asyncpg.create_pool(PG_DSN, min_size=1, max_size=4)
    monkeypatch.setattr(db, '_pool', pool)
    monkeypatch.setattr(db, '_loop', asyncio.get_running_loop())
    await db.run_ddl_file(str(DB_SQL))
    await db.pg_execute('TRUNCATE istio_group RESTART IDENTITY CASCADE')
    audits = []
    monkeypatch.setattr(audit, 'record_bg', lambda username, action, **kw: audits.append((username, action, kw)))
    agent = FakeAgent()
    app = web.Application(middlewares=[fake_auth])
    handlers.register_routes(app, agent.call)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield client, agent, audits
    finally:
        await client.close()
        await pool.close()


async def call(client, method, path, json_body=None, status=200, **params):
    headers = params.pop('headers', None)
    resp = await client.request(method, path, json=json_body, params=params, headers=headers)
    data = await resp.json()
    assert resp.status == status, data
    return data


def cells(row):
    return {k8s: cell['status'] for k8s, cell in row['cells'].items()}


async def setup_group(client, agent):
    """beta-k / beta-p 测试,prod-a 生产;A 在三个集群(hosts 各不同),B 只在测试集群且两边规则不同。"""
    a_routes = [route('/user/', 6602, name='/user', rewrite='/'), route('/api/', 6001)]
    agent.put('beta-k', 'a', vs(['a-beta.x.com', 'a-beta-k.x.com'], a_routes))
    agent.put('beta-p', 'a', vs(['a-beta.x.com', 'a-beta-p.x.com'], a_routes))
    agent.put('prod-a', 'a', vs(['a.x.com'], a_routes))
    agent.put('beta-k', 'b', vs(['b-beta.x.com'], [route('/b1/', 1), route('/b2/', 2)]))
    agent.put('beta-p', 'b', vs(['b-beta.x.com'], [route('/b1/', 1)]))
    created = await call(client, 'POST', '/api/istio/groups', {
        'name': 'cassmall', 'description': '双活',
        'clusters': [{'k8s_name': 'beta-k', 'env_type': 'test'}, {'k8s_name': 'beta-p', 'env_type': 'test'},
                     {'k8s_name': 'prod-a', 'env_type': 'prod', 'remark': '项目A'}],
    })
    return created['data']['id']


async def matrix(client, group_id, **kw):
    data = await call(client, 'GET', '/api/istio/matrix', group_id=group_id, **kw)
    return {row['name']: row for row in data['data']['rows']}


@pytest.mark.asyncio
async def test_collect_compare_import_edit_and_apply(env):
    client, agent, audits = env
    group_id = await setup_group(client, agent)

    collected = await call(client, 'POST', '/api/istio/collect', {'group_id': group_id})
    assert {r['k8s_name']: r['count'] for r in collected['data']} == {'beta-k': 2, 'beta-p': 2, 'prod-a': 1}

    rows = await matrix(client, group_id)
    assert rows['a']['vs_id'] is None and len(rows['a']['versions']) == 1 and rows['a']['coverage'] == ''
    assert [v['clusters'] for v in rows['b']['versions']] == [['beta-k'], ['beta-p']]
    assert rows['b']['coverage'] == 'test_only' and cells(rows['b'])['prod-a'] == S.NONE

    # 纳管:A 三个集群一致;B 以 beta-k 为准,beta-p 规则不同 → 待下发
    imported = await call(client, 'POST', '/api/istio/import', {'group_id': group_id, 'items': [
        {'namespace': NS, 'name': 'a', 'base': 'beta-k', 'clusters': ['beta-k', 'beta-p', 'prod-a']},
        {'namespace': NS, 'name': 'b', 'base': 'beta-k', 'clusters': ['beta-k', 'beta-p']},
        {'namespace': NS, 'name': 'nope', 'base': 'beta-k', 'clusters': ['beta-k']},
    ]})
    results = {r['name']: r for r in imported['data']}
    assert results['a']['success'] and results['b']['success']
    assert not results['nope']['success'] and '刷新现状' in results['nope']['error']
    again = await call(client, 'POST', '/api/istio/import', {'group_id': group_id, 'items': [
        {'namespace': NS, 'name': 'a', 'base': 'beta-k', 'clusters': ['beta-k']}]})
    assert '已经有' in again['data'][0]['error']

    rows = await matrix(client, group_id)
    a_id, b_id = rows['a']['vs_id'], rows['b']['vs_id']
    assert set(cells(rows['a']).values()) == {S.SYNCED}
    assert rows['a']['cells']['prod-a']['hosts'] == ['a.x.com']  # 各集群 hosts 取自己的现状
    assert cells(rows['b']) == {'beta-k': S.SYNCED, 'beta-p': S.PENDING, 'prod-a': S.NONE}
    assert rows['b']['standard_version'] == 'A'

    diff = (await call(client, 'GET', '/api/istio/compare', group_id=group_id, namespace=NS, name='b',
                       left='standard@beta-p', right='cluster@beta-p'))['data']
    assert diff['left']['label'] == '组标准 · beta-p' and diff['diff']['hosts']['same']
    assert diff['diff']['summary']['left_only'] == 1  # 组标准比 beta-p 多一条 /b2/

    # 改组标准:加一条规则 → A 三个集群都待下发
    bad = await call(client, 'POST', f'/api/istio/httproute?vs_id={a_id}', {
        'name': '/boss', 'match_rules': [{'uri': {'prefix': '/boss/'}}], 'forward_type': 'route',
        'forward_detail': [{'destination': {'host': 'boss', 'port': {'number': 80}}}]}, status=400)
    assert '没用重写却设置了名称' in bad['error']
    await call(client, 'POST', f'/api/istio/httproute?vs_id={a_id}', {
        'name': '/order', 'match_rules': [{'uri': {'prefix': '/order/'}}], 'rewrite_rules': {'uri': '/'},
        'forward_type': 'route', 'forward_detail': [{'destination': {'host': 'order', 'port': {'number': 80}}}],
        'timeout': '10s'})
    rows = await matrix(client, group_id)
    assert set(cells(rows['a']).values()) == {S.PENDING} and rows['a']['route_count'] == 3

    # 预览:取集群最新现状对比
    preview = (await call(client, 'POST', '/api/istio/apply/preview', {
        'items': [{'vs_id': a_id, 'k8s_name': 'beta-k'}]}))['data'][0]
    assert preview['exists'] and not preview['same'] and preview['error'] is None
    assert preview['rendered']['hosts'] == ['a-beta.x.com', 'a-beta-k.x.com']
    assert len(preview['rendered']['http']) == 3 and len(preview['live']['http']) == 2
    assert preview['diff']['summary']['right_only'] == 1 and preview['diff']['hosts']['same']

    # 只下发两个测试集群:各自带自己的 hosts
    applied = await call(client, 'POST', '/api/istio/apply', {
        'items': [{'vs_id': a_id, 'k8s_name': 'beta-k'}, {'vs_id': a_id, 'k8s_name': 'beta-p'}]})
    assert all(r['success'] for r in applied['data'])
    bodies = {env_name: body for env_name, body in agent.applied}
    assert bodies['beta-p']['spec']['hosts'] == ['a-beta.x.com', 'a-beta-p.x.com']
    assert bodies['beta-k']['kind'] == 'VirtualService' and bodies['beta-k']['metadata'] == {'name': 'a', 'namespace': NS}
    assert [a[1] for a in audits] == ['istio.apply', 'istio.apply'] and audits[0][2]['success']
    rows = await matrix(client, group_id)
    assert cells(rows['a']) == {'beta-k': S.SYNCED, 'beta-p': S.SYNCED, 'prod-a': S.PENDING}
    assert rows['a']['cells']['beta-k']['applied_by'] == 'admin'

    # 有人直接改了生产集群 → 刷新现状后显示不一致
    agent.put('prod-a', 'a', vs(['a.x.com'], [route('/hotfix/', 1)]))
    await call(client, 'POST', '/api/istio/collect', {'group_id': group_id, 'k8s_names': ['prod-a']})
    rows = await matrix(client, group_id)
    assert cells(rows['a'])['prod-a'] == S.DIFF

    # 以 beta-p 的现状更新 B 的组标准:beta-p 一致,beta-k 变成待下发;hosts 不变
    await call(client, 'POST', '/api/istio/vs/reset', {'vs_id': b_id, 'k8s_name': 'beta-p'})
    rows = await matrix(client, group_id)
    assert cells(rows['b']) == {'beta-k': S.PENDING, 'beta-p': S.SYNCED, 'prod-a': S.NONE}

    # 下发失败:状态不变,错误原样返回
    await call(client, 'PUT', '/api/istio/vs/cluster', {'vs_id': b_id, 'k8s_name': 'beta-k',
                                                       'hosts': ['reject.example.com']})
    failed = (await call(client, 'POST', '/api/istio/apply', {'items': [{'vs_id': b_id, 'k8s_name': 'beta-k'}]}))
    assert not failed['data'][0]['success'] and 'Unprocessable' in failed['data'][0]['error']
    assert audits[-1][2]['success'] is False


@pytest.mark.asyncio
async def test_vs_detail_routes_bindings_and_permissions(env):
    client, agent, _ = env
    group_id = await setup_group(client, agent)
    await call(client, 'POST', '/api/istio/collect', {'group_id': group_id})

    # 集群里已有同名 VS:不能新建,要用纳管
    exists = await call(client, 'POST', '/api/istio/vs', {
        'group_id': group_id, 'namespace': NS, 'name': 'a', 'gateways': ['istio-system/internal'],
        'clusters': [{'k8s_name': 'beta-k', 'hosts': ['x']}]}, status=400)
    assert '纳管' in exists['error']
    bad_name = await call(client, 'POST', '/api/istio/vs', {
        'group_id': group_id, 'namespace': NS, 'name': 'Bad_Name',
        'clusters': [{'k8s_name': 'beta-k', 'hosts': ['x']}]}, status=400)
    assert 'VS 名称' in bad_name['error']

    created = await call(client, 'POST', '/api/istio/vs', {
        'group_id': group_id, 'namespace': NS, 'name': 'b2c.courier-int', 'gateways': ['istio-system/internal'],
        'df_forward_type': 'route', 'df_forward_timeout': '30s',
        'df_forward_detail': [{'destination': {'host': 'web', 'port': {'number': 80}}}],
        'clusters': [{'k8s_name': 'beta-k', 'hosts': ['c-beta.x.com', ' c-beta.x.com ', '']},
                     {'k8s_name': 'prod-a', 'hosts': ['c.x.com']}]})
    vs_id = created['data']['id']
    for prefix in ('/r1/', '/r2/', '/r3/'):
        await call(client, 'POST', f'/api/istio/httproute?vs_id={vs_id}', {
            'match_rules': [{'uri': {'prefix': prefix}}], 'forward_type': 'route',
            'forward_detail': [{'destination': {'host': 'h', 'port': {'number': 1}}}]})
    dup = await call(client, 'POST', f'/api/istio/httproute?vs_id={vs_id}', {
        'match_rules': [{'uri': {'prefix': '/r4/'}}], 'forward_type': 'route', 'priority': 20,
        'forward_detail': [{'destination': {'host': 'h'}}]}, status=400)
    assert '优先级 20' in dup['error']
    bad_timeout = await call(client, 'POST', f'/api/istio/httproute?vs_id={vs_id}', {
        'match_rules': [{'uri': {'prefix': '/r4/'}}], 'forward_type': 'route', 'timeout': '7',
        'forward_detail': [{'destination': {'host': 'h'}}]}, status=400)
    assert '必须带时间单位' in bad_timeout['error']

    detail = (await call(client, 'GET', '/api/istio/vs', vs_id=vs_id))['data']
    assert [r['priority'] for r in detail['routes']] == [10, 20, 30]
    clusters = {c['k8s_name']: c for c in detail['clusters']}
    assert clusters['beta-k']['hosts'] == ['c-beta.x.com'] and clusters['beta-k']['status'] == S.MISSING
    assert clusters['beta-p']['status'] == S.NONE and not clusters['beta-p']['bound']
    assert detail['group'] == {'id': group_id, 'name': 'cassmall'} and detail['lint'] == []

    # 改规则(保留位置)、删规则(优先级重排)、改默认路由
    middle = detail['routes'][1]
    await call(client, 'PUT', f"/api/istio/httproute?route_id={middle['id']}", {
        'name': '/r2', 'match_rules': middle['match_rules'], 'rewrite_rules': {'uri': '/'},
        'forward_type': 'route', 'forward_detail': middle['forward_detail']})
    await call(client, 'DELETE', f"/api/istio/httproute?route_id={detail['routes'][0]['id']}")
    await call(client, 'PUT', f'/api/istio/vs?vs_id={vs_id}', {'gateways': ['istio-system/external']})
    detail = (await call(client, 'GET', '/api/istio/vs', vs_id=vs_id))['data']
    assert [(r['priority'], r['name']) for r in detail['routes']] == [(10, '/r2'), (20, None)]
    assert detail['gateways'] == ['istio-system/external'] and detail['df_forward_type'] is None

    # 只能看 beta-k 的用户:只返回 beta-k;一个都看不到 → 403
    reader = {'X-Test-Role': 'none', 'X-Test-Clusters': json.dumps({'beta-k': 'read'})}
    rows = await matrix(client, group_id, headers=reader)
    assert set(rows['a']['cells']) == {'beta-k'}
    detail = (await call(client, 'GET', '/api/istio/vs', vs_id=vs_id, headers=reader))['data']
    assert [c['k8s_name'] for c in detail['clusters']] == ['beta-k']
    denied = await call(client, 'GET', '/api/istio/compare', group_id=group_id, namespace=NS, name='a',
                        left='cluster@beta-k', right='cluster@prod-a', headers=reader, status=403)
    assert 'prod-a' in denied['error']
    outsider = {'X-Test-Role': 'none', 'X-Test-Clusters': '{}'}
    await call(client, 'GET', '/api/istio/matrix', group_id=group_id, headers=outsider, status=403)
    groups = (await call(client, 'GET', '/api/istio/groups', headers=reader))['data']
    assert [c['k8s_name'] for c in groups['groups'][0]['clusters']] == ['beta-k'] and 'clusters' not in groups

    # 不再部署到某集群;删除组标准
    await call(client, 'DELETE', '/api/istio/vs/cluster', vs_id=vs_id, k8s_name='prod-a')
    await call(client, 'DELETE', '/api/istio/vs/cluster', vs_id=vs_id, k8s_name='prod-a', status=404)
    await call(client, 'DELETE', '/api/istio/vs', vs_id=vs_id)
    await call(client, 'GET', '/api/istio/vs', vs_id=vs_id, status=404)


@pytest.mark.asyncio
async def test_group_membership_rules(env, monkeypatch):
    client, agent, _ = env
    import utils

    async def names():
        return ['beta-k', 'beta-p', 'prod-a', 'prod-b']

    monkeypatch.setattr(utils, 'get_k8s_names', names)
    group_id = await setup_group(client, agent)
    await call(client, 'POST', '/api/istio/collect', {'group_id': group_id})
    await call(client, 'POST', '/api/istio/import', {'group_id': group_id, 'items': [
        {'namespace': NS, 'name': 'a', 'base': 'beta-k', 'clusters': ['beta-k', 'prod-a']}]})

    taken = await call(client, 'POST', '/api/istio/groups', {
        'name': 'other', 'clusters': [{'k8s_name': 'beta-k', 'env_type': 'test'}]}, status=400)
    assert '已属于组 cassmall' in taken['error']
    dup = await call(client, 'POST', '/api/istio/groups', {'name': 'cassmall'}, status=400)
    assert '已存在' in dup['error']
    bad_type = await call(client, 'POST', '/api/istio/groups', {
        'name': 'x', 'clusters': [{'k8s_name': 'prod-b', 'env_type': 'dev'}]}, status=400)
    assert '集群类型' in bad_type['error']

    groups = (await call(client, 'GET', '/api/istio/groups'))['data']
    assert {c['k8s_name']: c['group'] for c in groups['clusters']}['prod-b'] == ''

    # 集群离线:只有它失败
    agent.offline.add('beta-p')
    collected = {r['k8s_name']: r for r in (await call(client, 'POST', '/api/istio/collect',
                                                         {'group_id': group_id}))['data']}
    assert collected['beta-k']['success'] and not collected['beta-p']['success']
    assert '不在线' in collected['beta-p']['error']
    outside = await call(client, 'POST', '/api/istio/collect', {'group_id': group_id, 'k8s_names': ['prod-b']},
                         status=400)
    assert '不在组' in outside['error']

    # 把 prod-a 移出组:它的部署关系和现状一起删掉;调整顺序和类型
    await call(client, 'PUT', f'/api/istio/groups?id={group_id}', {
        'name': 'cassmall', 'clusters': [{'k8s_name': 'beta-p', 'env_type': 'test'},
                                         {'k8s_name': 'beta-k', 'env_type': 'test'},
                                         {'k8s_name': 'prod-b', 'env_type': 'prod'}]})
    rows = await matrix(client, group_id)
    assert list(rows['a']['cells']) == ['beta-p', 'beta-k', 'prod-b']
    assert await db.pg_fetchval("SELECT count(*) FROM istio_vs_snapshot WHERE k8s_name = 'prod-a'") == 0
    assert await db.pg_fetchval("SELECT count(*) FROM istio_vs_cluster WHERE k8s_name = 'prod-a'") == 0

    await call(client, 'DELETE', '/api/istio/groups', id=group_id)
    assert await db.pg_fetchval('SELECT count(*) FROM istio_vs') == 0
    await call(client, 'DELETE', '/api/istio/groups', id=group_id, status=404)


def domain_values(lists):
    return [[item['value'] for item in block] for block in lists]


@pytest.mark.asyncio
async def test_domain_conditions_are_kept_per_cluster(env):
    """规则里的域名条件按集群:只差域名的规则算一致,纳管、新增规则、下发、补设置、重置都各用各的域名。"""
    client, agent, _ = env

    def site_vs(site):
        hosts = [f'svc-{site}.x.com', 'svc.x.com']
        host_rule = {'match': [{'authority': {'exact': h}} for h in hosts],
                     'route': [{'destination': {'host': 'svc.b2c.svc.cluster.local'}}]}
        return vs(hosts, [host_rule, route('/api/', 6001)])

    agent.put('prod-p', 'svc', site_vs('p'))
    agent.put('prod-k', 'svc', site_vs('k'))
    # 测试集群:authority 和 headers.host 两种写法混用
    agent.put('beta-k', 'svc', vs(['svc-beta-p.x.com', 'svc-beta.x.com'], [
        {'match': [{'headers': {'host': {'exact': 'svc-beta-p.x.com'}}}, {'authority': {'exact': 'svc-beta.x.com'}}],
         'route': [{'destination': {'host': 'svc.b2c.svc.cluster.local'}}]}, route('/api/', 6001)]))
    agent.clusters['prod-x'] = {}
    created = await call(client, 'POST', '/api/istio/groups', {'name': 'g', 'clusters': [
        {'k8s_name': 'beta-k', 'env_type': 'test'}, {'k8s_name': 'prod-p', 'env_type': 'prod'},
        {'k8s_name': 'prod-k', 'env_type': 'prod'}, {'k8s_name': 'prod-x', 'env_type': 'prod'}]})
    group_id = created['data']['id']
    await call(client, 'POST', '/api/istio/collect', {'group_id': group_id})

    # 只是域名不同:同一个版本;以 prod-p 为准纳管后各集群都一致(域名取各自的现状)
    assert len((await matrix(client, group_id))['svc']['versions']) == 1
    diff = (await call(client, 'GET', '/api/istio/compare', group_id=group_id, namespace=NS, name='svc',
                       left='cluster@prod-p', right='cluster@prod-k'))['data']['diff']
    assert diff['rules_same'] and not diff['shared_same'] and diff['summary']['domains'] == 1
    await call(client, 'POST', '/api/istio/import', {'group_id': group_id, 'items': [
        {'namespace': NS, 'name': 'svc', 'base': 'prod-p', 'clusters': ['beta-k', 'prod-p', 'prod-k']}]})
    row = (await matrix(client, group_id))['svc']
    vs_id = row['vs_id']
    assert cells(row) == {'beta-k': S.SYNCED, 'prod-p': S.SYNCED, 'prod-k': S.SYNCED, 'prod-x': S.NONE}
    detail = (await call(client, 'GET', '/api/istio/vs', vs_id=vs_id))['data']
    host_rule = detail['routes'][0]
    assert host_rule['match_rules'] == [{S.DOMAIN_KEY: True}]
    assert domain_values(host_rule['domains']['prod-k']) == [['svc-k.x.com', 'svc.x.com']]
    assert [item['field'] for item in host_rule['domains']['beta-k'][0]] == ['host', 'authority']
    assert all(not c['missing_domains'] for c in detail['clusters'] if c['bound']) and detail['lint'] == []

    # 新增一条带域名条件的规则:纳入了的集群都要设置,而且至少一个域名
    rule = {'match_rules': [{'uri': {'prefix': '/admin/'}, S.DOMAIN_KEY: True}], 'forward_type': 'route',
            'forward_detail': [{'destination': {'host': 'admin.b2c.svc.cluster.local'}}],
            'domains': {'beta-k': [[{'value': 'svc-beta.x.com'}]], 'prod-p': [[{'value': 'svc-p.x.com'}]]}}
    path = f'/api/istio/httproute?vs_id={vs_id}'
    assert '请设置集群 prod-k 的域名' in (await call(client, 'POST', path, rule, status=400))['error']
    rule['domains']['prod-k'] = [[]]
    assert '一个域名都没选' in (await call(client, 'POST', path, rule, status=400))['error']
    rule['domains']['prod-k'] = [[{'value': 'svc-k.x.com'}, {'value': ' svc-k.x.com '}]]  # 重复的去掉
    rule['domains']['prod-x'] = [[{'value': 'svc-x.x.com'}]]
    assert '没有部署到集群 prod-x' in (await call(client, 'POST', path, rule, status=400))['error']
    rule['domains'].pop('prod-x')
    await call(client, 'POST', path, rule)
    assert set(cells((await matrix(client, group_id))['svc']).values()) == {S.PENDING, S.NONE}

    # 下发到 prod-k:用的是 prod-k 自己的域名
    await call(client, 'POST', '/api/istio/apply', {'items': [{'vs_id': vs_id, 'k8s_name': 'prod-k'}]})
    env_name, body = agent.applied[-1]
    http = body['spec']['http']
    assert env_name == 'prod-k' and body['spec']['hosts'] == ['svc-k.x.com', 'svc.x.com']
    assert http[0]['match'] == [{'authority': {'exact': 'svc-k.x.com'}}, {'authority': {'exact': 'svc.x.com'}}]
    assert http[2]['match'] == [{'uri': {'prefix': '/admin/'}, 'authority': {'exact': 'svc-k.x.com'}}]
    assert cells((await matrix(client, group_id))['svc'])['prod-k'] == S.SYNCED

    # 部署到新集群只填了 hosts:缺少域名,不能下发;补上域名后才行
    await call(client, 'PUT', '/api/istio/vs/cluster', {'vs_id': vs_id, 'k8s_name': 'prod-x', 'hosts': ['svc-x.x.com']})
    assert cells((await matrix(client, group_id))['svc'])['prod-x'] == S.INCOMPLETE
    item = {'vs_id': vs_id, 'k8s_name': 'prod-x'}
    preview = (await call(client, 'POST', '/api/istio/apply/preview', {'items': [item]}))['data'][0]
    assert '还没设置 prod-x 的域名' in preview['error'] and preview['diff'] is None
    assert '还没设置' in (await call(client, 'POST', '/api/istio/apply', {'items': [item]}, status=400))['error']
    detail = (await call(client, 'GET', '/api/istio/vs', vs_id=vs_id))['data']
    prod_x = next(c for c in detail['clusters'] if c['k8s_name'] == 'prod-x')
    assert len(prod_x['missing_domains']) == 2
    rule_ids = [r['id'] for r in detail['routes'] if r['domains']]
    bad = await call(client, 'PUT', '/api/istio/vs/cluster', {
        'vs_id': vs_id, 'k8s_name': 'prod-x', 'hosts': ['svc-x.x.com'], 'domains': {str(rule_ids[0]): [[]]}}, status=400)
    assert '第 1 条规则一个域名都没选' in bad['error']
    await call(client, 'PUT', '/api/istio/vs/cluster', {'vs_id': vs_id, 'k8s_name': 'prod-x', 'hosts': ['svc-x.x.com'],
                                                       'domains': {str(i): [[{'value': 'svc-x.x.com'}]] for i in rule_ids}})
    assert cells((await matrix(client, group_id))['svc'])['prod-x'] == S.MISSING

    # 集群现状里对应规则的域名:给「部署到此集群」做默认值
    detail = (await call(client, 'GET', '/api/istio/vs', vs_id=vs_id))['data']
    prod_p = next(c for c in detail['clusters'] if c['k8s_name'] == 'prod-p')
    assert domain_values(prod_p['live_domains'][str(rule_ids[0])]) == [['svc-p.x.com', 'svc.x.com']]

    # 修改规则的域名;以 prod-k 现状更新组标准:其他集群原来的域名不变
    admin_rule = detail['routes'][2]
    await call(client, 'PUT', f"/api/istio/httproute?route_id={admin_rule['id']}", {
        **{k: admin_rule[k] for k in ('match_rules', 'forward_type', 'forward_detail')},
        'domains': {**admin_rule['domains'], 'beta-k': [[{'value': 'svc-beta.x.com'}, {'value': 'svc-beta-p.x.com'}]]}})
    await call(client, 'POST', '/api/istio/vs/reset', {'vs_id': vs_id, 'k8s_name': 'prod-k'})
    detail = (await call(client, 'GET', '/api/istio/vs', vs_id=vs_id))['data']
    by_name = {c['k8s_name']: c for c in detail['clusters']}
    assert by_name['prod-k']['status'] == S.SYNCED and by_name['prod-x']['status'] == S.MISSING
    assert domain_values(detail['routes'][0]['domains']['prod-p']) == [['svc-p.x.com', 'svc.x.com']]
    assert domain_values(detail['routes'][2]['domains']['beta-k']) == [['svc-beta.x.com', 'svc-beta-p.x.com']]

    # 不再部署到 prod-x:它在各规则里的域名一起删掉
    await call(client, 'DELETE', '/api/istio/vs/cluster', vs_id=vs_id, k8s_name='prod-x')
    assert await db.pg_fetchval("SELECT count(*) FROM istio_vs_route_domain WHERE k8s_name = 'prod-x'") == 0
